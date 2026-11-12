"""Modelos de dominio, máquinas de estado y excepciones del motor N6 (Goal & Planning Engine).

Este módulo define la estructura formal de un objetivo (`Goal`), sus subtareas
(`GoalTask`), el plan completo (`GoalPlan`), las categorías de riesgo y las
transiciones válidas de estado. Mantiene separación estricta entre planificación
y autorización de ejecución.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import re
from typing import Any, Optional
import uuid

from config import settings


def utc_now_iso() -> str:
    """Retorna la marca de tiempo UTC actual en formato ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


def generate_short_id(prefix: str) -> str:
    """Genera un identificador legible y único con prefijo de dominio."""
    clean_prefix = re.sub(r"[^a-zA-Z0-9_-]", "", prefix).lower() or "id"
    return f"{clean_prefix}-{uuid.uuid4().hex[:12]}"


class GoalError(Exception):
    """Excepción base para fallos controlados del motor de objetivos N6."""


class GoalPlanningError(GoalError):
    """Error al interpretar el objetivo o construir/validar un plan de ejecución."""


class GoalStateError(GoalError):
    """Error por transición de estado inválida o intento de reejecutar tareas terminales."""


class GoalAuthorizationError(GoalError):
    """Error por falta de aprobación explícita o alteración del plan aprobado."""


class GoalPolicyViolationError(GoalError):
    """Error cuando una operación intenta eludir políticas de seguridad o límites."""


class GoalPersistenceError(GoalError):
    """Error al persistir, validar o recuperar estado aislado de objetivos N6."""


class GoalTimeoutError(GoalError):
    """Error cuando una tarea o la ejecución global excede el tiempo máximo configurado."""


class GoalStatus(str, Enum):
    """Estados posibles del ciclo de vida de un objetivo en J.A.R.V.I.S."""

    DRAFT = "draft"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    RUNNING = "running"
    PAUSED = "paused"
    REPLANNING = "replanning"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class GoalType(str, Enum):
    """Clasificación semántica de alto nivel para dirigir estrategias de planificación."""

    OPTIMIZATION = "optimization"
    DIAGNOSTIC = "diagnostic"
    RESEARCH = "research"
    AUTOMATION = "automation"
    DOCUMENTATION = "documentation"
    GENERAL = "general"
    AMBIGUOUS = "ambiguous"


class TaskStatus(str, Enum):
    """Estados permitidos para cada subtarea dentro de un `GoalPlan`."""

    PENDING = "pending"
    READY = "ready"
    REQUIRES_APPROVAL = "requires_approval"
    APPROVED = "approved"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class TaskRiskCategory(str, Enum):
    """Nivel de riesgo operacional de cada subtarea para gobernar la autorización.

    - SAFE_PLANNING: solo razonamiento, análisis o lectura de capacidades en memoria.
    - LOW_RISK_EXECUTION: lectura no destructiva del sistema o búsqueda/extracción acotada.
    - SENSITIVE_OPERATION: modificación de archivos, cambios de sistema, lanzamiento de apps
      o acciones con impacto externo; exige aprobación explícita e irrenunciable.
    """

    SAFE_PLANNING = "safe_planning"
    LOW_RISK_EXECUTION = "low_risk_execution"
    SENSITIVE_OPERATION = "sensitive_operation"


TERMINAL_GOAL_STATUSES: frozenset[GoalStatus] = frozenset(
    {
        GoalStatus.COMPLETED,
        GoalStatus.PARTIAL,
        GoalStatus.FAILED,
        GoalStatus.CANCELLED,
    }
)

TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.SKIPPED,
        TaskStatus.CANCELLED,
    }
)

