"""
Cliente ligero para el motor cloud Anthropic Claude utilizando la librería estándar (`urllib.request`).

Justificación técnica junior:
1. No instalamos el SDK pesado `anthropic` para mantener liviano el entorno de J.A.R.V.I.S.
2. Usamos el endpoint oficial `POST https://api.anthropic.com/v1/messages` documentado por Anthropic,
   con los encabezados obligatorios `x-api-key`, `anthropic-version: 2023-06-01` y `content-type: application/json`.
3. Separamos las funciones de construcción del payload, transporte HTTP y clasificación de errores
   para poder probar cada caso offline mediante mocks sin exponer jamás la clave API.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Optional

from config.prompts import JARVIS_SYSTEM_PROMPT
from config.settings import (
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    CLOUD_TIMEOUT,
    VOICE_MAX_TOKENS,
)
from src.brain.exceptions import BrainError, BrainErrorKind, redact_secrets

ANTHROPIC_MESSAGES_URL: str = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION: str = "2023-06-01"


class ClaudeBrain:
    """Cliente del cerebro en la nube Anthropic Claude."""

    provider_name: str = "Claude"

    def __init__(
        self,
        api_key: str = ANTHROPIC_API_KEY,
        model_name: str = CLAUDE_MODEL,
        endpoint: str = ANTHROPIC_MESSAGES_URL,
        timeout: float = CLOUD_TIMEOUT,
    ) -> None:
        self.api_key: str = api_key.strip() if api_key else ""
        self.model_name: str = (model_name or CLAUDE_MODEL).strip() or "claude-sonnet-4-6"
        self.endpoint: str = endpoint
        self.timeout: float = float(timeout) if timeout and timeout > 0 else 15.0

        self.last_used_model: Optional[str] = None
        self.last_error: Optional[BrainError] = None
        self.last_fallback_note: Optional[str] = None

        if not self.api_key:
            print("[!] ANTHROPIC_API_KEY no está configurada; Claude queda en modo degradado.")
        else:
            print(f"Cerebro Claude inicializado con el modelo '{self.model_name}'.")

    @property
    def is_configured(self) -> bool:
        """Indica si el cliente dispone de una clave API no vacía."""
        return bool(self.api_key)

    def _build_headers(self) -> dict[str, str]:
        """Construye los encabezados requeridos por la API Messages de Anthropic."""
        return {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
            "accept": "application/json",
        }

    @staticmethod
    def _build_payload(
        prompt: str,
        system_prompt: str,
        model_name: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        """Construye el cuerpo JSON conforme al esquema oficial de `/v1/messages`."""
        return {
            "model": model_name,
            "max_tokens": max_tokens,
            "temperature": 0.3,
            "system": system_prompt,
            "messages": [
                {"role": "user", "content": prompt},
            ],
        }

    @staticmethod
    def _extract_text(response_data: dict[str, Any]) -> str:
        """Extrae el texto de la lista `content` devuelta por Anthropic."""
        content_blocks = response_data.get("content")
        if not isinstance(content_blocks, list):
            return ""
        parts: list[str] = []
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text_val = block.get("text")
                if isinstance(text_val, str) and text_val.strip():
                    parts.append(text_val.strip())
        return "\n".join(parts).strip()

    def _classify_http_error(
        self,
        status_code: int,
        raw_body: str,
        target_model: str,
    ) -> BrainError:
        """
        Distingue entre modelo inexistente, autenticación inválida, límites de uso,
        solicitud inválida y errores del servidor de Anthropic basándose en el código HTTP
        y la estructura JSON `{"error": {"type": "...", "message": "..."}}`.
        """
        error_type = ""
        error_message = ""
        try:
            parsed = json.loads(raw_body) if raw_body else {}
            if isinstance(parsed, dict):
                err_obj = parsed.get("error", {})
                if isinstance(err_obj, dict):
                    error_type = str(err_obj.get("type", "") or "").lower()
                    error_message = str(err_obj.get("message", "") or "")
        except Exception:
            error_message = raw_body

        safe_detail = redact_secrets(f"HTTP {status_code} ({error_type}): {error_message or raw_body}", (self.api_key,))
        lowered_msg = (error_message or raw_body).lower()

        # 1. Autenticación o permisos inválidos (HTTP 401 / 403)
        if status_code in (401, 403) or error_type in ("authentication_error", "permission_error"):
            return BrainError(
                provider="Claude",
                kind=BrainErrorKind.AUTH,
                user_message="Lo siento señor, la clave de API de Claude (Anthropic) es inválida o no tiene permisos.",
                model=target_model,
                status_code=status_code,
                detail=safe_detail,
            )

        # 2. Límite de cuota o rate-limit (HTTP 429 / 529 overloaded)
        if status_code in (429, 529) or error_type in ("rate_limit_error", "overloaded_error") or "credit balance" in lowered_msg:
            return BrainError(
                provider="Claude",
                kind=BrainErrorKind.QUOTA,
                user_message="Lo siento señor, se alcanzó el límite de uso o cuota disponible en Claude.",
                model=target_model,
                status_code=status_code,
                detail=safe_detail,
            )

        # 3. Modelo inexistente: HTTP 404 con not_found_error sobre model, o HTTP 400 que menciona explícitamente el modelo
        is_model_error = (
            (status_code == 404 and (error_type == "not_found_error" or "model" in lowered_msg))
            or (status_code == 400 and "model" in lowered_msg and ("not found" in lowered_msg or "does not exist" in lowered_msg or "invalid" in lowered_msg))
        )
        if is_model_error:
            return BrainError(
                provider="Claude",
                kind=BrainErrorKind.MODEL_NOT_FOUND,
                user_message=f"Lo siento señor, el modelo Claude '{target_model}' no existe o no está disponible.",
                model=target_model,
                status_code=status_code,
                detail=safe_detail,
            )

        # 4. Solicitud inválida (HTTP 400 / 404 de ruta incorrecta)
        if status_code in (400, 404, 422) or error_type == "invalid_request_error":
            return BrainError(
                provider="Claude",
                kind=BrainErrorKind.INVALID_REQUEST,
                user_message="Lo siento señor, Claude rechazó el formato de la solicitud.",
                model=target_model,
                status_code=status_code,
                detail=safe_detail,
            )

        # 5. Error del servidor (HTTP 5xx)
        if status_code >= 500 or error_type == "api_error":
            return BrainError(
                provider="Claude",
                kind=BrainErrorKind.SERVER,
                user_message="Lo siento señor, los servidores de Claude presentan un error temporal.",
                model=target_model,
                status_code=status_code,
                detail=safe_detail,
            )

        return BrainError(
            provider="Claude",
            kind=BrainErrorKind.UNKNOWN,
            user_message="Lo siento señor, no pude obtener respuesta de Claude en este momento.",
            model=target_model,
            status_code=status_code,
            detail=safe_detail,
        )

    def _send_request(self, payload: dict[str, Any], target_model: str) -> dict[str, Any]:
        """Envía la petición HTTP POST con timeout acotado y parsea la respuesta JSON."""
        data_bytes = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=data_bytes,
            headers=self._build_headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw_text = response.read().decode("utf-8", errors="replace")
                return json.loads(raw_text) if raw_text else {}
        except urllib.error.HTTPError as http_err:
            body_text = ""
            try:
                body_text = http_err.read().decode("utf-8", errors="replace")
            except Exception:
                body_text = str(http_err)
            raise self._classify_http_error(http_err.code, body_text, target_model) from None
        except (TimeoutError, socket.timeout) as timeout_err:
            raise BrainError(
                provider="Claude",
                kind=BrainErrorKind.TIMEOUT,
                user_message="Lo siento señor, la conexión con Claude superó el tiempo de espera (timeout).",
                model=target_model,
                detail=redact_secrets(str(timeout_err), (self.api_key,)),
            ) from None
        except urllib.error.URLError as url_err:
            reason = getattr(url_err, "reason", url_err)
            if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).lower():
                raise BrainError(
                    provider="Claude",
                    kind=BrainErrorKind.TIMEOUT,
                    user_message="Lo siento señor, la conexión con Claude superó el tiempo de espera (timeout).",
                    model=target_model,
                    detail=redact_secrets(str(reason), (self.api_key,)),
                ) from None
            raise BrainError(
                provider="Claude",
                kind=BrainErrorKind.NETWORK,
                user_message="Lo siento señor, no pude conectar con Claude por un problema de red.",
                model=target_model,
                detail=redact_secrets(str(reason), (self.api_key,)),
            ) from None
        except BrainError:
            raise
        except Exception as unexpected:
            raise BrainError(
                provider="Claude",
                kind=BrainErrorKind.NETWORK,
                user_message="Lo siento señor, ocurrió un error al comunicarme con Claude.",
                model=target_model,
                detail=redact_secrets(str(unexpected), (self.api_key,)),
            ) from None

    def generate_response(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        *,
        model_override: Optional[str] = None,
        max_output_tokens: Optional[int] = None,
        raise_on_error: bool = False,
    ) -> str:
        """
        Genera una respuesta utilizando Anthropic Claude.
        Si no hay clave configurada, devuelve un error controlado y explícito.
        """
        self.last_error = None
        self.last_fallback_note = None

        target_model = (model_override or self.model_name).strip() or self.model_name
        if not self.api_key:
            err = BrainError(
                provider="Claude",
                kind=BrainErrorKind.UNCONFIGURED,
                user_message="Lo siento señor, Claude no está configurado (falta ANTHROPIC_API_KEY en el entorno).",
                model=target_model,
            )
            self.last_error = err
            if raise_on_error:
                raise err
            return err.user_message

        active_system_prompt = system_prompt or JARVIS_SYSTEM_PROMPT
        token_limit = int(max_output_tokens) if isinstance(max_output_tokens, int) and max_output_tokens > 0 else VOICE_MAX_TOKENS
        payload = self._build_payload(
            prompt=prompt,
            system_prompt=active_system_prompt,
            model_name=target_model,
            max_tokens=token_limit,
        )

        try:
            response_data = self._send_request(payload, target_model)
            text = self._extract_text(response_data)
            self.last_used_model = str(response_data.get("model") or target_model)
            if not text:
                empty_err = BrainError(
                    provider="Claude",
                    kind=BrainErrorKind.EMPTY_RESPONSE,
                    user_message="Lo siento señor, Claude devolvió una respuesta vacía.",
                    model=self.last_used_model,
                )
                self.last_error = empty_err
                if raise_on_error:
                    raise empty_err
                return empty_err.user_message
            return text
        except BrainError as brain_err:
            self.last_error = brain_err
            print(f"[Claude] Falló la generación ({brain_err.kind.value}) con '{target_model}'.")
            if raise_on_error:
                raise
            return brain_err.user_message
