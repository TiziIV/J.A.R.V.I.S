"""
Módulo Principal de J.A.R.V.I.S.

Modos de inicio:
1. Consola primero (`python main.py`):
   - Inicia en modo texto interactivo sin activar el micrófono ni cargar modelos de voz
     (`faster-whisper`, `SpeechToText`, `AudioRecorder`, `WakeWordDetector`, CUDA STT).
2. Consola con voz de salida (`python main.py --tts`):
   - Permite escribir por consola y escuchar las respuestas mediante `TextToSpeech`,
     sin activar el micrófono ni cargar modelos de reconocimiento de voz.
3. Manos libres completo (`python main.py --voice`):
   - Activa el flujo de voz de dos niveles (Modo Reposo con `WakeWordDetector` + Sesión Activa
     Continua con `AudioRecorder`, `SpeechToText`, `TextToSpeech`, `SoundFX` y `JarvisTray`).
   - Si se pasan `--voice` y `--tts` simultáneamente, `--voice` tiene prioridad.
"""

from __future__ import annotations

import argparse
import os
import time
from typing import Any, Callable, Optional, Sequence

from config.prompts import JARVIS_SYSTEM_PROMPT
from config.settings import (
    BRAIN_MODES,
    DEFAULT_BRAIN_MODE,
    DEFAULT_INPUT_MODE,
    SESSION_IDLE_TIMEOUT,
    SESSION_POST_SPEECH_PAUSE,
    SESSION_SILENCE_DURATION,
    SFX_ENABLED,
)
from src.brain.brain_manager import ENGINE_SELECTOR_PROMPT, BrainManager
from src.brain.memory import ConversationMemory
from src.session import ActiveSession, SessionEnd, normalize_text
from src.tools.skill_manager import SkillManager
from src.utils.logger import logger

# Comandos directos para salir de la consola interactiva de forma limpia.
CONSOLE_EXIT_COMMANDS: tuple[str, ...] = ("salir", "exit", "chau", "/salir", "/exit")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """
    Interpreta los argumentos de línea de comandos de J.A.R.V.I.S.

    Prioridad de modos de entrada:
    - Si se indica `--voice`, el modo efectivo es `'VOICE'` (tiene prioridad sobre `--tts`).
    - Si se indica `--tts` (sin `--voice`), el modo efectivo es `'TTS'`.
    - Sin flags, utiliza `DEFAULT_INPUT_MODE` (`'TEXT'` por defecto).
    """
    parser = argparse.ArgumentParser(
        prog="jarvis",
        description="Asistente J.A.R.V.I.S. con arquitectura Multi-Brain (consola primero y voz opcional).",
    )
    parser.add_argument(
        "--tts",
        action="store_true",
        help="Activa síntesis de voz (TTS) para leer las respuestas en modo consola sin usar micrófono.",
    )
    parser.add_argument(
        "--voice",
        action="store_true",
        help="Activa el modo manos libres completo (Wake Word, micrófono, Whisper STT y TTS).",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default=None,
        help="Modo de inteligencia inicial (AUTO, OLLAMA, OLLAMA_UNCENSORED, GEMINI, CLAUDE, GPT, CODEX).",
    )

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.voice:
        args.input_mode = "VOICE"
    elif args.tts:
        args.input_mode = "TTS"
    else:
        args.input_mode = DEFAULT_INPUT_MODE if DEFAULT_INPUT_MODE in ("TEXT", "TTS", "VOICE") else "TEXT"

    args.brain_mode = (args.mode or DEFAULT_BRAIN_MODE).strip().upper()
    return args


def _speak_blocking(tts_instance: Any, text: str) -> None:
    """
    Reproduce un texto por TTS de manera bloqueante para evitar solapamientos o eco acústico.
    """
    if tts_instance is None or not text or not text.strip():
        return
    pending = tts_instance.speak(text)
    if pending is not None:
        pending.result()
    while getattr(tts_instance, "is_speaking", False):
        time.sleep(0.04)