# Mapa estricto de transiciones válidas para `Goal`.
# Nota de seguridad: `AWAITING_APPROVAL` NO puede saltar directo a `RUNNING`; debe pasar
# obligatoriamente por `APPROVED` tras confirmación explícita.
ALLOWED_GOAL_TRANSITIONS: dict[GoalStatus, frozenset[GoalStatus]] = {
    GoalStatus.DRAFT: frozenset(
        {
            GoalStatus.AWAITING_CLARIFICATION,
            GoalStatus.PLANNED,
            GoalStatus.FAILED,
            GoalStatus.CANCELLED,
        }
    ),
    GoalStatus.AWAITING_CLARIFICATION: frozenset(
        {
            GoalStatus.PLANNED,
            GoalStatus.DRAFT,
            GoalStatus.FAILED,
            GoalStatus.CANCELLED,
        }
    ),
    GoalStatus.PLANNED: frozenset(
        {
            GoalStatus.AWAITING_APPROVAL,
            GoalStatus.APPROVED,
            GoalStatus.RUNNING,
            GoalStatus.REPLANNING,
            GoalStatus.FAILED,
            GoalStatus.CANCELLED,
        }
    ),
    GoalStatus.AWAITING_APPROVAL: frozenset(
        {
            GoalStatus.APPROVED,
            GoalStatus.REPLANNING,
            GoalStatus.FAILED,
            GoalStatus.CANCELLED,
        }
    ),
    GoalStatus.APPROVED: frozenset(
        {
            GoalStatus.RUNNING,
            GoalStatus.PAUSED,
            GoalStatus.REPLANNING,
            GoalStatus.FAILED,
            GoalStatus.CANCELLED,
        }
    ),
    GoalStatus.RUNNING: frozenset(
        {
            GoalStatus.PAUSED,
            GoalStatus.REPLANNING,
            GoalStatus.AWAITING_APPROVAL,
            GoalStatus.COMPLETED,
            GoalStatus.PARTIAL,
            GoalStatus.FAILED,
            GoalStatus.CANCELLED,
        }
    ),
    GoalStatus.PAUSED: frozenset(
        {
            GoalStatus.RUNNING,
            GoalStatus.REPLANNING,
            GoalStatus.AWAITING_APPROVAL,
            GoalStatus.PARTIAL,
            GoalStatus.FAILED,
            GoalStatus.CANCELLED,
        }
    ),
    GoalStatus.REPLANNING: frozenset(
        {
            GoalStatus.PLANNED,
            GoalStatus.AWAITING_APPROVAL,
            GoalStatus.APPROVED,
            GoalStatus.RUNNING,
            GoalStatus.PARTIAL,
            GoalStatus.FAILED,
            GoalStatus.CANCELLED,
        }
    ),
    GoalStatus.COMPLETED: frozenset(),
    GoalStatus.PARTIAL: frozenset(),
    GoalStatus.FAILED: frozenset(),
    GoalStatus.CANCELLED: frozenset(),
}

