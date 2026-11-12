"""
Configuración y límites de seguridad/rendimiento para `WebExtractionSkill`.

Se separa en un módulo propio dentro de `skills/` para:
1. Centralizar límites conservadores configurables por código o variables de entorno
   (`WEB_EXTRACT_*`), aprovechando la carga previa de `.env` realizada por `config.settings`.
2. Evitar desactivaciones accidentales de políticas críticas (puertos, TLS, límites de bytes)
   mediante validación estricta de tipos y rangos.
3. No acoplar la configuración de extracción web a motores de IA, audio ni renderizadores DOCX.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
import sys
from typing import Iterable

# Garantizamos que `.env` haya sido cargado por `config.settings` sin modificar dicho archivo.
import config.settings  # noqa: F401


# Puertos predeterminados estrictos por esquema HTTP/HTTPS.
DEFAULT_HTTP_PORT: int = 80
DEFAULT_HTTPS_PORT: int = 443
DEFAULT_ALLOWED_PORTS: frozenset[int] = frozenset({DEFAULT_HTTP_PORT, DEFAULT_HTTPS_PORT})

# Tipos MIME permitidos por defecto para extracción HTML determinista.
DEFAULT_ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
    }
)

# Límites máximos absolutos de seguridad para evitar configuraciones irrazonables.
MAX_PORT_NUMBER: int = 65535
MIN_PORT_NUMBER: int = 1
_DIGIT_TOKEN_RE = re.compile(r"^\d+$")


def _validate_port(port: object) -> int:
    """
    Valida que un puerto sea un entero real (no booleano) dentro del rango TCP válido 1..65535.
    """
    if isinstance(port, bool) or not isinstance(port, int):
        raise TypeError(f"El puerto debe ser un entero válido (recibido: {type(port).__name__}).")
    if port < MIN_PORT_NUMBER or port > MAX_PORT_NUMBER:
        raise ValueError(
            f"Puerto fuera de rango permitido ({MIN_PORT_NUMBER}-{MAX_PORT_NUMBER}): {port}."
        )
    return port


def validate_additional_ports(ports: Iterable[object] | None) -> frozenset[int]:
    """
    Valida una colección explícita de puertos TCP adicionales.

    Rechaza cadenas arbitrarias, booleanos, puertos negativos, puerto 0 o > 65535.
    """
    if ports is None:
        return frozenset()
    if isinstance(ports, (str, bytes)):
        raise TypeError(
            "additional_ports debe ser una colección de enteros (por ejemplo [8080, 8443]), no una cadena."
        )
    validated: set[int] = set()
    for raw_port in ports:
        validated.add(_validate_port(raw_port))
    return frozenset(validated)


def parse_additional_ports_env(raw_value: str | None) -> frozenset[int]:
    """
    Interpreta `WEB_EXTRACT_ADDITIONAL_PORTS` (ej. '8080,8443') de forma defensiva.

    Si algún token está malformado (ej. '*', 'all', '-1', 'abc'), se ignora con un aviso
    y JAMÁS se relajan ni desactivan las restricciones de puertos.
    """
    if not raw_value or not isinstance(raw_value, str):
        return frozenset()

    ports: set[int] = set()
    for token in raw_value.split(","):
        cleaned = token.strip()
        if not cleaned:
            continue
        if not _DIGIT_TOKEN_RE.match(cleaned):
            print(
                f"[WebExtractionConfig] Aviso: puerto adicional inválido ignorado '{cleaned}'.",
                file=sys.stderr,
            )
            continue
        port_num = int(cleaned)
        if MIN_PORT_NUMBER <= port_num <= MAX_PORT_NUMBER:
            ports.add(port_num)
        else:
            print(
                f"[WebExtractionConfig] Aviso: puerto adicional fuera de rango ignorado '{cleaned}'.",
                file=sys.stderr,
            )
    return frozenset(ports)


def _env_bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """Lee un entero desde entorno garantizando límites mínimo y máximo."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        parsed = int(raw.strip())
    except ValueError:
        return default
    if parsed < minimum or parsed > maximum:
        return default
    return parsed


