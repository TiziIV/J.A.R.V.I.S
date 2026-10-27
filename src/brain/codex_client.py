"""
Cliente oficial e independiente para OpenAI Codex CLI (`src/brain/codex_client.py`).

Justificación técnica junior:
A diferencia de `OpenAIBrain` (que realiza peticiones HTTP directas a la API de OpenAI usando
`OPENAI_API_KEY`), este módulo encapsula la herramienta de línea de comandos oficial **OpenAI Codex CLI**
(`codex`), la cual puede autenticarse directamente con la sesión de **ChatGPT** del usuario
(`codex login` / `codex login status`) sin requerir una clave API en `.env`.

Principios de seguridad implementados:
1. Jamás se utiliza `shell=True` al invocar `subprocess`.
2. Jamás se permiten banderas inseguras como `--yolo`, `--dangerously-bypass-approvals-and-sandbox`
   ni el sandbox `danger-full-access`.
3. Se distinguen dos categorías estrictas de tarea:
   - **Tipo A (`readonly`):** consultas, revisiones de código, explicaciones y análisis con
     `--sandbox read-only` (cero modificaciones en disco).
   - **Tipo B (`coding`):** creación o edición autorizada de código dentro del proyecto con
     `--sandbox workspace-write`, validación previa de `CODEX_WORKSPACE_DIR` e inspección
     posterior del árbol Git sin hacer commit ni push automáticos.
4. Toda salida y mensaje de error se sanitiza mediante `sanitize_output()` para no filtrar
   tokens, claves ni credenciales en consola o logs.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unicodedata
from typing import Any, Optional, Sequence

from config.settings import (
    BASE_DIR,
    CODEX_ALLOW_AUTO,
    CODEX_ALLOWED_SANDBOXES,
    CODEX_ENABLED,
    CODEX_MODEL,
    CODEX_SANDBOX,
    CODEX_TIMEOUT,
    CODEX_WORKSPACE_DIR,
)
from src.brain.exceptions import BrainError, BrainErrorKind, redact_secrets
from src.utils.git_guard import GitSafetyGuard, GitTreeSnapshot


# Variantes de nombre de ejecutable de Codex CLI habituales en Windows (npm global / binarios nativos).
CODEX_EXECUTABLE_CANDIDATES: tuple[str, ...] = ("codex", "codex.cmd", "codex.exe")

# Argumentos y modos de sandbox terminantemente prohibidos por política de seguridad.
FORBIDDEN_CODEX_ARGS: frozenset[str] = frozenset(
    {
        "--yolo",
        "--dangerously-bypass-approvals-and-sandbox",
        "danger-full-access",
    }
)

# Patrones de operaciones destructivas o de alto impacto externo que jamás deben ejecutarse en Codex.
_DESTRUCTIVE_TASK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(--yolo|danger-full-access|dangerously-bypass-approvals)"),
    re.compile(r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|push\s+--force|push\s+-f|checkout\s+--\s*\.)\b"),
    re.compile(r"\b(rm\s+-rf|del\s+/[fqs]|format\s+[a-z]:|remove-item\s+.*-recurse\s+.*-force)\b"),
    re.compile(r"\b(borra|elimina|destruye)\s+(todo\s+el\s+repositorio|todo\s+el\s+proyecto|la\s+base\s+de\s+datos)\b"),
    re.compile(r"\b(publica\s+en\s+produccion|despliega\s+en\s+produccion|haz\s+git\s+push|sube\s+a\s+github\s+automaticamente)\b"),
)

# Verbos de revisión/análisis que exigen modo de solo lectura ("Tipo A").
_READONLY_PRIMARY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^(?:por\s+favor\s+)?(?:usa\s+codex\s+para\s+|con\s+codex\s+)?"
        r"(revisa|revisar|explica|explicar|analiza|analizar|inspecciona|inspeccionar|"
        r"encuentra|encontrar|busca|buscar|diagnostica|diagnosticar|evalua|evaluar|"
        r"propone|proponer|sugiere|sugerir|describe|describir|lee|leer|resume|resumir|"
        r"que\s+hace|como\s+funciona|por\s+que\s+falla)\b"
    ),
    re.compile(r"\b(sin\s+modificar|sin\s+editar|solo\s+lectura|solo\s+revisa|solo\s+analiza|no\s+modifiques)\b"),
)

# Verbos de programación/edición autorizada en el repositorio ("Tipo B").
_CODING_WRITE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(crea|crear|implementa|implementar|modifica|modificar|corrige|corregir|"
        r"arregla|arreglar|refactoriza|refactorizar|agrega|agregar|anade|anadir|"
        r"edita|editar|actualiza|actualizar|escribe|escribir)\b.*\b"
        r"(archivo|archivos|modulo|modulos|funcion|funciones|clase|clases|prueba|pruebas|"
        r"test|tests|componente|componentes|error|bug|fallo|codigo|proyecto|cliente|skill|motor)\b"
    ),
    re.compile(
        r"^(?:por\s+favor\s+)?(?:usa\s+codex\s+para\s+|con\s+codex\s+)?"
        r"(crea|crear|implementa|implementar|modifica|modificar|corrige|corregir|"
        r"arregla|arreglar|refactoriza|refactorizar|agrega|agregar|edita|editar)\b"
    ),
)

# Expresiones que indican duda o ambigüedad sobre si el usuario realmente autoriza editar archivos.
_AMBIGUOUS_EDIT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(revisa|revisar|analiza|analizar)\b.*\b(y\s+si\s+hace\s+falta\s+modifica|o\s+modifica|y\s+quizas\s+cambia|y\s+fijate\s+si\s+cambias)\b"),
    re.compile(r"^(?:deberiamos|conviene|podrias\s+ver\s+si\s+conviene)\s+(modificar|cambiar|editar|refactorizar)\b"),
)


def _normalize_prompt_for_intent(text: str) -> str:
    """Normaliza un prompt a minúsculas y sin tildes para clasificar la intención de forma estable."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text.lower().strip())
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(no_accents.split())


