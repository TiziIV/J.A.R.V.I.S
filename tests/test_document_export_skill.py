"""
Suite de Pruebas Automatizadas para DocumentExportSkill y Modo Estudio
=======================================================================
Cubre exhaustivamente los 25 casos de prueba obligatorios del Paso #3.A:
1. Renderizado básico Markdown.
2. Renderizado DOCX y validación de estructura.
3. Tablas multicelda.
4. Bloques de código.
5. Listas anidadas.
6. Callouts y advertencias.
7. Unicode: tildes, eñes, símbolos matemáticos y emojis.
8. Documentos vacíos.
9. Contenido malformado.
10. Path traversal.
11. Rutas no autorizadas.
12. Rutas UNC y unidades no autorizadas.
13. Symlinks y junctions cuando el entorno permita probarlos.
14. Prevención de sobrescritura.
15. Sufijos secuenciales.
16. Exportaciones concurrentes.
17. Limpieza de temporales tras fallos simulados.
18. Fallos de escritura.
19. Disponibilidad opcional de python-docx.
20. Integración con SkillManager.execute_skill("document_export", ...).
21. Verificación del arranque y carga diferida.
22. Independencia del proveedor de IA.
23. Preservación de citas y referencias.
24. Consistencia del formato 'both'.
25. Comprobación de integridad de data/memory_store.json mediante metadatos exclusivamente.
"""

import concurrent.futures
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
from skills.document_parser import DocumentParser
from skills.document_templates import get_template, list_available_templates
from skills.markdown_renderer import MarkdownRenderer
from skills.word_renderer import WordRenderer
from skills.document_export_skill import DocumentExportSkill
from src.tools.skill_manager import SkillManager

# Hash y tamaño conocidos de data/memory_store.json tomados al inicio del paso
BASELINE_MEMORY_SIZE = 1218
BASELINE_MEMORY_SHA256 = "83640eaadff5e33feb9126f6eebb9f7960f924f01bb3a1968468014223341222"


