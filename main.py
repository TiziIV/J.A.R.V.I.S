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


def run_console_session(
    brain_manager: BrainManager,
    *,
    enable_tts: bool = False,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    tts_factory: Optional[Callable[[], Any]] = None,
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
    output_fn(" Comandos: /modo | /voz | /limpiar | salir | exit | chau")
    output_fn("=======================================================")

    try:
        while True:
            prompt_label = f"Tu [Modo: {brain_manager.global_mode}] > "
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

            # 5. Procesamiento del turno mediante BrainManager (selector -> skills locales -> LLM)
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
