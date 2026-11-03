"""
Módulo de Modelos Documentales Intermedios (IR) para J.A.R.V.I.S.
==================================================================
Define la representación intermedia (Intermediate Representation o IR)
agnóstica al formato final (Markdown, Word DOCX, HTML, etc.).

Principios de diseño:
1. Inmutabilidad profunda: todas las clases son dataclasses congeladas (frozen=True).
2. Colecciones inmutables: listas internas se normalizan a tuplas en __post_init__.
3. Validaciones de integridad estrictas en tiempo de construcción.
4. Desacoplamiento total de proveedores de IA o bibliotecas externas de renderizado.
"""

from abc import ABC
from dataclasses import dataclass, field
from typing import Any, Sequence


# Conjunto cerrado de tipos permitidos para bloques de aviso (Callouts)
ALLOWED_CALLOUT_TYPES: frozenset[str] = frozenset({"NOTE", "WARNING", "TIP", "IMPORTANT"})


@dataclass(frozen=True)
class DocElement(ABC):
    """
    Clase base abstracta e inmutable para cualquier nodo o elemento del documento.
    Todos los elementos concretos heredan de esta estructura.
    """
    pass


@dataclass(frozen=True)
class Heading(DocElement):
    """
    Representa un encabezado jerárquico (H1 hasta H6).

    Atributos:
        level (int): Nivel de profundidad del encabezado (1 a 6).
        text (str): Texto visible del encabezado.
    """
    level: int
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.level, int) or self.level < 1 or self.level > 6:
            raise ValueError(f"El nivel de encabezado debe estar entre 1 y 6 (recibido: {self.level}).")
        clean_text = str(self.text or "").strip()
        if not clean_text:
            raise ValueError("El texto del encabezado no puede estar vacío.")
        object.__setattr__(self, "text", clean_text)


@dataclass(frozen=True)
class Paragraph(DocElement):
    """
    Representa un párrafo de texto estándar con estilos opcionales.

    Atributos:
        text (str): Contenido textual del párrafo.
        bold (bool): Si todo el párrafo tiene énfasis en negrita.
        italic (bool): Si todo el párrafo tiene énfasis en cursiva.
    """
    text: str
    bold: bool = False
    italic: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", str(self.text or ""))
        object.__setattr__(self, "bold", bool(self.bold))
        object.__setattr__(self, "italic", bool(self.italic))


@dataclass(frozen=True)
class ListItem(DocElement):
    """
    Representa un elemento individual dentro de una lista.

    Atributos:
        text (str): Texto del ítem.
        level (int): Nivel de anidamiento indentado (0 para raíz, >= 0).
    """
    text: str
    level: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.level, int) or self.level < 0:
            raise ValueError(f"El nivel de anidamiento de la lista debe ser >= 0 (recibido: {self.level}).")
        clean_text = str(self.text or "").strip()
        if not clean_text:
            raise ValueError("El ítem de lista no puede estar vacío.")
        object.__setattr__(self, "text", clean_text)
        object.__setattr__(self, "level", int(self.level))


