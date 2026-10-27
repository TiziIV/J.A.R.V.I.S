"""
Suite exhaustiva de pruebas unitarias para alcanzar >90% de cobertura en:
- src/audio/stt.py (SpeechToText)
- src/audio/wake_word.py (WakeWordDetector)
- src/brain/gemini_client.py (GeminiBrain)
- src/audio/tts.py (rutas internas)
- src/ui/tray.py (ciclo de vida)
"""

import os
import unittest
from unittest.mock import MagicMock, patch, PropertyMock
import numpy as np


# ─── Tests para SpeechToText (src/audio/stt.py) ──────────────────────────────

class TestSpeechToText(unittest.TestCase):

    @patch("src.audio.stt.WhisperModel")
    def test_init_cuda_exitoso(self, mock_whisper):
        from src.audio.stt import SpeechToText
        stt = SpeechToText(model_size="base", device="cuda", compute_type="float16")
        self.assertEqual(stt.device, "cuda")
        self.assertEqual(stt.compute_type, "float16")
        mock_whisper.assert_called_once_with("base", device="cuda", compute_type="float16")

    @patch("src.audio.stt.WhisperModel")
    def test_init_cuda_falla_conmuta_a_cpu(self, mock_whisper):
        from src.audio.stt import SpeechToText
        # Primer llamado falla con error de CUDA, segundo llamado en CPU tiene éxito
        mock_whisper.side_effect = [RuntimeError("CUDA error"), MagicMock()]
        stt = SpeechToText(model_size="base", device="cuda", compute_type="float16")
        self.assertEqual(stt.device, "cpu")
        self.assertEqual(stt.compute_type, "int8")

    @patch("src.audio.stt.WhisperModel", side_effect=RuntimeError("CPU error"))
    def test_init_cpu_falla_lanza_excepcion(self, mock_whisper):
        from src.audio.stt import SpeechToText
        with self.assertRaises(RuntimeError):
            SpeechToText(model_size="base", device="cpu", compute_type="int8")

    @patch("src.audio.stt.WhisperModel")
    def test_transcribe_archivo_inexistente_lanza_filenotfound(self, mock_whisper):
        from src.audio.stt import SpeechToText
        stt = SpeechToText()
        with self.assertRaises(FileNotFoundError):
            stt.transcribe("ruta/inexistente/a_un_audio.wav")

    @patch("os.path.exists", return_value=True)
    @patch("soundfile.read", return_value=(np.zeros(16000, dtype=np.float32), 16000))
    @patch("src.audio.stt.WhisperModel")
    def test_transcribe_audio_valido(self, mock_whisper_cls, mock_sf_read, mock_exists):
        from src.audio.stt import SpeechToText
        mock_model = MagicMock()
        mock_whisper_cls.return_value = mock_model

        # Mock de segmentos devueltos por Whisper
        seg1 = MagicMock(text="Hola Jarvis", no_speech_prob=0.1, avg_logprob=-0.2)
        seg2 = MagicMock(text="qué hora es", no_speech_prob=0.05, avg_logprob=-0.1)
        mock_model.transcribe.return_value = ([seg1, seg2], None)

        stt = SpeechToText()
        resultado = stt.transcribe("audio.wav")
        self.assertEqual(resultado, "Hola Jarvis qué hora es")

    @patch("os.path.exists", return_value=True)
    @patch("soundfile.read", return_value=(np.zeros(16000, dtype=np.float32), 16000))
    @patch("src.audio.stt.WhisperModel")
    def test_transcribe_filtra_alucinaciones_y_ruido(self, mock_whisper_cls, mock_sf_read, mock_exists):
        from src.audio.stt import SpeechToText
        mock_model = MagicMock()
        mock_whisper_cls.return_value = mock_model

        # Segmento 1: no_speech_prob alto (ruido)
        seg_ruido = MagicMock(text="ruido de fondo", no_speech_prob=0.8, avg_logprob=-0.2)
        # Segmento 2: confianza baja
        seg_baja_conf = MagicMock(text="alucinacion", no_speech_prob=0.1, avg_logprob=-1.5)
        # Segmento 3: una sola letra
        seg_una_letra = MagicMock(text="a", no_speech_prob=0.1, avg_logprob=-0.1)
        # Segmento 4: válido ("sí")
        seg_valido = MagicMock(text="sí", no_speech_prob=0.1, avg_logprob=-0.2)

        mock_model.transcribe.return_value = ([seg_ruido, seg_baja_conf, seg_una_letra, seg_valido], None)

        stt = SpeechToText()
        resultado = stt.transcribe("audio.wav")
        self.assertEqual(resultado, "sí")


