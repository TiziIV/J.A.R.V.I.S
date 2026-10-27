"""
Cliente ligero para el motor cloud OpenAI GPT utilizando la librería estándar (`urllib.request`).

Justificación técnica junior:
1. Utiliza el endpoint oficial `POST https://api.openai.com/v1/chat/completions` con autenticación
   Bearer (`Authorization: Bearer <OPENAI_API_KEY>`). También admite parsear el esquema de
   `https://api.openai.com/v1/responses` si el usuario personaliza el endpoint.
2. Verifica el contexto de los errores HTTP 404: solo considera que un modelo no existe cuando el
   endpoint consultado es válido (`/v1/chat/completions` o `/v1/responses`) y el cuerpo JSON de
   OpenAI incluye `error.code == "model_not_found"`, `error.param == "model"` o un mensaje explícito
   sobre el modelo. Un 404 genérico de servidor proxy o ruta mal escrita se clasifica como
   `INVALID_REQUEST` y NO activa un reintento ciego con otro modelo.
3. Cuando el modelo principal (`OPENAI_MODEL`) falla por no estar disponible o no ser compatible,
   intenta `OPENAI_FALLBACK_MODEL` (`gpt-4o`) una sola vez e informa cuál terminó respondiendo.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from config.prompts import JARVIS_SYSTEM_PROMPT
from config.settings import (
    CLOUD_TIMEOUT,
    OPENAI_API_KEY,
    OPENAI_FALLBACK_MODEL,
    OPENAI_MODEL,
    VOICE_MAX_TOKENS,
)
from src.brain.exceptions import BrainError, BrainErrorKind, redact_secrets

OPENAI_CHAT_COMPLETIONS_URL: str = "https://api.openai.com/v1/chat/completions"
_VALID_OPENAI_PATHS: tuple[str, ...] = ("/v1/chat/completions", "/v1/responses")


class OpenAIBrain:
    """Cliente del cerebro en la nube OpenAI GPT."""

    provider_name: str = "GPT"

    def __init__(
        self,
        api_key: str = OPENAI_API_KEY,
        model_name: str = OPENAI_MODEL,
        fallback_model_name: str = OPENAI_FALLBACK_MODEL,
        endpoint: str = OPENAI_CHAT_COMPLETIONS_URL,
        timeout: float = CLOUD_TIMEOUT,
    ) -> None:
        self.api_key: str = api_key.strip() if api_key else ""
        self.model_name: str = (model_name or OPENAI_MODEL).strip() or "gpt-5.4"
        self.fallback_model_name: str = (fallback_model_name or OPENAI_FALLBACK_MODEL).strip() or "gpt-4o"
        self.endpoint: str = endpoint
        self.timeout: float = float(timeout) if timeout and timeout > 0 else 15.0

        self.last_used_model: Optional[str] = None
        self.last_error: Optional[BrainError] = None
        self.last_fallback_note: Optional[str] = None

        if not self.api_key:
            print("[!] OPENAI_API_KEY no está configurada; GPT queda en modo degradado.")
        else:
            print(f"Cerebro GPT inicializado con el modelo '{self.model_name}'.")

    @property
    def is_configured(self) -> bool:
        """Indica si el cliente dispone de una clave API no vacía."""
        return bool(self.api_key)

    def _build_headers(self) -> dict[str, str]:
        """Construye los encabezados HTTP oficiales de OpenAI."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _is_valid_endpoint_format(self) -> bool:
        """Comprueba que la URL apunte a un endpoint oficial reconocido de generación de OpenAI."""
        try:
            parsed = urllib.parse.urlparse(self.endpoint)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                return False
            return any(parsed.path.rstrip("/").endswith(path) for path in _VALID_OPENAI_PATHS)
        except Exception:
            return False

    def _build_payload(
        self,
        prompt: str,
        system_prompt: str,
        model_name: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        """
        Construye el payload compatible con el endpoint configurado (`/v1/chat/completions`
        o `/v1/responses`).
        """
        if self.endpoint.rstrip("/").endswith("/v1/responses"):
            return {
                "model": model_name,
                "instructions": system_prompt,
                "input": prompt,
                "max_output_tokens": max_tokens,
            }
        # En /v1/chat/completions usamos max_completion_tokens (compatible con GPT-5.x y GPT-4o).
        return {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "max_completion_tokens": max_tokens,
        }

    @staticmethod
    def _extract_text(response_data: dict[str, Any]) -> str:
        """Extrae el texto tanto del esquema Chat Completions como del esquema Responses API."""
        # 1. Esquema Chat Completions: choices[0].message.content
        choices = response_data.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message", {})
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str) and content.strip():
                        return content.strip()
                    if isinstance(content, list):
                        parts = [
                            item.get("text", "")
                            for item in content
                            if isinstance(item, dict) and isinstance(item.get("text"), str)
                        ]
                        joined = "\n".join(p.strip() for p in parts if p.strip())
                        if joined:
                            return joined

        # 2. Esquema Responses API: output_text directo o bloques dentro de output[]
        output_text = response_data.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        output_items = response_data.get("output")
        if isinstance(output_items, list):
            collected: list[str] = []
            for item in output_items:
                if not isinstance(item, dict):
                    continue
                for content_block in item.get("content", []) or []:
                    if isinstance(content_block, dict):
                        txt = content_block.get("text")
                        if isinstance(txt, str) and txt.strip():
                            collected.append(txt.strip())
            if collected:
                return "\n".join(collected).strip()

        return ""

    def _classify_http_error(
        self,
        status_code: int,
        raw_body: str,
        target_model: str,
    ) -> BrainError:
        """
        Clasifica errores HTTP de OpenAI comprobando código HTTP, estructura JSON del objeto `error`
        (`code`, `type`, `param`, `message`) y validez del endpoint.
        """
        err_code = ""
        err_type = ""
        err_param = ""
        err_msg = ""
        has_openai_error_schema = False

        try:
            parsed = json.loads(raw_body) if raw_body else {}
            if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
                has_openai_error_schema = True
                err_obj = parsed["error"]
                err_code = str(err_obj.get("code", "") or "").lower()
                err_type = str(err_obj.get("type", "") or "").lower()
                err_param = str(err_obj.get("param", "") or "").lower()
                err_msg = str(err_obj.get("message", "") or "")
        except Exception:
            err_msg = raw_body

        safe_detail = redact_secrets(
            f"HTTP {status_code} (code={err_code}, type={err_type}): {err_msg or raw_body}",
            (self.api_key,),
        )
        lowered_msg = (err_msg or "").lower()

        # 1. Autenticación inválida (HTTP 401 / 403)
        if status_code in (401, 403) or err_code in ("invalid_api_key", "AccountDeactivated".lower()):
            return BrainError(
                provider="GPT",
                kind=BrainErrorKind.AUTH,
                user_message="Lo siento señor, la clave de API de OpenAI (GPT) es inválida o no tiene autorización.",
                model=target_model,
                status_code=status_code,
                detail=safe_detail,
            )

        # 2. Cuota agotada o límite de velocidad (HTTP 429)
        if status_code == 429 or err_code in ("insufficient_quota", "rate_limit_exceeded", "billing_hard_limit_reached"):
            return BrainError(
                provider="GPT",
                kind=BrainErrorKind.QUOTA,
                user_message="Lo siento señor, se agotó la cuota o límite de peticiones de OpenAI (GPT).",
                model=target_model,
                status_code=status_code,
                detail=safe_detail,
            )

        # 3. Modelo inexistente o incompatible:
        # No cualquier HTTP 404 significa que el modelo no existe. Exigimos que el endpoint tenga
        # formato válido y que el cuerpo JSON confirme un problema de modelo (model_not_found,
        # param=='model', o mensaje explícito sobre el modelo en el esquema de OpenAI).
        endpoint_ok = self._is_valid_endpoint_format()
        is_confirmed_model_error = endpoint_ok and has_openai_error_schema and (
            err_code in ("model_not_found", "unsupported_model")
            or err_param == "model"
            or (
                "model" in lowered_msg
                and (
                    "does not exist" in lowered_msg
                    or "not found" in lowered_msg
                    or "not supported" in lowered_msg
                    or "do not have access" in lowered_msg
                    or "incompatible" in lowered_msg
                )
            )
        )
        if is_confirmed_model_error and status_code in (400, 404):
            return BrainError(
                provider="GPT",
                kind=BrainErrorKind.MODEL_NOT_FOUND,
                user_message=f"Lo siento señor, el modelo GPT '{target_model}' no está disponible o no es compatible.",
                model=target_model,
                status_code=status_code,
                detail=safe_detail,
            )

        # 4. Solicitud o ruta inválida (incluyendo un 404 de URL mal escrita o sin estructura de modelo)
        if status_code in (400, 404, 422):
            return BrainError(
                provider="GPT",
                kind=BrainErrorKind.INVALID_REQUEST,
                user_message="Lo siento señor, OpenAI rechazó la solicitud o el endpoint configurado no es válido.",
                model=target_model,
                status_code=status_code,
                detail=safe_detail,
            )

        # 5. Error interno del servidor OpenAI (HTTP 5xx)
        if status_code >= 500 or err_type == "server_error":
            return BrainError(
                provider="GPT",
                kind=BrainErrorKind.SERVER,
                user_message="Lo siento señor, los servidores de OpenAI (GPT) presentan un error temporal.",
                model=target_model,
                status_code=status_code,
                detail=safe_detail,
            )

        return BrainError(
            provider="GPT",
            kind=BrainErrorKind.UNKNOWN,
            user_message="Lo siento señor, no pude obtener respuesta de GPT en este momento.",
            model=target_model,
            status_code=status_code,
            detail=safe_detail,
        )

    def _send_request(self, payload: dict[str, Any], target_model: str) -> dict[str, Any]:
        """Envía la petición HTTP POST a OpenAI con timeout acotado."""
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
                provider="GPT",
                kind=BrainErrorKind.TIMEOUT,
                user_message="Lo siento señor, la conexión con OpenAI (GPT) superó el tiempo de espera (timeout).",
                model=target_model,
                detail=redact_secrets(str(timeout_err), (self.api_key,)),
            ) from None
        except urllib.error.URLError as url_err:
            reason = getattr(url_err, "reason", url_err)
            if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).lower():
                raise BrainError(
                    provider="GPT",
                    kind=BrainErrorKind.TIMEOUT,
                    user_message="Lo siento señor, la conexión con OpenAI (GPT) superó el tiempo de espera (timeout).",
                    model=target_model,
                    detail=redact_secrets(str(reason), (self.api_key,)),
                ) from None
            raise BrainError(
                provider="GPT",
                kind=BrainErrorKind.NETWORK,
                user_message="Lo siento señor, no pude conectar con OpenAI (GPT) por un problema de red.",
                model=target_model,
                detail=redact_secrets(str(reason), (self.api_key,)),
            ) from None
        except BrainError:
            raise
        except Exception as unexpected:
            raise BrainError(
                provider="GPT",
                kind=BrainErrorKind.NETWORK,
                user_message="Lo siento señor, ocurrió un error al comunicarme con OpenAI (GPT).",
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
        Genera una respuesta con OpenAI GPT.
        Si el modelo principal falla por `MODEL_NOT_FOUND`, intenta `OPENAI_FALLBACK_MODEL` una sola vez.
        """
        self.last_error = None
        self.last_fallback_note = None

        primary_model = (model_override or self.model_name).strip() or self.model_name
        if not self.api_key:
            err = BrainError(
                provider="GPT",
                kind=BrainErrorKind.UNCONFIGURED,
                user_message="Lo siento señor, GPT no está configurado (falta OPENAI_API_KEY en el entorno).",
                model=primary_model,
            )
            self.last_error = err
            if raise_on_error:
                raise err
            return err.user_message

        active_system_prompt = system_prompt or JARVIS_SYSTEM_PROMPT
        token_limit = int(max_output_tokens) if isinstance(max_output_tokens, int) and max_output_tokens > 0 else VOICE_MAX_TOKENS
        candidate_models = list(dict.fromkeys([primary_model, self.fallback_model_name]))

        for index, candidate in enumerate(candidate_models):
            payload = self._build_payload(
                prompt=prompt,
                system_prompt=active_system_prompt,
                model_name=candidate,
                max_tokens=token_limit,
            )
            try:
                response_data = self._send_request(payload, candidate)
                text = self._extract_text(response_data)
                self.last_used_model = str(response_data.get("model") or candidate)
                self.model_name = candidate
                if not text:
                    empty_err = BrainError(
                        provider="GPT",
                        kind=BrainErrorKind.EMPTY_RESPONSE,
                        user_message="Lo siento señor, GPT devolvió una respuesta vacía.",
                        model=self.last_used_model,
                    )
                    self.last_error = empty_err
                    if raise_on_error:
                        raise empty_err
                    return empty_err.user_message
                return text
            except BrainError as brain_err:
                has_fallback = index + 1 < len(candidate_models)
                if brain_err.kind == BrainErrorKind.MODEL_NOT_FOUND and has_fallback:
                    next_model = candidate_models[index + 1]
                    note = f"El modelo GPT '{candidate}' no está disponible; se utiliza el respaldo '{next_model}'."
                    self.last_fallback_note = note
                    print(f"[GPT] {note}")
                    continue

                self.last_error = brain_err
                print(f"[GPT] Falló la generación ({brain_err.kind.value}) con '{candidate}'.")
                if raise_on_error:
                    raise
                return brain_err.user_message

        return "Lo siento señor, GPT no pudo generar una respuesta en este momento."