def _resolve_existing_n8_db_path() -> Optional[Any]:
    """
    Resuelve la ruta efectiva de la base de datos N8 sin crearla en disco.
    Respeta el aislamiento de pruebas (`JARVIS_ENV=test`) y `settings.EXPERIENCES_DB_PATH`.
    """
    from pathlib import Path
    from config import settings

    if not settings.N8_ENABLED:
        return None

    env_mode = os.environ.get("JARVIS_ENV", "").strip().lower()
    configured_path = Path(settings.EXPERIENCES_DB_PATH)
    if env_mode in {"test", "testing"}:
        sandbox_root = (Path(settings.BASE_DIR) / "temp" / "jarvis_test_isolated").resolve()
        try:
            configured_resolved = configured_path.resolve()
            configured_resolved.relative_to(sandbox_root)
            candidate = configured_resolved
        except (OSError, ValueError):
            custom_root = os.environ.get("JARVIS_TEST_TEMP_ROOT", "").strip()
            target_root = sandbox_root
            if custom_root:
                try:
                    cand_root = Path(custom_root).resolve()
                    cand_root.relative_to(sandbox_root)
                    target_root = cand_root
                except (OSError, ValueError):
                    target_root = sandbox_root
            candidate = target_root / "experiences.db"
    else:
        candidate = configured_path

    return candidate if candidate.exists() and candidate.is_file() else None


def attach_existing_memory_engine(
    brain_manager: BrainManager,
    *,
    memory_engine: Optional[Any] = None,
) -> Optional[Any]:
    """
    Conecta perezosamente una única instancia de `MemoryEngine` a `brain_manager` cuando
    `settings.N8_ENABLED` está activo y `experiences.db` ya existe en disco (remediación F-09).

    Garantías:
    - Reutiliza cualquier instancia ya existente en `memory_engine` o `brain_manager.memory_engine`.
    - Nunca crea `experiences.db` ni directorios de memoria solo por arrancar J.A.R.V.I.S.
    - Aplica el estado de privacidad actual de `brain_manager.memory` antes de exponer el motor.
    - No importa ningún módulo de audio, STT, TTS ni GPU.
    """
    existing = memory_engine or getattr(brain_manager, "memory_engine", None)
    if existing is not None:
        if getattr(brain_manager, "memory_engine", None) is None:
            brain_manager.memory_engine = existing
        return existing

    db_path = _resolve_existing_n8_db_path()
    if db_path is None:
        return None

    from src.memory.engine import MemoryEngine
    from src.memory.store import ExperienceStore

    conv_mem = getattr(brain_manager, "memory", None)
    priv_now = bool(conv_mem is not None and getattr(conv_mem, "is_private_mode", False) is True)
    allow_hist_now = (
        True
        if not priv_now
        else (conv_mem is None or getattr(conv_mem, "private_allow_history", True) is not False)
    )

    store = ExperienceStore(db_path=db_path)
    engine = MemoryEngine(
        store=store,
        is_private_mode_fn=(
            (lambda: getattr(conv_mem, "is_private_mode", False) is True)
            if conv_mem is not None
            else None
        ),
        allow_history_in_private_fn=(
            (lambda: getattr(conv_mem, "private_allow_history", True) is not False)
            if conv_mem is not None
            else None
        ),
    )
    engine.set_private_mode(enabled=priv_now, allow_history=allow_hist_now)
    brain_manager.memory_engine = engine
    return engine


