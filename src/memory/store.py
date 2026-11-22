"""
Almacén transaccional SQLite (`experiences.db`) y protocolo de bloqueo `ExperienceStoreLock` (N8.1).

Justificación técnica junior:
1. Cada operación del almacén abre una conexión SQLite efímera, ejecuta en modo WAL con
   `BEGIN IMMEDIATE` para escrituras y CIERRA la conexión antes de liberar `ExperienceStoreLock`.
   En Windows esto evita archivos bloqueados entre hilos, instancias distintas o subprocesos.
2. `ExperienceStoreLock` guarda su registro por ruta canónica a nivel de módulo (`_LOCK_REGISTRY`),
   no por instancia: así dos instancias diferentes de `ExperienceStore` sobre el mismo archivo
   comparten el mismo `RLock` y no se autobloquean ni rompen `msvcrt.locking` cuando reentran
   desde el mismo hilo.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time
from typing import Any, Iterator
from uuid import uuid4

from config import settings
from src.memory.migrations import (
    CURRENT_SCHEMA_VERSION,
    CORE_TABLES,
    MigrationResult,
    apply_migrations,
    bump_store_version_token_tx,
    configure_connection,
    fts5_available,
    read_store_version_components,
    rotate_store_version_token_tx,
    table_exists,
)
from src.memory.models import (
    ALLOWED_MEMORY_TRANSITIONS,
    BackupInfo,
    ExecutionInvalidationResult,
    IntegrityReport,
    MemoryAuditAction,
    MemoryAuditEntry,
    MemoryBackupError,
    MemoryCategory,
    MemoryCorruptionError,
    MemoryCreateInput,
    MemoryDuplicateError,
    MemoryIdempotencyConflictError,
    MemoryLockTimeoutError,
    MemoryNotFoundError,
    MemoryPersistenceError,
    MemoryPolicyError,
    MemoryProvenance,
    MemoryQueryFilter,
    MemoryRecord,
    MemoryRestoreError,
    MemoryRevision,
    MemoryScopeType,
    MemorySearchCandidate,
    MemorySource,
    MemoryStatus,
    MemoryTransitionError,
    MemoryValidationError,
    MemoryWriteResult,
    PurgeResult,
    RestoreResult,
    StoreStats,
    canonical_json,
    generate_memory_id,
    parse_iso_utc,
    utc_now_iso,
)
from src.memory.policy import (
    MemoryPolicy,
    PreparedMemoryCreate,
    compute_content_fingerprint,
    is_test_environment,
    normalize_scope,
    normalize_text_for_search,
    resolve_test_sandbox_root,
    sanitize_memory_text,
    validate_safe_backup_dir,
    validate_safe_local_db_path,
)

if os.name == "nt":
    import msvcrt
else:  # pragma: no cover - respaldo POSIX equivalente exigido por especificación
    import fcntl


# ------------------------------------------------------------------------------
# Protocolo ExperienceStoreLock (C4): RLock modular + msvcrt.locking / fcntl.flock
# ------------------------------------------------------------------------------


@dataclass
class _CanonicalLockEntry:
    canonical_key: str
    lock_file_path: Path
    rlock: threading.RLock
    owner_thread_id: int | None = None
    reentry_depth: int = 0
    lock_fd: int | None = None


_REGISTRY_MUTEX = threading.Lock()
_LOCK_REGISTRY: dict[str, _CanonicalLockEntry] = {}


def _canonical_lock_key(db_path: Path) -> str:
    return os.path.normcase(str(db_path.resolve()))


def _get_or_create_lock_entry(db_path: Path) -> _CanonicalLockEntry:
    resolved = db_path.resolve()
    key = os.path.normcase(str(resolved))
    with _REGISTRY_MUTEX:
        entry = _LOCK_REGISTRY.get(key)
        if entry is None:
            lock_file = resolved.with_name(f"{resolved.name}.lock")
            entry = _CanonicalLockEntry(
                canonical_key=key,
                lock_file_path=lock_file,
                rlock=threading.RLock(),
            )
            _LOCK_REGISTRY[key] = entry
        return entry


def _acquire_os_file_lock(fd: int) -> None:
    """Adquiere un bloqueo exclusivo no bloqueante de 1 byte sobre `<db>.lock`."""
    if os.name == "nt":
        # En Windows msvcrt.locking bloquea desde el puntero actual del archivo; fijamos offset 0.
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - rama POSIX
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_os_file_lock(fd: int) -> None:
    """Libera el bloqueo exclusivo sobre `<db>.lock`."""
    if os.name == "nt":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover - rama POSIX
        fcntl.flock(fd, fcntl.LOCK_UN)


class ExperienceStoreLock:
    """
    Lock híbrido reentrante intra-proceso + inter-proceso por ruta canónica de base SQLite.

    Justificación técnica junior:
    - Entre hilos o distintas instancias en el mismo proceso: serializa con `threading.RLock`.
    - Entre procesos separados (`python`, subprocesos): serializa con `msvcrt.locking` sobre `<db>.lock`.
    - Reentrancia en el mismo hilo (incluso con otra instancia de `ExperienceStore`): reutiliza el
      descriptor abierto sin re-bloquear ni soltar prematuramente el lock del SO hasta que el último
      bloque `with` sale.
    """

    def __init__(self, db_path: Path | str, *, timeout_sec: float = 5.0) -> None:
        validated = validate_safe_local_db_path(db_path)
        if isinstance(timeout_sec, bool) or not isinstance(timeout_sec, (int, float)) or timeout_sec <= 0:
            raise MemoryValidationError("timeout_sec de ExperienceStoreLock debe ser > 0.")
        self.db_path: Path = validated
        self.timeout_sec: float = float(timeout_sec)
        self._entry: _CanonicalLockEntry = _get_or_create_lock_entry(validated)
        self._held_depth: int = 0

    @property
    def lock_file_path(self) -> Path:
        return self._entry.lock_file_path

    def is_held_by_current_thread(self) -> bool:
        return (
            self._entry.reentry_depth > 0
            and self._entry.owner_thread_id == threading.get_ident()
        )

    def acquire(self, *, timeout_sec: float | None = None) -> None:
        effective_timeout = self.timeout_sec if timeout_sec is None else float(timeout_sec)
        if effective_timeout <= 0:
            raise MemoryValidationError("timeout_sec debe ser > 0.")
        deadline = time.monotonic() + effective_timeout

        if not self._entry.rlock.acquire(blocking=True, timeout=effective_timeout):
            raise MemoryLockTimeoutError(
                f"Timeout ({effective_timeout:.2f}s) esperando RLock local para '{self.db_path.name}'."
            )

        current_tid = threading.get_ident()
        acquired_os = False
        fd: int | None = None
        try:
            # Reentrancia en el mismo hilo (misma u otra instancia): no tocar lock OS otra vez.
            if self._entry.reentry_depth > 0 and self._entry.owner_thread_id == current_tid:
                self._entry.reentry_depth += 1
                self._held_depth += 1
                return

            self._entry.lock_file_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self._entry.lock_file_path), os.O_CREAT | os.O_RDWR, 0o600)
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\x00")
                os.lseek(fd, 0, os.SEEK_SET)

            while True:
                try:
                    _acquire_os_file_lock(fd)
                    acquired_os = True
                    break
                except OSError as exc:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise MemoryLockTimeoutError(
                            f"Timeout ({effective_timeout:.2f}s) esperando lock interproceso en '{self._entry.lock_file_path.name}'."
                        ) from exc
                    time.sleep(min(0.015, max(0.002, remaining)))

            self._entry.lock_fd = fd
            self._entry.owner_thread_id = current_tid
            self._entry.reentry_depth = 1
            self._held_depth += 1
        except Exception:
            if not acquired_os and fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._entry.rlock.release()
            raise

    def release(self) -> None:
        current_tid = threading.get_ident()
        if self._held_depth <= 0 or self._entry.owner_thread_id != current_tid or self._entry.reentry_depth <= 0:
            raise MemoryPersistenceError("Intento de liberar ExperienceStoreLock no poseído por el hilo actual.")

        self._held_depth -= 1
        self._entry.reentry_depth -= 1
        try:
            if self._entry.reentry_depth == 0:
                fd = self._entry.lock_fd
                self._entry.lock_fd = None
                self._entry.owner_thread_id = None
                if fd is not None:
                    try:
                        _release_os_file_lock(fd)
                    finally:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
        finally:
            self._entry.rlock.release()

    def __enter__(self) -> "ExperienceStoreLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


# ------------------------------------------------------------------------------
# Helpers de mapeo SQL -> Modelos tipados N8
# ------------------------------------------------------------------------------


def _default_db_path() -> Path:
    if is_test_environment():
        return resolve_test_sandbox_root() / "experiences.db"
    return settings.EXPERIENCES_DB_PATH


def _default_backup_dir() -> Path:
    if is_test_environment():
        return resolve_test_sandbox_root() / "backups" / "memory"
    return settings.MEMORY_BACKUP_DIR


def _compute_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def _escape_sql_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _row_to_memory_record(row: sqlite3.Row) -> MemoryRecord:
    tags_raw = json.loads(row["tags_json"] or "[]")
    meta_raw = json.loads(row["metadata_json"] or "{}")
    metadata_tuple = tuple((str(k), str(v)) for k, v in sorted(meta_raw.items()))
    provenance = MemoryProvenance(
        source=MemorySource(row["source"]),
        source_layer=row["source_layer"],
        source_goal_id=row["source_goal_id"],
        source_plan_id=row["source_plan_id"],
        source_task_id=row["source_task_id"],
        source_execution_id=row["source_execution_id"],
        approval_hash=row["approval_hash"],
        verifier_component=row["verifier_component"],
        verified_at=row["verified_at"],
        evidence_hash=row["evidence_hash"],
    )
    row_keys = set(row.keys())
    return MemoryRecord(
        memory_id=row["memory_id"],
        category=MemoryCategory(row["category"]),
        source=MemorySource(row["source"]),
        status=MemoryStatus(row["status"]),
        scope_type=MemoryScopeType(row["scope_type"]),
        scope_key=row["scope_key"],
        title=row["title"],
        content=row["content"],
        normalized_text=row["normalized_text"],
        summary=row["summary"],
        confidence=float(row["confidence"]),
        importance=int(row["importance"]),
        revision_number=int(row["revision_number"]),
        content_fingerprint=row["content_fingerprint"],
        tags=tuple(str(item) for item in tags_raw),
        metadata=metadata_tuple,
        provenance=provenance,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        superseded_by=row["superseded_by"],
        last_verified_at=row["last_verified_at"],
        access_count=int(row["access_count"]),
        canonical_unit_id=row["canonical_unit_id"] if "canonical_unit_id" in row_keys else None,
        idempotency_key=row["idempotency_key"] if "idempotency_key" in row_keys else None,
    )


def _row_to_revision(row: sqlite3.Row) -> MemoryRevision:
    return MemoryRevision(
        revision_id=row["revision_id"],
        memory_id=row["memory_id"],
        revision_number=int(row["revision_number"]),
        previous_title=row["previous_title"],
        previous_content=row["previous_content"],
        previous_summary=row["previous_summary"],
        previous_fingerprint=row["previous_fingerprint"],
        new_title=row["new_title"],
        new_content=row["new_content"],
        new_summary=row["new_summary"],
        new_fingerprint=row["new_fingerprint"],
        change_reason=row["change_reason"],
        changed_by=row["changed_by"],
        changed_at=row["changed_at"],
        source=MemorySource(row["source"]),
    )


def _row_to_audit(row: sqlite3.Row) -> MemoryAuditEntry:
    return MemoryAuditEntry(
        audit_id=row["audit_id"],
        action=MemoryAuditAction(row["action"]),
        memory_id=row["memory_id"],
        actor=row["actor"],
        reason=row["reason"],
        details_json=row["details_json"],
        created_at=row["created_at"],
    )


# ------------------------------------------------------------------------------
# ExperienceStore: almacén independiente N8.1
# ------------------------------------------------------------------------------


class ExperienceStore:
    """
    Almacén independiente y transaccional de memoria a largo plazo (`data/experiences.db`).

    Justificación técnica junior:
    - No hereda ni comparte archivo con `data/memory_store.json` ni con `goal_persistence` (N6).
    - Todas las operaciones normales y administrativas adquieren `ExperienceStoreLock`.
    - Las búsquedas textuales excluyen automáticamente recuerdos `REVOKED`/`EXPIRED`/`SUPERSEDED`.
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        policy: MemoryPolicy | None = None,
        lock_timeout_sec: float | None = None,
        busy_timeout_ms: int | None = None,
        enable_fts5: bool | None = None,
        auto_initialize: bool = True,
    ) -> None:
        raw_path = _default_db_path() if db_path is None else db_path
        self.db_path: Path = validate_safe_local_db_path(raw_path)
        self.policy: MemoryPolicy = policy or MemoryPolicy()
        self.lock_timeout_sec: float = (
            settings.N8_LOCK_TIMEOUT_SEC
            if lock_timeout_sec is None
            else float(lock_timeout_sec)
        )
        if self.lock_timeout_sec <= 0:
            raise MemoryValidationError("lock_timeout_sec debe ser > 0.")
        self.busy_timeout_ms: int = (
            settings.N8_SQLITE_BUSY_TIMEOUT_MS
            if busy_timeout_ms is None
            else max(250, int(busy_timeout_ms))
        )
        self.enable_fts5: bool = (
            settings.N8_FTS5_ENABLED if enable_fts5 is None else bool(enable_fts5)
        )
        if auto_initialize:
            self.initialize()

    @contextmanager
    def acquire_lock(self, *, timeout_sec: float | None = None) -> Iterator[ExperienceStoreLock]:
        """Adquiere el lock canónico compartido por todas las operaciones de lectura/escritura/mantenimiento."""
        lock = ExperienceStoreLock(
            self.db_path,
            timeout_sec=self.lock_timeout_sec if timeout_sec is None else timeout_sec,
        )
        with lock:
            yield lock

    @contextmanager
    def _open_connection(self, *, enable_wal: bool = False) -> Iterator[sqlite3.Connection]:
        """
        Abre una conexión SQLite efímera dentro del lock activo y garantiza su cierre previo al release.
        """
        probe_lock = ExperienceStoreLock(self.db_path, timeout_sec=self.lock_timeout_sec)
        if not probe_lock.is_held_by_current_thread():
            raise MemoryPersistenceError(
                "Invariant violated: _open_connection requiere ExperienceStoreLock activo."
            )

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=max(0.25, self.busy_timeout_ms / 1000.0),
                isolation_level=None,
            )
            configure_connection(
                conn,
                busy_timeout_ms=self.busy_timeout_ms,
                enable_wal=enable_wal,
            )
            yield conn
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" in msg or "busy" in msg:
                raise MemoryLockTimeoutError(f"SQLite ocupado/bloqueado en '{self.db_path.name}': {exc}") from exc
            if "not a database" in msg or "malformed" in msg or "corrupt" in msg:
                raise MemoryCorruptionError(f"Error de integridad/formato SQLite: {exc}") from exc
            raise MemoryPersistenceError(f"Error operacional SQLite en '{self.db_path.name}': {exc}") from exc
        except sqlite3.DatabaseError as exc:
            msg = str(exc).lower()
            if "not a database" in msg or "malformed" in msg or "corrupt" in msg:
                raise MemoryCorruptionError(f"Corrupción detectada en '{self.db_path.name}': {exc}") from exc
            raise MemoryPersistenceError(f"Error de base de datos SQLite: {exc}") from exc
        finally:
            if conn is not None:
                try:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                conn.close()

    def _ensure_ready(self, conn: sqlite3.Connection) -> bool:
        """Asegura esquema migrado cuando la instancia se creó con `auto_initialize=False`."""
        uv = conn.execute("PRAGMA user_version").fetchone()
        if not uv or int(uv[0]) < CURRENT_SCHEMA_VERSION:
            res = apply_migrations(
                conn,
                enable_fts5=self.enable_fts5,
                busy_timeout_ms=self.busy_timeout_ms,
            )
            return res.fts_active
        return self._is_fts_active(conn)

    @staticmethod
    def _is_fts_active(conn: sqlite3.Connection) -> bool:
        if not table_exists(conn, "memory_fts"):
            return False
        row = conn.execute("SELECT value FROM store_metadata WHERE key = 'fts_active'").fetchone()
        return bool(row and row[0] == "1")

    @staticmethod
    def _insert_audit_tx(
        conn: sqlite3.Connection,
        *,
        action: MemoryAuditAction,
        memory_id: str | None,
        actor: str,
        reason: str,
        details: dict[str, Any],
        created_at: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO memory_audit (audit_id, action, memory_id, actor, reason, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                generate_memory_id("aud"),
                action.value,
                memory_id,
                actor,
                reason,
                canonical_json(details),
                created_at,
            ),
        )

    @staticmethod
    def _sync_fts_active_row_tx(conn: sqlite3.Connection, record: MemoryRecord) -> None:
        conn.execute("DELETE FROM memory_fts WHERE memory_id = ?", (record.memory_id,))
        if record.status == MemoryStatus.ACTIVE:
            conn.execute(
                """
                INSERT INTO memory_fts (memory_id, title, summary, content, tags, scope_key)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.memory_id,
                    record.title,
                    record.summary,
                    record.content,
                    " ".join(record.tags),
                    record.scope_key,
                ),
            )

    def initialize(self) -> MigrationResult:
        """Inicializa o migra la base de datos bajo `ExperienceStoreLock` de forma idempotente."""
        self.policy.assert_enabled()
        with self.acquire_lock():
            with self._open_connection(enable_wal=True) as conn:
                return apply_migrations(
                    conn,
                    enable_fts5=self.enable_fts5,
                    busy_timeout_ms=self.busy_timeout_ms,
                )

    def get_store_version_token(self) -> str:
        """Consulta el `store_version_token` global vigente sin mutar estado ni avanzar la versión."""
        self.policy.assert_enabled()
        with self.acquire_lock():
            with self._open_connection() as conn:
                self._ensure_ready(conn)
                _, _, token = read_store_version_components(conn)
                return token

    @staticmethod
    def _execution_payload_matches(
        existing: MemoryRecord,
        prepared: PreparedMemoryCreate,
    ) -> tuple[bool, tuple[str, ...]]:
        """
        Compara evidencia y payload estructural entre un registro `EXECUTION_VERIFIED` previo y
        un nuevo intento con igual `idempotency_key` o unidad canónica, sin filtrar texto en mensajes.
        """
        mismatches: list[str] = []
        checks = (
            ("category", existing.category == prepared.category),
            ("scope_type", existing.scope_type == prepared.scope_type),
            ("scope_key", existing.scope_key == prepared.scope_key),
            ("title", existing.title == prepared.title),
            ("content", existing.content == prepared.content),
            ("summary", existing.summary == prepared.summary),
            ("content_fingerprint", existing.content_fingerprint == prepared.content_fingerprint),
            ("tags", existing.tags == prepared.tags),
            ("importance", existing.importance == prepared.importance),
            ("confidence", abs(existing.confidence - prepared.confidence) < 1e-9),
            ("canonical_unit_id", existing.canonical_unit_id == prepared.canonical_unit_id),
            ("expires_at", existing.expires_at == prepared.expires_at),
            ("source_layer", existing.provenance.source_layer == prepared.provenance.source_layer),
            ("source_execution_id", existing.provenance.source_execution_id == prepared.provenance.source_execution_id),
            ("source_plan_id", existing.provenance.source_plan_id == prepared.provenance.source_plan_id),
            ("source_task_id", existing.provenance.source_task_id == prepared.provenance.source_task_id),
            ("approval_hash", existing.provenance.approval_hash == prepared.provenance.approval_hash),
            ("evidence_hash", existing.provenance.evidence_hash == prepared.provenance.evidence_hash),
        )
        for field_name, matched in checks:
            if not matched:
                mismatches.append(field_name)
        return (len(mismatches) == 0), tuple(mismatches)

    def create_memory(
        self,
        payload: MemoryCreateInput,
        *,
        private_mode: bool = False,
        actor: str = "system",
    ) -> MemoryWriteResult:
        """
        Valida políticas, sanitiza y persiste un recuerdo aplicando reglas diferenciadas:
        - `EXECUTION_VERIFIED`: idempotencia estricta por `idempotency_key` antes de cualquier otra regla,
          rechazo auditable ante evidencia/payload distinto y no fusión por `content_fingerprint` entre ejecuciones.
        - Orígenes convencionales (`USER_EXPLICIT` / `DECISION_CONFIRMED`): dedupe por `content_fingerprint`.
        """
        prepared = self.policy.prepare_create(payload, private_mode=private_mode)
        clean_actor = sanitize_memory_text(
            actor,
            max_chars=64,
            allow_newlines=False,
            field_name="actor",
            check_secrets=True,
        )

        with self.acquire_lock():
            with self._open_connection(enable_wal=True) as conn:
                fts_active = self._ensure_ready(conn)
                now = utc_now_iso()
                conn.execute("BEGIN IMMEDIATE")
                try:
                    # 1) Para EXECUTION_VERIFIED la idempotencia se resuelve primero y nunca por texto/fingerprint.
                    if prepared.source == MemorySource.EXECUTION_VERIFIED:
                        assert prepared.idempotency_key is not None
                        dup_idem = conn.execute(
                            """
                            SELECT * FROM memories
                            WHERE idempotency_key = ?
                            LIMIT 1
                            """,
                            (prepared.idempotency_key,),
                        ).fetchone()
                        if dup_idem is not None:
                            existing = _row_to_memory_record(dup_idem)
                            matches, mismatches = self._execution_payload_matches(existing, prepared)
                            if matches:
                                self._insert_audit_tx(
                                    conn,
                                    action=MemoryAuditAction.DEDUPE_HIT,
                                    memory_id=existing.memory_id,
                                    actor=clean_actor,
                                    reason="idempotency_key",
                                    details={
                                        "idempotency_key": prepared.idempotency_key,
                                        "canonical_unit_id": prepared.canonical_unit_id,
                                        "existing_status": existing.status.value,
                                    },
                                    created_at=now,
                                )
                                conn.execute("COMMIT")
                                return MemoryWriteResult(
                                    record=existing,
                                    created=False,
                                    deduplicated=True,
                                    matched_reason="idempotency_key",
                                )

                            self._insert_audit_tx(
                                conn,
                                action=MemoryAuditAction.REJECT_IDEMPOTENCY_CONFLICT,
                                memory_id=existing.memory_id,
                                actor=clean_actor,
                                reason="idempotency_key_payload_mismatch",
                                details={
                                    "idempotency_key": prepared.idempotency_key,
                                    "canonical_unit_id": prepared.canonical_unit_id,
                                    "mismatched_fields": list(mismatches),
                                },
                                created_at=now,
                            )
                            # Persistimos la auditoría del rechazo sin modificar recuerdos ni avanzar store_version_token.
                            conn.execute("COMMIT")
                            raise MemoryIdempotencyConflictError(
                                "Conflicto de idempotencia: ya existe un registro con la misma clave y evidencia/payload incompatible "
                                f"(campos en conflicto: {', '.join(mismatches)})."
                            )

                        # Segunda línea de defensa: impedir otra clave distinta para la misma unidad canónica ACTIVA.
                        dup_unit = conn.execute(
                            """
                            SELECT * FROM memories
                            WHERE source = 'EXECUTION_VERIFIED'
                              AND source_layer = ?
                              AND source_execution_id = ?
                              AND COALESCE(source_plan_id, '') = ?
                              AND COALESCE(source_task_id, '') = ?
                              AND canonical_unit_id = ?
                              AND status = 'ACTIVE'
                            LIMIT 1
                            """,
                            (
                                prepared.provenance.source_layer,
                                prepared.provenance.source_execution_id,
                                prepared.provenance.source_plan_id or "",
                                prepared.provenance.source_task_id or "",
                                prepared.canonical_unit_id,
                            ),
                        ).fetchone()
                        if dup_unit is not None:
                            existing = _row_to_memory_record(dup_unit)
                            self._insert_audit_tx(
                                conn,
                                action=MemoryAuditAction.REJECT_IDEMPOTENCY_CONFLICT,
                                memory_id=existing.memory_id,
                                actor=clean_actor,
                                reason="canonical_unit_conflict",
                                details={
                                    "idempotency_key": prepared.idempotency_key,
                                    "existing_idempotency_key": existing.idempotency_key,
                                    "canonical_unit_id": prepared.canonical_unit_id,
                                },
                                created_at=now,
                            )
                            conn.execute("COMMIT")
                            raise MemoryIdempotencyConflictError(
                                "Conflicto de unidad canónica: ya existe un recuerdo activo para esa unidad de ejecución con distinta idempotency_key."
                            )
                    else:
                        # 2) Dedupe convencional por huella canónica únicamente entre recuerdos NO EXECUTION_VERIFIED.
                        dup_fp = conn.execute(
                            """
                            SELECT * FROM memories
                            WHERE content_fingerprint = ?
                              AND status = 'ACTIVE'
                              AND source != 'EXECUTION_VERIFIED'
                            LIMIT 1
                            """,
                            (prepared.content_fingerprint,),
                        ).fetchone()
                        if dup_fp is not None:
                            existing = _row_to_memory_record(dup_fp)
                            self._insert_audit_tx(
                                conn,
                                action=MemoryAuditAction.DEDUPE_HIT,
                                memory_id=existing.memory_id,
                                actor=clean_actor,
                                reason="content_fingerprint",
                                details={"content_fingerprint": prepared.content_fingerprint},
                                created_at=now,
                            )
                            conn.execute("COMMIT")
                            return MemoryWriteResult(
                                record=existing,
                                created=False,
                                deduplicated=True,
                                matched_reason="content_fingerprint",
                            )

                    # 3) Control de capacidad activa.
                    active_count_row = conn.execute(
                        "SELECT COUNT(*) FROM memories WHERE status = 'ACTIVE'"
                    ).fetchone()
                    active_count = int(active_count_row[0]) if active_count_row else 0
                    if active_count >= self.policy.max_active_memories:
                        raise MemoryPolicyError(
                            f"Capacidad máxima de recuerdos activos alcanzada ({self.policy.max_active_memories})."
                        )

                    memory_id = generate_memory_id("mem")
                    tags_json = canonical_json(list(prepared.tags))
                    metadata_json = canonical_json({k: v for k, v in prepared.metadata})
                    evidence_json = (
                        canonical_json(prepared.verification_evidence.to_dict())
                        if prepared.verification_evidence is not None
                        else None
                    )
                    last_verified_at = (
                        prepared.verification_evidence.verified_at
                        if prepared.verification_evidence is not None
                        else None
                    )

                    conn.execute(
                        """
                        INSERT INTO memories (
                            memory_id, category, source, status, scope_type, scope_key,
                            title, content, normalized_text, summary,
                            confidence, importance, revision_number, content_fingerprint,
                            tags_json, metadata_json,
                            source_layer, source_goal_id, source_plan_id, source_task_id,
                            source_execution_id, approval_hash, verifier_component, verified_at,
                            evidence_hash, evidence_json,
                            created_at, updated_at, expires_at, revoked_at,
                            superseded_by, last_verified_at, access_count,
                            canonical_unit_id, idempotency_key
                        )
                        VALUES (?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, 0, ?, ?)
                        """,
                        (
                            memory_id,
                            prepared.category.value,
                            prepared.source.value,
                            prepared.scope_type.value,
                            prepared.scope_key,
                            prepared.title,
                            prepared.content,
                            prepared.normalized_text,
                            prepared.summary,
                            prepared.confidence,
                            prepared.importance,
                            prepared.content_fingerprint,
                            tags_json,
                            metadata_json,
                            prepared.provenance.source_layer,
                            prepared.provenance.source_goal_id,
                            prepared.provenance.source_plan_id,
                            prepared.provenance.source_task_id,
                            prepared.provenance.source_execution_id,
                            prepared.provenance.approval_hash,
                            prepared.provenance.verifier_component,
                            prepared.provenance.verified_at,
                            prepared.provenance.evidence_hash,
                            evidence_json,
                            now,
                            now,
                            prepared.expires_at,
                            last_verified_at,
                            prepared.canonical_unit_id,
                            prepared.idempotency_key,
                        ),
                    )

                    row = conn.execute(
                        "SELECT * FROM memories WHERE memory_id = ?",
                        (memory_id,),
                    ).fetchone()
                    if row is None:
                        raise MemoryPersistenceError("Fallo al leer el recuerdo recién insertado.")
                    created_record = _row_to_memory_record(row)

                    if fts_active:
                        self._sync_fts_active_row_tx(conn, created_record)

                    new_ver_token = bump_store_version_token_tx(conn, now=now)

                    self._insert_audit_tx(
                        conn,
                        action=MemoryAuditAction.CREATE,
                        memory_id=memory_id,
                        actor=clean_actor,
                        reason="memory_created",
                        details={
                            "category": prepared.category.value,
                            "source": prepared.source.value,
                            "scope_type": prepared.scope_type.value,
                            "scope_key": prepared.scope_key,
                            "store_version_token": new_ver_token,
                        },
                        created_at=now,
                    )
                    conn.execute("COMMIT")
                    return MemoryWriteResult(record=created_record, created=True, deduplicated=False)
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise

    def get_memory(self, memory_id: str) -> MemoryRecord | None:
        """Consulta un recuerdo por ID respetando el protocolo de locking."""
        self.policy.assert_enabled()
        if not isinstance(memory_id, str) or not memory_id.strip():
            raise MemoryValidationError("memory_id no puede estar vacío.")
        with self.acquire_lock():
            with self._open_connection() as conn:
                self._ensure_ready(conn)
                row = conn.execute(
                    "SELECT * FROM memories WHERE memory_id = ?",
                    (memory_id.strip(),),
                ).fetchone()
                return _row_to_memory_record(row) if row is not None else None

    def get_status(self, memory_id: str) -> MemoryStatus | None:
        """Consulta el estado de ciclo de vida de un recuerdo sin traer todo el contenido."""
        self.policy.assert_enabled()
        if not isinstance(memory_id, str) or not memory_id.strip():
            raise MemoryValidationError("memory_id no puede estar vacío.")
        with self.acquire_lock():
            with self._open_connection() as conn:
                self._ensure_ready(conn)
                row = conn.execute(
                    "SELECT status FROM memories WHERE memory_id = ?",
                    (memory_id.strip(),),
                ).fetchone()
                return MemoryStatus(row["status"]) if row is not None else None

    def list_memories(
        self,
        query_filter: MemoryQueryFilter | None = None,
        *,
        status: MemoryStatus | None = MemoryStatus.ACTIVE,
        category: MemoryCategory | None = None,
        source: MemorySource | None = None,
        scope_type: MemoryScopeType | None = None,
        scope_key: str | None = None,
        pending_only: bool = False,
        exclude_pending: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MemoryRecord]:
        """Lista recuerdos con filtros deterministas y orden estable."""
        self.policy.assert_enabled()
        flt = query_filter or MemoryQueryFilter(
            status=status,
            category=category,
            source=source,
            scope_type=scope_type,
            scope_key=scope_key,
            limit=limit,
            offset=offset,
        )
        eff_limit = max(1, min(1000, int(flt.limit)))
        eff_offset = max(0, int(flt.offset))

        clauses: list[str] = []
        params: list[Any] = []
        if flt.status is not None:
            clauses.append("status = ?")
            params.append(MemoryStatus(flt.status).value)
        if flt.category is not None:
            clauses.append("category = ?")
            params.append(MemoryCategory(flt.category).value)
        if flt.source is not None:
            clauses.append("source = ?")
            params.append(MemorySource(flt.source).value)
        if flt.scope_type is not None:
            norm_st, norm_sk = (
                (MemoryScopeType.GLOBAL, "global")
                if flt.scope_type == MemoryScopeType.GLOBAL
                else (
                    normalize_scope(flt.scope_type, flt.scope_key)
                    if flt.scope_key
                    else (MemoryScopeType(flt.scope_type), None)
                )
            )
            clauses.append("scope_type = ?")
            params.append(norm_st.value)
            if norm_sk is not None and flt.scope_key is not None:
                clauses.append("scope_key = ?")
                params.append(norm_sk)
        elif flt.scope_key is not None:
            clauses.append("scope_key = ?")
            params.append(flt.scope_key.strip().lower())

        if pending_only:
            clauses.append("json_extract(metadata_json, '$.confirmation_state') = 'pending'")
        elif exclude_pending:
            clauses.append("COALESCE(json_extract(metadata_json, '$.confirmation_state'), 'confirmed') != 'pending'")

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT * FROM memories
            {where_sql}
            ORDER BY updated_at DESC, created_at DESC, memory_id ASC
            LIMIT ? OFFSET ?
        """
        params.extend([eff_limit, eff_offset])

        with self.acquire_lock():
            with self._open_connection() as conn:
                self._ensure_ready(conn)
                rows = conn.execute(sql, tuple(params)).fetchall()
                return [_row_to_memory_record(r) for r in rows]

    def update_memory_with_revision(
        self,
        memory_id: str,
        *,
        title: str | None = None,
        content: str | None = None,
        summary: str | None = None,
        confidence: float | None = None,
        importance: int | None = None,
        tags: tuple[str, ...] | None = None,
        reason: str,
        actor: str = "user",
        source: MemorySource | None = None,
        private_mode: bool = False,
    ) -> MemoryRecord:
        """
        Actualiza un recuerdo ACTIVE registrando su estado previo como revisión inmutable.
        """
        self.policy.assert_write_allowed(private_mode=private_mode)
        if not isinstance(memory_id, str) or not memory_id.strip():
            raise MemoryValidationError("memory_id no puede estar vacío.")
        clean_id = memory_id.strip()

        clean_reason = sanitize_memory_text(
            reason,
            max_chars=240,
            allow_newlines=False,
            field_name="reason",
            check_secrets=True,
        )
        clean_actor = sanitize_memory_text(
            actor,
            max_chars=64,
            allow_newlines=False,
            field_name="actor",
            check_secrets=True,
        )

        with self.acquire_lock():
            with self._open_connection() as conn:
                fts_active = self._ensure_ready(conn)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    current_row = conn.execute(
                        "SELECT * FROM memories WHERE memory_id = ?",
                        (clean_id,),
                    ).fetchone()
                    if current_row is None:
                        raise MemoryNotFoundError(f"No existe el recuerdo '{clean_id}'.")

                    current = _row_to_memory_record(current_row)
                    if current.status != MemoryStatus.ACTIVE:
                        raise MemoryTransitionError(
                            f"Solo se pueden revisar recuerdos ACTIVE (estado actual: {current.status.value})."
                        )

                    if current.revision_number >= self.policy.max_revisions_per_memory:
                        raise MemoryPolicyError(
                            f"Límite máximo de revisiones ({self.policy.max_revisions_per_memory}) alcanzado para '{clean_id}'."
                        )

                    new_title = (
                        sanitize_memory_text(
                            title,
                            max_chars=self.policy.max_title_chars,
                            allow_newlines=False,
                            field_name="title",
                        )
                        if title is not None
                        else current.title
                    )
                    new_content = (
                        sanitize_memory_text(
                            content,
                            max_chars=self.policy.max_content_chars,
                            allow_newlines=True,
                            field_name="content",
                        )
                        if content is not None
                        else current.content
                    )
                    if summary is not None:
                        new_summary = sanitize_memory_text(
                            summary,
                            max_chars=self.policy.max_summary_chars,
                            allow_newlines=False,
                            field_name="summary",
                        )
                    elif content is not None:
                        compact = " ".join(new_content.replace("\n", " ").split())
                        new_summary = compact[: self.policy.max_summary_chars]
                        if len(compact) > self.policy.max_summary_chars:
                            new_summary = new_summary[: max(1, self.policy.max_summary_chars - 3)].rstrip() + "..."
                    else:
                        new_summary = current.summary

                    if confidence is not None:
                        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                            raise MemoryValidationError("confidence debe ser numérico en [0.0, 1.0].")
                        new_confidence = float(confidence)
                        if not (0.0 <= new_confidence <= 1.0):
                            raise MemoryValidationError("confidence fuera de rango [0.0, 1.0].")
                    else:
                        new_confidence = current.confidence

                    if importance is not None:
                        if isinstance(importance, bool) or not isinstance(importance, int) or not (1 <= importance <= 100):
                            raise MemoryValidationError("importance debe ser entero en [1, 100].")
                        new_importance = int(importance)
                    else:
                        new_importance = current.importance

                    new_tags = self.policy.sanitize_tags(tags) if tags is not None else current.tags
                    rev_source = current.source if source is None else MemorySource(source)

                    new_normalized = normalize_text_for_search(f"{new_title} {new_content}")
                    new_fingerprint = compute_content_fingerprint(
                        category=current.category,
                        scope_type=current.scope_type,
                        scope_key=current.scope_key,
                        normalized_text=new_normalized,
                    )

                    if current.source != MemorySource.EXECUTION_VERIFIED:
                        conflict = conn.execute(
                            """
                            SELECT memory_id FROM memories
                            WHERE content_fingerprint = ?
                              AND status = 'ACTIVE'
                              AND source != 'EXECUTION_VERIFIED'
                              AND memory_id != ?
                            LIMIT 1
                            """,
                            (new_fingerprint, clean_id),
                        ).fetchone()
                        if conflict is not None:
                            raise MemoryDuplicateError(
                                f"La revisión colisiona con el recuerdo activo '{conflict['memory_id']}'."
                            )

                    now = utc_now_iso()
                    new_rev_num = current.revision_number + 1
                    revision_id = generate_memory_id("rev")

                    conn.execute(
                        """
                        INSERT INTO memory_revisions (
                            revision_id, memory_id, revision_number,
                            previous_title, previous_content, previous_summary, previous_fingerprint,
                            new_title, new_content, new_summary, new_fingerprint,
                            change_reason, changed_by, changed_at, source
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            revision_id,
                            clean_id,
                            new_rev_num,
                            current.title,
                            current.content,
                            current.summary,
                            current.content_fingerprint,
                            new_title,
                            new_content,
                            new_summary,
                            new_fingerprint,
                            clean_reason,
                            clean_actor,
                            now,
                            rev_source.value,
                        ),
                    )

                    conn.execute(
                        """
                        UPDATE memories
                        SET title = ?,
                            content = ?,
                            normalized_text = ?,
                            summary = ?,
                            confidence = ?,
                            importance = ?,
                            revision_number = ?,
                            content_fingerprint = ?,
                            tags_json = ?,
                            updated_at = ?
                        WHERE memory_id = ?
                        """,
                        (
                            new_title,
                            new_content,
                            new_normalized,
                            new_summary,
                            new_confidence,
                            new_importance,
                            new_rev_num,
                            new_fingerprint,
                            canonical_json(list(new_tags)),
                            now,
                            clean_id,
                        ),
                    )

                    updated_row = conn.execute(
                        "SELECT * FROM memories WHERE memory_id = ?",
                        (clean_id,),
                    ).fetchone()
                    if updated_row is None:
                        raise MemoryPersistenceError("Fallo al leer el recuerdo actualizado.")
                    updated = _row_to_memory_record(updated_row)

                    if fts_active:
                        self._sync_fts_active_row_tx(conn, updated)

                    new_ver_token = bump_store_version_token_tx(conn, now=now)

                    self._insert_audit_tx(
                        conn,
                        action=MemoryAuditAction.REVISE,
                        memory_id=clean_id,
                        actor=clean_actor,
                        reason=clean_reason,
                        details={
                            "revision_id": revision_id,
                            "revision_number": new_rev_num,
                            "store_version_token": new_ver_token,
                        },
                        created_at=now,
                    )
                    conn.execute("COMMIT")
                    return updated
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise

    def revoke_memory(
        self,
        memory_id: str,
        *,
        reason: str,
        actor: str = "user",
    ) -> MemoryRecord:
        """Revoca lógicamente un recuerdo y lo purga de `memory_fts` de forma transaccional."""
        return self.transition_status(
            memory_id,
            new_status=MemoryStatus.REVOKED,
            reason=reason,
            actor=actor,
        )

    def invalidate_by_execution(
        self,
        execution_id: str,
        *,
        reason: str = "rollback_execution",
        actor: str = "system",
        private_mode: bool = False,
    ) -> ExecutionInvalidationResult:
        """
        Invalida lógicamente (`REVOKED`) los recuerdos `EXECUTION_VERIFIED` activos asociados
        a `execution_id`, purgándolos de `memory_fts` de forma idempotente y auditable.

        Justificación técnica junior:
        Se utiliza `REVOKED` y nunca `SUPERSEDED` porque la máquina de estados N8 exige un
        `superseded_by` válido hacia otro registro. Si no existen recuerdos activos para esa
        ejecución (o ya fueron invalidados antes), no muta datos ni avanza `store_version_token`.
        """
        self.policy.assert_write_allowed(private_mode=private_mode)
        if not isinstance(execution_id, str) or not execution_id.strip():
            raise MemoryValidationError("execution_id no puede estar vacío.")
        clean_exec_id = sanitize_memory_text(
            execution_id,
            max_chars=128,
            allow_newlines=False,
            field_name="execution_id",
            check_secrets=True,
        )
        clean_reason = sanitize_memory_text(
            reason,
            max_chars=240,
            allow_newlines=False,
            field_name="reason",
            check_secrets=True,
        )
        clean_actor = sanitize_memory_text(
            actor,
            max_chars=64,
            allow_newlines=False,
            field_name="actor",
            check_secrets=True,
        )

        with self.acquire_lock():
            with self._open_connection(enable_wal=True) as conn:
                fts_active = self._ensure_ready(conn)
                now = utc_now_iso()
                conn.execute("BEGIN IMMEDIATE")
                try:
                    active_rows = conn.execute(
                        """
                        SELECT memory_id FROM memories
                        WHERE source = 'EXECUTION_VERIFIED'
                          AND source_execution_id = ?
                          AND status = 'ACTIVE'
                        ORDER BY created_at ASC, memory_id ASC
                        """,
                        (clean_exec_id,),
                    ).fetchall()
                    already_row = conn.execute(
                        """
                        SELECT COUNT(*) FROM memories
                        WHERE source = 'EXECUTION_VERIFIED'
                          AND source_execution_id = ?
                          AND status != 'ACTIVE'
                        """,
                        (clean_exec_id,),
                    ).fetchone()
                    already_inactive = int(already_row[0]) if already_row else 0

                    invalidated_ids: list[str] = []
                    if active_rows:
                        new_token = bump_store_version_token_tx(conn, now=now)
                        for row in active_rows:
                            mem_id = str(row["memory_id"])
                            conn.execute(
                                """
                                UPDATE memories
                                SET status = 'REVOKED',
                                    updated_at = ?,
                                    revoked_at = ?
                                WHERE memory_id = ?
                                """,
                                (now, now, mem_id),
                            )
                            if fts_active:
                                conn.execute("DELETE FROM memory_fts WHERE memory_id = ?", (mem_id,))
                            self._insert_audit_tx(
                                conn,
                                action=MemoryAuditAction.REVOKE,
                                memory_id=mem_id,
                                actor=clean_actor,
                                reason=clean_reason,
                                details={
                                    "from_status": MemoryStatus.ACTIVE.value,
                                    "to_status": MemoryStatus.REVOKED.value,
                                    "source_execution_id": clean_exec_id,
                                    "invalidation_kind": "rollback_execution",
                                    "store_version_token": new_token,
                                },
                                created_at=now,
                            )
                            invalidated_ids.append(mem_id)
                    else:
                        _, _, new_token = read_store_version_components(conn)

                    conn.execute("COMMIT")
                    return ExecutionInvalidationResult(
                        execution_id=clean_exec_id,
                        invalidated_count=len(invalidated_ids),
                        invalidated_memory_ids=tuple(invalidated_ids),
                        already_inactive_count=already_inactive,
                        store_version_token=new_token,
                        reason=clean_reason,
                    )
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise

    @staticmethod
    def _has_rollback_audit_tx(conn: sqlite3.Connection, execution_id: str) -> bool:
        """Comprueba exactamente mediante JSON parseado si existe marca o revocación por rollback."""
        like_pat = f"%{execution_id}%"
        rows = conn.execute(
            """
            SELECT details_json FROM memory_audit
            WHERE action = 'REVOKE' AND details_json LIKE ?
            ORDER BY created_at DESC
            LIMIT 100
            """,
            (like_pat,),
        ).fetchall()
        for row in rows:
            try:
                parsed = json.loads(str(row["details_json"]))
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                isinstance(parsed, dict)
                and parsed.get("source_execution_id") == execution_id
                and parsed.get("invalidation_kind")
                in {"rollback_execution", "rollback_execution_tombstone"}
            ):
                return True
        return False

    def is_execution_invalidated(self, execution_id: str) -> bool:
        """Indica si `execution_id` figura revertida por rollback en memoria persistente."""
        self.policy.assert_enabled()
        if not isinstance(execution_id, str) or not execution_id.strip():
            raise MemoryValidationError("execution_id no puede estar vacío.")
        clean_exec_id = sanitize_memory_text(
            execution_id,
            max_chars=128,
            allow_newlines=False,
            field_name="execution_id",
            check_secrets=True,
        )
        with self.acquire_lock():
            with self._open_connection() as conn:
                self._ensure_ready(conn)
                row = conn.execute(
                    """
                    SELECT 1 FROM memories
                    WHERE source = 'EXECUTION_VERIFIED'
                      AND source_execution_id = ?
                      AND status = 'REVOKED'
                    LIMIT 1
                    """,
                    (clean_exec_id,),
                ).fetchone()
                if row is not None:
                    return True
                return self._has_rollback_audit_tx(conn, clean_exec_id)

    def mark_execution_rollback_tombstone(
        self,
        execution_id: str,
        *,
        reason: str = "rollback_execution",
        actor: str = "system",
        private_mode: bool = False,
    ) -> bool:
        """
        Registra de forma idempotente en `memory_audit` una marca persistente de rollback confirmado
        cuando todavía no existían recuerdos activos en `memories`, bloqueando capturas tardías futuras.
        """
        self.policy.assert_write_allowed(private_mode=private_mode)
        if not isinstance(execution_id, str) or not execution_id.strip():
            raise MemoryValidationError("execution_id no puede estar vacío.")
        clean_exec_id = sanitize_memory_text(
            execution_id,
            max_chars=128,
            allow_newlines=False,
            field_name="execution_id",
            check_secrets=True,
        )
        clean_reason = sanitize_memory_text(
            reason,
            max_chars=240,
            allow_newlines=False,
            field_name="reason",
            check_secrets=True,
        )
        clean_actor = sanitize_memory_text(
            actor,
            max_chars=64,
            allow_newlines=False,
            field_name="actor",
            check_secrets=True,
        )
        with self.acquire_lock():
            with self._open_connection(enable_wal=True) as conn:
                self._ensure_ready(conn)
                now = utc_now_iso()
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if self._has_rollback_audit_tx(conn, clean_exec_id):
                        conn.execute("COMMIT")
                        return False
                    self._insert_audit_tx(
                        conn,
                        action=MemoryAuditAction.REVOKE,
                        memory_id=None,
                        actor=clean_actor,
                        reason=clean_reason,
                        details={
                            "source_execution_id": clean_exec_id,
                            "invalidation_kind": "rollback_execution_tombstone",
                        },
                        created_at=now,
                    )
                    conn.execute("COMMIT")
                    return True
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise

    def transition_status(
        self,
        memory_id: str,
        *,
        new_status: MemoryStatus,
        reason: str,
        actor: str = "system",
        superseded_by: str | None = None,
    ) -> MemoryRecord:
        """
        Ejecuta una transición controlada de ciclo de vida respetando `ALLOWED_MEMORY_TRANSITIONS`.
        """
        self.policy.assert_enabled()
        if not isinstance(memory_id, str) or not memory_id.strip():
            raise MemoryValidationError("memory_id no puede estar vacío.")
        clean_id = memory_id.strip()
        target_status = MemoryStatus(new_status)

        clean_reason = sanitize_memory_text(
            reason,
            max_chars=240,
            allow_newlines=False,
            field_name="reason",
            check_secrets=True,
        )
        clean_actor = sanitize_memory_text(
            actor,
            max_chars=64,
            allow_newlines=False,
            field_name="actor",
            check_secrets=True,
        )
        clean_superseded_by = superseded_by.strip() if isinstance(superseded_by, str) and superseded_by.strip() else None
        if clean_superseded_by == clean_id:
            raise MemoryValidationError("superseded_by no puede apuntar al mismo memory_id.")
        if target_status == MemoryStatus.SUPERSEDED and not clean_superseded_by:
            raise MemoryValidationError("Transición a SUPERSEDED requiere superseded_by.")

        with self.acquire_lock():
            with self._open_connection() as conn:
                fts_active = self._ensure_ready(conn)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    row = conn.execute(
                        "SELECT * FROM memories WHERE memory_id = ?",
                        (clean_id,),
                    ).fetchone()
                    if row is None:
                        raise MemoryNotFoundError(f"No existe el recuerdo '{clean_id}'.")

                    current = _row_to_memory_record(row)
                    allowed_next = ALLOWED_MEMORY_TRANSITIONS.get(current.status, frozenset())
                    if target_status not in allowed_next:
                        raise MemoryTransitionError(
                            f"Transición prohibida: {current.status.value} -> {target_status.value}."
                        )

                    if clean_superseded_by is not None:
                        target_exists = conn.execute(
                            "SELECT 1 FROM memories WHERE memory_id = ?",
                            (clean_superseded_by,),
                        ).fetchone()
                        if target_exists is None:
                            raise MemoryNotFoundError(
                                f"superseded_by referencia a '{clean_superseded_by}' inexistente."
                            )

                    now = utc_now_iso()
                    revoked_at = now if target_status == MemoryStatus.REVOKED else current.revoked_at
                    sup_id = (
                        clean_superseded_by
                        if target_status == MemoryStatus.SUPERSEDED
                        else current.superseded_by
                    )

                    conn.execute(
                        """
                        UPDATE memories
                        SET status = ?,
                            updated_at = ?,
                            revoked_at = ?,
                            superseded_by = ?
                        WHERE memory_id = ?
                        """,
                        (
                            target_status.value,
                            now,
                            revoked_at,
                            sup_id,
                            clean_id,
                        ),
                    )

                    if fts_active:
                        conn.execute("DELETE FROM memory_fts WHERE memory_id = ?", (clean_id,))

                    new_ver_token = bump_store_version_token_tx(conn, now=now)

                    audit_action = {
                        MemoryStatus.REVOKED: MemoryAuditAction.REVOKE,
                        MemoryStatus.EXPIRED: MemoryAuditAction.EXPIRE,
                        MemoryStatus.SUPERSEDED: MemoryAuditAction.SUPERSEDE,
                    }[target_status]
                    self._insert_audit_tx(
                        conn,
                        action=audit_action,
                        memory_id=clean_id,
                        actor=clean_actor,
                        reason=clean_reason,
                        details={
                            "from_status": current.status.value,
                            "to_status": target_status.value,
                            "superseded_by": sup_id,
                            "store_version_token": new_ver_token,
                        },
                        created_at=now,
                    )

                    updated_row = conn.execute(
                        "SELECT * FROM memories WHERE memory_id = ?",
                        (clean_id,),
                    ).fetchone()
                    if updated_row is None:
                        raise MemoryPersistenceError("Fallo al leer recuerdo tras cambio de estado.")
                    conn.execute("COMMIT")
                    return _row_to_memory_record(updated_row)
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise

    def get_revisions(self, memory_id: str) -> list[MemoryRevision]:
        """Devuelve el historial inmutable de revisiones de un recuerdo."""
        self.policy.assert_enabled()
        if not isinstance(memory_id, str) or not memory_id.strip():
            raise MemoryValidationError("memory_id no puede estar vacío.")
        with self.acquire_lock():
            with self._open_connection() as conn:
                self._ensure_ready(conn)
                rows = conn.execute(
                    """
                    SELECT * FROM memory_revisions
                    WHERE memory_id = ?
                    ORDER BY revision_number ASC
                    """,
                    (memory_id.strip(),),
                ).fetchall()
                return [_row_to_revision(r) for r in rows]

    def get_audit_entries(
        self,
        *,
        memory_id: str | None = None,
        limit: int = 100,
    ) -> list[MemoryAuditEntry]:
        """Consulta las entradas registradas en `memory_audit`."""
        self.policy.assert_enabled()
        eff_limit = max(1, min(1000, int(limit)))
        with self.acquire_lock():
            with self._open_connection() as conn:
                self._ensure_ready(conn)
                if memory_id is not None and memory_id.strip():
                    rows = conn.execute(
                        """
                        SELECT * FROM memory_audit
                        WHERE memory_id = ?
                        ORDER BY rowid DESC
                        LIMIT ?
                        """,
                        (memory_id.strip(), eff_limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT * FROM memory_audit
                        ORDER BY rowid DESC
                        LIMIT ?
                        """,
                        (eff_limit,),
                    ).fetchall()
                return [_row_to_audit(r) for r in rows]

    def search_text_candidates(
        self,
        query: str,
        *,
        scope_type: MemoryScopeType | None = None,
        scope_key: str | None = None,
        categories: tuple[MemoryCategory, ...] | None = None,
        exclude_pending: bool = False,
        max_tokens: int | None = None,
        limit: int = 20,
        force_sql_fallback: bool = False,
    ) -> list[MemorySearchCandidate]:
        """
        Primitiva de almacenamiento para buscar recuerdos ACTIVOS mediante FTS5 con respaldo SQL LIKE.
        """
        candidates, _ = self.search_text_candidates_with_version(
            query,
            scope_type=scope_type,
            scope_key=scope_key,
            categories=categories,
            exclude_pending=exclude_pending,
            max_tokens=max_tokens,
            limit=limit,
            force_sql_fallback=force_sql_fallback,
        )
        return candidates

    def search_text_candidates_with_version(
        self,
        query: str,
        *,
        scope_type: MemoryScopeType | None = None,
        scope_key: str | None = None,
        categories: tuple[MemoryCategory, ...] | None = None,
        exclude_pending: bool = False,
        max_tokens: int | None = None,
        limit: int = 20,
        force_sql_fallback: bool = False,
    ) -> tuple[list[MemorySearchCandidate], str]:
        """
        Busca candidatos ACTIVOS y lee atómicamente `store_version_token` dentro del mismo
        `ExperienceStoreLock` y conexión de instantánea.
        """
        self.policy.assert_enabled()
        if not isinstance(query, str):
            raise MemoryValidationError("query debe ser una cadena de texto.")
        if "\x00" in query:
            raise MemoryValidationError("query contiene byte NUL no permitido.")

        normalized_q = normalize_text_for_search(query)
        token_cap = max(1, int(settings.N8_SEARCH_MAX_TOKENS if max_tokens is None else max_tokens))
        tokens: list[str] = []
        seen_tok: set[str] = set()
        for raw_tok in normalized_q.split():
            tok = raw_tok.strip(".-_")
            if len(tok) >= 2 and tok not in seen_tok:
                seen_tok.add(tok)
                tokens.append(tok)
            if len(tokens) >= token_cap:
                break

        if not tokens:
            return [], ""

        eff_limit = max(1, min(200, int(limit)))
        filter_clauses: list[str] = ["m.status = 'ACTIVE'"]
        if exclude_pending:
            filter_clauses.append(
                "COALESCE(json_extract(m.metadata_json, '$.confirmation_state'), 'confirmed') != 'pending'"
            )
        filter_params: list[Any] = []

        if scope_type is not None:
            norm_st = MemoryScopeType(scope_type)
            filter_clauses.append("m.scope_type = ?")
            filter_params.append(norm_st.value)
            if norm_st == MemoryScopeType.GLOBAL:
                filter_clauses.append("m.scope_key = 'global'")
            elif scope_key is not None and scope_key.strip():
                _, norm_sk = normalize_scope(norm_st, scope_key)
                filter_clauses.append("m.scope_key = ?")
                filter_params.append(norm_sk)
        elif scope_key is not None and scope_key.strip():
            filter_clauses.append("m.scope_key = ?")
            filter_params.append(scope_key.strip().lower())

        if categories:
            cat_vals = [MemoryCategory(cat).value for cat in categories]
            placeholders = ", ".join("?" for _ in cat_vals)
            filter_clauses.append(f"m.category IN ({placeholders})")
            filter_params.extend(cat_vals)

        where_base = " AND ".join(filter_clauses)

        with self.acquire_lock():
            with self._open_connection() as conn:
                fts_active = self._ensure_ready(conn)
                _, _, ver_token = read_store_version_components(conn)
                use_fts = fts_active and not force_sql_fallback

                if use_fts:
                    fts_expr = " OR ".join('"' + tok.replace('"', '""') + '"' for tok in tokens)
                    sql = f"""
                        SELECT m.*, bm25(memory_fts) AS rank_score
                        FROM memory_fts
                        JOIN memories AS m ON m.memory_id = memory_fts.memory_id
                        WHERE memory_fts MATCH ? AND {where_base}
                        ORDER BY rank_score ASC, m.importance DESC, m.updated_at DESC
                        LIMIT ?
                    """
                    rows = conn.execute(sql, (fts_expr, *filter_params, eff_limit)).fetchall()
                    return (
                        [
                            MemorySearchCandidate(
                                record=_row_to_memory_record(r),
                                match_backend="fts5",
                                raw_score=float(r["rank_score"]),
                            )
                            for r in rows
                        ],
                        ver_token,
                    )

                # Respaldo SQL determinista con LIKE parametrizado y ESCAPE explícito.
                like_clauses: list[str] = []
                score_terms: list[str] = []
                like_params: list[Any] = []
                for tok in tokens:
                    pattern = f"%{_escape_sql_like(tok)}%"
                    cond = "(m.normalized_text LIKE ? ESCAPE '\\' OR LOWER(m.title) LIKE ? ESCAPE '\\' OR LOWER(m.summary) LIKE ? ESCAPE '\\')"
                    like_clauses.append(cond)
                    score_terms.append(f"(CASE WHEN {cond} THEN 1.0 ELSE 0.0 END)")
                    like_params.extend([pattern, pattern, pattern])

                hit_expr = " + ".join(score_terms)
                # Repetimos like_params porque se evalúa en SELECT hit_score y en WHERE.
                sql_fallback = f"""
                    SELECT m.*, ({hit_expr}) AS hit_score
                    FROM memories AS m
                    WHERE {where_base} AND ({' OR '.join(like_clauses)})
                    ORDER BY hit_score DESC, m.importance DESC, m.updated_at DESC
                    LIMIT ?
                """
                full_params = [*like_params, *filter_params, *like_params, eff_limit]
                rows = conn.execute(sql_fallback, tuple(full_params)).fetchall()
                return (
                    [
                        MemorySearchCandidate(
                            record=_row_to_memory_record(r),
                            match_backend="sql_like",
                            raw_score=float(r["hit_score"]),
                        )
                        for r in rows
                    ],
                    ver_token,
                )

    def _quarantine_corrupt_files_locked(self) -> list[Path]:
        """
        Mueve una base SQLite corrupta y sus artefactos WAL/SHM a `.corrupt.<uuid>` dentro de la
        misma carpeta segura local, una vez cerrada toda conexión abierta.
        """
        moved: list[Path] = []
        suffix = f".corrupt.{uuid4().hex[:12]}"
        candidates = (
            self.db_path,
            Path(f"{self.db_path}-wal"),
            Path(f"{self.db_path}-shm"),
            Path(f"{self.db_path}-journal"),
        )
        for src in candidates:
            if src.exists():
                dst = Path(f"{src}{suffix}")
                os.replace(str(src), str(dst))
                moved.append(dst)
        return moved

    @staticmethod
    def _check_fts_sync(conn: sqlite3.Connection) -> tuple[bool, str]:
        """Comprueba que `memory_fts` contenga exactamente los recuerdos `ACTIVE` actuales."""
        active_db = int(
            conn.execute("SELECT COUNT(*) FROM memories WHERE status = 'ACTIVE'").fetchone()[0]
        )
        fts_total = int(conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0])
        stale_in_fts = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM memory_fts
                LEFT JOIN memories ON memories.memory_id = memory_fts.memory_id
                WHERE memories.memory_id IS NULL OR memories.status != 'ACTIVE'
                """
            ).fetchone()[0]
        )
        fts_sync_ok = (active_db == fts_total) and (stale_in_fts == 0)
        details = (
            ""
            if fts_sync_ok
            else f"fts_desync(active={active_db}, fts={fts_total}, stale={stale_in_fts})"
        )
        return fts_sync_ok, details

    def verify_integrity(
        self,
        *,
        check_fts_sync: bool = True,
        quarantine_on_corruption: bool = False,
        actor: str = "system",
    ) -> IntegrityReport:
        """
        Verifica `PRAGMA integrity_check == 'ok'`, llaves foráneas y sincronía `memory_fts`.

        Respeta el mismo protocolo `ExperienceStoreLock` que los escritores normales. Si detecta
        corrupción física o estructural y `quarantine_on_corruption=True`, cierra la conexión antes
        de aislar el archivo a `.corrupt.<uuid>` y lanza `MemoryCorruptionError`.
        """
        self.policy.assert_enabled()
        clean_actor = sanitize_memory_text(
            actor,
            max_chars=64,
            allow_newlines=False,
            field_name="actor",
            check_secrets=True,
        )

        report: IntegrityReport | None = None
        corruption_exc_msg: str | None = None

        with self.acquire_lock():
            try:
                with self._open_connection() as conn:
                    ic_rows = conn.execute("PRAGMA integrity_check").fetchall()
                    ic_result = ", ".join(str(r[0]) for r in ic_rows) if ic_rows else "empty"
                    ic_ok = ic_result.strip().lower() == "ok"

                    fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
                    fk_ok = len(fk_rows) == 0

                    uv_row = conn.execute("PRAGMA user_version").fetchone()
                    schema_ver = int(uv_row[0]) if uv_row else 0
                    missing_tables = [t for t in CORE_TABLES if not table_exists(conn, t)]

                    fts_avail = fts5_available(conn)
                    fts_active = self._is_fts_active(conn)
                    fts_sync_ok = True
                    sync_details = ""

                    if not missing_tables and check_fts_sync and fts_active:
                        fts_sync_ok, sync_details = self._check_fts_sync(conn)

                    token_ok = True
                    if not missing_tables and schema_ver == CURRENT_SCHEMA_VERSION:
                        _, _, current_token = read_store_version_components(conn)
                        token_ok = bool(current_token)

                    overall_ok = (
                        ic_ok
                        and fk_ok
                        and not missing_tables
                        and schema_ver == CURRENT_SCHEMA_VERSION
                        and fts_sync_ok
                        and token_ok
                    )

                    details_parts: list[str] = []
                    if not ic_ok:
                        details_parts.append(f"integrity_check={ic_result}")
                    if not fk_ok:
                        details_parts.append(f"foreign_key_violations={len(fk_rows)}")
                    if missing_tables:
                        details_parts.append(f"missing_tables={missing_tables}")
                    if schema_ver != CURRENT_SCHEMA_VERSION:
                        details_parts.append(f"schema_version={schema_ver}!={CURRENT_SCHEMA_VERSION}")
                    if sync_details:
                        details_parts.append(sync_details)
                    if not token_ok:
                        details_parts.append("missing_store_version_token")

                    report = IntegrityReport(
                        ok=overall_ok,
                        integrity_check_result=ic_result,
                        foreign_keys_ok=fk_ok,
                        fts_available=fts_avail,
                        fts_active=fts_active,
                        fts_sync_ok=fts_sync_ok,
                        schema_version=schema_ver,
                        details="; ".join(details_parts) if details_parts else "ok",
                    )

                    if overall_ok and not missing_tables:
                        now = utc_now_iso()
                        conn.execute("BEGIN IMMEDIATE")
                        try:
                            self._insert_audit_tx(
                                conn,
                                action=MemoryAuditAction.INTEGRITY_CHECK,
                                memory_id=None,
                                actor=clean_actor,
                                reason="verify_integrity_ok",
                                details=report.to_dict(),
                                created_at=now,
                            )
                            conn.execute("COMMIT")
                        except Exception:
                            if conn.in_transaction:
                                conn.execute("ROLLBACK")
                            raise
            except MemoryCorruptionError as exc:
                corruption_exc_msg = str(exc)

            # En este punto la conexión efímera ya fue cerrada en `_open_connection.finally`.
            if corruption_exc_msg is not None:
                if quarantine_on_corruption:
                    moved = self._quarantine_corrupt_files_locked()
                    raise MemoryCorruptionError(
                        f"{corruption_exc_msg}. Artefactos en cuarentena: {[p.name for p in moved]}"
                    )
                return IntegrityReport(
                    ok=False,
                    integrity_check_result=corruption_exc_msg,
                    foreign_keys_ok=False,
                    fts_available=fts5_available(None),
                    fts_active=False,
                    fts_sync_ok=False,
                    schema_version=0,
                    details=corruption_exc_msg,
                )

            assert report is not None
            if not report.ok and quarantine_on_corruption:
                moved = self._quarantine_corrupt_files_locked()
                raise MemoryCorruptionError(
                    f"Fallo de integridad en '{self.db_path.name}': {report.details}. "
                    f"Cuarentena: {[p.name for p in moved]}"
                )
            return report

    def get_stats(self) -> StoreStats:
        """Devuelve estadísticas consistentes de conteo y configuración del almacén."""
        self.policy.assert_enabled()
        with self.acquire_lock():
            with self._open_connection() as conn:
                self._ensure_ready(conn)
                uv_row = conn.execute("PRAGMA user_version").fetchone()
                jm_row = conn.execute("PRAGMA journal_mode").fetchone()

                counts = {s.value: 0 for s in MemoryStatus}
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS cnt FROM memories GROUP BY status"
                ).fetchall():
                    counts[row["status"]] = int(row["cnt"])

                rev_count = int(conn.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0])
                aud_count = int(conn.execute("SELECT COUNT(*) FROM memory_audit").fetchone()[0])
                _, _, ver_token = read_store_version_components(conn)
                total = sum(counts.values())

                return StoreStats(
                    db_path=str(self.db_path),
                    schema_version=int(uv_row[0]) if uv_row else 0,
                    fts_available=fts5_available(conn),
                    fts_active=self._is_fts_active(conn),
                    journal_mode=str(jm_row[0]).lower() if jm_row else "unknown",
                    total_memories=total,
                    active_memories=counts[MemoryStatus.ACTIVE.value],
                    superseded_memories=counts[MemoryStatus.SUPERSEDED.value],
                    revoked_memories=counts[MemoryStatus.REVOKED.value],
                    expired_memories=counts[MemoryStatus.EXPIRED.value],
                    total_revisions=rev_count,
                    total_audit_entries=aud_count,
                    store_version_token=ver_token,
                )

    # ------------------------------------------------------------------
    # Primitivas administrativas N8.2: confirmación, purga física, backup y restauración
    # ------------------------------------------------------------------

    def confirm_memory(
        self,
        memory_id: str,
        *,
        actor: str = "user",
        reason: str = "manual_confirmation",
        private_mode: bool = False,
    ) -> MemoryRecord:
        """
        Confirma un recuerdo ACTIVE (pendiente o sin verificación explícita), registrando
        revisión histórica y habilitándolo para recuperación normal.
        """
        self.policy.assert_write_allowed(private_mode=private_mode)
        if not isinstance(memory_id, str) or not memory_id.strip():
            raise MemoryValidationError("memory_id no puede estar vacío.")
        clean_id = memory_id.strip()
        clean_actor = sanitize_memory_text(
            actor,
            max_chars=64,
            allow_newlines=False,
            field_name="actor",
            check_secrets=True,
        )
        clean_reason = sanitize_memory_text(
            reason,
            max_chars=240,
            allow_newlines=False,
            field_name="reason",
            check_secrets=True,
        )

        with self.acquire_lock():
            with self._open_connection() as conn:
                fts_active = self._ensure_ready(conn)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    row = conn.execute(
                        "SELECT * FROM memories WHERE memory_id = ?",
                        (clean_id,),
                    ).fetchone()
                    if row is None:
                        raise MemoryNotFoundError(f"No existe el recuerdo '{clean_id}'.")

                    current = _row_to_memory_record(row)
                    if current.status != MemoryStatus.ACTIVE:
                        raise MemoryTransitionError(
                            f"Solo se pueden confirmar recuerdos ACTIVE (estado actual: {current.status.value})."
                        )

                    if current.revision_number >= self.policy.max_revisions_per_memory:
                        raise MemoryPolicyError(
                            f"Límite máximo de revisiones ({self.policy.max_revisions_per_memory}) alcanzado para '{clean_id}'."
                        )

                    now = utc_now_iso()
                    meta_dict = {k: v for k, v in current.metadata}
                    was_pending = meta_dict.get("confirmation_state") == "pending"
                    meta_dict["confirmation_state"] = "confirmed"
                    meta_dict["confirmed_by"] = clean_actor
                    meta_dict["confirmed_at"] = now
                    new_conf = max(current.confidence, 0.90) if was_pending else max(current.confidence, 0.85)
                    new_rev_num = current.revision_number + 1
                    revision_id = generate_memory_id("rev")

                    conn.execute(
                        """
                        INSERT INTO memory_revisions (
                            revision_id, memory_id, revision_number,
                            previous_title, previous_content, previous_summary, previous_fingerprint,
                            new_title, new_content, new_summary, new_fingerprint,
                            change_reason, changed_by, changed_at, source
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            revision_id,
                            clean_id,
                            new_rev_num,
                            current.title,
                            current.content,
                            current.summary,
                            current.content_fingerprint,
                            current.title,
                            current.content,
                            current.summary,
                            current.content_fingerprint,
                            clean_reason,
                            clean_actor,
                            now,
                            current.source.value,
                        ),
                    )

                    conn.execute(
                        """
                        UPDATE memories
                        SET confidence = ?,
                            revision_number = ?,
                            metadata_json = ?,
                            last_verified_at = ?,
                            updated_at = ?
                        WHERE memory_id = ?
                        """,
                        (
                            new_conf,
                            new_rev_num,
                            canonical_json(meta_dict),
                            now,
                            now,
                            clean_id,
                        ),
                    )

                    updated_row = conn.execute(
                        "SELECT * FROM memories WHERE memory_id = ?",
                        (clean_id,),
                    ).fetchone()
                    if updated_row is None:
                        raise MemoryPersistenceError("Fallo al leer recuerdo tras confirmación.")
                    updated = _row_to_memory_record(updated_row)

                    if fts_active:
                        self._sync_fts_active_row_tx(conn, updated)

                    new_ver_token = bump_store_version_token_tx(conn, now=now)

                    self._insert_audit_tx(
                        conn,
                        action=MemoryAuditAction.REVISE,
                        memory_id=clean_id,
                        actor=clean_actor,
                        reason="memory_confirmed",
                        details={
                            "revision_id": revision_id,
                            "revision_number": new_rev_num,
                            "was_pending": was_pending,
                            "store_version_token": new_ver_token,
                        },
                        created_at=now,
                    )
                    conn.execute("COMMIT")
                    return updated
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise

    def purge_memory(
        self,
        memory_id: str,
        *,
        actor: str = "user",
        reason: str = "manual_purge",
        vacuum: bool = False,
        private_mode: bool = False,
    ) -> PurgeResult:
        """
        Eliminación física controlada de un recuerdo y todos sus rastros internos en SQLite
        (`memory_fts`, `memory_revisions`, `memory_audit`, referencias y `memories`) bajo `ExperienceStoreLock`.
        """
        self.policy.assert_write_allowed(private_mode=private_mode)
        if not isinstance(memory_id, str) or not memory_id.strip():
            raise MemoryValidationError("memory_id no puede estar vacío.")
        clean_id = memory_id.strip()
        clean_actor = sanitize_memory_text(
            actor,
            max_chars=64,
            allow_newlines=False,
            field_name="actor",
            check_secrets=True,
        )
        clean_reason = sanitize_memory_text(
            reason,
            max_chars=240,
            allow_newlines=False,
            field_name="reason",
            check_secrets=True,
        )
        limitations = (
            "Purga SQLite aplicada con PRAGMA secure_delete=ON y wal_checkpoint(TRUNCATE); "
            "no es posible garantizar destrucción física irrecuperable en bloques SSD (wear-leveling), "
            "metadatos NTFS, instantáneas VSS ni respaldos externos creados previamente."
        )

        with self.acquire_lock():
            with self._open_connection(enable_wal=True) as conn:
                fts_active = self._ensure_ready(conn)
                now = utc_now_iso()
                revs_deleted = 0
                aud_deleted = 0

                conn.execute("BEGIN IMMEDIATE")
                try:
                    exists = conn.execute(
                        "SELECT 1 FROM memories WHERE memory_id = ?",
                        (clean_id,),
                    ).fetchone()
                    if exists is None:
                        raise MemoryNotFoundError(f"No existe el recuerdo '{clean_id}'.")

                    if fts_active:
                        conn.execute("DELETE FROM memory_fts WHERE memory_id = ?", (clean_id,))

                    conn.execute(
                        "UPDATE memories SET superseded_by = NULL WHERE superseded_by = ?",
                        (clean_id,),
                    )

                    rev_cur = conn.execute(
                        "DELETE FROM memory_revisions WHERE memory_id = ?",
                        (clean_id,),
                    )
                    revs_deleted = int(rev_cur.rowcount if rev_cur.rowcount is not None and rev_cur.rowcount >= 0 else 0)

                    escaped_like = f"%{_escape_sql_like(clean_id)}%"
                    aud_cur = conn.execute(
                        """
                        DELETE FROM memory_audit
                        WHERE memory_id = ? OR details_json LIKE ? ESCAPE '\\'
                        """,
                        (clean_id, escaped_like),
                    )
                    aud_deleted = int(aud_cur.rowcount if aud_cur.rowcount is not None and aud_cur.rowcount >= 0 else 0)

                    conn.execute("DELETE FROM memories WHERE memory_id = ?", (clean_id,))

                    new_ver_token = bump_store_version_token_tx(conn, now=now)

                    id_digest = hashlib.sha256(clean_id.encode("utf-8")).hexdigest()
                    self._insert_audit_tx(
                        conn,
                        action=MemoryAuditAction.REVOKE,
                        memory_id=None,
                        actor=clean_actor,
                        reason="physical_purge_completed",
                        details={
                            "purged_id_sha256": id_digest,
                            "purge_reason_hash": hashlib.sha256(clean_reason.encode("utf-8")).hexdigest()[:16],
                            "revisions_deleted": revs_deleted,
                            "audit_rows_deleted": aud_deleted,
                            "store_version_token": new_ver_token,
                        },
                        created_at=now,
                    )
                    conn.execute("COMMIT")
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise

                # Checkpoint WAL fuera de transacción para volcar cambios al archivo principal.
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                vacuum_done = False
                if vacuum:
                    conn.execute("VACUUM")
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    vacuum_done = True

                return PurgeResult(
                    memory_id=clean_id,
                    purged=True,
                    revisions_deleted=revs_deleted,
                    audit_rows_deleted=aud_deleted,
                    wal_checkpointed=True,
                    vacuum_executed=vacuum_done,
                    limitations_notice=limitations,
                )

    @staticmethod
    def _validate_sqlite_snapshot_file(candidate_db: Path) -> tuple[int, int]:
        """
        Verifica integridad estructural SQLite de un archivo candidato a backup/restauración.
        Devuelve `(schema_version, memory_count)` o lanza `MemoryRestoreError`.
        """
        if not candidate_db.exists() or not candidate_db.is_file():
            raise MemoryRestoreError(f"Archivo de backup inexistente: '{candidate_db}'.")
        try:
            with closing(sqlite3.connect(str(candidate_db), timeout=2.0)) as check_conn:
                check_conn.row_factory = sqlite3.Row
                qc_rows = check_conn.execute("PRAGMA quick_check").fetchall()
                qc_res = ", ".join(str(r[0]) for r in qc_rows) if qc_rows else "empty"
                if qc_res.strip().lower() != "ok":
                    raise MemoryRestoreError(f"Backup falló quick_check: {qc_res}")

                ic_rows = check_conn.execute("PRAGMA integrity_check").fetchall()
                ic_res = ", ".join(str(r[0]) for r in ic_rows) if ic_rows else "empty"
                if ic_res.strip().lower() != "ok":
                    raise MemoryRestoreError(f"Backup falló integrity_check: {ic_res}")

                fk_rows = check_conn.execute("PRAGMA foreign_key_check").fetchall()
                if fk_rows:
                    raise MemoryRestoreError(f"Backup contiene violaciones de llaves foráneas ({len(fk_rows)}).")

                uv = int(check_conn.execute("PRAGMA user_version").fetchone()[0])
                if uv < 1 or uv > CURRENT_SCHEMA_VERSION:
                    raise MemoryRestoreError(f"Versión de esquema incompatible en backup: {uv}")

                missing = [t for t in CORE_TABLES if not table_exists(check_conn, t)]
                if missing:
                    raise MemoryRestoreError(f"Backup incompleto; faltan tablas: {missing}")

                mem_count = int(check_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
                return uv, mem_count
        except MemoryRestoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise MemoryRestoreError(f"Archivo de backup SQLite inválido o corrupto: {exc}") from exc

    def create_backup(
        self,
        backup_dir: Path | str | None = None,
        *,
        retention: int | None = None,
        actor: str = "system",
    ) -> BackupInfo:
        """
        Genera un backup consistente mediante `sqlite3.Connection.backup()`, lo valida
        íntegramente antes de publicarlo vía `os.replace()` y aplica retención máxima.
        """
        self.policy.assert_enabled()
        clean_actor = sanitize_memory_text(
            actor,
            max_chars=64,
            allow_newlines=False,
            field_name="actor",
            check_secrets=True,
        )
        target_dir = validate_safe_backup_dir(_default_backup_dir() if backup_dir is None else backup_dir)
        max_keep = max(1, int(settings.N8_BACKUP_MAX_RETENTION if retention is None else retention))

        with self.acquire_lock():
            with self._open_connection(enable_wal=True) as src_conn:
                self._ensure_ready(src_conn)
                target_dir.mkdir(parents=True, exist_ok=True)

                now_iso = utc_now_iso()
                stamp = now_iso.replace(":", "").replace("-", "").replace(".", "_")[:19]
                backup_id = f"bkp_{stamp}_{uuid4().hex[:8]}"
                tmp_db = target_dir / f".{backup_id}.db.tmp"
                final_db = target_dir / f"{backup_id}.db"
                tmp_meta = target_dir / f".{backup_id}.backup.json.tmp"
                final_meta = target_dir / f"{backup_id}.backup.json"

                try:
                    with closing(sqlite3.connect(str(tmp_db), timeout=5.0)) as dst_conn:
                        src_conn.backup(dst_conn)
                        dst_conn.commit()

                    schema_ver, mem_count = self._validate_sqlite_snapshot_file(tmp_db)
                    sha256 = _compute_file_sha256(tmp_db)
                    size_bytes = int(tmp_db.stat().st_size)

                    os.replace(str(tmp_db), str(final_db))

                    meta_payload = {
                        "backup_id": backup_id,
                        "backup_file": final_db.name,
                        "sha256": sha256,
                        "size_bytes": size_bytes,
                        "created_at": now_iso,
                        "schema_version": schema_ver,
                        "memory_count": mem_count,
                        "integrity_ok": True,
                    }
                    with open(tmp_meta, "x", encoding="utf-8") as fh:
                        fh.write(canonical_json(meta_payload))
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.replace(str(tmp_meta), str(final_meta))

                    # Solo después de validar y publicar el nuevo snapshot recortamos históricos.
                    existing = self._read_backup_entries_dir(target_dir)
                    for obsolete in existing[max_keep:]:
                        for victim in (Path(obsolete.backup_path), Path(obsolete.metadata_path)):
                            if victim.exists():
                                victim.unlink(missing_ok=True)

                    src_conn.execute("BEGIN IMMEDIATE")
                    try:
                        self._insert_audit_tx(
                            src_conn,
                            action=MemoryAuditAction.INTEGRITY_CHECK,
                            memory_id=None,
                            actor=clean_actor,
                            reason="backup_created",
                            details={"backup_id": backup_id, "sha256": sha256, "size_bytes": size_bytes},
                            created_at=now_iso,
                        )
                        src_conn.execute("COMMIT")
                    except Exception:
                        if src_conn.in_transaction:
                            src_conn.execute("ROLLBACK")
                        raise

                    return BackupInfo(
                        backup_id=backup_id,
                        backup_path=str(final_db),
                        metadata_path=str(final_meta),
                        sha256=sha256,
                        size_bytes=size_bytes,
                        created_at=now_iso,
                        schema_version=schema_ver,
                        memory_count=mem_count,
                        integrity_ok=True,
                    )
                except MemoryBackupError:
                    raise
                except (MemoryRestoreError, OSError, sqlite3.Error) as exc:
                    raise MemoryBackupError(f"No se pudo crear un backup válido de N8: {exc}") from exc
                finally:
                    for leftover in (tmp_db, tmp_meta):
                        if leftover.exists():
                            leftover.unlink(missing_ok=True)

    @staticmethod
    def _read_backup_entries_dir(target_dir: Path) -> list[BackupInfo]:
        if not target_dir.exists() or not target_dir.is_dir():
            return []
        items: list[BackupInfo] = []
        for meta_file in sorted(target_dir.glob("*.backup.json")):
            try:
                raw = json.loads(meta_file.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    continue
                bkp_id = str(raw.get("backup_id") or "").strip()
                bkp_name = str(raw.get("backup_file") or "").strip()
                sha = str(raw.get("sha256") or "").strip().lower()
                if not bkp_id or not bkp_name or len(sha) != 64:
                    continue
                db_candidate = (target_dir / bkp_name).resolve()
                if db_candidate.parent != target_dir.resolve() or not db_candidate.exists():
                    continue
                items.append(
                    BackupInfo(
                        backup_id=bkp_id,
                        backup_path=str(db_candidate),
                        metadata_path=str(meta_file.resolve()),
                        sha256=sha,
                        size_bytes=int(raw.get("size_bytes", 0)),
                        created_at=str(raw.get("created_at") or ""),
                        schema_version=int(raw.get("schema_version", 1)),
                        memory_count=int(raw.get("memory_count", 0)),
                        integrity_ok=bool(raw.get("integrity_ok", False)),
                    )
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
        items.sort(key=lambda b: (b.created_at, b.backup_id), reverse=True)
        return items

    def list_backups(self, backup_dir: Path | str | None = None) -> list[BackupInfo]:
        """Lista los backups disponibles en `backup_dir` ordenados del más reciente al más antiguo."""
        self.policy.assert_enabled()
        target_dir = validate_safe_backup_dir(_default_backup_dir() if backup_dir is None else backup_dir)
        with self.acquire_lock():
            return self._read_backup_entries_dir(target_dir)

    def restore_from_backup(
        self,
        backup_ref: Path | str,
        *,
        backup_dir: Path | str | None = None,
        expected_sha256: str | None = None,
        actor: str = "user",
        private_mode: bool = False,
    ) -> RestoreResult:
        """
        Restaura `experiences.db` de forma atómica y recuperable bajo `ExperienceStoreLock`:
        1. Valida SHA-256 esperado y verificación SQLite completa antes de tocar la DB activa.
        2. Adquiere lock exclusivo inter-proceso.
        3. Preserva una copia verificada de la DB actual (y WAL/SHM) aunque esté corrupta.
        4. Prepara staging verificado en la misma carpeta, limpia WAL/SHM obsoletos y sustituye vía `os.replace()`.
        5. Verifica integridad post-restauración y revierte a la copia preservada ante fallo.
        """
        self.policy.assert_write_allowed(private_mode=private_mode)
        clean_actor = sanitize_memory_text(
            actor,
            max_chars=64,
            allow_newlines=False,
            field_name="actor",
            check_secrets=True,
        )
        target_dir = validate_safe_backup_dir(_default_backup_dir() if backup_dir is None else backup_dir)
        ref_str = str(backup_ref).strip()
        if not ref_str:
            raise MemoryRestoreError("Debe especificar un backup_id o ruta válida para restaurar.")

        candidate_by_id = target_dir / f"{ref_str}.db"
        if candidate_by_id.exists() and not any(sep in ref_str for sep in ("/", "\\")):
            backup_db = validate_safe_local_db_path(candidate_by_id)
        else:
            backup_db = validate_safe_local_db_path(backup_ref)

        meta_file = backup_db.with_name(f"{backup_db.stem}.backup.json")
        resolved_expected_hash = (expected_sha256 or "").strip().lower()
        backup_id = backup_db.stem
        if meta_file.exists():
            try:
                meta_raw = json.loads(meta_file.read_text(encoding="utf-8"))
                meta_hash = str(meta_raw.get("sha256") or "").strip().lower()
                if not resolved_expected_hash:
                    resolved_expected_hash = meta_hash
                elif meta_hash and resolved_expected_hash != meta_hash:
                    raise MemoryRestoreError("El SHA-256 indicado no coincide con los metadatos del backup.")
                if meta_raw.get("backup_id"):
                    backup_id = str(meta_raw["backup_id"])
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                raise MemoryRestoreError(f"Metadatos de backup inválidos o ilegibles: {exc}") from exc

        if len(resolved_expected_hash) != 64:
            raise MemoryRestoreError(
                "No se puede restaurar sin huella SHA-256 verificable (metadato `.backup.json` ausente o inválido)."
            )

        live_hash_before = _compute_file_sha256(backup_db)
        if live_hash_before != resolved_expected_hash:
            raise MemoryRestoreError(
                "Validación SHA-256 fallida: el archivo de backup fue modificado o está dañado."
            )

        self._validate_sqlite_snapshot_file(backup_db)

        with self.acquire_lock():
            # Revalidación anti-TOCTOU dentro del lock exclusivo.
            if _compute_file_sha256(backup_db) != resolved_expected_hash:
                raise MemoryRestoreError("El archivo de backup cambió mientras se adquiría el lock.")
            self._validate_sqlite_snapshot_file(backup_db)

            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            preserved_paths: list[str] = []
            preserved_main_db: Path | None = None
            staging_db = self.db_path.parent / f"{self.db_path.name}.restore_staging_{uuid4().hex[:10]}"

            prev_live_seq = 0

            try:
                # Si la DB actual existe y abre sin error, consolidamos WAL y leemos su secuencia previa
                # para que el token rotado tras restaurar un backup antiguo nunca reutilice versiones previas.
                if self.db_path.exists():
                    try:
                        with self._open_connection() as cur_conn:
                            cur_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                            if table_exists(cur_conn, "store_metadata"):
                                _, prev_live_seq, _ = read_store_version_components(cur_conn)
                    except (MemoryCorruptionError, MemoryPersistenceError, sqlite3.Error):
                        pass

                    target_dir.mkdir(parents=True, exist_ok=True)
                    preserve_dir = target_dir / f"preserved_pre_restore_{uuid4().hex[:10]}"
                    preserve_dir.mkdir(parents=True, exist_ok=True)
                    for suffix in ("", "-wal", "-shm", "-journal"):
                        src_art = Path(f"{self.db_path}{suffix}")
                        if src_art.exists():
                            dst_art = preserve_dir / src_art.name
                            shutil.copy2(str(src_art), str(dst_art))
                            if suffix == "":
                                if _compute_file_sha256(dst_art) != _compute_file_sha256(src_art):
                                    raise MemoryRestoreError(
                                        "No se pudo verificar la copia de preservación previa a restaurar."
                                    )
                                preserved_main_db = dst_art
                            preserved_paths.append(str(dst_art))

                # Preparamos el archivo de staging en la misma partición/carpeta y verificamos fsync + hash.
                with backup_db.open("rb") as src_f, open(staging_db, "xb") as dst_f:
                    shutil.copyfileobj(src_f, dst_f, length=65536)
                    dst_f.flush()
                    os.fsync(dst_f.fileno())
                if _compute_file_sha256(staging_db) != resolved_expected_hash:
                    raise MemoryRestoreError("Fallo de verificación SHA-256 en archivo temporal de restauración.")

                # Limpiamos WAL/SHM/journal residuales antes de reemplazar el archivo principal.
                for side_ext in ("-wal", "-shm", "-journal"):
                    side_file = Path(f"{self.db_path}{side_ext}")
                    if side_file.exists():
                        side_file.unlink()

                os.replace(str(staging_db), str(self.db_path))

                # Verificación integral, migración v1->v2 si aplica, chequeo FTS5 y rotación de token post-swap.
                with self._open_connection(enable_wal=True) as restored_conn:
                    ic_rows = restored_conn.execute("PRAGMA integrity_check").fetchall()
                    ic_res = ", ".join(str(r[0]) for r in ic_rows) if ic_rows else "empty"
                    fk_rows = restored_conn.execute("PRAGMA foreign_key_check").fetchall()
                    uv_row = restored_conn.execute("PRAGMA user_version").fetchone()
                    schema_ver = int(uv_row[0]) if uv_row else 0
                    missing_tables = [t for t in CORE_TABLES if not table_exists(restored_conn, t)]

                    if (
                        ic_res.strip().lower() != "ok"
                        or fk_rows
                        or missing_tables
                        or schema_ver < 1
                        or schema_ver > CURRENT_SCHEMA_VERSION
                    ):
                        raise MemoryRestoreError(
                            f"La DB restaurada falló verificación post-swap "
                            f"(ic={ic_res}, fk={len(fk_rows)}, schema={schema_ver}, missing={missing_tables})."
                        )

                    migrated_from_version: int | None = None
                    if schema_ver < CURRENT_SCHEMA_VERSION:
                        migrated_from_version = schema_ver
                        mig_res = apply_migrations(
                            restored_conn,
                            enable_fts5=self.enable_fts5,
                            busy_timeout_ms=self.busy_timeout_ms,
                        )
                        schema_ver = mig_res.schema_version

                    fts_avail = fts5_available(restored_conn)
                    fts_active = self._is_fts_active(restored_conn)
                    fts_sync_ok = True
                    if fts_active:
                        fts_sync_ok, sync_details = self._check_fts_sync(restored_conn)
                        if not fts_sync_ok:
                            raise MemoryRestoreError(
                                f"La DB restaurada falló verificación FTS5 post-swap ({sync_details})."
                            )

                    now = utc_now_iso()
                    restored_conn.execute("BEGIN IMMEDIATE")
                    try:
                        # Rotamos epoch y garantizamos seq > prev_live_seq para invalidar caches tras restaurar.
                        rotated_token = rotate_store_version_token_tx(
                            restored_conn,
                            min_seq=prev_live_seq,
                            now=now,
                        )
                        self._insert_audit_tx(
                            restored_conn,
                            action=MemoryAuditAction.INTEGRITY_CHECK,
                            memory_id=None,
                            actor=clean_actor,
                            reason="backup_restored_verified",
                            details={
                                "backup_id": backup_id,
                                "sha256": resolved_expected_hash,
                                "migrated_from_version": migrated_from_version,
                                "schema_version": schema_ver,
                                "store_version_token": rotated_token,
                            },
                            created_at=now,
                        )
                        restored_conn.execute("COMMIT")
                    except Exception:
                        if restored_conn.in_transaction:
                            restored_conn.execute("ROLLBACK")
                        raise

                    report = IntegrityReport(
                        ok=True,
                        integrity_check_result=ic_res,
                        foreign_keys_ok=True,
                        fts_available=fts_avail,
                        fts_active=fts_active,
                        fts_sync_ok=fts_sync_ok,
                        schema_version=schema_ver,
                        details="ok",
                    )

                return RestoreResult(
                    restored=True,
                    backup_id=backup_id,
                    backup_sha256=resolved_expected_hash,
                    preserved_previous_paths=tuple(preserved_paths),
                    integrity_report=report,
                    details=(
                        f"Base restaurada y verificada desde '{backup_id}'. "
                        f"Copia previa preservada: {len(preserved_paths)} artefacto(s)."
                    ),
                )
            except Exception as exc:
                # Si la sustitución o verificación post-swap falló y preservamos una copia previa, revertimos.
                if preserved_main_db is not None and preserved_main_db.exists():
                    rollback_tmp = self.db_path.parent / f"{self.db_path.name}.restore_rollback_{uuid4().hex[:8]}"
                    try:
                        shutil.copy2(str(preserved_main_db), str(rollback_tmp))
                        os.replace(str(rollback_tmp), str(self.db_path))
                    except OSError:
                        if rollback_tmp.exists():
                            rollback_tmp.unlink(missing_ok=True)
                if isinstance(exc, MemoryRestoreError):
                    raise
                raise MemoryRestoreError(f"Error al restaurar backup '{backup_id}': {exc}") from exc
            finally:
                if staging_db.exists():
                    staging_db.unlink(missing_ok=True)
