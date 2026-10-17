import math
import os
import tempfile
from collections import deque

import numpy as np
import sounddevice as sd
from scipy.io import wavfile


class AudioRecorder:
    """Graba una orden de voz, espera el silencio final y devuelve un WAV temporal."""

    def __init__(self, sample_rate: int = 16000, channels: int = 1):
        # Whisper espera 16 kHz; conservar esta frecuencia evita remuestrear el audio.
        self.sample_rate: int = sample_rate
        # Un canal mantiene la señal mono, suficiente para reconocer voz y más liviano.
        self.channels: int = channels

    def record_command(
        self,
        initial_timeout: float = 6.0,
        silence_duration: float = 2.5,
        max_duration: float = 25.0,
    ) -> str | None:
        """Graba la voz si comienza dentro del plazo; retorna None si no detecta habla."""
        # Validamos tiempos antes de abrir el micrófono para detectar errores de configuración.
        if initial_timeout <= 0 or silence_duration <= 0 or max_duration <= 0:
            raise ValueError("Los tiempos de grabación deben ser mayores que cero.")

        # 480 muestras a 16 kHz equivalen a 30 ms por bloque y permiten VAD ágil.
        chunk_size: int = 480
        chunk_duration: float = chunk_size / self.sample_rate
        # 400 ms de calibración calculan cuántos bloques necesitamos al inicio para medir el ambiente.
        calibration_chunks: int = max(1, math.ceil(0.4 / chunk_duration))
        # Convertimos cada duración a bloques enteros para contar tiempo con precisión.
        grace_chunks: int = max(1, math.ceil(initial_timeout / chunk_duration))
        silence_chunks: int = max(1, math.ceil(silence_duration / chunk_duration))
        max_chunks: int = max(1, math.ceil(max_duration / chunk_duration))
        # Conservamos 250 ms previos para que la primera consonante no quede cortada.
        pre_roll_chunks: int = max(1, math.ceil(0.25 / chunk_duration))

        # Las listas mantienen el audio crudo y sus mediciones hasta conocer el umbral.
        calibration_frames: list[np.ndarray] = []
        calibration_rms: list[float] = []
        # La cola elimina automáticamente los bloques viejos y deja solo el pre-roll reciente.
        pre_roll: deque[np.ndarray] = deque(maxlen=pre_roll_chunks)
        # Esta lista solo recibe voz/pre-roll después de que se activa la detección.
        recorded_frames: list[np.ndarray] = []
        speech_started: bool = False
        silence_counter: int = 0
        chunks_read: int = 0
        waiting_chunks: int = 0

        print("[VAD] Midiendo el ruido ambiente durante 400 ms...")

        try:
            # InputStream mantiene una sola conexión abierta durante toda la orden.
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                blocksize=chunk_size,
            ) as stream:
                # Capturamos el ambiente antes de fijar el umbral de voz.
                for _ in range(min(calibration_chunks, max_chunks)):
                    audio_chunk, overflowed = stream.read(chunk_size)
                    if overflowed:
                        print("[!] Se perdió un bloque mientras se calibraba el micrófono.")
                    frame: np.ndarray = audio_chunk.copy()
                    calibration_frames.append(frame)
                    calibration_rms.append(self._calculate_rms(frame))
                    pre_roll.append(frame)
                    chunks_read += 1

                # La mediana estima el piso de ruido y reduce el efecto de un golpe aislado.
                ambient_rms: float = float(np.median(calibration_rms)) if calibration_rms else 0.0
                # Escalamos el ruido ambiente y usamos 120.0 como sensibilidad mínima de muestras int16.
                threshold: float = max(ambient_rms * 1.6, 120.0)
                print(f"[VAD] RMS ambiente: {ambient_rms:.1f}; umbral de voz: {threshold:.1f}")

                # Si la persona empezó durante la calibración, guardamos también esos primeros bloques.
                speech_started = any(rms > threshold for rms in calibration_rms)
                if speech_started:
                    recorded_frames.extend(calibration_frames)
                    print("[Voz detectada... grabando la orden]")

                # Leemos un bloque por vez hasta acabar la orden o alcanzar el límite máximo.
                while chunks_read < max_chunks:
                    # El plazo de gracia impide volver al reposo antes de que el usuario pueda hablar.
                    if not speech_started and waiting_chunks >= grace_chunks:
                        print("[VAD] No se detectó voz dentro del plazo inicial.")
                        return None

                    # read devuelve audio y un indicador por si el sistema perdió muestras.
                    audio_chunk, overflowed = stream.read(chunk_size)
                    chunks_read += 1
                    if not speech_started:
                        waiting_chunks += 1
                    if overflowed:
                        print("[!] Se perdió un bloque de audio; continúo la captura.")

                    # Copiamos los datos porque sounddevice reutiliza memoria internamente.
                    frame = audio_chunk.copy()
                    frame_rms: float = self._calculate_rms(frame)

                    if not speech_started:
                        # El pre-roll incluye el bloque actual para guardar el inicio de la palabra.
                        pre_roll.append(frame)
                        if frame_rms > threshold:
                            speech_started = True
                            recorded_frames.extend(pre_roll)
                            print("[Voz detectada... grabando la orden]")
                        # El bloque que activó la voz ya está dentro del pre-roll.
                        continue

                    # Después de detectar voz, guardamos cada bloque hasta terminar la frase.
                    recorded_frames.append(frame)
                    if frame_rms <= threshold:
                        silence_counter += 1
                        if silence_counter >= silence_chunks:
                            print("[Silencio continuo detectado; cierro la captura]")
                            break
                    else:
                        # Cualquier voz reinicia el contador para tolerar pausas entre palabras.
                        silence_counter = 0
        except Exception as error:
            # Convertimos errores del dispositivo a un mensaje claro para el bucle principal.
            raise RuntimeError(f"No se pudo capturar audio del micrófono: {error}") from error

        # Nunca enviamos a Whisper una grabación sin voz ni una lista vacía de bloques.
        if not speech_started or not recorded_frames:
            print("[VAD] No se obtuvo voz; se omite la creación del WAV.")
            return None

        # Unimos los bloques conservando las dimensiones de canales esperadas por el WAV.
        audio_data: np.ndarray = np.concatenate(recorded_frames, axis=0)
        duration_seconds: float = len(audio_data) / self.sample_rate
        # Rechazamos buffers vacíos, todo-cero o demasiado cortos para ser una orden útil.
        if audio_data.size == 0 or not np.any(audio_data) or duration_seconds < 0.5:
            print("[VAD] La captura está vacía o dura menos de 0.5 s; se descarta.")
            return None

        # Windows no permite que otros procesos abran un archivo temporal aún abierto.
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
            temp_path: str = temp_file.name

        try:
            # Guardamos la frecuencia original para que faster-whisper lea el audio correctamente.
            wavfile.write(temp_path, self.sample_rate, audio_data)
        except Exception:
            # Si la escritura falla, borramos el archivo parcial antes de informar el error.
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise

        print("[VAD] Audio guardado; listo para transcribir.")
        return os.path.abspath(temp_path)

    @staticmethod
    def _calculate_rms(frame: np.ndarray) -> float:
        """Calcula la energía RMS en escala int16 para compararla con el umbral dinámico."""
        # Pasar a float32 antes de elevar al cuadrado evita desbordamientos de int16.
        samples: np.ndarray = frame.astype(np.float32)
        # RMS resume la energía del bloque; la raíz devuelve la misma escala de amplitud.
        return float(np.sqrt(np.mean(np.square(samples))))
