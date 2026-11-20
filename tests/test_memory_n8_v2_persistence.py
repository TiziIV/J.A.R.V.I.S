"""
Suite aislada de pruebas N8.3 Subfase 1: SQLite v2, migración v1->v2, idempotencia estricta,
`store_version_token` e invalidación por ejecución.

Reglas de aislamiento:
- Opera estrictamente bajo `JARVIS_ENV=test` y `temp/jarvis_test_isolated/`.
- Jamás crea, lee ni altera `data/memory_store.json`, `data/experiences.db` ni `data/backups/`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

from config import settings
from src.memory import (
    CURRENT_SCHEMA_VERSION,
    ExecutionInvalidationResult,
    ExecutionVerificationEvidence,
    ExperienceStore,
    MemoryAuditAction,
    MemoryCategory,
    MemoryCreateInput,
    MemoryEngine,
    MemoryIdempotencyConflictError,
    MemoryPolicyError,
    MemoryPrivateModeError,
    MemoryRetrievalTarget,
    MemoryRetriever,
    MemoryScopeType,
    MemorySource,
    MemoryStatus,
    canonical_json,
    compute_content_fingerprint,
    compute_default_canonical_unit_id,
    compute_execution_idempotency_key,
    normalize_text_for_search,
    utc_now_iso,
)
from src.memory.migrations import (
    _V1_FTS_DDL,
    _V1_TABLES_DDL,
    apply_migrations,
    read_store_version_components,
)
from src.memory.policy import resolve_test_sandbox_root


PROJECT_ROOT = settings.BASE_DIR.resolve()
REAL_MEMORY_JSON = PROJECT_ROOT / "data" / "memory_store.json"
REAL_EXPERIENCES_DB = PROJECT_ROOT / "data" / "experiences.db"
REAL_BACKUPS_DIR = PROJECT_ROOT / "data" / "backups"


def _file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _tokenize_fts_content(text: str) -> str:
    return normalize_text_for_search(text)


class N8V2PersistenceAndIdempotencyTests(unittest.TestCase):
    """Pruebas aisladas para N8.3 Subfase 1 (persistencia v2, idempotencia, token y rollback)."""

    _prev_jarvis_env: str | None = None
    _baseline_json_sha: str | None = None
    _baseline_real_db_exists: bool = False
    _baseline_real_backups_exists: bool = False

    @classmethod
    def setUpClass(cls) -> None:
        cls._prev_jarvis_env = os.environ.get("JARVIS_ENV")
        os.environ["JARVIS_ENV"] = "test"
        cls._baseline_json_sha = _file_sha256(REAL_MEMORY_JSON)
        cls._baseline_real_db_exists = REAL_EXPERIENCES_DB.exists()
        cls._baseline_real_backups_exists = REAL_BACKUPS_DIR.exists()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._prev_jarvis_env is None:
            os.environ.pop("JARVIS_ENV", None)
        else:
            os.environ["JARVIS_ENV"] = cls._prev_jarvis_env

        assert _file_sha256(REAL_MEMORY_JSON) == cls._baseline_json_sha
        assert REAL_EXPERIENCES_DB.exists() == cls._baseline_real_db_exists
        assert REAL_BACKUPS_DIR.exists() == cls._baseline_real_backups_exists

    def setUp(self) -> None:
        sandbox_root = resolve_test_sandbox_root()
        sandbox_root.mkdir(parents=True, exist_ok=True)
        self.work_dir = sandbox_root / f"n8_v2_case_{uuid4().hex[:10]}"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.work_dir / "experiences.db"
        self.backup_dir = self.work_dir / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.assertEqual(_file_sha256(REAL_MEMORY_JSON), self._baseline_json_sha)
        self.assertEqual(REAL_EXPERIENCES_DB.exists(), self._baseline_real_db_exists)
        self.assertEqual(REAL_BACKUPS_DIR.exists(), self._baseline_real_backups_exists)

    def _make_evidence(
        self,
        *,
        exec_id: str = "exec_v2_001",
        plan_id: str | None = "plan_v2_001",
        goal_id: str | None = "goal_v2_001",
        task_id: str | None = "task_v2_001",
        source_layer: str = "N5_CHAIN_ORCHESTRATOR",
        operation_type: str = "file_write",
        verified_artifacts: tuple[str, ...] = ("temp/jarvis_test_isolated/artifact.txt",),
        approval_hash: str | None = "b" * 64,
        scope_type: MemoryScopeType = MemoryScopeType.PROJECT,
        scope_key: str = "jarvis-core",
        canonical_unit_id: str | None = None,
    ) -> ExecutionVerificationEvidence:
        return ExecutionVerificationEvidence(
            source_layer=source_layer,
            source_execution_id=exec_id,
            source_plan_id=plan_id,
            source_goal_id=goal_id,
            source_task_id=task_id,
            approval_hash=approval_hash,
            executor_component="FileEditorSkill",
            operation_type=operation_type,
            execution_outcome="verified",
            expected_outcome="Archivo verificado tras escritura segura",
            verifier_component="ChainOrchestratorVerifier",
            verified_at=utc_now_iso(),
            verified_artifacts=verified_artifacts,
            scope_type=scope_type,
            scope_key=scope_key,
            canonical_unit_id=canonical_unit_id,
        )

    def _build_authentic_v1_database(self, target_db: Path) -> dict[str, str]:
        """Crea una base SQLite v1 genuina con recuerdos, revisiones, auditoría e índice FTS5."""
        now = "2026-11-18T12:00:00+00:00"
        ev_active = self._make_evidence(exec_id="exec_legacy_active", plan_id="plan_1", task_id="task_1")
        ev_revoked = self._make_evidence(
            exec_id="exec_legacy_revoked",
            plan_id="plan_1",
            task_id="task_2",
            operation_type="shell_check",
        )

        user_title = "Preferencia histórica idioma"
        user_content = "Responder siempre en español técnico claro."
        user_norm = _tokenize_fts_content(f"{user_title} {user_content}")
        user_fp = compute_content_fingerprint(
            category=MemoryCategory.PREFERENCE,
            scope_type=MemoryScopeType.GLOBAL,
            scope_key="global",
            normalized_text=user_norm,
        )

        act_title = "Compilación verificada legacy"
        act_content = "La suite de pruebas unitarias se ejecutó correctamente en modo test."
        act_norm = _tokenize_fts_content(f"{act_title} {act_content}")
        act_fp = compute_content_fingerprint(
            category=MemoryCategory.EXPERIENCE,
            scope_type=MemoryScopeType.PROJECT,
            scope_key="jarvis-core",
            normalized_text=act_norm,
        )

        rev_title = "Chequeo revocado legacy"
        rev_content = "Ejecución legacy revocada por usuario."
        rev_norm = _tokenize_fts_content(f"{rev_title} {rev_content}")
        rev_fp = compute_content_fingerprint(
            category=MemoryCategory.FACT,
            scope_type=MemoryScopeType.PROJECT,
            scope_key="jarvis-core",
            normalized_text=rev_norm,
        )

        with closing(sqlite3.connect(str(target_db))) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            for ddl in _V1_TABLES_DDL:
                conn.execute(ddl)
            conn.execute(_V1_FTS_DDL)

            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at, checksum) VALUES (?, ?, ?, ?)",
                (1, "n8_initial_experiences_schema", now, "v1_seed_checksum"),
            )
            for k, v in (
                ("store_format", "jarvis_n8_sqlite_v1"),
                ("fts_available", "1"),
                ("fts_active", "1"),
                ("fts_tokenizer", "unicode61 remove_diacritics 2"),
                ("created_at", now),
            ):
                conn.execute(
                    "INSERT INTO store_metadata (key, value, updated_at) VALUES (?, ?, ?)",
                    (k, v, now),
                )

            insert_sql = """
                INSERT INTO memories (
                    memory_id, category, source, status, scope_type, scope_key,
                    title, content, normalized_text, summary,
                    confidence, importance, revision_number, content_fingerprint,
                    tags_json, metadata_json, source_layer,
                    source_goal_id, source_plan_id, source_task_id, source_execution_id,
                    approval_hash, verifier_component, verified_at, evidence_hash, evidence_json,
                    created_at, updated_at, expires_at, revoked_at, superseded_by,
                    last_verified_at, access_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            conn.execute(
                insert_sql,
                (
                    "mem_v1_user",
                    "PREFERENCE",
                    "USER_EXPLICIT",
                    "ACTIVE",
                    "GLOBAL",
                    "global",
                    user_title,
                    user_content,
                    user_norm,
                    "Responder en español",
                    0.95,
                    85,
                    2,
                    user_fp,
                    '["idioma"]',
                    "{}",
                    "USER",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    now,
                    now,
                    None,
                    None,
                    None,
                    now,
                    0,
                ),
            )
            conn.execute(
                insert_sql,
                (
                    "mem_v1_exec_active",
                    "EXPERIENCE",
                    "EXECUTION_VERIFIED",
                    "ACTIVE",
                    "PROJECT",
                    "jarvis-core",
                    act_title,
                    act_content,
                    act_norm,
                    "Suite ejecutada ok",
                    0.9,
                    75,
                    1,
                    act_fp,
                    '["tests"]',
                    "{}",
                    ev_active.source_layer,
                    ev_active.source_goal_id,
                    ev_active.source_plan_id,
                    ev_active.source_task_id,
                    ev_active.source_execution_id,
                    ev_active.approval_hash,
                    ev_active.verifier_component,
                    ev_active.verified_at,
                    ev_active.evidence_sha256,
                    canonical_json(ev_active.canonical_payload()),
                    now,
                    now,
                    None,
                    None,
                    None,
                    ev_active.verified_at,
                    0,
                ),
            )
            conn.execute(
                insert_sql,
                (
                    "mem_v1_exec_revoked",
                    "FACT",
                    "EXECUTION_VERIFIED",
                    "REVOKED",
                    "PROJECT",
                    "jarvis-core",
                    rev_title,
                    rev_content,
                    rev_norm,
                    "Revocado legacy",
                    0.8,
                    60,
                    1,
                    rev_fp,
                    '["legacy"]',
                    "{}",
                    ev_revoked.source_layer,
                    ev_revoked.source_goal_id,
                    ev_revoked.source_plan_id,
                    ev_revoked.source_task_id,
                    ev_revoked.source_execution_id,
                    ev_revoked.approval_hash,
                    ev_revoked.verifier_component,
                    ev_revoked.verified_at,
                    ev_revoked.evidence_sha256,
                    canonical_json(ev_revoked.canonical_payload()),
                    now,
                    now,
                    None,
                    now,
                    None,
                    ev_revoked.verified_at,
                    0,
                ),
            )

            conn.execute(
                """
                INSERT INTO memory_fts (memory_id, title, summary, content, tags, scope_key)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("mem_v1_user", user_title, "Responder en español", user_content, "idioma", "global"),
            )
            conn.execute(
                """
                INSERT INTO memory_fts (memory_id, title, summary, content, tags, scope_key)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("mem_v1_exec_active", act_title, "Suite ejecutada ok", act_content, "tests", "jarvis-core"),
            )

            conn.execute(
                """
                INSERT INTO memory_revisions (
                    revision_id, memory_id, revision_number,
                    previous_title, previous_content, previous_summary, previous_fingerprint,
                    new_title, new_content, new_summary, new_fingerprint,
                    change_reason, changed_by, changed_at, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "rev_v1_001",
                    "mem_v1_user",
                    2,
                    "Idioma previo",
                    "Texto previo",
                    "Resumen previo",
                    "f" * 64,
                    user_title,
                    user_content,
                    "Responder en español",
                    user_fp,
                    "clarificacion_usuario",
                    "user",
                    now,
                    "USER_EXPLICIT",
                ),
            )
            conn.execute(
                """
                INSERT INTO memory_audit (audit_id, action, memory_id, actor, reason, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("aud_v1_001", "CREATE", "mem_v1_user", "user", "legacy_v1_create", "{}", now),
            )
            conn.execute("PRAGMA user_version = 1")
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        return {
            "user_id": "mem_v1_user",
            "exec_active_id": "mem_v1_exec_active",
            "exec_revoked_id": "mem_v1_exec_revoked",
            "expected_exec_active_unit": compute_default_canonical_unit_id(
                operation_type="file_write",
                category=MemoryCategory.EXPERIENCE,
            ),
            "expected_exec_active_idem": compute_execution_idempotency_key(
                source_layer=ev_active.source_layer,
                source_execution_id="exec_legacy_active",
                source_plan_id="plan_1",
                source_task_id="task_1",
                canonical_unit_id=compute_default_canonical_unit_id(
                    operation_type="file_write",
                    category=MemoryCategory.EXPERIENCE,
                ),
            ),
        }

    # ------------------------------------------------------------------
    # 1. Migración v1 -> v2, idempotencia y restauración v1/v2
    # ------------------------------------------------------------------

    def test_authentic_v1_to_v2_migration_preserves_data_and_restore_v1_backup(self) -> None:
        """Migra una DB v1 genuina a v2 sin pérdida de datos y restaura un backup v1 con verify_integrity inmediato."""
        ids = self._build_authentic_v1_database(self.db_path)

        # Snapshot consistente de backup v1 antes de migrar.
        v1_backup_db = self.backup_dir / "backup_v1_seed.db"
        with closing(sqlite3.connect(str(self.db_path))) as src, closing(sqlite3.connect(str(v1_backup_db))) as dst:
            src.backup(dst)
        v1_sha = _file_sha256(v1_backup_db)
        assert v1_sha is not None
        (self.backup_dir / "backup_v1_seed.backup.json").write_text(
            json.dumps({"backup_id": "backup_v1_seed", "sha256": v1_sha, "schema_version": 1}),
            encoding="utf-8",
        )

        store = ExperienceStore(self.db_path)
        mig1 = store.initialize()
        mig2 = store.initialize()
        self.assertEqual(mig1.schema_version, 2)
        self.assertEqual(CURRENT_SCHEMA_VERSION, 2)
        self.assertFalse(mig2.migrated)
        self.assertTrue(mig2.fts_active)

        token_after_mig = store.get_store_version_token()
        self.assertRegex(token_after_mig, r"^[0-9a-f]{16}:\d+$")

        report = store.verify_integrity()
        self.assertTrue(report.ok)
        self.assertEqual(report.schema_version, 2)
        self.assertTrue(report.fts_sync_ok)

        # Verificar preservación de datos, revisiones, auditoría e índices nuevos.
        user_rec = store.get_memory(ids["user_id"])
        exec_act = store.get_memory(ids["exec_active_id"])
        exec_rev = store.get_memory(ids["exec_revoked_id"])
        self.assertIsNotNone(user_rec)
        self.assertIsNotNone(exec_act)
        self.assertIsNotNone(exec_rev)
        assert user_rec and exec_act and exec_rev

        self.assertIsNone(user_rec.canonical_unit_id)
        self.assertIsNone(user_rec.idempotency_key)
        self.assertEqual(exec_act.canonical_unit_id, ids["expected_exec_active_unit"])
        self.assertEqual(exec_act.idempotency_key, ids["expected_exec_active_idem"])
        self.assertIsNotNone(exec_rev.idempotency_key)

        revs = store.get_revisions(ids["user_id"])
        self.assertEqual(len(revs), 1)
        self.assertEqual(revs[0].revision_id, "rev_v1_001")

        audits = store.get_audit_entries(limit=20)
        self.assertTrue(any(a.audit_id == "aud_v1_001" for a in audits))
        self.assertTrue(any(a.action == MemoryAuditAction.MIGRATE and a.reason == "schema_v2_applied" for a in audits))

        with store.acquire_lock():
            with store._open_connection() as conn:
                migs = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()]
                self.assertEqual(migs, [1, 2])
                idx_names = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='memories'"
                    ).fetchall()
                }
                self.assertIn("idx_memories_idempotency_key", idx_names)
                self.assertIn("idx_memories_active_exec_unit", idx_names)
                self.assertIn("idx_memories_source_execution_id", idx_names)
                self.assertNotIn("idx_memories_active_exec_provenance", idx_names)

        # Avanzamos la versión en la DB viva creando más recuerdos antes de restaurar el backup v1.
        for idx in range(3):
            store.create_memory(
                MemoryCreateInput(
                    category=MemoryCategory.FACT,
                    source=MemorySource.USER_EXPLICIT,
                    title=f"Hecho nuevo {idx}",
                    content=f"Dato posterior número {idx} para elevar la secuencia.",
                    scope_type=MemoryScopeType.GLOBAL,
                )
            )
        live_token_before_restore = store.get_store_version_token()
        live_seq_before_restore = int(live_token_before_restore.split(":")[1])
        self.assertGreaterEqual(live_seq_before_restore, 3)

        restore_res = store.restore_from_backup(
            "backup_v1_seed",
            backup_dir=self.backup_dir,
            actor="admin",
        )
        self.assertTrue(restore_res.restored)
        self.assertTrue(restore_res.integrity_report.ok)
        self.assertEqual(restore_res.integrity_report.schema_version, 2)

        # Verificación inmediata sin pasos manuales intermedios.
        post_restore_report = store.verify_integrity()
        self.assertTrue(post_restore_report.ok)
        self.assertEqual(post_restore_report.schema_version, 2)
        self.assertTrue(post_restore_report.fts_sync_ok)

        restored_token = store.get_store_version_token()
        self.assertNotEqual(restored_token, token_after_mig)
        self.assertNotEqual(restored_token, live_token_before_restore)
        self.assertNotEqual(restored_token.split(":")[0], live_token_before_restore.split(":")[0])
        self.assertGreater(int(restored_token.split(":")[1]), live_seq_before_restore)
        # Los datos añadidos después del backup v1 desaparecen tras restaurar v1.
        self.assertEqual(store.get_stats().active_memories, 2)

        # Confirmamos además que la tabla memory_audit reconstruida acepta REJECT_IDEMPOTENCY_CONFLICT en una DB migrada desde v1.
        with self.assertRaises(MemoryIdempotencyConflictError):
            store.create_memory(
                MemoryCreateInput(
                    category=MemoryCategory.EXPERIENCE,
                    source=MemorySource.EXECUTION_VERIFIED,
                    title="Intento adulterado sobre legacy v1",
                    content="Contenido incompatible para validar el CHECK reconstruido de memory_audit.",
                    scope_type=MemoryScopeType.PROJECT,
                    scope_key="jarvis-core",
                    verification_evidence=self._make_evidence(
                        exec_id="exec_legacy_active",
                        plan_id="plan_1",
                        task_id="task_1",
                    ),
                    canonical_unit_id=exec_act.canonical_unit_id,
                )
            )

    # ------------------------------------------------------------------
    # 2. Matriz de idempotencia estricta y separación de dedupe
    # ------------------------------------------------------------------

    def test_execution_verified_idempotency_matrix_and_conventional_dedupe_separation(self) -> None:
        """Cubre retry idempotente, múltiples unidades por ejecución, conflictos y separación de fingerprint."""
        store = ExperienceStore(self.db_path)
        ev_a1 = self._make_evidence(exec_id="exec_m_1", plan_id="p_1", task_id="t_1")
        token_0 = store.get_store_version_token()

        # 1) Alta inicial de EXECUTION_VERIFIED.
        r1 = store.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.EXPERIENCE,
                source=MemorySource.EXECUTION_VERIFIED,
                title="Exportación reporte PDF",
                content="El archivo out.txt fue escrito y verificado en disco.",
                summary="Archivo verificado",
                tags=("export", "pdf"),
                confidence=0.92,
                importance=80,
                scope_type=MemoryScopeType.PROJECT,
                scope_key="jarvis-core",
                verification_evidence=ev_a1,
                canonical_unit_id="unit_export_pdf",
            )
        )
        self.assertTrue(r1.created)
        self.assertIsNotNone(r1.record.idempotency_key)
        self.assertEqual(r1.record.canonical_unit_id, "unit_export_pdf")
        token_1 = store.get_store_version_token()
        self.assertNotEqual(token_1, token_0)

        # 2) Retry idempotente exacto -> DEDUPE_HIT sin mutar recuerdo ni token.
        r1_retry = store.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.EXPERIENCE,
                source=MemorySource.EXECUTION_VERIFIED,
                title="Exportación reporte PDF",
                content="El archivo out.txt fue escrito y verificado en disco.",
                summary="Archivo verificado",
                tags=("export", "pdf"),
                confidence=0.92,
                importance=80,
                scope_type=MemoryScopeType.PROJECT,
                scope_key="jarvis-core",
                verification_evidence=ev_a1,
                canonical_unit_id="unit_export_pdf",
            )
        )
        self.assertFalse(r1_retry.created)
        self.assertTrue(r1_retry.deduplicated)
        self.assertEqual(r1_retry.matched_reason, "idempotency_key")
        self.assertEqual(r1_retry.record.memory_id, r1.record.memory_id)
        self.assertEqual(store.get_store_version_token(), token_1)

        # 3) Misma ejecución pero otra canonical_unit_id -> crea segundo recuerdo válido sin colisión.
        r2_unit_b = store.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.FACT,
                source=MemorySource.EXECUTION_VERIFIED,
                title="Ruta destino confirmada",
                content="El destino soporta exportación con codificación UTF-8.",
                scope_type=MemoryScopeType.PROJECT,
                scope_key="jarvis-core",
                verification_evidence=ev_a1,
                canonical_unit_id="unit_utf8_fact",
            )
        )
        self.assertTrue(r2_unit_b.created)
        self.assertNotEqual(r2_unit_b.record.memory_id, r1.record.memory_id)
        token_2 = store.get_store_version_token()
        self.assertNotEqual(token_2, token_1)

        # 4) Ejecución distinta con contenido exactamente idéntico -> NO fusiona por content_fingerprint.
        ev_other_exec = self._make_evidence(exec_id="exec_m_2", plan_id="p_2", task_id="t_9")
        r3_other_exec = store.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.EXPERIENCE,
                source=MemorySource.EXECUTION_VERIFIED,
                title="Exportación reporte PDF",
                content="El archivo out.txt fue escrito y verificado en disco.",
                summary="Archivo verificado",
                tags=("export", "pdf"),
                confidence=0.92,
                importance=80,
                scope_type=MemoryScopeType.PROJECT,
                scope_key="jarvis-core",
                verification_evidence=ev_other_exec,
                canonical_unit_id="unit_export_pdf",
            )
        )
        self.assertTrue(r3_other_exec.created)
        self.assertNotEqual(r3_other_exec.record.memory_id, r1.record.memory_id)
        token_3 = store.get_store_version_token()
        self.assertNotEqual(token_3, token_2)

        # 5) Misma idempotency_key con contenido distinto -> MemoryIdempotencyConflictError + auditoría + token intacto.
        with self.assertRaises(MemoryIdempotencyConflictError):
            store.create_memory(
                MemoryCreateInput(
                    category=MemoryCategory.EXPERIENCE,
                    source=MemorySource.EXECUTION_VERIFIED,
                    title="Exportación reporte PDF",
                    content="Contenido divergente intentando reutilizar la misma idempotency_key.",
                    summary="Archivo verificado",
                    tags=("export", "pdf"),
                    confidence=0.92,
                    importance=80,
                    scope_type=MemoryScopeType.PROJECT,
                    scope_key="jarvis-core",
                    verification_evidence=ev_a1,
                    canonical_unit_id="unit_export_pdf",
                )
            )
        self.assertEqual(store.get_store_version_token(), token_3)
        self.assertEqual(store.get_memory(r1.record.memory_id).content, r1.record.content)

        # 6) Misma idempotency_key con evidencia modificada (otro artifact -> distinto evidence_sha256) -> conflicto.
        ev_modified = self._make_evidence(
            exec_id="exec_m_1",
            plan_id="p_1",
            task_id="t_1",
            verified_artifacts=("temp/jarvis_test_isolated/different_out.txt",),
        )
        with self.assertRaises(MemoryIdempotencyConflictError):
            store.create_memory(
                MemoryCreateInput(
                    category=MemoryCategory.EXPERIENCE,
                    source=MemorySource.EXECUTION_VERIFIED,
                    title="Exportación reporte PDF",
                    content="El archivo out.txt fue escrito y verificado en disco.",
                    summary="Archivo verificado",
                    tags=("export", "pdf"),
                    confidence=0.92,
                    importance=80,
                    scope_type=MemoryScopeType.PROJECT,
                    scope_key="jarvis-core",
                    verification_evidence=ev_modified,
                    canonical_unit_id="unit_export_pdf",
                )
            )
        self.assertEqual(store.get_store_version_token(), token_3)

        conflict_audits = [
            a
            for a in store.get_audit_entries(memory_id=r1.record.memory_id, limit=50)
            if a.action == MemoryAuditAction.REJECT_IDEMPOTENCY_CONFLICT
        ]
        self.assertEqual(len(conflict_audits), 2)

        # 7) Conflicto por unidad canónica activa con clave explícita distinta -> rechazado sin sobreescritura.
        with self.assertRaises(MemoryIdempotencyConflictError):
            store.create_memory(
                MemoryCreateInput(
                    category=MemoryCategory.EXPERIENCE,
                    source=MemorySource.EXECUTION_VERIFIED,
                    title="Exportación reporte PDF",
                    content="El archivo out.txt fue escrito y verificado en disco.",
                    scope_type=MemoryScopeType.PROJECT,
                    scope_key="jarvis-core",
                    verification_evidence=ev_a1,
                    canonical_unit_id="unit_export_pdf",
                    idempotency_key="custom_key_distinct_same_active_unit",
                )
            )
        self.assertEqual(store.get_store_version_token(), token_3)

        # 8) Origen convencional USER_EXPLICIT con texto igual al EXECUTION_VERIFIED NO colisiona con él,
        #    pero un segundo USER_EXPLICIT idéntico sí deduplica por content_fingerprint.
        u1 = store.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.EXPERIENCE,
                source=MemorySource.USER_EXPLICIT,
                title="Exportación reporte PDF",
                content="El archivo out.txt fue escrito y verificado en disco.",
                scope_type=MemoryScopeType.PROJECT,
                scope_key="jarvis-core",
            )
        )
        u2 = store.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.EXPERIENCE,
                source=MemorySource.USER_EXPLICIT,
                title="exportación   reporte pdf",
                content="el archivo out.txt fue escrito y verificado en disco!",
                scope_type=MemoryScopeType.PROJECT,
                scope_key="jarvis-core",
            )
        )
        self.assertTrue(u1.created)
        self.assertNotIn(u1.record.memory_id, {r1.record.memory_id, r3_other_exec.record.memory_id})
        self.assertFalse(u2.created)
        self.assertTrue(u2.deduplicated)
        self.assertEqual(u2.matched_reason, "content_fingerprint")
        self.assertEqual(u1.record.memory_id, u2.record.memory_id)

        # 9) Rechazo de secretos antes de tocar persistencia o registrar texto sensible en auditoría.
        with self.assertRaises(MemoryPolicyError):
            store.create_memory(
                MemoryCreateInput(
                    category=MemoryCategory.EXPERIENCE,
                    source=MemorySource.EXECUTION_VERIFIED,
                    title="Intento con secreto",
                    content="api_key=" + "sk-" + "1234567890abcdef1234567890abcdef",
                    scope_type=MemoryScopeType.PROJECT,
                    scope_key="jarvis-core",
                    verification_evidence=ev_a1,
                    canonical_unit_id="unit_export_pdf",
                )
            )

    # ------------------------------------------------------------------
    # 3. store_version_token: monotonía, atomicidad y coherencia con backups
    # ------------------------------------------------------------------

    def test_store_version_token_monotonic_on_all_mutations_and_atomic_rollback(self) -> None:
        """Valida que toda mutación cambia `store_version_token`, las lecturas no lo mueven y un rollback lo preserva."""
        store = ExperienceStore(self.db_path)
        retriever = MemoryRetriever(store)
        seen_tokens: list[str] = [store.get_store_version_token()]

        def _assert_advanced() -> str:
            t = store.get_store_version_token()
            self.assertNotIn(t, seen_tokens)
            seen_tokens.append(t)
            return t

        # Create
        m1 = store.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.PREFERENCE,
                source=MemorySource.USER_EXPLICIT,
                title="Pref test",
                content="Usar pytest o unittest de forma aislada en jarvis-core.",
                scope_type=MemoryScopeType.PROJECT,
                scope_key="jarvis-core",
                pending_confirmation=True,
            )
        ).record
        t_after_create = _assert_advanced()

        # Confirm pending
        store.confirm_memory(m1.memory_id, actor="user", reason="ok")
        _assert_advanced()

        # Revise
        store.update_memory_with_revision(
            m1.memory_id,
            content="Usar siempre unittest en sandbox jarvis_test_isolated para pruebas seguras.",
            reason="precision",
            actor="user",
        )
        t_after_revise = _assert_advanced()

        # Operación de lectura + backup + retriever mantienen el mismo token exacto.
        _ = store.get_memory(m1.memory_id)
        _ = store.list_memories()
        _ = store.get_stats()
        _ = store.get_audit_entries(limit=10)
        _ = store.get_revisions(m1.memory_id)
        _ = store.verify_integrity()
        self.assertEqual(store.get_store_version_token(), t_after_revise)

        snap_res = retriever.retrieve(
            "unittest sandbox jarvis",
            target=MemoryRetrievalTarget.NORMAL,
            scope_type=MemoryScopeType.PROJECT,
            scope_key="jarvis-core",
        )
        self.assertEqual(snap_res.store_version_token, t_after_revise)
        self.assertEqual(snap_res.returned_count, 1)

        blocked_res = retriever.retrieve("unittest", allow_history=False)
        self.assertEqual(blocked_res.store_version_token, "")
        self.assertTrue(blocked_res.blocked_by_history_policy)

        empty_res = retriever.retrieve("a y ?", allow_history=True)
        self.assertEqual(empty_res.store_version_token, "")
        self.assertEqual(empty_res.backend_used, "none")

        # Backup v2 antes de seguir mutando.
        bkp = store.create_backup(backup_dir=self.backup_dir, actor="admin")
        self.assertEqual(store.get_store_version_token(), t_after_revise)

        # Create + Supersede
        m2 = store.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.PREFERENCE,
                source=MemorySource.USER_EXPLICIT,
                title="Pref test 2",
                content="Pref test suplementaria para transición supersede.",
                scope_type=MemoryScopeType.PROJECT,
                scope_key="jarvis-core",
            )
        ).record
        _assert_advanced()

        store.transition_status(
            m1.memory_id,
            new_status=MemoryStatus.SUPERSEDED,
            reason="replaced",
            actor="user",
            superseded_by=m2.memory_id,
        )
        _assert_advanced()

        # Expire + Revoke
        m3 = store.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.FACT,
                source=MemorySource.USER_EXPLICIT,
                title="Hecho temporal",
                content="Hecho temporal para probar transición a EXPIRED.",
                scope_type=MemoryScopeType.GLOBAL,
            )
        ).record
        _assert_advanced()
        store.transition_status(m3.memory_id, new_status=MemoryStatus.EXPIRED, reason="ttl_expired", actor="system")
        _assert_advanced()
        store.revoke_memory(m2.memory_id, reason="no_longer_needed", actor="user")
        _assert_advanced()

        # Purge
        store.purge_memory(m3.memory_id, actor="user", reason="gdpr_cleanup")
        t_before_fail = _assert_advanced()

        # Rollback transaccional forzado: si un paso falla tras modificar el token dentro de TX,
        # la DB vuelve exactamente al token previo sin incrementos fantasmas.
        with patch.object(
            store,
            "_insert_audit_tx",
            side_effect=RuntimeError("simulated_mid_tx_failure"),
        ):
            with self.assertRaises(RuntimeError):
                store.create_memory(
                    MemoryCreateInput(
                        category=MemoryCategory.FACT,
                        source=MemorySource.USER_EXPLICIT,
                        title="No debe persistir",
                        content="Este registro debe revertirse por completo ante fallo medio.",
                        scope_type=MemoryScopeType.GLOBAL,
                    )
                )
        self.assertEqual(store.get_store_version_token(), t_before_fail)
        self.assertTrue(store.verify_integrity().ok)

        # Persistencia tras reabrir desde disco con otra instancia.
        reopened = ExperienceStore(self.db_path)
        self.assertEqual(reopened.get_store_version_token(), t_before_fail)

        # Restaurar un backup v2 más antiguo rota epoch y avanza seq > pre_restore_seq.
        pre_restore_seq = int(t_before_fail.split(":")[1])
        reopened.restore_from_backup(bkp.backup_id, backup_dir=self.backup_dir, actor="admin")
        t_post_restore = reopened.get_store_version_token()
        self.assertNotIn(t_post_restore, seen_tokens)
        self.assertNotEqual(t_post_restore.split(":")[0], t_before_fail.split(":")[0])
        self.assertGreater(int(t_post_restore.split(":")[1]), pre_restore_seq)
        self.assertTrue(reopened.verify_integrity().ok)
        self.assertEqual(t_after_create, seen_tokens[1])

    # ------------------------------------------------------------------
    # 4. Invalidación por ejecución revertida (`invalidate_by_execution`)
    # ------------------------------------------------------------------

    def test_invalidate_by_execution_isolated_idempotent_and_private_mode_blocked(self) -> None:
        """Invalida solo recuerdos `EXECUTION_VERIFIED` de esa ejecución, preserva auditoría y respeta modo privado."""
        store = ExperienceStore(self.db_path)
        engine = MemoryEngine(store=store, backup_dir=self.backup_dir)

        ev_target = self._make_evidence(exec_id="exec_rollback_me", plan_id="p_r", task_id="t_1")
        ev_other = self._make_evidence(exec_id="exec_keep_me", plan_id="p_r", task_id="t_2")

        t_exp = engine.store.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.EXPERIENCE,
                source=MemorySource.EXECUTION_VERIFIED,
                title="Paso revertido alpha",
                content="Resultado temporal alpha generado por ejecución revertida.",
                scope_type=MemoryScopeType.PROJECT,
                scope_key="jarvis-core",
                verification_evidence=ev_target,
                canonical_unit_id="unit_alpha",
            )
        ).record
        t_fact = engine.store.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.FACT,
                source=MemorySource.EXECUTION_VERIFIED,
                title="Hecho revertido beta",
                content="Efecto secundario alpha eliminado tras rollback seguro.",
                scope_type=MemoryScopeType.PROJECT,
                scope_key="jarvis-core",
                verification_evidence=ev_target,
                canonical_unit_id="unit_beta",
            )
        ).record
        o_exp = engine.store.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.EXPERIENCE,
                source=MemorySource.EXECUTION_VERIFIED,
                title="Otra ejecución intacta",
                content="Resultado permanente alpha de otra ejecución independiente.",
                scope_type=MemoryScopeType.PROJECT,
                scope_key="jarvis-core",
                verification_evidence=ev_other,
                canonical_unit_id="unit_gamma",
            )
        ).record
        u_exp = engine.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.FACT,
                source=MemorySource.USER_EXPLICIT,
                title="Nota usuario permanente",
                content="Hecho manual alpha escrito por el usuario sin relación con rollback.",
                scope_type=MemoryScopeType.PROJECT,
                scope_key="jarvis-core",
            )
        ).record

        token_before = engine.get_store_version_token()
        inv_res = engine.invalidate_by_execution("exec_rollback_me", reason="n5_rollback_verified")
        self.assertIsInstance(inv_res, ExecutionInvalidationResult)
        self.assertEqual(inv_res.execution_id, "exec_rollback_me")
        self.assertEqual(inv_res.invalidated_count, 2)
        self.assertEqual(set(inv_res.invalidated_memory_ids), {t_exp.memory_id, t_fact.memory_id})
        self.assertNotEqual(inv_res.store_version_token, token_before)
        self.assertEqual(engine.get_store_version_token(), inv_res.store_version_token)

        # Solo revocación lógica: los recuerdos siguen presentes para trazabilidad con REVOKED,
        # pero fuera de FTS5 y recuperación normal.
        rec_a = store.get_memory(t_exp.memory_id)
        rec_b = store.get_memory(t_fact.memory_id)
        rec_other = store.get_memory(o_exp.memory_id)
        rec_user = store.get_memory(u_exp.memory_id)
        assert rec_a and rec_b and rec_other and rec_user
        self.assertEqual(rec_a.status, MemoryStatus.REVOKED)
        self.assertEqual(rec_b.status, MemoryStatus.REVOKED)
        self.assertEqual(rec_other.status, MemoryStatus.ACTIVE)
        self.assertEqual(rec_user.status, MemoryStatus.ACTIVE)

        search_after = engine.retrieve(
            "alpha",
            scope_type=MemoryScopeType.PROJECT,
            scope_key="jarvis-core",
        )
        returned_ids = {item.record.memory_id for item in search_after.items}
        self.assertNotIn(t_exp.memory_id, returned_ids)
        self.assertNotIn(t_fact.memory_id, returned_ids)
        self.assertEqual(returned_ids, {o_exp.memory_id, u_exp.memory_id})

        # Segundo intento sobre la misma ejecución: idempotente no-op sin mover el token.
        token_after_inv = engine.get_store_version_token()
        inv_again = engine.invalidate_by_execution("exec_rollback_me", reason="n5_rollback_verified")
        self.assertEqual(inv_again.invalidated_count, 0)
        self.assertEqual(inv_again.already_inactive_count, 2)
        self.assertEqual(engine.get_store_version_token(), token_after_inv)

        # Ejecución inexistente: 0 afectadas y token intacto.
        inv_unknown = engine.invalidate_by_execution("exec_never_existed")
        self.assertEqual(inv_unknown.invalidated_count, 0)
        self.assertEqual(inv_unknown.already_inactive_count, 0)
        self.assertEqual(engine.get_store_version_token(), token_after_inv)

        # Un retry idempotente de una ejecución ya invalidada NO resucita el recuerdo revocado.
        retry_revoked = store.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.EXPERIENCE,
                source=MemorySource.EXECUTION_VERIFIED,
                title="Paso revertido alpha",
                content="Resultado temporal alpha generado por ejecución revertida.",
                scope_type=MemoryScopeType.PROJECT,
                scope_key="jarvis-core",
                verification_evidence=ev_target,
                canonical_unit_id="unit_alpha",
            )
        )
        self.assertFalse(retry_revoked.created)
        self.assertTrue(retry_revoked.deduplicated)
        self.assertEqual(retry_revoked.record.status, MemoryStatus.REVOKED)
        self.assertEqual(engine.get_store_version_token(), token_after_inv)

        # Un retry de esa misma clave con contenido diferente sigue siendo conflicto rechazable.
        with self.assertRaises(MemoryIdempotencyConflictError):
            store.create_memory(
                MemoryCreateInput(
                    category=MemoryCategory.EXPERIENCE,
                    source=MemorySource.EXECUTION_VERIFIED,
                    title="Intento resurrección adulterado",
                    content="Otro texto bajo clave revocada.",
                    scope_type=MemoryScopeType.PROJECT,
                    scope_key="jarvis-core",
                    verification_evidence=ev_target,
                    canonical_unit_id="unit_alpha",
                )
            )

        # Bloqueo en modo privado tanto en MemoryEngine como en ExperienceStore.
        private_flag = True
        eng_priv = MemoryEngine(
            store=store,
            backup_dir=self.backup_dir,
            is_private_mode_fn=lambda: private_flag,
        )
        with self.assertRaises(MemoryPrivateModeError):
            eng_priv.invalidate_by_execution("exec_keep_me")
        with self.assertRaises(MemoryPrivateModeError):
            store.invalidate_by_execution("exec_keep_me", private_mode=True)
        self.assertEqual(store.get_memory(o_exp.memory_id).status, MemoryStatus.ACTIVE)
        self.assertTrue(store.verify_integrity().ok)

    # ------------------------------------------------------------------
    # 5. Concurrencia multi-hilo y multi-proceso v2
    # ------------------------------------------------------------------

    def test_concurrent_threads_idempotency_and_multiprocess_invalidation(self) -> None:
        """Valida carrera entre hilos sobre idempotency_key y subprocesos reales escribiendo/invalidando."""
        store = ExperienceStore(self.db_path)
        ev_race = self._make_evidence(exec_id="exec_race_1", plan_id="p_race", task_id="t_race")

        def _try_insert(variant_idx: int) -> str:
            content = (
                "Texto idempotente común en carrera concurrente."
                if variant_idx % 2 == 0
                else f"Texto conflictivo variante {variant_idx} sobre misma clave."
            )
            try:
                res = store.create_memory(
                    MemoryCreateInput(
                        category=MemoryCategory.EXPERIENCE,
                        source=MemorySource.EXECUTION_VERIFIED,
                        title="Carrera concurrente N8",
                        content=content,
                        scope_type=MemoryScopeType.PROJECT,
                        scope_key="jarvis-core",
                        verification_evidence=ev_race,
                        canonical_unit_id="unit_race",
                    )
                )
                return "created" if res.created else "dedupe"
            except MemoryIdempotencyConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(_try_insert, range(12)))

        self.assertEqual(outcomes.count("created"), 1)
        self.assertGreaterEqual(outcomes.count("conflict"), 1)
        self.assertTrue(store.verify_integrity().ok)

        # Subproceso real ejecutando altas idempotentes e invalidaciones sobre la misma DB v2.
        worker_code = """
import os, sys
os.environ["JARVIS_ENV"] = "test"
from src.memory import (
    ExecutionVerificationEvidence, ExperienceStore, MemoryCategory,
    MemoryCreateInput, MemoryIdempotencyConflictError, MemoryScopeType,
    MemorySource, utc_now_iso
)
db_path = sys.argv[1]
store = ExperienceStore(db_path, lock_timeout_sec=10.0)
for idx in range(5):
    ex_id = f"subproc_exec_{idx}"
    ev = ExecutionVerificationEvidence(
        source_layer="N5_CHAIN_ORCHESTRATOR",
        source_execution_id=ex_id,
        source_plan_id="sp_1",
        source_goal_id="sg_1",
        source_task_id=f"st_{idx}",
        approval_hash="c" * 64,
        executor_component="FileEditorSkill",
        operation_type="file_write",
        execution_outcome="verified",
        expected_outcome="Subproceso OK",
        verifier_component="ChainOrchestratorVerifier",
        verified_at=utc_now_iso(),
        verified_artifacts=(f"temp/jarvis_test_isolated/sp_{idx}.txt",),
        scope_type=MemoryScopeType.PROJECT,
        scope_key="jarvis-core",
    )
    store.create_memory(MemoryCreateInput(
        category=MemoryCategory.EXPERIENCE,
        source=MemorySource.EXECUTION_VERIFIED,
        title=f"Subproceso exp {idx}",
        content=f"Experiencia concurrente v2 generada por subproceso iteracion {idx}.",
        scope_type=MemoryScopeType.PROJECT,
        scope_key="jarvis-core",
        verification_evidence=ev,
    ))
    if idx % 2 == 0:
        inv = store.invalidate_by_execution(ex_id, reason="subproc_rollback")
        assert inv.invalidated_count == 1
print(store.get_store_version_token())
"""
        env = dict(os.environ)
        env["JARVIS_ENV"] = "test"
        proc = subprocess.Popen(
            [sys.executable, "-c", worker_code, str(self.db_path)],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        retriever = MemoryRetriever(store)
        observed_tokens: list[str] = []
        for idx in range(6):
            store.create_memory(
                MemoryCreateInput(
                    category=MemoryCategory.FACT,
                    source=MemorySource.USER_EXPLICIT,
                    title=f"Parent parallel {idx}",
                    content=f"Dato escrito desde proceso padre {idx} durante carrera subproceso.",
                    scope_type=MemoryScopeType.PROJECT,
                    scope_key="jarvis-core",
                )
            )
            ret = retriever.retrieve(
                "concurrente padre subproceso",
                scope_type=MemoryScopeType.PROJECT,
                scope_key="jarvis-core",
            )
            if ret.store_version_token:
                observed_tokens.append(ret.store_version_token)

        stdout, stderr = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, msg=f"Subproceso falló: {stderr}")
        self.assertTrue(stdout.strip())
        self.assertGreaterEqual(len(observed_tokens), 6)
        final_report = store.verify_integrity()
        self.assertTrue(final_report.ok)
        self.assertTrue(final_report.fts_sync_ok)


if __name__ == "__main__":
    unittest.main()
