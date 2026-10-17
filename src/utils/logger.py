"""
Módulo de registro y logging centralizado de J.A.R.V.I.S.

Configura el logger del sistema con salida dual (archivo de registro 'jarvis_system.log' en la raíz
y consola estándar). Resuelve la ruta del archivo de log de forma absoluta al mismo nivel que 'main.py'
y fuerza la codificación UTF-8 para garantizar la compatibilidad con tildes y caracteres especiales en Windows.
"""

import logging
import os
import sys

# Determinamos la ruta absoluta a la raíz del proyecto (donde reside main.py).
# ¿Por qué se hace así?
# - Si la aplicación está empaquetada con PyInstaller, sys.executable apunta a la carpeta de ejecución.
# - En modo desarrollo normal, este archivo está en <RAIZ>/src/utils/logger.py, por lo que retroceder
#   dos niveles (con '..', '..') nos sitúa de manera exacta e invariable en la raíz del proyecto.
if getattr(sys, "frozen", False):
    PROJECT_ROOT: str = os.path.dirname(os.path.abspath(sys.executable))
else:
    _CURRENT_FILE_DIR: str = os.path.dirname(os.path.abspath(__file__))
    PROJECT_ROOT: str = os.path.abspath(os.path.join(_CURRENT_FILE_DIR, "..", ".."))

# Ruta absoluta estricta del archivo de registro al mismo nivel que main.py
LOG_FILE_PATH: str = os.path.abspath(os.path.join(PROJECT_ROOT, "jarvis_system.log"))


def setup_logger(name: str = "JARVIS") -> logging.Logger:
    """
    Configura y retorna una instancia de Logger con manejador de archivo UTF-8 y consola.

    Args:
        name (str): Nombre identificador del logger.

    Returns:
        logging.Logger: Instancia del logger lista para escribir eventos.
    """
    custom_logger = logging.getLogger(name)
    custom_logger.setLevel(logging.INFO)
    # Evitamos que los mensajes se propaguen al root logger y se impriman duplicados en consola
    custom_logger.propagate = False

    # Evitamos duplicación de handlers si la función se invoca múltiples veces
    if not custom_logger.handlers:
        # Formato estándar claro con marca de tiempo, nivel de severidad y mensaje
        formatter = logging.Formatter(
            fmt="%(asctime)s - [%(levelname)s] - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # 1. FileHandler: escribe en la raíz del proyecto con codificación UTF-8 obligatoria.
        # Esto previene errores de codificación (UnicodeEncodeError) en consolas o sistemas Windows.
        file_handler = logging.FileHandler(LOG_FILE_PATH, encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        custom_logger.addHandler(file_handler)

        # 2. StreamHandler: emite los mismos mensajes a la consola estándar para visualización directa.
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        custom_logger.addHandler(console_handler)

    return custom_logger


# Instancia singleton preconfigurada para importación directa en otros módulos:
# Uso: from src.utils.logger import logger
logger: logging.Logger = setup_logger("JARVIS")

