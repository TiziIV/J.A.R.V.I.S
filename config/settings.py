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

# Modelo preferido de Google Gemini (gemini-2.0-flash para máxima velocidad de inferencia en la nube)
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

# URL base del daemon local de Ollama (por defecto localhost en el puerto 11434)
OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

# Modelo local ligero optimizado para 6 GB VRAM (GTX 1660): qwen2.5-coder:3b consume ~2.2 GB VRAM
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:3b")

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