# ─── Tests para WakeWordDetector (src/audio/wake_word.py) ───────────────────

class TestWakeWordDetector(unittest.TestCase):

    def test_threshold_invalido_menor_cero(self):
        from src.audio.wake_word import WakeWordDetector
        with self.assertRaises(ValueError):
            WakeWordDetector(threshold=-0.1)

    def test_threshold_invalido_mayor_uno(self):
        from src.audio.wake_word import WakeWordDetector
        with self.assertRaises(ValueError):
            WakeWordDetector(threshold=1.5)

    @patch("src.audio.wake_word.WakeWordDetector._load_model")
    def test_init_exitoso(self, mock_load):
        from src.audio.wake_word import WakeWordDetector
        mock_model = MagicMock()
        mock_load.return_value = (mock_model, "hey_jarvis")
        detector = WakeWordDetector(threshold=0.6)
        self.assertEqual(detector.threshold, 0.6)
        self.assertEqual(detector._wake_word_name, "hey_jarvis")

    def test_load_model_sin_openwakeword_lanza_runtimeerror(self):
        from src.audio.wake_word import WakeWordDetector
        with patch.dict("sys.modules", {"openwakeword": None, "openwakeword.model": None}):
            with self.assertRaises(RuntimeError):
                WakeWordDetector._load_model()

    def test_load_model_exitoso(self):
        from src.audio.wake_word import WakeWordDetector
        mock_oww_model = MagicMock()
        with patch.dict("sys.modules", {"openwakeword": MagicMock(), "openwakeword.model": mock_oww_model}):
            mock_oww_model.Model.return_value = MagicMock()
            model, name = WakeWordDetector._load_model()
            self.assertIn(name, WakeWordDetector.MODEL_CANDIDATES)

    def test_load_model_con_descarga_automatica(self):
        from src.audio.wake_word import WakeWordDetector
        mock_oww_model = MagicMock()
        mock_oww_utils = MagicMock()
        # Primer intento falla, pero download_models funciona y luego tiene éxito
        mock_oww_model.Model.side_effect = [RuntimeError("Modelo no encontrado"), MagicMock()]
        with patch.dict("sys.modules", {
            "openwakeword": MagicMock(),
            "openwakeword.model": mock_oww_model,
            "openwakeword.utils": mock_oww_utils,
        }):
            model, name = WakeWordDetector._load_model()
            self.assertIsNotNone(model)

    def test_get_score(self):
        from src.audio.wake_word import WakeWordDetector
        # Caso clave exacta
        self.assertEqual(WakeWordDetector._get_score({"hey_jarvis": 0.85}, "hey_jarvis"), 0.85)
        # Caso con espacios
        self.assertEqual(WakeWordDetector._get_score({"hey jarvis": 0.90}, "hey_jarvis"), 0.90)
        # Caso fallback al máximo
        self.assertEqual(WakeWordDetector._get_score({"otro_modelo": 0.70}, "desconocido"), 0.70)
        # Caso diccionario vacío
        self.assertEqual(WakeWordDetector._get_score({}, "desconocido"), 0.0)

    @patch("src.audio.wake_word.WakeWordDetector._load_model")
    def test_listen_timeout_invalido(self, mock_load):
        from src.audio.wake_word import WakeWordDetector
        mock_load.return_value = (MagicMock(), "hey_jarvis")
        detector = WakeWordDetector()
        with self.assertRaises(ValueError):
            detector.listen_for_wake_word(timeout=-1.0)

    @patch("src.audio.wake_word.WakeWordDetector._load_model")
    def test_listen_detecta_palabra_exitosamente(self, mock_load):
        from src.audio.wake_word import WakeWordDetector
        mock_model = MagicMock()
        mock_model.predict.return_value = {"hey_jarvis": 0.95}
        mock_load.return_value = (mock_model, "hey_jarvis")

        mock_stream = MagicMock()
        chunk_data = np.zeros((1280, 1), dtype=np.int16)
        mock_stream.read.return_value = (chunk_data, False)

        with patch("sounddevice.InputStream", return_value=mock_stream):
            detector = WakeWordDetector(threshold=0.5)
            detectado = detector.listen_for_wake_word(timeout=2.0)
            self.assertTrue(detectado)

    @patch("src.audio.wake_word.WakeWordDetector._load_model")
    def test_listen_timeout_vence_retorna_false(self, mock_load):
        from src.audio.wake_word import WakeWordDetector
        mock_model = MagicMock()
        mock_model.predict.return_value = {"hey_jarvis": 0.10}  # Bajo umbral
        mock_load.return_value = (mock_model, "hey_jarvis")

        mock_stream = MagicMock()
        chunk_data = np.zeros((1280, 1), dtype=np.int16)
        mock_stream.read.return_value = (chunk_data, False)

        with patch("sounddevice.InputStream", return_value=mock_stream):
            detector = WakeWordDetector(threshold=0.5)
            # Timeout muy corto para que expire inmediatamente
            detectado = detector.listen_for_wake_word(timeout=0.01)
            self.assertFalse(detectado)

    @patch("src.audio.wake_word.WakeWordDetector._load_model")
    def test_listen_concurrente_lanza_runtimeerror(self, mock_load):
        from src.audio.wake_word import WakeWordDetector
        mock_load.return_value = (MagicMock(), "hey_jarvis")
        detector = WakeWordDetector()
        detector._listen_lock.acquire()
        try:
            with self.assertRaises(RuntimeError):
                detector.listen_for_wake_word(timeout=1.0)
        finally:
            detector._listen_lock.release()

    @patch("src.audio.wake_word.WakeWordDetector._load_model")
    def test_listen_fallo_microfono_lanza_runtimeerror(self, mock_load):
        from src.audio.wake_word import WakeWordDetector
        mock_load.return_value = (MagicMock(), "hey_jarvis")
        with patch("sounddevice.InputStream", side_effect=OSError("No input device")):
            detector = WakeWordDetector()
            with self.assertRaises(RuntimeError):
                detector.listen_for_wake_word(timeout=1.0)

    @patch("src.audio.wake_word.WakeWordDetector._load_model")
    def test_stop_interrumpe_stream(self, mock_load):
        from src.audio.wake_word import WakeWordDetector
        mock_load.return_value = (MagicMock(), "hey_jarvis")
        detector = WakeWordDetector()
        mock_stream = MagicMock()
        detector._stream = mock_stream
        detector.stop()
        self.assertTrue(detector._stop_event.is_set())
        mock_stream.abort.assert_called_once()


