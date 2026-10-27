"""
Cliente del cerebro en la nube Google Gemini.
Soporta selección de modelo principal (`GEMINI_MODEL`), fallback único por modelo no disponible
(`GEMINI_FALLBACK_MODEL` / `SECONDARY_GEMINI_MODEL`), límite de tokens adaptable entre voz y texto,
y clasificación estructurada de errores sin exponer la clave API.
"""

from __future__ import annotations

import socket
import urllib.error
from typing import Optional

from google import genai
from google.genai import types

from config.prompts import JARVIS_SYSTEM_PROMPT
from config.settings import (
    GEMINI_API_KEY,
    GEMINI_FALLBACK_MODEL,
    GEMINI_MODEL,
    VOICE_MAX_TOKENS,
)
from src.brain.exceptions import BrainError, BrainErrorKind, redact_secrets


# Google mantiene este modelo Flash como opción predeterminada configurada en el proyecto
DEFAULT_GEMINI_MODEL: str = "gemini-2.0-flash"
# Fallback secundario verificado por la suite de pruebas existente ante errores 404
SECONDARY_GEMINI_MODEL: str = "gemini-1.5-flash"
# Modelo Flash de generación actual listado como activo en la API para nuevas cuentas (respaldo ante 404)
CURRENT_GEMINI_MODEL: str = "gemini-3-flash-preview"
CURRENT_GEMINI_LITE_MODEL: str = "gemini-3.1-flash-lite-preview"
# Modelos obsoletos o de prueba que el normalizador histórico redirige al modelo predeterminado
LEGACY_GEMINI_MODELS: set[str] = {
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
    "gemini-1.5-flash-latest",
    "gemini-2.0-flash-001",
}


