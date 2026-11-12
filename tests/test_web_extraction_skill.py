"""
Suite exhaustiva de pruebas unitarias, de seguridad de red (transporte real),
de límites HTTP/streaming e integración para `WebExtractionSkill` (Paso #3.B).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import unittest
from urllib.parse import urlsplit
import zlib

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from config.tools_schema import TOOLS_SCHEMA
from skills.document_export_skill import DocumentExportSkill
from skills.document_models import Callout, Citation
from skills.web_config import (
    WebExtractionConfig,
    parse_additional_ports_env,
    validate_additional_ports,
)
from skills.web_document_converter import WebDocumentConverter
from skills.web_extraction_skill import WebExtractionSkill
from skills.web_fetcher import WebFetcher
from skills.web_models import ExtractionStatus, SearchResult
from skills.web_search_provider import (
    SearchProvider,
    SearchResponse,
    UnconfiguredSearchProvider,
)
from skills.web_security import (
    DisallowedPortError,
    DisallowedSchemeError,
    InvalidURLError,
    SSRFBlockedError,
    WebNetworkPolicy,
    validate_ip_address,
)
from src.brain.intent_router import SKILL_CLASS_BY_TOOL_NAME
from src.tools.skill_manager import SkillManager


PUBLIC_TEST_IP = "93.184.216.34"
PUBLIC_TEST_IPV6 = "2606:2800:220:1:248:1893:25c8:1946"


def _make_routed_socket_factory(
    *,
    expected_remote_ip: str,
    local_port: int,
    recorded_connects: list[tuple[str, int]],
):
    """
    Crea un socket TCP real que registra la IP exacta solicitada por `_PinnedHTTPConnection`
    y redirige únicamente la conexión de prueba hacia el servidor local efímero.
    """

    class _RecordingLocalSocket(socket.socket):
        def connect(self, address: Any) -> None:
            host, port = address[0], address[1]
            recorded_connects.append((str(host), int(port)))
            if str(host) == expected_remote_ip:
                return super().connect(("127.0.0.1", local_port))
            return super().connect(address)

    def _factory(family: int, socktype: int, proto: int) -> socket.socket:
        return _RecordingLocalSocket(family, socktype, proto)

    return _factory


def _make_fixed_dns(ip_list: list[str]):
    """Construye un resolvedor compatible con `socket.getaddrinfo` para IPs controladas."""

    def _resolver(host: str, port: int, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        records: list[tuple[Any, ...]] = []
        for ip_str in ip_list:
            parsed = ipaddress.ip_address(ip_str)
            if parsed.version == 6:
                records.append((socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip_str, port, 0, 0)))
            else:
                records.append((socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip_str, port)))
        return records

    return _resolver


def _generate_ephemeral_tls_bundle(tmp_dir: Path, san_dns_names: list[str]) -> tuple[Path, Path, Path]:
    """
    Genera una CA efímera y un certificado de servidor firmado para validar TLS estricto + SNI.
    """
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "JARVIS-Test-CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(hours=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                key_encipherment=False,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    srv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    srv_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, san_dns_names[0])])
    alt_names = x509.SubjectAlternativeName([x509.DNSName(n) for n in san_dns_names])
    srv_cert = (
        x509.CertificateBuilder()
        .subject_name(srv_name)
        .issuer_name(ca_name)
        .public_key(srv_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(hours=2))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(alt_names, critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    ca_path = tmp_dir / "ca.pem"
    cert_path = tmp_dir / "server.crt"
    key_path = tmp_dir / "server.key"

    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(srv_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        srv_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return ca_path, cert_path, key_path


class TestWebConfigAndSecurityPolicy(unittest.TestCase):
    """Pruebas unitarias de configuración, esquema, puertos e IPv4/IPv6/mapped."""

    def test_port_policy_defaults_and_strict_validation(self) -> None:
        cfg = WebExtractionConfig()
        policy = WebNetworkPolicy(config=cfg)

        self.assertEqual(policy.validate_url("http://example.com/a").port, 80)
        self.assertEqual(policy.validate_url("https://example.com/a").port, 443)
        self.assertEqual(policy.validate_url("http://example.com:80/a").port, 80)
        self.assertEqual(policy.validate_url("https://example.com:443/a").port, 443)

        # Puertos no autorizados por defecto, incluyendo Ollama (11434), SMB (445) y dev (8080/8443).
        for bad_url in (
            "http://example.com:443/cross",
            "https://example.com:80/cross",
            "http://example.com:22/",
            "http://example.com:445/",
            "http://example.com:3306/",
            "http://example.com:8080/",
            "https://example.com:8443/",
            "http://example.com:11434/",
        ):
            with self.subTest(url=bad_url):
                with self.assertRaises(DisallowedPortError):
                    policy.validate_url(bad_url)

    def test_additional_ports_cannot_be_disabled_by_malformed_inputs(self) -> None:
        with self.assertRaises(TypeError):
            validate_additional_ports("8080")  # cadena rechazada
        with self.assertRaises(TypeError):
            validate_additional_ports([True])  # booleano rechazado
        for out_of_range in (0, -1, 65536, 70000):
            with self.assertRaises(ValueError):
                validate_additional_ports([out_of_range])

        parsed = parse_additional_ports_env("8080, *, all, -5, abc, 0, 99999, 8443")
        self.assertEqual(parsed, frozenset({8080, 8443}))

        cfg = WebExtractionConfig(additional_ports=frozenset({8443}))
        policy = WebNetworkPolicy(config=cfg)
        self.assertEqual(policy.validate_url("https://example.com:8443/path").port, 8443)
        with self.assertRaises(DisallowedPortError):
            policy.validate_url("https://example.com:9000/path")

    def test_schemes_credentials_and_host_obfuscations_rejected(self) -> None:
        policy = WebNetworkPolicy()

        for bad_scheme in (
            "file:///C:/Windows/System32/drivers/etc/hosts",
            "ftp://files.example.com/archive.zip",
            "javascript:alert(1)",
            "data:text/html,<h1>hola</h1>",
            "gopher://example.com:80/_GET",
            "example.com/no-scheme",
            "",
        ):
            with self.subTest(url=bad_scheme):
                with self.assertRaises((DisallowedSchemeError, InvalidURLError)):
                    policy.validate_url(bad_scheme)

        for cred_url in (
            "https://user:secret@example.com/",
            "http://admin@example.com/panel",
            "http://example.com\\@127.0.0.1/",
        ):
            with self.subTest(url=cred_url):
                with self.assertRaises(InvalidURLError):
                    policy.validate_url(cred_url)

        for ssrf_host_url in (
            "http://localhost/",
            "https://LOCALHOST./status",
            "http://api.localhost/info",
            "http://printer.local/config",
            "http://db.internal/",
            "http://service.home.arpa/",
            "http://wpad/wpad.dat",
            "http://intranet/admin",
            "http://2130706433/",
            "http://0x7f000001/",
            "http://0177.0.0.1/",
            "http://127.1/",
            "http://0x7f.0.0.1/",
            "http://１２７.０.０.１/",
            "http://ⓛⓞⓒⓐⓛⓗⓞⓢⓣ/",
            "http://[fe80::1%25eth0]/",
        ):
            with self.subTest(url=ssrf_host_url):
                with self.assertRaises((SSRFBlockedError, InvalidURLError)):
                    policy.validate_url(ssrf_host_url)

    def test_strict_ipv4_ipv6_and_mapped_validation(self) -> None:
        self.assertEqual(str(validate_ip_address(PUBLIC_TEST_IP)), PUBLIC_TEST_IP)
        self.assertEqual(str(validate_ip_address(PUBLIC_TEST_IPV6)), PUBLIC_TEST_IPV6)

        unsafe_ips = (
            "127.0.0.1",
            "127.255.0.1",
            "10.0.0.1",
            "172.16.5.4",
            "192.168.1.50",
            "169.254.169.254",  # Link-local / Cloud Metadata
            "100.64.0.1",       # CGNAT
            "0.0.0.0",
            "224.0.0.1",        # Multicast
            "240.0.0.1",        # Reserved
            "::1",              # IPv6 loopback
            "::",               # Unspecified
            "fc00::1",          # ULA
            "fd12:3456::1",     # ULA
            "fe80::1",          # Link-local IPv6
            "ff02::1",          # Multicast IPv6
            "::ffff:127.0.0.1",         # IPv4-mapped loopback
            "::ffff:169.254.169.254",   # IPv4-mapped metadata
            "::ffff:10.0.0.2",          # IPv4-mapped private
            "64:ff9b::127.0.0.1",       # NAT64 loopback
            "2002:7f00:0001::1",        # 6to4 loopback
        )
        for bad_ip in unsafe_ips:
            with self.subTest(ip=bad_ip):
                with self.assertRaises(SSRFBlockedError):
                    validate_ip_address(bad_ip)

    def test_multi_ip_dns_rejects_whole_target_if_any_record_is_unsafe(self) -> None:
        mixed_dns = _make_fixed_dns([PUBLIC_TEST_IP, "::ffff:127.0.0.1"])
        policy = WebNetworkPolicy(dns_resolver=mixed_dns)
        with self.assertRaises(SSRFBlockedError):
            policy.resolve_and_validate("dual.example.test", 80)

        mixed_dns_2 = _make_fixed_dns([PUBLIC_TEST_IP, "10.10.0.5"])
        policy2 = WebNetworkPolicy(dns_resolver=mixed_dns_2)
        with self.assertRaises(SSRFBlockedError):
            policy2.resolve_and_validate("dual2.example.test", 443)


class TestWebExtractionParsingAndNormalization(unittest.TestCase):
    """Pruebas de extracción HTML determinista, normalización y metadatos honestos."""

    def test_html_extraction_preserves_hierarchy_and_drops_noise(self) -> None:
        html_doc = """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>Arquitectura Python Modular</title>
  <meta name="description" content="Guía técnica sobre diseño desacoplado en Python.">
  <meta name="author" content="Ing. Sofía Vega">
  <link rel="canonical" href="/guias/python-modular">
  <style>.hidden { display: none; } body { margin: 0; }</style>
  <script>window.__SECRET__ = "NO_DEBE_APARECER_EN_TEXTO";</script>