# ─── Tests para GeminiBrain (src/brain/gemini_client.py) ─────────────────────

class TestGeminiBrainExhaustive(unittest.TestCase):

    def test_init_sin_api_key_modo_degradado(self):
        from src.brain.gemini_client import GeminiBrain
        brain = GeminiBrain(api_key="")
        self.assertIsNone(brain.client)
        resp = brain.generate_response("hola")
        self.assertIn("no está configurado", resp)

    @patch("src.brain.gemini_client.genai.Client")
    def test_init_con_api_key_exitoso(self, mock_client_cls):
        from src.brain.gemini_client import GeminiBrain
        mock_client_cls.return_value = MagicMock()
        brain = GeminiBrain(api_key="AIzaSyTestKey123", model_name="gemini-2.0-flash")
        self.assertIsNotNone(brain.client)
        self.assertEqual(brain.model_name, "gemini-2.0-flash")

    def test_normalize_model_name_legados(self):
        from src.brain.gemini_client import GeminiBrain, DEFAULT_GEMINI_MODEL
        self.assertEqual(GeminiBrain._normalize_model_name("gemini-3.8-flash"), DEFAULT_GEMINI_MODEL)
        self.assertEqual(GeminiBrain._normalize_model_name("models/gemini-2.0-flash"), "gemini-2.0-flash")
        self.assertEqual(GeminiBrain._normalize_model_name(""), DEFAULT_GEMINI_MODEL)
        self.assertEqual(GeminiBrain._normalize_model_name(None), DEFAULT_GEMINI_MODEL)

    @patch("src.brain.gemini_client.genai.Client")
    def test_generate_response_exitoso(self, mock_client_cls):
        from src.brain.gemini_client import GeminiBrain
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_response = MagicMock(text="Hola señor, ¿en qué le puedo servir?")
        mock_client.models.generate_content.return_value = mock_response

        brain = GeminiBrain(api_key="fake_key", model_name="gemini-2.0-flash")
        respuesta = brain.generate_response("Hola")
        self.assertEqual(respuesta, "Hola señor, ¿en qué le puedo servir?")

    @patch("src.brain.gemini_client.genai.Client")
    def test_generate_response_vacia_retorna_aviso(self, mock_client_cls):
        from src.brain.gemini_client import GeminiBrain
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_response = MagicMock(text=None)
        mock_client.models.generate_content.return_value = mock_response

        brain = GeminiBrain(api_key="fake_key")
        respuesta = brain.generate_response("Hola")
        self.assertIn("vacía", respuesta)

    @patch("src.brain.gemini_client.genai.Client")
    def test_generate_response_reintenta_ante_404(self, mock_client_cls):
        from src.brain.gemini_client import GeminiBrain, SECONDARY_GEMINI_MODEL
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        error_404 = Exception("404 Model not found")
        exito = MagicMock(text="Respuesta del modelo secundario")
        mock_client.models.generate_content.side_effect = [error_404, exito]

        brain = GeminiBrain(api_key="fake_key", model_name="modelo-inexistente")
        respuesta = brain.generate_response("Hola")
        self.assertEqual(respuesta, "Respuesta del modelo secundario")

    @patch("src.brain.gemini_client.genai.Client")
    def test_generate_response_todos_modelos_404(self, mock_client_cls):
        from src.brain.gemini_client import GeminiBrain
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        error_404 = Exception("404 NOT_FOUND")
        mock_client.models.generate_content.side_effect = error_404

        brain = GeminiBrain(api_key="fake_key", model_name="modelo-malo")
        respuesta = brain.generate_response("Hola")
        self.assertIn("no aceptaron la solicitud", respuesta)

    @patch("src.brain.gemini_client.genai.Client")
    def test_generate_response_error_de_red(self, mock_client_cls):
        from src.brain.gemini_client import GeminiBrain
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        mock_client.models.generate_content.side_effect = ConnectionError("Red caída")
        brain = GeminiBrain(api_key="fake_key")
        respuesta = brain.generate_response("Hola")
        self.assertIn("no pude conectar con Gemini", respuesta)


