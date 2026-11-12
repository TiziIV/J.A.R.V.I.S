"""
Modelos tipados e inmutables para el motor de extracción web de J.A.R.V.I.S.

Principios de diseño:
1. Independencia total del cliente HTTP, del parser HTML y de los motores de IA
   (Ollama, Gemini, Claude, GPT o Codex).
2. Inmutabilidad (`frozen=True`) y normalización de colecciones a tuplas en `__post_init__`.
3. Trazabilidad estricta: cada encabezado, párrafo, lista, sección, enlace e incidencia
   conserva la referencia explícita (`source_url`) a su fuente de origen.
4. Honestidad de metadatos: `published_at` permanece `None` si la página no lo declara y
   nunca se confunde con `extracted_at`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now_iso() -> str:
    """Retorna la marca temporal UTC actual en formato ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


class ExtractionStatus(str, Enum):
    """Estados posibles de extracción de una fuente o lote de investigación."""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class WebIssue:
    """Representa un error o advertencia trazable asociado a una fuente web."""

    code: str
    message: str
    source_url: str
    severity: str = "error"  # 'error' o 'warning'

    def __post_init__(self) -> None:
        sev = str(self.severity or "error").strip().lower()
        if sev not in ("error", "warning"):
            raise ValueError(f"Severidad inválida en WebIssue: {self.severity!r}")
        object.__setattr__(self, "code", str(self.code or "UNKNOWN").strip())
        object.__setattr__(self, "message", str(self.message or "").strip())
        object.__setattr__(self, "source_url", str(self.source_url or "").strip())
        object.__setattr__(self, "severity", sev)


@dataclass(frozen=True)
class WebHeading:
    """Encabezado HTML extraído (H1-H6) con referencia a su fuente."""

    level: int
    text: str
    source_url: str

    def __post_init__(self) -> None:
        if isinstance(self.level, bool) or not isinstance(self.level, int) or not (1 <= self.level <= 6):
            raise ValueError(f"Nivel de encabezado inválido: {self.level!r}")
        clean_text = str(self.text or "").strip()
        if not clean_text:
            raise ValueError("El texto del encabezado web no puede estar vacío.")
        object.__setattr__(self, "text", clean_text)
        object.__setattr__(self, "source_url", str(self.source_url or "").strip())


@dataclass(frozen=True)
class WebParagraph:
    """Párrafo normalizado con trazabilidad de fuente y sección contenedora."""

    text: str
    source_url: str
    section_heading: str | None = None
    index: int = 0

    def __post_init__(self) -> None:
        clean_text = str(self.text or "").strip()
        if not clean_text:
            raise ValueError("El texto del párrafo web no puede estar vacío.")
        object.__setattr__(self, "text", clean_text)
        object.__setattr__(self, "source_url", str(self.source_url or "").strip())
        sec = str(self.section_heading).strip() if self.section_heading else None
        object.__setattr__(self, "section_heading", sec or None)
        object.__setattr__(self, "index", int(self.index))


@dataclass(frozen=True)
class WebList:
    """Lista ordenada o desordenada extraída manteniendo su procedencia."""

    ordered: bool
    items: tuple[str, ...]
    source_url: str
    section_heading: str | None = None
    index: int = 0

    def __post_init__(self) -> None:
        normalized_items = tuple(str(item).strip() for item in (self.items or ()) if str(item).strip())
        if not normalized_items:
            raise ValueError("WebList debe contener al menos un elemento no vacío.")
        object.__setattr__(self, "ordered", bool(self.ordered))
        object.__setattr__(self, "items", normalized_items)
        object.__setattr__(self, "source_url", str(self.source_url or "").strip())
        sec = str(self.section_heading).strip() if self.section_heading else None
        object.__setattr__(self, "section_heading", sec or None)
        object.__setattr__(self, "index", int(self.index))


