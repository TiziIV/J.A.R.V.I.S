"""
Suite integral de pruebas para el subsistema N5: Recovery & Transaction Manager (`tests/test_transaction_recovery.py`).

Cubre los 24 escenarios obligatorios (T01 a T24) de persistencia transaccional, Write-Ahead Log (WAL),
rehidratación tras reinicio, cadenas de hashes por archivo (`H0 -> H1 -> H2 -> H3`), reversión LIFO
(`H3 -> H2 -> H1 -> H0`), interrupciones durante ejecución y durante rollback, bloqueo global entre hilos
y descriptores de sistema operativo (`msvcrt.locking`), detección de corrupción en WAL/Snapshot,
protección ante ediciones externas o backups ausentes/corruptos, preservación de backups huérfanos
y aislamiento estricto de `data/memory_store.json`.
"""

from __future__ import annotations

import hashlib
import json
import msvcrt
import os
from pathlib import Path
import shutil
import stat
import threading
import time
from typing import Any, Optional
import unittest
import uuid

os.environ["JARVIS_ENV"] = "test"

from config.settings import BASE_DIR
from skills.file_editor_skill import (
    AuditLogger,
    FileEditorSkill,
    compute_sha256_file,
)
from src.brain.chain_orchestrator import (
    ChainOrchestrator,
    ChainState,
    ChangeOperation,
    ChangePlan,
    OperationStatus,
)
from src.brain.transaction_manager import (
    GENESIS_PREV_HASH,
    GlobalTransactionLock,
    InvalidTransactionStateError,
    JournalCorruptionError,
    OperationJournalStage,
    SimulatedTransactionInterrupt,
    TransactionLockTimeoutError,
    TransactionManager,
    TransactionState,
    WalEvent,
    resolve_safe_journal_dir,
)


PROTECTED_MEMORY_PATH: Path = (BASE_DIR / "data" / "memory_store.json").resolve()


def _snapshot_memory_metadata() -> tuple[bool, Optional[int], Optional[str]]:
    """Obtiene existencia, tamaño en bytes y SHA-256 de `data/memory_store.json` sin imprimir su contenido."""
    if not PROTECTED_MEMORY_PATH.exists():
        return False, None, None
    raw = PROTECTED_MEMORY_PATH.read_bytes()
    return True, len(raw), hashlib.sha256(raw).hexdigest().upper()


def _force_remove_tree(root: Path) -> None:
    """Elimina recursivamente un directorio temporal de prueba en Windows limpiando atributos read-only."""
    if not root.exists():
        return

    def _on_rm_error(func: Any, path_str: str, _exc_info: Any) -> None:
        try:
            os.chmod(path_str, stat.S_IWRITE | stat.S_IREAD)
            func(path_str)
        except OSError:
            pass

    for _ in range(3):
        try:
            shutil.rmtree(root, onerror=_on_rm_error)
            if not root.exists():
                return
        except OSError:
            time.sleep(0.03)


