"""
Módulo de Plantillas Documentales y Modo Estudio para J.A.R.V.I.S.
==================================================================
Proporciona estructuras documentales estandarizadas para el Modo Estudio
y exportación técnica académica:
1. Apuntes estructurados (structured_notes)
2. Resúmenes ejecutivos (executive_summary)
3. Guías de estudio (study_guide)
4. Preguntas y respuestas / Q&A (qa_sheet)
5. Tablas comparativas (comparative_table)
6. Glosarios de términos (glossary)
7. Cuestionarios y autoevaluación (quiz)
8. Informes técnicos (technical_report)

Reglas arquitectónicas:
- El Modo Estudio organiza la estructura sin inventar datos ni generar
  conocimiento académico por su cuenta.
- Preserva íntegramente las fuentes, citas y referencias provistas por el usuario o LLM.
- No convierte afirmaciones no documentadas en citas inventadas.
- Desacoplado de proveedores de IA específicos.
"""

from abc import ABC, abstractmethod
import datetime
from typing import Any


class BaseDocumentTemplate(ABC):
    """Clase base abstracta para todas las plantillas del Modo Estudio."""

    def __init__(self, template_id: str, display_name: str, description: str) -> None:
        self.template_id = template_id
        self.display_name = display_name
        self.description = description

    @abstractmethod
    def format_document(
        self,
        content: str,
        title: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """
        Organiza el contenido provisto bajo la estructura formal de la plantilla.

        Args:
            content (str): Texto o contenido original proporcionado.
            title (str): Título principal del documento.
            metadata (dict, optional): Metadatos complementarios (materia, autor, fecha).

        Returns:
            str: Contenido formateado en Markdown estructurado.
        """
        pass

    def _extract_references(self, content: str) -> tuple[str, list[str]]:
        """
        Separa las referencias y citas existentes al final del texto para preservarlas intactas.
        """
        lines = content.strip().splitlines()
        body_lines: list[str] = []
        ref_lines: list[str] = []
        in_refs = False

        for line in lines:
            stripped = line.strip()
            if stripped.lower().startswith("## referencias") or stripped.lower().startswith("## fuentes"):
                in_refs = True
                continue
            if stripped.startswith("[^") and "]:" in stripped:
                ref_lines.append(line)
            elif in_refs:
                ref_lines.append(line)
            else:
                body_lines.append(line)

        return "\n".join(body_lines).strip(), ref_lines


class StructuredNotesTemplate(BaseDocumentTemplate):
    """Plantilla para apuntes universitarios y notas de clase estructuradas."""

    def __init__(self) -> None:
        super().__init__(
            template_id="structured_notes",
            display_name="Apuntes Estructurados",
            description="Organización jerárquica para estudio: conceptos clave, desarrollo teórico y puntos críticos.",
        )

    def format_document(self, content: str, title: str, metadata: dict[str, Any] | None = None) -> str:
        meta = metadata or {}
        subject = meta.get("subject", meta.get("materia", "Área de Estudio"))
        body, refs = self._extract_references(content)

        doc = [
            f"# {title}",
            f"> [!NOTE] Metadatos de Estudio",
            f"> Materia: {subject} | Modo: Apuntes Estructurados",
            "",
            "## 1. Conceptos Clave y Marco Teórico",
            body,
            "",
            "## 2. Puntos Críticos y Observaciones",
            "> [!IMPORTANT] Atención en Exámenes",
            "> Revisar demostraciones, casos límite y aplicaciones prácticas de este tema.",
            "",
        ]

        if refs:
            doc.append("## 3. Fuentes y Referencias")
            doc.extend(refs)
            doc.append("")

        return "\n".join(doc)


class ExecutiveSummaryTemplate(BaseDocumentTemplate):
    """Plantilla para resúmenes ejecutivos breves y conclusiones accionables."""

    def __init__(self) -> None:
        super().__init__(
            template_id="executive_summary",
            display_name="Resumen Ejecutivo",
            description="Síntesis de alto nivel con objetivos, hallazgos clave y recomendaciones.",
        )

    def format_document(self, content: str, title: str, metadata: dict[str, Any] | None = None) -> str:
        body, refs = self._extract_references(content)

        doc = [
            f"# {title}",
            "> [!TIP] Resumen Ejecutivo",
            "> Documento sintetizado para revisión rápida y toma de decisiones.",
            "",
            "## 1. Contexto y Objetivos",
            "El presente informe consolida los aspectos fundamentales del tema evaluado.",
            "",
            "## 2. Síntesis y Puntos Principales",
            body,
            "",
            "## 3. Conclusiones y Recomendaciones",
            "> [!NOTE] Directiva",
            "> Priorizar las áreas identificadas con mayor impacto operativo o académico.",
            "",
        ]

        if refs:
            doc.append("## Fuentes y Referencias de Respaldo")
            doc.extend(refs)
            doc.append("")

        return "\n".join(doc)


class StudyGuideTemplate(BaseDocumentTemplate):
    """Plantilla para guías de estudio orientadas a preparación de exámenes."""

    def __init__(self) -> None:
        super().__init__(
            template_id="study_guide",
            display_name="Guía de Estudio",
            description="Hoja de ruta temática con objetivos de aprendizaje y checklist de dominio.",
        )

    def format_document(self, content: str, title: str, metadata: dict[str, Any] | None = None) -> str:
        body, refs = self._extract_references(content)

        doc = [
            f"# Guía de Estudio: {title}",
            "> [!NOTE] Hoja de Ruta de Aprendizaje",
            "> Utilice esta guía para planificar sesiones de repaso espaciado y autoevaluación.",
            "",
            "## 1. Contenido Central",
            body,
            "",
            "## 2. Checklist de Autoevaluación",
            "- [ ] Dominar definiciones y terminología técnica básica.",
            "- [ ] Resolver ejercicios prácticos y aplicaciones directas.",
            "- [ ] Explicar los conceptos principales con palabras propias sin consultar apuntes.",
            "",
        ]

        if refs:
            doc.append("## Material de Consulta")
            doc.extend(refs)
            doc.append("")

        return "\n".join(doc)


class QASheetTemplate(BaseDocumentTemplate):
    """Plantilla para hojas de preguntas y respuestas frecuentes (Q&A)."""

    def __init__(self) -> None:
        super().__init__(
            template_id="qa_sheet",
            display_name="Preguntas y Respuestas (Q&A)",
            description="Formato de consulta directa mediante pares de preguntas y respuestas detalladas.",
        )

    def format_document(self, content: str, title: str, metadata: dict[str, Any] | None = None) -> str:
        body, refs = self._extract_references(content)

        doc = [
            f"# {title} — Preguntas y Respuestas",
            "> [!TIP] Sesión de Repaso Activo",
            "> Oculte las respuestas para verificar su retención antes de leerlas.",
            "",
            "## Banco de Preguntas y Respuestas",
            body,
            "",
        ]

        if refs:
            doc.append("## Referencias Consultadas")
            doc.extend(refs)
            doc.append("")

        return "\n".join(doc)


class ComparativeTableTemplate(BaseDocumentTemplate):
    """Plantilla para análisis comparativos y matrices de evaluación."""

    def __init__(self) -> None:
        super().__init__(
            template_id="comparative_table",
            display_name="Tabla Comparativa",
            description="Estructura orientada a contrastar alternativas, criterios, ventajas y desventajas.",
        )

    def format_document(self, content: str, title: str, metadata: dict[str, Any] | None = None) -> str:
        body, refs = self._extract_references(content)

        doc = [
            f"# Cuadro Comparativo: {title}",
            "> [!NOTE] Matriz de Contraste",
            "> Análisis diferencial de variables, criterios y resultados esperados.",
            "",
            "## Matriz Comparativa y Desarrollo",
            body,
            "",
        ]

        if refs:
            doc.append("## Fuentes de los Datos")
            doc.extend(refs)
            doc.append("")

        return "\n".join(doc)


class GlossaryTemplate(BaseDocumentTemplate):
    """Plantilla para glosarios y diccionarios terminológicos."""

    def __init__(self) -> None:
        super().__init__(
            template_id="glossary",
            display_name="Glosario de Términos",
            description="Compilación organizada de definiciones precisas y vocabulario técnico.",
        )

    def format_document(self, content: str, title: str, metadata: dict[str, Any] | None = None) -> str:
        body, refs = self._extract_references(content)

        doc = [
            f"# Glosario: {title}",
            "> [!NOTE] Vocabulario Técnico",
            "> Definiciones formales para consulta rápida durante el curso.",
            "",
            "## Términos y Definiciones",
            body,
            "",
        ]

        if refs:
            doc.append("## Fuentes Bibliográficas")
            doc.extend(refs)
            doc.append("")

        return "\n".join(doc)


class QuizTemplate(BaseDocumentTemplate):
    """Plantilla para cuestionarios, exámenes de práctica y autoevaluaciones."""

    def __init__(self) -> None:
        super().__init__(
            template_id="quiz",
            display_name="Cuestionario de Autoevaluación",
            description="Evaluación formativa estructurada en preguntas conceptuales o de opción múltiple.",
        )

    def format_document(self, content: str, title: str, metadata: dict[str, Any] | None = None) -> str:
        body, refs = self._extract_references(content)

        doc = [
            f"# Evaluación Formativa: {title}",
            "> [!WARNING] Instrucciones",
            "> Responda cada consigna individualmente antes de cotejar con el material de referencia.",
            "",
            "## Cuestionario",
            body,
            "",
            "> [!TIP] Clave de Autoevaluación",
            "> Verifique el razonamiento detrás de cada respuesta.",
            "",
        ]

        if refs:
            doc.append("## Material de Referencia")
            doc.extend(refs)
            doc.append("")

        return "\n".join(doc)


class TechnicalReportTemplate(BaseDocumentTemplate):
    """Plantilla para informes técnicos y documentación de ingeniería."""

    def __init__(self) -> None:
        super().__init__(
            template_id="technical_report",
            display_name="Informe Técnico",
            description="Estructura formal de ingeniería: alcance, metodología, resultados y conclusiones.",
        )

    def format_document(self, content: str, title: str, metadata: dict[str, Any] | None = None) -> str:
        meta = metadata or {}
        author = meta.get("author", meta.get("autor", "Equipo Técnico J.A.R.V.I.S."))
        date_str = meta.get("date", meta.get("fecha", datetime.date.today().isoformat()))
        body, refs = self._extract_references(content)

        doc = [
            f"# {title}",
            f"> Autor: {author} | Fecha: {date_str} | Estado: Versión Técnica",
            "",
            "## 1. Alcance y Contexto",
            "El objetivo de este informe es documentar formalmente las características y hallazgos técnicos del sistema.",
            "",
            "## 2. Desarrollo Técnico y Análisis",
            body,
            "",
            "## 3. Conclusiones y Próximos Pasos",
            "> [!IMPORTANT] Dictamen Técnico",
            "> Las implementaciones deben verificarse contra las suites de pruebas y lineamientos de arquitectura.",
            "",
        ]

        if refs:
            doc.append("## 4. Referencias y Documentación Técnica")
            doc.extend(refs)
            doc.append("")

        return "\n".join(doc)


# Registro central de plantillas oficiales del Modo Estudio
_TEMPLATES_REGISTRY: dict[str, BaseDocumentTemplate] = {
    "structured_notes": StructuredNotesTemplate(),
    "executive_summary": ExecutiveSummaryTemplate(),
    "study_guide": StudyGuideTemplate(),
    "qa_sheet": QASheetTemplate(),
    "comparative_table": ComparativeTableTemplate(),
    "glossary": GlossaryTemplate(),
    "quiz": QuizTemplate(),
    "technical_report": TechnicalReportTemplate(),
}

# Mapeo de alias en español y nombres comunes para tolerancia en comandos
_ALIASES: dict[str, str] = {
    "apuntes": "structured_notes",
    "apuntes_estructurados": "structured_notes",
    "notes": "structured_notes",
    "resumen": "executive_summary",
    "resumen_ejecutivo": "executive_summary",
    "summary": "executive_summary",
    "guia": "study_guide",
    "guia_estudio": "study_guide",
    "guide": "study_guide",
    "qa": "qa_sheet",
    "q&a": "qa_sheet",
    "preguntas": "qa_sheet",
    "preguntas_respuestas": "qa_sheet",
    "comparativa": "comparative_table",
    "tabla_comparativa": "comparative_table",
    "glosario": "glossary",
    "cuestionario": "quiz",
    "examen": "quiz",
    "informe": "technical_report",
    "informe_tecnico": "technical_report",
    "reporte": "technical_report",
}


def get_template(name: str | None) -> BaseDocumentTemplate:
    """
    Obtiene una instancia de plantilla a partir de su ID o alias en español.
    Si name es None o no se reconoce, devuelve la plantilla por defecto 'structured_notes'.

    Args:
        name (str | None): Identificador o alias de la plantilla.

    Returns:
        BaseDocumentTemplate: Instancia de la plantilla correspondiente.
    """
    if not name:
        return _TEMPLATES_REGISTRY["structured_notes"]

    norm_name = str(name).strip().lower().replace("-", "_").replace(" ", "_")
    target_id = _ALIASES.get(norm_name, norm_name)

    if target_id in _TEMPLATES_REGISTRY:
        return _TEMPLATES_REGISTRY[target_id]

    # Fallback seguro a apuntes estructurados si se solicita un nombre desconocido
    return _TEMPLATES_REGISTRY["structured_notes"]


def is_template_available(name: str) -> bool:
    """Indica si el identificador o alias pertenece al registro de plantillas."""
    if not isinstance(name, str) or not name.strip():
        return False
    normalized = name.strip().lower().replace("-", "_").replace(" ", "_")
    target_id = _ALIASES.get(normalized, normalized)
    return target_id in _TEMPLATES_REGISTRY


def list_available_templates() -> list[dict[str, str]]:
    """
    Retorna la lista de todas las plantillas disponibles con sus descripciones.
    """
    return [
        {
            "id": t.template_id,
            "name": t.display_name,
            "description": t.description,
        }
        for t in _TEMPLATES_REGISTRY.values()
    ]