</head>
<body>
  <div class="cookie-banner"><p>Accept all cookies para continuar.</p></div>
  <nav><a href="/inicio">Inicio</a><a href="/contacto">Contacto</a></nav>
  <aside><p>Publicidad lateral patrocinada</p></aside>
  <main>
    <article>
      <header>
        <h1>Arquitectura Python Modular</h1>
        <time datetime="2026-05-10">10 de mayo de 2026</time>
      </header>
      <p>Primer\u00a0párrafo\u200b con espacios   irregulares y <a href="/docs/intro#sec1">enlace interno</a>.</p>
      <p>Primer párrafo con espacios irregulares y enlace interno.</p>
      <p>Las cookies HTTP mantienen estado de sesión entre peticiones.</p>
      <h2>Principios Clave</h2>
      <ul>
        <li>Separación clara de responsabilidades</li>
        <li>Modelos inmutables de dominio</li>
      </ul>
      <p>Consulta la <a href="https://docs.python.org/3/library/html.parser.html">referencia oficial</a> y <a href="javascript:alert(1)">link peligroso</a>.</p>
    </article>
  </main>
  <footer><p>Copyright 2026 Pie de página</p></footer>
</body>
</html>""".encode("utf-8")

        from skills.web_fetcher import FetchResult
        from skills.web_parser import WebContentExtractor

        fetch_res = FetchResult(
            original_url="https://ejemplo.test/articulos/python",
            final_url="https://ejemplo.test/guias/python-modular",
            http_status=200,
            content_type="text/html",
            declared_charset="utf-8",
            content_language="es",
            body_bytes=html_doc,
            bytes_received=len(html_doc),
            bytes_decompressed=len(html_doc),
        )
        doc = WebContentExtractor().extract_from_fetch(fetch_res)

        self.assertEqual(doc.status, ExtractionStatus.SUCCESS)
        self.assertEqual(doc.metadata.title, "Arquitectura Python Modular")
        self.assertEqual(doc.metadata.description, "Guía técnica sobre diseño desacoplado en Python.")
        self.assertEqual(doc.metadata.author, "Ing. Sofía Vega")
        self.assertEqual(doc.metadata.published_at, "2026-05-10")
        self.assertEqual(doc.metadata.language, "es")
        self.assertEqual(doc.metadata.canonical_url, "https://ejemplo.test/guias/python-modular")
        self.assertNotEqual(doc.metadata.extracted_at, doc.metadata.published_at)

        # Ruido descartado, contenido técnico sobre cookies conservado, duplicado eliminado.
        joined = doc.normalized_text
        self.assertNotIn("NO_DEBE_APARECER_EN_TEXTO", joined)
        self.assertNotIn("Publicidad lateral", joined)
        self.assertNotIn("Pie de página", joined)
        self.assertNotIn("Accept all cookies", joined)
        self.assertIn("Las cookies HTTP mantienen estado de sesión", joined)
        self.assertNotIn("\u200b", joined)

        self.assertEqual([h.text for h in doc.headings], ["Arquitectura Python Modular", "Principios Clave"])
        self.assertEqual(len(doc.paragraphs), 3)
        self.assertEqual(len(doc.lists), 1)
        self.assertEqual(doc.lists[0].items, ("Separación clara de responsabilidades", "Modelos inmutables de dominio"))

        # Todos los fragmentos conservan trazabilidad a final_url.
        self.assertTrue(all(h.source_url == doc.final_url for h in doc.headings))
        self.assertTrue(all(p.source_url == doc.final_url for p in doc.paragraphs))
        self.assertTrue(all(lst.source_url == doc.final_url for lst in doc.lists))
        self.assertTrue(all(lk.source_url == doc.final_url for lk in doc.links))

        # Enlaces normalizados y sin javascript: ni nav.
        link_urls = [lk.url for lk in doc.links]
        self.assertIn("https://ejemplo.test/docs/intro", link_urls)
        self.assertIn("https://docs.python.org/3/library/html.parser.html", link_urls)
        self.assertFalse(any("javascript:" in u for u in link_urls))
        self.assertFalse(any("/contacto" in u for u in link_urls))

    def test_missing_metadata_is_never_invented_and_charset_fallback_warns(self) -> None:
        raw_bytes = b"<html><head><title>Nota ISO</title></head><body><p>Acent\xf3n \xff rota.</p></body></html>"
        from skills.web_fetcher import FetchResult
        from skills.web_parser import WebContentExtractor

        fetch_res = FetchResult(
            original_url="https://ejemplo.test/nota",
            final_url="https://ejemplo.test/nota",
            http_status=200,
            content_type="text/html",
            declared_charset=None,
            content_language=None,
            body_bytes=raw_bytes,
            bytes_received=len(raw_bytes),
            bytes_decompressed=len(raw_bytes),
        )
        doc = WebContentExtractor().extract_from_fetch(fetch_res)

        self.assertIsNone(doc.metadata.published_at)
        self.assertIsNone(doc.metadata.author)
        self.assertIsNone(doc.metadata.description)
        self.assertTrue(doc.metadata.extracted_at)
        self.assertTrue(any(w.code == "DECODE_REPLACEMENT" for w in doc.warnings))

    def test_normalized_text_truncation_marks_partial(self) -> None:
        from skills.web_fetcher import FetchResult
        from skills.web_parser import WebContentExtractor

        cfg = WebExtractionConfig(max_normalized_chars=120)
        body = ("<html><body><h1>Título</h1><p>" + ("palabra " * 60) + "</p></body></html>").encode("utf-8")
        doc = WebContentExtractor(cfg).extract_from_fetch(
            FetchResult(
                original_url="https://ejemplo.test/largo",
                final_url="https://ejemplo.test/largo",
                http_status=200,
                content_type="text/html",
                declared_charset="utf-8",
                content_language=None,
                body_bytes=body,
                bytes_received=len(body),
                bytes_decompressed=len(body),
            )
        )
        self.assertTrue(doc.truncated)
        self.assertLessEqual(len(doc.normalized_text), 120)
        self.assertEqual(doc.status, ExtractionStatus.PARTIAL)
        self.assertTrue(any(w.code == "NORMALIZED_TEXT_TRUNCATED" for w in doc.warnings))


class TestWebTransportSecurityAndStreamingRealServer(unittest.TestCase):
    """
    Pruebas reales de transporte HTTP/HTTPS contra servidores TCP/TLS locales efímeros.
    Verifica vinculación real de IP, verificación TLS/SNI, redirecciones, DNS rebinding,
    lectura acotada en streaming y bombas de descompresión.
    """

    def test_http_pinned_ip_redirects_and_ssrf_redirect_blocking(self) -> None:
        recorded_requests: list[tuple[str, str | None]] = []
        port_holder: dict[str, int] = {}

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                recorded_requests.append((self.path, self.headers.get("Host")))
                srv_port = port_holder["port"]
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", f"http://target.example.test:{srv_port}/final")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/final":
                    body = b"<html><head><title>Destino Final</title></head><body><h1>OK</h1><p>Contenido real.</p></body></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == "/redir-localhost":
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1/admin")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/redir-bad-port":
                    self.send_response(302)
                    self.send_header("Location", "http://target.example.test:22/ssh")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path.startswith("/loop"):
                    self.send_response(302)
                    self.send_header("Location", f"http://target.example.test:{srv_port}/loop")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        srv_port = server.server_address[1]
        port_holder["port"] = srv_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            recorded_connects: list[tuple[str, int]] = []
            cfg = WebExtractionConfig(additional_ports=frozenset({srv_port}), max_redirects=2)
            policy = WebNetworkPolicy(config=cfg, dns_resolver=_make_fixed_dns([PUBLIC_TEST_IP]))
            fetcher = WebFetcher(
                config=cfg,
                network_policy=policy,
                socket_factory=_make_routed_socket_factory(
                    expected_remote_ip=PUBLIC_TEST_IP,
                    local_port=srv_port,
                    recorded_connects=recorded_connects,
                ),
            )
            skill = WebExtractionSkill(config=cfg, network_policy=policy, fetcher=fetcher)

            # 1. Redirección pública legítima y verificación de IP vinculada real.
            doc = skill.extract_url(f"http://target.example.test:{srv_port}/start")
            self.assertEqual(doc.status, ExtractionStatus.SUCCESS)
            self.assertEqual(doc.final_url, f"http://target.example.test:{srv_port}/final")
            self.assertEqual(len(doc.redirect_chain), 2)
            self.assertEqual(recorded_connects, [(PUBLIC_TEST_IP, srv_port), (PUBLIC_TEST_IP, srv_port)])
            self.assertEqual(recorded_requests[0], ("/start", f"target.example.test:{srv_port}"))

            # 2. Redirección hacia 127.0.0.1 bloqueada sin conectar al salto 2.
            before_count = len(recorded_connects)
            blocked = skill.extract_url(f"http://target.example.test:{srv_port}/redir-localhost")
            self.assertEqual(blocked.status, ExtractionStatus.BLOCKED)
            self.assertEqual(blocked.errors[0].code, "SSRF_BLOCKED")
            self.assertEqual(len(recorded_connects), before_count + 1)

            # 3. Redirección hacia puerto prohibido (:22) bloqueada antes de abrir socket al salto 2.
            before_count = len(recorded_connects)
            bad_port_doc = skill.extract_url(f"http://target.example.test:{srv_port}/redir-bad-port")
            self.assertEqual(bad_port_doc.status, ExtractionStatus.BLOCKED)
            self.assertEqual(bad_port_doc.errors[0].code, "DISALLOWED_PORT")
            self.assertEqual(len(recorded_connects), before_count + 1)

            # 4. Bucle infinito de redirecciones acotado por max_redirects.
            loop_doc = skill.extract_url(f"http://target.example.test:{srv_port}/loop")
            self.assertEqual(loop_doc.status, ExtractionStatus.FAILED)
            self.assertEqual(loop_doc.errors[0].code, "TOO_MANY_REDIRECTS")
        finally:
            server.shutdown()
            server.server_close()

    def test_dns_rebinding_blocked_on_retry_and_subsequent_connection(self) -> None:
        port_holder: dict[str, int] = {}

        class _FlakyHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), _FlakyHandler)
        srv_port = server.server_address[1]
        port_holder["port"] = srv_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        dns_calls = 0

        def _rebinding_dns(host: str, port: int, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
            nonlocal dns_calls
            dns_calls += 1
            if dns_calls == 1:
                return _make_fixed_dns([PUBLIC_TEST_IP])(host, port)
            # En el segundo intento DNS apunta repentinamente a loopback interno (DNS rebinding).
            return _make_fixed_dns(["127.0.0.1"])(host, port)

        try:
            recorded_connects: list[tuple[str, int]] = []
            cfg = WebExtractionConfig(additional_ports=frozenset({srv_port}), max_retries=1)
            policy = WebNetworkPolicy(config=cfg, dns_resolver=_rebinding_dns)
            fetcher = WebFetcher(
                config=cfg,
                network_policy=policy,
                socket_factory=_make_routed_socket_factory(
                    expected_remote_ip=PUBLIC_TEST_IP,
                    local_port=srv_port,
                    recorded_connects=recorded_connects,
                ),
            )
            skill = WebExtractionSkill(config=cfg, network_policy=policy, fetcher=fetcher)
            doc = skill.extract_url(f"http://rebind.example.test:{srv_port}/data")

            self.assertEqual(dns_calls, 2)
            self.assertEqual(doc.status, ExtractionStatus.BLOCKED)
            self.assertEqual(doc.errors[0].code, "SSRF_BLOCKED")
            # Solo hubo 1 conexión física (al primer DNS público); el reintento fue abortado antes del connect().
            self.assertEqual(recorded_connects, [(PUBLIC_TEST_IP, srv_port)])
        finally:
            server.shutdown()
            server.server_close()

    def test_https_pinned_ip_preserves_sni_host_and_strict_tls_verification(self) -> None:
        observed_tls: list[tuple[str | None, str | None]] = []

        class _TlsHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                body = b"<html><head><title>HTTPS Seguro</title></head><body><p>TLS verificado.</p></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ca_path, cert_path, key_path = _generate_ephemeral_tls_bundle(tmp_dir, ["docs.example.test"])

            server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))

            def _sni_cb(ssl_sock: ssl.SSLSocket, server_name: str | None, ctx: ssl.SSLContext) -> None:
                observed_tls.append((server_name, ssl_sock.version()))

            server_ctx.sni_callback = _sni_cb

            server = ThreadingHTTPServer(("127.0.0.1", 0), _TlsHandler)
            server.socket = server_ctx.wrap_socket(server.socket, server_side=True)
            srv_port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            try:
                client_ctx = ssl.create_default_context(cafile=str(ca_path))
                client_ctx.check_hostname = True
                client_ctx.verify_mode = ssl.CERT_REQUIRED

                recorded_connects: list[tuple[str, int]] = []
                cfg = WebExtractionConfig(additional_ports=frozenset({srv_port}))
                policy = WebNetworkPolicy(config=cfg, dns_resolver=_make_fixed_dns([PUBLIC_TEST_IP]))
                fetcher = WebFetcher(
                    config=cfg,
                    network_policy=policy,
                    ssl_context=client_ctx,
                    socket_factory=_make_routed_socket_factory(
                        expected_remote_ip=PUBLIC_TEST_IP,
                        local_port=srv_port,
                        recorded_connects=recorded_connects,
                    ),
                )
                skill = WebExtractionSkill(config=cfg, network_policy=policy, fetcher=fetcher)

                # 1. Socket TLS real conecta a la IP pinneada (93.184.216.34) preservando SNI real.
                # Nota: si un antivirus/proxy local del host intercepta el tráfico TLS loopback con su
                # propia raíz no confiable, la verificación estricta debe rechazarlo con TLS_ERROR;
                # en cualquier caso, el SNI real ya habrá viajado y quedado registrado en server.sni_callback.
                doc_tls = skill.extract_url(f"https://docs.example.test:{srv_port}/guia")
                self.assertEqual(recorded_connects, [(PUBLIC_TEST_IP, srv_port)])
                self.assertIn("docs.example.test", [item[0] for item in observed_tls])
                if doc_tls.status == ExtractionStatus.SUCCESS:
                    self.assertEqual(doc_tls.title, "HTTPS Seguro")
                else:
                    self.assertEqual(doc_tls.status, ExtractionStatus.FAILED)
                    self.assertEqual(doc_tls.errors[0].code, "TLS_ERROR")

                # 2. Mismatch de hostname rechazado por verificación estricta del certificado.
                bad_host_doc = skill.extract_url(f"https://wrong-host.example.test:{srv_port}/guia")
                self.assertEqual(bad_host_doc.status, ExtractionStatus.FAILED)
                self.assertEqual(bad_host_doc.errors[0].code, "TLS_ERROR")

                # 3. Contexto sin CA de confianza falla verificación TLS.
                default_fetcher = WebFetcher(
                    config=cfg,
                    network_policy=policy,
                    socket_factory=_make_routed_socket_factory(
                        expected_remote_ip=PUBLIC_TEST_IP,
                        local_port=srv_port,
                        recorded_connects=recorded_connects,
                    ),
                )
                untrusted = WebExtractionSkill(config=cfg, network_policy=policy, fetcher=default_fetcher).extract_url(
                    f"https://docs.example.test:{srv_port}/guia"
                )
                self.assertEqual(untrusted.status, ExtractionStatus.FAILED)
                self.assertEqual(untrusted.errors[0].code, "TLS_ERROR")

                # 4. WebFetcher rechaza explícitamente intentar inyectar un contexto TLS permisivo.
                insecure_ctx = ssl.create_default_context()
                insecure_ctx.check_hostname = False
                insecure_ctx.verify_mode = ssl.CERT_NONE
                with self.assertRaises(ValueError):
                    WebFetcher(config=cfg, ssl_context=insecure_ctx)
            finally:
                server.shutdown()
                server.server_close()

            # 5. Verificación OpenSSL pura (MemoryBIO) del handshake estricto CERT_REQUIRED + check_hostname
            # sobre la CA efímera, independiente de interceptación WinSock del antivirus del host.
            cctx = ssl.create_default_context(cafile=str(ca_path))
            cctx.check_hostname = True
            cctx.verify_mode = ssl.CERT_REQUIRED
            sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            sctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
            bio_sni: list[str | None] = []
            sctx.sni_callback = lambda _s, name, _c: bio_sni.append(name)

            def _run_bio_handshake(hostname: str) -> tuple[str | None, bool]:
                c_in, c_out = ssl.MemoryBIO(), ssl.MemoryBIO()
                s_in, s_out = ssl.MemoryBIO(), ssl.MemoryBIO()
                c_obj = cctx.wrap_bio(c_in, c_out, server_side=False, server_hostname=hostname)
                s_obj = sctx.wrap_bio(s_in, s_out, server_side=True)
                for _ in range(10):
                    try:
                        c_obj.do_handshake()
                        c_done = True
                    except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                        c_done = False
                    s_in.write(c_out.read())
                    try:
                        s_obj.do_handshake()
                        s_done = True
                    except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                        s_done = False
                    c_in.write(s_out.read())
                    if c_done and s_done:
                        return c_obj.version(), True
                return None, False

            tls_ver, handshook = _run_bio_handshake("docs.example.test")
            self.assertTrue(handshook)
            self.assertIsNotNone(tls_ver)
            self.assertEqual(bio_sni, ["docs.example.test"])
            with self.assertRaises(ssl.SSLCertVerificationError):
                _run_bio_handshake("wrong-host.example.test")

    def test_streaming_limits_content_type_decompression_bomb_and_timeout(self) -> None:
        server_aborted = threading.Event()
        bomb_payload = gzip.compress(b"A" * (512 * 1024), compresslevel=9)  # ~1 KiB comprimido -> 512 KiB

        class _StreamHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                if self.path == "/pdf":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/pdf")
                    self.send_header("Content-Length", "4")
                    self.end_headers()
                    self.wfile.write(b"%PDF")
                    return

                if self.path == "/bad-encoding":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Encoding", "br")
                    self.send_header("Content-Length", "4")
                    self.end_headers()
                    self.wfile.write(b"1234")
                    return

                if self.path == "/chunked-huge":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    chunk_data = b"<p>" + (b"x" * 1020) + b"</p>"
                    frame = f"{len(chunk_data):x}\r\n".encode("ascii") + chunk_data + b"\r\n"
                    try:
                        for _ in range(200):
                            self.wfile.write(frame)
                            self.wfile.flush()
                        self.wfile.write(b"0\r\n\r\n")
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        server_aborted.set()
                    return

                if self.path == "/lying-length":
                    body = b"<html><body>" + (b"Z" * 10_000) + b"</body></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    # Cabecera engañosa mucho menor al tamaño real enviado:
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    return

                if self.path == "/declared-huge":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", "10000")
                    self.end_headers()
                    return

                if self.path == "/no-length-huge":
                    raw = b"<html><body>" + (b"M" * 20_000) + b"</body></html>"
                    self.wfile.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n"
                    )
                    try:
                        for i in range(0, len(raw), 512):
                            self.wfile.write(raw[i : i + 512])
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    return

                if self.path == "/gzip-bomb":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Encoding", "gzip")
                    self.send_header("Content-Length", str(len(bomb_payload)))
                    self.end_headers()
                    self.wfile.write(bomb_payload)
                    return

                if self.path == "/slow":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                    time.sleep(0.5)
                    return

                self.send_response(404)
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), _StreamHandler)
        srv_port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            cfg = WebExtractionConfig(
                additional_ports=frozenset({srv_port}),
                max_response_bytes=4096,
                max_decompressed_bytes=8192,
                chunk_size_bytes=512,
                connect_timeout_sec=1.0,
                read_timeout_sec=0.15,
                total_timeout_sec=1.5,
                max_retries=0,
            )
            policy = WebNetworkPolicy(config=cfg, dns_resolver=_make_fixed_dns([PUBLIC_TEST_IP]))
            recorded_connects: list[tuple[str, int]] = []
            fetcher = WebFetcher(
                config=cfg,
                network_policy=policy,
                socket_factory=_make_routed_socket_factory(
                    expected_remote_ip=PUBLIC_TEST_IP,
                    local_port=srv_port,
                    recorded_connects=recorded_connects,
                ),
            )
            skill = WebExtractionSkill(config=cfg, network_policy=policy, fetcher=fetcher)

            pdf_doc = skill.extract_url(f"http://stream.example.test:{srv_port}/pdf")
            self.assertEqual(pdf_doc.status, ExtractionStatus.FAILED)
            self.assertEqual(pdf_doc.errors[0].code, "UNSUPPORTED_CONTENT_TYPE")

            enc_doc = skill.extract_url(f"http://stream.example.test:{srv_port}/bad-encoding")
            self.assertEqual(enc_doc.errors[0].code, "UNSUPPORTED_ENCODING")

            huge_doc = skill.extract_url(f"http://stream.example.test:{srv_port}/chunked-huge")
            self.assertEqual(huge_doc.status, ExtractionStatus.FAILED)
            self.assertEqual(huge_doc.errors[0].code, "RESPONSE_TOO_LARGE")

            lying_doc = skill.extract_url(f"http://stream.example.test:{srv_port}/lying-length")
            self.assertLessEqual(lying_doc.bytes_received, 100)

            decl_huge_doc = skill.extract_url(f"http://stream.example.test:{srv_port}/declared-huge")
            self.assertEqual(decl_huge_doc.errors[0].code, "RESPONSE_TOO_LARGE")

            no_len_doc = skill.extract_url(f"http://stream.example.test:{srv_port}/no-length-huge")
            self.assertEqual(no_len_doc.errors[0].code, "RESPONSE_TOO_LARGE")

            bomb_doc = skill.extract_url(f"http://stream.example.test:{srv_port}/gzip-bomb")
            self.assertEqual(bomb_doc.status, ExtractionStatus.FAILED)
            self.assertEqual(bomb_doc.errors[0].code, "DECOMPRESSION_BOMB")

            slow_doc = skill.extract_url(f"http://stream.example.test:{srv_port}/slow")
            self.assertEqual(slow_doc.status, ExtractionStatus.FAILED)
            self.assertEqual(slow_doc.errors[0].code, "TIMEOUT")
        finally:
            server.shutdown()
            server.server_close()


class _FakeSearchProvider(SearchProvider):
    def __init__(self, urls: list[str]) -> None:
        self._urls = urls

    @property
    def provider_name(self) -> str:
        return "test_search"

    @property
    def is_available(self) -> bool:
        return True

    def search(self, query: str, *, max_results: int = 5) -> SearchResponse:
        items = tuple(
            SearchResult(
                title=f"Resultado {i}",
                url=url,
                description=f"Snippet para {query} #{i}",
                provider=self.provider_name,
                rank=i,
            )
            for i, url in enumerate(self._urls[:max_results], start=1)
        )
        return SearchResponse(provider_name=self.provider_name, available=True, results=items)


class TestSkillIntegrationAndDocumentConverter(unittest.TestCase):
    """Pruebas de colección múltiple, SearchProvider, SkillManager, carga perezosa y Paso #3.A."""

    def test_unconfigured_search_provider_is_honest_and_direct_urls_work(self) -> None:
        skill = WebExtractionSkill(search_provider=UnconfiguredSearchProvider())
        report = skill.extract_query("arquitectura python modular")
        self.assertEqual(report.status, ExtractionStatus.FAILED)
        self.assertEqual(report.search_provider, "unconfigured")
        self.assertEqual(report.errors[0].code, "SEARCH_PROVIDER_NOT_CONFIGURED")
        self.assertIn("url", report.errors[0].message.lower())

        summary = skill.execute({"query": "arquitectura python modular"})
        self.assertIn("SEARCH_PROVIDER_NOT_CONFIGURED", summary)

    def test_multi_url_partial_failures_concurrency_and_search_provider(self) -> None:
        active_lock = threading.Lock()
        active_count = 0
        peak_active = 0

        class _BatchHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                nonlocal active_count, peak_active
                with active_lock:
                    active_count += 1
                    peak_active = max(peak_active, active_count)
                try:
                    time.sleep(0.05)
                    if self.path in ("/a", "/b", "/c"):
                        html = (
                            f"<html><head><title>Página {self.path}</title></head>"
                            f"<body><h1>Encabezado {self.path}</h1><p>Texto normalizado {self.path}.</p></body></html>"
                        ).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(html)))
                        self.end_headers()
                        self.wfile.write(html)
                    else:
                        self.send_response(404)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                finally:
                    with active_lock:
                        active_count -= 1

        server = ThreadingHTTPServer(("127.0.0.1", 0), _BatchHandler)
        srv_port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            cfg = WebExtractionConfig(
                additional_ports=frozenset({srv_port}),
                max_pages=4,
                max_concurrent_connections=2,
            )
            policy = WebNetworkPolicy(config=cfg, dns_resolver=_make_fixed_dns([PUBLIC_TEST_IP]))
            connects: list[tuple[str, int]] = []
            fetcher = WebFetcher(
                config=cfg,
                network_policy=policy,
                socket_factory=_make_routed_socket_factory(
                    expected_remote_ip=PUBLIC_TEST_IP,
                    local_port=srv_port,
                    recorded_connects=connects,
                ),
            )
            provider = _FakeSearchProvider(
                [
                    f"http://srv.example.test:{srv_port}/a",
                    f"http://srv.example.test:{srv_port}/missing",
                    "http://127.0.0.1/secret",  # SSRF inyectado por proveedor: debe bloquearse igual.
                    f"http://srv.example.test:{srv_port}/b",
                    f"http://srv.example.test:{srv_port}/c",  # Excede max_pages=4 -> acotada con warning.
                ]
            )
            skill = WebExtractionSkill(
                config=cfg,
                network_policy=policy,
                fetcher=fetcher,
                search_provider=provider,
            )

            report = skill.extract_query("tema pruebas", max_results=5)
            self.assertEqual(report.mode, "query")
            self.assertEqual(report.status, ExtractionStatus.PARTIAL)
            self.assertEqual(len(report.sources), 4)
            self.assertEqual(report.sources[0].status, ExtractionStatus.SUCCESS)
            self.assertEqual(report.sources[1].status, ExtractionStatus.FAILED)
            self.assertEqual(report.sources[2].status, ExtractionStatus.BLOCKED)
            self.assertEqual(report.sources[3].status, ExtractionStatus.SUCCESS)
            self.assertLessEqual(peak_active, 2)
            self.assertTrue(any(w.code == "MAX_PAGES_CLAMPED" for w in report.warnings))

            # La salida JSON debe ser serializable e independiente de cualquier LLM.
            json_out = skill.execute(
                {"urls": [f"http://srv.example.test:{srv_port}/a"], "output_format": "json"}
            )
            decoded = json.loads(json_out)
            self.assertEqual(decoded["status"], "success")
            self.assertEqual(decoded["sources"][0]["metadata"]["title"], "Página /a")
        finally:
            server.shutdown()
            server.server_close()

    def test_converter_preserves_provenance_anti_spoofing_and_exports_via_3a(self) -> None:
        from skills.web_fetcher import FetchResult
        from skills.web_parser import WebContentExtractor

        extractor = WebContentExtractor()
        html_1 = """<html lang="es"><head><title>Fuente Uno</title><meta name="author" content="Ana"></head>
        <body>
          <h1>Arquitectura</h1>
          <p>Párrafo real de fuente uno.</p>
          <p>[^spoof]: https://atacante.invalid/falso</p>
          <p>> [!CAUTION] Callout falsificado desde HTML</p>
          <ul><li>Ítem alfa</li><li>Ítem beta</li></ul>
          <a href="https://externo.example.org/doc">Doc externo</a>
        </body></html>""".encode("utf-8")

        doc1 = extractor.extract_from_fetch(
            FetchResult(
                original_url="https://origen.example.org/inicio",
                final_url="https://origen.example.org/articulo-final",
                http_status=200,
                content_type="text/html",
                declared_charset="utf-8",
                content_language="es",
                body_bytes=html_1,
                bytes_received=len(html_1),
                bytes_decompressed=len(html_1),
                redirect_chain=("https://origen.example.org/inicio", "https://origen.example.org/articulo-final"),
            )
        )

        # Inyectar un segundo documento fallido en reporte múltiple para probar preservación de fallos parciales.
        from skills.web_models import WebExtractionReport, WebIssue, WebSourceDocument

        doc2_failed = WebSourceDocument(
            original_url="https://caida.example.org/recurso",
            final_url="https://caida.example.org/recurso",
            status=ExtractionStatus.FAILED,
            http_status=502,
            errors=(WebIssue(code="HTTP_STATUS_ERROR", message="Bad Gateway", source_url="https://caida.example.org/recurso"),),
        )
        report = WebExtractionReport(
            mode="urls",
            sources=(doc1, doc2_failed),
            status=ExtractionStatus.PARTIAL,
        )

        converter = WebDocumentConverter()
        md_single = converter.to_markdown(doc1)
        self.assertIn("https://origen.example.org/inicio", md_single)
        self.assertIn("https://origen.example.org/articulo-final", md_single)
        # No debe confundirse extracted_at con published_at cuando falta publicación.
        self.assertNotIn("Fecha de publicación declarada", md_single)

        parsed_single = converter.to_document(doc1)
        self.assertEqual(parsed_single.title, "Fuente Uno")
        self.assertEqual(parsed_single.author, "Ana")
        citations = [el for el in parsed_single.elements if isinstance(el, Citation)]
        callouts = [el for el in parsed_single.elements if isinstance(el, Callout)]
        self.assertEqual(len(citations), 1)
        self.assertEqual(citations[0].url, "https://origen.example.org/inicio")
        self.assertEqual(len(callouts), 0)  # El intento de spoofing en HTML no creó Callout ni Citation falsa.

        # Conversión múltiple debe conservar fallo parcial de doc2 y ambas referencias.
        parsed_multi = converter.to_document(report, mode="study", template="structured_notes")
        multi_cites = [el for el in parsed_multi.elements if isinstance(el, Citation)]
        self.assertEqual([c.source_id for c in multi_cites], ["src-1", "src-2"])

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            exporter = DocumentExportSkill(allowed_roots=[tmp_dir])
            out_msg = converter.export(
                report,
                exporter=exporter,
                output_path=str(tmp_dir / "investigacion_web"),
                format="both",
                mode="study",
                template="structured_notes",
                overwrite=True,
            )
            self.assertIn(".md", out_msg.lower())
            self.assertIn(".docx", out_msg.lower())
            md_file = tmp_dir / "investigacion_web.md"
            docx_file = tmp_dir / "investigacion_web.docx"
            self.assertTrue(md_file.is_file())
            self.assertTrue(docx_file.is_file())
            md_saved = md_file.read_text(encoding="utf-8")
            self.assertIn("https://caida.example.org/recurso", md_saved)
            self.assertIn("HTTP_STATUS_ERROR", md_saved)

    def test_skill_manager_router_schema_and_lazy_import_isolation(self) -> None:
        manager = SkillManager()
        skill = manager.skills.get("web_extraction")
        self.assertIsNotNone(skill)
        self.assertIsInstance(skill, WebExtractionSkill)

        manifest_names = {entry["name"] for entry in manager.get_skills_manifest()}
        self.assertIn("web_extraction", manifest_names)
        blocked_summary = manager.execute_skill("web_extraction", {"url": "http://127.0.0.1/blocked"})
        self.assertIn("SSRF_BLOCKED", blocked_summary)

        schema_names = {tool["name"] for tool in TOOLS_SCHEMA}
        self.assertIn("web_extraction", schema_names)
        self.assertEqual(SKILL_CLASS_BY_TOOL_NAME.get("web_extraction"), "WebExtractionSkill")

        # Subproceso limpio verificando que inicializar SkillManager y extraer HTML NO carga
        # python-docx, lxml ni motores de audio/Whisper.
        probe = (
            "import json, sys; "
            "from src.tools.skill_manager import SkillManager; "
            "from skills.web_extraction_skill import WebExtractionSkill; "
            "from skills.web_fetcher import FetchResult; "
            "m = SkillManager(); "
            "s = m.skills.get('web_extraction'); "
            "_ = s.extractor.extract_from_fetch(FetchResult("
            "original_url='https://ejemplo.test/', final_url='https://ejemplo.test/', "
            "http_status=200, content_type='text/html', declared_charset='utf-8', "
            "content_language='es', body_bytes=b'<html><body><p>Hola</p></body></html>', "
            "bytes_received=38, bytes_decompressed=38)); "
            "blocked = {'docx', 'lxml', 'faster_whisper', 'sounddevice', 'skills.web_document_converter'}; "
            "print(json.dumps(sorted(mod for mod in blocked if mod in sys.modules)))"
        )
        env = dict(os.environ)
        env["JARVIS_ENV"] = "test"
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        loaded_heavy = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(loaded_heavy, [])


if __name__ == "__main__":
    unittest.main()
