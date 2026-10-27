"""
Módulo del cerebro local de J.A.R.V.I.S. utilizando Ollama.
Implementa streaming en tiempo real (stream=True) segmentado por oraciones
para alimentar al módulo TTS sin latencia, y soporta selección de modelo por
solicitud (`model_override`) con fallback controlado del modelo alternativo al estándar.
"""

from __future__ import annotations

from collections.abc import Generator
import socket
import urllib.error
from typing import Any, Optional
import ollama

from config.prompts import JARVIS_SYSTEM_PROMPT
from config.settings import (
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    OLLAMA_MODEL_CENSORED,
    OLLAMA_MODEL_UNCENSORED,
)
from src.brain.exceptions import BrainError, BrainErrorKind, redact_secrets


class OllamaBrain:
    """
    Clase encargada de gestionar la inteligencia artificial local utilizando Ollama.
    Actúa como el 'cerebro' local de J.A.R.V.I.S., procesando texto y generando respuestas
    en streaming optimizadas para síntesis de voz o consola.
    """

    provider_name: str = "Ollama"

    def __init__(
        self,
        model_name: str = OLLAMA_MODEL,
        base_url: str = OLLAMA_BASE_URL,
        uncensored_model_name: str = OLLAMA_MODEL_UNCENSORED,
    ) -> None:
        """
        Inicializa el cliente de Ollama configurando el modelo estándar, el alternativo
        y la URL del servidor local.

        Args:
            model_name (str): Modelo LLM local estándar (ej. 'qwen2.5-coder:3b').
            base_url (str): Dirección donde corre el servicio de Ollama ('http://localhost:11434').
            uncensored_model_name (str): Modelo local alternativo para el modo OLLAMA_UNCENSORED.
        """
        self.model_name: str = model_name or OLLAMA_MODEL_CENSORED
        self.uncensored_model_name: str = uncensored_model_name or OLLAMA_MODEL_UNCENSORED
        self.base_url: str = base_url
        self.client: ollama.Client = ollama.Client(host=self.base_url)

        # Registro de diagnóstico de la última solicitud procesada:
        # permite que BrainManager y la consola muestren con exactitud qué modelo respondió.
        self.last_used_model: Optional[str] = None
        self.last_error: Optional[BrainError] = None
        self.last_fallback_note: Optional[str] = None

        print(f"Inicializando cerebro local Ollama con modelo '{self.model_name}' en '{self.base_url}'...")

    @staticmethod
    def _compose_system_prompt(
        system_prompt: Optional[str],
        context_prompt: Optional[str],
    ) -> str:
        """
        Combina el prompt del sistema con un bloque opcional de contexto conversacional
        sin perder ninguno de los dos.
        """
        base = system_prompt if system_prompt is not None else JARVIS_SYSTEM_PROMPT
        if context_prompt and context_prompt.strip() and context_prompt.strip() not in base:
            return f"{base}\n\n{context_prompt.strip()}"
        return base

    @staticmethod
    def _classify_ollama_error(
        error: Exception,
        target_model: str,
        *,
        partial_output: bool = False,
    ) -> BrainError:
        """
        Clasifica una excepción de Ollama inspeccionando primero el tipo de excepción,
        el código de estado HTTP (`status_code`) y la estructura del error antes del texto.
        """
        if partial_output:
            return BrainError(
                provider="Ollama",
                kind=BrainErrorKind.PARTIAL_STREAM,
                user_message="La transmisión local de Ollama se interrumpió después de emitir una respuesta parcial.",
                model=target_model,
                detail=str(error),
                partial_output=True,
            )

        status_code: Optional[int] = getattr(error, "status_code", None)
        raw_error_field = getattr(error, "error", None)
        error_text = str(raw_error_field if raw_error_field is not None else error)
        lowered = error_text.lower()
        error_type_name = type(error).__name__.lower()

        # 1. Timeout explícito de socket / httpx
        if isinstance(error, (TimeoutError, socket.timeout)) or "timeout" in error_type_name or "timed out" in lowered:
            return BrainError(
                provider="Ollama",
                kind=BrainErrorKind.TIMEOUT,
                user_message="Lo siento señor, el servidor local de Ollama tardó demasiado en responder (timeout).",
                model=target_model,
                status_code=status_code,
                detail=error_text,
            )

        # 2. Daemon de Ollama apagado o inaccesible (ConnectionError, ConnectError, URLError)
        if (
            isinstance(error, (ConnectionError, urllib.error.URLError))
            or "connecterror" in error_type_name
            or "connection" in error_type_name
            or "failed to connect to ollama" in lowered
            or "connection refused" in lowered
            or "actively refused" in lowered
        ):
            return BrainError(
                provider="Ollama",
                kind=BrainErrorKind.DAEMON_OFFLINE,
                user_message="Lo siento señor, el servidor local de Ollama no responde o no está disponible en este momento.",
                model=target_model,
                status_code=status_code,
                detail=error_text,
            )

        # 3. Modelo inexistente en Ollama: ResponseError con HTTP 404 y estructura de modelo faltante
        is_404 = status_code == 404 or ("status code: 404" in lowered)
        mentions_model_missing = (
            ("model" in lowered and ("not found" in lowered or "no encontrado" in lowered or "pull" in lowered))
            or (is_404 and "model" in lowered)
        )
        if (is_404 and mentions_model_missing) or (
            isinstance(error, getattr(ollama, "ResponseError", Exception)) and mentions_model_missing
        ):
            install_hint = f"ollama pull {target_model}"
            return BrainError(
                provider="Ollama",
                kind=BrainErrorKind.MODEL_NOT_FOUND,
                user_message=(
                    f"El modelo local '{target_model}' no está instalado en Ollama. "
                    f"Puede instalarlo manualmente ejecutando: {install_hint}"
                ),
                model=target_model,
                status_code=status_code or 404,
                detail=error_text,
            )

        # 4. Solicitud inválida (HTTP 400)
        if status_code == 400:
            return BrainError(
                provider="Ollama",
                kind=BrainErrorKind.INVALID_REQUEST,
                user_message=f"La solicitud enviada a Ollama para el modelo '{target_model}' no es válida.",
                model=target_model,
                status_code=status_code,
                detail=error_text,
            )

        # 5. Error interno del servidor Ollama (HTTP 5xx)
        if isinstance(status_code, int) and status_code >= 500:
            return BrainError(
                provider="Ollama",
                kind=BrainErrorKind.SERVER,
                user_message="Lo siento señor, el servidor local de Ollama sufrió un error interno.",
                model=target_model,
                status_code=status_code,
                detail=error_text,
            )

        # 6. Cualquier otro fallo de comunicación o de ejecución
        return BrainError(
            provider="Ollama",
            kind=BrainErrorKind.UNKNOWN,
            user_message="Lo siento señor, el servidor local de Ollama no responde o no está disponible en este momento.",
            model=target_model,
            status_code=status_code,
            detail=redact_secrets(error_text),
        )

    def _stream_once(
        self,
        target_model: str,
        messages: list[dict[str, str]],
        options: dict[str, Any],
    ) -> Generator[str, None, None]:
        """
        Ejecuta una única solicitud en streaming contra Ollama con el modelo indicado y
        emite oraciones completas a medida que llegan los tokens.
        """
        response_stream = self.client.chat(
            model=target_model,
            messages=messages,
            stream=True,
            options=options,
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

        remaining = buffer.strip()
        if remaining:
            yield remaining

    def generate_response_stream(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        *,
        model_override: Optional[str] = None,
        context_prompt: Optional[str] = None,
        num_predict: Optional[int] = None,
        temperature: float = 0.3,
        raise_on_error: bool = False,
    ) -> Generator[str, None, None]:
        """
        Genera texto en streaming desde Ollama (`stream=True`), acumulando tokens y
        emitiendo (`yield`) oraciones completas delimitadas por '.', '?' o '!' en tiempo real.

        Si se solicita un `model_override` (por ejemplo el modelo de `OLLAMA_UNCENSORED`)
        y Ollama confirma que dicho modelo no está instalado (404 / model not found),
        informa cómo instalarlo con `ollama pull <modelo>` e intenta el modelo estándar
        `self.model_name` una sola vez, siempre que no se haya emitido ya una respuesta parcial.
        """
        effective_system_prompt = self._compose_system_prompt(system_prompt, context_prompt)
        messages = [
            {"role": "system", "content": effective_system_prompt},
            {"role": "user", "content": prompt},
        ]
        predict_limit = int(num_predict) if isinstance(num_predict, int) and num_predict > 0 else 300
        options: dict[str, Any] = {
            "temperature": temperature,
            "num_predict": predict_limit,
        }

        requested_model = (model_override or self.model_name).strip() or self.model_name
        self.last_error = None
        self.last_fallback_note = None
        emitted_any = False

        try:
            for sentence in self._stream_once(requested_model, messages, options):
                if not emitted_any:
                    self.last_used_model = requested_model
                emitted_any = True
                yield sentence
            if not emitted_any:
                self.last_used_model = requested_model
            return

        except Exception as primary_error:
            classified = self._classify_ollama_error(
                primary_error,
                requested_model,
                partial_output=emitted_any,
            )

            # Si el modelo alternativo no existe, no hubo salida parcial y existe un modelo estándar distinto,
            # informamos cómo instalarlo manualmente y probamos el modelo estándar UNA sola vez.
            can_fallback_to_standard = (
                classified.kind == BrainErrorKind.MODEL_NOT_FOUND
                and not emitted_any
                and requested_model != self.model_name
                and bool(self.model_name)
            )
            if can_fallback_to_standard:
                install_cmd = f"ollama pull {requested_model}"
                note = (
                    f"Modelo local '{requested_model}' no encontrado en Ollama. "
                    f"Para instalarlo ejecute: `{install_cmd}`. "
                    f"Usando modelo estándar '{self.model_name}' por esta vez."
                )
                self.last_fallback_note = note
                print(f"[Ollama] {note}")
                try:
                    for sentence in self._stream_once(self.model_name, messages, options):
                        if not emitted_any:
                            self.last_used_model = self.model_name
                        emitted_any = True
                        yield sentence
                    if not emitted_any:
                        self.last_used_model = self.model_name
                    return
                except Exception as secondary_error:
                    classified = self._classify_ollama_error(
                        secondary_error,
                        self.model_name,
                        partial_output=emitted_any,
                    )

            self.last_error = classified
            if classified.kind == BrainErrorKind.MODEL_NOT_FOUND:
                print(
                    f"[Ollama] Modelo '{classified.model}' no instalado. "
                    f"Instálelo manualmente con: ollama pull {classified.model}"
                )
            elif classified.kind == BrainErrorKind.DAEMON_OFFLINE:
                print(f"[Ollama Stream Error] No se pudo comunicar con el servidor local: {classified.detail}")
            else:
                print(f"[Ollama Stream Error] Fallo con '{classified.model}' ({classified.kind.value}): {classified.detail}")

            if raise_on_error:
                raise classified from None
            if not emitted_any:
                yield classified.user_message

    def generate_stream(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        *,
        model_override: Optional[str] = None,
        context_prompt: Optional[str] = None,
        num_predict: Optional[int] = None,
        temperature: float = 0.3,
        raise_on_error: bool = False,
    ) -> Generator[str, None, None]:
        """Alias compatible con `generate_response_stream` que acepta `model_override`."""
        yield from self.generate_response_stream(
            prompt,
            system_prompt=system_prompt,
            model_override=model_override,
            context_prompt=context_prompt,
            num_predict=num_predict,
            temperature=temperature,
            raise_on_error=raise_on_error,
        )

    def generate_response(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        *,
        model_override: Optional[str] = None,
        context_prompt: Optional[str] = None,
        num_predict: Optional[int] = None,
        temperature: float = 0.3,
        raise_on_error: bool = False,
    ) -> str:
        """
        Genera una respuesta completa consolidando el stream en un único texto continuo.
        """
        if model_override is None and context_prompt is None and num_predict is None and not raise_on_error:
            chunks = list(self.generate_response_stream(prompt, system_prompt=system_prompt))
        else:
            chunks = list(
                self.generate_response_stream(
                    prompt,
                    system_prompt=system_prompt,
                    model_override=model_override,
                    context_prompt=context_prompt,
                    num_predict=num_predict,
                    temperature=temperature,
                    raise_on_error=raise_on_error,
                )
            )
        return " ".join(chunks).strip()
