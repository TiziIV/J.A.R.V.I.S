import base64
import os
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from src.audio.tts import TextToSpeech


class TestTTSStress(unittest.TestCase):
    """
    Suite de pruebas de estrés y robustez para TextToSpeech.
    Valida la gestión de textos masivos mediante archivos temporales y PowerShell Get-Content.
    """

    def setUp(self) -> None:
        self.tts = TextToSpeech()

    def tearDown(self) -> None:
        self.tts.close()

    @patch("subprocess.run")
    @patch.object(TextToSpeech, "_synthesize_edge_tts", side_effect=Exception("Simulated edge-tts offline"))
    def test_speak_stress_50000_characters(
        self,
        mock_synthesize_edge: MagicMock,
        mock_subprocess_run: MagicMock,
    ) -> None:
        """
        Envía un texto de más de 50,000 caracteres al TTS mockeado para asegurar que
        la generación del archivo temporal y el comando de PowerShell no colapsen.
        """
        # Generamos un texto extenso de más de 50,000 caracteres con acentos y puntuación
        segment = "J.A.R.V.I.S. prueba de estrés masiva para respuestas sin límite de búfer con acentos: áéíóú ñ. "
        large_text = segment * (50000 // len(segment) + 1)
        self.assertGreaterEqual(len(large_text), 50000)

        # Capturamos la ruta del archivo temporal durante la ejecución del subproceso
        captured_temp_path = []

        def side_effect(cmd, **kwargs):
            # Verificamos que el comando tenga la estructura esperada
            self.assertEqual(cmd[0], "powershell.exe")
            self.assertIn("-EncodedCommand", cmd)
            self.assertIn("-NonInteractive", cmd)
            self.assertIn("-NoProfile", cmd)

            # Decodificamos el script de PowerShell en Base64 UTF-16LE
            encoded_script = cmd[cmd.index("-EncodedCommand") + 1]
            decoded_script = base64.b64decode(encoded_script).decode("utf-16le")

            # Validamos que se use Get-Content y System.Speech
            self.assertIn("Get-Content", decoded_script)
            self.assertIn("-LiteralPath", decoded_script)
            self.assertIn("System.Speech", decoded_script)

            # Extraemos la ruta del archivo temporal
            start_marker = "$text = Get-Content -LiteralPath '"
            end_marker = "' -Raw -Encoding UTF8"
            self.assertIn(start_marker, decoded_script)
            self.assertIn(end_marker, decoded_script)
            path_start = decoded_script.index(start_marker) + len(start_marker)
            path_end = decoded_script.index(end_marker, path_start)
            temp_path = decoded_script[path_start:path_end]
            captured_temp_path.append(temp_path)

            # Verificamos que el archivo existe en disco durante la ejecución y contiene el texto completo
            self.assertTrue(os.path.exists(temp_path), f"El archivo temporal {temp_path} debe existir.")
            with open(temp_path, "r", encoding="utf-8-sig") as f:
                content = f.read()
                self.assertEqual(content, large_text.strip())

            # Simulamos ejecución exitosa de subprocess.run
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        mock_subprocess_run.side_effect = side_effect

        # Ejecutamos la síntesis de voz
        future = self.tts.speak(large_text)
        self.assertIsNotNone(future)
        future.result(timeout=10.0)

        # Validamos que subprocess.run fue llamado exactamente una vez
        self.assertEqual(mock_subprocess_run.call_count, 1)

        # Verificamos que NO se pasó timeout a subprocess.run (permite textos infinitos)
        _, kwargs = mock_subprocess_run.call_args
        self.assertNotIn("timeout", kwargs, "subprocess.run no debe contener un timeout que limite la locución.")

        # Verificamos que el archivo temporal fue eliminado de forma segura tras terminar
        self.assertTrue(len(captured_temp_path) > 0)
        final_temp_path = captured_temp_path[0]
        self.assertFalse(
            os.path.exists(final_temp_path),
            f"El archivo temporal {final_temp_path} debe eliminarse en el bloque finally."
        )

    def test_speak_empty_text_returns_none(self) -> None:
        """Verifica que textos vacíos o con solo espacios retornan None sin invocar trabajadores."""
        self.assertIsNone(self.tts.speak(""))
        self.assertIsNone(self.tts.speak("   \n\t  "))

    @patch("subprocess.run")
    @patch.object(TextToSpeech, "_synthesize_edge_tts", side_effect=Exception("Simulated edge-tts offline"))
    def test_speak_subprocess_error_does_not_crash(
        self,
        mock_synthesize_edge: MagicMock,
        mock_subprocess_run: MagicMock,
    ) -> None:
        """Verifica que un error en PowerShell no arroja excepciones fatales."""
        mock_subprocess_run.side_effect = subprocess.CalledProcessError(
            returncode=1, cmd="powershell.exe", stderr="Error de sintetizador"
        )
        future = self.tts.speak("Texto con error simulado")
        self.assertIsNotNone(future)
        result = future.result(timeout=5.0)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()