def sanitize_output(text: str, extra_secrets: Sequence[str] = ()) -> str:
    """
    Limpia cualquier salida de consola, error o evento JSON de Codex CLI para garantizar
    que no se expongan claves API, tokens OAuth, cabeceras Bearer ni rutas de credenciales.
    """
    if not text:
        return ""
    cleaned = redact_secrets(str(text), tuple(extra_secrets))
    # Oculta posibles tokens en líneas del tipo "Logged in using an API key: sk-..."
    cleaned = re.sub(
        r"(?i)(api\s+key|token|secret|password)\s*[:=]\s*([^\s,;\"']+)",
        r"\1: [REDACTED]",
        cleaned,
    )
    return cleaned


@dataclass(frozen=True)
class CodexAuthStatus:
    """
    Estado verificable de autenticación de OpenAI Codex CLI.
    Se evalúa como `True` en contextos booleanos únicamente cuando `authenticated == True`.
    """

    authenticated: bool
    method: str
    message: str
    exit_code: Optional[int] = None

    def __bool__(self) -> bool:
        return self.authenticated


@dataclass(frozen=True)
class CodexTaskClassification:
    """
    Clasificación de seguridad de una solicitud enviada a Codex CLI:
    - `task_type`: `'readonly'` (Tipo A) o `'coding'` (Tipo B).
    - `sandbox`: `'read-only'` o `'workspace-write'`.
    - `authorized_write`: `True` solo si la orden autoriza claramente editar el proyecto.
    - `needs_confirmation`: `True` si existe ambigüedad entre revisar y modificar archivos.
    - `is_destructive`: `True` si solicita acciones destructivas o banderas prohibidas (`--yolo`).
    """

    task_type: str
    sandbox: str
    authorized_write: bool
    needs_confirmation: bool
    is_destructive: bool
    reason: str


def classify_codex_task(prompt: str) -> CodexTaskClassification:
    """
    Clasifica una solicitud para Codex CLI entre Tipo A (consulta/revisión de solo lectura)
    y Tipo B (programación/modificación autorizada del proyecto).
    """
    norm = _normalize_prompt_for_intent(prompt)
    if not norm:
        return CodexTaskClassification(
            task_type="readonly",
            sandbox="read-only",
            authorized_write=False,
            needs_confirmation=False,
            is_destructive=False,
            reason="Solicitud vacía; se mantiene en modo de solo lectura.",
        )

    # 1. Bloqueo estricto de comandos destructivos, --yolo o danger-full-access
    if any(pattern.search(norm) for pattern in _DESTRUCTIVE_TASK_PATTERNS):
        return CodexTaskClassification(
            task_type="readonly",
            sandbox="read-only",
            authorized_write=False,
            needs_confirmation=False,
            is_destructive=True,
            reason="Solicitud bloqueada: contiene banderas inseguras (--yolo / danger-full-access) u operaciones destructivas/externas no permitidas.",
        )

    # 2. Ambigüedad entre revisar y editar -> requerir confirmación explícita antes de tocar archivos
    if any(pattern.search(norm) for pattern in _AMBIGUOUS_EDIT_PATTERNS):
        return CodexTaskClassification(
            task_type="readonly",
            sandbox="read-only",
            authorized_write=False,
            needs_confirmation=True,
            is_destructive=False,
            reason="La solicitud combina revisión con una posible edición ambigua; se solicita confirmación antes de modificar archivos.",
        )

    # 3. Una solicitud explícita de revisión, explicación o análisis SIEMPRE es Tipo A (read-only)
    if any(pattern.search(norm) for pattern in _READONLY_PRIMARY_PATTERNS):
        return CodexTaskClassification(
            task_type="readonly",
            sandbox="read-only",
            authorized_write=False,
            needs_confirmation=False,
            is_destructive=False,
            reason="Solicitud de consulta, revisión o análisis (Tipo A: solo lectura).",
        )

    # 4. Solicitud explícita de programación/modificación en el repositorio -> Tipo B (workspace-write)
    if any(pattern.search(norm) for pattern in _CODING_WRITE_PATTERNS):
        return CodexTaskClassification(
            task_type="coding",
            sandbox="workspace-write",
            authorized_write=True,
            needs_confirmation=False,
            is_destructive=False,
            reason="Solicitud explícita de programación o modificación en el proyecto (Tipo B: workspace-write).",
        )

    # 5. Por defecto, cualquier otra consulta se ejecuta en modo seguro de solo lectura
    return CodexTaskClassification(
        task_type="readonly",
        sandbox="read-only",
        authorized_write=False,
        needs_confirmation=False,
        is_destructive=False,
        reason="Consulta técnica general sin orden explícita de edición; se usa modo de solo lectura.",
    )


