"""
Suite aislada de pruebas N8.3 Subfase 2: Contratos de evidencia verificada, adaptadores N5/N6,
coordinador `ExperienceCaptureService`, privacidad efectiva, fallos cerrados e invalidación por rollback.

Reglas de aislamiento:
- Corre estrictamente bajo `JARVIS_ENV=test` y `temp/jarvis_test_isolated/`.
- Utiliza `FileEditorSkill`, `ChainOrchestrator`, `GoalEngine`, `ExecutionSupervisor`, `GoalStore`,
  `ExperienceStore` y `MemoryEngine` reales sobre directorios aislados.
- Jamás escribe ni altera `data/memory_store.json`, `data/experiences.db`, `data/transactions/` ni `data/goals/`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from config import settings
from skills.base_skill import BaseSkill
from skills.file_editor_skill import AuditLogger, BackupManager, FileEditorSkill, compute_sha256_file
from src.brain.brain_manager import BrainManager
from src.brain.chain_orchestrator import (
    ChainOrchestrator,
    ChainState,
    OperationStatus,
)
from src.brain.goal_engine import GoalEngine
from src.brain.goal_models import (
    Constraints,
    Goal,
    GoalContext,
    GoalPlan,
    GoalStatus,
    GoalTask,
    GoalType,
    TaskRiskCategory,
    TaskStatus,
)
from src.brain.goal_persistence import GoalStore
from src.brain.memory import ConversationMemory
from src.brain.transaction_manager import TransactionState
from src.memory import (
    CAPTURE_CONTRACT_VERSION,
    CaptureBatchResult,
    CaptureUnitOutcome,
    ExperienceCaptureService,
    ExperienceStore,
    MemoryAuditAction,
    MemoryCategory,
    MemoryCreateInput,
    MemoryEngine,
    MemoryEvidenceContractError,
    MemoryPersistenceError,
    MemoryPolicy,
    MemoryPolicyError,
    MemoryScopeType,
    MemorySource,
    MemoryStatus,
    N5ExecutionEvidenceAdapter,
    N6GoalEvidenceAdapter,
    RollbackInvalidationOutcome,
    VerifiedExecutionEvidenceContract,
    VerifiedOperationReference,
    utc_now_iso,
)
from src.memory.policy import resolve_test_sandbox_root
from src.tools.skill_manager import SkillManager


PROJECT_ROOT = settings.BASE_DIR.resolve()
REAL_MEMORY_JSON = PROJECT_ROOT / "data" / "memory_store.json"
REAL_EXPERIENCES_DB = PROJECT_ROOT / "data" / "experiences.db"
REAL_BACKUPS_DIR = PROJECT_ROOT / "data" / "backups"
REAL_TX_DIR = PROJECT_ROOT / "data" / "transactions"
REAL_GOALS_DIR = PROJECT_ROOT / "data" / "goals"


def _file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class _TestSkill(BaseSkill):
    """Skill determinista configurable para pruebas integradas N6."""

    def __init__(self, name: str, fn) -> None:
        super().__init__(name=name, description=f"Skill prueba {name}", parameters={})
        self._fn = fn

    def execute(self, **kwargs) -> str:
        return self._fn(**kwargs)


class _DummyN8LLM:
    def generate(self, *_args, **_kwargs) -> str:
        return "Respuesta offline."


def _build_skill_manager(overrides: dict[str, BaseSkill] | None = None) -> SkillManager:
    sm = SkillManager.__new__(SkillManager)
    sm.skills = {
        "system_time": _TestSkill("system_time", lambda **_: "2026-11-21 23:30:00 UTC"),
        "system_info": _TestSkill("system_info", lambda **_: "CPU: 14%, RAM libre: 10.2 GB, GPU: GTX 1660."),
        "web_search": _TestSkill("web_search", lambda **kw: f"Resultados query={kw.get('query', '')}"),
        "web_extraction": _TestSkill(
            "web_extraction",
            lambda **kw: "CONTENIDO_WEB_CRUDO_EXTERNO_NO_CONFIABLE_INJECTION_IGNORE_SYSTEM",
        ),
        "document_export": _TestSkill("document_export", lambda **kw: f"Documento exportado: {kw.get('title', 'informe')}"),
        "file_editor": _TestSkill("file_editor", lambda **_: "SHOULD_NOT_BYPASS"),
    }
    if overrides:
        sm.skills.update(overrides)
    return sm


class N8CaptureContractsAdaptersAndRollbackTests(unittest.TestCase):
    """Cobertura integral de N8.3 Subfase 2 (contratos, N5, N6, coordinador y rollback)."""

    _prev_jarvis_env: str | None = None
    _baseline_json_sha: str | None = None
    _baseline_db_exists: bool = False
    _baseline_backups_exists: bool = False
    _baseline_tx_exists: bool = False
    _baseline_goals_exists: bool = False

    @classmethod
    def setUpClass(cls) -> None:
        cls._prev_jarvis_env = os.environ.get("JARVIS_ENV")
        os.environ["JARVIS_ENV"] = "test"
        cls._baseline_json_sha = _file_sha256(REAL_MEMORY_JSON)
        cls._baseline_db_exists = REAL_EXPERIENCES_DB.exists()
        cls._baseline_backups_exists = REAL_BACKUPS_DIR.exists()
        cls._baseline_tx_exists = REAL_TX_DIR.exists()
        cls._baseline_goals_exists = REAL_GOALS_DIR.exists()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._prev_jarvis_env is None:
            os.environ.pop("JARVIS_ENV", None)
        else:
            os.environ["JARVIS_ENV"] = cls._prev_jarvis_env

        assert _file_sha256(REAL_MEMORY_JSON) == cls._baseline_json_sha
        assert REAL_EXPERIENCES_DB.exists() == cls._baseline_db_exists
        assert REAL_BACKUPS_DIR.exists() == cls._baseline_backups_exists
        assert REAL_TX_DIR.exists() == cls._baseline_tx_exists
        assert REAL_GOALS_DIR.exists() == cls._baseline_goals_exists

    def setUp(self) -> None:
        sandbox = resolve_test_sandbox_root()
        sandbox.mkdir(parents=True, exist_ok=True)
        self.work_dir = sandbox / f"n8_s2_case_{uuid4().hex[:10]}"
        self.work_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = self.work_dir / "experiences.db"
        self.mem_backup_dir = self.work_dir / "mem_backups"
        self.mem_backup_dir.mkdir(parents=True, exist_ok=True)

        self.cfg_root = self.work_dir / "authorized_cfg"
        self.cfg_root.mkdir(parents=True, exist_ok=True)
        self.n5_backup_dir = self.work_dir / "n5_backups"
        self.n5_backup_dir.mkdir(parents=True, exist_ok=True)
        self.n5_journal_dir = self.work_dir / "n5_journal"
        self.n5_journal_dir.mkdir(parents=True, exist_ok=True)
        self.n5_audit_log = self.work_dir / "n5_audit.jsonl"
        self.goal_store_dir = self.work_dir / "goal_store"
        self.goal_store_dir.mkdir(parents=True, exist_ok=True)

        self.conv_memory = ConversationMemory(
            max_turns=6,
            storage_file=self.work_dir / "conv_memory.json",
            auto_save=False,
        )
        self.store = ExperienceStore(self.db_path)
        self.engine = MemoryEngine(
            store=self.store,
            backup_dir=self.mem_backup_dir,
            is_private_mode_fn=lambda: self.conv_memory.is_private_mode,
        )
        self.capture_service = ExperienceCaptureService(
            memory_engine=self.engine,
            enabled=True,
            auto_capture_enabled=True,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.assertEqual(_file_sha256(REAL_MEMORY_JSON), self._baseline_json_sha)
        self.assertEqual(REAL_EXPERIENCES_DB.exists(), self._baseline_db_exists)
        self.assertEqual(REAL_BACKUPS_DIR.exists(), self._baseline_backups_exists)

    def _build_orchestrator(self, *, attach_capture: bool = True) -> tuple[FileEditorSkill, ChainOrchestrator]:
        fe = FileEditorSkill(
            authorized_roots=[self.cfg_root, self.n5_backup_dir],
            backup_dir=self.n5_backup_dir,
            audit_logger=AuditLogger(log_file=self.n5_audit_log),
            backup_manager=BackupManager(),
        )
        orch = ChainOrchestrator(
            file_editor=fe,
            journal_dir=self.n5_journal_dir,
            memory_capture_service=self.capture_service if attach_capture else None,
        )
        return fe, orch

    def _run_real_n5_execution(
        self,
        orch: ChainOrchestrator,
        *,
        exec_id: str,
        game_name: str = "CyberQuest",
        ini_content: str = "[ScalabilityGroups]\nsg.ShadowQuality=3\nsg.TextureQuality=3\n",
    ) -> tuple[Path, dict[str, object]]:
        game_dir = self.cfg_root / game_name / "Config"
        game_dir.mkdir(parents=True, exist_ok=True)
        cfg = game_dir / "GameUserSettings.ini"
        cfg.write_text(ini_content, encoding="utf-8")

        rec = orch.create_execution(exec_id)
        snap = orch.inspect_target_config(cfg)
        hw = orch.detect_hardware_context("GTX 1660 6 GB")
        recs = orch.investigate_optimizations(
            execution_id=rec.execution_id,
            user_goal="GTX 1660 6 GB",
            application_name=game_name,
            config_snapshot=snap,
            hardware_context=hw,
        )
        plan = orch.build_and_validate_plan(
            execution_id=rec.execution_id,
            user_goal="GTX 1660 6 GB",
            application_name=game_name,
            config_snapshot=snap,
            hardware_context=hw,
            recommendations=recs,
        )
        sim = orch.simulate_plan(plan)
        token = orch.authorize_plan_interactively(
            plan,
            approval_hash=sim["approval_hash"],
            selected_operation_ids=None,
            input_fn=lambda _: "sí",
        )
        summary = orch.execute_approved_plan(
            plan,
            approval_hash=sim["approval_hash"],
            user_approved=True,
            authorization_token=token,
        )
        return cfg, summary

    def _make_valid_n5_contract(self, *, exec_id: str = "exec-contract-1") -> VerifiedExecutionEvidenceContract:
        after_h = "1" * 64
        before_h = "0" * 64
        app_h = "a" * 64
        art_path = str(self.cfg_root / "CyberQuest" / "Config" / "GameUserSettings.ini")
        return VerifiedExecutionEvidenceContract(
            contract_version=CAPTURE_CONTRACT_VERSION,
            source_layer="N5_CHAIN_ORCHESTRATOR",
            source_execution_id=exec_id,
            canonical_unit_id="n5:plan_c:op-001:update_key",
            source_goal_id="n5_plan:plan_c",
            source_plan_id="plan_c",
            source_task_id="op-001",
            observed_final_state="COMPLETED",
            verification_outcome="verified",
            verifier_component="ChainOrchestratorVerifier",
            executor_component="FileEditorSkill",
            operation_type="update_key",
            expected_outcome="Valor ShadowQuality=1 verificado por SHA-256.",
            fact_title="Cambio verificado N5 (update_key) en GameUserSettings.ini",
            fact_content="Ejecución N5 aplicó y verificó op-001 sobre GameUserSettings.ini.",
            fact_summary="op-001 verificada en GameUserSettings.ini.",
            approval_hash=app_h,
            verified_operation_refs=(
                VerifiedOperationReference(
                    ref_id="op-001",
                    operation_type="update_key",
                    target_ref=art_path,
                    verification_status="VERIFIED",
                    before_sha256=before_h,
                    after_sha256=after_h,
                ),
            ),
            verified_artifacts=(art_path,),
            structured_evidence={
                "execution_id": exec_id,
                "plan_id": "plan_c",
                "plan_status": "COMPLETED",
                "approval_hash": app_h,
                "applied_count": "1",
                "verified_count": "1",
                "rollback_present": "false",
            },
            verified_at="2026-11-21T23:00:00Z",
            scope_type=MemoryScopeType.APP_OR_GAME,
            scope_key="cyberquest",
        )

    def _make_valid_n6_contract(self, *, plan_id: str = "plan_n6_c") -> VerifiedExecutionEvidenceContract:
        art = f"n6_skill/{plan_id}/t1_sysinfo/system_info"
        return VerifiedExecutionEvidenceContract(
            contract_version=CAPTURE_CONTRACT_VERSION,
            source_layer="N6_EXECUTION_SUPERVISOR",
            source_execution_id=f"n6:{plan_id}",
            canonical_unit_id=f"n6:{plan_id}:t1_sysinfo:system_info",
            source_goal_id="goal_n6_c",
            source_plan_id=plan_id,
            source_task_id="t1_sysinfo",
            observed_final_state="completed",
            verification_outcome="verified",
            verifier_component="ExecutionSupervisorSkillVerifier",
            executor_component="SkillManager:system_info",
            operation_type="n6_skill:system_info",
            expected_outcome="Ejecución verificada de system_info en tarea t1_sysinfo.",
            fact_title="Subtarea N6 verificada (system_info): t1_sysinfo",
            fact_content=f"En plan N6 '{plan_id}', t1_sysinfo ejecutó system_info verificadamente.",
            fact_summary="Subtarea N6 t1_sysinfo verificada.",
            approval_hash=None,
            verified_operation_refs=(
                VerifiedOperationReference(
                    ref_id="t1_sysinfo",
                    operation_type="n6_skill:system_info",
                    target_ref=art,
                    verification_status="VERIFIED",
                ),
            ),
            verified_artifacts=(art,),
            structured_evidence={
                "goal_id": "goal_n6_c",
                "plan_id": plan_id,
                "task_id": "t1_sysinfo",
                "goal_status": "completed",
                "task_status": "completed",
                "evidence_kind": "skill_execution",
            },
            verified_at="2026-11-21T23:05:00Z",
            scope_type=MemoryScopeType.PROJECT,
            scope_key="jarvis-core",
        )

    # ------------------------------------------------------------------
    # 1. Pruebas estrictas sobre Contratos de Evidencia
    # ------------------------------------------------------------------

    def test_contracts_determinism_immutability_and_rejection_of_forgery(self) -> None:
        """Valida contratos N5/N6 válidos, hash determinista, inmutabilidad y rechazo de manipulación/booleanos."""
        c_n5_a = self._make_valid_n5_contract()
        c_n5_b = self._make_valid_n5_contract()
        c_n6 = self._make_valid_n6_contract()

        self.assertEqual(c_n5_a.evidence_sha256, c_n5_b.evidence_sha256)
        self.assertEqual(c_n5_a.contract_sha256, c_n5_b.contract_sha256)
        self.assertTrue(c_n5_a.verify_integrity())
        self.assertTrue(c_n6.verify_integrity())

        roundtrip_n5 = VerifiedExecutionEvidenceContract.from_dict(c_n5_a.to_dict())
        roundtrip_n6 = VerifiedExecutionEvidenceContract.from_dict(c_n6.to_dict())
        self.assertEqual(roundtrip_n5.contract_sha256, c_n5_a.contract_sha256)
        self.assertEqual(roundtrip_n6.contract_sha256, c_n6.contract_sha256)

        # Mutación en memoria rechazada (frozen dataclass + MappingProxyType).
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            c_n5_a.fact_title = "Alterado"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            c_n5_a.structured_evidence_view["extra"] = "tamper"  # type: ignore[index]

        # Manipulación del diccionario serializado (sin actualizar o con hash manipulado) rechazada.
        tampered = c_n5_a.to_dict()
        tampered["fact_content"] = "Otro contenido falsificado"
        with self.assertRaises(MemoryEvidenceContractError):
            VerifiedExecutionEvidenceContract.from_dict(tampered)

        # Campos desconocidos rechazados tanto en contrato como en VerifiedOperationReference.
        unknown_field_payload = c_n5_a.to_dict()
        unknown_field_payload["extra_backdoor"] = True
        with self.assertRaises(MemoryEvidenceContractError):
            VerifiedExecutionEvidenceContract.from_dict(unknown_field_payload)

        unknown_op_ref = c_n5_a.verified_operation_refs[0].to_dict()
        unknown_op_ref["extra_flag"] = "x"
        with self.assertRaises(MemoryEvidenceContractError):
            VerifiedOperationReference.from_dict(unknown_op_ref)

        # Campos faltantes rechazados.
        incomplete_dict = c_n6.to_dict()
        del incomplete_dict["verifier_component"]
        with self.assertRaises(MemoryEvidenceContractError):
            VerifiedExecutionEvidenceContract.from_dict(incomplete_dict)

        # Evidencia fabricada con booleano aislado `verified=True` o verificador LLM rechazada.
        fabricated = c_n6.to_dict()
        fabricated.pop("contract_sha256", None)
        fabricated.pop("evidence_sha256", None)
        fabricated["structured_evidence"] = {"verified": True}
        with self.assertRaises(MemoryEvidenceContractError):
            VerifiedExecutionEvidenceContract.from_dict(fabricated)

        llm_verifier = c_n6.to_dict()
        llm_verifier.pop("contract_sha256", None)
        llm_verifier.pop("evidence_sha256", None)
        llm_verifier["verifier_component"] = "OllamaLLMClaim"
        with self.assertRaises(MemoryEvidenceContractError):
            VerifiedExecutionEvidenceContract.from_dict(llm_verifier)

        # Inconsistencias internas rechazadas (execution_id distinto en structured_evidence, N5 sin approval_hash).
        mismatch_exec = c_n5_a.to_dict()
        mismatch_exec.pop("contract_sha256", None)
        mismatch_exec.pop("evidence_sha256", None)
        mismatch_exec["structured_evidence"] = {
            **dict(c_n5_a.structured_evidence),
            "execution_id": "exec-other",
        }
        with self.assertRaises(MemoryEvidenceContractError):
            VerifiedExecutionEvidenceContract.from_dict(mismatch_exec)

        no_approval_n5 = c_n5_a.to_dict()
        no_approval_n5.pop("contract_sha256", None)
        no_approval_n5.pop("evidence_sha256", None)
        no_approval_n5["approval_hash"] = None
        with self.assertRaises(MemoryEvidenceContractError):
            VerifiedExecutionEvidenceContract.from_dict(no_approval_n5)

    # ------------------------------------------------------------------
    # 2. Adaptador N5 (`N5ExecutionEvidenceAdapter`)
    # ------------------------------------------------------------------

    def test_n5_adapter_accepts_verified_execution_and_rejects_invalid_cases(self) -> None:
        """Cubre COMPLETED real y rechaza ausencia de verificación, no aplicada, hashes, aprobación ajena y rollback."""
        _, orch = self._build_orchestrator(attach_capture=False)
        adapter = N5ExecutionEvidenceAdapter()
        _cfg, summary = self._run_real_n5_execution(orch, exec_id="exec-n5-valid")
        record = orch.get_execution("exec-n5-valid")
        txs = orch.transaction_manager.get_transactions_for_execution("exec-n5-valid")

        contracts = adapter.adapt_execution(record, summary=summary, transaction_records=txs)
        self.assertGreaterEqual(len(contracts), 1)
        self.assertTrue(all(c.verify_integrity() for c in contracts))
        self.assertEqual(contracts[0].approval_hash, record.approval_hash)
        self.assertEqual(contracts[0].source_execution_id, "exec-n5-valid")

        # Diccionario declarativo con user_approved=True / COMPLETED aislado rechazado.
        with self.assertRaises(MemoryEvidenceContractError):
            adapter.adapt_execution({"state": "COMPLETED", "user_approved": True, "execution_id": "fake"})

        # COMPLETED sin verificación post-write rechazado.
        rec_no_verif = orch.get_execution("exec-n5-valid")
        saved_verifs = list(rec_no_verif.verification_results)
        try:
            rec_no_verif.verification_results = []
            with self.assertRaises(MemoryEvidenceContractError):
                adapter.adapt_execution(rec_no_verif, summary=summary, transaction_records=txs)
        finally:
            rec_no_verif.verification_results = saved_verifs

        # Verificación post-write con hash incorrecto rechazado.
        try:
            rec_no_verif.verification_results = [
                {**v, "sha256": "f" * 64} for v in saved_verifs
            ]
            with self.assertRaises(MemoryEvidenceContractError):
                adapter.adapt_execution(rec_no_verif, summary=summary, transaction_records=txs)
        finally:
            rec_no_verif.verification_results = saved_verifs

        # approval_hash inconsistente rechazado.
        orig_app_hash = rec_no_verif.approval_hash
        try:
            rec_no_verif.approval_hash = "9" * 64
            with self.assertRaises(MemoryEvidenceContractError):
                adapter.adapt_execution(rec_no_verif, summary=summary, transaction_records=txs)
        finally:
            rec_no_verif.approval_hash = orig_app_hash

        # Evidencia perteneciente a otra ejecución rechazada.
        try:
            rec_no_verif.verification_results = [
                {**saved_verifs[0], "execution_id": "exec-other-999"}
            ]
            with self.assertRaises(MemoryEvidenceContractError):
                adapter.adapt_execution(rec_no_verif, summary=summary, transaction_records=txs)
        finally:
            rec_no_verif.verification_results = saved_verifs

        # Operación propuesta (bound) pero no aplicada efectivamente rechazada.
        saved_bound = rec_no_verif.bound_operation_ids
        try:
            rec_no_verif.bound_operation_ids = ("op-001", "op-never-applied")
            with self.assertRaises(MemoryEvidenceContractError):
                adapter.adapt_execution(rec_no_verif, summary=summary, transaction_records=txs)
        finally:
            rec_no_verif.bound_operation_ids = saved_bound

        # Ejecución con rollback previo rechazada por el adaptador N5.
        rb_res = orch.rollback_execution("exec-n5-valid")
        self.assertEqual(rb_res["state"], ChainState.ROLLED_BACK.value)
        txs_after_rb = orch.transaction_manager.get_transactions_for_execution("exec-n5-valid")
        with self.assertRaises(MemoryEvidenceContractError):
            adapter.adapt_execution(orch.get_execution("exec-n5-valid"), transaction_records=txs_after_rb)

    # ------------------------------------------------------------------
    # 3. Adaptador N6 (`N6GoalEvidenceAdapter`)
    # ------------------------------------------------------------------

    def test_n6_adapter_completed_partial_and_untrusted_rejection(self) -> None:
        """Valida captura en COMPLETED/PARTIAL real y rechazo de internal_synthesis, LLM y evidencia ajena."""
        sm = _build_skill_manager(
            {
                "web_search": _TestSkill(
                    "web_search",
                    lambda **_: (_ for _ in ()).throw(RuntimeError("Fallo simulado de skill")),
                )
            }
        )
        goal_store = GoalStore(store_dir=self.goal_store_dir)
        goal_engine = GoalEngine(
            skill_manager=sm,
            goal_store=goal_store,
            use_ai=False,
        )
        adapter = N6GoalEvidenceAdapter()

        # 3a) Objetivo COMPLETED con tarea verificada por skill real y tarea internal_synthesis:
        # captura únicamente la tarea con verificador objetivo real (system_info), excluye internal_synthesis.
        plan_comp = goal_engine.create_goal_and_plan(
            "Diagnóstico rápido de sistema",
            custom_tasks=[
                GoalTask(
                    task_id="t1_sysinfo",
                    title="Leer hardware y RAM",
                    description="Ejecutar system_info",
                    skill_name="system_info",
                    risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                ),
                GoalTask(
                    task_id="t2_synth",
                    title="Síntesis interna",
                    description="Resumir hallazgos",
                    skill_name=None,
                    depends_on=["t1_sysinfo"],
                    risk_category=TaskRiskCategory.SAFE_PLANNING,
                ),
            ],
        )
        exec_comp = goal_engine.execute_plan(plan_comp, enable_replan=False)
        self.assertEqual(exec_comp.final_status, GoalStatus.COMPLETED)

        report_comp = adapter.adapt_with_report(plan_comp, execution_result=exec_comp)
        self.assertEqual(report_comp.accepted_unit_ids, ("t1_sysinfo",))
        self.assertTrue(any(uid == "t2_synth" for uid, _ in report_comp.rejected_units))
        self.assertEqual(len(report_comp.contracts), 1)
        self.assertEqual(report_comp.contracts[0].observed_final_state, "completed")
        self.assertEqual(report_comp.contracts[0].verifier_component, "ExecutionSupervisorSkillVerifier")

        # 3b) Objetivo COMPLETED únicamente con internal_synthesis (sin evidencia objetiva): RECHAZADO.
        plan_synth_only = goal_engine.create_goal_and_plan(
            "Solo síntesis cognitiva",
            custom_tasks=[
                GoalTask(
                    task_id="t_only_synth",
                    title="Pensar sin herramienta",
                    description="Sin herramienta objetiva",
                    skill_name=None,
                    risk_category=TaskRiskCategory.SAFE_PLANNING,
                )
            ],
        )
        exec_synth_only = goal_engine.execute_plan(plan_synth_only, enable_replan=False)
        self.assertEqual(exec_synth_only.final_status, GoalStatus.COMPLETED)
        with self.assertRaises(MemoryEvidenceContractError):
            adapter.adapt_execution(plan_synth_only, execution_result=exec_synth_only)

        # 3c) Objetivo PARTIAL: captura exclusivamente t_ok verificada; nunca falla/bloqueada/pendiente
        # ni genera un hecho declarando éxito global del objetivo.
        plan_partial = goal_engine.create_goal_and_plan(
            "Plan mixto con fallo parcial",
            custom_tasks=[
                GoalTask(
                    task_id="t_ok",
                    title="Consultar hora",
                    description="Ejecutar system_time",
                    skill_name="system_time",
                    risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                    max_retries=0,
                ),
                GoalTask(
                    task_id="t_fail",
                    title="Paso que falla",
                    description="Falla controlada",
                    skill_name="web_search",
                    skill_params={"query": "fallo"},
                    risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                    max_retries=0,
                ),
                GoalTask(
                    task_id="t_blocked",
                    title="Paso dependiente bloqueado",
                    description="Bloqueado por fallo",
                    skill_name="system_info",
                    depends_on=["t_fail"],
                    risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                ),
            ],
        )
        exec_partial = goal_engine.execute_plan(plan_partial, enable_replan=False)
        self.assertEqual(exec_partial.final_status, GoalStatus.PARTIAL)

        contracts_partial = adapter.adapt_execution(plan_partial, execution_result=exec_partial)
        self.assertEqual(len(contracts_partial), 1)
        self.assertEqual(contracts_partial[0].source_task_id, "t_ok")
        self.assertEqual(contracts_partial[0].observed_final_state, "partial")
        self.assertIn("parcial", contracts_partial[0].fact_content.lower())
        self.assertNotIn("objetivo completo", contracts_partial[0].fact_content.lower())

        # 3d) Evidencia de tarea diferente y evidencia LLM sin verificador objetivo: RECHAZADAS.
        plan_forged = goal_engine.create_goal_and_plan(
            "Plan con evidencias falsas",
            custom_tasks=[
                GoalTask(
                    task_id="t_wrong_ref",
                    title="Evidencia ajena",
                    description="Referencia cambiada",
                    skill_name="system_info",
                    risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                ),
                GoalTask(
                    task_id="t_llm_claim",
                    title="Afirmación LLM",
                    description="Sin verificador objetivo",
                    skill_name="system_time",
                    risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                ),
            ],
        )
        goal_engine.execute_plan(plan_forged, enable_replan=False)
        t_wrong = plan_forged.get_task("t_wrong_ref")
        t_llm = plan_forged.get_task("t_llm_claim")
        assert t_wrong is not None and t_llm is not None
        assert t_wrong.execution_evidence is not None and t_llm.execution_evidence is not None
        t_wrong.execution_evidence["task_id"] = "otra_tarea_inexistente"
        t_llm.execution_evidence["verifier_component"] = "GeminiLLMVerifier"

        with self.assertRaises(MemoryEvidenceContractError):
            adapter.adapt_execution(plan_forged)

    # ------------------------------------------------------------------
    # 4. Coordinador (`ExperienceCaptureService`), Privacidad y Fallos Cerrados
    # ------------------------------------------------------------------

    def test_capture_service_policies_privacy_idempotency_and_fault_isolation(self) -> None:
        """Verifica idempotencia, conflictos, deshabilitación, modo privado y aislamiento de fallos SQLite."""
        _, orch = self._build_orchestrator(attach_capture=True)
        cfg, summary = self._run_real_n5_execution(orch, exec_id="exec-coord-1")
        self.assertEqual(summary["state"], ChainState.COMPLETED.value)

        active_mems = self.store.list_memories(status=MemoryStatus.ACTIVE)
        self.assertGreaterEqual(len(active_mems), 2)  # Dos operaciones (ShadowQuality y TextureQuality)
        self.assertTrue(all(m.source == MemorySource.EXECUTION_VERIFIED for m in active_mems))
        self.assertTrue(all(m.category == MemoryCategory.FACT for m in active_mems))
        # Ninguna se fusiona indebidamente entre unidades distintas de la misma ejecución.
        self.assertEqual(len({m.canonical_unit_id for m in active_mems}), len(active_mems))

        # Captura repetida de la misma ejecución es idempotente sin duplicar ni avanzar versión.
        tok_before = self.engine.get_store_version_token()
        rec_again = orch.get_execution("exec-coord-1")
        txs_again = orch.transaction_manager.get_transactions_for_execution("exec-coord-1")
        dup_batch = self.capture_service.handle_n5_execution_completed(
            record=rec_again,
            summary=summary,
            transaction_records=txs_again,
        )
        self.assertTrue(dup_batch.ok)
        self.assertEqual(dup_batch.captured_count, 0)
        self.assertEqual(dup_batch.deduplicated_count, len(active_mems))
        self.assertEqual(self.engine.get_store_version_token(), tok_before)

        # Conflicto de idempotencia auditado al intentar sobrescribir misma clave con payload distinto.
        base_c = self._make_valid_n5_contract(exec_id="exec-idem-conf")
        out_ok = self.capture_service.capture_contract(base_c)
        self.assertEqual(out_ok.status, "captured")

        conflict_c = VerifiedExecutionEvidenceContract(
            **{
                **{k: v for k, v in base_c.to_dict().items() if k not in {"evidence_sha256", "contract_sha256"}},
                "fact_content": "Payload incompatible bajo misma unidad canónica y ejecución.",
                "verified_operation_refs": (
                    VerifiedOperationReference(
                        ref_id="op-001",
                        operation_type="update_key",
                        target_ref=str(self.cfg_root / "CyberQuest" / "Config" / "GameUserSettings.ini"),
                        verification_status="VERIFIED",
                        before_sha256="0" * 64,
                        after_sha256="2" * 64,
                    ),
                ),
            }
        )
        out_conflict = self.capture_service.capture_contract(conflict_c)
        self.assertEqual(out_conflict.status, "idempotency_conflict")
        audits = self.store.get_audit_entries(memory_id=out_ok.memory_id, limit=20)
        self.assertTrue(any(a.action == MemoryAuditAction.REJECT_IDEMPOTENCY_CONFLICT for a in audits))

        # N8 deshabilitado y auto_capture deshabilitado: cero capturas en disco.
        count_before_gates = self.store.get_stats().total_memories
        svc_n8_off = ExperienceCaptureService(memory_engine=self.engine, enabled=False, auto_capture_enabled=True)
        res_off = svc_n8_off.handle_n5_execution_completed(record=rec_again)
        self.assertFalse(res_off.ok)
        self.assertEqual(res_off.status, "disabled")

        svc_auto_off = ExperienceCaptureService(memory_engine=self.engine, enabled=True, auto_capture_enabled=False)
        res_auto_off = svc_auto_off.handle_n5_execution_completed(record=rec_again)
        self.assertFalse(res_auto_off.ok)
        self.assertEqual(res_auto_off.status, "disabled")

        # Modo privado activo bloquea incluso llamadas programáticas directas y deja buffers privados intactos.
        self.conv_memory.enter_private_mode(allow_history=False)
        self.conv_memory.add_message("user", "Secreto privado de sesión")
        private_snapshot = list(self.conv_memory.get_messages_list())
        try:
            priv_contract = self._make_valid_n5_contract(exec_id="exec-priv-blocked")
            out_priv = self.capture_service.capture_contract(priv_contract)
            self.assertEqual(out_priv.status, "blocked_private_mode")
            self.assertEqual(self.store.get_stats().total_memories, count_before_gates)
            self.assertEqual(self.conv_memory.get_messages_list(), private_snapshot)
        finally:
            self.conv_memory.exit_private_mode()

        # Contenido web extraído en N6: la captura registra el hecho operativo, jamás el contenido crudo web.
        sm_web = _build_skill_manager()
        goal_eng_web = GoalEngine(
            skill_manager=sm_web,
            goal_store=GoalStore(store_dir=self.work_dir / "goals_web"),
            memory_capture_service=self.capture_service,
        )
        plan_web = goal_eng_web.create_goal_and_plan(
            "Extraer fuente web",
            custom_tasks=[
                GoalTask(
                    task_id="t_web",
                    title="Extraer página",
                    description="Web extraction acotada",
                    skill_name="web_extraction",
                    skill_params={"url": "https://example.test/guia"},
                    risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                )
            ],
        )
        exec_web = goal_eng_web.execute_plan(plan_web, enable_replan=False)
        self.assertEqual(exec_web.final_status, GoalStatus.COMPLETED)
        all_after_web = self.store.list_memories(status=MemoryStatus.ACTIVE)
        for mem in all_after_web:
            self.assertNotIn("CONTENIDO_WEB_CRUDO_EXTERNO_NO_CONFIABLE", mem.content)
            self.assertNotIn("CONTENIDO_WEB_CRUDO", mem.summary)

        # Rechazo de secretos/contenido prohibido al intentar fabricar un contrato con api_key.
        with self.assertRaises(MemoryPolicyError):
            VerifiedExecutionEvidenceContract(
                **{
                    **{k: v for k, v in base_c.to_dict().items() if k not in {"evidence_sha256", "contract_sha256"}},
                    "fact_content": "api_key=" + "sk-" + "1234567890abcdef1234567890abcdef",
                }
            )

        # Fallo de SQLite durante captura tras ejecución N5 exitosa: N5 completa normalmente sin excepciones.
        broken_engine = self.engine
        with patch.object(broken_engine, "create_memory", side_effect=MemoryPersistenceError("sqlite_io_error")):
            _cfg2, summary2 = self._run_real_n5_execution(orch, exec_id="exec-sqlite-down", game_name="NovaGame")
            self.assertEqual(summary2["state"], ChainState.COMPLETED.value)
            self.assertEqual(orch.get_execution("exec-sqlite-down").state, ChainState.COMPLETED)

        # El fallo directo al coordinador reporta status='failed' (cerrado, sin éxito falso).
        with patch.object(self.engine, "create_memory", side_effect=MemoryPersistenceError("disk_locked")):
            fail_out = self.capture_service.capture_contract(self._make_valid_n5_contract(exec_id="exec-fail-closed"))
            self.assertEqual(fail_out.status, "failed")
            self.assertIsNone(fail_out.memory_id)

    # ------------------------------------------------------------------
    # 5. Rollback N5 -> Invalidación N8, Concurrencia e Idempotencia
    # ------------------------------------------------------------------

    def test_n5_rollback_invalidation_isolation_idempotency_and_failure_safety(self) -> None:
        """Verifica rollback confirmado vs fallido/no ejecutado, capturas tardías y resiliencia ante errores."""
        _, orch = self._build_orchestrator(attach_capture=True)

        # Ejecución 1 y Ejecución 2 independientes más recuerdo manual de usuario sobre mismo juego.
        cfg1, sum1 = self._run_real_n5_execution(orch, exec_id="exec-rb-target", game_name="TitanGame")
        cfg2, sum2 = self._run_real_n5_execution(orch, exec_id="exec-rb-other", game_name="StarForge")
        self.assertEqual(sum1["state"], ChainState.COMPLETED.value)
        self.assertEqual(sum2["state"], ChainState.COMPLETED.value)

        manual_rec = self.engine.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.FACT,
                source=MemorySource.USER_EXPLICIT,
                title="Nota usuario TitanGame",
                content="El usuario prefiere sombras medias manualmente en TitanGame.",
                scope_type=MemoryScopeType.APP_OR_GAME,
                scope_key="titangame",
            ),
            actor="user",
        ).record

        # Capturamos además un recuerdo N6 vinculado a la misma ejecución N5 'exec-rb-target'
        # para verificar que el rollback de N5 invalida tanto la huella N5 como la unidad N6 asociada.
        n6_linked_contract = VerifiedExecutionEvidenceContract(
            contract_version=CAPTURE_CONTRACT_VERSION,
            source_layer="N6_EXECUTION_SUPERVISOR",
            source_execution_id="exec-rb-target",
            canonical_unit_id="n6:plan_rb_link:t_chain:chain_orchestrator",
            source_goal_id="goal_rb_link",
            source_plan_id="plan_rb_link",
            source_task_id="t_chain",
            observed_final_state="completed",
            verification_outcome="verified",
            verifier_component="ExecutionSupervisorChainVerifier",
            executor_component="ChainOrchestrator",
            operation_type="chain_orchestrator",
            expected_outcome="Subtarea N6 enlazada a ejecución N5.",
            fact_title="Subtarea N6 enlazada exec-rb-target",
            fact_content="La subtarea t_chain ejecutó cambios N5 en TitanGame.",
            fact_summary="t_chain completada.",
            approval_hash=orch.get_execution("exec-rb-target").approval_hash,
            verified_operation_refs=(
                VerifiedOperationReference(
                    ref_id="op-001",
                    operation_type="chain_operation",
                    target_ref=str(cfg1),
                    verification_status="VERIFIED",
                    after_sha256="a" * 64,
                ),
            ),
            verified_artifacts=(str(cfg1),),
            structured_evidence={
                "goal_id": "goal_rb_link",
                "plan_id": "plan_rb_link",
                "task_id": "t_chain",
                "goal_status": "completed",
                "task_status": "completed",
                "evidence_kind": "chain_orchestrator",
            },
            verified_at=utc_now_iso(),
            scope_type=MemoryScopeType.APP_OR_GAME,
            scope_key="titangame",
        )
        self.assertEqual(self.capture_service.capture_contract(n6_linked_contract).status, "captured")

        # 5a) Rollback solicitado / no confirmado NO debe invalidar recuerdos.
        unconfirmed = self.capture_service.handle_n5_rollback_confirmed(
            execution_id="exec-rb-target",
            record=orch.get_execution("exec-rb-target"),
            reason="requested_only",
        )
        self.assertFalse(unconfirmed.ok)
        self.assertEqual(unconfirmed.status, "not_confirmed")

        mismatch_id = self.capture_service.handle_n5_rollback_confirmed(
            execution_id="exec-different",
            rollback_result={"execution_id": "exec-rb-target", "state": "ROLLED_BACK"},
        )
        self.assertFalse(mismatch_id.ok)
        self.assertEqual(mismatch_id.status, "not_confirmed")

        active_target_before = [
            m
            for m in self.store.list_memories(status=MemoryStatus.ACTIVE)
            if m.provenance.source_execution_id == "exec-rb-target"
        ]
        self.assertGreaterEqual(len(active_target_before), 3)

        # Revocar manualmente uno de los recuerdos antes del rollback no debe corromperlo después.
        pre_revoked_target = active_target_before[0]
        pre_revoked_rec = self.engine.revoke_memory(
            pre_revoked_target.memory_id,
            reason="manual_pre_revoke",
            actor="user",
        )
        still_active_before = [
            m
            for m in self.store.list_memories(status=MemoryStatus.ACTIVE)
            if m.provenance.source_execution_id == "exec-rb-target"
        ]
        self.assertEqual(len(still_active_before), len(active_target_before) - 1)

        # 5b) Confirmar rollback REAL en N5: revoca exclusivamente los restantes de 'exec-rb-target',
        # preserva intacto el ya revocado y no toca 'exec-rb-other' ni el recuerdo manual.
        rb_done = orch.rollback_execution("exec-rb-target")
        self.assertEqual(rb_done["state"], ChainState.ROLLED_BACK.value)

        post_pre_revoked = self.store.get_memory(pre_revoked_target.memory_id)
        assert post_pre_revoked is not None
        self.assertEqual(post_pre_revoked.status, MemoryStatus.REVOKED)
        self.assertEqual(post_pre_revoked.revoked_at, pre_revoked_rec.revoked_at)

        for mem in self.store.list_memories(status=None, limit=100):
            if mem.provenance.source_execution_id == "exec-rb-target":
                self.assertEqual(mem.status, MemoryStatus.REVOKED)
                self.assertIsNone(mem.superseded_by)
            elif mem.provenance.source_execution_id == "exec-rb-other":
                self.assertEqual(mem.status, MemoryStatus.ACTIVE)

        self.assertEqual(self.store.get_memory(manual_rec.memory_id).status, MemoryStatus.ACTIVE)
        self.assertTrue(self.capture_service.is_execution_invalidated("exec-rb-target"))

        # Auditoría histórica conservada (nunca borrado silencioso).
        for old_m in still_active_before:
            auds = self.store.get_audit_entries(memory_id=old_m.memory_id, limit=20)
            self.assertTrue(any(a.action == MemoryAuditAction.REVOKE and a.reason == "n5_rollback_confirmed" for a in auds))

        # 5c) Repetición del evento de rollback es idempotente sin corrupción ni avance de token.
        tok_after_rb = self.engine.get_store_version_token()
        rep_rb = self.capture_service.handle_n5_rollback_confirmed(
            execution_id="exec-rb-target",
            record=orch.get_execution("exec-rb-target"),
            rollback_result=rb_done,
        )
        self.assertTrue(rep_rb.ok)
        self.assertEqual(rep_rb.status, "idempotent_noop")
        self.assertEqual(self.engine.get_store_version_token(), tok_after_rb)

        # 5d) Captura posterior de una ejecución ya invalidada (incluso con instancia nueva desde disco) es rechazada.
        fresh_capture = ExperienceCaptureService(
            memory_engine=self.engine,
            enabled=True,
            auto_capture_enabled=True,
        )
        late_out = fresh_capture.capture_contract(self._make_valid_n5_contract(exec_id="exec-rb-target"))
        self.assertEqual(late_out.status, "invalidated_execution")

        # Caso tombstone: rollback confirmado ANTES de cualquier captura previa también persiste bloqueo en disco.
        rb_early = fresh_capture.handle_n5_rollback_confirmed(
            execution_id="exec-rollback-before-capture",
            rollback_result={"execution_id": "exec-rollback-before-capture", "state": "ROLLED_BACK"},
        )
        self.assertTrue(rb_early.ok)
        self.assertEqual(rb_early.status, "tombstone_marked")
        fresh_after_restart = ExperienceCaptureService(
            memory_engine=MemoryEngine(store=ExperienceStore(self.db_path), backup_dir=self.mem_backup_dir),
            enabled=True,
            auto_capture_enabled=True,
        )
        late_early_rb = fresh_after_restart.capture_contract(
            self._make_valid_n5_contract(exec_id="exec-rollback-before-capture")
        )
        self.assertEqual(late_early_rb.status, "invalidated_execution")

        # 5e) Rollback fallido (conflicto externo en archivo) NO invalida los recuerdos de 'exec-rb-other'.
        cfg2.write_text("[ScalabilityGroups]\nsg.ShadowQuality=999_externo\n", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            orch.rollback_execution("exec-rb-other")
        self.assertEqual(orch.get_execution("exec-rb-other").state, ChainState.FAILED)
        other_mems = [
            m
            for m in self.store.list_memories(status=MemoryStatus.ACTIVE)
            if m.provenance.source_execution_id == "exec-rb-other"
        ]
        self.assertGreaterEqual(len(other_mems), 1)
        failed_hook = self.capture_service.handle_n5_rollback_confirmed(
            execution_id="exec-rb-other",
            record=orch.get_execution("exec-rb-other"),
        )
        self.assertFalse(failed_hook.ok)
        self.assertEqual(failed_hook.status, "not_confirmed")

        # 5f) Error en N8 durante rollback confirmado en N5 no altera ni revierte el rollback N5.
        _cfg3, _sum3 = self._run_real_n5_execution(orch, exec_id="exec-rb-n8err", game_name="AetherGame")
        with patch.object(self.engine, "invalidate_by_execution", side_effect=MemoryPersistenceError("sqlite_down_on_rb")):
            rb3 = orch.rollback_execution("exec-rb-n8err")
            self.assertEqual(rb3["state"], ChainState.ROLLED_BACK.value)
            self.assertEqual(orch.get_execution("exec-rb-n8err").state, ChainState.ROLLED_BACK)

    def test_concurrent_capture_and_rollback_leaves_no_orphan_active_memory(self) -> None:
        """Si captura y rollback ocurren en paralelo, la ejecución revertida nunca deja recuerdos ACTIVE huérfanos."""
        _, orch = self._build_orchestrator(attach_capture=False)
        _cfg, _sum = self._run_real_n5_execution(orch, exec_id="exec-race-rb", game_name="RaceGame")
        rec = orch.get_execution("exec-race-rb")
        rb_done = orch.rollback_execution("exec-race-rb")
        rec_rb = orch.get_execution("exec-race-rb")

        contract = self._make_valid_n5_contract(exec_id="exec-race-rb")
        barrier = threading.Barrier(2)

        def _run_capture() -> CaptureUnitOutcome:
            barrier.wait(timeout=5.0)
            return self.capture_service.capture_contract(contract)

        def _run_rollback() -> RollbackInvalidationOutcome:
            barrier.wait(timeout=5.0)
            return self.capture_service.handle_n5_rollback_confirmed(
                execution_id="exec-race-rb",
                record=rec_rb,
                rollback_result=rb_done,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_cap = pool.submit(_run_capture)
            f_rb = pool.submit(_run_rollback)
            cap_out = f_cap.result(timeout=10.0)
            rb_out = f_rb.result(timeout=10.0)

        self.assertTrue(rb_out.ok or rb_out.status in {"invalidated", "tombstone_marked", "idempotent_noop"})
        self.assertIn(cap_out.status, {"captured", "invalidated_execution"})
        orphan_active = [
            m
            for m in self.store.list_memories(status=MemoryStatus.ACTIVE)
            if m.provenance.source_execution_id == "exec-race-rb"
        ]
        self.assertEqual(orphan_active, [])
        self.assertTrue(self.store.verify_integrity().ok)
        self.assertNotEqual(rec.state, ChainState.PLANNING)

    def test_console_e2e_n5_and_n6_with_lightweight_import_no_voice(self) -> None:
        """Valida integración por consola en main.py (`/optimizar` -> `/rollback` + `/goal`) sin cargar Whisper/voz."""
        from main import run_console_session

        fe, orch = self._build_orchestrator(attach_capture=False)
        game_dir = self.cfg_root / "ConsoleGame" / "Config"
        game_dir.mkdir(parents=True, exist_ok=True)
        cfg = game_dir / "GameUserSettings.ini"
        cfg.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\nsg.TextureQuality=3\n", encoding="utf-8")

        bm = BrainManager(
            initial_mode="OLLAMA",
            ollama_brain=_DummyN8LLM(),
            skill_manager=_build_skill_manager({"file_editor": fe}),
            memory=self.conv_memory,
        )
        outputs: list[str] = []
        captured_exec: dict[str, str] = {"id": ""}
        cmd_queue = [
            "/optimizar ConsoleGame",
            "ROLLBACK_PLACEHOLDER",
            "/goal diagnosticar el uso de CPU y memoria RAM del sistema",
            "/plan ejecutar",
            "/memoria",
            "salir",
        ]

        def _fake_input(prompt_text: str) -> str:
            # Cualquier sub-prompt de confirmación interactiva (N5/N6) confirma sin consumir la cola de comandos.
            if not str(prompt_text or "").startswith("Tu ["):
                return "sí"
            if not cmd_queue:
                return "salir"
            nxt = cmd_queue.pop(0)
            if nxt == "ROLLBACK_PLACEHOLDER":
                for line in outputs:
                    if "ID de ejecución para rollback:" in line:
                        captured_exec["id"] = (
                            line.split("ID de ejecución para rollback:")[1]
                            .strip()
                            .rstrip(")")
                        )
                return f"/rollback {captured_exec['id']}"
            return nxt

        preloaded_voice = {
            mod: (mod in sys.modules)
            for mod in ("faster_whisper", "sounddevice", "pyttsx3", "speech_recognition")
        }
        with patch.object(settings, "N8_AUTO_CAPTURE_ENABLED", True):
            cap_svc = ExperienceCaptureService(
                memory_engine=self.engine,
                enabled=True,
                auto_capture_enabled=True,
            )
            orch.set_memory_capture_service(cap_svc)
            run_console_session(
                bm,
                enable_tts=False,
                input_fn=_fake_input,
                output_fn=outputs.append,
                orchestrator=orch,
                memory_engine=self.engine,
                memory_capture_service=cap_svc,
            )

        joined = "\n".join(outputs)
        self.assertIn("[ChainOrchestrator: COMPLETED]", joined)
        self.assertIn("[ChainOrchestrator: ROLLED_BACK]", joined)
        self.assertIn("Ejecución finalizada [completed]", joined)
        self.assertTrue(captured_exec["id"])

        revs_after_rb = [
            m
            for m in self.store.list_memories(status=None, limit=100)
            if m.provenance.source_execution_id == captured_exec["id"]
        ]
        self.assertGreaterEqual(len(revs_after_rb), 1)
        self.assertTrue(all(m.status == MemoryStatus.REVOKED for m in revs_after_rb))

        n6_console_active = [
            m
            for m in self.store.list_memories(status=MemoryStatus.ACTIVE, limit=100)
            if m.provenance.source_layer == "N6_EXECUTION_SUPERVISOR"
        ]
        self.assertGreaterEqual(len(n6_console_active), 1)
        for mod, was_loaded in preloaded_voice.items():
            if not was_loaded:
                self.assertNotIn(mod, sys.modules)

        # Subproceso limpio: garantiza que `import main` + sesión de consola sin TTS nunca cargue Whisper/voz.
        probe_code = (
            "import sys, main, src.memory; "
            "from src.brain.brain_manager import BrainManager; "
            "bm = BrainManager.__new__(BrainManager); "
            "bm.global_mode = 'OLLAMA'; bm.memory = None; bm.skill_manager = None; "
            "main.run_console_session(bm, enable_tts=False, input_fn=lambda _: 'salir', output_fn=lambda _: None); "
            "heavy = [m for m in ('faster_whisper', 'sounddevice', 'pyttsx3', 'speech_recognition') if m in sys.modules]; "
            "assert not heavy, f'Modulos de voz cargados indebidamente: {heavy}'"
        )
        env = dict(os.environ)
        env["JARVIS_ENV"] = "test"
        proc = subprocess.run(
            [sys.executable, "-c", probe_code],
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=25,
        )
        self.assertEqual(proc.returncode, 0, msg=f"Subproceso import liviano falló: {proc.stderr or proc.stdout}")

    def test_n6_partial_chain_delegation_and_recovery_rollback_e2e(self) -> None:
        """Valida captura real N6 PARTIAL en SQLite, delegación N6->N5->rollback y recovery rollback."""
        fe, orch = self._build_orchestrator(attach_capture=True)
        game_dir = self.cfg_root / "E2EGame" / "Config"
        game_dir.mkdir(parents=True, exist_ok=True)
        cfg = game_dir / "GameUserSettings.ini"
        cfg.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\n", encoding="utf-8")

        sm = _build_skill_manager(
            {
                "file_editor": fe,
                "web_search": _TestSkill(
                    "web_search",
                    lambda **_: (_ for _ in ()).throw(RuntimeError("Error esperado parcial")),
                ),
            }
        )
        goal_engine = GoalEngine(
            skill_manager=sm,
            chain_orchestrator=orch,
            goal_store=GoalStore(store_dir=self.goal_store_dir),
            memory_capture_service=self.capture_service,
        )

        # 1) Plan N6 PARTIAL persistido vía coordinator: captura solo t1_info.
        p_part = goal_engine.create_goal_and_plan(
            "Diagnóstico parcial real",
            custom_tasks=[
                GoalTask(
                    task_id="t1_info",
                    title="Leer sistema",
                    description="Ejecutar system_info",
                    skill_name="system_info",
                    risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                    max_retries=0,
                ),
                GoalTask(
                    task_id="t2_err",
                    title="Paso roto",
                    description="Falla controlada",
                    skill_name="web_search",
                    skill_params={"query": "fallo_parcial"},
                    risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                    max_retries=0,
                ),
            ],
        )
        res_part = goal_engine.execute_plan(p_part, enable_replan=False)
        self.assertEqual(res_part.final_status, GoalStatus.PARTIAL)

        mems_part = [
            m
            for m in self.store.list_memories(status=MemoryStatus.ACTIVE)
            if m.provenance.source_plan_id == p_part.plan_id
        ]
        self.assertEqual(len(mems_part), 1)
        self.assertEqual(mems_part[0].provenance.source_task_id, "t1_info")
        self.assertEqual(mems_part[0].category, MemoryCategory.FACT)

        # 2) Subtarea N6 delegada a ChainOrchestrator real + rollback posterior invalida N5 y N6.
        p_chain = goal_engine.create_goal_and_plan(
            f"Optimizar {cfg} en E2EGame",
            custom_tasks=[
                GoalTask(
                    task_id="t_n6_chain",
                    title="Optimizar via Chain",
                    description="Aplicar cambio validado",
                    skill_name="file_editor",
                    skill_params={"action": "modify", "file_path": str(cfg)},
                    risk_category=TaskRiskCategory.SENSITIVE_OPERATION,
                    requires_approval=True,
                    metadata={
                        "use_chain_orchestrator": True,
                        "modifies_files": True,
                        "target_app": "E2EGame",
                        "file_path": str(cfg),
                    },
                )
            ],
        )
        goal_engine.authorize_plan(p_chain)
        res_chain = goal_engine.execute_plan(p_chain, input_fn=lambda _: "sí", enable_replan=False)
        self.assertEqual(res_chain.final_status, GoalStatus.COMPLETED)
        t_chain = p_chain.get_task("t_n6_chain")
        assert t_chain is not None and t_chain.chain_execution_id
        chain_exec_id = t_chain.chain_execution_id

        linked_active = [
            m
            for m in self.store.list_memories(status=MemoryStatus.ACTIVE)
            if m.provenance.source_execution_id == chain_exec_id
        ]
        layers = {m.provenance.source_layer for m in linked_active}
        self.assertEqual(layers, {"N5_CHAIN_ORCHESTRATOR", "N6_EXECUTION_SUPERVISOR"})

        orch.rollback_execution(chain_exec_id)
        for m_id in [m.memory_id for m in linked_active]:
            rec_m = self.store.get_memory(m_id)
            assert rec_m is not None
            self.assertEqual(rec_m.status, MemoryStatus.REVOKED)

        # 3) Recovery de N5 confirmado en ROLLED_BACK marca invalidación en N8 de manera idempotente.
        from src.brain.transaction_manager import RecoveryReport

        fake_rep = RecoveryReport(
            transaction_id="tx_rec_1",
            execution_id="exec-recovered-rb",
            plan_id="plan_rec_1",
            initial_state=TransactionState.EXECUTING.value,
            final_state=TransactionState.ROLLED_BACK.value,
            decision="ROLLED_BACK_TO_ORIGINAL",
            restored_files=[str(cfg)],
            rolled_back_operations=["op-001"],
        )
        with patch.object(orch.transaction_manager, "recover_pending_transactions", return_value=[fake_rep]):
            with patch.object(orch, "rehydrate_execution", return_value=None):
                reps = orch.recover_pending_transactions()
                self.assertEqual(len(reps), 1)
        self.assertTrue(self.capture_service.is_execution_invalidated("exec-recovered-rb"))


if __name__ == "__main__":
    unittest.main()
