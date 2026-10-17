import os
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from src.audio.recorder import AudioRecorder


class TestRecorderResilience(unittest.TestCase):
    """
    Suite de pruebas de resiliencia para AudioRecorder y VAD adaptativo.
    Simula entradas vacías, nulas y ruido blanco para asegurar la estabilidad del algoritmo.
    """

    def setUp(self) -> None:
        self.recorder = AudioRecorder(sample_rate=16000, channels=1)

    @patch("sounddevice.InputStream")
    def test_null_or_silent_audio_returns_none(self, mock_input_stream: MagicMock) -> None:
        """
        Simula entrada de audio nula (silencio puro, todo ceros).
        El VAD debe alcanzar el plazo de espera inicial y retornar None sin crear un archivo.
        """
        mock_stream = MagicMock()
        mock_input_stream.return_value.__enter__.return_value = mock_stream

        # Generamos bloques de 480 muestras (30ms a 16kHz) con ceros absolutos
        silent_chunk = np.zeros((480, 1), dtype=np.int16)
        mock_stream.read.return_value = (silent_chunk, False)

        result = self.recorder.record_command(
            initial_timeout=0.3,
            silence_duration=0.2,
            max_duration=1.0,
        )

        self.assertIsNone(result, "El silencio absoluto debe devolver None sin generar audio.")

    @patch("sounddevice.InputStream")
    def test_continuous_white_noise_adapts_and_times_out(self, mock_input_stream: MagicMock) -> None:
        """
        Simula ruido blanco continuo constante.
        El VAD debe calibrar el umbral adaptativo sobre el piso de ruido y NO disparar
        falsos positivos de voz, retornando None al agotar el tiempo de espera inicial.
        """
        mock_stream = MagicMock()
        mock_input_stream.return_value.__enter__.return_value = mock_stream

        # Ruido blanco constante con amplitud uniforme [-100, 100] (RMS aprox ~57)
        np.random.seed(42)
        noise_samples = np.random.randint(-100, 100, size=(480, 1), dtype=np.int16)
        mock_stream.read.return_value = (noise_samples, False)

        result = self.recorder.record_command(
            initial_timeout=0.3,
            silence_duration=0.2,
            max_duration=1.0,
        )

        self.assertIsNone(result, "El ruido blanco continuo no debe disparar falsos positivos de voz.")

    @patch("sounddevice.InputStream")
    def test_speech_over_noise_successfully_recorded(self, mock_input_stream: MagicMock) -> None:
        """
        Simula habla humana legítima tras un periodo de ruido ambiental.
        El VAD debe detectar la voz, esperar el silencio de cierre y generar el archivo WAV.
        """
        mock_stream = MagicMock()
        mock_input_stream.return_value.__enter__.return_value = mock_stream

        # Bloque de ruido suave (RMS ~ 40)
        noise_chunk = np.random.randint(-60, 60, size=(480, 1), dtype=np.int16)
        # Bloque de voz fuerte (onda senoidal con amplitud 3000, RMS ~ 2121)
        t = np.linspace(0, 0.03, 480, endpoint=False)
        voice_chunk = (3000 * np.sin(2 * np.pi * 440 * t)).astype(np.int16).reshape(-1, 1)

        # Secuencia:
        # 15 chunks de calibración y espera (ruido)
        # 25 chunks de voz (> 0.75 s)
        # 15 chunks de silencio posterior (suficiente para superar silence_duration=0.3s)
        chunks_sequence = (
            [(noise_chunk, False)] * 15
            + [(voice_chunk, False)] * 25
            + [(noise_chunk, False)] * 15
        )

        chunk_idx = [0]

        def read_side_effect(blocksize):
            idx = chunk_idx[0]
            chunk_idx[0] += 1
            if idx < len(chunks_sequence):
                return chunks_sequence[idx]
            return (noise_chunk, False)

        mock_stream.read.side_effect = read_side_effect

        wav_path = self.recorder.record_command(
            initial_timeout=1.0,
            silence_duration=0.3,
            max_duration=3.0,
        )

        self.assertIsNotNone(wav_path, "La orden de voz debe registrarse con éxito.")
        self.assertTrue(os.path.exists(wav_path), "El archivo WAV temporal debe existir en disco.")

        # Limpiamos el archivo generado tras la verificación
        if os.path.exists(wav_path):
            os.remove(wav_path)

    @patch("sounddevice.InputStream")
    def test_audio_hardware_failure_raises_runtime_error(self, mock_input_stream: MagicMock) -> None:
        """Verifica que un fallo de hardware en el micrófono arroja RuntimeError comprensible."""
        mock_input_stream.side_effect = Exception("Dispositivo de micrófono no encontrado o desconectado.")

        with self.assertRaises(RuntimeError) as ctx:
            self.recorder.record_command()

        self.assertIn("No se pudo capturar audio del micrófono", str(ctx.exception))

    def test_invalid_parameters_raise_value_error(self) -> None:
        """Verifica que parámetros temporales no positivos levantan ValueError."""
        with self.assertRaises(ValueError):
            self.recorder.record_command(initial_timeout=-1.0)
        with self.assertRaises(ValueError):
            self.recorder.record_command(silence_duration=0.0)


if __name__ == "__main__":
    unittest.main()

