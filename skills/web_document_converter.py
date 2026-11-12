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
# (incluyendo definiciones de referencia normales `[id]: url` y con circunflejo `[^id]: url`).
_MD_BLOCK_SPOOF_RE = re.compile(r"^(#{1,6}\s|>\s?|```|~~~|\[\^?[^\]]+\]:|[-*+]\s|\d+[.)]\s)")

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
}


def _sanitize_markdown_url(raw_url: str) -> str:
    """
    Normaliza y codifica caracteres que pueden romper la sintaxis `[label](url)` o
    inyectar enlaces/definiciones adicionales en Markdown, preservando URLs válidas.
    """
    cleaned = "".join(ch for ch in (raw_url or "").strip() if ord(ch) >= 0x20 and ord(ch) != 0x7F)
    if not cleaned:
        return ""
    return "".join(_MD_UNSAFE_URL_CHARS.get(ch, ch) for ch in cleaned)


def _sanitize_markdown_text(text: str) -> str:
    """
    Neutraliza sintaxis de bloque Markdown accidental al comienzo de párrafos web crudos
    para que el texto extraído de páginas externas no falsifique citas ni encabezados.
    """
    clean = " ".join((text or "").splitlines()).strip()
    if not clean:
        return ""
    # Evitar también etiquetas de imagen/alerta Markdown crudas residuales.
    clean = clean.replace("[!", "\\[!").replace("![", "\\![")
    if _MD_BLOCK_SPOOF_RE.match(clean):
        return f"\\{clean}"
    return clean


def _sanitize_markdown_heading(text: str) -> str:
    """Limpia texto de encabezado o metadato para emitir una línea Markdown segura."""
    clean = " ".join((text or "").replace("\r", " ").replace("\n", " ").split()).strip()
    if _MD_BLOCK_SPOOF_RE.match(clean):
        return f"\\{clean}"
    return clean


def _sanitize_link_label(text: str, fallback: str) -> str:
    """Escapa corchetes en etiquetas de enlace Markdown sin alterar el texto legible."""
    base = _sanitize_markdown_heading(text) or _sanitize_markdown_url(fallback)
    return base.replace("[", "\\[").replace("]", "\\]")


def _append_source_provenance_lines(
    lines: list[str],
    source: WebSourceDocument,
    source_tag: str,
) -> None:
    """Agrega los metadatos reales y la procedencia de una fuente sin inventar campos ausentes."""
    safe_orig_url = _sanitize_markdown_url(source.original_url)
    safe_final_url = _sanitize_markdown_url(source.final_url)
    lines.append(f"- **Identificador de trazabilidad:** `[{source_tag}]`")
    lines.append(f"- **URL original:** {safe_orig_url}")
    if safe_final_url and safe_final_url != safe_orig_url:
        lines.append(f"- **URL final tras redirecciones:** {safe_final_url}")
        if source.redirect_chain:
            chain_txt = " -> ".join(_sanitize_markdown_url(u) for u in source.redirect_chain)
            lines.append(f"- **Cadena de redirecciones:** {chain_txt}")
    lines.append(f"- **Estado de extracción:** `{source.status.value}`")
    if source.http_status is not None:
        lines.append(f"- **Código HTTP:** {source.http_status}")
    lines.append(f"- **Fecha de extracción (UTC):** {source.metadata.extracted_at}")
    if source.metadata.published_at:
        lines.append(f"- **Fecha de publicación declarada:** {_sanitize_markdown_heading(source.metadata.published_at)}")
    if source.metadata.author:
        lines.append(f"- **Autor declarado:** {_sanitize_markdown_heading(source.metadata.author)}")
    if source.metadata.language:
        lines.append(f"- **Idioma:** {_sanitize_markdown_heading(source.metadata.language)}")
    if source.metadata.site_name:
        lines.append(f"- **Sitio:** {_sanitize_markdown_heading(source.metadata.site_name)}")
    if source.metadata.canonical_url:
        lines.append(f"- **URL canónica declarada:** {_sanitize_markdown_url(source.metadata.canonical_url)}")
    if source.metadata.description:
        lines.append(f"- **Descripción:** {_sanitize_markdown_text(source.metadata.description)}")
    lines.append("")

    if source.errors or source.warnings:
        lines.append("#### Incidencias de esta fuente")
        lines.append("")
        for err in source.errors:
            err_url = _sanitize_markdown_url(err.source_url or source.final_url)
            lines.append(
                f"- **Error [{err.code}]** ({err_url}): "
                f"{_sanitize_markdown_heading(err.message)}"
            )
        for warn in source.warnings:
            warn_url = _sanitize_markdown_url(warn.source_url or source.final_url)
            lines.append(
                f"- **Advertencia [{warn.code}]** ({warn_url}): "
                f"{_sanitize_markdown_heading(warn.message)}"
            )
        lines.append("")


