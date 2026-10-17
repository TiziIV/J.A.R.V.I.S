import base64
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from src.audio.tts import TextToSpeech


class TestTTSFallback(unittest.TestCase):
    """
    Pruebas unitarias para validar el fallback blindado de TTS:
    ante un fallo 403 o de red en edge-tts, se activa PowerShell System.Speech en español.
    """

    def setUp(self) -> None:
        self.tts = TextToSpeech()

    def tearDown(self) -> None:
        self.tts.close()

    @patch("subprocess.run")
    @patch.object(TextToSpeech, "_synthesize_edge_tts")
    def test_edge_tts_failure_triggers_powershell_spanish_fallback(
        self,
        mock_synthesize_edge: MagicMock,
        mock_subprocess_run: MagicMock,
    ) -> None:
        """
        Fuerza un error 403 en edge-tts y verifica que el script de PowerShell
        en español se ejecute correctamente con los filtros de idioma adecuados.
        """
        # Simulamos fallo 403 de edge-tts
        mock_synthesize_edge.side_effect = Exception("403 Forbidden: WSServerHandshakeError")

        captured_commands = []

        def fake_subprocess_run(cmd, **kwargs):
            captured_commands.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        mock_subprocess_run.side_effect = fake_subprocess_run

        future = self.tts.speak("Prueba de activación del fallback en español")
        self.assertIsNotNone(future)
        future.result(timeout=5.0)

        # Verificamos que edge-tts intentó ejecutarse
        mock_synthesize_edge.assert_called_once()

        # Verificamos que PowerShell fue invocado como fallback
        self.assertEqual(len(captured_commands), 1)
        cmd = captured_commands[0]
        self.assertEqual(cmd[0], "powershell.exe")
        self.assertIn("-EncodedCommand", cmd)

        # Decodificamos el script de PowerShell
        encoded_script = cmd[cmd.index("-EncodedCommand") + 1]
        decoded_script = base64.b64decode(encoded_script).decode("utf-16le")

        # Comprobamos que el script filtre estrictamente por idioma español
        self.assertIn("System.Speech", decoded_script)
        self.assertTrue(
            "TwoLetterISOLanguageName -eq 'es'" in decoded_script
            or "Culture.Name -like 'es-*'" in decoded_script
            or "Culture -eq 'es'" in decoded_script,
            "El script de PowerShell debe filtrar OBLIGATORIAMENTE por voces en español."
        )

        # Verificamos que el flag de sincronización esté apagado al concluir
        self.assertFalse(self.tts.is_speaking)

    @patch.object(TextToSpeech, "_synthesize_edge_tts")
    @patch.object(TextToSpeech, "_play_audio_file")
    def test_edge_tts_success_does_not_trigger_powershell(
        self,
        mock_play_audio: MagicMock,
        mock_synthesize_edge: MagicMock,
    ) -> None:
        """Verifica que si edge-tts funciona correctamente, no se invoca PowerShell."""
        mock_synthesize_edge.return_value = None

        with patch("subprocess.run") as mock_subprocess_run:
            future = self.tts.speak("Hola señor")
            self.assertIsNotNone(future)
            future.result(timeout=5.0)

            mock_synthesize_edge.assert_called_once()
            mock_play_audio.assert_called_once()
            mock_subprocess_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

