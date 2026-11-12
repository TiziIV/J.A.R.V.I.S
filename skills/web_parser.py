"""
Parser HTML y motor de normalización determinista para `WebExtractionSkill`.

Responsabilidades:
1. Decodificar de forma segura los bytes HTTP (cabecera HTTP, BOM, `<meta charset>`,
   UTF-8 estricto y respaldo controlado sin fallar por caracteres malformados).
2. Parsear HTML con `html.parser.HTMLParser` (biblioteca estándar, sin dependencias pesadas)
   descartando scripts, estilos, comentarios, SVG, iframes y ruido de navegación.
3. Priorizar el contenido principal (`<main>` / `<article>`) cuando existe y aporta bloques
   reales, conservando encabezados (H1-H6), párrafos, listas, enlaces y metadatos reales.
4. Normalizar espacios/Unicode, eliminar duplicados y ruido evidente, y preservar el orden
   jerárquico de secciones junto con la trazabilidad (`source_url`) de cada fragmento.
5. Nunca inventar metadatos ausentes ni confundir `published_at` con `extracted_at`.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass, field
from html.parser import HTMLParser
import json
import re
import unicodedata
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from skills.web_config import WebExtractionConfig
from skills.web_fetcher import FetchResult
from skills.web_models import (
    ExtractionStatus,
    WebHeading,
    WebIssue,
    WebLink,
    WebList,
    WebMetadata,
    WebParagraph,
    WebSection,
    WebSourceDocument,
    utc_now_iso,
)

# Etiquetas cuyo contenido textual jamás debe incorporarse al cuerpo visible.
_IGNORED_TAGS: frozenset[str] = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "canvas",
        "iframe",
        "object",
        "embed",
        "video",
        "audio",
        "map",
        "select",
        "textarea",
        "button",
    }
)

# Contenedores de navegación / cromado habitual fuera del contenido principal.
_CHROME_TAGS: frozenset[str] = frozenset({"nav", "aside", "footer", "form"})

# Roles ARIA que representan navegación o ruido ajeno al contenido informativo.
_NOISE_ROLES: frozenset[str] = frozenset(
    {
        "navigation",
        "banner",
        "contentinfo",
        "complementary",
        "search",
        "dialog",
        "alertdialog",
        "menu",
        "menubar",
        "toolbar",
    }
)

# Patrones de clase/id frecuentes en banners de cookies, menús laterales o publicidad.
_NOISE_ATTR_RE = re.compile(
    r"(?:^|[\s_-])("
    r"cookie(?:s|-?banner|-?consent|-?notice)?|"
    r"gdpr|consent-?banner|privacy-?banner|"
    r"nav(?:bar|-?menu|-?links)?|breadcrumb(?:s)?|"
    r"sidebar|side-?bar|widget-?area|"
    r"advert(?:isement)?|ad-?banner|promo-?box|sponsored|"
    r"newsletter-?signup|social-?share|share-?buttons|"
    r"skip-?to-?content|skip-?link"
    r")(?:$|[\s_-])",
    re.IGNORECASE,
)

# Frases cortas completas típicas de ruido UI (solo descartan bloques breves completos).
_EXACT_NOISE_PHRASES: frozenset[str] = frozenset(
    {
        "accept cookies",
        "accept all cookies",
        "reject all cookies",
        "manage cookies",
        "cookie settings",
        "aceptar cookies",
        "aceptar todas las cookies",
        "rechazar cookies",
        "configurar cookies",
        "skip to content",
        "skip to main content",
        "saltar al contenido",
        "ir al contenido principal",
        "menu",
        "menú",
        "close",
        "cerrar",
        "subscribe",
        "suscribirse",
        "share this",
        "compartir",
        "back to top",
        "volver arriba",
        "javascript is required",
        "please enable javascript",
    }
)

_HEADING_TAGS: dict[str, int] = {
    "h1": 1,
    "h2": 2,
    "h3": 3,
    "h4": 4,
    "h5": 5,
    "h6": 6,
}

_PARAGRAPH_TAGS: frozenset[str] = frozenset({"p", "blockquote", "figcaption", "pre", "dd", "dt"})
_VOID_TAGS: frozenset[str] = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_INLINE_TAGS: frozenset[str] = frozenset(
    {
        "a",
        "abbr",
        "b",
        "bdi",
        "bdo",
        "cite",
        "code",
        "data",
        "del",
        "dfn",
        "em",
        "i",
        "ins",
        "kbd",
        "label",
        "mark",
        "q",
        "s",
        "samp",
        "small",
        "span",
        "strong",
        "sub",
        "sup",
        "time",
        "u",
        "var",
    }
)
_CONTAINER_TAGS: frozenset[str] = frozenset(
    {"html", "body", "main", "article", "section", "div", "header", "figure", "details", "summary", "td", "th"}
)
_BODY_INITIATOR_TAGS: frozenset[str] = frozenset(
    {
        "body",
        "main",
        "article",
        "section",
        "div",
        "header",
        "footer",
        "nav",
        "aside",
        "p",
        "ul",
        "ol",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "pre",
        "table",
        "figure",
    }
)

# Caracteres invisibles, marcas/aislamientos/overrides bidireccionales Unicode (U+202A..U+202E,
# U+2066..U+2069, U+200E, U+200F, U+061C) y bloque Unicode Tags (U+E0000..U+E007F) usados en
# ofuscación visual e inyección indirecta de prompts.
_INVISIBLE_CHARS_RE = re.compile(
    r"[\u00ad\u061c\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\ufff9-\ufffb\U000e0000-\U000e007f]+"
)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_WHITESPACE_RE = re.compile(r"\s+")
_META_CHARSET_RE = re.compile(
    r"""<meta\b[^>]*?(?:charset\s*=\s*["']?\s*([A-Za-z0-9._-]+)|content\s*=\s*["'][^"']*?charset\s*=\s*([A-Za-z0-9._-]+))""",
    re.IGNORECASE,
)
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_CSS_ESCAPE_RE = re.compile(r"\\([0-9a-fA-F]{1,6}\s?|.)")
_CSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)
_HIDDEN_STYLE_RE = re.compile(
    r"(?:^|;)\s*(?:"
    r"display\s*:\s*none"
    r"|visibility\s*:\s*(?:hidden|collapse)"
    r"|content-visibility\s*:\s*hidden"
    r"|opacity\s*:\s*(?:0+(?:\.0+)?|\.0+)(?:%)?"
    r"|filter\s*:\s*opacity\(\s*(?:0+(?:\.0+)?|\.0+)(?:%)?\s*\)"
    r"|font-size\s*:\s*(?:0+(?:\.0+)?|\.0+)(?:px|pt|em|rem|%|vh|vw)?"
    r"|line-height\s*:\s*0(?:px|pt|em|rem|%)?(?=\s*;.*?\boverflow\s*:\s*hidden\b)"
    r"|color\s*:\s*(?:transparent|rgba\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*(?:0+(?:\.0+)?|\.0+)\s*\)|hsla\(\s*\d+(?:deg)?\s*,\s*\d+%\s*,\s*\d+%\s*,\s*(?:0+(?:\.0+)?|\.0+)\s*\)|#(?:0000|00000000)\b)"
    r"|transform\s*:\s*scale(?:[XY3d])?\(\s*(?:0+(?:\.0+)?|\.0+)(?:\s*,\s*(?:0+(?:\.0+)?|\.0+))*\s*\)"
    r"|(?:left|right|top|bottom|text-indent|margin-left|margin-top)\s*:\s*-\d{3,}(?:\.\d+)?(?:px|em|rem|vw|vh|pt|%)"
    r"|clip-path\s*:\s*(?:inset\(\s*(?:50%|100%)[^)]*\)|circle\(\s*0[^)]*\)|polygon\(\s*0\s+0[^)]*\))"
    r"|clip\s*:\s*rect\(\s*0(?:px)?[\s,]+0(?:px)?[\s,]+0(?:px)?[\s,]+0(?:px)?\s*\)"
    r")\s*(?:!important)?\s*(?:;|$)",
    re.IGNORECASE,
)
_HIDDEN_CLASS_TOKENS: frozenset[str] = frozenset(
    {
        "sr-only",
        "visually-hidden",
        "visually-hidden-focusable",
        "screen-reader-text",
        "screen-reader-only",
        "d-none",
        "u-hidden",
        "is-hidden",
        "offscreen",
        "a11y-hidden",
        "assistive-text",
        "clip-hide",
    }
)
_MAX_EXTRACTED_URL_LEN: int = 1024

_URL_UNSAFE_CHAR_MAP: dict[str, str] = {
    " ": "%20",
    "(": "%28",
    ")": "%29",
    "[": "%5B",
    "]": "%5D",
    "<": "%3C",
    ">": "%3E",
    '"': "%22",
    "'": "%27",
    "`": "%60",
}


def normalize_text(raw: str) -> str:
    """
    Normaliza Unicode (NFC), elimina caracteres invisibles, controles bidireccionales o Tags
    y colapsa espacios preservando texto multilingüe, acentos y símbolos legítimos.
    """
    if not raw:
        return ""
    text = unicodedata.normalize("NFC", raw)
    text = text.replace("\u00a0", " ")
    text = _INVISIBLE_CHARS_RE.sub("", text)
    text = _CONTROL_CHARS_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def _is_obvious_noise_text(text: str) -> bool:
    """
    Identifica cadenas de ruido evidente (sin descartar artículos técnicos que mencionen el tema).
    """
    if not text:
        return True
    if len(text) <= 2 and not any(ch.isalnum() for ch in text):
        return True
    lowered = text.lower().strip(" .!?:|•·-—»«")
    if lowered in _EXACT_NOISE_PHRASES:
        return True
    return False


def _validate_charset_name(name: str | None) -> str | None:
    """Verifica si un nombre de codificación es reconocido por Python."""
    if not name:
        return None
    cleaned = name.strip().strip("\"'").lower()
    if not cleaned:
        return None
    try:
        return codecs.lookup(cleaned).name
    except LookupError:
        return None


def decode_html_bytes(
    body_bytes: bytes,
    declared_charset: str | None = None,
) -> tuple[str, str, list[WebIssue]]:
    """
    Decodifica bytes HTML de forma segura siguiendo prioridad estándar:
    1. BOM explícito (UTF-8 / UTF-16).
    2. Charset declarado en la cabecera HTTP `Content-Type`.
    3. `<meta charset>` o `<meta http-equiv>` en los primeros 4 KiB.
    4. UTF-8 estricto.
    5. Respaldo UTF-8 / cp1252 con reemplazo y advertencia trazable.
    """
    warnings: list[WebIssue] = []
    if not body_bytes:
        return "", "utf-8", warnings

    # 1. BOM explícito.
    if body_bytes.startswith(codecs.BOM_UTF8):
        return body_bytes.decode("utf-8-sig", errors="replace"), "utf-8", warnings
    if body_bytes.startswith(codecs.BOM_UTF16_LE) or body_bytes.startswith(codecs.BOM_UTF16_BE):
        return body_bytes.decode("utf-16", errors="replace"), "utf-16", warnings

    # 2. Charset en cabecera HTTP.
    candidates: list[str] = []
    http_cs = _validate_charset_name(declared_charset)
    if http_cs:
        candidates.append(http_cs)

    # 3. Detección temprana en <meta> dentro de los primeros 4 KiB.
    head_preview = body_bytes[:4096].decode("latin-1", errors="ignore")
    for match in _META_CHARSET_RE.finditer(head_preview):
        meta_cs = _validate_charset_name(match.group(1) or match.group(2))
        if meta_cs and meta_cs not in candidates:
            candidates.append(meta_cs)

    # 4. UTF-8 estándar como fallback limpio.
    if "utf-8" not in candidates:
        candidates.append("utf-8")

    for codec_name in candidates:
        try:
            return body_bytes.decode(codec_name, errors="strict"), codec_name, warnings
        except UnicodeDecodeError:
            continue

    # 5. Último recurso seguro: nunca fallar todo el documento por un byte corrupto aislado.
    fallback_codec = candidates[0] if candidates else "utf-8"
    warnings.append(
        WebIssue(
            code="DECODE_REPLACEMENT",
            message=(
                f"El documento contenía secuencias de bytes inválidas; "
                f"se aplicó reemplazo seguro usando codificación '{fallback_codec}'."
            ),
            source_url="",
            severity="warning",
        )
    )
    return body_bytes.decode(fallback_codec, errors="replace"), fallback_codec, warnings


def normalize_extracted_url(raw_href: str, base_url: str = "") -> str | None:
    """
    Resuelve y normaliza un enlace relativo/absoluto conservando únicamente esquemas http/https.
    Descarta enlaces internos `#fragment`, `javascript:`, `mailto:`, `tel:`, caracteres de control
    y codifica caracteres que romperían la sintaxis de enlaces Markdown (espacios, paréntesis, etc.).
    """
    if not raw_href or not isinstance(raw_href, str):
        return None
    cleaned = raw_href.strip()
    if not cleaned or cleaned.startswith("#") or len(cleaned) > _MAX_EXTRACTED_URL_LEN:
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in cleaned):
        return None
    lower = cleaned.lower()
    if lower.startswith(("javascript:", "mailto:", "tel:", "data:", "vbscript:", "file:", "ftp:")):
        return None

    try:
        resolved = urljoin(base_url, cleaned)
        parts = urlsplit(resolved)
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.netloc:
        return None

    # Excluir credenciales incrustadas también en enlaces extraídos.
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return None

    try:
        port = parts.port
        hostname = parts.hostname
    except ValueError:
        return None
    if not hostname or any(ch in hostname for ch in " ()[]<>\"'`\\"):
        return None

    raw_host = hostname.strip().rstrip(".")
    if not raw_host:
        return None
    if ":" in raw_host:
        norm_host = f"[{raw_host.lower()}]"
    else:
        try:
            ascii_host = raw_host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return None
        if not ascii_host or any(ch in ascii_host for ch in " /?#@\\"):
            return None
        norm_host = ascii_host

    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{norm_host}:{port}"
    else:
        netloc = norm_host

    raw_path = parts.path or "/"
    safe_path = "".join(_URL_UNSAFE_CHAR_MAP.get(ch, ch) for ch in raw_path)
    safe_query = "".join(_URL_UNSAFE_CHAR_MAP.get(ch, ch) for ch in parts.query)
    # Conservamos ruta y query codificados de forma segura, removiendo fragmentos visuales.
    normalized = urlunsplit((scheme, netloc, safe_path, safe_query, ""))
    if len(normalized) > _MAX_EXTRACTED_URL_LEN:
        return None
    return normalized


def _has_same_origin(url_a: str, url_b: str) -> bool:
    """Comprueba si dos URLs HTTP/HTTPS comparten exactamente el mismo origen (scheme, host, port)."""
    try:
        pa = urlsplit(url_a)
        pb = urlsplit(url_b)
        sa = pa.scheme.lower()
        sb = pb.scheme.lower()
        if sa not in ("http", "https") or sa != sb:
            return False
        ha_raw = (pa.hostname or "").strip().rstrip(".")
        hb_raw = (pb.hostname or "").strip().rstrip(".")
        if not ha_raw or not hb_raw:
            return False
        ha = ha_raw.lower() if ":" in ha_raw else ha_raw.encode("idna").decode("ascii").lower()
        hb = hb_raw.lower() if ":" in hb_raw else hb_raw.encode("idna").decode("ascii").lower()
        if ha != hb:
            return False
        port_a = pa.port or (443 if sa == "https" else 80)
        port_b = pb.port or (443 if sb == "https" else 80)
        return port_a == port_b
    except (ValueError, UnicodeError):
        return False


@dataclass
class _RawBlock:
    """Bloque estructural intermedio recolectado durante el recorrido del HTML."""

    kind: str  # 'heading' | 'paragraph' | 'list'
    in_main: bool
    level: int = 0
    text: str = ""
    ordered: bool = False
    items: tuple[str, ...] = ()


@dataclass
class _RawLink:
    """Enlace intermedio recolectado antes de normalizar y acotar por límite."""

    href: str
    text: str
    in_main: bool


@dataclass
class _ListFrame:
    """Estado de una lista HTML activa (`ul` u `ol`)."""

    ordered: bool
    in_main: bool
    items: list[str] = field(default_factory=list)


class _StructuredHTMLParser(HTMLParser):
    """
    Parser HTML de una sola pasada sobre `html.parser.HTMLParser`.

    Mantiene pila de etiquetas ignoradas, contenedores principales (`main`/`article`) y
    buffers de texto para extraer encabezados, párrafos, listas, enlaces y metadatos reales.
    """

    def __init__(
        self,
        *,
        max_jsonld_bytes: int = 131072,
        max_style_bytes: int = 65536,
        max_blocks: int = 4096,
        max_raw_links: int = 2048,
        max_meta_field_chars: int = 8192,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self._max_jsonld_bytes = max_jsonld_bytes
        self._max_style_bytes = max_style_bytes
        self._max_blocks = max_blocks
        self._max_raw_links = max_raw_links
        self._max_meta_field_chars = max_meta_field_chars

        # Pila de control: cada elemento guarda (tag, is_ignored_subtree, is_main_landmark, is_head).
        self._stack: list[tuple[str, bool, bool, bool]] = []
        self._ignored_depth: int = 0
        self._main_depth: int = 0
        self._head_depth: int = 0
        self._body_started: bool = False

        # Metadatos recolectados.
        self.html_lang: str | None = None
        self.base_href: str | None = None
        self.title_text: str | None = None
        self.meta_values: dict[str, str] = {}
        self.meta_truncated: bool = False
        self.canonical_href: str | None = None
        self.author_link_text: str | None = None
        self.time_candidates: list[str] = []
        self.jsonld_blocks: list[dict[str, Any]] = []

        # Capturas puntuales de etiquetas especiales.
        self._in_title: bool = False
        self._title_parts: list[str] = []
        self._in_jsonld: bool = False
        self._jsonld_parts: list[str] = []
        self._jsonld_size: int = 0
        self._in_style: bool = False
        self._style_parts: list[str] = []
        self._style_size: int = 0
        self._css_hidden_classes: set[str] = set()
        self._css_hidden_ids: set[str] = set()

        # Buffers de contenido visible.
        self._heading_tag: str | None = None
        self._heading_in_main: bool = False
        self._heading_parts: list[str] = []

        self._paragraph_tag: str | None = None
        self._paragraph_in_main: bool = False
        self._paragraph_parts: list[str] = []

        self._list_stack: list[_ListFrame] = []
        self._in_li: bool = False
        self._li_parts: list[str] = []

        self._link_stack: list[tuple[str, bool, list[str]]] = []
        self._author_link_parts: list[str] | None = None

        # Buffer para texto directo dentro de contenedores sin <p> explícito (ej. <article>Texto</article>).
        self._loose_parts: list[str] = []
        self._loose_in_main: bool = False
        self._pending_inline_sep: bool = False
        self._in_loose_time: bool = False

        self.blocks: list[_RawBlock] = []
        self.links: list[_RawLink] = []
        self.blocks_capped: bool = False
        self.links_capped: bool = False

    @staticmethod
    def _attrs_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        result: dict[str, str] = {}
        for key, val in attrs:
            if key:
                result[key.lower()] = val or ""
        return result

    @staticmethod
    def _clean_css_text(style_raw: str) -> str:
        """Elimina comentarios CSS y secuencias de escape simples para evitar evasión léxica."""
        cleaned = _CSS_COMMENT_RE.sub("", style_raw)
        if "\\" in cleaned:
            cleaned = _CSS_ESCAPE_RE.sub(lambda m: m.group(1).strip(), cleaned)
        return cleaned

    @classmethod
    def _is_hidden_style(cls, style_raw: str) -> bool:
        if not style_raw:
            return False
        cleaned = cls._clean_css_text(style_raw)
        if not cleaned:
            return False
        if _HIDDEN_STYLE_RE.search(cleaned):
            return True
        if re.search(r"\boverflow(?:-[xy])?\s*:\s*(?:hidden|clip)\b", cleaned, re.IGNORECASE) and re.search(
            r"(?:^|[;\s])(?:max-)?(?:width|height)\s*:\s*(?:0+(?:\.0+)?|\.0+)(?:px|em|rem|%|pt|vh|vw)?(?=\s*(?:;|!important|$))",
            cleaned,
            re.IGNORECASE,
        ):
            return True
        return False

    def _parse_style_rules(self, css_raw: str) -> None:
        """Detecta selectores `.clase` o `#id` definidos como ocultos en bloques `<style>`."""
        cleaned = self._clean_css_text(css_raw)
        for match in _CSS_RULE_RE.finditer(cleaned):
            selectors_raw, body_raw = match.group(1), match.group(2)
            if not self._is_hidden_style(body_raw):
                continue
            for sel in selectors_raw.split(","):
                sel_s = sel.strip()
                if not sel_s or ":" in sel_s or ">" in sel_s or "+" in sel_s or "~" in sel_s:
                    continue
                class_m = re.match(r"^(?:[a-zA-Z0-9_-]+)?\.\s*([a-zA-Z0-9_-]+)$", sel_s)
                if class_m:
                    self._css_hidden_classes.add(class_m.group(1).lower())
                    continue
                id_m = re.match(r"^(?:[a-zA-Z0-9_-]+)?#\s*([a-zA-Z0-9_-]+)$", sel_s)
                if id_m:
                    self._css_hidden_ids.add(id_m.group(1).lower())

    def _has_hidden_attrs(self, attr_map: dict[str, str]) -> bool:
        if "hidden" in attr_map or "inert" in attr_map:
            return True
        if attr_map.get("aria-hidden", "").strip().lower() == "true":
            return True
        if self._is_hidden_style(attr_map.get("style", "")):
            return True
        class_raw = attr_map.get("class", "").strip().lower()
        if class_raw:
            tokens = set(class_raw.split())
            if tokens & _HIDDEN_CLASS_TOKENS or (self._css_hidden_classes and (tokens & self._css_hidden_classes)):
                return True
        elem_id = attr_map.get("id", "").strip().lower()
        if elem_id and (elem_id in _HIDDEN_CLASS_TOKENS or elem_id in self._css_hidden_ids):
            return True
        return False

    def _is_noise_element(self, tag: str, attr_map: dict[str, str]) -> bool:
        if tag in _IGNORED_TAGS or tag in _CHROME_TAGS:
            return True
        if self._has_hidden_attrs(attr_map):
            return True
        role = attr_map.get("role", "").strip().lower()
        if role in _NOISE_ROLES:
            return True
        class_id = f"{attr_map.get('class', '')} {attr_map.get('id', '')}".strip()
        if class_id and _NOISE_ATTR_RE.search(class_id):
            return True
        return False

    def _close_open_title(self) -> None:
        if not self._in_title:
            return
        self._in_title = False
        cleaned = normalize_text("".join(self._title_parts))
        if cleaned and self.title_text is None:
            self.title_text = cleaned
        self._title_parts.clear()

    def _flush_loose_text(self) -> None:
        if not self._loose_parts:
            return
        text = normalize_text("".join(self._loose_parts))
        in_main = self._loose_in_main
        self._loose_parts.clear()
        if not text or _is_obvious_noise_text(text):
            return
        if len(self.blocks) < self._max_blocks:
            self.blocks.append(_RawBlock(kind="paragraph", in_main=in_main, text=text))
        else:
            self.blocks_capped = True

    def _append_visible_text(self, text: str) -> None:
        if self._ignored_depth > 0 or self._head_depth > 0 or self._in_loose_time:
            return

        active_buf: list[str] | None = None
        if self._heading_tag is not None:
            active_buf = self._heading_parts
        elif self._in_li:
            active_buf = self._li_parts
        elif self._paragraph_tag is not None:
            active_buf = self._paragraph_parts
        elif self._stack and self._stack[-1][0] not in ("head", "title"):
            if not self._loose_parts:
                self._loose_in_main = self._main_depth > 0
            active_buf = self._loose_parts

        sep = ""
        if self._pending_inline_sep:
            self._pending_inline_sep = False
            first_ch = text[0] if text else ""
            if (
                active_buf
                and first_ch
                and not first_ch.isspace()
                and first_ch not in ".,;:!?)]}»\"'"
            ):
                last_fragment = active_buf[-1]
                if last_fragment and not last_fragment[-1].isspace() and last_fragment[-1] not in "([{\"«'":
                    sep = " "

        payload = f"{sep}{text}" if sep else text
        if self._link_stack:
            self._link_stack[-1][2].append(payload)
        if self._author_link_parts is not None:
            self._author_link_parts.append(payload)
        if active_buf is not None:
            active_buf.append(payload)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_l = tag.lower()
        attr_map = self._attrs_dict(attrs)
        is_void = tag_l in _VOID_TAGS

        # Si ya estamos dentro de un subárbol ignorado (<template>, <noscript>, hidden, nav, footer, etc.),
        # apilar solo para balancear cierre y no procesar ningún metadato ni contenido (F-02 / F-05).
        if self._ignored_depth > 0:
            if not is_void:
                self._stack.append((tag_l, False, False, False))
            return

        has_hidden = self._has_hidden_attrs(attr_map)

        # Transición implícita o explícita a cuerpo del documento (cierra <title> o <head> mal cerrados).
        if tag_l in _BODY_INITIATOR_TAGS:
            self._close_open_title()
            if self._head_depth > 0:
                self._head_depth = 0
                self._stack = [(t, ign, mn, False) for (t, ign, mn, _hd) in self._stack if t != "head"]
            self._body_started = True

        in_head_metadata_ctx = (self._head_depth > 0 or not self._body_started) and not has_hidden

        if tag_l == "html" and not self.html_lang and not has_hidden:
            lang = normalize_text(attr_map.get("lang") or attr_map.get("xml:lang") or "")
            if lang:
                self.html_lang = lang
        elif tag_l == "base" and not self.base_href:
            # <base href> se acepta exclusivamente dentro de <head> explícito y antes de <body> (F-02).
            if self._head_depth > 0 and not self._body_started and not has_hidden:
                href = attr_map.get("href", "").strip()
                if href:
                    self.base_href = href
        elif tag_l == "meta" and in_head_metadata_ctx:
            self._handle_meta(attr_map)
        elif tag_l == "link" and in_head_metadata_ctx:
            rel_tokens = set(attr_map.get("rel", "").lower().split())
            if "canonical" in rel_tokens and not self.canonical_href:
                href = attr_map.get("href", "").strip()
                if href:
                    self.canonical_href = href

        # Captura segura de JSON-LD únicamente en contexto de metadatos y fuera de subárboles ignorados/ocultos.
        if (
            tag_l == "script"
            and in_head_metadata_ctx
            and attr_map.get("type", "").strip().lower() == "application/ld+json"
        ):
            self._in_jsonld = True
            self._jsonld_parts.clear()
            self._jsonld_size = 0

        if tag_l == "style" and not has_hidden:
            self._in_style = True
            self._style_parts.clear()
            self._style_size = 0

        if is_void:
            if tag_l == "br":
                self._append_visible_text(" ")
            return

        if tag_l == "title" and in_head_metadata_ctx and not self._in_title and self.title_text is None:
            self._in_title = True
            self._title_parts.clear()

        # Determinar ignorado / landmark principal / head antes de apilar.
        becomes_head = tag_l == "head" and not self._body_started and not has_hidden
        becomes_ignored = self._is_noise_element(tag_l, attr_map) if tag_l != "head" else has_hidden
        becomes_main = tag_l in ("main", "article") or attr_map.get("role", "").strip().lower() == "main"

        if becomes_head:
            self._head_depth += 1
        if becomes_ignored:
            self._ignored_depth += 1
        if becomes_main and self._ignored_depth == 0 and self._head_depth == 0:
            self._main_depth += 1

        self._stack.append(
            (
                tag_l,
                becomes_ignored,
                becomes_main and self._ignored_depth == 0 and self._head_depth == 0,
                becomes_head,
            )
        )

        if self._ignored_depth > 0 or self._head_depth > 0:
            return

        # Al abrir bloques estructurales mayores, vaciar texto suelto previo para mantener el orden.
        if tag_l in _HEADING_TAGS or tag_l in _PARAGRAPH_TAGS or tag_l in ("ul", "ol", "section", "article", "main"):
            self._flush_loose_text()

        if tag_l in _HEADING_TAGS and self._heading_tag is None:
            self._heading_tag = tag_l
            self._heading_in_main = self._main_depth > 0
            self._heading_parts.clear()
        elif tag_l in _PARAGRAPH_TAGS and self._paragraph_tag is None and self._heading_tag is None and not self._in_li:
            self._paragraph_tag = tag_l
            self._paragraph_in_main = self._main_depth > 0
            self._paragraph_parts.clear()
        elif tag_l in ("ul", "ol"):
            self._list_stack.append(_ListFrame(ordered=(tag_l == "ol"), in_main=(self._main_depth > 0)))
        elif tag_l == "li" and self._list_stack and not self._in_li:
            self._in_li = True
            self._li_parts.clear()
        elif tag_l == "a":
            href = attr_map.get("href", "").strip()
            rel_tokens = set(attr_map.get("rel", "").lower().split())
            if "author" in rel_tokens and self.author_link_text is None and self._author_link_parts is None:
                self._author_link_parts = []
            if href:
                self._link_stack.append((href, self._main_depth > 0, []))
        elif tag_l == "time":
            dt = normalize_text(attr_map.get("datetime", ""))
            if dt and self._ignored_depth == 0:
                self.time_candidates.append(dt)
            if self._heading_tag is None and self._paragraph_tag is None and not self._in_li:
                self._in_loose_time = True

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        if tag_l in _VOID_TAGS:
            return

        if tag_l in ("title", "head") and self._in_title:
            self._close_open_title()

        if tag_l == "script" and self._in_jsonld:
            self._in_jsonld = False
            raw_json = "".join(self._jsonld_parts).strip()
            self._jsonld_parts.clear()
            if raw_json:
                self._parse_jsonld(raw_json)

        if tag_l == "style" and self._in_style:
            self._in_style = False
            raw_css = "".join(self._style_parts).strip()
            self._style_parts.clear()
            if raw_css:
                self._parse_style_rules(raw_css)

        was_ignored_before_pop = self._ignored_depth > 0 or self._head_depth > 0

        if not was_ignored_before_pop:
            if tag_l == "a":
                if self._author_link_parts is not None:
                    author_txt = normalize_text("".join(self._author_link_parts))
                    if author_txt and self.author_link_text is None:
                        self.author_link_text = author_txt
                    self._author_link_parts = None
                if self._link_stack:
                    href, in_main, link_parts = self._link_stack.pop()
                    link_text = normalize_text("".join(link_parts))
                    if len(self.links) < self._max_raw_links:
                        self.links.append(_RawLink(href=href, text=link_text, in_main=in_main))
                    else:
                        self.links_capped = True
                self._pending_inline_sep = True
            elif tag_l in _INLINE_TAGS:
                if tag_l == "time" and self._in_loose_time:
                    self._in_loose_time = False
                else:
                    # Separador espacial condicional solo si sigue texto distinto de puntuación.
                    self._pending_inline_sep = True
            elif tag_l == self._heading_tag:
                heading_text = normalize_text("".join(self._heading_parts))
                level = _HEADING_TAGS[tag_l]
                in_main = self._heading_in_main
                self._heading_tag = None
                self._pending_inline_sep = False
                self._heading_parts.clear()
                if heading_text and not _is_obvious_noise_text(heading_text):
                    if len(self.blocks) < self._max_blocks:
                        self.blocks.append(_RawBlock(kind="heading", in_main=in_main, level=level, text=heading_text))
                    else:
                        self.blocks_capped = True
            elif tag_l == self._paragraph_tag:
                para_text = normalize_text("".join(self._paragraph_parts))
                in_main = self._paragraph_in_main
                self._paragraph_tag = None
                self._pending_inline_sep = False
                self._paragraph_parts.clear()
                if para_text and not _is_obvious_noise_text(para_text):
                    if len(self.blocks) < self._max_blocks:
                        self.blocks.append(_RawBlock(kind="paragraph", in_main=in_main, text=para_text))
                    else:
                        self.blocks_capped = True
            elif tag_l == "li" and self._in_li:
                li_text = normalize_text("".join(self._li_parts))
                self._in_li = False
                self._pending_inline_sep = False
                self._li_parts.clear()
                if li_text and not _is_obvious_noise_text(li_text) and self._list_stack:
                    self._list_stack[-1].items.append(li_text)
            elif tag_l in ("ul", "ol") and self._list_stack:
                frame = self._list_stack.pop()
                if frame.items:
                    if len(self.blocks) < self._max_blocks:
                        self.blocks.append(
                            _RawBlock(
                                kind="list",
                                in_main=frame.in_main,
                                ordered=frame.ordered,
                                items=tuple(frame.items),
                            )
                        )
                    else:
                        self.blocks_capped = True
            elif tag_l in _CONTAINER_TAGS:
                self._flush_loose_text()

        # Desapilar estado de ignorado, main landmark y head incluso si HTML viene desbalanceado.
        for idx in range(len(self._stack) - 1, -1, -1):
            stacked_tag, started_ignored, started_main, started_head = self._stack[idx]
            if stacked_tag == tag_l:
                removed = self._stack[idx:]
                del self._stack[idx:]
                for _, ign, mn, hd in removed:
                    if ign:
                        self._ignored_depth = max(0, self._ignored_depth - 1)
                    if mn:
                        self._main_depth = max(0, self._main_depth - 1)
                    if hd:
                        self._head_depth = max(0, self._head_depth - 1)
                break

    def handle_data(self, data: str) -> None:
        if not data:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        if self._in_style:
            if self._style_size < self._max_style_bytes:
                chunk = data[: self._max_style_bytes - self._style_size]
                self._style_parts.append(chunk)
                self._style_size += len(chunk)
            return
        if self._in_jsonld:
            if self._jsonld_size < self._max_jsonld_bytes:
                chunk = data[: self._max_jsonld_bytes - self._jsonld_size]
                self._jsonld_parts.append(chunk)
                self._jsonld_size += len(chunk)
            return
        self._append_visible_text(data)

    def handle_comment(self, data: str) -> None:
        # Los comentarios HTML se descartan intencionadamente.
        return

    def close(self) -> None:
        super().close()
        self._close_open_title()
        self._flush_loose_text()

    def _handle_meta(self, attr_map: dict[str, str]) -> None:
        raw_content = attr_map.get("content", "")
        if len(raw_content) > self._max_meta_field_chars:
            raw_content = raw_content[: self._max_meta_field_chars]
            self.meta_truncated = True
        content = normalize_text(raw_content)
        if not content:
            return
        for key_attr in ("property", "name", "itemprop", "http-equiv"):
            key_val = attr_map.get(key_attr, "").strip().lower()
            if key_val and key_val not in self.meta_values:
                self.meta_values[key_val] = content

    def _parse_jsonld(self, raw_json: str) -> None:
        try:
            payload = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            return
        if isinstance(payload, dict):
            if isinstance(payload.get("@graph"), list):
                for item in payload["@graph"]:
                    if isinstance(item, dict):
                        self.jsonld_blocks.append(item)
            self.jsonld_blocks.append(payload)
        elif isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    self.jsonld_blocks.append(item)


def _extract_jsonld_field(blocks: list[dict[str, Any]], key: str) -> str | None:
    """Extrae un campo de texto simple o compuesto (ej. author.name) desde bloques JSON-LD."""
    for item in blocks:
        val = item.get(key)
        if isinstance(val, str):
            cleaned = normalize_text(val)
            if cleaned:
                return cleaned
        elif isinstance(val, dict):
            name = val.get("name") or val.get("headline")
            if isinstance(name, str) and normalize_text(name):
                return normalize_text(name)
        elif isinstance(val, list) and val:
            first = val[0]
            if isinstance(first, str) and normalize_text(first):
                return normalize_text(first)
            if isinstance(first, dict):
                name = first.get("name")
                if isinstance(name, str) and normalize_text(name):
                    return normalize_text(name)
    return None


class WebContentExtractor:
    """
    Convierte una respuesta HTTP descargada (`FetchResult`) en un `WebSourceDocument`
    estructurado, normalizado y con trazabilidad completa por fragmento.
    """

    def __init__(self, config: WebExtractionConfig | None = None) -> None:
        self.config: WebExtractionConfig = config or WebExtractionConfig()

    def extract_from_fetch(self, fetch_result: FetchResult) -> WebSourceDocument:
        """Decodifica, parsea y normaliza una descarga válida en `WebSourceDocument`."""
        extracted_at = utc_now_iso()
        source_url = fetch_result.final_url or fetch_result.original_url

        html_text, _codec, decode_warnings = decode_html_bytes(
            fetch_result.body_bytes,
            declared_charset=fetch_result.declared_charset,
        )
        warnings: list[WebIssue] = [
            WebIssue(
                code=w.code,
                message=w.message,
                source_url=source_url,
                severity="warning",
            )
            for w in decode_warnings
        ]

        parser = _StructuredHTMLParser()
        try:
            parser.feed(html_text)
            parser.close()
        except Exception as exc:
            return WebSourceDocument(
                original_url=fetch_result.original_url,
                final_url=source_url,
                status=ExtractionStatus.FAILED,
                http_status=fetch_result.http_status,
                metadata=WebMetadata(
                    extracted_at=extracted_at,
                    language=fetch_result.content_language,
                    content_type=fetch_result.content_type,
                ),
                redirect_chain=fetch_result.redirect_chain,
                bytes_received=fetch_result.bytes_received,
                bytes_decompressed=fetch_result.bytes_decompressed,
                errors=(
                    WebIssue(
                        code="HTML_PARSE_ERROR",
                        message=f"No fue posible analizar el HTML de la página: {exc}",
                        source_url=source_url,
                        severity="error",
                    ),
                ),
                warnings=tuple(warnings),
            )

        # Seleccionar bloques de <main>/<article> si aportan contenido informativo real.
        main_blocks = [b for b in parser.blocks if b.in_main]
        selected_blocks = (
            main_blocks
            if any(b.kind in ("paragraph", "list") for b in main_blocks) or (main_blocks and not any(b.kind in ("paragraph", "list") for b in parser.blocks))
            else parser.blocks
        )

        meta = parser.meta_values
        explicit_title = (
            parser.title_text
            or meta.get("og:title")
            or meta.get("twitter:title")
            or _extract_jsonld_field(parser.jsonld_blocks, "headline")
        )
        first_selected_heading = next((b.text for b in selected_blocks if b.kind == "heading"), None)

        max_budget = max(1, int(self.config.max_normalized_chars))
        text_parts: list[str] = []
        used_chars: int = 0
        truncated: bool = bool(parser.meta_truncated or parser.blocks_capped or parser.links_capped)

        # Acotación de metadatos dentro del presupuesto global (F-04).
        max_title_chars = min(300, max(40, max_budget // 3))
        max_desc_chars = min(600, max(40, max_budget // 5))
        max_meta_short = min(160, max(32, max_budget // 5))

        if explicit_title and len(explicit_title) > max_title_chars:
            explicit_title = explicit_title[:max_title_chars].rstrip()
            truncated = True

        raw_description = (
            meta.get("description")
            or meta.get("og:description")
            or meta.get("twitter:description")
            or _extract_jsonld_field(parser.jsonld_blocks, "description")
        )
        description: str | None = raw_description
        if description:
            if len(description) > max_desc_chars:
                description = description[:max_desc_chars].rstrip()
                truncated = True
            # Contabilizar la descripción dentro del presupuesto global compartido.
            used_chars += len(description)

        raw_links = parser.links
        if any(b.in_main for b in selected_blocks) and any(lk.in_main for lk in parser.links):
            raw_links = [lk for lk in parser.links if lk.in_main]

        # Si el documento incluye enlaces candidatos y el presupuesto es reducido, reservamos una fracción
        # acotada para que cuerpo, metadatos y enlaces compartan `max_budget` sin excederlo.
        links_reserve = min(len(raw_links) * 60, max_budget // 5) if raw_links and max_budget >= 250 else 0
        body_budget = max(1, max_budget - links_reserve)

        if explicit_title and (not first_selected_heading or first_selected_heading.casefold() != explicit_title.casefold()):
            title_part = explicit_title
            avail_title = max(0, body_budget - used_chars)
            if len(title_part) > avail_title:
                title_part = title_part[:avail_title].rstrip()
                truncated = True
            if title_part:
                text_parts.append(title_part)
                used_chars += len(title_part)

        # Deduplicación, estructuración ordenada y presupuesto compartido de caracteres (F-04).
        headings: list[WebHeading] = []
        paragraphs: list[WebParagraph] = []
        lists: list[WebList] = []
        sections: list[WebSection] = []

        seen_paragraphs: set[str] = set()
        seen_lists: set[tuple[bool, tuple[str, ...]]] = set()

        current_heading: WebHeading | None = None
        current_level: int = 0
        current_sec_paragraphs: list[WebParagraph] = []
        current_sec_lists: list[WebList] = []

        def flush_section() -> None:
            if current_heading is None and not current_sec_paragraphs and not current_sec_lists:
                return
            sections.append(
                WebSection(
                    heading=current_heading,
                    level=current_level,
                    paragraphs=tuple(current_sec_paragraphs),
                    lists=tuple(current_sec_lists),
                    source_url=source_url,
                    order_index=len(sections),
                )
            )
            current_sec_paragraphs.clear()
            current_sec_lists.clear()

        body_truncated = False
        for blk in selected_blocks:
            if body_truncated:
                truncated = True
                break
            if blk.kind == "heading":
                # Evitar encabezados duplicados inmediatamente consecutivos.
                if current_heading and current_heading.text.casefold() == blk.text.casefold():
                    continue
                sep_cost = 2 if text_parts else 0
                avail = body_budget - used_chars - sep_cost
                if avail <= 0:
                    truncated = True
                    body_truncated = True
                    break
                h_text = blk.text
                if len(h_text) > avail:
                    h_text = h_text[:avail].rstrip()
                    truncated = True
                    body_truncated = True
                    if not h_text:
                        break
                flush_section()
                heading_obj = WebHeading(level=blk.level, text=h_text, source_url=source_url)
                headings.append(heading_obj)
                current_heading = heading_obj
                current_level = blk.level
                text_parts.append(h_text)
                used_chars += sep_cost + len(h_text)
            elif blk.kind == "paragraph":
                dedup_key = blk.text.casefold()
                if dedup_key in seen_paragraphs:
                    continue
                seen_paragraphs.add(dedup_key)
                sep_cost = 2 if text_parts else 0
                avail = body_budget - used_chars - sep_cost
                if avail <= 0:
                    truncated = True
                    body_truncated = True
                    break
                p_text = blk.text
                if len(p_text) > avail:
                    p_text = p_text[:avail].rstrip()
                    truncated = True
                    body_truncated = True
                    if not p_text:
                        break
                para_obj = WebParagraph(
                    text=p_text,
                    source_url=source_url,
                    section_heading=current_heading.text if current_heading else None,
                    index=len(paragraphs),
                )
                paragraphs.append(para_obj)
                current_sec_paragraphs.append(para_obj)
                text_parts.append(p_text)
                used_chars += sep_cost + len(p_text)
            elif blk.kind == "list":
                dedup_items = tuple(item for item in blk.items if item)
                if not dedup_items:
                    continue
                list_key = (blk.ordered, tuple(it.casefold() for it in dedup_items))
                if list_key in seen_lists:
                    continue
                seen_lists.add(list_key)
                bounded_items: list[str] = []
                for idx, item in enumerate(dedup_items, start=1):
                    prefix = f"{idx}." if blk.ordered else "-"
                    sep_cost = 2 if text_parts else 0
                    avail = body_budget - used_chars - sep_cost
                    min_needed = len(prefix) + 1
                    if avail <= min_needed:
                        truncated = True
                        body_truncated = True
                        break
                    max_item_chars = avail - min_needed
                    item_text = item
                    if len(item_text) > max_item_chars:
                        item_text = item_text[:max_item_chars].rstrip()
                        truncated = True
                        body_truncated = True
                    if item_text:
                        bounded_items.append(item_text)
                        part_str = f"{prefix} {item_text}"
                        text_parts.append(part_str)
                        used_chars += sep_cost + len(part_str)
                    if body_truncated:
                        break
                if bounded_items:
                    list_obj = WebList(
                        ordered=blk.ordered,
                        items=tuple(bounded_items),
                        source_url=source_url,
                        section_heading=current_heading.text if current_heading else None,
                        index=len(lists),
                    )
                    lists.append(list_obj)
                    current_sec_lists.append(list_obj)

        flush_section()

        # Enlaces relevantes normalizados, deduplicados y acotados por max_links y presupuesto global (F-04).
        # <base href> solo se aplica si fue declarado en <head> y pertenece al mismo origen (F-02).
        base_for_links = source_url
        if parser.base_href:
            candidate_base = normalize_extracted_url(parser.base_href, source_url)
            if candidate_base and _has_same_origin(candidate_base, source_url):
                base_for_links = candidate_base
            else:
                warnings.append(
                    WebIssue(
                        code="CROSS_ORIGIN_BASE_IGNORED",
                        message="Se ignoró una etiqueta <base href> inválida o de distinto origen por seguridad.",
                        source_url=source_url,
                        severity="warning",
                    )
                )
        page_host = (urlsplit(source_url).hostname or "").lower()

        max_link_text_chars = min(200, max(24, max_budget // 6))
        remaining_link_budget = min(4000, max(0, max_budget - used_chars))
        links_used_chars = 0

        links_map: dict[str, WebLink] = {}
        for raw_lk in raw_links:
            if raw_lk.href and len(raw_lk.href.strip()) > _MAX_EXTRACTED_URL_LEN:
                truncated = True
                continue
            norm_url = normalize_extracted_url(raw_lk.href, base_for_links)
            if not norm_url:
                continue
            link_host = (urlsplit(norm_url).hostname or "").lower()
            is_external = bool(link_host and page_host and link_host != page_host)
            cand_text = raw_lk.text or norm_url
            if len(cand_text) > max_link_text_chars:
                cand_text = cand_text[:max_link_text_chars].rstrip()
                truncated = True
            existing = links_map.get(norm_url)
            if existing is None:
                if len(links_map) >= self.config.max_links:
                    truncated = True
                    continue
                link_cost = len(norm_url) + (len(cand_text) if cand_text != norm_url else 0)
                if links_used_chars + link_cost > remaining_link_budget:
                    truncated = True
                    continue
                links_map[norm_url] = WebLink(
                    url=norm_url,
                    text=cand_text or norm_url,
                    source_url=source_url,
                    is_external=is_external,
                )
                links_used_chars += link_cost
                used_chars += link_cost
            elif existing.text == existing.url and raw_lk.text:
                extra_cost = len(cand_text)
                if links_used_chars + extra_cost <= remaining_link_budget:
                    links_map[norm_url] = WebLink(
                        url=norm_url,
                        text=cand_text,
                        source_url=source_url,
                        is_external=is_external,
                    )
                    links_used_chars += extra_cost
                    used_chars += extra_cost
                else:
                    truncated = True

        # Construcción honesta y acotada de metadatos (sin inventar campos ausentes).
        title = explicit_title or (headings[0].text if headings else None)
        if title and len(title) > max_title_chars:
            title = title[:max_title_chars].rstrip()
            truncated = True
        author = (
            meta.get("author")
            or meta.get("article:author")
            or _extract_jsonld_field(parser.jsonld_blocks, "author")
            or parser.author_link_text
        )
        if author and len(author) > max_meta_short:
            author = author[:max_meta_short].rstrip()
            truncated = True
        published_at = (
            meta.get("article:published_time")
            or meta.get("datepublished")
            or meta.get("pubdate")
            or meta.get("dc.date.issued")
            or meta.get("date")
            or _extract_jsonld_field(parser.jsonld_blocks, "datePublished")
            or (parser.time_candidates[0] if parser.time_candidates else None)
        )
        if published_at and len(published_at) > 64:
            published_at = published_at[:64].rstrip()
            truncated = True
        language = (
            parser.html_lang
            or fetch_result.content_language
            or meta.get("content-language")
            or meta.get("og:locale")
            or _extract_jsonld_field(parser.jsonld_blocks, "inLanguage")
        )
        if language and len(language) > 32:
            language = language[:32].rstrip()
            truncated = True
        canonical_url = (
            normalize_extracted_url(parser.canonical_href, source_url)
            if parser.canonical_href
            else None
        )
        site_name = meta.get("og:site_name") or _extract_jsonld_field(parser.jsonld_blocks, "publisher")
        if site_name and len(site_name) > max_meta_short:
            site_name = site_name[:max_meta_short].rstrip()
            truncated = True

        metadata = WebMetadata(
            title=title,
            description=description,
            author=author,
            published_at=published_at,
            extracted_at=extracted_at,
            language=language,
            canonical_url=canonical_url,
            site_name=site_name,
            content_type=fetch_result.content_type,
        )

        full_normalized = "\n\n".join(part for part in text_parts if part)
        if len(full_normalized) > max_budget:
            full_normalized = full_normalized[:max_budget].rstrip()
            truncated = True
        if truncated:
            warnings.append(
                WebIssue(
                    code="NORMALIZED_TEXT_TRUNCATED",
                    message=(
                        f"El contenido estructurado y texto normalizado se acotaron a {max_budget} caracteres."
                    ),
                    source_url=source_url,
                    severity="warning",
                )
            )

        if not headings and not paragraphs and not lists:
            warnings.append(
                WebIssue(
                    code="EMPTY_CONTENT",
                    message="La página HTML no contenía bloques de texto informativo extraíbles.",
                    source_url=source_url,
                    severity="warning",
                )
            )
            status = ExtractionStatus.PARTIAL
        else:
            status = ExtractionStatus.PARTIAL if truncated else ExtractionStatus.SUCCESS

        return WebSourceDocument(
            original_url=fetch_result.original_url,
            final_url=source_url,
            status=status,
            http_status=fetch_result.http_status,
            metadata=metadata,
            headings=tuple(headings),
            paragraphs=tuple(paragraphs),
            lists=tuple(lists),
            sections=tuple(sections),
            links=tuple(links_map.values()),
            normalized_text=full_normalized,
            redirect_chain=fetch_result.redirect_chain,
            bytes_received=fetch_result.bytes_received,
            bytes_decompressed=fetch_result.bytes_decompressed,
            truncated=truncated,
            warnings=tuple(warnings),
        )