@dataclass(frozen=True)
class ListBlock(DocElement):
    """
    Representa un bloque de lista completo (ordenado o con viñetas).

    Atributos:
        ordered (bool): True si es una lista numerada (1., 2.), False si es con viñetas (-).
        items (tuple[ListItem, ...]): Secuencia inmutable de elementos de la lista.
    """
    ordered: bool
    items: tuple[ListItem, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        raw_items = self.items
        if isinstance(raw_items, (list, tuple)):
            normalized_items: list[ListItem] = []
            for item in raw_items:
                if isinstance(item, ListItem):
                    normalized_items.append(item)
                elif isinstance(item, str):
                    normalized_items.append(ListItem(text=item, level=0))
                else:
                    raise TypeError(f"Elemento de lista inválido: {type(item)}. Se esperaba ListItem o str.")
            object.__setattr__(self, "items", tuple(normalized_items))
        else:
            raise TypeError("items debe ser una lista o tupla de ListItem.")
        object.__setattr__(self, "ordered", bool(self.ordered))


@dataclass(frozen=True)
class Table(DocElement):
    """
    Representa una tabla tabular con encabezados y filas inmutables.

    Atributos:
        headers (tuple[str, ...]): Títulos de cada columna.
        rows (tuple[tuple[str, ...], ...]): Filas con celdas de datos.
        caption (str | None): Leyenda o título descriptivo opcional para la tabla.
    """
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    caption: str | None = None

    def __post_init__(self) -> None:
        raw_headers = tuple(str(h).strip() for h in (self.headers or ()))
        if not raw_headers:
            raise ValueError("La tabla debe contener al menos un encabezado de columna.")
        object.__setattr__(self, "headers", raw_headers)

        num_cols = len(raw_headers)
        normalized_rows: list[tuple[str, ...]] = []
        for i, row in enumerate(self.rows or ()):
            row_tuple = tuple(str(c).strip() for c in row)
            if len(row_tuple) != num_cols:
                raise ValueError(
                    f"Fila {i + 1} inválida: contiene {len(row_tuple)} celdas pero los encabezados definen {num_cols}."
                )
            normalized_rows.append(row_tuple)
        object.__setattr__(self, "rows", tuple(normalized_rows))

        clean_caption = str(self.caption).strip() if self.caption else None
        object.__setattr__(self, "caption", clean_caption)


@dataclass(frozen=True)
class CodeBlock(DocElement):
    """
    Representa un bloque de código fuente formateado.

    Atributos:
        code (str): Código fuente literal preservando saltos de línea e indentación.
        language (str | None): Identificador de sintaxis (ej. 'python', 'json', 'bash').
    """
    code: str
    language: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", str(self.code or ""))
        clean_lang = str(self.language).strip().lower() if self.language else None
        object.__setattr__(self, "language", clean_lang)


@dataclass(frozen=True)
class Callout(DocElement):
    """
    Representa una caja de llamada o aviso destacado (admonition).

    Atributos:
        callout_type (str): Tipo de aviso, estrictamente 'NOTE', 'WARNING', 'TIP' o 'IMPORTANT'.
        content (str): Texto o mensaje explicativo del aviso.
        title (str | None): Título opcional del recuadro.
    """
    callout_type: str
    content: str
    title: str | None = None

    def __post_init__(self) -> None:
        norm_type = str(self.callout_type or "").strip().upper()
        if norm_type not in ALLOWED_CALLOUT_TYPES:
            raise ValueError(
                f"Tipo de callout '{self.callout_type}' inválido. Permitidos: {sorted(ALLOWED_CALLOUT_TYPES)}"
            )
        object.__setattr__(self, "callout_type", norm_type)

        clean_content = str(self.content or "").strip()
        if not clean_content:
            raise ValueError("El contenido del callout no puede estar vacío.")
        object.__setattr__(self, "content", clean_content)

        clean_title = str(self.title).strip() if self.title else None
        object.__setattr__(self, "title", clean_title)


@dataclass(frozen=True)
class Citation(DocElement):
    """
    Representa una cita formal o referencia bibliográfica.

    Atributos:
        source_id (str): Identificador único o etiqueta de referencia (ej. '[1]', 'REF-01').
        text (str): Texto de la cita o descripción bibliográfica de la fuente.
        url (str | None): Enlace URL opcional verificado de la fuente.
    """
    source_id: str
    text: str
    url: str | None = None

    def __post_init__(self) -> None:
        clean_id = str(self.source_id or "").strip()
        if not clean_id:
            raise ValueError("El source_id de la cita no puede estar vacío.")
        object.__setattr__(self, "source_id", clean_id)

        clean_text = str(self.text or "").strip()
        if not clean_text:
            raise ValueError("El texto de la cita no puede estar vacío.")
        object.__setattr__(self, "text", clean_text)

        clean_url = str(self.url).strip() if self.url else None
        object.__setattr__(self, "url", clean_url)


@dataclass(frozen=True)
class Document:
    """
    Representa un documento completo e inmutable en el Modelo Documental Intermedio (IR).

    Atributos:
        title (str): Título principal del documento.
        elements (tuple[DocElement, ...]): Secuencia inmutable de elementos ordenados.
        author (str | None): Nombre del autor o entidad creadora.
        metadata (tuple[tuple[str, str], ...]): Metadatos clave-valor inmutables y ordenados.
    """
    title: str
    elements: tuple[DocElement, ...] = field(default_factory=tuple)
    author: str | None = None
    metadata: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        clean_title = str(self.title or "").strip()
        if not clean_title:
            raise ValueError("El título del documento no puede estar vacío.")
        object.__setattr__(self, "title", clean_title)

        # Normalización profunda de elementos a tupla inmutable
        raw_elements = self.elements
        if isinstance(raw_elements, (list, tuple)):
            for i, elem in enumerate(raw_elements):
                if not isinstance(elem, DocElement):
                    raise TypeError(f"El elemento en índice {i} no es un DocElement válido: {type(elem)}")
            object.__setattr__(self, "elements", tuple(raw_elements))
        else:
            raise TypeError("elements debe ser una secuencia (lista o tupla) de DocElement.")

        clean_author = str(self.author).strip() if self.author else None
        object.__setattr__(self, "author", clean_author)

        # Normalización profunda de metadata a tupla inmutable de pares (clave, valor)
        raw_meta = self.metadata
        if isinstance(raw_meta, dict):
            normalized_meta = tuple(sorted((str(k), str(v)) for k, v in raw_meta.items()))
        elif isinstance(raw_meta, (list, tuple)):
            normalized_meta = tuple(sorted((str(k), str(v)) for k, v in raw_meta))
        else:
            normalized_meta = ()
        object.__setattr__(self, "metadata", normalized_meta)

    @property
    def metadata_dict(self) -> dict[str, str]:
        """Retorna una copia en diccionario de los metadatos para lectura cómoda."""
        return dict(self.metadata)
