"""
Módulo de Parser Documental Estructurado para J.A.R.V.I.S.
=========================================================
Convierte texto estructurado (Markdown estándar, respuestas de LLM y plantillas)
en un objeto inmutable `Document` perteneciente al Modelo Documental Intermedio (IR).

Características:
- No depende de bibliotecas externas pesadas ni de proveedores cloud (Gemini, Ollama, etc.).
- Preserva Unicode íntegro (tildes, eñes, símbolos matemáticos, emojis).
- Reconoce encabezados (H1-H6), listas anidadas, tablas con separadores, bloques de código,
  callouts GitHub/Obsidian ([!NOTE], [!WARNING], etc.), citas y referencias bibliográficas.
- Valida límites de tamaño y consistencia estructural sin pérdida silenciosa de datos.
"""

import re
from typing import Any
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

# Límite predeterminado seguro de tamaño de texto de entrada: 5 MB
DEFAULT_MAX_CONTENT_CHARS: int = 5 * 1024 * 1024

# Patrones de expresiones regulares compilados para rendimiento
RE_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
RE_CALLOUT_START = re.compile(r"^>\s*\[!([A-Za-z]+)\](?:\s+(.*))?$")
RE_BLOCKQUOTE = re.compile(r"^>\s*(.*)$")
RE_UNORDERED_LIST = re.compile(r"^(\s*)[*\-+]\s+(.+)$")
RE_ORDERED_LIST = re.compile(r"^(\s*)\d+\.\s+(.+)$")
RE_CODE_FENCE = re.compile(r"^(?:```|~~~)([\w#+\.\-]+)?\s*$")
RE_TABLE_ROW = re.compile(r"^\|(.+)\|\s*$")
RE_TABLE_SEPARATOR = re.compile(r"^\|(\s*:?-+:?\s*\|)+\s*$")
RE_CITATION_DEF = re.compile(r"^\[\^?([^\]]+)\]:\s*(.+)$")
RE_URL = re.compile(r"https?://[^\s<>\"']+")


