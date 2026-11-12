"""
Cliente HTTP/HTTPS seguro con vinculación directa a IP validada, control manual de
redirecciones, lectura incremental por streaming y protección contra bombas de descompresión.

Garantías clave del transporte (`PinnedHTTPConnection` / `PinnedHTTPSConnection`):
1. La resolución DNS (`socket.getaddrinfo`) ocurre UNA sola vez dentro de `connect()` a través
   de `WebNetworkPolicy.resolve_and_validate()`, rechazando todo el destino si alguna IP es insegura.
2. El socket TCP se conecta directamente a la `sockaddr` de la IP validada mediante `socket.socket`,
   sin volver a invocar `socket.create_connection((hostname, port))` ni delegar DNS a la librería HTTP.
3. En HTTPS se preserva el hostname original para el encabezado HTTP `Host`, la extensión TLS SNI
   (`server_hostname`) y la verificación estricta de certificados (`CERT_REQUIRED` + `check_hostname`).
4. Cada salto de redirección y cada reintento seguro abren una conexión nueva e independiente
   (`Connection: close`), obligando a revalidar esquema, puerto, host y DNS en cada intento.
5. La descarga lee por chunks acotados y aplica descompresión incremental (`zlib.decompressobj` con
   `max_length`), abortando inmediatamente antes de expandir el cuerpo completo en memoria.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import http.client
import socket
import ssl
import time
from typing import Any, Callable
from urllib.parse import urljoin
import zlib

from src.brain.exceptions import redact_secrets
from skills.web_config import WebExtractionConfig
from skills.web_security import (
    DNSResolutionError,
    WebNetworkPolicy,
    WebSecurityError,
    ResolvedEndpoint,
    ValidatedTarget,
)

REDIRECT_STATUS_CODES: frozenset[int] = frozenset({301, 302, 303, 307, 308})
SUPPORTED_ENCODINGS: frozenset[str] = frozenset({"", "identity", "gzip", "x-gzip", "deflate"})

SocketFactory = Callable[[int, int, int], socket.socket]


class WebFetchError(RuntimeError):
    """Error estructurado en el transporte HTTP o lectura del cuerpo."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "FETCH_ERROR",
        http_status: int | None = None,
        retryable: bool = False,
        final_url: str | None = None,
        redirect_chain: tuple[str, ...] = (),
    ) -> None:
        super().__init__(redact_secrets(message))
        self.code: str = code
        self.http_status: int | None = http_status
        self.retryable: bool = retryable
        self.final_url: str | None = final_url
        self.redirect_chain: tuple[str, ...] = tuple(redirect_chain)


class WebTimeoutError(WebFetchError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="TIMEOUT", retryable=True)


class TooManyRedirectsError(WebFetchError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="TOO_MANY_REDIRECTS", retryable=False)


class UnsupportedContentTypeError(WebFetchError):
    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message, code="UNSUPPORTED_CONTENT_TYPE", http_status=http_status, retryable=False)


class UnsupportedEncodingError(WebFetchError):
    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message, code="UNSUPPORTED_ENCODING", http_status=http_status, retryable=False)


class ResponseTooLargeError(WebFetchError):
    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message, code="RESPONSE_TOO_LARGE", http_status=http_status, retryable=False)


class DecompressionBombError(WebFetchError):
    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message, code="DECOMPRESSION_BOMB", http_status=http_status, retryable=False)


class HTTPStatusError(WebFetchError):
    def __init__(self, message: str, *, http_status: int, retryable: bool = False) -> None:
        super().__init__(message, code="HTTP_STATUS_ERROR", http_status=http_status, retryable=retryable)


class TLSVerificationError(WebFetchError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="TLS_ERROR", retryable=False)


@dataclass(frozen=True)
class FetchResult:
    """Resultado crudo pero acotado y validado de una descarga HTTP/HTTPS."""

    original_url: str
    final_url: str
    http_status: int
    content_type: str
    declared_charset: str | None
    content_language: str | None
    body_bytes: bytes
    bytes_received: int
    bytes_decompressed: int
    redirect_chain: tuple[str, ...] = field(default_factory=tuple)
    connected_ips: tuple[str, ...] = field(default_factory=tuple)


