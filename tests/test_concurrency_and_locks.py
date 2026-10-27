"""
Tests de concurrencia: race conditions y deadlocks entre JarvisTray, TTS y AudioRecorder.
"""

import threading
import time
import unittest
from unittest.mock import MagicMock, patch
from src.brain.memory import ConversationMemory


# ─── TTS is_speaking bajo concurrencia ───────────────────────────────────────

class TestTTSConcurrency(unittest.TestCase):
    """Verifica que is_speaking no genere race conditions."""

    def setUp(self):
        patcher_mixer = patch("pygame.mixer")
        self.mock_mixer = patcher_mixer.start()
        self.mock_mixer.get_init.return_value = True
        self.addCleanup(patcher_mixer.stop)

    def _make_tts(self):
        from src.audio.tts import TextToSpeech
        with patch("pygame.mixer.init"):
            return TextToSpeech(voice="es-AR-TomasNeural")

    def test_is_speaking_false_al_inicio(self):
        tts = self._make_tts()
        self.assertFalse(tts.is_speaking)

    def test_is_speaking_true_durante_reproduccion(self):
        """is_speaking = True mientras _process_stream está activo."""
        from src.audio.tts import TextToSpeech
        tts = self._make_tts()
        latch = threading.Event()
        seen_speaking = []

        def spy_chunk(chunk):
            seen_speaking.append(tts.is_speaking)
            latch.set()
            time.sleep(0.05)

        with patch.object(tts, "_speak_chunk", side_effect=spy_chunk):
            f = tts.speak("Hola señor.")
            latch.wait(timeout=2.0)
            if f:
                f.result(timeout=3.0)

        self.assertTrue(any(seen_speaking), "is_speaking nunca fue True durante la reproducción")
        self.assertFalse(tts.is_speaking, "is_speaking debe ser False al terminar")

    def test_escrituras_concurrentes_is_speaking_sin_crash(self):
        """20 hilos leen/escriben is_speaking sin corrupción."""
        tts = self._make_tts()
        errors = []

        def toggle():
            try:
                for _ in range(50):
                    tts.is_speaking = True
                    _ = tts.is_speaking
                    tts.is_speaking = False
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=toggle) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(errors, [], f"Errores en hilos: {errors}")
        # Al terminar, el estado debe ser bool
        self.assertIsInstance(tts.is_speaking, bool)

    def test_multiples_speak_en_secuencia_no_deadlock(self):
        """Llamadas secuenciales a speak() desde el mismo hilo no causan deadlock."""
        tts = self._make_tts()
        with patch.object(tts, "_speak_chunk"):
            for i in range(5):
                f = tts.speak(f"Oración número {i}.")
                if f:
                    f.result(timeout=3.0)
        self.assertFalse(tts.is_speaking)

    def test_close_desde_hilo_secundario_no_bloquea(self):
        """close() llamado desde hilo distinto al principal termina en tiempo razonable."""
        tts = self._make_tts()
        terminado = threading.Event()

        def cerrar():
            tts.close()
            terminado.set()

        t = threading.Thread(target=cerrar, daemon=True)
        t.start()
        terminado.wait(timeout=5.0)
        self.assertTrue(terminado.is_set(), "close() no terminó en 5s → posible deadlock")


# ─── JarvisTray bajo concurrencia ────────────────────────────────────────────

class TestTrayConcurrency(unittest.TestCase):
    """Verifica que set_state() y stop() sean thread-safe."""

    def _make_tray(self):
        with patch("pystray.Icon"), patch("pystray.Menu"), patch("pystray.MenuItem"):
            from src.ui.tray import JarvisTray
            return JarvisTray()

    def test_set_state_concurrente_sin_crash(self):
        """20 hilos llamando set_state() simultáneamente no deben crashear."""
        tray = self._make_tray()
        states = ["reposo", "hablando", "escuchando", "procesando", "idle"]
        errors = []

        def cambiar_estado():
            try:
                for estado in states * 5:
                    tray.set_state(estado)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=cambiar_estado) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(errors, [], f"Errores en set_state concurrente: {errors}")

    def test_is_exit_requested_thread_safe(self):
        """is_exit_requested puede leerse concurrentemente sin error."""
        tray = self._make_tray()
        results = []

        def leer():
            for _ in range(100):
                results.append(tray.is_exit_requested)

        threads = [threading.Thread(target=leer) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3.0)

        self.assertTrue(all(isinstance(r, bool) for r in results))

    def test_exit_event_set_desde_hilo_secundario(self):
        """_exit_requested puede setearse desde un hilo y leerse desde otro."""
        tray = self._make_tray()

        def disparar_exit():
            time.sleep(0.05)
            tray._exit_requested.set()

        t = threading.Thread(target=disparar_exit, daemon=True)
        t.start()
        t.join(timeout=2.0)
        self.assertTrue(tray.is_exit_requested)

    def test_estado_inicial_es_reposo(self):
        tray = self._make_tray()
        self.assertEqual(tray.current_state, "reposo")

    def test_set_state_estado_desconocido_no_crashea(self):
        tray = self._make_tray()
        tray.set_state("estado_inventado_xyz")
        self.assertEqual(tray.current_state, "estado_inventado_xyz")

    def test_callbacks_opcionales_none_no_crashea(self):
        """Tray sin callbacks no lanza excepción al disparar eventos."""
        tray = self._make_tray()
        mock_icon = MagicMock()
        mock_item = MagicMock()
        tray._handle_status(mock_icon, mock_item)
        tray._handle_clear_memory(mock_icon, mock_item)

    def test_callback_que_lanza_excepcion_no_propaga(self):
        """Si el callback de salida lanza excepción, el tray no se rompe."""
        def callback_malo():
            raise RuntimeError("callback explotó")

        with patch("pystray.Icon"), patch("pystray.Menu"), patch("pystray.MenuItem"):
            from src.ui.tray import JarvisTray
            tray = JarvisTray(on_status=callback_malo)

        mock_icon = MagicMock()
        mock_item = MagicMock()
        # No debe propagar la excepción
        try:
            tray._handle_status(mock_icon, mock_item)
        except RuntimeError:
            self.fail("El callback interno propagó RuntimeError al caller")


