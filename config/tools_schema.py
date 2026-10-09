"""
Módulo de esquemas y catálogo de herramientas (Tools Schema) para J.A.R.V.I.S.
Define los metadatos, descripciones y parámetros estructurados de todas las herramientas
disponibles en el sistema, además de generar el System Prompt base para Function Calling.
"""

import json
from typing import Any, Dict, List

# Catálogo centralizado de esquemas de herramientas de J.A.R.V.I.S.
# Cada herramienta define su nombre único, descripción detallada y el esquema de parámetros esperados.
TOOLS_SCHEMA: List[Dict[str, Any]] = [
    {
        "name": "system_time",
        "description": "Devuelve la hora y la fecha actual del sistema en formato de lenguaje natural para el asistente de voz.",
        "parameters": {}  # No requiere parámetros adicionales
    },
    {
        "name": "app_launcher",
        "description": "Abre aplicaciones comunes de Windows de manera segura mediante una lista blanca (como bloc de notas, calculadora, navegador o VS Code).",
        "parameters": {
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "Nombre de la aplicación que se desea abrir (ej. 'bloc de notas', 'calculadora', 'code')."
                }
            },
            "required": ["app_name"]
        }
    },
    {
        "name": "system_info",
        "description": "Obtiene información del estado actual del sistema hardware (uso de CPU, memoria RAM, almacenamiento o GPU).",
        "parameters": {
            "type": "object",
            "properties": {
                "info_type": {
                    "type": "string",
                    "description": "Tipo de información de sistema solicitada ('cpu', 'ram', 'gpu', 'general').",
                    "default": "general"
                }
            },
            "required": []
        }
    },
    {
        "name": "web_search",
        "description": "Realiza una búsqueda web en internet para consultar información actualizada, noticias o datos específicos.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Término o pregunta exacta que se desea buscar en la web."
                }
            },
            "required": ["query"]
        }
    }
]


def get_system_instructions_with_tools() -> str:
    """
    Genera y retorna un System Prompt en español que instruye al LLM (Ollama o Gemini)
    a comportarse como J.A.R.V.I.S y a responder estrictamente en formato JSON si detecta
    que el usuario necesita invocar una de las herramientas documentadas en el catálogo.

    Returns:
        str: Prompt de sistema formateado con la personalidad de J.A.R.V.I.S y el catálogo de herramientas.
    """
    # Paso 1: Convertimos la lista de esquemas de herramientas a formato JSON compacto
    # Usamos ensure_ascii=False para mantener las tildes y eñes correctamente en español.
    tools_json = json.dumps(TOOLS_SCHEMA, ensure_ascii=False, separators=(",", ":"))

    # Paso 2: Construimos las instrucciones de sistema combinando la identidad de J.A.R.V.I.S
    # con las reglas estrictas de salida estructurada (JSON) para Function Calling.
    system_prompt = (
        "Eres J.A.R.V.I.S., el asistente virtual inteligente, respetuoso, directo y eficiente de Tony Stark. "
        "Tu objetivo es asistir al usuario en Windows con voz o texto.\n\n"
        "REGLAS CRÍTICAS DE FUNCIONAMIENTO:\n"
        "1. Analiza la consulta del usuario y el catálogo de herramientas disponible.\n"
        "2. Si la consulta requiere una herramienta del sistema, debes responder ÚNICAMENTE con un objeto JSON válido "
        "en el siguiente formato estricto, sin texto adicional ni bloques de marcado markdown:\n"
        '   {"action": "call_tool", "tool_name": "<nombre_de_la_herramienta>", "parameters": {<argumentos_necesarios>}}\n\n'
        "3. Si la consulta es una conversación general o no requiere ninguna herramienta, debes responder ÚNICAMENTE con un objeto JSON válido:\n"
        '   {"action": "chat", "response": "<tu_respuesta_breve_y_natural_en_espanol>"}\n\n'
        "Catálogo de herramientas disponibles en este sistema:\n"
        f"{tools_json}"
    )

    return system_prompt
