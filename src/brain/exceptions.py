"""
Clasificación uniforme de errores y sanitización de secretos para la arquitectura Multi-Brain.

Justificación técnica junior:
No todos los fallos de un motor de IA deben tratarse igual:
- Si un modelo específico devuelve HTTP 404 ('model not found'), tiene sentido probar un modelo
  de respaldo del mismo proveedor o pasar al siguiente motor.
- Si la clave API es inválida (HTTP 401/403) o la entrada es inválida (HTTP 400), reintentar con
  otro modelo del mismo proveedor solo desperdiciaría tiempo y cuota.
- Si un stream ya emitió parte de la respuesta al usuario, reenviar la consulta a otro motor
  duplicaría contenido en pantalla o en el sintetizador de voz.
"""

from __future__ import annotations

from enum import Enum
import re
from typing import Optional


class BrainErrorKind(str, Enum):
    """Categorías normalizadas de fallo entre motores locales y cloud."""

    UNCONFIGURED = "unconfigured"        # Falta la clave API del proveedor.
    AUTH = "auth"                        # Clave inválida o sin permisos (HTTP 401/403).
    QUOTA = "quota"                      # Cuota o rate-limit agotado (HTTP 429 / insufficient_quota).
    MODEL_NOT_FOUND = "model_not_found"  # Modelo inexistente o incompatible con el endpoint.
    DAEMON_OFFLINE = "daemon_offline"    # Servidor local Ollama apagado o inaccesible.
    TIMEOUT = "timeout"                  # Tiempo de espera agotado en conexión o lectura.
    NETWORK = "network"                  # Fallo de red / DNS / socket desconectado.
    SERVER = "server"                    # Error interno del proveedor (HTTP 5xx).
    INVALID_REQUEST = "invalid_request"  # Solicitud malformada o rechazada por validación (HTTP 400).
    PARTIAL_STREAM = "partial_stream"    # El stream se interrumpió tras emitir texto parcial.
    EMPTY_RESPONSE = "empty_response"    # El proveedor respondió HTTP 200 pero sin contenido útil.
    INVALID_RESPONSE = "invalid_response"  # Salida estructurada (JSON/JSONL) corrupta o incompleta.
    EXECUTION = "execution"              # Error de ejecución de subproceso o código de salida != 0.
    UNKNOWN = "unknown"                  # Cualquier otro error no clasificado.


# Patrones de posibles claves API, tokens, credenciales cloud y parámetros URL sensibles
# para garantizar que jamás aparezcan en logs, snapshots de contexto ni excepciones.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Claves Anthropic y OpenAI (incluyendo sk-proj-*, sk-ant-*)
    re.compile(r"(?i)\bsk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9_\-]{12,}"),
    # Claves Google Cloud / Gemini
    re.compile(r"\bAIza[0-9A-Za-z_\-]{12,}"),
    re.compile(r"\bAQ\.[0-9A-Za-z_\-]{16,}"),
    # Tokens de GitHub (PAT clásicos ghp_/gho_/ghu_/ghs_/ghr_ y fine-grained github_pat_)
    re.compile(r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,})\b"),
    # Access Key IDs de AWS (AKIA / ASIA, insensible a mayúsculas/minúsculas)
    re.compile(r"(?i)\b(?:akia|asia)[0-9a-z]{12,20}\b"),
    # Tokens de Slack, GitLab y Hugging Face
    re.compile(r"(?i)\b(?:xox[baprs]-[A-Za-z0-9\-]{10,}|glpat-[A-Za-z0-9_\-]{10,}|hf_[A-Za-z0-9]{16,})\b"),
    # Encabezados y esquemas Authorization: Bearer <token> o Bearer <token>
    re.compile(r"(?i)\b(?:authorization\s*[:=]\s*)?bearer\s+[A-Za-z0-9_\-\.~+/]{6,}=*"),
    # JSON Web Tokens (JWT eyJ... con tres segmentos base64url)
    re.compile(r"\beyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{2,}\.[A-Za-z0-9_\-]{2,}\b"),
    # Parámetros sensibles en query strings URL (?token=..., &access_token=..., ?api_key=...)
    re.compile(
        r"(?i)([?&](?:token|access[_\-]?token|refresh[_\-]?token|id[_\-]?token|api[_\-]?key|apikey|secret|password|passwd|pwd|auth[_\-]?token|client[_\-]?secret|sig|signature)\s*=\s*)[^&\s'\"<>]+"
    ),
    # Asignaciones clave=valor o clave: valor (api_key, token, secret, password, authorization, etc.)
    re.compile(
        r"(?i)\b(api[_\-]?key|apikey|x-api-key|authorization|access[_\-]?token|refresh[_\-]?token|id[_\-]?token|auth[_\-]?token|client[_\-]?secret|private[_\-]?key|token|secret|password|passwd|pwd)\s*[:=]\s*['\"]?[A-Za-z0-9_\-\.~+/]{6,}['\"]?"
    ),
)


def redact_secrets(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    """
    Oculta cualquier clave API o token de autenticación que pudiera filtrarse en un mensaje de error.

    Args:
        text (str): Mensaje original a sanitizar.
        extra_secrets (tuple[str, ...]): Claves activas conocidas en memoria para reemplazo exacto.

    Returns:
        str: Texto seguro apto para mostrarse en consola o guardarse en logs.
    """
    if not text:
        return ""
    sanitized = str(text)
    for secret in extra_secrets:
        if secret and len(secret.strip()) >= 6:
            sanitized = sanitized.replace(secret.strip(), "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub("[REDACTED]", sanitized)
    return sanitized


class BrainError(RuntimeError):
    """
    Excepción estructurada que conserva el proveedor, el tipo de error y si admite fallback.
    """

    def __init__(
        self,
        provider: str,
        kind: BrainErrorKind,
        user_message: str,
        *,
        model: Optional[str] = None,
        status_code: Optional[int] = None,
        detail: str = "",
        partial_output: bool = False,
    ) -> None:
        safe_message = redact_secrets(user_message)
        safe_detail = redact_secrets(detail)
        super().__init__(safe_message)
        self.provider: str = provider
        self.kind: BrainErrorKind = kind
        self.user_message: str = safe_message
        self.model: Optional[str] = model
        self.status_code: Optional[int] = status_code
        self.detail: str = safe_detail
        self.partial_output: bool = partial_output

    @property
    def allows_same_provider_model_fallback(self) -> bool:
        """
        Indica si vale la pena intentar un modelo secundario dentro del MISMO proveedor.
        Solo aplica cuando el modelo principal no existe o no es compatible y no hubo salida parcial.
        """
        return self.kind == BrainErrorKind.MODEL_NOT_FOUND and not self.partial_output

    @property
    def allows_provider_fallback(self) -> bool:
        """
        Indica si la política centralizada puede derivar la consulta a OTRO proveedor (ej. Claude -> Gemini).
        No se deriva si la entrada del usuario es inválida o si ya se transmitió respuesta parcial.
        """
        if self.partial_output or self.kind in (
            BrainErrorKind.PARTIAL_STREAM,
            BrainErrorKind.INVALID_REQUEST,
        ):
            return False
        return True
