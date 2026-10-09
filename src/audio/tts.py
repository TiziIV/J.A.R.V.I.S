import asyncio
import os
import tempfile
import time
from concurrent.futures import Future, ThreadPoolExecutor

import edge_tts
from playsound import playsound


class TextToSpeech:
    """Genera voz en segundo plano y permite consultar cuándo terminó la reproducción."""

    def __init__(self, voice: str = "es-AR-TomasNeural"):
        self.voice = voice
        # Un único trabajador evita reproducir dos respuestas a la vez.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-tts")
        print(f"Inicializando TextToSpeech con la voz neuronal '{self.voice}'...")

    async def _generate_audio_file(self, text: str, output_path: str) -> None:
        # edge-tts usa asyncio para descargar el audio sin bloquear este trabajador.
        communicate = edge_tts.Communicate(text, self.voice)
        await communicate.save(output_path)

    def speak(self, text: str) -> Future[None] | None:
        """Encola la voz y retorna inmediatamente un Future para seguir su estado."""
        if not text or not text.strip():
            print("Texto vacío, omitiendo síntesis de voz.")
            return None
        print(f"Sintetizando voz: \"{text}\"")
        # La red, la generación y el reproductor corren fuera del hilo conversacional.
        return self._executor.submit(self._speak_blocking, text.strip())

    def _speak_blocking(self, text: str) -> None:
        temp_path = ""
        try:
            # El archivo queda cerrado antes de que edge-tts lo escriba, algo necesario en Windows.
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as temp_file:
                temp_path = temp_file.name
            asyncio.run(self._generate_audio_file(text, temp_path))
            # playsound bloquea solo al trabajador dedicado, no al hilo que administra la conversación.
            playsound(temp_path)
        except Exception as error:
            print(f"Error durante la síntesis o reproducción de voz: {error}")
        finally:
            if temp_path:
                self._remove_temporary_file(temp_path)

    @staticmethod
    def _remove_temporary_file(path: str) -> None:
        # Windows puede mantener el MP3 bloqueado unos instantes después de terminar el reproductor.
        for attempt in range(5):
            try:
                if os.path.exists(path):
                    os.remove(path)
                return
            except PermissionError:
                if attempt == 4:
                    print(f"No se pudo eliminar el audio temporal bloqueado: {path}")
                else:
                    time.sleep(0.2 * (attempt + 1))
            except OSError as error:
                print(f"No se pudo eliminar el audio temporal: {error}")
                return

    def close(self) -> None:
        # Espera al último audio para no cerrar el proceso mientras aún se está reproduciendo.
        self._executor.shutdown(wait=True)
