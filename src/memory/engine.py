"""
API central de memoria a largo plazo N8.2 (`MemoryEngine`).

Justificación técnica junior:
`MemoryEngine` es el único punto de entrada recomendado para la aplicación (consola, `BrainManager`,
importador manual y utilidades administrativas). Garantiza que ninguna capa externa escriba en
SQLite saltándose `MemoryPolicy`, `ExperienceStoreLock` ni el aislamiento del modo privado, y
mantiene N8 estrictamente separado de N5 (autoridad/ejecución) y N6 (`GoalStore`).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace as dataclass_replace
import json
import os
from pathlib import Path
import threading
from typing import Any, Callable, Iterator, Sequence

from config import settings
from src.brain.exceptions import is_sensitive_key, redact_secrets
from src.memory.models import (
    BackupInfo,
    ExecutionInvalidationResult,
    IntegrityReport,
    MemoryCategory,
    MemoryCreateInput,
    MemoryPersistenceError,
    MemoryPolicyError,
    MemoryPrivateModeError,
    MemoryQueryFilter,
    MemoryRecord,
    MemoryRetrievalResult,
    MemoryRetrievalTarget,
    MemoryRevision,
    MemoryScopeContext,
    MemoryScopeType,
    MemorySource,
    MemoryStatus,
    MemoryValidationError,
    MemoryWriteResult,
    PreferenceImportItem,
    PreferenceImportPreview,
    PreferenceImportResult,
    PurgeResult,
    RestoreResult,
    StoreStats,
)
from src.memory.policy import (
    MemoryPolicy,
    assert_no_secrets,
    sanitize_memory_text,
    validate_safe_backup_dir,
    validate_safe_read_json_path,
)
from src.memory.retriever import MemoryRetriever
from src.memory.store import ExperienceStore, _default_backup_dir


_AMBIGUOUS_PREFERENCE_KEY_MARKERS: tuple[str, ...] = (
    "inference",
    "inferencia",
    "inferred",
    "guess",
    "raw_transcript",
    "transcript",
    "system_prompt",
    "untrusted",
)


_DEFAULT_PROVIDER_SENTINEL: object = object()


class MemoryEngine:
    """
    Fachada central N8.2 sobre persistencia (`ExperienceStore`), recuperación (`MemoryRetriever`),
    políticas (`MemoryPolicy`), modo privado, purga física, backups e importación manual.
    """

    def __init__(
        self,
        *,
        store: ExperienceStore | None = None,
        retriever: MemoryRetriever | None = None,
        policy: MemoryPolicy | None = None,
        backup_dir: Path | str | None = None,
        is_private_mode_fn: Callable[[], bool] | None = None,
        allow_history_in_private_fn: Callable[[], bool] | None = None,
        n5_journal_dir: Path | str | Sequence[Path | str] | None = None,
        n5_rollback_execution_provider: Any = _DEFAULT_PROVIDER_SENTINEL,
    ) -> None:
        self.policy: MemoryPolicy = policy or (store.policy if store is not None else MemoryPolicy())
        self.store: ExperienceStore = store or ExperienceStore(policy=self.policy)
        self.retriever: MemoryRetriever = retriever or MemoryRetriever(self.store, policy=self.policy)
        raw_backup = _default_backup_dir() if backup_dir is None else backup_dir
        self.backup_dir: Path = validate_safe_backup_dir(raw_backup)

        self._private_mode: bool = False
        self._allow_history_in_private: bool = True
        self._is_private_mode_fn: Callable[[], bool] | None = is_private_mode_fn
        self._allow_history_in_private_fn: Callable[[], bool] | None = allow_history_in_private_fn
        # Remediación F-05 / H-01 / H-02: cola RAM mínima para rollbacks diferidos y proveedor N5
        # conectado por defecto sobre el journal durable sin depender de clausuras de main.py.
        self._state_lock: threading.RLock = threading.RLock()
        self._pending_rollback_invalidations: dict[str, dict[str, str]] = {}
        self._pending_invalidation_error: str | None = None
        self._registered_n5_journal_dirs: list[Path] = []
        if n5_journal_dir is not None:
            if isinstance(n5_journal_dir, (str, Path)):
                self._registered_n5_journal_dirs.append(Path(n5_journal_dir).resolve())
            else:
                for item in n5_journal_dir:
                    self._registered_n5_journal_dirs.append(Path(item).resolve())

        self._custom_n5_provider_set: bool = n5_rollback_execution_provider is not _DEFAULT_PROVIDER_SENTINEL
        self._n5_rollback_execution_provider: Callable[[], Any] | None = (
            self._default_n5_rollback_provider
            if n5_rollback_execution_provider is _DEFAULT_PROVIDER_SENTINEL
            else n5_rollback_execution_provider
        )
        # Estado local para evitar transacciones SQLite de escritura sobre tombstones ya verificados
        # frente a la firma exacta en disco de experiences.db (+ WAL), sin omitir nuevos rollbacks.
        self._verified_db_signature: tuple[Any, ...] | None = None
        self._verified_reconciled_execs: set[str] = set()
        self._verified_reconciled_ops: set[tuple[str, str]] = set()
        self._reconciliation_tls: threading.local = threading.local()

    # ------------------------------------------------------------------
    # Control centralizado de Modo Privado e Invalidaciones Diferidas (F-05)
    # ------------------------------------------------------------------

    @property
    def is_private_mode(self) -> bool:
        ext_priv = (self._is_private_mode_fn() is True) if self._is_private_mode_fn else False
        return self._private_mode or ext_priv

    @property
    def allow_history_reads(self) -> bool:
        if not self.is_private_mode:
            return True
        ext_allow = (
            (self._allow_history_in_private_fn() is not False)
            if self._allow_history_in_private_fn
            else True
        )
        return self._allow_history_in_private and ext_allow

    def set_private_mode(self, *, enabled: bool, allow_history: bool = True) -> None:
        """
        Sincroniza el estado de privacidad del motor N8 sin escribir contenido privado en disco.
        Al salir de modo privado procesa automáticamente cualquier invalidación de rollback N5
        pendiente en RAM antes de permitir nuevas lecturas históricas (remediación F-05).
        """
        self._private_mode = bool(enabled)
        self._allow_history_in_private = True if not enabled else bool(allow_history)
        if not self.is_private_mode and self.has_pending_rollback_invalidations():
            try:
                self.flush_pending_rollback_invalidations()
            except Exception:
                # Se conserva la cola pendiente y `_pending_invalidation_error` para operar
                # en modo fail-closed ante cualquier lectura posterior hasta poder drenarla.
                pass

    def set_n5_rollback_execution_provider(
        self, provider: Callable[[], Any] | None
    ) -> None:
        """
        Conecta o actualiza el lector read-only del journal N5 usado antes de cada operación N8.
        Admite proveedores que retornen `JournalReconciliationAssessment` o `Sequence[str]`.
        """
        with self._state_lock:
            self._custom_n5_provider_set = True
            self._n5_rollback_execution_provider = provider

    @property
    def has_n5_rollback_provider(self) -> bool:
        with self._state_lock:
            return self._n5_rollback_execution_provider is not None

    def register_n5_journal_dir(
        self,
        journal_dir: Path | str,
        *,
        persist_metadata: bool = False,
    ) -> None:
        """
        Vincula un directorio real de transacciones N5 con esta instancia de `MemoryEngine`.
        Si `persist_metadata=True` y la escritura está permitida, anota la ruta en `store_metadata`
        para que tras un reinicio de proceso la reconciliación encuentre el mismo journal (H-02).
        """
        resolved = Path(journal_dir).resolve()
        with self._state_lock:
            if all(str(p).lower() != str(resolved).lower() for p in self._registered_n5_journal_dirs):
                self._registered_n5_journal_dirs.append(resolved)
        if persist_metadata and not self.is_private_mode:
            try:
                self.store.record_n5_journal_dir_metadata(resolved)
            except Exception:
                pass

    def _discover_n5_journal_dirs(self) -> list[Path]:
        """
        Resuelve de forma determinista y sin `Path.exists()` los directorios de journals N5 que
        deben inspeccionarse para proteger los recuerdos de esta instancia (remediación H-02).
        """
        from src.brain.transaction_manager import PRODUCTION_JOURNAL_DIR

        candidates: list[Path] = []
        seen: set[str] = set()

        def _add(raw_path: Path | str | None) -> None:
            if raw_path is None:
                return
            s_raw = str(raw_path).strip()
            if not s_raw:
                return
            resolved = Path(s_raw).resolve()
            norm_key = str(resolved).lower()
            if norm_key not in seen:
                seen.add(norm_key)
                candidates.append(resolved)

        with self._state_lock:
            registered_copy = list(self._registered_n5_journal_dirs)
        for reg_dir in registered_copy:
            _add(reg_dir)

        for meta_dir in self.store.read_registered_n5_journal_dirs():
            _add(meta_dir)

        env_name = os.environ.get("JARVIS_ENV", "").strip().lower()
        cfg_journal = Path(settings.TRANSACTION_JOURNAL_DIR).resolve()
        if env_name not in {"test", "testing"}:
            _add(cfg_journal)
            return candidates

        # En modo test nunca tocamos data/transactions productivo salvo override explícito seguro.
        if cfg_journal != PRODUCTION_JOURNAL_DIR:
            _add(cfg_journal)

        test_root_raw = os.environ.get("JARVIS_TEST_TEMP_ROOT", "").strip()
        if test_root_raw:
            _add(Path(test_root_raw).resolve() / "transactions")

        # Descubrimiento acotado al directorio aislado de la prueba propietaria de `db_path`
        # (nunca escanea la raíz global compartida `temp/jarvis_test_isolated`).
        shared_sandbox_root = (settings.BASE_DIR / "temp" / "jarvis_test_isolated").resolve()
        db_parent = self.store.db_path.resolve().parent
        if db_parent != shared_sandbox_root:
            _add(db_parent / "transactions")
            _add(db_parent / "backups" / "transactions")
            try:
                with os.scandir(str(db_parent)) as it:
                    for entry in sorted(list(it), key=lambda d: d.name):
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        sub_path = Path(entry.path).resolve()
                        low_name = entry.name.lower()
                        if low_name in {"journal", "tx_journal", "n5_journal", "n5_transactions"} or low_name.startswith("tx_run_"):
                            _add(sub_path)
                            continue
                        try:
                            with os.scandir(str(sub_path)) as sub_it:
                                if any(
                                    child.name.endswith((".tx.json", ".wal.jsonl"))
                                    for child in sub_it
                                ):
                                    _add(sub_path)
                        except PermissionError:
                            # Si un subdirectorio candidato tiene permisos denegados, lo incluimos
                            # para que `inspect_journal_directory_status` falle cerrado si procede.
                            if "tx" in low_name or "journal" in low_name or "transaction" in low_name:
                                _add(sub_path)
                        except OSError:
                            pass
            except OSError:
                pass
        return candidates

    def _default_n5_rollback_provider(self) -> Any:
        """
        Evalúa todos los journals N5 aplicables y devuelve un `JournalReconciliationAssessment`
        consolidado, preservando errores de corrupción o permisos para fallar cerrado (H-01/H-02).
        """
        from src.brain.transaction_manager import (
            JournalReconciliationAssessment,
            TransactionJournal,
            inspect_journal_directory_status,
        )

        dirs = self._discover_n5_journal_dirs()
        confirmed_execs: set[str] = set()
        partial_ops_map: dict[str, set[str]] = {}
        ambiguous_entries: list[Any] = []
        committed_clean: set[str] = set()
        all_entries: list[Any] = []
        any_accessible = False

        for j_dir in dirs:
            status = inspect_journal_directory_status(j_dir)
            if status == "absent":
                continue
            any_accessible = True
            assessment = TransactionJournal(j_dir, create_dirs=False).assess_reconciliation_state()
            confirmed_execs.update(assessment.confirmed_rollback_execution_ids)
            committed_clean.update(assessment.committed_clean_execution_ids)
            ambiguous_entries.extend(assessment.ambiguous_or_pending_transactions)
            all_entries.extend(assessment.entries)
            for eid, ops in assessment.partial_rollback_operations:
                partial_ops_map.setdefault(eid, set()).update(ops)

        # Si una ejecución aparece confirmada por completo, no requiere además entrada parcial redundante.
        for full_eid in confirmed_execs:
            partial_ops_map.pop(full_eid, None)

        return JournalReconciliationAssessment(
            journal_dir=";".join(str(d) for d in dirs),
            directory_status="accessible" if any_accessible else "absent",
            confirmed_rollback_execution_ids=tuple(sorted(confirmed_execs)),
            partial_rollback_operations=tuple(
                (eid, tuple(sorted(ops))) for eid, ops in sorted(partial_ops_map.items()) if ops
            ),
            ambiguous_or_pending_transactions=tuple(ambiguous_entries),
            committed_clean_execution_ids=tuple(sorted(committed_clean - confirmed_execs)),
            entries=tuple(all_entries),
        )

    def _current_db_disk_signature(self) -> tuple[Any, ...] | None:
        """Obtiene la huella `(mtime_ns, size)` de `experiences.db` y su `-wal` para validar caché local."""
        sig: list[Any] = [str(self.store.db_path)]
        for suffix in ("", "-wal"):
            target = f"{self.store.db_path}{suffix}"
            try:
                st = os.stat(target)
                sig.append((st.st_mtime_ns, st.st_size))
            except FileNotFoundError:
                if suffix == "":
                    return None
                sig.append(None)
            except OSError:
                return None
        return tuple(sig)

    def _store_invalidation_methods_patched(self) -> bool:
        store_vars = getattr(self.store, "__dict__", {})
        return any(
            k in store_vars
            for k in (
                "invalidate_by_execution",
                "invalidate_by_execution_operations",
                "is_execution_invalidated",
                "is_execution_operation_invalidated",
                "mark_execution_rollback_tombstone",
                "inspect_unreconciled_n5_rollbacks",
            )
        )

    @contextmanager
    def reconciliation_scope(self) -> Iterator[None]:
        """
        Ejecuta la reconciliación fail-closed N5/N8 una sola vez en el borde de una operación
        compuesta de `MemoryEngine`, evitando escaneos redundantes internos (H-03 / Sección 12).
        """
        depth = int(getattr(self._reconciliation_tls, "depth", 0))
        if depth == 0:
            self._ensure_pending_invalidations_applied_for_read()
        self._reconciliation_tls.depth = depth + 1
        try:
            yield
        finally:
            self._reconciliation_tls.depth = depth

    def queue_pending_rollback_invalidation(
        self,
        execution_id: str,
        *,
        reason: str = "n5_rollback_confirmed",
        actor: str = "n5_orchestrator",
    ) -> bool:
        """
        Encola en RAM la invalidación de una ejecución N5 revertida durante el modo privado.
        Solo almacena `execution_id`, `reason` y `actor` sanitizados (jamás texto de recuerdos
        ni datos conversacionales) y suprime inmediatamente dichos recuerdos de lecturas en curso.
        """
        clean_exec_id = sanitize_memory_text(
            execution_id,
            max_chars=128,
            allow_newlines=False,
            field_name="execution_id",
            check_secrets=True,
        )
        clean_reason = sanitize_memory_text(
            reason or "n5_rollback_confirmed",
            max_chars=120,
            allow_newlines=False,
            field_name="reason",
            check_secrets=True,
        )
        clean_actor = sanitize_memory_text(
            actor or "n5_orchestrator",
            max_chars=64,
            allow_newlines=False,
            field_name="actor",
            check_secrets=True,
        )
        with self._state_lock:
            is_new = clean_exec_id not in self._pending_rollback_invalidations
            self._pending_rollback_invalidations[clean_exec_id] = {
                "reason": clean_reason,
                "actor": clean_actor,
            }
            return is_new

    def has_pending_rollback_invalidations(self) -> bool:
        with self._state_lock:
            return bool(self._pending_rollback_invalidations)

    def pending_rollback_execution_ids(self) -> tuple[str, ...]:
        with self._state_lock:
            return tuple(sorted(self._pending_rollback_invalidations.keys()))

    def is_execution_pending_invalidation(self, execution_id: str | None) -> bool:
        if not isinstance(execution_id, str) or not execution_id.strip():
            return False
        clean_exec = execution_id.strip()
        with self._state_lock:
            return clean_exec in self._pending_rollback_invalidations

    def flush_pending_rollback_invalidations(self) -> dict[str, Any]:
        """
        Procesa en SQLite todas las invalidaciones de rollback N5 encoladas durante el modo privado.
        No crea nuevas experiencias ni escribe datos conversacionales; ante cualquier fallo de
        persistencia conserva el ID en la cola y lanza `MemoryPersistenceError` (fail-closed).
        """
        if self.is_private_mode:
            return {
                "flushed": 0,
                "pending": len(self.pending_rollback_execution_ids()),
                "errors": {},
            }

        with self._state_lock:
            pending_snapshot = list(self._pending_rollback_invalidations.items())

        if not pending_snapshot:
            with self._state_lock:
                self._pending_invalidation_error = None
            return {"flushed": 0, "pending": 0, "errors": {}}

        flushed = 0
        errors: dict[str, str] = {}
        for exec_id, meta in pending_snapshot:
            try:
                inv = self.store.invalidate_by_execution(
                    exec_id,
                    reason=meta["reason"],
                    actor=meta["actor"],
                    private_mode=False,
                )
                if inv.invalidated_count == 0 and inv.already_inactive_count == 0:
                    self.store.mark_execution_rollback_tombstone(
                        exec_id,
                        reason=meta["reason"],
                        actor=meta["actor"],
                        private_mode=False,
                    )
                with self._state_lock:
                    self._pending_rollback_invalidations.pop(exec_id, None)
                flushed += 1
            except Exception as exc:
                errors[exec_id] = redact_secrets(str(exc)) or type(exc).__name__

        if errors:
            summary_err = "; ".join(f"{k}: {v}" for k, v in sorted(errors.items()))
            with self._state_lock:
                self._pending_invalidation_error = summary_err
            raise MemoryPersistenceError(
                f"Fallo al procesar invalidaciones N5 pendientes al salir de modo privado (fail-closed): {summary_err}"
            )

        with self._state_lock:
            self._pending_invalidation_error = None
        return {"flushed": flushed, "pending": 0, "errors": {}}

    def _ensure_pending_invalidations_applied_for_read(self) -> None:
        """
        Frontera fail-closed unificada antes de exponer, respaldar, clasificar o modificar
        recuerdos en `MemoryEngine` (remediaciones F-05, H-01, H-02, H-03 y Sección 12):
        1. Inspecciona el journal N5 durable mediante el proveedor conectado.
        2. Aplica reconciliaciones confirmadas completas y granulares por operación.
        3. Bloquea de forma cerrada (`MemoryPersistenceError`) ante cualquier error del proveedor,
           corrupción/inaccesibilidad del journal, estado ambiguo (`ROLLING_BACK`, recuperación
           inconclusa o fallida) o fallo SQLite pendiente de reintento.
        """
        if int(getattr(self._reconciliation_tls, "depth", 0)) > 0:
            return

        provider = self._n5_rollback_execution_provider
        if provider is not None:
            from src.brain.transaction_manager import (
                JournalReconciliationAssessment,
                TransactionJournal,
            )

            try:
                bound_owner = getattr(provider, "__self__", None)
                if (
                    isinstance(bound_owner, TransactionJournal)
                    and getattr(provider, "__name__", "") == "list_confirmed_rollback_execution_ids"
                    and "list_confirmed_rollback_execution_ids" not in getattr(bound_owner, "__dict__", {})
                ):
                    raw_state = bound_owner.assess_reconciliation_state()
                else:
                    raw_state = provider()
            except MemoryPersistenceError:
                raise
            except Exception as exc:
                clean_msg = redact_secrets(str(exc)) or type(exc).__name__
                raise MemoryPersistenceError(
                    f"Reconciliación N5/N8 bloqueada de forma cerrada ({type(exc).__name__}): {clean_msg}"
                ) from exc

            if isinstance(raw_state, JournalReconciliationAssessment):
                outcomes = self.reconcile_n5_journal_assessment(raw_state)
                pending = {k: v for k, v in outcomes.items() if v.startswith("pending:")}
                if pending:
                    raise MemoryPersistenceError(
                        "La operación N8 queda bloqueada hasta reconciliar los rollbacks N5 confirmados."
                    )
                if raw_state.has_blocking_uncertainty:
                    reasons = "; ".join(
                        f"{e.transaction_id}({e.state}:{e.classification.value})"
                        for e in raw_state.ambiguous_or_pending_transactions[:5]
                    )
                    raise MemoryPersistenceError(
                        f"Lectura/operación N8 bloqueada (fail-closed) por transacción N5 ambigua o "
                        f"pendiente de recuperación: {reasons}"
                    )
            else:
                execution_ids = list(raw_state or ())
                outcomes = self.reconcile_confirmed_n5_rollbacks(execution_ids)
                pending = {key: value for key, value in outcomes.items() if value.startswith("pending:")}
                if pending:
                    raise MemoryPersistenceError(
                        "La lectura N8 queda bloqueada hasta reconciliar los rollbacks N5 confirmados."
                    )

        if not self.is_private_mode and self.has_pending_rollback_invalidations():
            self.flush_pending_rollback_invalidations()

    def _record_suppressed_by_pending_rollback(self, record: MemoryRecord | None) -> bool:
        if record is None or record.source != MemorySource.EXECUTION_VERIFIED:
            return False
        exec_id = record.provenance.source_execution_id
        return self.is_execution_pending_invalidation(exec_id)

    def _assert_write_allowed(self) -> None:
        self.policy.assert_write_allowed(private_mode=self.is_private_mode)

    # ------------------------------------------------------------------
    # Recuperación determinista y consulta
    # ------------------------------------------------------------------

    def retrieve(
        self,
        query: str,
        *,
        target: MemoryRetrievalTarget | str = MemoryRetrievalTarget.NORMAL,
        context_scope: MemoryScopeContext | None = None,
        scope_type: MemoryScopeType | None = None,
        scope_key: str | None = None,
        categories: tuple[MemoryCategory, ...] | None = None,
        include_pending: bool = False,
        force_sql_fallback: bool = False,
        allow_history_override: bool | None = None,
        max_items_override: int | None = None,
        max_chars_override: int | None = None,
    ) -> MemoryRetrievalResult:
        """
        Recupera recuerdos relevantes de forma determinista respetando presupuesto,
        invalidaciones N5 pendientes (fail-closed) y política `--sin-historial`.
        """
        self._ensure_pending_invalidations_applied_for_read()
        effective_allow_history = (
            self.allow_history_reads
            if allow_history_override is None
            else (bool(allow_history_override) and self.allow_history_reads)
        )
        result = self.retriever.retrieve(
            query,
            target=target,
            context_scope=context_scope,
            scope_type=scope_type,
            scope_key=scope_key,
            categories=categories,
            allow_history=effective_allow_history,
            include_pending=include_pending,
            force_sql_fallback=force_sql_fallback,
            max_items_override=max_items_override,
            max_chars_override=max_chars_override,
        )
        # Durante modo privado con historial (Opción A), cualquier recuerdo cuya ejecución N5
        # fue revertida en esta sesión se excluye inmediatamente en RAM sin tocar disco.
        if self.has_pending_rollback_invalidations() and result.items:
            kept = tuple(
                item
                for item in result.items
                if not self._record_suppressed_by_pending_rollback(item.record)
            )
            if len(kept) != len(result.items):
                return MemoryRetrievalResult(
                    query=result.query,
                    target=result.target,
                    items=kept,
                    total_candidates=len(kept),
                    returned_count=len(kept),
                    total_chars=sum(len(it.rendered_excerpt) for it in kept),
                    budget_max_items=result.budget_max_items,
                    budget_max_chars=result.budget_max_chars,
                    truncated_by_budget=result.truncated_by_budget,
                    blocked_by_history_policy=result.blocked_by_history_policy,
                    backend_used=result.backend_used,
                    store_version_token=result.store_version_token,
                )
        return result

    def get_memory(self, memory_id: str) -> MemoryRecord | None:
        self._ensure_pending_invalidations_applied_for_read()
        rec = self.store.get_memory(memory_id)
        if rec is not None and self._record_suppressed_by_pending_rollback(rec):
            # Durante modo privado devolvemos vista lógica REVOKED en RAM sin escribir en SQLite
            # para que MemorySnapshotGuard y GoalMemoryBridge lo excluyan de inmediato.
            return dataclass_replace(rec, status=MemoryStatus.REVOKED)
        return rec

    def list_memories(
        self,
        *,
        status: MemoryStatus | None = MemoryStatus.ACTIVE,
        category: MemoryCategory | None = None,
        source: MemorySource | None = None,
        scope_type: MemoryScopeType | None = None,
        scope_key: str | None = None,
        pending_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MemoryRecord]:
        self._ensure_pending_invalidations_applied_for_read()
        records = self.store.list_memories(
            status=status,
            category=category,
            source=source,
            scope_type=scope_type,
            scope_key=scope_key,
            pending_only=pending_only,
            limit=limit,
            offset=offset,
        )
        if not self.has_pending_rollback_invalidations():
            return records
        filtered: list[MemoryRecord] = []
        for rec in records:
            if self._record_suppressed_by_pending_rollback(rec):
                if status == MemoryStatus.ACTIVE:
                    continue
                filtered.append(dataclass_replace(rec, status=MemoryStatus.REVOKED))
            else:
                filtered.append(rec)
        return filtered

    def get_revisions(self, memory_id: str) -> list[MemoryRevision]:
        self._ensure_pending_invalidations_applied_for_read()
        return self.store.get_revisions(memory_id)

    def get_stats(self) -> StoreStats:
        self._ensure_pending_invalidations_applied_for_read()
        stats = self.store.get_stats()
        if not self.has_pending_rollback_invalidations():
            return stats
        suppressed_count = sum(
            1
            for rec in self.store.list_memories(status=MemoryStatus.ACTIVE, limit=self.policy.max_active_memories)
            if self._record_suppressed_by_pending_rollback(rec)
        )
        if suppressed_count <= 0:
            return stats
        return dataclass_replace(
            stats,
            active_memories=max(0, stats.active_memories - suppressed_count),
            revoked_memories=stats.revoked_memories + suppressed_count,
        )

    def get_store_version_token(self) -> str:
        """Devuelve el token monótono actual (`epoch:seq`) del almacén de experiencias."""
        self._ensure_pending_invalidations_applied_for_read()
        return self.store.get_store_version_token()

    def create_snapshot_guard(
        self,
        *,
        request_id: str,
        goal_id: str | None = None,
        plan_id: str | None = None,
        max_revalidations: int = 1,
        max_refreshes: int = 1,
    ) -> Any:
        """Instancia un `MemorySnapshotGuard` vinculado a una solicitud concreta."""
        from src.memory.snapshot_guard import MemorySnapshotGuard

        return MemorySnapshotGuard(
            memory_engine=self,
            request_id=request_id,
            goal_id=goal_id,
            plan_id=plan_id,
            max_revalidations=max_revalidations,
            max_refreshes=max_refreshes,
        )

    def verify_integrity(self, *, quarantine_on_corruption: bool = False) -> IntegrityReport:
        return self.store.verify_integrity(quarantine_on_corruption=quarantine_on_corruption)

    # ------------------------------------------------------------------
    # Ciclo de vida completo: crear, corregir, confirmar, olvidar, purgar
    # ------------------------------------------------------------------

    def create_memory(
        self,
        payload: MemoryCreateInput,
        *,
        actor: str = "user",
    ) -> MemoryWriteResult:
        """Crea un recuerdo validado aplicando políticas N8.1 y bloqueo por modo privado."""
        self._assert_write_allowed()
        return self.store.create_memory(
            payload,
            private_mode=self.is_private_mode,
            actor=actor,
        )

    def correct_memory(
        self,
        memory_id: str,
        *,
        title: str | None = None,
        content: str | None = None,
        summary: str | None = None,
        confidence: float | None = None,
        importance: int | None = None,
        tags: tuple[str, ...] | None = None,
        reason: str = "manual_correction",
        actor: str = "user",
    ) -> MemoryRecord:
        """Corrige un recuerdo ACTIVE preservando su versión anterior en `memory_revisions`."""
        self._assert_write_allowed()
        self._ensure_pending_invalidations_applied_for_read()
        return self.store.update_memory_with_revision(
            memory_id,
            title=title,
            content=content,
            summary=summary,
            confidence=confidence,
            importance=importance,
            tags=tags,
            reason=reason,
            actor=actor,
            private_mode=self.is_private_mode,
        )

    def confirm_memory(
        self,
        memory_id: str,
        *,
        actor: str = "user",
        reason: str = "manual_confirmation",
    ) -> MemoryRecord:
        """Confirma un recuerdo pendiente o activo para habilitarlo con confianza verificada."""
        self._assert_write_allowed()
        self._ensure_pending_invalidations_applied_for_read()
        return self.store.confirm_memory(
            memory_id,
            actor=actor,
            reason=reason,
            private_mode=self.is_private_mode,
        )

    def revoke_memory(
        self,
        memory_id: str,
        *,
        reason: str = "manual_revoke",
        actor: str = "user",
    ) -> MemoryRecord:
        """Revoca lógicamente un recuerdo excluyéndolo de inmediato de FTS5 y recuperación."""
        self._assert_write_allowed()
        return self.store.revoke_memory(
            memory_id,
            reason=reason,
            actor=actor,
        )

    def invalidate_by_execution(
        self,
        execution_id: str,
        *,
        reason: str = "rollback_execution",
        actor: str = "system",
    ) -> ExecutionInvalidationResult:
        """
        Revoca todos los recuerdos `EXECUTION_VERIFIED` activos asociados a una ejecución
        revertida sin purgar la evidencia histórica de auditoría ni tocar recuerdos ajenos.
        """
        self._assert_write_allowed()
        return self.store.invalidate_by_execution(
            execution_id,
            reason=reason,
            actor=actor,
            private_mode=self.is_private_mode,
        )

    def reconcile_confirmed_n5_rollbacks(
        self,
        execution_ids: tuple[str, ...] | list[str],
        *,
        partial_operations: Sequence[tuple[str, Sequence[str]]] = (),
    ) -> dict[str, str]:
        """Reconcilia estados ROLLED_BACK confirmados (totales o por operación) en N5, incluso en privado.

        Optimizaciones seguras (Sección 12):
        1. Si todas las ejecuciones/operaciones solicitadas ya fueron verificadas contra la firma
           exacta en disco de `experiences.db` (+ `-wal`) y los métodos del store no están parcheados,
           evita abrir conexiones o iniciar transacciones de escritura redundantes.
        2. Cuando existen candidatos no verificados, consulta primero con una sola conexión de lectura
           (`inspect_unreconciled_n5_rollbacks`) antes de ejecutar `BEGIN IMMEDIATE` únicamente sobre
           los IDs que aún carecen de invalidación o tombstone.
        """
        unique_execs = tuple(sorted({str(e).strip() for e in execution_ids if str(e).strip()}))
        norm_partials = tuple(
            (str(eid).strip(), tuple(sorted({str(op).strip() for op in ops if str(op).strip()})))
            for eid, ops in partial_operations
            if str(eid).strip() and ops
        )
        norm_partials = tuple((eid, ops) for eid, ops in norm_partials if ops)

        if not unique_execs and not norm_partials:
            return {}

        outcomes: dict[str, str] = {}
        patched = self._store_invalidation_methods_patched()
        disk_sig = None if patched else self._current_db_disk_signature()

        with self._state_lock:
            if disk_sig is None or disk_sig != self._verified_db_signature:
                self._verified_reconciled_execs.clear()
                self._verified_reconciled_ops.clear()
                self._verified_db_signature = disk_sig

            unverified_execs = [
                eid for eid in unique_execs if patched or eid not in self._verified_reconciled_execs
            ]
            unverified_partials: list[tuple[str, tuple[str, ...]]] = []
            for eid, ops in norm_partials:
                rem_ops = tuple(
                    op for op in ops if patched or (eid, op) not in self._verified_reconciled_ops
                )
                if rem_ops:
                    unverified_partials.append((eid, rem_ops))

        for eid in unique_execs:
            if eid not in unverified_execs:
                outcomes[eid] = "reconciled:0"
        for eid, ops in norm_partials:
            if all((eid, op) in self._verified_reconciled_ops for op in ops) and not patched:
                outcomes[f"{eid}#ops"] = "reconciled:0"

        if not unverified_execs and not unverified_partials:
            return outcomes

        # Pre-chequeo en una sola conexión de lectura cuando los métodos no fueron sustituidos en mocks.
        execs_to_write: list[str] = list(unverified_execs)
        partials_to_write: list[tuple[str, tuple[str, ...]]] = list(unverified_partials)
        if not patched:
            try:
                _tok, missing_execs, missing_partials = self.store.inspect_unreconciled_n5_rollbacks(
                    unverified_execs,
                    unverified_partials,
                )
                missing_exec_set = set(missing_execs)
                missing_partial_map = {eid: set(ops) for eid, ops in missing_partials}
                with self._state_lock:
                    cur_sig = self._current_db_disk_signature()
                    self._verified_db_signature = cur_sig
                    for eid in unverified_execs:
                        if eid not in missing_exec_set:
                            if cur_sig is not None:
                                self._verified_reconciled_execs.add(eid)
                            outcomes[eid] = "reconciled:0"
                    for eid, ops in unverified_partials:
                        pend = missing_partial_map.get(eid, set())
                        for op in ops:
                            if op not in pend and cur_sig is not None:
                                self._verified_reconciled_ops.add((eid, op))
                        if not pend:
                            outcomes[f"{eid}#ops"] = "reconciled:0"
                execs_to_write = list(missing_execs)
                partials_to_write = list(missing_partials)
            except Exception:
                # Ante fallo del pre-chequeo pasamos a la ruta por unidad para registrar pending exacto.
                execs_to_write = list(unverified_execs)
                partials_to_write = list(unverified_partials)

        for execution_id in execs_to_write:
            try:
                invalidation = self.store.invalidate_by_execution(
                    execution_id,
                    reason="n5_confirmed_rollback_reconciliation",
                    actor="n5_journal_recovery",
                    private_mode=False,
                )
                if not self.store.is_execution_invalidated(execution_id):
                    self.store.mark_execution_rollback_tombstone(
                        execution_id,
                        reason="n5_confirmed_rollback_reconciliation",
                        actor="n5_journal_recovery",
                        private_mode=False,
                    )
                if not self.store.is_execution_invalidated(execution_id):
                    raise MemoryPersistenceError(
                        "La invalidación de rollback no quedó verificada en la persistencia N8."
                    )
                outcomes[execution_id] = f"reconciled:{invalidation.invalidated_count}"
                if not patched:
                    with self._state_lock:
                        cur_sig = self._current_db_disk_signature()
                        self._verified_db_signature = cur_sig
                        if cur_sig is not None:
                            self._verified_reconciled_execs.add(execution_id)
            except Exception as exc:
                msg = redact_secrets(str(exc)) or type(exc).__name__
                outcomes[execution_id] = f"pending:{type(exc).__name__}:{msg}"

        for execution_id, op_ids in partials_to_write:
            key = f"{execution_id}#ops"
            try:
                invalidation = self.store.invalidate_by_execution_operations(
                    execution_id,
                    op_ids,
                    reason="n5_partial_rollback_operation_reconciliation",
                    actor="n5_journal_recovery",
                    private_mode=False,
                )
                for op_id in op_ids:
                    if not self.store.is_execution_operation_invalidated(execution_id, op_id):
                        raise MemoryPersistenceError(
                            f"La invalidación parcial de '{execution_id}/{op_id}' no quedó verificada."
                        )
                outcomes[key] = f"reconciled:{invalidation.invalidated_count}"
                if not patched:
                    with self._state_lock:
                        cur_sig = self._current_db_disk_signature()
                        self._verified_db_signature = cur_sig
                        if cur_sig is not None:
                            for op_id in op_ids:
                                self._verified_reconciled_ops.add((execution_id, op_id))
            except Exception as exc:
                msg = redact_secrets(str(exc)) or type(exc).__name__
                outcomes[key] = f"pending:{type(exc).__name__}:{msg}"

        return outcomes

    def reconcile_n5_journal_assessment(self, assessment: Any) -> dict[str, str]:
        """Aplica la evaluación completa de un `JournalReconciliationAssessment` sobre N8."""
        confirmed_ids = tuple(getattr(assessment, "confirmed_rollback_execution_ids", ()) or ())
        partial_ops = tuple(getattr(assessment, "partial_rollback_operations", ()) or ())
        return self.reconcile_confirmed_n5_rollbacks(
            confirmed_ids,
            partial_operations=partial_ops,
        )

    def is_execution_operation_invalidated(self, execution_id: str, operation_id: str) -> bool:
        """Indica si `(execution_id, operation_id)` fue revertida en cola RAM o en SQLite."""
        if self.is_execution_pending_invalidation(execution_id):
            return True
        return self.store.is_execution_operation_invalidated(execution_id, operation_id)

    def is_execution_invalidated(self, execution_id: str) -> bool:
        """Consulta si una ejecución fue revertida (en cola RAM pendiente o en SQLite)."""
        if self.is_execution_pending_invalidation(execution_id):
            return True
        return self.store.is_execution_invalidated(execution_id)

    def mark_execution_rollback_tombstone(
        self,
        execution_id: str,
        *,
        reason: str = "rollback_execution",
        actor: str = "system",
    ) -> bool:
        """Registra marca persistente de rollback confirmado respetando privacidad y bloqueo."""
        self._assert_write_allowed()
        return self.store.mark_execution_rollback_tombstone(
            execution_id,
            reason=reason,
            actor=actor,
            private_mode=self.is_private_mode,
        )

    def purge_memory(
        self,
        memory_id: str,
        *,
        confirmed: bool = False,
        vacuum: bool = False,
        reason: str = "manual_purge",
        actor: str = "user",
    ) -> PurgeResult:
        """Elimina físicamente un recuerdo y sus rastros internos tras confirmación explícita."""
        self._assert_write_allowed()
        if not confirmed:
            raise MemoryPolicyError(
                "La purga física es irreversible dentro de SQLite y requiere confirmación explícita."
            )
        return self.store.purge_memory(
            memory_id,
            actor=actor,
            reason=reason,
            vacuum=vacuum,
            private_mode=self.is_private_mode,
        )

    # ------------------------------------------------------------------
    # Backups verificados y restauración atómica
    # ------------------------------------------------------------------

    def create_backup(self, *, retention: int | None = None, actor: str = "user") -> BackupInfo:
        """Crea un backup SQLite validado con SHA-256 tras reconciliar rollbacks N5 pendientes."""
        self._assert_write_allowed()
        with self.reconciliation_scope():
            return self.store.create_backup(
                self.backup_dir,
                retention=retention,
                actor=actor,
            )

    def list_backups(self) -> list[BackupInfo]:
        return self.store.list_backups(self.backup_dir)

    def restore_backup(
        self,
        backup_ref: Path | str,
        *,
        confirmed: bool = False,
        expected_sha256: str | None = None,
        actor: str = "user",
    ) -> RestoreResult:
        """Restaura desde un backup verificado tras confirmación explícita del usuario."""
        self._assert_write_allowed()
        if not confirmed:
            raise MemoryPolicyError(
                "La restauración de la base N8 requiere confirmación explícita."
            )
        return self.store.restore_from_backup(
            backup_ref,
            backup_dir=self.backup_dir,
            expected_sha256=expected_sha256,
            actor=actor,
            private_mode=self.is_private_mode,
        )

    # ------------------------------------------------------------------
    # Importación manual de preferencias históricas (`memory_store.json`)
    # ------------------------------------------------------------------

    @staticmethod
    def _default_import_source_path() -> Path:
        return (settings.BASE_DIR / "data" / "memory_store.json").resolve()

    def _build_preference_candidate_input(
        self,
        raw_key: Any,
        raw_val: Any,
    ) -> tuple[bool, str, str, MemoryCreateInput | None, str]:
        """
        Valida un par `(clave, valor)` procedente de `memory_store.json` y construye su
        `MemoryCreateInput` solo si cumple seguridad, estructura escalar y ausencia de secretos.
        Devuelve `(ok, clean_key, value_summary, payload_or_None, reject_reason)`.
        """
        if not isinstance(raw_key, str) or not raw_key.strip():
            return False, str(raw_key)[:32], "", None, "Clave de preferencia vacía o no textual."

        try:
            clean_key = sanitize_memory_text(
                raw_key,
                max_chars=64,
                allow_newlines=False,
                field_name="preference_key",
                check_secrets=True,
            ).lower()
        except Exception:
            return False, "<clave_rechazada>", "", None, "Clave rechazada por sanitización o patrón sensible."

        if is_sensitive_key(clean_key):
            return False, clean_key, "", None, "Clave sensible rechazada por política de privacidad."

        if any(marker in clean_key for marker in _AMBIGUOUS_PREFERENCE_KEY_MARKERS):
            return False, clean_key, "", None, "Clave reservada para inferencias o datos ambiguos."

        if isinstance(raw_val, bool):
            val_text = "sí" if raw_val else "no"
        elif isinstance(raw_val, (int, float)):
            val_text = str(raw_val)
        elif isinstance(raw_val, str):
            val_text = raw_val
        else:
            return False, clean_key, "", None, "Tipo de valor complejo o no compatible (solo texto/escalar simple)."

        try:
            clean_val = sanitize_memory_text(
                val_text,
                max_chars=240,
                allow_newlines=False,
                field_name=f"preference[{clean_key}]",
                check_secrets=True,
            )
            assert_no_secrets(f"{clean_key}={clean_val}", field_name=f"preference[{clean_key}]")
        except Exception:
            return False, clean_key, "[OMITIDO]", None, "Valor rechazado por política de formato o posible secreto."

        payload = MemoryCreateInput(
            category=MemoryCategory.PREFERENCE,
            source=MemorySource.USER_EXPLICIT,
            title=f"Preferencia: {clean_key}",
            content=f"Preferencia histórica '{clean_key}': {clean_val}",
            summary=f"{clean_key}: {clean_val}"[: settings.N8_MAX_SUMMARY_CHARS],
            scope_type=MemoryScopeType.GLOBAL,
            scope_key="global",
            confidence=0.85,
            importance=60,
            tags=("preferencia", "importada"),
            metadata=(
                ("imported_from", "legacy_memory_store"),
                ("preference_key", clean_key),
            ),
        )
        return True, clean_key, clean_val, payload, ""

    def _classify_import_preview_locked(
        self,
        source_path: Path,
    ) -> tuple[PreferenceImportPreview, list[tuple[MemoryCreateInput, str | None]]]:
        """
        Lee en modo exclusivamente lectura el JSON legacy y clasifica cada elemento como
        `NEW`, `DUPLICATE`, `CONFLICT` o `REJECTED` sin modificar ni sobrescribir nada.
        """
        if not source_path.exists() or not source_path.is_file():
            return (
                PreferenceImportPreview(
                    source_path=str(source_path),
                    valid=False,
                    new_items=(),
                    duplicates=(),
                    conflicts=(),
                    rejected=(),
                    error=f"No existe el archivo de origen: '{source_path}'.",
                ),
                [],
            )

        try:
            with source_path.open("r", encoding="utf-8") as fh:
                raw_data = json.load(fh)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return (
                PreferenceImportPreview(
                    source_path=str(source_path),
                    valid=False,
                    new_items=(),
                    duplicates=(),
                    conflicts=(),
                    rejected=(),
                    error=f"Archivo JSON inválido o ilegible: {exc}",
                ),
                [],
            )

        if not isinstance(raw_data, dict):
            return (
                PreferenceImportPreview(
                    source_path=str(source_path),
                    valid=False,
                    new_items=(),
                    duplicates=(),
                    conflicts=(),
                    rejected=(),
                    error="Estructura inesperada: el archivo debe ser un objeto JSON con diccionario 'preferences'.",
                ),
                [],
            )

        raw_version = raw_data.get("version")
        if raw_version is not None and (isinstance(raw_version, bool) or not isinstance(raw_version, int)):
            return (
                PreferenceImportPreview(
                    source_path=str(source_path),
                    valid=False,
                    new_items=(),
                    duplicates=(),
                    conflicts=(),
                    rejected=(),
                    error="Campo 'version' inválido en JSON legacy.",
                ),
                [],
            )

        if "preferences" not in raw_data or not isinstance(raw_data["preferences"], dict):
            return (
                PreferenceImportPreview(
                    source_path=str(source_path),
                    valid=False,
                    new_items=(),
                    duplicates=(),
                    conflicts=(),
                    rejected=(),
                    error="Estructura inesperada: falta el diccionario obligatorio 'preferences'.",
                ),
                [],
            )

        if "messages" in raw_data and not isinstance(raw_data["messages"], list):
            return (
                PreferenceImportPreview(
                    source_path=str(source_path),
                    valid=False,
                    new_items=(),
                    duplicates=(),
                    conflicts=(),
                    rejected=(),
                    error="Campo 'messages' inválido en JSON legacy.",
                ),
                [],
            )

        prefs_dict: dict[str, Any] = raw_data["preferences"]
        active_memories = [
            m
            for m in self.store.list_memories(status=MemoryStatus.ACTIVE, limit=1000)
            if not self._record_suppressed_by_pending_rollback(m)
        ]
        by_fingerprint = {m.content_fingerprint: m for m in active_memories}
        by_pref_key: dict[str, MemoryRecord] = {}
        for mem in active_memories:
            if mem.category == MemoryCategory.PREFERENCE and mem.scope_type == MemoryScopeType.GLOBAL:
                meta = dict(mem.metadata)
                pkey = meta.get("preference_key")
                if pkey:
                    by_pref_key[pkey.lower()] = mem
                elif mem.title.lower().startswith("preferencia: "):
                    by_pref_key[mem.title[len("preferencia: ") :].strip().lower()] = mem

        new_items: list[PreferenceImportItem] = []
        duplicates: list[PreferenceImportItem] = []
        conflicts: list[PreferenceImportItem] = []
        rejected: list[PreferenceImportItem] = []
        actionable_payloads: list[tuple[MemoryCreateInput, str | None]] = []

        for raw_k, raw_v in sorted(prefs_dict.items(), key=lambda pair: str(pair[0])):
            ok, clean_key, clean_val, candidate, reject_reason = self._build_preference_candidate_input(
                raw_k, raw_v
            )
            if not ok or candidate is None:
                rejected.append(
                    PreferenceImportItem(
                        key=clean_key,
                        value_summary="[RECHAZADO]",
                        classification="REJECTED",
                        reason=reject_reason,
                    )
                )
                continue

            try:
                prepared = self.policy.prepare_create(candidate, private_mode=False)
            except Exception:
                rejected.append(
                    PreferenceImportItem(
                        key=clean_key,
                        value_summary="[RECHAZADO]",
                        classification="REJECTED",
                        reason="Rechazado por políticas de ingesta N8.",
                    )
                )
                continue

            if prepared.content_fingerprint in by_fingerprint:
                existing = by_fingerprint[prepared.content_fingerprint]
                duplicates.append(
                    PreferenceImportItem(
                        key=clean_key,
                        value_summary=clean_val,
                        classification="DUPLICATE",
                        reason="Ya existe una preferencia activa equivalente.",
                        existing_memory_id=existing.memory_id,
                    )
                )
            elif clean_key in by_pref_key:
                existing = by_pref_key[clean_key]
                conflicts.append(
                    PreferenceImportItem(
                        key=clean_key,
                        value_summary=clean_val,
                        classification="CONFLICT",
                        reason=f"Valor distinto al recuerdo activo existente ({existing.summary}).",
                        existing_memory_id=existing.memory_id,
                    )
                )
                actionable_payloads.append((candidate, existing.memory_id))
            else:
                new_items.append(
                    PreferenceImportItem(
                        key=clean_key,
                        value_summary=clean_val,
                        classification="NEW",
                        reason="Preferencia válida lista para importar.",
                    )
                )
                actionable_payloads.append((candidate, None))

        preview = PreferenceImportPreview(
            source_path=str(source_path),
            valid=True,
            new_items=tuple(new_items),
            duplicates=tuple(duplicates),
            conflicts=tuple(conflicts),
            rejected=tuple(rejected),
            error=None,
        )
        return preview, actionable_payloads

    def preview_preferences_import(
        self,
        source_path: Path | str | None = None,
    ) -> PreferenceImportPreview:
        """
        Inspecciona en solo lectura el JSON legacy y devuelve la vista previa clasificada
        sin modificar ni el JSON de origen ni la base `experiences.db`, previa reconciliación N5.
        """
        self.policy.assert_enabled()
        raw_src = self._default_import_source_path() if source_path is None else source_path
        validated_src = validate_safe_read_json_path(raw_src)
        with self.reconciliation_scope():
            with self.store.acquire_lock():
                preview, _ = self._classify_import_preview_locked(validated_src)
                return preview

    def commit_preferences_import(
        self,
        source_path: Path | str | None = None,
        *,
        confirmed: bool = False,
        resolve_conflicts: str = "skip",
        actor: str = "user",
    ) -> PreferenceImportResult:
        """
        Ejecuta la importación manual confirmada dentro de `ExperienceStoreLock` previa
        reconciliación N5/N8. Nunca modifica el JSON origen; ante `resolve_conflicts='supersede'`
        sustituye el recuerdo conflictivo previo marcándolo `SUPERSEDED`.
        """
        self._assert_write_allowed()
        if not confirmed:
            raise MemoryPolicyError(
                "La importación de preferencias requiere vista previa y confirmación explícita."
            )
        if resolve_conflicts not in {"skip", "supersede"}:
            raise MemoryValidationError("resolve_conflicts debe ser 'skip' o 'supersede'.")

        raw_src = self._default_import_source_path() if source_path is None else source_path
        validated_src = validate_safe_read_json_path(raw_src)

        with self.reconciliation_scope():
            with self.store.acquire_lock():
                preview, payloads = self._classify_import_preview_locked(validated_src)
                if not preview.valid:
                    raise MemoryValidationError(preview.error or "No se pudo validar el archivo JSON de origen.")

                created_ids: list[str] = []
                conflicts_resolved = 0
                conflicts_skipped = 0
                extra_duplicates = 0

                for candidate, conflicting_id in payloads:
                    if conflicting_id is not None and resolve_conflicts == "skip":
                        conflicts_skipped += 1
                        continue

                    write_res = self.store.create_memory(
                        candidate,
                        private_mode=self.is_private_mode,
                        actor=actor,
                    )
                    if write_res.created:
                        created_ids.append(write_res.record.memory_id)
                        if conflicting_id is not None and resolve_conflicts == "supersede":
                            self.store.transition_status(
                                conflicting_id,
                                new_status=MemoryStatus.SUPERSEDED,
                                superseded_by=write_res.record.memory_id,
                                reason="superseded_by_preference_import",
                                actor=actor,
                            )
                            conflicts_resolved += 1
                    else:
                        extra_duplicates += 1

                return PreferenceImportResult(
                    imported_count=len(created_ids),
                    skipped_duplicates=len(preview.duplicates) + extra_duplicates,
                    conflicts_resolved=conflicts_resolved,
                    conflicts_skipped=conflicts_skipped,
                    rejected_count=len(preview.rejected),
                    created_memory_ids=tuple(created_ids),
                )
