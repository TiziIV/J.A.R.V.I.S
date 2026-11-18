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
import sqlite3

from src.memory.models import (
    MemoryCorruptionError,
    MemoryPersistenceError,
    generate_memory_id,
    utc_now_iso,
)


CURRENT_SCHEMA_VERSION: int = 1

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

            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

        return MigrationResult(
            schema_version=CURRENT_SCHEMA_VERSION,
            migrated=True,
            fts_available=fts_supported,
            fts_active=should_create_fts,
            journal_mode=journal_mode,
        )
    except MemoryCorruptionError:
        raise
    except sqlite3.DatabaseError as exc:
        raise MemoryPersistenceError(f"Error al inicializar o migrar el esquema N8: {exc}") from exc
