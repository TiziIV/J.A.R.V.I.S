import unittest
from unittest.mock import MagicMock

from src.brain.intent_router import IntentRouter, generate_with_fallback


class TestLLMRouting(unittest.TestCase):
    """
    Suite de pruebas para validar la resiliencia en el enrutamiento de LLMs
    y el mecanismo de fallback automático de Ollama a Gemini.
    """

    def setUp(self) -> None:
        self.mock_ollama = MagicMock()
        self.mock_gemini = MagicMock()
        self.tools = [
            {
                "name": "system_time",
                "description": "Obtiene la hora actual",
                "parameters": {},
            },
            {
                "name": "app_launcher",
                "description": "Abre aplicaciones",
                "parameters": {"properties": {"app_name": {"type": "string"}}, "required": ["app_name"]},
            },
        ]

    def test_ollama_crash_triggers_instant_gemini_fallback(self) -> None:
        """
        Valida que si Ollama sufre una caída (excepción de conexión),
        generate_with_fallback recurre instantáneamente a Gemini sin romper el hilo principal.
        """
        self.mock_ollama.generate_response.side_effect = ConnectionError("Servidor Ollama local no disponible (Connection Refused)")
        self.mock_gemini.generate_response.return_value = "Hola señor, le asisto a través de Gemini en la nube."

        response = generate_with_fallback(
            prompt="Hola Jarvis",
            primary_llm=self.mock_ollama,
            fallback_llm=self.mock_gemini,
            system_prompt="Instrucción de prueba",
        )

        self.assertEqual(response, "Hola señor, le asisto a través de Gemini en la nube.")
        self.mock_ollama.generate_response.assert_called_once()
        self.mock_gemini.generate_response.assert_called_once()

    def test_ollama_error_text_triggers_gemini_fallback(self) -> None:
        """
        Valida que si Ollama devuelve una cadena indicando error o no disponibilidad,
        se deriva a Gemini en vez de propagar el texto defectuoso.
        """
        self.mock_ollama.generate_response.return_value = "Lo siento señor, el servidor local de Ollama no responde o no está disponible en este momento."
        self.mock_gemini.generate_response.return_value = "Respuesta recuperada con Gemini."

        response = generate_with_fallback(
            prompt="¿Cómo estás?",
            primary_llm=self.mock_ollama,
            fallback_llm=self.mock_gemini,
        )

        self.assertEqual(response, "Respuesta recuperada con Gemini.")
        self.mock_gemini.generate_response.assert_called_once()

    def test_both_llms_failing_does_not_crash_main_thread(self) -> None:
        """
        Valida que si tanto Ollama como Gemini fallan simultáneamente,
        el sistema retorna un mensaje de degradación controlado sin excepciones no capturadas.
        """
        self.mock_ollama.generate_response.side_effect = TimeoutError("Timeout en Ollama")
        self.mock_gemini.generate_response.side_effect = RuntimeError("Cuota excedida en Gemini API")

        response = generate_with_fallback(
            prompt="¿Qué hora es?",
            primary_llm=self.mock_ollama,
            fallback_llm=self.mock_gemini,
        )

        self.assertIn("ninguno de mis cerebros responde", response.lower())

    def test_intent_router_fallback_on_ollama_exception(self) -> None:
        """
        Valida que IntentRouter ejecute el fallback a Gemini cuando Ollama lanza excepción
        al analizar la intención del usuario.
        """
        self.mock_ollama.generate_response.side_effect = Exception("Ollama socket closed")
        self.mock_gemini.generate_response.return_value = '{"action":"call_tool","tool_name":"system_time","parameters":{}}'

        router = IntentRouter(
            llm=self.mock_ollama,
            tools=self.tools,
            fallback_llm=self.mock_gemini,
        )

        decision = router.analyze_intent("¿Qué hora tienes?")

        self.assertEqual(decision["action"], "call_tool")
        self.assertEqual(decision["tool_name"], "system_time")
        self.mock_gemini.generate_response.assert_called_once()

    def test_intent_router_uses_primary_when_available(self) -> None:
        """Valida que si Ollama responde correctamente, Gemini no sea consultado."""
        self.mock_ollama.generate_response.return_value = '{"action":"chat","response":"Todo en orden, señor."}'

        router = IntentRouter(
            llm=self.mock_ollama,
            tools=self.tools,
            fallback_llm=self.mock_gemini,
        )

        decision = router.analyze_intent("Hola")

        self.assertEqual(decision["action"], "chat")
        self.assertEqual(decision["response"], "Todo en orden, señor.")
        self.mock_gemini.generate_response.assert_not_called()


if __name__ == "__main__":
    unittest.main()

