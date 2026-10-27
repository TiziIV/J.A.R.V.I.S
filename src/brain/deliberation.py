"""
Deliberador automático de intención y complejidad para el modo `AUTO` de J.A.R.V.I.S.

Justificación técnica junior:
En lugar de elegir un motor buscando palabras sueltas (lo que provocaría falsos positivos como
activar el modelo alternativo ante "¿Qué es el hacking ético?" o cambiar de motor al preguntar
"¿Qué es Gemini?"), este componente evalúa tres dimensiones combinadas:
1. Intención comunicativa (saludo, pregunta conceptual breve, educación defensiva, arquitectura,
   depuración multi-módulo, análisis profundo o petición explícita de estilo alternativo).
2. Complejidad estructural (extensión, número de cláusulas/requisitos, verbos de diseño y análisis
   de múltiples etapas).
3. Reglas de seguridad (una petición de estilo alternativo jamás desactiva los controles frente
   a solicitudes dañinas o ilegales).
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import string
import unicodedata
from typing import Callable, Optional

from config.settings import (
    AUTO_ALLOW_UNCENSORED,
    AUTO_CLOUD_PROVIDER,
    AUTO_CODE_PROVIDER,
    AUTO_LONGFORM_PROVIDER,
    CODEX_ALLOW_AUTO,
    CODEX_ENABLED,
)


def normalize_for_analysis(text: str) -> str:
    """Normaliza texto a minúsculas sin tildes ni puntuación superflua conservando espacios."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text.lower())
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    clean = no_accents.translate(str.maketrans("", "", string.punctuation + "¿¡«»“”"))
    return " ".join(clean.split())


@dataclass(frozen=True)
class DeliberationDecision:
    """
    Resultado explicable y reproducible del deliberador de motores.

    Attributes:
        target_mode: Modo efectivo elegido ('OLLAMA', 'OLLAMA_UNCENSORED', 'GEMINI', 'CLAUDE', 'GPT').
        complexity: Nivel estimado ('baja', 'media', 'alta').
        intent_category: Categoría semántica detectada.
        reason: Motivo operativo breve para diagnóstico.
        source: Origen de la decisión ('AUTO', 'GLOBAL', 'ONE_SHOT').
        safety_blocked: True si se rechazó usar un estilo alternativo por políticas de seguridad.
    """

    target_mode: str
    complexity: str
    intent_category: str
    reason: str
    source: str = "AUTO"
    safety_blocked: bool = False


# Expresiones de saludo o charla breve cotidiana -> siempre local estándar.
_GREETING_PATTERNS: tuple[str, ...] = (
    "hola",
    "buenos dias",
    "buenas tardes",
    "buenas noches",
    "como estas",
    "que tal",
    "todo bien",
    "gracias",
    "muchas gracias",
    "quien eres",
    "como te llamas",
)

# Patrones educativos/académicos o defensivos: aunque contengan términos de seguridad informática,
# su intención es formativa y debe atenderse con el modelo estándar sin activar OLLAMA_UNCENSORED.
_EDUCATIONAL_SECURITY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(que es|como funciona|explica|definicion de|concepto de|diferencia entre)\b.*\b(hacking|exploit|rastreo|phishing|malware|pentesting|ciberseguridad|vulnerabilidad)\b"),
    re.compile(r"\b(hacking etico|seguridad informatica|seguridad defensiva|auditoria de seguridad|proteger|prevenir|mitigar|defensa contra)\b"),
)

# Patrones de solicitudes dañinas/ilegales reales: NUNCA deben activar el modelo alternativo
# ni eludir las restricciones de seguridad aunque el usuario pida "sin censura".
_UNSAFE_HARMFUL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(crea|genera|escribe|dame|programa)\b.*\b(ransomware|keylogger|troyano|virus|exploit funcional|robo de contrasenas|ataque ddos)\b"),
    re.compile(r"\b(como hackear|como vulnerar|como robar|invadir sin permiso|exfiltrar datos)\b"),
    re.compile(r"\b(ignora todas las reglas de seguridad|desactiva las medidas de seguridad|salta la seguridad)\b"),
)

