"""
Interfaz desacoplada de proveedores de búsqueda para `WebExtractionSkill`.

Estado verificado del repositorio en el Paso #3.B:
- La skill existente `skills.system_skills.WebSearchSkill` únicamente delega en el
  navegador web del sistema operativo (`webbrowser.open(...)`) y no retorna resultados
  programáticos (títulos, URLs ni descripciones).
- Por directriz arquitectónica, no se introduce scraping frágil sobre buscadores
  comerciales ni dependencias pagas o credenciales obligatorias.
- `UnconfiguredSearchProvider` informa con honestidad esta limitación cuando se solicita
  una búsqueda por consulta, manteniendo operativa al 100 % la extracción directa por URL
  o colección de URLs, y dejando listo un contrato limpio para conectar cualquier
  proveedor programático en fases futuras.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable

from skills.web_models import SearchResult


@dataclass(frozen=True)
class SearchResponse:
    """Respuesta normalizada de un proveedor de búsqueda desacoplado."""

    provider_name: str
    available: bool
    results: tuple[SearchResult, ...] = field(default_factory=tuple)
    message: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_name", str(self.provider_name or "unknown").strip())
        object.__setattr__(self, "available", bool(self.available))
        object.__setattr__(self, "results", tuple(self.results or ()))
        msg = str(self.message).strip() if self.message is not None else ""
        object.__setattr__(self, "message", msg or None)
        code = str(self.error_code).strip() if self.error_code is not None else ""
        object.__setattr__(self, "error_code", code or None)


def normalize_search_results(
    raw_items: Iterable[SearchResult | dict[str, Any]],
    *,
    provider_name: str,
    max_results: int = 5,
) -> tuple[SearchResult, ...]:
    """
    Normaliza y deduplica resultados heterogéneos de búsqueda en tuplas `SearchResult`.
    """
    if max_results <= 0:
        return ()

    normalized: list[SearchResult] = []
    seen_urls: set[str] = set()
    clean_provider = str(provider_name or "custom").strip()

    for item in raw_items:
        if len(normalized) >= max_results:
            break
        if isinstance(item, SearchResult):
            url = item.url
            title = item.title
            description = item.description
            item_provider = item.provider or clean_provider
        elif isinstance(item, dict):
            raw_url = item.get("url") or item.get("href") or item.get("link") or ""
            url = str(raw_url).strip()
            if not url:
                continue
            raw_title = item.get("title") or item.get("name") or url
            title = str(raw_title).strip()
            raw_desc = item.get("description") or item.get("snippet") or item.get("summary")
            description = str(raw_desc).strip() if raw_desc is not None else None
            item_provider = str(item.get("provider") or clean_provider).strip()
        else:
            continue

        dedup_key = url.strip()
        if not dedup_key or dedup_key in seen_urls:
            continue
        seen_urls.add(dedup_key)

        normalized.append(
            SearchResult(
                title=title or dedup_key,
                url=dedup_key,
                description=description or None,
                provider=item_provider,
                rank=len(normalized) + 1,
            )
        )

    return tuple(normalized)


class SearchProvider(ABC):
    """Contrato abstracto para proveedores programáticos de búsqueda web."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Identificador legible del proveedor (ej. 'searxng', 'tavily', 'unconfigured')."""

    @property
    @abstractmethod
    def is_available(self) -> bool:
        """Indica si el proveedor está configurado y listo para responder consultas."""

    @abstractmethod
    def search(self, query: str, *, max_results: int = 5) -> SearchResponse:
        """Ejecuta una búsqueda programática y devuelve URLs normalizadas sin extraer aún su HTML."""


class UnconfiguredSearchProvider(SearchProvider):
    """
    Implementación por defecto honesta cuando no existe un motor de búsqueda programático.

    No simula resultados exitosos, no abre ventanas del navegador y no realiza scraping frágil
    sobre buscadores comerciales.
    """

    @property
    def provider_name(self) -> str:
        return "unconfigured"

    @property
    def is_available(self) -> bool:
        return False

    def search(self, query: str, *, max_results: int = 5) -> SearchResponse:
        clean_query = str(query or "").strip()
        if not clean_query:
            return SearchResponse(
                provider_name=self.provider_name,
                available=False,
                results=(),
                error_code="EMPTY_QUERY",
                message="La consulta de investigación está vacía.",
            )

        return SearchResponse(
            provider_name=self.provider_name,
            available=False,
            results=(),
            error_code="SEARCH_PROVIDER_NOT_CONFIGURED",
            message=(
                "No hay un proveedor programático de búsqueda web configurado en este entorno "
                "(la skill existente 'web_search' únicamente abre el navegador del sistema sin "
                "retornar URLs estructuradas). Funcionalidad disponible: extracción directa por "
                "URL individual ('url') o colección de URLs ('urls'). Integración faltante: "
                "un adaptador `SearchProvider` externo conectado a una API o instancia de búsqueda."
            ),
        )
