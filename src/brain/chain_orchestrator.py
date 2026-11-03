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

    @classmethod
    def _missing_(cls, value: object) -> Optional["OperationStatus"]:
        if isinstance(value, str) and value.strip().upper() == "EXECUTED":
            return cls.APPLIED
        return None


# Transiciones controladas por operación. Los estados APPLIED, FAILED y BLOCKED son terminales:
# ninguna operación en estado terminal puede volver a modificarse silenciosamente.
ALLOWED_OPERATION_STATUS_TRANSITIONS: dict[OperationStatus, set[OperationStatus]] = {
    OperationStatus.PENDING: {
        OperationStatus.APPLIED,
        OperationStatus.FAILED,
        OperationStatus.BLOCKED,
    },
    OperationStatus.APPLIED: set(),
    OperationStatus.FAILED: set(),
    OperationStatus.BLOCKED: set(),
}

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
    Mantiene su estado individual (`PENDING`, `APPLIED`, `FAILED`, `BLOCKED`) y bloquea
    modificaciones silenciosas sobre estados terminales.
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
    depends_on: list[str] = field(default_factory=list)
    status: str = field(default=OperationStatus.PENDING.value, compare=False)
    status_history: list[tuple[str, str, str, str]] = field(
        default_factory=list, repr=False, compare=False
    )
    executed_file_hash: Optional[str] = field(default=None, repr=False, compare=False)
    backup_path: Optional[str] = field(default=None, repr=False, compare=False)
    error_detail: Optional[str] = field(default=None, repr=False, compare=False)

    def __setattr__(self, name: str, value: Any) -> None:
        """
        Controla la asignación del atributo `status` para impedir que un estado terminal
        (`APPLIED`, `FAILED`, `BLOCKED`) sea sobrescrito silenciosamente.
        """
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

            if current_enum != OperationStatus.PENDING:
                raise PolicyValidationError(
                    f"Transición ilegal en operación '{getattr(self, 'id', '<op>')}': "
                    f"el estado terminal '{current_enum.value}' no puede modificarse a '{target_enum.value}'."
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

    def transition_status(
        self,
        new_status: OperationStatus | str,
        *,
        reason: str = "",
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
        return all(
            op.status == OperationStatus.APPLIED.value for op in self.operations
        )

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

        # Detección de operaciones contradictorias sobre la misma clave en el mismo archivo
        if seen_targets is not None:
            target_sig = (
                str(canonical_path).lower(),
                (op.section or "").strip(),
                (op.key or "__FULL_CONTENT__").strip(),
            )
            if target_sig in seen_targets and seen_targets[target_sig] != op.proposed_value:
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
        4. Ejecuta DFS de 3 colores (`0=no visitado`, `1=en curso`, `2=completado`) para detectar
           ciclos directos o transitivos (`A -> B -> A`) e informar la ruta exacta del ciclo.
        5. Ejecuta el algoritmo de Kahn con cola de prioridad (`heapq`) indexada por la posición
           original de cada operación para obtener un orden topológico 100% determinista que
           respeta el orden original entre operaciones independientes.
        6. Verifica la compatibilidad de estados entre cada operación y sus dependencias.
        """
        if not operations:
            raise PolicyValidationError(
                "El plan no contiene ninguna operación ejecutable."
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

        # Detección de ciclos mediante DFS de 3 estados (0: UNVISITED, 1: VISITING, 2: VISITED)
        visit_state: dict[str, int] = {op_id: 0 for op_id in op_by_id}
        dfs_stack: list[str] = []

        def _dfs_detect_cycle(curr_id: str) -> None:
            visit_state[curr_id] = 1
            dfs_stack.append(curr_id)
            # Recorremos las dependencias en orden determinista
            for dep_id in op_by_id[curr_id].depends_on:
                dep_clean = dep_id.strip()
                state = visit_state[dep_clean]
                if state == 1:
                    cycle_start = dfs_stack.index(dep_clean)
                    cycle_chain = dfs_stack[cycle_start:] + [dep_clean]
                    # Invertimos o mostramos el ciclo claro entre operaciones
                    cycle_str = " -> ".join(cycle_chain)
                    raise PolicyValidationError(
                        f"Ciclo de dependencias detectado en el plan: {cycle_str}."
                    )
                if state == 0:
                    _dfs_detect_cycle(dep_clean)
            dfs_stack.pop()
            visit_state[curr_id] = 2

        for op in operations:
            if visit_state[op.id] == 0:
                _dfs_detect_cycle(op.id)

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
            raise PolicyValidationError(
                "El grafo de dependencias contiene un ciclo y no admite orden topológico."
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
    ) -> None:
        self.file_editor: FileEditorSkill = file_editor or FileEditorSkill()
        self.policy_engine: PolicyEngine = PolicyEngine(self.file_editor)
        self.rollback_manager: RollbackManager = RollbackManager(self.file_editor)
        self.brain_manager = brain_manager
        self.skill_manager = skill_manager
        self.provider_timeout_sec = float(provider_timeout_sec)
        self.max_provider_response_chars = int(max_provider_response_chars)

        self._executions: dict[str, ChainExecutionRecord] = {}
        self._investigation_cache: dict[str, list[dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # Gestión de máquina de estados
    # ------------------------------------------------------------------

    def create_execution(self, execution_id: Optional[str] = None) -> ChainExecutionRecord:
        """Crea un nuevo registro de ejecución en estado inicial `RECEIVED`."""
        exec_id = execution_id or f"exec-{uuid.uuid4().hex[:12]}"
        now_iso = datetime.now(timezone.utc).isoformat()
        record = ChainExecutionRecord(
            execution_id=exec_id,
            state=ChainState.RECEIVED,
            state_history=[(ChainState.RECEIVED.value, now_iso)],
        )
        self._executions[exec_id] = record
        return record

    def get_execution(self, execution_id: str) -> ChainExecutionRecord:
        """Obtiene el registro de una ejecución existente."""
        if execution_id not in self._executions:
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
    ) -> OperationStatus:
        """
        Ejecuta una transición de estado sobre una `ChangeOperation` individual, sincroniza
        el mapa `record.operation_states` y deja rastro auditable en `AuditLogger`.
        """
        prev_status = op.status
        final_status = op.transition_status(new_status, reason=reason)
        record.operation_states[op.id] = final_status.value
        entry = {
            "operation_id": op.id,
            "from_status": prev_status,
            "to_status": final_status.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
        }
        record.operation_state_history.append(entry)
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
            self._verify_single_operation_on_disk(
                dep_op,
                expected_final_hash=dep_op.executed_file_hash,
            )
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
        if record.state in (ChainState.RECEIVED, ChainState.INVESTIGATING):
            self.transition_state(execution_id, ChainState.PLANNING)

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
        record.plan = plan

        self.transition_state(execution_id, ChainState.VALIDATING)
        try:
            self.policy_engine.validate_plan(plan)
        except (PolicyValidationError, PermissionError) as exc:
            self.transition_state(execution_id, ChainState.REJECTED, reason=str(exc))
            raise

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
        if record.state in (ChainState.RECEIVED, ChainState.INVESTIGATING):
            self.transition_state(plan.execution_id, ChainState.PLANNING)
        if record.state == ChainState.PLANNING:
            self.transition_state(plan.execution_id, ChainState.VALIDATING)
        elif record.state == ChainState.COMPLETED and not plan.is_fully_completed():
            self.transition_state(plan.execution_id, ChainState.VALIDATING)

        try:
            if selected_operation_ids is not None:
                self.preflight_validate(
                    plan,
                    approved_operation_ids=selected_operation_ids,
                    record=record,
                )
                record.bound_operation_ids = tuple(
                    str(item).strip() for item in selected_operation_ids
                )
            else:
                self.policy_engine.validate_plan(plan)
                record.bound_operation_ids = None
        except (PolicyValidationError, PermissionError) as exc:
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
        record.plan = plan
        record.approval_hash = approval_token
        record.approved_plan_id = plan.plan_id
        record.approved_plan_snapshot = json.loads(plan.canonical_json())
        for op in plan.operations:
            record.operation_states.setdefault(op.id, op.status)
        self.transition_state(plan.execution_id, ChainState.AWAITING_APPROVAL)

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
        record.bound_operation_ids = tuple(
            str(item).strip() for item in approved_operation_ids
        )
        return current_hash

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
        approved_operation_ids: Optional[Sequence[str]] = None,
        continue_independent: bool = False,
        auto_rollback: bool = True,
    ) -> dict[str, Any]:
        """
        Fases 7, 8, 9 y 10:
        - Fase 7 (Autorización y Pre-Flight):
          1. Verifica `user_approved=True`.
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

        record = self.get_execution(plan.execution_id)
        for op in plan.operations:
            record.operation_states.setdefault(op.id, op.status)

        if not user_approved:
            if record.state in ALLOWED_STATE_TRANSITIONS and ChainState.REJECTED in ALLOWED_STATE_TRANSITIONS[record.state]:
                self.transition_state(
                    plan.execution_id,
                    ChainState.REJECTED,
                    reason="Ejecución rechazada: falta confirmación explícita del usuario.",
                )
            raise ApprovalBindingError(
                "No se puede ejecutar el plan sin autorización explícita del usuario (user_approved=True)."
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
            normalized_req = tuple(str(x).strip() for x in approved_operation_ids)
            if normalized_req != record.bound_operation_ids:
                self.transition_state(
                    plan.execution_id,
                    ChainState.REJECTED,
                    reason="Las operaciones seleccionadas fueron modificadas después de aprobarlas.",
                )
                raise ApprovalBindingError(
                    "No se permite modificar las operaciones seleccionadas después de otorgar la aprobación."
                )

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
                    continue

            expected_hash = current_file_hashes.get(
                canonical_str, op.expected_file_hash
            )

            try:
                res = self._apply_single_operation(
                    op,
                    execution_id=plan.execution_id,
                    expected_file_hash=expected_hash,
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

                # La escritura fue verificada en disco: registramos estado APPLIED y su evidencia
                self._record_operation_transition(
                    record,
                    op,
                    OperationStatus.APPLIED,
                    reason="Operación aplicada y verificada en disco con SHA-256.",
                )
                object.__setattr__(op, "executed_file_hash", str(res["final_hash"]))
                object.__setattr__(op, "backup_path", str(res["backup_path"]))
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
                self._propagate_blocked_status(
                    op.id,
                    plan.operations,
                    record=record,
                    reason=err_str,
                )

        # Sincronizar mapa de estados finales de cada operación
        record.operation_states = plan.get_operation_states()

        # Si hubo cualquier operación fallida o bloqueada, el plan jamás puede declararse exitoso
        if has_failure_occurred:
            if auto_rollback and backups_this_run:
                rollback_notes = self._rollback_applied_records(
                    backups_this_run, execution_id=plan.execution_id
                )
                record.rollback_summary = rollback_notes
                target_chain_state = (
                    ChainState.ROLLED_BACK
                    if rollback_notes["all_restored"]
                    else ChainState.FAILED
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
            rollback_notes = self._rollback_applied_records(
                backups_this_run, execution_id=plan.execution_id
            )
            record.rollback_summary = rollback_notes
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
    ) -> dict[str, Any]:
        """Ejecuta una `ChangeOperation` individual a través de `FileEditorSkill.modify_ini_or_text`."""
        if op.operation_type == "replace_content":
            return self.file_editor.modify_ini_or_text(
                op.file_path,
                replacement_content=op.proposed_value,
                execution_id=execution_id,
                expected_file_hash=expected_file_hash,
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
            )

        if op.operation_type in ("update_key", "update_xml_node"):
            return self.file_editor.modify_ini_or_text(
                op.file_path,
                key_values={op.key or "": op.proposed_value},
                section=op.section,
                execution_id=execution_id,
                expected_file_hash=expected_file_hash,
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
        Si múltiples operaciones modificaron el mismo archivo sobre claves distintas,
        verifica el hash final consolidado del archivo y el valor individual de cada clave.
        """
        results: list[dict[str, Any]] = []
        for op in operations:
            canonical = self.file_editor.validate_authorized_path(op.file_path, must_exist=True)
            expected_hash = expected_final_hashes.get(str(canonical))
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
    ) -> dict[str, Any]:
        """
        Restaura en orden inverso (LIFO) los backups aplicados durante una ejecución,
        verificando `latest_rec.post_edit_sha256` para no sobrescribir jamás cambios
        legítimos que el usuario haya realizado externamente sobre el archivo.
        """
        restored = 0
        errors: list[str] = []
        for bkp in reversed(list(backups)):
            latest_rec = (
                self.file_editor.backup_manager.get_record_by_backup(bkp.backup_path)
                or bkp
            )
            ok = self.file_editor.rollback(
                backup_path=latest_rec.backup_path,
                target_path=latest_rec.target_path,
                expected_current_hash=latest_rec.post_edit_sha256,
                execution_id=f"{execution_id}_rollback",
            )
            if ok:
                restored += 1
            else:
                errors.extend(
                    self.file_editor.last_operation_result.get("errors")
                    or ["Fallo o conflicto de concurrencia durante rollback"]
                )
        return {
            "restored_count": restored,
            "all_restored": len(errors) == 0 and restored == len(backups),
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
        record = self.get_execution(execution_id)
        if not record.applied_backups:
            raise RuntimeError(
                f"La ejecución '{execution_id}' no registra backups aplicados para restaurar."
            )

        restored_files: list[str] = []
        for bkp in reversed(record.applied_backups):
            # Recargar registro actualizado desde BackupManager por si post_edit_sha256 fue actualizado
            latest_rec = self.file_editor.backup_manager.get_record_by_backup(bkp.backup_path) or bkp
            expected_curr = None if force else latest_rec.post_edit_sha256
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
                raise RuntimeError(errs)
            restored_files.append(latest_rec.target_path)

        self.transition_state(execution_id, ChainState.ROLLED_BACK)
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
