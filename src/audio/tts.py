import os
import asyncio
import tempfile
import edge_tts
from playsound import playsound

class TextToSpeech:
    """
    Clase encargada de convertir texto a voz (Text-to-Speech) utilizando edge-tts.
    Genera audio neuronal en español y lo reproduce de forma confiable en Windows.
    """
    
    def __init__(self, voice: str = "es-AR-TomasNeural"):
        """
        Inicializa el sintetizador de voz con una voz neuronal predeterminada.
        
        Args:
            voice (str): Identificador de la voz de Microsoft Edge TTS (por defecto 'es-AR-TomasNeural').
                         También se pueden usar opciones como 'es-ES-AlvaroNeural' o 'es-MX-DaliaNeural'.
        """
        self.voice = voice
        print(f"Inicializando TextToSpeech con la voz neuronal '{self.voice}'...")

    async def _generate_audio_file(self, text: str, output_path: str) -> None:
        """
        Función asíncrona interna para generar el archivo de audio usando edge-tts.
        
        Args:
            text (str): Texto a sintetizar.
            output_path (str): Ruta donde se guardará el archivo MP3 temporal.
        """
        # Creamos comunicador de edge-tts con el texto y la voz seleccionados
        communicate = edge_tts.Communicate(text, self.voice)
        # Guardamos el resultado en el archivo temporal especificado
        await communicate.save(output_path)

    def speak(self, text: str) -> None:
        """
        Método público sincrónico para convertir texto a voz y reproducirlo inmediatamente.
        
        Args:
            text (str): Texto que el asistente pronunciará en voz alta.
        """
        if not text or not text.strip():
            print("Texto vacío, omitiendo síntesis de voz.")
            return

        print(f"Sintetizando voz: \"{text}\"")
        
        temp_mp3 = None
        try:
            # Creamos un archivo temporal con extensión .mp3 que se eliminará manualmente después
            temp_file = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            temp_file.close()
            temp_mp3 = temp_file.name

            # Ejecutamos la corrutina asíncrona de edge-tts usando asyncio.run
            # ¿Por qué? Porque edge-tts es puramente asíncrono y necesitamos llamarlo desde un entorno sincrónico.
            asyncio.run(self._generate_audio_file(text, temp_mp3))

            # Reproducimos el audio usando playsound (compatible con Windows)
            # ¿Por qué playsound? Es simple, sincrónico y bloquea la ejecución hasta que termina de hablar.
            playsound(temp_mp3)

        except Exception as e:
            # Capturamos fallos de red, problemas con edge-tts o errores del reproductor de audio
            print(f"Error durante la síntesis o reproducción de voz: {e}")

        finally:
            # Bloque de limpieza: aseguramos eliminar el archivo temporal MP3 para no acumular basura en disco
            if temp_mp3 and os.path.exists(temp_mp3):
                try:
                    os.remove(temp_mp3)
                except Exception as cleanup_error:
                    print(f"No se pudo eliminar el archivo temporal de audio: {cleanup_error}")
