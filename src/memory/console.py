"""
Interfaz de consola para administración de memoria a largo plazo y modo privado N8.2 (`/memoria`, `/privado`, `/incognito`).

Justificación técnica junior:
Separa completamente la interacción de usuario (validación de argumentos, vistas previas y
confirmaciones explícitas antes de acciones destructivas) de la lógica de negocio de `MemoryEngine`
y de los módulos pesados de audio/voz (Whisper, Micrófono, TTS), permitiendo usar `/memoria` y
`/privado` en consola ligera sin consumir VRAM.
"""

from __future__ import annotations

from pathlib import Path
import shlex
from typing import Any, Callable, Optional

from src.memory.engine import MemoryEngine
from src.memory.models import (
    MemoryCategory,
    MemoryPrivateModeError,
    MemoryRetrievalTarget,
    MemoryScopeContext,
    MemoryScopeType,
    MemoryStatus,
    MemorySubsystemError,
)


_POSITIVE_CONFIRMATIONS: frozenset[str] = frozenset(
    {"s", "si", "sí", "y", "yes", "confirmar", "confirmar purga", "aplicar"}
)


class MemoryConsoleHandler:
    """
    Procesa los comandos `/memoria`, `/privado` y `/incognito` en la consola de J.A.R.V.I.S.
    """

    def __init__(
        self,
        *,
        memory_engine: Optional[MemoryEngine] = None,
        memory_engine_factory: Optional[Callable[[], MemoryEngine]] = None,
        conversation_memory: Any = None,
        skill_manager: Any = None,
        default_import_path: Optional[Path | str] = None,
        on_private_mode_changed: Optional[Callable[[bool, bool], None]] = None,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
    ) -> None:
        self._engine: Optional[MemoryEngine] = memory_engine
        self._memory_engine_factory = memory_engine_factory
        self._conversation_memory: Any = conversation_memory
        self._skill_manager: Any = skill_manager
        self._default_import_path: Optional[Path | str] = default_import_path
        self._on_private_mode_changed: Optional[Callable[[bool, bool], None]] = on_private_mode_changed
        self._input_fn: Callable[[str], str] = input_fn
        self._output_fn: Callable[[str], None] = output_fn
        # Remediación F-04: mantener el estado de privacidad en la propia consola incluso
        # antes de instanciar perezosamente `MemoryEngine` (sin crear la DB innecesariamente).
        if memory_engine is not None:
            self._private_mode: bool = bool(getattr(memory_engine, "is_private_mode", False))
            self._allow_history: bool = (
                True
                if not self._private_mode
                else bool(getattr(memory_engine, "allow_history_reads", True))
            )
        elif conversation_memory is not None:
            self._private_mode = bool(getattr(conversation_memory, "is_private_mode", False))
            self._allow_history = (
                True
                if not self._private_mode
                else (getattr(conversation_memory, "private_allow_history", True) is not False)
            )
        else:
            self._private_mode = False
            self._allow_history = True

    @property
    def engine(self) -> MemoryEngine:
        if self._engine is None:
            if self._memory_engine_factory is not None:
                created_engine = self._memory_engine_factory()
                created_engine.set_private_mode(
                    enabled=self._private_mode,
                    allow_history=self._allow_history,
                )
                self._engine = created_engine
                return created_engine
            conv_mem = self._conversation_memory
            # Propagar de forma inmediata las restricciones de privacidad previas al crear
            # el motor perezoso, cerrando cualquier ventana donde el motor opere en modo normal.
            eff_priv = bool(
                self._private_mode
                or (conv_mem is not None and getattr(conv_mem, "is_private_mode", False) is True)
            )
            eff_allow = True
            if eff_priv:
                conv_allow = conv_mem is None or getattr(conv_mem, "private_allow_history", True) is not False
                eff_allow = bool(self._allow_history and conv_allow)

            created_engine = MemoryEngine(
                is_private_mode_fn=(
                    (lambda: bool(self._private_mode or getattr(conv_mem, "is_private_mode", False) is True))
                    if conv_mem is not None
                    else (lambda: self._private_mode)
                ),
                allow_history_in_private_fn=(
                    (
                        lambda: bool(
                            self._allow_history
                            and (getattr(conv_mem, "private_allow_history", True) is not False)
                        )
                    )
                    if conv_mem is not None
                    else (lambda: self._allow_history)
                ),
            )
            created_engine.set_private_mode(enabled=eff_priv, allow_history=eff_allow)
            self._engine = created_engine
        return self._engine

    def _is_private_active(self) -> bool:
        """Evalúa el estado privado vigente sin instanciar `self.engine` ni crear `experiences.db`."""
        return bool(
            self._private_mode
            or (
                self._conversation_memory is not None
                and getattr(self._conversation_memory, "is_private_mode", False) is True
            )
            or (self._engine is not None and self._engine.is_private_mode is True)
        )

    def _is_history_allowed_now(self) -> bool:
        """Evalúa si la lectura histórica N8 está permitida sin instanciar `self.engine`."""
        if not self._is_private_active():
            return True
        return bool(
            self._allow_history
            and (
                self._conversation_memory is None
                or getattr(self._conversation_memory, "private_allow_history", True) is not False
            )
            and (self._engine is None or self._engine.allow_history_reads is not False)
        )

    def _has_existing_or_live_db(self) -> bool:
        """Comprueba sin escribir en disco si ya existe un motor vivo o un archivo `experiences.db`."""
        if self._engine is not None:
            return True
        try:
            from src.memory.store import _default_db_path

            cand = _default_db_path()
            return cand.exists() and cand.is_file()
        except Exception:
            return False

    def _reconcile_existing_db_if_present(self) -> None:
        """
        Si la base N8 ya existe en disco durante `--sin-historial`, ejecuta únicamente la
        reconciliación obligatoria de seguridad N5/N8 sin exponer recuerdos ni crear DB nueva.
        """
        if self._has_existing_or_live_db():
            eng = self.engine
            if hasattr(eng, "_ensure_pending_invalidations_applied_for_read"):
                eng._ensure_pending_invalidations_applied_for_read()

    @staticmethod
    def is_memory_command(text: str) -> bool:
        """Indica si el comando pertenece a `/memoria`, `/privado` o `/incognito`."""
        stripped = (text or "").strip().lower()
        for prefix in ("/memoria", "/privado", "/incognito"):
            if stripped == prefix or stripped.startswith(f"{prefix} "):
                return True
        return False

    def _sync_private_state(self, *, enabled: bool, allow_history: bool) -> None:
        self._private_mode = bool(enabled)
        self._allow_history = True if not enabled else bool(allow_history)

        if self._conversation_memory is not None:
            if enabled:
                if hasattr(self._conversation_memory, "enter_private_mode"):
                    self._conversation_memory.enter_private_mode(allow_history=allow_history)
            else:
                if hasattr(self._conversation_memory, "exit_private_mode"):
                    self._conversation_memory.exit_private_mode()

        if self._engine is not None:
            self._engine.set_private_mode(enabled=self._private_mode, allow_history=self._allow_history)

        if self._skill_manager is not None and hasattr(self._skill_manager, "set_private_mode"):
            self._skill_manager.set_private_mode(self._private_mode)

        if self._on_private_mode_changed is not None:
            try:
                self._on_private_mode_changed(self._private_mode, self._allow_history)
            except Exception:
                pass

    def _ask_confirmation(
        self,
        prompt: str,
        *,
        active_in: Callable[[str], str],
    ) -> tuple[bool, str]:
        try:
            raw = active_in(prompt)
        except (EOFError, KeyboardInterrupt):
            return False, ""
        answer = (raw or "").strip().lower()
        return answer in _POSITIVE_CONFIRMATIONS, answer

    @staticmethod
    def _help_text() -> str:
        return (
            "Comandos de memoria a largo plazo (N8.2) y privacidad:\n"
            "  /memoria ayuda                       -> Muestra esta ayuda.\n"
            "  /memoria buscar <consulta>           -> Recupera recuerdos activos con ranking determinista.\n"
            "  /memoria listar [pendientes|revocados|todos] -> Lista recuerdos según estado.\n"
            "  /memoria ver <id>                    -> Muestra detalle completo, estado y revisiones.\n"
            "  /memoria corregir <id> [texto]       -> Crea nueva revisión inmutable preservando historial.\n"
            "  /memoria confirmar <id>              -> Confirma un recuerdo pendiente o activo.\n"
            "  /memoria olvidar <id> [motivo]       -> Revoca lógicamente (excluye de búsqueda y FTS5).\n"
            "  /memoria purgar <id>                 -> Eliminación física SQLite (requiere confirmación).\n"
            "  /memoria importar-preferencias [ruta]-> Previsualiza e importa preferencias de memory_store.json.\n"
            "  /memoria backup                      -> Crea backup consistente validado con SHA-256.\n"
            "  /memoria restaurar [id|num]          -> Restaura backup validado (requiere confirmación).\n"
            "  /privado                             -> Activa/desactiva modo privado (opción A: con lectura histórica).\n"
            "  /privado --sin-historial             -> Modo privado aislado sin lectura de historial ni N8.\n"
            "  /incognito [--sin-historial]         -> Alias de /privado."
        )

    def handle_command(
        self,
        text: str,
        *,
        input_fn: Optional[Callable[[str], str]] = None,
        output_fn: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Despacha `/memoria`, `/privado` e `/incognito` validando entradas y confirmaciones."""
        active_in = input_fn or self._input_fn
        active_out = output_fn or self._output_fn
        stripped = (text or "").strip()
        lowered = stripped.lower()

        try:
            if (
                lowered == "/privado"
                or lowered.startswith("/privado ")
                or lowered == "/incognito"
                or lowered.startswith("/incognito ")
            ):
                return self._handle_private_command(stripped, active_out=active_out)

            if lowered == "/memoria" or lowered.startswith("/memoria "):
                rest = stripped[len("/memoria") :].strip()
                return self._handle_memory_command(
                    rest,
                    active_in=active_in,
                    active_out=active_out,
                )

            msg = "[Error] Comando de memoria no reconocido. Use `/memoria ayuda`."
            active_out(f"JARVIS: {msg}")
            return msg

        except MemoryPrivateModeError as exc:
            msg = f"[Bloqueado - Modo Privado] {exc}"
            active_out(f"JARVIS: {msg}")
            return msg
        except MemorySubsystemError as exc:
            msg = f"[Error] {exc}"
            active_out(f"JARVIS: {msg}")
            return msg
        except Exception:
            # Nunca exponemos tracebacks internos ni mensajes sin sanitizar al usuario.
            msg = "[Error] No fue posible completar la operación sobre memoria N8. Verifique los parámetros e intente nuevamente."
            active_out(f"JARVIS: {msg}")
            return msg

    def _handle_private_command(
        self,
        full_cmd: str,
        *,
        active_out: Callable[[str], None],
    ) -> str:
        parts = full_cmd.split()
        args = [p.lower() for p in parts[1:]]

        current_priv = (
            self._private_mode
            or (self._conversation_memory is not None and getattr(self._conversation_memory, "is_private_mode", False) is True)
            or (self._engine is not None and self._engine.is_private_mode is True)
        )
        current_allow_hist = (
            self._allow_history
            and (self._conversation_memory is None or getattr(self._conversation_memory, "private_allow_history", True) is not False)
            and (self._engine is None or self._engine.allow_history_reads is not False)
        )

        if args == ["estado"] or args == ["status"]:
            if not current_priv:
                msg = "[Estado] Modo privado INACTIVO. Persistencia normal habilitada."
            elif current_allow_hist:
                msg = "[Estado] Modo privado ACTIVO (lectura histórica permitida; escrituras bloqueadas)."
            else:
                msg = "[Estado] Modo privado ACTIVO (--sin-historial: lectura y escritura bloqueadas)."
            active_out(f"JARVIS: {msg}")
            return msg

        if args in (["off"], ["salir"], ["desactivar"]):
            self._sync_private_state(enabled=False, allow_history=True)
            msg = "[Confirmación] Modo privado desactivado. Buffers temporales de RAM descartados sin persistir en disco."
            active_out(f"JARVIS: {msg}")
            return msg

        if "--sin-historial" in args or "--no-history" in args:
            unknown = [a for a in args if a not in {"--sin-historial", "--no-history", "on", "activar"}]
            if unknown:
                msg = f"[Error] Argumento no válido para modo privado: {' '.join(unknown)}"
                active_out(f"JARVIS: {msg}")
                return msg
            self._sync_private_state(enabled=True, allow_history=False)
            msg = (
                "[Confirmación] Modo privado SIN HISTORIAL activado. "
                "Aislamiento completo en RAM: escrituras bloqueadas y recuerdos históricos/N8 desactivados para responder."
            )
            active_out(f"JARVIS: {msg}")
            return msg

        if not args:
            # Toggle limpio: si estaba activo -> salir y descartar buffers; si estaba apagado -> activar Opción A.
            if current_priv:
                self._sync_private_state(enabled=False, allow_history=True)
                msg = "[Confirmación] Modo privado desactivado. Buffers privados de RAM descartados sin tocar el historial previo."
            else:
                self._sync_private_state(enabled=True, allow_history=True)
                msg = (
                    "[Confirmación] Modo privado activado. "
                    "Las escrituras N8, auto-save y registros persistentes quedan bloqueados; "
                    "la consulta de recuerdos anteriores permanece permitida."
                )
            active_out(f"JARVIS: {msg}")
            return msg

        if args in (["on"], ["activar"]):
            self._sync_private_state(enabled=True, allow_history=True)
            msg = (
                "[Confirmación] Modo privado activado. "
                "Escrituras bloqueadas en JSON/SQLite; mensajes retenidos solo en RAM."
            )
            active_out(f"JARVIS: {msg}")
            return msg

        msg = "[Error] Uso inválido. Ejemplos: `/privado`, `/privado --sin-historial`, `/privado off`, `/incognito`."
        active_out(f"JARVIS: {msg}")
        return msg

    def _handle_memory_command(
        self,
        rest: str,
        *,
        active_in: Callable[[str], str],
        active_out: Callable[[str], None],
    ) -> str:
        if not rest:
            msg = self._help_text()
            active_out(f"JARVIS:\n{msg}")
            return msg

        tokens = rest.split(maxsplit=1)
        subcmd = tokens[0].lower()
        arg_str = tokens[1].strip() if len(tokens) > 1 else ""

        if subcmd in {"ayuda", "help", "?"}:
            msg = self._help_text()
            active_out(f"JARVIS:\n{msg}")
            return msg

        if subcmd == "buscar":
            return self._cmd_search(arg_str, active_out=active_out)

        if subcmd == "listar":
            return self._cmd_list(arg_str, active_out=active_out)

        if subcmd == "ver":
            return self._cmd_view(arg_str, active_out=active_out)

        if subcmd == "corregir":
            return self._cmd_correct(arg_str, active_in=active_in, active_out=active_out)

        if subcmd == "confirmar":
            return self._cmd_confirm(arg_str, active_out=active_out)

        if subcmd == "olvidar":
            return self._cmd_forget(arg_str, active_out=active_out)

        if subcmd == "purgar":
            return self._cmd_purge(arg_str, active_in=active_in, active_out=active_out)

        if subcmd == "importar-preferencias":
            return self._cmd_import_preferences(arg_str, active_in=active_in, active_out=active_out)

        if subcmd == "backup":
            if arg_str:
                msg = "[Error] `/memoria backup` no acepta argumentos posicionales."
                active_out(f"JARVIS: {msg}")
                return msg
            if self._is_private_active():
                raise MemoryPrivateModeError(
                    "Operación de escritura bloqueada en memoria N8 porque el modo privado está activo."
                )
            info = self.engine.create_backup(actor="console_user")
            msg = (
                f"[Confirmación] Backup creado y verificado correctamente.\n"
                f"  ID: {info.backup_id}\n"
                f"  Recuerdos: {info.memory_count} | Esquema: v{info.schema_version}\n"
                f"  Tamaño: {info.size_bytes} bytes | SHA-256: {info.sha256}\n"
                f"  Archivo: {info.backup_path}"
            )
            active_out(f"JARVIS:\n{msg}")
            return msg

        if subcmd == "restaurar":
            return self._cmd_restore(arg_str, active_in=active_in, active_out=active_out)

        msg = f"[Error] Subcomando '/memoria {subcmd}' desconocido. Use `/memoria ayuda`."
        active_out(f"JARVIS: {msg}")
        return msg

    def _cmd_search(self, arg_str: str, *, active_out: Callable[[str], None]) -> str:
        if not arg_str:
            msg = "[Error] Uso: /memoria buscar <consulta> [--voz] [--proyecto <clave> | --app <clave>]"
            active_out(f"JARVIS: {msg}")
            return msg

        try:
            parts = shlex.split(arg_str)
        except ValueError:
            parts = arg_str.split()

        target = MemoryRetrievalTarget.NORMAL
        project_key: str | None = None
        app_key: str | None = None
        query_terms: list[str] = []

        idx = 0
        while idx < len(parts):
            tok = parts[idx]
            low = tok.lower()
            if low == "--voz":
                target = MemoryRetrievalTarget.VOICE
            elif low == "--proyecto" and idx + 1 < len(parts):
                project_key = parts[idx + 1]
                idx += 1
            elif low == "--app" and idx + 1 < len(parts):
                app_key = parts[idx + 1]
                idx += 1
            else:
                query_terms.append(tok)
            idx += 1

        query = " ".join(query_terms).strip()
        if not query:
            msg = "[Error] Debe indicar una consulta textual válida para buscar."
            active_out(f"JARVIS: {msg}")
            return msg

        scope_ctx = (
            MemoryScopeContext(project_key=project_key, app_or_game_key=app_key, include_global=True)
            if (project_key or app_key)
            else None
        )
        if not self._is_history_allowed_now():
            self._reconcile_existing_db_if_present()
            msg = "[Advertencia] Lectura histórica bloqueada porque `/privado --sin-historial` está activo."
            active_out(f"JARVIS: {msg}")
            return msg
        if self._is_private_active() and not self._has_existing_or_live_db():
            msg = f"No se encontraron recuerdos activos relevantes para '{query}'."
            active_out(f"JARVIS: {msg}")
            return msg

        res = self.engine.retrieve(query, target=target, context_scope=scope_ctx)
        if res.blocked_by_history_policy:
            msg = "[Advertencia] Lectura histórica bloqueada porque `/privado --sin-historial` está activo."
            active_out(f"JARVIS: {msg}")
            return msg
        if not res.items:
            msg = f"No se encontraron recuerdos activos relevantes para '{query}'."
            active_out(f"JARVIS: {msg}")
            return msg

        lines = [
            f"Resultados N8 ({res.returned_count} de {res.total_candidates} | {res.total_chars}/{res.budget_max_chars} chars | backend={res.backend_used}):"
        ]
        for item in res.items:
            r = item.record
            scope_txt = r.scope_type.value if r.scope_type == MemoryScopeType.GLOBAL else f"{r.scope_type.value}:{r.scope_key}"
            lines.append(
                f"  - [{r.memory_id}] ({r.category.value} | {scope_txt} | {r.source.value} | score={item.score:.3f}) {r.title}: {r.summary}"
            )
        msg = "\n".join(lines)
        active_out(f"JARVIS:\n{msg}")
        return msg

    def _cmd_list(self, arg_str: str, *, active_out: Callable[[str], None]) -> str:
        opt = (arg_str or "").strip().lower()
        status_filter: MemoryStatus | None = MemoryStatus.ACTIVE
        pending_only = False

        if opt in {"pendientes", "--pendientes", "pending"}:
            status_filter = MemoryStatus.ACTIVE
            pending_only = True
        elif opt in {"revocados", "--revocados", "revoked"}:
            status_filter = MemoryStatus.REVOKED
        elif opt in {"expirados", "--expirados", "expired"}:
            status_filter = MemoryStatus.EXPIRED
        elif opt in {"todos", "--todos", "all"}:
            status_filter = None
        elif opt and opt not in {"activos", "--activos", "active"}:
            msg = "[Error] Filtro inválido para `/memoria listar`. Opciones: activos | pendientes | revocados | expirados | todos."
            active_out(f"JARVIS: {msg}")
            return msg

        if not self._is_history_allowed_now():
            self._reconcile_existing_db_if_present()
            msg = "[Advertencia] Lectura histórica bloqueada porque `/privado --sin-historial` está activo."
            active_out(f"JARVIS: {msg}")
            return msg
        if self._is_private_active() and not self._has_existing_or_live_db():
            msg = "No hay recuerdos que coincidan con el filtro solicitado."
            active_out(f"JARVIS: {msg}")
            return msg

        records = self.engine.list_memories(status=status_filter, pending_only=pending_only, limit=50)
        if not records:
            msg = "No hay recuerdos que coincidan con el filtro solicitado."
            active_out(f"JARVIS: {msg}")
            return msg

        lines = [f"Listado de recuerdos ({len(records)}):"]
        for r in records:
            badge = " [PENDIENTE]" if r.is_pending_confirmation else ""
            scope_txt = r.scope_type.value if r.scope_type == MemoryScopeType.GLOBAL else f"{r.scope_type.value}:{r.scope_key}"
            lines.append(
                f"  - {r.memory_id} | {r.status.value}{badge} | {r.category.value} | {scope_txt} | {r.title}"
            )
        msg = "\n".join(lines)
        active_out(f"JARVIS:\n{msg}")
        return msg

    def _cmd_view(self, arg_str: str, *, active_out: Callable[[str], None]) -> str:
        mem_id = (arg_str or "").strip()
        if not mem_id or " " in mem_id:
            msg = "[Error] Uso: /memoria ver <memory_id>"
            active_out(f"JARVIS: {msg}")
            return msg

        if not self._is_history_allowed_now():
            self._reconcile_existing_db_if_present()
            msg = "[Advertencia] Lectura histórica bloqueada porque `/privado --sin-historial` está activo."
            active_out(f"JARVIS: {msg}")
            return msg
        if self._is_private_active() and not self._has_existing_or_live_db():
            msg = f"[Error] No se encontró ningún recuerdo con ID '{mem_id}'."
            active_out(f"JARVIS: {msg}")
            return msg

        rec = self.engine.get_memory(mem_id)
        if rec is None:
            msg = f"[Error] No se encontró ningún recuerdo con ID '{mem_id}'."
            active_out(f"JARVIS: {msg}")
            return msg

        revs = self.engine.get_revisions(mem_id)
        pending_note = " (PENDIENTE DE CONFIRMACIÓN)" if rec.is_pending_confirmation else ""
        scope_txt = rec.scope_type.value if rec.scope_type == MemoryScopeType.GLOBAL else f"{rec.scope_type.value}:{rec.scope_key}"
        lines = [
            f"Detalle de recuerdo [{rec.memory_id}]:",
            f"  Estado: {rec.status.value}{pending_note} | Categoría: {rec.category.value} | Fuente: {rec.source.value}",
            f"  Ámbito: {scope_txt} | Confianza: {rec.confidence:.2f} | Importancia: {rec.importance}/100",
            f"  Revisión actual: #{rec.revision_number} (históricas: {len(revs)})",
            f"  Creado: {rec.created_at} | Actualizado: {rec.updated_at}",
            f"  Título: {rec.title}",
            f"  Contenido: {rec.content}",
        ]
        if rec.tags:
            lines.append(f"  Tags: {', '.join(rec.tags)}")
        if revs:
            lines.append(f"  Último cambio: rev #{revs[-1].revision_number} ({revs[-1].change_reason} por {revs[-1].changed_by})")
        msg = "\n".join(lines)
        active_out(f"JARVIS:\n{msg}")
        return msg

    def _cmd_correct(
        self,
        arg_str: str,
        *,
        active_in: Callable[[str], str],
        active_out: Callable[[str], None],
    ) -> str:
        parts = arg_str.split(maxsplit=1) if arg_str else []
        if not parts:
            msg = "[Error] Uso: /memoria corregir <memory_id> [nuevo contenido]"
            active_out(f"JARVIS: {msg}")
            return msg
        if self._is_private_active():
            raise MemoryPrivateModeError(
                "Operación de escritura bloqueada en memoria N8 porque el modo privado está activo."
            )

        mem_id = parts[0].strip()
        existing = self.engine.get_memory(mem_id)
        if existing is None:
            msg = f"[Error] No se encontró ningún recuerdo con ID '{mem_id}'."
            active_out(f"JARVIS: {msg}")
            return msg

        new_content = parts[1].strip() if len(parts) > 1 else ""
        if not new_content:
            active_out(f"[Actual ({mem_id})]: {existing.content}")
            try:
                new_content = (active_in("Ingrese el nuevo contenido corregido (Enter para cancelar): ") or "").strip()
            except (EOFError, KeyboardInterrupt):
                new_content = ""
            if not new_content:
                msg = "[Advertencia] Corrección cancelada sin modificar el recuerdo."
                active_out(f"JARVIS: {msg}")
                return msg

        updated = self.engine.correct_memory(
            mem_id,
            content=new_content,
            reason="Corrección manual desde consola /memoria corregir",
            actor="console_user",
        )
        msg = (
            f"[Confirmación] Recuerdo '{updated.memory_id}' corregido con nueva revisión #{updated.revision_number} "
            f"(se conserva el historial previo en memory_revisions)."
        )
        active_out(f"JARVIS: {msg}")
        return msg

    def _cmd_confirm(self, arg_str: str, *, active_out: Callable[[str], None]) -> str:
        mem_id = (arg_str or "").strip()
        if not mem_id or " " in mem_id:
            msg = "[Error] Uso: /memoria confirmar <memory_id>"
            active_out(f"JARVIS: {msg}")
            return msg
        if self._is_private_active():
            raise MemoryPrivateModeError(
                "Operación de escritura bloqueada en memoria N8 porque el modo privado está activo."
            )

        rec = self.engine.confirm_memory(mem_id, actor="console_user")
        msg = (
            f"[Confirmación] Recuerdo '{rec.memory_id}' confirmado "
            f"(revisión #{rec.revision_number}, confianza={rec.confidence:.2f})."
        )
        active_out(f"JARVIS: {msg}")
        return msg

    def _cmd_forget(self, arg_str: str, *, active_out: Callable[[str], None]) -> str:
        parts = arg_str.split(maxsplit=1) if arg_str else []
        if not parts:
            msg = "[Error] Uso: /memoria olvidar <memory_id> [motivo]"
            active_out(f"JARVIS: {msg}")
            return msg
        if self._is_private_active():
            raise MemoryPrivateModeError(
                "Operación de escritura bloqueada en memoria N8 porque el modo privado está activo."
            )

        mem_id = parts[0].strip()
        reason = parts[1].strip() if len(parts) > 1 else "Olvidado por instrucción del usuario (/memoria olvidar)"
        rec = self.engine.revoke_memory(mem_id, reason=reason, actor="console_user")
        msg = (
            f"[Confirmación] Recuerdo '{rec.memory_id}' revocado lógicamente ({rec.status.value}) "
            "y excluido de recuperación e índice FTS5."
        )
        active_out(f"JARVIS: {msg}")
        return msg

    def _cmd_purge(
        self,
        arg_str: str,
        *,
        active_in: Callable[[str], str],
        active_out: Callable[[str], None],
    ) -> str:
        mem_id = (arg_str or "").strip()
        if not mem_id or " " in mem_id:
            msg = "[Error] Uso: /memoria purgar <memory_id>"
            active_out(f"JARVIS: {msg}")
            return msg

        # Verificamos primero bloqueo de modo privado sin instanciar `self.engine` (H-07).
        if self._is_private_active():
            raise MemoryPrivateModeError("No se permite purgar registros durante el modo privado.")

        rec = self.engine.get_memory(mem_id)
        if rec is None:
            msg = f"[Error] No se encontró ningún recuerdo con ID '{mem_id}'."
            active_out(f"JARVIS: {msg}")
            return msg

        revs = self.engine.get_revisions(mem_id)
        active_out(
            "[Advertencia - Eliminación física en SQLite]\n"
            f"  Objetivo: {rec.memory_id} | Título: {rec.title}\n"
            f"  Se eliminarán de SQLite: registro principal, índice FTS5, {len(revs)} revisión(es) y entradas de auditoría asociadas.\n"
            "  Nota: En SSD/NTFS, Shadow Copies (VSS) o backups previos podrían conservar bloques históricos."
        )
        ok, _ = self._ask_confirmation(
            "¿Confirma la purga física irreversible en SQLite? [s/n]: ",
            active_in=active_in,
        )
        if not ok:
            msg = "[Advertencia] Purga cancelada por el usuario. No se modificó la base de datos."
            active_out(f"JARVIS: {msg}")
            return msg

        purge_res = self.engine.purge_memory(
            mem_id,
            confirmed=True,
            vacuum=False,
            reason="Purga física confirmada desde /memoria purgar",
            actor="console_user",
        )
        msg = (
            f"[Confirmación] Recuerdo '{purge_res.memory_id}' purgado físicamente "
            f"(revisiones eliminadas={purge_res.revisions_deleted}, "
            f"auditoría depurada={purge_res.audit_rows_deleted}, wal_checkpoint={purge_res.wal_checkpointed}).\n"
            f"  Aviso: {purge_res.limitations_notice}"
        )
        active_out(f"JARVIS:\n{msg}")
        return msg

    def _cmd_import_preferences(
        self,
        arg_str: str,
        *,
        active_in: Callable[[str], str],
        active_out: Callable[[str], None],
    ) -> str:
        if self._is_private_active():
            raise MemoryPrivateModeError(
                "La importación de preferencias modifica `experiences.db` y está bloqueada en modo privado."
            )
        cleaned_arg = arg_str.strip().strip('"').strip("'").strip()
        raw_path: Optional[Path | str] = cleaned_arg if cleaned_arg else self._default_import_path
        preview = self.engine.preview_preferences_import(raw_path)
        if not preview.valid:
            msg = f"[Error] No se puede importar desde '{preview.source_path}': {preview.error}"
            active_out(f"JARVIS: {msg}")
            return msg

        lines = [
            f"Vista previa de importación (solo lectura) desde '{preview.source_path}':",
            f"  Nuevas: {len(preview.new_items)} | Duplicadas: {len(preview.duplicates)} | Conflictos: {len(preview.conflicts)} | Rechazadas: {len(preview.rejected)}",
        ]
        for item in preview.new_items:
            lines.append(f"    + [NUEVA] {item.key}: {item.value_summary}")
        for item in preview.conflicts:
            lines.append(f"    ! [CONFLICTO] {item.key}: {item.value_summary} ({item.reason})")
        for item in preview.rejected:
            lines.append(f"    x [RECHAZADA] {item.key}: {item.reason}")
        active_out("\n".join(lines))

        if not preview.new_items and not preview.conflicts:
            msg = "[Confirmación] No hay preferencias nuevas ni conflictos aplicables para importar."
            active_out(f"JARVIS: {msg}")
            return msg

        if self.engine.is_private_mode:
            raise MemoryPrivateModeError(
                "La importación de preferencias modifica `experiences.db` y está bloqueada en modo privado."
            )

        prompt_txt = (
            "¿Confirmar importación de nuevas preferencias? [s/n"
            + (" | 'sobrescribir' para reemplazar conflictos]" if preview.conflicts else "]")
            + ": "
        )
        ok, raw_ans = self._ask_confirmation(prompt_txt, active_in=active_in)
        resolve_mode = "skip"
        if raw_ans in {"sobrescribir", "sustituir", "supersede"}:
            ok = True
            resolve_mode = "supersede"
        if not ok:
            msg = "[Advertencia] Importación cancelada. No se realizó ninguna escritura."
            active_out(f"JARVIS: {msg}")
            return msg

        res = self.engine.commit_preferences_import(
            raw_path,
            confirmed=True,
            resolve_conflicts=resolve_mode,
            actor="console_user",
        )
        msg = (
            f"[Confirmación] Importación completada sin alterar el JSON origen: "
            f"importadas={res.imported_count}, duplicadas_omitidas={res.skipped_duplicates}, "
            f"conflictos_sustituidos={res.conflicts_resolved}, conflictos_omitidos={res.conflicts_skipped}, "
            f"rechazadas={res.rejected_count}."
        )
        active_out(f"JARVIS: {msg}")
        return msg

    def _cmd_restore(
        self,
        arg_str: str,
        *,
        active_in: Callable[[str], str],
        active_out: Callable[[str], None],
    ) -> str:
        if self._is_private_active():
            raise MemoryPrivateModeError("No se permite restaurar la base de memoria durante el modo privado.")

        backups = self.engine.list_backups()
        if not backups:
            msg = "[Advertencia] No hay backups disponibles para restaurar."
            active_out(f"JARVIS: {msg}")
            return msg

        listing = ["Backups disponibles para restauración (ordenados del más reciente al más antiguo):"]
        for idx, bkp in enumerate(backups, start=1):
            listing.append(
                f"  [{idx}] {bkp.backup_id} | {bkp.created_at} | recuerdos={bkp.memory_count} | sha256={bkp.sha256[:16]}..."
            )
        active_out("\n".join(listing))

        selection = (arg_str or "").strip()
        if not selection:
            try:
                selection = (
                    active_in("Indique el número [1..N] o ID del backup a restaurar (Enter para cancelar): ")
                    or ""
                ).strip()
            except (EOFError, KeyboardInterrupt):
                selection = ""
            if not selection:
                msg = "[Advertencia] Restauración cancelada sin cambios."
                active_out(f"JARVIS: {msg}")
                return msg

        target_ref: str = selection
        if selection.isdigit():
            pos = int(selection)
            if 1 <= pos <= len(backups):
                target_ref = backups[pos - 1].backup_id
            else:
                msg = f"[Error] Selección fuera de rango (1..{len(backups)})."
                active_out(f"JARVIS: {msg}")
                return msg

        active_out(
            f"[Advertencia] Se verificará SHA-256 + integridad SQLite de '{target_ref}', "
            "se preservará una copia recuperable de la base actual y se reemplazará de forma atómica."
        )
        ok, _ = self._ask_confirmation(
            "¿Confirma la restauración de la base N8? [s/n]: ",
            active_in=active_in,
        )
        if not ok:
            msg = "[Advertencia] Restauración cancelada por el usuario."
            active_out(f"JARVIS: {msg}")
            return msg

        restore_res = self.engine.restore_backup(
            target_ref,
            confirmed=True,
            actor="console_user",
        )
        msg = (
            f"[Confirmación] {restore_res.details}\n"
            f"  SHA-256 verificado: {restore_res.backup_sha256}\n"
            f"  PRAGMA integrity_check: {restore_res.integrity_report.integrity_check_result}"
        )
        active_out(f"JARVIS:\n{msg}")
        return msg
