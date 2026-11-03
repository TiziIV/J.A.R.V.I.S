"""
Módulo de Renderizador Markdown para J.A.R.V.I.S.
=================================================
Transforma el Modelo Documental Intermedio (Document IR) en texto Markdown
estándar, limpio y compatible con GitHub y Obsidian.

Características:
- Salida estricta en UTF-8.
- Respeta jerarquía de encabezados (H1-H6).
- Formatea párrafos con negritas y cursivas.
- Formatea listas ordenadas y desordenadas respetando niveles de anidamiento.
- Renderiza tablas estándar con bordes y alineaciones limpias.
- Soporta callouts de estilo GitHub/Obsidian (> [!NOTE], > [!WARNING], etc.).
- Preserva bloques de código con etiqueta de lenguaje.
- Incluye citas y referencias bibliográficas al pie o en sección final.
"""

from skills.document_models import (
    Callout,
    Citation,
    CodeBlock,
    DocElement,
    Document,
    Heading,
    ListBlock,
    ListItem,
    Paragraph,
    Table,
)


class MarkdownRenderer:
    """
    Renderizador de Document IR hacia sintaxis Markdown estándar UTF-8.
    """

    def render(self, document: Document) -> str:
        """
        Convierte una instancia de Document en una cadena Markdown.

        Args:
            document (Document): Documento estructurado IR.

        Returns:
            str: Contenido formateado en Markdown.
        """
        if not isinstance(document, Document):
            raise TypeError("El objeto a renderizar debe ser una instancia de Document.")

        lines: list[str] = []

        # Verificar si el primer elemento ya es un H1 que coincide con el título
        has_title_heading = False
        if document.elements and isinstance(document.elements[0], Heading) and document.elements[0].level == 1:
            has_title_heading = True

        if not has_title_heading:
            lines.append(f"# {document.title}\n")

        for elem in document.elements:
            elem_md = self._render_element(elem)
            if elem_md:
                lines.append(elem_md)

        return "\n\n".join(line.strip() for line in lines if line.strip()) + "\n"

    def _render_element(self, element: DocElement) -> str:
        """Renderiza un elemento individual a su representación Markdown."""
        if isinstance(element, Heading):
            prefix = "#" * element.level
            return f"{prefix} {element.text}"

        elif isinstance(element, Paragraph):
            text = element.text
            if element.bold:
                return f"**{text}**"
            elif element.italic:
                return f"*{text}*"
            return text

        elif isinstance(element, ListBlock):
            items_lines: list[str] = []
            for idx, item in enumerate(element.items, start=1):
                indent = "  " * item.level
                bullet = f"{idx}." if element.ordered else "-"
                items_lines.append(f"{indent}{bullet} {item.text}")
            return "\n".join(items_lines)

        elif isinstance(element, Table):
            col_widths = [len(h) for h in element.headers]
            for row in element.rows:
                for c_idx, cell in enumerate(row):
                    if c_idx < len(col_widths):
                        col_widths[c_idx] = max(col_widths[c_idx], len(str(cell)))

            # Cabecera
            header_cells = [h.ljust(col_widths[i]) for i, h in enumerate(element.headers)]
            header_line = f"| {' | '.join(header_cells)} |"

            # Separador
            sep_cells = ["-" * max(col_widths[i], 3) for i in range(len(element.headers))]
            sep_line = f"| {' | '.join(sep_cells)} |"

            # Filas
            row_lines: list[str] = []
            for row in element.rows:
                row_cells = [str(c).ljust(col_widths[i]) for i, c in enumerate(row)]
                row_lines.append(f"| {' | '.join(row_cells)} |")

            table_parts = [header_line, sep_line] + row_lines
            if element.caption:
                table_parts.insert(0, f"*{element.caption}*")
            return "\n".join(table_parts)

        elif isinstance(element, CodeBlock):
            lang = element.language or ""
            return f"```{lang}\n{element.code}\n```"

        elif isinstance(element, Callout):
            title_part = f" {element.title}" if element.title else ""
            header = f"> [!{element.callout_type}]{title_part}"
            content_lines = [f"> {line}" if line else ">" for line in element.content.splitlines()]
            return "\n".join([header] + content_lines)

        elif isinstance(element, Citation):
            url_part = f" <{element.url}>" if element.url else ""
            return f"[^{element.source_id}]: {element.text}{url_part}"

        return ""