@dataclass(frozen=True)
class WebSection:
    """
    Agrupación jerárquica ordenada de contenido bajo un encabezado (o bloque inicial sin encabezado).
    Conserva el orden relativo de los párrafos y listas dentro de la sección.
    """

    heading: WebHeading | None
    level: int
    paragraphs: tuple[WebParagraph, ...] = field(default_factory=tuple)
    lists: tuple[WebList, ...] = field(default_factory=tuple)
    source_url: str = ""
    order_index: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "level", int(self.level))
        object.__setattr__(self, "paragraphs", tuple(self.paragraphs or ()))
        object.__setattr__(self, "lists", tuple(self.lists or ()))
        object.__setattr__(self, "source_url", str(self.source_url or "").strip())
        object.__setattr__(self, "order_index", int(self.order_index))


@dataclass(frozen=True)
class WebLink:
    """Enlace relevante normalizado y trazado hacia la página donde apareció."""

    url: str
    text: str
    source_url: str
    is_external: bool = False

    def __post_init__(self) -> None:
        clean_url = str(self.url or "").strip()
        if not clean_url:
            raise ValueError("WebLink.url no puede estar vacío.")
        object.__setattr__(self, "url", clean_url)
        object.__setattr__(self, "text", str(self.text or "").strip())
        object.__setattr__(self, "source_url", str(self.source_url or "").strip())
        object.__setattr__(self, "is_external", bool(self.is_external))


@dataclass(frozen=True)
class WebMetadata:
    """
    Metadatos de página observados durante la extracción.
    Los campos ausentes permanecen estrictamente en `None`.
    """

    title: str | None = None
    description: str | None = None
    author: str | None = None
    published_at: str | None = None
    extracted_at: str = field(default_factory=utc_now_iso)
    language: str | None = None
    canonical_url: str | None = None
    site_name: str | None = None
    content_type: str | None = None

    def __post_init__(self) -> None:
        for attr in ("title", "description", "author", "published_at", "language", "canonical_url", "site_name", "content_type"):
            val = getattr(self, attr)
            cleaned = str(val).strip() if val is not None else ""
            object.__setattr__(self, attr, cleaned or None)
        ext = str(self.extracted_at or "").strip() or utc_now_iso()
        object.__setattr__(self, "extracted_at", ext)


@dataclass(frozen=True)
class WebSourceDocument:
    """
    Resultado estructurado, normalizado y trazable de una fuente web individual.
    """

    original_url: str
    final_url: str
    status: ExtractionStatus
    http_status: int | None = None
    metadata: WebMetadata = field(default_factory=WebMetadata)
    headings: tuple[WebHeading, ...] = field(default_factory=tuple)
    paragraphs: tuple[WebParagraph, ...] = field(default_factory=tuple)
    lists: tuple[WebList, ...] = field(default_factory=tuple)
    sections: tuple[WebSection, ...] = field(default_factory=tuple)
    links: tuple[WebLink, ...] = field(default_factory=tuple)
    normalized_text: str = ""
    redirect_chain: tuple[str, ...] = field(default_factory=tuple)
    bytes_received: int = 0
    bytes_decompressed: int = 0
    truncated: bool = False
    errors: tuple[WebIssue, ...] = field(default_factory=tuple)
    warnings: tuple[WebIssue, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "original_url", str(self.original_url or "").strip())
        object.__setattr__(self, "final_url", str(self.final_url or self.original_url).strip())
        if not isinstance(self.status, ExtractionStatus):
            object.__setattr__(self, "status", ExtractionStatus(str(self.status)))
        object.__setattr__(self, "headings", tuple(self.headings or ()))
        object.__setattr__(self, "paragraphs", tuple(self.paragraphs or ()))
        object.__setattr__(self, "lists", tuple(self.lists or ()))
        object.__setattr__(self, "sections", tuple(self.sections or ()))
        object.__setattr__(self, "links", tuple(self.links or ()))
        object.__setattr__(self, "normalized_text", str(self.normalized_text or ""))
        object.__setattr__(self, "redirect_chain", tuple(str(u) for u in (self.redirect_chain or ())))
        object.__setattr__(self, "bytes_received", int(self.bytes_received))
        object.__setattr__(self, "bytes_decompressed", int(self.bytes_decompressed))
        object.__setattr__(self, "truncated", bool(self.truncated))
        object.__setattr__(self, "errors", tuple(self.errors or ()))
        object.__setattr__(self, "warnings", tuple(self.warnings or ()))

    @property
    def title(self) -> str | None:
        return self.metadata.title

    @property
    def description(self) -> str | None:
        return self.metadata.description

    @property
    def author(self) -> str | None:
        return self.metadata.author

    @property
    def published_at(self) -> str | None:
        return self.metadata.published_at

    @property
    def extracted_at(self) -> str:
        return self.metadata.extracted_at

    @property
    def language(self) -> str | None:
        return self.metadata.language

    @property
    def ok(self) -> bool:
        return self.status in (ExtractionStatus.SUCCESS, ExtractionStatus.PARTIAL)

    def to_dict(self) -> dict[str, Any]:
        """Serializa el resultado a tipos primitivos JSON-compatibles."""
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass(frozen=True)
class SearchResult:
    """Resultado normalizado proveniente de un proveedor de búsqueda desacoplado."""

    title: str
    url: str
    description: str | None = None
    provider: str = ""
    rank: int = 1

    def __post_init__(self) -> None:
        clean_url = str(self.url or "").strip()
        if not clean_url:
            raise ValueError("SearchResult.url no puede estar vacío.")
        object.__setattr__(self, "title", str(self.title or clean_url).strip())
        object.__setattr__(self, "url", clean_url)
        desc = str(self.description).strip() if self.description is not None else ""
        object.__setattr__(self, "description", desc or None)
        object.__setattr__(self, "provider", str(self.provider or "").strip())
        object.__setattr__(self, "rank", int(self.rank))


