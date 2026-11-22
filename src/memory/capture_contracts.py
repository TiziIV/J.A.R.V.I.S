"""
Contratos inmutables y auditables de evidencia verificada para captura automática N8.3 Subfase 2.

Justificación técnica junior:
1. Un agente con LLM no puede promover texto conversacional, afirmaciones propias ("listo",
   "verified=True") ni estados declarativos aislados (`COMPLETED`) a memoria persistente de largo plazo.
2. Este módulo define contratos criptográficamente sellados (`VerifiedOperationReference` y
   `VerifiedExecutionEvidenceContract`) que exigen procedencia real de N5 (`ChainOrchestrator`)
   o N6 (`ExecutionSupervisor`), verificadores objetivos autorizados, referencias concretas a
   operaciones aplicadas y hashes SHA-256 deterministas tanto de la evidencia base (`evidence_sha256`)
   como del sobre completo del contrato (`contract_sha256`).
3. Rechaza campos desconocidos al deserializar, bloquea mutaciones tras la emisión (`frozen=True`)
   y filtra estrictamente transcripciones, prompts, contenido web crudo y secretos.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from types import MappingProxyType
from typing import Any, Mapping

from src.brain.exceptions import is_sensitive_key
from src.memory.models import (
    VALID_VERIFICATION_SOURCE_LAYERS,
    ExecutionVerificationEvidence,
    MemoryScopeType,
    MemoryValidationError,
    canonical_json,
    is_valid_sha256_hex,
    parse_iso_utc,
)
from src.memory.policy import assert_no_secrets, reject_malformed_or_untrusted_payload


CAPTURE_CONTRACT_VERSION: str = "2.0"
VALID_CAPTURE_CONTRACT_VERSIONS: frozenset[str] = frozenset({"2.0"})

# Verificadores objetivos reales permitidos por capa origen.
# Nunca se admiten modelos LLM, síntesis internas ni nombres genéricos sin verificador real.
AUTHORIZED_VERIFIERS_BY_LAYER: dict[str, frozenset[str]] = {
    "N5_CHAIN_ORCHESTRATOR": frozenset(
        {
            "ChainOrchestratorVerifier",
            "TransactionRecoveryVerifier",
        }
    ),
    "N6_EXECUTION_SUPERVISOR": frozenset(
        {
            "ExecutionSupervisorSkillVerifier",
            "ExecutionSupervisorChainVerifier",
            "ChainOrchestratorVerifier",
        }
    ),
}

# Marcadores de verificadores u orígenes especulativos que jamás deben aceptarse como evidencia objetiva.
FORBIDDEN_VERIFIER_MARKERS: tuple[str, ...] = (
    "llm",
    "ollama",
    "gemini",
    "openai",
    "claude",
    "gpt",
    "internal_synthesis",
    "ai_synthesis",
    "model_claim",
    "assistant_claim",
    "self_reported",
    "manual_bool",
    "unverified",
)

# Claves prohibidas en evidencia estructurada porque expondrían prompts, HTML crudo o razonamiento.
FORBIDDEN_EVIDENCE_KEYS: frozenset[str] = frozenset(
    {
        "raw_transcript",
        "transcript",
        "prompt",
        "system_prompt",
        "user_prompt",
        "assistant_response",
        "llm_response",
        "model_output",
        "internal_synthesis",
        "raw_html",
        "raw_web_content",
        "extracted_text",
        "web_text",
        "reasoning",
        "chain_of_thought",
        "verified",  # Impide suplantar evidencia real con un simple booleano {"verified": True}.
    }
)

# Patrones de contenido libre prohibido en hechos capturados automáticamente.
_FORBIDDEN_CAPTURE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("internal_synthesis", re.compile(r"\binternal_synthesis\b", re.IGNORECASE)),
    ("untrusted_web_envelope", re.compile(r"\buntrusted_external_content\b", re.IGNORECASE)),
    ("untrusted_system_envelope", re.compile(r"\buntrusted_system_context\b", re.IGNORECASE)),
    ("prompt_injection_marker", re.compile(r"<\s*(?:script|system|assistant|user)\b", re.IGNORECASE)),
    ("chain_of_thought_marker", re.compile(r"\b(?:chain_of_thought|system_prompt)\b", re.IGNORECASE)),
)

_ALLOWED_OPERATION_REF_FIELDS: frozenset[str] = frozenset(
    {
        "ref_id",
        "operation_type",
        "target_ref",
        "verification_status",
        "before_sha256",
        "after_sha256",
    }
)

_ALLOWED_CONTRACT_FIELDS: frozenset[str] = frozenset(
    {
        "contract_version",
        "source_layer",
        "source_execution_id",
        "canonical_unit_id",
        "source_goal_id",
        "source_plan_id",
        "source_task_id",
        "observed_final_state",
        "verification_outcome",
        "verifier_component",
        "executor_component",
        "operation_type",
        "expected_outcome",
        "fact_title",
        "fact_content",
        "fact_summary",
        "approval_hash",
        "verified_operation_refs",
        "verified_artifacts",
        "structured_evidence",
        "verified_at",
        "scope_type",
        "scope_key",
        "evidence_sha256",
        "contract_sha256",
    }
)

_N5_REQUIRED_STRUCTURED_KEYS: frozenset[str] = frozenset(
    {
        "execution_id",
        "plan_id",
        "plan_status",
        "approval_hash",
        "applied_count",
        "verified_count",
        "rollback_present",
    }
)

_N6_REQUIRED_STRUCTURED_KEYS: frozenset[str] = frozenset(
    {
        "goal_id",
        "plan_id",
        "task_id",
        "goal_status",
        "task_status",
        "evidence_kind",
    }
)


class MemoryEvidenceContractError(MemoryValidationError):
    """El contrato o evidencia recibida de N5/N6 no cumple los requisitos de integridad objetiva."""


def _freeze_text_scalar(value: Any, *, field_name: str, max_chars: int = 320) -> str:
    """Valida que un campo sea string no vacío, seguro y libre de NUL, HTML/transcripción o secretos."""
    if not isinstance(value, str):
        raise MemoryEvidenceContractError(f"El campo '{field_name}' debe ser string.")
    clean = value.strip()
    if not clean:
        raise MemoryEvidenceContractError(f"El campo '{field_name}' no puede estar vacío.")
    if len(clean) > max_chars:
        raise MemoryEvidenceContractError(
            f"El campo '{field_name}' excede el límite permitido ({len(clean)} > {max_chars})."
        )
    reject_malformed_or_untrusted_payload(clean, field_name=field_name)
    assert_no_secrets(clean, field_name=field_name)
    for label, pattern in _FORBIDDEN_CAPTURE_PATTERNS:
        if pattern.search(clean):
            raise MemoryEvidenceContractError(
                f"El campo '{field_name}' contiene marcador prohibido ({label})."
            )
    return clean


def _validate_target_ref(raw_ref: Any, *, field_name: str) -> str:
    """Valida una referencia de archivo o recurso operativo bloqueando UNC/traversal y secretos."""
    clean = _freeze_text_scalar(raw_ref, field_name=field_name, max_chars=260)
    if clean.startswith("\\\\") or clean.startswith("//"):
        raise MemoryEvidenceContractError(f"No se admiten rutas UNC/SMB en '{field_name}'.")
    parts = [p for p in re.split(r"[\\/]+", clean) if p]
    if ".." in parts:
        raise MemoryEvidenceContractError(f"No se admite path traversal '..' en '{field_name}'.")
    return clean


def _normalize_structured_evidence(
    raw: Any,
    *,
    source_layer: str,
) -> tuple[tuple[str, str], ...]:
    """
    Normaliza la evidencia estructurada mínima en tuplas inmutables ordenadas.

    Exige claves reales por capa e impide falsificaciones minimalistas o exposición de
    prompts, transcripciones o respuestas LLM.
    """
    if isinstance(raw, Mapping):
        pairs_raw = list(raw.items())
    elif isinstance(raw, (tuple, list)):
        pairs_raw = []
        for entry in raw:
            if not isinstance(entry, (tuple, list)) or len(entry) != 2:
                raise MemoryEvidenceContractError(
                    "structured_evidence debe contener pares (clave, valor) válidos."
                )
            pairs_raw.append((entry[0], entry[1]))
    else:
        raise MemoryEvidenceContractError(
            "structured_evidence debe ser un mapeo o secuencia de pares (clave, valor)."
        )

    if not pairs_raw:
        raise MemoryEvidenceContractError("structured_evidence no puede estar vacío.")

    seen: dict[str, str] = {}
    for raw_k, raw_v in pairs_raw:
        if not isinstance(raw_k, str) or not raw_k.strip():
            raise MemoryEvidenceContractError("Cada clave de structured_evidence debe ser string no vacío.")
        clean_k = raw_k.strip().lower()
        if len(clean_k) > 64:
            raise MemoryEvidenceContractError(f"Clave demasiado larga en structured_evidence: '{clean_k}'.")
        if clean_k in FORBIDDEN_EVIDENCE_KEYS or is_sensitive_key(clean_k):
            raise MemoryEvidenceContractError(
                f"Clave prohibida en structured_evidence por seguridad/integridad: '{clean_k}'."
            )
        if isinstance(raw_v, bool):
            str_v = "true" if raw_v else "false"
        elif isinstance(raw_v, (int, float)) and not isinstance(raw_v, bool):
            str_v = str(raw_v)
        elif isinstance(raw_v, str):
            str_v = raw_v.strip()
        else:
            raise MemoryEvidenceContractError(
                f"El valor de structured_evidence['{clean_k}'] debe ser escalar (str/int/float/bool)."
            )
        clean_v = _freeze_text_scalar(
            str_v,
            field_name=f"structured_evidence[{clean_k}]",
            max_chars=240,
        )
        seen[clean_k] = clean_v

    required = (
        _N5_REQUIRED_STRUCTURED_KEYS
        if source_layer == "N5_CHAIN_ORCHESTRATOR"
        else _N6_REQUIRED_STRUCTURED_KEYS
    )
    missing = sorted(required - set(seen.keys()))
    if missing:
        raise MemoryEvidenceContractError(
            f"structured_evidence incompleta para {source_layer}; faltan campos objetivos: {missing}."
        )

    if source_layer == "N5_CHAIN_ORCHESTRATOR":
        if seen.get("rollback_present") != "false":
            raise MemoryEvidenceContractError(
                "No se admite evidencia N5 cuando rollback_present no es 'false'."
            )
        if seen.get("plan_status") not in {"COMPLETED", "PARTIALLY_APPLIED"}:
            raise MemoryEvidenceContractError(
                f"plan_status N5 incompatible con captura verificada: {seen.get('plan_status')!r}."
            )
        try:
            if int(seen["applied_count"]) < 1 or int(seen["verified_count"]) < 1:
                raise MemoryEvidenceContractError(
                    "N5 requiere al menos una operación aplicada y verificada."
                )
        except ValueError as exc:
            raise MemoryEvidenceContractError("Contadores N5 inválidos en structured_evidence.") from exc

    if source_layer == "N6_EXECUTION_SUPERVISOR":
        if seen.get("goal_status") not in {"completed", "partial"}:
            raise MemoryEvidenceContractError(
                f"goal_status N6 no elegible para captura: {seen.get('goal_status')!r}."
            )
        if seen.get("task_status") != "completed":
            raise MemoryEvidenceContractError(
                "Solo se admite evidencia N6 de subtareas individuales en estado 'completed'."
            )
        ev_kind = seen.get("evidence_kind", "")
        if ev_kind not in {"skill_execution", "chain_orchestrator"}:
            raise MemoryEvidenceContractError(
                f"evidence_kind '{ev_kind}' no constituye verificación objetiva en N6."
            )

    return tuple(sorted(seen.items(), key=lambda item: item[0]))


@dataclass(frozen=True)
class VerifiedOperationReference:
    """Referencia inmutable y verificada a una operación concreta aplicada en N5 o N6."""

    ref_id: str
    operation_type: str
    target_ref: str
    verification_status: str = "VERIFIED"
    before_sha256: str | None = None
    after_sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "ref_id",
            _freeze_text_scalar(self.ref_id, field_name="operation_ref.ref_id", max_chars=96),
        )
        object.__setattr__(
            self,
            "operation_type",
            _freeze_text_scalar(
                self.operation_type,
                field_name="operation_ref.operation_type",
                max_chars=64,
            ),
        )
        object.__setattr__(
            self,
            "target_ref",
            _validate_target_ref(self.target_ref, field_name="operation_ref.target_ref"),
        )
        status = _freeze_text_scalar(
            self.verification_status,
            field_name="operation_ref.verification_status",
            max_chars=32,
        ).upper()
        if status != "VERIFIED":
            raise MemoryEvidenceContractError(
                f"VerifiedOperationReference exige verification_status='VERIFIED', recibido: {status!r}."
            )
        object.__setattr__(self, "verification_status", status)

        for hash_field in ("before_sha256", "after_sha256"):
            raw_h = getattr(self, hash_field)
            if raw_h is not None:
                if not isinstance(raw_h, str):
                    raise MemoryEvidenceContractError(f"'{hash_field}' debe ser string o None.")
                clean_h = raw_h.strip().lower()
                if clean_h and not is_valid_sha256_hex(clean_h):
                    raise MemoryEvidenceContractError(
                        f"'{hash_field}' debe ser un digest SHA-256 hexadecimal válido de 64 caracteres."
                    )
                object.__setattr__(self, hash_field, clean_h or None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref_id": self.ref_id,
            "operation_type": self.operation_type,
            "target_ref": self.target_ref,
            "verification_status": self.verification_status,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VerifiedOperationReference":
        if not isinstance(data, Mapping):
            raise MemoryEvidenceContractError("VerifiedOperationReference.from_dict requiere Mapping.")
        unknown = set(data.keys()) - _ALLOWED_OPERATION_REF_FIELDS
        if unknown:
            raise MemoryEvidenceContractError(
                f"Campos desconocidos en VerifiedOperationReference: {sorted(unknown)}"
            )
        required = {"ref_id", "operation_type", "target_ref", "verification_status"}
        missing = sorted(required - set(data.keys()))
        if missing:
            raise MemoryEvidenceContractError(
                f"Faltan campos obligatorios en VerifiedOperationReference: {missing}"
            )
        return cls(
            ref_id=data["ref_id"],
            operation_type=data["operation_type"],
            target_ref=data["target_ref"],
            verification_status=data["verification_status"],
            before_sha256=data.get("before_sha256"),
            after_sha256=data.get("after_sha256"),
        )


@dataclass(frozen=True)
class VerifiedExecutionEvidenceContract:
    """
    Contrato estricto, inmutable y autocontenido de evidencia verificada para captura automática N8.3.

    Justificación técnica junior:
    Extiende y sella criptográficamente el contrato de persistencia `ExecutionVerificationEvidence`
    agregando versión de contrato, estado final observado, verificación objetiva, verificador autorizado,
    evidencia estructurada mínima y referencias verificadas a operaciones concretas.
    """

    source_layer: str
    source_execution_id: str
    canonical_unit_id: str
    source_goal_id: str
    source_plan_id: str
    source_task_id: str
    observed_final_state: str
    verification_outcome: str
    verifier_component: str
    executor_component: str
    operation_type: str
    expected_outcome: str
    fact_title: str
    fact_content: str
    fact_summary: str
    verified_operation_refs: tuple[VerifiedOperationReference, ...]
    verified_artifacts: tuple[str, ...]
    structured_evidence: tuple[tuple[str, str], ...] | Mapping[str, Any]
    verified_at: str
    approval_hash: str | None = None
    scope_type: MemoryScopeType = MemoryScopeType.PROJECT
    scope_key: str = "default"
    contract_version: str = CAPTURE_CONTRACT_VERSION
    evidence_sha256: str = ""
    contract_sha256: str = ""

    def __post_init__(self) -> None:
        version = _freeze_text_scalar(
            self.contract_version,
            field_name="contract_version",
            max_chars=16,
        )
        if version not in VALID_CAPTURE_CONTRACT_VERSIONS:
            raise MemoryEvidenceContractError(f"Versión de contrato no soportada: {version!r}.")
        object.__setattr__(self, "contract_version", version)

        layer = _freeze_text_scalar(self.source_layer, field_name="source_layer", max_chars=48)
        if layer not in VALID_VERIFICATION_SOURCE_LAYERS:
            raise MemoryEvidenceContractError(
                f"source_layer no autorizado en contrato de captura: {layer!r}."
            )
        object.__setattr__(self, "source_layer", layer)

        for field_name, cap in (
            ("source_execution_id", 128),
            ("canonical_unit_id", 128),
            ("source_goal_id", 128),
            ("source_plan_id", 128),
            ("source_task_id", 128),
            ("executor_component", 96),
            ("operation_type", 64),
            ("expected_outcome", 240),
            ("fact_title", 120),
            ("fact_content", 900),
            ("fact_summary", 280),
        ):
            clean = _freeze_text_scalar(getattr(self, field_name), field_name=field_name, max_chars=cap)
            object.__setattr__(self, field_name, clean)

        verifier = _freeze_text_scalar(
            self.verifier_component,
            field_name="verifier_component",
            max_chars=96,
        )
        verifier_lower = verifier.lower()
        for bad_marker in FORBIDDEN_VERIFIER_MARKERS:
            if bad_marker in verifier_lower:
                raise MemoryEvidenceContractError(
                    f"Verificador no objetivo rechazado en contrato: '{verifier}'."
                )
        allowed_verifiers = AUTHORIZED_VERIFIERS_BY_LAYER.get(layer, frozenset())
        if verifier not in allowed_verifiers:
            raise MemoryEvidenceContractError(
                f"verifier_component '{verifier}' no está autorizado para {layer}."
            )
        object.__setattr__(self, "verifier_component", verifier)

        outcome = _freeze_text_scalar(
            self.verification_outcome,
            field_name="verification_outcome",
            max_chars=32,
        ).lower()
        if outcome != "verified":
            raise MemoryEvidenceContractError(
                f"verification_outcome debe ser 'verified', recibido: {outcome!r}."
            )
        object.__setattr__(self, "verification_outcome", outcome)

        observed = _freeze_text_scalar(
            self.observed_final_state,
            field_name="observed_final_state",
            max_chars=32,
        )
        if layer == "N5_CHAIN_ORCHESTRATOR":
            if observed != "COMPLETED":
                raise MemoryEvidenceContractError(
                    f"Estado observado inválido para N5_CHAIN_ORCHESTRATOR: {observed!r}."
                )
        else:
            observed = observed.lower()
            if observed not in {"completed", "partial"}:
                raise MemoryEvidenceContractError(
                    f"Estado observado inválido para N6_EXECUTION_SUPERVISOR: {observed!r}."
                )
        object.__setattr__(self, "observed_final_state", observed)

        if not isinstance(self.verified_at, str) or not self.verified_at.strip():
            raise MemoryEvidenceContractError("verified_at es obligatorio en el contrato.")
        parse_iso_utc(self.verified_at, field_name="verified_at")
        object.__setattr__(self, "verified_at", self.verified_at.strip())

        if self.approval_hash is not None:
            if not isinstance(self.approval_hash, str):
                raise MemoryEvidenceContractError("approval_hash debe ser string o None.")
            clean_app = self.approval_hash.strip().lower()
            if clean_app and not is_valid_sha256_hex(clean_app):
                raise MemoryEvidenceContractError("approval_hash debe ser SHA-256 hex válido de 64 chars.")
            object.__setattr__(self, "approval_hash", clean_app or None)
        if layer == "N5_CHAIN_ORCHESTRATOR" and not self.approval_hash:
            raise MemoryEvidenceContractError(
                "Todo contrato originado en N5_CHAIN_ORCHESTRATOR exige approval_hash SHA-256."
            )

        if not isinstance(self.verified_operation_refs, tuple):
            object.__setattr__(self, "verified_operation_refs", tuple(self.verified_operation_refs))
        if not self.verified_operation_refs or not all(
            isinstance(ref, VerifiedOperationReference) for ref in self.verified_operation_refs
        ):
            raise MemoryEvidenceContractError(
                "verified_operation_refs debe contener al menos una VerifiedOperationReference válida."
            )
        if layer == "N5_CHAIN_ORCHESTRATOR":
            for ref in self.verified_operation_refs:
                if not ref.after_sha256:
                    raise MemoryEvidenceContractError(
                        f"La referencia N5 '{ref.ref_id}' requiere after_sha256 post-escritura verificable."
                    )

        if not isinstance(self.verified_artifacts, tuple):
            object.__setattr__(self, "verified_artifacts", tuple(self.verified_artifacts))
        clean_artifacts = tuple(
            _validate_target_ref(item, field_name=f"verified_artifacts[{idx}]")
            for idx, item in enumerate(self.verified_artifacts)
        )
        if not clean_artifacts:
            raise MemoryEvidenceContractError("verified_artifacts no puede estar vacío.")
        object.__setattr__(self, "verified_artifacts", clean_artifacts)

        if layer == "N5_CHAIN_ORCHESTRATOR":
            artifact_set = set(clean_artifacts)
            for ref in self.verified_operation_refs:
                if ref.target_ref not in artifact_set:
                    raise MemoryEvidenceContractError(
                        f"Inconsistencia de artefactos N5: '{ref.target_ref}' no figura en verified_artifacts."
                    )

        frozen_struct = _normalize_structured_evidence(self.structured_evidence, source_layer=layer)
        object.__setattr__(self, "structured_evidence", frozen_struct)
        struct_map = dict(frozen_struct)
        if layer == "N5_CHAIN_ORCHESTRATOR":
            if struct_map["execution_id"] != self.source_execution_id:
                raise MemoryEvidenceContractError(
                    "Inconsistencia en structured_evidence.execution_id respecto a source_execution_id."
                )
            if struct_map["plan_id"] != self.source_plan_id:
                raise MemoryEvidenceContractError(
                    "Inconsistencia en structured_evidence.plan_id respecto a source_plan_id."
                )
            if struct_map["approval_hash"].lower() != (self.approval_hash or ""):
                raise MemoryEvidenceContractError(
                    "Inconsistencia en structured_evidence.approval_hash respecto a approval_hash."
                )
        else:
            if struct_map["goal_id"] != self.source_goal_id:
                raise MemoryEvidenceContractError(
                    "Inconsistencia en structured_evidence.goal_id respecto a source_goal_id."
                )
            if struct_map["plan_id"] != self.source_plan_id:
                raise MemoryEvidenceContractError(
                    "Inconsistencia en structured_evidence.plan_id respecto a source_plan_id."
                )
            if struct_map["task_id"] != self.source_task_id:
                raise MemoryEvidenceContractError(
                    "Inconsistencia en structured_evidence.task_id respecto a source_task_id."
                )
            if struct_map["goal_status"] != self.observed_final_state:
                raise MemoryEvidenceContractError(
                    "Inconsistencia entre observed_final_state y structured_evidence.goal_status."
                )

        if not isinstance(self.scope_type, MemoryScopeType):
            object.__setattr__(self, "scope_type", MemoryScopeType(self.scope_type))
        clean_scope_key = _freeze_text_scalar(self.scope_key, field_name="scope_key", max_chars=80).lower()
        if self.scope_type == MemoryScopeType.GLOBAL:
            clean_scope_key = "global"
        object.__setattr__(self, "scope_key", clean_scope_key)

        # Construimos y verificamos la evidencia persistible estándar de Subfase 1 sin mutarla.
        base_evidence = self.to_execution_verification_evidence()
        provided_ev_hash = (self.evidence_sha256 or "").strip().lower()
        if provided_ev_hash:
            if not is_valid_sha256_hex(provided_ev_hash) or provided_ev_hash != base_evidence.evidence_sha256:
                raise MemoryEvidenceContractError(
                    "evidence_sha256 del contrato no coincide con el digest canónico calculado."
                )
            object.__setattr__(self, "evidence_sha256", provided_ev_hash)
        else:
            object.__setattr__(self, "evidence_sha256", base_evidence.evidence_sha256)

        expected_contract_hash = self.compute_contract_digest()
        provided_contract_hash = (self.contract_sha256 or "").strip().lower()
        if provided_contract_hash:
            if not is_valid_sha256_hex(provided_contract_hash) or provided_contract_hash != expected_contract_hash:
                raise MemoryEvidenceContractError(
                    "contract_sha256 manipulado o incompatible con el contenido canónico del contrato."
                )
            object.__setattr__(self, "contract_sha256", provided_contract_hash)
        else:
            object.__setattr__(self, "contract_sha256", expected_contract_hash)

    @property
    def structured_evidence_view(self) -> Mapping[str, str]:
        """Vista inmutable de solo lectura de `structured_evidence`."""
        return MappingProxyType(dict(self.structured_evidence))

    def to_execution_verification_evidence(self) -> ExecutionVerificationEvidence:
        """Traduce este contrato al objeto `ExecutionVerificationEvidence` aceptado por N8 v2."""
        return ExecutionVerificationEvidence(
            source_layer=self.source_layer,
            source_execution_id=self.source_execution_id,
            source_plan_id=self.source_plan_id,
            source_goal_id=self.source_goal_id,
            source_task_id=self.source_task_id,
            approval_hash=self.approval_hash,
            executor_component=self.executor_component,
            operation_type=self.operation_type,
            execution_outcome=self.verification_outcome,
            expected_outcome=self.expected_outcome,
            verifier_component=self.verifier_component,
            verified_at=self.verified_at,
            verified_artifacts=self.verified_artifacts,
            scope_type=self.scope_type,
            scope_key=self.scope_key,
            canonical_unit_id=self.canonical_unit_id,
        )

    def canonical_contract_payload(self) -> dict[str, Any]:
        """Payload canónico determinista del contrato completo para cálculo de `contract_sha256`."""
        return {
            "contract_version": self.contract_version,
            "source_layer": self.source_layer,
            "source_execution_id": self.source_execution_id,
            "canonical_unit_id": self.canonical_unit_id,
            "source_goal_id": self.source_goal_id,
            "source_plan_id": self.source_plan_id,
            "source_task_id": self.source_task_id,
            "observed_final_state": self.observed_final_state,
            "verification_outcome": self.verification_outcome,
            "verifier_component": self.verifier_component,
            "executor_component": self.executor_component,
            "operation_type": self.operation_type,
            "expected_outcome": self.expected_outcome,
            "fact_title": self.fact_title,
            "fact_content": self.fact_content,
            "fact_summary": self.fact_summary,
            "approval_hash": self.approval_hash,
            "verified_operation_refs": [ref.to_dict() for ref in self.verified_operation_refs],
            "verified_artifacts": list(self.verified_artifacts),
            "structured_evidence": [[k, v] for k, v in self.structured_evidence],
            "verified_at": self.verified_at,
            "scope_type": self.scope_type.value,
            "scope_key": self.scope_key,
            "evidence_sha256": self.evidence_sha256,
        }

    def compute_contract_digest(self) -> str:
        """Calcula el digest SHA-256 determinista sobre el contrato sellado."""
        return hashlib.sha256(canonical_json(self.canonical_contract_payload()).encode("utf-8")).hexdigest()

    def verify_integrity(self) -> bool:
        """Verifica que tanto `evidence_sha256` como `contract_sha256` coincidan tras emisión."""
        expected_ev = self.to_execution_verification_evidence().evidence_sha256
        if self.evidence_sha256 != expected_ev:
            raise MemoryEvidenceContractError(
                "Integridad rota: evidence_sha256 no coincide con el payload canónico."
            )
        expected_contract = self.compute_contract_digest()
        if self.contract_sha256 != expected_contract:
            raise MemoryEvidenceContractError(
                "Integridad rota: contract_sha256 no coincide con el contrato sellado."
            )
        return True

    def to_dict(self) -> dict[str, Any]:
        payload = self.canonical_contract_payload()
        payload["contract_sha256"] = self.contract_sha256
        return payload

    def to_canonical_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VerifiedExecutionEvidenceContract":
        """Deserializa validando ausencia de campos desconocidos, integridad y hashes."""
        if not isinstance(data, Mapping):
            raise MemoryEvidenceContractError("VerifiedExecutionEvidenceContract.from_dict requiere Mapping.")
        unknown = set(data.keys()) - _ALLOWED_CONTRACT_FIELDS
        if unknown:
            raise MemoryEvidenceContractError(
                f"Campos desconocidos no permitidos en el contrato de evidencia: {sorted(unknown)}"
            )
        required = {
            "source_layer",
            "source_execution_id",
            "canonical_unit_id",
            "source_goal_id",
            "source_plan_id",
            "source_task_id",
            "observed_final_state",
            "verification_outcome",
            "verifier_component",
            "executor_component",
            "operation_type",
            "expected_outcome",
            "fact_title",
            "fact_content",
            "fact_summary",
            "verified_operation_refs",
            "verified_artifacts",
            "structured_evidence",
            "verified_at",
        }
        missing = sorted(required - set(data.keys()))
        if missing:
            raise MemoryEvidenceContractError(
                f"Faltan campos obligatorios en el contrato de evidencia: {missing}"
            )
        raw_refs = data.get("verified_operation_refs")
        if not isinstance(raw_refs, (list, tuple)) or not raw_refs:
            raise MemoryEvidenceContractError(
                "verified_operation_refs debe ser una lista no vacía en el contrato."
            )
        refs = tuple(
            item if isinstance(item, VerifiedOperationReference) else VerifiedOperationReference.from_dict(item)
            for item in raw_refs
        )
        raw_artifacts = data.get("verified_artifacts")
        if not isinstance(raw_artifacts, (list, tuple)) or not raw_artifacts:
            raise MemoryEvidenceContractError("verified_artifacts debe ser una secuencia no vacía.")

        return cls(
            contract_version=data.get("contract_version", CAPTURE_CONTRACT_VERSION),
            source_layer=data["source_layer"],
            source_execution_id=data["source_execution_id"],
            canonical_unit_id=data["canonical_unit_id"],
            source_goal_id=data["source_goal_id"],
            source_plan_id=data["source_plan_id"],
            source_task_id=data["source_task_id"],
            observed_final_state=data["observed_final_state"],
            verification_outcome=data["verification_outcome"],
            verifier_component=data["verifier_component"],
            executor_component=data["executor_component"],
            operation_type=data["operation_type"],
            expected_outcome=data["expected_outcome"],
            fact_title=data["fact_title"],
            fact_content=data["fact_content"],
            fact_summary=data["fact_summary"],
            approval_hash=data.get("approval_hash"),
            verified_operation_refs=refs,
            verified_artifacts=tuple(raw_artifacts),
            structured_evidence=data["structured_evidence"],
            verified_at=data["verified_at"],
            scope_type=data.get("scope_type", MemoryScopeType.PROJECT),
            scope_key=data.get("scope_key", "default"),
            evidence_sha256=data.get("evidence_sha256", ""),
            contract_sha256=data.get("contract_sha256", ""),
        )


@dataclass(frozen=True)
class AdaptationReport:
    """Resultado explícito de adaptar una ejecución N5 o N6 hacia contratos N8."""

    source_layer: str
    source_execution_id: str
    contracts: tuple[VerifiedExecutionEvidenceContract, ...]
    accepted_unit_ids: tuple[str, ...] = ()
    skipped_units: tuple[tuple[str, str], ...] = ()
    rejected_units: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class CaptureUnitOutcome:
    """Resultado individual de intentar persistir un contrato en N8 mediante `ExperienceCaptureService`."""

    canonical_unit_id: str
    source_execution_id: str
    status: str  # captured | deduplicated | rejected | idempotency_conflict | blocked_private_mode | disabled | invalidated_execution | failed
    memory_id: str | None = None
    idempotency_key: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class CaptureBatchResult:
    """Resumen completo y auditable de una operación de captura automática en `ExperienceCaptureService`."""

    ok: bool
    source_layer: str
    execution_id: str
    status: str
    captured_count: int = 0
    deduplicated_count: int = 0
    rejected_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    memory_ids: tuple[str, ...] = ()
    outcomes: tuple[CaptureUnitOutcome, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class RollbackInvalidationOutcome:
    """Resultado auditable del hook de invalidación en N8 tras confirmación real de rollback N5."""

    execution_id: str
    ok: bool
    status: str  # invalidated | idempotent_noop | tombstone_marked | blocked_private_mode | disabled | not_confirmed | failed
    invalidated_count: int = 0
    invalidated_memory_ids: tuple[str, ...] = ()
    already_inactive_count: int = 0
    store_version_token: str = ""
    error: str | None = None
