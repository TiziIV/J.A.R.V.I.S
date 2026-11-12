"""Fachada principal del Motor Autónomo de Objetivos y Planificación (N6).

Unifica comprensión en lenguaje natural, descomposición en subtareas,
selección de herramientas reales, persistencia aislada, autorización
explícita, supervisión de ejecución, evaluación y replanificación controlada.

Restricción de arquitectura: no importa ni inicializa módulos de voz,
micrófono, Whisper ni TTS, garantizando operación ligera en consola y tests.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any, Callable, Optional, TYPE_CHECKING

from src.brain.goal_models import (
    Constraints,
    Goal,
    GoalContext,
    GoalEngineLimits,
    GoalError,
    GoalPlan,
    GoalPlanningError,
    GoalStatus,
    GoalTask,
    TERMINAL_GOAL_STATUSES,
    TaskRiskCategory,
    TaskStatus,
)
from src.brain.goal_persistence import GoalStore
from src.brain.goal_planner import GoalPlanningEngine, TaskDecomposer
from src.brain.goal_skill_selector import GoalSkillSelector
from src.brain.goal_supervisor import ExecutionResult, ExecutionSupervisor, PlanEvaluator
from src.brain.goal_understanding import GoalUnderstandingEngine
from src.tools.skill_manager import SkillManager

if TYPE_CHECKING:
    from src.brain.brain_manager import BrainManager
    from src.brain.chain_orchestrator import ChainOrchestrator


@dataclass
class GoalSubmissionResult:
    """Resultado de recibir una petición `/goal`: o bien un plan listo, o preguntas de aclaración."""

    goal: Goal
    plan: Optional[GoalPlan] = None
    needs_clarification: bool = False
    clarification_questions: tuple[str, ...] = ()


class GoalEngine:
    """Orquestador de alto nivel para objetivos autónomos en J.A.R.V.I.S."""

    def __init__(
        self,
        *,
        skill_manager: Optional[SkillManager] = None,
        brain_manager: Optional["BrainManager"] = None,
        chain_orchestrator: Optional["ChainOrchestrator"] = None,
        goal_store: Optional[GoalStore] = None,
        limits: Optional[GoalEngineLimits] = None,
        use_ai: bool = False,
    ) -> None:
        self.skill_manager: SkillManager = skill_manager or SkillManager()
        self.brain_manager: Optional["BrainManager"] = brain_manager
        self.chain_orchestrator: Optional["ChainOrchestrator"] = chain_orchestrator
        self.limits: GoalEngineLimits = limits or GoalEngineLimits()

        self.skill_selector: GoalSkillSelector = GoalSkillSelector(self.skill_manager)
        self.goal_store: GoalStore = goal_store or GoalStore()
        self.understanding: GoalUnderstandingEngine = GoalUnderstandingEngine(
            brain_manager=brain_manager,
            use_ai=use_ai,
        )
        self.decomposer: TaskDecomposer = TaskDecomposer(
            skill_selector=self.skill_selector,
            brain_manager=brain_manager,
            use_ai=use_ai,
        )
        self.planner: GoalPlanningEngine = GoalPlanningEngine(
            skill_selector=self.skill_selector,
            decomposer=self.decomposer,
            default_limits=self.limits,
        )
        self.evaluator: PlanEvaluator = PlanEvaluator()
        self.supervisor: ExecutionSupervisor = ExecutionSupervisor(
            skill_manager=self.skill_manager,
            skill_selector=self.skill_selector,
            chain_orchestrator=chain_orchestrator,
            goal_store=self.goal_store,
            evaluator=self.evaluator,
        )

        self._active_plan: Optional[GoalPlan] = None
        self._pending_clarification_goal: Optional[Goal] = None

    def set_chain_orchestrator(self, chain_orchestrator: "ChainOrchestrator") -> None:
        """Conecta tardíamente `ChainOrchestrator` cuando el usuario lo inicializa."""
        self.chain_orchestrator = chain_orchestrator
        self.supervisor.chain_orchestrator = chain_orchestrator

    def interpret_goal(
        self,
        raw_input: str,
        *,
        context: Optional[GoalContext] = None,
        constraints: Optional[Constraints] = None,
    ) -> Goal:
        """Interpreta una petición en lenguaje natural devolviendo un `Goal` estructurado."""
        return self.understanding.interpret(
            raw_input,
            initial_context=context,
            explicit_constraints=constraints,
        )

    def submit_goal(
        self,
        raw_input: str,
        *,
        context: Optional[GoalContext] = None,
        constraints: Optional[Constraints] = None,
        limits: Optional[GoalEngineLimits] = None,
        persist: bool = True,
    ) -> GoalSubmissionResult:
        """Interpreta el objetivo; si es claro genera y persiste el plan, si es ambiguo solicita aclaración."""
        goal = self.interpret_goal(raw_input, context=context, constraints=constraints)
        if goal.needs_clarification or goal.status == GoalStatus.AWAITING_CLARIFICATION:
            self._pending_clarification_goal = goal
            return GoalSubmissionResult(
                goal=goal,
                plan=None,
                needs_clarification=True,
                clarification_questions=tuple(goal.clarification_questions),
            )

        self._pending_clarification_goal = None
        plan = self.planner.build_plan(goal, limits=limits or self.limits)
        self._active_plan = plan
        if persist:
            self.goal_store.save_plan(plan, set_active=True)
            self.goal_store.append_wal_event(
                plan.plan_id, "plan_created", {"goal_id": goal.goal_id}
            )
        return GoalSubmissionResult(goal=goal, plan=plan, needs_clarification=False)

    def create_goal_and_plan(
        self,
        raw_input: str,
        *,
        context: Optional[GoalContext] = None,
        constraints: Optional[Constraints] = None,
        limits: Optional[GoalEngineLimits] = None,
        custom_tasks: Optional[list[GoalTask]] = None,
        persist: bool = True,
    ) -> GoalPlan:
        """Construye un `GoalPlan` validado directamente o lanza `GoalPlanningError` ante ambigüedad."""
        goal = self.interpret_goal(raw_input, context=context, constraints=constraints)
        plan = self.planner.build_plan(
            goal,
            limits=limits or self.limits,
            custom_tasks=custom_tasks,
        )
        self._pending_clarification_goal = None
        self._active_plan = plan
        if persist:
            self.goal_store.save_plan(plan, set_active=True)
            self.goal_store.append_wal_event(
                plan.plan_id, "plan_created", {"goal_id": goal.goal_id}
            )
        return plan

    def clarify_pending_goal(
        self,
        clarification_text: str,
        *,
        limits: Optional[GoalEngineLimits] = None,
        persist: bool = True,
    ) -> GoalSubmissionResult:
        """Aplica la respuesta aclaratoria del usuario al objetivo pendiente de definición."""
        if self._pending_clarification_goal is None:
            raise GoalPlanningError(
                "No existe ningún objetivo pendiente de aclaración en la sesión actual."
            )
        refined_goal = self.understanding.apply_clarification(
            self._pending_clarification_goal,
            clarification_text,
        )
        if refined_goal.needs_clarification or refined_goal.status == GoalStatus.AWAITING_CLARIFICATION:
            self._pending_clarification_goal = refined_goal
            return GoalSubmissionResult(
                goal=refined_goal,
                plan=None,
                needs_clarification=True,
                clarification_questions=tuple(refined_goal.clarification_questions),
            )

        self._pending_clarification_goal = None
        plan = self.planner.build_plan(refined_goal, limits=limits or self.limits)
        self._active_plan = plan
        if persist:
            self.goal_store.save_plan(plan, set_active=True)
            self.goal_store.append_wal_event(
                plan.plan_id, "plan_created_after_clarification", {"goal_id": refined_goal.goal_id}
            )
        return GoalSubmissionResult(goal=refined_goal, plan=plan, needs_clarification=False)

    def get_active_plan(self, *, restore_from_disk: bool = True) -> Optional[GoalPlan]:
        """Obtiene el plan activo en memoria o lo recupera desde la persistencia aislada."""
        if self._active_plan is not None:
            return self._active_plan
        if restore_from_disk:
            loaded = self.goal_store.load_active_plan()
            if loaded is not None:
                self._active_plan = loaded
            return loaded
        return None

    def authorize_plan(
        self,
        plan: Optional[GoalPlan] = None,
        *,
        task_ids: Optional[list[str]] = None,
        approval_token: Optional[str] = None,
    ) -> list[str]:
        """Registra aprobación explícita sobre las operaciones sensibles de un plan."""
        target_plan = plan or self.get_active_plan()
        if target_plan is None:
            raise GoalError("No hay ningún plan activo para autorizar.")
        return self.supervisor.authorize_sensitive_tasks(
            target_plan,
            task_ids=task_ids,
            approval_token=approval_token,
        )

    def execute_plan(
        self,
        plan: Optional[GoalPlan] = None,
        *,
        cancel_event: Optional[threading.Event] = None,
        approval_callback: Optional[Callable[[GoalPlan, list[GoalTask]], bool]] = None,
        input_fn: Callable[[str], str] = input,
        strict_approval: bool = False,
        enable_replan: bool = True,
    ) -> ExecutionResult:
        """Ejecuta el plan indicado o el plan activo bajo supervisión de políticas y timeouts."""
        target_plan = plan or self.get_active_plan()
        if target_plan is None:
            raise GoalError("No hay ningún plan activo para ejecutar. Usa `/goal <objetivo>` primero.")
        self._active_plan = target_plan
        return self.supervisor.execute_plan(
            target_plan,
            cancel_event=cancel_event,
            approval_callback=approval_callback,
            input_fn=input_fn,
            strict_approval=strict_approval,
            enable_replan=enable_replan,
        )

    def cancel_active_goal(self, reason: str = "Cancelado por el usuario.") -> Optional[GoalPlan]:
        """Cancela el plan activo (en memoria o recuperado) sin alterar tareas ya completadas."""
        target_plan = self.get_active_plan()
        if target_plan is None:
            if self._pending_clarification_goal is not None:
                self._pending_clarification_goal.cancellation_reason = reason
                self._pending_clarification_goal.transition_to(GoalStatus.CANCELLED)
                self._pending_clarification_goal = None
            return None
        return self.supervisor.cancel_goal(target_plan, reason=reason)

    def format_plan_summary(self, plan: GoalPlan) -> str:
        """Formatea el plan actual en texto legible para consola con estados y riesgos."""
        lines: list[str] = [
            f"Objetivo ({plan.goal.goal_id}) [{plan.goal.status.value}]: {plan.goal.raw_input}",
            f"Interpretación: {plan.goal.interpreted_summary}",
            (
                f"Plan ID: {plan.plan_id} | Versión: {plan.version} | "
                f"Replanificaciones: {plan.replan_count}/{plan.limits.max_replans}"
            ),
        ]
        if plan.goal.constraints.read_only:
            lines.append("Restricciones: MODO SOLO LECTURA (sin modificación de archivos).")
        if plan.goal.missing_info:
            lines.append("Información faltante detectada: " + "; ".join(plan.goal.missing_info))
        lines.append("Subtareas:")
        for idx, task in enumerate(plan.tasks, start=1):
            deps = ", ".join(task.depends_on) if task.depends_on else "ninguna"
            skill = task.skill_name or "síntesis_interna"
            approval_tag = " [REQUIERE APROBACIÓN]" if task.requires_approval and not task.approval_granted else ""
            if task.requires_approval and task.approval_granted:
                approval_tag = " [APROBADA]"
            lines.append(
                f"  {idx}. [{task.task_id}] ({task.status.value} | {task.risk_category.value}{approval_tag}) "
                f"{task.title} | skill={skill} | deps={deps}"
            )
            if task.output_summary:
                preview = task.output_summary.replace("\n", " ")[:180]
                lines.append(f"     -> Resultado: {preview}")
            if task.last_error:
                lines.append(f"     -> Error: {task.last_error}")
        return "\n".join(lines)

    def format_status_report(self, plan: Optional[GoalPlan] = None) -> str:
        """Devuelve un resumen del estado actual del plan activo o de la aclaración pendiente."""
        target = plan or self.get_active_plan()
        if target is None:
            if self._pending_clarification_goal is not None:
                qs = "\n  - ".join(self._pending_clarification_goal.clarification_questions)
                return (
                    f"Objetivo pendiente de aclaración ({self._pending_clarification_goal.goal_id}):\n"
                    f"  - {qs}"
                )
            return "No hay ningún objetivo activo actualmente. Usa `/goal <objetivo>` para crear uno."

        completed = sum(1 for t in target.tasks if t.status == TaskStatus.COMPLETED)
        pending_app = [
            t.task_id
            for t in target.tasks
            if t.requires_approval and not t.approval_granted and t.status not in {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED}
        ]
        header = (
            f"Estado del objetivo '{target.goal.goal_id}': {target.goal.status.value.upper()} "
            f"({completed}/{len(target.tasks)} subtareas completadas, "
            f"replans={target.replan_count}/{target.limits.max_replans})."
        )
        if pending_app:
            header += f"\nPendiente de aprobación explícita para: {', '.join(pending_app)}."
        if target.goal.status in TERMINAL_GOAL_STATUSES and target.goal.error_summary:
            header += f"\nDetalle de errores: {target.goal.error_summary}"
        return self.format_plan_summary(target) + "\n" + header
