import unittest
from unittest.mock import MagicMock

from src.brain.ollama_client import OllamaBrain


class TestOllamaStreaming(unittest.TestCase):
    """
    Pruebas unitarias para validar el streaming con stream=True de OllamaBrain
    y la correcta segmentación por oraciones delimitadas por '.', '?' y '!'.
    """

    def setUp(self) -> None:
        self.brain = OllamaBrain()
        self.brain.client = MagicMock()

    def test_streaming_sentence_segmentation(self) -> None:
        """
        Mockea la respuesta por tokens de Ollama y valida que el generador
        emita oraciones completas de forma secuencial y en tiempo real.
        """
        # Simulamos la llegada fragmentada de tokens por streaming
        mock_chunks = [
            {"message": {"content": "Hola "}},
            {"message": {"content": "señor. "}},
            {"message": {"content": "¿En qué puedo "}},
            {"message": {"content": "ayudarle hoy? "}},
            {"message": {"content": "Estoy listo "}},
            {"message": {"content": "para trabajar!"}},
        ]

        self.brain.client.chat.return_value = iter(mock_chunks)

        sentences = list(self.brain.generate_response_stream("Hola"))

        # Deben haberse emitido exactamente 3 oraciones completas
        self.assertEqual(len(sentences), 3)
        self.assertEqual(sentences[0], "Hola señor.")
        self.assertEqual(sentences[1], "¿En qué puedo ayudarle hoy?")
        self.assertEqual(sentences[2], "Estoy listo para trabajar!")

        # Verificamos que se invocó chat con stream=True
        self.brain.client.chat.assert_called_once()
        _, kwargs = self.brain.client.chat.call_args
        self.assertTrue(kwargs.get("stream"), "La llamada a Ollama debe tener stream=True activado.")

    def test_generate_response_consolidates_stream(self) -> None:
        """Verifica que generate_response reúne todas las oraciones del stream en un solo texto."""
        mock_chunks = [
            {"message": {"content": "Primera oración. "}},
            {"message": {"content": "Segunda oración."}},
        ]
        self.brain.client.chat.return_value = iter(mock_chunks)

        full_text = self.brain.generate_response("Test")
        self.assertEqual(full_text, "Primera oración. Segunda oración.")

    def test_streaming_error_yields_fallback_message(self) -> None:
        """Verifica que una desconexión en el stream emite un mensaje descriptivo sin lanzar excepción."""
        self.brain.client.chat.side_effect = ConnectionError("Ollama service down")

        sentences = list(self.brain.generate_response_stream("Test error"))
        self.assertEqual(len(sentences), 1)
        self.assertIn("servidor local de Ollama no responde", sentences[0])


if __name__ == "__main__":
    unittest.main()

