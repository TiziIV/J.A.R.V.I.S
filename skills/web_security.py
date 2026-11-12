"""
Política y validaciones críticas de seguridad de red para extracción web (`web_security`).

Implementa defensa en profundidad contra SSRF, elusión por formatos alternativos de IP,
puertos no autorizados, redirecciones peligrosas y DNS rebinding:
1. Validación estricta de esquema (`http` / `https`), ausencia de credenciales y normalización IDNA.
2. Política cerrada de puertos (únicamente 80 para HTTP y 443 para HTTPS por defecto),
   ampliable solo mediante enteros validados en rango 1..65535.
3. Bloqueo de nombres locales/internos de Windows (`localhost`, `*.local`, etiquetas simples, etc.)
   y de formatos IP alternativos (decimal entero, octal `0177.0.0.1`, hexadecimal `0x7f...`, abreviado `127.1`).
4. Validación estricta de IPv4 e IPv6 mediante `ipaddress`, incluyendo propiedades
   (`is_private`, `is_loopback`, `is_link_local`, `is_multicast`, `is_reserved`, `is_unspecified`,
   `not is_global`) y desempaquetado recursivo de IPv4-mapped IPv6 (`::ffff:127.0.0.1`), 6to4 y Teredo.
5. Resolución DNS única mediante `socket.getaddrinfo()` que rechaza el destino completo si
   cualquiera de las direcciones obtenidas incumple la política de seguridad.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re
import socket
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import urlsplit, urlunsplit

from skills.web_config import (
    DEFAULT_HTTP_PORT,
    DEFAULT_HTTPS_PORT,
    WebExtractionConfig,
    validate_additional_ports,
)

# Protocolos estrictamente permitidos.
ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# Nombres reservados y sufijos internos / locales (incluyendo servicios Windows/mDNS/WPAD).
_BLOCKED_EXACT_HOSTS: frozenset[str] = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "broadcasthost",
        "wpad",
        "isatap",
    }
)
_BLOCKED_HOST_SUFFIXES: tuple[str, ...] = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".home.arpa",
    ".arpa",
    ".corp",
    ".lan",
    ".home",
    ".intranet",
)

# Redes IPv4/IPv6 adicionales bloqueadas explícitamente como segunda barrera junto a ipaddress.
_EXTRA_BLOCKED_IPV4_NETS: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("0.0.0.0/8"),       # "This" network
    ipaddress.IPv4Network("10.0.0.0/8"),      # RFC 1918
    ipaddress.IPv4Network("100.64.0.0/10"),   # CGNAT RFC 6598
    ipaddress.IPv4Network("127.0.0.0/8"),     # Loopback
    ipaddress.IPv4Network("169.254.0.0/16"),  # Link-local / Cloud metadata
    ipaddress.IPv4Network("172.16.0.0/12"),   # RFC 1918
    ipaddress.IPv4Network("192.0.0.0/24"),    # IETF protocol assignments
    ipaddress.IPv4Network("192.0.2.0/24"),    # TEST-NET-1
    ipaddress.IPv4Network("192.168.0.0/16"),  # RFC 1918
    ipaddress.IPv4Network("198.18.0.0/15"),   # Benchmarking
    ipaddress.IPv4Network("198.51.100.0/24"), # TEST-NET-2
    ipaddress.IPv4Network("203.0.113.0/24"),  # TEST-NET-3
    ipaddress.IPv4Network("224.0.0.0/4"),     # Multicast
    ipaddress.IPv4Network("240.0.0.0/4"),     # Reserved / Broadcast
)

_EXTRA_BLOCKED_IPV6_NETS: tuple[ipaddress.IPv6Network, ...] = (
    ipaddress.IPv6Network("::/128"),          # Unspecified
    ipaddress.IPv6Network("::1/128"),         # Loopback
    ipaddress.IPv6Network("64:ff9b::/96"),    # NAT64 well-known
    ipaddress.IPv6Network("64:ff9b:1::/48"),  # NAT64 local-use
    ipaddress.IPv6Network("100::/64"),        # Discard-only
    ipaddress.IPv6Network("2001:db8::/32"),   # Documentation
    ipaddress.IPv6Network("fc00::/7"),        # ULA / Private
    ipaddress.IPv6Network("fe80::/10"),       # Link-local
    ipaddress.IPv6Network("ff00::/8"),        # Multicast
)

_CONTROL_OR_WHITESPACE_RE = re.compile(r"[\x00-\x20\x7f]")
_NUMERIC_TOKEN_RE = re.compile(r"^(?:0x[0-9a-f]+|0[0-7]+|\d+)$", re.IGNORECASE)

DnsResolver = Callable[..., list[tuple[Any, ...]]]


class WebSecurityError(ValueError):
    """Excepción base para rechazos por política de seguridad web o SSRF."""

    def __init__(self, message: str, *, code: str = "SECURITY_BLOCKED") -> None:
        super().__init__(message)
        self.code: str = code


class InvalidURLError(WebSecurityError):
    """URL malformada, con credenciales embebidas o formato de host sospechoso."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="INVALID_URL")


