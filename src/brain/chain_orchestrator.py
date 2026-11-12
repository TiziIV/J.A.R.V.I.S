"""
Orquestador de cadenas de razonamiento y ejecución segura (ChainOrchestrator) para J.A.R.V.I.S.

Conecta el diagnóstico local de hardware/configuración, la investigación asistida por
proveedores de IA (BrainManager: Ollama, Gemini, OpenAI, Claude, Codex CLI), la planificación
estructurada (`ChangePlan` y `ChangeOperation`), la validación estricta de políticas locales
(`PolicyEngine`), la simulación en solo lectura, la aprobación vinculada a un hash canónico
SHA-256 y la aplicación transaccional mediante `FileEditorSkill` con verificación post-escritura
y rollback verificable.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import os
import re
import secrets
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from config import settings
from skills.file_editor_skill import (
    _INI_SECTION_RE,
    _XML_XXE_RE,
    BackupRecord,
    FileEditorSkill,
    RollbackManager,
    _decode_bytes_strict,
    _split_line_ending,
    compute_sha256_file,
)
from src.brain.transaction_manager import (
    InvalidTransactionStateError,
    JournalCorruptionError,
    OperationJournalStage,
    RecoveryReport,
    SimulatedTransactionInterrupt,
    TransactionLockTimeoutError,
    TransactionManager,
    TransactionRecord,
    TransactionState,
)


class ChainState(str, Enum):
    """Estados válidos de la máquina de estados de `ChainOrchestrator`."""

    RECEIVED = "RECEIVED"
    INVESTIGATING = "INVESTIGATING"
    PLANNING = "PLANNING"
    VALIDATING = "VALIDATING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPLYING = "APPLYING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


# Tabla de transiciones permitidas para garantizar trazabilidad y evitar saltos ilegales
# (por ejemplo, pasar de RECEIVED o PLANNING directamente a APPLYING sin VALIDATING y AWAITING_APPROVAL).
ALLOWED_STATE_TRANSITIONS: dict[ChainState, set[ChainState]] = {
    ChainState.RECEIVED: {
        ChainState.INVESTIGATING,
        ChainState.PLANNING,
        ChainState.REJECTED,
        ChainState.CANCELLED,
        ChainState.FAILED,
    },
    ChainState.INVESTIGATING: {
        ChainState.PLANNING,
        ChainState.REJECTED,
        ChainState.CANCELLED,
        ChainState.FAILED,
    },
    ChainState.PLANNING: {
        ChainState.VALIDATING,
        ChainState.REJECTED,
        ChainState.CANCELLED,
        ChainState.FAILED,
    },
    ChainState.VALIDATING: {
        ChainState.AWAITING_APPROVAL,
        ChainState.REJECTED,
        ChainState.CANCELLED,
        ChainState.FAILED,
    },
    ChainState.AWAITING_APPROVAL: {
        ChainState.APPLYING,
        ChainState.REJECTED,
        ChainState.CANCELLED,
        ChainState.FAILED,
    },
    ChainState.APPLYING: {
        ChainState.VERIFYING,
        ChainState.ROLLED_BACK,
        ChainState.FAILED,
    },
    ChainState.VERIFYING: {
        ChainState.COMPLETED,
        ChainState.ROLLED_BACK,
        ChainState.FAILED,
    },
    ChainState.COMPLETED: {
        ChainState.ROLLED_BACK,
        ChainState.FAILED,
        ChainState.VALIDATING,
        ChainState.AWAITING_APPROVAL,
    },
    ChainState.REJECTED: set(),
    ChainState.CANCELLED: set(),
    ChainState.FAILED: {
        ChainState.ROLLED_BACK,
    },
    ChainState.ROLLED_BACK: set(),
}


class OperationStatus(str, Enum):
    """
    Estados válidos del ciclo de vida de cada operación individual (`ChangeOperation`).

    Para un desarrollador principiante:
    - `PENDING`: la operación aún no se ha aplicado en disco.
    - `APPLIED` (o su alias `EXECUTED`): la operación fue escrita atómicamente y verificada en disco.
    - `FAILED`: la operación falló durante su validación, escritura o verificación post-escritura.
    - `BLOCKED`: la operación no pudo ejecutarse porque una dependencia directa o transitiva falló.
    """

    PENDING = "PENDING"
    APPLIED = "APPLIED"
    EXECUTED = "APPLIED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    ROLLED_BACK = "ROLLED_BACK"

    @classmethod
    def _missing_(cls, value: object) -> Optional["OperationStatus"]:
        if isinstance(value, str) and value.strip().upper() == "EXECUTED":
            return cls.APPLIED
        return None


# Transiciones controladas por operación. El rollback solo se registra tras verificar el disco:
# ninguna operación en estado terminal puede volver a modificarse silenciosamente.
ALLOWED_OPERATION_STATUS_TRANSITIONS: dict[OperationStatus, set[OperationStatus]] = {
    OperationStatus.PENDING: {
        OperationStatus.APPLIED,
        OperationStatus.FAILED,
        OperationStatus.BLOCKED,
    },
    OperationStatus.APPLIED: {OperationStatus.ROLLBACK_FAILED, OperationStatus.ROLLED_BACK},
    OperationStatus.ROLLBACK_FAILED: {OperationStatus.ROLLED_BACK},
    OperationStatus.FAILED: set(),
    OperationStatus.BLOCKED: set(),
    OperationStatus.ROLLED_BACK: set(),
}

MAX_OPERATIONS_PER_PLAN = max(1, int(getattr(settings, "CHAIN_MAX_OPERATIONS", 5000)))
_APPLICATION_EVIDENCE_AUTHORITY = object()

VALID_FORMATS: frozenset[str] = frozenset({"ini", "cfg", "conf", "json", "xml", "txt"})
VALID_OPERATION_TYPES: frozenset[str] = frozenset(
    {"update_key", "update_json_key", "update_xml_node", "replace_content"}
)
VALID_RISK_LEVELS: frozenset[str] = frozenset({"LOW", "MEDIUM", "HIGH"})
VALID_EVIDENCE_LEVELS: frozenset[str] = frozenset(
    {"VERIFIED", "PLAUSIBLE", "UNSUPPORTED", "REJECTED"}
)
APPROVABLE_EVIDENCE_LEVELS: frozenset[str] = frozenset({"VERIFIED", "PLAUSIBLE"})

_CHANGE_OPERATION_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "file_path",
        "format",
        "operation_type",
        "section",
        "key",
        "expected_current_value",
        "proposed_value",
        "justification",
        "risk_level",
        "evidence",
        "verification_method",
        "reversible",
        "requires_approval",
        "expected_file_hash",
        "depends_on",
    }
)
_CHANGE_OPERATION_DEFINITION_FIELDS: frozenset[str] = frozenset(
    _CHANGE_OPERATION_ALLOWED_FIELDS - {"status"}
)

_CHANGE_PLAN_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "plan_id",
        "execution_id",
        "user_goal",
        "application_name",
        "application_version",
        "hardware_context",
        "operations",
        "provider",
        "created_at",
        "policy_version",
        "status",
    }
)


class PolicyValidationError(ValueError):
    """Error lanzado cuando un `ChangePlan` o `ChangeOperation` incumple las políticas locales."""


class ApprovalBindingError(PermissionError):
    """Error lanzado cuando el hash de aprobación no coincide, expiró o carece de autorización."""


class DuplicateExecutionError(PolicyValidationError, ApprovalBindingError):
    """
    Error lanzado cuando se intenta volver a ejecutar accidentalmente una operación ya aplicada
    (`APPLIED`) o un plan previamente completado. Hereda de `PolicyValidationError` y
    `ApprovalBindingError` para mantener compatibilidad con ambos contratos de validación.
    """


@dataclass
class ChangeOperation:
    """
    Representa una operación individual, auditable y reversible sobre un archivo autorizado.
    Mantiene su estado individual (`PENDING`, `APPLIED`, `ROLLBACK_FAILED`, `ROLLED_BACK`,
    `FAILED`, `BLOCKED`)
    y bloquea modificaciones silenciosas de su definición tras la confirmación humana.
    """

    id: str
    file_path: str
    format: str
    operation_type: str
    section: Optional[str]
    key: Optional[str]
    expected_current_value: Optional[str]
    proposed_value: str
    justification: str
    risk_level: str
    evidence: str
    verification_method: str
    reversible: bool = True
    requires_approval: bool = True
    expected_file_hash: Optional[str] = None
    depends_on: list[str] | tuple[str, ...] = field(default_factory=list)
    status: str = field(default=OperationStatus.PENDING.value, compare=False)
    status_history: list[tuple[str, str, str, str]] = field(
        default_factory=list, repr=False, compare=False
    )
    executed_file_hash: Optional[str] = field(default=None, repr=False, compare=False)
    backup_path: Optional[str] = field(default=None, repr=False, compare=False)
    error_detail: Optional[str] = field(default=None, repr=False, compare=False)
    _execution_evidence_validated: bool = field(default=False, init=False, repr=False, compare=False)
    _definition_frozen: bool = field(default=False, init=False, repr=False, compare=False)

    def __setattr__(self, name: str, value: Any) -> None:
        """
        Controla la asignación del atributo `status` para impedir que un estado terminal
        (`APPLIED`, `FAILED`, `BLOCKED`) sea sobrescrito silenciosamente.
        """
        if (
            name in _CHANGE_OPERATION_DEFINITION_FIELDS
            and self.__dict__.get("_definition_frozen", False)
        ):
            raise PolicyValidationError(
                f"La definición de la operación '{getattr(self, 'id', '<op>')}' está congelada por aprobación humana."
            )
        if name == "_definition_frozen" and self.__dict__.get("_definition_frozen", False):
            raise PolicyValidationError("La definición aprobada no puede descongelarse.")
        if name == "status" and "status" in self.__dict__:
            current_raw = self.__dict__["status"]
            try:
                current_enum = OperationStatus(current_raw)
            except ValueError:
                current_enum = OperationStatus.PENDING

            try:
                target_enum = (
                    value
                    if isinstance(value, OperationStatus)
                    else OperationStatus(str(value).strip().upper())
                )
            except ValueError as exc:
                raise PolicyValidationError(
                    f"Estado de operación inválido '{value}' para '{getattr(self, 'id', '<op>')}'. "
                    f"Estados válidos: {[s.value for s in OperationStatus]}."
                ) from exc

            if target_enum != current_enum:
                raise PolicyValidationError(
                    f"La asignación directa de estado está prohibida en '{getattr(self, 'id', '<op>')}'; use transition_status()."
                )
            value = target_enum.value
        elif name == "status":
            try:
                target_enum = (
                    value
                    if isinstance(value, OperationStatus)
                    else OperationStatus(str(value).strip().upper())
                )
                value = target_enum.value
            except ValueError as exc:
                raise PolicyValidationError(
                    f"Estado inicial inválido '{value}' en ChangeOperation."
                ) from exc

        object.__setattr__(self, name, value)

    def freeze_definition(self) -> None:
        """Congela los campos que definen la operación tras la confirmación humana."""
        if not self._definition_frozen:
            object.__setattr__(self, "depends_on", tuple(self.depends_on))
            object.__setattr__(self, "_definition_frozen", True)

    def transition_status(
        self,
        new_status: OperationStatus | str,
        *,
        reason: str = "",
        evidence_hash: Optional[str] = None,
        _evidence_authority: object | None = None,
    ) -> OperationStatus:
        """
        Realiza una transición controlada de estado para esta operación individual
        y la registra en `self.status_history`.
        """
        current_enum = OperationStatus(self.status)
        target_enum = (
            new_status
            if isinstance(new_status, OperationStatus)
            else OperationStatus(str(new_status).strip().upper())
        )
        allowed = ALLOWED_OPERATION_STATUS_TRANSITIONS.get(current_enum, set())
        if target_enum not in allowed:
            raise PolicyValidationError(
                f"Transición de estado inválida en la operación '{self.id}': "
                f"{current_enum.value} -> {target_enum.value}."
            )
        if target_enum == OperationStatus.APPLIED:
            if (_evidence_authority is not _APPLICATION_EVIDENCE_AUTHORITY
                    or not evidence_hash or not re.fullmatch(r"[0-9a-fA-F]{64}", evidence_hash)):
                raise PolicyValidationError("APPLIED requiere evidencia SHA-256 posterior a la escritura.")
            object.__setattr__(self, "executed_file_hash", evidence_hash.lower())
            object.__setattr__(self, "_execution_evidence_validated", True)

        now_iso = datetime.now(timezone.utc).isoformat()
        object.__setattr__(self, "status", target_enum.value)
        if reason and target_enum in (OperationStatus.FAILED, OperationStatus.BLOCKED):
            object.__setattr__(self, "error_detail", reason)
        self.status_history.append(
            (current_enum.value, target_enum.value, now_iso, reason)
        )
        return target_enum

    def to_dict(self, *, include_volatile_status: bool = False) -> dict[str, Any]:
        """
        Serializa la operación en un diccionario determinista.
        Por defecto excluye el estado volátil de ejecución para que `approval_hash()`
        dependa exclusivamente de la definición inmutable de la operación.
        """
        payload: dict[str, Any] = {
            "id": self.id,
            "file_path": self.file_path,
            "format": self.format,
            "operation_type": self.operation_type,
            "section": self.section,
            "key": self.key,
            "expected_current_value": self.expected_current_value,
            "proposed_value": self.proposed_value,
            "justification": self.justification,
            "risk_level": self.risk_level,
            "evidence": self.evidence,
            "verification_method": self.verification_method,
            "reversible": self.reversible,
            "requires_approval": self.requires_approval,
            "expected_file_hash": self.expected_file_hash,
            "depends_on": list(self.depends_on),
        }
        if include_volatile_status:
            payload["status"] = self.status
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChangeOperation":
        """
        Construye un `ChangeOperation` validando que no existan campos desconocidos ni tipos inválidos.
        """
        if not isinstance(data, Mapping):
            raise PolicyValidationError("Cada operación del plan debe ser un objeto JSON (dict).")

        unknown_fields = set(data.keys()) - _CHANGE_OPERATION_ALLOWED_FIELDS
        if unknown_fields:
            raise PolicyValidationError(
                f"Propiedades desconocidas no permitidas en ChangeOperation: {sorted(unknown_fields)}"
            )

        required_str_fields = (
            "id",
            "file_path",
            "format",
            "operation_type",
            "proposed_value",
            "justification",
            "risk_level",
            "evidence",
            "verification_method",
        )
        for req in required_str_fields:
            if req not in data or not isinstance(data[req], str) or not data[req].strip():
                raise PolicyValidationError(
                    f"El campo obligatorio '{req}' en ChangeOperation debe ser un string no vacío."
                )

        for opt_str in ("section", "key", "expected_current_value", "expected_file_hash"):
            val = data.get(opt_str)
            if val is not None and not isinstance(val, str):
                raise PolicyValidationError(
                    f"El campo '{opt_str}' debe ser string o None, recibido: {type(val).__name__}."
                )

        reversible = data.get("reversible", True)
        requires_approval = data.get("requires_approval", True)
        if not isinstance(reversible, bool) or not isinstance(requires_approval, bool):
            raise PolicyValidationError(
                "Los campos 'reversible' y 'requires_approval' deben ser booleanos."
            )

        depends_on_raw = data.get("depends_on", [])
        if depends_on_raw is None:
            depends_on: list[str] = []
        elif isinstance(depends_on_raw, (list, tuple)) and all(
            isinstance(item, str) and item.strip() for item in depends_on_raw
        ):
            depends_on = [item.strip() for item in depends_on_raw]
        else:
            raise PolicyValidationError(
                "El campo 'depends_on' debe ser una lista de identificadores string no vacíos."
            )

        return cls(
            id=data["id"].strip(),
            file_path=data["file_path"].strip(),
            format=data["format"].strip().lower().lstrip("."),
            operation_type=data["operation_type"].strip(),
            section=data.get("section"),
            key=data.get("key"),
            expected_current_value=data.get("expected_current_value"),
            proposed_value=data["proposed_value"],
            justification=data["justification"].strip(),
            risk_level=data["risk_level"].strip().upper(),
            evidence=data["evidence"].strip().upper(),
            verification_method=data["verification_method"].strip(),
            reversible=reversible,
            requires_approval=requires_approval,
            expected_file_hash=data.get("expected_file_hash"),
            depends_on=depends_on,
        )


@dataclass
class ChangePlan:
    """
    Plan estructurado y auditable de cambios propuestos para una aplicación o juego.
    El método `approval_hash()` vincula criptográficamente el contenido exacto del plan,
    las rutas objetivo, sus hashes SHA-256 originales, las dependencias y los valores propuestos.
    """

    plan_id: str
    execution_id: str
    user_goal: str
    application_name: str
    application_version: str
    hardware_context: dict[str, Any]
    operations: list[ChangeOperation]
    provider: str
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    policy_version: str = field(
        default_factory=lambda: getattr(settings, "CHAIN_POLICY_VERSION", "2.0.0")
    )
    status: str = ChainState.PLANNING.value

    def is_fully_completed(self) -> bool:
        """
        Indica si la totalidad del plan se ejecutó con éxito.
        Un plan con operaciones pendientes (`PENDING`), fallidas (`FAILED`) o bloqueadas (`BLOCKED`)
        jamás puede declararse completamente exitoso.
        """
        if not self.operations:
            return False
        return all(op.status == OperationStatus.APPLIED.value and op._execution_evidence_validated
                   and op.executed_file_hash is not None
                   and len(op.executed_file_hash) == 64
                   and any(item[1] == OperationStatus.APPLIED.value for item in op.status_history)
                   for op in self.operations)

    def get_operation_states(self) -> dict[str, str]:
        """Devuelve un mapa `operation_id -> status` de todas las operaciones del plan."""
        return {op.id: op.status for op in self.operations}

    def to_dict(self, *, include_volatile_status: bool = True) -> dict[str, Any]:
        """Convierte el plan a diccionario."""
        payload: dict[str, Any] = {
            "plan_id": self.plan_id,
            "execution_id": self.execution_id,
            "user_goal": self.user_goal,
            "application_name": self.application_name,
            "application_version": self.application_version,
            "hardware_context": dict(self.hardware_context),
            "operations": [
                op.to_dict(include_volatile_status=False) for op in self.operations
            ],
            "provider": self.provider,
            "created_at": self.created_at,
            "policy_version": self.policy_version,
        }
        if include_volatile_status:
            payload["status"] = self.status
        return payload

    def canonical_json(self) -> str:
        """
        Genera la representación JSON canónica (claves ordenadas y sin espacios superfluos)
        de todos los campos inmutables del plan y sus operaciones.
        """
        canonical_payload = self.to_dict(include_volatile_status=False)
        return json.dumps(
            canonical_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def approval_hash(self) -> str:
        """
        Calcula el hash SHA-256 de la representación canónica del plan.
        Si cambia cualquier ID, dependencia, ruta, sección, clave, formato, valor esperado,
        hash de archivo o valor propuesto, el `approval_hash()` cambia inmediatamente
        e invalida aprobaciones previas.
        """
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChangePlan":
        """Construye un `ChangePlan` rechazando propiedades desconocidas o tipos inválidos."""
        if not isinstance(data, Mapping):
            raise PolicyValidationError("El ChangePlan debe ser un objeto JSON (dict).")

        unknown_fields = set(data.keys()) - _CHANGE_PLAN_ALLOWED_FIELDS
        if unknown_fields:
            raise PolicyValidationError(
                f"Propiedades desconocidas no permitidas en ChangePlan: {sorted(unknown_fields)}"
            )

        required_str = (
            "plan_id",
            "execution_id",
            "user_goal",
            "application_name",
            "application_version",
            "provider",
        )
        for req in required_str:
            if req not in data or not isinstance(data[req], str) or not data[req].strip():
                raise PolicyValidationError(
                    f"El campo obligatorio '{req}' en ChangePlan debe ser un string no vacío."
                )

        hw_ctx = data.get("hardware_context")
        if not isinstance(hw_ctx, Mapping):
            raise PolicyValidationError(
                "El campo 'hardware_context' debe ser un diccionario válido."
            )

        raw_ops = data.get("operations")
        if not isinstance(raw_ops, Sequence) or isinstance(raw_ops, (str, bytes)):
            raise PolicyValidationError(
                "El campo 'operations' debe ser una lista de operaciones."
            )

        operations = [
            op if isinstance(op, ChangeOperation) else ChangeOperation.from_dict(op)
            for op in raw_ops
        ]

        return cls(
            plan_id=data["plan_id"].strip(),
            execution_id=data["execution_id"].strip(),
            user_goal=data["user_goal"].strip(),
            application_name=data["application_name"].strip(),
            application_version=data["application_version"].strip(),
            hardware_context=dict(hw_ctx),
            operations=operations,
            provider=data["provider"].strip(),
            created_at=str(
                data.get("created_at") or datetime.now(timezone.utc).isoformat()
            ),
            policy_version=str(
                data.get("policy_version")
                or getattr(settings, "CHAIN_POLICY_VERSION", "2.0.0")
            ),
            status=str(data.get("status") or ChainState.PLANNING.value),
        )


class PolicyEngine:
    """
    Motor local de validación de políticas de seguridad para `ChangePlan` y `ChangeOperation`.
    Ningún proveedor externo (LLM cloud, local o CLI) puede desactivar ni omitir estas reglas.
    """

    def __init__(self, file_editor: FileEditorSkill) -> None:
        self.file_editor = file_editor

    def validate_operation(
        self,
        op: ChangeOperation,
        *,
        seen_ids: Optional[set[str]] = None,
        seen_targets: Optional[dict[tuple[str, str, str], str]] = None,
        expected_hash_override: Optional[str] = None,
    ) -> Path:
        """
        Valida una operación individual y devuelve la ruta canónica autorizada del archivo objetivo.
        Lanza `PolicyValidationError` o `PermissionError` ante cualquier violación.
        """
        if not isinstance(op, ChangeOperation):
            raise PolicyValidationError("La operación proporcionada no es una instancia de ChangeOperation.")

        if not op.id or not isinstance(op.id, str) or not op.id.strip():
            raise PolicyValidationError("Toda operación debe poseer un identificador 'id' válido.")

        if seen_ids is not None:
            if op.id in seen_ids:
                raise PolicyValidationError(
                    f"Identificador de operación duplicado en el plan: '{op.id}'."
                )
            seen_ids.add(op.id)

        # Validar que el estado de la operación sea un OperationStatus reconocido
        try:
            OperationStatus(op.status)
        except ValueError as exc:
            raise PolicyValidationError(
                f"Estado '{op.status}' inválido en la operación '{op.id}'."
            ) from exc

        fmt = (op.format or "").strip().lower().lstrip(".")
        if fmt not in VALID_FORMATS:
            raise PolicyValidationError(
                f"Formato '{op.format}' no permitido en operación '{op.id}'. "
                f"Formatos válidos: {sorted(VALID_FORMATS)}."
            )

        if op.operation_type not in VALID_OPERATION_TYPES:
            raise PolicyValidationError(
                f"Tipo de operación '{op.operation_type}' inválido en '{op.id}'. "
                f"Permitidos: {sorted(VALID_OPERATION_TYPES)}."
            )

        if op.risk_level not in VALID_RISK_LEVELS:
            raise PolicyValidationError(
                f"Nivel de riesgo '{op.risk_level}' inválido en '{op.id}'. "
                f"Permitidos: {sorted(VALID_RISK_LEVELS)}."
            )

        if op.evidence not in VALID_EVIDENCE_LEVELS:
            raise PolicyValidationError(
                f"Nivel de evidencia '{op.evidence}' inválido en '{op.id}'."
            )

        if op.evidence not in APPROVABLE_EVIDENCE_LEVELS:
            raise PolicyValidationError(
                f"La operación '{op.id}' tiene clasificación '{op.evidence}' y la política "
                "prohíbe incluir recomendaciones UNSUPPORTED o REJECTED en un plan ejecutable."
            )

        if not op.verification_method or not op.verification_method.strip():
            raise PolicyValidationError(
                f"La operación '{op.id}' carece de 'verification_method' obligatorio."
            )

        if not op.justification or not op.justification.strip():
            raise PolicyValidationError(
                f"La operación '{op.id}' carece de 'justification' técnica."
            )

        if not op.reversible:
            raise PolicyValidationError(
                f"La operación '{op.id}' declara reversible=False; la política exige que todo cambio sea reversible."
            )

        if not op.requires_approval:
            raise PolicyValidationError(
                f"La operación '{op.id}' intenta desactivar 'requires_approval'; "
                "toda escritura requiere aprobación explícita del usuario."
            )

        if "\x00" in op.proposed_value:
            raise PolicyValidationError(
                f"El valor propuesto en '{op.id}' contiene bytes nulos no permitidos."
            )

        # Coherencia entre formato y tipo de operación
        if op.operation_type == "update_key":
            if fmt not in ("ini", "cfg", "conf", "txt"):
                raise PolicyValidationError(
                    f"La operación '{op.id}' usa 'update_key' pero el formato es '{fmt}' (se requiere ini/cfg/conf/txt)."
                )
            if not op.key or not op.key.strip():
                raise PolicyValidationError(
                    f"La operación '{op.id}' ('update_key') requiere un nombre de 'key' no vacío."
                )
        elif op.operation_type == "update_json_key":
            if fmt != "json":
                raise PolicyValidationError(
                    f"La operación '{op.id}' usa 'update_json_key' pero el formato es '{fmt}' (se requiere json)."
                )
            if not op.key or not op.key.strip():
                raise PolicyValidationError(
                    f"La operación '{op.id}' ('update_json_key') requiere 'key' no vacía."
                )
        elif op.operation_type == "update_xml_node":
            if fmt != "xml":
                raise PolicyValidationError(
                    f"La operación '{op.id}' usa 'update_xml_node' pero el formato es '{fmt}' (se requiere xml)."
                )
            if not op.key or not op.key.strip():
                raise PolicyValidationError(
                    f"La operación '{op.id}' ('update_xml_node') requiere 'key' (ruta del nodo) no vacía."
                )
        elif op.operation_type == "replace_content":
            if op.key is not None and op.key.strip():
                raise PolicyValidationError(
                    f"Operación contradictoria en '{op.id}': 'replace_content' no debe especificar 'key' individual."
                )

        # Validación de ruta autorizada y existencia real del archivo
        canonical_path = self.file_editor.validate_authorized_path(
            op.file_path, must_exist=True
        )
        if op.operation_type in {"update_key", "update_json_key", "update_xml_node", "replace_content"}:
            if not op.expected_file_hash or not re.fullmatch(r"[0-9a-fA-F]{64}", op.expected_file_hash):
                raise PolicyValidationError(
                    f"La modificación de '{canonical_path.name}' requiere un SHA-256 inicial verificable."
                )
        ext = canonical_path.suffix.lower().lstrip(".")
        if ext != fmt:
            raise PolicyValidationError(
                f"Incoherencia entre la extensión real del archivo ('.{ext}') y el formato declarado ('{fmt}') en '{op.id}'."
            )

        # Verificar que el hash esperado del archivo coincida con el archivo en disco.
        # Si la operación ya fue aplicada previamente en un paso selectivo anterior, su
        # hash en disco debe coincidir con `op.executed_file_hash` (o con el hash de la
        # última operación aplicada sobre el mismo archivo).
        actual_hash = compute_sha256_file(canonical_path)
        effective_expected_hash = (
            expected_hash_override
            if expected_hash_override is not None
            else (
                op.executed_file_hash
                if (op.status == OperationStatus.APPLIED.value and op.executed_file_hash)
                else op.expected_file_hash
            )
        )
        if effective_expected_hash and effective_expected_hash.lower() != actual_hash.lower():
            raise PolicyValidationError(
                f"Conflicto de hash en '{canonical_path.name}' para la operación '{op.id}': "
                f"se esperaba '{effective_expected_hash[:12]}...' pero el archivo actual tiene '{actual_hash[:12]}...'."
            )

        # Detección de operaciones contradictorias sobre la misma clave en el mismo archivo.
        # Si una operación declara `depends_on` y su `expected_current_value` coincide con el
        # valor propuesto por el paso previo, se trata de un eslabón secuencial válido (H0 -> H1 -> H2 -> H3).
        if seen_targets is not None:
            target_sig = (
                str(canonical_path).lower(),
                (op.section or "").strip(),
                (op.key or "__FULL_CONTENT__").strip(),
            )
            is_sequential_chain_step = (
                bool(op.depends_on)
                and target_sig in seen_targets
                and op.expected_current_value is not None
                and str(op.expected_current_value) == str(seen_targets[target_sig])
            )
            if (
                target_sig in seen_targets
                and seen_targets[target_sig] != op.proposed_value
                and not is_sequential_chain_step
            ):
                raise PolicyValidationError(
                    f"Operaciones contradictorias detectadas en el plan para {canonical_path.name} "
                    f"[{op.section or 'GLOBAL'}] {op.key or '<contenido>'}: "
                    f"'{seen_targets[target_sig]}' vs '{op.proposed_value}'."
                )
            seen_targets[target_sig] = op.proposed_value

        return canonical_path

    @staticmethod
    def validate_dependency_graph(
        operations: Sequence[ChangeOperation],
    ) -> list[ChangeOperation]:
        """
        Valida íntegramente el grafo de dependencias de una lista de `ChangeOperation` y
        devuelve su orden topológico determinista:
        1. Verifica IDs únicos y no vacíos.
        2. Verifica que `depends_on` contenga strings válidos sin duplicados ni autorreferencias (`A -> A`).
        3. Verifica que todas las dependencias declaradas existan en el plan.
        4. Ejecuta el algoritmo de Kahn con cola de prioridad (`heapq`) indexada por la posición
           original de cada operación para obtener un orden topológico 100% determinista que
           respeta el orden original entre operaciones independientes.
        6. Verifica la compatibilidad de estados entre cada operación y sus dependencias.
        """
        if not operations:
            raise PolicyValidationError(
                "El plan no contiene ninguna operación ejecutable."
            )
        if len(operations) > MAX_OPERATIONS_PER_PLAN:
            raise PolicyValidationError(
                f"El plan contiene {len(operations)} operaciones; el límite es {MAX_OPERATIONS_PER_PLAN}."
            )

        op_by_id: dict[str, ChangeOperation] = {}
        original_index: dict[str, int] = {}

        for idx, op in enumerate(operations):
            if not isinstance(op, ChangeOperation):
                raise PolicyValidationError(
                    "Cada elemento de 'operations' debe ser una instancia de ChangeOperation."
                )
            if not op.id or not isinstance(op.id, str) or not op.id.strip():
                raise PolicyValidationError(
                    "Toda operación debe poseer un identificador 'id' string no vacío."
                )
            clean_id = op.id.strip()
            if clean_id in op_by_id:
                raise PolicyValidationError(
                    f"Identificador de operación duplicado en el plan: '{clean_id}'."
                )
            op_by_id[clean_id] = op
            original_index[clean_id] = idx

        # Construcción de listas de adyacencia (prerrequisito -> dependientes) y validación de aristas
        dependents_map: dict[str, list[str]] = {op_id: [] for op_id in op_by_id}
        in_degree: dict[str, int] = {op_id: 0 for op_id in op_by_id}

        for op in operations:
            if not isinstance(op.depends_on, (list, tuple)):
                raise PolicyValidationError(
                    f"El campo 'depends_on' en '{op.id}' debe ser una lista de IDs."
                )
            seen_deps_in_op: set[str] = set()
            for raw_dep in op.depends_on:
                if not isinstance(raw_dep, str) or not raw_dep.strip():
                    raise PolicyValidationError(
                        f"La operación '{op.id}' contiene un ID de dependencia vacío o inválido."
                    )
                dep_id = raw_dep.strip()
                if dep_id == op.id:
                    raise PolicyValidationError(
                        f"Dependencia autorreferencial inválida en la operación '{op.id}': "
                        f"una operación no puede depender de sí misma ('{dep_id}')."
                    )
                if dep_id not in op_by_id:
                    raise PolicyValidationError(
                        f"La operación '{op.id}' declara una dependencia inexistente en el plan: '{dep_id}'."
                    )
                if dep_id in seen_deps_in_op:
                    raise PolicyValidationError(
                        f"La operación '{op.id}' declara la dependencia '{dep_id}' de forma duplicada."
                    )
                seen_deps_in_op.add(dep_id)
                dependents_map[dep_id].append(op.id)
                in_degree[op.id] += 1

        # Ordenamiento topológico determinista mediante algoritmo de Kahn + min-heap por índice original
        ready_heap: list[tuple[int, str]] = [
            (original_index[op_id], op_id)
            for op_id, deg in in_degree.items()
            if deg == 0
        ]
        heapq.heapify(ready_heap)

        sorted_ops: list[ChangeOperation] = []
        while ready_heap:
            _, current_id = heapq.heappop(ready_heap)
            sorted_ops.append(op_by_id[current_id])
            for dependent_id in dependents_map[current_id]:
                in_degree[dependent_id] -= 1
                if in_degree[dependent_id] == 0:
                    heapq.heappush(
                        ready_heap, (original_index[dependent_id], dependent_id)
                    )

        if len(sorted_ops) != len(operations):
            unresolved = [op_id for op_id, degree in in_degree.items() if degree > 0]
            raise PolicyValidationError(
                f"El grafo contiene un ciclo y no admite orden topológico; nodos implicados: {unresolved[:20]}."
            )

        # Verificar compatibilidad de estados: una operación APPLIED no puede depender de una operación no APPLIED
        for op in sorted_ops:
            if op.status == OperationStatus.APPLIED.value:
                for dep_id in op.depends_on:
                    dep_op = op_by_id[dep_id.strip()]
                    if dep_op.status != OperationStatus.APPLIED.value:
                        raise PolicyValidationError(
                            f"Inconsistencia de estados en el grafo: la operación '{op.id}' figura como "
                            f"APPLIED pero su dependencia '{dep_op.id}' está en estado '{dep_op.status}'."
                        )

        return sorted_ops

    def validate_plan(self, plan: ChangePlan) -> list[Path]:
        """
        Valida íntegramente un `ChangePlan`, su grafo de dependencias (sin ciclos, IDs duplicados
        ni referencias huérfanas) y todas sus operaciones.
        Retorna la lista de rutas canónicas validadas en el orden del plan.
        """
        if not isinstance(plan, ChangePlan):
            raise PolicyValidationError("El plan debe ser una instancia de ChangePlan.")

        if not plan.plan_id or not plan.execution_id:
            raise PolicyValidationError("El plan debe tener 'plan_id' y 'execution_id' no vacíos.")

        if not plan.user_goal or not plan.application_name:
            raise PolicyValidationError(
                "El plan debe especificar 'user_goal' y 'application_name'."
            )

        if not plan.operations:
            raise PolicyValidationError(
                "El plan no contiene ninguna operación ejecutable."
            )

        seen_ids: set[str] = set()
        seen_targets: dict[tuple[str, str, str], str] = {}
        canonical_by_id: dict[str, Path] = {}

        # 1. Validamos primero cada operación individualmente (unicidad de ID, formato, ruta, evidencia, etc.)
        # Si una operación previa en el mismo archivo ya fue aplicada (`APPLIED`) con hash verificado,
        # el archivo en disco tendrá ese hash actualizado.
        active_file_hashes: dict[str, str] = {}
        for op in plan.operations:
            if (
                op.status == OperationStatus.APPLIED.value
                and op.executed_file_hash
                and op.file_path
            ):
                try:
                    c_path = self.file_editor.validate_authorized_path(
                        op.file_path, must_exist=True
                    )
                    active_file_hashes[str(c_path).lower()] = op.executed_file_hash
                except Exception:
                    pass

        for op in plan.operations:
            override_hash: Optional[str] = None
            try:
                c_preview = self.file_editor.validate_authorized_path(
                    op.file_path, must_exist=True
                )
                file_key = str(c_preview).lower()
                if file_key in active_file_hashes:
                    override_hash = active_file_hashes[file_key]
            except Exception:
                override_hash = None

            canonical_path = self.validate_operation(
                op,
                seen_ids=seen_ids,
                seen_targets=seen_targets,
                expected_hash_override=override_hash,
            )
            canonical_by_id[op.id] = canonical_path

        # 2. Validamos el grafo completo de dependencias (existencia, autorreferencias, ciclos y estados)
        self.validate_dependency_graph(plan.operations)

        return [canonical_by_id[op.id] for op in plan.operations]


@dataclass
class InvestigationRecommendation:
    """Recomendación individual clasificada durante la Fase 3 (Investigación)."""

    section: Optional[str]
    key: str
    proposed_value: str
    justification: str
    risk_level: str
    evidence: str
    source_provider: str
    reason: str = ""


@dataclass
class ChainExecutionRecord:
    """Registro de estado y trazabilidad de una ejecución de `ChainOrchestrator`."""

    execution_id: str
    state: ChainState
    state_history: list[tuple[str, str]] = field(default_factory=list)
    plan: Optional[ChangePlan] = None
    approval_hash: Optional[str] = None
    applied_backups: list[BackupRecord] = field(default_factory=list)
    verification_results: list[dict[str, Any]] = field(default_factory=list)
    recommendations: list[InvestigationRecommendation] = field(default_factory=list)
    error_message: Optional[str] = None
    approved_plan_id: Optional[str] = None
    approved_plan_snapshot: Optional[dict[str, Any]] = None
    bound_operation_ids: Optional[tuple[str, ...]] = None
    applied_operations: list[dict[str, Any]] = field(default_factory=list)
    failed_operations: list[dict[str, Any]] = field(default_factory=list)
    blocked_operations: list[str] = field(default_factory=list)
    operation_states: dict[str, str] = field(default_factory=dict)
    operation_state_history: list[dict[str, Any]] = field(default_factory=list)
    rollback_summary: Optional[dict[str, Any]] = None
    skipped_operations: list[dict[str, Any]] = field(default_factory=list)
    submitted_plan: Optional[ChangePlan] = field(default=None, repr=False)
    operation_runtime_snapshot: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)


class ChainOrchestrator:
    """
    Orquestador transaccional en 10 fases para investigación, planificación, validación,
    simulación, aprobación criptográfica, ejecución controlada, verificación y rollback.
    """

    def __init__(
        self,
        *,
        file_editor: Optional[FileEditorSkill] = None,
        brain_manager: Optional[Any] = None,
        skill_manager: Optional[Any] = None,
        provider_timeout_sec: float = 15.0,
        max_provider_response_chars: int = 16_000,
        transaction_manager: Optional[TransactionManager] = None,
        journal_dir: Optional[Path | str] = None,
    ) -> None:
        self.file_editor: FileEditorSkill = file_editor or FileEditorSkill()
        self.policy_engine: PolicyEngine = PolicyEngine(self.file_editor)
        self.rollback_manager: RollbackManager = RollbackManager(self.file_editor)
        self.brain_manager = brain_manager
        self.skill_manager = skill_manager
        self.provider_timeout_sec = float(provider_timeout_sec)
        self.max_provider_response_chars = int(max_provider_response_chars)

        default_journal_dir: Optional[Path | str] = journal_dir
        if default_journal_dir is None and self.file_editor.backup_dir is not None:
            default_journal_dir = Path(self.file_editor.backup_dir).resolve() / "transactions"
        self.transaction_manager: TransactionManager = transaction_manager or TransactionManager(
            self.file_editor,
            journal_dir=default_journal_dir,
        )

        self._executions: dict[str, ChainExecutionRecord] = {}
        self._investigation_cache: dict[str, list[dict[str, Any]]] = {}
        # Token opaco, de un solo uso y ligado a la aprobación; la garantía es local al proceso.
        self._approval_tokens: dict[str, dict[str, Any]] = {}
        self._approval_lock = threading.Lock()

    def _purge_approval_tokens_locked(self) -> None:
        """Retira tokens consumidos o vencidos; requiere mantener `_approval_lock`."""
        now = time.time()
        expired = [token for token, item in self._approval_tokens.items()
                   if item["used"] or item["expires_at"] < now]
        for token in expired:
            self._approval_tokens.pop(token, None)

    # ------------------------------------------------------------------
    # Gestión de máquina de estados
    # ------------------------------------------------------------------

    def create_execution(self, execution_id: Optional[str] = None) -> ChainExecutionRecord:
        """Crea un nuevo registro de ejecución en estado inicial `RECEIVED`."""
        exec_id = execution_id or f"exec-{uuid.uuid4().hex[:12]}"
        if exec_id in self._executions:
            raise ValueError(f"Ya existe la ejecución '{exec_id}'; no se reemplazará su registro.")
        now_iso = datetime.now(timezone.utc).isoformat()
        record = ChainExecutionRecord(
            execution_id=exec_id,
            state=ChainState.RECEIVED,
            state_history=[(ChainState.RECEIVED.value, now_iso)],
        )
        self._executions[exec_id] = record
        return record

    def recover_pending_transactions(self) -> list[RecoveryReport]:
        """
        Ejecuta la recuperación determinista de todas las transacciones inconclusas en el
        almacén persistente y sincroniza en memoria los registros de ejecución rehidratados.
        """
        reports = self.transaction_manager.recover_pending_transactions()
        for rep in reports:
            if rep.execution_id and rep.execution_id != "unknown":
                try:
                    self.rehydrate_execution(rep.execution_id)
                except Exception:
                    pass
        return reports

    def rehydrate_execution(self, execution_id: str) -> Optional[ChainExecutionRecord]:
        """
        Reconstruye en memoria un `ChainExecutionRecord` verificable a partir del journal
        persistido (`TransactionManager`) tras reiniciar J.A.R.V.I.S. o crear otra instancia
        de `ChainOrchestrator`.

        Reglas estrictas de seguridad (Sección 8 de N5):
        - Si existe una transacción inconclusa para `execution_id`, ejecuta primero
          `recover_transaction` antes de rehidratar.
        - Nunca inventa tokens de autorización, consentimiento humano, backups ni verificaciones.
        - Registra en `self.file_editor.backup_manager` los `BackupRecord` verificados para que
          `/rollback <execution_id>` funcione tras un reinicio.
        """
        with self.transaction_manager.global_lock:
            txs = self.transaction_manager.get_transactions_for_execution(execution_id)
            if not txs:
                return None

            incomplete_states = {
                TransactionState.CREATED.value,
                TransactionState.PREPARING.value,
                TransactionState.PREPARED.value,
                TransactionState.EXECUTING.value,
                TransactionState.ROLLING_BACK.value,
                TransactionState.RECOVERY_REQUIRED.value,
                TransactionState.RECOVERING.value,
            }
            recovered_any = False
            for tx in txs:
                if tx.state in incomplete_states:
                    self.transaction_manager.recover_transaction(
                        tx.transaction_id,
                        reason=f"Recuperación previa a la rehidratación de '{execution_id}'.",
                    )
                    recovered_any = True
            if recovered_any:
                txs = self.transaction_manager.get_transactions_for_execution(execution_id)

            latest_tx = txs[-1]
            plan = ChangePlan.from_dict(latest_tx.approved_plan_snapshot)
            for op in plan.operations:
                op.freeze_definition()

            op_by_id: dict[str, ChangeOperation] = {op.id: op for op in plan.operations}
            record = ChainExecutionRecord(
                execution_id=execution_id,
                state=ChainState.RECEIVED,
                state_history=[(ChainState.RECEIVED.value, latest_tx.created_at)],
                plan=plan,
                submitted_plan=plan,
                approval_hash=latest_tx.approval_hash,
                approved_plan_id=latest_tx.plan_id,
                approved_plan_snapshot=json.loads(plan.canonical_json()),
            )

            seen_backups: set[str] = set()
            applied_ops_by_id: dict[str, dict[str, Any]] = {}
            verifs_by_id: dict[str, dict[str, Any]] = {}

            for tx in txs:
                ordered_entries = sorted(tx.operations.values(), key=lambda e: e.sequence_index)
                for entry in ordered_entries:
                    op = op_by_id.get(entry.operation_id)
                    if op is None:
                        continue
                    eff_status = entry.status
                    if (
                        eff_status == "PENDING"
                        and tx.state == TransactionState.ROLLED_BACK.value
                        and entry.op_stage == OperationJournalStage.ROLLED_BACK.value
                    ):
                        eff_status = OperationStatus.ROLLED_BACK.value

                    object.__setattr__(op, "status", eff_status)
                    object.__setattr__(op, "executed_file_hash", entry.actual_after_sha256)
                    object.__setattr__(op, "backup_path", entry.backup_path)
                    object.__setattr__(op, "error_detail", entry.error_detail)
                    evid_ok = bool(entry.verification_result and entry.verification_result.get("verified"))
                    object.__setattr__(op, "_execution_evidence_validated", evid_ok)
                    op.status_history.append(
                        ("PENDING", eff_status, entry.updated_at, "Rehidratado desde journal transaccional.")
                    )
                    record.operation_states[op.id] = eff_status
                    record.operation_runtime_snapshot[op.id] = {
                        "status": eff_status,
                        "executed_file_hash": entry.actual_after_sha256,
                        "backup_path": entry.backup_path,
                        "error_detail": entry.error_detail,
                        "evidence_validated": evid_ok,
                        "status_history": list(op.status_history),
                    }

                    post_sha = entry.actual_after_sha256 or entry.planned_after_sha256
                    if entry.backup_path and post_sha:
                        bkp_path_str = str(Path(entry.backup_path).resolve())
                        existing_bkp = self.file_editor.backup_manager.get_record_by_backup(bkp_path_str)
                        if existing_bkp is None:
                            fsize = Path(bkp_path_str).stat().st_size if Path(bkp_path_str).exists() else 0
                            existing_bkp = BackupRecord(
                                execution_id=execution_id,
                                target_path=entry.file_path,
                                backup_path=bkp_path_str,
                                original_sha256=entry.expected_before_sha256,
                                backup_sha256=entry.backup_sha256 or entry.expected_before_sha256,
                                size_bytes=fsize,
                                created_at=entry.updated_at,
                                post_edit_sha256=post_sha,
                            )
                            self.file_editor.backup_manager.register_backup(existing_bkp)
                        if bkp_path_str not in seen_backups and entry.actual_after_sha256:
                            seen_backups.add(bkp_path_str)
                            record.applied_backups.append(existing_bkp)

                    if entry.actual_after_sha256 and eff_status in (
                        OperationStatus.APPLIED.value,
                        OperationStatus.ROLLED_BACK.value,
                        OperationStatus.ROLLBACK_FAILED.value,
                    ):
                        applied_ops_by_id[op.id] = {
                            "operation_id": op.id,
                            "file_path": entry.file_path,
                            "backup_path": entry.backup_path,
                            "original_hash": entry.expected_before_sha256,
                            "new_hash": entry.actual_after_sha256,
                            "status": eff_status,
                        }
                    if entry.verification_result:
                        verifs_by_id[op.id] = dict(entry.verification_result)
                    if eff_status == OperationStatus.FAILED.value:
                        record.failed_operations.append(
                            {
                                "operation_id": op.id,
                                "file_path": entry.file_path,
                                "error": entry.error_detail or "",
                                "status": eff_status,
                            }
                        )
                    if eff_status == OperationStatus.BLOCKED.value and op.id not in record.blocked_operations:
                        record.blocked_operations.append(op.id)

            for op in plan.operations:
                record.operation_states.setdefault(op.id, op.status)
                if op.id in applied_ops_by_id:
                    record.applied_operations.append(applied_ops_by_id[op.id])
                if op.id in verifs_by_id:
                    record.verification_results.append(verifs_by_id[op.id])

            if latest_tx.state in (
                TransactionState.COMMITTED.value,
                TransactionState.PARTIALLY_APPLIED.value,
            ):
                record.state = ChainState.COMPLETED
                plan.status = "COMPLETED" if plan.is_fully_completed() else "PARTIALLY_APPLIED"
            elif latest_tx.state == TransactionState.ROLLED_BACK.value:
                record.state = ChainState.ROLLED_BACK
                plan.status = ChainState.ROLLED_BACK.value
            else:
                record.state = ChainState.FAILED
                plan.status = ChainState.FAILED.value
                record.error_message = latest_tx.error_message

            record.state_history.append((record.state.value, latest_tx.updated_at))
            self._executions[execution_id] = record
            return record

    def get_execution(self, execution_id: str) -> ChainExecutionRecord:
        """Obtiene el registro de una ejecución existente o lo rehidrata desde el journal persistente."""
        if execution_id not in self._executions:
            rehydrated = self.rehydrate_execution(execution_id)
            if rehydrated is not None:
                return rehydrated
            raise KeyError(f"No existe la ejecución '{execution_id}'.")
        return self._executions[execution_id]

    def transition_state(
        self,
        execution_id: str,
        new_state: ChainState,
        *,
        reason: str = "",
    ) -> ChainState:
        """
        Realiza una transición controlada de estado verificando `ALLOWED_STATE_TRANSITIONS`.
        Si una ejecución se encuentra en `COMPLETED`, únicamente permite reabrir `VALIDATING` o
        `AWAITING_APPROVAL` cuando el plan tuvo una ejecución parcial selectiva y aún conserva
        operaciones en estado `PENDING`.
        """
        record = self.get_execution(execution_id)
        current = record.state
        if new_state == current:
            return current

        allowed = ALLOWED_STATE_TRANSITIONS.get(current, set())
        if new_state not in allowed:
            raise ValueError(
                f"Transición de estado inválida en '{execution_id}': {current.value} -> {new_state.value}."
            )

        if current == ChainState.COMPLETED and new_state in (
            ChainState.VALIDATING,
            ChainState.AWAITING_APPROVAL,
        ):
            if record.plan is None or record.plan.is_fully_completed():
                raise ValueError(
                    f"El plan en '{execution_id}' ya fue completado íntegramente y su estado terminal "
                    f"'{current.value}' no puede reabrirse a '{new_state.value}'."
                )

        now_iso = datetime.now(timezone.utc).isoformat()
        record.state = new_state
        record.state_history.append((new_state.value, now_iso))
        if record.plan is not None:
            if new_state == ChainState.COMPLETED and not record.plan.is_fully_completed():
                record.plan.status = "PARTIALLY_APPLIED"
            else:
                record.plan.status = new_state.value
            if record.submitted_plan is not None:
                record.submitted_plan.status = record.plan.status
        if reason and new_state in (ChainState.REJECTED, ChainState.FAILED, ChainState.CANCELLED):
            record.error_message = reason

        self.file_editor.audit_logger.record(
            "state_transition",
            execution_id,
            {
                "from_state": current.value,
                "to_state": new_state.value,
                "reason": reason,
            },
        )
        return new_state

    def _record_operation_transition(
        self,
        record: ChainExecutionRecord,
        op: ChangeOperation,
        new_status: OperationStatus,
        *,
        reason: str = "",
        evidence_hash: Optional[str] = None,
        evidence_verified: bool = False,
    ) -> OperationStatus:
        """
        Ejecuta una transición de estado sobre una `ChangeOperation` individual, sincroniza
        el mapa `record.operation_states` y deja rastro auditable en `AuditLogger`.
        """
        prev_status = op.status
        final_status = op.transition_status(
            new_status, reason=reason, evidence_hash=evidence_hash,
            _evidence_authority=(
                _APPLICATION_EVIDENCE_AUTHORITY if evidence_verified else None
            ),
        )
        record.operation_states[op.id] = final_status.value
        runtime = record.operation_runtime_snapshot.setdefault(op.id, {})
        runtime["status"] = final_status.value
        runtime["executed_file_hash"] = op.executed_file_hash
        runtime["backup_path"] = op.backup_path
        runtime["error_detail"] = op.error_detail
        runtime["evidence_validated"] = op._execution_evidence_validated
        runtime["status_history"] = list(op.status_history)
        entry = {
            "operation_id": op.id,
            "from_status": prev_status,
            "to_status": final_status.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
        }
        record.operation_state_history.append(entry)
        # El plan entregado al caller recibe solo el estado resultante; las escrituras
        # siempre se ejecutan desde la reconstrucción aprobada independiente.
        if record.submitted_plan is not None:
            submitted_op = next(
                (item for item in record.submitted_plan.operations if item.id == op.id), None
            )
            if submitted_op is not None and submitted_op.status == prev_status:
                object.__setattr__(submitted_op, "status", final_status.value)
                if evidence_verified:
                    object.__setattr__(submitted_op, "executed_file_hash", evidence_hash)
                    object.__setattr__(submitted_op, "_execution_evidence_validated", True)
                submitted_op.status_history.append(
                    (prev_status, final_status.value, entry["timestamp"], reason)
                )
        self.file_editor.audit_logger.record(
            "operation_state_transition",
            record.execution_id,
            entry,
        )
        return final_status

    def topological_sort_operations(
        self,
        operations: Sequence[ChangeOperation],
    ) -> list[ChangeOperation]:
        """
        Devuelve las operaciones ordenadas topológicamente de manera determinista
        utilizando el validador de grafo del `PolicyEngine`.
        """
        return self.policy_engine.validate_dependency_graph(operations)

    def _propagate_blocked_status(
        self,
        failed_op_id: str,
        operations: Sequence[ChangeOperation],
        *,
        record: ChainExecutionRecord,
        reason: str,
    ) -> list[str]:
        """
        Propaga de manera transitiva el estado `BLOCKED` a todas las operaciones pendientes
        que dependan directa o indirectamente de `failed_op_id` (por ejemplo: si A falla y
        `A -> B -> C`, tanto B como C pasan inmediatamente a `BLOCKED` y nunca llegan a
        `FileEditorSkill`).
        """
        dependents_map: dict[str, list[ChangeOperation]] = {op.id: [] for op in operations}
        for op in operations:
            for dep_id in op.depends_on:
                dep_clean = dep_id.strip()
                if dep_clean in dependents_map:
                    dependents_map[dep_clean].append(op)

        newly_blocked: list[str] = []
        queue: list[str] = [failed_op_id]
        visited: set[str] = {failed_op_id}

        while queue:
            curr_id = queue.pop(0)
            for child_op in dependents_map.get(curr_id, ()):
                if child_op.id not in visited:
                    visited.add(child_op.id)
                    if child_op.status == OperationStatus.PENDING.value:
                        block_reason = (
                            f"Bloqueada por fallo en dependencia '{failed_op_id}': {reason}"
                        )
                        self._record_operation_transition(
                            record,
                            child_op,
                            OperationStatus.BLOCKED,
                            reason=block_reason,
                        )
                        if child_op.id not in record.blocked_operations:
                            record.blocked_operations.append(child_op.id)
                        newly_blocked.append(child_op.id)
                    queue.append(child_op.id)

        return newly_blocked

    def _verify_single_operation_on_disk(
        self,
        op: ChangeOperation,
        *,
        expected_final_hash: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Verifica en el archivo real en disco que una operación se haya aplicado efectivamente:
        1. Valida que la ruta sea canónica y autorizada.
        2. Calcula el SHA-256 actual del archivo y comprueba `expected_final_hash` si se indica.
        3. Relee y decodifica estrictamente el contenido comprobando que la clave, nodo XML,
           ruta JSON o reemplazo completo coincida exactamente con `op.proposed_value`.
        """
        canonical = self.file_editor.validate_authorized_path(op.file_path, must_exist=True)
        actual_hash = compute_sha256_file(canonical)
        if expected_final_hash and actual_hash.lower() != expected_final_hash.lower():
            raise RuntimeError(
                f"El hash post-escritura de '{canonical.name}' ({actual_hash[:12]}...) "
                f"no coincide con el esperado ({expected_final_hash[:12]}...)."
            )

        raw_bytes = canonical.read_bytes()
        text, _, _ = _decode_bytes_strict(raw_bytes)

        if op.operation_type == "update_key":
            extracted: dict[tuple[Optional[str], str], str] = {}
            self._extract_ini_keys(text, extracted)
            if extracted.get((op.section, op.key or "")) != str(op.proposed_value):
                raise RuntimeError(
                    f"Verificación fallida en '{canonical.name}': [{op.section}] {op.key} "
                    f"no tiene el valor esperado '{op.proposed_value}'."
                )
        elif op.operation_type == "update_json_key":
            data = json.loads(text)
            parts = [p for p in (op.key or "").split(".") if p]
            curr: Any = data
            for p in parts:
                curr = curr[p]
            try:
                expected_val = json.loads(op.proposed_value)
            except Exception:
                expected_val = op.proposed_value
            if curr != expected_val:
                raise RuntimeError(
                    f"Verificación JSON fallida en '{canonical.name}' para '{op.key}'."
                )
        elif op.operation_type == "update_xml_node":
            if _XML_XXE_RE.search(text):
                raise RuntimeError("XXE detectado en verificación post-escritura.")
            root = ET.fromstring(text)
            target = root if op.key in (".", root.tag) else root.find(op.key or "")
            if target is None or (target.text or "") != str(op.proposed_value):
                raise RuntimeError(
                    f"Verificación XML fallida en '{canonical.name}' para '{op.key}'."
                )
        elif op.operation_type == "replace_content":
            if text != op.proposed_value:
                raise RuntimeError(
                    f"Verificación de contenido completo fallida en '{canonical.name}'."
                )

        return {
            "operation_id": op.id,
            "file_path": str(canonical),
            "verified": True,
            "sha256": actual_hash,
        }

    def _verify_completed_dependency_evidence(
        self,
        dep_op: ChangeOperation,
        *,
        record: Optional[ChainExecutionRecord],
    ) -> None:
        """
        Verifica que una dependencia que declara estado `APPLIED` posea evidencia real y
        verificable tanto en el registro de ejecución como en el archivo físico en disco.
        Impide una falsa garantía de idempotencia cuando alguien fuerza `status='APPLIED'`
        sin que la operación haya sido realmente ejecutada y verificada.
        """
        if dep_op.status != OperationStatus.APPLIED.value:
            raise PolicyValidationError(
                f"La dependencia '{dep_op.id}' no está en estado APPLIED (estado actual: '{dep_op.status}')."
            )

        if not dep_op.executed_file_hash or not isinstance(dep_op.executed_file_hash, str):
            raise PolicyValidationError(
                f"La dependencia '{dep_op.id}' figura como APPLIED pero carece de 'executed_file_hash' verificable."
            )

        if record is not None:
            has_applied_entry = any(
                item.get("operation_id") == dep_op.id
                and item.get("new_hash") == dep_op.executed_file_hash
                for item in record.applied_operations
            )
            has_verified_entry = any(
                item.get("operation_id") == dep_op.id
                and item.get("verified") is True
                and item.get("sha256") == dep_op.executed_file_hash
                for item in record.verification_results
            )
            has_backup_entry = any(
                (
                    self.file_editor.backup_manager.get_record_by_backup(b.backup_path)
                    or b
                ).post_edit_sha256
                == dep_op.executed_file_hash
                for b in record.applied_backups
            )
            if not (has_applied_entry and has_verified_entry and has_backup_entry):
                raise PolicyValidationError(
                    f"La dependencia '{dep_op.id}' figura como APPLIED pero no posee evidencia "
                    "de ejecución ni respaldo verificado en el registro del orquestador."
                )

        try:
            # Un paso posterior compatible puede cambiar el hash global del archivo.
            canonical = self.file_editor.validate_authorized_path(dep_op.file_path, must_exist=True)
            actual_hash = compute_sha256_file(canonical)
            if record is not None:
                same_file = [item for item in record.applied_operations
                             if item.get("file_path") and Path(str(item["file_path"])).resolve() == canonical]
                if same_file and same_file[-1].get("new_hash") != actual_hash:
                    raise RuntimeError("El hash actual rompe la cadena de cambios verificados.")
            self._verify_single_operation_on_disk(dep_op)
        except Exception as exc:
            raise PolicyValidationError(
                f"La dependencia '{dep_op.id}' figura como APPLIED pero el estado real del archivo "
                f"en disco no coincide con la evidencia esperada: {exc}"
            ) from exc

    def preflight_validate(
        self,
        plan: ChangePlan,
        *,
        approved_operation_ids: Optional[Sequence[str]] = None,
        record: Optional[ChainExecutionRecord] = None,
    ) -> list[ChangeOperation]:
        """
        Mecanismo centralizado de validación pre-flight antes de crear backups o tocar disco:
        1. Valida todas las políticas del plan y el grafo de dependencias completo (unicidad de IDs,
           existencia de dependencias, ausencia de autorreferencias y ausencia de ciclos).
        2. Si se especificó `approved_operation_ids`:
           - Rechaza selecciones vacías (`[]` o strings en blanco).
           - Rechaza IDs duplicados en la selección.
           - Rechaza IDs inexistentes (incluyendo selecciones mixtas con IDs válidos e inválidos).
        3. Verifica que ninguna operación seleccionada haya sido ya aplicada (`APPLIED`), ni esté
           en estado `FAILED` o `BLOCKED`.
        4. Verifica todas las dependencias directas y transitivas de las operaciones seleccionadas:
           - Si una dependencia forma parte de la selección, debe estar en estado `PENDING`.
           - Si una dependencia NO forma parte de la selección, solo se permite si ya fue
             completada (`APPLIED`) y su evidencia de ejecución en registro y en disco es válida.
           - Si cualquier dependencia está `PENDING` (fuera de la selección), `FAILED` o `BLOCKED`,
             rechaza toda la ejecución antes de modificar cualquier archivo.
        5. Devuelve la lista de operaciones seleccionadas en orden topológico determinista.
        """
        # 1. Validación integral del plan y del grafo de dependencias
        self.policy_engine.validate_plan(plan)
        full_topological_ops = self.policy_engine.validate_dependency_graph(plan.operations)
        op_by_id: dict[str, ChangeOperation] = {op.id: op for op in plan.operations}

        # 2. Validación de la selección de IDs
        if approved_operation_ids is None:
            selected_id_set: set[str] = {op.id for op in plan.operations}
        else:
            if isinstance(approved_operation_ids, (str, bytes)) or not isinstance(
                approved_operation_ids, Sequence
            ):
                raise PolicyValidationError(
                    "El parámetro 'approved_operation_ids' debe ser una secuencia (lista o tupla) de IDs."
                )

            raw_items = list(approved_operation_ids)
            if len(raw_items) == 0:
                raise PolicyValidationError(
                    "Selección vacía: se solicitó ejecutar operaciones pero no se proporcionó ningún ID. "
                    "Para cancelar explícitamente el plan utilice cancel_plan()."
                )

            cleaned_ids: list[str] = []
            for item in raw_items:
                if not isinstance(item, str) or not item.strip():
                    raise PolicyValidationError(
                        "Selección inválida: contiene un identificador de operación vacío o no válido."
                    )
                cleaned_ids.append(item.strip())

            # Detectar IDs duplicados en la selección proporcionada
            seen_sel: set[str] = set()
            duplicate_sel: list[str] = []
            for cid in cleaned_ids:
                if cid in seen_sel and cid not in duplicate_sel:
                    duplicate_sel.append(cid)
                seen_sel.add(cid)
            if duplicate_sel:
                raise PolicyValidationError(
                    f"Selección inválida: se detectaron IDs de operación duplicados en la selección: {duplicate_sel}."
                )

            # Detectar IDs desconocidos (incluyendo selecciones mixtas válidas + inválidas)
            unknown_ids = [cid for cid in cleaned_ids if cid not in op_by_id]
            if unknown_ids:
                raise PolicyValidationError(
                    f"Selección inválida: los siguientes IDs de operación no existen en el plan: {unknown_ids}."
                )

            selected_id_set = set(cleaned_ids)

        if not selected_id_set:
            raise PolicyValidationError(
                "Selección vacía: no hay operaciones seleccionadas para ejecutar."
            )

        # 3. Validar el estado de cada operación seleccionada (prevenir reejecución accidental)
        for sel_id in selected_id_set:
            sel_op = op_by_id[sel_id]
            if sel_op.status == OperationStatus.APPLIED.value:
                # Comprobamos además si el estado en disco coincide con APPLIED
                self._verify_completed_dependency_evidence(sel_op, record=record)
                raise DuplicateExecutionError(
                    f"La operación '{sel_id}' ya fue aplicada y verificada previamente (estado APPLIED); "
                    "se rechaza su reejecución accidental."
                )
            if sel_op.status == OperationStatus.FAILED.value:
                raise PolicyValidationError(
                    f"La operación seleccionada '{sel_id}' se encuentra en estado terminal FAILED y no puede ejecutarse."
                )
            if sel_op.status == OperationStatus.BLOCKED.value:
                raise PolicyValidationError(
                    f"La operación seleccionada '{sel_id}' se encuentra en estado BLOCKED y no puede ejecutarse."
                )
            if sel_op.status != OperationStatus.PENDING.value:
                raise PolicyValidationError(
                    f"La operación seleccionada '{sel_id}' tiene un estado incompatible para ejecución: '{sel_op.status}'."
                )

        # 4. Validar dependencias directas y transitivas de cada operación seleccionada
        for op in full_topological_ops:
            if op.id not in selected_id_set:
                continue

            visited_deps: set[str] = set()
            dep_queue: list[str] = [d.strip() for d in op.depends_on]
            while dep_queue:
                dep_id = dep_queue.pop(0)
                if dep_id in visited_deps:
                    continue
                visited_deps.add(dep_id)
                dep_op = op_by_id[dep_id]

                if dep_id in selected_id_set:
                    if dep_op.status != OperationStatus.PENDING.value:
                        raise PolicyValidationError(
                            f"La dependencia seleccionada '{dep_id}' requerida por '{op.id}' "
                            f"no está en estado PENDING (estado actual: '{dep_op.status}')."
                        )
                else:
                    # La dependencia no forma parte de la selección actual:
                    # solo es válida si fue ejecutada previamente (APPLIED) con evidencia verificable.
                    if dep_op.status == OperationStatus.PENDING.value:
                        raise PolicyValidationError(
                            f"Dependencia faltante en aprobación selectiva: la operación '{op.id}' "
                            f"depende de '{dep_id}' (estado PENDING), que no fue incluida en la selección."
                        )
                    if dep_op.status in (
                        OperationStatus.FAILED.value,
                        OperationStatus.BLOCKED.value,
                    ):
                        raise PolicyValidationError(
                            f"Dependencia inválida para '{op.id}': la operación requerida '{dep_id}' "
                            f"se encuentra en estado '{dep_op.status}'."
                        )
                    self._verify_completed_dependency_evidence(dep_op, record=record)

                for transitive_dep in dep_op.depends_on:
                    t_clean = transitive_dep.strip()
                    if t_clean not in visited_deps:
                        dep_queue.append(t_clean)

        # 5. Filtrar el orden topológico determinista conservando únicamente las operaciones seleccionadas
        ordered_selected = [
            op for op in full_topological_ops if op.id in selected_id_set
        ]
        return ordered_selected

    # ------------------------------------------------------------------
    # Fase 1 y Fase 2: Interpretación y diagnóstico local en solo lectura
    # ------------------------------------------------------------------

    def detect_hardware_context(
        self,
        user_goal: str = "",
        override_context: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        Fase 2: Obtiene el contexto de hardware real del sistema local (en solo lectura)
        combinando `psutil` / `SystemInfoSkill` con cualquier restricción mencionada por el usuario
        (ej. 'GTX 1660 de 6 GB') sin forzar la GTX 1660 cuando el hardware o el objetivo difieren.
        """
        if override_context is not None:
            return dict(override_context)

        hw: dict[str, Any] = {
            "platform": "Windows",
            "cpu_detection": "detected",
        }
        try:
            import psutil

            hw["cpu_cores_logical"] = psutil.cpu_count(logical=True)
            vm = psutil.virtual_memory()
            hw["ram_total_gb"] = round(vm.total / (1024**3), 1)
            hw["ram_available_gb"] = round(vm.available / (1024**3), 1)
        except Exception:
            hw["ram_total_gb"] = 16.0

        if self.skill_manager is not None and hasattr(self.skill_manager, "skills"):
            sys_skill = self.skill_manager.skills.get("system_info")
            if sys_skill is not None:
                try:
                    hw["system_info_summary"] = sys_skill.execute(metric="todo")
                except Exception:
                    pass

        goal_lower = (user_goal or "").lower()
        vram_match = re.search(r"(\d+)\s*gb", goal_lower)
        if "1660" in goal_lower:
            hw["gpu_model"] = "NVIDIA GeForce GTX 1660"
            hw["vram_gb"] = int(vram_match.group(1)) if vram_match else 6
        elif vram_match:
            hw["vram_gb"] = int(vram_match.group(1))

        return hw

    def inspect_target_config(self, file_path: str | Path) -> dict[str, Any]:
        """
        Fase 2: Lee en modo de solo lectura un archivo de configuración autorizado,
        calcula su SHA-256 y extrae sus claves actuales sin alterar el archivo.
        """
        canonical = self.file_editor.validate_authorized_path(file_path, must_exist=True)
        file_hash = compute_sha256_file(canonical)
        ext = canonical.suffix.lower().lstrip(".")
        raw_bytes = canonical.read_bytes()
        text, encoding, _ = _decode_bytes_strict(raw_bytes)

        parsed_keys: dict[tuple[Optional[str], str], str] = {}
        if ext in ("ini", "cfg", "conf", "txt"):
            self._extract_ini_keys(text, parsed_keys)
        elif ext == "json":
            data = json.loads(text)
            if isinstance(data, dict):
                self._flatten_json_keys(data, "", parsed_keys)

        return {
            "canonical_path": str(canonical),
            "format": ext,
            "encoding": encoding,
            "sha256": file_hash,
            "keys": parsed_keys,
            "raw_text": text,
        }

    @staticmethod
    def _extract_ini_keys(
        text: str,
        out: dict[tuple[Optional[str], str], str],
    ) -> None:
        """Extrae pares `(section, key) -> value` preservando detección de duplicados."""
        current_sec: Optional[str] = None
        kv_re = re.compile(
            r"^(?P<indent>\s*)(?P<key>[^\[;#=:\s][^;#=:\r\n]*?)(?P<sep>\s*[=:]\s*)(?P<rest>.*)$"
        )
        for raw_line in text.splitlines(keepends=True):
            body, _ = _split_line_ending(raw_line)
            stripped = body.strip()
            if not stripped or stripped.startswith((";", "#")):
                continue
            sec_m = _INI_SECTION_RE.match(body)
            if sec_m:
                current_sec = sec_m.group("section").strip()
                continue
            kv_m = kv_re.match(body)
            if kv_m:
                k = kv_m.group("key").strip()
                rest = kv_m.group("rest")
                val_part = rest
                for idx, ch in enumerate(rest):
                    if ch in (";", "#") and (idx == 0 or rest[idx - 1].isspace()):
                        val_part = rest[:idx]
                        break
                pair = (current_sec, k)
                if pair not in out:
                    out[pair] = val_part.strip()
                else:
                    out[pair] = "__AMBIGUOUS_DUPLICATE__"

    def _flatten_json_keys(
        self,
        node: dict[str, Any],
        prefix: str,
        out: dict[tuple[Optional[str], str], str],
    ) -> None:
        for k, v in node.items():
            dotted = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                self._flatten_json_keys(v, dotted, out)
            else:
                out[(None, dotted)] = (
                    json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
                )

    # ------------------------------------------------------------------
    # Fase 3: Investigación y clasificación de recomendaciones
    # ------------------------------------------------------------------

    def investigate_optimizations(
        self,
        *,
        execution_id: str,
        user_goal: str,
        application_name: str,
        config_snapshot: Mapping[str, Any],
        hardware_context: Mapping[str, Any],
        provider_callable: Optional[Callable[[str], str]] = None,
        candidate_recommendations: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> list[InvestigationRecommendation]:
        """
        Fase 3: Consulta un proveedor de IA (o utiliza candidatos proporcionados/heurísticos)
        con límite de tiempo, tamaño máximo y caché para evitar llamadas duplicadas.
        Clasifica cada sugerencia en `VERIFIED`, `PLAUSIBLE`, `UNSUPPORTED` o `REJECTED`.
        """
        record = self.get_execution(execution_id)
        if record.state == ChainState.RECEIVED:
            self.transition_state(execution_id, ChainState.INVESTIGATING)

        cache_key = hashlib.sha256(
            json.dumps(
                {
                    "goal": user_goal.strip(),
                    "app": application_name.strip(),
                    "sha256": config_snapshot.get("sha256", ""),
                    "hw": dict(hardware_context),
                },
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()

        raw_candidates: list[dict[str, Any]] = []
        provider_name = "local_heuristic"

        if candidate_recommendations is not None:
            raw_candidates = [dict(item) for item in candidate_recommendations]
            provider_name = "explicit_candidates"
        elif cache_key in self._investigation_cache:
            raw_candidates = list(self._investigation_cache[cache_key])
            provider_name = "cached_provider"
        elif provider_callable is not None or self.brain_manager is not None:
            raw_candidates, provider_name = self._query_provider_safely(
                user_goal=user_goal,
                application_name=application_name,
                config_snapshot=config_snapshot,
                hardware_context=hardware_context,
                provider_callable=provider_callable,
            )
            if raw_candidates:
                self._investigation_cache[cache_key] = list(raw_candidates)

        if not raw_candidates:
            raw_candidates = self._build_local_heuristic_candidates(
                config_snapshot=config_snapshot,
                hardware_context=hardware_context,
            )

        classified = self._classify_candidates(
            raw_candidates=raw_candidates,
            config_snapshot=config_snapshot,
            provider_name=provider_name,
        )
        record.recommendations = classified
        return classified

    def _query_provider_safely(
        self,
        *,
        user_goal: str,
        application_name: str,
        config_snapshot: Mapping[str, Any],
        hardware_context: Mapping[str, Any],
        provider_callable: Optional[Callable[[str], str]] = None,
    ) -> tuple[list[dict[str, Any]], str]:
        """Invoca al proveedor configurado controlando disponibilidad, tiempo de espera y longitud."""
        prompt = (
            f"Objetivo: {user_goal}\n"
            f"Aplicación: {application_name}\n"
            f"Hardware: {json.dumps(dict(hardware_context), ensure_ascii=False)}\n"
            "Devuelve un JSON con clave 'recommendations' (lista de objetos con "
            "section, key, proposed_value, justification, risk_level, evidence)."
        )
        start_ts = time.monotonic()
        try:
            if provider_callable is not None:
                raw_response = provider_callable(prompt)
                provider_name = getattr(provider_callable, "__name__", "custom_provider")
            elif self.brain_manager is not None:
                raw_response = self.brain_manager.generate_response(prompt)
                provider_name = getattr(self.brain_manager, "active_mode", "brain_manager")
            else:
                return [], "none"
        except TimeoutError as exc:
            raise TimeoutError(
                f"El proveedor de IA superó el tiempo máximo permitido ({self.provider_timeout_sec}s)."
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"El proveedor de IA no está disponible o falló durante la investigación: {exc}"
            ) from exc

        elapsed = time.monotonic() - start_ts
        if elapsed > self.provider_timeout_sec:
            raise TimeoutError(
                f"El proveedor tardó {elapsed:.2f}s, excediendo el límite de {self.provider_timeout_sec}s."
            )

        if not isinstance(raw_response, str) or not raw_response.strip():
            raise RuntimeError("El proveedor devolvió una respuesta vacía o inválida.")

        trimmed = raw_response[: self.max_provider_response_chars].strip()
        try:
            start_idx = trimmed.find("{")
            end_idx = trimmed.rfind("}")
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                parsed = json.loads(trimmed[start_idx : end_idx + 1])
                recs = parsed.get("recommendations", [])
                if isinstance(recs, list):
                    return [dict(r) for r in recs if isinstance(r, Mapping)], str(provider_name)
        except Exception:
            pass

        return [], str(provider_name)

    def _build_local_heuristic_candidates(
        self,
        *,
        config_snapshot: Mapping[str, Any],
        hardware_context: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """
        Genera recomendaciones conservadoras para GPUs de ~6 GB VRAM únicamente
        sobre claves que existen realmente en el archivo inspeccionado.
        """
        existing_keys: dict[tuple[Optional[str], str], str] = config_snapshot.get("keys", {})
        vram_gb = int(hardware_context.get("vram_gb", 6) or 6)
        suggestions: list[dict[str, Any]] = []

        known_vram_keys: dict[str, tuple[str, str, str, str]] = {
            "sg.ShadowQuality": (
                "1" if vram_gb <= 6 else "2",
                "Reducir calidad de sombras disminuye el consumo de VRAM y mejora la estabilidad de frame time.",
                "LOW",
                "VERIFIED",
            ),
            "sg.TextureQuality": (
                "1" if vram_gb <= 6 else "2",
                "Ajustar texturas a nivel medio evita saturar el buffer de 6 GB de VRAM.",
                "LOW",
                "VERIFIED",
            ),
            "sg.EffectsQuality": (
                "1",
                "Disminuir efectos volumétricos reduce picos de carga en sombreadores.",
                "LOW",
                "PLAUSIBLE",
            ),
            "r.Shadow.MaxResolution": (
                "1024",
                "Limitar la resolución máxima de mapas de sombras a 1024 ahorra VRAM en GPUs de 6 GB.",
                "LOW",
                "VERIFIED",
            ),
            "ShadowQuality": (
                "Medium",
                "Ajustar sombras a Medium equilibra calidad visual y uso de VRAM.",
                "LOW",
                "PLAUSIBLE",
            ),
            "TextureQuality": (
                "Medium",
                "Ajustar texturas a Medium previene stuttering por paginación de VRAM.",
                "LOW",
                "VERIFIED",
            ),
        }

        for (section, key), current_val in existing_keys.items():
            if key in known_vram_keys:
                prop_val, justif, risk, evid = known_vram_keys[key]
                if str(current_val) != str(prop_val):
                    suggestions.append(
                        {
                            "section": section,
                            "key": key,
                            "proposed_value": prop_val,
                            "justification": justif,
                            "risk_level": risk,
                            "evidence": evid,
                        }
                    )

        return suggestions

    def _classify_candidates(
        self,
        *,
        raw_candidates: Sequence[Mapping[str, Any]],
        config_snapshot: Mapping[str, Any],
        provider_name: str,
    ) -> list[InvestigationRecommendation]:
        """
        Clasifica cada candidato en `VERIFIED`, `PLAUSIBLE`, `UNSUPPORTED` o `REJECTED`
        contrastándolo con las claves reales del archivo y las reglas de seguridad.
        """
        existing_keys: dict[tuple[Optional[str], str], str] = config_snapshot.get("keys", {})
        results: list[InvestigationRecommendation] = []

        for item in raw_candidates:
            sec = item.get("section")
            key = str(item.get("key") or "").strip()
            prop_val = str(item.get("proposed_value") or "").strip()
            justif = str(item.get("justification") or "Optimización sugerida.").strip()
            risk = str(item.get("risk_level") or "LOW").strip().upper()
            requested_evid = str(item.get("evidence") or "PLAUSIBLE").strip().upper()

            if risk not in VALID_RISK_LEVELS:
                risk = "MEDIUM"

            if (
                not key
                or "\x00" in prop_val
                or ".." in key
                or any(tok in prop_val.lower() for tok in ("rm -rf", "del /f", "format c:", "--yolo"))
            ):
                results.append(
                    InvestigationRecommendation(
                        section=sec,
                        key=key or "<invalid>",
                        proposed_value=prop_val,
                        justification=justif,
                        risk_level="HIGH",
                        evidence="REJECTED",
                        source_provider=provider_name,
                        reason="Contiene parámetros peligrosos o clave vacía.",
                    )
                )
                continue

            if (sec, key) not in existing_keys:
                results.append(
                    InvestigationRecommendation(
                        section=sec,
                        key=key,
                        proposed_value=prop_val,
                        justification=justif,
                        risk_level=risk,
                        evidence="UNSUPPORTED",
                        source_provider=provider_name,
                        reason=f"La clave '{key}' en sección '{sec}' no existe en el archivo de configuración detectado.",
                    )
                )
                continue

            if existing_keys[(sec, key)] == "__AMBIGUOUS_DUPLICATE__":
                results.append(
                    InvestigationRecommendation(
                        section=sec,
                        key=key,
                        proposed_value=prop_val,
                        justification=justif,
                        risk_level="HIGH",
                        evidence="REJECTED",
                        source_provider=provider_name,
                        reason=f"La clave '{key}' está duplicada de forma ambigua en la sección '{sec}'.",
                    )
                )
                continue

            final_evidence = (
                requested_evid if requested_evid in APPROVABLE_EVIDENCE_LEVELS else "PLAUSIBLE"
            )
            results.append(
                InvestigationRecommendation(
                    section=sec,
                    key=key,
                    proposed_value=prop_val,
                    justification=justif,
                    risk_level=risk,
                    evidence=final_evidence,
                    source_provider=provider_name,
                    reason="Clave verificada en el archivo local.",
                )
            )

        return results

    # ------------------------------------------------------------------
    # Fase 4 y Fase 5: Planificación y Validación
    # ------------------------------------------------------------------

    def build_and_validate_plan(
        self,
        *,
        execution_id: str,
        user_goal: str,
        application_name: str,
        application_version: str = "1.0",
        config_snapshot: Mapping[str, Any],
        hardware_context: Mapping[str, Any],
        recommendations: Sequence[InvestigationRecommendation],
        provider_name: str = "BrainManager",
    ) -> ChangePlan:
        """
        Fase 4 y Fase 5: Construye un `ChangePlan` únicamente con las recomendaciones
        `VERIFIED` o `PLAUSIBLE`, lo valida mediante `PolicyEngine` y deja la ejecución
        lista para simulación.
        """
        record = self.get_execution(execution_id)
        # Un registro existente solo se puede usar para la primera planificación y debe
        # estar libre de cualquier plan o evidencia operativa anterior.
        if record.state not in {
            ChainState.RECEIVED,
            ChainState.INVESTIGATING,
            ChainState.PLANNING,
        }:
            raise ApprovalBindingError(
                f"No se puede construir un plan nuevo desde el estado {record.state.value}."
            )
        if (
            record.plan is not None
            or record.submitted_plan is not None
            or record.approved_plan_snapshot is not None
            or record.approval_hash is not None
            or record.operation_runtime_snapshot
            or record.operation_states
        ):
            raise ApprovalBindingError(
                f"La ejecución '{execution_id}' ya contiene información de plan o ejecución."
            )

        canonical_path = str(config_snapshot["canonical_path"])
        fmt = str(config_snapshot["format"])
        file_hash = str(config_snapshot["sha256"])
        existing_keys: dict[tuple[Optional[str], str], str] = config_snapshot.get("keys", {})

        operations: list[ChangeOperation] = []
        op_type = (
            "update_json_key"
            if fmt == "json"
            else ("update_xml_node" if fmt == "xml" else "update_key")
        )

        for idx, rec in enumerate(recommendations, start=1):
            if rec.evidence not in APPROVABLE_EVIDENCE_LEVELS:
                continue
            current_val = existing_keys.get((rec.section, rec.key))
            if current_val == rec.proposed_value:
                continue

            operations.append(
                ChangeOperation(
                    id=f"op-{idx:03d}",
                    file_path=canonical_path,
                    format=fmt,
                    operation_type=op_type,
                    section=rec.section,
                    key=rec.key,
                    expected_current_value=current_val,
                    proposed_value=rec.proposed_value,
                    justification=rec.justification,
                    risk_level=rec.risk_level,
                    evidence=rec.evidence,
                    verification_method=f"read_back_and_verify_{fmt}_key_and_sha256",
                    reversible=True,
                    requires_approval=True,
                    expected_file_hash=file_hash,
                )
            )

        plan = ChangePlan(
            plan_id=f"plan-{uuid.uuid4().hex[:10]}",
            execution_id=execution_id,
            user_goal=user_goal,
            application_name=application_name,
            application_version=application_version,
            hardware_context=dict(hardware_context),
            operations=operations,
            provider=provider_name,
        )
        try:
            self.policy_engine.validate_plan(plan)
        except (PolicyValidationError, PermissionError) as exc:
            raise

        # Solo tras validar el candidato avanzamos el registro y publicamos el plan.
        if record.state in (ChainState.RECEIVED, ChainState.INVESTIGATING):
            self.transition_state(execution_id, ChainState.PLANNING)
        self.transition_state(execution_id, ChainState.VALIDATING)
        plan.status = ChainState.VALIDATING.value
        record.plan = plan
        record.submitted_plan = plan

        return plan

    # ------------------------------------------------------------------
    # Fase 6: Simulación (Dry-Run en solo lectura)
    # ------------------------------------------------------------------

    def simulate_plan(
        self,
        plan: ChangePlan,
        *,
        selected_operation_ids: Optional[Sequence[str]] = None,
    ) -> dict[str, Any]:
        """
        Fase 6: Valida el plan en modo de solo lectura (incluyendo el grafo completo de
        dependencias y, si se indica, la selección de operaciones), genera la vista previa
        detallada y transiciona a `AWAITING_APPROVAL`. No modifica ningún archivo en disco.
        """
        record = self.get_execution(plan.execution_id)
        allowed_simulation_states = {
            ChainState.RECEIVED,
            ChainState.INVESTIGATING,
            ChainState.PLANNING,
            ChainState.VALIDATING,
            ChainState.AWAITING_APPROVAL,
        }
        if record.state not in allowed_simulation_states:
            raise ApprovalBindingError(
                f"No se puede simular una ejecución en estado terminal o incompatible: {record.state.value}."
            )
        if record.plan is not None:
            if record.plan.plan_id != plan.plan_id or record.plan.approval_hash() != plan.approval_hash():
                raise ApprovalBindingError("No se puede sustituir el plan asociado a esta ejecución.")
        runtime_states = record.operation_runtime_snapshot.values()
        if any(
            state.get("status", OperationStatus.PENDING.value) != OperationStatus.PENDING.value
            or state.get("executed_file_hash")
            or state.get("evidence_validated")
            for state in runtime_states
        ):
            raise ApprovalBindingError("El registro ya contiene evidencia operativa incompatible con una nueva simulación.")
        if any(
            status != OperationStatus.PENDING.value
            for status in record.operation_states.values()
        ):
            raise ApprovalBindingError("El historial de operaciones impide iniciar otra simulación.")
        if any(
            op.status in {OperationStatus.APPLIED.value, OperationStatus.ROLLBACK_FAILED.value,
                          OperationStatus.ROLLED_BACK.value, OperationStatus.FAILED.value,
                          OperationStatus.BLOCKED.value}
            for op in (record.plan.operations if record.plan is not None else ())
        ):
            raise ApprovalBindingError("La ejecución contiene operaciones aplicadas, fallidas o revertidas.")
        if any(op.status != OperationStatus.PENDING.value for op in plan.operations):
            raise PolicyValidationError("La simulación solo admite operaciones nuevas en estado PENDING.")

        try:
            if selected_operation_ids is not None:
                self.preflight_validate(
                    plan,
                    approved_operation_ids=selected_operation_ids,
                    record=record,
                )
                selected_ids = self._normalize_selection(plan, selected_operation_ids)
            else:
                self.policy_engine.validate_plan(plan)
                selected_ids = None
        except (PolicyValidationError, PermissionError) as exc:
            if ChainState.REJECTED in ALLOWED_STATE_TRANSITIONS.get(record.state, set()):
                self.transition_state(plan.execution_id, ChainState.REJECTED, reason=str(exc))
            raise

        preview_items: list[dict[str, Any]] = []
        for op in plan.operations:
            preview_items.append(
                {
                    "operation_id": op.id,
                    "file_path": op.file_path,
                    "section": op.section,
                    "key": op.key,
                    "original_value": op.expected_current_value,
                    "proposed_value": op.proposed_value,
                    "justification": op.justification,
                    "risk_level": op.risk_level,
                    "evidence": op.evidence,
                    "depends_on": list(op.depends_on),
                    "status": op.status,
                    "backup_strategy": (
                        f"Copia byte a byte en '{self.file_editor.backup_dir or Path(op.file_path).parent}' con verificación SHA-256"
                    ),
                    "verification_method": op.verification_method,
                    "rollback_method": "FileEditorSkill.rollback / RollbackManager con verificación de hash original",
                }
            )

        approval_token = plan.approval_hash()
        # Prevalidamos las transiciones antes de publicar cambios en el registro.
        target_state = record.state
        if target_state in (ChainState.RECEIVED, ChainState.INVESTIGATING):
            if ChainState.PLANNING not in ALLOWED_STATE_TRANSITIONS.get(target_state, set()):
                raise ApprovalBindingError("La ejecución no admite iniciar la planificación.")
            target_state = ChainState.PLANNING
        if target_state == ChainState.PLANNING:
            if ChainState.VALIDATING not in ALLOWED_STATE_TRANSITIONS.get(target_state, set()):
                raise ApprovalBindingError("La ejecución no admite validación.")
            target_state = ChainState.VALIDATING
        if target_state == ChainState.VALIDATING and ChainState.AWAITING_APPROVAL not in ALLOWED_STATE_TRANSITIONS.get(target_state, set()):
            raise ApprovalBindingError("La ejecución no admite solicitar aprobación.")

        if record.state in (ChainState.RECEIVED, ChainState.INVESTIGATING):
            self.transition_state(plan.execution_id, ChainState.PLANNING)
        if record.state == ChainState.PLANNING:
            self.transition_state(plan.execution_id, ChainState.VALIDATING)
        if record.state == ChainState.VALIDATING:
            self.transition_state(plan.execution_id, ChainState.AWAITING_APPROVAL)
        record.bound_operation_ids = selected_ids
        plan.status = record.state.value
        record.plan = plan
        record.submitted_plan = plan
        record.approval_hash = approval_token
        record.approved_plan_id = plan.plan_id
        record.approved_plan_snapshot = json.loads(plan.canonical_json())
        for op in plan.operations:
            record.operation_states.setdefault(op.id, op.status)
            record.operation_runtime_snapshot[op.id] = {
                "status": op.status,
                "executed_file_hash": op.executed_file_hash,
                "backup_path": op.backup_path,
                "error_detail": op.error_detail,
                "status_history": list(op.status_history),
                "evidence_validated": op._execution_evidence_validated,
            }
        return {
            "plan_id": plan.plan_id,
            "execution_id": plan.execution_id,
            "application_name": plan.application_name,
            "state": record.state.value,
            "approval_hash": approval_token,
            "operations_preview": preview_items,
            "total_operations": len(preview_items),
        }

    def bind_selective_approval(
        self,
        plan: ChangePlan,
        approved_operation_ids: Sequence[str],
        *,
        approval_hash: Optional[str] = None,
    ) -> str:
        """
        Vincula criptográficamente una selección específica de IDs de operación al registro
        de aprobación antes de ejecutar. Cualquier alteración posterior de la lista de IDs
        o del contenido del plan invalidará la autorización en `execute_approved_plan`.
        """
        record = self.get_execution(plan.execution_id)
        current_hash = plan.approval_hash()
        if approval_hash is not None and approval_hash != current_hash:
            raise ApprovalBindingError(
                "El hash proporcionado al vincular la selección no coincide con el plan actual."
            )
        if record.approval_hash and record.approval_hash != current_hash:
            raise ApprovalBindingError(
                "El plan fue alterado respecto al hash emitido durante la simulación."
            )

        self.preflight_validate(
            plan,
            approved_operation_ids=approved_operation_ids,
            record=record,
        )
        record.bound_operation_ids = self._normalize_selection(plan, approved_operation_ids)
        return current_hash

    @staticmethod
    def _normalize_selection(
        plan: ChangePlan, selected_ids: Optional[Sequence[str]]
    ) -> tuple[str, ...]:
        """Normaliza la selección por identidad, preservando el orden canónico del plan."""
        known = {op.id for op in plan.operations}
        if selected_ids is None:
            return tuple(op.id for op in plan.operations)
        if isinstance(selected_ids, (str, bytes)) or not isinstance(selected_ids, Sequence):
            raise PolicyValidationError("La selección debe ser una secuencia de IDs.")
        raw = [item.strip() if isinstance(item, str) else "" for item in selected_ids]
        if not raw or any(not item for item in raw) or len(set(raw)) != len(raw):
            raise PolicyValidationError("La selección está vacía o contiene IDs vacíos/duplicados.")
        unknown = set(raw) - known
        if unknown:
            raise PolicyValidationError(f"IDs de operación desconocidos: {sorted(unknown)}.")
        requested = set(raw)
        return tuple(op.id for op in plan.operations if op.id in requested)

    def authorize_plan_interactively(
        self,
        plan: ChangePlan,
        *,
        approval_hash: str,
        selected_operation_ids: Optional[Sequence[str]] = None,
        input_fn: Callable[[str], str] = input,
        ttl_seconds: Optional[int] = None,
    ) -> str:
        """Emite token efímero solo tras confirmación explícita por consola.

        El nonce no es una frontera ante código local comprometido; la garantía cubre
        el flujo de autorización de esta aplicación dentro del proceso actual.
        """
        record = self.get_execution(plan.execution_id)
        if (
            record.execution_id != plan.execution_id
            or record.plan is None
            or record.plan.plan_id != plan.plan_id
            or record.approved_plan_id != plan.plan_id
        ):
            raise ApprovalBindingError("El plan no corresponde al registro de ejecución.")

        current_hash = plan.approval_hash()
        if (
            not approval_hash
            or approval_hash != current_hash
            or record.approval_hash != current_hash
            or record.approved_plan_snapshot is None
            or json.loads(plan.canonical_json()) != record.approved_plan_snapshot
        ):
            raise ApprovalBindingError("El hash o la instantánea de aprobación no coincide con el plan.")

        normalized = self._normalize_selection(plan, selected_operation_ids)
        if record.bound_operation_ids is not None and normalized != record.bound_operation_ids:
            raise ApprovalBindingError("La selección no coincide con la selección vinculada al plan.")

        reopen_partial = (
            record.state == ChainState.COMPLETED
            and record.plan is not None
            and not record.plan.is_fully_completed()
            and selected_operation_ids is not None
        )
        if record.state != ChainState.AWAITING_APPROVAL and not reopen_partial:
            raise ApprovalBindingError(
                f"La ejecución no admite autorización en estado {record.state.value}."
            )
        if reopen_partial and ChainState.AWAITING_APPROVAL not in ALLOWED_STATE_TRANSITIONS.get(record.state, set()):
            raise ApprovalBindingError("La ejecución parcial no admite reabrir la aprobación.")

        prompt = f"Confirme en consola el plan {plan.plan_id} y operaciones {list(normalized)} [s/n]: "
        answer = input_fn(prompt)
        if (answer or "").strip().lower() not in {"s", "si", "sí", "y", "yes"}:
            raise ApprovalBindingError("La autorización humana fue cancelada.")
        if reopen_partial:
            self.transition_state(plan.execution_id, ChainState.AWAITING_APPROVAL)
        for operation in plan.operations:
            operation.freeze_definition()
        token = secrets.token_urlsafe(32)
        with self._approval_lock:
            self._purge_approval_tokens_locked()
            self._approval_tokens[token] = {
                "plan_id": plan.plan_id,
                "execution_id": plan.execution_id,
                "approval_hash": approval_hash,
                "selection": normalized,
                "expires_at": time.time() + max(
                    1, int(ttl_seconds or getattr(settings, "CHAIN_APPROVAL_TTL_SECONDS", 300))
                ),
                "used": False,
            }
        return token

    # ------------------------------------------------------------------
    # Fase 7, 8, 9 y 10: Aprobación vinculada a hash, Ejecución, Verificación e Informe
    # ------------------------------------------------------------------

    def cancel_plan(self, execution_id: str, reason: str = "Cancelado por el usuario.") -> dict[str, Any]:
        """Cancela explícitamente un plan pendiente sin modificar ningún archivo en disco."""
        self.transition_state(execution_id, ChainState.CANCELLED, reason=reason)
        return {
            "execution_id": execution_id,
            "state": ChainState.CANCELLED.value,
            "reason": reason,
        }

    def execute_approved_plan(
        self,
        plan: ChangePlan,
        *,
        approval_hash: str,
        user_approved: bool = False,
        authorization_token: Optional[str] = None,
        approved_operation_ids: Optional[Sequence[str]] = None,
        continue_independent: bool = False,
        auto_rollback: bool = True,
    ) -> dict[str, Any]:
        """
        Fases 7, 8, 9 y 10:
        - Fase 7 (Autorización y Pre-Flight):
          1. Exige un token válido de confirmación interactiva; el booleano legado no autoriza.
          2. Verifica que el plan no haya sido completado previamente (evita reejecución).
          3. Recalcula `plan.approval_hash()` inmediatamente antes de ejecutar y verifica
             coincidencia exacta con `approval_hash`, `record.approval_hash`, `record.approved_plan_id`
             y `record.approved_plan_snapshot`.
          4. Verifica que las operaciones seleccionadas no hayan sido modificadas si existía
             una selección vinculada en `record.bound_operation_ids`.
          5. Ejecuta `preflight_validate()` completo antes de crear cualquier backup o tocar
             archivos: valida el grafo de dependencias, detecta ciclos, rechaza IDs desconocidos,
             duplicados o selecciones vacías, verifica dependencias directas/transitivas y
             obtiene el orden topológico determinista (`ops_to_run`).
        - Fase 8 (Aplicación Controlada y Propagación de Estados):
          Aplica cada operación en orden topológico mediante `FileEditorSkill.modify_ini_or_text`
          y la verifica inmediatamente en disco antes de transicionarla a `APPLIED`.
          Si una operación `A` falla:
          - `A` pasa a `FAILED` (nunca se registra como aplicada).
          - Sus dependientes directas (`B`) y transitivas (`C`) pasan inmediatamente a `BLOCKED`
            y jamás se envían a `FileEditorSkill`.
          - Las operaciones independientes posteriores solo continúan si `continue_independent=True`,
            `auto_rollback=False` y no afectan al mismo archivo fallido.
        - Fase 9 (Verificación Post-Escritura):
          Confirma integridad SHA-256 y valores reales en disco para todas las operaciones aplicadas.
        - Fase 10 (Informe y Política de Resultados Parciales):
          Preserva en todo momento la evidencia de las operaciones completadas previamente.
          Un plan con operaciones pendientes, fallidas o bloqueadas nunca se declara
          completamente exitoso (`fully_completed=False`).
        """
        if not isinstance(plan, ChangePlan):
            raise PolicyValidationError("Se requiere una instancia válida de ChangePlan.")

        with self.transaction_manager.global_lock:
            return self._execute_approved_plan_locked(
                plan,
                approval_hash=approval_hash,
                user_approved=user_approved,
                authorization_token=authorization_token,
                approved_operation_ids=approved_operation_ids,
                continue_independent=continue_independent,
                auto_rollback=auto_rollback,
            )

    def _execute_approved_plan_locked(
        self,
        plan: ChangePlan,
        *,
        approval_hash: str,
        user_approved: bool = False,
        authorization_token: Optional[str] = None,
        approved_operation_ids: Optional[Sequence[str]] = None,
        continue_independent: bool = False,
        auto_rollback: bool = True,
    ) -> dict[str, Any]:
        record = self.get_execution(plan.execution_id)
        if not authorization_token:
            raise ApprovalBindingError(
                "No se puede ejecutar sin token emitido tras confirmación interactiva en consola."
            )

        # Se comprueba primero que el registro realmente admite ejecutar. Así un token
        # válido no se consume cuando la ejecución ya es terminal o incompatible.
        state_before_token = record.state
        partial_completed = (
            state_before_token == ChainState.COMPLETED
            and record.plan is not None
            and not record.plan.is_fully_completed()
            and approved_operation_ids is not None
        )
        if state_before_token != ChainState.AWAITING_APPROVAL and not partial_completed:
            raise ApprovalBindingError(
                f"El registro no admite ejecución en estado {state_before_token.value}."
            )
        if record.plan is None or record.plan.plan_id != plan.plan_id or record.approved_plan_id != plan.plan_id:
            raise ApprovalBindingError("El plan no coincide con el plan asociado al registro.")

        current_plan_hash = plan.approval_hash()
        normalized_selection = self._normalize_selection(plan, approved_operation_ids)
        if (
            approval_hash != current_plan_hash
            or record.approval_hash != current_plan_hash
            or record.approved_plan_snapshot is None
            or json.loads(plan.canonical_json()) != record.approved_plan_snapshot
        ):
            raise ApprovalBindingError("El hash o la instantánea de aprobación ya no coincide con el plan.")
        if record.bound_operation_ids is not None and normalized_selection != record.bound_operation_ids:
            raise ApprovalBindingError("La selección no coincide con la selección vinculada a la aprobación.")

        with self._approval_lock:
            self._purge_approval_tokens_locked()
            token_record = self._approval_tokens.get(authorization_token)
            if (
                record.state != state_before_token
                or token_record is None
                or token_record["used"]
                or token_record["expires_at"] < time.time()
                or token_record["plan_id"] != plan.plan_id
                or token_record["execution_id"] != plan.execution_id
                or token_record["approval_hash"] != current_plan_hash
                or token_record["selection"] != normalized_selection
            ):
                raise ApprovalBindingError("Token inválido, expirado, reutilizado o vinculado a otro plan/selección.")
            self._approval_tokens.pop(authorization_token, None)
        for op in plan.operations:
            record.operation_states.setdefault(op.id, op.status)

        if not authorization_token:
            raise ApprovalBindingError(
                "No se puede ejecutar sin token emitido tras confirmación interactiva en consola."
            )

        # Detección de intento de reejecución sobre un plan ya completado íntegramente
        if record.state == ChainState.COMPLETED and plan.is_fully_completed():
            raise DuplicateExecutionError(
                f"El plan '{plan.plan_id}' ya fue ejecutado y completado previamente; "
                "no puede volver a aplicarse accidentalmente."
            )

        # Si el plan tuvo una ejecución parcial previa (quedan operaciones PENDING) y se invoca
        # para las operaciones restantes, reabrimos controladamente AWAITING_APPROVAL.
        if (
            record.state == ChainState.COMPLETED
            and not plan.is_fully_completed()
            and approved_operation_ids is not None
        ):
            self.transition_state(plan.execution_id, ChainState.AWAITING_APPROVAL)

        if record.state != ChainState.AWAITING_APPROVAL:
            raise ApprovalBindingError(
                f"El plan '{plan.plan_id}' no está en estado AWAITING_APPROVAL (estado actual: {record.state.value})."
            )

        # Evitar reutilizar la aprobación de otro plan distinto
        if (
            record.plan is None
            or record.approved_plan_id != plan.plan_id
            or record.execution_id != plan.execution_id
        ):
            self.transition_state(
                plan.execution_id,
                ChainState.REJECTED,
                reason="Intento de reutilizar aprobación de otro plan o ejecución no simulada.",
            )
            raise ApprovalBindingError(
                f"La aprobación proporcionada no pertenece al plan '{plan.plan_id}' en la ejecución '{plan.execution_id}'."
            )

        # Recálculo del hash SHA-256 inmediatamente antes de ejecutar
        current_plan_hash = plan.approval_hash()
        if not approval_hash or approval_hash != current_plan_hash:
            self.transition_state(
                plan.execution_id,
                ChainState.REJECTED,
                reason="Hash de aprobación inválido o plan modificado tras la simulación.",
            )
            raise ApprovalBindingError(
                "El hash de aprobación no coincide con el contenido canónico actual del plan."
            )

        if not record.approval_hash or record.approval_hash != current_plan_hash:
            self.transition_state(
                plan.execution_id,
                ChainState.REJECTED,
                reason="El plan fue modificado después de emitir el hash de simulación.",
            )
            raise ApprovalBindingError(
                "La aprobación expiró porque el plan fue modificado después de la simulación."
            )

        if (
            record.approved_plan_snapshot is not None
            and json.loads(plan.canonical_json()) != record.approved_plan_snapshot
        ):
            self.transition_state(
                plan.execution_id,
                ChainState.REJECTED,
                reason="El contenido estructural del plan difiere de la instantánea aprobada.",
            )
            raise ApprovalBindingError(
                "El contenido del plan fue alterado después de la aprobación."
            )

        # Verificar que las operaciones seleccionadas no hayan sido alteradas tras vincularlas
        if record.bound_operation_ids is not None:
            if approved_operation_ids is None:
                self.transition_state(
                    plan.execution_id,
                    ChainState.REJECTED,
                    reason="Se intentó ampliar la selección de operaciones vinculada sin nueva aprobación.",
                )
                raise ApprovalBindingError(
                    "Las operaciones seleccionadas no coinciden con la selección vinculada en la aprobación."
                )
            if isinstance(approved_operation_ids, (str, bytes)) or not isinstance(
                approved_operation_ids, Sequence
            ):
                self.transition_state(
                    plan.execution_id,
                    ChainState.REJECTED,
                    reason="Formato inválido en approved_operation_ids.",
                )
                raise PolicyValidationError(
                    "El parámetro 'approved_operation_ids' debe ser una secuencia de IDs."
                )
            normalized_req = self._normalize_selection(plan, approved_operation_ids)
            if normalized_req != record.bound_operation_ids:
                self.transition_state(
                    plan.execution_id,
                    ChainState.REJECTED,
                    reason="Las operaciones seleccionadas fueron modificadas después de aprobarlas.",
                )
                raise ApprovalBindingError(
                    "No se permite modificar las operaciones seleccionadas después de otorgar la aprobación."
                )

        # Token ya consumido atómicamente; continúa con la instantánea aprobada.
        plan = ChangePlan.from_dict(record.approved_plan_snapshot or {})
        for op in plan.operations:
            runtime = record.operation_runtime_snapshot.get(op.id, {})
            object.__setattr__(op, "status", runtime.get("status", OperationStatus.PENDING.value))
            object.__setattr__(op, "executed_file_hash", runtime.get("executed_file_hash"))
            object.__setattr__(op, "backup_path", runtime.get("backup_path"))
            object.__setattr__(op, "error_detail", runtime.get("error_detail"))
            object.__setattr__(op, "_execution_evidence_validated", bool(runtime.get("evidence_validated", False)))
            op.status_history.extend(runtime.get("status_history", []))
        record.plan = plan

        # Validación Pre-Flight centralizada (grafo, ciclos, IDs, selección vacía, dependencias y rutas)
        # Se ejecuta íntegramente ANTES de cualquier backup o escritura.
        try:
            ops_to_run = self.preflight_validate(
                plan,
                approved_operation_ids=approved_operation_ids,
                record=record,
            )
        except DuplicateExecutionError as dup_exc:
            self.transition_state(
                plan.execution_id,
                ChainState.REJECTED,
                reason=str(dup_exc),
            )
            raise
        except PolicyValidationError as pol_exc:
            self.transition_state(
                plan.execution_id,
                ChainState.REJECTED,
                reason=str(pol_exc),
            )
            raise
        except PermissionError as perm_exc:
            self.transition_state(
                plan.execution_id,
                ChainState.REJECTED,
                reason=str(perm_exc),
            )
            raise ApprovalBindingError(
                f"La validación previa a la ejecución detectó una violación de permisos: {perm_exc}"
            ) from perm_exc

        # Sembrar hashes actuales de archivos que ya fueron modificados por operaciones APPLIED previas
        current_file_hashes: dict[str, str] = {}
        for existing_op in plan.operations:
            if (
                existing_op.status == OperationStatus.APPLIED.value
                and existing_op.executed_file_hash
            ):
                canonical_existing = str(
                    self.file_editor.validate_authorized_path(
                        existing_op.file_path, must_exist=True
                    )
                )
                current_file_hashes[canonical_existing] = existing_op.executed_file_hash

        # Antes de abrir una transacción nueva, recuperamos cualquier transacción previa inconclusa
        # y bloqueamos si existen journals corruptos sin resolver.
        recovery_reports = self.recover_pending_transactions()
        corrupt_issues = self.transaction_manager.has_corrupt_or_failed_recovery()
        failed_recoveries = [
            r for r in recovery_reports if r.final_state == TransactionState.RECOVERY_FAILED.value
        ]
        if corrupt_issues or failed_recoveries:
            self.transition_state(
                plan.execution_id,
                ChainState.FAILED,
                reason="Ejecución bloqueada por transacción previa en estado RECOVERY_FAILED o journal corrupto.",
            )
            raise RuntimeError(
                "Ejecución bloqueada: existen transacciones en estado RECOVERY_FAILED o con journal corrupto."
            )

        # Etapa 3 del protocolo de escritura: Crear y persistir la transacción en estado PREPARED
        tx_record = self.transaction_manager.begin_transaction(
            execution_id=plan.execution_id,
            plan_id=plan.plan_id,
            approval_hash=current_plan_hash,
            approved_plan_snapshot=record.approved_plan_snapshot or {},
            ordered_selected_ops=ops_to_run,
            all_plan_ops=plan.operations,
            initial_file_hashes=current_file_hashes,
        )

        # Fase 8: Aplicación controlada en orden topológico determinista
        self.transition_state(plan.execution_id, ChainState.APPLYING)
        applied_ops_this_run: list[dict[str, Any]] = []
        backups_this_run: list[BackupRecord] = []
        failed_files: set[str] = set()
        has_failure_occurred: bool = False
        first_error_msg: str = ""
        op_by_id: dict[str, ChangeOperation] = {op.id: op for op in plan.operations}

        for op in ops_to_run:
            # Ninguna operación bloqueada puede llegar jamás a FileEditorSkill
            if op.status == OperationStatus.BLOCKED.value:
                continue

            # Verificar que todas las dependencias directas de `op` estén efectivamente en estado APPLIED
            unmet_deps = [
                dep_id.strip()
                for dep_id in op.depends_on
                if op_by_id[dep_id.strip()].status != OperationStatus.APPLIED.value
            ]
            if unmet_deps:
                block_msg = (
                    f"Operación '{op.id}' bloqueada porque sus dependencias {unmet_deps} "
                    "no se completaron satisfactoriamente."
                )
                self._record_operation_transition(
                    record, op, OperationStatus.BLOCKED, reason=block_msg
                )
                if op.id not in record.blocked_operations:
                    record.blocked_operations.append(op.id)
                self._propagate_blocked_status(
                    op.id, plan.operations, record=record, reason=block_msg
                )
                continue

            canonical_str = str(
                self.file_editor.validate_authorized_path(op.file_path, must_exist=True)
            )

            # Si ocurrió un fallo previo en una operación independiente:
            # Solo continuamos si la política explícita lo permite (`continue_independent=True` y
            # `auto_rollback=False`) y el archivo objetivo de `op` no sufrió ningún fallo previo.
            if has_failure_occurred:
                if not continue_independent or auto_rollback or (canonical_str in failed_files):
                    skipped = {
                        "operation_id": op.id,
                        "file_path": op.file_path,
                        "reason": "Omitida por política tras un fallo previo en la ejecución.",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    record.skipped_operations.append(skipped)
                    self.file_editor.audit_logger.record(
                        "operation_skipped", plan.execution_id, skipped
                    )
                    continue

            expected_hash = current_file_hashes.get(
                canonical_str, op.expected_file_hash
            )

            def _on_intent(
                orig_sha: str,
                planned_sha: str,
                _target_str: str,
                _op_id: str = op.id,
            ) -> None:
                self.transaction_manager.log_operation_intent(
                    tx_record,
                    _op_id,
                    expected_before_sha256=orig_sha,
                    planned_after_sha256=planned_sha,
                )

            def _on_backup(
                bkp_record: BackupRecord,
                _op_id: str = op.id,
            ) -> None:
                self.transaction_manager.log_backup_verified(
                    tx_record,
                    _op_id,
                    bkp_record,
                )

            def _on_after_replace(
                _final_sha: str,
                _bkp_path: Path,
                _op_id: str = op.id,
            ) -> None:
                self.transaction_manager.journal._trigger_fault(
                    "after_os_replace_before_applied",
                    tx_record.transaction_id,
                    _op_id,
                )

            try:
                res = self._apply_single_operation(
                    op,
                    execution_id=plan.execution_id,
                    expected_file_hash=expected_hash,
                    on_intent_prepared=_on_intent,
                    on_backup_verified=_on_backup,
                    on_after_atomic_replace=_on_after_replace,
                )
                if res.get("status") != "success":
                    err_detail = "; ".join(
                        res.get("errors") or [f"Fallo al aplicar la operación '{op.id}'"]
                    )
                    raise RuntimeError(err_detail)

                # Si se creó un backup durante la escritura, obtener su registro
                bkp_rec: Optional[BackupRecord] = None
                if res.get("backup_path"):
                    bkp_rec = self.file_editor.backup_manager.get_record_by_backup(
                        str(res["backup_path"])
                    )

                # Verificar inmediatamente en disco que el resultado real coincida con el estado esperado
                # antes de declarar la operación como APPLIED (previene inconsistencia estado vs disco).
                try:
                    verif_entry = self._verify_single_operation_on_disk(
                        op,
                        expected_final_hash=str(res["final_hash"]),
                    )
                except Exception as verif_exc:
                    if bkp_rec is not None:
                        record.applied_backups.append(bkp_rec)
                        backups_this_run.append(bkp_rec)
                    raise RuntimeError(
                        f"Inconsistencia post-escritura en '{op.id}': {verif_exc}"
                    ) from verif_exc

                # Si modify_ini_or_text fue simulado en un test sin invocar callbacks, aseguramos transición a EXECUTING
                if tx_record.state == TransactionState.PREPARED.value:
                    self.transaction_manager.log_operation_intent(
                        tx_record,
                        op.id,
                        expected_before_sha256=str(res.get("original_hash") or expected_hash or ""),
                        planned_after_sha256=str(res["final_hash"]),
                    )
                if bkp_rec is not None and tx_record.operations[op.id].backup_path is None:
                    self.transaction_manager.log_backup_verified(tx_record, op.id, bkp_rec)

                # Etapa 13 del protocolo de escritura: Registrar operación como APPLIED en WAL y snapshot
                self.transaction_manager.log_operation_applied(
                    tx_record,
                    op.id,
                    actual_after_sha256=str(res["final_hash"]),
                    verification_result=verif_entry,
                )

                # La escritura fue verificada en disco: registramos estado APPLIED y su evidencia
                self._record_operation_transition(
                    record,
                    op,
                    OperationStatus.APPLIED,
                    reason="Operación aplicada y verificada en disco con SHA-256.",
                    evidence_hash=str(res["final_hash"]),
                    evidence_verified=True,
                )
                object.__setattr__(op, "executed_file_hash", str(res["final_hash"]))
                object.__setattr__(op, "backup_path", str(res["backup_path"]))
                runtime = record.operation_runtime_snapshot.setdefault(op.id, {})
                runtime["executed_file_hash"] = op.executed_file_hash
                runtime["backup_path"] = op.backup_path
                if record.submitted_plan is not None:
                    original_op = next(
                        (item for item in record.submitted_plan.operations if item.id == op.id), None
                    )
                    if original_op is not None:
                        object.__setattr__(original_op, "executed_file_hash", op.executed_file_hash)
                        object.__setattr__(original_op, "backup_path", op.backup_path)
                current_file_hashes[canonical_str] = str(res["final_hash"])

                if bkp_rec is not None:
                    record.applied_backups.append(bkp_rec)
                    backups_this_run.append(bkp_rec)

                applied_entry = {
                    "operation_id": op.id,
                    "file_path": res["file_path"],
                    "backup_path": res["backup_path"],
                    "original_hash": res["original_hash"],
                    "new_hash": res["final_hash"],
                    "status": op.status,
                }
                applied_ops_this_run.append(applied_entry)
                record.applied_operations.append(applied_entry)
                record.verification_results.append(verif_entry)

            except (
                SimulatedTransactionInterrupt,
                JournalCorruptionError,
                InvalidTransactionStateError,
                TransactionLockTimeoutError,
            ):
                # Interrupción abrupta simulada o error crítico de integridad del journal:
                # propagamos de inmediato sin ejecutar limpieza en memoria para que el recuperador actúe desde disco.
                raise
            except Exception as op_exc:
                err_str = str(op_exc)
                has_failure_occurred = True
                failed_files.add(canonical_str)
                if not first_error_msg:
                    first_error_msg = err_str

                if op.status == OperationStatus.PENDING.value:
                    self._record_operation_transition(
                        record,
                        op,
                        OperationStatus.FAILED,
                        reason=err_str,
                    )
                record.failed_operations.append(
                    {
                        "operation_id": op.id,
                        "file_path": op.file_path,
                        "error": err_str,
                        "status": op.status,
                    }
                )
                # Propagar BLOCKED a todas las operaciones dependientes directas y transitivas
                newly_blocked = self._propagate_blocked_status(
                    op.id,
                    plan.operations,
                    record=record,
                    reason=err_str,
                )
                self.transaction_manager.log_operation_failed(
                    tx_record,
                    op.id,
                    error_detail=err_str,
                    blocked_operation_ids=newly_blocked,
                )

        # Sincronizar mapa de estados finales de cada operación
        record.operation_states = plan.get_operation_states()

        # Si hubo cualquier operación fallida o bloqueada, el plan jamás puede declararse exitoso
        if has_failure_occurred:
            if auto_rollback and backups_this_run:
                self.transaction_manager.transition_transaction_state(
                    tx_record,
                    TransactionState.ROLLING_BACK,
                    reason=f"Iniciando rollback automático por fallo: {first_error_msg}",
                )
                rollback_notes = self._rollback_applied_records(
                    backups_this_run,
                    execution_id=plan.execution_id,
                    tx_record=tx_record,
                )
                record.rollback_summary = rollback_notes
                target_chain_state = (
                    ChainState.ROLLED_BACK
                    if rollback_notes["all_restored"]
                    else ChainState.FAILED
                )
                target_tx_state = (
                    TransactionState.ROLLED_BACK
                    if rollback_notes["all_restored"]
                    else TransactionState.FAILED
                )
                self.transaction_manager.transition_transaction_state(
                    tx_record,
                    target_tx_state,
                    reason=f"Resultado de rollback automático: all_restored={rollback_notes['all_restored']}.",
                )
                self.transition_state(
                    plan.execution_id,
                    target_chain_state,
                    reason=f"Fallo durante la aplicación ({first_error_msg}); rollback ejecutado.",
                )
                raise RuntimeError(
                    f"Fallo parcial al ejecutar el plan '{plan.plan_id}': {first_error_msg}. "
                    f"Operaciones bloqueadas: {record.blocked_operations}. "
                    f"Se detuvieron las operaciones dependientes y se restauraron {rollback_notes['restored_count']} backups."
                )

            self.transaction_manager.transition_transaction_state(
                tx_record,
                TransactionState.FAILED,
                reason=f"Fallo durante la ejecución ({first_error_msg}).",
            )
            self.transition_state(
                plan.execution_id,
                ChainState.FAILED,
                reason=f"Fallo durante la ejecución ({first_error_msg}).",
            )
            raise RuntimeError(
                f"Fallo al ejecutar el plan '{plan.plan_id}': {first_error_msg}. "
                f"Operaciones aplicadas antes/independientes: {[a['operation_id'] for a in applied_ops_this_run]}. "
                f"Operaciones bloqueadas: {record.blocked_operations}."
            )

        # Fase 9: Verificación integral post-escritura del lote ejecutado
        self.transition_state(plan.execution_id, ChainState.VERIFYING)
        try:
            verifications = self._verify_applied_operations(
                ops_to_run,
                expected_final_hashes=current_file_hashes,
            )
            # Conservamos verificaciones de pasos previos si existían y actualizamos las actuales
            verified_ids_now = {v["operation_id"] for v in verifications}
            prior_verifications = [
                v
                for v in record.verification_results
                if v.get("operation_id") not in verified_ids_now
            ]
            record.verification_results = prior_verifications + verifications
        except Exception as exc:
            self.transaction_manager.transition_transaction_state(
                tx_record,
                TransactionState.ROLLING_BACK,
                reason=f"Verificación post-escritura fallida ({exc}); iniciando rollback.",
            )
            rollback_notes = self._rollback_applied_records(
                backups_this_run,
                execution_id=plan.execution_id,
                tx_record=tx_record,
            )
            record.rollback_summary = rollback_notes
            self.transaction_manager.transition_transaction_state(
                tx_record,
                TransactionState.ROLLED_BACK if rollback_notes["all_restored"] else TransactionState.FAILED,
                reason=f"Verificación post-escritura fallida ({exc}); rollback ejecutado.",
            )
            self.transition_state(
                plan.execution_id,
                ChainState.ROLLED_BACK if rollback_notes["all_restored"] else ChainState.FAILED,
                reason=f"Verificación post-escritura fallida ({exc}); rollback ejecutado.",
            )
            raise RuntimeError(
                f"La verificación post-escritura falló: {exc}. Los archivos fueron restaurados."
            ) from exc

        # Fase 10: Completado e informe real (distinguiendo ejecución total vs parcial selectiva)
        fully_completed = plan.is_fully_completed()
        self.transaction_manager.commit_transaction(
            tx_record,
            fully_completed=fully_completed,
        )
        self.transition_state(plan.execution_id, ChainState.COMPLETED)
        return {
            "plan_id": plan.plan_id,
            "execution_id": plan.execution_id,
            "state": record.state.value,
            "plan_status": "COMPLETED" if fully_completed else "PARTIALLY_APPLIED",
            "fully_completed": fully_completed,
            "partial_execution": not fully_completed,
            "operation_states": dict(record.operation_states),
            "applied_operations": applied_ops_this_run,
            "verifications": verifications,
            "summary": (
                f"Se aplicaron y verificaron {len(applied_ops_this_run)} operaciones en "
                f"'{plan.application_name}' con respaldos SHA-256 verificados. "
                "Nota: el impacto real en FPS o estabilidad debe comprobarse ejecutando la aplicación."
            ),
        }

    def _apply_single_operation(
        self,
        op: ChangeOperation,
        *,
        execution_id: str,
        expected_file_hash: Optional[str],
        on_intent_prepared: Optional[Callable[[str, str, str], None]] = None,
        on_backup_verified: Optional[Callable[[BackupRecord], None]] = None,
        on_after_atomic_replace: Optional[Callable[[str, Path], None]] = None,
    ) -> dict[str, Any]:
        """Ejecuta una `ChangeOperation` individual a través de `FileEditorSkill.modify_ini_or_text`."""
        tx_kwargs: dict[str, Any] = {}
        if on_intent_prepared is not None:
            tx_kwargs["on_intent_prepared"] = on_intent_prepared
        if on_backup_verified is not None:
            tx_kwargs["on_backup_verified"] = on_backup_verified
        if on_after_atomic_replace is not None:
            tx_kwargs["on_after_atomic_replace"] = on_after_atomic_replace

        if op.operation_type == "replace_content":
            return self.file_editor.modify_ini_or_text(
                op.file_path,
                replacement_content=op.proposed_value,
                execution_id=execution_id,
                expected_file_hash=expected_file_hash,
                **tx_kwargs,
            )

        if op.operation_type == "update_json_key":
            try:
                parsed_val = json.loads(op.proposed_value)
            except Exception:
                parsed_val = op.proposed_value
            return self.file_editor.modify_ini_or_text(
                op.file_path,
                key_values={op.key or "": parsed_val},
                execution_id=execution_id,
                expected_file_hash=expected_file_hash,
                **tx_kwargs,
            )

        if op.operation_type in ("update_key", "update_xml_node"):
            return self.file_editor.modify_ini_or_text(
                op.file_path,
                key_values={op.key or "": op.proposed_value},
                section=op.section,
                execution_id=execution_id,
                expected_file_hash=expected_file_hash,
                **tx_kwargs,
            )

        raise PolicyValidationError(f"Tipo de operación no soportado: '{op.operation_type}'.")

    def _verify_applied_operations(
        self,
        operations: Sequence[ChangeOperation],
        *,
        expected_final_hashes: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        """
        Fase 9: Relee cada archivo modificado, comprueba que el valor propuesto esté
        efectivamente presente, que la sintaxis sea válida y que el SHA-256 coincida.
        Si múltiples operaciones modificaron el mismo archivo (en claves distintas o en una
        cadena secuencial `H0 -> H1 -> H2 -> H3`), comprueba siempre el último hash esperado
        de la cadena y el estado final de cada clave/archivo.
        """
        # Determinar la última operación en la secuencia para cada (archivo, sección, clave)
        # y para cada archivo con `replace_content`, de modo que la verificación final compruebe
        # el último eslabón de la cadena (Sección 5.1).
        last_op_for_target: dict[tuple[str, str, str], str] = {}
        last_full_replace_for_file: dict[str, str] = {}
        for op in operations:
            canonical_str = str(self.file_editor.validate_authorized_path(op.file_path, must_exist=True))
            if op.operation_type == "replace_content":
                last_full_replace_for_file[canonical_str] = op.id
            else:
                sig = (canonical_str, (op.section or "").strip(), (op.key or "").strip())
                last_op_for_target[sig] = op.id

        results: list[dict[str, Any]] = []
        for op in operations:
            canonical = self.file_editor.validate_authorized_path(op.file_path, must_exist=True)
            canonical_str = str(canonical)
            expected_hash = expected_final_hashes.get(canonical_str)
            sig = (canonical_str, (op.section or "").strip(), (op.key or "").strip())
            superseded_by_later_op = (
                (canonical_str in last_full_replace_for_file and last_full_replace_for_file[canonical_str] != op.id)
                or (op.operation_type != "replace_content" and last_op_for_target.get(sig) != op.id)
            )
            if superseded_by_later_op:
                actual_hash = compute_sha256_file(canonical)
                if expected_hash and actual_hash.lower() != expected_hash.lower():
                    raise RuntimeError(
                        f"El hash final de la cadena en '{canonical.name}' ({actual_hash[:12]}...) "
                        f"no coincide con el último hash esperado ({expected_hash[:12]}...)."
                    )
                results.append(
                    {
                        "operation_id": op.id,
                        "file_path": canonical_str,
                        "verified": True,
                        "sha256": actual_hash,
                    }
                )
            else:
                res = self._verify_single_operation_on_disk(
                    op,
                    expected_final_hash=expected_hash,
                )
                results.append(res)
        return results

    def _rollback_applied_records(
        self,
        backups: Sequence[BackupRecord],
        *,
        execution_id: str,
        tx_record: Optional[TransactionRecord] = None,
    ) -> dict[str, Any]:
        """
        Restaura en orden inverso (LIFO) los backups aplicados durante una ejecución,
        verificando `latest_rec.post_edit_sha256` para no sobrescribir jamás cambios
        legítimos que el usuario haya realizado externamente sobre el archivo.
        """
        restored = 0
        errors: list[str] = []
        record = self.get_execution(execution_id)
        pending_backups: list[BackupRecord] = []
        for backup in backups:
            op_id = next((str(item.get("operation_id")) for item in record.applied_operations
                          if str(item.get("backup_path")) == str(backup.backup_path)), None)
            op = next((item for item in record.plan.operations if item.id == op_id), None) if record.plan else None
            if op is None or op.status in (
                OperationStatus.APPLIED.value, OperationStatus.ROLLBACK_FAILED.value
            ):
                pending_backups.append(backup)
        for bkp in reversed(pending_backups):
            latest_rec = (
                self.file_editor.backup_manager.get_record_by_backup(bkp.backup_path)
                or bkp
            )
            op_id = next((str(item.get("operation_id")) for item in record.applied_operations
                          if str(item.get("backup_path")) == str(latest_rec.backup_path)), None)
            op = next((item for item in record.plan.operations if item.id == op_id), None) if record.plan else None
            ok = self.file_editor.rollback(
                backup_path=latest_rec.backup_path,
                target_path=latest_rec.target_path,
                expected_current_hash=latest_rec.post_edit_sha256,
                execution_id=f"{execution_id}_rollback",
            )
            if ok:
                restored_hash = compute_sha256_file(Path(latest_rec.target_path))
                if restored_hash.lower() != latest_rec.original_sha256.lower():
                    err_msg = f"Hash restaurado no coincide para {latest_rec.target_path}."
                    errors.append(err_msg)
                    if op and op.status == OperationStatus.APPLIED.value:
                        self._record_operation_transition(
                            record, op, OperationStatus.ROLLBACK_FAILED,
                            reason=(
                                f"timestamp={datetime.now(timezone.utc).isoformat()}; "
                                f"hash_anterior={latest_rec.post_edit_sha256}; "
                                f"hash_restaurado={restored_hash}; backup={latest_rec.backup_path}; "
                                "resultado=restauración no coincide con el hash original."
                            ),
                        )
                    if tx_record is not None and op_id and op_id in tx_record.operations:
                        self.transaction_manager.log_operation_rollback_failed(
                            tx_record, op_id, error_detail=err_msg
                        )
                    continue
                restored += 1
                if op and op.status in (OperationStatus.APPLIED.value, OperationStatus.ROLLBACK_FAILED.value):
                    self._record_operation_transition(
                        record, op, OperationStatus.ROLLED_BACK,
                        reason=(
                            f"timestamp={datetime.now(timezone.utc).isoformat()}; "
                            f"hash_anterior={latest_rec.post_edit_sha256}; "
                            f"hash_restaurado={restored_hash}; backup={latest_rec.backup_path}; "
                            "resultado=restauración verificada."
                        ),
                    )
                if tx_record is not None and op_id and op_id in tx_record.operations:
                    pre_rb_bak = self.file_editor.last_operation_result.get("pre_rollback_backup")
                    self.transaction_manager.log_operation_rolled_back(
                        tx_record,
                        op_id,
                        restored_sha256=restored_hash,
                        pre_rollback_backup_path=str(pre_rb_bak) if pre_rb_bak else None,
                    )
            else:
                rb_errs = (
                    self.file_editor.last_operation_result.get("errors")
                    or ["Fallo o conflicto de concurrencia durante rollback"]
                )
                errors.extend(rb_errs)
                if op and op.status == OperationStatus.APPLIED.value:
                    self._record_operation_transition(
                        record, op, OperationStatus.ROLLBACK_FAILED,
                        reason=(
                            f"timestamp={datetime.now(timezone.utc).isoformat()}; "
                            f"hash_anterior={latest_rec.post_edit_sha256}; backup={latest_rec.backup_path}; "
                            "resultado=falló la restauración verificada."
                        ),
                    )
                if tx_record is not None and op_id and op_id in tx_record.operations:
                    self.transaction_manager.log_operation_rollback_failed(
                        tx_record, op_id, error_detail="; ".join(rb_errs)
                    )
        return {
            "restored_count": restored,
            "all_restored": len(errors) == 0 and restored == len(pending_backups),
            "attempted_count": len(pending_backups),
            "errors": errors,
        }

    def rollback_execution(
        self,
        execution_id: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """
        Restaura los archivos modificados en una ejecución utilizando sus backups registrados
        en orden inverso (LIFO) para devolver cada archivo exactamente a su SHA-256 original,
        detectando conflictos si el archivo fue editado externamente después de la ejecución.
        """
        with self.transaction_manager.global_lock:
            record = self.get_execution(execution_id)
            if record.state == ChainState.ROLLED_BACK or (
                record.plan is not None
                and any(op.status == OperationStatus.ROLLED_BACK.value for op in record.plan.operations)
                and all(op.status not in (OperationStatus.APPLIED.value, OperationStatus.ROLLBACK_FAILED.value)
                        for op in record.plan.operations)
            ):
                raise RuntimeError(f"La ejecución '{execution_id}' ya fue revertida; no se permite repetir el rollback.")
            if force:
                raise PolicyValidationError("force=True está deshabilitado para proteger cambios externos.")
            if not record.applied_backups:
                raise RuntimeError(
                    f"La ejecución '{execution_id}' no registra backups aplicados para restaurar."
                )

            tx_list = self.transaction_manager.get_transactions_for_execution(execution_id)
            tx_by_op_id: dict[str, TransactionRecord] = {}
            for tx in tx_list:
                for op_id_key in tx.operations:
                    tx_by_op_id[op_id_key] = tx
                if tx.state in (
                    TransactionState.COMMITTED.value,
                    TransactionState.PARTIALLY_APPLIED.value,
                    TransactionState.FAILED.value,
                ):
                    self.transaction_manager.transition_transaction_state(
                        tx,
                        TransactionState.ROLLING_BACK,
                        reason=f"Iniciando rollback explícito de la ejecución '{execution_id}'.",
                    )

            restored_files: list[str] = []
            pending_backups: list[BackupRecord] = []
            for backup in record.applied_backups:
                op_id = next((str(item.get("operation_id")) for item in record.applied_operations
                              if str(item.get("backup_path")) == str(backup.backup_path)), None)
                op = next((item for item in record.plan.operations if item.id == op_id), None) if record.plan else None
                if op is None or op.status in (
                    OperationStatus.APPLIED.value, OperationStatus.ROLLBACK_FAILED.value
                ):
                    pending_backups.append(backup)
            for bkp in reversed(pending_backups):
                # Recargar registro actualizado desde BackupManager por si post_edit_sha256 fue actualizado
                latest_rec = self.file_editor.backup_manager.get_record_by_backup(bkp.backup_path) or bkp
                expected_curr = latest_rec.post_edit_sha256
                op_id = next((str(item.get("operation_id")) for item in record.applied_operations
                              if str(item.get("backup_path")) == str(latest_rec.backup_path)), None)
                op = next((item for item in record.plan.operations if item.id == op_id), None) if record.plan else None
                owning_tx = tx_by_op_id.get(op_id or "")

                ok = self.file_editor.rollback(
                    backup_path=latest_rec.backup_path,
                    target_path=latest_rec.target_path,
                    expected_current_hash=expected_curr,
                    execution_id=f"{execution_id}_rollback",
                )
                if not ok:
                    errs = "; ".join(
                        self.file_editor.last_operation_result.get("errors")
                        or ["Conflicto o fallo durante rollback"]
                    )
                    record.rollback_summary = {
                        "restored_count": len(restored_files), "all_restored": False,
                        "restored_files": list(restored_files), "error": errs,
                    }
                    if op and op.status == OperationStatus.APPLIED.value:
                        self._record_operation_transition(
                            record, op, OperationStatus.ROLLBACK_FAILED,
                            reason=(
                                f"timestamp={datetime.now(timezone.utc).isoformat()}; "
                                f"hash_anterior={latest_rec.post_edit_sha256}; backup={latest_rec.backup_path}; "
                                f"resultado=falló la restauración: {errs}"
                            ),
                        )
                    if owning_tx is not None and op_id:
                        self.transaction_manager.log_operation_rollback_failed(
                            owning_tx, op_id, error_detail=errs
                        )
                    for tx in tx_list:
                        if tx.state == TransactionState.ROLLING_BACK.value:
                            self.transaction_manager.transition_transaction_state(
                                tx, TransactionState.FAILED, reason=f"Rollback parcial fallido: {errs}"
                            )
                    if record.state != ChainState.FAILED:
                        self.transition_state(
                            execution_id, ChainState.FAILED,
                            reason=f"Rollback parcial; se preservaron {len(restored_files)} restauraciones verificadas: {errs}",
                        )
                    raise RuntimeError(errs)
                restored_hash = compute_sha256_file(Path(latest_rec.target_path))
                if restored_hash.lower() != latest_rec.original_sha256.lower():
                    if op and op.status == OperationStatus.APPLIED.value:
                        self._record_operation_transition(
                            record, op, OperationStatus.ROLLBACK_FAILED,
                            reason=(
                                f"timestamp={datetime.now(timezone.utc).isoformat()}; "
                                f"hash_anterior={latest_rec.post_edit_sha256}; "
                                f"hash_restaurado={restored_hash}; backup={latest_rec.backup_path}; "
                                "resultado=restauración no coincide con el hash original."
                            ),
                        )
                    if owning_tx is not None and op_id:
                        self.transaction_manager.log_operation_rollback_failed(
                            owning_tx, op_id, error_detail="Hash restaurado no coincide con el original."
                        )
                    for tx in tx_list:
                        if tx.state == TransactionState.ROLLING_BACK.value:
                            self.transaction_manager.transition_transaction_state(
                                tx, TransactionState.FAILED, reason="Hash restaurado no coincide con el original."
                            )
                    if record.state != ChainState.FAILED:
                        self.transition_state(
                            execution_id, ChainState.FAILED,
                            reason="Rollback parcial: el hash restaurado no coincide con el backup.",
                        )
                    raise RuntimeError(f"La restauración de '{latest_rec.target_path}' falló la verificación SHA-256.")
                restored_files.append(latest_rec.target_path)
                if op and op.status in (OperationStatus.APPLIED.value, OperationStatus.ROLLBACK_FAILED.value):
                    self._record_operation_transition(
                        record, op, OperationStatus.ROLLED_BACK,
                        reason=(
                            f"timestamp={datetime.now(timezone.utc).isoformat()}; "
                            f"hash_anterior={latest_rec.post_edit_sha256}; "
                            f"hash_restaurado={restored_hash}; backup={latest_rec.backup_path}; "
                            "resultado=restauración verificada."
                        ),
                    )
                if owning_tx is not None and op_id:
                    pre_rb_bak = self.file_editor.last_operation_result.get("pre_rollback_backup")
                    self.transaction_manager.log_operation_rolled_back(
                        owning_tx,
                        op_id,
                        restored_sha256=restored_hash,
                        pre_rollback_backup_path=str(pre_rb_bak) if pre_rb_bak else None,
                    )
                    self.transaction_manager.journal._trigger_fault(
                        "after_manual_rollback_step",
                        owning_tx.transaction_id,
                        op_id,
                    )

            for tx in tx_list:
                if tx.state == TransactionState.ROLLING_BACK.value:
                    self.transaction_manager.transition_transaction_state(
                        tx,
                        TransactionState.ROLLED_BACK,
                        reason="Rollback explícito completado y verificado con SHA-256.",
                    )
            self.transition_state(execution_id, ChainState.ROLLED_BACK)
            if record.plan is not None:
                record.operation_states = record.plan.get_operation_states()
            record.rollback_summary = {"restored_count": len(restored_files), "all_restored": True,
                                       "restored_files": list(restored_files)}
            return {
                "execution_id": execution_id,
                "state": record.state.value,
                "restored_files": restored_files,
            }

    # ------------------------------------------------------------------
    # Utilidades de extremo a extremo para consola interactiva (/optimizar)
    # ------------------------------------------------------------------

    @staticmethod
    def format_simulation_report(simulation: Mapping[str, Any]) -> str:
        """
        Formatea una simulación (`simulate_plan`) en una tabla legible para consola,
        mostrando cada operación, su valor original y propuesto, nivel de evidencia,
        riesgo, estrategia de backup y el `approval_hash` requerido.
        """
        lines: list[str] = [
            "======================================================================",
            f" SIMULACIÓN EN SOLO LECTURA — {simulation.get('application_name', 'Aplicación')}",
            f" Plan ID: {simulation.get('plan_id')} | Ejecución: {simulation.get('execution_id')}",
            f" Hash Canónico de Aprobación (SHA-256): {simulation.get('approval_hash')}",
            "----------------------------------------------------------------------",
        ]
        ops = simulation.get("operations_preview") or []
        for item in ops:
            sec_str = f"[{item.get('section')}] " if item.get("section") else ""
            lines.append(
                f" • [{item.get('operation_id')}] {sec_str}{item.get('key')}: "
                f"'{item.get('original_value')}' -> '{item.get('proposed_value')}' "
                f"| Evidencia: {item.get('evidence')} | Riesgo: {item.get('risk_level')}"
            )
            lines.append(f"   Motivo: {item.get('justification')}")
            lines.append(f"   Archivo: {item.get('file_path')}")
        lines.append("----------------------------------------------------------------------")
        lines.append(
            f" Total de operaciones propuestas: {simulation.get('total_operations', len(ops))}"
        )
        lines.append(
            f" Para revertir tras aplicar: /rollback {simulation.get('execution_id')}"
        )
        lines.append("======================================================================")
        return "\n".join(lines)

    def prepare_interactive_optimization(
        self,
        app_or_target: str,
        *,
        target_filename: Optional[str] = None,
        file_path: Optional[str | Path] = None,
        user_goal: Optional[str] = None,
        provider_callable: Optional[Callable[[str], str]] = None,
    ) -> dict[str, Any]:
        """
        Ejecuta las Fases 1 a 6 en modo de solo lectura para el comando interactivo `/optimizar`:
        1. Localiza el archivo de configuración dentro de las raíces autorizadas (o valida `file_path`).
        2. Inspecciona las claves actuales y detecta el hardware local.
        3. Investiga y clasifica optimizaciones (`VERIFIED` / `PLAUSIBLE`).
        4. Construye y valida el `ChangePlan` con `PolicyEngine`.
        5. Ejecuta `simulate_plan(plan)` dejando la ejecución en `AWAITING_APPROVAL`.
        """
        clean_target = (app_or_target or "").strip().strip('"').strip("'")
        goal = (
            user_goal
            or f"Optimizar configuración de {clean_target or 'la aplicación'} para GTX 1660 de 6 GB VRAM"
        )
        record = self.create_execution()
        exec_id = record.execution_id

        resolved_file: Optional[Path] = None
        app_name = clean_target or "Aplicación"

        # 1. Si se proporcionó ruta explícita o el argumento es una ruta existente autorizada
        candidate_raw = str(file_path).strip() if file_path else clean_target
        if candidate_raw and (
            os.path.isabs(candidate_raw) or candidate_raw.endswith((".ini", ".cfg", ".conf", ".json", ".xml", ".txt"))
        ):
            try:
                resolved_file = self.file_editor.validate_authorized_path(
                    candidate_raw, must_exist=True
                )
                app_name = resolved_file.parent.name or resolved_file.stem
            except Exception:
                resolved_file = None

        # 2. Si no era una ruta directa, buscamos de forma acotada en las raíces autorizadas
        if resolved_file is None:
            candidate_filenames = (
                [target_filename]
                if target_filename
                else [
                    "GameUserSettings.ini",
                    "Engine.ini",
                    "settings.ini",
                    "settings.json",
                    "config.ini",
                    "config.cfg",
                    "graphics.json",
                    "settings.xml",
                ]
            )
            for fname in candidate_filenames:
                if not fname:
                    continue
                disc = self.file_editor.discover_config_files_detailed(clean_target, fname)
                if disc.get("status") == "found" and disc.get("selected_path"):
                    resolved_file = Path(str(disc["selected_path"]))
                    break
                if disc.get("status") == "ambiguous":
                    self.transition_state(
                        exec_id,
                        ChainState.CANCELLED,
                        reason="Múltiples instalaciones detectadas (ambigüedad).",
                    )
                    return {
                        "status": "ambiguous",
                        "execution_id": exec_id,
                        "matches": disc.get("matches", []),
                        "message": str(disc.get("reason")),
                    }

        if resolved_file is None:
            self.transition_state(
                exec_id,
                ChainState.CANCELLED,
                reason="No se localizó un archivo de configuración autorizado.",
            )
            return {
                "status": "not_found",
                "execution_id": exec_id,
                "message": (
                    f"No se encontró un archivo de configuración autorizado para '{clean_target}', señor. "
                    "Puede indicar el nombre del juego o la ruta absoluta autorizada."
                ),
            }

        snap = self.inspect_target_config(resolved_file)
        hw = self.detect_hardware_context(goal)
        recs = self.investigate_optimizations(
            execution_id=exec_id,
            user_goal=goal,
            application_name=app_name,
            config_snapshot=snap,
            hardware_context=hw,
            provider_callable=provider_callable,
        )

        # Verificar si hay al menos una recomendación aplicable cuyo valor difiera del actual
        existing_keys = snap.get("keys", {})
        applicable = [
            r
            for r in recs
            if r.evidence in APPROVABLE_EVIDENCE_LEVELS
            and existing_keys.get((r.section, r.key)) != r.proposed_value
        ]
        if not applicable:
            self.transition_state(
                exec_id,
                ChainState.CANCELLED,
                reason="El archivo ya cuenta con los valores recomendados o no hay cambios verificados.",
            )
            return {
                "status": "no_changes_needed",
                "execution_id": exec_id,
                "file_path": str(resolved_file),
                "message": (
                    f"El archivo '{resolved_file.name}' ya posee valores óptimos o no requiere cambios "
                    "adicionales verificados, señor."
                ),
            }

        plan = self.build_and_validate_plan(
            execution_id=exec_id,
            user_goal=goal,
            application_name=app_name,
            config_snapshot=snap,
            hardware_context=hw,
            recommendations=recs,
        )
        sim = self.simulate_plan(plan)
        return {
            "status": "awaiting_approval",
            "execution_id": exec_id,
            "plan": plan,
            "simulation": sim,
            "formatted_preview": self.format_simulation_report(sim),
        }
