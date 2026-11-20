"""
Modelos tipados, enumeraciones, excepciones y contratos base para N8.1 (Memoria a Largo Plazo).

Justificación técnica junior:
Mantener los modelos separados de SQLite y de las políticas de validación garantiza
que cualquier componente futuro (N8.2 recuperación, N8.3 captura automática) trabaje
con estructuras deterministas, inmutables y auditables sin acoplarse a detalles internos
de la base de datos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
import re
from typing import Any
from uuid import uuid4


# Regex estricta para digitos hexadecimales SHA-256 (64 caracteres en minusculas).
_SHA256_HEX_RE: re.Pattern[str] = re.compile(r"^[0-9a-f]{64}$")

# Capas autorizadas para emitir evidencia de ejecucion verificada (contrato C2/EXECUTION_VERIFIED).
VALID_VERIFICATION_SOURCE_LAYERS: frozenset[str] = frozenset(
    {
        "N5_CHAIN_ORCHESTRATOR",
        "N6_EXECUTION_SUPERVISOR",
    }
)


class MemoryCategory(str, Enum):
    """Taxonomía cerrada de recuerdos de largo plazo en N8."""

    FACT = "FACT"
    PREFERENCE = "PREFERENCE"
    DECISION = "DECISION"
    EXPERIENCE = "EXPERIENCE"
    # Reservada por diseño; desactivada para ingesta en N8.1 para evitar inferencias especulativas.
    INFERENCE = "INFERENCE"


class MemorySource(str, Enum):
    """Origen autorizado de un recuerdo persistido en N8."""

    USER_EXPLICIT = "USER_EXPLICIT"
    DECISION_CONFIRMED = "DECISION_CONFIRMED"
    EXECUTION_VERIFIED = "EXECUTION_VERIFIED"


class MemoryStatus(str, Enum):
    """Ciclo de vida explícito de cada recuerdo."""

    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


class MemoryScopeType(str, Enum):
    """Ámbito de aplicabilidad para evitar mezclar contexto global con proyectos/juegos específicos."""

    GLOBAL = "GLOBAL"
    PROJECT = "PROJECT"
    APP_OR_GAME = "APP_OR_GAME"


class MemoryAuditAction(str, Enum):
    """Acciones registradas en la bitácora append-only `memory_audit`."""

    CREATE = "CREATE"
    REVISE = "REVISE"
    REVOKE = "REVOKE"
    EXPIRE = "EXPIRE"
    SUPERSEDE = "SUPERSEDE"
    DEDUPE_HIT = "DEDUPE_HIT"
    INTEGRITY_CHECK = "INTEGRITY_CHECK"
    MIGRATE = "MIGRATE"
    REJECT_PRIVATE_MODE = "REJECT_PRIVATE_MODE"


# Máquina de estados permitida: REVOKED es terminal; nunca se reactiva un recuerdo revocado.
ALLOWED_MEMORY_TRANSITIONS: dict[MemoryStatus, frozenset[MemoryStatus]] = {
    MemoryStatus.ACTIVE: frozenset(
        {MemoryStatus.SUPERSEDED, MemoryStatus.REVOKED, MemoryStatus.EXPIRED}
    ),
    MemoryStatus.SUPERSEDED: frozenset({MemoryStatus.REVOKED}),
    MemoryStatus.REVOKED: frozenset(),
    MemoryStatus.EXPIRED: frozenset({MemoryStatus.REVOKED}),
}


# ------------------------------------------------------------------------------
# Excepciones tipadas de N8 (evitamos sombrear el built-in `MemoryError` de Python)
# ------------------------------------------------------------------------------


class MemorySubsystemError(Exception):
    """Excepción base del subsistema de memoria a largo plazo N8."""


class MemoryValidationError(MemorySubsystemError, ValueError):
    """Los datos de entrada, timestamps o contratos estructurales son inválidos."""


class MemoryPolicyError(MemorySubsystemError):
    """Una regla de seguridad, capacidad, secreto o elegibilidad rechazó la operación."""


class MemoryPrivateModeError(MemoryPolicyError):
    """Se bloqueó una operación de escritura porque el modo privado está activo."""


class MemoryPersistenceError(MemorySubsystemError):
    """Error al inicializar, leer, migrar o confirmar transacciones en SQLite."""


class MemoryLockTimeoutError(MemoryPersistenceError, TimeoutError):
    """No se pudo adquirir el lock de concurrencia `ExperienceStoreLock` dentro del tiempo límite."""


class MemoryCorruptionError(MemoryPersistenceError):
    """La base SQLite falló verificación de integridad estructural o llaves foráneas."""


class MemoryDuplicateError(MemoryPolicyError):
    """Conflicto explícito de duplicación al intentar actualizar/revisar contra otro recuerdo activo."""


class MemoryNotFoundError(MemorySubsystemError, LookupError):
    """El identificador de recuerdo solicitado no existe en el almacén."""


class MemoryTransitionError(MemoryPolicyError):
    """Transición de ciclo de vida no permitida por la máquina de estados N8."""


class MemoryBackupError(MemoryPersistenceError):
    """Fallo al crear, verificar o retener snapshots de respaldo N8."""


class MemoryRestoreError(MemoryPersistenceError):
    """Fallo de integridad, hash o reemplazo atómico durante una restauración."""


# ------------------------------------------------------------------------------
# Utilidades deterministas de tiempo, IDs y serialización
# ------------------------------------------------------------------------------


def utc_now_iso() -> str:
    """
    Devuelve la marca temporal UTC actual en formato ISO-8601 con sufijo 'Z'.

    Conservamos precisión de microsegundos para preservar el orden cronológico exacto
    cuando ocurren múltiples revisiones o eventos dentro del mismo segundo.
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso_utc(raw: str, *, field_name: str = "timestamp") -> datetime:
    """Valida e interpreta un timestamp ISO-8601 con zona horaria explícita."""
    if not isinstance(raw, str) or not raw.strip():
        raise MemoryValidationError(f"El campo '{field_name}' debe ser un timestamp ISO-8601 no vacío.")
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise MemoryValidationError(f"Timestamp ISO-8601 inválido en '{field_name}': {raw!r}") from exc
    if parsed.tzinfo is None:
        raise MemoryValidationError(
            f"El campo '{field_name}' debe incluir zona horaria explícita (UTC 'Z' u offset)."
        )
    return parsed.astimezone(timezone.utc)


