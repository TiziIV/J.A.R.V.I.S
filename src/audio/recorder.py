import os
import tempfile
from collections import deque

import numpy as np
import sounddevice as sd
from scipy.io import wavfile


class AudioRecorder:
    """Captura voz desde el micrófono y guarda cada turno como un WAV temporal."""

    def __init__(self, sample_rate: int = 16000):
        # Whisper espera audio mono a 16 kHz; mantenerlo así evita remuestreos costosos.
        self.sample_rate = sample_rate

    def record_until_silence(
        self,
        energy_threshold: float = 0.015,
        silence_duration: float = 1.8,
        max_duration: float = 20.0,
        pre_speech_duration: float = 0.25,
    ) -> str:
        """Graba hasta 1,8 s de silencio y conserva 250 ms previos al inicio detectado."""
        print("[Escuchando al micrófono... Hable cuando guste]")

        chunk_size = 1024
        chunk_duration = chunk_size / self.sample_rate
        required_silence_chunks = max(1, int(silence_duration / chunk_duration))
        max_chunks = max(1, int(max_duration / chunk_duration))
        pre_speech_chunks = max(1, int(pre_speech_duration / chunk_duration))
        # El búfer circular recupera el comienzo de una palabra aunque el umbral tarde en activarse.
        pre_speech_frames: deque[np.ndarray] = deque(maxlen=pre_speech_chunks)
        frames: list[np.ndarray] = []
        speech_started = False
        silence_counter = 0

        try:
            # Un bloque de 1024 muestras dura unos 64 ms, suficiente para medir energía con baja latencia.
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                blocksize=chunk_size,
            ) as stream:
                for _ in range(max_chunks):
                    audio_chunk, overflowed = stream.read(chunk_size)
                    if overflowed:
                        print("[!] Se perdió un bloque de audio; continúo la captura.")

                    current_frame = audio_chunk.copy()
                    audio_float = current_frame.astype(np.float32) / 32768.0
                    rms = float(np.sqrt(np.mean(audio_float ** 2)))

                    if not speech_started:
                        pre_speech_frames.append(current_frame)
                        if rms >= energy_threshold:
                            speech_started = True
                            # Añadimos los bloques recientes para no cortar consonantes iniciales.
                            frames.extend(pre_speech_frames)
                            print("[Voz detectada... grabando activamente]")
                    else:
                        frames.append(current_frame)
                        if rms < energy_threshold:
                            silence_counter += 1
                        else:
                            silence_counter = 0

                        # El margen ampliado tolera pausas cortas entre palabras y frases.
                        if silence_counter >= required_silence_chunks:
                            print("[Silencio prolongado detectado. Finalizando grabación]")
                            break
        except Exception as error:
            # El error del periférico se informa sin ocultar su causa al bucle principal.
            raise RuntimeError(f"No se pudo capturar audio del micrófono: {error}") from error

        if not frames:
            # Evita que una ausencia completa de voz se convierta en error de concatenación.
            frames = list(pre_speech_frames)
        if not frames:
            frames = [np.zeros((chunk_size, 1), dtype=np.int16)]

        audio_data = np.concatenate(frames, axis=0)
        # El archivo temporal se cierra antes de escribirlo para evitar bloqueos en Windows.
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
            temp_path = temp_file.name
        try:
            wavfile.write(temp_path, self.sample_rate, audio_data)
        except Exception:
            # Si falla la escritura, limpiamos el archivo incompleto antes de propagar el error.
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise
        return os.path.abspath(temp_path)
