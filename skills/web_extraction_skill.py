"""
Skill reutilizable `WebExtractionSkill` para el Proyecto J.A.R.V.I.S. (Paso #3.B).

Responsabilidades:
1. Aceptar una URL individual, una colección de URLs o una consulta de investigación.
2. Delegar descarga HTTP/HTTPS segura a `WebFetcher` (SSRF, política estricta de puertos,
   DNS rebinding mitigado en el socket real, redirecciones controladas, streaming acotado).
3. Extraer y normalizar contenido HTML mediante `WebContentExtractor` en modelos
   inmutables independientes de Ollama, Gemini, Claude, GPT o Codex.
4. Utilizar `SearchProvider` desacoplado (`UnconfiguredSearchProvider` por defecto cuando no
   existe un motor programático en el repositorio), informando con honestidad sus límites sin
   romper la extracción directa por URL.
5. Exponer contrato estándar `BaseSkill.execute(...)` con soporte perezoso de salida en
   resumen textual, JSON estructurado o Markdown vía `WebDocumentConverter`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from typing import Any, Iterable, Sequence

from skills.base_skill import BaseSkill
from skills.web_config import WebExtractionConfig
from skills.web_fetcher import WebFetchError, WebFetcher
from skills.web_models import (
    ExtractionStatus,
    WebExtractionReport,
    WebIssue,
    WebSourceDocument,
    utc_now_iso,
)
from skills.web_parser import WebContentExtractor
from skills.web_search_provider import SearchProvider, UnconfiguredSearchProvider
from skills.web_security import WebNetworkPolicy, WebSecurityError
from src.brain.exceptions import redact_secrets


def _compute_batch_status(sources: Sequence[WebSourceDocument]) -> ExtractionStatus:
    """Calcula el estado agregado de múltiples fuentes sin ocultar fallos parciales."""
    if not sources:
        return ExtractionStatus.FAILED
    statuses = [s.status for s in sources]
    ok_count = sum(1 for s in sources if s.ok)
    if ok_count == len(sources):
        if all(st == ExtractionStatus.SUCCESS for st in statuses):
            return ExtractionStatus.SUCCESS
        return ExtractionStatus.PARTIAL
    if ok_count > 0:
        return ExtractionStatus.PARTIAL
    if all(st == ExtractionStatus.BLOCKED for st in statuses):
        return ExtractionStatus.BLOCKED
    return ExtractionStatus.FAILED


def _format_human_summary(report: WebExtractionReport) -> str:
    """Genera un resumen conciso, legible y trazable apto para cualquier cerebro o consola."""
    lines: list[str] = [
        f"Extracción web ({report.mode}) — estado: {report.status.value} | fuentes: {len(report.sources)}"
    ]

    if report.query:
        provider_label = report.search_provider or "sin proveedor"
        lines.append(f"Consulta: {report.query} (proveedor: {provider_label})")

    for err in report.errors:
        lines.append(f"- Error [{err.code}]: {redact_secrets(err.message)}")
    for warn in report.warnings:
        lines.append(f"- Advertencia [{warn.code}]: {redact_secrets(warn.message)}")

    for idx, src in enumerate(report.sources, start=1):
        title = src.metadata.title or src.final_url or src.original_url
        lines.append(f"[{idx}] {title} ({src.status.value})")
        lines.append(f"    URL original: {src.original_url}")
        if src.final_url and src.final_url != src.original_url:
            lines.append(f"    URL final: {src.final_url}")
        meta_bits: list[str] = [f"extraído: {src.metadata.extracted_at}"]
        if src.metadata.published_at:
            meta_bits.append(f"publicado: {src.metadata.published_at}")
        if src.metadata.author:
            meta_bits.append(f"autor: {src.metadata.author}")
        if src.metadata.language:
            meta_bits.append(f"idioma: {src.metadata.language}")
        lines.append(f"    Metadatos: {' | '.join(meta_bits)}")
        lines.append(
            f"    Estructura: {len(src.headings)} encabezados, "
            f"{len(src.paragraphs)} párrafos, {len(src.lists)} listas, {len(src.links)} enlaces"
        )
        if src.paragraphs:
            preview = src.paragraphs[0].text
            if len(preview) > 220:
                preview = preview[:217].rstrip() + "..."
            lines.append(f"    Extracto: {preview}")
        elif src.normalized_text:
            preview = src.normalized_text[:220].replace("\n", " ").strip()
            lines.append(f"    Extracto: {preview}")

        for err in src.errors:
            lines.append(f"    Error fuente [{err.code}]: {redact_secrets(err.message)}")
        for warn in src.warnings:
            lines.append(f"    Aviso fuente [{warn.code}]: {redact_secrets(warn.message)}")

    return "\n".join(lines)


class WebExtractionSkill(BaseSkill):
    """
    Habilidad modular para extracción web segura, estructurada, normalizada y trazable.

    Opera de forma completamente agnóstica al motor de IA y no carga librerías de audio,
    `python-docx` ni `lxml` durante su inicialización ni durante la extracción web.
    """

    def __init__(
        self,
        *,
        config: WebExtractionConfig | None = None,
        network_policy: WebNetworkPolicy | None = None,
        fetcher: WebFetcher | None = None,
        extractor: WebContentExtractor | None = None,
        search_provider: SearchProvider | None = None,
    ) -> None:
        super().__init__(
            name="web_extraction",
            description=(
                "Extrae y normaliza contenido web estructurado (encabezados, párrafos, listas, enlaces y "
                "metadatos trazables) a partir de una URL, una lista de URLs o una consulta de investigación "
                "cuando hay un proveedor de búsqueda configurado."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL HTTP/HTTPS individual."},
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Lista de URLs HTTP/HTTPS.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Consulta de investigación cuando exista SearchProvider.",
                    },
                    "output_format": {
                        "type": "string",
                        "description": "Formato: 'summary', 'json' o 'markdown'.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Cantidad máxima de fuentes a extraer.",
                    },
                },
            },
        )
        self.config: WebExtractionConfig = config or WebExtractionConfig.from_env()
        self.network_policy: WebNetworkPolicy = network_policy or WebNetworkPolicy(config=self.config)
        self.fetcher: WebFetcher = fetcher or WebFetcher(
            config=self.config,
            network_policy=self.network_policy,
        )
        self.extractor: WebContentExtractor = extractor or WebContentExtractor(config=self.config)
        self.search_provider: SearchProvider = search_provider or UnconfiguredSearchProvider()

    def extract_url(self, url: str) -> WebSourceDocument:
        """
        Descarga y extrae de forma segura una URL individual devolviendo `WebSourceDocument`.
        Los bloqueos de seguridad y fallos de red quedan registrados de manera estructurada.
        """
        raw_url = str(url or "").strip() if isinstance(url, str) else repr(url)
        try:
            fetch_result = self.fetcher.fetch(url)
            return self.extractor.extract_from_fetch(fetch_result)
        except WebSecurityError as exc:
            return WebSourceDocument(
                original_url=raw_url,
                final_url=raw_url,
                status=ExtractionStatus.BLOCKED,
                errors=(
                    WebIssue(
                        code=exc.code,
                        message=redact_secrets(str(exc)),
                        source_url=raw_url,
                        severity="error",
                    ),
                ),
            )
        except WebFetchError as exc:
            return WebSourceDocument(
                original_url=raw_url,
                final_url=raw_url,
                status=ExtractionStatus.FAILED,
                http_status=exc.http_status,
                errors=(
                    WebIssue(
                        code=exc.code,
                        message=redact_secrets(str(exc)),
                        source_url=raw_url,
                        severity="error",
                    ),
                ),
            )
        except Exception as exc:
            return WebSourceDocument(
                original_url=raw_url,
                final_url=raw_url,
                status=ExtractionStatus.FAILED,
                errors=(
                    WebIssue(
                        code="UNEXPECTED_EXTRACTION_ERROR",
                        message=redact_secrets(f"Error inesperado al extraer la fuente: {exc}"),
                        source_url=raw_url,
                        severity="error",
                    ),
                ),
            )

    def extract_urls(
        self,
        urls: Iterable[str],
        *,
        max_pages: int | None = None,
    ) -> WebExtractionReport:
        """
        Extrae una colección acotada de URLs preservando orden, límite de páginas y
        concurrencia máxima configurada en `WebExtractionConfig`.
        """
        extracted_at = utc_now_iso()
        if isinstance(urls, (str, bytes)) or not isinstance(urls, Iterable):
            return WebExtractionReport(
                mode="urls",
                status=ExtractionStatus.FAILED,
                extracted_at=extracted_at,
                errors=(
                    WebIssue(
                        code="INVALID_URLS_INPUT",
                        message="'urls' debe ser una colección de cadenas URL válidas.",
                        source_url="",
                        severity="error",
                    ),
                ),
            )

        effective_limit = self.config.max_pages
        warnings: list[WebIssue] = []
        if max_pages is not None:
            if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
                return WebExtractionReport(
                    mode="urls",
                    status=ExtractionStatus.FAILED,
                    extracted_at=extracted_at,
                    errors=(
                        WebIssue(
                            code="INVALID_LIMIT",
                            message="'max_pages' debe ser un entero positivo.",
                            source_url="",
                            severity="error",
                        ),
                    ),
                )
            if max_pages > self.config.max_pages:
                warnings.append(
                    WebIssue(
                        code="MAX_PAGES_CLAMPED",
                        message=(
                            f"El límite solicitado ({max_pages}) se acotó al máximo "
                            f"configurado ({self.config.max_pages})."
                        ),
                        source_url="",
                        severity="warning",
                    )
                )
                effective_limit = self.config.max_pages
            else:
                effective_limit = max_pages

        unique_urls: list[str] = []
        seen: set[str] = set()
        for raw_item in urls:
            if not isinstance(raw_item, str):
                return WebExtractionReport(
                    mode="urls",
                    status=ExtractionStatus.FAILED,
                    extracted_at=extracted_at,
                    errors=(
                        WebIssue(
                            code="INVALID_URL_ELEMENT",
                            message=f"Todos los elementos de 'urls' deben ser texto (recibido: {type(raw_item).__name__}).",
                            source_url="",
                            severity="error",
                        ),
                    ),
                )
            cleaned = raw_item.strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            unique_urls.append(cleaned)

        if not unique_urls:
            return WebExtractionReport(
                mode="urls",
                status=ExtractionStatus.FAILED,
                extracted_at=extracted_at,
                errors=(
                    WebIssue(
                        code="EMPTY_URLS_LIST",
                        message="No se recibieron URLs no vacías para extraer.",
                        source_url="",
                        severity="error",
                    ),
                ),
            )

        if len(unique_urls) > effective_limit:
            warnings.append(
                WebIssue(
                    code="MAX_PAGES_LIMIT_REACHED",
                    message=(
                        f"Se recibieron {len(unique_urls)} URLs únicas; solo se procesaron "
                        f"las primeras {effective_limit} según la política de límites."
                    ),
                    source_url="",
                    severity="warning",
                )
            )
            target_urls = unique_urls[:effective_limit]
        else:
            target_urls = unique_urls

        if len(target_urls) == 1 or self.config.max_concurrent_connections <= 1:
            sources = tuple(self.extract_url(u) for u in target_urls)
        else:
            max_workers = min(len(target_urls), self.config.max_concurrent_connections)
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                sources = tuple(pool.map(self.extract_url, target_urls))

        status = _compute_batch_status(sources)
        return WebExtractionReport(
            mode="urls",
            sources=sources,
            status=status,
            extracted_at=extracted_at,
            warnings=tuple(warnings),
        )

    def extract_query(
        self,
        query: str,
        *,
        max_results: int | None = None,
    ) -> WebExtractionReport:
        """
        Ejecuta una consulta de investigación usando el `SearchProvider` configurado y
        extrae de forma segura los resultados devueltos.
        """
        extracted_at = utc_now_iso()
        if not isinstance(query, str) or not query.strip():
            return WebExtractionReport(
                mode="query",
                status=ExtractionStatus.FAILED,
                search_provider=self.search_provider.provider_name,
                extracted_at=extracted_at,
                errors=(
                    WebIssue(
                        code="EMPTY_QUERY",
                        message="La consulta de investigación ('query') debe ser una cadena no vacía.",
                        source_url="",
                        severity="error",
                    ),
                ),
            )

        clean_query = query.strip()
        warnings: list[WebIssue] = []
        if max_results is not None:
            if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1:
                return WebExtractionReport(
                    mode="query",
                    query=clean_query,
                    status=ExtractionStatus.FAILED,
                    search_provider=self.search_provider.provider_name,
                    extracted_at=extracted_at,
                    errors=(
                        WebIssue(
                            code="INVALID_LIMIT",
                            message="'max_results' debe ser un entero positivo.",
                            source_url="",
                            severity="error",
                        ),
                    ),
                )
            effective_limit = min(max_results, self.config.max_pages)
            if max_results > self.config.max_pages:
                warnings.append(
                    WebIssue(
                        code="MAX_PAGES_CLAMPED",
                        message=(
                            f"'max_results' ({max_results}) se acotó al máximo "
                            f"configurado ({self.config.max_pages})."
                        ),
                        source_url="",
                        severity="warning",
                    )
                )
        else:
            effective_limit = self.config.max_pages

        try:
            search_resp = self.search_provider.search(clean_query, max_results=effective_limit)
        except Exception as exc:
            return WebExtractionReport(
                mode="query",
                query=clean_query,
                search_provider=self.search_provider.provider_name,
                status=ExtractionStatus.FAILED,
                extracted_at=extracted_at,
                errors=(
                    WebIssue(
                        code="SEARCH_PROVIDER_ERROR",
                        message=redact_secrets(
                            f"El proveedor de búsqueda '{self.search_provider.provider_name}' falló: {exc}"
                        ),
                        source_url="",
                        severity="error",
                    ),
                ),
            )

        if not search_resp.available:
            return WebExtractionReport(
                mode="query",
                query=clean_query,
                search_provider=search_resp.provider_name,
                search_results=(),
                sources=(),
                status=ExtractionStatus.FAILED,
                extracted_at=extracted_at,
                errors=(
                    WebIssue(
                        code=search_resp.error_code or "SEARCH_PROVIDER_UNAVAILABLE",
                        message=redact_secrets(
                            search_resp.message
                            or "No hay un proveedor de búsqueda disponible para resolver consultas."
                        ),
                        source_url="",
                        severity="error",
                    ),
                ),
                warnings=tuple(warnings),
            )

        search_results = tuple(search_resp.results[:effective_limit])
        if not search_results:
            return WebExtractionReport(
                mode="query",
                query=clean_query,
                search_provider=search_resp.provider_name,
                search_results=(),
                sources=(),
                status=ExtractionStatus.PARTIAL,
                extracted_at=extracted_at,
                warnings=tuple(warnings)
                + (
                    WebIssue(
                        code="SEARCH_EMPTY_RESULTS",
                        message=redact_secrets(
                            search_resp.message
                            or f"El proveedor '{search_resp.provider_name}' no devolvió URLs para '{clean_query}'."
                        ),
                        source_url="",
                        severity="warning",
                    ),
                ),
            )

        batch_report = self.extract_urls([r.url for r in search_results], max_pages=effective_limit)
        return WebExtractionReport(
            mode="query",
            sources=batch_report.sources,
            status=batch_report.status,
            query=clean_query,
            search_provider=search_resp.provider_name,
            search_results=search_results,
            extracted_at=batch_report.extracted_at,
            errors=batch_report.errors,
            warnings=tuple(warnings) + batch_report.warnings,
        )

    def extract(
        self,
        *,
        url: str | None = None,
        urls: Iterable[str] | None = None,
        query: str | None = None,
        max_results: int | None = None,
    ) -> WebExtractionReport:
        """
        Punto de entrada unificado y estructurado para una URL, lista de URLs o consulta.
        """
        provided = [
            name
            for name, val in (("url", url), ("urls", urls), ("query", query))
            if val is not None
        ]
        if len(provided) == 0:
            return WebExtractionReport(
                mode="url",
                status=ExtractionStatus.FAILED,
                errors=(
                    WebIssue(
                        code="MISSING_INPUT",
                        message="Debe indicar exactamente uno de: 'url', 'urls' o 'query'.",
                        source_url="",
                        severity="error",
                    ),
                ),
            )
        if len(provided) > 1:
            return WebExtractionReport(
                mode="url",
                status=ExtractionStatus.FAILED,
                errors=(
                    WebIssue(
                        code="AMBIGUOUS_INPUT",
                        message=(
                            "Indique únicamente un modo de entrada por llamada ('url', 'urls' o 'query'; "
                            f"recibidos: {', '.join(provided)})."
                        ),
                        source_url="",
                        severity="error",
                    ),
                ),
            )

        if url is not None:
            if not isinstance(url, str) or not url.strip():
                return WebExtractionReport(
                    mode="url",
                    status=ExtractionStatus.FAILED,
                    errors=(
                        WebIssue(
                            code="INVALID_URL_INPUT",
                            message="El parámetro 'url' debe ser una cadena no vacía.",
                            source_url="",
                            severity="error",
                        ),
                    ),
                )
            doc = self.extract_url(url)
            return WebExtractionReport(
                mode="url",
                sources=(doc,),
                status=doc.status,
                extracted_at=doc.metadata.extracted_at,
                errors=doc.errors,
                warnings=doc.warnings,
            )

        if urls is not None:
            return self.extract_urls(urls, max_pages=max_results)

        assert query is not None
        return self.extract_query(query, max_results=max_results)

    def execute(self, params: dict[str, Any] | None = None, **kwargs: Any) -> str:
        """
        Ejecución compatible con `SkillManager` y `IntentRouter`.

        Parámetros admitidos:
        - `url` (str): URL individual a extraer.
        - `urls` (list[str]): colección de URLs a extraer.
        - `query` (str): consulta de investigación (requiere `SearchProvider` disponible).
        - `output_format` (str, opcional): `'summary'` (predeterminado), `'json'` o `'markdown'`.
        - `max_results` / `max_pages` (int, opcional): tope de páginas a extraer.
        """
        if params is not None and not isinstance(params, dict):
            return "Error de parámetros en WebExtractionSkill: 'params' debe ser un diccionario."

        payload: dict[str, Any] = dict(params or {})
        payload.update(kwargs)

        raw_fmt = payload.get("output_format", "summary")
        if not isinstance(raw_fmt, str) or raw_fmt.strip().lower() not in ("summary", "json", "markdown"):
            return (
                "Error de formato de salida en WebExtractionSkill: "
                "'output_format' debe ser 'summary', 'json' o 'markdown'."
            )
        output_format = raw_fmt.strip().lower()

        raw_limit = payload.get("max_results", payload.get("max_pages"))
        report = self.extract(
            url=payload.get("url"),
            urls=payload.get("urls"),
            query=payload.get("query"),
            max_results=raw_limit,
        )

        if output_format == "json":
            return json.dumps(report.to_dict(), ensure_ascii=False)
        if output_format == "markdown":
            # Carga perezosa para no acoplar la skill ni importar renderizadores salvo pedido expreso.
            from skills.web_document_converter import WebDocumentConverter

            return WebDocumentConverter().to_markdown(report)
        return _format_human_summary(report)
