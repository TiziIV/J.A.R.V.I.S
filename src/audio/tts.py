"""
Módulo de Síntesis de Voz (Text-To-Speech) de J.A.R.V.I.S.
Implementa streaming por oraciones, síntesis premium con edge-tts,
reproducción sin bloqueos mediante pygame, y fallback blindado a PowerShell System.Speech (español).
"""

import asyncio
import base64
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Optional, Union

import edge_tts
import pygame


class TextToSpeech:
    """
    Controlador de síntesis de voz de J.A.R.V.I.S. con soporte de streaming por fragmentos,
    voces neuronales en español vía edge-tts y respaldo offline local mediante System.Speech.
    """

    def __init__(self, voice: str = "es-AR-TomasNeural") -> None:
        """
        Inicializa el motor de TTS y el pool de subprocesos dedicado.

        Args:
            voice (str): Voz neuronal de edge-tts (ej. 'es-AR-TomasNeural' o 'es-ES-AlvaroNeural').
        """
        self.voice: str = voice
        # Indicador de estado para evitar que el micrófono escuche mientras el asistente habla (anti-eco)
        self.is_speaking: bool = False
        # Circuit Breaker: si edge-tts falla una vez (403/red), se conmuta permanentemente a System.Speech
        self.edge_tts_disabled: bool = False

        # Ejecutor de un solo trabajador para secuenciar la reproducción sin trabar el hilo principal
        self._executor: ThreadPoolExecutor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-tts")

        # Inicializamos el subsistema de audio de pygame si aún no está inicializado
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init()
        except Exception as error:
            print(f"[TTS Init Warning] No se pudo inicializar pygame mixer en este entorno: {error}")

        print(f"Inicializando TextToSpeech (Voz principal: {self.voice})...")

    @staticmethod
    def _build_powershell_command(temp_file_path: str) -> list[str]:
        """
        Genera el comando seguro de PowerShell para leer el archivo temporal
        y reproducirlo síncronamente con System.Speech, filtrando OBLIGATORIAMENTE
        por idioma español (Culture -eq 'es') para evitar voces robóticas en inglés.
        """
        escaped_path = temp_file_path.replace("'", "''")
        script = f"""
Add-Type -AssemblyName System.Speech
$text = Get-Content -LiteralPath '{escaped_path}' -Raw -Encoding UTF8
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {{
    # Filtramos OBLIGATORIAMENTE por 'Culture -eq es' o nombres hispanohablantes conocidos
    $spanishVoice = $synth.GetInstalledVoices() | Where-Object {{
        $_.Enabled -and (
            $_.VoiceInfo.Culture.TwoLetterISOLanguageName -eq 'es' -or
            $_.VoiceInfo.Culture.Name -like 'es-*' -or
            $_.VoiceInfo.Name -match 'Spanish|Helena|Sabina|Laura|Pablo|Raul'
        )
    }} | Select-Object -First 1

    if ($spanishVoice) {{
        $synth.SelectVoice($spanishVoice.VoiceInfo.Name)
    }} else {{
        # Si Windows no tiene ninguna voz española instalada, advertimos para evitar voz en inglés
        Write-Warning "No se encontró voz española nativa en System.Speech."
    }}

    $synth.Rate = 1
    $synth.Volume = 100
    $synth.Speak($text)
}} finally {{
    $synth.Dispose()
}}
"""
        encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_script]

    def _speak_powershell(self, text: str) -> None:
        """
        Método de respaldo offline local utilizando PowerShell y System.Speech.
        Escribe el texto en un archivo temporal con UTF-8 BOM y lo reproduce síncronamente.
        """
        temp_file_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w", encoding="utf-8-sig") as temp_file:
                temp_file.write(text)
                temp_file_path = temp_file.name

            cmd = self._build_powershell_command(temp_file_path)
            subprocess.run(
                cmd,
                check=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as error:
            print(f"[TTS Fallback Error] Falló locución en PowerShell: {error}")
        finally:
            if temp_file_path and os.path.exists(temp_file_path):
                for _ in range(5):
                    try:
                        os.remove(temp_file_path)
                        break
                    except Exception:
                        time.sleep(0.1)

    async def _synthesize_edge_tts(self, text: str, output_path: str) -> None:
        """Sintetiza el texto de forma asíncrona a un archivo MP3 mediante edge-tts."""
        communicate = edge_tts.Communicate(text, self.voice)
        await communicate.save(output_path)

    def _play_audio_file(self, file_path: str) -> None:
        """Reproduce un archivo de audio mediante pygame liberando el descriptor al terminar."""
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init()
            pygame.mixer.music.load(file_path)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                pygame.time.Clock().tick(20)
            # Descargamos la pista para que Windows desbloquee el archivo en disco
            pygame.mixer.music.unload()
        except Exception as error:
            print(f"[TTS Playback Error] Falló la reproducción con pygame: {error}")

    def _speak_chunk(self, chunk: str) -> None:
        """
        Sintetiza y reproduce un fragmento u oración de texto.
        Si edge-tts está deshabilitado por el Circuit Breaker, utiliza directamente System.Speech.
        Si edge-tts falla (error 403 / WSServerHandshakeError / red), activa el Circuit Breaker
        para todas las llamadas futuras e invoca el fallback local en PowerShell.
        """
        # Si el Circuit Breaker ya fue activado, vamos directo al motor local de Windows
        if self.edge_tts_disabled:
            self._speak_powershell(chunk)
            return

        temp_mp3_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as temp_mp3:
                temp_mp3_path = temp_mp3.name

            # Ejecutamos la tarea asíncrona de edge-tts dentro del hilo trabajador
            asyncio.run(self._synthesize_edge_tts(chunk, temp_mp3_path))

            # Reproducimos el archivo generado
            self._play_audio_file(temp_mp3_path)
        except Exception:
            # Activación del Circuit Breaker: fija el motor local System.Speech sin imprimir URLs largas
            self.edge_tts_disabled = True
            print("[TTS] Edge-TTS bloqueado (403). Fijando motor local System.Speech como predeterminado.")
            self._speak_powershell(chunk)
        finally:
            if temp_mp3_path and os.path.exists(temp_mp3_path):
                for _ in range(5):
                    try:
                        os.remove(temp_mp3_path)
                        break
                    except Exception:
                        time.sleep(0.1)

    def _process_stream(self, text_or_stream: Union[str, Iterable[str]]) -> None:
        """
        Bucle procesador de locución. Activa is_speaking = True al inicio
        y lo apaga al finalizar toda la cola de fragmentos.
        """
        self.is_speaking = True
        try:
            if isinstance(text_or_stream, str):
                clean = text_or_stream.strip()
                if clean:
                    self._speak_chunk(clean)
            else:
                for chunk in text_or_stream:
                    if chunk and chunk.strip():
                        self._speak_chunk(chunk.strip())
        finally:
            self.is_speaking = False

    @staticmethod
    def _clean_for_voice(text: str) -> str:
        """
        Limpia el texto antes de enviarlo al motor de síntesis de voz.

        1. Elimina caracteres de formato Markdown (*, #, `, _) que el TTS leería literalmente.
        2. Trunca el texto a 300 caracteres, cortando en el punto seguido más cercano
           para no romper una oración a la mitad.

        Args:
            text (str): Texto crudo recibido del LLM.

        Returns:
            str: Texto limpio y acotado, listo para sintetizar.
        """
        # Quitamos símbolos de Markdown: asteriscos, almohadillas, acentos graves, guiones bajos
        text = re.sub(r"[*#`_]", "", text)
        # Si el texto supera los 300 caracteres, buscamos el último punto antes del límite
        if len(text) > 300:
            cut = text[:300].rfind(".")
            # Si encontramos un punto, cortamos ahí (incluido); si no, cortamos en bruto
            text = text[: cut + 1] if cut > 0 else text[:300]
        return text.strip()

    def speak(self, text_or_stream: Union[str, Iterable[str]]) -> Optional[Future[None]]:
        """
        Encola la locución de un texto completo o un generador de oraciones en streaming.

        Args:
            text_or_stream: Texto simple (str) o iterable/generador de oraciones (Iterable[str]).

        Returns:
            Future que completa al finalizar la reproducción, o None si la entrada está vacía.
        """
        if isinstance(text_or_stream, str):
            if not text_or_stream or not text_or_stream.strip():
                print("Texto vacío, omitiendo síntesis de voz.")
                return None
            # Aplicamos limpieza de Markdown y truncado antes de enviar al motor de síntesis
            clean_text = self._clean_for_voice(text_or_stream.strip())
            if not clean_text:
                print("Texto vacío tras limpieza, omitiendo síntesis de voz.")
                return None
            print(f"[TTS] Reproduciendo locución ({len(clean_text)} caracteres)...")
            return self._executor.submit(self._process_stream, clean_text)

        print("[TTS] Iniciando reproducción en streaming de oraciones...")
        return self._executor.submit(self._process_stream, text_or_stream)

    def close(self) -> None:
        """Espera las tareas pendientes y libera los recursos del reproductor."""
        self._executor.shutdown(wait=True)
        try:
            if pygame.mixer.get_init():
                pygame.mixer.quit()
        except Exception:
            pass