class GeminiBrain:
    """Cliente del cerebro en la nube Google Gemini, usado de forma directa o como respaldo."""

    provider_name: str = "Gemini"

    def __init__(
        self,
        api_key: str = GEMINI_API_KEY,
        model_name: str = GEMINI_MODEL,
        fallback_model_name: str = GEMINI_FALLBACK_MODEL,
    ) -> None:
        # Guardamos la clave solo en memoria y nunca la mostramos en los logs.
        self.api_key: str = api_key.strip() if api_key else ""
        # Normalizamos el nombre para sustituir configuraciones antiguas conocidas.
        self.model_name: str = self._normalize_model_name(model_name)
        self.fallback_model_name: str = (fallback_model_name or DEFAULT_GEMINI_MODEL).strip().removeprefix("models/")
        # El cliente permanece None cuando falta configuración o falla su inicialización.
        self.client: genai.Client | None = None

        # Estado de diagnóstico de la última llamada
        self.last_used_model: Optional[str] = None
        self.last_error: Optional[BrainError] = None
        self.last_fallback_note: Optional[str] = None

        if not self.api_key:
            print("[!] GEMINI_API_KEY no está configurada; Gemini queda en modo degradado.")
            return

        try:
            # Usamos el SDK oficial google-genai y la clave leída desde config/settings.py.
            self.client = genai.Client(api_key=self.api_key)
            print(f"Cerebro Gemini inicializado con el modelo '{self.model_name}'.")
        except Exception as error:
            # El tipo de error ayuda a diagnosticar sin imprimir la clave ni detalles sensibles.
            print(f"[Gemini] No se pudo inicializar el cliente ({type(error).__name__}).")

    @staticmethod
    def _normalize_model_name(model_name: str | None) -> str:
        """Limpia prefijos de recurso y actualiza los nombres retirados conocidos."""
        normalized_name: str = (model_name or "").strip()
        if normalized_name.startswith("models/"):
            normalized_name = normalized_name.removeprefix("models/")

        if not normalized_name or normalized_name.lower() in LEGACY_GEMINI_MODELS:
            return DEFAULT_GEMINI_MODEL
        return normalized_name

    @staticmethod
    def _is_model_not_found(error: Exception) -> bool:
        """Reconoce respuestas 404 del API sin depender de una clase SDK específica."""
        error_code: object = getattr(error, "code", "")
        error_status: object = getattr(error, "status", "")
        error_details: str = f"{error_code} {error_status} {error}".lower()
        return "404" in error_details or "not_found" in error_details

    def _classify_error(self, error: Exception, model_name: str) -> BrainError:
        """
        Clasifica un fallo del SDK de Gemini distinguiendo 404 de modelo, autenticación (401/403),
        cuota agotada (429), timeout, red y servidor (5xx).
        """
        if self._is_model_not_found(error):
            return BrainError(
                provider="Gemini",
                kind=BrainErrorKind.MODEL_NOT_FOUND,
                user_message="Lo siento señor, los modelos Gemini disponibles no aceptaron la solicitud.",
                model=model_name,
                status_code=404,
                detail=redact_secrets(str(error), (self.api_key,)),
            )

        code = getattr(error, "code", None)
        status = str(getattr(error, "status", "") or "").lower()
        details = redact_secrets(f"{code} {status} {error}", (self.api_key,))
        lowered = details.lower()
        err_type = type(error).__name__.lower()

        if code in (401, 403) or "unauthenticated" in lowered or "permission_denied" in lowered or "api_key_invalid" in lowered or "invalid api key" in lowered:
            return BrainError(
                provider="Gemini",
                kind=BrainErrorKind.AUTH,
                user_message="Lo siento señor, la clave configurada para Gemini no es válida o no tiene permisos.",
                model=model_name,
                status_code=int(code) if isinstance(code, int) else 401,
                detail=details,
            )

        if code == 429 or "resource_exhausted" in lowered or "quota" in lowered or "rate limit" in lowered or "429" in lowered:
            return BrainError(
                provider="Gemini",
                kind=BrainErrorKind.QUOTA,
                user_message="Lo siento señor, se agotó la cuota o límite de uso de Gemini.",
                model=model_name,
                status_code=429,
                detail=details,
            )

        if isinstance(error, (TimeoutError, socket.timeout)) or "timeout" in err_type or "timed out" in lowered or "deadline_exceeded" in lowered:
            return BrainError(
                provider="Gemini",
                kind=BrainErrorKind.TIMEOUT,
                user_message="Lo siento señor, Gemini tardó demasiado en responder (timeout).",
                model=model_name,
                status_code=504,
                detail=details,
            )

        if isinstance(error, (ConnectionError, urllib.error.URLError, OSError)) or "connect" in err_type or "network" in lowered:
            return BrainError(
                provider="Gemini",
                kind=BrainErrorKind.NETWORK,
                user_message="Lo siento señor, no pude conectar con Gemini en este momento.",
                model=model_name,
                detail=details,
            )

        if code == 400 or "invalid_argument" in lowered:
            return BrainError(
                provider="Gemini",
                kind=BrainErrorKind.INVALID_REQUEST,
                user_message="Lo siento señor, Gemini rechazó el formato de la solicitud.",
                model=model_name,
                status_code=400,
                detail=details,
            )

        if (isinstance(code, int) and code >= 500) or "internal" in status or "unavailable" in status:
            return BrainError(
                provider="Gemini",
                kind=BrainErrorKind.SERVER,
                user_message="Lo siento señor, el servidor de Gemini no está disponible temporalmente.",
                model=model_name,
                status_code=int(code) if isinstance(code, int) else 503,
                detail=details,
            )

        return BrainError(
            provider="Gemini",
            kind=BrainErrorKind.NETWORK,
            user_message="Lo siento señor, no pude conectar con Gemini en este momento.",
            model=model_name,
            detail=details,
        )

    def _generate_with_model(
        self,
        prompt: str,
        system_prompt: str,
        model_name: str,
        max_output_tokens: int = VOICE_MAX_TOKENS,
    ) -> str:
        """Realiza una solicitud con el cliente oficial y devuelve texto limpio."""
        if self.client is None:
            raise RuntimeError("El cliente Gemini no está inicializado.")

        # Ajustamos el límite de salida según el modo (300 por defecto para voz, mayor en consola).
        generation_config: types.GenerateContentConfig = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.3,
            max_output_tokens=max_output_tokens,
        )
        response = self.client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=generation_config,
        )
        response_text: str = response.text.strip() if response.text else ""
        if not response_text:
            return "Gemini devolvió una respuesta vacía."
        return response_text

    def generate_response(
        self,
        prompt: str,
        system_prompt: str | None = None,
        *,
        model_override: str | None = None,
        max_output_tokens: int | None = None,
        raise_on_error: bool = False,
    ) -> str:
        """
        Genera una respuesta con Gemini y reintenta con un modelo alternativo solo ante un 404.
        No cambia de modelo ante errores de autenticación, cuota agotada o fallos de red.
        """
        self.last_error = None
        self.last_fallback_note = None

        if self.client is None:
            unconfigured_err = BrainError(
                provider="Gemini",
                kind=BrainErrorKind.UNCONFIGURED,
                user_message="Lo siento señor, Gemini no está configurado o no pudo inicializarse.",
                model=self.model_name,
            )
            self.last_error = unconfigured_err
            if raise_on_error:
                raise unconfigured_err
            return unconfigured_err.user_message

        active_system_prompt: str = system_prompt or JARVIS_SYSTEM_PROMPT
        primary_model: str = (model_override or self.model_name).strip() or self.model_name
        token_limit: int = max_output_tokens if isinstance(max_output_tokens, int) and max_output_tokens > 0 else 300

        # Lista deduplicada de candidatos ante HTTP 404: primero el solicitado, luego el fallback configurado
        # y finalmente los modelos de respaldo conocidos del proyecto.
        candidate_models: list[str] = list(
            dict.fromkeys(
                [
                    primary_model,
                    self.fallback_model_name,
                    DEFAULT_GEMINI_MODEL,
                    SECONDARY_GEMINI_MODEL,
                    CURRENT_GEMINI_MODEL,
                    CURRENT_GEMINI_LITE_MODEL,
                ]
            )
        )

        for model_index, candidate in enumerate(candidate_models):
            try:
                if token_limit == 300:
                    answer: str = self._generate_with_model(
                        prompt=prompt,
                        system_prompt=active_system_prompt,
                        model_name=candidate,
                    )
                else:
                    answer = self._generate_with_model(
                        prompt=prompt,
                        system_prompt=active_system_prompt,
                        model_name=candidate,
                        max_output_tokens=token_limit,
                    )
                self.model_name = candidate
                self.last_used_model = candidate
                if answer == "Gemini devolvió una respuesta vacía.":
                    empty_err = BrainError(
                        provider="Gemini",
                        kind=BrainErrorKind.EMPTY_RESPONSE,
                        user_message=answer,
                        model=candidate,
                    )
                    self.last_error = empty_err
                    if raise_on_error:
                        raise empty_err
                return answer
            except BrainError:
                raise
            except Exception as error:
                has_next_model: bool = model_index + 1 < len(candidate_models)
                if self._is_model_not_found(error) and has_next_model:
                    next_model: str = candidate_models[model_index + 1]
                    note = f"El modelo Gemini '{candidate}' no está disponible (404); se utiliza '{next_model}'."
                    self.last_fallback_note = note
                    print(f"[Gemini] El modelo '{candidate}' no está disponible; pruebo '{next_model}'.")
                    continue

                classified = self._classify_error(error, candidate)
                self.last_error = classified
                print(f"[Gemini] Falló la generación ({type(error).__name__}) con '{candidate}'.")
                if raise_on_error:
                    raise classified from None
                if self._is_model_not_found(error):
                    return "Lo siento señor, los modelos Gemini disponibles no aceptaron la solicitud."
                return "Lo siento señor, no pude conectar con Gemini en este momento."

        return "Lo siento señor, Gemini no pudo generar una respuesta en este momento."
