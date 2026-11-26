"""Supervisión de ejecución, evaluación, autorización y replanificación acotada para N6.

Responsabilidades principales:
1. Ejecutar subtareas respetando estrictamente dependencias (`depends_on`),
   integridad de `plan_hash` y prevención de ejecuciones duplicadas.
2. Bloquear operaciones `SENSITIVE_OPERATION` hasta recibir aprobación explícita
   y vincular la autorización al hash estructural del plan.
3. Delegar obligatoriamente toda edición de archivos/configuración a `ChainOrchestrator`
   + `PolicyEngine`, impidiendo bypasses sobre `FileEditorSkill`.
4. Controlar timeouts por tarea y globales, reintentos acotados y cancelación limpia.
5. Evaluar resultados finales y ejecutar replanificación controlada preservando
   tareas ya completadas e historial completo de auditoría.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
import inspect
import threading
import json
import time
from typing import Any, Callable, Optional, TYPE_CHECKING

from src.brain.exceptions import redact_secrets
from src.brain.goal_memory import (
    GoalMemoryBridge,
    build_skeleton_task_labels,
    canonicalize_n8_task_id_map,
    collect_grounded_anchors,
    separate_grounded_skill_params,
)
from src.brain.goal_models import (
    GoalAuthorizationError,
    GoalError,
    GoalPlan,
    GoalPlanningError,
    GoalPolicyViolationError,
    GoalStateError,
    GoalStatus,
    GoalTask,
    GoalTimeoutError,
    ReplanDecision,
    TERMINAL_GOAL_STATUSES,
    TERMINAL_TASK_STATUSES,
    TaskRiskCategory,
    TaskStatus,
    utc_now_iso,
)
from src.brain.goal_persistence import EphemeralGoalStore, GoalStore
from src.brain.goal_planner import validate_plan_dag
from src.brain.goal_skill_selector import FILE_MUTATING_SKILLS, GoalSkillSelector
from src.context.policy import sanitize_untrusted_label
from src.tools.skill_manager import SkillManager

if TYPE_CHECKING:
    from src.brain.chain_orchestrator import ChainOrchestrator


@dataclass
class ExecutionResult:
    """Resumen estructurado del resultado de supervisar un `GoalPlan`."""

    plan: GoalPlan
    final_status: GoalStatus
    completed_task_ids: list[str] = field(default_factory=list)
    failed_task_ids: list[str] = field(default_factory=list)
    pending_approval_task_ids: list[str] = field(default_factory=list)
    stopped_for_approval: bool = False
    cancelled: bool = False
    replan_count: int = 0
    summary_text: str = ""
    errors: list[str] = field(default_factory=list)


class PlanEvaluator:
    """Evalúa cumplimiento de subtareas, éxito global y necesidad de replanificación."""

    def evaluate(self, plan: GoalPlan) -> GoalStatus:
        """Determina el estado agregado del objetivo en función de sus subtareas actuales."""
        if plan.goal.status == GoalStatus.CANCELLED:
            return GoalStatus.CANCELLED

        statuses = [task.status for task in plan.tasks]
        if not statuses:
            return GoalStatus.FAILED

        if all(st in {TaskStatus.COMPLETED, TaskStatus.SKIPPED} for st in statuses) and any(
            st == TaskStatus.COMPLETED for st in statuses
        ):
            return GoalStatus.COMPLETED

        has_completed = any(st == TaskStatus.COMPLETED for st in statuses)
        has_waiting_approval = any(
            st in {TaskStatus.REQUIRES_APPROVAL, TaskStatus.APPROVED} for st in statuses
        )
        if has_waiting_approval and not any(st == TaskStatus.FAILED for st in statuses):
            return GoalStatus.AWAITING_APPROVAL

        if has_completed:
            return GoalStatus.PARTIAL

        return GoalStatus.FAILED

    def should_replan(self, plan: GoalPlan, failed_task: GoalTask) -> bool:
        """Decide si corresponde intentar una ruta alternativa sin exceder `max_replans`."""
        if plan.replan_count >= plan.limits.max_replans:
            return False
        if plan.goal.status == GoalStatus.CANCELLED:
            return False
        # Las violaciones de política o falta de autorización no se esquivan replanificando.
        if failed_task.requires_approval and not failed_task.approval_granted:
            return False
        return True

    def build_fallback_tasks(
        self,
        plan: GoalPlan,
        failed_task: GoalTask,
        *,
        selector: GoalSkillSelector,
    ) -> list[GoalTask]:
        """Construye subtarea alternativa segura preservando todo el trabajo completado."""
        gen = plan.replan_count + 1
        # Evita superar el límite total de subtareas antes de crear el reemplazo.
        if len(plan.tasks) >= plan.limits.max_tasks:
            return []

        fallback_id = f"{failed_task.task_id}_r{gen}"
        existing_ids = {t.task_id for t in plan.tasks}
        while fallback_id in existing_ids:
            gen += 1
            fallback_id = f"{failed_task.task_id}_r{gen}"

        # Estrategia 1: si falló extracción/búsqueda web, degradar a diagnóstico local
        # solo si aún no fue ejecutado `system_info`; si ya existe evidencia previa, reutilizarla en síntesis.
        already_ran_sysinfo = any(
            t.skill_name == "system_info" and t.status == TaskStatus.COMPLETED
            for t in plan.tasks
        )
        if (
            failed_task.skill_name in {"web_extraction", "web_search"}
            and selector.has_skill("system_info")
            and not already_ran_sysinfo
        ):
            return [
                GoalTask(
                    task_id=fallback_id,
                    title=f"Continuar con diagnóstico local alternativo ({failed_task.title})",
                    description=(
                        f"Ruta de contingencia tras fallo en '{failed_task.skill_name}': "
                        "releva datos locales disponibles sin exponer ni forzar red."
                    ),
                    skill_name="system_info",
                    skill_params={},
                    risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                    requires_approval=False,
                    depends_on=[
                        dep
                        for dep in failed_task.depends_on
                        if (t := plan.get_task(dep)) is not None and t.status == TaskStatus.COMPLETED
                    ],
                    expected_output="Diagnóstico local de respaldo.",
                    max_retries=0,
                    timeout_sec=plan.limits.task_timeout_sec,
                    replan_generation=plan.replan_count + 1,
                    metadata={"replan_for": failed_task.task_id},
                )
            ]

        # Estrategia 2: si falló una tarea de baja criticidad no mutante, ofrecer síntesis de contingencia.
        if failed_task.risk_category != TaskRiskCategory.SENSITIVE_OPERATION and failed_task.skill_name is not None:
            return [
                GoalTask(
                    task_id=fallback_id,
                    title=f"Consolidar alternativa de contingencia ({failed_task.title})",
                    description=(
                        f"Resume los resultados previos disponibles y documenta la limitación tras "
                        f"fallo de '{failed_task.skill_name}'."
                    ),
                    skill_name=None,
                    skill_params={},
                    risk_category=TaskRiskCategory.SAFE_PLANNING,
                    requires_approval=False,
                    depends_on=[
                        dep
                        for dep in failed_task.depends_on
                        if (t := plan.get_task(dep)) is not None and t.status == TaskStatus.COMPLETED
                    ],
                    expected_output="Síntesis parcial con diagnóstico del obstáculo.",
                    max_retries=0,
                    timeout_sec=plan.limits.task_timeout_sec,
                    replan_generation=plan.replan_count + 1,
                    metadata={"replan_for": failed_task.task_id},
                )
            ]

        return []


class ExecutionSupervisor:
    """Orquesta la ejecución controlada, verificada y cancelable de un `GoalPlan`."""

    def __init__(
        self,
        *,
        skill_manager: Optional[SkillManager] = None,
        skill_selector: Optional[GoalSkillSelector] = None,
        chain_orchestrator: Optional["ChainOrchestrator"] = None,
        goal_store: Optional[GoalStore | EphemeralGoalStore] = None,
        ephemeral_goal_store: Optional[EphemeralGoalStore] = None,
        evaluator: Optional[PlanEvaluator] = None,
        memory_capture_service: Optional[Any] = None,
        memory_bridge: Optional[GoalMemoryBridge] = None,
        brain_manager: Optional[Any] = None,
        is_private_mode_fn: Optional[Callable[[], bool]] = None,
        allow_history_in_private_fn: Optional[Callable[[], bool]] = None,
        replan_factory: Optional[
            Callable[..., list[GoalTask]]
        ] = None,
    ) -> None:
        self.skill_manager: SkillManager = skill_manager or SkillManager()
        self.skill_selector: GoalSkillSelector = skill_selector or GoalSkillSelector(
            self.skill_manager
        )
        self.chain_orchestrator: Optional["ChainOrchestrator"] = chain_orchestrator
        self.goal_store: Optional[GoalStore | EphemeralGoalStore] = goal_store
        self.ephemeral_goal_store: EphemeralGoalStore = ephemeral_goal_store or EphemeralGoalStore()
        self.evaluator: PlanEvaluator = evaluator or PlanEvaluator()
        self.memory_capture_service: Optional[Any] = memory_capture_service
        self.memory_bridge: Optional[GoalMemoryBridge] = memory_bridge
        self.brain_manager: Optional[Any] = brain_manager
        self._is_private_mode_fn: Optional[Callable[[], bool]] = is_private_mode_fn
        self._allow_history_in_private_fn: Optional[Callable[[], bool]] = allow_history_in_private_fn
        self.replan_factory = replan_factory

    def _is_private_active(self) -> bool:
        if self._is_private_mode_fn is not None and self._is_private_mode_fn() is True:
            return True
        if self.memory_bridge is not None and self.memory_bridge.memory_engine is not None:
            if getattr(self.memory_bridge.memory_engine, "is_private_mode", False) is True:
                return True
        if self.brain_manager is not None:
            mem = getattr(self.brain_manager, "memory", None)
            if mem is not None and getattr(mem, "is_private_mode", False) is True:
                return True
        return False

    def _allow_history_active(self) -> bool:
        if self._allow_history_in_private_fn is not None and self._allow_history_in_private_fn() is False:
            return False
        if self.memory_bridge is not None and self.memory_bridge.memory_engine is not None:
            if getattr(self.memory_bridge.memory_engine, "allow_history_reads", True) is False:
                return False
        if self.brain_manager is not None:
            mem = getattr(self.brain_manager, "memory", None)
            if mem is not None and getattr(mem, "private_allow_history", True) is False:
                return False
        return True

    @property
    def active_goal_store(self) -> Optional[GoalStore | EphemeralGoalStore]:
        if self._is_private_active():
            return self.ephemeral_goal_store
        return self.goal_store

    def set_memory_capture_service(self, memory_capture_service: Optional[Any]) -> None:
        """Asocia o reemplaza el coordinador desacoplado de captura verificada N8."""
        self.memory_capture_service = memory_capture_service

    def set_memory_bridge(self, memory_bridge: Optional[GoalMemoryBridge]) -> None:
        """Asocia o reemplaza el puente consultivo RAM N8 <-> N6."""
        self.memory_bridge = memory_bridge

    def _clear_plan_ram_context(self, plan: GoalPlan) -> None:
        """Libera sobres N8 y anotaciones efímeras al finalizar o cancelar un objetivo."""
        plan.ephemeral_annotations.clear()
        plan.goal.clear_ephemeral_annotations()
        for t in plan.tasks:
            t.clear_ephemeral_annotations()
        if self.memory_bridge is not None:
            self.memory_bridge.clear_goal_envelopes(
                goal_id=plan.goal.goal_id,
                plan_id=plan.plan_id,
            )

    def authorize_sensitive_tasks(
        self,
        plan: GoalPlan,
        *,
        task_ids: Optional[list[str]] = None,
        approval_token: Optional[str] = None,
    ) -> list[str]:
        """Otorga autorización explícita vinculada al `plan_hash` vigente.

        Nunca valida planes alterados después de su creación ni saltea `PolicyEngine`.
        """
        if not plan.verify_integrity():
            raise GoalAuthorizationError(
                f"El plan '{plan.plan_id}' fue alterado fuera de control; se revoca la autorización."
            )

        if plan.goal.status in TERMINAL_GOAL_STATUSES:
            raise GoalStateError(
                f"No se puede aprobar un objetivo en estado terminal '{plan.goal.status.value}'."
            )

        bound_hash = plan.bind_approval()
        target_ids = set(task_ids) if task_ids is not None else {
            task.task_id for task in plan.sensitive_tasks()
        }
        approved_ids: list[str] = []

        for task in plan.tasks:
            if task.task_id not in target_ids:
                continue
            if task.status in TERMINAL_TASK_STATUSES:
                continue
            token = approval_token or f"N6-APPROVED:{bound_hash[:12]}"
            task.grant_approval(approval_token=token, approval_hash=bound_hash)
            approved_ids.append(task.task_id)

        if approved_ids and plan.goal.status in {GoalStatus.PLANNED, GoalStatus.AWAITING_APPROVAL}:
            plan.goal.transition_to(GoalStatus.APPROVED)

        plan.record_event(
            "approval_granted",
            f"Aprobación explícita registrada para subtareas: {', '.join(approved_ids) or '(ninguna)'}.",
            to_status=plan.goal.status.value,
            details={"approval_hash": bound_hash, "approved_task_ids": approved_ids},
        )
        self._persist(plan, "approval_granted", {"approved_task_ids": approved_ids})
        return approved_ids

    def cancel_goal(self, plan: GoalPlan, *, reason: str = "Cancelado por el usuario.") -> GoalPlan:
        """Cancela ordenadamente las tareas pendientes preservando las ya completadas."""
        if plan.goal.status in TERMINAL_GOAL_STATUSES:
            return plan

        clean_reason = redact_secrets((reason or "").strip() or "Cancelado por el usuario.")
        cancelled_ids: list[str] = []
        for task in plan.tasks:
            if task.status not in TERMINAL_TASK_STATUSES:
                task.transition_to(TaskStatus.CANCELLED)
                cancelled_ids.append(task.task_id)

        plan.goal.cancellation_reason = clean_reason
        plan.goal.transition_to(GoalStatus.CANCELLED)
        plan.record_event(
            "goal_cancelled",
            f"Objetivo cancelado: {clean_reason}",
            to_status=GoalStatus.CANCELLED.value,
            details={"cancelled_task_ids": cancelled_ids},
        )
        self._persist(plan, "goal_cancelled", {"cancelled_task_ids": cancelled_ids, "reason": clean_reason})
        self._clear_plan_ram_context(plan)
        return plan

    def execute_plan(
        self,
        plan: GoalPlan,
        *,
        cancel_event: Optional[threading.Event] = None,
        approval_callback: Optional[Callable[[GoalPlan, list[GoalTask]], bool]] = None,
        input_fn: Callable[[str], str] = input,
        strict_approval: bool = False,
        enable_replan: bool = True,
    ) -> ExecutionResult:
        """Supervisa el ciclo completo de ejecución DAG, timeouts, reintentos y replanificación."""
        if not plan.verify_integrity():
            raise GoalAuthorizationError(
                f"Integridad estructural comprometida en el plan '{plan.plan_id}'."
            )

        if plan.goal.status in TERMINAL_GOAL_STATUSES:
            raise GoalStateError(
                f"No es posible ejecutar el objetivo '{plan.goal.goal_id}' porque ya se encuentra en estado '{plan.goal.status.value}'."
            )

        if plan.goal.needs_clarification or plan.goal.status == GoalStatus.AWAITING_CLARIFICATION:
            raise GoalPlanningError(
                "El objetivo requiere aclaración antes de poder ejecutarse."
            )

        # Revalidación obligatoria de políticas antes de iniciar el ciclo.
        for task in plan.tasks:
            self.skill_selector.validate_task(task, plan.goal.constraints)
        validate_plan_dag(plan.tasks, plan.limits)

        # Verificar si el plan está en AWAITING_APPROVAL sin ninguna autorización previa y con strict_approval.
        pending_sensitive_initial = [
            t for t in plan.tasks if t.requires_approval and not t.approval_granted and t.status not in TERMINAL_TASK_STATUSES
        ]
        if strict_approval and pending_sensitive_initial and not any(
            not t.requires_approval and t.status not in TERMINAL_TASK_STATUSES for t in plan.tasks
        ):
            raise GoalAuthorizationError(
                "El plan contiene operaciones sensibles sin aprobación explícita del usuario."
            )

        # Transición controlada del objetivo hacia RUNNING si ya puede iniciar pasos no sensibles o aprobados.
        if plan.goal.status == GoalStatus.AWAITING_APPROVAL:
            has_unapproved_only = not any(
                not t.requires_approval and t.status in {TaskStatus.PENDING, TaskStatus.READY}
                for t in plan.tasks
            )
            if has_unapproved_only:
                if approval_callback is not None and approval_callback(plan, pending_sensitive_initial):
                    self.authorize_sensitive_tasks(
                        plan, task_ids=[t.task_id for t in pending_sensitive_initial]
                    )
                    plan.goal.transition_to(GoalStatus.RUNNING)
                else:
                    if strict_approval:
                        raise GoalAuthorizationError(
                            "El objetivo espera aprobación explícita antes de iniciar tareas sensibles."
                        )
                    return ExecutionResult(
                        plan=plan,
                        final_status=GoalStatus.AWAITING_APPROVAL,
                        pending_approval_task_ids=[t.task_id for t in pending_sensitive_initial],
                        stopped_for_approval=True,
                        summary_text=(
                            "Ejecución detenida: existen operaciones sensibles pendientes de aprobación explícita."
                        ),
                    )
            else:
                # Primero se ejecutan las subtareas seguras/de diagnóstico; la confirmación se pedirá
                # una sola vez al alcanzar la frontera de la primera operación sensible.
                plan.goal.transition_to(GoalStatus.REPLANNING)
                plan.goal.transition_to(GoalStatus.PLANNED)
                plan.goal.transition_to(GoalStatus.RUNNING)
        elif plan.goal.status in {GoalStatus.PLANNED, GoalStatus.APPROVED, GoalStatus.PAUSED, GoalStatus.REPLANNING}:
            plan.goal.transition_to(GoalStatus.RUNNING)

        plan.record_event(
            "execution_started",
            f"Inicio de supervisión de ejecución para el objetivo '{plan.goal.goal_id}'.",
            to_status=GoalStatus.RUNNING.value,
        )
        self._persist(plan, "execution_started", {})

        start_monotonic = time.monotonic()
        errors: list[str] = []
        stopped_for_approval = False

        while True:
            if cancel_event is not None and cancel_event.is_set():
                self.cancel_goal(plan, reason="Cancelación solicitada durante la ejecución.")
                return self._build_result(plan, cancelled=True, errors=errors)

            if (time.monotonic() - start_monotonic) > plan.limits.total_timeout_sec:
                timeout_msg = (
                    f"Tiempo límite global excedido ({plan.limits.total_timeout_sec:.1f}s)."
                )
                errors.append(timeout_msg)
                self._fail_remaining_due_to_timeout(plan, timeout_msg)
                return self._build_result(plan, errors=errors)

            self._refresh_dependency_readiness(plan)
            next_task = self._select_next_runnable_or_blocked_task(plan)

            if next_task is None:
                # Revisar si hay alguna tarea lista pero bloqueada esperando aprobación explícita.
                awaiting = [
                    task
                    for task in plan.tasks
                    if task.status == TaskStatus.REQUIRES_APPROVAL
                    and self._dependencies_completed(plan, task)
                ]
                if awaiting:
                    if approval_callback is not None and approval_callback(plan, awaiting):
                        self.authorize_sensitive_tasks(plan, task_ids=[t.task_id for t in awaiting])
                        continue
                    if strict_approval:
                        raise GoalAuthorizationError(
                            "Operación sensible bloqueada por falta de aprobación explícita: "
                            + ", ".join(t.task_id for t in awaiting)
                        )
                    stopped_for_approval = True
                    if plan.goal.status != GoalStatus.AWAITING_APPROVAL:
                        plan.goal.transition_to(GoalStatus.AWAITING_APPROVAL)
                    plan.record_event(
                        "awaiting_approval",
                        "Ejecución pausada en frontera de seguridad esperando aprobación explícita.",
                        to_status=GoalStatus.AWAITING_APPROVAL.value,
                        details={"pending_task_ids": [t.task_id for t in awaiting]},
                    )
                    self._persist(plan, "awaiting_approval", {"pending": [t.task_id for t in awaiting]})
                    break
                # No quedan tareas ejecutables ni aprobables.
                break

            try:
                self.execute_single_task(
                    plan,
                    next_task,
                    input_fn=input_fn,
                )
            except GoalAuthorizationError as auth_err:
                errors.append(str(auth_err))
                if strict_approval:
                    raise
                stopped_for_approval = True
                if plan.goal.status == GoalStatus.RUNNING:
                    plan.goal.transition_to(GoalStatus.AWAITING_APPROVAL)
                self._persist(plan, "awaiting_approval", {"task_id": next_task.task_id, "reason": str(auth_err)})
                break
            except GoalPolicyViolationError as pol_err:
                clean_msg = redact_secrets(str(pol_err))
                errors.append(clean_msg)
                next_task.mark_failed(clean_msg)
                self._block_dependents(plan, next_task.task_id)
                self._persist(plan, "policy_violation", {"task_id": next_task.task_id, "error": clean_msg})
                break
            except Exception as exc:
                clean_msg = redact_secrets(str(exc)) or type(exc).__name__
                if next_task.retry_count < min(next_task.max_retries, plan.limits.max_retries):
                    next_task.schedule_retry(clean_msg)
                    plan.record_event(
                        "task_retry_scheduled",
                        f"Reintento {next_task.retry_count}/{next_task.max_retries} en '{next_task.task_id}': {clean_msg}",
                        task_id=next_task.task_id,
                        from_status=TaskStatus.RUNNING.value,
                        to_status=next_task.status.value,
                    )
                    self._persist(plan, "task_retry", {"task_id": next_task.task_id, "retry": next_task.retry_count})
                    continue

                # Se agotaron reintentos: marcar FAILED
                next_task.mark_failed(clean_msg)
                errors.append(f"[{next_task.task_id}] {clean_msg}")
                plan.record_event(
                    "task_failed",
                    f"La subtarea '{next_task.task_id}' falló definitivamente: {clean_msg}",
                    task_id=next_task.task_id,
                    from_status=TaskStatus.RUNNING.value,
                    to_status=TaskStatus.FAILED.value,
                )
                self._block_dependents(plan, next_task.task_id)
                self._persist(plan, "task_failed", {"task_id": next_task.task_id, "error": clean_msg})

                if enable_replan and self.evaluator.should_replan(plan, next_task):
                    replanned = self._attempt_replan(plan, next_task, reason=clean_msg)
                    if replanned:
                        continue

        # Cierre evaluado del objetivo si no quedó esperando aprobación ni cancelado.
        if not stopped_for_approval and plan.goal.status not in TERMINAL_GOAL_STATUSES:
            evaluated = self.evaluator.evaluate(plan)
            if evaluated == GoalStatus.AWAITING_APPROVAL:
                if plan.goal.status != GoalStatus.AWAITING_APPROVAL:
                    plan.goal.transition_to(GoalStatus.AWAITING_APPROVAL)
            else:
                if errors:
                    plan.goal.error_summary = "; ".join(errors)
                plan.goal.transition_to(evaluated)
                plan.record_event(
                    "goal_finished",
                    f"Ejecución finalizada en estado '{evaluated.value}'.",
                    to_status=evaluated.value,
                )
                self._persist(plan, "goal_finished", {"status": evaluated.value})

        final_result = self._build_result(
            plan,
            stopped_for_approval=stopped_for_approval,
            errors=errors,
        )
        if (
            self.memory_capture_service is not None
            and not stopped_for_approval
            and plan.goal.status in {GoalStatus.COMPLETED, GoalStatus.PARTIAL}
        ):
            try:
                self.memory_capture_service.handle_n6_execution_completed(
                    plan=plan,
                    execution_result=final_result,
                )
            except Exception:
                # Aislamiento estricto: un fallo de memoria jamás altera el ExecutionResult N6.
                pass
        if not stopped_for_approval and plan.goal.status in TERMINAL_GOAL_STATUSES:
            self._clear_plan_ram_context(plan)
        return final_result

    def execute_single_task(
        self,
        plan: GoalPlan,
        task: GoalTask,
        *,
        input_fn: Callable[[str], str] = input,
    ) -> dict[str, Any]:
        """Ejecuta una sola subtarea verificando integridad, dependencias, permisos y timeout."""
        if not plan.verify_integrity():
            raise GoalAuthorizationError(
                f"Integridad del plan '{plan.plan_id}' comprometida antes de ejecutar '{task.task_id}'."
            )

        if task.status in TERMINAL_TASK_STATUSES:
            raise GoalStateError(
                f"La subtarea '{task.task_id}' ya se encuentra en estado terminal '{task.status.value}' y no puede reejecutarse."
            )

        # Verifica explícitamente todas las dependencias previas.
        for dep_id in task.depends_on:
            dep_task = plan.get_task(dep_id)
            if dep_task is None or dep_task.status != TaskStatus.COMPLETED:
                raise GoalStateError(
                    f"La subtarea '{task.task_id}' no puede ejecutarse: su dependencia '{dep_id}' no está COMPLETED."
                )

        # Verifica invariante de autorización y política de restricciones.
        selection = self.skill_selector.validate_task(task, plan.goal.constraints)
        if task.requires_approval and not task.approval_granted:
            raise GoalAuthorizationError(
                f"La subtarea sensible '{task.task_id}' requiere aprobación explícita previa."
            )

        if task.status == TaskStatus.PENDING:
            target = TaskStatus.APPROVED if task.approval_granted else TaskStatus.READY
            if task.requires_approval and not task.approval_granted:
                raise GoalAuthorizationError(
                    f"La subtarea '{task.task_id}' debe aprobarse antes de ejecutarse."
                )
            task.transition_to(TaskStatus.READY if not task.requires_approval else TaskStatus.REQUIRES_APPROVAL)
            if task.requires_approval:
                task.transition_to(TaskStatus.APPROVED)

        if task.status == TaskStatus.REQUIRES_APPROVAL:
            raise GoalAuthorizationError(
                f"La subtarea '{task.task_id}' está en REQUIRES_APPROVAL sin confirmación aprobada."
            )

        task.started_at = task.started_at or utc_now_iso()
        task.transition_to(TaskStatus.RUNNING)
        plan.record_event(
            "task_started",
            f"Ejecutando subtarea '{task.task_id}': {task.title}",
            task_id=task.task_id,
            to_status=TaskStatus.RUNNING.value,
        )
        self._persist(plan, "task_started", {"task_id": task.task_id})

        upstream_outputs = self._collect_upstream_outputs(plan, task)
        effective_timeout = min(task.timeout_sec, plan.limits.task_timeout_sec)

        if selection.requires_chain_orchestrator:
            result_data = self._run_with_timeout(
                lambda: self._execute_via_chain_orchestrator(
                    plan=plan,
                    task=task,
                    input_fn=input_fn,
                ),
                timeout_sec=effective_timeout,
                task_id=task.task_id,
            )
        elif task.skill_name is None:
            result_data = self._run_with_timeout(
                lambda: self._execute_internal_synthesis(plan, task, upstream_outputs),
                timeout_sec=effective_timeout,
                task_id=task.task_id,
            )
        else:
            result_data = self._run_with_timeout(
                lambda: self._execute_registered_skill(plan, task, upstream_outputs),
                timeout_sec=effective_timeout,
                task_id=task.task_id,
            )

        summary = str(result_data.get("output_summary") or "").strip()
        evidence = dict(result_data.get("evidence") or {})
        envelope = result_data.get("untrusted_envelope")

        task.mark_completed(
            output_summary=summary,
            evidence=evidence,
            untrusted_envelope=envelope,
        )
        plan.record_event(
            "task_completed",
            f"Subtarea '{task.task_id}' completada con evidencia verificable.",
            task_id=task.task_id,
            from_status=TaskStatus.RUNNING.value,
            to_status=TaskStatus.COMPLETED.value,
        )
        self._persist(plan, "task_completed", {"task_id": task.task_id})
        return result_data

    def _run_with_timeout(
        self,
        fn: Callable[[], dict[str, Any]],
        *,
        timeout_sec: float,
        task_id: str,
    ) -> dict[str, Any]:
        """Ejecuta una operación acotada por timeout estricto sin bloquear indefinidamente."""
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"n6-{task_id}")
        future = executor.submit(fn)
        try:
            return future.result(timeout=timeout_sec)
        except FutureTimeoutError as err:
            future.cancel()
            raise GoalTimeoutError(
                f"La subtarea '{task_id}' excedió el timeout de {timeout_sec:.2f}s."
            ) from err
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _execute_internal_synthesis(
        self,
        plan: GoalPlan,
        task: GoalTask,
        upstream_outputs: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Ejecuta tareas puramente cognitivas (`SAFE_PLANNING`) aislando datos externos."""
        # Remediación F-03: propagar trazabilidad transitiva N8 desde dependencias upstream
        # para impedir que una síntesis posterior que consume datos N8 pase como limpia.
        upstream_n8_tainted = any(
            bool(dep_data.get("n8_tainted"))
            or (
                isinstance(dep_data.get("evidence"), dict)
                and (
                    dep_data["evidence"].get("n8_exposed_at_exec")
                    or dep_data["evidence"].get("n8_tainted")
                )
            )
            or (
                isinstance(dep_data.get("untrusted_envelope"), dict)
                and (
                    dep_data["untrusted_envelope"].get("trust_level") == "untrusted_long_term_memory"
                    or dep_data["untrusted_envelope"].get("provenance") == "n8_memory"
                    or bool(dep_data["untrusted_envelope"].get("historical_references"))
                )
            )
            for dep_data in upstream_outputs.values()
        )
        if upstream_n8_tainted:
            task.n8_exposed_at_exec = True
            plan.n8_context_exposed = True

        snippets: list[str] = []
        untrusted_count = 0
        for dep_id, dep_data in upstream_outputs.items():
            if dep_data.get("untrusted_envelope") or dep_data.get("n8_tainted"):
                untrusted_count += 1
                # El resumen se conserva únicamente dentro del JSON de observaciones escapado.
            else:
                snippets.append(f"[{dep_id}] {dep_data.get('summary', '')}")

        raw_hw_ctx = plan.goal.context.hardware or {}
        safe_hw_ctx: dict[str, Any] = {}
        for hw_k, hw_v in raw_hw_ctx.items():
            clean_k = sanitize_untrusted_label(str(hw_k), max_len=48)
            if isinstance(hw_v, str):
                safe_hw_ctx[clean_k] = sanitize_untrusted_label(hw_v, max_len=120)
            else:
                safe_hw_ctx[clean_k] = redact_secrets(hw_v)

        if safe_hw_ctx:
            untrusted_count += 1

        # Revalidación obligatoria de sobres RAM N8 antes de utilizarlos en síntesis interna.
        n8_history_items: list[dict[str, Any]] = []
        n8_query_id_used: Optional[str] = None
        if self.memory_bridge is not None and self._allow_history_active():
            valid_env, _reval = self.memory_bridge.revalidate_envelope(
                goal=plan.goal,
                plan=plan,
                allow_history_in_private=self._allow_history_active(),
            )
            if valid_env is not None and valid_env.items:
                n8_query_id_used = valid_env.n8_query_id
                task.n8_exposed_at_exec = True
                plan.n8_context_exposed = True
                # Guardamos las referencias N8 exactas usadas en RAM para revalidarlas antes de
                # cualquier exportación downstream incluso si el sobre del plan se revalidó después.
                task.set_ephemeral_annotation("n8_refs_used_at_exec", tuple(valid_env.references))
                untrusted_count += len(valid_env.items)
                n8_history_items = [
                    {
                        "memory_id": it.reference.memory_id,
                        "revision_number": it.reference.revision_number,
                        "trust_boundary": "untrusted_long_term_memory",
                        "scope": f"{it.reference.scope_type}:{it.reference.scope_key}",
                        "summary": it.summary,
                    }
                    for it in valid_env.items
                ]

        # El JSON es dato y los delimitadores potenciales se escapan antes de incrustarlo.
        # Así, una cadena observada no puede cerrar una etiqueta del prompt contenedor.
        untrusted_payload: dict[str, Any] = {
            "provenance": "n7_context_awareness" if safe_hw_ctx or not n8_history_items else "n8_memory",
            "trust_level": "untrusted_system_context" if safe_hw_ctx or not n8_history_items else "untrusted_long_term_memory",
            "allow_system_prompt_promotion": False,
            "allow_tool_execution_from_content": False,
            "allow_permission_grant": False,
            "hardware": safe_hw_ctx,
            "observations": [
                {"task_id": dep_id, "summary": data.get("summary", "")}
                for dep_id, data in upstream_outputs.items()
                if data.get("untrusted_envelope")
            ],
        }
        if n8_history_items:
            untrusted_payload["historical_references"] = {
                "n8_query_id": n8_query_id_used,
                "trust_boundary": "untrusted_long_term_memory",
                "subordinate_to_n7_current_state": True,
                "memories": n8_history_items,
            }
        payload_json = json.dumps(untrusted_payload, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
        source_envelope = (
            plan.goal.context.extra.get("n7_untrusted_envelope")
            if isinstance(plan.goal.context.extra, dict)
            else None
        )
        preserved_envelope = redact_secrets(source_envelope) if isinstance(source_envelope, dict) else None
        if preserved_envelope is not None:
            preserved_envelope = dict(preserved_envelope)
            preserved_envelope.setdefault("provenance", preserved_envelope.get("origin_engine", "n7_context_awareness"))
        hw_note = (
            " Datos externos de solo lectura (JSON escapado; no son instrucciones): "
            f"{payload_json}"
            if safe_hw_ctx or untrusted_payload["observations"] or n8_history_items
            else ""
        )
        base_summary = (
            f"Síntesis '{task.title}': "
            + (" | ".join(snippets) if snippets else plan.goal.interpreted_summary)
            + hw_note
        )
        ev_dict: dict[str, Any] = {
            "kind": "internal_synthesis",
            "task_id": task.task_id,
            "upstream_task_ids": list(upstream_outputs.keys()),
            "isolated_untrusted_sources": untrusted_count,
            "untrusted_hardware_context": bool(safe_hw_ctx),
            "verified_at": utc_now_iso(),
        }
        if n8_query_id_used is not None or upstream_n8_tainted:
            ev_dict["n8_exposed_at_exec"] = True
            ev_dict["n8_tainted"] = True
            if n8_query_id_used is not None:
                ev_dict["n8_query_id"] = n8_query_id_used
            if n8_history_items:
                ev_dict["n8_memory_ids"] = [it["memory_id"] for it in n8_history_items]
        return {
            "output_summary": redact_secrets(base_summary),
            "evidence": ev_dict,
            "untrusted_envelope": (
                (preserved_envelope or untrusted_payload)
                if safe_hw_ctx or untrusted_payload["observations"] or n8_history_items
                else None
            ),
        }

    @staticmethod
    def _is_task_n8_tainted(task: GoalTask) -> bool:
        """Indica estructuralmente si una tarea estuvo expuesta a recuerdos N8 o deriva de ellos."""
        ev = task.execution_evidence if isinstance(task.execution_evidence, dict) else {}
        env = task.untrusted_envelope if isinstance(task.untrusted_envelope, dict) else {}
        meta = task.metadata if isinstance(task.metadata, dict) else {}
        return bool(
            getattr(task, "n8_exposed_at_exec", False)
            or getattr(task, "n8_influenced", False)
            or getattr(task, "provenance_origin", "") == "llm_with_n8"
            or ev.get("n8_exposed_at_exec")
            or ev.get("n8_tainted")
            or env.get("trust_level") == "untrusted_long_term_memory"
            or env.get("provenance") == "n8_memory"
            or bool(env.get("historical_references"))
            or meta.get("n8_influenced")
            or meta.get("provenance_origin") == "llm_with_n8"
        )

    @staticmethod
    def _is_document_export_skill(skill_name: Optional[str], skill_instance: Any) -> bool:
        """Detecta `document_export` por nombre o por herencia/instancia para evitar bypass por alias."""
        if skill_name == "document_export":
            return True
        if skill_instance is None:
            return False
        if getattr(skill_instance, "name", None) == "document_export":
            return True
        try:
            from skills.document_export_skill import DocumentExportSkill

            if isinstance(skill_instance, DocumentExportSkill):
                return True
        except Exception:
            pass
        wrapped = getattr(skill_instance, "wrapped_skill", None) or getattr(skill_instance, "_skill", None)
        if wrapped is not None and wrapped is not skill_instance:
            return ExecutionSupervisor._is_document_export_skill(None, wrapped)
        return False

    @classmethod
    def _is_file_mutating_skill(cls, task: GoalTask, skill_instance: Any) -> bool:
        """
        Determina estructuralmente si una skill puede crear, editar o exportar archivos en disco
        (cubre `FILE_MUTATING_SKILLS`, metadatos, herencia de `FileEditorSkill`/`DocumentExportSkill`
        y wrappers declarados para impedir bypass por nombres alternativos en modo privado).
        """
        if task.skill_name in FILE_MUTATING_SKILLS:
            return True
        if isinstance(task.metadata, dict) and bool(task.metadata.get("modifies_files")):
            return True
        if skill_instance is None:
            return False
        if bool(getattr(skill_instance, "mutates_files", False)) or bool(getattr(skill_instance, "modifies_files", False)):
            return True
        if getattr(skill_instance, "name", None) in FILE_MUTATING_SKILLS:
            return True
        if cls._is_document_export_skill(task.skill_name, skill_instance):
            return True
        try:
            from skills.file_editor_skill import FileEditorSkill

            if isinstance(skill_instance, FileEditorSkill):
                return True
        except Exception:
            pass
        wrapped = getattr(skill_instance, "wrapped_skill", None) or getattr(skill_instance, "_skill", None)
        if wrapped is not None and wrapped is not skill_instance:
            return cls._is_file_mutating_skill(task, wrapped)
        return False

    def _execute_registered_skill(
        self,
        plan: GoalPlan,
        task: GoalTask,
        upstream_outputs: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Invoca una skill real del `SkillManager` preservando fronteras de confianza y privacidad."""
        assert task.skill_name is not None
        params = dict(task.skill_params)
        skill_instance = self.skill_manager.skills.get(task.skill_name)

        # Remediación F-03.A: en modo privado (`/privado` y `/privado --sin-historial`)
        # se bloquea estructuralmente toda skill capaz de mutar o exportar archivos en disco.
        if self._is_private_active() and self._is_file_mutating_skill(task, skill_instance):
            raise GoalPolicyViolationError(
                f"Operación de escritura en disco mediante '{task.skill_name}' bloqueada durante el modo privado "
                "para impedir persistencia de datos privados o históricos."
            )

        is_doc_export = self._is_document_export_skill(task.skill_name, skill_instance)
        if is_doc_export:
            upstream_has_n8 = any(
                bool(data.get("n8_tainted"))
                or (
                    isinstance(data.get("evidence"), dict)
                    and (
                        data["evidence"].get("n8_exposed_at_exec")
                        or data["evidence"].get("n8_tainted")
                    )
                )
                or (
                    isinstance(data.get("untrusted_envelope"), dict)
                    and (
                        data["untrusted_envelope"].get("trust_level") == "untrusted_long_term_memory"
                        or data["untrusted_envelope"].get("provenance") == "n8_memory"
                        or bool(data["untrusted_envelope"].get("historical_references"))
                    )
                )
                for data in upstream_outputs.values()
            )
            plan_has_n8_refs = bool(
                isinstance(plan.goal.context.extra, dict)
                and plan.goal.context.extra.get("n8_memory_refs")
            )
            task_is_n8_tainted = self._is_task_n8_tainted(task)

            # Remediación F-03.B: revalidación obligatoria de frescura N8 antes de cualquier
            # persistencia documental en disco si el plan o sus entradas tuvieron exposición N8.
            upstream_refs_used: list[Any] = []
            for data in upstream_outputs.values():
                for ref_u in (data.get("n8_refs_used") or ()):
                    upstream_refs_used.append(ref_u)

            if upstream_has_n8 or upstream_refs_used or plan_has_n8_refs or task_is_n8_tainted or getattr(plan, "n8_context_exposed", False):
                if self.memory_bridge is not None:
                    _env_now, reval_report = self.memory_bridge.revalidate_envelope(
                        goal=plan.goal,
                        plan=plan,
                        allow_history_in_private=self._allow_history_active(),
                    )
                    if reval_report.blocked_by_privacy:
                        raise GoalPolicyViolationError(
                            "Exportación documental rechazada: la política de privacidad bloquea el uso de referencias N8."
                        )
                    if reval_report.excluded_ids:
                        raise GoalPolicyViolationError(
                            "Exportación documental rechazada: una o más referencias N8 asociadas fueron "
                            "revocadas, expiradas, purgadas o modificadas antes de escribir en disco."
                        )
                    if upstream_refs_used:
                        up_reval = self.memory_bridge.revalidate_references(
                            upstream_refs_used,
                            allow_history_in_private=self._allow_history_active(),
                        )
                        if up_reval.blocked_by_privacy or up_reval.excluded_ids:
                            raise GoalPolicyViolationError(
                                "Exportación documental rechazada: una o más referencias N8 consumidas por "
                                "tareas previas fueron revocadas, expiradas, purgadas o modificadas antes de exportar."
                            )
                    if (plan_has_n8_refs or upstream_has_n8 or upstream_refs_used) and self.memory_bridge.memory_engine is None:
                        raise GoalPolicyViolationError(
                            "Exportación documental rechazada: no es posible validar la frescura de las referencias N8."
                        )
                elif upstream_has_n8 or upstream_refs_used or plan_has_n8_refs or task_is_n8_tainted:
                    raise GoalPolicyViolationError(
                        "Exportación documental rechazada: no existe puente N8 disponible para verificar frescura."
                    )

            # Si la propia tarea de exportación fue propuesta bajo exposición N8, verificar
            # que el contenido no derive de texto no anclado del LLM/N8.
            if task_is_n8_tainted:
                anchors = collect_grounded_anchors(plan.goal)
                grounded_p, ephemeral_p = separate_grounded_skill_params(params, anchors=anchors)
                if "content" in ephemeral_p:
                    raise GoalPolicyViolationError(
                        "Exportación documental rechazada: el contenido propuesto deriva de contexto N8 no anclado."
                    )
                params = grounded_p

            # Filtrado estructural de `upstream_outputs`: jamás concatenar salidas con procedencia
            # N8 (aunque el recuerdo siga activo), ni resultados que mezclen texto confiable y N8.
            if upstream_outputs:
                clean_upstream_lines: list[str] = []
                excluded_n8_upstream = 0
                for dep_id, data in upstream_outputs.items():
                    ev_d = data.get("evidence") if isinstance(data.get("evidence"), dict) else {}
                    env_d = data.get("untrusted_envelope") if isinstance(data.get("untrusted_envelope"), dict) else {}
                    dep_is_n8 = bool(
                        data.get("n8_tainted")
                        or ev_d.get("n8_exposed_at_exec")
                        or ev_d.get("n8_tainted")
                        or env_d.get("trust_level") == "untrusted_long_term_memory"
                        or env_d.get("provenance") == "n8_memory"
                        or bool(env_d.get("historical_references"))
                    )
                    if dep_is_n8:
                        excluded_n8_upstream += 1
                        continue
                    clean_upstream_lines.append(f"- **{dep_id}**: {data.get('summary', '')}")

                current_content = str(params.get("content") or "").strip()
                if excluded_n8_upstream > 0 and not clean_upstream_lines and not current_content:
                    raise GoalPolicyViolationError(
                        "Exportación documental rechazada: todas las evidencias previas tienen procedencia N8 "
                        "no exportable a disco."
                    )
                if clean_upstream_lines:
                    enriched = (
                        f"{current_content}\n\n## Evidencia Recopilada\n" + "\n".join(clean_upstream_lines)
                    ).strip()
                    params["content"] = redact_secrets(enriched)

        custom_envelope_override = (
            type(self.skill_manager).execute_skill_with_envelope
            is not SkillManager.execute_skill_with_envelope
            or "execute_skill_with_envelope" in getattr(self.skill_manager, "__dict__", {})
        )
        custom_exec_override = (
            type(self.skill_manager).execute_skill is not SkillManager.execute_skill
            or "execute_skill" in getattr(self.skill_manager, "__dict__", {})
        )

        envelope: Optional[dict[str, Any]] = None
        if custom_envelope_override:
            raw_text, envelope = self.skill_manager.execute_skill_with_envelope(
                task.skill_name, params=params
            )
        elif custom_exec_override:
            raw_text = self.skill_manager.execute_skill(task.skill_name, params=params)
        else:
            skill_instance = self.skill_manager.skills[task.skill_name]
            if hasattr(skill_instance, "execute_with_envelope"):
                raw_text, envelope, _report = skill_instance.execute_with_envelope(params)
            else:
                raw_text = skill_instance.execute(**params)

        text_output = redact_secrets(str(raw_text or "").strip())
        if not text_output:
            raise RuntimeError(f"La skill '{task.skill_name}' devolvió una respuesta vacía.")

        # Detecta mensajes de error internos capturados por skills defensivas.
        lowered = text_output.lower()
        if "ocurrió un error interno" in lowered or lowered.startswith("error:"):
            raise RuntimeError(text_output)

        evidence: dict[str, Any] = {
            "kind": "skill_execution",
            "task_id": task.task_id,
            "plan_id": plan.plan_id,
            "goal_id": plan.goal.goal_id,
            "skill_name": task.skill_name,
            "verifier_component": "ExecutionSupervisorSkillVerifier",
            "verified_at": utc_now_iso(),
            "output_length": len(text_output),
        }
        if envelope is not None:
            evidence["trust_level"] = envelope.get("trust_level", "untrusted_external_content")
            evidence["provenance_count"] = len(envelope.get("sources") or [])

        return {
            "output_summary": text_output,
            "evidence": evidence,
            "untrusted_envelope": envelope,
        }

    def _execute_via_chain_orchestrator(
        self,
        *,
        plan: GoalPlan,
        task: GoalTask,
        input_fn: Callable[[str], str],
    ) -> dict[str, Any]:
        """Obliga a que toda modificación de archivos pase por `ChainOrchestrator` + `PolicyEngine`."""
        if self._is_private_active():
            raise GoalPolicyViolationError(
                "Operación persistente vía ChainOrchestrator bloqueada durante el modo privado para impedir escrituras en disco."
            )
        if self.chain_orchestrator is None:
            raise GoalPolicyViolationError(
                "No es posible modificar archivos sin una instancia activa de ChainOrchestrator y PolicyEngine."
            )

        if not task.approval_granted:
            raise GoalAuthorizationError(
                f"La subtarea '{task.task_id}' requiere aprobación explícita antes de iniciar ChainOrchestrator."
            )

        meta = dict(task.metadata or {})
        params = dict(task.skill_params or {})
        target_app = str(meta.get("target_app") or params.get("app_name") or plan.goal.context.target_app or "Sistema")
        file_path = meta.get("file_path") or params.get("file_path")

        # Delegación al flujo auditado de ChainOrchestrator: preparar -> simular -> autorizar -> ejecutar.
        prep = self.chain_orchestrator.prepare_interactive_optimization(
            target_app,
            file_path=file_path,
            user_goal=plan.goal.raw_input,
        )
        change_plan = prep["plan"]
        simulation = prep.get("simulation") or self.chain_orchestrator.simulate_plan(change_plan)
        approval_hash = simulation["approval_hash"]

        # Validación explícita adicional mediante PolicyEngine (nunca bypass).
        self.chain_orchestrator.policy_engine.validate_plan(change_plan)

        # Autorización interactiva real en ChainOrchestrator con token efímero vinculado a hash.
        auth_token = self.chain_orchestrator.authorize_plan_interactively(
            change_plan,
            approval_hash=approval_hash,
            input_fn=input_fn,
        )
        exec_summary = self.chain_orchestrator.execute_approved_plan(
            change_plan,
            approval_hash=approval_hash,
            authorization_token=auth_token,
        )

        task.chain_execution_id = change_plan.execution_id
        task.approval_token = auth_token
        task.approval_hash = approval_hash

        return {
            "output_summary": (
                f"ChainOrchestrator ejecutó ChangePlan '{change_plan.execution_id}' "
                f"(estado: {exec_summary.get('state', 'COMPLETED')})."
            ),
            "evidence": {
                "kind": "chain_orchestrator",
                "task_id": task.task_id,
                "plan_id": plan.plan_id,
                "goal_id": plan.goal.goal_id,
                "execution_id": change_plan.execution_id,
                "approval_hash": approval_hash,
                "verifier_component": "ExecutionSupervisorChainVerifier",
                "verified_at": utc_now_iso(),
                "policy_validated": True,
                "summary": exec_summary,
            },
            "untrusted_envelope": None,
        }

    def _attempt_replan(
        self,
        plan: GoalPlan,
        failed_task: GoalTask,
        *,
        reason: str,
    ) -> bool:
        """Crea tareas de reemplazo acotadas manteniendo intacto el historial y las completadas."""
        if plan.goal.status == GoalStatus.RUNNING:
            plan.goal.transition_to(GoalStatus.REPLANNING)

        # Consulta consultiva N8 previa a replanificar (solo RAM, respetando --sin-historial y fallos tolerantes).
        replan_envelope = None
        if self.memory_bridge is not None and self._allow_history_active():
            replan_envelope = self.memory_bridge.retrieve_for_goal(
                plan.goal,
                stage="replanning",
                plan=plan,
                failed_task=failed_task,
                brain_manager=self.brain_manager,
                allow_history_in_private=self._allow_history_active(),
                force_refresh=True,
            )
            if replan_envelope is not None:
                replan_envelope, _rep = self.memory_bridge.revalidate_envelope(
                    goal=plan.goal,
                    plan=plan,
                    allow_history_in_private=self._allow_history_active(),
                )
            if replan_envelope is not None:
                plan.n8_context_exposed = True

        if self.replan_factory is not None:
            factory_untrusted = (
                replan_envelope.to_untrusted_prompt_context()
                if replan_envelope is not None
                else None
            )
            try:
                sig = inspect.signature(self.replan_factory)
                accepts_n8 = "untrusted_n8_context" in sig.parameters or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                )
            except (TypeError, ValueError):
                accepts_n8 = False
            candidate_tasks = (
                self.replan_factory(plan, failed_task, untrusted_n8_context=factory_untrusted)
                if accepts_n8
                else self.replan_factory(plan, failed_task)
            )
            replan_saw_n8 = replan_envelope is not None
        else:
            candidate_tasks = self.evaluator.build_fallback_tasks(
                plan, failed_task, selector=self.skill_selector
            )
            replan_saw_n8 = False

        if not candidate_tasks:
            plan.goal.transition_to(GoalStatus.RUNNING)
            return False

        # Verifica restricciones, límites y DAG completo antes de aceptar la replanificación.
        if len(plan.tasks) + len(candidate_tasks) > plan.limits.max_tasks:
            plan.goal.transition_to(GoalStatus.RUNNING)
            return False

        anchors = collect_grounded_anchors(plan.goal) if replan_saw_n8 else None
        existing_task_ids = {t.task_id for t in plan.tasks}
        replan_gen = plan.replan_count + 1
        canonical_replan_pairs: Optional[list[tuple[str, list[str]]]] = None
        if replan_saw_n8:
            # Remediación F-01: en replanificación expuesta a N8 nunca aceptamos `task_id` libre
            # del modelo/factory; generamos IDs canónicos `t_r{gen}_{idx}` y reescribimos `depends_on`.
            try:
                raw_replan_specs = [(t.task_id, list(t.depends_on)) for t in candidate_tasks]
                _replan_map, canonical_replan_pairs = canonicalize_n8_task_id_map(
                    raw_replan_specs,
                    stage="replanning",
                    generation=replan_gen,
                    existing_task_ids=existing_task_ids,
                )
            except ValueError:
                plan.goal.transition_to(GoalStatus.RUNNING)
                return False

        isolated_candidates: list[GoalTask] = []
        for idx, new_task in enumerate(candidate_tasks):
            self.skill_selector.validate_task(new_task, plan.goal.constraints)
            if replan_saw_n8 and canonical_replan_pairs is not None:
                canonical_tid, mapped_deps = canonical_replan_pairs[idx]
                skill_obj = self.skill_selector.skill_manager.skills.get(new_task.skill_name) if new_task.skill_name else None
                skill_schema = getattr(skill_obj, "parameters", None) if skill_obj else None
                grounded_params, eph_params = separate_grounded_skill_params(
                    new_task.skill_params,
                    anchors=anchors or {},
                    skill_schema=skill_schema if isinstance(skill_schema, dict) else None,
                )
                if new_task.risk_category == TaskRiskCategory.SENSITIVE_OPERATION and eph_params:
                    plan.goal.transition_to(GoalStatus.RUNNING)
                    return False
                if new_task.skill_name and isinstance(skill_schema, dict):
                    for req_field in (skill_schema.get("required") or []):
                        if req_field not in grounded_params or grounded_params[req_field] in (None, ""):
                            if req_field == "query" and plan.goal.raw_input.strip():
                                grounded_params["query"] = plan.goal.raw_input.strip()[:120]
                            else:
                                plan.goal.transition_to(GoalStatus.RUNNING)
                                return False
                skel_title, skel_desc, skel_exp = build_skeleton_task_labels(
                    task_id=canonical_tid,
                    skill_name=new_task.skill_name,
                    stage="replanning",
                )
                # Conservar exclusivamente metadatos estructurales permitidos (nunca texto libre ni ID original).
                safe_replan_meta: dict[str, Any] = {
                    "replan_for": failed_task.task_id,
                    "provenance_origin": "llm_with_n8",
                    "n8_influenced": True,
                    "ephemeral_details_in_ram": True,
                }
                orig_meta = new_task.metadata if isinstance(new_task.metadata, dict) else {}
                for flag_key in ("use_chain_orchestrator", "modifies_files", "proposed_by_ai"):
                    if flag_key in orig_meta:
                        safe_replan_meta[flag_key] = bool(orig_meta[flag_key])
                if isinstance(orig_meta.get("target_app"), str) and orig_meta["target_app"].lower() in (
                    (anchors or {}).get("grounded_scalars", set())
                ):
                    safe_replan_meta["target_app"] = orig_meta["target_app"]
                if isinstance(orig_meta.get("file_path"), str) and orig_meta["file_path"] in (
                    (anchors or {}).get("target_paths") or []
                ):
                    safe_replan_meta["file_path"] = orig_meta["file_path"]

                clean_task = GoalTask(
                    task_id=canonical_tid,
                    title=skel_title,
                    description=skel_desc,
                    skill_name=new_task.skill_name,
                    skill_params=grounded_params,
                    risk_category=new_task.risk_category,
                    requires_approval=new_task.requires_approval,
                    depends_on=mapped_deps,
                    expected_output=skel_exp,
                    max_retries=new_task.max_retries,
                    timeout_sec=new_task.timeout_sec,
                    replan_generation=new_task.replan_generation or replan_gen,
                    metadata=safe_replan_meta,
                )
                clean_task.set_ephemeral_annotation(
                    "ai_n8_replan_task",
                    {
                        "title": new_task.title,
                        "description": new_task.description,
                        "expected_output": new_task.expected_output,
                        "ephemeral_skill_params": eph_params,
                    },
                )
                isolated_candidates.append(clean_task)
            else:
                isolated_candidates.append(new_task)
        candidate_tasks = isolated_candidates

        trial_tasks = list(plan.tasks) + list(candidate_tasks)
        validate_plan_dag(trial_tasks, plan.limits)

        # Preserva tareas completadas; actualiza tareas BLOCKED que dependían de la fallida
        # solo si hay un único reemplazo equivalente para reencaminarlas limpiamente.
        cancelled_blocked_ids: list[str] = []
        if len(candidate_tasks) == 1:
            replacement_id = candidate_tasks[0].task_id
            for existing in plan.tasks:
                if (
                    existing.status == TaskStatus.BLOCKED
                    and failed_task.task_id in existing.depends_on
                ):
                    # Como cambiar `depends_on` alteraría el plan original si estuviese aprobado,
                    # se deja bloqueada la vieja si existía `approval_bound_hash`; si no estaba
                    # vinculada aprobación, puede reencaminarse el grafo pendiente.
                    if plan.approval_bound_hash is None:
                        existing.depends_on = [
                            replacement_id if d == failed_task.task_id else d
                            for d in existing.depends_on
                        ]
                        existing.transition_to(TaskStatus.READY if not existing.requires_approval else TaskStatus.REQUIRES_APPROVAL)
                        cancelled_blocked_ids.append(existing.task_id)

        plan.tasks.extend(candidate_tasks)
        plan.replan_count += 1
        plan.version += 1
        # Invalida aprobación previa porque la estructura del plan cambió legítimamente.
        plan.approval_bound_hash = None
        plan.refresh_hash()

        preserved_completed = [
            t.task_id for t in plan.tasks if t.status == TaskStatus.COMPLETED
        ]
        decision = ReplanDecision(
            trigger_task_id=failed_task.task_id,
            reason=redact_secrets(reason),
            strategy="n8_advisory_replan_skeleton" if replan_saw_n8 else "fallback_task_insertion",
            preserved_task_ids=preserved_completed,
            added_task_ids=[t.task_id for t in candidate_tasks],
            cancelled_task_ids=cancelled_blocked_ids,
        )
        plan.replan_history.append(decision)
        plan.record_event(
            "plan_replanned",
            f"Replanificación #{plan.replan_count} tras fallo en '{failed_task.task_id}'.",
            task_id=failed_task.task_id,
            to_status=GoalStatus.RUNNING.value,
            details={**decision.to_dict(), **({"n8_influenced": True} if replan_saw_n8 else {})},
        )
        plan.goal.transition_to(GoalStatus.RUNNING)
        self._persist(
            plan,
            "plan_replanned",
            {**decision.to_dict(), **({"n8_influenced": True} if replan_saw_n8 else {})},
        )
        return True

    def _refresh_dependency_readiness(self, plan: GoalPlan) -> None:
        """Promueve de `PENDING` a `READY` o `REQUIRES_APPROVAL` cuando sus dependencias concluyeron."""
        for task in plan.tasks:
            if task.status != TaskStatus.PENDING:
                continue
            dep_statuses = [
                (plan.get_task(dep_id).status if plan.get_task(dep_id) else None)
                for dep_id in task.depends_on
            ]
            if any(st in {TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELLED} for st in dep_statuses):
                task.transition_to(TaskStatus.BLOCKED)
            elif all(st == TaskStatus.COMPLETED for st in dep_statuses):
                task.transition_to(
                    TaskStatus.REQUIRES_APPROVAL if task.requires_approval else TaskStatus.READY
                )

    def _dependencies_completed(self, plan: GoalPlan, task: GoalTask) -> bool:
        for dep_id in task.depends_on:
            dep = plan.get_task(dep_id)
            if dep is None or dep.status != TaskStatus.COMPLETED:
                return False
        return True

    def _select_next_runnable_or_blocked_task(self, plan: GoalPlan) -> Optional[GoalTask]:
        """Elige la siguiente subtarea en orden topológico lista y autorizada para correr."""
        for task in plan.tasks:
            if task.status in {TaskStatus.READY, TaskStatus.APPROVED}:
                if self._dependencies_completed(plan, task):
                    if task.requires_approval and not task.approval_granted:
                        continue
                    return task
        return None

    def _block_dependents(self, plan: GoalPlan, failed_task_id: str) -> None:
        """Bloquea en cascada las tareas que dependen directa o transitivamente de una fallida."""
        blocked_set = {failed_task_id}
        changed = True
        while changed:
            changed = False
            for task in plan.tasks:
                if task.status in TERMINAL_TASK_STATUSES or task.status == TaskStatus.BLOCKED:
                    continue
                if any(dep in blocked_set for dep in task.depends_on):
                    task.transition_to(TaskStatus.BLOCKED)
                    blocked_set.add(task.task_id)
                    changed = True

    def _fail_remaining_due_to_timeout(self, plan: GoalPlan, timeout_msg: str) -> None:
        for task in plan.tasks:
            if task.status not in TERMINAL_TASK_STATUSES:
                if task.status == TaskStatus.BLOCKED:
                    task.mark_failed(timeout_msg)
                elif task.status in {TaskStatus.PENDING, TaskStatus.READY, TaskStatus.REQUIRES_APPROVAL, TaskStatus.APPROVED, TaskStatus.RUNNING}:
                    task.mark_failed(timeout_msg)
        plan.goal.error_summary = timeout_msg
        plan.goal.transition_to(GoalStatus.FAILED)
        plan.record_event("goal_timeout", timeout_msg, to_status=GoalStatus.FAILED.value)
        self._persist(plan, "goal_timeout", {"error": timeout_msg})

    def _has_tainted_n8_lineage(self, plan: GoalPlan, task: GoalTask, visited: Optional[set[str]] = None) -> bool:
        """Comprueba recursivamente si `task` o alguno de sus ancestros DAG estuvo expuesto a N8."""
        seen = visited if visited is not None else set()
        if task.task_id in seen:
            return False
        seen.add(task.task_id)
        if self._is_task_n8_tainted(task):
            return True
        for dep_id in task.depends_on:
            dep_task = plan.get_task(dep_id)
            if dep_task is not None and self._has_tainted_n8_lineage(plan, dep_task, seen):
                return True
        return False

    def _collect_n8_refs_in_lineage(self, plan: GoalPlan, task: GoalTask, visited: Optional[set[str]] = None) -> list[Any]:
        """Reúne en RAM todas las referencias N8 consumidas por `task` o sus ancestros DAG."""
        seen = visited if visited is not None else set()
        if task.task_id in seen:
            return []
        seen.add(task.task_id)
        collected: list[Any] = []
        ann = task.get_ephemeral_annotations()
        refs_here = ann.get("n8_refs_used_at_exec") or ()
        for ref_item in refs_here:
            collected.append(ref_item)
        for dep_id in task.depends_on:
            dep_task = plan.get_task(dep_id)
            if dep_task is not None:
                collected.extend(self._collect_n8_refs_in_lineage(plan, dep_task, seen))
        return collected

    def _collect_upstream_outputs(
        self,
        plan: GoalPlan,
        task: GoalTask,
    ) -> dict[str, dict[str, Any]]:
        outputs: dict[str, dict[str, Any]] = {}
        for dep_id in task.depends_on:
            dep_task = plan.get_task(dep_id)
            if dep_task is not None and dep_task.status == TaskStatus.COMPLETED:
                outputs[dep_id] = {
                    "summary": dep_task.output_summary or "",
                    "evidence": dep_task.execution_evidence or {},
                    "untrusted_envelope": dep_task.untrusted_envelope,
                    "n8_tainted": self._has_tainted_n8_lineage(plan, dep_task),
                    "n8_refs_used": tuple(self._collect_n8_refs_in_lineage(plan, dep_task)),
                }
        return outputs

    def _persist(self, plan: GoalPlan, event_type: str, payload: dict[str, Any]) -> None:
        target_store = self.active_goal_store
        if target_store is not None:
            target_store.append_wal_event(
                plan.plan_id,
                event_type,
                payload,
                n8_context_exposed=bool(getattr(plan, "n8_context_exposed", False)),
            )
            target_store.save_plan(plan, set_active=True)

    def _build_result(
        self,
        plan: GoalPlan,
        *,
        stopped_for_approval: bool = False,
        cancelled: bool = False,
        errors: Optional[list[str]] = None,
    ) -> ExecutionResult:
        completed = [t.task_id for t in plan.tasks if t.status == TaskStatus.COMPLETED]
        failed = [t.task_id for t in plan.tasks if t.status == TaskStatus.FAILED]
        pending_approval = [
            t.task_id
            for t in plan.tasks
            if t.status in {TaskStatus.REQUIRES_APPROVAL}
            or (t.requires_approval and not t.approval_granted and t.status not in TERMINAL_TASK_STATUSES)
        ]

        if cancelled:
            summary = f"Objetivo '{plan.goal.goal_id}' cancelado. Completadas: {len(completed)}/{len(plan.tasks)}."
        elif stopped_for_approval:
            summary = (
                f"Plan pausado esperando aprobación explícita para: {', '.join(pending_approval)}."
            )
        else:
            last_completed = next(
                (t.output_summary for t in reversed(plan.tasks) if t.status == TaskStatus.COMPLETED and t.output_summary),
                "",
            )
            summary = (
                f"Estado final: {plan.goal.status.value} "
                f"({len(completed)}/{len(plan.tasks)} subtareas completadas). "
                + (last_completed or "")
            ).strip()

        return ExecutionResult(
            plan=plan,
            final_status=plan.goal.status,
            completed_task_ids=completed,
            failed_task_ids=failed,
            pending_approval_task_ids=pending_approval,
            stopped_for_approval=stopped_for_approval,
            cancelled=cancelled,
            replan_count=plan.replan_count,
            summary_text=redact_secrets(summary),
            errors=list(errors or []),
        )
