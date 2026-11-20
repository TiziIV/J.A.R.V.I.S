"""
Esquema SQLite versionado, configuración de durabilidad WAL e infraestructura FTS5 (N8.1).

Justificación técnica junior:
1. `PRAGMA journal_mode = WAL` permite lecturas concurrentes sin bloquear escritores y mejora
   la resistencia ante cortes respecto al modo DELETE clásico.
2. `PRAGMA synchronous = FULL` asegura confirmación física en disco de las transacciones.
3. `PRAGMA secure_delete = ON` sobrescribe con ceros las páginas liberadas al revocar o borrar
   entradas del índice FTS5 para no dejar residuos recuperables en el archivo `.db`.
4. `PRAGMA foreign_keys = ON` es imprescindible en SQLite porque por defecto viene desactivado
   en cada nueva conexión.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sqlite3
from uuid import uuid4

from src.memory.models import (
    MemoryCategory,
    MemoryCorruptionError,
    MemoryPersistenceError,
    generate_memory_id,
    utc_now_iso,
)
from src.memory.policy import (
    compute_default_canonical_unit_id,
    compute_execution_idempotency_key,
)


CURRENT_SCHEMA_VERSION: int = 2

CORE_TABLES: tuple[str, ...] = (
    "schema_migrations",
    "store_metadata",
    "memories",
    "memory_revisions",
    "memory_audit",
)


@dataclass(frozen=True)
class MigrationResult:
    """Resumen del estado del esquema tras ejecutar `apply_migrations`."""

    schema_version: int
    migrated: bool
    fts_available: bool
    fts_active: bool
    journal_mode: str


def fts5_available(conn: sqlite3.Connection | None = None) -> bool:
    """
    Comprueba en tiempo de ejecución si la compilación local de SQLite soporta FTS5.
    """
    owns_conn = False
    target = conn
    try:
        if target is None:
            target = sqlite3.connect(":memory:")
            owns_conn = True
        row = target.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')").fetchone()
        return bool(row and row[0] == 1)
    except sqlite3.Error:
        return False
    finally:
        if owns_conn and target is not None:
            target.close()


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """Verifica si una tabla o tabla virtual existe en `sqlite_master`."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def configure_connection(
    conn: sqlite3.Connection,
    *,
    busy_timeout_ms: int = 5000,
    enable_wal: bool = False,
) -> str:
    """
    Aplica pragmas obligatorios en cada conexión efímera y opcionalmente activa WAL.
    """
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {max(250, int(busy_timeout_ms))}")
    conn.execute("PRAGMA secure_delete = ON")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA temp_store = MEMORY")
    if enable_wal:
        row = conn.execute("PRAGMA journal_mode = WAL").fetchone()
        return str(row[0]).lower() if row else "wal"
    row = conn.execute("PRAGMA journal_mode").fetchone()
    return str(row[0]).lower() if row else "unknown"


