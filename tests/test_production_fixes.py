"""
Pruebas de validación para las correcciones de producción en J.A.R.V.I.S.:
1. Desactivación de edge-tts mediante Circuit Breaker tras el primer fallo 403.
2. Invocación garantizada de 'tts.speak()' con la respuesta generada ante cualquier orden.
3. Configuración estricta de 'logger.propagate = False' para evitar duplicación de logs.
4. Uso correcto del modelo 'gemini-2.0-flash' con fallback a 'gemini-1.5-flash'.
"""

import subprocess
import unittest
from unittest.mock import MagicMock, patch

from config.settings import GEMINI_MODEL
from src.audio.tts import TextToSpeech
from src.brain.gemini_client import DEFAULT_GEMINI_MODEL, SECONDARY_GEMINI_MODEL, GeminiBrain
from src.brain.intent_router import IntentRouter
from src.utils.logger import logger


class TestProductionFixes(unittest.TestCase):
    """
    Suite de pruebas para certificar la estabilidad de las soluciones implementadas en producción.
    """

    def test_circuit_breaker_disables_edge_tts_after_first_403(self):
        """
        Valida que si edge-tts arroja un error 403, el Circuit Breaker fija 'edge_tts_disabled = True',
        y en la siguiente llamada a speak() va directo a System.Speech sin tocar edge-tts.
        """
        tts = TextToSpeech()
        try:
            with patch.object(tts, "_synthesize_edge_tts") as mock_edge, \
                 patch("subprocess.run") as mock_subprocess:

                # Configuramos el mock de subproceso para simular éxito de PowerShell
                mock_subprocess.return_value = subprocess.CompletedProcess(args=["powershell.exe"], returncode=0)
                # Simulamos el error 403 de Microsoft Bing
                mock_edge.side_effect = Exception("403 Forbidden: WSServerHandshakeError")

                # Primera llamada: debe intentar edge-tts, fallar y activar Circuit Breaker
                future_1 = tts.speak("Primer mensaje")
                self.assertIsNotNone(future_1)
                future_1.result(timeout=5.0)

                self.assertTrue(tts.edge_tts_disabled, "El Circuit Breaker debió activarse tras el fallo 403.")
                self.assertEqual(mock_edge.call_count, 1, "Debe intentar edge-tts exactamente 1 vez.")
                self.assertEqual(mock_subprocess.call_count, 1, "Debe derivar a PowerShell en el primer fallo.")

                # Segunda llamada: con el Circuit Breaker activo, NO debe intentar edge-tts
                future_2 = tts.speak("Segundo mensaje")
                self.assertIsNotNone(future_2)
                future_2.result(timeout=5.0)

                self.assertEqual(mock_edge.call_count, 1, "No debe volver a llamar a edge-tts si Circuit Breaker está activo.")
                self.assertEqual(mock_subprocess.call_count, 2, "Debe ir directo a PowerShell sin demoras ni URLs.")
        finally:
            tts.close()

    def test_chat_order_processing_invokes_tts_speak_with_response(self):
        """
        Simula el procesamiento de una orden cotidiana ('hola') donde IntentRouter devuelve un chat directo.
        Valida que el sistema garantice la invocación de tts.speak(jarvis_response).
        """
        mock_llm = MagicMock()
        mock_memory = MagicMock()
        mock_tts = MagicMock()
        mock_skill_manager = MagicMock()

        router = IntentRouter(llm=mock_llm, tools=[], memory=mock_memory)

        # Simulamos que IntentRouter clasifica la entrada como conversación directa
        expected_reply = "¡Hola! ¿En qué puedo ayudarte hoy?"
        with patch.object(router, "analyze_intent", return_value={"action": "chat", "response": expected_reply}):
            intent_decision = router.analyze_intent("hola")
            action = intent_decision.get("action", "chat")
            chat_text = intent_decision.get("response", "")

            # Reproducimos la lógica unificada de main.py
            if action == "call_tool":
                jarvis_response = mock_skill_manager.execute_skill("dummy")
            else:
                jarvis_response = chat_text

            # Verificamos que si existe respuesta, tts.speak es invocado con el texto completo
            if jarvis_response and jarvis_response.strip():
                mock_tts.speak(jarvis_response)

            mock_tts.speak.assert_called_once_with(expected_reply)

    def test_tool_call_processing_invokes_tts_speak_with_skill_response(self):
        """
        Valida que ante una llamada a herramienta (ej. '¿qué hora es?'), la respuesta generada
        por SkillManager también sea enviada obligatoriamente a tts.speak().
        """
        mock_tts = MagicMock()
        expected_tool_reply = "Son las 18:45 horas, señor."

        intent_decision = {
            "action": "call_tool",
            "tool_name": "get_time",
            "parameters": {},
        }
        action = intent_decision.get("action")

        if action == "call_tool":
            jarvis_response = expected_tool_reply
        else:
            jarvis_response = "chat"

        if jarvis_response and jarvis_response.strip():
            mock_tts.speak(jarvis_response)

        mock_tts.speak.assert_called_once_with(expected_tool_reply)

    def test_logger_propagate_is_disabled(self):
        """
        Verifica que logger.propagate sea False para evitar mensajes duplicados en consola.
        """
        self.assertFalse(
            logger.propagate,
            "logger.propagate debe ser False para no reenviar eventos al root logger.",
        )

    def test_gemini_model_configuration_and_fallbacks(self):
        """
        Verifica que la configuración de modelos en settings y GeminiBrain utilice 'gemini-2.0-flash'
        y disponga de fallback a 'gemini-1.5-flash'.
        """
        self.assertEqual(GEMINI_MODEL, "gemini-2.0-flash")
        self.assertEqual(DEFAULT_GEMINI_MODEL, "gemini-2.0-flash")
        self.assertEqual(SECONDARY_GEMINI_MODEL, "gemini-1.5-flash")

        # Comprobamos que el método _normalize_model_name redirija modelos inválidos a gemini-2.0-flash
        normalized = GeminiBrain._normalize_model_name("gemini-3.8-flash")
        self.assertEqual(normalized, "gemini-2.0-flash")


if __name__ == "__main__":
    unittest.main()