def _append_source_body(
    lines: list[str],
    source: WebSourceDocument,
    *,
    heading_offset: int,
    source_tag: str,
) -> None:
    """Renderiza secciones, párrafos y listas conservando orden jerárquico y referencia a fuente."""
    if source.sections:
        for sec in source.sections:
            if sec.heading is not None:
                lvl = min(6, max(heading_offset, sec.heading.level + heading_offset - 1))
                hashes = "#" * lvl
                lines.append(f"{hashes} {_sanitize_markdown_heading(sec.heading.text)}")
                lines.append("")

            # Reconstruir párrafos y listas conservando su orden dentro de la sección.
            for para in sec.paragraphs:
                safe_p = _sanitize_markdown_text(para.text)
                if safe_p:
                    lines.append(safe_p)
                    lines.append("")
            for lst in sec.lists:
                for idx, item in enumerate(lst.items, start=1):
                    safe_item = _sanitize_markdown_text(item)
                    if not safe_item:
                        continue
                    prefix = f"{idx}." if lst.ordered else "-"
                    lines.append(f"{prefix} {safe_item}")
                lines.append("")
    elif source.normalized_text.strip():
        for raw_block in source.normalized_text.split("\n\n"):
            safe_block = _sanitize_markdown_text(raw_block)
            if safe_block:
                lines.append(safe_block)
                lines.append("")
    else:
        lines.append(
            f"*No se extrajo contenido textual de `[{source_tag}]` "
            f"(estado: `{source.status.value}`).*"
        )
        lines.append("")


