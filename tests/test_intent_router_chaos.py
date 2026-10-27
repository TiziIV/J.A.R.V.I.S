"""
Tests de caos para IntentRouter — 25 casos borde de respuestas LLM malformadas.
Garantiza que ninguna excepción no controlada escape de analyze_intent().
"""

import json
import unittest
from unittest.mock import MagicMock, patch
from src.brain.intent_router import IntentRouter
from src.brain.memory import ConversationMemory

# Manifiesto mínimo de tools para todos los tests
TOOLS = [
    {
        "name": "system_time",
        "description": "Hora actual",
        "parameters": {},
    },
    {
        "name": "web_search",
        "description": "Busca en la web",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]


def _make_router(llm_response: str, fallback_response: str | None = None) -> IntentRouter:
    """Crea un IntentRouter con un LLM falso que devuelve el string dado."""
    mock_llm = MagicMock()
    mock_llm.generate_response.return_value = llm_response
    fallback = None
    if fallback_response is not None:
        mock_fallback = MagicMock()
        mock_fallback.generate_response.return_value = fallback_response
        fallback = mock_fallback
    return IntentRouter(llm=mock_llm, tools=TOOLS, fallback_llm=fallback)


class TestIntentRouterChaos(unittest.TestCase):

    # ── Casos 1-5: JSON truncado ──────────────────────────────────────────────

    def test_01_json_truncado_a_la_mitad(self):
        """JSON cortado en la mitad → fallback limpio sin excepción."""
        router = _make_router('{"action":"chat","respon')
        result = router.analyze_intent("hola")
        self.assertIn("action", result)

    def test_02_solo_llave_apertura(self):
        """Solo '{' → fallback."""
        router = _make_router("{")
        result = router.analyze_intent("hola")
        self.assertEqual(result["action"], "chat")

    def test_03_json_anidado_roto(self):
        """Llaves anidadas que no cierran."""
        router = _make_router('{"action":"call_tool","tool_name":"web_search","parameters":{"query":"test"')
        result = router.analyze_intent("busca algo")
        self.assertIn("action", result)

    def test_04_array_en_lugar_de_objeto(self):
        """El LLM devuelve un array JSON en vez de objeto."""
        router = _make_router('[{"action":"chat","response":"hola"}]')
        result = router.analyze_intent("hola")
        self.assertIn("action", result)

    def test_05_json_con_clave_duplicada(self):
        """JSON con clave 'action' duplicada → rechazado por _reject_duplicate_keys → fallback."""
        router = _make_router('{"action":"chat","action":"call_tool","response":"hola"}')
        result = router.analyze_intent("hola")
        self.assertIn("action", result)

    # ── Casos 6-10: Markdown fences ──────────────────────────────────────────

    def test_06_json_en_bloque_markdown_simple(self):
        """JSON envuelto en ```json ... ``` → debe extraerse correctamente."""
        router = _make_router('```json\n{"action":"chat","response":"Hola señor"}\n```')
        result = router.analyze_intent("hola")
        self.assertEqual(result["action"], "chat")
        self.assertIn("response", result)

    def test_07_json_en_bloque_markdown_sin_lenguaje(self):
        """JSON en ``` ... ``` sin indicar lenguaje."""
        router = _make_router('``` {"action":"chat","response":"OK"} ```')
        result = router.analyze_intent("hola")
        self.assertIn("action", result)

    def test_08_texto_antes_y_despues_del_json(self):
        """Texto arbitrario antes y después del JSON válido."""
        router = _make_router('Aquí está mi respuesta: {"action":"chat","response":"Hola"} eso es todo.')
        result = router.analyze_intent("hola")
        self.assertEqual(result["action"], "chat")

    def test_09_markdown_con_texto_extra_en_multiples_lineas(self):
        """Texto multilinea antes del bloque markdown."""
        payload = 'Claro, aquí está:\n```json\n{"action":"chat","response":"Perfecto señor"}\n```\nEspero haber ayudado.'
        router = _make_router(payload)
        result = router.analyze_intent("hola")
        self.assertIn("action", result)

    def test_10_triple_backtick_sin_cerrar(self):
        """Bloque markdown que no se cierra correctamente."""
        router = _make_router('```json\n{"action":"chat","response":"OK"}')
        result = router.analyze_intent("hola")
        # No debe lanzar excepción; puede encontrar el JSON o hacer fallback
        self.assertIn("action", result)

    # ── Casos 11-15: Comillas simples y JSON inválido ─────────────────────────

    def test_11_comillas_simples_en_lugar_de_dobles(self):
        """JSON con comillas simples → inválido en JSON estándar → fallback."""
        router = _make_router("{'action':'chat','response':'hola'}")
        result = router.analyze_intent("hola")
        self.assertIn("action", result)

    def test_12_clave_sin_comillas(self):
        """JSON con claves sin comillas (JS-style) → inválido → fallback."""
        router = _make_router('{action:"chat",response:"hola"}')
        result = router.analyze_intent("hola")
        self.assertIn("action", result)

    def test_13_valor_undefined(self):
        """JSON con 'undefined' como valor → inválido → fallback."""
        router = _make_router('{"action":"chat","response":undefined}')
        result = router.analyze_intent("hola")
        self.assertIn("action", result)

    def test_14_caracteres_de_control_en_string(self):
        """Caracteres de control no escapados dentro del string JSON."""
        router = _make_router('{"action":"chat","response":"hola\x01mundo"}')
        result = router.analyze_intent("hola")
        self.assertIn("action", result)

    def test_15_json_con_null_como_response(self):
        """response: null → no es str con strip → fallback."""
        router = _make_router('{"action":"chat","response":null}')
        result = router.analyze_intent("hola")
        # fallback tiene action=chat y response=user_text
        self.assertEqual(result["action"], "chat")

    # ── Casos 16-20: Respuestas vacías y texto plano ─────────────────────────

    def test_16_respuesta_vacia(self):
        """LLM devuelve string vacío → fallback."""
        router = _make_router("")
        result = router.analyze_intent("hola")
        self.assertIn("action", result)

    def test_17_respuesta_solo_espacios(self):
        """LLM devuelve solo espacios."""
        router = _make_router("   ")
        result = router.analyze_intent("hola")
        self.assertIn("action", result)

    def test_18_texto_plano_sin_json(self):
        """LLM devuelve texto conversacional sin JSON."""
        router = _make_router("Claro, con gusto le ayudo señor.")
        result = router.analyze_intent("hola")
        self.assertEqual(result["action"], "chat")

    def test_19_respuesta_solo_numero(self):
        """LLM devuelve solo un número."""
        router = _make_router("42")
        result = router.analyze_intent("hola")
        self.assertIn("action", result)

    def test_20_respuesta_none_provoca_fallo_en_llm(self):
        """LLM lanza excepción → debe activar fallback o retornar fallback seguro."""
        mock_llm = MagicMock()
        mock_llm.generate_response.side_effect = ConnectionError("red caída")
        router = IntentRouter(llm=mock_llm, tools=TOOLS)
        result = router.analyze_intent("hola")
        self.assertIn("action", result)
        self.assertNotIn("traceback", str(result).lower())

    # ── Casos 21-25: Tool parameters malformados / inexistentes ──────────────

    def test_21_tool_inexistente(self):
        """LLM llama a tool que no existe en el manifiesto → fallback."""
        router = _make_router('{"action":"call_tool","tool_name":"magic_skill","parameters":{}}')
        result = router.analyze_intent("haz magia")
        self.assertEqual(result["action"], "chat")

    def test_22_parametro_no_declarado(self):
        """LLM pasa parámetro no declarado en la tool → fallback."""
        router = _make_router('{"action":"call_tool","tool_name":"web_search","parameters":{"query":"test","extra_field":"oops"}}')
        result = router.analyze_intent("busca test")
        self.assertEqual(result["action"], "chat")

    def test_23_parametro_requerido_ausente(self):
        """LLM omite parámetro requerido 'query' en web_search → fallback."""
        router = _make_router('{"action":"call_tool","tool_name":"web_search","parameters":{}}')
        result = router.analyze_intent("busca algo")
        self.assertEqual(result["action"], "chat")

    def test_24_parameters_no_es_dict(self):
        """LLM envía parameters como lista en vez de dict → fallback."""
        router = _make_router('{"action":"call_tool","tool_name":"system_time","parameters":["a","b"]}')
        result = router.analyze_intent("qué hora es")
        self.assertEqual(result["action"], "chat")

    def test_25_ambos_llm_fallan_retorna_fallback_seguro(self):
        """Primario y fallback fallan → retorno limpio sin excepción."""
        mock_primary = MagicMock()
        mock_primary.generate_response.side_effect = RuntimeError("primario caído")
        mock_fallback = MagicMock()
        mock_fallback.generate_response.side_effect = RuntimeError("fallback caído")
        router = IntentRouter(llm=mock_primary, tools=TOOLS, fallback_llm=mock_fallback)
        result = router.analyze_intent("hola")
        self.assertIn("action", result)
        self.assertIsInstance(result, dict)

    # ── Tests adicionales: entradas de usuario inválidas ──────────────────────

    def test_user_text_vacio_no_llama_al_llm(self):
        """Texto de usuario vacío → retorno inmediato sin llamar al LLM."""
        mock_llm = MagicMock()
        mock_llm.generate_response.return_value = '{"action":"chat","response":"ok"}'
        router = IntentRouter(llm=mock_llm, tools=TOOLS)
        result = router.analyze_intent("")
        mock_llm.generate_response.assert_not_called()
        self.assertIn("action", result)

    def test_user_text_solo_espacios_no_llama_al_llm(self):
        """Texto solo con espacios → fallback inmediato."""
        mock_llm = MagicMock()
        router = IntentRouter(llm=mock_llm, tools=TOOLS)
        result = router.analyze_intent("   ")
        mock_llm.generate_response.assert_not_called()
        self.assertIn("action", result)

    def test_call_tool_action_directo_en_action_field(self):
        """LLM usa el nombre de tool directamente en 'action' (patrón alternativo documentado)."""
        router = _make_router('{"action":"system_time","parameters":{}}')
        result = router.analyze_intent("qué hora es")
        self.assertEqual(result["action"], "call_tool")
        self.assertEqual(result["tool_name"], "system_time")

    def test_chat_con_campos_extra_rechazado(self):
        """Chat con campos extra → fallback (contrato cerrado)."""
        router = _make_router('{"action":"chat","response":"hola","extra":"dato"}')
        result = router.analyze_intent("hola")
        self.assertEqual(result["action"], "chat")

    def test_memory_inyectada_no_rompe_router(self):
        """Router con memoria activa no lanza excepciones."""
        memory = ConversationMemory(max_turns=4)
        memory.add_message("user", "hola")
        memory.add_message("assistant", "Hola señor")
        router = _make_router('{"action":"chat","response":"Hola de nuevo"}')
        router.memory = memory
        result = router.analyze_intent("cómo estás")
        self.assertIn("action", result)

    def test_tools_normalizacion_objeto_con_atributos(self):
        """_normalize_tools acepta objetos con atributos name/description/parameters."""
        obj = MagicMock()
        obj.name = "custom_tool"
        obj.description = "herramienta custom"
        obj.parameters = {}
        mock_llm = MagicMock()
        mock_llm.generate_response.return_value = '{"action":"chat","response":"ok"}'
        router = IntentRouter(llm=mock_llm, tools=[obj])
        self.assertIn("custom_tool", router._tools_by_name)

    def test_timeout_red_activando_fallback(self):
        """Timeout de red en primario → fallback activo devuelve resultado válido."""
        import socket
        mock_primary = MagicMock()
        mock_primary.generate_response.side_effect = socket.timeout("timeout")
        mock_fallback = MagicMock()
        mock_fallback.generate_response.return_value = '{"action":"chat","response":"Fallback OK"}'
        router = IntentRouter(llm=mock_primary, tools=TOOLS, fallback_llm=mock_fallback)
        result = router.analyze_intent("hola")
        self.assertIn("action", result)

    def test_respuesta_json_number_no_dict(self):
        """LLM devuelve JSON numérico (42) → no es dict → fallback."""
        router = _make_router("42")
        result = router.analyze_intent("hola")
        self.assertEqual(result["action"], "chat")

    def test_respuesta_con_bom_utf8(self):
        """Respuesta con BOM UTF-8 al inicio → debe manejarse sin crash."""
        router = _make_router('\ufeff{"action":"chat","response":"hola señor"}')
        result = router.analyze_intent("hola")
        self.assertIn("action", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
