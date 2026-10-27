"""Clasifica mensajes y propone llamadas a herramientas sin ejecutarlas."""

import json
import re
import string
import unicodedata
from typing import Any, Mapping, Protocol, Sequence
from config.prompts import JARVIS_SYSTEM_PROMPT
from src.brain.memory import ConversationMemory

# Mapa entre el identificador interno de cada herramienta y el nombre de su clase Skill.
# Permite emitir diagnósticos consistentes como `[Skill Local: TimeSkill]`.
SKILL_CLASS_BY_TOOL_NAME: dict[str, str] = {
    "system_time": "TimeSkill",
    "app_launcher": "AppLauncherSkill",
    "system_info": "SystemInfoSkill",
    "web_search": "WebSearchSkill",
    "activate_protocol": "StarkProtocolSkill",
    "volume_control": "VolumeControlSkill",
    "media_control": "MediaControlSkill",
    "clipboard_manager": "ClipboardSkill",
    "system_power": "SystemPowerSkill",
}

# Mensajes degradados que devuelven los cerebros locales o cloud cuando no pueden responder.
# ¿Por qué se filtran? Si el modelo local Ollama falla por falta de VRAM o timeout,
# devuelve un mensaje de error amigable. El router debe detectar que fue un fallo técnico
# para activar el fallback, en lugar de creer que el LLM quiso hablarle de eso al usuario.
# Se comparan solo contra texto plano (sin JSON) para no confundir una respuesta legítima.
_PROVIDER_ERROR_MARKERS: tuple[str, ...] = (
    "no responde",
    "no está disponible en este momento",
    "no pude conectar con gemini",
    "gemini no está configurado",
    "gemini devolvió una respuesta vacía",
    "no aceptaron la solicitud",
    "no pudo generar una respuesta",
    "claude no está configurado",
    "claude devolvió una respuesta vacía",
    "no pude conectar con claude",
    "gpt no está configurado",
    "gpt devolvió una respuesta vacía",
    "no pude conectar con openai",
    "no está instalado en ollama",
    "tardó demasiado en responder",
    "superó el tiempo de espera",
    "agotó la cuota",
    "límite de uso",
    "clave de api",
)


def is_provider_error(raw_response: object) -> bool:
    """
    Determina si la salida es vacía o coincide con un mensaje de fallo de un proveedor.
    
    Si retorna True, el IntentRouter sabe que el LLM primario falló y procede a invocar
    inmediatamente al LLM de respaldo (Gemini).
    """
    if not isinstance(raw_response, str) or not raw_response.strip():
        return True
    # Si contiene una llave '{', es un intento de JSON estructurado, no un error genérico del cliente
    if "{" in raw_response:
        return False
    lowered = raw_response.lower()
    return any(marker in lowered for marker in _PROVIDER_ERROR_MARKERS)


class LanguageModel(Protocol):
    """Interfaz mínima que ya implementan OllamaBrain y GeminiBrain."""

    def generate_response(self, prompt: str, system_prompt: str | None = None) -> str:
        """Solicita texto al modelo usando un prompt de sistema opcional."""