def _env_bounded_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    """Lee un flotante desde entorno garantizando límites mínimo y máximo."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        parsed = float(raw.strip())
    except ValueError:
        return default
    if parsed < minimum or parsed > maximum:
        return default
    return parsed


def _validate_int_range(name: str, value: object, *, minimum: int, maximum: int) -> int:
    """Valida tipo entero real y rango cerrado para parámetros programáticos."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} debe ser un entero (recibido: {type(value).__name__}).")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} debe estar entre {minimum} y {maximum} (recibido: {value}).")
    return value


def _validate_float_range(name: str, value: object, *, minimum: float, maximum: float) -> float:
    """Valida tipo numérico real y rango cerrado para parámetros de tiempo."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} debe ser numérico (recibido: {type(value).__name__}).")
    fval = float(value)
    if fval < minimum or fval > maximum:
        raise ValueError(f"{name} debe estar entre {minimum} y {maximum} (recibido: {fval}).")
    return fval


@dataclass(frozen=True)
class WebExtractionConfig:
    """
    Configuración inmutable y conservadora del motor de extracción web.

    Todos los límites tienen valores predeterminados seguros aptos para un asistente
    local sin sobrecargar RAM, CPU ni ancho de banda.
    """

    connect_timeout_sec: float = 5.0
    read_timeout_sec: float = 10.0
    total_timeout_sec: float = 20.0
    max_response_bytes: int = 2 * 1024 * 1024       # 2 MiB en red
    max_decompressed_bytes: int = 4 * 1024 * 1024   # 4 MiB descomprimidos
    chunk_size_bytes: int = 16 * 1024               # 16 KiB por lectura incremental
    max_pages: int = 5
    max_redirects: int = 5
    max_links: int = 100
    max_normalized_chars: int = 100_000
    max_retries: int = 1
    max_concurrent_connections: int = 3
    additional_ports: frozenset[int] = field(default_factory=frozenset)
    allowed_content_types: frozenset[str] = DEFAULT_ALLOWED_CONTENT_TYPES
    user_agent: str = "JARVIS-WebExtractor/1.0 (+local-assistant)"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "connect_timeout_sec",
            _validate_float_range("connect_timeout_sec", self.connect_timeout_sec, minimum=0.05, maximum=60.0),
        )
        object.__setattr__(
            self,
            "read_timeout_sec",
            _validate_float_range("read_timeout_sec", self.read_timeout_sec, minimum=0.05, maximum=120.0),
        )
        object.__setattr__(
            self,
            "total_timeout_sec",
            _validate_float_range("total_timeout_sec", self.total_timeout_sec, minimum=0.1, maximum=300.0),
        )
        object.__setattr__(
            self,
            "max_response_bytes",
            _validate_int_range("max_response_bytes", self.max_response_bytes, minimum=64, maximum=50 * 1024 * 1024),
        )
        object.__setattr__(
            self,
            "max_decompressed_bytes",
            _validate_int_range(
                "max_decompressed_bytes",
                self.max_decompressed_bytes,
                minimum=64,
                maximum=64 * 1024 * 1024,
            ),
        )
        object.__setattr__(
            self,
            "chunk_size_bytes",
            _validate_int_range("chunk_size_bytes", self.chunk_size_bytes, minimum=64, maximum=256 * 1024),
        )
        object.__setattr__(
            self,
            "max_pages",
            _validate_int_range("max_pages", self.max_pages, minimum=1, maximum=25),
        )
        object.__setattr__(
            self,
            "max_redirects",
            _validate_int_range("max_redirects", self.max_redirects, minimum=0, maximum=10),
        )
        object.__setattr__(
            self,
            "max_links",
            _validate_int_range("max_links", self.max_links, minimum=0, maximum=1000),
        )
        object.__setattr__(
            self,
            "max_normalized_chars",
            _validate_int_range("max_normalized_chars", self.max_normalized_chars, minimum=100, maximum=1_000_000),
        )
        object.__setattr__(
            self,
            "max_retries",
            _validate_int_range("max_retries", self.max_retries, minimum=0, maximum=3),
        )
        object.__setattr__(
            self,
            "max_concurrent_connections",
            _validate_int_range("max_concurrent_connections", self.max_concurrent_connections, minimum=1, maximum=8),
        )

        validated_extra = validate_additional_ports(self.additional_ports)
        object.__setattr__(self, "additional_ports", validated_extra)

        if not self.allowed_content_types or isinstance(self.allowed_content_types, (str, bytes)):
            raise ValueError("allowed_content_types debe ser una colección no vacía de tipos MIME.")
        normalized_types = frozenset(
            str(mime).strip().lower() for mime in self.allowed_content_types if str(mime).strip()
        )
        if not normalized_types:
            raise ValueError("allowed_content_types no puede quedar vacío.")
        object.__setattr__(self, "allowed_content_types", normalized_types)

        ua = str(self.user_agent or "").strip()
        if not ua or "\r" in ua or "\n" in ua:
            raise ValueError("user_agent inválido.")
        object.__setattr__(self, "user_agent", ua)

    @classmethod
    def from_env(cls) -> WebExtractionConfig:
        """
        Construye una instancia validada leyendo variables `WEB_EXTRACT_*` del entorno.
        Cualquier valor malformado o fuera de rango revierte al valor predeterminado seguro.
        """
        return cls(
            connect_timeout_sec=_env_bounded_float(
                "WEB_EXTRACT_CONNECT_TIMEOUT", 5.0, minimum=0.05, maximum=60.0
            ),
            read_timeout_sec=_env_bounded_float(
                "WEB_EXTRACT_READ_TIMEOUT", 10.0, minimum=0.05, maximum=120.0
            ),
            total_timeout_sec=_env_bounded_float(
                "WEB_EXTRACT_TOTAL_TIMEOUT", 20.0, minimum=0.1, maximum=300.0
            ),
            max_response_bytes=_env_bounded_int(
                "WEB_EXTRACT_MAX_RESPONSE_BYTES", 2 * 1024 * 1024, minimum=64, maximum=50 * 1024 * 1024
            ),
            max_decompressed_bytes=_env_bounded_int(
                "WEB_EXTRACT_MAX_DECOMPRESSED_BYTES", 4 * 1024 * 1024, minimum=64, maximum=64 * 1024 * 1024
            ),
            chunk_size_bytes=_env_bounded_int(
                "WEB_EXTRACT_CHUNK_SIZE_BYTES", 16 * 1024, minimum=64, maximum=256 * 1024
            ),
            max_pages=_env_bounded_int("WEB_EXTRACT_MAX_PAGES", 5, minimum=1, maximum=25),
            max_redirects=_env_bounded_int("WEB_EXTRACT_MAX_REDIRECTS", 5, minimum=0, maximum=10),
            max_links=_env_bounded_int("WEB_EXTRACT_MAX_LINKS", 100, minimum=0, maximum=1000),
            max_normalized_chars=_env_bounded_int(
                "WEB_EXTRACT_MAX_NORMALIZED_CHARS", 100_000, minimum=100, maximum=1_000_000
            ),
            max_retries=_env_bounded_int("WEB_EXTRACT_MAX_RETRIES", 1, minimum=0, maximum=3),
            max_concurrent_connections=_env_bounded_int(
                "WEB_EXTRACT_MAX_CONCURRENCY", 3, minimum=1, maximum=8
            ),
            additional_ports=parse_additional_ports_env(os.getenv("WEB_EXTRACT_ADDITIONAL_PORTS")),
        )
