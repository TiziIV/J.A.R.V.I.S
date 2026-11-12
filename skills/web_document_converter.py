"""
Convertidor desacoplado entre resultados de `WebExtractionSkill` y el subsistema de
exportación documental (`DocumentExportSkill`, Paso #3.A).

Reglas de diseño:
1. Desacoplamiento total: NO modifica ningún archivo cerrado del Paso #3.A y opera
   únicamente contra sus interfaces públicas (`DocumentParser`, `get_template`,
   `is_template_available`, `DocumentExportSkill.execute`).
2. Carga perezosa real: `DocumentParser` y `DocumentExportSkill` se importan únicamente
   cuando se invoca la conversión o exportación; jamás al importar `WebExtractionSkill`.
3. Trazabilidad sin pérdidas: preserva `original_url`, `final_url`, títulos, metadatos
   reales (sin confundir `extracted_at` con `published_at`), encabezados, párrafos, listas,
   enlaces, referencias `[^src-N]` y errores/advertencias parciales de fuentes múltiples.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from skills.web_models import (
    ExtractionStatus,
    WebExtractionReport,
    WebLink,
    WebSourceDocument,
)

if TYPE_CHECKING:
    from skills.document_export_skill import DocumentExportSkill
    from skills.document_models import Document

# Detecta marcadores al inicio de línea que `DocumentParser` interpretaría como elementos especiales
# (incluyendo definiciones de referencia normales `[id]: url`, con circunflejo `[^id]: url` y tablas `|`).
_MD_BLOCK_SPOOF_RE = re.compile(r"^(#{1,6}\s|>\s?|```|~~~|\[\^?[^\]]+\]:|[-*+]\s|\d+[.)]\s|\|)")
_EMBEDDED_URL_SCHEME_RE = re.compile(r"(?:https?|ftp|file|javascript|data|vbscript)\s*:\s*//?[^\s)\]`]*", re.IGNORECASE)

_MD_UNSAFE_URL_CHARS: dict[str, str] = {
    " ": "%20",
    "\t": "%09",
    "(": "%28",
    ")": "%29",
    "[": "%5B",
    "]": "%5D",
    "<": "%3C",
    ">": "%3E",
    '"': "%22",
    "'": "%27",
    "`": "%60",
    "\\": "%5C",
}

_MAX_SAFE_MD_URL_LEN: int = 1024
_DEFAULT_MAX_SOURCE_MD_CHARS: int = 16_000
_DEFAULT_MAX_REPORT_MD_CHARS: int = 64_000


def _sanitize_markdown_url(raw_url: str) -> str:
    """
    Valida, normaliza a punycode ASCII y codifica caracteres que pueden romper la sintaxis
    `[label](url)` o inyectar enlaces/definiciones adicionales en Markdown.
    Rechaza esquemas no HTTP/HTTPS, credenciales incrustadas y URLs sobredimensionadas.
    """
    cleaned = "".join(ch for ch in (raw_url or "").strip() if ord(ch) >= 0x20 and ord(ch) != 0x7F)
    if not cleaned or len(cleaned) > _MAX_SAFE_MD_URL_LEN:
        return ""
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        return ""
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.netloc:
        return ""
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return ""
    try:
        port = parts.port
        hostname = parts.hostname
    except ValueError:
        return ""
    if not hostname or any(ch in hostname for ch in " ()[]<>\"'`\\"):
        return ""
    raw_host = hostname.strip().rstrip(".")
    if not raw_host:
        return ""
    if ":" in raw_host:
        norm_host = f"[{raw_host.lower()}]"
    else:
        try:
            norm_host = raw_host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return ""
        if not norm_host or any(ch in norm_host for ch in " /?#@\\"):
            return ""
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{norm_host}:{port}"
    else:
        netloc = norm_host
    safe_path = "".join(_MD_UNSAFE_URL_CHARS.get(ch, ch) for ch in (parts.path or "/"))
    safe_query = "".join(_MD_UNSAFE_URL_CHARS.get(ch, ch) for ch in parts.query)
    normalized = urlunsplit((scheme, netloc, safe_path, safe_query, ""))
    if len(normalized) > _MAX_SAFE_MD_URL_LEN:
        return ""
    return normalized


def _sanitize_inline_code(value: str, *, max_len: int = 80) -> str:
    """Sanea identificadores técnicos (proveedor, código de error, modo, timestamp) para spans `` `...` ``."""
    clean = " ".join((str(value or "")).replace("\r", " ").replace("\n", " ").split()).strip()
    clean = _EMBEDDED_URL_SCHEME_RE.sub("", clean)
    for ch in ("`", "[", "]", "(", ")", "<", ">", "|", "\\"):
        clean = clean.replace(ch, "")
    return " ".join(clean.split())[:max_len].strip() or "n/a"


def _sanitize_markdown_text(text: str, *, max_len: int = 4000) -> str:
    """
    Neutraliza sintaxis de bloque Markdown accidental al comienzo de párrafos web crudos
    para que el texto extraído de páginas externas no falsifique citas, tablas ni encabezados.
    """
    clean = " ".join((text or "").splitlines()).strip()
    if not clean:
        return ""
    if len(clean) > max_len:
        clean = clean[:max_len].rstrip()
    clean = clean.rstrip("\\").strip()
    if not clean:
        return ""
    # Evitar etiquetas de imagen/alerta Markdown crudas residuales y enlaces por referencia `[x][y]`.
    clean = clean.replace("[!", "\\[!").replace("![", "\\![")
    clean = re.sub(r"\]\s*\[", "]\\[", clean)
    if _MD_BLOCK_SPOOF_RE.match(clean):
        return f"\\{clean}"
    return clean


def _sanitize_markdown_heading(text: str, *, max_len: int = 300) -> str:
    """Limpia texto de encabezado o metadato para emitir una línea Markdown segura."""
    clean = " ".join((text or "").replace("\r", " ").replace("\n", " ").split()).strip()
    if len(clean) > max_len:
        clean = clean[:max_len].rstrip()
    clean = clean.rstrip("\\").strip()
    if not clean:
        return ""
    clean = clean.replace("[!", "\\[!").replace("![", "\\![")
    clean = re.sub(r"\]\s*\[", "]\\[", clean)
    if _MD_BLOCK_SPOOF_RE.match(clean):
        return f"\\{clean}"
    return clean


def _sanitize_citation_label(text: str, *, max_len: int = 240) -> str:
    """
    Sanea el título mostrado dentro de una definición `[^src-N]:` neutralizando cualquier
    esquema URL (`http://`, `https://`, `javascript:`, etc.) para impedir que `RE_URL` de
    `DocumentParser` secuestre la URL oficial de la fuente (F-01 Citation URL Hijack).
    """
    base = _sanitize_markdown_heading(text, max_len=max_len)
    if not base:
        return "Fuente web"
    neutralized = _EMBEDDED_URL_SCHEME_RE.sub("[url-neutralizada] ", base)
    neutralized = neutralized.replace("[", "(").replace("]", ")")
    return " ".join(neutralized.split()).strip() or "Fuente web"


def _sanitize_link_label(text: str, fallback: str, *, max_len: int = 200) -> str:
    """Escapa corchetes en etiquetas de enlace Markdown sin alterar el texto legible."""
    base = _sanitize_markdown_heading(text, max_len=max_len) or _sanitize_markdown_url(fallback) or "enlace"
    return base.replace("[", "\\[").replace("]", "\\]")


def _append_source_provenance_lines(
    lines: list[str],
    source: WebSourceDocument,
    source_tag: str,
) -> None:
    """Agrega los metadatos reales y la procedencia de una fuente sin inventar campos ausentes."""
    safe_tag = _sanitize_inline_code(source_tag, max_len=32)
    safe_orig_url = _sanitize_markdown_url(source.original_url) or "[url-invalida-omitida]"
    safe_final_url = _sanitize_markdown_url(source.final_url)
    lines.append(f"- **Identificador de trazabilidad:** `[{safe_tag}]`")
    lines.append(f"- **URL original:** {safe_orig_url}")
    if safe_final_url and safe_final_url != safe_orig_url:
        lines.append(f"- **URL final tras redirecciones:** {safe_final_url}")
        if source.redirect_chain:
            chain_urls = [_sanitize_markdown_url(u) for u in source.redirect_chain[:10]]
            chain_txt = " -> ".join(u for u in chain_urls if u)
            if chain_txt:
                lines.append(f"- **Cadena de redirecciones:** {chain_txt}")
    lines.append(f"- **Estado de extracción:** `{_sanitize_inline_code(source.status.value, max_len=24)}`")
    if source.http_status is not None:
        lines.append(f"- **Código HTTP:** {int(source.http_status)}")
    lines.append(f"- **Fecha de extracción (UTC):** {_sanitize_inline_code(source.metadata.extracted_at, max_len=64)}")
    if source.metadata.published_at:
        lines.append(f"- **Fecha de publicación declarada:** {_sanitize_markdown_heading(source.metadata.published_at, max_len=64)}")
    if source.metadata.author:
        lines.append(f"- **Autor declarado:** {_sanitize_markdown_heading(source.metadata.author, max_len=160)}")
    if source.metadata.language:
        lines.append(f"- **Idioma:** {_sanitize_markdown_heading(source.metadata.language, max_len=32)}")
    if source.metadata.site_name:
        lines.append(f"- **Sitio:** {_sanitize_markdown_heading(source.metadata.site_name, max_len=160)}")
    if source.metadata.canonical_url:
        safe_canon = _sanitize_markdown_url(source.metadata.canonical_url)
        if safe_canon:
            lines.append(f"- **URL canónica declarada:** {safe_canon}")
    if source.metadata.description:
        lines.append(f"- **Descripción:** {_sanitize_markdown_text(source.metadata.description, max_len=600)}")
    lines.append("")

    if source.errors or source.warnings:
        lines.append("#### Incidencias de esta fuente")
        lines.append("")
        for err in source.errors[:20]:
            err_url = _sanitize_markdown_url(err.source_url or source.final_url) or safe_orig_url
            safe_code = _sanitize_inline_code(err.code, max_len=48)
            lines.append(
                f"- **Error [{safe_code}]** ({err_url}): "
                f"{_sanitize_markdown_heading(err.message, max_len=240)}"
            )
        for warn in source.warnings[:20]:
            warn_url = _sanitize_markdown_url(warn.source_url or source.final_url) or safe_orig_url
            safe_code = _sanitize_inline_code(warn.code, max_len=48)
            lines.append(
                f"- **Advertencia [{safe_code}]** ({warn_url}): "
                f"{_sanitize_markdown_heading(warn.message, max_len=240)}"
            )
        lines.append("")


def _append_source_body(
    lines: list[str],
    source: WebSourceDocument,
    *,
    heading_offset: int,
    source_tag: str,
    max_body_chars: int = _DEFAULT_MAX_SOURCE_MD_CHARS,
) -> None:
    """Renderiza secciones, párrafos y listas conservando orden jerárquico y referencia a fuente."""
    emitted_chars = 0
    budget = max(120, int(max_body_chars))
    safe_tag = _sanitize_inline_code(source_tag, max_len=32)

    if source.sections:
        for sec in source.sections:
            if emitted_chars >= budget:
                break
            if sec.heading is not None:
                lvl = min(6, max(heading_offset, sec.heading.level + heading_offset - 1))
                hashes = "#" * lvl
                safe_h = _sanitize_markdown_heading(sec.heading.text, max_len=min(300, budget - emitted_chars))
                if safe_h:
                    lines.append(f"{hashes} {safe_h}")
                    lines.append("")
                    emitted_chars += len(safe_h)

            # Reconstruir párrafos y listas conservando su orden dentro de la sección.
            for para in sec.paragraphs:
                if emitted_chars >= budget:
                    break
                safe_p = _sanitize_markdown_text(para.text, max_len=max(1, budget - emitted_chars))
                if safe_p:
                    lines.append(safe_p)
                    lines.append("")
                    emitted_chars += len(safe_p)
            for lst in sec.lists:
                if emitted_chars >= budget:
                    break
                for idx, item in enumerate(lst.items, start=1):
                    if emitted_chars >= budget:
                        break
                    safe_item = _sanitize_markdown_text(item, max_len=max(1, budget - emitted_chars))
                    if not safe_item:
                        continue
                    prefix = f"{idx}." if lst.ordered else "-"
                    lines.append(f"{prefix} {safe_item}")
                    emitted_chars += len(safe_item)
                lines.append("")
    elif source.normalized_text.strip():
        for raw_block in source.normalized_text.split("\n\n"):
            if emitted_chars >= budget:
                break
            safe_block = _sanitize_markdown_text(raw_block, max_len=max(1, budget - emitted_chars))
            if safe_block:
                lines.append(safe_block)
                lines.append("")
                emitted_chars += len(safe_block)
    else:
        lines.append(
            f"*No se extrajo contenido textual de `[{safe_tag}]` "
            f"(estado: `{_sanitize_inline_code(source.status.value, max_len=24)}`).*"
        )
        lines.append("")


class WebDocumentConverter:
    """
    Adaptador desacoplado para convertir resultados de `WebExtractionSkill` hacia
    el formato Markdown, modelo `Document` y parámetros de `DocumentExportSkill`.
    """

    def __init__(self, *, max_markdown_chars: int = _DEFAULT_MAX_REPORT_MD_CHARS) -> None:
        self.max_markdown_chars = max(1000, int(max_markdown_chars))

    def resolve_title(
        self,
        data: WebSourceDocument | WebExtractionReport,
        explicit_title: str | None = None,
    ) -> str:
        """Determina un título trazable sin inventar metadatos."""
        if explicit_title and explicit_title.strip():
            return _sanitize_markdown_heading(explicit_title)
        if isinstance(data, WebSourceDocument):
            return _sanitize_markdown_heading(data.metadata.title or data.final_url or data.original_url)
        if data.query:
            return _sanitize_markdown_heading(f"Investigación web: {data.query}")
        if len(data.sources) == 1:
            only = data.sources[0]
            return _sanitize_markdown_heading(only.metadata.title or only.final_url or only.original_url)
        for src in data.sources:
            if src.ok and src.metadata.title:
                return _sanitize_markdown_heading(f"Síntesis multi-fuente: {src.metadata.title}")
        return "Informe de extracción web"

    def _collect_provenance_author(self, data: WebSourceDocument | WebExtractionReport) -> str | None:
        if isinstance(data, WebSourceDocument):
            return data.metadata.author
        authors = [s.metadata.author for s in data.sources if s.metadata.author]
        unique = list(dict.fromkeys(authors))
        return unique[0] if len(unique) == 1 else None

    def to_markdown(
        self,
        data: WebSourceDocument | WebExtractionReport,
        *,
        title: str | None = None,
        mode: str = "general",
        template: str | None = None,
        include_links: bool = True,
        max_links_per_source: int = 25,
        max_output_chars: int | None = None,
    ) -> str:
        """
        Convierte un `WebSourceDocument` o `WebExtractionReport` a Markdown estructurado
        compatible con `DocumentParser` y las plantillas de `DocumentExportSkill`.
        """
        if not isinstance(data, (WebSourceDocument, WebExtractionReport)):
            raise TypeError(
                "data debe ser una instancia de WebSourceDocument o WebExtractionReport."
            )

        norm_mode = str(mode or "general").strip().lower()
        if norm_mode not in ("general", "study"):
            raise ValueError(f"Modo documental no soportado: '{mode}'. Use 'general' o 'study'.")
        if template is not None and str(template).strip():
            from skills.document_templates import get_template, is_template_available

            clean_tpl = str(template).strip()
            if not is_template_available(clean_tpl):
                raise ValueError(f"Plantilla documental desconocida: '{template}'.")
            _ = get_template(clean_tpl)

        effective_max_chars = max(1000, int(max_output_chars or self.max_markdown_chars))
        doc_title = self.resolve_title(data, explicit_title=title)
        lines: list[str] = [f"# {doc_title}", ""]

        if isinstance(data, WebSourceDocument):
            lines.append("## Procedencia y Metadatos")
            lines.append("")
            _append_source_provenance_lines(lines, data, "src-1")

            lines.append("## Contenido Extraído [^src-1]")
            lines.append("")
            _append_source_body(
                lines,
                data,
                heading_offset=3,
                source_tag="src-1",
                max_body_chars=min(_DEFAULT_MAX_SOURCE_MD_CHARS, max(500, effective_max_chars - 1500)),
            )

            if include_links and data.links:
                link_lines: list[str] = []
                links_chars = 0
                max_links_budget = min(4000, max(200, effective_max_chars // 4))
                for lk in data.links[:max_links_per_source]:
                    safe_lk_url = _sanitize_markdown_url(lk.url)
                    if not safe_lk_url:
                        continue
                    scope = "externo" if lk.is_external else "interno"
                    label = _sanitize_link_label(lk.text, safe_lk_url)
                    entry = f"- [{label}]({safe_lk_url}) ({scope}; fuente: `[{ 'src-1' }]`)"
                    if links_chars + len(entry) > max_links_budget:
                        break
                    link_lines.append(entry)
                    links_chars += len(entry)
                if link_lines:
                    lines.append("## Enlaces Relevantes")
                    lines.append("")
                    lines.extend(link_lines)
                    lines.append("")

            # Se coloca al final para que `_extract_references()` del Paso #3.A capture
            # únicamente las citas oficiales de procedencia sin posibilidad de secuestro de URL (F-01).
            lines.append("## Fuentes y Referencias")
            lines.append("")
            cite_title = _sanitize_citation_label(data.metadata.title or "Fuente principal")
            safe_ref_url = _sanitize_markdown_url(data.original_url) or _sanitize_markdown_url(data.final_url) or "https://invalid.local/"
            safe_ext_at = _sanitize_inline_code(data.metadata.extracted_at, max_len=64)
            lines.append(
                f"[^src-1]: {cite_title} — {safe_ref_url} (extraído: {safe_ext_at})"
            )
            lines.append("")
            return "\n".join(lines).strip() + "\n"

        # Caso múltiple o consulta: `WebExtractionReport`.
        sources = data.sources
        lines.append("## Resumen de Extracción")
        lines.append("")
        lines.append(f"- **Modo de investigación:** `{_sanitize_inline_code(data.mode, max_len=32)}`")
        lines.append(f"- **Estado general:** `{_sanitize_inline_code(data.status.value, max_len=24)}`")
        lines.append(f"- **Fecha de extracción (UTC):** {_sanitize_inline_code(data.extracted_at, max_len=64)}")
        if data.query:
            lines.append(f"- **Consulta:** {_sanitize_markdown_heading(data.query)}")
        if data.search_provider:
            lines.append(f"- **Proveedor de búsqueda:** `{_sanitize_inline_code(data.search_provider, max_len=48)}`")
        lines.append(f"- **Total de fuentes procesadas:** {len(sources)}")
        lines.append(f"- **Fuentes exitosas o parciales:** {len(data.successful_sources)}")
        lines.append(f"- **Fuentes fallidas o bloqueadas:** {len(data.failed_sources)}")
        lines.append("")

        if data.search_results:
            sr_lines: list[str] = []
            for sr in data.search_results[:25]:
                safe_sr_url = _sanitize_markdown_url(sr.url)
                if not safe_sr_url:
                    continue
                desc = f" — {_sanitize_markdown_text(sr.description, max_len=300)}" if sr.description else ""
                label = _sanitize_link_label(sr.title, safe_sr_url)
                safe_prov = _sanitize_inline_code(sr.provider, max_len=48)
                sr_lines.append(f"{int(sr.rank)}. [{label}]({safe_sr_url}) (`{safe_prov}`){desc}")
            if sr_lines:
                lines.append("## Resultados de Búsqueda")
                lines.append("")
                lines.extend(sr_lines)
                lines.append("")

        if data.errors or data.warnings:
            lines.append("## Incidencias Globales")
            lines.append("")
            for err in data.errors[:20]:
                safe_err_url = _sanitize_markdown_url(err.source_url) if err.source_url else ""
                origin = f" ({safe_err_url})" if safe_err_url else ""
                safe_code = _sanitize_inline_code(err.code, max_len=48)
                lines.append(
                    f"- **Error [{safe_code}]**{origin}: {_sanitize_markdown_heading(err.message, max_len=240)}"
                )
            for warn in data.warnings[:20]:
                safe_warn_url = _sanitize_markdown_url(warn.source_url) if warn.source_url else ""
                origin = f" ({safe_warn_url})" if safe_warn_url else ""
                safe_code = _sanitize_inline_code(warn.code, max_len=48)
                lines.append(
                    f"- **Advertencia [{safe_code}]**{origin}: {_sanitize_markdown_heading(warn.message, max_len=240)}"
                )
            lines.append("")

        per_source_budget = max(
            800,
            min(_DEFAULT_MAX_SOURCE_MD_CHARS, (effective_max_chars - 2500) // max(1, len(sources))),
        )
        all_links: list[tuple[str, WebLink]] = []
        for idx, src in enumerate(sources, start=1):
            src_tag = f"src-{idx}"
            src_title = _sanitize_markdown_heading(src.metadata.title or src.final_url or src.original_url)
            lines.append(f"## Fuente [{src_tag}]: {src_title} [^{src_tag}]")
            lines.append("")
            _append_source_provenance_lines(lines, src, src_tag)
            _append_source_body(
                lines,
                src,
                heading_offset=3,
                source_tag=src_tag,
                max_body_chars=per_source_budget,
            )
            if include_links and src.links:
                for lk in src.links[:max_links_per_source]:
                    all_links.append((src_tag, lk))

        if include_links and all_links:
            consolidated_lines: list[str] = []
            links_chars = 0
            max_links_budget = min(6000, max(300, effective_max_chars // 5))
            for src_tag, lk in all_links[: max_links_per_source * max(1, len(sources))]:
                safe_lk_url = _sanitize_markdown_url(lk.url)
                if not safe_lk_url:
                    continue
                scope = "externo" if lk.is_external else "interno"
                label = _sanitize_link_label(lk.text, safe_lk_url)
                entry = f"- `[{src_tag}]` [{label}]({safe_lk_url}) ({scope})"
                if links_chars + len(entry) > max_links_budget:
                    break
                consolidated_lines.append(entry)
                links_chars += len(entry)
            if consolidated_lines:
                lines.append("## Enlaces Relevantes Consolidados")
                lines.append("")
                lines.extend(consolidated_lines)
                lines.append("")

        # Sección final de citas/fuentes: contiene una sola URL (`original_url`) por definición `[^src-N]`.
        lines.append("## Fuentes y Referencias")
        lines.append("")
        if sources:
            for idx, src in enumerate(sources, start=1):
                src_tag = f"src-{idx}"
                src_label = _sanitize_citation_label(src.metadata.title or "Fuente web")
                safe_ref_url = _sanitize_markdown_url(src.original_url) or _sanitize_markdown_url(src.final_url) or "https://invalid.local/"
                safe_status = _sanitize_inline_code(src.status.value, max_len=24)
                safe_ext_at = _sanitize_inline_code(src.metadata.extracted_at, max_len=64)
                lines.append(
                    f"[^{src_tag}]: {src_label} — {safe_ref_url} "
                    f"(estado: {safe_status}; extraído: {safe_ext_at})"
                )
        else:
            lines.append("- No se obtuvieron fuentes web en esta ejecución.")
        lines.append("")
        return "\n".join(lines).strip() + "\n"

    def to_document(
        self,
        data: WebSourceDocument | WebExtractionReport,
        *,
        title: str | None = None,
        mode: str = "general",
        template: str | None = None,
        include_links: bool = True,
        max_links_per_source: int = 25,
    ) -> Document:
        """
        Construye el modelo intermedio `skills.document_models.Document` reutilizando
        el parser público del Paso #3.A sin modificar sus definiciones.
        """
        from skills.document_parser import DocumentParser

        doc_title = self.resolve_title(data, explicit_title=title)
        md_content = self.to_markdown(
            data,
            title=doc_title,
            mode=mode,
            template=template,
            include_links=include_links,
            max_links_per_source=max_links_per_source,
        )
        metadata: dict[str, str] = {
            "source_pipeline": "web_extraction",
            "extracted_at": data.extracted_at if isinstance(data, WebExtractionReport) else data.metadata.extracted_at,
            "extraction_status": data.status.value,
        }
        if isinstance(data, WebSourceDocument):
            metadata["original_url"] = data.original_url
            metadata["final_url"] = data.final_url
            if data.metadata.published_at:
                metadata["published_at"] = data.metadata.published_at
            if data.metadata.language:
                metadata["language"] = data.metadata.language
        else:
            metadata["mode"] = data.mode
            metadata["source_count"] = str(len(data.sources))
            if data.query:
                metadata["query"] = data.query
            if data.search_provider:
                metadata["search_provider"] = data.search_provider

        parser = DocumentParser()
        return parser.parse(
            md_content,
            title=doc_title,
            author=self._collect_provenance_author(data),
            metadata=metadata,
        )

    def to_export_params(
        self,
        data: WebSourceDocument | WebExtractionReport,
        *,
        output_path: str | None = None,
        format: str = "md",
        title: str | None = None,
        mode: str = "general",
        template: str | None = None,
        overwrite: bool = False,
        include_links: bool = True,
        max_links_per_source: int = 25,
    ) -> dict[str, Any]:
        """
        Genera el diccionario de parámetros listo para `DocumentExportSkill.execute(...)`.
        """
        if isinstance(overwrite, str) or not isinstance(overwrite, bool):
            raise TypeError("overwrite debe ser un booleano estricto (True/False).")
        norm_fmt = str(format or "md").strip().lower()
        if norm_fmt not in ("md", "docx", "both"):
            raise ValueError(f"Formato de exportación no soportado: '{format}'.")

        doc_title = self.resolve_title(data, explicit_title=title)
        md_content = self.to_markdown(
            data,
            title=doc_title,
            mode=mode,
            template=template,
            include_links=include_links,
            max_links_per_source=max_links_per_source,
        )
        params: dict[str, Any] = {
            "content": md_content,
            "format": norm_fmt,
            "title": doc_title,
            "mode": str(mode or "general").strip().lower(),
            "overwrite": overwrite,
        }
        if output_path is not None:
            params["output_path"] = output_path
        if template is not None:
            params["template"] = template
        return params

    def export(
        self,
        data: WebSourceDocument | WebExtractionReport,
        *,
        exporter: DocumentExportSkill | None = None,
        output_path: str | None = None,
        format: str = "md",
        title: str | None = None,
        mode: str = "general",
        template: str | None = None,
        overwrite: bool = False,
        include_links: bool = True,
        max_links_per_source: int = 25,
    ) -> str:
        """
        Adapta el resultado web y delega la escritura segura en `DocumentExportSkill`.
        """
        from skills.document_export_skill import DocumentExportSkill

        target_exporter = exporter or DocumentExportSkill()
        params = self.to_export_params(
            data,
            output_path=output_path,
            format=format,
            title=title,
            mode=mode,
            template=template,
            overwrite=overwrite,
            include_links=include_links,
            max_links_per_source=max_links_per_source,
        )
        return target_exporter.execute(params)