def generate_memory_id(prefix: str = "mem") -> str:
    """Genera identificadores únicos legibles y sin información sensible."""
    clean_prefix = (prefix or "mem").strip().lower()
    if not re.match(r"^[a-z0-9_]{1,12}$", clean_prefix):
        raise MemoryValidationError(f"Prefijo de ID inválido: {prefix!r}")
    return f"{clean_prefix}_{uuid4().hex[:20]}"


def canonical_json(data: Any) -> str:
    """Serializa estructuras de datos de forma canónica y determinista (orden estable de claves)."""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def is_valid_sha256_hex(value: Any) -> bool:
    """Verifica si un valor corresponde exactamente a un hash SHA-256 hexadecimal de 64 caracteres."""
    return isinstance(value, str) and bool(_SHA256_HEX_RE.match(value.strip().lower()))


# ------------------------------------------------------------------------------
# Modelos de procedencia, evidencia verificada, recuerdos y auditoría
# ------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionVerificationEvidence:
    """
    Evidencia estructurada obligatoria para ingerir recuerdos con origen EXECUTION_VERIFIED.

    Justificación técnica junior:
    Un recuerdo automático basado en ejecución no puede inventarse a partir de texto libre
    de un LLM. Exigir identificadores reales de N5/N6, resultado 'verified', artefactos
    verificados y, en el caso de N5, `approval_hash`, impide almacenar alucinaciones como hechos.
    """

    source_layer: str
    source_execution_id: str
    source_plan_id: str
    source_goal_id: str
    source_task_id: str
    approval_hash: str | None
    executor_component: str
    operation_type: str
    execution_outcome: str
    expected_outcome: str
    verifier_component: str
    verified_at: str
    verified_artifacts: tuple[str, ...]
    scope_type: MemoryScopeType = MemoryScopeType.PROJECT
    scope_key: str = "default"
    evidence_sha256: str = ""

    def __post_init__(self) -> None:
        layer = (self.source_layer or "").strip()
        if layer not in VALID_VERIFICATION_SOURCE_LAYERS:
            raise MemoryValidationError(
                f"source_layer no autorizado para evidencia verificada: {self.source_layer!r}"
            )
        object.__setattr__(self, "source_layer", layer)

        for attr_name in (
            "source_execution_id",
            "source_plan_id",
            "source_goal_id",
            "source_task_id",
            "executor_component",
            "operation_type",
            "expected_outcome",
            "verifier_component",
        ):
            raw_val = getattr(self, attr_name)
            if not isinstance(raw_val, str) or not raw_val.strip():
                raise MemoryValidationError(
                    f"ExecutionVerificationEvidence.{attr_name} es obligatorio y no puede estar vacío."
                )
            object.__setattr__(self, attr_name, raw_val.strip())

        outcome = (self.execution_outcome or "").strip().lower()
        if outcome != "verified":
            raise MemoryValidationError(
                "Solo se admite evidencia con execution_outcome='verified'."
            )
        object.__setattr__(self, "execution_outcome", outcome)

        parse_iso_utc(self.verified_at, field_name="verified_at")
        object.__setattr__(self, "verified_at", self.verified_at.strip())

        if self.approval_hash is not None:
            clean_hash = str(self.approval_hash).strip().lower()
            if not clean_hash:
                clean_hash = ""
            if clean_hash and not is_valid_sha256_hex(clean_hash):
                raise MemoryValidationError(
                    "approval_hash debe ser un SHA-256 hexadecimal válido de 64 caracteres."
                )
            object.__setattr__(self, "approval_hash", clean_hash or None)

        if layer == "N5_CHAIN_ORCHESTRATOR" and not self.approval_hash:
            raise MemoryValidationError(
                "La evidencia originada en N5_CHAIN_ORCHESTRATOR requiere approval_hash SHA-256."
            )

        if not isinstance(self.verified_artifacts, tuple):
            object.__setattr__(self, "verified_artifacts", tuple(self.verified_artifacts))
        clean_artifacts = tuple(
            str(item).strip() for item in self.verified_artifacts if isinstance(item, str) and item.strip()
        )
        if not clean_artifacts or len(clean_artifacts) != len(self.verified_artifacts):
            raise MemoryValidationError(
                "verified_artifacts debe contener al menos un artefacto válido y no vacío."
            )
        object.__setattr__(self, "verified_artifacts", clean_artifacts)

        if not isinstance(self.scope_type, MemoryScopeType):
            object.__setattr__(self, "scope_type", MemoryScopeType(self.scope_type))
        if not isinstance(self.scope_key, str) or not self.scope_key.strip():
            raise MemoryValidationError("scope_key de evidencia verificada no puede estar vacío.")
        object.__setattr__(self, "scope_key", self.scope_key.strip())

        expected_digest = self.compute_evidence_digest()
        provided_digest = (self.evidence_sha256 or "").strip().lower()
        if provided_digest:
            if not is_valid_sha256_hex(provided_digest):
                raise MemoryValidationError("evidence_sha256 inválido en ExecutionVerificationEvidence.")
            if provided_digest != expected_digest:
                raise MemoryValidationError(
                    "evidence_sha256 no coincide con el contenido canónico de la evidencia."
                )
            object.__setattr__(self, "evidence_sha256", provided_digest)
        else:
            object.__setattr__(self, "evidence_sha256", expected_digest)

    def canonical_payload(self) -> dict[str, Any]:
        """Devuelve el payload estructural utilizado para firma/huella determinista."""
        return {
            "source_layer": self.source_layer,
            "source_execution_id": self.source_execution_id,
            "source_plan_id": self.source_plan_id,
            "source_goal_id": self.source_goal_id,
            "source_task_id": self.source_task_id,
            "approval_hash": self.approval_hash,
            "executor_component": self.executor_component,
            "operation_type": self.operation_type,
            "execution_outcome": self.execution_outcome,
            "expected_outcome": self.expected_outcome,
            "verifier_component": self.verifier_component,
            "verified_at": self.verified_at,
            "verified_artifacts": list(self.verified_artifacts),
            "scope_type": self.scope_type.value,
            "scope_key": self.scope_key,
        }

    def compute_evidence_digest(self) -> str:
        """Calcula el SHA-256 determinista del contrato de evidencia sin mutar el objeto."""
        import hashlib

        return hashlib.sha256(canonical_json(self.canonical_payload()).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Serialización JSON completa y determinista de la evidencia verificada."""
        data = self.canonical_payload()
        data["evidence_sha256"] = self.evidence_sha256
        return data


@dataclass(frozen=True)
class MemoryProvenance:
    """Metadatos de procedencia y referencias externas hacia N5/N6/usuario."""

    source: MemorySource
    source_layer: str = "N8"
    source_goal_id: str | None = None
    source_plan_id: str | None = None
    source_task_id: str | None = None
    source_execution_id: str | None = None
    approval_hash: str | None = None
    verifier_component: str | None = None
    verified_at: str | None = None
    evidence_hash: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, MemorySource):
            object.__setattr__(self, "source", MemorySource(self.source))
        layer = (self.source_layer or "N8").strip()
        if not layer:
            raise MemoryValidationError("MemoryProvenance.source_layer no puede estar vacío.")
        object.__setattr__(self, "source_layer", layer)

        for opt_field in (
            "source_goal_id",
            "source_plan_id",
            "source_task_id",
            "source_execution_id",
            "verifier_component",
        ):
            val = getattr(self, opt_field)
            if val is not None:
                clean = str(val).strip()
                object.__setattr__(self, opt_field, clean or None)

        if self.verified_at is not None and str(self.verified_at).strip():
            parse_iso_utc(str(self.verified_at), field_name="provenance.verified_at")
            object.__setattr__(self, "verified_at", str(self.verified_at).strip())
        else:
            object.__setattr__(self, "verified_at", None)

        for hash_field in ("approval_hash", "evidence_hash"):
            hval = getattr(self, hash_field)
            if hval is not None and str(hval).strip():
                clean_h = str(hval).strip().lower()
                if not is_valid_sha256_hex(clean_h):
                    raise MemoryValidationError(
                        f"MemoryProvenance.{hash_field} debe ser un hash SHA-256 de 64 hex."
                    )
                object.__setattr__(self, hash_field, clean_h)
            else:
                object.__setattr__(self, hash_field, None)

    def to_dict(self) -> dict[str, Any]:
        """Serialización determinista de los metadatos de procedencia."""
        return {
            "source": self.source.value,
            "source_layer": self.source_layer,
            "source_goal_id": self.source_goal_id,
            "source_plan_id": self.source_plan_id,
            "source_task_id": self.source_task_id,
            "source_execution_id": self.source_execution_id,
            "approval_hash": self.approval_hash,
            "verifier_component": self.verifier_component,
            "verified_at": self.verified_at,
            "evidence_hash": self.evidence_hash,
        }


@dataclass(frozen=True)
class MemoryCreateInput:
    """Contrato de entrada validado para solicitar el alta de un recuerdo en N8.1."""

    category: MemoryCategory
    source: MemorySource
    title: str
    content: str
    summary: str | None = None
    scope_type: MemoryScopeType = MemoryScopeType.GLOBAL
    scope_key: str = "global"
    confidence: float = 0.85
    importance: int = 50
    tags: tuple[str, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()
    expires_at: str | None = None
    provenance: MemoryProvenance | None = None
    verification_evidence: ExecutionVerificationEvidence | None = None
    pending_confirmation: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.category, MemoryCategory):
            object.__setattr__(self, "category", MemoryCategory(self.category))
        if not isinstance(self.source, MemorySource):
            object.__setattr__(self, "source", MemorySource(self.source))
        if not isinstance(self.scope_type, MemoryScopeType):
            object.__setattr__(self, "scope_type", MemoryScopeType(self.scope_type))
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
            raise MemoryValidationError("confidence debe ser un número flotante entre 0.0 y 1.0.")
        conf = float(self.confidence)
        if not (0.0 <= conf <= 1.0):
            raise MemoryValidationError(f"confidence fuera de rango [0.0, 1.0]: {conf}")
        object.__setattr__(self, "confidence", conf)

        if isinstance(self.importance, bool) or not isinstance(self.importance, int):
            raise MemoryValidationError("importance debe ser un entero entre 1 y 100.")
        if not (1 <= self.importance <= 100):
            raise MemoryValidationError(f"importance fuera de rango [1, 100]: {self.importance}")

        if not isinstance(self.tags, tuple):
            object.__setattr__(self, "tags", tuple(self.tags))

        if not isinstance(self.metadata, tuple):
            if isinstance(self.metadata, dict):
                object.__setattr__(
                    self,
                    "metadata",
                    tuple((str(k), str(v)) for k, v in sorted(self.metadata.items())),
                )
            else:
                object.__setattr__(self, "metadata", tuple(self.metadata))

        if self.expires_at is not None and str(self.expires_at).strip():
            parse_iso_utc(str(self.expires_at), field_name="expires_at")
            object.__setattr__(self, "expires_at", str(self.expires_at).strip())
        else:
            object.__setattr__(self, "expires_at", None)


@dataclass(frozen=True)
class MemoryRecord:
    """Representación inmutable de un recuerdo persistido en `memories`."""

    memory_id: str
    category: MemoryCategory
    source: MemorySource
    status: MemoryStatus
    scope_type: MemoryScopeType
    scope_key: str
    title: str
    content: str
    normalized_text: str
    summary: str
    confidence: float
    importance: int
    revision_number: int
    content_fingerprint: str
    tags: tuple[str, ...]
    metadata: tuple[tuple[str, str], ...]
    provenance: MemoryProvenance
    created_at: str
    updated_at: str
    expires_at: str | None = None
    revoked_at: str | None = None
    superseded_by: str | None = None
    last_verified_at: str | None = None
    access_count: int = 0

    @property
    def is_pending_confirmation(self) -> bool:
        """Indica si el recuerdo aguarda confirmación explícita antes de participar en recuperación normal."""
        return dict(self.metadata).get("confirmation_state") == "pending"

    def to_dict(self) -> dict[str, Any]:
        """Serializa el recuerdo completo de manera determinista."""
        return {
            "memory_id": self.memory_id,
            "category": self.category.value,
            "source": self.source.value,
            "status": self.status.value,
            "scope_type": self.scope_type.value,
            "scope_key": self.scope_key,
            "title": self.title,
            "content": self.content,
            "normalized_text": self.normalized_text,
            "summary": self.summary,
            "confidence": self.confidence,
            "importance": self.importance,
            "revision_number": self.revision_number,
            "content_fingerprint": self.content_fingerprint,
            "tags": list(self.tags),
            "metadata": {k: v for k, v in self.metadata},
            "provenance": self.provenance.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "revoked_at": self.revoked_at,
            "superseded_by": self.superseded_by,
            "last_verified_at": self.last_verified_at,
            "access_count": self.access_count,
        }

    def to_canonical_json(self) -> str:
        """Representación JSON canónica del recuerdo."""
        return canonical_json(self.to_dict())


@dataclass(frozen=True)
class MemoryRevision:
    """Registro inmutable de una modificación histórica en `memory_revisions`."""

    revision_id: str
    memory_id: str
    revision_number: int
    previous_title: str
    previous_content: str
    previous_summary: str
    previous_fingerprint: str
    new_title: str
    new_content: str
    new_summary: str
    new_fingerprint: str
    change_reason: str
    changed_by: str
    changed_at: str
    source: MemorySource

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "memory_id": self.memory_id,
            "revision_number": self.revision_number,
            "previous_title": self.previous_title,
            "previous_content": self.previous_content,
            "previous_summary": self.previous_summary,
            "previous_fingerprint": self.previous_fingerprint,
            "new_title": self.new_title,
            "new_content": self.new_content,
            "new_summary": self.new_summary,
            "new_fingerprint": self.new_fingerprint,
            "change_reason": self.change_reason,
            "changed_by": self.changed_by,
            "changed_at": self.changed_at,
            "source": self.source.value,
        }


@dataclass(frozen=True)
class MemoryAuditEntry:
    """Entrada de auditoría inmutable en `memory_audit`."""

    audit_id: str
    action: MemoryAuditAction
    memory_id: str | None
    actor: str
    reason: str
    details_json: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "action": self.action.value,
            "memory_id": self.memory_id,
            "actor": self.actor,
            "reason": self.reason,
            "details_json": self.details_json,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class MemoryWriteResult:
    """Resultado explícito de una operación de creación/deduplicación en `ExperienceStore`."""

    record: MemoryRecord
    created: bool
    deduplicated: bool
    matched_reason: str | None = None


@dataclass(frozen=True)
class MemoryQueryFilter:
    """Filtro tipado de listado para `ExperienceStore` (preparado también para N8.2)."""

    status: MemoryStatus | None = MemoryStatus.ACTIVE
    category: MemoryCategory | None = None
    source: MemorySource | None = None
    scope_type: MemoryScopeType | None = None
    scope_key: str | None = None
    limit: int = 100
    offset: int = 0


@dataclass(frozen=True)
class MemorySearchCandidate:
    """
    Candidato crudo retornado por la primitiva de búsqueda de almacenamiento.

    Justificación técnica junior:
    N8.1 no implementa ranking ni presupuesto de tokens (eso pertenece a N8.2 `MemoryRetriever`),
    pero sí entrega el candidato recuperado y el backend usado (`fts5` o `sql_like`) para
    validar FTS5 y su respaldo SQL.
    """

    record: MemoryRecord
    match_backend: str
    raw_score: float


@dataclass(frozen=True)
class IntegrityReport:
    """Resultado estructurado de la validación de integridad de SQLite, FKs y FTS5."""

    ok: bool
    integrity_check_result: str
    foreign_keys_ok: bool
    fts_available: bool
    fts_active: bool
    fts_sync_ok: bool
    schema_version: int
    details: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "integrity_check_result": self.integrity_check_result,
            "foreign_keys_ok": self.foreign_keys_ok,
            "fts_available": self.fts_available,
            "fts_active": self.fts_active,
            "fts_sync_ok": self.fts_sync_ok,
            "schema_version": self.schema_version,
            "details": self.details,
        }


@dataclass(frozen=True)
class StoreStats:
    """Contadores operativos del almacén `experiences.db`."""

    db_path: str
    schema_version: int
    fts_available: bool
    fts_active: bool
    journal_mode: str
    total_memories: int
    active_memories: int
    superseded_memories: int
    revoked_memories: int
    expired_memories: int
    total_revisions: int
    total_audit_entries: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_path": self.db_path,
            "schema_version": self.schema_version,
            "fts_available": self.fts_available,
            "fts_active": self.fts_active,
            "journal_mode": self.journal_mode,
            "total_memories": self.total_memories,
            "active_memories": self.active_memories,
            "superseded_memories": self.superseded_memories,
            "revoked_memories": self.revoked_memories,
            "expired_memories": self.expired_memories,
            "total_revisions": self.total_revisions,
            "total_audit_entries": self.total_audit_entries,
        }


# ------------------------------------------------------------------------------
# Contratos N8.2: recuperación determinista, purga física, backups e importación
# ------------------------------------------------------------------------------


class MemoryRetrievalTarget(str, Enum):
    """Canal destino que determina el presupuesto máximo de resultados y texto."""

    NORMAL = "NORMAL"
    VOICE = "VOICE"
    LOCAL_3B = "LOCAL_3B"


@dataclass(frozen=True)
class MemoryScopeContext:
    """Contexto de ámbito consultado durante una recuperación determinista."""

    project_key: str | None = None
    app_or_game_key: str | None = None
    include_global: bool = True


@dataclass(frozen=True)
class MemoryRetrievalItem:
    """Recuerdo recuperado junto con su puntuación determinista y extracto acotado."""

    record: MemoryRecord
    score: float
    score_breakdown: dict[str, float]
    match_backend: str
    rendered_excerpt: str


@dataclass(frozen=True)
class MemoryRetrievalResult:
    """Resultado acotado de recuperación N8.2 con sobre no confiable explícito."""

    query: str
    target: MemoryRetrievalTarget
    items: tuple[MemoryRetrievalItem, ...]
    total_candidates: int
    returned_count: int
    total_chars: int
    budget_max_items: int
    budget_max_chars: int
    truncated_by_budget: bool
    blocked_by_history_policy: bool = False
    untrusted_advisory_json: str = ""
    backend_used: str = "none"


@dataclass(frozen=True)
class PurgeResult:
    """Resultado explícito de una purga física controlada con aviso de límites reales de almacenamiento."""

    memory_id: str
    purged: bool
    revisions_deleted: int
    audit_rows_deleted: int
    wal_checkpointed: bool
    vacuum_executed: bool
    limitations_notice: str


@dataclass(frozen=True)
class BackupInfo:
    """Metadatos verificados de un snapshot SQLite consistente."""

    backup_id: str
    backup_path: str
    metadata_path: str
    sha256: str
    size_bytes: int
    created_at: str
    schema_version: int
    memory_count: int
    integrity_ok: bool


@dataclass(frozen=True)
class RestoreResult:
    """Resultado verificable de una restauración atómica desde backup SHA-256."""

    restored: bool
    backup_id: str
    backup_sha256: str
    preserved_previous_paths: tuple[str, ...]
    integrity_report: IntegrityReport
    details: str


@dataclass(frozen=True)
class PreferenceImportItem:
    """Elemento clasificado en la vista previa de importación manual de preferencias."""

    key: str
    value_summary: str
    classification: str  # NEW | DUPLICATE | CONFLICT | REJECTED
    reason: str
    existing_memory_id: str | None = None


@dataclass(frozen=True)
class PreferenceImportPreview:
    """Previsualización previa a confirmación explícita de `/memoria importar-preferencias`."""

    source_path: str
    valid: bool
    new_items: tuple[PreferenceImportItem, ...]
    duplicates: tuple[PreferenceImportItem, ...]
    conflicts: tuple[PreferenceImportItem, ...]
    rejected: tuple[PreferenceImportItem, ...]
    error: str | None = None


@dataclass(frozen=True)
class PreferenceImportResult:
    """Resumen auditable tras confirmar una importación manual de preferencias."""

    imported_count: int
    skipped_duplicates: int
    conflicts_resolved: int
    conflicts_skipped: int
    rejected_count: int
    created_memory_ids: tuple[str, ...]
