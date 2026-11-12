"""Adaptador de comandos interactivos de consola para el motor N6 (`/goal` y `/plan`).

Mantiene `main.py` limpio delegando el parsing de comandos y la interacción de
confirmación explícita sin inicializar módulos de voz ni alterar el flujo del resto
de comandos (`/modo`, `/voz`, `/optimizar`, `/rollback`, `/limpiar`, `salir`).
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from src.brain.goal_engine import GoalEngine
from src.brain.goal_models import GoalError, GoalPlan, GoalTask


_POSITIVE_CONFIRMATIONS: frozenset[str] = frozenset(
    {"si", "sí", "s", "yes", "y", "confirmar", "aprobar"}
)


class GoalConsoleHandler:
    """Procesa los comandos `/goal` y `/plan` en la consola interactiva de J.A.R.V.I.S."""

    def __init__(
        self,
        *,
        goal_engine: Optional[GoalEngine] = None,
        skill_manager: Any = None,
        brain_manager: Any = None,
        ensure_orchestrator_fn: Optional[Callable[[], Any]] = None,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
    ) -> None:
        self._goal_engine: Optional[GoalEngine] = goal_engine
        self._skill_manager = skill_manager
        self._brain_manager = brain_manager
        self._ensure_orchestrator_fn = ensure_orchestrator_fn
        self._input_fn = input_fn
        self._output_fn = output_fn

    @property
    def engine(self) -> GoalEngine:
        if self._goal_engine is None:
            self._goal_engine = GoalEngine(
                skill_manager=self._skill_manager,
                brain_manager=self._brain_manager,
                use_ai=False,
            )
        return self._goal_engine

    @staticmethod
    def is_goal_command(text: str) -> bool:
        """Indica si la entrada del usuario corresponde a `/goal` o `/plan`."""
        stripped = (text or "").strip().lower()
        return (
            stripped == "/goal"
            or stripped.startswith("/goal ")
            or stripped == "/plan"
            or stripped.startswith("/plan ")
        )

    def handle_command(
        self,
        text: str,
        *,
        input_fn: Optional[Callable[[str], str]] = None,
        output_fn: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Despacha `/goal` y `/plan` retornando la respuesta textual formateada."""
        active_in = input_fn or self._input_fn
        active_out = output_fn or self._output_fn
        stripped = (text or "").strip()
        lowered = stripped.lower()

        try:
            if lowered == "/goal" or lowered.startswith("/goal "):
                raw_goal = stripped[5:].strip()
                if not raw_goal:
                    msg = "Uso: /goal <objetivo en lenguaje natural> (ej: /goal quiero optimizar el rendimiento de mi PC para jugar)"
                    active_out(f"JARVIS: {msg}")
                    return msg

                submission = self.engine.submit_goal(raw_goal)
                if submission.needs_clarification:
                    questions = "\n".join(f"  - {q}" for q in submission.clarification_questions)
                    msg = (
                        "Necesito una breve aclaración antes de construir un plan seguro:\n"
                        f"{questions}\n"
                        "Responde con `/plan aclarar <detalle>` o reformula `/goal <objetivo completo>`."
                    )
                    active_out(f"JARVIS: {msg}")
                    return msg

                assert submission.plan is not None
                summary = self.engine.format_plan_summary(submission.plan)
                msg = (
                    f"{summary}\n"
                    "Plan creado sin ejecutar acciones sensibles. "
                    "Usa `/plan ejecutar` para iniciarlo o `/plan cancelar` para abortarlo."
                )
                active_out(f"JARVIS:\n{msg}")
                return msg

            if lowered == "/plan" or lowered.startswith("/plan "):
                rest = stripped[5:].strip()
                rest_lower = rest.lower()

                if not rest or rest_lower in {"ver", "mostrar"}:
                    active = self.engine.get_active_plan()
                    if active is None:
                        msg = self.engine.format_status_report(None)
                    else:
                        msg = self.engine.format_plan_summary(active)
                    active_out(f"JARVIS:\n{msg}")
                    return msg

                if rest_lower == "estado":
                    msg = self.engine.format_status_report()
                    active_out(f"JARVIS:\n{msg}")
                    return msg

                if rest_lower == "cancelar" or rest_lower.startswith("cancelar "):
                    reason = rest[8:].strip() or "Cancelado por el usuario desde consola."
                    cancelled = self.engine.cancel_active_goal(reason=reason)
                    if cancelled is None:
                        msg = "No había ningún plan activo ni objetivo pendiente para cancelar."
                    else:
                        msg = (
                            f"Plan '{cancelled.plan_id}' cancelado correctamente "
                            f"(estado: {cancelled.goal.status.value})."
                        )
                    active_out(f"JARVIS: {msg}")
                    return msg

                if rest_lower == "aclarar" or rest_lower.startswith("aclarar "):
                    detail = rest[7:].strip()
                    if not detail:
                        msg = "Uso: /plan aclarar <información adicional del objetivo>"
                        active_out(f"JARVIS: {msg}")
                        return msg
                    submission = self.engine.clarify_pending_goal(detail)
                    if submission.needs_clarification:
                        questions = "\n".join(f"  - {q}" for q in submission.clarification_questions)
                        msg = f"Todavía requiero mayor precisión:\n{questions}"
                    else:
                        assert submission.plan is not None
                        msg = (
                            f"{self.engine.format_plan_summary(submission.plan)}\n"
                            "Usa `/plan ejecutar` cuando desees iniciar la ejecución."
                        )
                    active_out(f"JARVIS:\n{msg}")
                    return msg

                if rest_lower == "ejecutar":
                    active = self.engine.get_active_plan()
                    if active is None:
                        msg = "No hay ningún plan activo para ejecutar. Usa `/goal <objetivo>` primero."
                        active_out(f"JARVIS: {msg}")
                        return msg

                    needs_chain = any(
                        self.engine.skill_selector.requires_chain_orchestrator(t.skill_name, t.metadata)
                        for t in active.tasks
                    )
                    already_approved_chain = any(
                        self.engine.skill_selector.requires_chain_orchestrator(t.skill_name, t.metadata)
                        and t.approval_granted
                        for t in active.tasks
                    )
                    if already_approved_chain and self.engine.chain_orchestrator is None and self._ensure_orchestrator_fn is not None:
                        self.engine.set_chain_orchestrator(self._ensure_orchestrator_fn())

                    def _console_approval(plan: GoalPlan, awaiting: list[GoalTask]) -> bool:
                        active_out("\n[Autorización requerida — N6]")
                        active_out(f"Plan: {plan.plan_id} (hash={plan.plan_id and plan.plan_hash[:12]}...)")
                        for item in awaiting:
                            active_out(
                                f"  - [{item.task_id}] {item.title} | "
                                f"skill={item.skill_name} | riesgo={item.risk_category.value} | "
                                f"params={item.skill_params}"
                            )
                        try:
                            answer = active_in(
                                "¿Confirmas explícitamente autorizar estas operaciones sensibles? [si/no] > "
                            )
                        except (EOFError, KeyboardInterrupt):
                            return False
                        confirmed = str(answer or "").strip().lower() in _POSITIVE_CONFIRMATIONS
                        if confirmed and needs_chain and self.engine.chain_orchestrator is None and self._ensure_orchestrator_fn is not None:
                            self.engine.set_chain_orchestrator(self._ensure_orchestrator_fn())
                        return confirmed

                    result = self.engine.execute_plan(
                        active,
                        approval_callback=_console_approval,
                        input_fn=active_in,
                        strict_approval=False,
                    )
                    if result.stopped_for_approval:
                        msg = (
                            f"Ejecución detenida sin aplicar cambios sensibles: "
                            f"pendientes de confirmación explícita ({', '.join(result.pending_approval_task_ids)})."
                        )
                    else:
                        msg = (
                            f"Ejecución finalizada [{result.final_status.value}]: {result.summary_text}"
                        )
                    active_out(f"JARVIS: {msg}")
                    return msg

                msg = (
                    "Subcomando desconocido para /plan. Opciones: "
                    "/plan | /plan ejecutar | /plan estado | /plan cancelar | /plan aclarar <texto>"
                )
                active_out(f"JARVIS: {msg}")
                return msg

        except GoalError as err:
            msg = f"No fue posible procesar el objetivo/plan: {err}"
            active_out(f"JARVIS: {msg}")
            return msg

        msg = "Comando N6 no reconocido."
        active_out(f"JARVIS: {msg}")
        return msg
