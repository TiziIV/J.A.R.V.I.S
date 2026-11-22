"""
API central de memoria a largo plazo N8.2 (`MemoryEngine`).

Justificación técnica junior:
`MemoryEngine` es el único punto de entrada recomendado para la aplicación (consola, `BrainManager`,
importador manual y utilidades administrativas). Garantiza que ninguna capa externa escriba en
SQLite saltándose `MemoryPolicy`, `ExperienceStoreLock` ni el aislamiento del modo privado, y
mantiene N8 estrictamente separado de N5 (autoridad/ejecución) y N6 (`GoalStore`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from config import settings
from src.brain.exceptions import is_sensitive_key
from src.memory.models import (
    BackupInfo,
    ExecutionInvalidationResult,
    IntegrityReport,
    MemoryCategory,
    MemoryCreateInput,
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

    # ------------------------------------------------------------------
    # Control centralizado de Modo Privado
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
        """Sincroniza el estado de privacidad del motor N8 sin escribir en disco ni logs sensibles."""
        self._private_mode = bool(enabled)
        self._allow_history_in_private = True if not enabled else bool(allow_history)

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
        Recupera recuerdos relevantes de forma determinista respetando presupuesto y política
        `--sin-historial` del modo privado.
        """
        effective_allow_history = (
            self.allow_history_reads
            if allow_history_override is None
            else (bool(allow_history_override) and self.allow_history_reads)
        )
        return self.retriever.retrieve(
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

    def get_memory(self, memory_id: str) -> MemoryRecord | None:
        return self.store.get_memory(memory_id)

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
        return self.store.list_memories(
            status=status,
            category=category,
            source=source,
            scope_type=scope_type,
            scope_key=scope_key,
            pending_only=pending_only,
            limit=limit,
            offset=offset,
        )

    def get_revisions(self, memory_id: str) -> list[MemoryRevision]:
        return self.store.get_revisions(memory_id)

    def get_stats(self) -> StoreStats:
        return self.store.get_stats()

    def get_store_version_token(self) -> str:
        """Devuelve el token monótono actual (`epoch:seq`) del almacén de experiencias."""
        return self.store.get_store_version_token()

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

    def is_execution_invalidated(self, execution_id: str) -> bool:
        """Consulta en SQLite si una ejecución fue revertida o invalidada previamente."""
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
        """Crea un backup SQLite validado con SHA-256 sin tocar ni escribir en modo privado."""
        self._assert_write_allowed()
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
        active_memories = self.store.list_memories(status=MemoryStatus.ACTIVE, limit=1000)
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
        sin modificar ni el JSON de origen ni la base `experiences.db`.
        """
        self.policy.assert_enabled()
        raw_src = self._default_import_source_path() if source_path is None else source_path
        validated_src = validate_safe_read_json_path(raw_src)
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
        Ejecuta la importación manual confirmada dentro de `ExperienceStoreLock`.
        Nunca modifica el JSON origen; ante `resolve_conflicts='supersede'` sustituye el
        recuerdo conflictivo previo marcándolo `SUPERSEDED`.
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