_V1_TABLES_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        applied_at TEXT NOT NULL,
        checksum TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS store_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memories (
        memory_id TEXT PRIMARY KEY,
        category TEXT NOT NULL CHECK (
            category IN ('FACT', 'PREFERENCE', 'DECISION', 'EXPERIENCE', 'INFERENCE')
        ),
        source TEXT NOT NULL CHECK (
            source IN ('USER_EXPLICIT', 'DECISION_CONFIRMED', 'EXECUTION_VERIFIED')
        ),
        status TEXT NOT NULL CHECK (
            status IN ('ACTIVE', 'SUPERSEDED', 'REVOKED', 'EXPIRED')
        ),
        scope_type TEXT NOT NULL CHECK (
            scope_type IN ('GLOBAL', 'PROJECT', 'APP_OR_GAME')
        ),
        scope_key TEXT NOT NULL,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        normalized_text TEXT NOT NULL,
        summary TEXT NOT NULL,
        confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
        importance INTEGER NOT NULL CHECK (importance >= 1 AND importance <= 100),
        revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
        content_fingerprint TEXT NOT NULL,
        tags_json TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        source_layer TEXT NOT NULL,
        source_goal_id TEXT,
        source_plan_id TEXT,
        source_task_id TEXT,
        source_execution_id TEXT,
        approval_hash TEXT,
        verifier_component TEXT,
        verified_at TEXT,
        evidence_hash TEXT,
        evidence_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT,
        revoked_at TEXT,
        superseded_by TEXT REFERENCES memories(memory_id) ON DELETE SET NULL,
        last_verified_at TEXT,
        access_count INTEGER NOT NULL DEFAULT 0 CHECK (access_count >= 0)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_active_fingerprint
    ON memories(content_fingerprint)
    WHERE status = 'ACTIVE'
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_active_exec_provenance
    ON memories(
        source_layer,
        source_execution_id,
        COALESCE(source_plan_id, ''),
        COALESCE(source_task_id, '')
    )
    WHERE source = 'EXECUTION_VERIFIED' AND source_execution_id IS NOT NULL AND status = 'ACTIVE'
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memories_status_scope
    ON memories(status, scope_type, scope_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memories_category_status
    ON memories(category, status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memories_updated_at
    ON memories(updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memories_expires_at
    ON memories(expires_at)
    WHERE expires_at IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memories_normalized_text
    ON memories(normalized_text)
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_revisions (
        revision_id TEXT PRIMARY KEY,
        memory_id TEXT NOT NULL REFERENCES memories(memory_id) ON DELETE RESTRICT,
        revision_number INTEGER NOT NULL CHECK (revision_number >= 2),
        previous_title TEXT NOT NULL,
        previous_content TEXT NOT NULL,
        previous_summary TEXT NOT NULL,
        previous_fingerprint TEXT NOT NULL,
        new_title TEXT NOT NULL,
        new_content TEXT NOT NULL,
        new_summary TEXT NOT NULL,
        new_fingerprint TEXT NOT NULL,
        change_reason TEXT NOT NULL,
        changed_by TEXT NOT NULL,
        changed_at TEXT NOT NULL,
        source TEXT NOT NULL CHECK (
            source IN ('USER_EXPLICIT', 'DECISION_CONFIRMED', 'EXECUTION_VERIFIED')
        ),
        UNIQUE(memory_id, revision_number)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memory_revisions_memory
    ON memory_revisions(memory_id, revision_number DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_audit (
        audit_id TEXT PRIMARY KEY,
        action TEXT NOT NULL CHECK (
            action IN (
                'CREATE',
                'REVISE',
                'REVOKE',
                'EXPIRE',
                'SUPERSEDE',
                'DEDUPE_HIT',
                'INTEGRITY_CHECK',
                'MIGRATE',
                'REJECT_PRIVATE_MODE'
            )
        ),
        memory_id TEXT REFERENCES memories(memory_id) ON DELETE SET NULL,
        actor TEXT NOT NULL,
        reason TEXT NOT NULL,
        details_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memory_audit_created
    ON memory_audit(created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memory_audit_memory
    ON memory_audit(memory_id, created_at DESC)
    """,
)

_V1_FTS_DDL: str = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    memory_id UNINDEXED,
    title,
    summary,
    content,
    tags,
    scope_key,
    tokenize='unicode61 remove_diacritics 2'
)
"""


def _v1_checksum() -> str:
    joined = "\n".join(_V1_TABLES_DDL) + "\n" + _V1_FTS_DDL.strip()
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


_V2_AUDIT_TABLE_SQL: str = """
CREATE TABLE memory_audit_v2 (
    audit_id TEXT PRIMARY KEY,
    action TEXT NOT NULL CHECK (
        action IN (
            'CREATE',
            'REVISE',
            'REVOKE',
            'EXPIRE',
            'SUPERSEDE',
            'DEDUPE_HIT',
            'REJECT_IDEMPOTENCY_CONFLICT',
            'INTEGRITY_CHECK',
            'MIGRATE',
            'REJECT_PRIVATE_MODE'
        )
    ),
    memory_id TEXT REFERENCES memories(memory_id) ON DELETE SET NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_V2_INDEXES_DDL: tuple[str, ...] = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_active_fingerprint
    ON memories(content_fingerprint)
    WHERE status = 'ACTIVE' AND source != 'EXECUTION_VERIFIED'
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_idempotency_key
    ON memories(idempotency_key)
    WHERE idempotency_key IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_active_exec_unit
    ON memories(
        source_layer,
        source_execution_id,
        COALESCE(source_plan_id, ''),
        COALESCE(source_task_id, ''),
        canonical_unit_id
    )
    WHERE source = 'EXECUTION_VERIFIED'
      AND source_execution_id IS NOT NULL
      AND canonical_unit_id IS NOT NULL
      AND status = 'ACTIVE'
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memories_source_execution_id
    ON memories(source_execution_id, status)
    WHERE source_execution_id IS NOT NULL
    """,
)


def _v2_checksum() -> str:
    joined = _V2_AUDIT_TABLE_SQL.strip() + "\n" + "\n".join(_V2_INDEXES_DDL)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def read_store_version_components(conn: sqlite3.Connection) -> tuple[str, int, str]:
    """
    Lee `(epoch, seq, token)` desde `store_metadata`.
    Si alguna clave aún no existía, devuelve valores base seguros sin mutar el almacén.
    """
    rows = conn.execute(
        """
        SELECT key, value FROM store_metadata
        WHERE key IN ('store_version_epoch', 'store_version_seq', 'store_version_token')
        """
    ).fetchall()
    meta = {str(r["key"]): str(r["value"]) for r in rows}
    epoch = meta.get("store_version_epoch") or ""
    raw_seq = meta.get("store_version_seq")
    try:
        seq = int(raw_seq) if raw_seq is not None else 0
    except ValueError:
        seq = 0
    token = meta.get("store_version_token") or (f"{epoch}:{seq}" if epoch else "")
    return epoch, max(0, seq), token


def _write_store_version_tx(
    conn: sqlite3.Connection,
    *,
    epoch: str,
    seq: int,
    now: str,
) -> str:
    token = f"{epoch}:{seq}"
    for key, value in (
        ("store_version_epoch", epoch),
        ("store_version_seq", str(seq)),
        ("store_version_token", token),
    ):
        conn.execute(
            """
            INSERT INTO store_metadata (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, now),
        )
    return token


def bump_store_version_token_tx(conn: sqlite3.Connection, *, now: str | None = None) -> str:
    """
    Incrementa atómicamente `store_version_seq` dentro de la transacción activa.
    Detecta revocaciones, expiraciones, supersesiones y purgas incluso sin cambiar `revision_number`.
    """
    epoch, seq, _ = read_store_version_components(conn)
    eff_epoch = epoch or uuid4().hex[:16]
    eff_now = now or utc_now_iso()
    return _write_store_version_tx(conn, epoch=eff_epoch, seq=seq + 1, now=eff_now)


def rotate_store_version_token_tx(
    conn: sqlite3.Connection,
    *,
    min_seq: int = 0,
    now: str | None = None,
) -> str:
    """
    Rota `store_version_epoch` a un nuevo identificador aleatorio y avanza `seq`.
    Impide que una restauración desde backup antiguo reutilice jamás un token previo.
    """
    _, restored_seq, _ = read_store_version_components(conn)
    new_epoch = uuid4().hex[:16]
    new_seq = max(restored_seq, max(0, int(min_seq))) + 1
    eff_now = now or utc_now_iso()
    return _write_store_version_tx(conn, epoch=new_epoch, seq=new_seq, now=eff_now)


def _apply_v2_migration_tx(conn: sqlite3.Connection, *, from_version: int, now: str) -> None:
    """Ejecuta de forma incremental, transaccional e idempotente el salto de esquema v1 -> v2."""
    existing_cols = {
        str(row["name"]) for row in conn.execute("PRAGMA table_info(memories)").fetchall()
    }
    if "canonical_unit_id" not in existing_cols:
        conn.execute("ALTER TABLE memories ADD COLUMN canonical_unit_id TEXT")
    if "idempotency_key" not in existing_cols:
        conn.execute("ALTER TABLE memories ADD COLUMN idempotency_key TEXT")

    # Backfill determinista de filas EXECUTION_VERIFIED heredadas desde v1 respetando unidades únicas.
    existing_keys_rows = conn.execute(
        "SELECT idempotency_key FROM memories WHERE idempotency_key IS NOT NULL"
    ).fetchall()
    used_keys: set[str] = {str(r["idempotency_key"]) for r in existing_keys_rows}

    legacy_exec_rows = conn.execute(
        """
        SELECT memory_id, category, status, source_layer, source_execution_id,
               source_plan_id, source_task_id, evidence_json, canonical_unit_id, idempotency_key
        FROM memories
        WHERE source = 'EXECUTION_VERIFIED'
        ORDER BY CASE WHEN status = 'ACTIVE' THEN 0 ELSE 1 END, created_at ASC, memory_id ASC
        """
    ).fetchall()
    for row in legacy_exec_rows:
        unit_id = row["canonical_unit_id"]
        if not unit_id:
            op_type = "legacy"
            if row["evidence_json"]:
                try:
                    ev_data = json.loads(str(row["evidence_json"]))
                    if isinstance(ev_data, dict) and ev_data.get("operation_type"):
                        op_type = str(ev_data["operation_type"])
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
            unit_id = compute_default_canonical_unit_id(
                operation_type=op_type,
                category=MemoryCategory(str(row["category"])),
            )

        idem_key = row["idempotency_key"]
        if not idem_key and row["source_execution_id"]:
            candidate_key = compute_execution_idempotency_key(
                source_layer=str(row["source_layer"]),
                source_execution_id=str(row["source_execution_id"]),
                source_plan_id=row["source_plan_id"],
                source_task_id=row["source_task_id"],
                canonical_unit_id=str(unit_id),
            )
            if candidate_key not in used_keys:
                idem_key = candidate_key
                used_keys.add(candidate_key)

        conn.execute(
            """
            UPDATE memories
            SET canonical_unit_id = ?, idempotency_key = ?
            WHERE memory_id = ?
            """,
            (str(unit_id), idem_key, str(row["memory_id"])),
        )

    # Reemplazar índices restrictivos v1 por índices compatibles con N8.3 (sin fusionar ejecuciones distintas).
    conn.execute("DROP INDEX IF EXISTS idx_memories_active_fingerprint")
    conn.execute("DROP INDEX IF EXISTS idx_memories_active_exec_provenance")
    for idx_sql in _V2_INDEXES_DDL:
        conn.execute(idx_sql)

    # Reconstruir `memory_audit` para ampliar CHECK con REJECT_IDEMPOTENCY_CONFLICT preservando historial y FK.
    conn.execute("DROP TABLE IF EXISTS memory_audit_v2")
    conn.execute(_V2_AUDIT_TABLE_SQL)
    conn.execute(
        """
        INSERT INTO memory_audit_v2 (audit_id, action, memory_id, actor, reason, details_json, created_at)
        SELECT audit_id, action, memory_id, actor, reason, details_json, created_at
        FROM memory_audit
        ORDER BY rowid ASC
        """
    )
    conn.execute("DROP TABLE memory_audit")
    conn.execute("ALTER TABLE memory_audit_v2 RENAME TO memory_audit")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_audit_created
        ON memory_audit(created_at DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_audit_memory
        ON memory_audit(memory_id, created_at DESC)
        """
    )

    conn.execute(
        """
        INSERT OR IGNORE INTO schema_migrations (version, name, applied_at, checksum)
        VALUES (?, ?, ?, ?)
        """,
        (2, "n8_v2_idempotency_and_store_version", now, _v2_checksum()),
    )
    conn.execute(
        """
        INSERT INTO store_metadata (key, value, updated_at)
        VALUES ('store_format', 'jarvis_n8_sqlite_v2', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (now,),
    )

    epoch, seq, token = read_store_version_components(conn)
    if not epoch or not token:
        eff_epoch = epoch or uuid4().hex[:16]
        _write_store_version_tx(conn, epoch=eff_epoch, seq=seq, now=now)

    conn.execute(
        """
        INSERT INTO memory_audit (audit_id, action, memory_id, actor, reason, details_json, created_at)
        VALUES (?, 'MIGRATE', NULL, 'system', 'schema_v2_applied', ?, ?)
        """,
        (
            generate_memory_id("aud"),
            f'{{"from_version":{from_version},"to_version":2}}',
            now,
        ),
    )
    conn.execute("PRAGMA user_version = 2")


def _detect_fts_active(conn: sqlite3.Connection) -> bool:
    if not table_exists(conn, "memory_fts"):
        return False
    row = conn.execute(
        "SELECT value FROM store_metadata WHERE key = 'fts_active'"
    ).fetchone()
    return bool(row and row[0] == "1")


def apply_migrations(
    conn: sqlite3.Connection,
    *,
    enable_fts5: bool = True,
    busy_timeout_ms: int = 5000,
) -> MigrationResult:
    """
    Aplica migraciones incrementales idempotentes hasta `CURRENT_SCHEMA_VERSION`.

    La migración completa corre dentro de `BEGIN IMMEDIATE`: si ocurre una interrupción o fallo,
    SQLite revierte la transacción sin dejar tablas parciales ni avanzar `PRAGMA user_version`.
    """
    try:
        journal_mode = configure_connection(
            conn,
            busy_timeout_ms=busy_timeout_ms,
            enable_wal=True,
        )
        fts_supported = fts5_available(conn)
        current_uv_row = conn.execute("PRAGMA user_version").fetchone()
        current_version = int(current_uv_row[0]) if current_uv_row else 0

        if current_version > CURRENT_SCHEMA_VERSION:
            raise MemoryCorruptionError(
                f"Versión de esquema no soportada ({current_version} > {CURRENT_SCHEMA_VERSION})."
            )

        if current_version == CURRENT_SCHEMA_VERSION:
            missing = [t for t in CORE_TABLES if not table_exists(conn, t)]
            if missing:
                raise MemoryCorruptionError(
                    f"Esquema en versión {current_version} pero faltan tablas obligatorias: {missing}"
                )
            return MigrationResult(
                schema_version=current_version,
                migrated=False,
                fts_available=fts_supported,
                fts_active=_detect_fts_active(conn),
                journal_mode=journal_mode,
            )

        if current_version >= 1:
            missing_prev = [t for t in CORE_TABLES if not table_exists(conn, t)]
            if missing_prev:
                raise MemoryCorruptionError(
                    f"Esquema en versión {current_version} incompleto; faltan tablas obligatorias: {missing_prev}"
                )

        now = utc_now_iso()
        should_create_fts = bool(enable_fts5 and fts_supported)

        conn.execute("BEGIN IMMEDIATE")
        try:
            if current_version < 1:
                for ddl in _V1_TABLES_DDL:
                    conn.execute(ddl)

                if should_create_fts:
                    conn.execute(_V1_FTS_DDL)

                conn.execute(
                    """
                    INSERT OR IGNORE INTO schema_migrations (version, name, applied_at, checksum)
                    VALUES (?, ?, ?, ?)
                    """,
                    (1, "n8_initial_experiences_schema", now, _v1_checksum()),
                )

                metadata_pairs = [
                    ("store_format", "jarvis_n8_sqlite_v1"),
                    ("fts_available", "1" if fts_supported else "0"),
                    ("fts_active", "1" if should_create_fts else "0"),
                    ("fts_tokenizer", "unicode61 remove_diacritics 2" if should_create_fts else "none"),
                    ("created_at", now),
                ]
                for key, value in metadata_pairs:
                    conn.execute(
                        """
                        INSERT INTO store_metadata (key, value, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                        """,
                        (key, value, now),
                    )

                conn.execute(
                    """
                    INSERT INTO memory_audit (audit_id, action, memory_id, actor, reason, details_json, created_at)
                    VALUES (?, 'MIGRATE', NULL, 'system', 'schema_v1_applied', ?, ?)
                    """,
                    (
                        generate_memory_id("aud"),
                        f'{{"from_version":{current_version},"to_version":1,"fts_active":{str(should_create_fts).lower()}}}',
                        now,
                    ),
                )
                conn.execute("PRAGMA user_version = 1")

            if current_version < 2:
                _apply_v2_migration_tx(conn, from_version=current_version, now=now)

            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

        return MigrationResult(
            schema_version=CURRENT_SCHEMA_VERSION,
            migrated=True,
            fts_available=fts_supported,
            fts_active=_detect_fts_active(conn),
            journal_mode=journal_mode,
        )
    except MemoryCorruptionError:
        raise
    except sqlite3.DatabaseError as exc:
        raise MemoryPersistenceError(f"Error al inicializar o migrar el esquema N8: {exc}") from exc
