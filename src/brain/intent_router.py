"""Clasifica mensajes y propone llamadas a herramientas sin ejecutarlas."""

import json
import re
from typing import Any, Mapping, Protocol, Sequence


class LanguageModel(Protocol):
    """Interfaz mínima que ya implementan OllamaBrain y GeminiBrain."""

    def generate_response(self, prompt: str, system_prompt: str | None = None) -> str:
        """Solicita texto al modelo usando un prompt de sistema opcional."""


class IntentRouter:
    """Convierte texto del usuario en una respuesta de chat o una propuesta de herramienta."""

    def __init__(self, llm: LanguageModel, tools: Sequence[object]):
        # El enrutador solo decide; el componente que lo llama conserva el control de ejecución.
        self.llm = llm
        self.tools = self._normalize_tools(tools)
        self._tools_by_name = {tool["name"]: tool for tool in self.tools}

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
        try:
            # Separamos las instrucciones del texto para reducir la posibilidad de mezclar roles.
            raw_response = self.llm.generate_response(user_text, system_prompt=system_prompt)
            payload = self._parse_json_response(raw_response)
            return self._validate_payload(payload, fallback)
        except Exception as e:
            # Ante errores del modelo, JSON inválido o estructura inesperada, mostramos diagnóstico y devolvemos fallback seguro
            print(f"[IntentRouter] Info/Fallback: {e} | Crudo: '{raw_response if 'raw_response' in locals() else None}'")
            return fallback

    def _build_system_prompt(self) -> str:
        """Incluye el catálogo como datos y define un contrato de salida cerrado."""
        manifest = json.dumps(self.tools, ensure_ascii=False, separators=(",", ":"))
        return (
            "Eres el enrutador de intenciones de J.A.R.V.I.S. Devuelve ÚNICAMENTE un objeto JSON válido sin texto adicional ni markdown.\n"
            "INSTRUCCIONES CLAVE:\n"
            "- Si el usuario pide la hora o fecha actual, usa SIEMPRE 'system_time'.\n"
            "- Si el usuario pide abrir o iniciar una app (calculadora, bloc de notas, etc.), usa SIEMPRE 'app_launcher' con 'app_name'.\n"
            "- Si el usuario pregunta por hardware, CPU o memoria RAM, usa SIEMPRE 'system_info'.\n"
            "- Si el usuario pide buscar en internet o YouTube, usa SIEMPRE 'web_search' con 'query'.\n"
            "- Si es charla general o saludo, responde en formato chat.\n\n"
            "EJEMPLOS DE FORMATO OBLIGATORIO:\n"
            'Herramienta con argumento: {"action":"call_tool","tool_name":"web_search","parameters":{"query":"música clásica"}}\n'
            'Herramienta de apertura: {"action":"call_tool","tool_name":"app_launcher","parameters":{"app_name":"calculadora"}}\n'
            'Herramienta sin argumento: {"action":"call_tool","tool_name":"system_time","parameters":{}}\n'
            'Chat general: {"action":"chat","response":"Hola señor, ¿en qué puedo ayudarle?"}\n\n'
            f"Catálogo de herramientas disponibles: {manifest}"
        )

    @staticmethod
    def _parse_json_response(raw_response: str) -> dict[str, Any]:
        """Quita fences y busca objetos JSON completos aunque haya texto alrededor."""
        if not isinstance(raw_response, str):
            raise ValueError("La respuesta del modelo no es texto.")

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

        raise ValueError("No se encontró un objeto JSON válido en la respuesta.")

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
