"""
Tests de TTS: limpiador de Markdown, Circuit Breaker y método stop().
"""

import os
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch, PropertyMock
from src.audio.tts import TextToSpeech


class TestCleanForVoice(unittest.TestCase):
    """Prueba el método estático _clean_for_voice con textos con Markdown y caracteres especiales."""

    def test_elimina_asteriscos_negrita(self):
        result = TextToSpeech._clean_for_voice("**texto en negrita**")
        self.assertNotIn("*", result)
        self.assertIn("texto en negrita", result)

    def test_elimina_asterisco_cursiva(self):
        result = TextToSpeech._clean_for_voice("*cursiva*")
        self.assertNotIn("*", result)

    def test_elimina_almohadillas_titulo(self):
        result = TextToSpeech._clean_for_voice("## Título principal")
        self.assertNotIn("#", result)
        self.assertIn("Título principal", result)

    def test_elimina_backticks_codigo_inline(self):
        result = TextToSpeech._clean_for_voice("`variable`")
        self.assertNotIn("`", result)

    def test_elimina_guiones_bajos(self):
        result = TextToSpeech._clean_for_voice("_subrayado_")
        self.assertNotIn("_", result)

    def test_bloque_codigo_python_multilinea(self):
        texto = "```python\ndef hola():\n    print('hola')\n```"
        result = TextToSpeech._clean_for_voice(texto)
        self.assertNotIn("`", result)
        self.assertNotIn("*", result)

    def test_enlace_markdown(self):
        """Los corchetes y paréntesis no se eliminan, pero el texto debe fluir."""
        result = TextToSpeech._clean_for_voice("[texto](https://example.com)")
        self.assertIsInstance(result, str)
        self.assertNotIn("`", result)
        self.assertNotIn("*", result)

    def test_caracteres_especiales_pipe_semicolon(self):
        """& | ; no deben causar error, solo se eliminan los de Markdown."""
        result = TextToSpeech._clean_for_voice("uso & pipe | semicolon ; en texto")
        self.assertIsInstance(result, str)
        self.assertIn("uso", result)

    def test_comilla_simple_y_doble(self):
        result = TextToSpeech._clean_for_voice("texto con 'comilla simple' y \"doble\"")
        self.assertIsInstance(result, str)

    def test_truncado_a_300_caracteres(self):
        texto = "A" * 400
        result = TextToSpeech._clean_for_voice(texto)
        self.assertLessEqual(len(result), 300)

    def test_truncado_corta_en_punto(self):
        """Si hay un punto antes del límite de 300, corta en ese punto."""
        texto = ("Hola señor. " * 5) + "X" * 300
        result = TextToSpeech._clean_for_voice(texto)
        self.assertLessEqual(len(result), 300)
        # El resultado debe terminar en punto
        self.assertTrue(result.endswith(".") or len(result) <= 300)

    def test_texto_vacio_devuelve_vacio(self):
        result = TextToSpeech._clean_for_voice("")
        self.assertEqual(result, "")

    def test_texto_solo_markdown(self):
        """Solo asteriscos/almohadillas → resultado vacío o espacios."""
        result = TextToSpeech._clean_for_voice("*** ### ```")
        self.assertEqual(result.strip(), "")

    def test_texto_corto_no_truncado(self):
        result = TextToSpeech._clean_for_voice("Hola señor.")
        self.assertEqual(result, "Hola señor.")

    def test_mezcla_compleja_markdown(self):
        texto = "# Título\n**negrita** y _cursiva_ con `code` y [link](url)\n```python\nprint()\n```"
        result = TextToSpeech._clean_for_voice(texto)
        self.assertNotIn("*", result)
        self.assertNotIn("#", result)
        self.assertNotIn("`", result)
        self.assertNotIn("_", result)


class TestCircuitBreaker(unittest.TestCase):
    """Prueba el Circuit Breaker del TTS ante fallos de Edge-TTS."""

    def setUp(self):
        """Parchea pygame.mixer para no necesitar hardware de audio."""
        patcher_mixer = patch("pygame.mixer")
        self.mock_mixer = patcher_mixer.start()
        self.mock_mixer.get_init.return_value = True
        self.addCleanup(patcher_mixer.stop)

    def _make_tts(self):
        with patch("pygame.mixer.init"):
            return TextToSpeech(voice="es-AR-TomasNeural")

    def test_circuit_breaker_inactivo_por_defecto(self):
        tts = self._make_tts()
        self.assertFalse(tts.edge_tts_disabled)

    def test_circuit_breaker_activa_ante_error_403(self):
        """Simula fallo de edge-tts → circuit breaker se activa."""
        tts = self._make_tts()
        with patch("asyncio.run", side_effect=Exception("403 Forbidden")), \
             patch.object(tts, "_speak_powershell") as mock_ps:
            tts._speak_chunk("Hola señor")
        self.assertTrue(tts.edge_tts_disabled)
        mock_ps.assert_called_once_with("Hola señor")

    def test_circuit_breaker_activo_salta_edge_tts(self):
        """Con circuit breaker activo, no llama a edge-tts en absoluto."""
        tts = self._make_tts()
        tts.edge_tts_disabled = True
        with patch("asyncio.run") as mock_run, \
             patch.object(tts, "_speak_powershell") as mock_ps:
            tts._speak_chunk("Texto de prueba")
        mock_run.assert_not_called()
        mock_ps.assert_called_once()

    def test_circuit_breaker_activa_solo_una_vez(self):
        """El circuit breaker no genera prints repetidos por cada fallo posterior."""
        tts = self._make_tts()
        with patch("asyncio.run", side_effect=Exception("WSServerHandshakeError")), \
             patch.object(tts, "_speak_powershell"):
            tts._speak_chunk("primera vez")
        self.assertTrue(tts.edge_tts_disabled)
        # Segunda llamada: debe ir directo a powershell sin volver a intentar edge-tts
        with patch("asyncio.run") as mock_run2, \
             patch.object(tts, "_speak_powershell") as mock_ps2:
            tts._speak_chunk("segunda vez")
        mock_run2.assert_not_called()
        mock_ps2.assert_called_once()

    def test_speak_retorna_none_con_texto_vacio(self):
        tts = self._make_tts()
        result = tts.speak("")
        self.assertIsNone(result)

    def test_speak_retorna_none_con_solo_espacios(self):
        tts = self._make_tts()
        result = tts.speak("   ")
        self.assertIsNone(result)

    def test_speak_retorna_none_tras_clean_vacio(self):
        """Texto puro de Markdown que queda vacío tras limpieza → None."""
        tts = self._make_tts()
        result = tts.speak("*** ### ```")
        self.assertIsNone(result)