class DisallowedSchemeError(WebSecurityError):
    """Esquema distinto de HTTP o HTTPS."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="DISALLOWED_SCHEME")


class DisallowedPortError(WebSecurityError):
    """Puerto TCP fuera de la política estricta permitida."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="DISALLOWED_PORT")


class SSRFBlockedError(WebSecurityError):
    """Destino bloqueado por apuntar a loopback, red privada, reservada o nombre local."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="SSRF_BLOCKED")


class DNSResolutionError(WebSecurityError):
    """Error o resultado vacío al resolver el nombre de dominio mediante DNS."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="DNS_RESOLUTION_ERROR")


@dataclass(frozen=True)
class ValidatedTarget:
    """Representación normalizada y validada de un destino URL antes de resolver DNS."""

    original_url: str
    normalized_url: str
    scheme: str
    hostname: str
    port: int
    path_with_query: str
    is_ip_literal: bool


@dataclass(frozen=True)
class ResolvedEndpoint:
    """Punto final TCP resuelto mediante getaddrinfo y aprobado por la política IP."""

    family: int
    socktype: int
    proto: int
    ip_address: str
    sockaddr: tuple[Any, ...]


def validate_ip_address(raw_ip: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """
    Valida estrictamente una dirección IPv4 o IPv6 contra todas las categorías inseguras.

    Incluye desempaquetado recursivo de IPv4-mapped IPv6 (`::ffff:127.0.0.1`), 6to4 y Teredo.
    Lanza `SSRFBlockedError` si la dirección no es globalmente enrutable y pública.
    """
    if isinstance(raw_ip, str):
        cleaned = raw_ip.strip().strip("[]")
        if not cleaned or "%" in cleaned:
            raise SSRFBlockedError(f"Dirección IP inválida o con identificador de zona prohibido: '{raw_ip}'.")
        try:
            ip_obj = ipaddress.ip_address(cleaned)
        except ValueError as exc:
            raise SSRFBlockedError(f"Dirección IP no reconocida '{raw_ip}': {exc}") from exc
    else:
        ip_obj = raw_ip

    _check_ip_recursive(ip_obj, depth=0)
    return ip_obj


def _check_ip_recursive(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address, *, depth: int) -> None:
    """Evalúa propiedades de seguridad IP y valida recursivamente direcciones IPv4 encapsuladas."""
    if depth > 4:
        raise SSRFBlockedError(f"Profundidad excesiva de encapsulación IP en '{ip_obj}'.")

    # Comprobación explícita de las 6 banderas requeridas + globalidad.
    unsafe_reasons: list[str] = []
    if ip_obj.is_private:
        unsafe_reasons.append("privada")
    if ip_obj.is_loopback:
        unsafe_reasons.append("loopback")
    if ip_obj.is_link_local:
        unsafe_reasons.append("link-local")
    if ip_obj.is_multicast:
        unsafe_reasons.append("multicast")
    if ip_obj.is_reserved:
        unsafe_reasons.append("reservada")
    if ip_obj.is_unspecified:
        unsafe_reasons.append("no especificada")
    if not ip_obj.is_global:
        unsafe_reasons.append("no global")

    if isinstance(ip_obj, ipaddress.IPv4Address):
        if any(ip_obj in net for net in _EXTRA_BLOCKED_IPV4_NETS):
            unsafe_reasons.append("rango IPv4 bloqueado")
    elif isinstance(ip_obj, ipaddress.IPv6Address):
        if any(ip_obj in net for net in _EXTRA_BLOCKED_IPV6_NETS):
            unsafe_reasons.append("rango IPv6 bloqueado")

        # Verificar direcciones IPv4 embebidas antes o además del envoltorio IPv6.
        mapped = ip_obj.ipv4_mapped
        if mapped is not None:
            _check_ip_recursive(mapped, depth=depth + 1)

        sixtofour = ip_obj.sixtofour
        if sixtofour is not None:
            _check_ip_recursive(sixtofour, depth=depth + 1)

        teredo = ip_obj.teredo
        if teredo is not None:
            _check_ip_recursive(teredo[0], depth=depth + 1)
            _check_ip_recursive(teredo[1], depth=depth + 1)

    if unsafe_reasons:
        reasons_str = ", ".join(dict.fromkeys(unsafe_reasons))
        raise SSRFBlockedError(f"Dirección IP '{ip_obj}' bloqueada por política SSRF ({reasons_str}).")


def _reject_alternative_ip_formats(host: str) -> None:
    """
    Detecta y rechaza codificaciones alternativas de direcciones IP diseñadas para evadir filtros:
    - Enteros decimales (ej. http://2130706433)
    - Hexadecimales (ej. http://0x7f000001, http://0x7f.0.0.1)
    - Octales / ceros a la izquierda (ej. http://0177.0.0.1, http://0177.0.0.01)
    - Formas abreviadas IPv4 (ej. http://127.1, http://127.0.1)
    """
    if _NUMERIC_TOKEN_RE.match(host):
        raise SSRFBlockedError(
            f"Formato numérico alternativo de IP rechazado por seguridad: '{host}'."
        )

    parts = host.split(".")
    if parts and all(_NUMERIC_TOKEN_RE.match(p) for p in parts):
        if len(parts) != 4:
            raise SSRFBlockedError(
                f"Formato IPv4 abreviado no canónico rechazado por seguridad: '{host}'."
            )
        for part in parts:
            if part.lower().startswith("0x"):
                raise SSRFBlockedError(
                    f"Formato IPv4 hexadecimal rechazado por seguridad: '{host}'."
                )
            if len(part) > 1 and part.startswith("0"):
                raise SSRFBlockedError(
                    f"Formato IPv4 octal o con ceros iniciales rechazado por seguridad: '{host}'."
                )


class WebNetworkPolicy:
    """
    Política central de seguridad de red para validar URLs, puertos, nombres e IPs resueltas.
    """

    def __init__(
        self,
        *,
        additional_ports: Iterable[int] | None = None,
        dns_resolver: DnsResolver | None = None,
        config: WebExtractionConfig | None = None,
    ) -> None:
        base_extra = config.additional_ports if config is not None else frozenset()
        explicit_extra = validate_additional_ports(additional_ports) if additional_ports is not None else frozenset()
        self.additional_ports: frozenset[int] = base_extra | explicit_extra
        self._dns_resolver: DnsResolver = dns_resolver or socket.getaddrinfo

    def allowed_ports_for_scheme(self, scheme: str) -> frozenset[int]:
        """Retorna el conjunto exacto de puertos TCP autorizados para un esquema."""
        norm = scheme.strip().lower()
        if norm == "http":
            return frozenset({DEFAULT_HTTP_PORT}) | self.additional_ports
        if norm == "https":
            return frozenset({DEFAULT_HTTPS_PORT}) | self.additional_ports
        raise DisallowedSchemeError(f"Esquema no permitido: '{scheme}'. Solo se aceptan HTTP y HTTPS.")

    def validate_port(self, scheme: str, explicit_port: int | None) -> int:
        """Valida que el puerto efectivo esté autorizado explícitamente para el esquema."""
        norm_scheme = scheme.strip().lower()
        if norm_scheme not in ALLOWED_SCHEMES:
            raise DisallowedSchemeError(f"Esquema no permitido: '{scheme}'.")
        effective_port = (
            explicit_port
            if explicit_port is not None
            else (DEFAULT_HTTP_PORT if norm_scheme == "http" else DEFAULT_HTTPS_PORT)
        )
        if isinstance(effective_port, bool) or not isinstance(effective_port, int) or not (1 <= effective_port <= 65535):
            raise DisallowedPortError(f"Número de puerto inválido: {effective_port!r}.")
        allowed = self.allowed_ports_for_scheme(norm_scheme)
        if effective_port not in allowed:
            raise DisallowedPortError(
                f"Puerto {effective_port} bloqueado para '{norm_scheme}'. "
                f"Puertos permitidos: {sorted(allowed)}."
            )
        return effective_port

    def validate_url(self, raw_url: str) -> ValidatedTarget:
        """
        Valida sintaxis, esquema, ausencia de credenciales, puerto, hostname y formato IP literal.
        No realiza resolución DNS aquí; eso ocurre vinculado a la conexión efectiva en `resolve_and_validate`.
        """
        if not isinstance(raw_url, str):
            raise InvalidURLError("La URL debe ser una cadena de texto.")
        cleaned = raw_url.strip()
        if not cleaned:
            raise InvalidURLError("La URL está vacía.")
        if len(cleaned) > 4096:
            raise InvalidURLError("La URL supera la longitud máxima permitida (4096 caracteres).")
        if _CONTROL_OR_WHITESPACE_RE.search(cleaned):
            raise InvalidURLError("La URL contiene caracteres de control o espacios no válidos.")

        # Inspeccionar estructura previa para detectar barras invertidas en autoridad/esquema.
        if "://" not in cleaned:
            scheme_hint = cleaned.split(":", 1)[0].lower() if ":" in cleaned else ""
            if scheme_hint and scheme_hint not in ALLOWED_SCHEMES and not scheme_hint.isdigit():
                raise DisallowedSchemeError(
                    f"Esquema '{scheme_hint}' no permitido. Solo se aceptan URLs http:// o https://."
                )
            raise InvalidURLError(f"URL inválida o sin esquema explícito http/https: '{cleaned}'.")

        scheme_part, rest_part = cleaned.split("://", 1)
        authority_part = rest_part.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        if "\\" in scheme_part or "\\" in authority_part:
            raise InvalidURLError("La URL contiene barras invertidas ('\\') no permitidas en el destino.")

        parsed = urlsplit(cleaned)
        scheme = parsed.scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            raise DisallowedSchemeError(
                f"Esquema '{parsed.scheme}' no permitido. Solo se aceptan HTTP y HTTPS."
            )

        if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
            raise InvalidURLError(
                "URL rechazada: no se permiten credenciales embebidas ('usuario:clave@host')."
            )

        try:
            explicit_port = parsed.port
        except ValueError as exc:
            raise DisallowedPortError(f"Puerto inválido o malformado en la URL '{cleaned}'.") from exc

        port = self.validate_port(scheme, explicit_port)

        raw_host = parsed.hostname
        if not raw_host:
            raise InvalidURLError(f"La URL no contiene un hostname válido: '{cleaned}'.")
        if "%" in raw_host or "%" in authority_part:
            raise InvalidURLError("El hostname contiene codificación '%' o identificador de zona no permitido.")
        if raw_host.endswith(".."):
            raise InvalidURLError(f"Hostname malformado: '{raw_host}'.")

        # Normalización IDNA para neutralizar homóglifos Unicode (ej. dígitos ancho completo).
        if ":" in raw_host:
            ascii_host = raw_host.lower()
        else:
            try:
                ascii_host = raw_host.encode("idna").decode("ascii").lower()
            except UnicodeError as exc:
                raise InvalidURLError(f"Hostname con codificación internacional inválida: '{raw_host}'.") from exc

        check_host = ascii_host[:-1] if ascii_host.endswith(".") else ascii_host
        if not check_host:
            raise InvalidURLError("Hostname vacío tras normalización.")

        # Bloqueo explícito de localhost y nombres locales/internos de Windows o red LAN.
        if check_host in _BLOCKED_EXACT_HOSTS or any(
            check_host.endswith(suffix) for suffix in _BLOCKED_HOST_SUFFIXES
        ):
            raise SSRFBlockedError(
                f"Destino bloqueado por política SSRF: '{check_host}' corresponde a un host local o interno."
            )

        # Verificar si es una IP literal estándar (IPv4 o IPv6).
        is_ip_literal = False
        try:
            ip_obj = ipaddress.ip_address(check_host)
            is_ip_literal = True
            validate_ip_address(ip_obj)
        except SSRFBlockedError:
            raise
        except ValueError:
            if ":" in check_host:
                raise InvalidURLError(f"Dirección IPv6 literal malformada: '{check_host}'.")
            _reject_alternative_ip_formats(check_host)

        # Bloquear hostnames de una sola etiqueta sin punto (ej. nombres NetBIOS/LLMNR de Windows)
        # cuando no son literales IP públicos válidos.
        if not is_ip_literal and "." not in check_host:
            raise SSRFBlockedError(
                f"Hostname local de etiqueta simple bloqueado por seguridad en Windows: '{check_host}'."
            )

        # Reconstrucción canónica sin fragmentos ni credenciales.
        host_for_netloc = f"[{check_host}]" if ":" in check_host else check_host
        is_default_port = (scheme == "http" and port == DEFAULT_HTTP_PORT) or (
            scheme == "https" and port == DEFAULT_HTTPS_PORT
        )
        netloc = host_for_netloc if is_default_port else f"{host_for_netloc}:{port}"
        path = parsed.path or "/"
        path_with_query = f"{path}?{parsed.query}" if parsed.query else path
        normalized_url = urlunsplit((scheme, netloc, path, parsed.query, ""))

        return ValidatedTarget(
            original_url=cleaned,
            normalized_url=normalized_url,
            scheme=scheme,
            hostname=check_host,
            port=port,
            path_with_query=path_with_query,
            is_ip_literal=is_ip_literal,
        )

    def resolve_and_validate(self, hostname: str, port: int) -> list[ResolvedEndpoint]:
        """
        Resuelve `hostname` mediante `socket.getaddrinfo()` y valida TODAS las direcciones devueltas.

        Regla crítica:
        Si la resolución devuelve múltiples registros (ej. uno público y uno privado o loopback),
        se rechaza el destino completo para impedir ataques de selección parcial o DNS rebinding.
        """
        clean_host = str(hostname or "").strip().strip("[]")
        if not clean_host or "%" in clean_host:
            raise InvalidURLError(f"Hostname inválido para resolución DNS: '{hostname}'.")

        try:
            records = self._dns_resolver(clean_host, port, 0, socket.SOCK_STREAM)
        except OSError as exc:
            raise DNSResolutionError(f"No se pudo resolver el dominio '{clean_host}': {exc}") from exc

        if not records:
            raise DNSResolutionError(f"La resolución DNS para '{clean_host}' no devolvió direcciones.")

        endpoints: list[ResolvedEndpoint] = []
        seen_ips: set[str] = set()

        # Paso 1: validar TODAS las direcciones devueltas antes de aceptar ninguna.
        for entry in records:
            if not isinstance(entry, Sequence) or len(entry) < 5:
                raise DNSResolutionError(f"Registro DNS malformado recibido para '{clean_host}'.")
            family, socktype, proto, _, sockaddr = entry[:5]
            if family not in (socket.AF_INET, socket.AF_INET6):
                continue
            if not isinstance(sockaddr, Sequence) or len(sockaddr) < 2:
                raise DNSResolutionError(f"Dirección de socket DNS malformada para '{clean_host}'.")

            ip_raw = str(sockaddr[0])
            resolved_port = sockaddr[1]
            if resolved_port != port:
                raise SSRFBlockedError(
                    f"Discrepancia de puerto en resolución DNS para '{clean_host}': {resolved_port} != {port}."
                )

            validated_ip = validate_ip_address(ip_raw)
            ip_canonical = str(validated_ip)
            if ip_canonical not in seen_ips:
                seen_ips.add(ip_canonical)
                endpoints.append(
                    ResolvedEndpoint(
                        family=int(family),
                        socktype=int(socktype or socket.SOCK_STREAM),
                        proto=int(proto or 0),
                        ip_address=ip_canonical,
                        sockaddr=tuple(sockaddr),
                    )
                )

        if not endpoints:
            raise DNSResolutionError(
                f"La resolución DNS para '{clean_host}' no produjo endpoints IPv4/IPv6 utilizables."
            )

        return endpoints
