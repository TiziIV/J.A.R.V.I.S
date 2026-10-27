"""
Módulo del cerebro local de J.A.R.V.I.S. utilizando Ollama.
Implementa streaming en tiempo real (stream=True) segmentado por oraciones
para alimentar al módulo TTS sin latencia.
"""

from collections.abc import Generator
from typing import Optional
import ollama

from config.prompts import JARVIS_SYSTEM_PROMPT
from config.settings import OLLAMA_BASE_URL, OLLAMA_MODEL


class OllamaBrain:
    """
    Clase encargada de gestionar la inteligencia artificial local utilizando Ollama.
    Actúa como el 'cerebro' del asistente J.A.R.V.I.S., procesando texto y generando respuestas
    en streaming optimizadas para síntesis de voz inmediata.
    """

    def __init__(self, model_name: str = OLLAMA_MODEL, base_url: str = OLLAMA_BASE_URL) -> None:
        """
        Inicializa el cliente de Ollama configurando el modelo y la URL del servidor local.

        Args:
            model_name (str): Nombre del modelo LLM local (ej. 'qwen2.5-coder:3b' o 'llama3').
            base_url (str): Dirección donde corre el servicio de Ollama (ej. 'http://localhost:11434').
        """
        self.model_name: str = model_name
        self.base_url: str = base_url
        self.client: ollama.Client = ollama.Client(host=self.base_url)

        print(f"Inicializando cerebro local Ollama con modelo '{self.model_name}' en '{self.base_url}'...")

    def generate_response_stream(
        self, prompt: str, system_prompt: Optional[str] = None
    ) -> Generator[str, None, None]:
        """
        Genera texto en streaming desde Ollama (stream=True), acumulando tokens y
        emitiendo ('yield') oraciones completas delimitadas por '.', '?' o '!' en tiempo real.
        Permite que el TTS comience a hablar inmediatamente la primera oración mientras
        el LLM continúa generando el resto.

        Args:
            prompt (str): Mensaje o pregunta del usuario.
            system_prompt (str, optional): Instrucciones del sistema y contexto conversacional.

        Yields:
            str: Oración completa lista para ser sintetizada por el TTS.
        """
        if system_prompt is None:
            system_prompt = JARVIS_SYSTEM_PROMPT

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        try:
            # Petición HTTP a Ollama con stream=True para recibir tokens a medida que se generan
            response_stream = self.client.chat(
                model=self.model_name,
                messages=messages,
                stream=True,
                options={
                    "temperature": 0.3,
                    "num_predict": 300,  # Suficiente para JSON completo del IntentRouter y respuestas conversacionales
                },
            )

            buffer = ""
            for chunk in response_stream:
                token = ""
                if isinstance(chunk, dict):
                    token = chunk.get("message", {}).get("content", "")
                else:
                    token = getattr(getattr(chunk, "message", None), "content", "")

                if not token:
                    continue

                buffer += token

                # Buscamos si en el búfer se completó una o más oraciones (delimitadas por signo y espacio/salto)
                while True:
                    split_idx = -1
                    for punct in (". ", "? ", "! ", ".\n", "?\n", "!\n"):
                        idx = buffer.find(punct)
                        if idx != -1 and (split_idx == -1 or idx < split_idx):
                            split_idx = idx + 1  # Conservamos el signo de puntuación en la oración actual

                    if split_idx != -1:
                        sentence = buffer[:split_idx].strip()
                        buffer = buffer[split_idx:].lstrip()
                        if sentence:
                            yield sentence
                    else:
                        break

            # Al terminar el stream, emitimos cualquier remanente final en el búfer
            remaining = buffer.strip()
            if remaining:
                yield remaining

        except Exception as error:
            print(f"[Ollama Stream Error] No se pudo comunicar con el servidor local: {error}")
            yield "Lo siento señor, el servidor local de Ollama no responde o no está disponible en este momento."

    def generate_response(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        """
        Genera una respuesta completa consolidando el stream en un único texto continuo.

        Args:
            prompt (str): Texto o pregunta ingresada por el usuario.
            system_prompt (str, optional): Instrucciones del sistema.

        Returns:
            str: Respuesta completa generada por el modelo.
        """
        chunks = list(self.generate_response_stream(prompt, system_prompt=system_prompt))
        return " ".join(chunks).strip()