def _create_strict_ssl_context() -> ssl.SSLContext:
    """Crea un contexto TLS estricto con verificación obligatoria de cadena y hostname."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def _validate_ssl_context(ctx: ssl.SSLContext) -> ssl.SSLContext:
    """Impide usar contextos TLS sin verificación de certificado o sin validación de hostname."""
    if ctx.verify_mode != ssl.CERT_REQUIRED or not ctx.check_hostname:
        raise ValueError(
            "Política TLS violada: ssl_context debe exigir ssl.CERT_REQUIRED y check_hostname=True."
        )
    return ctx


def _default_socket_factory(family: int, socktype: int, proto: int) -> socket.socket:
    return socket.socket(family, socktype, proto)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """
    Subclase de `http.client.HTTPConnection` que vincula la conexión TCP directamente
    a la dirección IP resuelta y validada por `WebNetworkPolicy`, sin segunda resolución DNS.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        network_policy: WebNetworkPolicy,
        connect_timeout: float,
        read_timeout: float,
        socket_factory: SocketFactory = _default_socket_factory,
    ) -> None:
        super().__init__(host=host, port=port, timeout=connect_timeout)
        self._network_policy = network_policy
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._socket_factory = socket_factory
        self.connected_endpoint: ResolvedEndpoint | None = None
        self.resolved_endpoints: tuple[ResolvedEndpoint, ...] = ()

    def connect(self) -> None:
        # Única resolución DNS para esta conexión, validando TODAS las IPs obtenidas.
        endpoints = self._network_policy.resolve_and_validate(self.host, self.port)
        self.resolved_endpoints = tuple(endpoints)
        target_ep = endpoints[0]

        sock = self._socket_factory(target_ep.family, target_ep.socktype, target_ep.proto)
        try:
            sock.settimeout(self._connect_timeout)
            # Marcamos tcp_nodelay si está disponible y conectamos por IP ya validada.
            sock.connect(target_ep.sockaddr)
            sock.settimeout(self._read_timeout)
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            raise

        self.sock = sock
        self.connected_endpoint = target_ep


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """
    Subclase de `http.client.HTTPSConnection` que conecta por IP validada y conserva
    el hostname original para SNI, verificación de certificado TLS y encabezado `Host`.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        network_policy: WebNetworkPolicy,
        ssl_context: ssl.SSLContext,
        connect_timeout: float,
        read_timeout: float,
        socket_factory: SocketFactory = _default_socket_factory,
    ) -> None:
        _validate_ssl_context(ssl_context)
        super().__init__(host=host, port=port, timeout=connect_timeout, context=ssl_context)
        self._network_policy = network_policy
        self._strict_ssl_context = ssl_context
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._socket_factory = socket_factory
        self.connected_endpoint: ResolvedEndpoint | None = None
        self.resolved_endpoints: tuple[ResolvedEndpoint, ...] = ()

    def connect(self) -> None:
        # Única resolución DNS para esta conexión, validando TODAS las IPs obtenidas.
        endpoints = self._network_policy.resolve_and_validate(self.host, self.port)
        self.resolved_endpoints = tuple(endpoints)
        target_ep = endpoints[0]

        raw_sock = self._socket_factory(target_ep.family, target_ep.socktype, target_ep.proto)
        try:
            raw_sock.settimeout(self._connect_timeout)
            raw_sock.connect(target_ep.sockaddr)
            raw_sock.settimeout(self._read_timeout)
            # Conservamos self.host original para SNI y validación del certificado TLS.
            tls_sock = self._strict_ssl_context.wrap_socket(raw_sock, server_hostname=self.host)
            tls_sock.settimeout(self._read_timeout)
        except Exception:
            try:
                raw_sock.close()
            except OSError:
                pass
            raise

        self.sock = tls_sock
        self.connected_endpoint = target_ep


def _parse_content_type(header_value: str | None) -> tuple[str, str | None]:
    """Extrae el tipo MIME limpio y el charset opcional de `Content-Type`."""
    if not header_value or not isinstance(header_value, str):
        return "", None
    parts = [part.strip() for part in header_value.split(";") if part.strip()]
    if not parts:
        return "", None
    mime_type = parts[0].lower()
    charset: str | None = None
    for param in parts[1:]:
        if "=" in param:
            key, val = param.split("=", 1)
            if key.strip().lower() == "charset":
                cleaned = val.strip().strip('"').strip("'").lower()
                if cleaned:
                    charset = cleaned
                break
    return mime_type, charset


def _validate_content_encoding(header_value: str | None) -> str:
    """
    Valida `Content-Encoding` rechazando codificaciones desconocidas o apiladas
    (por ejemplo `gzip, gzip` o `br`) antes de descargar el cuerpo.
    """
    if not header_value or not isinstance(header_value, str):
        return "identity"
    tokens = [tok.strip().lower() for tok in header_value.split(",") if tok.strip()]
    if not tokens:
        return "identity"
    if len(tokens) > 1:
        raise UnsupportedEncodingError(
            f"Codificación HTTP múltiple o apilada rechazada por seguridad: '{header_value}'."
        )
    enc = tokens[0]
    if enc not in SUPPORTED_ENCODINGS:
        raise UnsupportedEncodingError(
            f"Codificación de contenido no soportada: '{enc}'. Solo se admite identity, gzip y deflate."
        )
    return "identity" if enc == "" else ("gzip" if enc == "x-gzip" else enc)


class _IncrementalBoundedDecompressor:
    """
    Descompresor incremental estricto que aplica el límite de bytes descomprimidos
    ANTES de incorporar cualquier fragmento expandido a memoria.
    """

    def __init__(self, encoding: str, max_decompressed_bytes: int) -> None:
        self.encoding = encoding
        self.max_decompressed_bytes = max_decompressed_bytes
        self.total_decompressed: int = 0
        self._decompressor: Any = None
        self._raw_deflate_retried: bool = False
        self._first_chunk: bool = True

        if encoding == "gzip":
            self._decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            self._decompressor = zlib.decompressobj(zlib.MAX_WBITS)

    def feed(self, chunk: bytes) -> list[bytes]:
        if not chunk:
            return []

        if self.encoding == "identity":
            if self.total_decompressed + len(chunk) > self.max_decompressed_bytes:
                raise DecompressionBombError(
                    f"Contenido supera el límite máximo de {self.max_decompressed_bytes} bytes."
                )
            self.total_decompressed += len(chunk)
            self._first_chunk = False
            return [chunk]

        out_parts: list[bytes] = []
        pending = chunk

        while pending:
            remaining = self.max_decompressed_bytes - self.total_decompressed
            try:
                out = self._decompressor.decompress(pending, max_length=remaining + 1)
            except zlib.error as exc:
                # Algunos servidores envían deflate crudo sin cabecera zlib.
                if (
                    self.encoding == "deflate"
                    and self._first_chunk
                    and not self._raw_deflate_retried
                ):
                    self._raw_deflate_retried = True
                    self._decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
                    try:
                        out = self._decompressor.decompress(pending, max_length=remaining + 1)
                    except zlib.error as raw_exc:
                        raise UnsupportedEncodingError(
                            f"Flujo comprimido deflate inválido: {raw_exc}"
                        ) from raw_exc
                else:
                    raise UnsupportedEncodingError(
                        f"Flujo comprimido {self.encoding} corrupto o inválido: {exc}"
                    ) from exc

            self._first_chunk = False

            if self.total_decompressed + len(out) > self.max_decompressed_bytes:
                raise DecompressionBombError(
                    f"Posible bomba de descompresión: se superó el límite de {self.max_decompressed_bytes} bytes descomprimidos."
                )
            if self._decompressor.unconsumed_tail:
                raise DecompressionBombError(
                    f"Posible bomba de descompresión: quedaron bytes pendientes tras alcanzar el límite de {self.max_decompressed_bytes} bytes."
                )

            if out:
                self.total_decompressed += len(out)
                out_parts.append(out)

            if self._decompressor.eof:
                leftover = self._decompressor.unused_data
                if leftover:
                    if self.encoding == "gzip":
                        # Siguiente miembro gzip manteniendo el contador acumulado global.
                        self._decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
                        pending = leftover
                        continue
                    raise UnsupportedEncodingError(
                        "Datos residuales inesperados después del cierre del flujo deflate."
                    )
                pending = b""
            else:
                pending = b""

        return out_parts

    def finish(self) -> list[bytes]:
        if self.encoding == "identity" or self._decompressor is None:
            return []
        remaining = self.max_decompressed_bytes - self.total_decompressed
        try:
            tail = self._decompressor.flush()
        except zlib.error as exc:
            raise UnsupportedEncodingError(f"Error al finalizar descompresión {self.encoding}: {exc}") from exc
        if len(tail) > remaining:
            raise DecompressionBombError(
                f"Posible bomba de descompresión al vaciar buffers: límite de {self.max_decompressed_bytes} bytes superado."
            )
        # Verificar que el flujo comprimido haya cerrado formalmente su bloque final y trailer
        # (CRC32/ISIZE en gzip, Adler32/BFINAL en deflate) sin dejar datos inconclusos.
        if (
            not getattr(self._decompressor, "eof", False)
            or getattr(self._decompressor, "unused_data", b"")
            or getattr(self._decompressor, "unconsumed_tail", b"")
        ):
            raise UnsupportedEncodingError(
                f"Flujo comprimido {self.encoding} truncado o incompleto (EOF de compresión no alcanzado)."
            )
        self.total_decompressed += len(tail)
        return [tail] if tail else []


class WebFetcher:
    """
    Cliente HTTP/HTTPS desacoplado con seguridad SSRF vinculada al socket efectivo.
    """

    def __init__(
        self,
        *,
        config: WebExtractionConfig | None = None,
        network_policy: WebNetworkPolicy | None = None,
        ssl_context: ssl.SSLContext | None = None,
        socket_factory: SocketFactory | None = None,
    ) -> None:
        self.config: WebExtractionConfig = config or WebExtractionConfig.from_env()
        self.network_policy: WebNetworkPolicy = network_policy or WebNetworkPolicy(config=self.config)
        self._ssl_context: ssl.SSLContext = (
            _validate_ssl_context(ssl_context) if ssl_context is not None else _create_strict_ssl_context()
        )
        self._socket_factory: SocketFactory = socket_factory or _default_socket_factory

    def fetch(self, url: str) -> FetchResult:
        """
        Descarga una URL aplicando validación por salto de redirección, vinculación de IP
        en cada conexión, timeouts estrictos, streaming por chunks y límites de descompresión.
        """
        deadline = time.monotonic() + self.config.total_timeout_sec
        current_target = self.network_policy.validate_url(url)
        original_url = current_target.normalized_url
        redirect_chain: list[str] = [original_url]
        connected_ips: list[str] = []
        current_url = original_url

        try:
            for hop in range(self.config.max_redirects + 1):
                remaining_total = deadline - time.monotonic()
                if remaining_total <= 0:
                    raise WebTimeoutError(
                        f"Tiempo límite total agotado ({self.config.total_timeout_sec}s) antes de completar la descarga."
                    )

                hop_result = self._fetch_single_hop_with_retries(
                    current_target,
                    deadline=deadline,
                )

                if hop_result["connected_ip"]:
                    connected_ips.append(hop_result["connected_ip"])

                status = hop_result["status"]
                if status in REDIRECT_STATUS_CODES:
                    location = hop_result["location"]
                    if not location:
                        raise WebFetchError(
                            f"Respuesta de redirección HTTP {status} sin encabezado 'Location'.",
                            code="INVALID_REDIRECT",
                            http_status=status,
                        )
                    next_raw_url = urljoin(current_target.normalized_url, location).strip()
                    if hop >= self.config.max_redirects:
                        if next_raw_url and (not redirect_chain or redirect_chain[-1] != next_raw_url):
                            redirect_chain.append(next_raw_url)
                        current_url = next_raw_url or current_url
                        raise TooManyRedirectsError(
                            f"Se superó el máximo de {self.config.max_redirects} redirecciones permitidas."
                        )
                    # Registrar el destino del salto antes de validar para preservar trazabilidad
                    # incluso cuando el salto redirigido sea bloqueado por SSRF o puerto prohibido.
                    current_url = next_raw_url
                    if not redirect_chain or redirect_chain[-1] != next_raw_url:
                        redirect_chain.append(next_raw_url)
                    # Validación completa del nuevo destino ANTES de abrir la siguiente conexión.
                    current_target = self.network_policy.validate_url(next_raw_url)
                    current_url = current_target.normalized_url
                    redirect_chain[-1] = current_url
                    continue

                return FetchResult(
                    original_url=original_url,
                    final_url=current_target.normalized_url,
                    http_status=status,
                    content_type=hop_result["content_type"],
                    declared_charset=hop_result["declared_charset"],
                    content_language=hop_result["content_language"],
                    body_bytes=hop_result["body_bytes"],
                    bytes_received=hop_result["bytes_received"],
                    bytes_decompressed=hop_result["bytes_decompressed"],
                    redirect_chain=tuple(redirect_chain),
                    connected_ips=tuple(connected_ips),
                )

            raise TooManyRedirectsError(
                f"Se superó el máximo de {self.config.max_redirects} redirecciones permitidas."
            )
        except (WebSecurityError, WebFetchError) as exc:
            if not getattr(exc, "final_url", None):
                exc.final_url = current_url
            if not getattr(exc, "redirect_chain", ()):
                exc.redirect_chain = tuple(redirect_chain)
            raise

    def _fetch_single_hop_with_retries(
        self,
        target: ValidatedTarget,
        *,
        deadline: float,
    ) -> dict[str, Any]:
        """
        Ejecuta un salto HTTP creando una conexión nueva en cada intento.
        Los reintentos solo aplican a fallos transitorios de transporte y SIEMPRE revalidan DNS.
        """
        attempts = 1 + self.config.max_retries
        last_error: Exception | None = None

        for attempt in range(attempts):
            if deadline - time.monotonic() <= 0:
                raise WebTimeoutError(
                    f"Tiempo total agotado ({self.config.total_timeout_sec}s) durante la descarga."
                )
            try:
                return self._execute_single_request(target, deadline=deadline)
            except (WebSecurityError, TooManyRedirectsError, UnsupportedContentTypeError,
                    UnsupportedEncodingError, ResponseTooLargeError, DecompressionBombError,
                    TLSVerificationError):
                # Nunca reintentar bloqueos de seguridad, TLS o violaciones de tamaño/tipo.
                raise
            except WebFetchError as exc:
                last_error = exc
                if not exc.retryable or attempt >= attempts - 1 or (deadline - time.monotonic()) <= 0:
                    raise
            except Exception as exc:
                # Las excepciones no tipadas corresponden a errores permanentes o de validación interna,
                # nunca a fallos transitorios de red: no se reintentan.
                raise WebFetchError(
                    f"Error no recuperable al conectar con '{target.hostname}': {exc}",
                    code="FETCH_ERROR",
                    retryable=False,
                ) from exc

        assert last_error is not None
        raise last_error

    def _build_connection(
        self,
        target: ValidatedTarget,
        *,
        connect_timeout: float,
        read_timeout: float,
    ) -> _PinnedHTTPConnection | _PinnedHTTPSConnection:
        if target.scheme == "https":
            return _PinnedHTTPSConnection(
                target.hostname,
                target.port,
                network_policy=self.network_policy,
                ssl_context=self._ssl_context,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                socket_factory=self._socket_factory,
            )
        return _PinnedHTTPConnection(
            target.hostname,
            target.port,
            network_policy=self.network_policy,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            socket_factory=self._socket_factory,
        )

    def _execute_single_request(self, target: ValidatedTarget, *, deadline: float) -> dict[str, Any]:
        """Abre una conexión nueva, valida cabeceras y lee el cuerpo en streaming acotado."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WebTimeoutError(f"Tiempo total agotado antes de solicitar '{target.normalized_url}'.")

        eff_connect_timeout = min(self.config.connect_timeout_sec, remaining)
        eff_read_timeout = min(self.config.read_timeout_sec, remaining)

        conn = self._build_connection(
            target,
            connect_timeout=eff_connect_timeout,
            read_timeout=eff_read_timeout,
        )
        response: http.client.HTTPResponse | None = None

        try:
            headers = {
                "User-Agent": self.config.user_agent,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "close",
            }
            conn.request("GET", target.path_with_query, headers=headers)
            response = conn.getresponse()

            connected_ip = (
                conn.connected_endpoint.ip_address
                if getattr(conn, "connected_endpoint", None) is not None
                else ""
            )

            status = int(response.status)
            if status in REDIRECT_STATUS_CODES:
                return {
                    "status": status,
                    "location": response.getheader("Location"),
                    "connected_ip": connected_ip,
                }

            if status < 200 or status >= 300:
                retryable = status in (502, 503, 504)
                raise HTTPStatusError(
                    f"El servidor devolvió estado HTTP {status} para '{target.normalized_url}'.",
                    http_status=status,
                    retryable=retryable,
                )

            mime_type, declared_charset = _parse_content_type(response.getheader("Content-Type"))
            if not mime_type or mime_type not in self.config.allowed_content_types:
                raise UnsupportedContentTypeError(
                    f"Content-Type '{mime_type or 'ausente'}' no admitido para extracción.",
                    http_status=status,
                )

            encoding = _validate_content_encoding(response.getheader("Content-Encoding"))
            content_language = response.getheader("Content-Language")

            # Content-Length se usa solo como rechazo temprano; nunca como límite confiable.
            raw_cl = response.getheader("Content-Length")
            if raw_cl is not None:
                try:
                    declared_len = int(raw_cl.strip())
                    if declared_len > self.config.max_response_bytes:
                        raise ResponseTooLargeError(
                            f"Content-Length declarado ({declared_len} bytes) supera el máximo permitido ({self.config.max_response_bytes} bytes).",
                            http_status=status,
                        )
                except ValueError:
                    pass

            body_bytes, bytes_received, bytes_decompressed = self._read_bounded_stream(
                response,
                encoding=encoding,
                deadline=deadline,
                http_status=status,
            )

            return {
                "status": status,
                "location": None,
                "connected_ip": connected_ip,
                "content_type": mime_type,
                "declared_charset": declared_charset,
                "content_language": content_language,
                "body_bytes": body_bytes,
                "bytes_received": bytes_received,
                "bytes_decompressed": bytes_decompressed,
            }

        except (WebSecurityError, DNSResolutionError, WebFetchError):
            raise
        except ssl.SSLCertVerificationError as exc:
            raise TLSVerificationError(f"Verificación de certificado TLS rechazada: {exc}") from exc
        except ssl.SSLError as exc:
            raise TLSVerificationError(f"Error TLS en la conexión con '{target.hostname}': {exc}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise WebTimeoutError(f"Timeout de conexión/lectura con '{target.hostname}': {exc}") from exc
        except (http.client.HTTPException, OSError) as exc:
            raise WebFetchError(
                f"Fallo de red o protocolo HTTP con '{target.hostname}': {exc}",
                code="NETWORK_ERROR",
                retryable=True,
            ) from exc
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            try:
                conn.close()
            except Exception:
                pass

    def _read_bounded_stream(
        self,
        response: http.client.HTTPResponse,
        *,
        encoding: str,
        deadline: float,
        http_status: int,
    ) -> tuple[bytes, int, int]:
        """
        Lee el cuerpo HTTP incrementalmente por chunks, verificando bytes recibidos y
        bytes descomprimidos antes de almacenar cualquier dato en memoria.
        """
        bytes_received = 0
        decompressor = _IncrementalBoundedDecompressor(
            encoding=encoding,
            max_decompressed_bytes=self.config.max_decompressed_bytes,
        )
        decompressed_parts: list[bytes] = []

        while True:
            if deadline - time.monotonic() <= 0:
                raise WebTimeoutError(
                    f"Tiempo total agotado ({self.config.total_timeout_sec}s) durante la lectura del stream."
                )

            remaining_wire = self.config.max_response_bytes - bytes_received
            # Solicitamos hasta 1 byte por encima del restante para detectar exceso inmediato
            # sin leer un bloque completo extra cuando falta poco para el límite.
            to_read = min(self.config.chunk_size_bytes, remaining_wire + 1)
            chunk = response.read(to_read)
            if not chunk:
                break

            if bytes_received + len(chunk) > self.config.max_response_bytes:
                raise ResponseTooLargeError(
                    f"La descarga superó el límite máximo de {self.config.max_response_bytes} bytes recibidos.",
                    http_status=http_status,
                )

            bytes_received += len(chunk)
            decompressed_parts.extend(decompressor.feed(chunk))

        decompressed_parts.extend(decompressor.finish())
        return b"".join(decompressed_parts), bytes_received, decompressor.total_decompressed
