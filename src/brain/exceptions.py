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
from typing import Any, Optional


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


_REDACTED = "[REDACTED]"

# --- Tokens con formato propio (firma reconocible): se reemplaza el token completo. ---
# Un prefijo de formato fijo es una señal estructural fuerte: casi no genera falsos positivos.
_TOKEN_FORMAT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Claves Anthropic y OpenAI (incluyendo sk-proj-*, sk-ant-*)
    re.compile(r"(?i)\bsk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9_\-]{12,}"),
    # Claves Google Cloud / Gemini
    re.compile(r"\bAIza[0-9A-Za-z_\-]{12,}"),
    re.compile(r"\bAQ\.[0-9A-Za-z_\-]{16,}"),
    # Tokens de GitHub (PAT clásicos ghp_/gho_/ghu_/ghs_/ghr_ y fine-grained github_pat_)
    re.compile(r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,})\b"),
    # Access Key IDs de AWS: prefijo AKIA/ASIA + exactamente 16 caracteres alfanuméricos.
    re.compile(r"(?i)\b(?:akia|asia)[0-9a-z]{16}\b"),
    # Tokens de Slack, GitLab y Hugging Face
    re.compile(r"(?i)\b(?:xox[baprs]-[A-Za-z0-9\-]{10,}|glpat-[A-Za-z0-9_\-]{10,}|hf_[A-Za-z0-9]{16,})\b"),
    # JSON Web Tokens (JWT eyJ... con tres segmentos base64url)
    re.compile(r"\beyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{2,}\.[A-Za-z0-9_\-]{2,}\b"),
)

# Encabezado `Authorization: <esquema> <credencial>` (Bearer/Basic/Digest/Token/Negotiate).
# Conserva el nombre del encabezado y el esquema; solo se oculta la credencial.
_AUTH_SCHEME_RE: re.Pattern[str] = re.compile(
    r"(?i)\b((?:proxy-)?authorization\s*[:=]\s*[\"']?(?:bearer|basic|digest|token|negotiate)\s+)"
    r"([A-Za-z0-9_\-\.~+/]{6,}={0,2})"
)
# Encabezado sin esquema: exige que el valor contenga al menos un dígito para no tocar prosa.
_AUTH_BARE_RE: re.Pattern[str] = re.compile(
    r"(?i)\b((?:proxy-)?authorization\s*[:=]\s*[\"']?)"
    r"(?=[A-Za-z0-9_\-\.~+/]*\d)([A-Za-z0-9_\-\.~+/]{8,}={0,2})"
)
# `Bearer <token>` suelto (sin encabezado): solo si el token es largo y contiene dígitos.
# Así "The bearer authorization failed twice" (prosa) NO coincide.
_BEARER_STANDALONE_RE: re.Pattern[str] = re.compile(
    r"(?i)(\bbearer\s+)(?=[A-Za-z0-9_\-\.~+/]*\d)([A-Za-z0-9_\-\.~+/]{16,}={0,2})"
)
# Credenciales embebidas en una URL: scheme://usuario:CLAVE@host
_URL_USERINFO_RE: re.Pattern[str] = re.compile(
    r"(\b[a-zA-Z][a-zA-Z0-9+.\-]{1,20}://[^\s/:@\"']+:)([^\s/@\"']+)(@)"
)
# Parámetros sensibles en query strings o fragmentos URL (?token=..., &access_token=..., #id_token=...).
_URL_PARAM_RE: re.Pattern[str] = re.compile(
    r"(?i)([?&#;](?:token|access[_\-]?token|refresh[_\-]?token|id[_\-]?token|api[_\-]?key|apikey|"
    r"secret|password|passwd|pwd|auth[_\-]?token|client[_\-]?secret|sig|signature)\s*=\s*)"
    r"([^&\s'\"<>]+)"
)
# Asignaciones estructuradas `clave = valor`, `clave: valor` y JSON `"clave": "valor"`.
# - Valor entre comillas (JSON/config): siempre se redacta.
# - Valor sin comillas con `=`: se redacta.
# - Valor sin comillas con `:`: solo si parece una credencial (ver _CREDENTIAL_LIKE_RE),
#   para no destruir prosa como "password: incorrect".
_KV_RE: re.Pattern[str] = re.compile(
    r"""(?ix)
    (?P<prefix>
        (?P<kq>["']?)\b
        (?P<key>api[_\-]?key|apikey|x-api-key|access[_\-]?token|refresh[_\-]?token|id[_\-]?token|
                auth[_\-]?token|client[_\-]?secret|private[_\-]?key|secret|token|password|passwd|pwd)
        (?P=kq)\s*(?P<sep>[:=])\s*
    )
    (?:
        (?P<vq>["'])(?P<qval>[^"'\r\n]{3,})(?P=vq)
        |
        (?P<val>[A-Za-z0-9_\-\.~+/!@\#$%^*]{6,}={0,2})
    )
    """
)
_CREDENTIAL_LIKE_RE: re.Pattern[str] = re.compile(r"[\d_\-/+~!@#$%^*=]")

