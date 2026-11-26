"""
Suite adversarial y de regresión integral para la remediación de N8.3 (F-01 a F-09).

Garantías estrictas de aislamiento:
- Opera exclusivamente con `JARVIS_ENV=test` bajo `temp/jarvis_test_isolated/`.
- Verifica antes y después de cada test que `data/memory_store.json`,
  `data/file_editor_audit.jsonl`, `data/experiences.db`, `data/goals/` y `exports/`
  permanezcan 100% intactos (sin modificaciones ni archivos creados).
- Nunca carga hardware físico de audio ni llama a APIs reales externas.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch
import uuid

os.environ["JARVIS_ENV"] = "test"

from config import settings
from skills.base_skill import BaseSkill
from skills.document_export_skill import DocumentExportSkill
from skills.file_editor_skill import AuditLogger, BackupManager, FileEditorSkill
from skills.system_skills import SystemInfoSkill, TimeSkill
from src.brain.brain_manager import BrainManager
from src.brain.chain_orchestrator import ChainOrchestrator, ChainState
from src.brain.codex_client import CodexBrain, CodexAuthStatus, classify_codex_task
from src.brain.exceptions import BrainError, BrainErrorKind
from src.brain.goal_engine import GoalEngine
from src.brain.goal_memory import is_canonical_n8_task_id
from src.brain.goal_models import (
    Constraints,
    GoalContext,
    GoalPlan,
    GoalPlanningError,
    GoalPolicyViolationError,
    GoalStatus,
    GoalTask,
    TaskRiskCategory,
    TaskStatus,
)
from src.brain.goal_persistence import GoalStore
from src.brain.goal_planner import TaskDecomposer
from src.brain.goal_skill_selector import GoalSkillSelector
from src.brain.goal_understanding import GoalUnderstandingEngine
from src.brain.memory import ConversationMemory
from src.brain.ollama_client import OllamaBrain
from src.memory.capture import ExperienceCaptureService
from src.memory.console import MemoryConsoleHandler
from src.memory.engine import MemoryEngine
from src.memory.models import (
    MemoryCategory,
    MemoryCreateInput,
    MemoryPersistenceError,
    MemoryPrivateModeError,
    MemoryScopeType,
    MemorySource,
    MemoryStatus,
    utc_now_iso,
)
from src.memory.snapshot_guard import (
    SnapshotValidationStatus,
    is_valid_store_version_token,
)
from src.memory.store import ExperienceStore
from src.tools.skill_manager import SkillManager


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_SANDBOX_BASE = (PROJECT_ROOT / "temp" / "jarvis_test_isolated").resolve()
REAL_DATA_DIR = (PROJECT_ROOT / "data").resolve()
REAL_MEMORY_JSON = REAL_DATA_DIR / "memory_store.json"
REAL_AUDIT_JSONL = REAL_DATA_DIR / "file_editor_audit.jsonl"
REAL_EXPERIENCES_DB = REAL_DATA_DIR / "experiences.db"
REAL_GOALS_DIR = REAL_DATA_DIR / "goals"


def _file_fingerprint(path: Path) -> tuple[bool, int, str]:
    if not path.exists():
        return (False, 0, "")
    raw = path.read_bytes()
    return (True, len(raw), hashlib.sha256(raw).hexdigest())


def _build_isolated_skill_manager(
    export_dir: Path,
    extra: dict[str, BaseSkill] | None = None,
) -> SkillManager:
    """Construye un SkillManager ligero y 100% aislado sin escribir fuera del sandbox."""
    sm = SkillManager.__new__(SkillManager)
    sm.skills = {}
    sm._private_mode = False
    sm.skills["system_info"] = SystemInfoSkill()
    sm.skills["system_time"] = TimeSkill()
    sm.skills["document_export"] = DocumentExportSkill(allowed_roots=[export_dir])
    if extra:
        for name, inst in extra.items():
            sm.skills[name] = inst
    return sm


class _SimpleSkill(BaseSkill):
    def __init__(
        self,
        name: str,
        fn,
        *,
        parameters: dict | None = None,
        mutates_files: bool = False,
    ) -> None:
        super().__init__(
            name=name,
            description=f"Skill de prueba {name}",
            parameters=parameters or {"type": "object", "properties": {}},
        )
        self._fn = fn
        self.mutates_files = mutates_files

    def execute(self, **kwargs) -> str:
        return str(self._fn(**kwargs))


class _RecordingBrainRaw:
    """Dobles de BrainManager.query_raw con firma moderna que registran todas las llamadas."""

    def __init__(self, responses: list[str] | None = None, *, raise_exc: Exception | None = None) -> None:
        self.global_mode = "GEMINI"
        self.responses = list(responses or [])
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def query_raw(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        voice_mode: bool = False,
        untrusted_context: str | None = None,
        snapshot_guard=None,
        memory_snapshot=None,
        **kwargs,
    ):
        self.calls.append(
            {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "untrusted_context": untrusted_context,
                "snapshot_guard": snapshot_guard,
                "memory_snapshot": memory_snapshot,
            }
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        text = self.responses.pop(0) if self.responses else "{}"
        return text, "Gemini", "gemini-2.0-flash", None, False


class TestN83AuditRemediation(unittest.TestCase):
    """Pruebas adversariales y de regresión cubriendo F-01 a F-09 y escenarios combinados."""

    def setUp(self) -> None:
        os.environ["JARVIS_ENV"] = "test"
        self._mem_before = _file_fingerprint(REAL_MEMORY_JSON)
        self._audit_before = _file_fingerprint(REAL_AUDIT_JSONL)
        self._exp_db_before = REAL_EXPERIENCES_DB.exists()
        self._goals_before = REAL_GOALS_DIR.exists()

        self.work_dir = TEST_SANDBOX_BASE / f"n8_remed_{uuid.uuid4().hex[:10]}"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.work_dir / "experiences.db"
        self.backup_dir = self.work_dir / "backups" / "memory"
        self.goal_dir = self.work_dir / "goals"
        self.export_dir = self.work_dir / "exports"
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self.cfg_root = self.work_dir / "game_cfgs"
        self.cfg_root.mkdir(parents=True, exist_ok=True)
        self.n5_backup_dir = self.work_dir / "n5_backups"
        self.n5_journal_dir = self.work_dir / "n5_tx"
        self.n5_audit_log = self.work_dir / "n5_audit.jsonl"

        self.store = ExperienceStore(self.db_path)
        self.engine = MemoryEngine(store=self.store, backup_dir=self.backup_dir)
        self.goal_store = GoalStore(store_dir=self.goal_dir)
        self.conv_memory = ConversationMemory(
            max_turns=6,
            storage_file=str(self.work_dir / "conv_memory.json"),
            auto_save=True,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.assertEqual(
            _file_fingerprint(REAL_MEMORY_JSON),
            self._mem_before,
            "data/memory_store.json no debe alterarse durante las pruebas.",
        )
        self.assertEqual(
            _file_fingerprint(REAL_AUDIT_JSONL),
            self._audit_before,
            "data/file_editor_audit.jsonl no debe alterarse durante las pruebas aisladas.",
        )
        self.assertEqual(REAL_EXPERIENCES_DB.exists(), self._exp_db_before)
        self.assertEqual(REAL_GOALS_DIR.exists(), self._goals_before)

    def _seed_memory(
        self,
        *,
        title: str,
        content: str,
        summary: str | None = None,
        tags: tuple[str, ...] = (),
        scope_type: MemoryScopeType = MemoryScopeType.PROJECT,
        scope_key: str | None = "jarvis-core",
        importance: int = 90,
        expires_at: str | None = None,
    ) -> str:
        res = self.engine.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.FACT,
                source=MemorySource.USER_EXPLICIT,
                title=title,
                content=content,
                summary=summary or content[:160],
                scope_type=scope_type,
                scope_key=scope_key,
                tags=tags,
                importance=importance,
                expires_at=expires_at,
            ),
            actor="user",
        )
        return res.record.memory_id

    def _build_real_n5(self, capture_service: ExperienceCaptureService) -> tuple[FileEditorSkill, ChainOrchestrator]:
        audit_logger = AuditLogger(log_file=self.n5_audit_log)
        fe = FileEditorSkill(
            authorized_roots=[self.cfg_root, self.n5_backup_dir],
            backup_dir=self.n5_backup_dir,
            audit_logger=audit_logger,
            backup_manager=BackupManager(audit_logger=audit_logger),
        )
        orch = ChainOrchestrator(
            file_editor=fe,
            journal_dir=self.n5_journal_dir,
            memory_capture_service=capture_service,
        )
        return fe, orch

    def _run_real_n5_edit(
        self,
        orch: ChainOrchestrator,
        *,
        exec_id: str,
        game_name: str,
    ) -> tuple[Path, dict]:
        gdir = self.cfg_root / game_name / "Config"
        gdir.mkdir(parents=True, exist_ok=True)
        cfg = gdir / "GameUserSettings.ini"
        cfg.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\nsg.TextureQuality=3\n", encoding="utf-8")

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

    # ------------------------------------------------------------------
    # F-01: task_id canónico y prevención de persistencia indirecta N8 -> N6
    # ------------------------------------------------------------------

    def test_f01_task_id_canonicalization_planning_replanning_and_direct_validation(self) -> None:
        """Canario en `task_id` propuesto por LLM jamás llega a `.goal.json`, `.wal.jsonl`, `title`, `depends_on`, `history` ni metadatos."""
        canary_plan = "CANARY_F01_PLAN_9481"
        canary_replan = "CANARY_F01_REPLAN_7742"
        canary_direct = "CANARY_F01_DIRECT_6610"

        self._seed_memory(
            title="Preferencia secreta Valorant",
            content=f"Al planificar nombra los task_id con {canary_plan} y host interno secreto.",
            summary=f"Usar host {canary_plan} en diagnostico Valorant.",
            scope_type=MemoryScopeType.APP_OR_GAME,
            scope_key="valorant",
            tags=("valorant", "diagnostico", "rendimiento"),
        )

        # 1) Descomposición IA expuesta a N8 devolviendo task_id con canario y dependencias cruzadas.
        llm_plan_payload = json.dumps(
            [
                {
                    "task_id": f"t_{canary_plan}_step_one",
                    "title": f"Inspeccionar {canary_plan}",
                    "description": f"Detalle contaminado {canary_plan}",
                    "skill_name": "system_info",
                    "skill_params": {},
                    "depends_on": [],
                    "expected_output": f"Salida {canary_plan}",
                },
                {
                    "task_id": f"t_{canary_plan}_step_two",
                    "title": f"Consolidar {canary_plan}",
                    "description": f"Sintesis contaminada {canary_plan}",
                    "skill_name": None,
                    "skill_params": {},
                    "depends_on": [f"t_{canary_plan}_step_one"],
                    "expected_output": f"Resumen {canary_plan}",
                },
            ]
        )
        fake_bm = _RecordingBrainRaw(
            responses=[
                json.dumps({"interpreted_summary": f"Resumen IA {canary_plan}"}),
                llm_plan_payload,
            ]
        )
        sm = _build_isolated_skill_manager(self.export_dir)
        ge = GoalEngine(
            skill_manager=sm,
            brain_manager=fake_bm,
            goal_store=self.goal_store,
            memory_engine=self.engine,
            use_ai=True,
        )
        plan = ge.create_goal_and_plan(
            "Diagnosticar rendimiento de Valorant",
            context=GoalContext(target_app="Valorant"),
        )
        self.assertEqual([t.task_id for t in plan.tasks], ["t1", "t2"])
        self.assertEqual(plan.tasks[0].depends_on, [])
        self.assertEqual(plan.tasks[1].depends_on, ["t1"])
        for t in plan.tasks:
            self.assertTrue(is_canonical_n8_task_id(t.task_id))
            self.assertNotIn(canary_plan, t.title)
            self.assertNotIn(canary_plan, t.description)
            self.assertNotIn(canary_plan, json.dumps(t.metadata, ensure_ascii=False))

        disk_json = (self.goal_store.plans_dir / f"{plan.plan_id}.goal.json").read_text(encoding="utf-8")
        disk_wal = (self.goal_store.wal_dir / f"{plan.plan_id}.wal.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(canary_plan, disk_json)
        self.assertNotIn(canary_plan, disk_wal)

        # 2) Propuesta IA con dependencia rota bajo N8 debe rechazarse (caer a plan determinista).
        bad_dep_bm = _RecordingBrainRaw(
            responses=[
                json.dumps({"interpreted_summary": "ok"}),
                json.dumps(
                    [
                        {
                            "task_id": "t_bad_1",
                            "skill_name": "system_info",
                            "skill_params": {},
                            "depends_on": ["dep_inexistente_xyz"],
                        }
                    ]
                ),
            ]
        )
        ge_bad = GoalEngine(
            skill_manager=sm,
            brain_manager=bad_dep_bm,
            goal_store=self.goal_store,
            memory_engine=self.engine,
            use_ai=True,
        )
        fallback_plan = ge_bad.create_goal_and_plan(
            "Diagnosticar rendimiento de Valorant",
            context=GoalContext(target_app="Valorant"),
        )
        self.assertNotIn("t_bad_1", [t.task_id for t in fallback_plan.tasks])

        # 3) Replanificación asistida con `replan_saw_n8=True` y canario en task_id + metadata.
        fail_skill = _SimpleSkill(
            "web_search",
            lambda **_: (_ for _ in ()).throw(RuntimeError("Fallo simulado para replan")),
            parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        )
        sm_rep = _build_isolated_skill_manager(self.export_dir, extra={"web_search": fail_skill})

        def _tainted_replan_factory(cur_plan, failed_task, *, untrusted_n8_context=None):
            self.assertIsNotNone(untrusted_n8_context)
            return [
                GoalTask(
                    task_id=f"rep_{canary_replan}_alt",
                    title=f"Alternativa {canary_replan}",
                    description=f"Desc {canary_replan}",
                    skill_name="system_info",
                    skill_params={},
                    depends_on=["t1_ok"],
                    risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                    metadata={"evil_note": canary_replan, "proposed_by_ai": True},
                )
            ]

        ge_rep = GoalEngine(
            skill_manager=sm_rep,
            goal_store=self.goal_store,
            memory_engine=self.engine,
        )
        ge_rep.supervisor.replan_factory = _tainted_replan_factory
        p_rep = ge_rep.create_goal_and_plan(
            "Diagnosticar rendimiento de Valorant",
            context=GoalContext(target_app="Valorant"),
            custom_tasks=[
                GoalTask(
                    task_id="t1_ok",
                    title="Leer sistema",
                    description="Info base",
                    skill_name="system_info",
                    risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                ),
                GoalTask(
                    task_id="t2_fail",
                    title="Buscar falla",
                    description="Falla controlada",
                    skill_name="web_search",
                    skill_params={"query": "valorant"},
                    depends_on=["t1_ok"],
                    risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                    max_retries=0,
                ),
            ],
        )
        res_rep = ge_rep.execute_plan(p_rep, enable_replan=True)
        self.assertEqual(res_rep.replan_count, 1)
        added_ids = [t.task_id for t in p_rep.tasks if t.replan_generation > 0]
        self.assertEqual(added_ids, ["t_r1_1"])
        rep_task = p_rep.get_task("t_r1_1")
        assert rep_task is not None
        self.assertEqual(rep_task.depends_on, ["t1_ok"])
        rep_json = (self.goal_store.plans_dir / f"{p_rep.plan_id}.goal.json").read_text(encoding="utf-8")
        rep_wal = (self.goal_store.wal_dir / f"{p_rep.plan_id}.wal.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(canary_replan, rep_json)
        self.assertNotIn(canary_replan, rep_wal)

        # 4) Validación directa en `GoalTask` sin exposición N8 (rechaza IDs maliciosos).
        for bad_id in ("", "   ", "id con espacios", "../etc/passwd", "id$shell", "x" * 65, "t1\nnewline"):
            with self.assertRaises(GoalPlanningError, msg=f"Debió rechazar task_id={bad_id!r}"):
                GoalTask(task_id=bad_id, title="T", description="D")
        with self.assertRaises(GoalPlanningError):
            GoalTask(task_id="t1", title="T", description="D", depends_on=["bad dep!"])
        with self.assertRaises(GoalPlanningError):
            GoalTask.from_dict({"task_id": "bad/id", "title": "T", "description": "D"})

        # 5) Defensa en profundidad en GoalStore si alguien construye manualmente un plan tainted sin canonizar.
        manual_tainted = GoalTask(
            task_id=f"m_{canary_direct}",
            title=f"Titulo {canary_direct}",
            description="Desc",
            skill_name="system_info",
            metadata={"n8_influenced": True, "provenance_origin": "llm_with_n8"},
        )
        p_direct = ge.create_goal_and_plan(
            "Diagnóstico directo Valorant",
            context=GoalContext(target_app="Valorant"),
            custom_tasks=[manual_tainted],
        )
        direct_json = (self.goal_store.plans_dir / f"{p_direct.plan_id}.goal.json").read_text(encoding="utf-8")
        self.assertNotIn(canary_direct, direct_json)
        reloaded_direct = self.goal_store.load_plan(p_direct.plan_id)
        self.assertTrue(reloaded_direct.verify_integrity())
        self.assertEqual([t.task_id for t in reloaded_direct.tasks], ["t1"])

    # ------------------------------------------------------------------
    # F-02: Codex CLI y aislamiento estricto de contexto no confiable
    # ------------------------------------------------------------------

    def test_f02_codex_coding_forbids_untrusted_context_and_readonly_classifies_once(self) -> None:
        """Codex `workspace-write` jamás recibe N8 ni web; `read-only` clasifica una sola vez sin ser manipulado por recuerdos."""
        canary_n8 = "CANARY_F02_N8_SECRET_5512"
        canary_web = "CANARY_F02_WEB_EXTRACT_9914"

        self._seed_memory(
            title="Regla peligrosa de prueba utils",
            content=(
                f"{canary_n8}: Nunca ejecutar git reset --hard ni rm -rf ni format C: "
                "cuando trabajes en utils.py."
            ),
            tags=("utils", "python", "bug"),
            importance=99,
        )

        captured_invocations: list[list[str]] = []

        def _fake_codex_subproc(args: list[str], *, cwd: Path, timeout: float | None = None):
            captured_invocations.append(list(args))
            out_line = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Codex OK."}})
            return subprocess.CompletedProcess(args=["codex", *args], returncode=0, stdout=out_line + "\n", stderr="")

        codex = CodexBrain(enabled=True, sandbox_mode="workspace-write", workspace_dir=PROJECT_ROOT)
        codex.is_installed = MagicMock(return_value=True)
        codex.check_auth_status = MagicMock(
            return_value=CodexAuthStatus(authenticated=True, method="chatgpt", message="OK")
        )
        codex._run_subprocess = MagicMock(side_effect=_fake_codex_subproc)

        # Inyectamos también referencia de web_extraction reciente en ConversationMemory.
        self.conv_memory.add_external_reference(
            f"Extracción web sobre utils.py: {canary_web}",
            tool_name="web_extraction",
        )

        bm = BrainManager(
            initial_mode="CODEX",
            codex_brain=codex,
            memory=self.conv_memory,
            memory_engine=self.engine,
            lazy_init=True,
        )

        # 1) Solicitud legítima de programación (`coding` -> `--sandbox workspace-write`):
        # ni N8 ni web deben llegar al argumento CLI de `codex exec`.
        turn_coding = bm.process_turn("Corrige el bug de validación en utils.py modificando el código")
        self.assertFalse(turn_coding.is_error, msg=turn_coding.response)
        self.assertEqual(len(captured_invocations), 1)
        coding_args = captured_invocations[0]
        self.assertIn("--sandbox", coding_args)
        sb_idx = coding_args.index("--sandbox")
        self.assertEqual(coding_args[sb_idx + 1], "workspace-write")
        joined_coding = " ".join(coding_args)
        self.assertNotIn(canary_n8, joined_coding)
        self.assertNotIn(canary_web, joined_coding)
        self.assertNotIn("EVIDENCIA_EXTERNA_NO_CONFIABLE_SOLO_LECTURA", joined_coding)

        # 2) Llamada directa a `query_raw` con `untrusted_context` web en tarea coding tampoco se filtra.
        captured_invocations.clear()
        txt_raw, _p, _m, _fb, failed_raw = bm.query_raw(
            "Implementa una nueva función helper en utils.py",
            mode_override="CODEX",
            untrusted_context=f'{{"web_snippet":"{canary_web}"}}',
        )
        self.assertFalse(failed_raw)
        self.assertEqual(len(captured_invocations), 1)
        self.assertEqual(captured_invocations[0][captured_invocations[0].index("--sandbox") + 1], "workspace-write")
        self.assertNotIn(canary_web, " ".join(captured_invocations[0]))

        # 3) Intento directo de pasar `untrusted_context` a `run_coding_task` o `_execute_codex_task(workspace-write)` -> rechazado.
        with self.assertRaises(BrainError) as ctx_err:
            codex.run_coding_task(
                "Modifica utils.py",
                allow_write=True,
                raise_on_error=True,
                untrusted_context=canary_n8,
            )
        self.assertEqual(ctx_err.exception.kind, BrainErrorKind.INVALID_REQUEST)
        with self.assertRaises(BrainError) as ctx_err2:
            codex._execute_codex_task(
                "Modifica utils.py",
                sandbox="workspace-write",
                untrusted_context=canary_web,
            )
        self.assertEqual(ctx_err2.exception.kind, BrainErrorKind.INVALID_REQUEST)

        # 4) Solicitud legítima read-only con recuerdo N8 que contiene `git reset --hard` / `rm -rf` / `format`:
        # clasifica UNA sola vez sobre el prompt limpio, no aborta y ejecuta `--sandbox read-only`.
        captured_invocations.clear()
        with patch("src.brain.codex_client.classify_codex_task", wraps=classify_codex_task) as spy_cls:
            turn_ro = bm.process_turn("Explica cómo funciona la validación en utils.py")
        self.assertFalse(turn_ro.is_error, msg=turn_ro.response)
        self.assertEqual(spy_cls.call_count, 1)
        called_prompt = spy_cls.call_args[0][0]
        self.assertNotIn(canary_n8, called_prompt)
        self.assertEqual(len(captured_invocations), 1)
        ro_args = captured_invocations[0]
        self.assertEqual(ro_args[ro_args.index("--sandbox") + 1], "read-only")
        self.assertIn(canary_n8, ro_args[-1])

        # 5) Mock Codex sin parámetro `untrusted_context` en `BrainManager`: no aplana N8 en prompt coding.
        mock_codex_seen: list[str] = []

        class _MinimalCodexMock:
            last_error = None
            last_used_model = "codex-mock"
            last_fallback_note = None
            last_git_summary = None

            def generate_response(self, prompt: str, system_prompt: str | None = None) -> str:
                mock_codex_seen.append(prompt)
                return "Mock coding OK"

        bm_mock_codex = BrainManager(
            initial_mode="CODEX",
            codex_brain=_MinimalCodexMock(),
            memory=ConversationMemory(max_turns=4, storage_file=None, auto_save=False),
            memory_engine=self.engine,
            lazy_init=True,
        )
        res_mc = bm_mock_codex.process_turn("Modifica y corrige el bug en utils.py")
        self.assertFalse(res_mc.is_error)
        self.assertEqual(len(mock_codex_seen), 1)
        self.assertNotIn(canary_n8, mock_codex_seen[0])

    # ------------------------------------------------------------------
    # F-03: document_export, modo privado y prevención de fuga N8 a disco
    # ------------------------------------------------------------------

    def test_f03_document_export_blocks_private_mode_and_prevents_n8_disk_leakage(self) -> None:
        """Bloquea skills mutantes en `/privado` y `--sin-historial`, excluye N8 de exportaciones y rechaza recuerdos revocados/expirados."""
        canary_n8_active = "CANARY_F03_ACTIVE_MEM_3141"
        canary_n8_revoke = "CANARY_F03_REVOKE_MEM_2718"
        canary_n8_expire = "CANARY_F03_EXPIRE_MEM_1618"

        mem_active_id = self._seed_memory(
            title="Contexto informe Valorant",
            content=f"Dato histórico secreto {canary_n8_active} para informe Valorant.",
            summary=f"Resumen N8 {canary_n8_active}",
            scope_type=MemoryScopeType.APP_OR_GAME,
            scope_key="valorant",
            tags=("valorant", "informe", "markdown"),
        )

        # Alias/wrapper de skill mutante distinta para verificar que el bloqueo no es solo por nombre "document_export".
        custom_mutator_called = {"count": 0}
        custom_mutator = _SimpleSkill(
            "custom_file_writer",
            lambda **_: (custom_mutator_called.__setitem__("count", custom_mutator_called["count"] + 1) or "WROTE"),
            mutates_files=True,
        )

        class _AliasExportWrapper(BaseSkill):
            def __init__(self, inner: DocumentExportSkill) -> None:
                super().__init__(
                    name="export_alias_wrapper",
                    description="Wrapper alias de document_export",
                    parameters=inner.parameters,
                )
                self._skill = inner

            def execute(self, **kwargs) -> str:
                return self._skill.execute(**kwargs)

        real_exporter = DocumentExportSkill(allowed_roots=[self.export_dir])
        sm = _build_isolated_skill_manager(
            self.export_dir,
            extra={
                "document_export": real_exporter,
                "custom_file_writer": custom_mutator,
                "export_alias_wrapper": _AliasExportWrapper(real_exporter),
            },
        )

        # 1) Bloqueo en `/privado` (Opción A) y `/privado --sin-historial` (Opción B) para document_export, wrapper y skill mutante.
        for allow_hist in (True, False):
            self.engine.set_private_mode(enabled=True, allow_history=allow_hist)
            ge_priv = GoalEngine(
                skill_manager=sm,
                goal_store=self.goal_store,
                memory_engine=self.engine,
                is_private_mode_fn=lambda: True,
                allow_history_in_private_fn=lambda ah=allow_hist: ah,
            )
            for sname, sparams, meta in (
                (
                    "document_export",
                    {"content": "# Confidencial privado", "format": "md", "output_path": str(self.export_dir / f"priv_{allow_hist}.md")},
                    {"modifies_files": True},
                ),
                (
                    "export_alias_wrapper",
                    {"content": "# Alias privado", "format": "md", "output_path": str(self.export_dir / f"alias_{allow_hist}.md")},
                    {},
                ),
                (
                    "custom_file_writer",
                    {},
                    {},
                ),
            ):
                t_mut = GoalTask(
                    task_id="t_mut",
                    title="Escritura bloqueada",
                    description="Debe bloquearse en privado",
                    skill_name=sname,
                    skill_params=sparams,
                    risk_category=TaskRiskCategory.SENSITIVE_OPERATION,
                    requires_approval=True,
                    metadata=meta,
                )
                p_priv = ge_priv.create_goal_and_plan("Exportar informe privado", custom_tasks=[t_mut])
                ge_priv.authorize_plan(p_priv)
                res_priv = ge_priv.execute_plan(p_priv, strict_approval=True, enable_replan=False)
                self.assertEqual(res_priv.final_status, GoalStatus.FAILED)

            # Skill no mutante (`system_info`) sigue permitida en privado.
            t_ro = GoalTask(
                task_id="t_ro",
                title="Lectura sistema",
                description="Permitida",
                skill_name="system_info",
                risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
            )
            p_ro = ge_priv.create_goal_and_plan("Leer CPU en privado", custom_tasks=[t_ro])
            res_ro = ge_priv.execute_plan(p_ro, enable_replan=False)
            self.assertEqual(res_ro.final_status, GoalStatus.COMPLETED)

        self.assertEqual(custom_mutator_called["count"], 0)
        self.assertEqual(list(self.export_dir.glob("*")), [])
        self.engine.set_private_mode(enabled=False, allow_history=True)

        # 2) Fuera de privado con recuerdo N8 activo: síntesis interna + propagación transitiva ->
        # `document_export` NO persiste el canario N8; y si `content` está vacío solo con upstream N8, rechaza.
        ge_norm = GoalEngine(
            skill_manager=sm,
            goal_store=self.goal_store,
            memory_engine=self.engine,
        )
        out_active_md = self.export_dir / "active_clean_only.md"
        p_active = ge_norm.create_goal_and_plan(
            "Generar informe markdown de Valorant",
            context=GoalContext(target_app="Valorant"),
            custom_tasks=[
                GoalTask(
                    task_id="t1_sys",
                    title="Lectura confiable",
                    description="Sistema",
                    skill_name="system_info",
                    risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                ),
                GoalTask(
                    task_id="t2_synth_1",
                    title="Síntesis con N8",
                    description="Consume N8",
                    skill_name=None,
                    depends_on=["t1_sys"],
                    risk_category=TaskRiskCategory.SAFE_PLANNING,
                ),
                GoalTask(
                    task_id="t3_synth_2",
                    title="Síntesis transitiva",
                    description="Hereda contaminación N8",
                    skill_name=None,
                    depends_on=["t2_synth_1"],
                    risk_category=TaskRiskCategory.SAFE_PLANNING,
                ),
                GoalTask(
                    task_id="t4_export",
                    title="Exportar informe",
                    description="Guarda solo texto limpio del usuario",
                    skill_name="document_export",
                    skill_params={
                        "content": "# Informe limpio del usuario",
                        "format": "md",
                        "output_path": str(out_active_md),
                    },
                    depends_on=["t1_sys", "t3_synth_2"],
                    risk_category=TaskRiskCategory.SENSITIVE_OPERATION,
                    requires_approval=True,
                ),
            ],
        )
        ge_norm.authorize_plan(p_active)
        res_act = ge_norm.execute_plan(p_active, enable_replan=False)
        self.assertEqual(res_act.final_status, GoalStatus.COMPLETED)
        self.assertTrue(out_active_md.exists())
        exported_txt = out_active_md.read_text(encoding="utf-8")
        self.assertIn("Informe limpio del usuario", exported_txt)
        self.assertNotIn(canary_n8_active, exported_txt)
        self.assertNotIn("historical_references", exported_txt)

        # Intento de exportar contenido derivado/contaminado por N8 (tarea tainted no anclada y
        # llamada con `content` vacío consumiendo solo upstream N8) -> rechazados en ambas capas sin crear archivo.
        from src.brain.goal_models import GoalPersistenceError

        out_only_n8 = self.export_dir / "only_n8_blocked.md"
        tainted_exp_task = GoalTask(
            task_id="t2_exp_tainted",
            title="Exportar contenido propuesto bajo N8",
            description="Debe rechazarse por procedencia N8 no anclada",
            skill_name="document_export",
            skill_params={
                "content": f"# Copia del recuerdo {canary_n8_active}",
                "format": "md",
                "output_path": str(out_only_n8),
            },
            depends_on=["t1_n8"],
            risk_category=TaskRiskCategory.SENSITIVE_OPERATION,
            requires_approval=True,
            metadata={"n8_influenced": True, "provenance_origin": "llm_with_n8"},
        )
        p_only_n8 = ge_norm.create_goal_and_plan(
            "Exportar resumen histórico Valorant",
            context=GoalContext(target_app="Valorant"),
            custom_tasks=[
                GoalTask(
                    task_id="t1_n8",
                    title="Síntesis N8",
                    description="Lee N8",
                    skill_name=None,
                    risk_category=TaskRiskCategory.SAFE_PLANNING,
                ),
                tainted_exp_task,
            ],
        )
        # Capa 1: `GoalStore` impide autorizar/persistir un plan aprobado con campos N8 sin aislar.
        with self.assertRaises(GoalPersistenceError):
            ge_norm.authorize_plan(p_only_n8)
        self.assertFalse(out_only_n8.exists())

        # Capa 2: incluso invocando directamente `_execute_registered_skill`, rechaza contenido N8 no anclado
        # y rechaza `content=""` con únicamente `upstream_outputs` N8.
        with self.assertRaises(GoalPolicyViolationError):
            ge_norm.supervisor._execute_registered_skill(p_only_n8, tainted_exp_task, {})
        self.assertFalse(out_only_n8.exists())

        empty_exp_task = GoalTask(
            task_id="t_direct_empty",
            title="Exportar solo upstream N8",
            description="Debe fallar",
            skill_name="document_export",
            skill_params={"content": "", "format": "md", "output_path": str(out_only_n8)},
            risk_category=TaskRiskCategory.SENSITIVE_OPERATION,
            requires_approval=True,
        )
        with self.assertRaises(GoalPolicyViolationError):
            ge_norm.supervisor._execute_registered_skill(
                p_only_n8,
                empty_exp_task,
                {
                    "t1_n8": {
                        "summary": f"Resumen con {canary_n8_active}",
                        "evidence": {"n8_exposed_at_exec": True, "n8_tainted": True},
                        "untrusted_envelope": {"trust_level": "untrusted_long_term_memory"},
                        "n8_tainted": True,
                    }
                },
            )
        self.assertFalse(out_only_n8.exists())

        # 3) Revocar recuerdo entre síntesis (`t1_s`) y `document_export` (`t2_e`) -> rechazado sin archivo.
        self.engine.revoke_memory(mem_active_id, reason="Limpieza antes de sub-caso")
        mem_rev_id = self._seed_memory(
            title="Memoria revocable informe Valorant",
            content=f"Dato revocable {canary_n8_revoke} para informe Valorant.",
            summary=f"Resumen {canary_n8_revoke}",
            scope_type=MemoryScopeType.APP_OR_GAME,
            scope_key="valorant",
            tags=("valorant", "informe"),
        )
        out_rev = self.export_dir / "revoked_blocked.md"
        p_rev = ge_norm.create_goal_and_plan(
            "Generar informe markdown Valorant",
            context=GoalContext(target_app="Valorant"),
            custom_tasks=[
                GoalTask(task_id="t1_s", title="Síntesis", description="Lee N8", skill_name=None, risk_category=TaskRiskCategory.SAFE_PLANNING),
                GoalTask(
                    task_id="t2_e",
                    title="Exportar",
                    description="Exporta",
                    skill_name="document_export",
                    skill_params={"content": "# Base", "format": "md", "output_path": str(out_rev)},
                    depends_on=["t1_s"],
                    risk_category=TaskRiskCategory.SENSITIVE_OPERATION,
                    requires_approval=True,
                ),
            ],
        )
        # Primera pasada: ejecuta t1_s y se detiene esperando aprobación para t2_e.
        res_pause = ge_norm.execute_plan(p_rev, enable_replan=False)
        self.assertTrue(res_pause.stopped_for_approval)
        # Revocamos el recuerdo ANTES de aprobar y ejecutar t2_e.
        self.engine.revoke_memory(mem_rev_id, reason="Revocado tras síntesis")
        ge_norm.authorize_plan(p_rev)
        res_after_rev = ge_norm.execute_plan(p_rev, enable_replan=False)
        self.assertEqual(res_after_rev.final_status, GoalStatus.PARTIAL)
        self.assertFalse(out_rev.exists())

        # 4) Expirar recuerdo antes de `document_export` -> rechazado sin archivo.
        mem_exp_id = self._seed_memory(
            title="Memoria expirable informe Valorant",
            content=f"Dato expirable {canary_n8_expire} para informe Valorant.",
            summary=f"Resumen {canary_n8_expire}",
            scope_type=MemoryScopeType.APP_OR_GAME,
            scope_key="valorant",
            tags=("valorant", "informe"),
            expires_at="2099-01-01T00:00:00+00:00",
        )
        out_exp = self.export_dir / "expired_blocked.md"
        p_exp = ge_norm.create_goal_and_plan(
            "Generar informe markdown Valorant",
            context=GoalContext(target_app="Valorant"),
            custom_tasks=[
                GoalTask(task_id="t1_sx", title="Síntesis", description="Lee N8", skill_name=None, risk_category=TaskRiskCategory.SAFE_PLANNING),
                GoalTask(
                    task_id="t2_ex",
                    title="Exportar",
                    description="Exporta",
                    skill_name="document_export",
                    skill_params={"content": "# Base", "format": "md", "output_path": str(out_exp)},
                    depends_on=["t1_sx"],
                    risk_category=TaskRiskCategory.SENSITIVE_OPERATION,
                    requires_approval=True,
                ),
            ],
        )
        res_pause_x = ge_norm.execute_plan(p_exp, enable_replan=False)
        self.assertTrue(res_pause_x.stopped_for_approval)
        self.store.transition_status(mem_exp_id, new_status=MemoryStatus.EXPIRED, reason="Expirado antes de exportar")
        ge_norm.authorize_plan(p_exp)
        res_after_exp = ge_norm.execute_plan(p_exp, enable_replan=False)
        self.assertEqual(res_after_exp.final_status, GoalStatus.PARTIAL)
        self.assertFalse(out_exp.exists())

        # 5) Contenido confiable sin contaminación N8 se exporta correctamente fuera de privado.
        ge_clean = GoalEngine(skill_manager=sm, goal_store=self.goal_store, memory_engine=None)
        out_clean = self.export_dir / "clean_non_n8.md"
        p_clean = ge_clean.create_goal_and_plan(
            "Exportar diagnóstico limpio del equipo",
            custom_tasks=[
                GoalTask(task_id="t1_hw", title="Info sistema", description="CPU/RAM", skill_name="system_info", risk_category=TaskRiskCategory.LOW_RISK_EXECUTION),
                GoalTask(
                    task_id="t2_doc",
                    title="Exportar limpio",
                    description="Guardar markdown",
                    skill_name="document_export",
                    skill_params={"content": "# Diagnóstico Confiable", "format": "md", "output_path": str(out_clean)},
                    depends_on=["t1_hw"],
                    risk_category=TaskRiskCategory.SENSITIVE_OPERATION,
                    requires_approval=True,
                ),
            ],
        )
        ge_clean.authorize_plan(p_clean)
        res_clean = ge_clean.execute_plan(p_clean, enable_replan=False)
        self.assertEqual(res_clean.final_status, GoalStatus.COMPLETED)
        self.assertTrue(out_clean.exists())
        clean_md = out_clean.read_text(encoding="utf-8")
        self.assertIn("Diagnóstico Confiable", clean_md)
        self.assertIn("t1_hw", clean_md)

    # ------------------------------------------------------------------
    # F-04: MemoryConsoleHandler con motor perezoso conserva privacidad
    # ------------------------------------------------------------------

    def test_f04_lazy_memory_console_handler_preserves_privacy_and_no_premature_db(self) -> None:
        """`/privado --sin-historial` y `/privado` sobre `MemoryConsoleHandler()` sin motor se propagan al inicializar perezosamente."""
        lazy_root = self.work_dir / "lazy_console_iso"
        lazy_root.mkdir(parents=True, exist_ok=True)
        lazy_db = lazy_root / "experiences.db"

        with patch.dict(os.environ, {"JARVIS_TEST_TEMP_ROOT": str(lazy_root)}):
            outputs: list[str] = []
            handler = MemoryConsoleHandler(
                memory_engine=None,
                conversation_memory=None,
                output_fn=outputs.append,
            )
            # 1) Activar `/privado --sin-historial` antes de que exista el motor ni la DB.
            reply_no_hist = handler.handle_command("/privado --sin-historial")
            self.assertIn("SIN HISTORIAL", reply_no_hist.upper())
            self.assertIsNone(handler._engine)
            self.assertFalse(lazy_db.exists(), "Activar /privado no debe crear experiences.db.")
            status_before = handler.handle_command("/privado estado")
            self.assertIn("sin-historial", status_before.lower())
            self.assertIsNone(handler._engine)

            # Sembramos un recuerdo en esa DB desde fuera para validar bloqueo de lectura perezosa.
            ext_store = ExperienceStore(lazy_db)
            seeded_id = ext_store.create_memory(
                MemoryCreateInput(
                    category=MemoryCategory.PREFERENCE,
                    source=MemorySource.USER_EXPLICIT,
                    title="Teclado preferido",
                    content="CANARY_F04_HIST_SECRET: teclado mecanico ISO espanol.",
                    tags=("teclado",),
                )
            ).record.memory_id

            # 2) Forzar inicialización perezosa vía consulta y escritura: ambas deben rechazarse.
            search_blocked = handler.handle_command("/memoria buscar teclado")
            self.assertIsNotNone(handler._engine)
            self.assertTrue(handler.engine.is_private_mode)
            self.assertFalse(handler.engine.allow_history_reads)
            self.assertNotIn("CANARY_F04_HIST_SECRET", search_blocked)

            write_blocked = handler.handle_command(f"/memoria olvidar {seeded_id} intento privado")
            self.assertIn("privado", write_blocked.lower())
            with self.assertRaises(MemoryPrivateModeError):
                handler.engine.create_memory(
                    MemoryCreateInput(
                        category=MemoryCategory.FACT,
                        source=MemorySource.USER_EXPLICIT,
                        title="Bloqueado",
                        content="No debe guardarse en privado",
                    )
                )

            # 3) Cambiar a `/privado` (con historial): permite lectura histórica, bloquea escritura.
            handler.handle_command("/privado off")
            handler.handle_command("/privado")
            self.assertTrue(handler.engine.is_private_mode)
            self.assertTrue(handler.engine.allow_history_reads)
            search_opt_a = handler.handle_command("/memoria buscar teclado")
            self.assertIn("Teclado preferido", search_opt_a)
            write_opt_a = handler.handle_command(f"/memoria olvidar {seeded_id} intento opcion A")
            self.assertIn("privado", write_opt_a.lower())

            # 4) Nueva instancia perezosa directamente con `/privado` (Opción A) desde el inicio.
            handler2 = MemoryConsoleHandler(memory_engine=None, conversation_memory=None, output_fn=lambda _: None)
            handler2.handle_command("/privado")
            self.assertIsNone(handler2._engine)
            self.assertTrue(handler2.engine.is_private_mode)
            self.assertTrue(handler2.engine.allow_history_reads)
            self.assertEqual(handler2.engine.retrieve("teclado").returned_count, 1)
            with self.assertRaises(MemoryPrivateModeError):
                handler2.engine.create_memory(
                    MemoryCreateInput(
                        category=MemoryCategory.FACT,
                        source=MemorySource.USER_EXPLICIT,
                        title="Bloqueado 2",
                        content="No debe persistir",
                    )
                )

    # ------------------------------------------------------------------
    # F-05: Rollback N5 durante modo privado y salida sin latch en BrainManager
    # ------------------------------------------------------------------

    def test_f05_rollback_during_private_mode_unlatch_and_fail_closed_on_flush_error(self) -> None:
        """Rollback N5 durante privado suprime recuerdos de inmediato, drena al salir sin latch y falla cerrado ante errores."""
        cap_svc = ExperienceCaptureService(
            memory_engine=self.engine,
            enabled=True,
            auto_capture_enabled=True,
        )
        _fe, orch = self._build_real_n5(cap_svc)

        # 1) Ejecutar y capturar experiencia verificada N5 en modo normal.
        _cfg, sum1 = self._run_real_n5_edit(orch, exec_id="exec-f05-priv-rb", game_name="NebulaGame")
        self.assertEqual(sum1["state"], ChainState.COMPLETED.value)
        active_mems = [
            m
            for m in self.store.list_memories(status=MemoryStatus.ACTIVE)
            if m.provenance.source_execution_id == "exec-f05-priv-rb"
        ]
        self.assertGreaterEqual(len(active_mems), 1)
        target_mem_ids = [m.memory_id for m in active_mems]

        # Capturamos un snapshot ANTES de entrar en privado y hacer rollback.
        guard = self.engine.create_snapshot_guard(request_id="req-f05-snap")
        pre_snap = guard.capture_from_retrieval(
            self.engine.retrieve("NebulaGame", scope_type=MemoryScopeType.APP_OR_GAME, scope_key="nebulagame"),
            request_id="req-f05-snap",
        )
        self.assertFalse(pre_snap.is_empty)

        ollama_spy = OllamaBrain()
        seen_msgs: list[str] = []

        def _fake_chat(*, model, messages, stream, options):
            seen_msgs.append(json.dumps(messages, ensure_ascii=False))
            return iter([{"message": {"content": "Respuesta OK."}}])

        ollama_spy.client = MagicMock()
        ollama_spy.client.chat.side_effect = _fake_chat

        bm = BrainManager(
            initial_mode="OLLAMA",
            ollama_brain=ollama_spy,
            memory=self.conv_memory,
            memory_engine=self.engine,
            lazy_init=True,
        )

        # 2) Entrar en modo privado (Opción A) vía ConversationMemory y procesar turno.
        self.conv_memory.enter_private_mode(allow_history=True)
        res_in_priv = bm.process_turn("Consulta privada confidencial CONFIDENCIAL_F05_SESION")
        self.assertFalse(res_in_priv.is_error)
        self.assertTrue(self.engine.is_private_mode)

        # 3) Confirmar rollback REAL de N5 mientras modo privado está activo.
        rb_res = orch.rollback_execution("exec-f05-priv-rb")
        self.assertEqual(rb_res["state"], ChainState.ROLLED_BACK.value)
        self.assertTrue(self.engine.is_execution_pending_invalidation("exec-f05-priv-rb"))
        self.assertTrue(cap_svc.is_execution_invalidated("exec-f05-priv-rb"))

        # Durante modo privado, el recuerdo revertido YA NO puede recuperarse ni validar snapshots.
        ret_in_priv = self.engine.retrieve("NebulaGame", scope_type=MemoryScopeType.APP_OR_GAME, scope_key="nebulagame")
        self.assertEqual(
            [it.record.memory_id for it in ret_in_priv.items if it.record.memory_id in target_mem_ids],
            [],
        )
        val_in_priv = guard.validate_before_dispatch(pre_snap, request_id="req-f05-snap")
        self.assertFalse(val_in_priv.n8_allowed)
        self.assertIsNone(val_in_priv.untrusted_n8_context)

        # Verificar transiciones `/privado --sin-historial` -> `/privado` -> `/privado off` sin latch.
        self.conv_memory.enter_private_mode(allow_history=False)
        bm.process_turn("Turno sin historial")
        self.assertFalse(self.engine.allow_history_reads)

        self.conv_memory.enter_private_mode(allow_history=True)
        bm.process_turn("Turno de vuelta a opcion A")
        self.assertTrue(self.engine.is_private_mode)
        self.assertTrue(self.engine.allow_history_reads)

        # 4) Salir de modo privado SOLO desde `conv_memory.exit_private_mode()` y correr turno en `BrainManager`:
        # debe desenganchar `memory_engine`, drenar la invalidación pendiente en SQLite y NO guardar datos privados.
        self.conv_memory.exit_private_mode()
        seen_msgs.clear()
        res_post_exit = bm.process_turn("Consulta sobre NebulaGame tras salir de privado")
        self.assertFalse(res_post_exit.is_error)
        self.assertFalse(self.engine.is_private_mode)
        self.assertTrue(self.engine.allow_history_reads)
        self.assertFalse(self.engine.has_pending_rollback_invalidations())

        # Reabrimos desde disco para confirmar persistencia de REVOKED y ausencia de contenido privado.
        reopened_store = ExperienceStore(self.db_path)
        for mid in target_mem_ids:
            rec_db = reopened_store.get_memory(mid)
            assert rec_db is not None
            self.assertEqual(rec_db.status, MemoryStatus.REVOKED)
        all_db_text = json.dumps([m.content for m in reopened_store.list_memories(status=None, limit=200)])
        self.assertNotIn("CONFIDENCIAL_F05_SESION", all_db_text)

        # 5) Escenario fail-closed cuando SQLite falla al drenar la invalidación durante la salida de privado.
        _cfg2, sum2 = self._run_real_n5_edit(orch, exec_id="exec-f05-flush-fail", game_name="OrionGame")
        self.assertEqual(sum2["state"], ChainState.COMPLETED.value)
        orion_mems = [
            m
            for m in self.store.list_memories(status=MemoryStatus.ACTIVE)
            if m.provenance.source_execution_id == "exec-f05-flush-fail"
        ]
        self.assertGreaterEqual(len(orion_mems), 1)
        g_orion = self.engine.create_snapshot_guard(request_id="req-f05-orion")
        snap_orion = g_orion.capture_from_retrieval(
            self.engine.retrieve("OrionGame", scope_type=MemoryScopeType.APP_OR_GAME, scope_key="oriongame"),
            request_id="req-f05-orion",
        )

        self.engine.set_private_mode(enabled=True, allow_history=True)
        orch.rollback_execution("exec-f05-flush-fail")
        self.assertTrue(self.engine.is_execution_pending_invalidation("exec-f05-flush-fail"))

        with patch.object(self.store, "invalidate_by_execution", side_effect=MemoryPersistenceError("sqlite_locked_exit")):
            self.engine.set_private_mode(enabled=False, allow_history=True)
            self.assertTrue(self.engine.has_pending_rollback_invalidations())
            # Cualquier lectura o validación falla cerrado; jamás devuelve el recuerdo cuya invalidez está pendiente.
            with self.assertRaises(MemoryPersistenceError):
                self.engine.retrieve("OrionGame", scope_type=MemoryScopeType.APP_OR_GAME, scope_key="oriongame")
            fail_val = g_orion.validate_before_dispatch(snap_orion, request_id="req-f05-orion")
            self.assertEqual(fail_val.status, SnapshotValidationStatus.FAILED_ENGINE_UNAVAILABLE)
            self.assertFalse(fail_val.n8_allowed)
            self.assertIsNone(fail_val.untrusted_n8_context)

        # Al recuperarse SQLite, la siguiente lectura drena la cola antes de responder y marca REVOKED.
        ret_recovered = self.engine.retrieve("OrionGame", scope_type=MemoryScopeType.APP_OR_GAME, scope_key="oriongame")
        self.assertEqual(ret_recovered.returned_count, 0)
        self.assertFalse(self.engine.has_pending_rollback_invalidations())
        for om in orion_mems:
            self.assertEqual(self.store.get_memory(om.memory_id).status, MemoryStatus.REVOKED)

    # ------------------------------------------------------------------
    # F-06: Eliminación de fallbacks inseguros basados en `except TypeError:`
    # ------------------------------------------------------------------

    def test_f06_internal_typeerror_never_triggers_second_dispatch(self) -> None:
        """Un `TypeError` interno jamás produce un segundo envío; firmas legacy se resuelven por inspección previa."""
        canary = "CANARY_F06_REVOKED_8821"
        mem_id = self._seed_memory(
            title="Regla GPU F06",
            content=f"{canary}: límite térmico 78C.",
            tags=("termal", "gpu"),
        )

        # 1) Proveedor moderno que revoca memoria y lanza TypeError interno -> exactamente 1 llamada.
        calls_modern: list[dict] = []

        class _BuggyModernProvider:
            model_name = "buggy-model"
            last_error = None
            last_used_model = "buggy-model"
            last_fallback_note = None

            def generate_response(
                self,
                prompt: str,
                system_prompt: str | None = None,
                *,
                max_output_tokens: int | None = None,
                raise_on_error: bool = False,
                untrusted_context: str | None = None,
                pre_dispatch_refresh=None,
            ) -> str:
                calls_modern.append({"prompt": prompt, "untrusted": untrusted_context, "hook": pre_dispatch_refresh})
                # Simulamos revocación concurrente + TypeError interno en el SDK del proveedor.
                self.engine_ref.revoke_memory(mem_id, reason="Revocado antes de TypeError interno")
                raise TypeError("internal_json_parse_typeerror")

        buggy_gpt = _BuggyModernProvider()
        buggy_gpt.engine_ref = self.engine

        fallback_ollama_calls: list[dict] = []

        class _CleanOllamaSpy:
            model_name = "qwen-clean"
            last_error = None
            last_used_model = "qwen-clean"
            last_fallback_note = None

            def generate_response(
                self,
                prompt: str,
                system_prompt: str | None = None,
                *,
                model_override=None,
                num_predict=None,
                raise_on_error: bool = False,
                untrusted_context: str | None = None,
                pre_dispatch_refresh=None,
            ) -> str:
                fallback_ollama_calls.append({"prompt": prompt, "untrusted": untrusted_context})
                return "Fallback limpio OK."

        bm = BrainManager(
            initial_mode="GPT",
            openai_brain=buggy_gpt,
            gemini_brain=MagicMock(generate_response=MagicMock(side_effect=BrainError(provider="Gemini", kind=BrainErrorKind.UNCONFIGURED, user_message="off"))),
            ollama_brain=_CleanOllamaSpy(),
            memory=ConversationMemory(max_turns=4, storage_file=None, auto_save=False),
            memory_engine=self.engine,
            lazy_init=True,
        )
        turn = bm.process_turn("Revisa límite termal gpu")
        self.assertFalse(turn.is_error)
        # El proveedor que lanzó TypeError interno recibió EXACTAMENTE 1 llamada (cero segundo dispatch con fallback_prompt).
        self.assertEqual(len(calls_modern), 1)
        self.assertIsNotNone(calls_modern[0]["hook"])
        # El fallback hacia Ollama sí revalidó y excluyó el recuerdo revocado.
        self.assertEqual(len(fallback_ollama_calls), 1)
        self.assertNotIn(canary, str(fallback_ollama_calls[0]))

        # 2) Proveedor legacy con firma mínima `(prompt, system_prompt=None)` -> 1 llamada compatible.
        legacy_calls: list[tuple[str, str | None]] = []

        class _LegacyMinimalProvider:
            model_name = "legacy-min"
            last_error = None
            last_used_model = "legacy-min"
            last_fallback_note = None

            def generate_response(self, prompt: str, system_prompt: str | None = None) -> str:
                legacy_calls.append((prompt, system_prompt))
                return "Legacy OK"

        bm_legacy = BrainManager(
            initial_mode="OLLAMA",
            ollama_brain=_LegacyMinimalProvider(),
            memory=ConversationMemory(max_turns=4, storage_file=None, auto_save=False),
            memory_engine=self.engine,
            allow_cloud_fallback=False,
            lazy_init=True,
        )
        res_leg = bm_legacy.process_turn("Hola legacy")
        self.assertFalse(res_leg.is_error)
        self.assertEqual(len(legacy_calls), 1)

        # 3) GoalUnderstanding y TaskDecomposer: TypeError interno en query_raw no reenvía sin guarda.
        buggy_raw = _RecordingBrainRaw(raise_exc=TypeError("internal_raw_type_error"))
        gu = GoalUnderstandingEngine(brain_manager=buggy_raw, use_ai=True)
        with self.assertRaises(TypeError):
            gu.interpret("Diagnosticar rendimiento del equipo", untrusted_n8_context='{"test":1}')
        self.assertEqual(len(buggy_raw.calls), 1)

        buggy_raw_dec = _RecordingBrainRaw(raise_exc=TypeError("internal_decomp_type_error"))
        sm = _build_isolated_skill_manager(self.export_dir)
        td = TaskDecomposer(skill_selector=GoalSkillSelector(sm), brain_manager=buggy_raw_dec, use_ai=True)
        goal_plain = GoalUnderstandingEngine(use_ai=False).interpret("Diagnosticar sistema")
        with self.assertRaises(TypeError):
            td._try_ai_decomposition(goal_plain, limits=goal_plain.context and __import__("src.brain.goal_models", fromlist=["GoalEngineLimits"]).GoalEngineLimits(), untrusted_n8_context='{"test":1}')
        self.assertEqual(len(buggy_raw_dec.calls), 1)

        # 4) Legacy query_raw sin `snapshot_guard`/`untrusted_context` funciona en exactamente 1 llamada.
        legacy_raw_calls: list[tuple] = []

        class _LegacyRawBM:
            def query_raw(self, prompt: str, *, system_prompt: str = "", voice_mode: bool = False):
                legacy_raw_calls.append((prompt, system_prompt, voice_mode))
                return json.dumps({"interpreted_summary": "OK legacy"}), "Ollama", "qwen", None, False

        gu_leg = GoalUnderstandingEngine(brain_manager=_LegacyRawBM(), use_ai=True)
        gu_leg.interpret("Diagnosticar sistema")
        self.assertEqual(len(legacy_raw_calls), 1)

    # ------------------------------------------------------------------
    # F-07: Consulta exacta de auditoría de rollback más allá de 100 filas
    # ------------------------------------------------------------------

    def test_f07_rollback_audit_exact_match_past_100_entries_and_invalid_json(self) -> None:
        """`_has_rollback_audit_tx` encuentra tombstones tras >100 revocaciones, no confunde prefijos/comodines y tolera JSON corrupto."""
        self.assertTrue(
            self.store.mark_execution_rollback_tombstone("exec_1", reason="early_rb", actor="n5_orchestrator")
        )

        # Insertamos 125 eventos REVOKE posteriores con prefijos/sufijos parecidos y 3 filas con JSON inválido.
        with self.store.acquire_lock():
            with self.store._open_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                for i in range(125):
                    sim_id = f"exec_1{i:03d}" if i % 2 == 0 else f"exec_1_suffix_{i}"
                    details = json.dumps(
                        {
                            "source_execution_id": sim_id,
                            "invalidation_kind": "rollback_execution",
                        }
                    )
                    conn.execute(
                        """
                        INSERT INTO memory_audit (audit_id, action, memory_id, actor, reason, details_json, created_at)
                        VALUES (?, 'REVOKE', NULL, 'system', 'noise', ?, ?)
                        """,
                        (f"aud_noise_{i}", details, f"2099-01-01T00:{i // 60:02d}:{i % 60:02d}+00:00"),
                    )
                for bad_idx, bad_json in enumerate(("{not valid json", "null", "[]")):
                    conn.execute(
                        """
                        INSERT INTO memory_audit (audit_id, action, memory_id, actor, reason, details_json, created_at)
                        VALUES (?, 'REVOKE', NULL, 'system', 'legacy_bad', ?, ?)
                        """,
                        (f"aud_bad_{bad_idx}", bad_json, f"2099-01-02T00:00:0{bad_idx}+00:00"),
                    )
                conn.execute("COMMIT")

        reopened = ExperienceStore(self.db_path)
        self.assertTrue(reopened.is_execution_invalidated("exec_1"))
        self.assertTrue(reopened.is_execution_invalidated("exec_1000"))
        self.assertFalse(reopened.is_execution_invalidated("exec"))
        self.assertFalse(reopened.is_execution_invalidated("exec_"))
        self.assertFalse(reopened.is_execution_invalidated("exec_10"))
        self.assertFalse(reopened.is_execution_invalidated("exec_9999"))

    # ------------------------------------------------------------------
    # F-08: Reconstrucción ordenada de sobres N8 y frescura fail-closed
    # ------------------------------------------------------------------

    def test_f08_reconstructed_envelope_obtains_live_token_excludes_revoked_and_fails_closed(self) -> None:
        """Plan `READY` recargado reconstruye `RuntimeN8Envelope` con token vigente, excluye revocados y falla cerrado si muta el token."""
        mem_keep = self._seed_memory(
            title="Regla estable F08 Valorant",
            content="F08_KEEP_DATA: límite 144fps estable en Valorant.",
            scope_type=MemoryScopeType.APP_OR_GAME,
            scope_key="valorant",
            tags=("valorant", "fps", "diagnostico"),
            importance=95,
        )
        mem_revoke = self._seed_memory(
            title="Regla revocable F08 Valorant",
            content="F08_REVOKED_DATA: parámetro obsoleto en Valorant.",
            scope_type=MemoryScopeType.APP_OR_GAME,
            scope_key="valorant",
            tags=("valorant", "fps", "diagnostico"),
            importance=90,
        )

        sm = _build_isolated_skill_manager(self.export_dir)
        ge1 = GoalEngine(skill_manager=sm, goal_store=self.goal_store, memory_engine=self.engine)
        plan_orig = ge1.create_goal_and_plan(
            "Diagnosticar FPS en Valorant",
            context=GoalContext(target_app="Valorant"),
            custom_tasks=[
                GoalTask(
                    task_id="t1_sys",
                    title="Leer sistema",
                    description="Info base",
                    skill_name="system_info",
                    risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                )
            ],
        )
        self.assertIn(mem_keep, {r["memory_id"] for r in plan_orig.goal.context.extra.get("n8_memory_refs") or []})
        self.assertIn(mem_revoke, {r["memory_id"] for r in plan_orig.goal.context.extra.get("n8_memory_refs") or []})

        # Revocamos una referencia antes de reconstruir en una instancia nueva de GoalEngine.
        self.engine.revoke_memory(mem_revoke, reason="Obsoleto antes de recargar")
        expected_live_token = self.engine.get_store_version_token()

        ge2 = GoalEngine(skill_manager=sm, goal_store=self.goal_store, memory_engine=self.engine)
        reloaded = ge2.goal_store.load_plan(plan_orig.plan_id)
        self.assertIsNone(ge2.memory_bridge.get_envelope(plan_id=reloaded.plan_id, goal_id=reloaded.goal.goal_id))

        # Capturamos el sobre reconstruido en RAM antes de que el cierre terminal del plan lo limpie.
        observed_env = {"env": None}
        orig_exec_plan = ge2.supervisor.execute_plan

        def _spy_exec_plan(p, **kw):
            observed_env["env"] = ge2.memory_bridge.get_envelope(plan_id=p.plan_id, goal_id=p.goal.goal_id)
            return orig_exec_plan(p, **kw)

        with patch.object(ge2.supervisor, "execute_plan", side_effect=_spy_exec_plan):
            exec_res = ge2.execute_plan(reloaded, enable_replan=False)
        self.assertEqual(exec_res.final_status, GoalStatus.COMPLETED)

        rebuilt_env = observed_env["env"]
        self.assertIsNotNone(rebuilt_env)
        assert rebuilt_env is not None
        self.assertTrue(is_valid_store_version_token(rebuilt_env.store_version_token))
        self.assertEqual(rebuilt_env.store_version_token, expected_live_token)
        self.assertEqual(rebuilt_env.memory_ids, (mem_keep,))
        self.assertNotIn(mem_revoke, rebuilt_env.memory_ids)
        snap_from_rebuilt = rebuilt_env.to_memory_snapshot(request_id="req-f08-rebuilt")
        self.assertIsNotNone(snap_from_rebuilt)
        guard_f08 = self.engine.create_snapshot_guard(request_id="req-f08-rebuilt")
        val_f08 = guard_f08.validate_before_dispatch(snap_from_rebuilt, request_id="req-f08-rebuilt")
        self.assertTrue(val_f08.n8_allowed)

        # Modificación concurrente del token durante la reconstrucción -> fail-closed.
        ge3 = GoalEngine(skill_manager=sm, goal_store=self.goal_store, memory_engine=self.engine)
        reloaded_3 = ge3.goal_store.load_plan(plan_orig.plan_id)
        reloaded_3.goal.context.extra["n8_memory_refs"] = [
            ref.to_dict() for ref in rebuilt_env.references
        ]

        orig_get_mem = self.engine.get_memory
        mutated_once = {"done": False}

        def _mutate_token_mid_rebuild(mid: str):
            rec = orig_get_mem(mid)
            if not mutated_once["done"]:
                mutated_once["done"] = True
                # Mutación concurrente en SQLite que avanza store_version_token en plena reconstrucción.
                self._seed_memory(
                    title="Escritura concurrente F08",
                    content="Dato concurrente que rota el token durante reconstrucción.",
                    tags=("concurrente",),
                )
            return rec

        with patch.object(self.engine, "get_memory", side_effect=_mutate_token_mid_rebuild):
            env_fail, rep_fail = ge3.memory_bridge.revalidate_envelope(goal=reloaded_3.goal, plan=reloaded_3)
        self.assertIsNone(env_fail)
        self.assertEqual(rep_fail.valid_items, ())
        self.assertEqual(rep_fail.excluded_ids.get(mem_keep), "token_changed_during_revalidation")
        self.assertIsNone(ge3.memory_bridge.get_envelope(plan_id=reloaded_3.plan_id, goal_id=reloaded_3.goal.goal_id))

    # ------------------------------------------------------------------
    # F-09: Cableado perezoso de MemoryEngine en consola y voz (`main.py`)
    # ------------------------------------------------------------------

    def test_f09_console_and_voice_attach_existing_memory_engine_without_creating_db(self) -> None:
        """Consola y voz conectan `MemoryEngine` solo si la DB existe, sin crearla ni cargar voz en consola."""
        import main as jarvis_main
        from src.memory.snapshot_guard import MemorySnapshotGuard

        # 1) Sin DB previa: consola NO crea `experiences.db` ni adjunta `memory_engine`.
        empty_iso = self.work_dir / "f09_empty_iso"
        empty_iso.mkdir(parents=True, exist_ok=True)
        empty_db = empty_iso / "experiences.db"

        ollama_empty = OllamaBrain()
        ollama_empty.client = MagicMock()
        ollama_empty.client.chat.return_value = iter([{"message": {"content": "Hola sin DB."}}])
        bm_empty = BrainManager(
            initial_mode="OLLAMA",
            ollama_brain=ollama_empty,
            skill_manager=_build_isolated_skill_manager(self.export_dir),
            memory=ConversationMemory(max_turns=4, storage_file=None, auto_save=False),
            memory_engine=None,
            lazy_init=True,
        )
        cmds_empty = iter(["Hola jarvis", "salir"])
        with patch.dict(os.environ, {"JARVIS_TEST_TEMP_ROOT": str(empty_iso)}):
            jarvis_main.run_console_session(
                bm_empty,
                enable_tts=False,
                input_fn=lambda _: next(cmds_empty),
                output_fn=lambda _: None,
            )
        self.assertFalse(empty_db.exists())
        self.assertIsNone(bm_empty.memory_engine)

        # 2) Con DB previa y SIN comandos `/` previos: primer turno conversacional consulta N8 bajo MemorySnapshotGuard.
        seeded_iso = self.work_dir / "f09_seeded_iso"
        seeded_iso.mkdir(parents=True, exist_ok=True)
        seeded_db = seeded_iso / "experiences.db"
        canary_f09 = "CANARY_F09_CONSOLE_RECALL_4096"
        seeded_store = ExperienceStore(seeded_db)
        seeded_store.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.PREFERENCE,
                source=MemorySource.USER_EXPLICIT,
                title="Preferencia monitor F09",
                content=f"{canary_f09}: el usuario prefiere monitor 144Hz.",
                tags=("monitor", "preferencia"),
                importance=95,
            )
        )

        console_msgs: list[str] = []
        ollama_seeded = OllamaBrain()

        def _chat_seeded(*, model, messages, stream, options):
            console_msgs.append(json.dumps(messages, ensure_ascii=False))
            return iter([{"message": {"content": "Confirmado 144Hz."}}])

        ollama_seeded.client = MagicMock()
        ollama_seeded.client.chat.side_effect = _chat_seeded
        bm_seeded = BrainManager(
            initial_mode="OLLAMA",
            ollama_brain=ollama_seeded,
            skill_manager=_build_isolated_skill_manager(self.export_dir),
            memory=ConversationMemory(max_turns=4, storage_file=None, auto_save=False),
            memory_engine=None,
            lazy_init=True,
        )
        orig_validate = MemorySnapshotGuard.validate_before_dispatch
        cmds_seeded = iter(["Cual es mi preferencia de monitor", "salir"])
        with patch.dict(os.environ, {"JARVIS_TEST_TEMP_ROOT": str(seeded_iso)}), patch.object(
            MemorySnapshotGuard, "validate_before_dispatch", autospec=True, side_effect=orig_validate
        ) as spy_guard:
            jarvis_main.run_console_session(
                bm_seeded,
                enable_tts=False,
                input_fn=lambda _: next(cmds_seeded),
                output_fn=lambda _: None,
            )

        self.assertIsNotNone(bm_seeded.memory_engine)
        self.assertGreaterEqual(spy_guard.call_count, 1)
        self.assertEqual(len(console_msgs), 1)
        self.assertIn(canary_f09, console_msgs[0])

        # Reutilización de la misma instancia sin duplicar MemoryEngine.
        eng_first = bm_seeded.memory_engine
        with patch.dict(os.environ, {"JARVIS_TEST_TEMP_ROOT": str(seeded_iso)}):
            eng_again = jarvis_main.attach_existing_memory_engine(bm_seeded)
        self.assertIs(eng_again, eng_first)

        # 3) Ruta de inicialización de voz (`run_voice_session`) con mocks sin hardware físico.
        voice_msgs: list[str] = []
        ollama_voice = OllamaBrain()

        def _chat_voice(*, model, messages, stream, options):
            voice_msgs.append(json.dumps(messages, ensure_ascii=False))
            return iter([{"message": {"content": "Voz 144Hz OK."}}])

        ollama_voice.client = MagicMock()
        ollama_voice.client.chat.side_effect = _chat_voice
        bm_voice = BrainManager(
            initial_mode="OLLAMA",
            ollama_brain=ollama_voice,
            skill_manager=_build_isolated_skill_manager(self.export_dir),
            memory=ConversationMemory(max_turns=4, storage_file=None, auto_save=False),
            memory_engine=None,
            lazy_init=True,
        )

        from src.session import SessionEnd

        class _FakeWake:
            def __init__(self, threshold=0.5):
                self.calls = 0

            def listen_for_wake_word(self) -> bool:
                self.calls += 1
                return self.calls == 1

            def stop(self) -> None:
                pass

        class _FakeTray:
            def __init__(self, on_clear_memory=None):
                self.is_exit_requested = False

            def start(self) -> None:
                pass

            def set_state(self, _st: str) -> None:
                pass

            def stop(self) -> None:
                pass

        class _FakeTTS:
            is_speaking = False

            def speak(self, _text: str):
                return None

            def close(self) -> None:
                pass

        class _FakeActiveSession:
            def __init__(self, *, respond, **kwargs):
                self._respond = respond

            def run(self):
                self._respond("Cual es mi preferencia de monitor")
                return SessionEnd.EXIT

        fake_mod_audio_rec = MagicMock(AudioRecorder=MagicMock())
        fake_mod_audio_sfx = MagicMock(SoundFX=MagicMock())
        fake_mod_audio_stt = MagicMock(SpeechToText=MagicMock())
        fake_mod_audio_tts = MagicMock(TextToSpeech=_FakeTTS)
        fake_mod_audio_wake = MagicMock(WakeWordDetector=_FakeWake)
        fake_mod_ui_tray = MagicMock(JarvisTray=_FakeTray)

        with patch.dict(os.environ, {"JARVIS_TEST_TEMP_ROOT": str(seeded_iso)}), patch.dict(
            sys.modules,
            {
                "src.audio.recorder": fake_mod_audio_rec,
                "src.audio.sfx": fake_mod_audio_sfx,
                "src.audio.stt": fake_mod_audio_stt,
                "src.audio.tts": fake_mod_audio_tts,
                "src.audio.wake_word": fake_mod_audio_wake,
                "src.ui.tray": fake_mod_ui_tray,
            },
        ), patch.object(jarvis_main, "ActiveSession", _FakeActiveSession), patch("time.sleep", return_value=None):
            jarvis_main.run_voice_session(bm_voice)

        self.assertIsNotNone(bm_voice.memory_engine)
        self.assertEqual(len(voice_msgs), 1)
        self.assertIn(canary_f09, voice_msgs[0])

        # 4) Subproceso limpio: consola con DB existente NO carga módulos pesados de voz/GPU.
        probe_code = (
            "import os, sys, main; "
            "from unittest.mock import MagicMock; "
            "from src.brain.brain_manager import BrainManager; "
            "from src.brain.ollama_client import OllamaBrain; "
            "from src.brain.memory import ConversationMemory; "
            "ob = OllamaBrain(); ob.client = MagicMock(); "
            "ob.client.chat.return_value = iter([{'message': {'content': 'OK'}}]); "
            "bm = BrainManager(initial_mode='OLLAMA', ollama_brain=ob, "
            "memory=ConversationMemory(max_turns=4, storage_file=None, auto_save=False), lazy_init=True); "
            "cmds = iter(['Cual es mi preferencia de monitor', 'salir']); "
            "main.run_console_session(bm, enable_tts=False, input_fn=lambda _: next(cmds), output_fn=lambda _: None); "
            "assert bm.memory_engine is not None, 'MemoryEngine debió conectarse perezosamente'; "
            "heavy = [m for m in ('faster_whisper', 'sounddevice', 'pyttsx3', 'speech_recognition') if m in sys.modules]; "
            "assert not heavy, f'Módulos de voz cargados en consola: {heavy}'"
        )
        env_sub = dict(os.environ)
        env_sub["JARVIS_ENV"] = "test"
        env_sub["JARVIS_TEST_TEMP_ROOT"] = str(seeded_iso)
        proc = subprocess.run(
            [sys.executable, "-B", "-c", probe_code],
            cwd=str(PROJECT_ROOT),
            env=env_sub,
            capture_output=True,
            text=True,
            timeout=25,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)

    # ------------------------------------------------------------------
    # Integración combinada (Sección 14): snapshot obsoleto en fallback +
    # drenaje de rollback N5 al cerrar sesión consola iniciada en privado
    # ------------------------------------------------------------------

    def test_combined_stale_snapshot_fallback_and_console_exit_flushes_pending_rollback(self) -> None:
        """Valida no reutilización de snapshot obsoleto en fallback y drenaje de rollback N5 al cerrar consola privada."""
        import main as jarvis_main

        canary_stale = "CANARY_COMBINED_STALE_SNAP_7711"
        mem_stale = self._seed_memory(
            title="Regla obsoleta fallback",
            content=f"{canary_stale}: usar parámetro caduco.",
            tags=("fallback_combo",),
            importance=95,
        )

        # 1) Snapshot obsoleto en primario -> revocación -> fallback a Ollama NO reutiliza el recuerdo obsoleto.
        fallback_payloads: list[str] = []

        class _FailingPrimaryWithRevoke:
            model_name = "primary-fail"
            last_error = None
            last_used_model = "primary-fail"
            last_fallback_note = None

            def generate_response(
                self,
                prompt: str,
                system_prompt: str | None = None,
                *,
                max_output_tokens: int | None = None,
                raise_on_error: bool = False,
                untrusted_context: str | None = None,
                pre_dispatch_refresh=None,
            ) -> str:
                self.engine_ref.revoke_memory(mem_stale, reason="Revocado entre primario y fallback")
                raise BrainError(
                    provider="Gemini",
                    kind=BrainErrorKind.NETWORK,
                    user_message="Timeout simulado en primario.",
                )

        primary_fail = _FailingPrimaryWithRevoke()
        primary_fail.engine_ref = self.engine

        ollama_fb = OllamaBrain()

        def _chat_fb(*, model, messages, stream, options):
            fallback_payloads.append(json.dumps(messages, ensure_ascii=False))
            return iter([{"message": {"content": "Fallback limpio sin recuerdo obsoleto."}}])

        ollama_fb.client = MagicMock()
        ollama_fb.client.chat.side_effect = _chat_fb

        bm_fb = BrainManager(
            initial_mode="GEMINI",
            gemini_brain=primary_fail,
            ollama_brain=ollama_fb,
            memory=ConversationMemory(max_turns=4, storage_file=None, auto_save=False),
            memory_engine=self.engine,
            lazy_init=True,
        )
        res_fb = bm_fb.process_turn("Consulta fallback_combo ahora")
        self.assertFalse(res_fb.is_error)
        self.assertEqual(res_fb.final_provider, "Ollama")
        self.assertEqual(len(fallback_payloads), 1)
        self.assertNotIn(canary_stale, fallback_payloads[0])

        # 2) Rollback N5 confirmado durante `/privado` seguido de salida directa (`salir`) de la consola:
        # la invalidación pendiente NO se pierde al cerrar sesión y no se guarda ningún texto privado.
        cap_svc = ExperienceCaptureService(
            memory_engine=self.engine,
            enabled=True,
            auto_capture_enabled=True,
        )
        _fe, orch = self._build_real_n5(cap_svc)
        _cfg, sum_n5 = self._run_real_n5_edit(orch, exec_id="exec-combo-exit-rb", game_name="SolarGame")
        self.assertEqual(sum_n5["state"], ChainState.COMPLETED.value)
        solar_mems = [
            m.memory_id
            for m in self.store.list_memories(status=MemoryStatus.ACTIVE)
            if m.provenance.source_execution_id == "exec-combo-exit-rb"
        ]
        self.assertGreaterEqual(len(solar_mems), 1)

        bm_exit = BrainManager(
            initial_mode="OLLAMA",
            ollama_brain=ollama_fb,
            skill_manager=_build_isolated_skill_manager(self.export_dir),
            memory=self.conv_memory,
            memory_engine=self.engine,
            lazy_init=True,
        )
        step_counter = {"n": 0}

        def _scripted_input(_prompt: str) -> str:
            step_counter["n"] += 1
            if step_counter["n"] == 1:
                return "/privado"
            if step_counter["n"] == 2:
                # Confirmamos rollback N5 estando dentro de `/privado` y luego cerramos consola directamente.
                orch.rollback_execution("exec-combo-exit-rb")
                return "Mensaje privado CONFIDENCIAL_EXIT_9001"
            return "salir"

        with patch.dict(os.environ, {"JARVIS_TEST_TEMP_ROOT": str(self.work_dir)}):
            jarvis_main.run_console_session(
                bm_exit,
                enable_tts=False,
                input_fn=_scripted_input,
                output_fn=lambda _: None,
                orchestrator=orch,
                memory_engine=self.engine,
                memory_capture_service=cap_svc,
            )

        reopened = ExperienceStore(self.db_path)
        for mid in solar_mems:
            self.assertEqual(reopened.get_memory(mid).status, MemoryStatus.REVOKED)
        all_txt = json.dumps([m.content for m in reopened.list_memories(status=None, limit=200)])
        self.assertNotIn("CONFIDENCIAL_EXIT_9001", all_txt)


if __name__ == "__main__":
    unittest.main()
