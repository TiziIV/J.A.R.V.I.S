"""
Configuración para el módulo de detección de palabra de activación (Wake Word / Hands-Free) de J.A.R.V.I.S.
Permite ajustar el modelo openWakeWord, la sensibilidad, la frecuencia de muestreo y el tamaño de los fragmentos de audio.
"""

import os
from dotenv import load_dotenv

# Cargamos las variables de entorno desde el archivo .env (si existe)
load_dotenv()

# Nombre del modelo de palabra de activación a utilizar por defecto
# openWakeWord incluye modelos preentrenados como 'hey_jarvis', 'alexa', 'hey_mycroft', etc.
WAKE_WORD_MODEL: str = os.getenv("WAKE_WORD_MODEL", "hey_jarvis")

# Umbral de confianza (threshold) para la activación (entre 0.0 y 1.0)
# Un valor de 0.3 ofrece un buen balance entre detección precisa y prevención de falsos positivos.
WAKE_WORD_THRESHOLD: float = float(os.getenv("WAKE_WORD_THRESHOLD", "0.3"))

# Frecuencia de muestreo requerida por openWakeWord (estándar de 16 kHz en mono)
WAKE_SAMPLE_RATE: int = int(os.getenv("WAKE_SAMPLE_RATE", "16000"))

# Tamaño del fragmento (chunk) de audio procesado por iteración (en número de muestras)
# 1280 muestras a 16 kHz equivalen a exactamente 80 milisegundos de audio, ideal para baja latencia en CPU.
WAKE_CHUNK_SIZE: int = int(os.getenv("WAKE_CHUNK_SIZE", "1280"))
