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


# Patrones de posibles claves API para asegurarnos de que jamás aparezcan en logs ni excepciones.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{12,}"),
    re.compile(r"AQ\.[0-9A-Za-z_\-]{16,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(
        r"(?i)(api[_\-]?key|x-api-key|authorization|bearer|access[_\-]?token|refresh[_\-]?token|secret|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-\.]{8,}['\"]?"
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
