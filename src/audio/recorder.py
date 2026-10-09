import os
import tempfile
import numpy as np
import sounddevice as sd
from scipy.io import wavfile

class AudioRecorder:
    """
    Clase encargada de capturar audio desde el micrófono utilizando sounddevice.
    Implementa grabación inteligente con Detección de Actividad de Voz (VAD / Voice Activity Detection)
    basada en energía (RMS), eliminando la necesidad de grabar por segundos fijos.
    """
    
    def __init__(self, sample_rate: int = 16000):
        """
        Inicializa el grabador de audio.
        
        Args:
            sample_rate (int): Frecuencia de muestreo en Hz (por defecto 16000 Hz, óptimo para Whisper).
        """
        self.sample_rate = sample_rate

    def record_until_silence(
        self,
        energy_threshold: float = 0.015,
        silence_duration: float = 1.3,
        max_duration: float = 20.0
    ) -> str:
        """
        Escucha el micrófono de manera inteligente: espera a que el usuario empiece a hablar
        y corta automáticamente cuando detecta una pausa o silencio continuo.
        
        Args:
            energy_threshold (float): Umbral de energía RMS para diferenciar voz de ruido de fondo.
            silence_duration (float): Segundos de silencio continuo necesarios para detener la grabación (ej. 1.3s).
            max_duration (float): Tiempo máximo de seguridad en segundos para evitar grabaciones infinitas (ej. 20s).
            
        Returns:
            str: Ruta absoluta del archivo temporal .wav generado con el audio grabado.
        """
        print("[Escuchando al micrófono... Hable cuando guste]")
        
        chunk_size = 1024  # Cantidad de muestras por cada bloque de audio leído
        chunk_duration = chunk_size / self.sample_rate  # Duración de cada chunk en segundos (~0.064s)
        
        # Calculamos cuántos chunks consecutivos de silencio equivalen al tiempo límite de pausa
        required_silence_chunks = int(silence_duration / chunk_duration)
        max_chunks = int(max_duration / chunk_duration)
        
        frames = []              # Lista para acumular todos los bloques de audio capturados
        speech_started = False   # Bandera para saber si el usuario ya empezó a hablar
        silence_counter = 0      # Contador de chunks consecutivos con silencio tras haber empezado a hablar
        chunks_read = 0          # Contador total de bloques leídos (control de timeout)

        # Abrimos un stream de entrada en tiempo real usando sounddevice
        # - samplerate: 16000 Hz
        # - channels: 1 (mono, ideal para STT)
        # - dtype: 'int16' (enteros de 16 bits, estándar para audio PCM)
        # - blocksize: 1024 muestras por bloque
        with sd.InputStream(samplerate=self.sample_rate, channels=1, dtype='int16', blocksize=chunk_size) as stream:
            while chunks_read < max_chunks:
                # Leemos un bloque (chunk) del micrófono y un indicador de desbordamiento (overflow)
                audio_chunk, overflowed = stream.read(chunk_size)
                
                if overflowed:
                    # Si el búfer se desbordó, mostramos una advertencia leve (no crítico)
                    print("[!] Advertencia: desbordamiento de búfer de audio detectado.")

                # Añadimos el bloque actual a nuestra lista acumulativa
                frames.append(audio_chunk.copy())
                chunks_read += 1

                # Calculamos la energía del bloque (RMS / Root Mean Square)
                # 1. Convertimos los enteros de 16 bits (-32768 a 32767) a flotantes entre -1.0 y 1.0
                audio_float = audio_chunk.astype(np.float32) / 32768.0
                # 2. Raíz cuadrada de la media de los cuadrados (RMS)
                rms = np.sqrt(np.mean(audio_float ** 2))

                # Evaluamos si el usuario está hablando o hay silencio
                if not speech_started:
                    # Fase 1: Esperando a que el usuario empiece a hablar
                    if rms > energy_threshold:
                        speech_started = True
                        print("[Voz detectada... grabando activamente]")
                        silence_counter = 0
                else:
                    # Fase 2: El usuario ya empezó a hablar; monitoreamos si hace una pausa
                    if rms < energy_threshold:
                        # El bloque actual es silencio
                        silence_counter += 1
                    else:
                        # El usuario sigue hablando; reiniciamos el contador de silencio
                        silence_counter = 0

                    # Si el silencio continuo supera el límite requerido, cortamos la grabación
                    if silence_counter >= required_silence_chunks:
                        print("[Silencio prolongado detectado. Finalizando grabación]")
                        break

        # Convertimos la lista de bloques acumulados en un único arreglo de NumPy
        audio_data = np.concatenate(frames, axis=0)

        # Creamos un archivo temporal con extensión .wav que no se borre automáticamente al cerrar
        temp_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        temp_file.close()

        # Guardamos el audio acumulado en el archivo temporal usando scipy.io.wavfile
        wavfile.write(temp_file.name, self.sample_rate, audio_data)

        # Retornamos la ruta absoluta del archivo de audio resultante
        abs_path = os.path.abspath(temp_file.name)
        return abs_path
