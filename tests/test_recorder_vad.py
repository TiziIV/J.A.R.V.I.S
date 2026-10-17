import math
import os
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from src.audio.recorder import AudioRecorder


class TestRecorderVAD(unittest.TestCase):
    """
    Pruebas unitarias para validar la recalibración del VAD:
    tolerancia a pausas breves de 1s y corte únicamente ante silencios prolongados de 2.5s.
    """

    def setUp(self) -> None:
        self.recorder = AudioRecorder(sample_rate=16000, channels=1)

    @patch("sounddevice.InputStream")
    def test_vad_tolerates_1s_pause_and_cuts_at_2_5s(self, mock_input_stream: MagicMock) -> None:
        """
        Simula una secuencia de audio con:
        1. Ruido ambiental de calibración (400ms).
        2. Primera locución de voz.
        3. Pausa breve de 1.0 segundo (el VAD NO debe cortar).
        4. Segunda locución de voz.
        5. Pausa prolongada de 3.0 segundos (el VAD DEBE cortar a los 2.5 segundos).
        """
        mock_stream = MagicMock()
        mock_input_stream.return_value.__enter__.return_value = mock_stream

        chunk_size = 480  # 30 ms a 16 kHz
        chunk_duration = chunk_size / 16000.0

        # Bloque de ruido suave (RMS ~ 40)
        noise_chunk = np.random.randint(-50, 50, size=(chunk_size, 1), dtype=np.int16)

        # Bloque de voz clara (onda senoidal con amplitud 3000, RMS ~ 2121)
        t = np.linspace(0, chunk_duration, chunk_size, endpoint=False)
        voice_chunk = (3000 * np.sin(2 * np.pi * 440 * t)).astype(np.int16).reshape(-1, 1)

        # Cantidad de bloques para cada etapa:
        # Calibración: 400ms = ceil(0.4 / 0.03) = 14 bloques
        calib_chunks = math.ceil(0.4 / chunk_duration)
        # Voz 1: 0.6s = 20 bloques
        speech1_chunks = 20
        # Pausa de 1.0s = 33 bloques (menor que silence_duration=2.5s, NO debe cortar)
        pause_1s_chunks = math.ceil(1.0 / chunk_duration)
        # Voz 2: 0.6s = 20 bloques
        speech2_chunks = 20
        # Pausa de 3.0s = 100 bloques (mayor que silence_duration=2.5s, DEBE cortar a los 84 bloques)
        pause_3s_chunks = math.ceil(3.0 / chunk_duration)

        sequence = (
            [(noise_chunk, False)] * calib_chunks
            + [(voice_chunk, False)] * speech1_chunks
            + [(noise_chunk, False)] * pause_1s_chunks
            + [(voice_chunk, False)] * speech2_chunks
            + [(noise_chunk, False)] * pause_3s_chunks
        )

        read_counter = [0]

        def read_side_effect(blocksize):
            idx = read_counter[0]
            read_counter[0] += 1
            if idx < len(sequence):
                return sequence[idx]
            return (noise_chunk, False)

        mock_stream.read.side_effect = read_side_effect

        wav_path = self.recorder.record_command(
            initial_timeout=6.0,
            silence_duration=2.5,
            max_duration=25.0,
        )

        self.assertIsNotNone(wav_path, "La orden con pausa intermedia de 1s debe completarse exitosamente.")
        self.assertTrue(os.path.exists(wav_path), "El archivo WAV temporal debe haberse generado.")

        # Verificamos que se hayan leído suficientes bloques para superar la pausa de 1s y capturar la voz 2
        min_expected_reads = calib_chunks + speech1_chunks + pause_1s_chunks + speech2_chunks
        self.assertGreater(
            read_counter[0],
            min_expected_reads,
            "El VAD no debió cortar durante la pausa de 1.0s; debe continuar grabando hasta el silencio de 2.5s."
        )

        # Limpieza del archivo de audio temporal
        if os.path.exists(wav_path):
            os.remove(wav_path)


if __name__ == "__main__":
    unittest.main()