class TestTransactionRecoveryN5(unittest.TestCase):
    """Pruebas verificables T01–T24 para Recovery & Transaction Manager (N5)."""

    _mem_before_suite: tuple[bool, Optional[int], Optional[str]] = (False, None, None)

    @classmethod
    def setUpClass(cls) -> None:
        os.environ["JARVIS_ENV"] = "test"
        cls._mem_before_suite = _snapshot_memory_metadata()

    @classmethod
    def tearDownClass(cls) -> None:
        after = _snapshot_memory_metadata()
        if cls._mem_before_suite != after:
            raise AssertionError(
                f"Violación de aislamiento en data/memory_store.json: antes={cls._mem_before_suite}, después={after}"
            )

    def setUp(self) -> None:
        os.environ["JARVIS_ENV"] = "test"
        self._mem_before_test = _snapshot_memory_metadata()

        # Directorio aislado único por cada test dentro de temp/
        self.run_root: Path = (BASE_DIR / "temp" / f"run_{uuid.uuid4().hex}").resolve()
        self.workspace_dir: Path = self.run_root / "workspace"
        self.backup_dir: Path = self.run_root / "backups"
        self.journal_dir: Path = self.run_root / "transactions"
        self.audit_log_path: Path = self.run_root / "logs" / "file_ops.jsonl"

        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.journal_dir.mkdir(parents=True, exist_ok=True)

        self.editor = FileEditorSkill(
            authorized_roots=[self.workspace_dir, self.backup_dir],
            backup_dir=self.backup_dir,
            audit_logger=AuditLogger(self.audit_log_path),
            sleep_fn=lambda _s: None,
        )
        self.tx_manager = TransactionManager(
            self.editor,
            journal_dir=self.journal_dir,
            lock_timeout_sec=1.0,
        )
        self.orchestrator = ChainOrchestrator(
            file_editor=self.editor,
            transaction_manager=self.tx_manager,
        )

    def tearDown(self) -> None:
        _force_remove_tree(self.run_root)
        after = _snapshot_memory_metadata()
        self.assertEqual(
            self._mem_before_test,
            after,
            "data/memory_store.json no debe alterarse durante ninguna prueba de N5.",
        )

    # ------------------------------------------------------------------
    # Helpers de construcción de operaciones y planes aprobados
    # ------------------------------------------------------------------
    def _make_ini_op(
        self,
        op_id: str,
        file_path: Path,
        key: str,
        expected_current_value: str,
        proposed_value: str,
        *,
        section: str = "ScalabilityGroups",
        depends_on: Optional[list[str]] = None,
        expected_file_hash: Optional[str] = None,
    ) -> ChangeOperation:
        sha = expected_file_hash or compute_sha256_file(file_path)
        return ChangeOperation(
            id=op_id,
            file_path=str(file_path),
            format="ini",
            operation_type="update_key",
            section=section,
            key=key,
            expected_current_value=expected_current_value,
            proposed_value=proposed_value,
            justification=f"Ajuste de {key} a {proposed_value} para prueba transaccional N5",
            risk_level="LOW",
            evidence="VERIFIED",
            verification_method="read_back_and_verify_ini_key_and_sha256",
            reversible=True,
            requires_approval=True,
            expected_file_hash=sha,
            depends_on=list(depends_on or []),
        )

    def _prepare_and_authorize_plan(
        self,
        execution_id: str,
        operations: list[ChangeOperation],
        *,
        orchestrator: Optional[ChainOrchestrator] = None,
        selected_ids: Optional[list[str]] = None,
    ) -> tuple[ChangePlan, str, str]:
        orch = orchestrator or self.orchestrator
        orch.create_execution(execution_id)
        plan = ChangePlan(
            plan_id=f"plan_{execution_id}",
            execution_id=execution_id,
            user_goal="Prueba transaccional N5",
            application_name="TestGameN5",
            application_version="1.0",
            hardware_context={"gpu_model": "NVIDIA GeForce GTX 1660", "vram_gb": 6},
            operations=operations,
            provider="local_test",
        )
        sim = orch.simulate_plan(plan, selected_operation_ids=selected_ids)
        approval_hash = str(sim["approval_hash"])
        token = orch.authorize_plan_interactively(
            plan,
            approval_hash=approval_hash,
            selected_operation_ids=selected_ids,
            input_fn=lambda _p: "sí",
        )
        return plan, approval_hash, token

    # ==================================================================
    # Grupo 1 — Persistencia y rehidratación
    # ==================================================================

    def test_T01_full_transaction_persistence_wal_and_snapshot(self) -> None:
        """T01: Verifica que una ejecución aprobada crea journal y WAL en orden y culmina en COMMITTED."""
        cfg = self.workspace_dir / "t01.ini"
        cfg.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\nsg.TextureQuality=3\n", encoding="utf-8")

        op1 = self._make_ini_op("op-1", cfg, "sg.ShadowQuality", "3", "1")
        op2 = self._make_ini_op("op-2", cfg, "sg.TextureQuality", "3", "1", depends_on=["op-1"])
        plan, app_hash, token = self._prepare_and_authorize_plan("exec-t01", [op1, op2])

        res = self.orchestrator.execute_approved_plan(
            plan,
            approval_hash=app_hash,
            authorization_token=token,
        )
        self.assertEqual(res["state"], ChainState.COMPLETED.value)

        tx = self.tx_manager.get_latest_transaction_for_execution("exec-t01")
        self.assertIsNotNone(tx)
        assert tx is not None
        self.assertEqual(tx.state, TransactionState.COMMITTED.value)

        wal_file = self.tx_manager.journal.wal_path(tx.transaction_id)
        snap_file = self.tx_manager.journal.snapshot_path(tx.transaction_id)
        self.assertTrue(wal_file.exists())
        self.assertTrue(snap_file.exists())

        events = self.tx_manager.journal.validate_wal_file(tx.transaction_id)
        self.assertGreaterEqual(len(events), 8)
        self.assertEqual([ev.seq for ev in events], list(range(1, len(events) + 1)))
        self.assertEqual(events[0].previous_event_sha256, GENESIS_PREV_HASH)
        for idx in range(1, len(events)):
            self.assertEqual(events[idx].previous_event_sha256, events[idx - 1].event_sha256)

        self.assertEqual(tx.last_seq, events[-1].seq)
        self.assertEqual(tx.last_event_sha256, events[-1].event_sha256)
        event_types = [ev.event_type for ev in events]
        self.assertIn("TRANSACTION_CREATED", event_types)
        self.assertIn("STATE_PREPARED", event_types)
        self.assertIn("OPERATION_INTENT_LOGGED", event_types)
        self.assertIn("OPERATION_BACKUP_VERIFIED", event_types)
        self.assertIn("OPERATION_APPLIED", event_types)
        self.assertEqual(event_types[-1], "STATE_COMMITTED")

    def test_T02_rehydration_and_rollback_after_restart(self) -> None:
        """T02: Verifica rehidratación y rollback en una nueva instancia de ChainOrchestrator tras reinicio."""
        cfg = self.workspace_dir / "t02.ini"
        original_content = "[ScalabilityGroups]\nsg.ShadowQuality=3\nsg.TextureQuality=3\n"
        cfg.write_text(original_content, encoding="utf-8")
        h0 = compute_sha256_file(cfg)

        op1 = self._make_ini_op("op-1", cfg, "sg.ShadowQuality", "3", "1")
        op2 = self._make_ini_op("op-2", cfg, "sg.TextureQuality", "3", "1", depends_on=["op-1"])
        plan, app_hash, token = self._prepare_and_authorize_plan("exec-t02", [op1, op2])

        self.orchestrator.execute_approved_plan(
            plan,
            approval_hash=app_hash,
            authorization_token=token,
        )
        self.assertNotEqual(compute_sha256_file(cfg), h0)

        # Simulamos reinicio completo de J.A.R.V.I.S.: nuevo FileEditorSkill, nuevo TransactionManager y nuevo ChainOrchestrator
        editor_restarted = FileEditorSkill(
            authorized_roots=[self.workspace_dir, self.backup_dir],
            backup_dir=self.backup_dir,
            audit_logger=AuditLogger(self.audit_log_path),
            sleep_fn=lambda _s: None,
        )
        tx_mgr_restarted = TransactionManager(
            editor_restarted,
            journal_dir=self.journal_dir,
        )
        orch_restarted = ChainOrchestrator(
            file_editor=editor_restarted,
            transaction_manager=tx_mgr_restarted,
        )
        self.assertEqual(orch_restarted._executions, {})
        self.assertEqual(orch_restarted._approval_tokens, {})

        rehydrated = orch_restarted.get_execution("exec-t02")
        self.assertEqual(rehydrated.state, ChainState.COMPLETED)
        self.assertEqual(len(rehydrated.applied_backups), 2)
        self.assertEqual(orch_restarted._approval_tokens, {})

        rb_res = orch_restarted.rollback_execution("exec-t02")
        self.assertEqual(rb_res["state"], ChainState.ROLLED_BACK.value)
        self.assertEqual(compute_sha256_file(cfg), h0)
        self.assertEqual(cfg.read_text(encoding="utf-8"), original_content)

        tx_after = tx_mgr_restarted.get_latest_transaction_for_execution("exec-t02")
        assert tx_after is not None
        self.assertEqual(tx_after.state, TransactionState.ROLLED_BACK.value)

    # ==================================================================
    # Grupo 2 — Interrupciones durante ejecución
    # ==================================================================

    def test_T03_interruption_in_prepared_before_touching_files(self) -> None:
        """T03: Interrupción en PREPARED antes de tocar archivos; recovery cierra en ROLLED_BACK."""
        cfg = self.workspace_dir / "t03.ini"
        cfg.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\n", encoding="utf-8")
        h0 = compute_sha256_file(cfg)

        op1 = self._make_ini_op("op-1", cfg, "sg.ShadowQuality", "3", "1")
        plan, app_hash, token = self._prepare_and_authorize_plan("exec-t03", [op1])

        def crash_at_prepared(hook: str, _tx_id: str, _op_id: Optional[str]) -> None:
            if hook == "after_snapshot_write:STATE_PREPARED":
                raise SimulatedTransactionInterrupt("Caída simulada justo en PREPARED")

        self.tx_manager.journal.fault_injector = crash_at_prepared
        with self.assertRaises(SimulatedTransactionInterrupt):
            self.orchestrator.execute_approved_plan(
                plan,
                approval_hash=app_hash,
                authorization_token=token,
            )
        self.tx_manager.journal.fault_injector = None

        self.assertEqual(compute_sha256_file(cfg), h0)
        self.assertEqual(list(self.backup_dir.glob("*.bak")), [])

        reports = self.orchestrator.recover_pending_transactions()
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].initial_state, TransactionState.PREPARED.value)
        self.assertEqual(reports[0].final_state, TransactionState.ROLLED_BACK.value)
        self.assertEqual(compute_sha256_file(cfg), h0)

        tx = self.tx_manager.get_latest_transaction_for_execution("exec-t03")
        assert tx is not None
        self.assertIsNotNone(tx.pre_recovery_metadata)
        assert tx.pre_recovery_metadata is not None
        self.assertEqual(tx.pre_recovery_metadata["previous_state"], TransactionState.PREPARED.value)

    def test_T04_interruption_after_backup_verified_before_os_replace(self) -> None:
        """T04: Interrupción tras crear y verificar el backup pero antes de os.replace(); archivo sigue en H0."""
        cfg = self.workspace_dir / "t04.ini"
        cfg.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\n", encoding="utf-8")
        h0 = compute_sha256_file(cfg)

        op1 = self._make_ini_op("op-1", cfg, "sg.ShadowQuality", "3", "1")
        plan, app_hash, token = self._prepare_and_authorize_plan("exec-t04", [op1])

        def crash_after_backup(hook: str, _tx_id: str, _op_id: Optional[str]) -> None:
            if hook == "after_snapshot_write:OPERATION_BACKUP_VERIFIED":
                raise SimulatedTransactionInterrupt("Caída simulada tras verificar backup antes de os.replace")

        self.tx_manager.journal.fault_injector = crash_after_backup
        with self.assertRaises(SimulatedTransactionInterrupt):
            self.orchestrator.execute_approved_plan(
                plan,
                approval_hash=app_hash,
                authorization_token=token,
            )
        self.tx_manager.journal.fault_injector = None

        # El backup existe pero el archivo original aún no fue modificado
        self.assertEqual(len(list(self.backup_dir.glob("*.bak"))), 1)
        self.assertEqual(compute_sha256_file(cfg), h0)

        reports = self.orchestrator.recover_pending_transactions()
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].final_state, TransactionState.ROLLED_BACK.value)
        self.assertEqual(compute_sha256_file(cfg), h0)

    def test_T05_interruption_after_os_replace_before_applied_logged(self) -> None:
        """T05: Interrupción después de os.replace() de op-1 y antes de registrar APPLIED (con op-2 pendiente)."""
        cfg = self.workspace_dir / "t05.ini"
        original_text = "[ScalabilityGroups]\nsg.ShadowQuality=3\nsg.TextureQuality=3\n"
        cfg.write_text(original_text, encoding="utf-8")
        h0 = compute_sha256_file(cfg)

        op1 = self._make_ini_op("op-1", cfg, "sg.ShadowQuality", "3", "1")
        op2 = self._make_ini_op("op-2", cfg, "sg.TextureQuality", "3", "1", depends_on=["op-1"])
        plan, app_hash, token = self._prepare_and_authorize_plan("exec-t05", [op1, op2])

        def crash_after_replace(hook: str, _tx_id: str, op_id: Optional[str]) -> None:
            if hook == "after_os_replace_before_applied" and op_id == "op-1":
                raise SimulatedTransactionInterrupt("Caída después de os.replace() y antes de APPLIED")

        self.tx_manager.journal.fault_injector = crash_after_replace
        with self.assertRaises(SimulatedTransactionInterrupt):
            self.orchestrator.execute_approved_plan(
                plan,
                approval_hash=app_hash,
                authorization_token=token,
            )
        self.tx_manager.journal.fault_injector = None

        # El archivo en disco ya cambió a H1 aunque op-1 aún figure en BACKUP_VERIFIED en el journal
        self.assertNotEqual(compute_sha256_file(cfg), h0)
        tx_mid = self.tx_manager.get_latest_transaction_for_execution("exec-t05")
        assert tx_mid is not None
        self.assertEqual(tx_mid.operations["op-1"].op_stage, OperationJournalStage.BACKUP_VERIFIED.value)
        self.assertEqual(tx_mid.operations["op-2"].op_stage, OperationJournalStage.PENDING.value)

        # Recovery detecta que el archivo tiene planned_after_sha256 de op-1 y que op-2 está PENDING:
        # revierte op-1 desde su backup sin ejecutar op-2.
        reports = self.orchestrator.recover_pending_transactions()
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].final_state, TransactionState.ROLLED_BACK.value)
        self.assertIn("op-1", reports[0].rolled_back_operations)
        self.assertEqual(compute_sha256_file(cfg), h0)
        self.assertEqual(cfg.read_text(encoding="utf-8"), original_text)

    def test_T06_interruption_mid_multi_operation_plan_never_executes_pending(self) -> None:
        """T06: Op-1 aplicada, Op-2 modificada en curso, Op-3 pendiente -> revierte en LIFO y jamás ejecuta Op-3."""
        f1 = self.workspace_dir / "t06_a.ini"
        f2 = self.workspace_dir / "t06_b.ini"
        f3 = self.workspace_dir / "t06_c.ini"
        f1.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\n", encoding="utf-8")
        f2.write_text("[ScalabilityGroups]\nsg.TextureQuality=3\n", encoding="utf-8")
        f3.write_text("[ScalabilityGroups]\nsg.EffectsQuality=3\n", encoding="utf-8")
        h0_1, h0_2, h0_3 = compute_sha256_file(f1), compute_sha256_file(f2), compute_sha256_file(f3)

        op1 = self._make_ini_op("op-1", f1, "sg.ShadowQuality", "3", "1")
        op2 = self._make_ini_op("op-2", f2, "sg.TextureQuality", "3", "1", depends_on=["op-1"])
        op3 = self._make_ini_op("op-3", f3, "sg.EffectsQuality", "3", "1", depends_on=["op-2"])
        plan, app_hash, token = self._prepare_and_authorize_plan("exec-t06", [op1, op2, op3])

        def crash_on_op2(hook: str, _tx_id: str, op_id: Optional[str]) -> None:
            if hook == "after_os_replace_before_applied" and op_id == "op-2":
                raise SimulatedTransactionInterrupt("Caída en op-2 tras os.replace")

        self.tx_manager.journal.fault_injector = crash_on_op2
        with self.assertRaises(SimulatedTransactionInterrupt):
            self.orchestrator.execute_approved_plan(
                plan,
                approval_hash=app_hash,
                authorization_token=token,
            )
        self.tx_manager.journal.fault_injector = None

        self.assertNotEqual(compute_sha256_file(f1), h0_1)
        self.assertNotEqual(compute_sha256_file(f2), h0_2)
        self.assertEqual(compute_sha256_file(f3), h0_3)

        reports = self.orchestrator.recover_pending_transactions()
        self.assertEqual(len(reports), 1)
        rep = reports[0]
        self.assertEqual(rep.final_state, TransactionState.ROLLED_BACK.value)
        # Reversión estrictamente en orden inverso LIFO: op-2 primero, luego op-1; op-3 jamás tocada
        self.assertEqual(rep.rolled_back_operations, ["op-2", "op-1"])
        self.assertEqual(compute_sha256_file(f1), h0_1)
        self.assertEqual(compute_sha256_file(f2), h0_2)
        self.assertEqual(compute_sha256_file(f3), h0_3)

    # ==================================================================
    # Grupo 3 — Múltiples operaciones sobre el mismo archivo
    # ==================================================================

    def test_T07_three_successive_operations_on_same_file_hash_chain_and_lifo_rollback(self) -> None:
        """T07: Verifica la cadena H0 -> H1 -> H2 -> H3 sobre un mismo archivo y su reversión H3 -> H2 -> H1 -> H0."""
        cfg = self.workspace_dir / "t07_chain.ini"
        initial_text = (
            "[ScalabilityGroups]\n"
            "sg.ShadowQuality=3\n"
            "sg.TextureQuality=3\n"
            "sg.EffectsQuality=3\n"
        )
        cfg.write_text(initial_text, encoding="utf-8")
        h0 = compute_sha256_file(cfg)

        # Subcaso A: Interrupción después de H2 (con op-3 pendiente) -> revierte H2 -> H1 -> H0
        op_a1 = self._make_ini_op("op-a1", cfg, "sg.ShadowQuality", "3", "1")
        op_a2 = self._make_ini_op("op-a2", cfg, "sg.TextureQuality", "3", "1", depends_on=["op-a1"])
        op_a3 = self._make_ini_op("op-a3", cfg, "sg.EffectsQuality", "3", "1", depends_on=["op-a2"])
        plan_a, hash_a, tok_a = self._prepare_and_authorize_plan("exec-t07-partial", [op_a1, op_a2, op_a3])

        def crash_after_op_a2(hook: str, _tx_id: str, op_id: Optional[str]) -> None:
            if hook == "after_snapshot_write:OPERATION_APPLIED" and op_id == "op-a2":
                raise SimulatedTransactionInterrupt("Caída después de aplicar op-a2 (H2)")

        self.tx_manager.journal.fault_injector = crash_after_op_a2
        with self.assertRaises(SimulatedTransactionInterrupt):
            self.orchestrator.execute_approved_plan(
                plan_a,
                approval_hash=hash_a,
                authorization_token=tok_a,
            )
        self.tx_manager.journal.fault_injector = None

        h2_on_disk = compute_sha256_file(cfg)
        self.assertNotEqual(h2_on_disk, h0)

        rep_a = self.orchestrator.recover_pending_transactions()[0]
        self.assertEqual(rep_a.final_state, TransactionState.ROLLED_BACK.value)
        self.assertEqual(rep_a.rolled_back_operations, ["op-a2", "op-a1"])
        self.assertEqual(compute_sha256_file(cfg), h0)

        # Subcaso B: Ejecución completa H0 -> H1 -> H2 -> H3, verificación de cadena y rollback H3 -> H2 -> H1 -> H0
        op_b1 = self._make_ini_op("op-b1", cfg, "sg.ShadowQuality", "3", "1")
        op_b2 = self._make_ini_op("op-b2", cfg, "sg.TextureQuality", "3", "1", depends_on=["op-b1"])
        op_b3 = self._make_ini_op("op-b3", cfg, "sg.EffectsQuality", "3", "1", depends_on=["op-b2"])
        plan_b, hash_b, tok_b = self._prepare_and_authorize_plan("exec-t07-full", [op_b1, op_b2, op_b3])

        res_b = self.orchestrator.execute_approved_plan(
            plan_b,
            approval_hash=hash_b,
            authorization_token=tok_b,
        )
        self.assertEqual(res_b["state"], ChainState.COMPLETED.value)
        h3 = compute_sha256_file(cfg)

        tx_b = self.tx_manager.get_latest_transaction_for_execution("exec-t07-full")
        assert tx_b is not None
        chain = tx_b.file_hash_chains[str(cfg.resolve())]
        self.assertEqual(len(chain), 3)

        h1 = chain[0]["actual_after_sha256"]
        h2 = chain[1]["actual_after_sha256"]
        self.assertEqual(chain[0]["expected_before_sha256"], h0)
        self.assertEqual(chain[1]["expected_before_sha256"], h1)
        self.assertEqual(chain[2]["expected_before_sha256"], h2)
        self.assertEqual(chain[2]["actual_after_sha256"], h3)
        self.assertEqual(len({h0, h1, h2, h3}), 4)

        # Rollback completo LIFO: H3 -> H2 -> H1 -> H0
        rb_b = self.orchestrator.rollback_execution("exec-t07-full")
        self.assertEqual(rb_b["state"], ChainState.ROLLED_BACK.value)
        self.assertEqual(compute_sha256_file(cfg), h0)
        self.assertEqual(cfg.read_text(encoding="utf-8"), initial_text)

    # ==================================================================
    # Grupo 4 — Interrupción durante rollback
    # ==================================================================

    def test_T08_interruption_during_rollback_resumes_from_disk_idempotently(self) -> None:
        """T08: Interrupción a mitad de un rollback LIFO; recovery reanuda desde disco sin repetir lo ya restaurado."""
        cfg = self.workspace_dir / "t08.ini"
        initial_text = (
            "[ScalabilityGroups]\n"
            "sg.ShadowQuality=3\n"
            "sg.TextureQuality=3\n"
            "sg.EffectsQuality=3\n"
        )
        cfg.write_text(initial_text, encoding="utf-8")
        h0 = compute_sha256_file(cfg)

        op1 = self._make_ini_op("op-1", cfg, "sg.ShadowQuality", "3", "1")
        op2 = self._make_ini_op("op-2", cfg, "sg.TextureQuality", "3", "1", depends_on=["op-1"])
        op3 = self._make_ini_op("op-3", cfg, "sg.EffectsQuality", "3", "1", depends_on=["op-2"])
        plan, app_hash, token = self._prepare_and_authorize_plan("exec-t08", [op1, op2, op3])

        self.orchestrator.execute_approved_plan(
            plan,
            approval_hash=app_hash,
            authorization_token=token,
        )
        tx_before_rb = self.tx_manager.get_latest_transaction_for_execution("exec-t08")
        assert tx_before_rb is not None
        h2 = tx_before_rb.operations["op-2"].actual_after_sha256

        # Simulamos interrupción durante rollback manual justo después de revertir op-3 (de H3 a H2)
        def crash_after_first_rb_step(hook: str, _tx_id: str, op_id: Optional[str]) -> None:
            if hook == "after_manual_rollback_step" and op_id == "op-3":
                raise SimulatedTransactionInterrupt("Caída a mitad de rollback tras revertir op-3")

        self.tx_manager.journal.fault_injector = crash_after_first_rb_step
        with self.assertRaises(SimulatedTransactionInterrupt):
            self.orchestrator.rollback_execution("exec-t08")
        self.tx_manager.journal.fault_injector = None

        # El archivo físico quedó en H2 (op-3 revertida, op-2 y op-1 aún pendientes de revertir)
        self.assertEqual(compute_sha256_file(cfg), h2)

        # Creamos una nueva instancia desde cero (sin estado en memoria) y ejecutamos recovery
        editor_2 = FileEditorSkill(
            authorized_roots=[self.workspace_dir, self.backup_dir],
            backup_dir=self.backup_dir,
            audit_logger=AuditLogger(self.audit_log_path),
            sleep_fn=lambda _s: None,
        )
        tx_mgr_2 = TransactionManager(editor_2, journal_dir=self.journal_dir)
        orch_2 = ChainOrchestrator(file_editor=editor_2, transaction_manager=tx_mgr_2)

        reports = orch_2.recover_pending_transactions()
        self.assertEqual(len(reports), 1)
        rep = reports[0]
        self.assertEqual(rep.initial_state, TransactionState.ROLLING_BACK.value)
        self.assertEqual(rep.final_state, TransactionState.ROLLED_BACK.value)
        # Solo restaura las operaciones restantes (op-2 y op-1); op-3 ya estaba en ROLLED_BACK
        self.assertEqual(rep.rolled_back_operations, ["op-2", "op-1"])
        self.assertEqual(compute_sha256_file(cfg), h0)
        self.assertEqual(cfg.read_text(encoding="utf-8"), initial_text)

    # ==================================================================
    # Grupo 5 — Concurrencia y lock global
    # ==================================================================

    def test_T09_global_lock_prevents_simultaneous_writers_interthread_and_interprocess(self) -> None:
        """T09: Dos escritores simultáneos (entre hilos y mediante descriptor OS independiente) no pueden pisarse."""
        cfg_a = self.workspace_dir / "t09_a.ini"
        cfg_b = self.workspace_dir / "t09_b.ini"
        cfg_a.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\n", encoding="utf-8")
        cfg_b.write_text("[ScalabilityGroups]\nsg.TextureQuality=3\n", encoding="utf-8")

        op_a = self._make_ini_op("op-a", cfg_a, "sg.ShadowQuality", "3", "1")
        op_b = self._make_ini_op("op-b", cfg_b, "sg.TextureQuality", "3", "1")
        plan_a, hash_a, tok_a = self._prepare_and_authorize_plan("exec-t09-a", [op_a])
        plan_b, hash_b, tok_b = self._prepare_and_authorize_plan("exec-t09-b", [op_b])

        # 1. Prueba de contención real con segundo descriptor de archivo del SO (msvcrt.locking)
        lock_file = self.tx_manager.global_lock.lock_file
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        external_fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.lseek(external_fd, 0, os.SEEK_END) == 0:
                os.write(external_fd, b"L")
            os.lseek(external_fd, 0, os.SEEK_SET)
            msvcrt.locking(external_fd, msvcrt.LK_NBLCK, 1)

            self.tx_manager.global_lock.default_timeout_sec = 0.12
            with self.assertRaises(TransactionLockTimeoutError):
                self.orchestrator.execute_approved_plan(
                    plan_a,
                    approval_hash=hash_a,
                    authorization_token=tok_a,
                )
        finally:
            os.lseek(external_fd, 0, os.SEEK_SET)
            msvcrt.locking(external_fd, msvcrt.LK_UNLCK, 1)
            os.close(external_fd)
            self.tx_manager.global_lock.default_timeout_sec = 1.0

        # 2. Prueba entre dos hilos donde el primero retiene el lock y el segundo expira controladamente
        plan_a2, hash_a2, tok_a2 = self._prepare_and_authorize_plan("exec-t09-a2", [op_a])
        entered_event = threading.Event()
        release_event = threading.Event()

        def hold_during_tx(hook: str, _tx_id: str, _op_id: Optional[str]) -> None:
            if hook == "after_snapshot_write:STATE_PREPARED":
                entered_event.set()
                release_event.wait(timeout=2.0)

        self.tx_manager.journal.fault_injector = hold_during_tx
        self.tx_manager.global_lock.default_timeout_sec = 0.15

        t1_err: list[BaseException] = []

        def worker_1() -> None:
            try:
                self.orchestrator.execute_approved_plan(
                    plan_a2,
                    approval_hash=hash_a2,
                    authorization_token=tok_a2,
                )
            except BaseException as exc:
                t1_err.append(exc)

        th1 = threading.Thread(target=worker_1)
        th1.start()
        self.assertTrue(entered_event.wait(timeout=2.0))

        # Mientras th1 está dentro de la transacción, el intento de th2 falla con TransactionLockTimeoutError
        with self.assertRaises(TransactionLockTimeoutError):
            self.orchestrator.execute_approved_plan(
                plan_b,
                approval_hash=hash_b,
                authorization_token=tok_b,
            )

        release_event.set()
        th1.join(timeout=2.0)
        self.tx_manager.journal.fault_injector = None
        self.tx_manager.global_lock.default_timeout_sec = 1.0
        self.assertEqual(t1_err, [])
        self.assertIn("sg.ShadowQuality=1", cfg_a.read_text(encoding="utf-8"))
        self.assertIn("sg.TextureQuality=3", cfg_b.read_text(encoding="utf-8"))

    def test_T10_new_execution_blocked_while_recovery_in_progress(self) -> None:
        """T10: Una nueva ejecución no puede iniciar ni interferir mientras una recuperación está en curso."""
        cfg_1 = self.workspace_dir / "t10_1.ini"
        cfg_2 = self.workspace_dir / "t10_2.ini"
        cfg_1.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\n", encoding="utf-8")
        cfg_2.write_text("[ScalabilityGroups]\nsg.TextureQuality=3\n", encoding="utf-8")

        op1 = self._make_ini_op("op-1", cfg_1, "sg.ShadowQuality", "3", "1")
        op2 = self._make_ini_op("op-2", cfg_2, "sg.TextureQuality", "3", "1")
        plan1, hash1, tok1 = self._prepare_and_authorize_plan("exec-t10-1", [op1])
        plan2, hash2, tok2 = self._prepare_and_authorize_plan("exec-t10-2", [op2])

        # Interrumpimos exec-t10-1 tras verificar el backup (antes de os.replace)
        def crash_1(hook: str, _tx_id: str, _op_id: Optional[str]) -> None:
            if hook == "after_snapshot_write:OPERATION_BACKUP_VERIFIED":
                raise SimulatedTransactionInterrupt("Caída para probar T10")

        self.tx_manager.journal.fault_injector = crash_1
        with self.assertRaises(SimulatedTransactionInterrupt):
            self.orchestrator.execute_approved_plan(plan1, approval_hash=hash1, authorization_token=tok1)
        self.tx_manager.journal.fault_injector = None

        rec_entered = threading.Event()
        rec_release = threading.Event()

        def pause_inside_recovery(hook: str, _tx_id: str, _op_id: Optional[str]) -> None:
            if hook == "after_snapshot_write:STATE_RECOVERING":
                rec_entered.set()
                rec_release.wait(timeout=2.0)

        self.tx_manager.journal.fault_injector = pause_inside_recovery
        self.tx_manager.global_lock.default_timeout_sec = 0.15

        rec_reports: list[Any] = []

        def run_recovery() -> None:
            rec_reports.extend(self.orchestrator.recover_pending_transactions())

        th_rec = threading.Thread(target=run_recovery)
        th_rec.start()
        self.assertTrue(rec_entered.wait(timeout=2.0))

        # Intentar ejecutar plan2 mientras la recuperación posee el lock global debe bloquearse por timeout
        with self.assertRaises(TransactionLockTimeoutError):
            self.orchestrator.execute_approved_plan(plan2, approval_hash=hash2, authorization_token=tok2)

        rec_release.set()
        th_rec.join(timeout=2.0)
        self.tx_manager.journal.fault_injector = None
        self.tx_manager.global_lock.default_timeout_sec = 1.0
        self.assertEqual(len(rec_reports), 1)
        self.assertEqual(rec_reports[0].final_state, TransactionState.ROLLED_BACK.value)

    # ==================================================================
    # Grupo 6 — Integridad del WAL y snapshot (T11 a T17)
    # ==================================================================

    def _create_interrupted_tx_for_wal_tampering(self, exec_id: str) -> tuple[Path, str, str]:
        cfg = self.workspace_dir / f"{exec_id}.ini"
        cfg.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\n", encoding="utf-8")
        h0 = compute_sha256_file(cfg)
        op = self._make_ini_op("op-1", cfg, "sg.ShadowQuality", "3", "1")
        plan, app_hash, token = self._prepare_and_authorize_plan(exec_id, [op])

        def crash_at_intent(hook: str, _tx_id: str, _op_id: Optional[str]) -> None:
            if hook == "after_snapshot_write:OPERATION_INTENT_LOGGED":
                raise SimulatedTransactionInterrupt("Interrupción en INTENT_LOGGED")

        self.tx_manager.journal.fault_injector = crash_at_intent
        with self.assertRaises(SimulatedTransactionInterrupt):
            self.orchestrator.execute_approved_plan(plan, approval_hash=app_hash, authorization_token=token)
        self.tx_manager.journal.fault_injector = None

        tx = self.tx_manager.get_latest_transaction_for_execution(exec_id)
        assert tx is not None
        return cfg, h0, tx.transaction_id

    def test_T11_duplicate_event_in_wal_detected_and_blocked(self) -> None:
        """T11: Evento duplicado en WAL es detectado (DUPLICATE_SEQ) y bloquea la recuperación."""
        cfg, h0, tx_id = self._create_interrupted_tx_for_wal_tampering("exec-t11")
        wal_path = self.tx_manager.journal.wal_path(tx_id)
        lines = wal_path.read_text(encoding="utf-8").splitlines()
        wal_path.write_text("\n".join(lines + [lines[-1]]) + "\n", encoding="utf-8")

        with self.assertRaises(JournalCorruptionError) as ctx:
            self.tx_manager.journal.validate_wal_file(tx_id)
        self.assertEqual(ctx.exception.code, "DUPLICATE_SEQ")

        rep = self.tx_manager.recover_transaction(tx_id)
        self.assertEqual(rep.final_state, TransactionState.RECOVERY_FAILED.value)
        self.assertEqual(rep.decision, "BLOCKED_CORRUPT_JOURNAL")
        self.assertEqual(compute_sha256_file(cfg), h0)

    def test_T12_missing_event_in_wal_detected_and_blocked(self) -> None:
        """T12: Evento faltante en WAL (salto de seq) es detectado (MISSING_SEQ) y bloquea."""
        cfg, h0, tx_id = self._create_interrupted_tx_for_wal_tampering("exec-t12")
        wal_path = self.tx_manager.journal.wal_path(tx_id)
        lines = wal_path.read_text(encoding="utf-8").splitlines()
        # Eliminamos el segundo evento (seq=2)
        del lines[1]
        wal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with self.assertRaises(JournalCorruptionError) as ctx:
            self.tx_manager.journal.validate_wal_file(tx_id)
        self.assertEqual(ctx.exception.code, "MISSING_SEQ")

        rep = self.tx_manager.recover_transaction(tx_id)
        self.assertEqual(rep.final_state, TransactionState.RECOVERY_FAILED.value)
        self.assertEqual(compute_sha256_file(cfg), h0)

    def test_T13_altered_event_in_wal_detected_and_blocked(self) -> None:
        """T13: Alterar un evento en el WAL invalida event_sha256 y bloquea."""
        cfg, h0, tx_id = self._create_interrupted_tx_for_wal_tampering("exec-t13")
        wal_path = self.tx_manager.journal.wal_path(tx_id)
        lines = wal_path.read_text(encoding="utf-8").splitlines()
        ev_dict = json.loads(lines[2])
        ev_dict["payload"]["reason"] = "Payload manipulado maliciosamente"
        lines[2] = json.dumps(ev_dict, ensure_ascii=False, sort_keys=True)
        wal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with self.assertRaises(JournalCorruptionError) as ctx:
            self.tx_manager.journal.validate_wal_file(tx_id)
        self.assertEqual(ctx.exception.code, "WAL_EVENT_HASH_MISMATCH")

        rep = self.tx_manager.recover_transaction(tx_id)
        self.assertEqual(rep.final_state, TransactionState.RECOVERY_FAILED.value)
        self.assertEqual(compute_sha256_file(cfg), h0)

    def test_T14_out_of_order_events_in_wal_detected_and_blocked(self) -> None:
        """T14: Eventos desordenados en el WAL son rechazados con OUT_OF_ORDER_SEQ."""
        cfg, h0, tx_id = self._create_interrupted_tx_for_wal_tampering("exec-t14")
        wal_path = self.tx_manager.journal.wal_path(tx_id)
        lines = wal_path.read_text(encoding="utf-8").splitlines()
        lines[1], lines[2] = lines[2], lines[1]
        wal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with self.assertRaises(JournalCorruptionError) as ctx:
            self.tx_manager.journal.validate_wal_file(tx_id)
        self.assertEqual(ctx.exception.code, "OUT_OF_ORDER_SEQ")

        rep = self.tx_manager.recover_transaction(tx_id)
        self.assertEqual(rep.final_state, TransactionState.RECOVERY_FAILED.value)
        self.assertEqual(compute_sha256_file(cfg), h0)

    def test_T15_truncated_final_line_vs_interior_corruption_in_wal(self) -> None:
        """T15: Distingue última línea truncada (TRUNCATED_FINAL_WAL_LINE) de corrupción interior y preserva archivos."""
        cfg, h0, tx_id = self._create_interrupted_tx_for_wal_tampering("exec-t15")
        wal_path = self.tx_manager.journal.wal_path(tx_id)
        original_wal = wal_path.read_text(encoding="utf-8")

        # 1. Última línea truncada por interrupción a mitad de escritura
        wal_path.write_text(original_wal + '{"seq": 99, "timestamp": "2026-11-03', encoding="utf-8")
        with self.assertRaises(JournalCorruptionError) as ctx_trunc:
            self.tx_manager.journal.validate_wal_file(tx_id)
        self.assertEqual(ctx_trunc.exception.code, "TRUNCATED_FINAL_WAL_LINE")

        rep = self.tx_manager.recover_transaction(tx_id)
        self.assertEqual(rep.final_state, TransactionState.RECOVERY_FAILED.value)
        # El archivo WAL corrupto no se repara ni borra silenciosamente
        self.assertTrue(wal_path.exists())
        self.assertIn('{"seq": 99', wal_path.read_text(encoding="utf-8"))
        self.assertEqual(compute_sha256_file(cfg), h0)

        # 2. Corrupción en línea interior
        lines = original_wal.splitlines()
        lines[1] = "{corrupt_interior_json"
        wal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(JournalCorruptionError) as ctx_int:
            self.tx_manager.journal.validate_wal_file(tx_id)
        self.assertEqual(ctx_int.exception.code, "INTERIOR_WAL_CORRUPTION")

    def test_T16_foreign_transaction_event_in_wal_detected_and_blocked(self) -> None:
        """T16: Evento de otra transacción mezclado en el WAL se detecta con FOREIGN_TRANSACTION_ID."""
        cfg, h0, tx_id = self._create_interrupted_tx_for_wal_tampering("exec-t16")
        wal_path = self.tx_manager.journal.wal_path(tx_id)
        events = self.tx_manager.journal.validate_wal_file(tx_id)

        foreign_ev = WalEvent(
            seq=events[-1].seq + 1,
            timestamp="2026-11-03T12:00:00+00:00",
            transaction_id="tx_foreign_999999",
            event_type="STATE_EXECUTING",
            operation_id=None,
            payload={"state": "EXECUTING"},
            previous_event_sha256=events[-1].event_sha256,
        )
        foreign_ev.event_sha256 = foreign_ev.compute_hash()
        with open(wal_path, "a", encoding="utf-8") as wf:
            wf.write(json.dumps(foreign_ev.to_dict(), sort_keys=True) + "\n")

        with self.assertRaises(JournalCorruptionError) as ctx:
            self.tx_manager.journal.validate_wal_file(tx_id)
        self.assertEqual(ctx.exception.code, "FOREIGN_TRANSACTION_ID")

        rep = self.tx_manager.recover_transaction(tx_id)
        self.assertEqual(rep.final_state, TransactionState.RECOVERY_FAILED.value)
        self.assertEqual(compute_sha256_file(cfg), h0)

    def test_T17_snapshot_incompatible_with_wal_detected_and_blocked(self) -> None:
        """T17: Un snapshot con last_seq o last_event_sha256 incompatible con el WAL se bloquea."""
        cfg, h0, tx_id = self._create_interrupted_tx_for_wal_tampering("exec-t17")
        snap_path = self.tx_manager.journal.snapshot_path(tx_id)
        snap_data = json.loads(snap_path.read_text(encoding="utf-8"))

        # Alteramos last_event_sha256 y recalculamos record_sha256 para simular desincronización snapshot vs WAL
        snap_data["last_event_sha256"] = "f" * 64
        body_no_hash = {k: v for k, v in snap_data.items() if k != "record_sha256"}
        snap_data["record_sha256"] = hashlib.sha256(
            json.dumps(body_no_hash, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        snap_path.write_text(json.dumps(snap_data, indent=2), encoding="utf-8")

        with self.assertRaises(JournalCorruptionError) as ctx:
            self.tx_manager.journal.validate_and_load_transaction(tx_id)
        self.assertEqual(ctx.exception.code, "SNAPSHOT_INCOMPATIBLE")

        rep = self.tx_manager.recover_transaction(tx_id)
        self.assertEqual(rep.final_state, TransactionState.RECOVERY_FAILED.value)
        self.assertEqual(compute_sha256_file(cfg), h0)

    # ==================================================================
    # Grupo 7 — Backups y conflictos externos (T18 a T20)
    # ==================================================================

    def test_T18_missing_backup_during_recovery_transitions_to_recovery_failed(self) -> None:
        """T18: Si falta el archivo de backup durante recovery, pasa a RECOVERY_FAILED sin destruir el archivo."""
        cfg = self.workspace_dir / "t18.ini"
        cfg.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\nsg.TextureQuality=3\n", encoding="utf-8")
        op1 = self._make_ini_op("op-1", cfg, "sg.ShadowQuality", "3", "1")
        op2 = self._make_ini_op("op-2", cfg, "sg.TextureQuality", "3", "1", depends_on=["op-1"])
        plan, app_hash, token = self._prepare_and_authorize_plan("exec-t18", [op1, op2])

        def crash_after_op1(hook: str, _tx_id: str, op_id: Optional[str]) -> None:
            if hook == "after_snapshot_write:OPERATION_APPLIED" and op_id == "op-1":
                raise SimulatedTransactionInterrupt("Caída tras aplicar op-1")

        self.tx_manager.journal.fault_injector = crash_after_op1
        with self.assertRaises(SimulatedTransactionInterrupt):
            self.orchestrator.execute_approved_plan(plan, approval_hash=app_hash, authorization_token=token)
        self.tx_manager.journal.fault_injector = None

        h1 = compute_sha256_file(cfg)
        for bak in self.backup_dir.glob("*.bak"):
            bak.unlink()

        reports = self.orchestrator.recover_pending_transactions()
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].final_state, TransactionState.RECOVERY_FAILED.value)
        # El archivo objetivo no fue destruido
        self.assertEqual(compute_sha256_file(cfg), h1)

    def test_T19_corrupt_backup_during_recovery_blocked_by_sha256(self) -> None:
        """T19: Si el backup fue corrompido en disco, recovery detecta el SHA-256 inválido y pasa a RECOVERY_FAILED."""
        cfg = self.workspace_dir / "t19.ini"
        cfg.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\nsg.TextureQuality=3\n", encoding="utf-8")
        op1 = self._make_ini_op("op-1", cfg, "sg.ShadowQuality", "3", "1")
        op2 = self._make_ini_op("op-2", cfg, "sg.TextureQuality", "3", "1", depends_on=["op-1"])
        plan, app_hash, token = self._prepare_and_authorize_plan("exec-t19", [op1, op2])

        def crash_after_op1(hook: str, _tx_id: str, op_id: Optional[str]) -> None:
            if hook == "after_snapshot_write:OPERATION_APPLIED" and op_id == "op-1":
                raise SimulatedTransactionInterrupt("Caída tras aplicar op-1")

        self.tx_manager.journal.fault_injector = crash_after_op1
        with self.assertRaises(SimulatedTransactionInterrupt):
            self.orchestrator.execute_approved_plan(plan, approval_hash=app_hash, authorization_token=token)
        self.tx_manager.journal.fault_injector = None

        h1 = compute_sha256_file(cfg)
        for bak in self.backup_dir.glob("*.bak"):
            bak.write_text("[CORRUPTED_BACKUP]\n", encoding="utf-8")

        reports = self.orchestrator.recover_pending_transactions()
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].final_state, TransactionState.RECOVERY_FAILED.value)
        # El archivo objetivo conserva H1 y jamás recibe el contenido corrupto del backup
        self.assertEqual(compute_sha256_file(cfg), h1)
        self.assertNotIn("CORRUPTED_BACKUP", cfg.read_text(encoding="utf-8"))

    def test_T20_externally_modified_file_before_recovery_is_protected(self) -> None:
        """T20: Si el usuario editó manualmente el archivo antes del recovery, no se sobrescribe y queda en RECOVERY_FAILED."""
        cfg = self.workspace_dir / "t20.ini"
        cfg.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\nsg.TextureQuality=3\n", encoding="utf-8")
        op1 = self._make_ini_op("op-1", cfg, "sg.ShadowQuality", "3", "1")
        op2 = self._make_ini_op("op-2", cfg, "sg.TextureQuality", "3", "1", depends_on=["op-1"])
        plan, app_hash, token = self._prepare_and_authorize_plan("exec-t20", [op1, op2])

        def crash_after_op1(hook: str, _tx_id: str, op_id: Optional[str]) -> None:
            if hook == "after_snapshot_write:OPERATION_APPLIED" and op_id == "op-1":
                raise SimulatedTransactionInterrupt("Caída tras aplicar op-1")

        self.tx_manager.journal.fault_injector = crash_after_op1
        with self.assertRaises(SimulatedTransactionInterrupt):
            self.orchestrator.execute_approved_plan(plan, approval_hash=app_hash, authorization_token=token)
        self.tx_manager.journal.fault_injector = None

        # El usuario edita manualmente el archivo antes de que corra la recuperación
        external_text = "[ScalabilityGroups]\nsg.ShadowQuality=0\nsg.TextureQuality=3\nUserManualKey=777\n"
        cfg.write_text(external_text, encoding="utf-8")
        external_sha = compute_sha256_file(cfg)

        reports = self.orchestrator.recover_pending_transactions()
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].final_state, TransactionState.RECOVERY_FAILED.value)
        self.assertEqual(compute_sha256_file(cfg), external_sha)
        self.assertIn("UserManualKey=777", cfg.read_text(encoding="utf-8"))

    # ==================================================================
    # Grupo 8 — Idempotencia y casos borde (T21 a T23)
    # ==================================================================

    def test_T21_running_recovery_twice_is_safe_idempotent_noop(self) -> None:
        """T21: Ejecutar recover_transaction dos veces seguidas es idempotente y no repite escrituras."""
        cfg = self.workspace_dir / "t21.ini"
        cfg.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\nsg.TextureQuality=3\n", encoding="utf-8")
        h0 = compute_sha256_file(cfg)

        op1 = self._make_ini_op("op-1", cfg, "sg.ShadowQuality", "3", "1")
        op2 = self._make_ini_op("op-2", cfg, "sg.TextureQuality", "3", "1", depends_on=["op-1"])
        plan, app_hash, token = self._prepare_and_authorize_plan("exec-t21", [op1, op2])

        def crash_after_op1(hook: str, _tx_id: str, op_id: Optional[str]) -> None:
            if hook == "after_snapshot_write:OPERATION_APPLIED" and op_id == "op-1":
                raise SimulatedTransactionInterrupt("Caída tras op-1")

        self.tx_manager.journal.fault_injector = crash_after_op1
        with self.assertRaises(SimulatedTransactionInterrupt):
            self.orchestrator.execute_approved_plan(plan, approval_hash=app_hash, authorization_token=token)
        self.tx_manager.journal.fault_injector = None

        tx = self.tx_manager.get_latest_transaction_for_execution("exec-t21")
        assert tx is not None

        rep1 = self.tx_manager.recover_transaction(tx.transaction_id)
        self.assertEqual(rep1.final_state, TransactionState.ROLLED_BACK.value)
        self.assertFalse(rep1.idempotent_repeat)
        self.assertEqual(compute_sha256_file(cfg), h0)

        events_after_first = len(self.tx_manager.journal.validate_wal_file(tx.transaction_id))

        rep2 = self.tx_manager.recover_transaction(tx.transaction_id)
        self.assertEqual(rep2.final_state, TransactionState.ROLLED_BACK.value)
        self.assertTrue(rep2.idempotent_repeat)
        self.assertEqual(rep2.decision, "NO_ACTION_NEEDED")
        self.assertEqual(compute_sha256_file(cfg), h0)
        self.assertEqual(len(self.tx_manager.journal.validate_wal_file(tx.transaction_id)), events_after_first)

    def test_T22_fully_applied_transaction_reconciled_to_committed_on_recovery(self) -> None:
        """T22: Si todas las operaciones seleccionadas ya se aplicaron en disco antes de caer, reconcilia a COMMITTED."""
        cfg = self.workspace_dir / "t22.ini"
        cfg.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\nsg.TextureQuality=3\n", encoding="utf-8")

        op1 = self._make_ini_op("op-1", cfg, "sg.ShadowQuality", "3", "1")
        op2 = self._make_ini_op("op-2", cfg, "sg.TextureQuality", "3", "1", depends_on=["op-1"])
        plan, app_hash, token = self._prepare_and_authorize_plan("exec-t22", [op1, op2])

        # Simulamos caída justo después de que os.replace() de la ÚLTIMA operación (op-2) escribió en disco,
        # pero antes de registrar APPLIED y COMMITTED.
        def crash_after_last_replace(hook: str, _tx_id: str, op_id: Optional[str]) -> None:
            if hook == "after_os_replace_before_applied" and op_id == "op-2":
                raise SimulatedTransactionInterrupt("Caída después del último os.replace antes de COMMITTED")

        self.tx_manager.journal.fault_injector = crash_after_last_replace
        with self.assertRaises(SimulatedTransactionInterrupt):
            self.orchestrator.execute_approved_plan(plan, approval_hash=app_hash, authorization_token=token)
        self.tx_manager.journal.fault_injector = None

        final_disk_sha = compute_sha256_file(cfg)
        self.assertIn("sg.ShadowQuality=1", cfg.read_text(encoding="utf-8"))
        self.assertIn("sg.TextureQuality=1", cfg.read_text(encoding="utf-8"))

        reports = self.orchestrator.recover_pending_transactions()
        self.assertEqual(len(reports), 1)
        rep = reports[0]
        self.assertEqual(rep.decision, "RECONCILED_COMMITTED")
        self.assertEqual(rep.final_state, TransactionState.COMMITTED.value)
        self.assertEqual(rep.reconciled_operations, ["op-2"])
        self.assertEqual(compute_sha256_file(cfg), final_disk_sha)

    def test_T23_orphan_backup_preserved_when_interrupted_before_journal_update(self) -> None:
        """T23: Si el proceso cae después de crear el backup y antes de actualizar el journal, conserva el backup huérfano."""
        cfg = self.workspace_dir / "t23.ini"
        cfg.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\n", encoding="utf-8")
        h0 = compute_sha256_file(cfg)

        op1 = self._make_ini_op("op-1", cfg, "sg.ShadowQuality", "3", "1")
        plan, app_hash, token = self._prepare_and_authorize_plan("exec-t23", [op1])

        # Fallamos justo cuando create_backup terminó pero antes de que el journal registre OPERATION_BACKUP_VERIFIED
        real_log_bkp = self.tx_manager.log_backup_verified

        def crash_before_logging_backup(*args: Any, **kwargs: Any) -> None:
            raise SimulatedTransactionInterrupt("Caída antes de registrar backup en el journal")

        self.tx_manager.log_backup_verified = crash_before_logging_backup  # type: ignore[method-assign]
        try:
            with self.assertRaises(SimulatedTransactionInterrupt):
                self.orchestrator.execute_approved_plan(plan, approval_hash=app_hash, authorization_token=token)
        finally:
            self.tx_manager.log_backup_verified = real_log_bkp  # type: ignore[method-assign]

        orphan_backups = list(self.backup_dir.glob("*.bak"))
        self.assertEqual(len(orphan_backups), 1)
        self.assertEqual(compute_sha256_file(orphan_backups[0]), h0)

        # Ejecutamos recovery y verificamos que el backup huérfano jamás es eliminado automáticamente
        reports = self.orchestrator.recover_pending_transactions()
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].final_state, TransactionState.ROLLED_BACK.value)
        self.assertTrue(orphan_backups[0].exists())
        self.assertEqual(compute_sha256_file(cfg), h0)

    # ==================================================================
    # Grupo 9 — Protección de memoria real y máquina de estados (T24)
    # ==================================================================

    def test_T24_memory_store_isolation_and_state_machine_guards(self) -> None:
        """T24: Verifica el aislamiento total de data/memory_store.json y el rechazo de transiciones ilegales."""
        # 1. Prohibición de apuntar el journal o el lock a data/memory_store.json o a data/transactions en modo test
        with self.assertRaises(PermissionError):
            resolve_safe_journal_dir(PROTECTED_MEMORY_PATH)
        with self.assertRaises(PermissionError):
            resolve_safe_journal_dir(BASE_DIR / "data" / "transactions")
        with self.assertRaises(PermissionError):
            GlobalTransactionLock(PROTECTED_MEMORY_PATH)

        # 2. Rechazo de transiciones ilegales en TransactionManager sin mutar parcialmente el registro
        cfg = self.workspace_dir / "t24.ini"
        cfg.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\n", encoding="utf-8")
        op1 = self._make_ini_op("op-1", cfg, "sg.ShadowQuality", "3", "1")
        tx = self.tx_manager.begin_transaction(
            execution_id="exec-t24",
            plan_id="plan-t24",
            approval_hash="a" * 64,
            approved_plan_snapshot={"plan_id": "plan-t24"},
            ordered_selected_ops=[op1],
            all_plan_ops=[op1],
            initial_file_hashes={},
        )
        self.assertEqual(tx.state, TransactionState.PREPARED.value)
        seq_before = tx.last_seq
        sha_before = tx.record_sha256

        # PREPARED -> COMMITTED directo es ilegal (debe pasar por EXECUTING)
        with self.assertRaises(InvalidTransactionStateError):
            self.tx_manager.transition_transaction_state(tx, TransactionState.COMMITTED)
        self.assertEqual(tx.state, TransactionState.PREPARED.value)
        self.assertEqual(tx.last_seq, seq_before)
        self.assertEqual(tx.record_sha256, sha_before)

        # Cerramos limpiamente la transacción de prueba
        self.tx_manager.transition_transaction_state(tx, TransactionState.ROLLED_BACK)
        self.assertEqual(tx.state, TransactionState.ROLLED_BACK.value)

        # ROLLED_BACK es terminal: no admite nuevas transiciones
        with self.assertRaises(InvalidTransactionStateError):
            self.tx_manager.transition_transaction_state(tx, TransactionState.EXECUTING)

        # 3. Comprobación explícita de integridad de data/memory_store.json
        current_meta = _snapshot_memory_metadata()
        self.assertEqual(self._mem_before_suite, current_meta)


if __name__ == "__main__":
    unittest.main()