class CodexBrain:
    """
    Cliente aislado para el motor especializado de programación **OpenAI Codex CLI**.
    Compatible con el contrato de motores de `BrainManager`.
    """

    provider_name: str = "Codex"

    def __init__(
        self,
        *,
        enabled: bool = CODEX_ENABLED,
        timeout: float = CODEX_TIMEOUT,
        workspace_dir: str | Path = CODEX_WORKSPACE_DIR,
        allowed_root: str | Path = BASE_DIR,
        sandbox_mode: str = CODEX_SANDBOX,
        model_name: str = CODEX_MODEL,
        allow_auto: bool = CODEX_ALLOW_AUTO,
        executable_override: Optional[str] = None,
    ) -> None:
        self.enabled: bool = bool(enabled)
        self.timeout: float = max(float(timeout), 5.0)
        self.allowed_root: Path = Path(allowed_root).resolve()
        self.workspace_dir: Path = Path(workspace_dir).resolve()
        self.allow_auto: bool = bool(allow_auto)
        self.model_name: str = (model_name or "").strip()
        self._executable_override: Optional[str] = executable_override

        # Validamos que el sandbox configurado sea seguro ('read-only' o 'workspace-write')
        norm_sandbox = (sandbox_mode or "workspace-write").strip().lower().replace("_", "-")
        if norm_sandbox not in CODEX_ALLOWED_SANDBOXES:
            norm_sandbox = "read-only"
        self.sandbox_mode: str = norm_sandbox

        # Estado de diagnóstico de la última llamada
        self.last_used_model: Optional[str] = self.model_name or "codex-cli"
        self.last_error: Optional[BrainError] = None
        self.last_fallback_note: Optional[str] = None
        self.last_task_type: Optional[str] = None
        self.last_sandbox_used: Optional[str] = None
        self.last_git_summary: Optional[str] = None

    @staticmethod
    def sanitize_output(text: str, extra_secrets: Sequence[str] = ()) -> str:
        """Método estático de sanitización expuesto en el cliente."""
        return sanitize_output(text, extra_secrets)

    def resolve_executable(self) -> Optional[str]:
        """
        Localiza el ejecutable oficial de Codex CLI en Windows o sistemas POSIX usando `shutil.which()`.
        Comprueba las variantes habituales (`codex`, `codex.cmd`, `codex.exe`) y valida que la ruta exista.
        """
        if self._executable_override:
            override_path = Path(self._executable_override)
            if override_path.is_file():
                return str(override_path.resolve())
            found_override = shutil.which(self._executable_override)
            if found_override and Path(found_override).is_file():
                return str(Path(found_override).resolve())
            return None

        for candidate in CODEX_EXECUTABLE_CANDIDATES:
            resolved = shutil.which(candidate)
            if resolved:
                resolved_path = Path(resolved)
                if resolved_path.is_file():
                    return str(resolved_path.resolve())
        return None

    def is_installed(self) -> bool:
        """Verifica si OpenAI Codex CLI está instalado y accesible en el PATH del sistema."""
        return self.resolve_executable() is not None

    def _run_subprocess(
        self,
        args: Sequence[str],
        *,
        cwd: Optional[Path] = None,
        timeout: Optional[float] = None,
    ) -> subprocess.CompletedProcess[str]:
        """
        Ejecuta Codex CLI de forma segura mediante `subprocess.run` con lista de argumentos
        y `shell=False`. Valida previamente que no exista ninguna bandera prohibida.
        """
        if not self.enabled:
            raise BrainError(
                provider="Codex",
                kind=BrainErrorKind.UNCONFIGURED,
                user_message="El motor Codex CLI está deshabilitado en la configuración (CODEX_ENABLED=false).",
                model=self.last_used_model,
            )

        executable = self.resolve_executable()
        if not executable:
            raise BrainError(
                provider="Codex",
                kind=BrainErrorKind.UNCONFIGURED,
                user_message=(
                    "OpenAI Codex CLI no está instalado o no se encuentra en el PATH. "
                    "Consulte https://developers.openai.com/codex/cli/ para instalarlo "
                    "(por ejemplo: `npm install -g @openai/codex`) y luego ejecute `codex login`."
                ),
                model=self.last_used_model,
            )

        # Validamos que ningún argumento intente desactivar el sandbox o usar --yolo
        for arg in args:
            arg_clean = str(arg).strip().lower()
            if arg_clean in FORBIDDEN_CODEX_ARGS or any(
                forbidden in arg_clean for forbidden in FORBIDDEN_CODEX_ARGS
            ):
                raise BrainError(
                    provider="Codex",
                    kind=BrainErrorKind.INVALID_REQUEST,
                    user_message="Operación bloqueada: no se permite usar '--yolo' ni desactivar las protecciones de sandbox de Codex.",
                    model=self.last_used_model,
                )

        effective_timeout = max(float(timeout if timeout is not None else self.timeout), 1.0)
        work_dir = str(cwd) if cwd is not None else str(self.allowed_root)

        cmd: list[str] = [executable, *[str(a) for a in args]]
        try:
            return subprocess.run(
                cmd,
                cwd=work_dir,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=effective_timeout,
                shell=False,
                check=False,
            )
        except FileNotFoundError as exc:
            raise BrainError(
                provider="Codex",
                kind=BrainErrorKind.UNCONFIGURED,
                user_message=(
                    "No se encontró el ejecutable de OpenAI Codex CLI al iniciar el proceso. "
                    "Verifique su instalación oficial en https://developers.openai.com/codex/cli/."
                ),
                model=self.last_used_model,
                detail=sanitize_output(str(exc)),
            ) from None
        except subprocess.TimeoutExpired as exc:
            raise BrainError(
                provider="Codex",
                kind=BrainErrorKind.TIMEOUT,
                user_message=f"Lo siento señor, Codex CLI superó el tiempo máximo de ejecución ({effective_timeout:.0f}s).",
                model=self.last_used_model,
                status_code=504,
                detail=sanitize_output(str(exc)),
            ) from None
        except KeyboardInterrupt:
            raise BrainError(
                provider="Codex",
                kind=BrainErrorKind.EXECUTION,
                user_message="La ejecución de Codex CLI fue cancelada por el usuario.",
                model=self.last_used_model,
            ) from None
        except OSError as exc:
            raise BrainError(
                provider="Codex",
                kind=BrainErrorKind.EXECUTION,
                user_message="Error del sistema operativo al ejecutar Codex CLI.",
                model=self.last_used_model,
                detail=sanitize_output(str(exc)),
            ) from None

    def get_version(self) -> Optional[str]:
        """Obtiene la versión instalada de Codex CLI (`codex --version`) sin exponer datos sensibles."""
        if not self.enabled or not self.is_installed():
            return None
        try:
            proc = self._run_subprocess(["--version"], timeout=10.0)
        except BrainError:
            return None

        combined = sanitize_output(f"{proc.stdout or ''}\n{proc.stderr or ''}".strip())
        if proc.returncode == 0 and combined:
            first_line = combined.splitlines()[0].strip()
            return first_line or None
        return None

    def check_auth_status(self) -> CodexAuthStatus:
        """
        Comprueba el estado de autenticación de Codex CLI ejecutando `codex login status`.
        Distingue entre sesión activa con cuenta de ChatGPT, sesión con API key, falta de sesión
        (`codex login`) y versiones antiguas incompatibles. Nunca expone credenciales.
        """
        if not self.enabled:
            return CodexAuthStatus(
                authenticated=False,
                method="disabled",
                message="La integración con Codex CLI está deshabilitada (CODEX_ENABLED=false).",
                exit_code=None,
            )

        if not self.is_installed():
            return CodexAuthStatus(
                authenticated=False,
                method="not_installed",
                message=(
                    "OpenAI Codex CLI no está instalado. Instálelo desde la documentación oficial "
                    "(https://developers.openai.com/codex/cli/) y luego autentíquese con `codex login`."
                ),
                exit_code=None,
            )

        try:
            proc = self._run_subprocess(["login", "status"], timeout=12.0)
        except BrainError as err:
            return CodexAuthStatus(
                authenticated=False,
                method="error",
                message=err.user_message,
                exit_code=err.status_code,
            )

        raw_out = f"{proc.stdout or ''}\n{proc.stderr or ''}".strip()
        safe_out = sanitize_output(raw_out)
        lowered = safe_out.lower()

        # Detección de versiones antiguas o ejecutables que no admiten `login status`
        unsupported_markers = (
            "unknown command",
            "unrecognized subcommand",
            "invalid subcommand",
            "unexpected argument 'status'",
            "unknown option",
        )
        if any(marker in lowered for marker in unsupported_markers):
            return CodexAuthStatus(
                authenticated=False,
                method="unsupported",
                message=(
                    "La versión instalada de Codex CLI no admite `codex login status`. "
                    "Actualice Codex CLI o ejecute manualmente `codex login` en su terminal."
                ),
                exit_code=proc.returncode,
            )

        unauth_markers = (
            "not logged in",
            "unauthenticated",
            "login required",
            "no active session",
            "please run codex login",
            "please log in",
        )
        if proc.returncode == 0 and not any(marker in lowered for marker in unauth_markers):
            if "chatgpt" in lowered:
                method = "chatgpt"
                msg = "Autenticado en Codex CLI mediante cuenta de ChatGPT."
            elif "api key" in lowered or "api_key" in lowered:
                method = "api_key"
                msg = "Autenticado en Codex CLI mediante clave API."
            else:
                method = "authenticated"
                msg = "Sesión de Codex CLI autenticada y activa."
            return CodexAuthStatus(
                authenticated=True,
                method=method,
                message=msg,
                exit_code=0,
            )

        return CodexAuthStatus(
            authenticated=False,
            method="unauthenticated",
            message=(
                "Codex CLI no tiene una sesión autenticada activa. "
                "Por favor ejecute manualmente `codex login` en su terminal para iniciar sesión con su cuenta de ChatGPT."
            ),
            exit_code=proc.returncode,
        )

    def is_ready_for_auto(self) -> bool:
        """
        Indica si el modo `AUTO` puede delegar tareas de programación a Codex:
        requiere `CODEX_ENABLED`, `CODEX_ALLOW_AUTO`, CLI instalada y sesión autenticada.
        """
        if not self.enabled or not self.allow_auto:
            return False
        if not self.is_installed():
            return False
        auth = self.check_auth_status()
        return auth.authenticated

    def validate_workspace(self, workspace_dir: Optional[str | Path] = None) -> Path:
        """
        Verifica que el directorio de trabajo exista, sea un directorio real y se encuentre
        estrictamente dentro de la raíz permitida del proyecto (`self.allowed_root`).
        """
        candidate_raw = workspace_dir if workspace_dir is not None else self.workspace_dir
        try:
            resolved_target = Path(candidate_raw).expanduser().resolve()
            resolved_root = self.allowed_root.resolve()
        except (OSError, ValueError) as exc:
            raise BrainError(
                provider="Codex",
                kind=BrainErrorKind.INVALID_REQUEST,
                user_message=f"Ruta de directorio de trabajo inválida para Codex: '{candidate_raw}'.",
                model=self.last_used_model,
                detail=sanitize_output(str(exc)),
            ) from None

        if not resolved_target.exists() or not resolved_target.is_dir():
            raise BrainError(
                provider="Codex",
                kind=BrainErrorKind.INVALID_REQUEST,
                user_message=f"El directorio de trabajo para Codex no existe: '{resolved_target}'.",
                model=self.last_used_model,
            )

        if not resolved_target.is_relative_to(resolved_root):
            raise BrainError(
                provider="Codex",
                kind=BrainErrorKind.INVALID_REQUEST,
                user_message=(
                    f"Acceso denegado: el directorio '{resolved_target}' está fuera del "
                    f"proyecto autorizado ('{resolved_root}')."
                ),
                model=self.last_used_model,
            )

        return resolved_target

    def _parse_codex_jsonl_output(self, stdout: str, stderr: str = "") -> str:
        """
        Interpreta el flujo de eventos JSON Lines (`codex exec --json`) conforme al esquema oficial:
        - Extrae únicamente eventos de respuesta final al usuario (`item.completed` con `item.type == 'agent_message'`,
          o variantes compatibles `assistant_message` / `result` / `response.output_text.done`).
        - No confunde eventos internos (`thread.started`, `turn.started`, `reasoning`, `command_execution`)
          con la respuesta final.
        - Si detecta errores de autenticación o formato JSON inválido/incompleto, lanza `BrainError` claro.
        """
        raw_stdout = (stdout or "").strip()
        safe_stderr = sanitize_output((stderr or "").strip())

        if not raw_stdout:
            raise BrainError(
                provider="Codex",
                kind=BrainErrorKind.EMPTY_RESPONSE,
                user_message="Codex CLI finalizó sin devolver contenido en la salida.",
                model=self.last_used_model,
                detail=safe_stderr,
            )

        agent_messages: list[str] = []
        error_messages: list[str] = []
        valid_json_events: int = 0
        invalid_json_lines: int = 0
        saw_turn_started: bool = False
        saw_turn_completed: bool = False

        for line in raw_stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                invalid_json_lines += 1
                continue

            if not isinstance(event, dict):
                invalid_json_lines += 1
                continue

            valid_json_events += 1
            ev_type = str(event.get("type") or "").strip().lower()

            if ev_type in ("thread.started", "turn.started"):
                saw_turn_started = True
                continue

            if ev_type == "turn.completed":
                saw_turn_completed = True
                continue

            if ev_type in ("error", "turn.failed"):
                err_obj = event.get("error")
                if isinstance(err_obj, dict) and err_obj.get("message"):
                    error_messages.append(sanitize_output(str(err_obj["message"])))
                elif event.get("message"):
                    error_messages.append(sanitize_output(str(event["message"])))
                continue

            # Esquema oficial principal: {"type": "item.completed", "item": {"type": "agent_message", "text": "..."}}
            if ev_type == "item.completed":
                item = event.get("item")
                if isinstance(item, dict):
                    item_type = str(item.get("type") or "").strip().lower()
                    if item_type in ("agent_message", "assistant_message", "message"):
                        text_val = item.get("text")
                        if isinstance(text_val, str) and text_val.strip():
                            agent_messages.append(sanitize_output(text_val.strip()))
                        elif isinstance(item.get("content"), list):
                            parts: list[str] = []
                            for block in item["content"]:
                                if isinstance(block, dict) and isinstance(block.get("text"), str):
                                    parts.append(block["text"].strip())
                            if parts:
                                agent_messages.append(sanitize_output("\n".join(p for p in parts if p)))
                continue

            # Compatibilidad con eventos de resultado directos en versiones de CLI
            if ev_type in ("agent_message", "result", "response.output_text.done", "final_message"):
                direct_text = event.get("text") or event.get("result") or event.get("message")
                if isinstance(direct_text, str) and direct_text.strip():
                    agent_messages.append(sanitize_output(direct_text.strip()))
                continue

            if ev_type == "message" and str(event.get("role") or "").lower() == "assistant":
                content_val = event.get("content") or event.get("text")
                if isinstance(content_val, str) and content_val.strip():
                    agent_messages.append(sanitize_output(content_val.strip()))
                elif isinstance(content_val, list):
                    parts = [
                        str(b.get("text", "")).strip()
                        for b in content_val
                        if isinstance(b, dict) and b.get("text")
                    ]
                    if parts:
                        agent_messages.append(sanitize_output("\n".join(parts)))

        if error_messages and not agent_messages:
            joined_err = " | ".join(error_messages)
            lowered_err = joined_err.lower()
            if any(k in lowered_err for k in ("unauthorized", "not logged in", "authentication", "401", "login")):
                raise BrainError(
                    provider="Codex",
                    kind=BrainErrorKind.AUTH,
                    user_message=(
                        "Sesión de Codex CLI no autorizada o expirada. "
                        "Ejecute manualmente `codex login` en su terminal."
                    ),
                    model=self.last_used_model,
                    status_code=401,
                    detail=joined_err,
                )
            raise BrainError(
                provider="Codex",
                kind=BrainErrorKind.EXECUTION,
                user_message=f"Codex CLI informó un error durante la tarea: {joined_err}",
                model=self.last_used_model,
                detail=joined_err,
            )

        if agent_messages:
            return "\n\n".join(agent_messages).strip()

        if valid_json_events == 0 and invalid_json_lines > 0:
            raise BrainError(
                provider="Codex",
                kind=BrainErrorKind.INVALID_RESPONSE,
                user_message="La salida estructurada de Codex CLI no tiene un formato JSON válido.",
                model=self.last_used_model,
                detail=sanitize_output(raw_stdout[:400]),
            )

        if saw_turn_started and not saw_turn_completed:
            raise BrainError(
                provider="Codex",
                kind=BrainErrorKind.INVALID_RESPONSE,
                user_message="La respuesta de Codex CLI quedó incompleta antes de finalizar el turno.",
                model=self.last_used_model,
                detail=sanitize_output(raw_stdout[:400]),
            )

        raise BrainError(
            provider="Codex",
            kind=BrainErrorKind.EMPTY_RESPONSE,
            user_message="Codex CLI completó la ejecución pero no emitió un mensaje de respuesta final.",
            model=self.last_used_model,
            detail=sanitize_output(raw_stdout[:400]),
        )

    def _execute_codex_task(
        self,
        prompt: str,
        *,
        sandbox: str,
        system_prompt: Optional[str] = None,
        workspace_dir: Optional[str | Path] = None,
        timeout: Optional[float] = None,
    ) -> str:
        """
        Valida precondiciones (habilitación, instalación, autenticación, workspace y sandbox)
        y ejecuta `codex exec --json --sandbox <sandbox>`.
        """
        if not self.enabled:
            raise BrainError(
                provider="Codex",
                kind=BrainErrorKind.UNCONFIGURED,
                user_message="El motor Codex CLI está deshabilitado en la configuración (CODEX_ENABLED=false).",
                model=self.last_used_model,
            )

        if sandbox not in CODEX_ALLOWED_SANDBOXES:
            raise BrainError(
                provider="Codex",
                kind=BrainErrorKind.INVALID_REQUEST,
                user_message=f"Modo de sandbox no permitido para Codex: '{sandbox}'.",
                model=self.last_used_model,
            )

        valid_workspace = self.validate_workspace(workspace_dir)

        if not self.is_installed():
            raise BrainError(
                provider="Codex",
                kind=BrainErrorKind.UNCONFIGURED,
                user_message=(
                    "OpenAI Codex CLI no está instalado en este sistema. "
                    "Instálelo siguiendo https://developers.openai.com/codex/cli/ "
                    "(por ejemplo: `npm install -g @openai/codex`) y autentíquese con `codex login`."
                ),
                model=self.last_used_model,
            )

        auth_status = self.check_auth_status()
        if not auth_status.authenticated:
            raise BrainError(
                provider="Codex",
                kind=BrainErrorKind.AUTH,
                user_message=auth_status.message,
                model=self.last_used_model,
                status_code=401,
                detail=auth_status.message,
            )

        composed_prompt = prompt.strip()
        if system_prompt and system_prompt.strip():
            composed_prompt = (
                f"[Contexto del asistente: {system_prompt.strip()}] "
                f"Solicitud del usuario: {composed_prompt}"
            )
        # En Windows, el wrapper `codex.cmd` usa `%*` de cmd.exe, que truncaría argumentos en saltos
        # de línea (`\r` o `\n`). Normalizamos saltos de línea a espacios para preservar todo el prompt.
        cli_safe_prompt = " ".join(composed_prompt.splitlines()).strip()

        args: list[str] = ["exec", "--json", "--sandbox", sandbox]
        if self.model_name:
            args.extend(["-m", self.model_name])
        args.append(cli_safe_prompt)

        proc = self._run_subprocess(args, cwd=valid_workspace, timeout=timeout)
        self.last_sandbox_used = sandbox
        self.last_used_model = self.model_name or "codex-cli"

        if proc.returncode != 0:
            safe_err = sanitize_output((proc.stderr or proc.stdout or "").strip())
            lowered_err = safe_err.lower()
            if any(k in lowered_err for k in ("not logged in", "unauthorized", "authentication", "login")):
                raise BrainError(
                    provider="Codex",
                    kind=BrainErrorKind.AUTH,
                    user_message=(
                        "Fallo de autenticación en Codex CLI. "
                        "Ejecute manualmente `codex login` en su terminal para iniciar sesión."
                    ),
                    model=self.last_used_model,
                    status_code=401,
                    detail=safe_err,
                )
            raise BrainError(
                provider="Codex",
                kind=BrainErrorKind.EXECUTION,
                user_message=f"Codex CLI finalizó con código de error ({proc.returncode}).",
                model=self.last_used_model,
                status_code=proc.returncode,
                detail=safe_err,
            )

        return self._parse_codex_jsonl_output(proc.stdout or "", proc.stderr or "")

    def run_readonly_task(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
        workspace_dir: Optional[str | Path] = None,
        timeout: Optional[float] = None,
        raise_on_error: bool = False,
    ) -> str:
        """
        TIPO A: Ejecuta consultas, explicaciones, revisiones de código o análisis de arquitectura
        utilizando estrictamente `--sandbox read-only` (sin modificar archivos del repositorio).
        """
        self.last_error = None
        self.last_fallback_note = None
        self.last_task_type = "readonly"
        self.last_git_summary = None

        classification = classify_codex_task(prompt)
        if classification.is_destructive:
            err = BrainError(
                provider="Codex",
                kind=BrainErrorKind.INVALID_REQUEST,
                user_message=classification.reason,
                model=self.last_used_model,
            )
            self.last_error = err
            if raise_on_error:
                raise err
            return err.user_message

        try:
            return self._execute_codex_task(
                prompt,
                sandbox="read-only",
                system_prompt=system_prompt,
                workspace_dir=workspace_dir,
                timeout=timeout,
            )
        except BrainError as err:
            self.last_error = err
            if raise_on_error:
                raise
            return err.user_message

    def run_coding_task(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
        workspace_dir: Optional[str | Path] = None,
        timeout: Optional[float] = None,
        allow_write: bool = True,
        raise_on_error: bool = False,
    ) -> str:
        """
        TIPO B: Ejecuta una tarea de programación autorizada utilizando `--sandbox workspace-write`.
        Valida previamente el workspace, protege cambios preexistentes en Git e inspecciona
        `git status` y `git diff` al finalizar sin realizar commit ni push automáticos.
        """
        self.last_error = None
        self.last_fallback_note = None
        self.last_task_type = "coding"
        self.last_git_summary = None

        classification = classify_codex_task(prompt)
        if classification.is_destructive:
            err = BrainError(
                provider="Codex",
                kind=BrainErrorKind.INVALID_REQUEST,
                user_message=classification.reason,
                model=self.last_used_model,
            )
            self.last_error = err
            if raise_on_error:
                raise err
            return err.user_message

        if not allow_write:
            err = BrainError(
                provider="Codex",
                kind=BrainErrorKind.INVALID_REQUEST,
                user_message="La tarea de modificación requiere autorización explícita de escritura (allow_write=True).",
                model=self.last_used_model,
            )
            self.last_error = err
            if raise_on_error:
                raise err
            return err.user_message

        if self.sandbox_mode == "read-only":
            err = BrainError(
                provider="Codex",
                kind=BrainErrorKind.INVALID_REQUEST,
                user_message=(
                    "La configuración actual (CODEX_SANDBOX='read-only') solo permite consultas "
                    "de lectura y bloquea modificaciones en el repositorio."
                ),
                model=self.last_used_model,
            )
            self.last_error = err
            if raise_on_error:
                raise err
            return err.user_message

        try:
            valid_workspace = self.validate_workspace(workspace_dir)
            git_guard = GitSafetyGuard(repo_dir=valid_workspace)
            baseline_snapshot: Optional[GitTreeSnapshot] = None
            try:
                baseline_snapshot = git_guard.inspect_state()
            except Exception:
                baseline_snapshot = None

            answer = self._execute_codex_task(
                prompt,
                sandbox="workspace-write",
                system_prompt=system_prompt,
                workspace_dir=valid_workspace,
                timeout=timeout,
            )

            # Inspección post-ejecución de Git (Fase 4: verificar cambios sin hacer commit ni push)
            if baseline_snapshot is not None:
                try:
                    after_snapshot = git_guard.inspect_state()
                    task_files, preexisting_files = git_guard.detect_task_changes(
                        baseline_snapshot, after_snapshot
                    )
                    if task_files:
                        summary = (
                            f"[Git] Archivos tocados por esta tarea ({len(task_files)}): "
                            f"{', '.join(task_files)}. "
                            "(Edición completada; commit y push requieren autorización por separado)."
                        )
                    else:
                        summary = "[Git] No se detectaron archivos nuevos modificados fuera del estado previo."
                    if preexisting_files:
                        summary += f" Cambios preexistentes preservados ({len(preexisting_files)})."
                    self.last_git_summary = summary
                except Exception:
                    pass

            return answer
        except BrainError as err:
            self.last_error = err
            if raise_on_error:
                raise
            return err.user_message

    def generate_response(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        *,
        task_type: Optional[str] = None,
        allow_write: Optional[bool] = None,
        workspace_dir: Optional[str | Path] = None,
        timeout: Optional[float] = None,
        raise_on_error: bool = False,
        **kwargs: Any,
    ) -> str:
        """
        Punto de entrada unificado compatible con `BrainManager`.
        Distingue automáticamente entre consultas de solo lectura (Tipo A),
        edición autorizada (Tipo B) y solicitudes ambiguas o bloqueadas.
        """
        classification = classify_codex_task(prompt)

        if classification.is_destructive:
            err = BrainError(
                provider="Codex",
                kind=BrainErrorKind.INVALID_REQUEST,
                user_message=classification.reason,
                model=self.last_used_model,
            )
            self.last_error = err
            if raise_on_error:
                raise err
            return err.user_message

        if classification.needs_confirmation and allow_write is not True and task_type != "coding":
            self.last_task_type = "confirmation_required"
            return (
                "Señor, su solicitud combina revisión con una posible modificación del proyecto. "
                "Por seguridad no he editado ningún archivo. ¿Desea que solo revise en modo lectura "
                "o autoriza explícitamente modificar los archivos con Codex?"
            )

        effective_type = (task_type or classification.task_type).strip().lower()
        should_write = (
            bool(allow_write)
            if allow_write is not None
            else (effective_type == "coding" and classification.authorized_write)
        )

        if effective_type == "coding" and should_write:
            return self.run_coding_task(
                prompt,
                system_prompt=system_prompt,
                workspace_dir=workspace_dir,
                timeout=timeout,
                allow_write=True,
                raise_on_error=raise_on_error,
            )

        return self.run_readonly_task(
            prompt,
            system_prompt=system_prompt,
            workspace_dir=workspace_dir,
            timeout=timeout,
            raise_on_error=raise_on_error,
        )


# Alias de compatibilidad
CodexClient = CodexBrain
