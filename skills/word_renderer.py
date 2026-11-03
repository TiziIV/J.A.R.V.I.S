"""
Módulo de Renderizador Microsoft Word (.docx) para J.A.R.V.I.S.
===============================================================
Genera documentos editables en formato Microsoft Word (.docx) a partir
del Modelo Documental Intermedio (Document IR).

Reglas arquitectónicas críticas:
- Carga diferida (lazy import): `python-docx` NO se importa en el inicio del módulo
  ni en el arranque del sistema. Solo se importa al momento de renderizar.
- Si `python-docx` no está disponible, lanza una excepción clara sin afectar el arranque
  ni la exportación a Markdown.
- Estilos aplicados:
  - Encabezados jerárquicos y título principal.
  - Tablas con bordes limpios ('Table Grid') y encabezados destacados.
  - Bloques de código con tipografía monoespaciada (Consolas) y sombreado.
  - Recuadros visuales (Callouts) diferenciados según tipo (NOTE, WARNING, TIP, IMPORTANT).
  - Listas con sangría y viñetas / numeración.
  - Citas y notas bibliográficas.
"""

from pathlib import Path
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


class WordRenderer:
    """
    Renderizador para transformar un Document IR en un archivo .docx de Word.
    """

    def __init__(self) -> None:
        """Inicializa el renderizador sin importar dependencias pesadas."""
        pass

    @staticmethod
    def is_available() -> bool:
        """
        Comprueba de forma segura y diferida si la biblioteca python-docx está instalada.
        """
        try:
            import docx  # type: ignore # noqa: F401
            return True
        except ImportError:
            return False

    def render_to_file(self, document: Document, destination_path: Path | str) -> Path:
        """
        Renderiza el Document IR y lo guarda en el archivo destino especificado.

        Args:
            document (Document): Documento estructurado IR.
            destination_path (Path | str): Ruta de guardado para el archivo .docx.

        Returns:
            Path: Ruta resuelta del archivo .docx generado.

        Raises:
            RuntimeError: Si python-docx no está instalado en el entorno.
            ValueError: Si el documento o la ruta son inválidos.
        """
        if not isinstance(document, Document):
            raise TypeError("El objeto a renderizar debe ser una instancia de Document.")

        dest = Path(destination_path)

        # Importación diferida obligatoria
        try:
            import docx  # type: ignore
            from docx.shared import Inches, Pt, RGBColor  # type: ignore
            from docx.enum.text import WD_ALIGN_PARAGRAPH  # type: ignore
            from docx.oxml import parse_xml  # type: ignore
            from docx.oxml.ns import nsdecls  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "La exportación a formato Word (.docx) requiere la dependencia 'python-docx'. "
                "Instálela en el entorno mediante: .\\venv\\Scripts\\python.exe -m pip install python-docx"
            ) from exc

        doc = docx.Document()

        # Configuración de márgenes estándar (1 pulgada = 2.54 cm)
        for section in doc.sections:
            section.top_margin = Inches(1.0)
            section.bottom_margin = Inches(1.0)
            section.left_margin = Inches(1.0)
            section.right_margin = Inches(1.0)

        # Portada / Título principal
        title_p = doc.add_paragraph()
        title_p.paragraph_format.space_before = Pt(12)
        title_p.paragraph_format.space_after = Pt(6)
        title_run = title_p.add_run(document.title)
        title_run.font.name = "Calibri"
        title_run.font.size = Pt(22)
        title_run.font.bold = True
        title_run.font.color.rgb = RGBColor(0x1F, 0x4E, 0x79)

        # Subtítulo con metadatos o autor si existen
        meta_items: list[str] = []
        if document.author:
            meta_items.append(f"Autor: {document.author}")
        meta_dict = document.metadata_dict
        if "subject" in meta_dict:
            meta_items.append(f"Materia: {meta_dict['subject']}")
        if "date" in meta_dict:
            meta_items.append(f"Fecha: {meta_dict['date']}")

        if meta_items:
            sub_p = doc.add_paragraph()
            sub_p.paragraph_format.space_after = Pt(16)
            sub_run = sub_p.add_run(" | ".join(meta_items))
            sub_run.font.name = "Calibri"
            sub_run.font.size = Pt(10)
            sub_run.font.italic = True
            sub_run.font.color.rgb = RGBColor(0x59, 0x59, 0x59)

        # Iterar sobre elementos del Document IR
        for index, elem in enumerate(document.elements):
            if (
                index == 0
                and isinstance(elem, Heading)
                and elem.level == 1
                and elem.text.strip() == document.title.strip()
            ):
                continue
            self._render_element(doc, elem)

        # Guardar en disco
        dest.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(dest))
        return dest

    def _render_element(self, doc: Any, element: DocElement) -> None:
        """Renderiza un DocElement agregándolo al documento Word de python-docx."""
        from docx.shared import Inches, Pt, RGBColor  # type: ignore
        from docx.oxml import parse_xml  # type: ignore
        from docx.oxml.ns import nsdecls  # type: ignore

        if isinstance(element, Heading):
            # En docx, level va de 1 a 6
            h = doc.add_heading(element.text, level=min(element.level, 6))
            h.paragraph_format.space_before = Pt(10)
            h.paragraph_format.space_after = Pt(4)

        elif isinstance(element, Paragraph):
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(6)
            p.paragraph_format.line_spacing = 1.15
            run = p.add_run(element.text)
            run.font.name = "Calibri"
            run.font.size = Pt(11)
            if element.bold:
                run.bold = True
            if element.italic:
                run.italic = True

        elif isinstance(element, ListBlock):
            style_name = "List Number" if element.ordered else "List Bullet"
            for item in element.items:
                lp = doc.add_paragraph(style=style_name)
                lp.paragraph_format.space_after = Pt(3)
                lp.paragraph_format.line_spacing = 1.15
                if item.level > 0:
                    lp.paragraph_format.left_indent = Inches(0.25 * (item.level + 1))
                run = lp.add_run(item.text)
                run.font.name = "Calibri"
                run.font.size = Pt(11)

        elif isinstance(element, Table):
            num_rows = len(element.rows) + 1
            num_cols = len(element.headers)
            tbl = doc.add_table(rows=num_rows, cols=num_cols, style="Table Grid")
            tbl.autofit = True

            # Formatear encabezados de tabla
            header_row = tbl.rows[0]
            header_bg = parse_xml(f'<w:shd {nsdecls("w")} w:fill="1F4E79"/>')
            for col_idx, header_text in enumerate(element.headers):
                cell = header_row.cells[col_idx]
                cell.text = header_text
                # Fondo azul oscuro y texto blanco en negrita
                try:
                    cell_bg = parse_xml(f'<w:shd {nsdecls("w")} w:fill="1F4E79"/>')
                    cell._tc.get_or_add_tcPr().append(cell_bg)
                except Exception:
                    pass
                for p in cell.paragraphs:
                    p.paragraph_format.space_before = Pt(4)
                    p.paragraph_format.space_after = Pt(4)
                    for r in p.runs:
                        r.font.name = "Calibri"
                        r.font.size = Pt(10)
                        r.bold = True
                        r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

            # Filas de datos
            for r_idx, row_data in enumerate(element.rows, start=1):
                data_row = tbl.rows[r_idx]
                # Alternar sombreado tenue en filas pares
                bg_color = "F2F2F2" if r_idx % 2 == 0 else "FFFFFF"
                for c_idx, cell_value in enumerate(row_data):
                    cell = data_row.cells[c_idx]
                    cell.text = str(cell_value)
                    try:
                        shd = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{bg_color}"/>')
                        cell._tc.get_or_add_tcPr().append(shd)
                    except Exception:
                        pass
                    for p in cell.paragraphs:
                        p.paragraph_format.space_before = Pt(3)
                        p.paragraph_format.space_after = Pt(3)
                        for r in p.runs:
                            r.font.name = "Calibri"
                            r.font.size = Pt(10)

            doc.add_paragraph().paragraph_format.space_after = Pt(6)

        elif isinstance(element, CodeBlock):
            # Recuadro con fondo gris suave y tipografía monoespaciada
            tbl = doc.add_table(rows=1, cols=1, style="Table Grid")
            cell = tbl.cell(0, 0)
            try:
                shd = parse_xml(f'<w:shd {nsdecls("w")} w:fill="F4F4F4"/>')
                cell._tc.get_or_add_tcPr().append(shd)
            except Exception:
                pass
            p = cell.paragraphs[0]
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after = Pt(4)
            run = p.add_run(element.code)
            run.font.name = "Consolas"
            run.font.size = Pt(9.5)
            run.font.color.rgb = RGBColor(0x24, 0x29, 0x2E)
            doc.add_paragraph().paragraph_format.space_after = Pt(6)

        elif isinstance(element, Callout):
            # Tabla 1x1 con color temático según tipo
            colors = {
                "NOTE": ("EBF3FB", RGBColor(0x1F, 0x4E, 0x79)),
                "TIP": ("E8F5E9", RGBColor(0x2E, 0x7D, 0x32)),
                "WARNING": ("FFF4E5", RGBColor(0xD9, 0x77, 0x06)),
                "IMPORTANT": ("FFEBEE", RGBColor(0xC6, 0x28, 0x28)),
            }
            bg_hex, text_color = colors.get(element.callout_type, ("F2F2F2", RGBColor(0x33, 0x33, 0x33)))

            tbl = doc.add_table(rows=1, cols=1, style="Table Grid")
            cell = tbl.cell(0, 0)
            try:
                shd = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{bg_hex}"/>')
                cell._tc.get_or_add_tcPr().append(shd)
            except Exception:
                pass

            p = cell.paragraphs[0]
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after = Pt(2)
            title_text = f"[{element.callout_type}] {element.title or ''}".strip()
            r_title = p.add_run(f"{title_text}\n")
            r_title.bold = True
            r_title.font.name = "Calibri"
            r_title.font.size = Pt(10.5)
            r_title.font.color.rgb = text_color

            r_content = p.add_run(element.content)
            r_content.font.name = "Calibri"
            r_content.font.size = Pt(10)
            doc.add_paragraph().paragraph_format.space_after = Pt(6)

        elif isinstance(element, Citation):
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(0.3)
            p.paragraph_format.space_after = Pt(3)
            r_id = p.add_run(f"[{element.source_id}] ")
            r_id.bold = True
            r_id.font.name = "Calibri"
            r_id.font.size = Pt(9.5)
            r_id.font.color.rgb = RGBColor(0x59, 0x59, 0x59)

            r_text = p.add_run(element.text)
            r_text.font.name = "Calibri"
            r_text.font.size = Pt(9.5)
            r_text.font.color.rgb = RGBColor(0x33, 0x33, 0x33)

            if element.url:
                r_url = p.add_run(f" ({element.url})")
                r_url.font.name = "Calibri"
                r_url.font.size = Pt(9.5)
                r_url.font.color.rgb = RGBColor(0x05, 0x63, 0xC1)
                r_url.font.underline = True