class WebDocumentConverter:
    """
    Adaptador desacoplado para convertir resultados de `WebExtractionSkill` hacia
    el formato Markdown, modelo `Document` y parámetros de `DocumentExportSkill`.
    """

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

        doc_title = self.resolve_title(data, explicit_title=title)
        lines: list[str] = [f"# {doc_title}", ""]

        if isinstance(data, WebSourceDocument):
            lines.append("## Procedencia y Metadatos")
            lines.append("")
            _append_source_provenance_lines(lines, data, "src-1")

            lines.append("## Contenido Extraído [^src-1]")
            lines.append("")
            _append_source_body(lines, data, heading_offset=3, source_tag="src-1")

            if include_links and data.links:
                lines.append("## Enlaces Relevantes")
                lines.append("")
                for lk in data.links[:max_links_per_source]:
                    scope = "externo" if lk.is_external else "interno"
                    safe_lk_url = _sanitize_markdown_url(lk.url)
                    label = _sanitize_link_label(lk.text, safe_lk_url)
                    lines.append(f"- [{label}]({safe_lk_url}) ({scope}; fuente: `[{ 'src-1' }]`)")
                lines.append("")

            # Se coloca al final para que `_extract_references()` del Paso #3.A capture
            # únicamente las citas oficiales de procedencia.
            lines.append("## Fuentes y Referencias")
            lines.append("")
            cite_title = _sanitize_markdown_heading(data.metadata.title or "Fuente principal")
            safe_ref_url = _sanitize_markdown_url(data.original_url)
            lines.append(
                f"[^src-1]: {cite_title} — {safe_ref_url} (extraído: {data.metadata.extracted_at})"
            )
            lines.append("")
            return "\n".join(lines).strip() + "\n"

        # Caso múltiple o consulta: `WebExtractionReport`.
        sources = data.sources
        lines.append("## Resumen de Extracción")
        lines.append("")
        lines.append(f"- **Modo de investigación:** `{data.mode}`")
        lines.append(f"- **Estado general:** `{data.status.value}`")
        lines.append(f"- **Fecha de extracción (UTC):** {data.extracted_at}")
        if data.query:
            lines.append(f"- **Consulta:** {_sanitize_markdown_heading(data.query)}")
        if data.search_provider:
            lines.append(f"- **Proveedor de búsqueda:** `{data.search_provider}`")
        lines.append(f"- **Total de fuentes procesadas:** {len(sources)}")
        lines.append(f"- **Fuentes exitosas o parciales:** {len(data.successful_sources)}")
        lines.append(f"- **Fuentes fallidas o bloqueadas:** {len(data.failed_sources)}")
        lines.append("")

        if data.search_results:
            lines.append("## Resultados de Búsqueda")
            lines.append("")
            for sr in data.search_results:
                desc = f" — {_sanitize_markdown_text(sr.description)}" if sr.description else ""
                safe_sr_url = _sanitize_markdown_url(sr.url)
                label = _sanitize_link_label(sr.title, safe_sr_url)
                lines.append(f"{sr.rank}. [{label}]({safe_sr_url}) (`{sr.provider}`){desc}")
            lines.append("")

        if data.errors or data.warnings:
            lines.append("## Incidencias Globales")
            lines.append("")
            for err in data.errors:
                origin = f" ({_sanitize_markdown_url(err.source_url)})" if err.source_url else ""
                lines.append(
                    f"- **Error [{err.code}]**{origin}: {_sanitize_markdown_heading(err.message)}"
                )
            for warn in data.warnings:
                origin = f" ({_sanitize_markdown_url(warn.source_url)})" if warn.source_url else ""
                lines.append(
                    f"- **Advertencia [{warn.code}]**{origin}: {_sanitize_markdown_heading(warn.message)}"
                )
            lines.append("")

        all_links: list[tuple[str, WebLink]] = []
        for idx, src in enumerate(sources, start=1):
            src_tag = f"src-{idx}"
            src_title = _sanitize_markdown_heading(src.metadata.title or src.final_url or src.original_url)
            lines.append(f"## Fuente [{src_tag}]: {src_title} [^{src_tag}]")
            lines.append("")
            _append_source_provenance_lines(lines, src, src_tag)
            _append_source_body(lines, src, heading_offset=3, source_tag=src_tag)
            if include_links and src.links:
                for lk in src.links[:max_links_per_source]:
                    all_links.append((src_tag, lk))

        if include_links and all_links:
            lines.append("## Enlaces Relevantes Consolidados")
            lines.append("")
            for src_tag, lk in all_links[: max_links_per_source * max(1, len(sources))]:
                scope = "externo" if lk.is_external else "interno"
                safe_lk_url = _sanitize_markdown_url(lk.url)
                label = _sanitize_link_label(lk.text, safe_lk_url)
                lines.append(f"- `[{src_tag}]` [{label}]({safe_lk_url}) ({scope})")
            lines.append("")

        # Sección final de citas/fuentes: contiene una sola URL (`original_url`) por definición `[^src-N]`.
        lines.append("## Fuentes y Referencias")
        lines.append("")
        if sources:
            for idx, src in enumerate(sources, start=1):
                src_tag = f"src-{idx}"
                src_label = _sanitize_markdown_heading(src.metadata.title or "Fuente web")
                safe_ref_url = _sanitize_markdown_url(src.original_url)
                lines.append(
                    f"[^{src_tag}]: {src_label} — {safe_ref_url} "
                    f"(estado: {src.status.value}; extraído: {src.metadata.extracted_at})"
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