class DocumentParser:
    """
    Parser agnóstico y robusto para transformar Markdown en el modelo Document IR.
    """

    def __init__(self, max_length: int = DEFAULT_MAX_CONTENT_CHARS) -> None:
        """
        Inicializa el parser con límites configurables de tamaño.

        Args:
            max_length (int): Límite máximo de caracteres permitidos.
        """
        self.max_length = max_length

    def parse(
        self,
        content: str,
        title: str | None = None,
        author: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Document:
        """
        Analiza el contenido estructurado y produce un Document inmutable.

        Args:
            content (str): Texto en formato Markdown o estructurado.
            title (str, optional): Título del documento. Si es None, se infiere del contenido.
            author (str, optional): Autor del documento.
            metadata (dict, optional): Metadatos adicionales.

        Returns:
            Document: Instancia validada e inmutable del modelo IR.

        Raises:
            ValueError: Si el contenido está vacío, supera el límite o es inconsistente.
        """
        if content is None or not isinstance(content, str):
            raise ValueError("El contenido documental debe ser una cadena de texto no nula.")

        clean_content = content.strip()
        if not clean_content:
            raise ValueError("El contenido documental proporcionado está vacío.")

        if len(clean_content) > self.max_length:
            raise ValueError(
                f"El contenido excede el tamaño máximo permitido ({len(clean_content)} > {self.max_length} caracteres)."
            )

        lines = clean_content.splitlines()
        elements: list[DocElement] = []
        inferred_title: str | None = None

        i = 0
        total_lines = len(lines)

        while i < total_lines:
            line = lines[i]
            stripped = line.strip()

            # 1. Líneas vacías: separadores que se ignoran entre bloques
            if not stripped:
                i += 1
                continue

            # 2. Bloques de código delimitados por ```
            fence_match = RE_CODE_FENCE.match(stripped)
            if fence_match:
                lang = fence_match.group(1)
                code_lines: list[str] = []
                i += 1
                while i < total_lines:
                    if RE_CODE_FENCE.match(lines[i].strip()):
                        i += 1
                        break
                    code_lines.append(lines[i])
                    i += 1
                elements.append(CodeBlock(code="\n".join(code_lines), language=lang))
                continue

            # 3. Referencias bibliográficas explícitas tipo [^1]: o [REF]:
            cit_match = RE_CITATION_DEF.match(stripped)
            if cit_match:
                source_id = cit_match.group(1).strip()
                cit_text = cit_match.group(2).strip()
                url_match = RE_URL.search(cit_text)
                url_val = url_match.group(0) if url_match else None
                if url_match:
                    # Guardamos la URL una sola vez en el campo estructurado del IR.
                    cit_text = (cit_text[:url_match.start()] + cit_text[url_match.end():]).strip()
                elements.append(Citation(source_id=source_id, text=cit_text, url=url_val))
                i += 1
                continue

            # 4. Callouts y citas de bloque (Blockquotes)
            if stripped.startswith(">"):
                callout_match = RE_CALLOUT_START.match(stripped)
                if callout_match:
                    raw_type = callout_match.group(1).upper()
                    c_title = callout_match.group(2)
                    c_lines: list[str] = []
                    i += 1
                    while i < total_lines and lines[i].strip().startswith(">"):
                        bq_line = lines[i].strip()[1:].strip()
                        if bq_line:
                            c_lines.append(bq_line)
                        i += 1
                    c_content = "\n".join(c_lines) or (c_title or "Aviso")
                    # Normalizar a tipos válidos reconocidos
                    final_type = raw_type if raw_type in {"NOTE", "WARNING", "TIP", "IMPORTANT"} else "NOTE"
                    elements.append(Callout(callout_type=final_type, content=c_content, title=c_title))
                    continue
                else:
                    # Blockquote estándar: interpretado como Citation o Paragraph destacado
                    bq_lines: list[str] = []
                    while i < total_lines and lines[i].strip().startswith(">"):
                        bq_clean = lines[i].strip()[1:].strip()
                        if bq_clean:
                            bq_lines.append(bq_clean)
                        i += 1
                    quote_text = "\n".join(bq_lines)
                    url_match = RE_URL.search(quote_text)
                    url_val = url_match.group(0) if url_match else None
                    if url_match:
                        quote_text = (quote_text[:url_match.start()] + quote_text[url_match.end():]).strip()
                    # Si tiene formato de cita o referencia con autor/guion
                    elements.append(
                        Citation(
                            source_id=f"Cita-{len([e for e in elements if isinstance(e, Citation)]) + 1}",
                            text=quote_text,
                            url=url_val,
                        )
                    )
                    continue

            # 5. Tablas Markdown (| Col1 | Col2 |)
            if stripped.startswith("|") and stripped.endswith("|") and i + 1 < total_lines:
                next_stripped = lines[i + 1].strip()
                if RE_TABLE_SEPARATOR.match(next_stripped) or (next_stripped.startswith("|") and "---" in next_stripped):
                    # Fila de encabezados
                    raw_headers = [c.strip() for c in stripped[1:-1].split("|")]
                    headers = tuple(h if h else f"Columna {idx + 1}" for idx, h in enumerate(raw_headers))
                    num_cols = len(headers)
                    rows: list[tuple[str, ...]] = []
                    i += 2  # Salteamos encabezado y separador

                    while i < total_lines and lines[i].strip().startswith("|") and lines[i].strip().endswith("|"):
                        row_cells = [c.strip() for c in lines[i].strip()[1:-1].split("|")]
                        # Normalizar longitud de columnas para evitar fallos de consistencia
                        if len(row_cells) < num_cols:
                            row_cells.extend([""] * (num_cols - len(row_cells)))
                        elif len(row_cells) > num_cols:
                            row_cells = row_cells[:num_cols]
                        rows.append(tuple(row_cells))
                        i += 1

                    elements.append(Table(headers=headers, rows=tuple(rows)))
                    continue

            # 6. Encabezados (# H1 hasta ###### H6)
            h_match = RE_HEADING.match(stripped)
            if h_match:
                level = len(h_match.group(1))
                h_text = h_match.group(2).strip()
                if level == 1 and inferred_title is None:
                    inferred_title = h_text
                elements.append(Heading(level=level, text=h_text))
                i += 1
                continue

            # 7. Listas (ordenadas y desordenadas)
            is_unord = RE_UNORDERED_LIST.match(line)
            is_ord = RE_ORDERED_LIST.match(line)
            if is_unord or is_ord:
                is_ordered = bool(is_ord)
                list_items: list[ListItem] = []

                while i < total_lines:
                    cur_line = lines[i]
                    cur_unord = RE_UNORDERED_LIST.match(cur_line)
                    cur_ord = RE_ORDERED_LIST.match(cur_line)

                    # Si el tipo de lista cambia o la línea no es lista, rompemos el bloque
                    if is_ordered and not cur_ord:
                        break
                    if not is_ordered and not cur_unord:
                        break

                    matched = cur_ord if is_ordered else cur_unord
                    assert matched is not None
                    indent_spaces = len(matched.group(1).expandtabs(2))
                    nesting_level = max(0, indent_spaces // 2)
                    item_text = matched.group(2).strip()

                    list_items.append(ListItem(text=item_text, level=nesting_level))
                    i += 1

                elements.append(ListBlock(ordered=is_ordered, items=tuple(list_items)))
                continue

            # 8. Párrafos generales de texto
            # Acumulamos líneas consecutivas no vacías que no inicien ningún otro bloque especial
            para_lines: list[str] = [stripped]
            i += 1
            while i < total_lines:
                next_l = lines[i]
                next_str = next_l.strip()
                if not next_str:
                    break
                if (
                    RE_HEADING.match(next_str)
                    or RE_CODE_FENCE.match(next_str)
                    or next_str.startswith(">")
                    or (next_str.startswith("|") and next_str.endswith("|"))
                    or RE_UNORDERED_LIST.match(next_l)
                    or RE_ORDERED_LIST.match(next_l)
                    or RE_CITATION_DEF.match(next_str)
                ):
                    break
                para_lines.append(next_str)
                i += 1

            full_para_text = " ".join(para_lines).strip()
            # Solo quitamos delimitadores si la línea completa forma un énfasis válido.
            bold_match = re.fullmatch(r"\*\*(.+?)\*\*", full_para_text)
            italic_match = re.fullmatch(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)|_(?!_)(.+?)_(?!_)", full_para_text)
            if bold_match:
                elements.append(Paragraph(text=bold_match.group(1), bold=True))
            elif italic_match:
                italic_text = italic_match.group(1) or italic_match.group(2)
                elements.append(Paragraph(text=italic_text, italic=True))
            else:
                elements.append(Paragraph(text=full_para_text))

        # Determinar título final
        final_title = title.strip() if (title and title.strip()) else (inferred_title or "Documento Exportado")

        return Document(
            title=final_title,
            elements=tuple(elements),
            author=author,
            metadata=metadata or {},
        )