def run_console_session(
    brain_manager: BrainManager,
    *,
    enable_tts: bool = False,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    tts_factory: Optional[Callable[[], Any]] = None,
    orchestrator: Optional[Any] = None,
    memory_engine: Optional[Any] = None,
    memory_handler: Optional[Any] = None,
    memory_capture_service: Optional[Any] = None,
) -> None:
    """
    Ejecuta el bucle interactivo por consola (modo texto o texto + TTS).

    Importante para el ahorro de recursos (GTX 1660 6 GB VRAM):
    Esta función jamás importa ni inicializa `SpeechToText`, `faster-whisper`,
    `AudioRecorder` ni `WakeWordDetector`. El motor `TextToSpeech` solo se carga
    si `enable_tts=True` o cuando el usuario ejecuta el comando `/voz`.
    """
    tts_active: bool = bool(enable_tts)
    tts_engine: Any = None
    chain_orchestrator: Any = orchestrator
    goal_console_handler: Any = None
    context_engine_instance: Any = None
    context_console_handler: Any = None
    memory_engine_instance: Any = memory_engine or getattr(brain_manager, "memory_engine", None)
    memory_console_handler: Any = memory_handler
    memory_capture_instance: Any = memory_capture_service

    def _is_private_mode_active() -> bool:
        conv_priv = (
            brain_manager.memory is not None
            and getattr(brain_manager.memory, "is_private_mode", False) is True
        )
        eng_priv = (
            memory_engine_instance is not None
            and getattr(memory_engine_instance, "is_private_mode", False) is True
        )
        return conv_priv or eng_priv

    def _allow_history_in_private() -> bool:
        if not _is_private_mode_active():
            return True
        conv_allow = (
            brain_manager.memory is None
            or getattr(brain_manager.memory, "private_allow_history", True) is not False
        )
        eng_allow = (
            memory_engine_instance is None
            or getattr(memory_engine_instance, "allow_history_reads", True) is not False
        )
        return conv_allow and eng_allow

    def _ensure_memory_engine() -> Any:
        nonlocal memory_engine_instance
        if memory_engine_instance is None:
            existing_attached = getattr(brain_manager, "memory_engine", None)
            if existing_attached is not None:
                memory_engine_instance = existing_attached
            else:
                from src.memory.engine import MemoryEngine

                conv_mem = brain_manager.memory
                priv_now = bool(conv_mem is not None and getattr(conv_mem, "is_private_mode", False) is True)
                allow_hist_now = (
                    True
                    if not priv_now
                    else (conv_mem is None or getattr(conv_mem, "private_allow_history", True) is not False)
                )
                memory_engine_instance = MemoryEngine(
                    is_private_mode_fn=(
                        (lambda: getattr(conv_mem, "is_private_mode", False) is True)
                        if conv_mem is not None
                        else None
                    ),
                    allow_history_in_private_fn=(
                        (lambda: getattr(conv_mem, "private_allow_history", True) is not False)
                        if conv_mem is not None
                        else None
                    ),
                )
                memory_engine_instance.set_private_mode(enabled=priv_now, allow_history=allow_hist_now)
                brain_manager.memory_engine = memory_engine_instance
        elif getattr(brain_manager, "memory_engine", None) is None:
            brain_manager.memory_engine = memory_engine_instance
        if goal_console_handler is not None and hasattr(goal_console_handler, "set_memory_engine"):
            goal_console_handler.set_memory_engine(memory_engine_instance)
        return memory_engine_instance

    def _resolve_existing_memory_engine() -> Optional[Any]:
        nonlocal memory_engine_instance
        resolved = attach_existing_memory_engine(
            brain_manager,
            memory_engine=memory_engine_instance,
        )
        if resolved is not None:
            memory_engine_instance = resolved
            if goal_console_handler is not None and hasattr(goal_console_handler, "set_memory_engine"):
                goal_console_handler.set_memory_engine(memory_engine_instance)
        return memory_engine_instance

    def _on_private_mode_changed(enabled: bool, _allow_history: bool) -> None:
        if goal_console_handler is not None:
            if not enabled and hasattr(goal_console_handler, "sync_private_mode_exit"):
                goal_console_handler.sync_private_mode_exit()
            elif getattr(goal_console_handler, "_goal_engine", None) is not None:
                goal_console_handler._goal_engine._reconcile_private_state()
        if (
            not enabled
            and memory_engine_instance is not None
            and hasattr(memory_engine_instance, "flush_pending_rollback_invalidations")
        ):
            try:
                memory_engine_instance.flush_pending_rollback_invalidations()
            except Exception as flush_err:
                logger.warning(f"Aviso drenando invalidaciones N5 pendientes al salir de privado: {flush_err}")

    def _ensure_memory_capture_service() -> Optional[Any]:
        nonlocal memory_capture_instance
        if memory_capture_instance is not None:
            return memory_capture_instance
        from config import settings

        should_attach = bool(
            memory_engine_instance is not None
            or (
                settings.N8_ENABLED
                and (settings.N8_AUTO_CAPTURE_ENABLED or settings.EXPERIENCES_DB_PATH.exists())
            )
        )
        if not should_attach:
            return None
        from src.memory.capture import ExperienceCaptureService

        memory_capture_instance = ExperienceCaptureService(
            memory_engine=_ensure_memory_engine(),
        )
        return memory_capture_instance

    def _ensure_memory_handler() -> Any:
        nonlocal memory_console_handler
        if memory_console_handler is None:
            from src.memory.console import MemoryConsoleHandler

            memory_console_handler = MemoryConsoleHandler(
                memory_engine=_ensure_memory_engine(),
                conversation_memory=brain_manager.memory,
                skill_manager=getattr(brain_manager, "skill_manager", None),
                on_private_mode_changed=_on_private_mode_changed,
                input_fn=input_fn,
                output_fn=output_fn,
            )
        return memory_console_handler

    def _ensure_context_engine() -> Any:
        nonlocal context_engine_instance
        if context_engine_instance is None:
            from src.context.engine import ContextAwarenessEngine

            context_engine_instance = ContextAwarenessEngine()
        return context_engine_instance

    def _ensure_context_handler() -> Any:
        nonlocal context_console_handler
        if context_console_handler is None:
            from src.context.console import ContextConsoleHandler

            context_console_handler = ContextConsoleHandler(
                context_engine=_ensure_context_engine(),
                output_fn=output_fn,
            )
        return context_console_handler

    def _ensure_orchestrator() -> Any:
        nonlocal chain_orchestrator
        if chain_orchestrator is None:
            from src.brain.chain_orchestrator import ChainOrchestrator

            sm = brain_manager.skill_manager
            fe = sm.skills.get("file_editor") if (sm is not None and hasattr(sm, "skills")) else None
            chain_orchestrator = ChainOrchestrator(
                file_editor=fe,
                brain_manager=brain_manager,
                skill_manager=sm,
                memory_capture_service=_ensure_memory_capture_service(),
            )
            if hasattr(chain_orchestrator, "recover_pending_transactions"):
                try:
                    chain_orchestrator.recover_pending_transactions()
                except Exception as rec_err:
                    logger.warning(f"Advertencia al recuperar transacciones pendientes: {rec_err}")
        elif (
            getattr(chain_orchestrator, "memory_capture_service", None) is None
            and hasattr(chain_orchestrator, "set_memory_capture_service")
            and type(chain_orchestrator).__name__ == "ChainOrchestrator"
        ):
            cap_svc = _ensure_memory_capture_service()
            if cap_svc is not None:
                chain_orchestrator.set_memory_capture_service(cap_svc)
        return chain_orchestrator

    def _ensure_goal_handler() -> Any:
        nonlocal goal_console_handler
        if goal_console_handler is None:
            from src.brain.goal_console import GoalConsoleHandler

            goal_console_handler = GoalConsoleHandler(
                skill_manager=getattr(brain_manager, "skill_manager", None),
                brain_manager=brain_manager,
                context_engine=_ensure_context_engine(),
                ensure_orchestrator_fn=_ensure_orchestrator,
                memory_capture_service=_ensure_memory_capture_service(),
                memory_engine=_resolve_existing_memory_engine(),
                is_private_mode_fn=_is_private_mode_active,
                allow_history_in_private_fn=_allow_history_in_private,
                input_fn=input_fn,
                output_fn=output_fn,
            )
        return goal_console_handler

    def _ensure_tts() -> Any:
        nonlocal tts_engine
        if tts_engine is None:
            if tts_factory is not None:
                tts_engine = tts_factory()
            else:
                from src.audio.tts import TextToSpeech

                tts_engine = TextToSpeech()
        return tts_engine

    if tts_active:
        try:
            _ensure_tts()
        except Exception as tts_err:
            logger.warning(f"No se pudo inicializar TTS al arranque: {tts_err}")
            tts_active = False

    output_fn("=======================================================")
    output_fn(" J.A.R.V.I.S. — Consola Interactiva Multi-Brain")
    output_fn(" Comandos: /modo | /voz | /contexto | /memoria | /privado | /goal | /plan | /optimizar | /rollback | /limpiar | salir")
    output_fn("=======================================================")

    try:
        while True:
            priv_badge = ""
            if _is_private_mode_active():
                allow_hist = (
                    brain_manager.memory is None
                    or getattr(brain_manager.memory, "private_allow_history", True) is not False
                )
                priv_badge = " | PRIVADO" if allow_hist else " | PRIVADO:sin-historial"
            prompt_label = f"Tu [Modo: {brain_manager.global_mode}{priv_badge}] > "
            try:
                user_raw = input_fn(prompt_label)
            except (EOFError, KeyboardInterrupt):
                output_fn("\n[i] Finalizando sesión de consola de J.A.R.V.I.S. Hasta pronto, señor.")
                break

            if user_raw is None:
                output_fn("\n[i] Entrada cerrada (EOF). Finalizando sesión de J.A.R.V.I.S.")
                break

            text = user_raw.strip()
            if not text:
                continue

            lowered = text.lower()

            # 1. Comandos de salida controlada
            if lowered in CONSOLE_EXIT_COMMANDS:
                farewell = "Entendido, señor. Cerrando sesión de consola."
                output_fn(f"JARVIS: {farewell}")
                if tts_active and tts_engine is not None:
                    try:
                        _speak_blocking(tts_engine, farewell)
                    except Exception:
                        pass
                break

            # 2. Comando /limpiar -> limpia la memoria conversacional
            if lowered in ("/limpiar", "/clear"):
                if brain_manager.memory is not None:
                    brain_manager.memory.clear()
                msg = "Contexto de conversación limpiado, señor."
                output_fn(f"JARVIS: {msg}")
                if tts_active and tts_engine is not None:
                    try:
                        _speak_blocking(tts_engine, msg)
                    except Exception:
                        pass
                continue

            # 3. Comando /voz -> activa o desactiva TTS de forma diferida
            if lowered in ("/voz", "/tts"):
                tts_active = not tts_active
                if tts_active:
                    try:
                        _ensure_tts()
                        msg = "Síntesis de voz (TTS) activada para la sesión, señor."
                    except Exception as err:
                        tts_active = False
                        msg = f"No fue posible inicializar el sintetizador de voz: {err}"
                else:
                    msg = "Síntesis de voz (TTS) desactivada. Continuamos solo por texto, señor."
                output_fn(f"JARVIS: {msg}")
                if tts_active and tts_engine is not None:
                    try:
                        _speak_blocking(tts_engine, msg)
                    except Exception:
                        pass
                continue

            # 4. Comando /modo [opción] -> consulta o selecciona el motor de inteligencia
            if lowered == "/modo" or lowered.startswith("/modo "):
                arg = text[5:].strip()
                if not arg:
                    brain_manager.awaiting_mode_choice = True
                    modes_str = ", ".join(BRAIN_MODES)
                    output_fn(f"[Modo actual: {brain_manager.global_mode} | Disponibles: {modes_str}]")
                    output_fn(f"JARVIS: {ENGINE_SELECTOR_PROMPT}")
                    if tts_active and tts_engine is not None:
                        try:
                            _speak_blocking(tts_engine, ENGINE_SELECTOR_PROMPT)
                        except Exception:
                            pass
                else:
                    _, _, reply = brain_manager.set_mode_by_choice(arg)
                    output_fn(f"JARVIS: {reply}")
                    if tts_active and tts_engine is not None:
                        try:
                            _speak_blocking(tts_engine, reply)
                        except Exception:
                            pass
                continue

            # 4b. Comando /optimizar <juego o ruta> -> ejecuta simulación y solicita aprobación explícita
            if lowered == "/optimizar" or lowered.startswith("/optimizar "):
                if _is_private_mode_active():
                    output_fn(
                        "JARVIS: Operación '/optimizar' rechazada durante el modo privado porque "
                        "requiere registrar journals y respaldos persistentes en disco. Salga de `/privado` antes, señor."
                    )
                    continue
                target_arg = text[len("/optimizar") :].strip()
                if not target_arg:
                    msg = (
                        "Uso: /optimizar <nombre_del_juego_o_ruta_de_archivo>. "
                        "Ejemplo: /optimizar CyberGame"
                    )
                    output_fn(f"JARVIS: {msg}")
                    continue

                orch = _ensure_orchestrator()
                try:
                    prep = orch.prepare_interactive_optimization(target_arg)
                except Exception as opt_err:
                    output_fn(f"JARVIS: No fue posible preparar la optimización, señor: {opt_err}")
                    continue

                status = prep.get("status")
                if status != "awaiting_approval":
                    msg = str(prep.get("message") or "No se generaron cambios aplicables.")
                    output_fn(f"JARVIS: {msg}")
                    if status == "ambiguous" and prep.get("matches"):
                        for idx_m, m_path in enumerate(prep["matches"], start=1):
                            output_fn(f"  [{idx_m}] {m_path}")
                    continue

                output_fn(str(prep["formatted_preview"]))
                plan = prep["plan"]
                sim = prep["simulation"]
                approval_hash = str(sim["approval_hash"])

                try:
                    confirm_raw = input_fn(
                        "¿Desea aplicar estos cambios? [s/n o IDs ej. op-001]: "
                    )
                except (EOFError, KeyboardInterrupt):
                    orch.cancel_plan(plan.execution_id, reason="Cancelado por interrupción de entrada.")
                    output_fn("\nJARVIS: Operación cancelada sin modificar archivos, señor.")
                    continue

                choice = (confirm_raw or "").strip()
                choice_low = choice.lower()
                if choice_low in ("s", "si", "sí", "y", "yes", "aplicar", "autorizar"):
                    selected_ids = None
                elif choice_low.startswith("op-"):
                    selected_ids = [
                        tok.strip() for tok in choice.split(",") if tok.strip()
                    ]
                else:
                    orch.cancel_plan(plan.execution_id, reason="Rechazado por el usuario en consola.")
                    output_fn(
                        "JARVIS: Plan cancelado, señor. No se realizó ninguna modificación en disco."
                    )
                    continue

                try:
                    auth_token = orch.authorize_plan_interactively(
                        plan,
                        approval_hash=approval_hash,
                        selected_operation_ids=selected_ids,
                        input_fn=input_fn,
                    )
                    exec_res = orch.execute_approved_plan(
                        plan,
                        approval_hash=approval_hash,
                        authorization_token=auth_token,
                        approved_operation_ids=selected_ids,
                    )
                    summary_msg = (
                        f"{exec_res['summary']} "
                        f"(ID de ejecución para rollback: {exec_res['execution_id']})"
                    )
                    output_fn(f"[ChainOrchestrator: {exec_res['state']}]")
                    output_fn(f"JARVIS: {summary_msg}")
                    if tts_active and tts_engine is not None:
                        try:
                            _speak_blocking(tts_engine, summary_msg)
                        except Exception:
                            pass
                except Exception as exec_err:
                    output_fn(
                        f"JARVIS: Se produjo un problema al ejecutar el plan y se protegió el archivo: {exec_err}"
                    )
                continue

            # 4c. Comando /rollback <execution_id> -> restaura los archivos desde sus backups verificados
            if lowered == "/rollback" or lowered.startswith("/rollback "):
                if _is_private_mode_active():
                    output_fn(
                        "JARVIS: Operación '/rollback' rechazada durante el modo privado porque "
                        "modifica el estado persistente del journal transaccional. Salga de `/privado` antes, señor."
                    )
                    continue
                exec_id_arg = text[len("/rollback") :].strip()
                if not exec_id_arg:
                    output_fn(
                        "JARVIS: Por favor indique el ID de ejecución a revertir (ej. /rollback exec-123456), señor."
                    )
                    continue

                orch = _ensure_orchestrator()
                try:
                    rb_res = orch.rollback_execution(exec_id_arg)
                    count_rb = len(rb_res.get("restored_files") or [])
                    msg = (
                        f"Rollback completado con verificación SHA-256 para '{exec_id_arg}' "
                        f"({count_rb} archivo(s) restaurado(s) a su estado original), señor."
                    )
                    output_fn(f"[ChainOrchestrator: {rb_res['state']}]")
                    output_fn(f"JARVIS: {msg}")
                except Exception as rb_err:
                    output_fn(
                        f"JARVIS: No fue posible completar el rollback para '{exec_id_arg}', señor: {rb_err}"
                    )
                continue

            # 4.5 Comandos N6 (/goal y /plan) -> en modo privado opera sobre EphemeralGoalStore en RAM
            if lowered == "/goal" or lowered.startswith("/goal ") or lowered == "/plan" or lowered.startswith("/plan "):
                handler = _ensure_goal_handler()
                goal_reply = handler.handle_command(text, input_fn=input_fn, output_fn=output_fn)
                if tts_active and tts_engine is not None and goal_reply and not _is_private_mode_active():
                    try:
                        _speak_blocking(tts_engine, goal_reply)
                    except Exception:
                        pass
                continue

            # 4.6 Comandos N7 (/contexto y /context) -> observación contextual del entorno en solo lectura
            if (
                lowered == "/contexto"
                or lowered.startswith("/contexto ")
                or lowered == "/context"
                or lowered.startswith("/context ")
            ):
                try:
                    ctx_handler = _ensure_context_handler()
                    ctx_reply = ctx_handler.handle_command(text, output_fn=output_fn)
                except Exception as ctx_err:
                    ctx_reply = f"Consulta contextual rechazada o no disponible: {ctx_err}"
                    output_fn(f"JARVIS: {ctx_reply}")
                if tts_active and tts_engine is not None and ctx_reply:
                    try:
                        _speak_blocking(tts_engine, ctx_reply)
                    except Exception:
                        pass
                continue

            # 4.7 Comandos N8.2 (/memoria, /privado, /incognito) -> ciclo de vida de memoria y modo privado
            if (
                lowered == "/memoria"
                or lowered.startswith("/memoria ")
                or lowered == "/privado"
                or lowered.startswith("/privado ")
                or lowered == "/incognito"
                or lowered.startswith("/incognito ")
            ):
                mem_handler = _ensure_memory_handler()
                mem_reply = mem_handler.handle_command(text, input_fn=input_fn, output_fn=output_fn)
                if tts_active and tts_engine is not None and mem_reply and not _is_private_mode_active():
                    try:
                        _speak_blocking(tts_engine, mem_reply)
                    except Exception:
                        pass
                continue

            # 5. Procesamiento del turno mediante BrainManager (selector -> skills locales -> LLM)
            # Remediación F-09: conectar perezosamente MemoryEngine si `experiences.db` ya existe
            # en disco (sin crearla nunca si no existe y respetando el estado privado vigente).
            _resolve_existing_memory_engine()
            turn_result = brain_manager.process_turn(text, voice_mode=tts_active)

            if turn_result.selection_source == "ONE_SHOT":
                output_fn(
                    f"[One-Shot: {turn_result.effective_mode} | Modo global conservado: {brain_manager.global_mode}]"
                )
            if turn_result.fallback_info:
                output_fn(f"[Fallback] {turn_result.fallback_info}")
            if turn_result.diagnostic_line:
                output_fn(turn_result.diagnostic_line)

            output_fn(f"JARVIS: {turn_result.response}")

            if tts_active and tts_engine is not None:
                try:
                    _speak_blocking(tts_engine, turn_result.response)
                except Exception as speak_err:
                    logger.debug(f"Aviso en locución TTS de consola: {speak_err}")

    finally:
        if goal_console_handler is not None and _is_private_mode_active():
            try:
                goal_console_handler.sync_private_mode_exit()
            except Exception:
                pass
        # Remediación F-05: al cerrar sesión, si existían invalidaciones de rollback N5
        # encoladas durante modo privado, liberar modo privado y drenar solo los IDs N5
        # (sin escribir nunca datos de la conversación privada).
        if memory_engine_instance is not None and hasattr(
            memory_engine_instance, "has_pending_rollback_invalidations"
        ):
            try:
                if memory_engine_instance.has_pending_rollback_invalidations():
                    if brain_manager.memory is not None and getattr(
                        brain_manager.memory, "is_private_mode", False
                    ):
                        brain_manager.memory.exit_private_mode()
                    memory_engine_instance.set_private_mode(enabled=False, allow_history=True)
                    memory_engine_instance.flush_pending_rollback_invalidations()
            except Exception:
                pass
        if tts_engine is not None:
            try:
                tts_engine.close()
            except Exception:
                pass