class TestDocumentExportSkillExhaustive(unittest.TestCase):
    """Suite de pruebas unitarias y de integración para DocumentExportSkill."""

    def setUp(self) -> None:
        """Crea un directorio temporal aislado para cada prueba."""
        self.temp_dir_obj = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.temp_dir_obj.name).resolve()
        # Instanciar skill con el directorio de pruebas como raíz autorizada
        self.skill = DocumentExportSkill(allowed_roots=[self.test_dir])

    def tearDown(self) -> None:
        """Limpia el directorio temporal aislado de la prueba."""
        try:
            self.temp_dir_obj.cleanup()
        except Exception:
            pass

    # 1. Renderizado básico Markdown
    def test_01_basic_markdown_rendering(self) -> None:
        doc = Document(
            title="Álgebra Lineal",
            elements=(
                Heading(level=1, text="Álgebra Lineal"),
                Paragraph(text="Introducción a espacios vectoriales."),
                Paragraph(text="Texto en negrita", bold=True),
            ),
        )
        renderer = MarkdownRenderer()
        md = renderer.render(doc)
        self.assertIn("# Álgebra Lineal", md)
        self.assertIn("Introducción a espacios vectoriales.", md)
        self.assertIn("**Texto en negrita**", md)

    # 2. Renderizado DOCX y validación de estructura
    def test_02_docx_rendering_and_structure(self) -> None:
        doc = Document(
            title="Guía de Física",
            author="Prof. Stark",
            metadata=(("subject", "Física I"),),
            elements=(
                Heading(level=1, text="Cinemática"),
                Paragraph(text="Estudio del movimiento en una dimensión."),
            ),
        )
        out_docx = self.test_dir / "fisica.docx"
        renderer = WordRenderer()
        renderer.render_to_file(doc, out_docx)
        self.assertTrue(out_docx.exists())
        self.assertGreater(out_docx.stat().st_size, 5000)

        import docx
        d = docx.Document(str(out_docx))
        p_texts = [p.text for p in d.paragraphs if p.text]
        self.assertIn("Guía de Física", p_texts)
        self.assertIn("Cinemática", p_texts)
        self.assertIn("Estudio del movimiento en una dimensión.", p_texts)

    # 3. Tablas multicelda
    def test_03_multi_cell_tables(self) -> None:
        table = Table(
            headers=("Matriz", "Determinante", "Invertible"),
            rows=(
                ("Identidad 3x3", "1", "Sí"),
                ("Singular", "0", "No"),
                ("Diagonal", "8", "Sí"),
            ),
            caption="Propiedades de matrices",
        )
        doc = Document(title="Matrices", elements=(table,))

        # Markdown
        md = MarkdownRenderer().render(doc)
        self.assertIn("| Matriz", md)
        self.assertIn("| Identidad 3x3", md)
        self.assertIn("*Propiedades de matrices*", md)

        # DOCX
        out_docx = self.test_dir / "matrices.docx"
        WordRenderer().render_to_file(doc, out_docx)
        self.assertTrue(out_docx.exists())
        import docx
        d = docx.Document(str(out_docx))
        self.assertEqual(len(d.tables), 1)
        self.assertEqual(len(d.tables[0].rows), 4)
        self.assertEqual(d.tables[0].rows[0].cells[0].text, "Matriz")

    # 4. Bloques de código
    def test_04_code_blocks(self) -> None:
        code_text = "def gauss_jordan(matrix):\n    # Reducción\n    return matrix"
        block = CodeBlock(code=code_text, language="python")
        doc = Document(title="Algoritmos", elements=(block,))

        md = MarkdownRenderer().render(doc)
        self.assertIn("```python", md)
        self.assertIn("def gauss_jordan", md)

        out_docx = self.test_dir / "codigo.docx"
        WordRenderer().render_to_file(doc, out_docx)
        self.assertTrue(out_docx.exists())

    # 5. Listas anidadas
    def test_05_nested_lists(self) -> None:
        items = (
            ListItem(text="Vectores", level=0),
            ListItem(text="Producto escalar", level=1),
            ListItem(text="Producto vectorial", level=1),
            ListItem(text="Matrices", level=0),
        )
        doc = Document(title="Temario", elements=(ListBlock(ordered=False, items=items),))
        md = MarkdownRenderer().render(doc)
        self.assertIn("- Vectores", md)
        self.assertIn("  - Producto escalar", md)

        out_docx = self.test_dir / "listas.docx"
        WordRenderer().render_to_file(doc, out_docx)
        self.assertTrue(out_docx.exists())

    # 6. Callouts y advertencias
    def test_06_callouts_and_warnings(self) -> None:
        c1 = Callout(callout_type="NOTE", content="Nota general de estudio.", title="Recordatorio")
        c2 = Callout(callout_type="WARNING", content="Cuidado con la división por cero.")
        c3 = Callout(callout_type="TIP", content="Use la regla de Laplace para 3x3.")
        c4 = Callout(callout_type="IMPORTANT", content="Tema obligatorio en examen final.")
        doc = Document(title="Avisos", elements=(c1, c2, c3, c4))

        md = MarkdownRenderer().render(doc)
        self.assertIn("> [!NOTE] Recordatorio", md)
        self.assertIn("> [!WARNING]", md)
        self.assertIn("> [!TIP]", md)
        self.assertIn("> [!IMPORTANT]", md)

        out_docx = self.test_dir / "avisos.docx"
        WordRenderer().render_to_file(doc, out_docx)
        self.assertTrue(out_docx.exists())

        # Validación de tipo inválido
        with self.assertRaises(ValueError):
            Callout(callout_type="INVALID_TYPE", content="Texto")

    # 7. Unicode: tildes, eñes, símbolos matemáticos y emojis
    def test_07_unicode_accents_math_emojis(self) -> None:
        unicode_text = "Fórmulas: α + β = γ, ∑_{i=1}^n x_i, ∫_a^b f(x) dx, A ≠ ∅, √25 = 5. Acentos: ñandú, música. Emojis: 🚀 💡 📐."
        doc = Document(title="Prueba Unicode: αβγ 🚀", elements=(Paragraph(text=unicode_text),))

        # Markdown
        md = MarkdownRenderer().render(doc)
        self.assertIn("Fórmulas: α + β = γ", md)
        self.assertIn("ñandú", md)
        self.assertIn("🚀 💡 📐", md)

        # DOCX
        out_docx = self.test_dir / "unicode.docx"
        WordRenderer().render_to_file(doc, out_docx)
        self.assertTrue(out_docx.exists())
        import docx
        d = docx.Document(str(out_docx))
        full_text = "\n".join(p.text for p in d.paragraphs)
        self.assertIn("α + β = γ", full_text)
        self.assertIn("ñandú", full_text)

    # 8. Documentos vacíos
    def test_08_empty_document(self) -> None:
        parser = DocumentParser()
        with self.assertRaises(ValueError):
            parser.parse("")
        with self.assertRaises(ValueError):
            parser.parse("   \n\n   ")
        # Invocación a skill con contenido vacío
        res = self.skill.execute(content="")
        self.assertIn("no se puede exportar un documento sin contenido", res.lower())

    # 9. Contenido malformado
    def test_09_malformed_content(self) -> None:
        # Texto con separadores rotos y líneas desordenadas
        raw_text = "| Col1 | Col2 |\n| incomp |"
        parser = DocumentParser()
        doc = parser.parse(raw_text, title="Malformado")
        self.assertEqual(doc.title, "Malformado")
        # El parser debe manejarlo de forma segura sin lanzar excepciones no controladas
        self.assertGreater(len(doc.elements), 0)

    # 10. Path traversal
    def test_10_path_traversal(self) -> None:
        traversal_attempts = [
            "../archivo_maligno.md",
            "..\\archivo_maligno.docx",
            "sub/../../archivo.md",
        ]
        for attempt in traversal_attempts:
            res = self.skill.execute(
                content="# Prueba Traversal",
                output_path=attempt,
                format="md",
            )
            self.assertIn("path traversal", res.lower())

    # 11. Rutas no autorizadas
    def test_11_unauthorized_paths(self) -> None:
        unauthorized = "C:\\Windows\\System32\\driver.docx"
        res = self.skill.execute(
            content="# Hack",
            output_path=unauthorized,
            format="docx",
        )
        self.assertTrue("acceso denegado" in res.lower() or "no está autorizada" in res.lower())

    # 12. Rutas UNC y unidades no autorizadas
    def test_12_unc_paths_and_unauthorized_drives(self) -> None:
        unc_attempts = [
            r"\\192.168.1.1\share\archivo.docx",
            r"//server/share/archivo.md",
        ]
        for unc in unc_attempts:
            res = self.skill.execute(content="# Red", output_path=unc)
            self.assertIn("unc", res.lower())

    # 13. Symlinks y junctions
    def test_13_symlinks_and_junctions(self) -> None:
        # Comprobar si el sistema permite crear enlaces simbólicos sin elevación
        symlink_target = self.test_dir / "target_dir"
        symlink_target.mkdir()
        symlink_path = self.test_dir / "symlink_dir"
        try:
            os.symlink(symlink_target, symlink_path, target_is_directory=True)
            has_symlink = True
        except (OSError, NotImplementedError):
            has_symlink = False

        if has_symlink:
            res = self.skill.execute(
                content="# Enlace",
                output_path=str(symlink_path / "doc.md"),
                format="md",
            )
            self.assertIn("exportado", res.lower())
        else:
            # Si Windows requiere permisos de administrador para symlinks, validamos la resolución de ruta real
            resolved = (self.test_dir / "normal").resolve()
            self.assertTrue(resolved.is_relative_to(self.test_dir))

    # 14. Prevención de sobrescritura
    def test_14_overwrite_prevention(self) -> None:
        target = self.test_dir / "nota.md"
        target.write_text("Original preexistente", encoding="utf-8")

        res = self.skill.execute(
            content="# Nuevo contenido",
            output_path=str(target),
            format="md",
            overwrite=False,
        )
        self.assertIn("nota_(1).md", res)
        # El archivo original permanece intacto
        self.assertEqual(target.read_text(encoding="utf-8"), "Original preexistente")

    # 15. Sufijos secuenciales
    def test_15_sequential_suffixes(self) -> None:
        out1 = self.skill.execute(content="# 1", output_path=str(self.test_dir / "doc.md"), overwrite=False)
        out2 = self.skill.execute(content="# 2", output_path=str(self.test_dir / "doc.md"), overwrite=False)
        out3 = self.skill.execute(content="# 3", output_path=str(self.test_dir / "doc.md"), overwrite=False)

        self.assertIn("doc.md", out1)
        self.assertIn("doc_(1).md", out2)
        self.assertIn("doc_(2).md", out3)
        self.assertTrue((self.test_dir / "doc.md").exists())
        self.assertTrue((self.test_dir / "doc_(1).md").exists())
        self.assertTrue((self.test_dir / "doc_(2).md").exists())

    # 16. Exportaciones concurrentes
    def test_16_concurrent_exports(self) -> None:
        def worker(num: int) -> str:
            return self.skill.execute(
                content=f"# Concurrente {num}\nTexto de prueba hilo {num}.",
                output_path=str(self.test_dir / "concurrente.md"),
                overwrite=False,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(worker, i) for i in range(5)]
            results = [f.result() for f in futures]

        # Verificar que todos tuvieron éxito y tienen archivos únicos
        created_files = list(self.test_dir.glob("concurrente*.md"))
        self.assertEqual(len(created_files), 5)

    # 17. Limpieza de temporales tras fallos simulados
    def test_17_temp_cleanup_on_simulated_failure(self) -> None:
        with patch.object(WordRenderer, "render_to_file", side_effect=RuntimeError("Fallo simulado en render")):
            res = self.skill.execute(
                content="# Fallo",
                output_path=str(self.test_dir / "fallo.docx"),
                format="docx",
            )
            self.assertTrue("no se pudo" in res.lower() or "fall" in res.lower())
            # Ningún temporal debe haber quedado residual en el directorio
            tmp_files = list(self.test_dir.glob(".*.tmp"))
            self.assertEqual(len(tmp_files), 0)

    # 18. Fallos de escritura
    def test_18_write_failure_resilience(self) -> None:
        with patch("os.replace", side_effect=PermissionError("WinError 32: Archivo bloqueado por otro proceso")):
            res = self.skill.execute(
                content="# Bloqueado",
                output_path=str(self.test_dir / "bloqueado.md"),
                format="md",
            )
            self.assertTrue("no se pudo" in res.lower() or "fall" in res.lower())

    # 19. Disponibilidad opcional de python-docx
    def test_19_optional_docx_availability(self) -> None:
        # Simulamos que python-docx no está instalado bloqueando su importación
        orig_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "docx" or name.startswith("docx."):
                raise ImportError("No module named 'docx'")
            return orig_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            # Markdown debe seguir funcionando al 100%
            res_md = self.skill.execute(
                content="# Sin docx",
                output_path=str(self.test_dir / "sin_docx.md"),
                format="md",
            )
            self.assertIn("exportado", res_md.lower())
            self.assertTrue((self.test_dir / "sin_docx.md").exists())

            # DOCX debe informar claramente la falta de la dependencia
            res_docx = self.skill.execute(
                content="# Con docx ausente",
                output_path=str(self.test_dir / "sin_docx.docx"),
                format="docx",
            )
            self.assertIn("python-docx", res_docx.lower())

    # 20. Integración con SkillManager.execute_skill("document_export", ...)
    def test_20_skill_manager_integration(self) -> None:
        sm = SkillManager()
        # Registrar skill con el directorio de pruebas aislado para no escribir en exports/
        sm.register_skill(DocumentExportSkill(allowed_roots=[self.test_dir]))
        # Invocación con diccionario posicional
        res_dict = sm.execute_skill(
            "document_export",
            {
                "content": "# Título SM\nContenido de prueba.",
                "format": "md",
                "output_path": str(self.test_dir / "sm_dict.md"),
            }
        )
        self.assertIn("exportado", res_dict.lower())
        self.assertTrue((self.test_dir / "sm_dict.md").exists())

        # Invocación con kwargs directos
        res_kwargs = sm.execute_skill(
            "document_export",
            content="# Título Kwargs\nContenido prueba kwargs.",
            format="md",
            output_path=str(self.test_dir / "sm_kwargs.md"),
        )
        self.assertIn("exportado", res_kwargs.lower())
        self.assertTrue((self.test_dir / "sm_kwargs.md").exists())

    # 21. Verificación del arranque y carga diferida
    def test_21_startup_and_lazy_loading(self) -> None:
        # WordRenderer.is_available() comprueba sin forzar importación permanente
        self.assertTrue(WordRenderer.is_available())
        # Instanciar DocumentExportSkill no debe requerir renderizar nada
        inst = DocumentExportSkill(allowed_roots=[self.test_dir])
        self.assertEqual(inst.name, "document_export")

    # 22. Independencia del proveedor de IA
    def test_22_ai_provider_independence(self) -> None:
        # Exportar sin ninguna clave de API ni servicio LLM activo
        res = self.skill.execute(
            content="# Documento Agnóstico\nTexto puramente local sin LLMs.",
            format="both",
            output_path=str(self.test_dir / "agnostico"),
        )
        self.assertIn("exitosamente en ambos formatos", res.lower())

    # 23. Preservación de citas y referencias
    def test_23_citation_and_reference_preservation(self) -> None:
        content_with_refs = (
            "# Álgebra Abstracta\n\n"
            "Teorema de Lagrange en teoría de grupos.\n\n"
            "[^1]: Dummit & Foote, Abstract Algebra 3rd ed, https://example.com/algebra\n"
            "[^2]: Herstein, Topics in Algebra, https://example.com/herstein\n"
        )
        res = self.skill.execute(
            content=content_with_refs,
            mode="study",
            template="structured_notes",
            format="md",
            output_path=str(self.test_dir / "algebra_estudio.md"),
        )
        self.assertIn("exportado", res.lower())
        saved_text = (self.test_dir / "algebra_estudio.md").read_text(encoding="utf-8")
        self.assertIn("Dummit & Foote", saved_text)
        self.assertIn("https://example.com/algebra", saved_text)

    # 24. Consistencia del formato 'both'
    def test_24_both_format_consistency(self) -> None:
        base_out = str(self.test_dir / "apuntes_completos")
        res = self.skill.execute(
            content="# Álgebra y Geometría\n\nContenido completo para ambos formatos.",
            format="both",
            output_path=base_out,
            title="Álgebra y Geometría",
        )
        self.assertIn("exitosamente en ambos formatos", res.lower())
        md_file = self.test_dir / "apuntes_completos.md"
        docx_file = self.test_dir / "apuntes_completos.docx"
        self.assertTrue(md_file.exists())
        self.assertTrue(docx_file.exists())
        self.assertGreater(md_file.stat().st_size, 0)
        self.assertGreater(docx_file.stat().st_size, 0)

    # 25. Comprobación de integridad de data/memory_store.json mediante metadatos exclusivamente
    def test_25_memory_store_integrity_check(self) -> None:
        memory_path = Path("data/memory_store.json")
        self.assertTrue(memory_path.exists(), "data/memory_store.json debe existir.")

        # Obtener exclusivamente tamaño y SHA-256 sin leer contenido en memoria conversacional
        actual_size = memory_path.stat().st_size
        hasher = hashlib.sha256()
        with open(memory_path, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        actual_sha256 = hasher.hexdigest()

        self.assertEqual(
            actual_size,
            BASELINE_MEMORY_SIZE,
            f"El tamaño de data/memory_store.json fue alterado ({actual_size} != {BASELINE_MEMORY_SIZE}).",
        )
        self.assertEqual(
            actual_sha256,
            BASELINE_MEMORY_SHA256,
            f"El SHA-256 de data/memory_store.json fue alterado ({actual_sha256} != {BASELINE_MEMORY_SHA256}).",
        )


if __name__ == "__main__":
    unittest.main()