# Nombres de clave que, en un diccionario, indican que su valor escalar es secreto.
_SENSITIVE_KEY_RE: re.Pattern[str] = re.compile(
    r"(?i)^(?:api[_\-]?key|apikey|x-api-key|access[_\-]?token|refresh[_\-]?token|id[_\-]?token|"
    r"auth[_\-]?token|client[_\-]?secret|private[_\-]?key|secret|token|password|passwd|pwd|"
    r"authorization|proxy-authorization)$"
)


def is_sensitive_key(key: Any) -> bool:
    """Indica si el nombre de una clave de diccionario identifica un valor secreto."""
    return isinstance(key, str) and bool(_SENSITIVE_KEY_RE.match(key.strip()))


def _kv_replacement(match: re.Match[str]) -> str:
    quoted_value = match.group("qval")
    if quoted_value is not None:
        quote = match.group("vq")
        return f"{match.group('prefix')}{quote}{_REDACTED}{quote}"
    value = match.group("val")
    if match.group("sep") == ":" and not _CREDENTIAL_LIKE_RE.search(value):
        return match.group(0)
    return f"{match.group('prefix')}{_REDACTED}"


def _redact_text(text: str, extra_secrets: tuple[str, ...]) -> str:
    sanitized = text
    for secret in extra_secrets:
        if secret and len(secret.strip()) >= 6:
            sanitized = sanitized.replace(secret.strip(), _REDACTED)
    for pattern in _TOKEN_FORMAT_PATTERNS:
        sanitized = pattern.sub(_REDACTED, sanitized)
    sanitized = _AUTH_SCHEME_RE.sub(lambda m: m.group(1) + _REDACTED, sanitized)
    sanitized = _AUTH_BARE_RE.sub(lambda m: m.group(1) + _REDACTED, sanitized)
    sanitized = _BEARER_STANDALONE_RE.sub(lambda m: m.group(1) + _REDACTED, sanitized)
    sanitized = _URL_USERINFO_RE.sub(lambda m: m.group(1) + _REDACTED + m.group(3), sanitized)
    sanitized = _URL_PARAM_RE.sub(lambda m: m.group(1) + _REDACTED, sanitized)
    sanitized = _KV_RE.sub(_kv_replacement, sanitized)
    return sanitized


def _redact_structure(value: Any, extra_secrets: tuple[str, ...]) -> Any:
    """Redacción recursiva sobre diccionarios, listas y tuplas (otros tipos se devuelven intactos)."""
    if isinstance(value, str):
        return _redact_text(value, extra_secrets)
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if is_sensitive_key(key) and isinstance(item, (str, int, float)) and not isinstance(item, bool) and item != "":
                redacted[key] = _REDACTED
            else:
                redacted[key] = _redact_structure(item, extra_secrets)
        return redacted
    if isinstance(value, list):
        return [_redact_structure(item, extra_secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_structure(item, extra_secrets) for item in value)
    return value


def redact_secrets(text: Any, extra_secrets: tuple[str, ...] = ()) -> Any:
    """
    Oculta claves API, tokens y credenciales mediante reglas estructurales y específicas.

    Diseño anti falsos positivos: la prosa común ("The bearer authorization failed twice",
    "password: incorrect") se conserva; se redactan tokens con formato propio, encabezados
    `Authorization`, credenciales en URLs, asignaciones `clave=valor` y campos JSON sensibles.

    Args:
        text: Mensaje (str) a sanitizar. Si recibe dict/list/tuple aplica redacción recursiva
            (incluida la redacción por nombre de clave sensible en diccionarios).
        extra_secrets (tuple[str, ...]): Claves activas conocidas en memoria para reemplazo exacto.

    Returns:
        Texto (o estructura) seguro para mostrarse en consola o guardarse en logs.
        Límite conocido: una contraseña en prosa sin formato (p. ej. "password: hunter") no es
        detectable sin una clave o formato estructural y puede permanecer.
    """
    if isinstance(text, (dict, list, tuple)):
        return _redact_structure(text, extra_secrets)
    if not text:
        return ""
    return _redact_text(str(text), extra_secrets)


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