class IntentRouter:
    """Convierte texto del usuario en una respuesta de chat o una propuesta de herramienta."""

    def __init__(
        self,
        llm: LanguageModel,
        tools: Sequence[object],
        memory: ConversationMemory | None = None,
        fallback_llm: LanguageModel | None = None,
    ):
        # El enrutador solo decide; el componente que lo llama conserva el control de ejecución.
        self.llm = llm
        self.fallback_llm = fallback_llm
        self.tools = self._normalize_tools(tools)
        self._tools_by_name = {tool["name"]: tool for tool in self.tools}
        self.memory = memory

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Normaliza texto en minúsculas sin acentos ni signos de puntuación para reglas locales."""
        if not text:
            return ""
        nfkd = unicodedata.normalize("NFKD", text.lower())
        no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
        clean = no_accents.translate(str.maketrans("", "", string.punctuation + "¿¡«»“”"))
        return " ".join(clean.split())

    def detect_local_skill(self, user_text: str) -> dict[str, Any] | None:
        """
        Detecta de forma determinista y sin llamadas a LLM si la orden del usuario corresponde
        directamente a una de las habilidades locales de Windows registradas en el manifiesto.

        ¿Por qué existe este método?
        1. Evita llamadas cloud o locales innecesarias para órdenes inequívocas como "¿qué hora es?",
           "sube el volumen" o "abre la calculadora".
        2. Garantiza que las habilidades locales funcionen al 100% incluso si Ollama está apagado
           y no hay claves API configuradas.
        3. Si una orden local es incompleta/ambigua (ej. "abre una aplicación" sin decir cuál),
           devuelve una solicitud de aclaración breve (`action == "clarify"`).
        """
        if not isinstance(user_text, str) or not user_text.strip():
            return None

        raw = user_text.strip()
        norm = self._normalize_text(raw)
        if not norm:
            return None

        def _make_tool_call(tool_name: str, parameters: dict[str, Any]) -> dict[str, Any] | None:
            if tool_name not in self._tools_by_name:
                return None
            return {
                "action": "call_tool",
                "tool_name": tool_name,
                "parameters": parameters,
                "skill_class": SKILL_CLASS_BY_TOOL_NAME.get(tool_name, tool_name),
            }

        # 1. StarkProtocolSkill ('activate_protocol')
        if "activate_protocol" in self._tools_by_name:
            if any(p in norm for p in ("protocolo de trabajo", "protocolo trabajo", "modo programacion", "modo desarrollo")):
                return _make_tool_call("activate_protocol", {"protocol": "trabajo"})
            if any(p in norm for p in ("protocolo diagnostico", "protocolo de diagnostico", "reporte de estado")):
                return _make_tool_call("activate_protocol", {"protocol": "diagnostico"})
            if any(p in norm for p in ("protocolo silencio", "protocolo de silencio", "modo sigilo")):
                return _make_tool_call("activate_protocol", {"protocol": "silencio"})

        # 2. TimeSkill ('system_time')
        if "system_time" in self._tools_by_name:
            time_phrases = (
                "que hora es",
                "que hora tienes",
                "dime la hora",
                "decime la hora",
                "hora actual",
                "que fecha es hoy",
                "que dia es hoy",
                "dime la fecha",
                "fecha y hora",
            )
            if any(p in norm for p in time_phrases) or norm in ("hora", "fecha", "la hora"):
                return _make_tool_call("system_time", {})

        # 3. SystemInfoSkill ('system_info')
        if "system_info" in self._tools_by_name:
            sys_phrases = (
                "estado del sistema",
                "uso de cpu",
                "uso del procesador",
                "cuanta ram",
                "memoria ram libre",
                "estado de la ram",
                "como esta el procesador",
                "estado del hardware",
            )
            if any(p in norm for p in sys_phrases):
                return _make_tool_call("system_info", {})

        # 4. VolumeControlSkill ('volume_control')
        if "volume_control" in self._tools_by_name:
            if re.search(r"\b(sube|subir|aumenta|aumentar)\b.*\b(volumen|audio)\b", norm):
                return _make_tool_call("volume_control", {"action": "subir"})
            if re.search(r"\b(baja|bajar|reduce|reducir|disminuye)\b.*\b(volumen|audio)\b", norm):
                return _make_tool_call("volume_control", {"action": "bajar"})
            if re.search(r"\b(silencia|silenciar|mutea|mutear)\b.*\b(volumen|audio|sonido)\b", norm):
                return _make_tool_call("volume_control", {"action": "silenciar"})

        # 5. MediaControlSkill ('media_control')
        if "media_control" in self._tools_by_name:
            if re.search(r"\b(pausa|pausar)\b.*\b(musica|cancion|reproduccion)\b", norm):
                return _make_tool_call("media_control", {"action": "pausar"})
            if re.search(r"\b(reproduce|reproducir|reanuda|reanudar)\b.*\b(musica|cancion|reproduccion)\b", norm):
                return _make_tool_call("media_control", {"action": "reproducir"})
            if re.search(r"\b(siguiente|proxima|adelanta)\b.*\b(cancion|pista)\b", norm):
                return _make_tool_call("media_control", {"action": "siguiente"})
            if re.search(r"\b(anterior|previa)\b.*\b(cancion|pista)\b|\bcancion anterior\b", norm):
                return _make_tool_call("media_control", {"action": "anterior"})

        # 6. ClipboardSkill ('clipboard_manager')
        if "clipboard_manager" in self._tools_by_name:
            if re.search(r"\b(lee|leer|mostrar|muestra|que hay en)\b.*\bportapapeles\b", norm):
                return _make_tool_call("clipboard_manager", {"action": "leer"})
            copy_match = re.search(
                r"(?:copia|copiar|guarda|guardar)\s+(?:en\s+el|al)?\s*portapapeles\s*[:,-]?\s*(.*)",
                raw,
                flags=re.IGNORECASE,
            )
            if copy_match:
                text_to_copy = copy_match.group(1).strip().strip('"').strip("'")
                if not text_to_copy:
                    return {
                        "action": "clarify",
                        "response": "Por favor indique qué texto desea copiar al portapapeles, señor.",
                        "skill_class": "ClipboardSkill",
                    }
                return _make_tool_call("clipboard_manager", {"action": "copiar", "text": text_to_copy})

        # 7. SystemPowerSkill ('system_power') — conserva controles de seguridad:
        # solo admite bloquear pantalla o cancelar apagado, nunca apagar/reiniciar sin control.
        if "system_power" in self._tools_by_name:
            if re.search(r"\b(bloquea|bloquear)\b.*\b(pantalla|equipo|pc|computadora|sesion)\b", norm):
                return _make_tool_call("system_power", {"action": "bloquear"})
            if re.search(r"\b(cancela|cancelar|aborta|abortar)\b.*\bapagado\b", norm):
                return _make_tool_call("system_power", {"action": "cancelar_apagado"})

        # 8. WebSearchSkill ('web_search')
        if "web_search" in self._tools_by_name:
            yt_match = re.search(
                r"\b(?:busca|buscar|buscame)\s+(?:en\s+youtube\s+)(.+?)(?:\s+en\s+youtube)?$",
                raw,
                flags=re.IGNORECASE,
            )
            if not yt_match:
                yt_match = re.search(
                    r"\b(?:busca|buscar|buscame)\s+(.+?)\s+en\s+youtube\s*$",
                    raw,
                    flags=re.IGNORECASE,
                )
            if yt_match:
                query = yt_match.group(1).strip().strip('"').strip("'")
                if query:
                    return _make_tool_call("web_search", {"query": query, "platform": "youtube"})

            web_match = re.search(
                r"\b(?:busca|buscar|buscame)\s+(?:en\s+(?:google|internet|la\s+web)\s+)(.+)$",
                raw,
                flags=re.IGNORECASE,
            )
            if not web_match:
                web_match = re.search(
                    r"\b(?:busca|buscar|buscame)\s+(.+?)\s+en\s+(?:google|internet|la\s+web)\s*$",
                    raw,
                    flags=re.IGNORECASE,
                )
            if web_match:
                query = web_match.group(1).strip().strip('"').strip("'")
                if query:
                    return _make_tool_call("web_search", {"query": query})

            if norm in ("busca en google", "buscar en google", "busca en internet", "busca en youtube", "buscar en youtube"):
                return {
                    "action": "clarify",
                    "response": "Por favor especifique qué término desea buscar en el navegador, señor.",
                    "skill_class": "WebSearchSkill",
                }

        # 9. AppLauncherSkill ('app_launcher')
        if "app_launcher" in self._tools_by_name:
            if norm in ("abre", "abrir", "abre una aplicacion", "abrir aplicacion", "abre un programa"):
                return {
                    "action": "clarify",
                    "response": "Por favor especifique qué aplicación desea abrir, señor.",
                    "skill_class": "AppLauncherSkill",
                }
            app_match = re.match(
                r"^(?:jarvis\s+)?(?:por\s+favor\s+)?(?:abre|abrir|inicia|iniciar|ejecuta|ejecutar)\s+(?:el\s+|la\s+)?(.+?)(?:\s+por\s+favor)?$",
                norm,
            )
            if app_match:
                candidate_app = app_match.group(1).strip()
                # Evitamos capturar frases que son de otra categoría (ej. "inicia protocolo de trabajo" ya se filtró arriba)
                ignored_targets = ("sesion", "conversacion", "selector", "modo")
                if candidate_app and not any(candidate_app.startswith(t) for t in ignored_targets):
                    return _make_tool_call("app_launcher", {"app_name": candidate_app})

        return None

    @staticmethod
    def _normalize_tools(tools: Sequence[object]) -> list[dict[str, Any]]:
        """Convierte skills u objetos tipo manifiesto a una estructura segura y uniforme."""
        normalized: list[dict[str, Any]] = []
        seen_names: set[str] = set()

        for tool in tools:
            # El proyecto entrega diccionarios desde get_skills_manifest, pero también aceptamos skills.
            if isinstance(tool, Mapping):
                name = tool.get("name")
                description = tool.get("description", "")
                parameters = tool.get("parameters", {})
            else:
                name = getattr(tool, "name", None)
                description = getattr(tool, "description", "")
                parameters = getattr(tool, "parameters", {})

            # Solo publicamos entradas con nombres utilizables y únicos.
            if not isinstance(name, str) or not name.strip() or name in seen_names:
                continue
            if not isinstance(description, str):
                description = ""
            if not isinstance(parameters, Mapping):
                parameters = {}

            normalized.append(
                {
                    "name": name,
                    "description": description,
                    "parameters": dict(parameters),
                }
            )
            seen_names.add(name)

        return normalized

    def analyze_intent(self, user_text: str) -> dict:
        """Devuelve chat o una llamada validada contra el manifiesto disponible."""
        fallback = {"action": "chat", "response": user_text}
        if not isinstance(user_text, str) or not user_text.strip():
            return fallback

        system_prompt = self._build_system_prompt()
        raw_response: str | None = None
        try:
            # Separamos las instrucciones del texto para reducir la posibilidad de mezclar roles.
            raw_response = self.llm.generate_response(user_text, system_prompt=system_prompt)
            if is_provider_error(raw_response):
                raise RuntimeError("LLM primario no disponible o devolvió respuesta no válida.")
        except Exception as primary_error:
            if self.fallback_llm is not None:
                print(f"[IntentRouter] Primario falló ({primary_error}). Derivando a fallback...")
                try:
                    raw_response = self.fallback_llm.generate_response(user_text, system_prompt=system_prompt)
                except Exception as fallback_error:
                    print(f"[IntentRouter] Fallback también falló ({fallback_error}). Retornando chat por defecto.")
                    return fallback
            else:
                print(f"[IntentRouter] Primario falló ({primary_error}) y no hay fallback configurado.")
                return fallback

        try:
            payload = self._parse_json_response(raw_response)
            return self._validate_payload(payload, fallback)
        except Exception as e:
            # El modelo pudo ignorar el formato y contestar en texto plano: lo usamos directamente
            # para evitar una segunda llamada al LLM (que duplicaría la latencia del turno).
            plain = self._plain_text_decision(raw_response)
            if plain is not None:
                return plain
            print(f"[IntentRouter] Info/Fallback: {e} | Crudo: '{raw_response}'")
            return fallback

    def _plain_text_decision(self, raw_response: object) -> dict | None:
        """
        Recuperador inteligente cuando el LLM responde en lenguaje natural directo sin envolver en JSON.
        
        ¿Por qué existe este método?
        Modelos ligeros locales (como Llama 3 o Qwen con bajo num_predict) a veces devuelven
        directamente la respuesta hablada (ej. "Enseguida abro el navegador, señor.") ignorando el JSON.
        En lugar de lanzar un error y hacer una SEGUNDA llamada lenta al LLM (que duplicaría la latencia
        y arruinaría la fluidez por voz), si detectamos texto inteligible lo aceptamos como chat directo.
        Si coincide con el nombre exacto de una herramienta sin parámetros obligatorios, la dispara.
        """
        if not isinstance(raw_response, str) or "{" in raw_response or is_provider_error(raw_response):
            return None
        text = re.sub(r"```[\s\S]*?```|`", "", raw_response).strip().strip('"').strip()
        if sum(c.isalnum() for c in text) < 2:
            return None
        tool = self._tools_by_name.get(text)
        if tool is not None:
            declared = tool["parameters"]
            if not self._get_required_parameter_names(declared, self._get_parameter_names(declared)):
                return {"action": "call_tool", "tool_name": text, "parameters": {}}
            return None
        return {"action": "chat", "response": text}

    def _build_system_prompt(self) -> str:
        """Personalidad compartida + catálogo como datos + contrato de salida JSON cerrado."""
        manifest = json.dumps(self.tools, ensure_ascii=False, separators=(",", ":"))

        # Inyectamos el historial conversacional reciente si la memoria está presente
        history_context = ""
        if self.memory is not None:
            history_text = self.memory.get_context_prompt()
            # Si hay mensajes previos en la memoria, los agregamos como contexto para el modelo
            if history_text and "sin mensajes" not in history_text:
                history_context = f"\n\n{history_text}\nUsa este historial para resolver referencias y responder con los datos y preferencias del usuario."

        protocol_hint = ""
        if "activate_protocol" in self._tools_by_name:
            protocol_hint = (
                "- Si el usuario pide un protocolo o modo (trabajo, programación, diagnóstico, reporte de estado, "
                "silencio, sigilo), usa 'activate_protocol' con 'protocol' ('trabajo', 'diagnostico' o 'silencio').\n"
            )

        return (
            f"{JARVIS_SYSTEM_PROMPT}\n\n"
            "FORMATO DE SALIDA: devuelve ÚNICAMENTE un objeto JSON válido en una sola línea, sin texto adicional ni Markdown. "
            "El campo 'response' contiene exactamente lo que dirás en voz alta.\n"
            "INSTRUCCIONES CLAVE:\n"
            "- Si el usuario pide la hora o fecha actual, usa SIEMPRE 'system_time'.\n"
            "- Si el usuario pide abrir o iniciar una app (calculadora, bloc de notas, etc.), usa SIEMPRE 'app_launcher' con 'app_name'.\n"
            "- Si el usuario pregunta por hardware, CPU o memoria RAM, usa SIEMPRE 'system_info'.\n"
            "- Si el usuario pide buscar en internet o YouTube, usa SIEMPRE 'web_search' con 'query'.\n"
            "- Si el usuario pide subir, bajar o silenciar el volumen, usa 'volume_control' con 'action'.\n"
            "- Si el usuario pide pausar, reproducir, siguiente o anterior canción, usa 'media_control' con 'action'.\n"
            "- Si el usuario pide leer o copiar en el portapapeles, usa 'clipboard_manager' con 'action' (y 'text' si copia).\n"
            "- Si el usuario pide bloquear la pantalla o cancelar apagado, usa 'system_power' con 'action'.\n"
            f"{protocol_hint}"
            "- Si es charla general, saludo o preguntas sobre el usuario o la conversación, responde en formato chat.\n\n"
            "EJEMPLOS DE FORMATO OBLIGATORIO:\n"
            'Herramienta con argumento: {"action":"call_tool","tool_name":"web_search","parameters":{"query":"música clásica"}}\n'
            'Herramienta de apertura: {"action":"call_tool","tool_name":"app_launcher","parameters":{"app_name":"calculadora"}}\n'
            'Herramienta sin argumento: {"action":"call_tool","tool_name":"system_time","parameters":{}}\n'
            'Chat general: {"action":"chat","response":"En línea, señor. ¿Qué necesitamos?"}\n\n'
            f"Catálogo de herramientas disponibles: {manifest}"
            f"{history_context}"
        )

    @staticmethod
    def _parse_json_response(raw_response: str) -> dict[str, Any]:
        """Quita fences, busca objetos JSON completos y repara salidas truncadas."""
        if not isinstance(raw_response, str):
            raise ValueError("La respuesta del modelo no es texto.")
        raw_response = raw_response.lstrip("\ufeff")

        # Algunos modelos envuelven el JSON en un bloque Markdown pese a la instrucción.
        fenced_blocks = re.findall(
            r"```(?:json)?\s*(.*?)```", raw_response, flags=re.IGNORECASE | re.DOTALL
        )
        candidates = fenced_blocks + [raw_response]

        for candidate in candidates:
            for json_text in IntentRouter._extract_json_objects(candidate):
                try:
                    parsed = json.loads(json_text, object_pairs_hook=IntentRouter._reject_duplicate_keys)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(parsed, dict):
                    return parsed

        # Reparador 1: cerramos comillas/llaves pendientes de un objeto cortado por num_predict.
        repaired = IntentRouter._repair_truncated_json(raw_response)
        if repaired is not None:
            return repaired

        # Reparador 2: extraemos el texto de 'response' de un chat truncado aunque el resto esté roto.
        truncated_chat = re.search(
            r'\{\s*"action"\s*:\s*"chat"\s*,\s*"response"\s*:\s*"(.+)',
            raw_response, flags=re.DOTALL,
        )
        if truncated_chat:
            recovered = IntentRouter._read_json_string_prefix(truncated_chat.group(1))
            if recovered:
                return {"action": "chat", "response": recovered}

        raise ValueError("No se encontró un objeto JSON válido en la respuesta.")

    @staticmethod
    def _repair_truncated_json(text: str) -> dict[str, Any] | None:
        """
        Reparador algorítmico de JSON cortado prematuramente por límite de tokens (num_predict).
        
        ¿Cómo funciona?
        1. Localiza la primera llave de apertura '{'.
        2. Recorre carácter por carácter distinguiendo si está dentro de una cadena literal o no
           (respetando caracteres de escape '\\"').
        3. Mantiene una pila (stack) de llaves y corchetes abiertos pendientes de cierre.
        4. Si el texto se cortó dentro de una cadena, le añade una comilla de cierre '"'.
        5. Cierra todas las llaves y corchetes pendientes en orden inverso al de la pila (LIFO).
        6. Reintenta 'json.loads()'. Si se recupera un diccionario válido, evita un descarte innecesario.
        """
        start = text.find("{")
        if start == -1:
            return None
        fragment = text[start:]
        stack: list[str] = []
        inside_string = False
        escaped = False
        for character in fragment:
            if inside_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    inside_string = False
                continue
            if character == '"':
                inside_string = True
            elif character in "{[":
                stack.append("}" if character == "{" else "]")
            elif character in "}]" and stack:
                stack.pop()
        if not stack:
            return None  # Estaba balanceado: ya se intentó (y falló) el parseo normal.
        if escaped:
            fragment = fragment[:-1]
        if inside_string:
            fragment += '"'
        fragment = fragment.rstrip().rstrip(",")
        try:
            parsed = json.loads(
                fragment + "".join(reversed(stack)),
                object_pairs_hook=IntentRouter._reject_duplicate_keys,
            )
        except (json.JSONDecodeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _read_json_string_prefix(text: str) -> str:
        """
        Lector de cadenas JSON con escape seguro para extracciones directas de regex.
        
        Si el LLM generó '{"action":"chat","response":"Hola señor\\n¿Cómo está?',
        este método procesa los caracteres escapados (saltos de línea '\\n', comillas '\\"')
        y extrae el contenido lingüístico sin romper el intérprete.
        """
        chars: list[str] = []
        escaped = False
        for character in text:
            if escaped:
                chars.append({"n": " ", "t": " ", "r": "", '"': '"', "\\": "\\", "/": "/"}.get(character, character))
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                break
            else:
                chars.append(character)
        return "".join(chars).strip()

    @staticmethod
    def _extract_json_objects(text: str) -> list[str]:
        """Extrae objetos balanceando llaves sin confundirse con llaves dentro de cadenas."""
        objects: list[str] = []
        start: int | None = None
        depth = 0
        inside_string = False
        escaped = False

        for index, character in enumerate(text):
            if inside_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    inside_string = False
                continue

            if character == '"':
                inside_string = True
            elif character == "{":
                if depth == 0:
                    start = index
                depth += 1
            elif character == "}" and depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    objects.append(text[start : index + 1])
                    start = None

        return objects

    @staticmethod
    def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        """Rechaza claves repetidas para evitar interpretaciones distintas del mismo JSON."""
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Clave JSON duplicada: {key}")
            result[key] = value
        return result

    def _validate_payload(self, payload: dict[str, Any], fallback: dict[str, str]) -> dict:
        """Valida la acción, el nombre de herramienta y sus argumentos permitidos."""
        action = payload.get("action")
        if action == "chat":
            # Cada acción tiene un formato cerrado; los campos extra pueden confundir al ejecutor.
            if set(payload) != {"action", "response"}:
                return fallback
            response = payload.get("response")
            if isinstance(response, str) and response.strip():
                return {"action": "chat", "response": response.strip()}
            return fallback

        # Si el modelo devuelve el nombre de la herramienta directamente en 'action' (ej. {"action": "web_search"})
        if action in self._tools_by_name and "tool_name" not in payload:
            tool_name = action
            action = "call_tool"
            parameters = payload.get("parameters", {})
            payload = {"action": "call_tool", "tool_name": tool_name, "parameters": parameters if isinstance(parameters, dict) else {}}

        if action != "call_tool":
            return fallback
        if set(payload) != {"action", "tool_name", "parameters"}:
            return fallback

        tool_name = payload.get("tool_name")
        parameters = payload.get("parameters")
        if not isinstance(tool_name, str) or tool_name not in self._tools_by_name:
            return fallback
        if not isinstance(parameters, dict) or any(not isinstance(key, str) for key in parameters):
            return fallback

        # Solo se admiten claves declaradas por la skill; evita argumentos alucinados o inesperados.
        declared = self._tools_by_name[tool_name]["parameters"]
        declared_keys = self._get_parameter_names(declared)
        if any(key not in declared_keys for key in parameters):
            return fallback
        required_keys = self._get_required_parameter_names(declared, declared_keys)
        if not required_keys.issubset(parameters):
            return fallback

        return {"action": "call_tool", "tool_name": tool_name, "parameters": parameters}

    @staticmethod
    def _get_parameter_names(declared: Mapping[str, Any]) -> set[str]:
        """Acepta tanto esquemas JSON Schema como mapas sencillos usados por BaseSkill."""
        properties = declared.get("properties")
        if isinstance(properties, Mapping):
            return {key for key in properties if isinstance(key, str)}
        return {key for key in declared if isinstance(key, str)}

    @staticmethod
    def _get_required_parameter_names(
        declared: Mapping[str, Any], declared_keys: set[str]
    ) -> set[str]:
        """Lee campos requeridos de JSON Schema o asume requeridos los parámetros de BaseSkill."""
        properties = declared.get("properties")
        required = declared.get("required")
        if isinstance(properties, Mapping):
            if isinstance(required, list):
                return {key for key in required if isinstance(key, str)}
            return set()
        return declared_keys


def generate_with_fallback(
    prompt: str,
    primary_llm: LanguageModel,
    fallback_llm: LanguageModel,
    system_prompt: str | None = None,
) -> str:
    """
    Intenta generar respuesta con el LLM primario (Ollama). Si este falla o no responde,
    deriva instantáneamente al LLM secundario (Gemini) sin arrojar excepciones fatales
    en el hilo principal.
    """
    try:
        response = primary_llm.generate_response(prompt, system_prompt=system_prompt)
        if is_provider_error(response):
            raise RuntimeError("LLM primario no disponible o devolvió mensaje de error.")
        return response
    except Exception as primary_error:
        print(f"[LLM Routing] Fallo en primario ({primary_error}). Derivando a fallback...")
        try:
            fallback_response = fallback_llm.generate_response(prompt, system_prompt=system_prompt)
            if not fallback_response or not fallback_response.strip():
                return "Lo siento señor, el sistema de respaldo no devolvió contenido."
            return fallback_response
        except Exception as fallback_error:
            print(f"[LLM Routing] Fallo también en fallback ({fallback_error}).")
            return "Lo siento señor, ninguno de mis cerebros responde en este momento."