# Peticiones EXPLÍCITAS de estilo menos restrictivo o modelo alternativo local (en contexto seguro).
_EXPLICIT_ALT_STYLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(usa|utiliza|activa|responde con|emplea)\b.*\b(modelo alternativo|ollama alternativo|modo sin censura|estilo sin censura|estilo menos restrictivo|estilo directo sin filtro|abliterated)\b"),
    re.compile(r"\b(con un estilo menos restrictivo|sin filtro moralizador|sin sermones morales)\b"),
)

# Señales de arquitectura de software, programación multi-módulo o depuración avanzada.
_COMPLEX_ARCH_AND_CODE_TERMS: tuple[str, ...] = (
    "arquitectura de software",
    "microservicios",
    "sistema distribuido",
    "alta disponibilidad",
    "escalabilidad",
    "cqrs",
    "event sourcing",
    "hexagonal",
    "clean architecture",
    "depura estos modulos",
    "varios modulos",
    "multiples modulos",
    "refactoriza",
    "condicion de carrera",
    "deadlock",
    "concurrencia",
    "asincrono",
    "complejidad algoritmica",
    "pipeline de despliegue",
    "diseña la arquitectura",
    "disena la arquitectura",
)

# Señales de análisis profundo, investigación científica/histórica o síntesis multi-documento.
_DEEP_ANALYSIS_TERMS: tuple[str, ...] = (
    "analisis profundo",
    "analiza en profundidad",
    "razonamiento paso a paso",
    "varias etapas",
    "compara exhaustivamente",
    "evalua las ventajas y desventajas",
    "trade offs",
    "tradeoffs",
    "sintetiza estos documentos",
    "multiples documentos",
    "investigacion cientifica",
    "contexto historico detallado",
    "ensayo extenso",
    "informe detallado",
    "estado del arte",
    "revision bibliografica",
)

# Verbos de tarea compleja que combinados con alcance amplio aumentan la complejidad estructural.
_MULTI_STAGE_VERBS: tuple[str, ...] = (
    "disena",
    "arquitectura",
    "implementa",
    "refactoriza",
    "optimiza",
    "compara",
    "analiza",
    "estructura",
    "planifica",
    "sintetiza",
    "evalua",
    "depura",
)

# Señales de tareas de programación, depuración, pruebas o edición de repositorio aptas para CODEX en modo AUTO.
_CODEX_REPO_CODING_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(depura|depurar|refactoriza|refactorizar|corrige|corregir|implementa|implementar|"
        r"crea|crear|agrega|agregar|escribe|escribir|revisa|revisar)\b.*\b"
        r"(archivo|archivos|modulo|modulos|funcion|funciones|clase|clases|prueba|pruebas|"
        r"test|tests|unittest|repositorio|codigo|bug|error|traceback|excepcion)\b"
    ),
    re.compile(r"\b(en\s+src/|en\s+tests/|en\s+config/|main\.py|settings\.py|brain_manager\.py)\b"),
)

# Acciones externas o de alto impacto que AUTO jamás debe delegar automáticamente a Codex.
_HIGH_IMPACT_EXTERNAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(publica|publicar|despliega|desplegar|deploy|git\s+push|borra|borrar|elimina|eliminar|instala|instalar|pip\s+install|npm\s+install)\b"),
)