def run_voice_session(brain_manager: BrainManager) -> None:
    """
    Ejecuta el flujo de voz manos libres completo de dos niveles (`--voice`).
    Realiza importaciones diferidas para que los módulos de micrófono, Whisper y Wake Word
    solo se carguen cuando el usuario elige explícitamente el modo de voz.
    """
    from config.wake_config import WAKE_WORD_THRESHOLD
    from src.audio.recorder import AudioRecorder
    from src.audio.sfx import SoundFX
    from src.audio.stt import SpeechToText
    from src.audio.tts import TextToSpeech
    from src.audio.wake_word import WakeWordDetector
    from src.ui.tray import JarvisTray

    logger.info("=== Inicializando Modo Manos Libres (--voice) de J.A.R.V.I.S. ===")

    wake_detector = WakeWordDetector(threshold=WAKE_WORD_THRESHOLD)
    recorder = AudioRecorder(sample_rate=16000)
    stt = SpeechToText()
    tts = TextToSpeech()
    sfx = SoundFX(enabled=SFX_ENABLED)
    memory = brain_manager.memory or ConversationMemory(max_turns=8)
    skill_manager = brain_manager.skill_manager or SkillManager()
    brain_manager.memory = memory
    brain_manager.skill_manager = skill_manager
    # Remediación F-09: conectar perezosamente MemoryEngine en modo voz si `experiences.db` ya existe.
    attach_existing_memory_engine(brain_manager)

    # System Tray reactivo con callback de limpieza de memoria
    def on_tray_clear_memory() -> None:
        memory.clear()
        logger.info("[Tray] Memoria conversacional reseteada por el usuario.")

    tray = JarvisTray(on_clear_memory=on_tray_clear_memory)
    try:
        tray.start()
    except Exception as tray_error:
        logger.warning(f"No se pudo iniciar el System Tray: {tray_error}")

    # Mantenemos compatibilidad con el IntentRouter clásico para las órdenes habladas ambiguas
    intent_router = brain_manager.intent_router

    logger.info("Sistema J.A.R.V.I.S. en línea y en espera pasiva.")
    logger.info("Diga 'Hey Jarvis' una sola vez para iniciar una conversación continua.")
    logger.info("Protocolos disponibles: 'Protocolo de trabajo', 'Protocolo diagnóstico', 'Protocolo silencio'.")

    def speak_blocking(text: str) -> None:
        _speak_blocking(tts, text)

    def listen_step() -> str | None:
        while tts.is_speaking:
            time.sleep(0.04)

        audio_path = recorder.record_command(
            initial_timeout=SESSION_IDLE_TIMEOUT,
            silence_duration=SESSION_SILENCE_DURATION,
        )
        if not audio_path or not os.path.isfile(audio_path):
            return None

        try:
            transcript = stt.transcribe(audio_path)
            return transcript
        finally:
            if os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except Exception as err:
                    logger.debug(f"Aviso eliminando temporal: {err}")

    def respond_step(user_transcript: str) -> str:
        """
        Procesa una orden hablada dentro de la sesión activa usando BrainManager
        (selector de motor -> habilidades locales -> deliberación/motores con fallback).
        """
        attach_existing_memory_engine(brain_manager)
        result = brain_manager.process_turn(user_transcript, voice_mode=True, sfx=sfx)
        if result.fallback_info:
            print(f"[Fallback] {result.fallback_info}")
        if result.diagnostic_line:
            print(result.diagnostic_line)
        return result.response or "A su disposición, señor."

    try:
        while True:
            if tray.is_exit_requested:
                print("\n[+] Cierre ordenado solicitado desde la bandeja del sistema.")
                tray.set_state("hablando")
                speak_blocking("Entendido señor. Apagando sistemas desde la bandeja del sistema.")
                break

            while tts.is_speaking:
                time.sleep(0.05)
            time.sleep(0.35)

            tray.set_state("reposo")
            print("-" * 55)
            print("[Modo Reposo] En espera pasiva. Diga 'Hey Jarvis' para activar.")

            is_wake_detected = wake_detector.listen_for_wake_word()
            if not is_wake_detected:
                continue

            logger.info("[+] Wake Word detectada. Activando sesión continua...")
            sfx.wake()
            tray.set_state("escuchando")
            print("[Sesión Activa] J.A.R.V.I.S. en línea y escuchando continuamente...")

            session = ActiveSession(
                listen=listen_step,
                respond=respond_step,
                speak=speak_blocking,
                sfx=sfx,
                set_state=tray.set_state,
                should_abort=lambda: tray.is_exit_requested,
                post_speech_pause=SESSION_POST_SPEECH_PAUSE,
            )
            session_result = session.run()

            if session_result == SessionEnd.EXIT:
                logger.info("[Sesión] Salida solicitada por voz.")
                break
            elif session_result == SessionEnd.ABORT:
                logger.info("[Sesión] Cierre abortado por la bandeja del sistema.")
                break
            else:
                logger.info(f"[Sesión] Fin de sesión continua ({session_result.value}). Volviendo a Modo Reposo.")

    except KeyboardInterrupt:
        print("\n\n[i] Interrupción de teclado detectada (Ctrl + C).")
        try:
            wake_detector.stop()
            tray.set_state("hablando")
            speak_blocking("Apagando sistemas por comando de usuario. Que tenga un buen día, señor.")
        except Exception:
            pass

    finally:
        eng_voice = getattr(brain_manager, "memory_engine", None)
        if eng_voice is not None and hasattr(eng_voice, "has_pending_rollback_invalidations"):
            try:
                if eng_voice.has_pending_rollback_invalidations():
                    if getattr(memory, "is_private_mode", False):
                        memory.exit_private_mode()
                    eng_voice.set_private_mode(enabled=False, allow_history=True)
                    eng_voice.flush_pending_rollback_invalidations()
            except Exception:
                pass
        wake_detector.stop()
        tts.close()
        try:
            tray.stop()
        except Exception:
            pass

    print("\n=== Sesión de J.A.R.V.I.S. finalizada correctamente ===")


def main(argv: Optional[Sequence[str]] = None) -> None:
    """
    Punto de entrada principal de J.A.R.V.I.S.
    Inicia por defecto en consola de texto ligera; activa TTS con `--tts` o voz completa con `--voice`.
    """
    args = parse_args(argv)
    logger.info(f"=== Inicializando J.A.R.V.I.S. (Entrada: {args.input_mode} | Cerebro: {args.brain_mode}) ===")

    memory = ConversationMemory(max_turns=8)
    skill_manager = SkillManager()
    brain_manager = BrainManager(
        initial_mode=args.brain_mode,
        skill_manager=skill_manager,
        memory=memory,
        lazy_init=True,
    )

    if args.input_mode == "VOICE":
        run_voice_session(brain_manager)
    else:
        run_console_session(
            brain_manager,
            enable_tts=(args.input_mode == "TTS"),
        )


if __name__ == "__main__":
    main()
