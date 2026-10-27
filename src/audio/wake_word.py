"""Detección local y continua de una palabra de activación."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from typing import Any, Protocol


class _WakeWordModel(Protocol):
    """Interfaz mínima de openWakeWord usada por este módulo."""

    def predict(self, audio: Any) -> Mapping[str, float]:
        ...


class WakeWordDetector:
    """Escucha el micrófono y detecta una palabra configurada localmente.

    `openwakeword` se carga de forma diferida: los demás módulos de audio
    pueden importarse aunque todavía no esté instalada esta dependencia.
    """

    SAMPLE_RATE = 16000
    CHUNK_SIZE = 1280  # 80 ms a 16 kHz, el tamaño esperado por openWakeWord.
    MODEL_CANDIDATES = ("hey_jarvis", "alexa", "timer")

    def __init__(self, threshold: float = 0.5) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold debe estar entre 0.0 y 1.0")

        self.threshold: float = threshold
        self._stop_event: threading.Event = threading.Event()
        self._stream_lock: threading.Lock = threading.Lock()
        self._listen_lock: threading.Lock = threading.Lock()
        self._stream: Any | None = None
        self._model: _WakeWordModel
        self._wake_word_name: str
        self._model, self._wake_word_name = self._load_model()

    @classmethod
    def _load_model(cls) -> tuple[_WakeWordModel, str]:
        """Prueba modelos conocidos en orden y explica fallos de instalación."""
        try:
            from openwakeword.model import Model
        except ImportError as error:
            raise RuntimeError(
                "Falta openwakeword. Instálalo con 'pip install openwakeword' "
                "para habilitar la detección de palabra de activación."
            ) from error

        errors: list[str] = []
        for name in cls.MODEL_CANDIDATES:
            try:
                # La biblioteca resuelve el modelo integrado al inicializar.
                return Model(wakeword_models=[name]), name
            except Exception as error:
                # Si no está disponible localmente, intentamos descargar automáticamente los modelos
                try:
                    import openwakeword.utils
                    openwakeword.utils.download_models([name])
                    return Model(wakeword_models=[name]), name
                except Exception as dl_error:
                    errors.append(f"{name}: carga={error}; descarga={dl_error}")

        details = "; ".join(errors)
        raise RuntimeError(
            "No se pudo cargar ningún modelo de openWakeWord "
            f"({details}). Verifica la instalación y la disponibilidad de los modelos."
        )

    @staticmethod
    def _get_score(predictions: Mapping[str, float], model_name: str) -> float:
        """Obtiene el puntaje del modelo incluso si la biblioteca cambia la clave."""
        if model_name in predictions:
            return float(predictions[model_name])
        # Algunos backends devuelven la etiqueta sin guion bajo o con otro nombre.
        normalized_name = model_name.replace("_", " ").casefold()
        for name, score in predictions.items():
            if name.replace("_", " ").casefold() == normalized_name:
                return float(score)
        return max((float(score) for score in predictions.values()), default=0.0)

    def listen_for_wake_word(self, timeout: float | None = None) -> bool:
        """Escucha hasta detectar la palabra, agotar `timeout` o recibir `stop()`.

        Devuelve `True` si se supera el umbral y `False` al vencer el tiempo o
        detener la escucha. Los errores de micrófono se convierten en RuntimeError.
        """
        if timeout is not None and timeout < 0:
            raise ValueError("timeout no puede ser negativo")
        if not self._listen_lock.acquire(blocking=False):
            raise RuntimeError("Ya hay una escucha de palabra de activación en curso")

        try:
            try:
                import numpy as np
                import sounddevice as sd
            except ImportError as error:
                raise RuntimeError(
                    "Falta sounddevice o numpy para capturar audio del micrófono."
                ) from error

            self._stop_event.clear()
            deadline = None if timeout is None else time.monotonic() + timeout
            # Se importa aquí para que el módulo pueda cargarse sin acceso al dispositivo.
            stream: Any | None = None
            try:
                stream = sd.InputStream(
                    samplerate=self.SAMPLE_RATE,
                    channels=1,
                    dtype="int16",
                    blocksize=self.CHUNK_SIZE,
                )
                stream.start()
            except Exception as error:
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
                raise RuntimeError(
                    f"No se pudo abrir el micrófono para detectar la palabra: {error}"
                ) from error

            with self._stream_lock:
                self._stream = stream

            try:
                while not self._stop_event.is_set():
                    if deadline is not None and time.monotonic() >= deadline:
                        return False
                    try:
                        chunk, overflowed = stream.read(self.CHUNK_SIZE)
                    except Exception as error:
                        if self._stop_event.is_set():
                            return False
                        raise RuntimeError(f"Error leyendo el micrófono: {error}") from error

                    if overflowed:
                        # Un bloque perdido no debe cancelar toda la escucha.
                        continue
                    audio_chunk = np.asarray(chunk, dtype=np.int16).reshape(-1)
                    predictions = self._model.predict(audio_chunk)
                    score = self._get_score(predictions, self._wake_word_name)
                    if score >= self.threshold:
                        return True
                return False
            finally:
                with self._stream_lock:
                    if self._stream is stream:
                        self._stream = None
                # Cerrar también ante detección y excepciones libera el micrófono
                # antes de que AudioRecorder inicie su propia captura.
                try:
                    stream.stop()
                finally:
                    stream.close()
        finally:
            self._listen_lock.release()

    def stop(self) -> None:
        """Solicita terminar la escucha actual e interrumpe la lectura si es posible."""
        self._stop_event.set()
        with self._stream_lock:
            stream = self._stream
        if stream is not None:
            # abort() evita esperar a que llegue otro bloque del micrófono.
            try:
                stream.abort()
            except Exception:
                # La escucha detectará el evento y limpiará el stream en su finally.
                pass
