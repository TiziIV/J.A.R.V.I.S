"""Suite integral de pruebas unitarias, funcionales y adversariales para N6.

Cubre los 27 escenarios obligatorios de especificación N6:
- Comprensión, ambigüedad y extracción de restricciones.
- Descomposición DAG, orden topológico (Kahn), ciclos y límites.
- Selección de skills reales vs rechazo de herramientas inexistentes.
- Supervisión, orden por dependencias, bloqueo de duplicados, reintentos y timeouts.
- Máquina de estados estricta, cancelación y replanificación preservando historial.
- Persistencia aislada bajo `temp/jarvis_test_isolated/` sin tocar `memory_store.json`.
- Autorización explícita, integridad criptográfica (`plan_hash`), no-bypass de
  `ChainOrchestrator`/`PolicyEngine`, consola (`/goal`, `/plan*`) y ausencia de carga de voz.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock
import uuid

from config import settings
from main import run_console_session
from skills.base_skill import BaseSkill
from skills.file_editor_skill import AuditLogger, BackupManager, FileEditorSkill
from src.brain.chain_orchestrator import ChainOrchestrator
from src.brain.goal_console import GoalConsoleHandler
from src.brain.goal_engine import GoalEngine
from src.brain.goal_models import (
    Constraints,
    Goal,
    GoalAuthorizationError,
    GoalContext,
    GoalEngineLimits,
    GoalPersistenceError,
    GoalPlan,
    GoalPlanningError,
    GoalPolicyViolationError,
    GoalStateError,
    GoalStatus,
    GoalTask,
    GoalType,
    TaskRiskCategory,
    TaskStatus,
)
from src.brain.goal_persistence import GoalStore
from src.tools.skill_manager import SkillManager


class _FakeSkill(BaseSkill):
    """Skill de prueba configurable para simular respuestas, fallos y latencia."""

    def __init__(
        self,
        name: str,
        fn,
        *,
        parameters: dict | None = None,
        description: str = "Skill de prueba N6",
    ) -> None:
        super().__init__(name=name, description=description, parameters=parameters or {})
        self._fn = fn

    def execute(self, **kwargs) -> str:
        return self._fn(**kwargs)


class _DummyBrainManager:
    """Dobles livianos de BrainManager para pruebas de consola y consulta AI."""

    def __init__(self, skill_manager: SkillManager | None = None, query_reply: str = "") -> None:
        self.global_mode = "offline"
        self.skill_manager = skill_manager
        self.memory = MagicMock()
        self.query_reply = query_reply
        self.calls: list[str] = []

    def get_brain_status(self) -> str:
        return "Modo: offline (test)"

    def query_raw(self, prompt: str, **_kwargs):
        self.calls.append(prompt)
        if not self.query_reply:
            return "error", None, None, None, True
        return self.query_reply, "fake", "fake-model", None, False

    def process_turn(self, user_text: str, voice_mode: bool = False):
        return MagicMock(
            response=f"ECO:{user_text}",
            diagnostic_line="[Mock]",
            fallback_info=None,
            is_error=False,
        )


def _make_clean_skill_manager(overrides: dict[str, BaseSkill] | None = None) -> SkillManager:
    """Construye un `SkillManager` con dobles seguros por defecto (sin tocar sistema real)."""
    sm = SkillManager.__new__(SkillManager)
    sm.skills = {
        "system_time": _FakeSkill("system_time", lambda **_: "Son las 12:00 del 12 de noviembre."),
        "system_info": _FakeSkill("system_info", lambda **_: "CPU al 18% y 9.4 GB de RAM libres."),
        "web_search": _FakeSkill(
            "web_search",
            lambda **kw: f"Búsqueda preparada para: {kw.get('query', '')}",
            parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        ),
        "web_extraction": _FakeSkill(
            "web_extraction",
            lambda **kw: f"Fuente extraída: {kw.get('urls') or kw.get('url')}",
            parameters={"type": "object", "properties": {"urls": {"type": "array"}, "url": {"type": "string"}}},
        ),
        "clipboard_manager": _FakeSkill(
            "clipboard_manager",
            lambda **kw: f"Portapapeles ({kw.get('action', 'read')}): contenido",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["read", "write"]},
                    "text": {"type": "string"},
                },
                "required": ["action"],
            },
        ),
        "file_editor": _FakeSkill(
            "file_editor",
            lambda **_: "SHOULD_NEVER_BYPASS_CHAIN_ORCHESTRATOR",
            parameters={"type": "object", "properties": {"action": {"type": "string"}, "file_path": {"type": "string"}}},
        ),
        "document_export": _FakeSkill(
            "document_export",
            lambda **kw: f"Exportado: {kw.get('title', 'doc')}",
            parameters={"type": "object", "properties": {"content": {"type": "string"}, "format": {"type": "string"}}, "required": ["content"]},
        ),
        "app_launcher": _FakeSkill(
            "app_launcher",
            lambda **kw: f"Abriendo {kw.get('app_name', '')}",
            parameters={"type": "object", "properties": {"app_name": {"type": "string"}}, "required": ["app_name"]},
        ),
        "activate_protocol": _FakeSkill(
            "activate_protocol",
            lambda **kw: f"Protocolo {kw.get('mode', 'diagnostico')}",
            parameters={"type": "object", "properties": {"mode": {"type": "string"}}, "required": ["mode"]},
        ),
        "system_power": _FakeSkill("system_power", lambda **_: "Power blocked in tests."),
        "volume_control": _FakeSkill("volume_control", lambda **_: "Volumen ajustado."),
        "media_control": _FakeSkill("media_control", lambda **_: "Media OK."),
    }
    if overrides:
        sm.skills.update(overrides)
    return sm


class TestGoalEngineN6(unittest.TestCase):
    """Pruebas integrales del Autonomous Goal & Planning Engine (N6)."""

    def setUp(self) -> None:
        self._old_env = os.environ.get("JARVIS_ENV")
        os.environ["JARVIS_ENV"] = "test"
        self.isolated_root = (
            Path(settings.BASE_DIR) / "temp" / "jarvis_test_isolated" / f"n6_{uuid.uuid4().hex[:10]}"
        ).resolve()
        self.isolated_root.mkdir(parents=True, exist_ok=True)
        self.store = GoalStore(store_dir=self.isolated_root / "goals")
        self.sm = _make_clean_skill_manager()
        self.engine = GoalEngine(skill_manager=self.sm, goal_store=self.store)

    def tearDown(self) -> None:
        shutil.rmtree(self.isolated_root, ignore_errors=True)
        if self._old_env is None:
            os.environ.pop("JARVIS_ENV", None)
        else:
            os.environ["JARVIS_ENV"] = self._old_env

    # 1. Comprensión de objetivo claro
    def test_01_clear_goal_interpretation(self) -> None:
        goal = self.engine.interpret_goal(
            "Jarvis, quiero optimizar el rendimiento de mi PC para jugar."
        )
        self.assertEqual(goal.goal_type, GoalType.OPTIMIZATION)
        self.assertFalse(goal.needs_clarification)
        self.assertEqual(goal.status, GoalStatus.DRAFT)
        self.assertTrue(goal.missing_info)
        self.assertIn("system_info", goal.required_capabilities.skills)

    # 2. Detección de objetivo ambiguo o incompleto + aclaración
    def test_02_ambiguous_goal_and_clarification_flow(self) -> None:
        submission = self.engine.submit_goal("mejorá todo")
        self.assertTrue(submission.needs_clarification)
        self.assertIsNone(submission.plan)
        self.assertEqual(submission.goal.goal_type, GoalType.AMBIGUOUS)
        self.assertEqual(submission.goal.status, GoalStatus.AWAITING_CLARIFICATION)
        self.assertGreaterEqual(len(submission.clarification_questions), 1)

        with self.assertRaises(GoalPlanningError):
            self.engine.create_goal_and_plan("")

        clarified = self.engine.clarify_pending_goal(
            "Quiero solo un diagnóstico del uso de CPU y memoria RAM sin modificar archivos."
        )
        self.assertFalse(clarified.needs_clarification)
        self.assertIsNotNone(clarified.plan)
        self.assertTrue(clarified.goal.constraints.read_only)

    # 3. Extracción de restricciones explícitas
    def test_03_explicit_constraints_extraction_and_enforcement(self) -> None:
        goal = self.engine.interpret_goal(
            "Analiza rendimiento de Valorant solo diagnóstico, sin modificar archivos y sin internet."
        )
        self.assertTrue(goal.constraints.read_only)
        self.assertFalse(goal.constraints.allow_file_modifications)
        self.assertFalse(goal.constraints.allow_network)
        self.assertIn("file_editor", goal.constraints.forbidden_actions)
        self.assertIn("web_search", goal.constraints.forbidden_actions)

        plan = self.engine.planner.build_plan(goal)
        skills_used = {t.skill_name for t in plan.tasks}
        self.assertNotIn("file_editor", skills_used)
        self.assertNotIn("web_search", skills_used)

    # 4. Descomposición en subtareas coherentes
    def test_04_coherent_task_decomposition(self) -> None:
        plan = self.engine.create_goal_and_plan(
            "Jarvis, quiero optimizar el rendimiento de mi PC para jugar."
        )
        self.assertGreaterEqual(len(plan.tasks), 2)
        self.assertEqual(plan.tasks[0].skill_name, "system_info")
        self.assertEqual(plan.tasks[-1].risk_category, TaskRiskCategory.SAFE_PLANNING)
        self.assertTrue(plan.verify_integrity())

    # 5. Validación de dependencias válidas
    def test_05_valid_dependencies_dag_order(self) -> None:
        goal = self.engine.interpret_goal("Diagnóstico ordenado de sistema")
        t2 = GoalTask(task_id="t2", title="Paso 2", description="Depende de t1", skill_name=None, depends_on=["t1"])
        t1 = GoalTask(task_id="t1", title="Paso 1", description="Raíz", skill_name="system_info")
        plan = self.engine.planner.build_plan(goal, custom_tasks=[t2, t1])
        self.assertEqual([t.task_id for t in plan.tasks], ["t1", "t2"])

    # 6. Rechazo de dependencias circulares
    def test_06_reject_circular_dependencies(self) -> None:
        goal = self.engine.interpret_goal("Diagnóstico de sistema")
        a = GoalTask(task_id="a", title="A", description="A", depends_on=["b"])
        b = GoalTask(task_id="b", title="B", description="B", depends_on=["a"])
        with self.assertRaises(GoalPlanningError):
            self.engine.planner.build_plan(goal, custom_tasks=[a, b])

        self_dep = GoalTask(task_id="solo", title="Solo", description="Self", depends_on=["solo"])
        with self.assertRaises(GoalPlanningError):
            self.engine.planner.build_plan(goal, custom_tasks=[self_dep])

    # 7. Validación de plan válido vs inválido
    def test_07_valid_vs_invalid_plan_validation(self) -> None:
        goal = self.engine.interpret_goal("Diagnóstico de sistema")
        with self.assertRaises(GoalPlanningError):
            self.engine.planner.build_plan(goal, custom_tasks=[])

        broken_dep = GoalTask(task_id="t1", title="T1", description="D", depends_on=["missing"])
        with self.assertRaises(GoalPlanningError):
            self.engine.planner.build_plan(goal, custom_tasks=[broken_dep])

        dup1 = GoalTask(task_id="dup", title="One", description="1")
        dup2 = GoalTask(task_id="dup", title="Two", description="2")
        with self.assertRaises(GoalPlanningError):
            self.engine.planner.build_plan(goal, custom_tasks=[dup1, dup2])

    # 8. Selección de skill existente
    def test_08_existing_skill_selection_and_risk_classification(self) -> None:
        selector = self.engine.skill_selector
        res_info = selector.select_skill_for_intent("hardware_inspection", constraints=Constraints())
        self.assertEqual(res_info.skill_name, "system_info")
        self.assertEqual(res_info.risk_category, TaskRiskCategory.LOW_RISK_EXECUTION)
        self.assertFalse(res_info.requires_approval)

        res_clip_write = selector.select_skill_for_intent(
            "clipboard_write",
            constraints=Constraints(),
            params={"action": "write", "text": "hola"},
        )
        self.assertEqual(res_clip_write.risk_category, TaskRiskCategory.SENSITIVE_OPERATION)
        self.assertTrue(res_clip_write.requires_approval)

        res_file = selector.select_skill_for_intent(
            "file_modification",
            constraints=Constraints(),
            params={"action": "modify", "file_path": "cfg.ini"},
        )
        self.assertEqual(res_file.risk_category, TaskRiskCategory.SENSITIVE_OPERATION)
        self.assertTrue(res_file.requires_chain_orchestrator)

    # 9. Detección de skill inexistente
    def test_09_reject_nonexistent_skill(self) -> None:
        goal = self.engine.interpret_goal("Diagnóstico de sistema")
        invented = GoalTask(
            task_id="t_fake",
            title="Inventada",
            description="No existe",
            skill_name="quantum_turbo_booster",
        )
        with self.assertRaises(GoalPlanningError):
            self.engine.planner.build_plan(goal, custom_tasks=[invented])

    # 10. Ejecución ordenada de tareas independientes
    def test_10_independent_tasks_execution(self) -> None:
        order: list[str] = []
        sm = _make_clean_skill_manager(
            {
                "system_info": _FakeSkill("system_info", lambda **_: (order.append("info"), "CPU OK")[1]),
                "system_time": _FakeSkill("system_time", lambda **_: (order.append("time"), "12:00")[1]),
            }
        )
        engine = GoalEngine(skill_manager=sm, goal_store=self.store)
        plan = engine.create_goal_and_plan(
            "Quiero un diagnóstico de uso de CPU y RAM solo lectura",
        )
        result = engine.execute_plan(plan)
        self.assertEqual(result.final_status, GoalStatus.COMPLETED)
        self.assertEqual(order, ["info", "time"])

    # 11. Ejecución secuencial respetando `depends_on`
    def test_11_sequential_dependency_order_enforced(self) -> None:
        events: list[str] = []
        sm = _make_clean_skill_manager(
            {
                "system_info": _FakeSkill("system_info", lambda **_: (events.append("t1"), "CPU 10%")[1]),
                "system_time": _FakeSkill("system_time", lambda **_: (events.append("t2"), "13:00")[1]),
            }
        )
        engine = GoalEngine(skill_manager=sm, goal_store=self.store)
        goal = engine.interpret_goal("Secuencia ordenada de diagnóstico del sistema")
        t1 = GoalTask(task_id="t1", title="Primero", description="1", skill_name="system_info")
        t2 = GoalTask(task_id="t2", title="Segundo", description="2", skill_name="system_time", depends_on=["t1"])
        t3 = GoalTask(task_id="t3", title="Tercero", description="3", skill_name=None, depends_on=["t2"])
        plan = engine.planner.build_plan(goal, custom_tasks=[t3, t2, t1])

        # Intentar ejecutar t2 manualmente antes que t1 debe fallar
        with self.assertRaises(GoalStateError):
            engine.supervisor.execute_single_task(plan, t2)

        res = engine.execute_plan(plan)
        self.assertEqual(res.final_status, GoalStatus.COMPLETED)
        self.assertEqual(events, ["t1", "t2"])
        self.assertEqual(res.completed_task_ids, ["t1", "t2", "t3"])

    # 12. Transiciones de estado válidas
    def test_12_valid_state_transitions(self) -> None:
        goal = Goal(raw_input="Test", interpreted_summary="Test")
        self.assertEqual(goal.status, GoalStatus.DRAFT)
        goal.transition_to(GoalStatus.PLANNED)
        goal.transition_to(GoalStatus.AWAITING_APPROVAL)
        goal.transition_to(GoalStatus.APPROVED)
        goal.transition_to(GoalStatus.RUNNING)
        goal.transition_to(GoalStatus.COMPLETED)
        self.assertEqual(goal.status, GoalStatus.COMPLETED)

        task = GoalTask(
            task_id="t1",
            title="Sensible",
            description="Test",
            risk_category=TaskRiskCategory.SENSITIVE_OPERATION,
            requires_approval=True,
        )
        task.transition_to(TaskStatus.REQUIRES_APPROVAL)
        task.grant_approval(approval_token="tok-1", approval_hash="h-1")
        self.assertEqual(task.status, TaskStatus.APPROVED)
        task.transition_to(TaskStatus.RUNNING)
        task.mark_completed(output_summary="OK", evidence={"verified": True})
        self.assertEqual(task.status, TaskStatus.COMPLETED)

    # 13. Rechazo de transiciones de estado inválidas
    def test_13_reject_invalid_state_transitions(self) -> None:
        task = GoalTask(task_id="t1", title="T1", description="D1")
        # No puede pasar a COMPLETED sin evidencia
        with self.assertRaises(GoalStateError):
            task.transition_to(TaskStatus.COMPLETED)

        task.transition_to(TaskStatus.READY)
        task.transition_to(TaskStatus.RUNNING)
        task.mark_completed(output_summary="Hecho", evidence={"ok": True})
        with self.assertRaises(GoalStateError):
            task.transition_to(TaskStatus.RUNNING)

        sensitive = GoalTask(
            task_id="t_sens",
            title="Sensible",
            description="D",
            risk_category=TaskRiskCategory.SENSITIVE_OPERATION,
            requires_approval=True,
        )
        sensitive.transition_to(TaskStatus.REQUIRES_APPROVAL)
        with self.assertRaises(GoalAuthorizationError):
            sensitive.transition_to(TaskStatus.RUNNING)

        goal = Goal(raw_input="X", interpreted_summary="X", status=GoalStatus.CANCELLED)
        with self.assertRaises(GoalStateError):
            goal.transition_to(GoalStatus.RUNNING)

    # 14. Prevención de ejecución duplicada de tarea ya completada
    def test_14_prevent_duplicate_execution_of_completed_task(self) -> None:
        calls = {"count": 0}

        def _count_fn(**_):
            calls["count"] += 1
            return "Métrica única"

        sm = _make_clean_skill_manager({"system_info": _FakeSkill("system_info", _count_fn)})
        engine = GoalEngine(skill_manager=sm, goal_store=self.store)
        plan = engine.create_goal_and_plan("Diagnóstico de CPU solo lectura")
        t_info = plan.get_task("t1_cpu_ram")
        assert t_info is not None
        res = engine.execute_plan(plan)
        self.assertEqual(res.final_status, GoalStatus.COMPLETED)
        self.assertEqual(calls["count"], 1)

        with self.assertRaises(GoalStateError):
            engine.supervisor.execute_single_task(plan, t_info)
        with self.assertRaises(GoalStateError):
            engine.execute_plan(plan)
        self.assertEqual(calls["count"], 1)

    # 15. Manejo de fallo de subtarea
    def test_15_subtask_failure_blocks_dependents_and_marks_partial(self) -> None:
        def _boom(**_):
            raise RuntimeError("Fallo de sensor crítico")

        sm = _make_clean_skill_manager({"system_time": _FakeSkill("system_time", _boom)})
        limits = GoalEngineLimits(max_retries=0, max_replans=0)
        engine = GoalEngine(skill_manager=sm, goal_store=self.store, limits=limits)
        plan = engine.create_goal_and_plan("Diagnóstico de CPU y memoria RAM", limits=limits)
        res = engine.execute_plan(plan, enable_replan=False)
        self.assertEqual(res.final_status, GoalStatus.PARTIAL)
        self.assertIn("t1_cpu_ram", res.completed_task_ids)
        self.assertIn("t2_timestamp", res.failed_task_ids)
        t3 = plan.get_task("t3_report")
        assert t3 is not None
        self.assertEqual(t3.status, TaskStatus.BLOCKED)

    # 16. Reintentos acotados
    def test_16_bounded_retries_succeed_or_exhaust_cleanly(self) -> None:
        attempts = {"n": 0}

        def _flaky(**_):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("Error transitorio de lectura")
            return "Recuperado al segundo intento"

        sm = _make_clean_skill_manager({"system_info": _FakeSkill("system_info", _flaky)})
        limits = GoalEngineLimits(max_retries=1, max_replans=0)
        engine = GoalEngine(skill_manager=sm, goal_store=self.store, limits=limits)
        t1 = GoalTask(task_id="t1", title="Lectura", description="Flaky", skill_name="system_info", max_retries=1)
        plan = engine.create_goal_and_plan("Diagnóstico simple", custom_tasks=[t1], limits=limits)
        res = engine.execute_plan(plan, enable_replan=False)
        self.assertEqual(res.final_status, GoalStatus.COMPLETED)
        self.assertEqual(attempts["n"], 2)
        self.assertEqual(t1.retry_count, 1)

        # Ahora verificar agotamiento estricto cuando falla siempre
        always_fail_calls = {"n": 0}

        def _always_fail(**_):
            always_fail_calls["n"] += 1
            raise RuntimeError("Fallo persistente")

        sm_fail = _make_clean_skill_manager({"system_info": _FakeSkill("system_info", _always_fail)})
        engine_fail = GoalEngine(skill_manager=sm_fail, goal_store=self.store, limits=GoalEngineLimits(max_retries=2, max_replans=0))
        t_fail = GoalTask(task_id="tf", title="Falla", description="Siempre falla", skill_name="system_info", max_retries=2)
        plan_fail = engine_fail.create_goal_and_plan("Diagnóstico fail", custom_tasks=[t_fail], limits=GoalEngineLimits(max_retries=2, max_replans=0))
        res_fail = engine_fail.execute_plan(plan_fail, enable_replan=False)
        self.assertEqual(res_fail.final_status, GoalStatus.FAILED)
        self.assertEqual(always_fail_calls["n"], 3)  # 1 intento inicial + 2 reintentos
        self.assertEqual(t_fail.retry_count, 2)

    # 17. Cancelación limpia
    def test_17_clean_cancellation_mid_execution_preserves_completed(self) -> None:
        cancel_ev = threading.Event()

        def _trigger_cancel(**_):
            cancel_ev.set()
            return "CPU leída antes de cancelar"

        sm = _make_clean_skill_manager({"system_info": _FakeSkill("system_info", _trigger_cancel)})
        engine = GoalEngine(skill_manager=sm, goal_store=self.store)
        plan = engine.create_goal_and_plan("Diagnóstico de CPU y memoria RAM")
        res = engine.execute_plan(plan, cancel_event=cancel_ev)
        self.assertTrue(res.cancelled)
        self.assertEqual(res.final_status, GoalStatus.CANCELLED)
        self.assertIn("t1_cpu_ram", res.completed_task_ids)
        t2 = plan.get_task("t2_timestamp")
        assert t2 is not None
        self.assertEqual(t2.status, TaskStatus.CANCELLED)

    # 18. Replanificación ante fallo parcial
    def test_18_replanning_on_partial_failure(self) -> None:
        def _fail_web(**_):
            raise RuntimeError("Error de red controlado")

        sm = _make_clean_skill_manager({"web_extraction": _FakeSkill("web_extraction", _fail_web)})
        limits = GoalEngineLimits(max_retries=0, max_replans=2)
        engine = GoalEngine(skill_manager=sm, goal_store=self.store, limits=limits)
        t1 = GoalTask(task_id="t1", title="Extraer web", description="Web", skill_name="web_extraction", skill_params={"urls": ["https://example.com"]})
        t2 = GoalTask(task_id="t2", title="Sintetizar", description="Síntesis", skill_name=None, depends_on=["t1"])
        plan = engine.create_goal_and_plan("Investigar https://example.com", custom_tasks=[t1, t2], limits=limits)

        res = engine.execute_plan(plan, enable_replan=True)
        self.assertEqual(res.replan_count, 1)
        self.assertEqual(res.final_status, GoalStatus.PARTIAL)  # t1 quedó FAILED, pero fallback + t2 completaron
        self.assertIsNotNone(plan.get_task("t1_r1"))
        self.assertEqual(plan.get_task("t1_r1").status, TaskStatus.COMPLETED)
        self.assertEqual(plan.get_task("t2").status, TaskStatus.COMPLETED)

    # 19. Preservación de resultados ya completados al replanificar
    def test_19_preserve_completed_tasks_and_history_on_replan(self) -> None:
        t1_counter = {"runs": 0}

        def _ok_once(**_):
            t1_counter["runs"] += 1
            return "EVIDENCIA_ORIGINAL_T1"

        def _fail_web(**_):
            raise RuntimeError("Timeout remoto")

        sm = _make_clean_skill_manager(
            {
                "system_info": _FakeSkill("system_info", _ok_once),
                "web_extraction": _FakeSkill("web_extraction", _fail_web),
            }
        )
        limits = GoalEngineLimits(max_retries=0, max_replans=2)
        engine = GoalEngine(skill_manager=sm, goal_store=self.store, limits=limits)
        t1 = GoalTask(task_id="t1_keep", title="Paso completado", description="OK", skill_name="system_info")
        t2 = GoalTask(task_id="t2_web", title="Paso web", description="Falla", skill_name="web_extraction", skill_params={"urls": ["https://example.org"]}, depends_on=["t1_keep"])
        plan = engine.create_goal_and_plan("Investigar con web", custom_tasks=[t1, t2], limits=limits)

        engine.execute_plan(plan, enable_replan=True)
        self.assertEqual(t1_counter["runs"], 1)
        self.assertEqual(t1.output_summary, "EVIDENCIA_ORIGINAL_T1")
        self.assertEqual(plan.replan_history[0].preserved_task_ids, ["t1_keep"])
        self.assertGreaterEqual(len(plan.history), 5)

    # 20. Límite de cantidad máxima de tareas
    def test_20_enforce_max_tasks_limit(self) -> None:
        limits = GoalEngineLimits(max_tasks=3)
        goal = self.engine.interpret_goal("Plan grande")
        too_many = [
            GoalTask(task_id=f"t{i}", title=f"T{i}", description="D", skill_name=None)
            for i in range(4)
        ]
        with self.assertRaises(GoalPlanningError):
            self.engine.planner.build_plan(goal, limits=limits, custom_tasks=too_many)

    # 21. Límite de profundidad de plan
    def test_21_enforce_max_depth_limit(self) -> None:
        limits = GoalEngineLimits(max_tasks=10, max_depth=3)
        goal = self.engine.interpret_goal("Plan profundo")
        chain = [
            GoalTask(
                task_id=f"d{i}",
                title=f"Nivel {i}",
                description="D",
                skill_name=None,
                depends_on=[f"d{i - 1}"] if i > 1 else [],
            )
            for i in range(1, 5)  # profundidad 4 > max_depth 3
        ]
        with self.assertRaises(GoalPlanningError):
            self.engine.planner.build_plan(goal, limits=limits, custom_tasks=chain)

    # 22. Límite de timeout por tarea y global
    def test_22_enforce_task_and_total_timeout(self) -> None:
        def _slow(**_):
            time.sleep(0.35)
            return "Demasiado tarde"

        sm = _make_clean_skill_manager({"system_info": _FakeSkill("system_info", _slow)})
        limits = GoalEngineLimits(max_retries=0, task_timeout_sec=0.1, total_timeout_sec=1.0, max_replans=0)
        engine = GoalEngine(skill_manager=sm, goal_store=self.store, limits=limits)
        t1 = GoalTask(task_id="t_slow", title="Lenta", description="Lenta", skill_name="system_info", timeout_sec=0.1, max_retries=0)
        plan = engine.create_goal_and_plan("Diagnóstico lento", custom_tasks=[t1], limits=limits)

        res = engine.execute_plan(plan, enable_replan=False)
        self.assertEqual(res.final_status, GoalStatus.FAILED)
        self.assertIn("timeout", (t1.last_error or "").lower())

        # Timeout global antes de ejecutar la segunda tarea
        def _medium(**_):
            time.sleep(0.08)
            return "OK"

        sm_global = _make_clean_skill_manager({"system_info": _FakeSkill("system_info", _medium)})
        limits_g = GoalEngineLimits(max_retries=0, task_timeout_sec=1.0, total_timeout_sec=0.05, max_replans=0)
        engine_g = GoalEngine(skill_manager=sm_global, goal_store=self.store, limits=limits_g)
        tg1 = GoalTask(task_id="g1", title="G1", description="1", skill_name="system_info")
        tg2 = GoalTask(task_id="g2", title="G2", description="2", skill_name="system_info", depends_on=["g1"])
        plan_g = engine_g.create_goal_and_plan("Timeout global", custom_tasks=[tg1, tg2], limits=limits_g)
        res_g = engine_g.execute_plan(plan_g, enable_replan=False)
        self.assertEqual(res_g.final_status, GoalStatus.FAILED)
        self.assertEqual(tg2.status, TaskStatus.FAILED)

    # 23. Persistencia, recuperación y aislamiento estricto de memory_store.json
    def test_23_persistence_recovery_isolation_and_integrity(self) -> None:
        mem_file = Path(settings.BASE_DIR) / "data" / "memory_store.json"
        mem_before = hashlib.sha256(mem_file.read_bytes()).hexdigest() if mem_file.exists() else None

        # Bloquear persistencia fuera de temp/jarvis_test_isolated en modo test
        with self.assertRaises(GoalPersistenceError):
            GoalStore(store_dir=Path(settings.BASE_DIR) / "data" / "goals")

        plan = self.engine.create_goal_and_plan("Diagnóstico de CPU y RAM solo lectura")
        res = self.engine.execute_plan(plan)
        self.assertEqual(res.final_status, GoalStatus.COMPLETED)

        # Recuperación en una nueva instancia de GoalStore / GoalEngine
        fresh_store = GoalStore(store_dir=self.isolated_root / "goals")
        fresh_engine = GoalEngine(skill_manager=self.sm, goal_store=fresh_store)
        recovered = fresh_engine.get_active_plan()
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.plan_id, plan.plan_id)
        self.assertEqual(recovered.goal.status, GoalStatus.COMPLETED)
        self.assertTrue(recovered.verify_integrity())
        self.assertGreater(len(fresh_store.read_wal_events(plan.plan_id)), 0)

        # Alterar snapshot en disco debe detectarse como corrupción/integridad comprometida
        snap_path = fresh_store.plans_dir / f"{plan.plan_id}.goal.json"
        corrupted = json.loads(snap_path.read_text(encoding="utf-8"))
        corrupted["tasks"][0]["title"] = "TITULO_ALTERADO_MALICIOSO"
        snap_path.write_text(json.dumps(corrupted), encoding="utf-8")
        with self.assertRaises(GoalPersistenceError):
            fresh_store.load_plan(plan.plan_id)

        mem_after = hashlib.sha256(mem_file.read_bytes()).hexdigest() if mem_file.exists() else None
        self.assertEqual(mem_before, mem_after)

    # 24. Integración con consola (/goal, /plan, /plan ejecutar, /plan estado, /plan cancelar)
    def test_24_console_commands_and_sensitive_confirmation_denial(self) -> None:
        inputs = iter(
            [
                "/goal Quiero optimizar el rendimiento de Valorant para jugar",
                "/plan",
                "/plan estado",
                "/plan ejecutar",
                "no",  # Rechaza operación sensible en /plan ejecutar
                "/plan cancelar",
                "salir",
            ]
        )
        outputs: list[str] = []
        fe_console = FileEditorSkill(
            authorized_roots=[self.isolated_root],
            backup_dir=self.isolated_root / "backups_console",
            audit_logger=AuditLogger(log_file=self.isolated_root / "audit_console.jsonl"),
            backup_manager=BackupManager(),
        )
        sm_console = _make_clean_skill_manager({"file_editor": fe_console})
        brain = _DummyBrainManager(skill_manager=sm_console)
        chain_console = ChainOrchestrator(
            file_editor=fe_console,
            brain_manager=brain,
            skill_manager=sm_console,
            journal_dir=self.isolated_root / "journal_console",
        )

        # Aislamos GOAL_STORE_DIR implícito con JARVIS_TEST_TEMP_ROOT para esta sesión de consola
        old_temp_root = os.environ.get("JARVIS_TEST_TEMP_ROOT")
        os.environ["JARVIS_TEST_TEMP_ROOT"] = str(self.isolated_root)
        try:
            run_console_session(
                brain,
                enable_tts=False,
                orchestrator=chain_console,
                input_fn=lambda prompt: next(inputs),
                output_fn=outputs.append,
            )
        finally:
            if old_temp_root is None:
                os.environ.pop("JARVIS_TEST_TEMP_ROOT", None)
            else:
                os.environ["JARVIS_TEST_TEMP_ROOT"] = old_temp_root

        joined = "\n".join(outputs)
        self.assertIn("Plan creado sin ejecutar acciones sensibles", joined)
        self.assertIn("[Autorización requerida — N6]", joined)
        self.assertIn("pendientes de confirmación explícita", joined)
        self.assertIn("cancelado correctamente", joined)
        # No se ejecutó la modificación sensible
        self.assertNotIn("SHOULD_NEVER_BYPASS_CHAIN_ORCHESTRATOR", joined)

    # 25. No bypass de PolicyEngine / ChainOrchestrator
    def test_25_no_bypass_policy_engine_and_chain_orchestrator(self) -> None:
        cfg_dir = self.isolated_root / "game_cfg"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg_file = cfg_dir / "settings.ini"
        original_content = "[Graphics]\nShadows=High\n"
        cfg_file.write_text(original_content, encoding="utf-8")

        # 25a. Sin ChainOrchestrator, file_editor queda bloqueado incluso si fue autorizado en N6
        engine_no_chain = GoalEngine(skill_manager=self.sm, goal_store=self.store)
        goal = engine_no_chain.interpret_goal(
            f"Optimizar configuración {cfg_file} en Valorant",
            context=GoalContext(target_app="Valorant", target_paths=[str(cfg_file)]),
        )
        t_edit = GoalTask(
            task_id="t_edit",
            title="Editar config",
            description="Edición segura",
            skill_name="file_editor",
            skill_params={"action": "modify", "file_path": str(cfg_file)},
            risk_category=TaskRiskCategory.SENSITIVE_OPERATION,
            requires_approval=True,
            metadata={"use_chain_orchestrator": True, "modifies_files": True, "file_path": str(cfg_file)},
        )
        plan = engine_no_chain.create_goal_and_plan(goal.raw_input, custom_tasks=[t_edit])
        engine_no_chain.authorize_plan(plan)
        with self.assertRaises(GoalPolicyViolationError):
            engine_no_chain.supervisor.execute_single_task(plan, t_edit)
        self.assertEqual(cfg_file.read_text(encoding="utf-8"), original_content)

        # 25b. Con ChainOrchestrator real: si usuario dice "no" en autorización interactiva,
        # el archivo permanece intacto y se respeta PolicyEngine.
        fe = FileEditorSkill(
            authorized_roots=[cfg_dir],
            backup_dir=self.isolated_root / "backups",
            audit_logger=AuditLogger(log_file=self.isolated_root / "audit.jsonl"),
            backup_manager=BackupManager(),
        )
        sm_with_fe = _make_clean_skill_manager({"file_editor": fe})
        chain = ChainOrchestrator(
            file_editor=fe,
            brain_manager=_DummyBrainManager(skill_manager=sm_with_fe),
            skill_manager=sm_with_fe,
            journal_dir=self.isolated_root / "journal",
        )
        engine_chain = GoalEngine(
            skill_manager=sm_with_fe,
            chain_orchestrator=chain,
            goal_store=GoalStore(store_dir=self.isolated_root / "goals_chain"),
        )
        plan2 = engine_chain.create_goal_and_plan(
            f"Optimizar {cfg_file} de Valorant",
            custom_tasks=[
                GoalTask(
                    task_id="t_chain",
                    title="Optimizar via Chain",
                    description="ChangePlan",
                    skill_name="file_editor",
                    skill_params={"action": "modify", "file_path": str(cfg_file)},
                    risk_category=TaskRiskCategory.SENSITIVE_OPERATION,
                    requires_approval=True,
                    metadata={
                        "use_chain_orchestrator": True,
                        "modifies_files": True,
                        "target_app": "Valorant",
                        "file_path": str(cfg_file),
                    },
                )
            ],
        )
        engine_chain.authorize_plan(plan2)
        res_denied = engine_chain.execute_plan(plan2, input_fn=lambda _p: "no", enable_replan=False)
        self.assertNotEqual(res_denied.final_status, GoalStatus.COMPLETED)
        self.assertEqual(cfg_file.read_text(encoding="utf-8"), original_content)

    # 26. Bloqueo de cambios sensibles sin aprobación + detección de manipulación del plan
    def test_26_block_sensitive_changes_without_approval_and_tampering(self) -> None:
        plan = self.engine.create_goal_and_plan(
            "Exportar un informe de diagnóstico del sistema en markdown"
        )
        t_export = plan.get_task("t3_export_doc")
        assert t_export is not None
        self.assertTrue(t_export.requires_approval)
        self.assertEqual(plan.goal.status, GoalStatus.AWAITING_APPROVAL)

        # Ejecutar sin aprobación ejecuta pasos de diagnóstico seguros y se detiene antes de t3_export_doc
        res = self.engine.execute_plan(plan, strict_approval=False)
        self.assertTrue(res.stopped_for_approval)
        self.assertEqual(res.pending_approval_task_ids, ["t3_export_doc"])
        self.assertEqual(t_export.status, TaskStatus.REQUIRES_APPROVAL)

        # Con dependencias ya completadas pero sin aprobación, execute_single_task y strict_approval bloquean
        with self.assertRaises(GoalAuthorizationError):
            self.engine.supervisor.execute_single_task(plan, t_export)
        with self.assertRaises(GoalAuthorizationError):
            self.engine.execute_plan(plan, strict_approval=True)

        # Aprobar el plan y luego alterar un parámetro debe invalidar integridad y bloquear ejecución
        self.engine.authorize_plan(plan)
        t_export.skill_params["output_path"] = "C:\\Windows\\System32\\hack.md"
        with self.assertRaises(GoalAuthorizationError):
            self.engine.supervisor.execute_single_task(plan, t_export)

    # 27. Multi-Brain AI validation, frontera de datos externos y aislamiento de voz
    def test_27_ai_validation_untrusted_boundary_and_no_voice_imports(self) -> None:
        # AI propone skill inexistente -> motor descarta y usa plan determinista válido
        brain = _DummyBrainManager(
            skill_manager=self.sm,
            query_reply='[{"task_id":"x1","title":"Fake","skill_name":"delete_all_files","depends_on":[]}]',
        )
        ai_engine = GoalEngine(
            skill_manager=self.sm,
            brain_manager=brain,
            goal_store=self.store,
            use_ai=True,
        )
        plan = ai_engine.create_goal_and_plan("Consultar diagnóstico de CPU y memoria RAM solo lectura")
        self.assertTrue(all(t.skill_name != "delete_all_files" for t in plan.tasks))

        # Preservación de sobre untrusted_external_content cuando web_extraction devuelve envelope
        env_sm = _make_clean_skill_manager()
        env_sm.execute_skill_with_envelope = MagicMock(
            return_value=(
                "Resumen web aislado",
                {
                    "trust_level": "untrusted_external_content",
                    "allow_tool_execution_from_content": False,
                    "sources": [{"url": "https://example.com"}],
                },
            )
        )
        env_engine = GoalEngine(skill_manager=env_sm, goal_store=self.store)
        web_plan = env_engine.create_goal_and_plan("Investigar https://example.com en documentación")
        web_res = env_engine.execute_plan(web_plan)
        self.assertEqual(web_res.final_status, GoalStatus.COMPLETED)
        t_web = web_plan.get_task("t1_extract_web")
        assert t_web is not None
        self.assertIsNotNone(t_web.untrusted_envelope)
        self.assertEqual(t_web.untrusted_envelope.get("trust_level"), "untrusted_external_content")

        # No carga módulos pesados de voz/audio al importar e inicializar GoalEngine en proceso limpio
        probe_code = (
            "import sys; "
            "from src.brain.goal_engine import GoalEngine; "
            "from src.brain.goal_console import GoalConsoleHandler; "
            "forbidden = [m for m in ('src.audio.stt','src.audio.tts','src.audio.recorder','faster_whisper') if m in sys.modules]; "
            "assert not forbidden, f'Loaded forbidden modules: {forbidden}'"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe_code],
            cwd=str(settings.BASE_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)


if __name__ == "__main__":
    unittest.main()