# ─── ConversationMemory bajo concurrencia ────────────────────────────────────

class TestMemoryConcurrency(unittest.TestCase):
    """Verifica que ConversationMemory sea seguro ante accesos concurrentes."""

    def test_add_message_concurrente_sin_corrupcion(self):
        """10 hilos agregando mensajes simultáneamente no corrompen la deque."""
        mem = ConversationMemory(max_turns=8)
        errors = []

        def agregar(i):
            try:
                mem.add_message("user", f"mensaje_user_{i}")
                mem.add_message("assistant", f"respuesta_{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=agregar, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(errors, [], f"Errores en add_message concurrente: {errors}")
        # La deque tiene maxlen=16, los mensajes deben ser ≤ 16
        self.assertLessEqual(len(mem.get_messages_list()), 16)

    def test_get_context_prompt_durante_escritura_no_crashea(self):
        """Leer get_context_prompt() mientras otro hilo escribe no debe crashear."""
        mem = ConversationMemory(max_turns=4)
        errors = []
        running = threading.Event()
        running.set()

        def escritor():
            i = 0
            while running.is_set():
                try:
                    mem.add_message("user", f"msg_{i}")
                    mem.add_message("assistant", f"resp_{i}")
                    i += 1
                except Exception as e:
                    errors.append(e)
                    break

        def lector():
            for _ in range(50):
                try:
                    _ = mem.get_context_prompt()
                    _ = mem.get_messages_list()
                except Exception as e:
                    errors.append(e)

        t_escritor = threading.Thread(target=escritor, daemon=True)
        t_lector = threading.Thread(target=lector)
        t_escritor.start()
        t_lector.start()
        t_lector.join(timeout=3.0)
        running.clear()
        t_escritor.join(timeout=2.0)

        self.assertEqual(errors, [], f"Errores en lectura/escritura concurrente: {errors}")

    def test_clear_concurrente_no_deadlock(self):
        """clear() y add_message() llamados desde distintos hilos no dan deadlock."""
        mem = ConversationMemory(max_turns=4)
        terminado = threading.Event()
        errors = []

        def ciclo():
            try:
                for i in range(20):
                    mem.add_message("user", f"mensaje {i}")
                    if i % 5 == 0:
                        mem.clear()
            except Exception as e:
                errors.append(e)
            finally:
                terminado.set()

        t = threading.Thread(target=ciclo, daemon=True)
        t.start()
        terminado.wait(timeout=5.0)
        self.assertTrue(terminado.is_set(), "Posible deadlock en clear/add_message concurrente")
        self.assertEqual(errors, [])


# ─── Integración: TTS + Tray + Memory bajo carga ─────────────────────────────

class TestIntegrationConcurrency(unittest.TestCase):
    """Simula interacción real entre componentes en múltiples hilos."""

    def test_tray_y_memory_concurrente_sin_crash(self):
        """Tray cambiando estado mientras Memory recibe mensajes: sin crash."""
        with patch("pystray.Icon"), patch("pystray.Menu"), patch("pystray.MenuItem"):
            from src.ui.tray import JarvisTray
            tray = JarvisTray()

        mem = ConversationMemory(max_turns=4)
        errors = []

        def tray_worker():
            estados = ["reposo", "escuchando", "procesando", "hablando"] * 10
            try:
                for estado in estados:
                    tray.set_state(estado)
                    time.sleep(0.001)
            except Exception as e:
                errors.append(("tray", e))

        def memory_worker():
            try:
                for i in range(20):
                    mem.add_message("user", f"pregunta {i}")
                    mem.add_message("assistant", f"respuesta {i}")
                    _ = mem.get_context_prompt()
            except Exception as e:
                errors.append(("memory", e))

        t1 = threading.Thread(target=tray_worker, daemon=True)
        t2 = threading.Thread(target=memory_worker)
        t1.start()
        t2.start()
        t2.join(timeout=5.0)
        t1.join(timeout=2.0)

        self.assertEqual(errors, [], f"Errores de integración: {errors}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
