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
from typing import Any, Callable, Optional, Sequence, TYPE_CHECKING

from src.brain.goal_memory import GoalMemoryBridge, RuntimeN8Envelope
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
from src.brain.goal_persistence import EphemeralGoalStore, GoalStore
from src.brain.goal_planner import GoalPlanningEngine, TaskDecomposer
from src.brain.goal_skill_selector import GoalSkillSelector
from src.brain.goal_supervisor import ExecutionResult, ExecutionSupervisor, PlanEvaluator
from src.brain.goal_understanding import GoalUnderstandingEngine
from src.context.policy import sanitize_untrusted_label
from src.tools.skill_manager import SkillManager

if TYPE_CHECKING:
    from src.brain.brain_manager import BrainManager
    from src.brain.chain_orchestrator import ChainOrchestrator
    from src.context.engine import ContextAwarenessEngine
    from src.context.models import ContextSnapshot


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
        context_engine: Optional["ContextAwarenessEngine"] = None,
        goal_store: Optional[GoalStore | EphemeralGoalStore] = None,
        ephemeral_goal_store: Optional[EphemeralGoalStore] = None,
        limits: Optional[GoalEngineLimits] = None,
        memory_capture_service: Optional[Any] = None,
        memory_engine: Optional[Any] = None,
        is_private_mode_fn: Optional[Callable[[], bool]] = None,
        allow_history_in_private_fn: Optional[Callable[[], bool]] = None,
        use_ai: bool = False,
    ) -> None:
        self.skill_manager: SkillManager = skill_manager or SkillManager()
        self.brain_manager: Optional["BrainManager"] = brain_manager
        self.chain_orchestrator: Optional["ChainOrchestrator"] = chain_orchestrator
        self.context_engine: Optional["ContextAwarenessEngine"] = context_engine
        self.memory_capture_service: Optional[Any] = memory_capture_service
        resolved_mem_engine = (
            memory_engine
            if memory_engine is not None
            else getattr(brain_manager, "memory_engine", None)
        )
        self.memory_engine: Optional[Any] = resolved_mem_engine
        self._is_private_mode_fn: Optional[Callable[[], bool]] = is_private_mode_fn
        self._allow_history_in_private_fn: Optional[Callable[[], bool]] = allow_history_in_private_fn
        self._manual_private_mode: bool = False
        self._manual_allow_history: bool = True
        self._was_private_mode: bool = False
        self._last_allow_history: bool = True

        self.limits: GoalEngineLimits = limits or GoalEngineLimits()

        self.skill_selector: GoalSkillSelector = GoalSkillSelector(self.skill_manager)
        self.goal_store: GoalStore | EphemeralGoalStore = goal_store or GoalStore()
        self.ephemeral_goal_store: EphemeralGoalStore = ephemeral_goal_store or EphemeralGoalStore()
        self.memory_bridge: GoalMemoryBridge = GoalMemoryBridge(memory_engine=self.memory_engine)
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
            ephemeral_goal_store=self.ephemeral_goal_store,
            evaluator=self.evaluator,
            memory_capture_service=memory_capture_service,
            memory_bridge=self.memory_bridge,
            brain_manager=brain_manager,
            is_private_mode_fn=lambda: self.is_private_mode,
            allow_history_in_private_fn=lambda: self.allow_history_in_private,
        )

        self._active_plan: Optional[GoalPlan] = None
        self._pending_clarification_goal: Optional[Goal] = None
        self._reconcile_private_state()

    @property
    def is_private_mode(self) -> bool:
        """Detecta si el modo privado está activo vía bandera propia, callbacks, MemoryEngine o BrainManager."""
        if self._manual_private_mode:
            return True
        if self._is_private_mode_fn is not None and self._is_private_mode_fn() is True:
            return True
        if self.memory_engine is not None and getattr(self.memory_engine, "is_private_mode", False) is True:
            return True
        if self.brain_manager is not None:
            conv_mem = getattr(self.brain_manager, "memory", None)
            if conv_mem is not None and getattr(conv_mem, "is_private_mode", False) is True:
                return True
            bm_eng = getattr(self.brain_manager, "memory_engine", None)
            if bm_eng is not None and getattr(bm_eng, "is_private_mode", False) is True:
                return True
        return False

    @property
    def allow_history_in_private(self) -> bool:
        """Indica si las lecturas históricas N8 están habilitadas bajo el modo actual (`--sin-historial`)."""
        if self._manual_private_mode and not self._manual_allow_history:
            return False
        if self._allow_history_in_private_fn is not None and self._allow_history_in_private_fn() is False:
            return False
        if self.memory_engine is not None and getattr(self.memory_engine, "allow_history_reads", True) is False:
            return False
        if self.brain_manager is not None:
            conv_mem = getattr(self.brain_manager, "memory", None)
            if conv_mem is not None and getattr(conv_mem, "private_allow_history", True) is False:
                return False
            bm_eng = getattr(self.brain_manager, "memory_engine", None)
            if bm_eng is not None and getattr(bm_eng, "allow_history_reads", True) is False:
                return False
        return True

    @property
    def active_goal_store(self) -> GoalStore | EphemeralGoalStore:
        """Devuelve `EphemeralGoalStore` siempre que el modo privado esté activo."""
        if self.is_private_mode:
            return self.ephemeral_goal_store
        return self.goal_store

    @property
    def _runtime_n8_envelopes(self) -> dict[str, RuntimeN8Envelope]:
        return self.memory_bridge._runtime_n8_envelopes

    def set_memory_engine(self, memory_engine: Optional[Any]) -> None:
        """Conecta o reemplaza la instancia compartida de `MemoryEngine` sin duplicar SQLite."""
        self.memory_engine = memory_engine
        self.memory_bridge.set_memory_engine(memory_engine)
        self._reconcile_private_state()

    def set_private_mode(self, *, enabled: bool, allow_history: bool = True) -> None:
        """Control programático explícito de modo privado en N6, independiente de la consola."""
        self._manual_private_mode = bool(enabled)
        self._manual_allow_history = True if not enabled else bool(allow_history)
        self._reconcile_private_state()

    def on_private_mode_exited(self) -> None:
        """Descarta todo estado privado en RAM (EphemeralGoalStore, sobres N8 y borradores) al salir."""
        self.ephemeral_goal_store.clear()
        self.memory_bridge.clear_all()
        if self._active_plan is not None and getattr(self._active_plan, "is_ephemeral_private", False):
            self._active_plan = None
        if self._pending_clarification_goal is not None:
            self._pending_clarification_goal = None

    def _reconcile_private_state(self) -> None:
        priv = self.is_private_mode
        allow_hist = self.allow_history_in_private
        if self._was_private_mode and not priv:
            self.on_private_mode_exited()
        elif priv and not self._was_private_mode:
            # Al entrar en modo privado aislamos en RAM sin heredar planes persistentes previos.
            if self._active_plan is not None and not getattr(self._active_plan, "is_ephemeral_private", False):
                self._active_plan = None
            self._pending_clarification_goal = None
            if not allow_hist:
                self.memory_bridge.clear_all()
        elif not allow_hist and self._last_allow_history:
            self.memory_bridge.clear_all()
        self._was_private_mode = priv
        self._last_allow_history = allow_hist

    def set_memory_capture_service(self, memory_capture_service: Optional[Any]) -> None:
        """Conecta desacopladamente el coordinador de captura N8 al supervisor N6."""
        self.memory_capture_service = memory_capture_service
        self.supervisor.set_memory_capture_service(memory_capture_service)

    def set_chain_orchestrator(self, chain_orchestrator: "ChainOrchestrator") -> None:
        """Conecta tardíamente `ChainOrchestrator` cuando el usuario lo inicializa."""
        self.chain_orchestrator = chain_orchestrator
        self.supervisor.chain_orchestrator = chain_orchestrator

    def set_context_engine(self, context_engine: Optional["ContextAwarenessEngine"]) -> None:
        """Conecta o reemplaza el motor de observación contextual N7."""
        self.context_engine = context_engine

    def request_context(
        self,
        categories: Optional[Sequence[str]] = None,
        *,
        target_paths: Optional[Sequence[str]] = None,
        force_refresh: bool = False,
    ) -> Optional["ContextSnapshot"]:
        """Solicita bajo demanda un snapshot al motor N7 si se encuentra disponible y habilitado."""
        if self.context_engine is None or not getattr(self.context_engine, "enabled", False):
            return None
        return self.context_engine.get_context_safe(
            categories=categories,
            target_paths=target_paths,
            force_refresh=force_refresh,
        )

    def _enrich_goal_with_n7_context(self, goal: Goal) -> None:
        """Incorpora contexto real de N7 al `Goal` cuando resulta pertinente para el objetivo.

        Garantías:
        - Si N7 no está conectado o está deshabilitado, conserva el comportamiento previo de N6.
        - Evita consultas innecesarias cuando el objetivo no requiere datos del sistema local.
        - Aísla los datos de N7 bajo un sobre no confiable (`untrusted_system_context`).
        """
        if self.context_engine is None or not getattr(self.context_engine, "enabled", False):
            return
        if goal.needs_clarification or goal.status == GoalStatus.AWAITING_CLARIFICATION:
            return

        try:
            relevant_cats = self.context_engine.infer_relevant_categories_for_goal(
                goal.raw_input,
                target_paths=goal.context.target_paths,
                target_app=goal.context.target_app,
            )
        except Exception:
            return

        if not relevant_cats:
            return

        snapshot = self.context_engine.get_context_safe(
            categories=relevant_cats,
            target_paths=goal.context.target_paths or None,
            target_app=goal.context.target_app or None,
            include_windows=False,
            refresh_resources=True,
        )
        payload = self.context_engine.build_n6_context_payload(
            snapshot,
            target_app=goal.context.target_app or None,
        )

        # Completa información real de hardware sin sobrescribir pistas explícitas del usuario,
        # sanitizando cualquier etiqueta textual para mantener la frontera de datos no confiables (F4.C).
        for hw_key, hw_val in (payload.get("hardware") or {}).items():
            if hw_key not in goal.context.hardware and hw_val is not None:
                goal.context.hardware[hw_key] = (
                    sanitize_untrusted_label(hw_val, max_len=120)
                    if isinstance(hw_val, str)
                    else hw_val
                )

        for env_key, env_val in (payload.get("environment") or {}).items():
            if env_key not in goal.context.environment and env_val:
                goal.context.environment[env_key] = sanitize_untrusted_label(str(env_val), max_len=96)

        goal.context.extra["n7_snapshot_id"] = payload.get("snapshot_id")
        goal.context.extra["n7_trust_boundary"] = "untrusted_system_context"
        goal.context.extra["n7_requested_categories"] = list(
            payload.get("requested_categories") or []
        )
        goal.context.extra["n7_untrusted_envelope"] = payload.get("untrusted_envelope")
        if payload.get("unavailable_fields"):
            goal.context.extra["n7_unavailable_fields"] = list(payload["unavailable_fields"])
        if payload.get("partial_errors"):
            goal.context.extra["n7_partial_errors"] = list(payload["partial_errors"])
            for err_item in payload["partial_errors"]:
                cat_name = sanitize_untrusted_label(str(err_item.get("category", "context")), max_len=32)
                err_msg = sanitize_untrusted_label(str(err_item.get("message", "No disponible")), max_len=120)
                note = f"Dato contextual N7 no disponible ({cat_name}): {err_msg}"
                if note not in goal.missing_info:
                    goal.missing_info.append(note)

        if snapshot.applications is not None and goal.context.target_app:
            target_low = goal.context.target_app.lower()
            is_running = any(
                target_low in a.name.lower() or (a.executable_name and target_low in a.executable_name.lower())
                for a in snapshot.applications.running_applications
            )
            is_installed = is_running or any(
                target_low in a.name.lower() or (a.executable_name and target_low in a.executable_name.lower())
                for a in snapshot.applications.installed_applications
            )
            goal.context.extra["n7_target_app_state"] = {
                "target_app": sanitize_untrusted_label(goal.context.target_app, max_len=64),
                "is_running": is_running,
                "is_installed": is_installed,
            }

        if snapshot.filesystem is not None and snapshot.filesystem.queried_files:
            goal.context.extra["n7_target_files"] = [
                {
                    "name": sanitize_untrusted_label(f.name, max_len=80),
                    "exists": f.exists,
                    "is_dir": f.is_dir,
                    "category": f.file_category,
                }
                for f in snapshot.filesystem.queried_files
            ]

    def _consult_n8_before_planning(
        self,
        goal: Goal,
        *,
        stage: str = "planning",
        plan: Optional[GoalPlan] = None,
        force_refresh: bool = False,
    ) -> Optional[RuntimeN8Envelope]:
        """Consulta acotada y consultiva de recuerdos N8 para un objetivo claro."""
        if goal.needs_clarification or goal.status == GoalStatus.AWAITING_CLARIFICATION:
            return None
        if self.memory_engine is None:
            return None
        if not self.allow_history_in_private:
            self.memory_bridge.clear_goal_envelopes(
                goal_id=goal.goal_id,
                plan_id=plan.plan_id if plan is not None else None,
            )
            return None
        return self.memory_bridge.retrieve_for_goal(
            goal,
            stage=stage,
            plan=plan,
            brain_manager=self.brain_manager,
            allow_history_in_private=self.allow_history_in_private,
            force_refresh=force_refresh,
        )

    def _release_previous_goal_context(self, *, keep_goal_id: Optional[str] = None) -> None:
        if self._active_plan is not None and self._active_plan.goal.goal_id != keep_goal_id:
            self.memory_bridge.clear_goal_envelopes(
                goal_id=self._active_plan.goal.goal_id,
                plan_id=self._active_plan.plan_id,
            )

    def interpret_goal(
        self,
        raw_input: str,
        *,
        context: Optional[GoalContext] = None,
        constraints: Optional[Constraints] = None,
    ) -> Goal:
        """Interpreta una petición en lenguaje natural devolviendo un `Goal` estructurado."""
        self._reconcile_private_state()
        goal = self.understanding.interpret(
            raw_input,
            initial_context=context,
            explicit_constraints=constraints,
            run_ai_enrichment=False,
        )
        self._enrich_goal_with_n7_context(goal)
        # Re-capturamos baseline incluyendo datos deterministas observados de N7 previos a AI+N8.
        goal.capture_deterministic_baseline()

        n8_env = self._consult_n8_before_planning(goal, stage="planning")
        untrusted_n8 = n8_env.to_untrusted_prompt_context() if n8_env is not None else None
        self.understanding.run_ai_enrichment(goal, untrusted_n8_context=untrusted_n8)
        return goal

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
        self._reconcile_private_state()
        self._release_previous_goal_context()
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
        valid_env, _reval = self.memory_bridge.revalidate_envelope(
            goal=goal,
            allow_history_in_private=self.allow_history_in_private,
        )
        untrusted_n8 = valid_env.to_untrusted_prompt_context() if valid_env is not None else None
        plan = self.planner.build_plan(
            goal,
            limits=limits or self.limits,
            untrusted_n8_context=untrusted_n8,
        )
        if self.is_private_mode:
            plan.is_ephemeral_private = True
        self.memory_bridge.bind_plan(goal.goal_id, plan.plan_id)
        self._active_plan = plan
        if persist:
            store = self.active_goal_store
            store.save_plan(plan, set_active=True)
            wal_payload: dict[str, Any] = {"goal_id": goal.goal_id}
            if goal.context.extra.get("n8_query_id"):
                wal_payload["n8_query_id"] = goal.context.extra["n8_query_id"]
            store.append_wal_event(plan.plan_id, "plan_created", wal_payload)
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
        self._reconcile_private_state()
        self._release_previous_goal_context()
        goal = self.interpret_goal(raw_input, context=context, constraints=constraints)
        valid_env, _reval = self.memory_bridge.revalidate_envelope(
            goal=goal,
            allow_history_in_private=self.allow_history_in_private,
        )
        untrusted_n8 = valid_env.to_untrusted_prompt_context() if valid_env is not None else None
        plan = self.planner.build_plan(
            goal,
            limits=limits or self.limits,
            custom_tasks=custom_tasks,
            untrusted_n8_context=untrusted_n8,
        )
        if self.is_private_mode:
            plan.is_ephemeral_private = True
        self.memory_bridge.bind_plan(goal.goal_id, plan.plan_id)
        self._pending_clarification_goal = None
        self._active_plan = plan
        if persist:
            store = self.active_goal_store
            store.save_plan(plan, set_active=True)
            wal_payload: dict[str, Any] = {"goal_id": goal.goal_id}
            if goal.context.extra.get("n8_query_id"):
                wal_payload["n8_query_id"] = goal.context.extra["n8_query_id"]
            store.append_wal_event(plan.plan_id, "plan_created", wal_payload)
        return plan

    def clarify_pending_goal(
        self,
        clarification_text: str,
        *,
        limits: Optional[GoalEngineLimits] = None,
        persist: bool = True,
    ) -> GoalSubmissionResult:
        """Aplica la respuesta aclaratoria del usuario al objetivo pendiente de definición."""
        self._reconcile_private_state()
        if self._pending_clarification_goal is None:
            raise GoalPlanningError(
                "No existe ningún objetivo pendiente de aclaración en la sesión actual."
            )
        refined_goal = self.understanding.apply_clarification(
            self._pending_clarification_goal,
            clarification_text,
            run_ai_enrichment=False,
        )
        if refined_goal.needs_clarification or refined_goal.status == GoalStatus.AWAITING_CLARIFICATION:
            self._pending_clarification_goal = refined_goal
            return GoalSubmissionResult(
                goal=refined_goal,
                plan=None,
                needs_clarification=True,
                clarification_questions=tuple(refined_goal.clarification_questions),
            )

        self._enrich_goal_with_n7_context(refined_goal)
        refined_goal.capture_deterministic_baseline()
        n8_env = self._consult_n8_before_planning(refined_goal, stage="planning")
        untrusted_n8 = n8_env.to_untrusted_prompt_context() if n8_env is not None else None
        self.understanding.run_ai_enrichment(refined_goal, untrusted_n8_context=untrusted_n8)
        self._pending_clarification_goal = None
        plan = self.planner.build_plan(
            refined_goal,
            limits=limits or self.limits,
            untrusted_n8_context=untrusted_n8,
        )
        if self.is_private_mode:
            plan.is_ephemeral_private = True
        self.memory_bridge.bind_plan(refined_goal.goal_id, plan.plan_id)
        self._active_plan = plan
        if persist:
            store = self.active_goal_store
            store.save_plan(plan, set_active=True)
            wal_payload: dict[str, Any] = {"goal_id": refined_goal.goal_id}
            if refined_goal.context.extra.get("n8_query_id"):
                wal_payload["n8_query_id"] = refined_goal.context.extra["n8_query_id"]
            store.append_wal_event(plan.plan_id, "plan_created_after_clarification", wal_payload)
        return GoalSubmissionResult(goal=refined_goal, plan=plan, needs_clarification=False)

    def get_active_plan(self, *, restore_from_disk: bool = True) -> Optional[GoalPlan]:
        """Obtiene el plan activo en memoria o lo recupera desde el almacén vigente (persistente o efímero)."""
        self._reconcile_private_state()
        if self._active_plan is not None:
            return self._active_plan
        if restore_from_disk:
            loaded = self.active_goal_store.load_active_plan()
            if loaded is not None:
                self._active_plan = loaded
            return loaded
        return None

    def resume_plan(
        self,
        plan: Optional[GoalPlan] = None,
        *,
        execute: bool = False,
        cancel_event: Optional[threading.Event] = None,
        approval_callback: Optional[Callable[[GoalPlan, list[GoalTask]], bool]] = None,
        input_fn: Callable[[str], str] = input,
        strict_approval: bool = False,
        enable_replan: bool = True,
    ) -> GoalPlan | ExecutionResult:
        """Reanuda un objetivo pausado o recargado revalidando y refrescando su contexto consultivo N8."""
        self._reconcile_private_state()
        target_plan = plan or self.get_active_plan(restore_from_disk=True)
        if target_plan is None:
            raise GoalError("No hay ningún objetivo activo o recuperable para reanudar.")
        self._active_plan = target_plan
        if self.is_private_mode:
            target_plan.is_ephemeral_private = True

        self._prepare_n8_context_for_execution(target_plan, force_resume_refresh=True)
        store = self.active_goal_store
        if store is not None:
            store.save_plan(target_plan, set_active=True)
            store.append_wal_event(
                target_plan.plan_id,
                "goal_resumed_n8_revalidated",
                {
                    "goal_id": target_plan.goal.goal_id,
                    "n8_query_id": target_plan.goal.context.extra.get("n8_query_id"),
                    "n8_ref_count": len(target_plan.goal.context.extra.get("n8_memory_refs") or []),
                },
            )
        if execute:
            return self.supervisor.execute_plan(
                target_plan,
                cancel_event=cancel_event,
                approval_callback=approval_callback,
                input_fn=input_fn,
                strict_approval=strict_approval,
                enable_replan=enable_replan,
            )
        return target_plan

    def _prepare_n8_context_for_execution(
        self,
        plan: GoalPlan,
        *,
        force_resume_refresh: bool = False,
    ) -> None:
        """Revalida referencias N8 antes del uso y consulta N8 solo si se trata de una reanudación."""
        # Siempre revalida cualquier referencia existente sin auto-sustituir revocadas/expiradas.
        self.memory_bridge.revalidate_envelope(
            goal=plan.goal,
            plan=plan,
            allow_history_in_private=self.allow_history_in_private,
        )
        has_live_envelope = (
            self.memory_bridge.get_envelope(plan_id=plan.plan_id, goal_id=plan.goal.goal_id)
            is not None
        )
        is_resumed = force_resume_refresh or (
            plan.goal.status == GoalStatus.PAUSED
            or (
                not has_live_envelope
                and bool(plan.goal.context.extra.get("n8_memory_refs") or plan.history)
            )
        )
        if is_resumed and self.memory_engine is not None and self.allow_history_in_private:
            env = self.memory_bridge.retrieve_for_goal(
                plan.goal,
                stage="resuming",
                plan=plan,
                brain_manager=self.brain_manager,
                allow_history_in_private=self.allow_history_in_private,
                force_refresh=force_resume_refresh,
            )
            if env is not None:
                plan.n8_context_exposed = True
                self.memory_bridge.revalidate_envelope(
                    goal=plan.goal,
                    plan=plan,
                    allow_history_in_private=self.allow_history_in_private,
                )

    def authorize_plan(
        self,
        plan: Optional[GoalPlan] = None,
        *,
        task_ids: Optional[list[str]] = None,
        approval_token: Optional[str] = None,
    ) -> list[str]:
        """Registra aprobación explícita sobre las operaciones sensibles de un plan."""
        self._reconcile_private_state()
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
        self._reconcile_private_state()
        target_plan = plan or self.get_active_plan()
        if target_plan is None:
            raise GoalError("No hay ningún plan activo para ejecutar. Usa `/goal <objetivo>` primero.")
        if self.is_private_mode:
            target_plan.is_ephemeral_private = True
        self._active_plan = target_plan
        self._prepare_n8_context_for_execution(target_plan, force_resume_refresh=False)
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
        self._reconcile_private_state()
        target_plan = self.get_active_plan()
        if target_plan is None:
            if self._pending_clarification_goal is not None:
                self._pending_clarification_goal.cancellation_reason = reason
                self._pending_clarification_goal.transition_to(GoalStatus.CANCELLED)
                self.memory_bridge.clear_goal_envelopes(goal_id=self._pending_clarification_goal.goal_id)
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
        if self.is_private_mode or getattr(plan, "is_ephemeral_private", False):
            lines.append("Modo privado activo: almacenamiento temporal en RAM (EphemeralGoalStore).")
        if plan.goal.constraints.read_only:
            lines.append("Restricciones: MODO SOLO LECTURA (sin modificación de archivos).")
        n7_snap_id = plan.goal.context.extra.get("n7_snapshot_id")
        if n7_snap_id:
            n7_cats = ", ".join(plan.goal.context.extra.get("n7_requested_categories") or [])
            lines.append(f"Contexto N7 ({n7_snap_id}): categorías={n7_cats or 'ninguna'}")
        n8_qid = plan.goal.context.extra.get("n8_query_id")
        if n8_qid:
            n8_refs = plan.goal.context.extra.get("n8_memory_refs") or []
            lines.append(f"Referencias N8 ({n8_qid}): refs={len(n8_refs)} [untrusted_long_term_memory]")
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
        self._reconcile_private_state()
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