class AutoDeliberator:
    """
    Componente independiente encargado de deliberar qué motor utilizar cuando `global_mode == "AUTO"`.
    Sus decisiones son deterministas y reproducibles para entradas equivalentes.
    """

    def __init__(
        self,
        default_cloud_provider: str = AUTO_CLOUD_PROVIDER,
        code_cloud_provider: str = AUTO_CODE_PROVIDER,
        longform_cloud_provider: str = AUTO_LONGFORM_PROVIDER,
        allow_uncensored_in_auto: bool = AUTO_ALLOW_UNCENSORED,
        *,
        codex_enabled: bool = CODEX_ENABLED,
        codex_allow_auto: bool = CODEX_ALLOW_AUTO,
        codex_ready_fn: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.default_cloud_provider: str = (default_cloud_provider or "GEMINI").strip().upper()
        self.code_cloud_provider: str = (code_cloud_provider or self.default_cloud_provider).strip().upper()
        self.longform_cloud_provider: str = (longform_cloud_provider or self.default_cloud_provider).strip().upper()
        self.allow_uncensored_in_auto: bool = bool(allow_uncensored_in_auto)
        self.codex_enabled: bool = bool(codex_enabled)
        self.codex_allow_auto: bool = bool(codex_allow_auto)
        self.codex_ready_fn: Optional[Callable[[], bool]] = codex_ready_fn

    def _is_codex_available_for_auto(self) -> bool:
        """
        Verifica si Codex CLI puede ser elegido en modo `AUTO`:
        requiere `codex_enabled`, `codex_allow_auto` y que `codex_ready_fn` confirme
        que la CLI está instalada y autenticada.
        """
        if not self.codex_enabled or not self.codex_allow_auto:
            return False
        if self.codex_ready_fn is None:
            return False
        try:
            return bool(self.codex_ready_fn())
        except Exception:
            return False

    def deliberate(self, user_text: str) -> DeliberationDecision:
        """
        Evalúa la intención, el contexto y la complejidad de `user_text` y devuelve
        la decisión explicable para el modo `AUTO`.
        """
        raw = (user_text or "").strip()
        norm = normalize_for_analysis(raw)
        words = norm.split()
        word_count = len(words)

        if not norm:
            return DeliberationDecision(
                target_mode="OLLAMA",
                complexity="baja",
                intent_category="vacio",
                reason="Entrada vacía o mínima; se mantiene en el motor local estándar.",
                source="AUTO",
            )

        # 1. Verificación prioritaria de seguridad:
        # Si la consulta intenta pedir acciones dañinas o desactivar la seguridad, jamás se deriva
        # al modelo alternativo aunque mencione "sin censura" o "exploit".
        if any(pattern.search(norm) for pattern in _UNSAFE_HARMFUL_PATTERNS):
            return DeliberationDecision(
                target_mode="OLLAMA",
                complexity="baja",
                intent_category="seguridad_protegida",
                reason="Solicitud bloqueada para modelo alternativo por controles de seguridad; se atiende con motor estándar seguro.",
                source="AUTO",
                safety_blocked=True,
            )

        # 2. Consultas educativas o defensivas sobre ciberseguridad (ej. "¿Qué es el hacking ético?"):
        # Se resuelven con el motor estándar (o cloud si son muy extensas), nunca con OLLAMA_UNCENSORED.
        if any(pattern.search(norm) for pattern in _EDUCATIONAL_SECURITY_PATTERNS) and word_count < 35:
            return DeliberationDecision(
                target_mode="OLLAMA",
                complexity="baja",
                intent_category="seguridad_educativa",
                reason="Consulta académica o defensiva sobre seguridad informática; se resuelve con Ollama estándar.",
                source="AUTO",
            )

        # 3. Solicitud explícita de estilo menos restrictivo / modelo alternativo (en contexto seguro):
        if any(pattern.search(norm) for pattern in _EXPLICIT_ALT_STYLE_PATTERNS):
            if self.allow_uncensored_in_auto:
                return DeliberationDecision(
                    target_mode="OLLAMA_UNCENSORED",
                    complexity="media",
                    intent_category="estilo_alternativo",
                    reason="El usuario solicitó explícitamente un estilo menos restrictivo dentro de políticas seguras.",
                    source="AUTO",
                )
            return DeliberationDecision(
                target_mode="OLLAMA",
                complexity="media",
                intent_category="estilo_alternativo_deshabilitado",
                reason="Estilo alternativo deshabilitado en AUTO; se utiliza Ollama estándar.",
                source="AUTO",
            )

        # 4. Saludos y charla casual corta -> Ollama estándar.
        if word_count <= 8 and any(greet in norm for greet in _GREETING_PATTERNS):
            return DeliberationDecision(
                target_mode="OLLAMA",
                complexity="baja",
                intent_category="saludo",
                reason="Saludo o conversación casual breve; ideal para respuesta rápida local.",
                source="AUTO",
            )

        # 5. Evaluación de tareas de programación/repositorio para CODEX (si está habilitado, instalado y autenticado)
        is_repo_coding_task = any(p.search(norm) for p in _CODEX_REPO_CODING_PATTERNS)
        is_high_impact_external = any(p.search(norm) for p in _HIGH_IMPACT_EXTERNAL_PATTERNS)

        if is_repo_coding_task and not is_high_impact_external:
            if self._is_codex_available_for_auto():
                return DeliberationDecision(
                    target_mode="CODEX",
                    complexity="alta",
                    intent_category="programacion_codex",
                    reason="Tarea de programación, depuración o revisión de repositorio delegada a Codex CLI.",
                    source="AUTO",
                )

        # 6. Evaluación de complejidad para motores Cloud vs. Local:
        arch_hits = sum(1 for term in _COMPLEX_ARCH_AND_CODE_TERMS if term in norm)
        deep_hits = sum(1 for term in _DEEP_ANALYSIS_TERMS if term in norm)
        stage_verb_hits = sum(1 for verb in _MULTI_STAGE_VERBS if re.search(rf"\b{re.escape(verb)}\b", norm))
        has_code_block = "```" in raw or raw.count("\n") >= 4
        clause_markers = sum(raw.lower().count(marker) for marker in (",", ";", "1.", "2.", "además", "por un lado", "paso a paso"))

        # Puntaje estructural combinando señales semánticas y sintácticas
        complexity_score = (
            (arch_hits * 3)
            + (deep_hits * 3)
            + (stage_verb_hits * 1)
            + (2 if has_code_block else 0)
            + (2 if word_count >= 35 else (1 if word_count >= 18 else 0))
            + (1 if clause_markers >= 3 else 0)
        )

        if arch_hits >= 1 and (complexity_score >= 4 or word_count >= 10):
            return DeliberationDecision(
                target_mode=self.code_cloud_provider,
                complexity="alta",
                intent_category="arquitectura_software",
                reason="Solicitud de arquitectura de software o programación avanzada multi-módulo.",
                source="AUTO",
            )

        if deep_hits >= 1 and (complexity_score >= 4 or word_count >= 10):
            return DeliberationDecision(
                target_mode=self.longform_cloud_provider,
                complexity="alta",
                intent_category="analisis_profundo",
                reason="Solicitud de análisis profundo, síntesis extensa o investigación de varias etapas.",
                source="AUTO",
            )

        if complexity_score >= 5:
            return DeliberationDecision(
                target_mode=self.default_cloud_provider,
                complexity="alta",
                intent_category="razonamiento_complejo",
                reason="Consulta extensa con múltiples requisitos técnicos y razonamiento multi-etapa.",
                source="AUTO",
            )

        # 6. Por defecto, las preguntas sencillas, explicaciones breves y consultas que incluso
        # mencionen el nombre de un proveedor ("¿Qué es Gemini?") se atienden localmente.
        complexity_label = "media" if complexity_score >= 3 else "baja"
        return DeliberationDecision(
            target_mode="OLLAMA",
            complexity=complexity_label,
            intent_category="consulta_breve",
            reason="Consulta directa o explicación breve que no requiere llamada cloud.",
            source="AUTO",
        )


def deliberate_route(
    user_text: str,
    *,
    default_cloud_provider: Optional[str] = None,
) -> DeliberationDecision:
    """Función auxiliar de conveniencia para deliberar una consulta en modo `AUTO`."""
    deliberator = AutoDeliberator(
        default_cloud_provider=default_cloud_provider or AUTO_CLOUD_PROVIDER,
    )
    return deliberator.deliberate(user_text)