# ─── Tests adicionales para TTS (src/audio/tts.py) ───────────────────────────

class TestTTSInternalMethods(unittest.TestCase):

    def setUp(self):
        patcher = patch("pygame.mixer")
        self.mock_mixer = patcher.start()
        self.mock_mixer.get_init.return_value = True
        self.mock_mixer.music.get_busy.return_value = False
        self.addCleanup(patcher.stop)

    def test_build_powershell_command_contiene_filtro_espanol(self):
        from src.audio.tts import TextToSpeech
        cmd = TextToSpeech._build_powershell_command("C:\\temp\\prueba.txt")
        self.assertEqual(cmd[0], "powershell.exe")
        self.assertIn("-EncodedCommand", cmd)

    def test_play_audio_file_con_pygame(self):
        from src.audio.tts import TextToSpeech
        with patch("pygame.mixer.init"):
            tts = TextToSpeech()
        tts._play_audio_file("fake_audio.mp3")
        self.mock_mixer.music.load.assert_called_with("fake_audio.mp3")
        self.mock_mixer.music.play.assert_called_once()
        self.mock_mixer.music.unload.assert_called_once()


# ─── Tests adicionales para JarvisTray (src/ui/tray.py) ──────────────────────

class TestTrayLifecycle(unittest.TestCase):

    @patch("pystray.Icon")
    @patch("pystray.Menu")
    @patch("pystray.MenuItem")
    def test_tray_start_y_stop(self, mock_item, mock_menu, mock_icon_cls):
        import threading
        from src.ui.tray import JarvisTray
        mock_icon = MagicMock()
        stop_event = threading.Event()
        mock_icon.run.side_effect = stop_event.wait
        mock_icon.stop.side_effect = stop_event.set
        mock_icon_cls.return_value = mock_icon

        tray = JarvisTray()
        tray.start()
        self.assertTrue(tray._is_running)

        # Llamar start de nuevo no crea otro hilo
        tray.start()

        tray.stop()
        self.assertFalse(tray._is_running)
        mock_icon.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