# Mapa estricto de transiciones válidas para `GoalTask`.
# Nota de seguridad:
# - `REQUIRES_APPROVAL` nunca pasa directo a `RUNNING`; requiere pasar por `APPROVED`.
# - `COMPLETED`, `FAILED`, `SKIPPED` y `CANCELLED` son inmutables (evita ejecuciones duplicadas).
ALLOWED_TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {
            TaskStatus.READY,
            TaskStatus.REQUIRES_APPROVAL,
            TaskStatus.BLOCKED,
            TaskStatus.SKIPPED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.READY: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.REQUIRES_APPROVAL,
            TaskStatus.BLOCKED,
            TaskStatus.SKIPPED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.REQUIRES_APPROVAL: frozenset(
        {
            TaskStatus.APPROVED,
            TaskStatus.BLOCKED,
            TaskStatus.SKIPPED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.APPROVED: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.BLOCKED,
            TaskStatus.SKIPPED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.READY,  # Solo para reintentos acotados bajo control de `retry_count`
            TaskStatus.REQUIRES_APPROVAL,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.BLOCKED: frozenset(
        {
            TaskStatus.READY,
            TaskStatus.REQUIRES_APPROVAL,
            TaskStatus.SKIPPED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.SKIPPED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


@dataclass(frozen=True)
class GoalEngineLimits:
    """Límites operativos y de seguridad que restringen la autonomía de N6."""

    max_tasks: int = field(default_factory=lambda: settings.GOAL_MAX_TASKS)
    max_depth: int = field(default_factory=lambda: settings.GOAL_MAX_DEPTH)
    max_retries: int = field(default_factory=lambda: settings.GOAL_MAX_TASK_RETRIES)
    task_timeout_sec: float = field(default_factory=lambda: settings.GOAL_TASK_TIMEOUT_SEC)
    total_timeout_sec: float = field(default_factory=lambda: settings.GOAL_TOTAL_TIMEOUT_SEC)
    max_replans: int = field(default_factory=lambda: settings.GOAL_MAX_REPLANS)

    def __post_init__(self) -> None:
        if self.max_tasks < 1:
            raise GoalPlanningError("El límite `max_tasks` debe ser mayor o igual a 1.")
        if self.max_depth < 1:
            raise GoalPlanningError("El límite `max_depth` debe ser mayor o igual a 1.")
        if self.max_retries < 0:
            raise GoalPlanningError("El límite `max_retries` no puede ser negativo.")
        if self.task_timeout_sec <= 0:
            raise GoalPlanningError("El límite `task_timeout_sec` debe ser positivo.")
        if self.total_timeout_sec <= 0:
            raise GoalPlanningError("El límite `total_timeout_sec` debe ser positivo.")
        if self.max_replans < 0:
            raise GoalPlanningError("El límite `max_replans` no puede ser negativo.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_tasks": self.max_tasks,
            "max_depth": self.max_depth,
            "max_retries": self.max_retries,
            "task_timeout_sec": self.task_timeout_sec,
            "total_timeout_sec": self.total_timeout_sec,
            "max_replans": self.max_replans,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "GoalEngineLimits":
        if not data:
            return cls()
        return cls(
            max_tasks=int(data.get("max_tasks", settings.GOAL_MAX_TASKS)),
            max_depth=int(data.get("max_depth", settings.GOAL_MAX_DEPTH)),
            max_retries=int(data.get("max_retries", settings.GOAL_MAX_TASK_RETRIES)),
            task_timeout_sec=float(data.get("task_timeout_sec", settings.GOAL_TASK_TIMEOUT_SEC)),
            total_timeout_sec=float(data.get("total_timeout_sec", settings.GOAL_TOTAL_TIMEOUT_SEC)),
            max_replans=int(data.get("max_replans", settings.GOAL_MAX_REPLANS)),
        )


@dataclass
class SuccessCriteria:
    """Criterios observables para evaluar si un objetivo quedó satisfecho."""

    summary: str
    required_outcomes: list[str] = field(default_factory=list)
    verification_checks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "required_outcomes": list(self.required_outcomes),
            "verification_checks": list(self.verification_checks),
        }

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "SuccessCriteria":
        data = data or {}
        return cls(
            summary=str(data.get("summary") or "Cumplir el objetivo de forma segura."),
            required_outcomes=[str(item) for item in (data.get("required_outcomes") or [])],
            verification_checks=[str(item) for item in (data.get("verification_checks") or [])],
        )


@dataclass
class Constraints:
    """Restricciones explícitas o de política derivadas del usuario y del sistema."""

    read_only: bool = False
    allow_file_modifications: bool = True
    allow_network: bool = True
    require_explicit_approval_for_changes: bool = True
    forbidden_actions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "read_only": self.read_only,
            "allow_file_modifications": self.allow_file_modifications,
            "allow_network": self.allow_network,
            "require_explicit_approval_for_changes": self.require_explicit_approval_for_changes,
            "forbidden_actions": list(self.forbidden_actions),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "Constraints":
        data = data or {}
        return cls(
            read_only=bool(data.get("read_only", False)),
            allow_file_modifications=bool(data.get("allow_file_modifications", True)),
            allow_network=bool(data.get("allow_network", True)),
            require_explicit_approval_for_changes=bool(
                data.get("require_explicit_approval_for_changes", True)
            ),
            forbidden_actions=[str(x) for x in (data.get("forbidden_actions") or [])],
            notes=[str(x) for x in (data.get("notes") or [])],
        )


@dataclass
class RequiredCapabilities:
    """Capacidades técnicas identificadas durante el análisis del objetivo."""

    skills: list[str] = field(default_factory=list)
    requires_chain_orchestrator: bool = False
    requires_web: bool = False
    requires_hardware_inspection: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "skills": list(self.skills),
            "requires_chain_orchestrator": self.requires_chain_orchestrator,
            "requires_web": self.requires_web,
            "requires_hardware_inspection": self.requires_hardware_inspection,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "RequiredCapabilities":
        data = data or {}
        return cls(
            skills=[str(x) for x in (data.get("skills") or [])],
            requires_chain_orchestrator=bool(data.get("requires_chain_orchestrator", False)),
            requires_web=bool(data.get("requires_web", False)),
            requires_hardware_inspection=bool(data.get("requires_hardware_inspection", False)),
        )


@dataclass
class GoalContext:
    """Contexto técnico conocido al momento de interpretar o ejecutar el objetivo."""

    hardware: dict[str, Any] = field(default_factory=dict)
    target_app: Optional[str] = None
    target_paths: list[str] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hardware": dict(self.hardware),
            "target_app": self.target_app,
            "target_paths": list(self.target_paths),
            "environment": dict(self.environment),
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "GoalContext":
        data = data or {}
        return cls(
            hardware=dict(data.get("hardware") or {}),
            target_app=data.get("target_app"),
            target_paths=[str(x) for x in (data.get("target_paths") or [])],
            environment={str(k): str(v) for k, v in (data.get("environment") or {}).items()},
            extra=dict(data.get("extra") or {}),
        )


class GoalTask:
    """Unidad mínima ejecutable dentro de un `GoalPlan` con transición de estado blindada."""

    def __init__(
        self,
        *,
        task_id: str,
        title: str,
        description: str,
        skill_name: Optional[str] = None,
        skill_params: Optional[dict[str, Any]] = None,
        risk_category: TaskRiskCategory = TaskRiskCategory.LOW_RISK_EXECUTION,
        requires_approval: bool = False,
        depends_on: Optional[list[str]] = None,
        expected_output: str = "",
        status: TaskStatus = TaskStatus.PENDING,
        retry_count: int = 0,
        max_retries: int = 1,
        timeout_sec: float = 30.0,
        approval_granted: bool = False,
        approval_token: Optional[str] = None,
        approval_hash: Optional[str] = None,
        chain_execution_id: Optional[str] = None,
        execution_evidence: Optional[dict[str, Any]] = None,
        output_summary: Optional[str] = None,
        untrusted_envelope: Optional[dict[str, Any]] = None,
        last_error: Optional[str] = None,
        error_history: Optional[list[str]] = None,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
        replan_generation: int = 0,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        clean_id = (task_id or "").strip()
        if not clean_id:
            raise GoalPlanningError("Toda `GoalTask` debe tener un `task_id` no vacío.")
        self.task_id: str = clean_id
        self.title: str = (title or "").strip() or clean_id
        self.description: str = (description or "").strip() or self.title
        self.skill_name: Optional[str] = skill_name.strip() if isinstance(skill_name, str) and skill_name.strip() else None
        self.skill_params: dict[str, Any] = dict(skill_params or {})
        self.risk_category: TaskRiskCategory = (
            risk_category
            if isinstance(risk_category, TaskRiskCategory)
            else TaskRiskCategory(str(risk_category))
        )
        # Invariante de seguridad: toda operación sensible SIEMPRE exige aprobación.
        self.requires_approval: bool = bool(requires_approval) or (
            self.risk_category == TaskRiskCategory.SENSITIVE_OPERATION
        )
        self.depends_on: list[str] = list(dict.fromkeys(str(dep).strip() for dep in (depends_on or []) if str(dep).strip()))
        self.expected_output: str = (expected_output or "").strip() or f"Resultado verificado de {self.title}."
        self.retry_count: int = int(retry_count)
        self.max_retries: int = max(0, int(max_retries))
        self.timeout_sec: float = max(0.1, float(timeout_sec))
        self.approval_granted: bool = bool(approval_granted)
        self.approval_token: Optional[str] = approval_token
        self.approval_hash: Optional[str] = approval_hash
        self.chain_execution_id: Optional[str] = chain_execution_id
        self.execution_evidence: Optional[dict[str, Any]] = dict(execution_evidence) if execution_evidence else None
        self.output_summary: Optional[str] = output_summary
        self.untrusted_envelope: Optional[dict[str, Any]] = dict(untrusted_envelope) if untrusted_envelope else None
        self.last_error: Optional[str] = last_error
        self.error_history: list[str] = list(error_history or [])
        self.started_at: Optional[str] = started_at
        self.finished_at: Optional[str] = finished_at
        self.replan_generation: int = int(replan_generation)
        self.metadata: dict[str, Any] = dict(metadata or {})
        initial_status = status if isinstance(status, TaskStatus) else TaskStatus(str(status))
        if initial_status == TaskStatus.COMPLETED and not self._has_verifiable_evidence(
            self.execution_evidence, self.output_summary
        ):
            raise GoalStateError(
                f"La tarea '{self.task_id}' no puede inicializarse en COMPLETED sin evidencia verificable."
            )
        self._status: TaskStatus = initial_status

    @staticmethod
    def _has_verifiable_evidence(
        evidence: Optional[dict[str, Any]],
        output_summary: Optional[str],
    ) -> bool:
        """Verifica que exista evidencia estructurada o resumen de salida no vacío."""
        if isinstance(evidence, dict) and len(evidence) > 0:
            return True
        if isinstance(output_summary, str) and output_summary.strip():
            return True
        return False

    @property
    def status(self) -> TaskStatus:
        return self._status

    @status.setter
    def status(self, new_status: TaskStatus) -> None:
        """Protege mutaciones directas forzando la máquina de estados."""
        self.transition_to(new_status)

    def transition_to(self, new_status: TaskStatus) -> TaskStatus:
        """Aplica una transición de estado validada contra `ALLOWED_TASK_TRANSITIONS`."""
        target = new_status if isinstance(new_status, TaskStatus) else TaskStatus(str(new_status))
        if target == self._status:
            return self._status

        if target == TaskStatus.RUNNING and (
            self._status == TaskStatus.REQUIRES_APPROVAL
            or (self.requires_approval and not self.approval_granted)
        ):
            raise GoalAuthorizationError(
                f"La tarea sensible '{self.task_id}' requiere aprobación explícita antes de ejecutarse."
            )

        allowed = ALLOWED_TASK_TRANSITIONS.get(self._status, frozenset())
        if target not in allowed:
            raise GoalStateError(
                f"Transición inválida en tarea '{self.task_id}': "
                f"{self._status.value} -> {target.value}."
            )

        if target == TaskStatus.COMPLETED and not self._has_verifiable_evidence(
            self.execution_evidence, self.output_summary
        ):
            raise GoalStateError(
                f"La tarea '{self.task_id}' no puede pasar a COMPLETED sin evidencia verificable."
            )

        self._status = target
        return self._status

    def grant_approval(
        self,
        *,
        approval_token: Optional[str] = None,
        approval_hash: Optional[str] = None,
    ) -> None:
        """Marca la tarea sensible como aprobada explícitamente por el usuario."""
        self.approval_granted = True
        if approval_token:
            self.approval_token = approval_token
        if approval_hash:
            self.approval_hash = approval_hash
        if self._status in {TaskStatus.PENDING, TaskStatus.READY, TaskStatus.REQUIRES_APPROVAL, TaskStatus.BLOCKED}:
            if self._status == TaskStatus.PENDING:
                self.transition_to(TaskStatus.REQUIRES_APPROVAL)
            elif self._status == TaskStatus.READY:
                self.transition_to(TaskStatus.REQUIRES_APPROVAL)
            elif self._status == TaskStatus.BLOCKED:
                self.transition_to(TaskStatus.REQUIRES_APPROVAL)
            self.transition_to(TaskStatus.APPROVED)

    def mark_completed(
        self,
        *,
        output_summary: str,
        evidence: dict[str, Any],
        untrusted_envelope: Optional[dict[str, Any]] = None,
    ) -> None:
        """Registra evidencia verificable y transiciona la tarea a `COMPLETED`."""
        clean_summary = (output_summary or "").strip()
        if not clean_summary and not (isinstance(evidence, dict) and evidence):
            raise GoalStateError(
                f"Imposible completar la tarea '{self.task_id}' sin evidencia ni resumen verificable."
            )
        self.output_summary = clean_summary or "Completada con evidencia."
        self.execution_evidence = dict(evidence or {"verified": True, "summary": self.output_summary})
        if untrusted_envelope is not None:
            self.untrusted_envelope = dict(untrusted_envelope)
        self.finished_at = utc_now_iso()
        self.transition_to(TaskStatus.COMPLETED)

    def mark_failed(self, error_message: str) -> None:
        """Registra el fallo definitivo y transiciona a `FAILED`."""
        clean_err = (error_message or "").strip() or "Error desconocido en ejecución de subtarea."
        self.last_error = clean_err
        self.error_history.append(clean_err)
        self.finished_at = utc_now_iso()
        if self._status != TaskStatus.FAILED:
            self.transition_to(TaskStatus.FAILED)

    def schedule_retry(self, error_message: str) -> None:
        """Reencola la tarea para reintento acotado si no agotó `max_retries`."""
        if self.retry_count >= self.max_retries:
            raise GoalStateError(
                f"La tarea '{self.task_id}' agotó su límite de reintentos ({self.max_retries})."
            )
        clean_err = (error_message or "").strip() or "Fallo transitorio en subtarea."
        self.last_error = clean_err
        self.error_history.append(clean_err)
        self.retry_count += 1
        if self.requires_approval and self.approval_granted:
            # Si ya fue aprobada explícitamente, vuelve a READY (ya tiene `approval_granted=True`)
            self.transition_to(TaskStatus.READY)
        else:
            self.transition_to(TaskStatus.READY)

    def canonical_definition_dict(self) -> dict[str, Any]:
        """Representación estructural inmutable para calcular integridad criptográfica del plan."""
        return {
            "task_id": self.task_id,
            "title": self.title,
            "description": self.description,
            "skill_name": self.skill_name,
            "skill_params": self.skill_params,
            "risk_category": self.risk_category.value,
            "requires_approval": self.requires_approval,
            "depends_on": list(self.depends_on),
            "expected_output": self.expected_output,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "description": self.description,
            "skill_name": self.skill_name,
            "skill_params": dict(self.skill_params),
            "risk_category": self.risk_category.value,
            "requires_approval": self.requires_approval,
            "depends_on": list(self.depends_on),
            "expected_output": self.expected_output,
            "status": self._status.value,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "timeout_sec": self.timeout_sec,
            "approval_granted": self.approval_granted,
            "approval_token": self.approval_token,
            "approval_hash": self.approval_hash,
            "chain_execution_id": self.chain_execution_id,
            "execution_evidence": dict(self.execution_evidence) if self.execution_evidence else None,
            "output_summary": self.output_summary,
            "untrusted_envelope": dict(self.untrusted_envelope) if self.untrusted_envelope else None,
            "last_error": self.last_error,
            "error_history": list(self.error_history),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "replan_generation": self.replan_generation,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GoalTask":
        return cls(
            task_id=str(data["task_id"]),
            title=str(data.get("title") or data["task_id"]),
            description=str(data.get("description") or ""),
            skill_name=data.get("skill_name"),
            skill_params=dict(data.get("skill_params") or {}),
            risk_category=TaskRiskCategory(str(data.get("risk_category", TaskRiskCategory.LOW_RISK_EXECUTION.value))),
            requires_approval=bool(data.get("requires_approval", False)),
            depends_on=[str(x) for x in (data.get("depends_on") or [])],
            expected_output=str(data.get("expected_output") or ""),
            status=TaskStatus(str(data.get("status", TaskStatus.PENDING.value))),
            retry_count=int(data.get("retry_count", 0)),
            max_retries=int(data.get("max_retries", 1)),
            timeout_sec=float(data.get("timeout_sec", 30.0)),
            approval_granted=bool(data.get("approval_granted", False)),
            approval_token=data.get("approval_token"),
            approval_hash=data.get("approval_hash"),
            chain_execution_id=data.get("chain_execution_id"),
            execution_evidence=data.get("execution_evidence"),
            output_summary=data.get("output_summary"),
            untrusted_envelope=data.get("untrusted_envelope"),
            last_error=data.get("last_error"),
            error_history=[str(x) for x in (data.get("error_history") or [])],
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            replan_generation=int(data.get("replan_generation", 0)),
            metadata=dict(data.get("metadata") or {}),
        )


class Goal:
    """Representación estructurada de un objetivo del usuario y su estado global."""

    def __init__(
        self,
        *,
        goal_id: Optional[str] = None,
        raw_input: str,
        interpreted_summary: str,
        goal_type: GoalType = GoalType.GENERAL,
        status: GoalStatus = GoalStatus.DRAFT,
        success_criteria: Optional[SuccessCriteria] = None,
        constraints: Optional[Constraints] = None,
        required_capabilities: Optional[RequiredCapabilities] = None,
        context: Optional[GoalContext] = None,
        needs_clarification: bool = False,
        clarification_questions: Optional[list[str]] = None,
        missing_info: Optional[list[str]] = None,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
        approved_at: Optional[str] = None,
        completed_at: Optional[str] = None,
        cancellation_reason: Optional[str] = None,
        error_summary: Optional[str] = None,
    ) -> None:
        self.goal_id: str = (goal_id or "").strip() or generate_short_id("goal")
        self.raw_input: str = (raw_input or "").strip()
        self.interpreted_summary: str = (interpreted_summary or "").strip() or self.raw_input
        self.goal_type: GoalType = (
            goal_type if isinstance(goal_type, GoalType) else GoalType(str(goal_type))
        )
        self._status: GoalStatus = status if isinstance(status, GoalStatus) else GoalStatus(str(status))
        self.success_criteria: SuccessCriteria = success_criteria or SuccessCriteria(
            summary=f"Resolver de forma segura y verificable: {self.interpreted_summary}"
        )
        self.constraints: Constraints = constraints or Constraints()
        self.required_capabilities: RequiredCapabilities = required_capabilities or RequiredCapabilities()
        self.context: GoalContext = context or GoalContext()
        self.needs_clarification: bool = bool(needs_clarification)
        self.clarification_questions: list[str] = list(clarification_questions or [])
        self.missing_info: list[str] = list(missing_info or [])
        now_iso = utc_now_iso()
        self.created_at: str = created_at or now_iso
        self.updated_at: str = updated_at or now_iso
        self.approved_at: Optional[str] = approved_at
        self.completed_at: Optional[str] = completed_at
        self.cancellation_reason: Optional[str] = cancellation_reason
        self.error_summary: Optional[str] = error_summary

    @property
    def status(self) -> GoalStatus:
        return self._status

    @status.setter
    def status(self, new_status: GoalStatus) -> None:
        self.transition_to(new_status)

    def transition_to(self, new_status: GoalStatus) -> GoalStatus:
        """Valida y ejecuta una transición de estado del objetivo."""
        target = new_status if isinstance(new_status, GoalStatus) else GoalStatus(str(new_status))
        if target == self._status:
            return self._status
        allowed = ALLOWED_GOAL_TRANSITIONS.get(self._status, frozenset())
        if target not in allowed:
            raise GoalStateError(
                f"Transición inválida en objetivo '{self.goal_id}': "
                f"{self._status.value} -> {target.value}."
            )
        self._status = target
        now_iso = utc_now_iso()
        self.updated_at = now_iso
        if target == GoalStatus.APPROVED and not self.approved_at:
            self.approved_at = now_iso
        if target in TERMINAL_GOAL_STATUSES:
            self.completed_at = now_iso
        return self._status

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "raw_input": self.raw_input,
            "interpreted_summary": self.interpreted_summary,
            "goal_type": self.goal_type.value,
            "status": self._status.value,
            "success_criteria": self.success_criteria.to_dict(),
            "constraints": self.constraints.to_dict(),
            "required_capabilities": self.required_capabilities.to_dict(),
            "context": self.context.to_dict(),
            "needs_clarification": self.needs_clarification,
            "clarification_questions": list(self.clarification_questions),
            "missing_info": list(self.missing_info),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "approved_at": self.approved_at,
            "completed_at": self.completed_at,
            "cancellation_reason": self.cancellation_reason,
            "error_summary": self.error_summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Goal":
        return cls(
            goal_id=str(data["goal_id"]),
            raw_input=str(data.get("raw_input") or ""),
            interpreted_summary=str(data.get("interpreted_summary") or ""),
            goal_type=GoalType(str(data.get("goal_type", GoalType.GENERAL.value))),
            status=GoalStatus(str(data.get("status", GoalStatus.DRAFT.value))),
            success_criteria=SuccessCriteria.from_dict(data.get("success_criteria")),
            constraints=Constraints.from_dict(data.get("constraints")),
            required_capabilities=RequiredCapabilities.from_dict(data.get("required_capabilities")),
            context=GoalContext.from_dict(data.get("context")),
            needs_clarification=bool(data.get("needs_clarification", False)),
            clarification_questions=[str(x) for x in (data.get("clarification_questions") or [])],
            missing_info=[str(x) for x in (data.get("missing_info") or [])],
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            approved_at=data.get("approved_at"),
            completed_at=data.get("completed_at"),
            cancellation_reason=data.get("cancellation_reason"),
            error_summary=data.get("error_summary"),
        )


@dataclass
class ExecutionHistoryEntry:
    """Entrada cronológica auditable de eventos durante planificación, ejecución y replan."""

    event_type: str
    message: str
    task_id: Optional[str] = None
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: generate_short_id("evt"))
    timestamp: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "message": self.message,
            "task_id": self.task_id,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionHistoryEntry":
        return cls(
            event_id=str(data.get("event_id") or generate_short_id("evt")),
            timestamp=str(data.get("timestamp") or utc_now_iso()),
            event_type=str(data.get("event_type") or "info"),
            message=str(data.get("message") or ""),
            task_id=data.get("task_id"),
            from_status=data.get("from_status"),
            to_status=data.get("to_status"),
            details=dict(data.get("details") or {}),
        )


@dataclass
class ReplanDecision:
    """Registro trazable de una decisión de replanificación controlada."""

    trigger_task_id: Optional[str]
    reason: str
    strategy: str
    preserved_task_ids: list[str] = field(default_factory=list)
    added_task_ids: list[str] = field(default_factory=list)
    cancelled_task_ids: list[str] = field(default_factory=list)
    replan_id: str = field(default_factory=lambda: generate_short_id("rpl"))
    timestamp: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "replan_id": self.replan_id,
            "timestamp": self.timestamp,
            "trigger_task_id": self.trigger_task_id,
            "reason": self.reason,
            "strategy": self.strategy,
            "preserved_task_ids": list(self.preserved_task_ids),
            "added_task_ids": list(self.added_task_ids),
            "cancelled_task_ids": list(self.cancelled_task_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReplanDecision":
        return cls(
            replan_id=str(data.get("replan_id") or generate_short_id("rpl")),
            timestamp=str(data.get("timestamp") or utc_now_iso()),
            trigger_task_id=data.get("trigger_task_id"),
            reason=str(data.get("reason") or ""),
            strategy=str(data.get("strategy") or ""),
            preserved_task_ids=[str(x) for x in (data.get("preserved_task_ids") or [])],
            added_task_ids=[str(x) for x in (data.get("added_task_ids") or [])],
            cancelled_task_ids=[str(x) for x in (data.get("cancelled_task_ids") or [])],
        )


@dataclass
class GoalPlan:
    """Plan estructurado, verificable y versionado que vincula un `Goal` con sus `GoalTask`."""

    goal: Goal
    tasks: list[GoalTask]
    limits: GoalEngineLimits = field(default_factory=GoalEngineLimits)
    plan_id: str = field(default_factory=lambda: generate_short_id("plan"))
    version: int = 1
    replan_count: int = 0
    plan_hash: str = ""
    approval_bound_hash: Optional[str] = None
    history: list[ExecutionHistoryEntry] = field(default_factory=list)
    replan_history: list[ReplanDecision] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if not self.plan_hash:
            self.plan_hash = self.compute_plan_hash()

    def compute_plan_hash(self) -> str:
        """Calcula hash SHA-256 canónico sobre la estructura aprobable del plan."""
        canonical_payload = {
            "plan_id": self.plan_id,
            "goal_id": self.goal.goal_id,
            "goal_type": self.goal.goal_type.value,
            "constraints": self.goal.constraints.to_dict(),
            "version": self.version,
            "tasks": [task.canonical_definition_dict() for task in self.tasks],
        }
        serialized = json.dumps(
            canonical_payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def refresh_hash(self) -> str:
        """Recalcula `plan_hash` tras una replanificación legítima y actualiza timestamp."""
        self.plan_hash = self.compute_plan_hash()
        self.updated_at = utc_now_iso()
        return self.plan_hash

    def verify_integrity(self) -> bool:
        """Comprueba que la estructura del plan coincida con `plan_hash` y con cualquier aprobación."""
        current = self.compute_plan_hash()
        if current != self.plan_hash:
            return False
        if self.approval_bound_hash is not None and current != self.approval_bound_hash:
            return False
        return True

    def bind_approval(self) -> str:
        """Fija el hash estructural actual como evidencia vinculante de la aprobación del usuario."""
        self.refresh_hash()
        self.approval_bound_hash = self.plan_hash
        return self.approval_bound_hash

    def get_task(self, task_id: str) -> Optional[GoalTask]:
        """Obtiene una subtarea por identificador o `None` si no existe."""
        for task in self.tasks:
            if task.task_id == task_id:
                return task
        return None

    def sensitive_tasks(self) -> list[GoalTask]:
        """Lista todas las tareas que requieren aprobación explícita por política o riesgo."""
        return [
            task
            for task in self.tasks
            if task.requires_approval or task.risk_category == TaskRiskCategory.SENSITIVE_OPERATION
        ]

    def record_event(
        self,
        event_type: str,
        message: str,
        *,
        task_id: Optional[str] = None,
        from_status: Optional[str] = None,
        to_status: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> ExecutionHistoryEntry:
        """Añade un evento auditable al historial sin perder trazas previas."""
        entry = ExecutionHistoryEntry(
            event_type=event_type,
            message=message,
            task_id=task_id,
            from_status=from_status,
            to_status=to_status,
            details=dict(details or {}),
        )
        self.history.append(entry)
        self.updated_at = utc_now_iso()
        return entry

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "goal": self.goal.to_dict(),
            "tasks": [task.to_dict() for task in self.tasks],
            "limits": self.limits.to_dict(),
            "version": self.version,
            "replan_count": self.replan_count,
            "plan_hash": self.plan_hash,
            "approval_bound_hash": self.approval_bound_hash,
            "history": [entry.to_dict() for entry in self.history],
            "replan_history": [decision.to_dict() for decision in self.replan_history],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GoalPlan":
        plan = cls(
            plan_id=str(data["plan_id"]),
            goal=Goal.from_dict(data["goal"]),
            tasks=[GoalTask.from_dict(item) for item in (data.get("tasks") or [])],
            limits=GoalEngineLimits.from_dict(data.get("limits")),
            version=int(data.get("version", 1)),
            replan_count=int(data.get("replan_count", 0)),
            plan_hash=str(data.get("plan_hash") or ""),
            approval_bound_hash=data.get("approval_bound_hash"),
            history=[ExecutionHistoryEntry.from_dict(item) for item in (data.get("history") or [])],
            replan_history=[ReplanDecision.from_dict(item) for item in (data.get("replan_history") or [])],
            created_at=str(data.get("created_at") or utc_now_iso()),
            updated_at=str(data.get("updated_at") or utc_now_iso()),
        )
        if not plan.verify_integrity():
            raise GoalPersistenceError(
                f"Integridad comprometida en GoalPlan '{plan.plan_id}': el hash estructural no coincide."
            )
        return plan