@dataclass(frozen=True)
class WebExtractionReport:
    """
    Resultado consolidado de una operación de `WebExtractionSkill`
    (URL individual, colección de URLs o consulta de investigación).
    """

    mode: str  # 'url', 'urls', 'query'
    sources: tuple[WebSourceDocument, ...] = field(default_factory=tuple)
    status: ExtractionStatus = ExtractionStatus.FAILED
    query: str | None = None
    search_provider: str | None = None
    search_results: tuple[SearchResult, ...] = field(default_factory=tuple)
    extracted_at: str = field(default_factory=utc_now_iso)
    errors: tuple[WebIssue, ...] = field(default_factory=tuple)
    warnings: tuple[WebIssue, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        mode_clean = str(self.mode or "url").strip().lower()
        if mode_clean not in ("url", "urls", "query"):
            raise ValueError(f"Modo inválido en WebExtractionReport: {self.mode!r}")
        object.__setattr__(self, "mode", mode_clean)
        object.__setattr__(self, "sources", tuple(self.sources or ()))
        if not isinstance(self.status, ExtractionStatus):
            object.__setattr__(self, "status", ExtractionStatus(str(self.status)))
        q = str(self.query).strip() if self.query is not None else ""
        object.__setattr__(self, "query", q or None)
        sp = str(self.search_provider).strip() if self.search_provider is not None else ""
        object.__setattr__(self, "search_provider", sp or None)
        object.__setattr__(self, "search_results", tuple(self.search_results or ()))
        ext = str(self.extracted_at or "").strip() or utc_now_iso()
        object.__setattr__(self, "extracted_at", ext)
        object.__setattr__(self, "errors", tuple(self.errors or ()))
        object.__setattr__(self, "warnings", tuple(self.warnings or ()))

    @property
    def successful_sources(self) -> tuple[WebSourceDocument, ...]:
        return tuple(s for s in self.sources if s.ok)

    @property
    def failed_sources(self) -> tuple[WebSourceDocument, ...]:
        return tuple(s for s in self.sources if not s.ok)

    def to_dict(self) -> dict[str, Any]:
        """Serializa el reporte completo para consumo agnóstico por cualquier cerebro IA."""
        return {
            "mode": self.mode,
            "status": self.status.value,
            "query": self.query,
            "search_provider": self.search_provider,
            "extracted_at": self.extracted_at,
            "search_results": [asdict(r) for r in self.search_results],
            "sources": [s.to_dict() for s in self.sources],
            "errors": [asdict(e) for e in self.errors],
            "warnings": [asdict(w) for w in self.warnings],
        }
