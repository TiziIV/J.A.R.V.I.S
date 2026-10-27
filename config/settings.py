"""
Módulo de Configuración Global de J.A.R.V.I.S.
Centraliza la carga segura de variables de entorno (.env), registro de DLLs de hardware CUDA,
y constantes operativas para el cerebro LLM, reconocimiento Whisper y síntesis de voz.
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# ==============================================================================
# REGISTRO DINÁMICO DE DLLs DE NVIDIA CUDA PARA CTranslate2 EN WINDOWS
# ==============================================================================
# Justificación técnica junior:
# A partir de Python 3.8 en Windows, el sistema operativo por motivos de seguridad
# ya NO busca bibliotecas dinámicas (.dll) en la variable de entorno PATH de forma automática.
# Para que la biblioteca CTranslate2 (el motor de aceleración GPU de faster-whisper)
# pueda encontrar 'cublas64_*.dll' y 'cudnn64_*.dll' instalados en el virtualenv,
# es estrictamente necesario invocar 'os.add_dll_directory()' con las rutas absolutas
# de los paquetes 'nvidia-cublas-cu12' y 'nvidia-cudnn-cu12', además de sincronizar 'os.environ["PATH"]'.
if sys.platform == "win32":
    site_packages: Path = Path(sys.prefix) / "Lib" / "site-packages"
    cublas_dir: Path = site_packages / "nvidia" / "cublas" / "bin"
    cudnn_dir: Path = site_packages / "nvidia" / "cudnn" / "bin"
    if cublas_dir.exists():
        os.add_dll_directory(str(cublas_dir))
        os.environ["PATH"] = str(cublas_dir) + os.pathsep + os.environ["PATH"]
    if cudnn_dir.exists():
        os.add_dll_directory(str(cudnn_dir))
        os.environ["PATH"] = str(cudnn_dir) + os.pathsep + os.environ["PATH"]

# ==============================================================================
# CARGA SEGURA DE VARIABLES DE ENTORNO (.env)
# ==============================================================================
# Localizamos la raíz del proyecto dinámicamente mediante pathlib para garantizar que
# el asistente funcione sin importar desde qué directorio se ejecute en la terminal.
BASE_DIR: Path = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# Clave API para el cerebro en la nube (Google Gemini). Si está ausente, opera en modo degradado local.
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")

# Modelo preferido de Google Gemini.
# Se conserva 'gemini-2.0-flash' como valor predeterminado para mantener compatibilidad estricta
# con los contratos y suites de pruebas existentes del proyecto; puede sobreescribirse en `.env`
# (por ejemplo con 'gemini-2.5-flash').
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

# Modelo Gemini de respaldo: se intenta cuando el modelo principal devuelve 404 / no disponible.
GEMINI_FALLBACK_MODEL: str = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-2.0-flash")

# URL base del daemon local de Ollama (por defecto localhost en el puerto 11434)
OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

# Modelo local ligero optimizado para 6 GB VRAM (GTX 1660): qwen2.5-coder:3b consume ~2.2 GB VRAM
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:3b")

# Alias explícito del modelo local estándar (mismo valor que OLLAMA_MODEL, que conserva sus consumidores).
OLLAMA_MODEL_CENSORED: str = OLLAMA_MODEL

# Modelo local alternativo (modo OLLAMA_UNCENSORED). No se descarga automáticamente:
# si falta, J.A.R.V.I.S. muestra el comando `ollama pull <modelo>` y usa el estándar una sola vez.
OLLAMA_MODEL_UNCENSORED: str = os.getenv("OLLAMA_MODEL_UNCENSORED", "huihui_ai/qwen2.5-abliterated:3b")

# Claves y modelos de los proveedores cloud opcionales. Sin clave, el proveedor queda deshabilitado
# con un mensaje claro y el resto del asistente sigue funcionando.
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-5.4")
OPENAI_FALLBACK_MODEL: str = os.getenv("OPENAI_FALLBACK_MODEL", "gpt-4o")
# Esfuerzo de razonamiento para modelos GPT-5/o-series ('' = no enviar el parámetro y usar el del modelo).
OPENAI_REASONING_EFFORT: str = os.getenv("OPENAI_REASONING_EFFORT", "").strip().lower()

# Tamaño del modelo Whisper: 'base' ofrece ~95% de precisión en español con latencia menor a 400ms
WHISPER_MODEL_SIZE: str = os.getenv("WHISPER_MODEL_SIZE", "base")

# Dispositivo de ejecución: 'cuda' para aceleración por GPU, con fallback a 'cpu' si no hay GPU
WHISPER_DEVICE: str = os.getenv("WHISPER_DEVICE", "cuda")

# Precisión de cálculo: 'float16' reduce a la mitad el consumo de VRAM y acelera la inferencia en tarjetas Turing/GTX 1660
WHISPER_COMPUTE_TYPE: str = os.getenv("WHISPER_COMPUTE_TYPE", "float16")


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    """
    Función auxiliar para leer variables numéricas flotantes (float) desde el entorno (.env).
    ¿Por qué se hace así? En producción, los usuarios pueden ingresar texto inválido o valores negativos
    por error. Si falla la conversión (TypeError o ValueError), devolvemos el valor por defecto seguro
    y garantizamos que el resultado nunca sea menor al umbral mínimo técnico establecido.
    """
    try:
        value = float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


# ==============================================================================
# SESIÓN CONVERSACIONAL CONTINUA Y EFECTOS HUD (STARK MCU)
# ==============================================================================
# 1. SESSION_IDLE_TIMEOUT (7.0 segundos por defecto):
# Ventana máxima de inactividad que espera J.A.R.V.I.S. tras terminar de hablar.
# Justificación junior: Si el usuario guarda silencio más de 7 segundos, se asume que terminó
# la interacción y el asistente entra en modo reposo automáticamente para ahorrar CPU.
SESSION_IDLE_TIMEOUT: float = _env_float("SESSION_IDLE_TIMEOUT", 7.0, minimum=1.0)

# 2. SESSION_SILENCE_DURATION (1.8 segundos por defecto):
# Duración de silencio continuo que detecta el VAD para dar por concluida la orden del usuario.
# Justificación junior: En sesión continua se usa 1.8s en lugar de 2.5s para que la conversación
# sea ágil y dinámica, evitando silencios incómodos tras terminar de hablar.
SESSION_SILENCE_DURATION: float = _env_float("SESSION_SILENCE_DURATION", 1.8, minimum=0.5)

# 3. SESSION_POST_SPEECH_PAUSE (0.35 segundos por defecto):
# Margen de seguridad temporal (anti-eco) entre el fin de la locución TTS y la reactivación del micrófono.
# Justificación junior: El hardware de audio y los controladores de Windows tardan unos milisegundos
# en vaciar sus búferes de salida; si abriéramos el micrófono inmediatamente, J.A.R.V.I.S. escucharía
# el eco de su propia voz y entraría en bucles de transcripción infinitos.
SESSION_POST_SPEECH_PAUSE: float = _env_float("SESSION_POST_SPEECH_PAUSE", 0.35)

# 4. SFX_ENABLED (True por defecto):
# Bandera booleana para habilitar o deshabilitar los efectos sonoros HUD sintetizados en memoria.
# Permite silenciar los pitidos en entornos de desarrollo sin modificar el código fuente.
SFX_ENABLED: bool = os.getenv("SFX_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


# ==============================================================================
# ARQUITECTURA MULTI-BRAIN: MODOS, LÍMITES Y TIEMPOS DE ESPERA
# ==============================================================================
# Advertencias de configuración acumuladas (sin secretos). main.py las muestra al iniciar.
CONFIG_WARNINGS: list[str] = []


def _env_bool(name: str, default: bool) -> bool:
    """Lee un interruptor booleano tolerante ('1', 'true', 'si', 'on' activan)."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "si", "sí", "yes", "on")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """Lee un entero positivo; ante texto inválido o valores menores al mínimo usa el valor seguro."""
    try:
        value = int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def _env_choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    """
    Lee una opción cerrada (p. ej. un modo) normalizando mayúsculas, espacios y guiones.

    ¿Por qué validar? Un error tipográfico en `.env` ('OLAMA') no debe romper el arranque:
    se usa el valor seguro documentado y se registra una advertencia comprensible.
    """
    raw = os.getenv(name, default)
    value = (raw or "").strip().upper().replace("-", "_").replace(" ", "_")
    if value in allowed:
        return value
    CONFIG_WARNINGS.append(
        f"{name}='{raw}' no es válido (opciones: {', '.join(allowed)}). Se usa '{default}'."
    )
    return default


# Modos de inteligencia admitidos. AUTO deja que el deliberador elija el motor en cada solicitud.
BRAIN_MODES: tuple[str, ...] = (
    "AUTO",
    "OLLAMA",
    "OLLAMA_UNCENSORED",
    "GEMINI",
    "CLAUDE",
    "GPT",
    "CODEX",
)
DEFAULT_BRAIN_MODE: str = _env_choice("DEFAULT_BRAIN_MODE", "AUTO", BRAIN_MODES)

# Modos de entrada: TEXT (consola), TTS (consola + voz de salida) y VOICE (manos libres).
# Los argumentos --tts / --voice de main.py tienen prioridad sobre este valor.
INPUT_MODES: tuple[str, ...] = ("TEXT", "TTS", "VOICE")
DEFAULT_INPUT_MODE: str = _env_choice("DEFAULT_INPUT_MODE", "TEXT", INPUT_MODES)

# Proveedor cloud que AUTO usa cuando una solicitud compleja justifica salir del equipo.
# Sin evidencia objetiva para preferir uno, se usa un único predeterminado configurable.
CLOUD_PROVIDERS: tuple[str, ...] = ("GEMINI", "CLAUDE", "GPT")
AUTO_CLOUD_PROVIDER: str = _env_choice("AUTO_CLOUD_PROVIDER", "GEMINI", CLOUD_PROVIDERS)
# Preferencias opcionales por categoría (por defecto, el mismo proveedor general).
AUTO_CODE_PROVIDER: str = _env_choice("AUTO_CODE_PROVIDER", AUTO_CLOUD_PROVIDER, CLOUD_PROVIDERS)
AUTO_LONGFORM_PROVIDER: str = _env_choice("AUTO_LONGFORM_PROVIDER", AUTO_CLOUD_PROVIDER, CLOUD_PROVIDERS)

# AUTO solo puede elegir el modelo local alternativo ante una petición EXPLÍCITA de estilo menos
# restrictivo y sin señales de daño. Con 0 nunca lo elige automáticamente (sigue disponible manualmente).
AUTO_ALLOW_UNCENSORED: bool = _env_bool("AUTO_ALLOW_UNCENSORED", True)

# Permite enviar una solicitud a la nube cuando el motor local falla (política de fallback).
BRAIN_ALLOW_CLOUD_FALLBACK: bool = _env_bool("BRAIN_ALLOW_CLOUD_FALLBACK", True)

# Límites de generación por estilo de salida. Voz conserva los 300 tokens históricos del proyecto.
VOICE_MAX_TOKENS: int = _env_int("VOICE_MAX_TOKENS", 300, minimum=32)
TEXT_MAX_TOKENS: int = _env_int("TEXT_MAX_TOKENS", 1500, minimum=64)

# Tiempos de espera (segundos). En urllib el timeout limita cada operación de socket (conexión o
# lectura), no la duración total; por eso los clientes cloud también controlan un plazo total.
# Las respuestas largas en modo texto tardan más en generarse antes del primer byte.
CLOUD_TIMEOUT: float = _env_float("CLOUD_TIMEOUT", 15.0, minimum=1.0)
CLOUD_TEXT_TIMEOUT: float = _env_float("CLOUD_TEXT_TIMEOUT", 45.0, minimum=1.0)
CLOUD_TOTAL_TIMEOUT: float = _env_float("CLOUD_TOTAL_TIMEOUT", 90.0, minimum=5.0)
# Ollama: plazo de httpx por operación (incluye la carga en frío del modelo en VRAM).
OLLAMA_TIMEOUT: float = _env_float("OLLAMA_TIMEOUT", 120.0, minimum=5.0)

# ==============================================================================
# INTEGRACIÓN OFICIAL DE OPENAI CODEX CLI (MOTOR ESPECIALIZADO DE PROGRAMACIÓN)
# ==============================================================================
# Niveles de sandbox permitidos: nunca se acepta 'danger-full-access' ni '--yolo'.
CODEX_ALLOWED_SANDBOXES: tuple[str, ...] = ("read-only", "workspace-write")


def _env_sandbox(name: str, default: str = "workspace-write") -> str:
    """
    Valida el nivel máximo de sandbox permitido para Codex CLI.
    Rechaza explícitamente 'danger-full-access', '--yolo' o cualquier valor no autorizado.
    """
    raw = os.getenv(name, default)
    normalized = (raw or "").strip().lower().replace("_", "-")
    if normalized in CODEX_ALLOWED_SANDBOXES:
        return normalized
    CONFIG_WARNINGS.append(
        f"{name}='{raw}' no está permitido (solo: {', '.join(CODEX_ALLOWED_SANDBOXES)}). "
        f"Se usa '{default}'."
    )
    return default


def _env_workspace_dir(name: str, default_dir: Path) -> str:
    """
    Resuelve y valida que el directorio de trabajo de Codex esté dentro de la raíz del proyecto
    (`BASE_DIR`). Si apunta fuera del proyecto o no existe, restablece `BASE_DIR` y emite aviso.
    """
    raw = os.getenv(name, "").strip()
    base_resolved = default_dir.resolve()
    if not raw:
        return str(base_resolved)
    try:
        candidate = Path(raw).expanduser().resolve()
        if candidate.exists() and candidate.is_dir() and candidate.is_relative_to(base_resolved):
            return str(candidate)
    except (OSError, ValueError):
        pass
    CONFIG_WARNINGS.append(
        f"{name}='{raw}' no es un directorio válido dentro de '{base_resolved}'. "
        f"Se usa '{base_resolved}'."
    )
    return str(base_resolved)


# Habilita o deshabilita el motor Codex CLI. Si es False, J.A.R.V.I.S. no ejecuta el proceso.
CODEX_ENABLED: bool = _env_bool("CODEX_ENABLED", True)

# Tiempo máximo de ejecución (segundos) para cada subproceso `codex exec`.
CODEX_TIMEOUT: float = _env_float("CODEX_TIMEOUT", 120.0, minimum=5.0)

# Directorio de trabajo permitido (validado dentro de BASE_DIR).
CODEX_WORKSPACE_DIR: str = _env_workspace_dir("CODEX_WORKSPACE_DIR", BASE_DIR)

# Permite que el modo AUTO delegue tareas de programación/depuración a Codex si está instalado y autenticado.
CODEX_ALLOW_AUTO: bool = _env_bool("CODEX_ALLOW_AUTO", True)

# Nivel máximo de aislamiento permitido ('read-only' o 'workspace-write'; las consultas usan siempre 'read-only').
CODEX_SANDBOX: str = _env_sandbox("CODEX_SANDBOX", "workspace-write")

# Modelo opcional para `codex exec` (vacío = usar el modelo predeterminado de la sesión ChatGPT/CLI).
CODEX_MODEL: str = os.getenv("CODEX_MODEL", "").strip()

# Si una variable inválida generó advertencias, se informan una vez al importar (sin secretos).
for _warning in CONFIG_WARNINGS:
    print(f"[Config] Aviso: {_warning}", file=sys.stderr)