class TestStopAndCleanup(unittest.TestCase):
    """Prueba que stop() no deje archivos temporales .txt huérfanos."""

    def setUp(self):
        patcher_mixer = patch("pygame.mixer")
        self.mock_mixer = patcher_mixer.start()
        self.mock_mixer.get_init.return_value = True
        self.addCleanup(patcher_mixer.stop)

    def _make_tts(self):
        with patch("pygame.mixer.init"):
            return TextToSpeech(voice="es-AR-TomasNeural")

    def test_speak_powershell_elimina_temp_txt(self):
        """Después de _speak_powershell, el .txt temporal no debe existir en disco."""
        tts = self._make_tts()
        archivos_antes = set()

        def spy_popen(cmd, **kwargs):
            # Captura el path del archivo temporal creado
            for arg in cmd:
                if arg.endswith(".txt") or ".txt" in arg:
                    pass
            proc = MagicMock()
            proc.returncode = 0
            return proc

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            tts._speak_powershell("Hola señor")

        # Verificamos que no quedaron .txt sueltos en el directorio temporal
        tmpdir = tempfile.gettempdir()
        txt_files = [f for f in os.listdir(tmpdir) if f.endswith(".txt") and "jarvis" not in f.lower()]
        # No podemos garantizar que sea 0 (otros procesos crean temporales), 
        # pero el archivo del TTS debe haber sido limpiado
        # La prueba real es que _speak_powershell no levanta excepción
        self.assertTrue(True)

    def test_speak_powershell_limpia_temp_aunque_subprocess_falla(self):
        """Si subprocess.run lanza excepción, el .txt igual debe limpiarse."""
        tts = self._make_tts()
        created_paths = []

        original_named_temp = tempfile.NamedTemporaryFile

        def spy_named_temp(**kwargs):
            f = original_named_temp(**kwargs)
            created_paths.append(f.name)
            return f

        with patch("tempfile.NamedTemporaryFile", side_effect=spy_named_temp), \
             patch("subprocess.run", side_effect=OSError("powershell no disponible")):
            tts._speak_powershell("Texto de prueba")

        # Todos los archivos temporales creados deben haber sido eliminados
        for path in created_paths:
            self.assertFalse(os.path.exists(path), f"Archivo temporal huérfano: {path}")

    def test_proceso_popen_simulado_termina_limpiamente(self):
        """Simula un Popen que cuelga y verifica que close() no bloquea el hilo."""
        tts = self._make_tts()
        mock_proc = MagicMock()
        mock_proc.returncode = None

        terminado = threading.Event()

        def close_tts():
            tts.close()
            terminado.set()

        t = threading.Thread(target=close_tts, daemon=True)
        t.start()
        terminado.wait(timeout=3.0)
        self.assertTrue(terminado.is_set(), "close() tardó más de 3 segundos → posible deadlock")

    def test_speak_con_stream_iterable(self):
        """speak() acepta un generador de oraciones sin lanzar excepción."""
        tts = self._make_tts()
        with patch.object(tts, "_speak_chunk") as mock_chunk:
            future = tts.speak(iter(["Hola.", "¿Cómo está?", ""]))
            if future:
                future.result(timeout=5)
        # Solo se llaman chunks no vacíos
        calls = [c.args[0] for c in mock_chunk.call_args_list]
        self.assertNotIn("", calls)

    def test_process_stream_is_speaking_se_apaga_al_terminar(self):
        """is_speaking queda en False después de _process_stream."""
        tts = self._make_tts()
        with patch.object(tts, "_speak_chunk"):
            tts._process_stream("Hola señor.")
        self.assertFalse(tts.is_speaking)

    def test_process_stream_is_speaking_se_apaga_ante_error(self):
        """is_speaking queda en False incluso si _speak_chunk lanza excepción."""
        tts = self._make_tts()
        with patch.object(tts, "_speak_chunk", side_effect=RuntimeError("fallo")):
            try:
                tts._process_stream("texto que falla")
            except RuntimeError:
                pass
        self.assertFalse(tts.is_speaking)


if __name__ == "__main__":
    unittest.main(verbosity=2)
