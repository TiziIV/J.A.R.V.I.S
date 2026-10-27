"""
Gestor centralizado de la arquitectura Multi-Brain de J.A.R.V.I.S. (`BrainManager`).

Responsabilidades principales:
1. Mantener una única fuente de verdad para `global_mode` (`AUTO`, `OLLAMA`, `OLLAMA_UNCENSORED`,
   `GEMINI`, `CLAUDE`, `GPT`).
2. Resolver la prioridad de selección por solicitud:
   a) Comandos explícitos de consola o selector de motor.
   b) Selección puntual One-Shot para una única consulta (sin alterar `global_mode`).
   c) Modo global fijado manualmente por el usuario.
   d) Deliberación automática por intención y complejidad cuando `global_mode == "AUTO"`.
3. Coordinar la detección prioritaria de habilidades locales de Windows antes de consultar un LLM.
4. Centralizar la política acotada de fallbacks entre proveedores evitando ciclos
   (como Gemini -> Ollama -> Gemini) y garantizando que ninguna clave API se filtre en logs.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import string
import unicodedata
from typing import TYPE_CHECKING, Any, Optional

from config.prompts import JARVIS_SYSTEM_PROMPT, JARVIS_TEXT_SYSTEM_PROMPT
from config.settings import (
    BRAIN_ALLOW_CLOUD_FALLBACK,
    BRAIN_MODES,
    DEFAULT_BRAIN_MODE,
    OLLAMA_MODEL_CENSORED,
    OLLAMA_MODEL_UNCENSORED,
    TEXT_MAX_TOKENS,
    VOICE_MAX_TOKENS,
)
from src.brain.deliberation import AutoDeliberator, DeliberationDecision
from src.brain.exceptions import BrainError, BrainErrorKind, redact_secrets
from src.brain.intent_router import SKILL_CLASS_BY_TOOL_NAME, IntentRouter, is_provider_error
from src.brain.memory import ConversationMemory

if TYPE_CHECKING:
    from src.brain.claude_client import ClaudeBrain
    from src.brain.codex_client import CodexBrain
    from src.brain.gemini_client import GeminiBrain
    from src.brain.ollama_client import OllamaBrain
    from src.brain.openai_client import OpenAIBrain
    from src.tools.skill_manager import SkillManager

# Pregunta exacta requerida cuando se abre el selector interactivo de motor.
ENGINE_SELECTOR_PROMPT: str = "¿Qué motor de inteligencia desea utilizar, señor?"

# Etiquetas legibles de proveedor por cada modo de motor.
PROVIDER_DISPLAY_NAME: dict[str, str] = {
    "OLLAMA": "Ollama",
    "OLLAMA_UNCENSORED": "Ollama",
    "GEMINI": "Gemini",
    "CLAUDE": "Claude",
    "GPT": "GPT",
    "CODEX": "Codex",
}

# Alias admitidos para `set_mode_by_choice`. El alias "ollama" apunta siempre al modo local estándar.
_MODE_ALIASES: dict[str, str] = {
    # AUTO
    "1": "AUTO",
    "auto": "AUTO",
    "automatico": "AUTO",
    "modo auto": "AUTO",
    "modo automatico": "AUTO",
    "deliberacion": "AUTO",
    "inteligente": "AUTO",
    "por defecto": "AUTO",
    "default": "AUTO",
    # OLLAMA (estándar)
    "2": "OLLAMA",
    "ollama": "OLLAMA",
    "local": "OLLAMA",
    "motor local": "OLLAMA",
    "cerebro local": "OLLAMA",
    "ollama estandar": "OLLAMA",
    "ollama normal": "OLLAMA",
    "ollama censurado": "OLLAMA",
    "ollama censored": "OLLAMA",
    "ollama_censored": "OLLAMA",
    "estandar": "OLLAMA",
    "qwen": "OLLAMA",
    # OLLAMA_UNCENSORED (alternativo)
    "3": "OLLAMA_UNCENSORED",
    "ollama_uncensored": "OLLAMA_UNCENSORED",
    "ollama uncensored": "OLLAMA_UNCENSORED",
    "uncensored": "OLLAMA_UNCENSORED",
    "ollama sin censura": "OLLAMA_UNCENSORED",
    "modo sin censura": "OLLAMA_UNCENSORED",
    "sin censura": "OLLAMA_UNCENSORED",
    "ollama alternativo": "OLLAMA_UNCENSORED",
    "modelo alternativo": "OLLAMA_UNCENSORED",
    "local alternativo": "OLLAMA_UNCENSORED",
    "abliterated": "OLLAMA_UNCENSORED",
    # GEMINI
    "4": "GEMINI",
    "gemini": "GEMINI",
    "google": "GEMINI",
    "google gemini": "GEMINI",
    "modo gemini": "GEMINI",
    # CLAUDE
    "5": "CLAUDE",
    "claude": "CLAUDE",
    "anthropic": "CLAUDE",
    "claude sonnet": "CLAUDE",
    "sonnet": "CLAUDE",
    "modo claude": "CLAUDE",
    # GPT
    "6": "GPT",
    "gpt": "GPT",
    "openai": "GPT",
    "chatgpt": "GPT",
    "chat gpt": "GPT",
    "gpt 5": "GPT",
    "gpt 54": "GPT",
    "gpt 4o": "GPT",
    "modo gpt": "GPT",
    # CODEX
    "7": "CODEX",
    "codex": "CODEX",
    "openai codex": "CODEX",
    "codex cli": "CODEX",
    "modo codex": "CODEX",
    "motor codex": "CODEX",
    "cerebro codex": "CODEX",
}


def _normalize_choice_text(text: str) -> str:
    """
    Normaliza texto eliminando tildes, puntuación y espacios redundantes,
    conservando guiones bajos cuando forman parte del identificador.
    """
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text.lower().strip())
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    punctuation_without_underscore = string.punctuation.replace("_", "") + "¿¡«»“”"
    cleaned = no_accents.translate(str.maketrans(punctuation_without_underscore, " " * len(punctuation_without_underscore)))
    return " ".join(cleaned.split())


@dataclass
class TurnResult:
    """
    Resultado completo y auditable de procesar una solicitud a través de `BrainManager`.
    """

    response: str
    global_mode: str
    effective_mode: str
    final_provider: Optional[str]
    effective_model: Optional[str]
    selection_source: str
    selection_reason: str
    fallback_info: Optional[str] = None
    skill_name: Optional[str] = None
    skill_class: Optional[str] = None
    diagnostic_line: str = ""
    is_error: bool = False


class BrainManager:
    """
    Coordinador Multi-Brain de J.A.R.V.I.S.
    Gestiona el modo global, selecciones One-Shot, deliberación automática (`AUTO`),
    prioridad de habilidades locales y cadena acotada de fallbacks.
    """

    def __init__(
        self,
        initial_mode: str = DEFAULT_BRAIN_MODE,
        *,
        ollama_brain: Optional[OllamaBrain] = None,
        gemini_brain: Optional[GeminiBrain] = None,
        claude_brain: Optional[ClaudeBrain] = None,
        openai_brain: Optional[OpenAIBrain] = None,
        codex_brain: Optional[CodexBrain] = None,
        deliberator: Optional[AutoDeliberator] = None,
        skill_manager: Optional[SkillManager] = None,
        memory: Optional[ConversationMemory] = None,
        intent_router: Optional[IntentRouter] = None,
        allow_cloud_fallback: bool = BRAIN_ALLOW_CLOUD_FALLBACK,
        lazy_init: bool = True,
    ) -> None:
        # Validamos el modo inicial contra BRAIN_MODES; si es inválido, usamos AUTO de forma segura.
        self.config_warning: Optional[str] = None
        normalized_initial = (initial_mode or "").strip().upper().replace("-", "_").replace(" ", "_")
        if normalized_initial in BRAIN_MODES:
            self.global_mode: str = normalized_initial
        else:
            self.global_mode = "AUTO"
            self.config_warning = (
                f"Modo inicial '{initial_mode}' inválido. Se restablece al valor seguro 'AUTO'."
            )
            print(f"[BrainManager] Aviso: {self.config_warning}")

        # Clientes inyectados o creados bajo demanda (lazy_init evita instanciar clientes que aún no se usan)
        self._ollama: Optional[OllamaBrain] = ollama_brain
        self._gemini: Optional[GeminiBrain] = gemini_brain
        self._claude: Optional[ClaudeBrain] = claude_brain
        self._openai: Optional[OpenAIBrain] = openai_brain
        self._codex: Optional[CodexBrain] = codex_brain

        if not lazy_init:
            _ = self.ollama_brain
            _ = self.gemini_brain
            _ = self.claude_brain
            _ = self.openai_brain
            _ = self.codex_brain

        self.deliberator: AutoDeliberator = deliberator or AutoDeliberator(
            codex_ready_fn=lambda: bool(getattr(self.codex_brain, "is_ready_for_auto", lambda: False)())
        )
        self.skill_manager: Optional[SkillManager] = skill_manager
        self.memory: Optional[ConversationMemory] = memory
        self._intent_router: Optional[IntentRouter] = intent_router
        self.allow_cloud_fallback: bool = allow_cloud_fallback

        # Estado de la solicitud actual / última solicitud procesada
        self.effective_mode: str = self.global_mode
        self.effective_model: Optional[str] = None
        self.selection_reason: str = f"Modo global inicial: {self.global_mode}"
        self.selection_source: str = "GLOBAL"
        self.fallback_info: Optional[str] = None
        self.final_provider: Optional[str] = None
        self.last_deliberation: Optional[DeliberationDecision] = None
        self.awaiting_mode_choice: bool = False

    # ------------------------------------------------------------------
    # Propiedades de acceso diferido a los motores
    # ------------------------------------------------------------------
    @property
    def ollama_brain(self) -> OllamaBrain:
        if self._ollama is None:
            from src.brain.ollama_client import OllamaBrain as _OllamaBrain

            self._ollama = _OllamaBrain()
        return self._ollama

    @property
    def gemini_brain(self) -> GeminiBrain:
        if self._gemini is None:
            from src.brain.gemini_client import GeminiBrain as _GeminiBrain

            self._gemini = _GeminiBrain()
        return self._gemini

    @property
    def claude_brain(self) -> ClaudeBrain:
        if self._claude is None:
            from src.brain.claude_client import ClaudeBrain as _ClaudeBrain

            self._claude = _ClaudeBrain()
        return self._claude

    @property
    def openai_brain(self) -> OpenAIBrain:
        if self._openai is None:
            from src.brain.openai_client import OpenAIBrain as _OpenAIBrain

            self._openai = _OpenAIBrain()
        return self._openai

    @property
    def codex_brain(self) -> CodexBrain:
        if self._codex is None:
            from src.brain.codex_client import CodexBrain as _CodexBrain

            self._codex = _CodexBrain()
        return self._codex

    @property
    def intent_router(self) -> Optional[IntentRouter]:
        if self._intent_router is None and self.skill_manager is not None:
            # No forzamos la carga de Ollama/Gemini solo para construir el enrutador de manifiestos:
            # detect_local_skill() solo inspecciona el catálogo de herramientas.
            self._intent_router = IntentRouter(
                llm=self._ollama,  # type: ignore[arg-type]
                tools=self.skill_manager.get_skills_manifest(),
                memory=self.memory,
                fallback_llm=self._gemini,
            )
        return self._intent_router

    # ------------------------------------------------------------------
    # Selección y validación de modos
    # ------------------------------------------------------------------
    @staticmethod
    def resolve_mode_alias(choice: str) -> Optional[str]:
        """
        Resuelve un nombre o alias de modo a su identificador canónico en `BRAIN_MODES`.
        Tolera mayúsculas, tildes, signos de puntuación y espacios extras.
        """
        if not isinstance(choice, str):
            return None
        norm = _normalize_choice_text(choice)
        if not norm:
            return None

        # Coincidencia directa en el diccionario de alias
        if norm in _MODE_ALIASES:
            return _MODE_ALIASES[norm]

        upper_underscored = norm.upper().replace(" ", "_")
        if upper_underscored in BRAIN_MODES:
            return upper_underscored

        # Limpieza de prefijos conversacionales comunes al responder al selector
        # (ej. "quiero usar claude por favor", "pon modo gemini", "elijo ollama")
        stripped = re.sub(
            r"^(?:por\s+favor\s+|quiero\s+|prefiero\s+|elijo\s+|selecciona\s+|seleccionar\s+|"
            r"usa\s+|usar\s+|utiliza\s+|utilizar\s+|cambia\s+a\s+|cambiar\s+a\s+|pon\s+|poner\s+|"
            r"activa\s+|activar\s+|modo\s+|motor\s+|cerebro\s+)+",
            "",
            norm,
        ).strip()
        stripped = re.sub(r"\s+(?:por\s+favor|senor)$", "", stripped).strip()

        if stripped in _MODE_ALIASES:
            return _MODE_ALIASES[stripped]
        if stripped.upper().replace(" ", "_") in BRAIN_MODES:
            return stripped.upper().replace(" ", "_")

        return None

    def set_mode_by_choice(self, choice: str) -> tuple[bool, Optional[str], str]:
        """
        Cambia `global_mode` a partir de la elección del usuario o un alias válido.

        Returns:
            tuple[bool, Optional[str], str]: `(exito, modo_canonico, mensaje_respuesta)`
        """
        resolved = self.resolve_mode_alias(choice)
        if resolved is None:
            valid_list = ", ".join(BRAIN_MODES)
            return (
                False,
                None,
                f"Modo '{choice}' no reconocido, señor. Los modos disponibles son: {valid_list}.",
            )

        self.global_mode = resolved
        self.effective_mode = resolved
        self.selection_source = "COMMAND"
        self.selection_reason = f"Modo global cambiado manualmente a {resolved}."
        self.awaiting_mode_choice = False
        return (
            True,
            resolved,
            f"Modo de inteligencia establecido en {resolved}, señor.",
        )

    @staticmethod
    def is_selector_open_request(user_text: str) -> bool:
        """
        Detecta si el usuario está solicitando abrir el selector de motor sin haber indicado cuál.
        No se activa si el usuario simplemente pregunta qué es un motor ("¿Qué es Gemini?").
        """
        norm = _normalize_choice_text(user_text)
        if not norm:
            return False

        exact_triggers = {
            "modo",
            "cambiar modo",
            "cambia de modo",
            "cambiar de modo",
            "cambiar motor",
            "cambia de motor",
            "cambiar de motor",
            "cambiar cerebro",
            "cambia de cerebro",
            "cambiar de cerebro",
            "seleccionar motor",
            "selecciona motor",
            "seleccionar cerebro",
            "elegir motor",
            "elegir cerebro",
            "selector de motor",
            "selector de cerebro",
            "que motores tienes",
            "que cerebros tienes",
        }
        cleaned = re.sub(r"\b(?:jarvis|por\s+favor|senor)\b", "", norm).strip()
        cleaned = " ".join(cleaned.split())
        return cleaned in exact_triggers

    def detect_explicit_mode_command(self, user_text: str) -> Optional[tuple[bool, Optional[str], str]]:
        """
        Detecta si el texto del usuario es una orden explícita para cambiar el modo global
        (ej. "cambia a modo Claude", "activar modo automático", "pasar a modo GPT").
        No confunde menciones dentro de preguntas normales ("¿Qué es Claude?").
        """
        if not isinstance(user_text, str) or not user_text.strip():
            return None

        if self.is_selector_open_request(user_text):
            self.awaiting_mode_choice = True
            return (True, None, ENGINE_SELECTOR_PROMPT)

        norm = _normalize_choice_text(user_text)
        cleaned = re.sub(r"^(?:jarvis\s+)?(?:por\s+favor\s+)?", "", norm).strip()

        # Solo interpretamos como cambio global cuando hay un verbo de cambio de modo/motor
        # y la frase completa es la orden de cambio (sin una consulta técnica adjunta).
        patterns = (
            r"^(?:cambia|cambiar|pasa|pasar|pon|poner|establece|establecer|fija|fijar|activa|activar)\s+"
            r"(?:el\s+|al\s+|a\s+)?(?:modo|motor|cerebro)\s+(?:a\s+)?(.+?)(?:\s+por\s+favor|\s+senor)?$",
            r"^(?:cambia|cambiar|pasa|pasar)\s+(?:a|al)\s+(.+?)(?:\s+por\s+favor|\s+senor)?$",
            r"^(?:usa|utiliza)\s+siempre\s+(?:el\s+)?(?:modo\s+|motor\s+)?(.+?)(?:\s+por\s+favor|\s+senor)?$",
        )
        for pattern in patterns:
            match = re.match(pattern, cleaned)
            if match:
                candidate = match.group(1).strip()
                # Si contiene demasiadas palabras, probablemente es una consulta completa y no un cambio de modo
                if len(candidate.split()) <= 4:
                    resolved = self.resolve_mode_alias(candidate)
                    if resolved is not None:
                        return self.set_mode_by_choice(resolved)

        return None

    def extract_one_shot_override(self, user_text: str) -> tuple[Optional[str], str]:
        """
        Detecta si el usuario solicitó puntualmente (One-Shot) responder ESTA consulta con un motor
        específico, sin cambiar `self.global_mode`.

        Ejemplos que SÍ activan One-Shot:
        - "Con GPT: explica qué es un decorador en Python"
        - "Usando Claude analiza esta función"
        - "Responde con Gemini cuál es la capital de Australia"
        - "Pregúntale a GPT cómo optimizar SQL"

        Ejemplos que NO activan One-Shot (menciones informativas):
        - "¿Qué es Gemini y quién lo creó?"
        - "¿Cuál es la diferencia entre Claude y GPT?"
        - "¿Cómo funciona Ollama en Windows?"
        """
        if not isinstance(user_text, str) or not user_text.strip():
            return None, user_text

        raw = user_text.strip()

        # Mapa de nombres de motor reconocibles en prefijos/sufijos One-Shot
        engine_token_pattern = (
            r"(?P<engine>"
            r"ollama\s+sin\s+censura|ollama\s+uncensored|ollama_uncensored|ollama\s+alternativo|modo\s+sin\s+censura|"
            r"ollama\s+estandar|ollama|google\s+gemini|gemini|anthropic|claude|"
            r"openai\s+codex|codex\s+cli|codex|openai|chatgpt|gpt"
            r")"
        )

        # Patrón 1: Prefijo explícito ("Con GPT: <consulta>", "Usá Codex para revisar este archivo", "Responde con Gemini <consulta>")
        prefix_regex = re.compile(
            rf"^(?:jarvis[\s,]+)?(?:por\s+favor[\s,]+)?"
            rf"(?:(?:responde|contesta|analiza|explica|resuelve|consulta|pregunta(?:le)?)\s+)?"
            rf"(?:con|usando|mediante|via|vía|en|a|usa|usá|utiliza|utilizá)\s+(?:el\s+motor\s+|el\s+cerebro\s+)?"
            rf"{engine_token_pattern}"
            rf"(?:\s+para\s+|\s*[:,;-]\s*|\s+)(?P<query>.+)$",
            flags=re.IGNORECASE | re.DOTALL,
        )
        match = prefix_regex.match(raw)
        if match:
            engine_raw = match.group("engine")
            remaining_query = match.group("query").strip()
            resolved = self.resolve_mode_alias(engine_raw)
            if resolved and resolved != "AUTO" and len(remaining_query) >= 2:
                # Evitamos falsos positivos como "con GPT o con Claude cuál es mejor" si empieza con conjunción comparativa
                norm_rem = _normalize_choice_text(remaining_query)
                if not norm_rem.startswith(("y ", "o ", "como funciona", "que diferencia")):
                    return resolved, remaining_query

        # Patrón 2: Sufijo explícito ("<consulta>, responde con Claude" / "<consulta> usando GPT")
        suffix_regex = re.compile(
            rf"^(?P<query>.+?)\s*(?:,\s*|\s+)"
            rf"(?:responde|respondeme|contestame|procesalo|hazlo)?\s*"
            rf"(?:con|usando|mediante|via|vía)\s+(?:el\s+motor\s+|el\s+cerebro\s+)?"
            rf"{engine_token_pattern}\s*[.?!]*$",
            flags=re.IGNORECASE | re.DOTALL,
        )
        match_suffix = suffix_regex.match(raw)
        if match_suffix:
            engine_raw = match_suffix.group("engine")
            base_query = match_suffix.group("query").strip()
            norm_base = _normalize_choice_text(base_query)
            # Descartamos preguntas comparativas del tipo "diferencia entre Claude y GPT" o "conectar con Gemini"
            comparative_markers = (
                "diferencia entre",
                "comparar",
                "compara",
                "mejor que",
                "hablar con",
                "conectar con",
                "que pasa con",
            )
            if not any(marker in norm_base for marker in comparative_markers):
                resolved = self.resolve_mode_alias(engine_raw)
                if resolved and resolved != "AUTO" and len(base_query) >= 3:
                    return resolved, base_query

        return None, raw

    def plan_request(self, user_text: str) -> tuple[str, str, str, str]:
        """
        Determina qué motor debe atender `user_text` respetando la jerarquía:
        1. One-Shot puntual (sin modificar `self.global_mode`).
        2. Modo global manual (`OLLAMA`, `OLLAMA_UNCENSORED`, `GEMINI`, `CLAUDE`, `GPT`).
        3. Deliberación automática cuando `self.global_mode == "AUTO"`.

        Returns:
            tuple[str, str, str, str]: `(effective_mode, cleaned_query, selection_source, selection_reason)`
        """
        one_shot_mode, cleaned_query = self.extract_one_shot_override(user_text)
        if one_shot_mode is not None:
            reason = f"Selección puntual One-Shot solicitada por el usuario ({one_shot_mode}); modo global sigue en {self.global_mode}."
            self.effective_mode = one_shot_mode
            self.selection_source = "ONE_SHOT"
            self.selection_reason = reason
            return one_shot_mode, cleaned_query, "ONE_SHOT", reason

        if self.global_mode != "AUTO":
            reason = f"Modo global fijado manualmente en {self.global_mode}."
            self.effective_mode = self.global_mode
            self.selection_source = "GLOBAL"
            self.selection_reason = reason
            return self.global_mode, user_text, "GLOBAL", reason

        decision = self.deliberator.deliberate(user_text)
        self.last_deliberation = decision
        self.effective_mode = decision.target_mode
        self.selection_source = "AUTO"
        self.selection_reason = f"AUTO ({decision.complexity} / {decision.intent_category}): {decision.reason}"
        return decision.target_mode, user_text, "AUTO", self.selection_reason

    # ------------------------------------------------------------------
    # Cadena centralizada y acotada de Fallbacks (sin ciclos)
    # ------------------------------------------------------------------
    def get_fallback_chain(self, primary_mode: str) -> list[str]:
        """
        Devuelve la lista ordenada y sin ciclos de modos a intentar para una solicitud.

        Política inicial:
        - CODEX -> GEMINI -> OLLAMA (cuando se permite fallback)
        - CLAUDE -> GEMINI -> OLLAMA
        - GPT -> GEMINI -> OLLAMA
        - GEMINI -> OLLAMA (nunca vuelve a GEMINI, evitando ciclos Gemini -> Ollama -> Gemini)
        - OLLAMA_UNCENSORED -> (OllamaBrain ya cae internamente 1 vez a OLLAMA estándar si falta el modelo;
          si el daemon local está caído y se permite cloud fallback, pasa a GEMINI)
        - OLLAMA -> GEMINI (si se permite cloud fallback)
        """
        mode = (primary_mode or "OLLAMA").upper()
        if mode == "CODEX":
            chain = ["CODEX", "GEMINI", "OLLAMA"] if self.allow_cloud_fallback else ["CODEX", "OLLAMA"]
        elif mode == "CLAUDE":
            chain = ["CLAUDE", "GEMINI", "OLLAMA"]
        elif mode == "GPT":
            chain = ["GPT", "GEMINI", "OLLAMA"]
        elif mode == "GEMINI":
            chain = ["GEMINI", "OLLAMA"]
        elif mode == "OLLAMA_UNCENSORED":
            chain = ["OLLAMA_UNCENSORED", "GEMINI"] if self.allow_cloud_fallback else ["OLLAMA_UNCENSORED"]
        else:
            chain = ["OLLAMA", "GEMINI"] if self.allow_cloud_fallback else ["OLLAMA"]

        # Deduplicación defensiva preservando orden para impedir cualquier ciclo
        deduped: list[str] = []
        for item in chain:
            if item not in deduped:
                deduped.append(item)
        return deduped

    def _invoke_single_mode(
        self,
        mode: str,
        prompt: str,
        system_prompt: str,
        *,
        voice_mode: bool = False,
    ) -> tuple[str, str, Optional[str], Optional[str]]:
        """
        Invoca un modo concreto con `raise_on_error=True` para capturar `BrainError` estructurado.

        Returns:
            tuple[str, str, Optional[str], Optional[str]]:
                `(response_text, provider_name, confirmed_model, internal_fallback_note)`
        """
        max_tokens = VOICE_MAX_TOKENS if voice_mode else TEXT_MAX_TOKENS

        if mode in ("OLLAMA", "OLLAMA_UNCENSORED"):
            brain = self.ollama_brain
            override_model: Optional[str] = None
            if mode == "OLLAMA_UNCENSORED":
                override_model = getattr(brain, "uncensored_model_name", OLLAMA_MODEL_UNCENSORED)
            try:
                text = brain.generate_response(
                    prompt,
                    system_prompt=system_prompt,
                    model_override=override_model,
                    num_predict=max_tokens,
                    raise_on_error=True,
                )
            except TypeError:
                # Compatibilidad con mocks simples en pruebas que solo aceptan (prompt, system_prompt=...)
                text = brain.generate_response(prompt, system_prompt=system_prompt)
            if is_provider_error(text):
                err = getattr(brain, "last_error", None) or BrainError(
                    provider="Ollama",
                    kind=BrainErrorKind.DAEMON_OFFLINE,
                    user_message=str(text),
                    model=override_model or getattr(brain, "model_name", OLLAMA_MODEL_CENSORED),
                )
                raise err
            confirmed_model = getattr(brain, "last_used_model", None) or (
                override_model or getattr(brain, "model_name", None)
            )
            note = getattr(brain, "last_fallback_note", None)
            return text, "Ollama", confirmed_model, note

        if mode == "GEMINI":
            brain_g = self.gemini_brain
            try:
                text = brain_g.generate_response(
                    prompt,
                    system_prompt=system_prompt,
                    max_output_tokens=max_tokens,
                    raise_on_error=True,
                )
            except TypeError:
                text = brain_g.generate_response(prompt, system_prompt=system_prompt)
            if is_provider_error(text):
                err = getattr(brain_g, "last_error", None) or BrainError(
                    provider="Gemini",
                    kind=BrainErrorKind.NETWORK,
                    user_message=str(text),
                    model=getattr(brain_g, "model_name", None),
                )
                raise err
            confirmed_model = getattr(brain_g, "last_used_model", None) or getattr(brain_g, "model_name", None)
            note = getattr(brain_g, "last_fallback_note", None)
            return text, "Gemini", confirmed_model, note

        if mode == "CLAUDE":
            brain_c = self.claude_brain
            try:
                text = brain_c.generate_response(
                    prompt,
                    system_prompt=system_prompt,
                    max_output_tokens=max_tokens,
                    raise_on_error=True,
                )
            except TypeError:
                text = brain_c.generate_response(prompt, system_prompt=system_prompt)
            if is_provider_error(text):
                err = getattr(brain_c, "last_error", None) or BrainError(
                    provider="Claude",
                    kind=BrainErrorKind.NETWORK,
                    user_message=str(text),
                    model=getattr(brain_c, "model_name", None),
                )
                raise err
            confirmed_model = getattr(brain_c, "last_used_model", None) or getattr(brain_c, "model_name", None)
            note = getattr(brain_c, "last_fallback_note", None)
            return text, "Claude", confirmed_model, note

        if mode == "GPT":
            brain_o = self.openai_brain
            try:
                text = brain_o.generate_response(
                    prompt,
                    system_prompt=system_prompt,
                    max_output_tokens=max_tokens,
                    raise_on_error=True,
                )
            except TypeError:
                text = brain_o.generate_response(prompt, system_prompt=system_prompt)
            if is_provider_error(text):
                err = getattr(brain_o, "last_error", None) or BrainError(
                    provider="GPT",
                    kind=BrainErrorKind.NETWORK,
                    user_message=str(text),
                    model=getattr(brain_o, "model_name", None),
                )
                raise err
            confirmed_model = getattr(brain_o, "last_used_model", None) or getattr(brain_o, "model_name", None)
            note = getattr(brain_o, "last_fallback_note", None)
            return text, "GPT", confirmed_model, note

        if mode == "CODEX":
            brain_x = self.codex_brain
            try:
                text = brain_x.generate_response(
                    prompt,
                    system_prompt=system_prompt,
                    raise_on_error=True,
                )
            except TypeError:
                text = brain_x.generate_response(prompt, system_prompt=system_prompt)
            if is_provider_error(text):
                err = getattr(brain_x, "last_error", None) or BrainError(
                    provider="Codex",
                    kind=BrainErrorKind.EXECUTION,
                    user_message=str(text),
                    model=getattr(brain_x, "last_used_model", "codex-cli"),
                )
                raise err
            confirmed_model = getattr(brain_x, "last_used_model", None) or getattr(brain_x, "model_name", "codex-cli")
            note = getattr(brain_x, "last_fallback_note", None) or getattr(brain_x, "last_git_summary", None)
            return text, "Codex", confirmed_model, note

        raise BrainError(
            provider=mode,
            kind=BrainErrorKind.INVALID_REQUEST,
            user_message=f"Modo de motor '{mode}' desconocido.",
        )

    @staticmethod
    def format_brain_diagnostic(provider: Optional[str], model: Optional[str]) -> str:
        """
        Genera la etiqueta de diagnóstico estándar `[Cerebro: <Proveedor> / <modelo>]`.
        Si el modelo exacto no está confirmado, no lo inventa y muestra solo el proveedor.
        """
        if not provider:
            return "[Cerebro: No disponible]"
        if model and str(model).strip():
            return f"[Cerebro: {provider} / {str(model).strip()}]"
        return f"[Cerebro: {provider}]"

    @staticmethod
    def format_skill_diagnostic(skill_class: str) -> str:
        """Genera la etiqueta de diagnóstico para habilidades locales `[Skill Local: <Clase>]`."""
        return f"[Skill Local: {skill_class}]"

    def process_turn(
        self,
        user_text: str,
        *,
        voice_mode: bool = False,
        sfx: Optional[Any] = None,
    ) -> TurnResult:
        """
        Procesa un turno completo respetando el orden estricto de prioridades:
        1. Respuesta pendiente al selector de motor (si `awaiting_mode_choice` está activo)
           o comando explícito para cambiar/consultar el modo.
        2. Detección y ejecución prioritaria de habilidades locales de Windows (sin llamadas cloud).
        3. Planificación del motor (One-Shot > Modo Global > Deliberación AUTO).
        4. Consulta al motor de IA con política centralizada de fallbacks y registro en memoria.
        """
        self.fallback_info = None
        self.final_provider = None
        self.effective_model = None

        raw_input = (user_text or "").strip()
        if not raw_input:
            return TurnResult(
                response="A su disposición, señor.",
                global_mode=self.global_mode,
                effective_mode=self.global_mode,
                final_provider=None,
                effective_model=None,
                selection_source="EMPTY",
                selection_reason="Entrada vacía.",
            )

        # 0. Si el selector estaba esperando una respuesta puntual del usuario, intentamos resolverla.
        if self.awaiting_mode_choice:
            resolved = self.resolve_mode_alias(raw_input)
            if resolved is not None:
                _, _, msg = self.set_mode_by_choice(resolved)
                return TurnResult(
                    response=msg,
                    global_mode=self.global_mode,
                    effective_mode=self.global_mode,
                    final_provider=None,
                    effective_model=None,
                    selection_source="COMMAND",
                    selection_reason=self.selection_reason,
                )
            # Si escribió otra cosa que no es un modo, cerramos el selector y procesamos normalmente
            self.awaiting_mode_choice = False

        # 1. Comandos explícitos de cambio de modo o apertura del selector
        mode_cmd = self.detect_explicit_mode_command(raw_input)
        if mode_cmd is not None:
            _, _, msg = mode_cmd
            return TurnResult(
                response=msg,
                global_mode=self.global_mode,
                effective_mode=self.global_mode,
                final_provider=None,
                effective_model=None,
                selection_source="COMMAND",
                selection_reason=self.selection_reason,
            )

        # Comandos hablados/escritos de limpieza de memoria conversacional
        norm_input = _normalize_choice_text(raw_input)
        clear_memory_triggers = (
            "olvida lo que hablamos",
            "reiniciar contexto",
            "olvidar contexto",
            "olvida todo",
            "borrar memoria",
            "reinicia memoria",
        )
        if any(trigger in norm_input for trigger in clear_memory_triggers):
            if self.memory is not None:
                self.memory.clear()
            if sfx is not None and hasattr(sfx, "confirm"):
                try:
                    sfx.confirm()
                except Exception:
                    pass
            return TurnResult(
                response="Entendido, señor. He reiniciado la memoria de nuestra conversación.",
                global_mode=self.global_mode,
                effective_mode=self.global_mode,
                final_provider=None,
                effective_model=None,
                selection_source="COMMAND",
                selection_reason="Reinicio explícito de memoria conversacional.",
            )

        # 2. Detección y ejecución prioritaria de habilidades locales de Windows (antes de llamar al LLM)
        router = self.intent_router
        if router is not None and self.skill_manager is not None:
            local_decision = router.detect_local_skill(raw_input)
            if local_decision is not None:
                action = local_decision.get("action")
                skill_class = str(local_decision.get("skill_class") or "LocalSkill")
                diag = self.format_skill_diagnostic(skill_class)
                self.final_provider = "Skill Local"
                self.selection_source = "LOCAL_SKILL"
                self.selection_reason = f"Habilidad local prioritaria ({skill_class})."

                if self.memory is not None:
                    self.memory.add_message("user", raw_input)

                if action == "clarify":
                    reply_text = str(local_decision.get("response") or "Por favor especifique los detalles, señor.")
                else:
                    tool_name = str(local_decision.get("tool_name") or "")
                    params = local_decision.get("parameters") or {}
                    if sfx is not None and hasattr(sfx, "confirm"):
                        try:
                            sfx.confirm()
                        except Exception:
                            pass
                    reply_text = self.skill_manager.execute_skill(tool_name, **params)

                if self.memory is not None and reply_text:
                    self.memory.add_message("assistant", reply_text)

                return TurnResult(
                    response=reply_text,
                    global_mode=self.global_mode,
                    effective_mode=self.effective_mode,
                    final_provider="Skill Local",
                    effective_model=None,
                    selection_source="LOCAL_SKILL",
                    selection_reason=self.selection_reason,
                    skill_name=local_decision.get("tool_name"),
                    skill_class=skill_class,
                    diagnostic_line=diag,
                )

        # 3. Selección del motor para la consulta conversacional (One-Shot > Global > AUTO)
        target_mode, cleaned_query, source, reason = self.plan_request(raw_input)

        # Registramos el mensaje del usuario en la memoria conversacional
        base_prompt = JARVIS_SYSTEM_PROMPT if voice_mode else JARVIS_TEXT_SYSTEM_PROMPT
        if self.memory is not None:
            system_prompt_with_ctx = self.memory.inject_context(base_prompt)
            self.memory.add_message("user", raw_input)
        else:
            system_prompt_with_ctx = base_prompt

        # 4. Ejecución con política acotada de fallbacks
        response_text, provider, model_used, fallback_summary, failed = self._generate_with_fallback_policy(
            primary_mode=target_mode,
            prompt=cleaned_query,
            system_prompt=system_prompt_with_ctx,
            voice_mode=voice_mode,
        )

        if not failed and self.memory is not None and response_text:
            self.memory.add_message("assistant", response_text)

        diag_line = self.format_brain_diagnostic(provider, model_used) if not failed else "[Cerebro: Sin disponibilidad]"
        return TurnResult(
            response=response_text,
            global_mode=self.global_mode,
            effective_mode=target_mode,
            final_provider=provider,
            effective_model=model_used,
            selection_source=source,
            selection_reason=reason,
            fallback_info=fallback_summary,
            diagnostic_line=diag_line,
            is_error=failed,
        )

    def _generate_with_fallback_policy(
        self,
        primary_mode: str,
        prompt: str,
        system_prompt: str,
        *,
        voice_mode: bool = False,
    ) -> tuple[str, Optional[str], Optional[str], Optional[str], bool]:
        """
        Ejecuta la consulta en `primary_mode` y aplica la cadena acotada de fallbacks
        únicamente cuando el tipo de error lo permite.
        """
        chain = self.get_fallback_chain(primary_mode)
        errors_seen: list[BrainError] = []

        for idx, candidate_mode in enumerate(chain):
            try:
                text, provider, confirmed_model, internal_note = self._invoke_single_mode(
                    candidate_mode,
                    prompt,
                    system_prompt,
                    voice_mode=voice_mode,
                )
                self.final_provider = provider
                self.effective_model = confirmed_model

                fallback_notes: list[str] = []
                if idx > 0 and errors_seen:
                    first_err = errors_seen[0]
                    fallback_notes.append(
                        f"Fallback activado: {first_err.provider} ({first_err.kind.value}) -> {provider}"
                        + (f" / {confirmed_model}" if confirmed_model else "")
                    )
                if internal_note:
                    fallback_notes.append(internal_note)

                self.fallback_info = " | ".join(fallback_notes) if fallback_notes else None
                return text, provider, confirmed_model, self.fallback_info, False

            except Exception as raw_exc:
                if isinstance(raw_exc, BrainError):
                    brain_err = raw_exc
                else:
                    brain_err = BrainError(
                        provider=PROVIDER_DISPLAY_NAME.get(candidate_mode, candidate_mode),
                        kind=BrainErrorKind.UNKNOWN,
                        user_message="Fallo inesperado al consultar el motor.",
                        detail=redact_secrets(str(raw_exc)),
                    )
                errors_seen.append(brain_err)

                # Si el error NO admite fallback entre proveedores (p. ej. solicitud inválida o
                # stream parcialmente emitido), detenemos la cadena de inmediato.
                if not brain_err.allows_provider_fallback:
                    break

        # Todos los motores aplicables fallaron: conservamos `self.global_mode` intacto
        # y devolvemos un mensaje controlado y breve con el problema principal.
        self.final_provider = None
        self.effective_model = None
        primary_problem = errors_seen[0].user_message if errors_seen else "Ningún motor de IA está disponible."
        attempted = ", ".join(err.provider for err in errors_seen) if errors_seen else primary_mode
        self.fallback_info = f"Fallaron los motores consultados ({attempted})."
        controlled_msg = (
            f"{primary_problem} "
            f"(Motores verificados: {attempted}. Las habilidades locales siguen operativas, señor.)"
        )
        return redact_secrets(controlled_msg), None, None, self.fallback_info, True
