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
import threading
import time
from typing import Any, Callable, Optional, TYPE_CHECKING

from src.brain.exceptions import redact_secrets
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
from src.brain.goal_persistence import GoalStore
from src.brain.goal_planner import validate_plan_dag
from src.brain.goal_skill_selector import GoalSkillSelector
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
        goal_store: Optional[GoalStore] = None,
        evaluator: Optional[PlanEvaluator] = None,
        replan_factory: Optional[
            Callable[[GoalPlan, GoalTask], list[GoalTask]]
        ] = None,
    ) -> None:
        self.skill_manager: SkillManager = skill_manager or SkillManager()
        self.skill_selector: GoalSkillSelector = skill_selector or GoalSkillSelector(
            self.skill_manager
        )
        self.chain_orchestrator: Optional["ChainOrchestrator"] = chain_orchestrator
        self.goal_store: Optional[GoalStore] = goal_store
        self.evaluator: PlanEvaluator = evaluator or PlanEvaluator()
        self.replan_factory = replan_factory

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

        return self._build_result(
            plan,
            stopped_for_approval=stopped_for_approval,
            errors=errors,
        )

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
        snippets: list[str] = []
        untrusted_count = 0
        for dep_id, dep_data in upstream_outputs.items():
            if dep_data.get("untrusted_envelope"):
                untrusted_count += 1
                snippets.append(
                    f"[{dep_id} | dato externo no confiable] {dep_data.get('summary', '')}"
                )
            else:
                snippets.append(f"[{dep_id}] {dep_data.get('summary', '')}")

        hw_ctx = plan.goal.context.hardware
        hw_note = f" Hardware en contexto: {hw_ctx}." if hw_ctx else ""
        base_summary = (
            f"Síntesis '{task.title}': "
            + (" | ".join(snippets) if snippets else plan.goal.interpreted_summary)
            + hw_note
        )
        return {
            "output_summary": redact_secrets(base_summary),
            "evidence": {
                "kind": "internal_synthesis",
                "task_id": task.task_id,
                "upstream_task_ids": list(upstream_outputs.keys()),
                "isolated_untrusted_sources": untrusted_count,
                "verified_at": utc_now_iso(),
            },
            "untrusted_envelope": None,
        }

    def _execute_registered_skill(
        self,
        plan: GoalPlan,
        task: GoalTask,
        upstream_outputs: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Invoca una skill real del `SkillManager` preservando fronteras de confianza."""
        assert task.skill_name is not None
        params = dict(task.skill_params)

        # Si es `document_export`, enriquecer contenido usando evidencias upstream limpias.
        if task.skill_name == "document_export" and upstream_outputs:
            upstream_lines = [
                f"- **{dep_id}**: {data.get('summary', '')}"
                for dep_id, data in upstream_outputs.items()
            ]
            current_content = str(params.get("content") or "").strip()
            enriched = (
                f"{current_content}\n\n## Evidencia Recopilada\n" + "\n".join(upstream_lines)
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
            "skill_name": task.skill_name,
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
                "execution_id": change_plan.execution_id,
                "approval_hash": approval_hash,
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

        candidate_tasks = (
            self.replan_factory(plan, failed_task)
            if self.replan_factory is not None
            else self.evaluator.build_fallback_tasks(plan, failed_task, selector=self.skill_selector)
        )
        if not candidate_tasks:
            plan.goal.transition_to(GoalStatus.RUNNING)
            return False

        # Verifica restricciones, límites y DAG completo antes de aceptar la replanificación.
        if len(plan.tasks) + len(candidate_tasks) > plan.limits.max_tasks:
            plan.goal.transition_to(GoalStatus.RUNNING)
            return False

        for new_task in candidate_tasks:
            self.skill_selector.validate_task(new_task, plan.goal.constraints)

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
            strategy="fallback_task_insertion",
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
            details=decision.to_dict(),
        )
        plan.goal.transition_to(GoalStatus.RUNNING)
        self._persist(plan, "plan_replanned", decision.to_dict())
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
                }
        return outputs

    def _persist(self, plan: GoalPlan, event_type: str, payload: dict[str, Any]) -> None:
        if self.goal_store is not None:
            self.goal_store.append_wal_event(plan.plan_id, event_type, payload)
            self.goal_store.save_plan(plan, set_active=True)

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
