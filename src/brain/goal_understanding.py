"""Módulo de comprensión de objetivos en lenguaje natural para N6.

Traduce peticiones abiertas del usuario en una estructura formal `Goal`,
identificando intención principal, restricciones explícitas, información faltante,
ambigüedad que requiera aclaración y capacidades necesarias.

Principio de diseño: si un cerebro LLM está disponible puede enriquecer la
interpretación semántica, pero la validación de restricciones y el fallback
determinista aseguran operación confiable incluso sin conexión o con respuestas
inválidas del modelo.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional, TYPE_CHECKING

from src.brain.goal_models import (
    Constraints,
    Goal,
    GoalContext,
    GoalPlanningError,
    GoalStatus,
    GoalType,
    RequiredCapabilities,
    SuccessCriteria,
)

if TYPE_CHECKING:
    from src.brain.brain_manager import BrainManager


_URL_RE = re.compile(r"https?://[^\s<>'\"`]+", re.IGNORECASE)
_PATH_HINT_RE = re.compile(
    r"(?:[a-zA-Z]:\\[^\s'\"]+|[\w.-]+\.(?:ini|cfg|conf|json|yaml|yml|toml|txt|md|docx))",
    re.IGNORECASE,
)
_GPU_HINT_RE = re.compile(r"\b(?:GTX|RTX|RX|Radeon|Arc)\s*[A-Za-z0-9-]+\b", re.IGNORECASE)
_VRAM_HINT_RE = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*(?:gb|gigas?)\s*(?:de\s*)?vram\b", re.IGNORECASE)
_RAM_HINT_RE = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*(?:gb|gigas?)\s*(?:de\s*)?ram\b", re.IGNORECASE)

_READ_ONLY_PATTERNS: tuple[str, ...] = (
    "solo diagnostico",
    "solo diagnóstico",
    "sin modificar",
    "no modifiques",
    "sin cambiar archivos",
    "no cambies archivos",
    "sin tocar archivos",
    "no toques archivos",
    "solo lectura",
    "sin editar",
    "no edites",
    "sin escribir",
    "no escribas",
    "sin aplicar cambios",
)

_OFFLINE_PATTERNS: tuple[str, ...] = (
    "sin internet",
    "sin red",
    "modo offline",
    "offline",
    "no busques en internet",
    "sin consultar la web",
)

_AMBIGUOUS_EXACT: frozenset[str] = frozenset(
    {
        "ayuda",
        "ayudame",
        "ayúdame",
        "mejora todo",
        "mejorá todo",
        "hazlo mejor",
        "hacelo mejor",
        "arreglalo",
        "arréglalo",
        "arregla todo",
        "optimiza",
        "optimizá",
        "hace algo",
        "haz algo",
        "quiero mejorar",
        "necesito ayuda",
        "investiga",
        "investigá",
        "busca",
        "buscá",
        "exporta",
        "exportá",
    }
)

_KNOWN_GAMES_AND_APPS: tuple[str, ...] = (
    "valorant",
    "counter-strike",
    "cs2",
    "csgo",
    "fortnite",
    "minecraft",
    "cyberpunk",
    "league of legends",
    "lol",
    "gta",
    "elden ring",
    "apex legends",
    "warzone",
    "steam",
    "chrome",
    "edge",
    "vs code",
    "vscode",
    "obs",
)

_STOPWORDS: frozenset[str] = frozenset(
    {
        "jarvis",
        "hola",
        "por",
        "favor",
        "quiero",
        "necesito",
        "podes",
        "puedes",
        "poder",
        "mi",
        "mis",
        "tu",
        "el",
        "la",
        "los",
        "las",
        "un",
        "una",
        "unos",
        "unas",
        "de",
        "del",
        "en",
        "para",
        "con",
        "sin",
        "y",
        "o",
        "que",
        "esto",
        "eso",
        "algo",
        "todo",
        "mas",
        "más",
        "mejor",
    }
)


def _strip_accents_lower(text: str) -> str:
    """Normaliza mínimamente acentos comunes para clasificación robusta en español."""
    replacements = {
        "á": "a",
        "é": "e",
        "í": "i",
        "ó": "o",
        "ú": "u",
        "ü": "u",
    }
    lowered = (text or "").lower().strip()
    for src, dst in replacements.items():
        lowered = lowered.replace(src, dst)
    return lowered


class GoalUnderstandingEngine:
    """Interpreta objetivos de lenguaje natural y produce un `Goal` validado."""

    def __init__(
        self,
        *,
        brain_manager: Optional["BrainManager"] = None,
        use_ai: bool = False,
    ) -> None:
        self.brain_manager: Optional["BrainManager"] = brain_manager
        self.use_ai: bool = bool(use_ai and brain_manager is not None)

    def interpret(
        self,
        raw_input: str,
        *,
        initial_context: Optional[GoalContext] = None,
        explicit_constraints: Optional[Constraints] = None,
    ) -> Goal:
        """Convierte una petición del usuario en un objeto `Goal` estructurado y trazable."""
        cleaned = (raw_input or "").strip()
        if not cleaned:
            raise GoalPlanningError("El objetivo no puede estar vacío.")

        constraints = self._extract_constraints(cleaned, base=explicit_constraints)
        context = self._extract_context(cleaned, base=initial_context)
        goal_type = self._classify_goal_type(cleaned)
        needs_clarification, questions = self._detect_ambiguity(cleaned, goal_type, context)
        missing_info = self._identify_missing_info(cleaned, goal_type, context)
        capabilities = self._infer_capabilities(cleaned, goal_type, constraints, context)
        success_criteria = self._build_success_criteria(cleaned, goal_type, constraints)
        summary = self._build_summary(cleaned, goal_type, constraints)

        initial_status = (
            GoalStatus.AWAITING_CLARIFICATION if needs_clarification else GoalStatus.DRAFT
        )
        goal = Goal(
            raw_input=cleaned,
            interpreted_summary=summary,
            goal_type=GoalType.AMBIGUOUS if needs_clarification else goal_type,
            status=initial_status,
            success_criteria=success_criteria,
            constraints=constraints,
            required_capabilities=capabilities,
            context=context,
            needs_clarification=needs_clarification,
            clarification_questions=questions,
            missing_info=missing_info,
        )

        # Enriquecimiento opcional vía Multi-Brain: el modelo propone, el motor valida.
        if self.use_ai and self.brain_manager is not None and not needs_clarification:
            self._enrich_with_ai(goal)

        return goal

    def apply_clarification(self, goal: Goal, clarification_text: str) -> Goal:
        """Incorpora aclaración del usuario sobre un objetivo ambiguo y reevalúa su definición."""
        extra = (clarification_text or "").strip()
        if not extra:
            raise GoalPlanningError("La aclaración proporcionada no puede estar vacía.")
        combined = f"{goal.raw_input}. Aclaración del usuario: {extra}"
        updated = self.interpret(
            combined,
            initial_context=goal.context,
            explicit_constraints=goal.constraints,
        )
        updated.goal_id = goal.goal_id
        updated.created_at = goal.created_at
        return updated

    def _extract_constraints(
        self,
        raw_input: str,
        *,
        base: Optional[Constraints] = None,
    ) -> Constraints:
        normalized = _strip_accents_lower(raw_input)
        constraints = Constraints.from_dict(base.to_dict()) if base else Constraints()

        for pattern in _READ_ONLY_PATTERNS:
            if _strip_accents_lower(pattern) in normalized:
                constraints.read_only = True
                constraints.allow_file_modifications = False
                for forbidden in ("file_editor", "document_export"):
                    if forbidden not in constraints.forbidden_actions:
                        constraints.forbidden_actions.append(forbidden)
                if "Restricción detectada: modo solo lectura / sin modificación de archivos." not in constraints.notes:
                    constraints.notes.append(
                        "Restricción detectada: modo solo lectura / sin modificación de archivos."
                    )
                break

        for pattern in _OFFLINE_PATTERNS:
            if _strip_accents_lower(pattern) in normalized:
                constraints.allow_network = False
                for forbidden in ("web_search", "web_extraction"):
                    if forbidden not in constraints.forbidden_actions:
                        constraints.forbidden_actions.append(forbidden)
                if "Restricción detectada: ejecución offline sin acceso de red." not in constraints.notes:
                    constraints.notes.append(
                        "Restricción detectada: ejecución offline sin acceso de red."
                    )
                break

        # Regla institucional inviolable: las modificaciones siempre exigen aprobación explícita.
        constraints.require_explicit_approval_for_changes = True
        return constraints

    def _extract_context(
        self,
        raw_input: str,
        *,
        base: Optional[GoalContext] = None,
    ) -> GoalContext:
        ctx = GoalContext.from_dict(base.to_dict()) if base else GoalContext()
        urls = _URL_RE.findall(raw_input)
        if urls:
            existing_urls = list(ctx.extra.get("urls") or [])
            for url in urls:
                if url not in existing_urls:
                    existing_urls.append(url)
            ctx.extra["urls"] = existing_urls

        for path_match in _PATH_HINT_RE.findall(raw_input):
            if path_match not in ctx.target_paths:
                ctx.target_paths.append(path_match)

        gpu_match = _GPU_HINT_RE.search(raw_input)
        if gpu_match and "gpu" not in ctx.hardware:
            ctx.hardware["gpu"] = gpu_match.group(0)
        vram_match = _VRAM_HINT_RE.search(raw_input)
        if vram_match and "vram_gb" not in ctx.hardware:
            ctx.hardware["vram_gb"] = float(vram_match.group(1).replace(",", "."))
        ram_match = _RAM_HINT_RE.search(raw_input)
        if ram_match and "ram_gb" not in ctx.hardware:
            ctx.hardware["ram_gb"] = float(ram_match.group(1).replace(",", "."))

        lowered = raw_input.lower()
        if not ctx.target_app:
            for candidate in _KNOWN_GAMES_AND_APPS:
                if re.search(rf"\b{re.escape(candidate)}\b", lowered):
                    ctx.target_app = candidate
                    break

        return ctx

    def _classify_goal_type(self, raw_input: str) -> GoalType:
        normalized = _strip_accents_lower(raw_input)

        if any(tok in normalized for tok in ("exporta", "exportar", "docx", "markdown")):
            return GoalType.DOCUMENTATION
        if any(tok in normalized for tok in ("informe", "documento", "estudio")):
            if not any(tok in normalized for tok in ("optimiz", "diagnost")):
                return GoalType.DOCUMENTATION
        if any(tok in normalized for tok in ("diagnost", "estado del sistema", "cpu", "ram", "temperatura", "uso de memoria", "monitorear", "chequear pc")):
            if "optimiz" not in normalized and "jugar" not in normalized and "juego" not in normalized:
                return GoalType.DIAGNOSTIC
        if any(tok in normalized for tok in ("optimiz", "rendimiento", "fps", "jugar", "gaming", "acelerar", "fluidez")):
            return GoalType.OPTIMIZATION
        if _URL_RE.search(raw_input) or any(tok in normalized for tok in ("investiga", "investigar", "busca", "buscar", "web", "fuente", "articulo", "documentacion")):
            return GoalType.RESEARCH
        if any(tok in normalized for tok in ("edita", "editar", "modifica", "modificar", "config", "archivo", "abre", "abrir", "protocolo", "volumen")):
            return GoalType.AUTOMATION
        return GoalType.GENERAL

    def _detect_ambiguity(
        self,
        raw_input: str,
        goal_type: GoalType,
        context: GoalContext,
    ) -> tuple[bool, list[str]]:
        normalized = re.sub(r"[^\w\sáéíóúüñ]", " ", raw_input.lower()).strip()
        collapsed = " ".join(normalized.split())
        if _strip_accents_lower(collapsed) in {_strip_accents_lower(item) for item in _AMBIGUOUS_EXACT}:
            return True, [
                "¿Qué sistema, archivo, aplicación o tema específico deseas abordar?",
                "¿Prefieres solo un diagnóstico informativo o autorizar modificaciones posteriores?",
            ]

        meaningful_tokens = [
            tok for tok in re.findall(r"[a-zA-ZáéíóúüñÁÉÍÓÚÜÑ0-9]+", collapsed)
            if _strip_accents_lower(tok) not in _STOPWORDS and len(tok) >= 3
        ]
        # Frases excesivamente genéricas o muy cortas sin URL ni app ni archivo objetivo.
        has_concrete_anchor = bool(
            context.target_app
            or context.target_paths
            or context.extra.get("urls")
            or any(
                kw in _strip_accents_lower(collapsed)
                for kw in (
                    "pc",
                    "cpu",
                    "ram",
                    "gpu",
                    "jugar",
                    "rendimiento",
                    "diagnostico",
                    "hora",
                    "portapapeles",
                    "documento",
                    "archivo",
                    "sistema",
                    "windows",
                    "python",
                )
            )
        )
        if len(meaningful_tokens) < 2 and not has_concrete_anchor:
            return True, [
                "Tu petición es demasiado breve para planificar con seguridad. ¿Cuál es el resultado concreto que esperas obtener?",
                "¿Existe alguna aplicación, archivo o restricción de solo lectura que deba respetar?",
            ]

        # Si el usuario pide explícitamente editar/modificar sin indicar archivo, ruta ni juego.
        norm_no_acc = _strip_accents_lower(collapsed)
        if goal_type == GoalType.AUTOMATION and any(
            v in norm_no_acc for v in ("editar archivo", "modificar archivo", "cambiar configuracion", "editar configuracion")
        ):
            if not context.target_paths and not context.target_app:
                return True, [
                    "¿Qué archivo o configuración específica deseas modificar?",
                    "¿Cuál es el cambio exacto que deseas evaluar antes de aprobarlo?",
                ]

        return False, []

    def _identify_missing_info(
        self,
        raw_input: str,
        goal_type: GoalType,
        context: GoalContext,
    ) -> list[str]:
        missing: list[str] = []
        if goal_type == GoalType.OPTIMIZATION:
            if not context.hardware:
                missing.append("Métricas actuales de CPU y RAM disponible (obtenibles mediante diagnóstico local)")
            if not context.target_app and not context.target_paths:
                missing.append("Juego o aplicación objetivo específica y archivo de configuración si se desean ajustes finos")
        elif goal_type == GoalType.DIAGNOSTIC:
            if not context.hardware:
                missing.append("Lectura en vivo de uso de CPU y memoria RAM disponible")
        elif goal_type == GoalType.RESEARCH:
            if not context.extra.get("urls"):
                missing.append("URLs verificadas específicas si se requiere extracción directa sin navegador")
        return missing

    def _infer_capabilities(
        self,
        raw_input: str,
        goal_type: GoalType,
        constraints: Constraints,
        context: GoalContext,
    ) -> RequiredCapabilities:
        skills: list[str] = []
        requires_chain = False
        requires_web = False
        requires_hw = False

        if goal_type in {GoalType.OPTIMIZATION, GoalType.DIAGNOSTIC}:
            skills.append("system_info")
            requires_hw = True

        if goal_type == GoalType.OPTIMIZATION and not constraints.read_only and constraints.allow_file_modifications:
            if context.target_app or context.target_paths:
                skills.append("file_editor")
                requires_chain = True

        if goal_type == GoalType.RESEARCH and constraints.allow_network:
            requires_web = True
            if context.extra.get("urls"):
                skills.append("web_extraction")
            else:
                skills.append("web_search")

        if goal_type == GoalType.DOCUMENTATION and not constraints.read_only and constraints.allow_file_modifications:
            skills.extend(["system_info", "document_export"])
            requires_hw = True

        lowered = _strip_accents_lower(raw_input)
        if "hora" in lowered or "fecha" in lowered:
            if "system_time" not in skills:
                skills.append("system_time")
        if "portapapeles" in lowered or "clipboard" in lowered:
            if "clipboard_manager" not in skills:
                skills.append("clipboard_manager")
        if context.target_paths and not constraints.read_only and constraints.allow_file_modifications:
            if "file_editor" not in skills and goal_type != GoalType.DOCUMENTATION:
                skills.append("file_editor")
                requires_chain = True

        return RequiredCapabilities(
            skills=skills,
            requires_chain_orchestrator=requires_chain,
            requires_web=requires_web,
            requires_hardware_inspection=requires_hw,
        )

    def _build_success_criteria(
        self,
        raw_input: str,
        goal_type: GoalType,
        constraints: Constraints,
    ) -> SuccessCriteria:
        if goal_type == GoalType.OPTIMIZATION:
            outcomes = [
                "Diagnosticar estado base de CPU y memoria RAM del equipo.",
                "Consolidar recomendaciones de optimización adaptadas al hardware detectado.",
            ]
            if not constraints.read_only and constraints.allow_file_modifications:
                outcomes.append(
                    "Preparar cualquier modificación de archivos bajo ChangePlan y esperar aprobación explícita antes de aplicar cambios."
                )
            else:
                outcomes.append("Emitir diagnóstico y plan seguro sin alterar archivos del sistema.")
            return SuccessCriteria(
                summary="Optimizar o diagnosticar el rendimiento del equipo con trazabilidad y seguridad.",
                required_outcomes=outcomes,
                verification_checks=["Evidencia de system_info verificada", "Sin cambios no autorizados"],
            )

        if goal_type == GoalType.DIAGNOSTIC:
            return SuccessCriteria(
                summary="Obtener diagnóstico real del estado actual del sistema sin efectos secundarios.",
                required_outcomes=["Lectura real de métricas del sistema", "Resumen técnico claro"],
                verification_checks=["Sin modificaciones en disco"],
            )

        return SuccessCriteria(
            summary=f"Completar de manera verificable y controlada: {raw_input}",
            required_outcomes=["Ejecutar subtareas válidas en orden de dependencias", "Generar reporte final"],
            verification_checks=["Estado consistente y validado por PolicyEngine/Supervisor"],
        )

    def _build_summary(
        self,
        raw_input: str,
        goal_type: GoalType,
        constraints: Constraints,
    ) -> str:
        suffix = " (solo lectura / sin modificaciones)" if constraints.read_only else ""
        return f"[{goal_type.value}] {raw_input}{suffix}"

    def _enrich_with_ai(self, goal: Goal) -> None:
        """Consulta opcional al Multi-Brain para refinar resumen/criterios sin debilitar políticas."""
        assert self.brain_manager is not None
        prompt = (
            "Analiza el siguiente objetivo del usuario y responde SOLO con un objeto JSON válido "
            "con claves opcionales: interpreted_summary (str), missing_info (list[str]), "
            "required_outcomes (list[str]), target_app (str|null).\n"
            f"Objetivo: {goal.raw_input}\n"
            f"Tipo tentativo: {goal.goal_type.value}"
        )
        response_text, _provider, _model, _fallback, failed = self.brain_manager.query_raw(
            prompt,
            system_prompt=(
                "Eres el analizador semántico de objetivos de J.A.R.V.I.S. "
                "Devuelve exclusivamente JSON válido sin texto adicional ni markdown."
            ),
            voice_mode=False,
        )
        if failed or not response_text:
            return

        cleaned = response_text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.DOTALL).strip()
        try:
            payload: dict[str, Any] = json.loads(cleaned)
        except Exception:
            return
        if not isinstance(payload, dict):
            return

        summary = payload.get("interpreted_summary")
        if isinstance(summary, str) and summary.strip():
            goal.interpreted_summary = summary.strip()
        missing = payload.get("missing_info")
        if isinstance(missing, list):
            for item in missing:
                text = str(item).strip()
                if text and text not in goal.missing_info:
                    goal.missing_info.append(text)
        outcomes = payload.get("required_outcomes")
        if isinstance(outcomes, list) and outcomes:
            refined = [str(x).strip() for x in outcomes if str(x).strip()]
            if refined:
                goal.success_criteria.required_outcomes = refined
        target_app = payload.get("target_app")
        if isinstance(target_app, str) and target_app.strip() and not goal.context.target_app:
            goal.context.target_app = target_app.strip()
