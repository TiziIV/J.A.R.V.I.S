"""
Pruebas unitarias para el sistema de logging centralizado de J.A.R.V.I.S. ('src/utils/logger.py').

Valida:
1. Resolución absoluta de la ruta del archivo 'jarvis_system.log' en la raíz del proyecto.
2. Configuración obligatoria de codificación UTF-8 en el FileHandler.
3. Creación física del archivo en disco al registrar mensajes.
4. Soporte íntegro de caracteres especiales y tildes en español sin errores de codificación.
"""

import logging
import os
import unittest
from src.utils.logger import LOG_FILE_PATH, logger, PROJECT_ROOT


class TestJarvisLogger(unittest.TestCase):
    """
    Suite de pruebas para validar el comportamiento del logger centralizado.
    """

    def test_log_file_path_is_absolute_and_in_project_root(self):
        """
        Valida que LOG_FILE_PATH sea una ruta absoluta y apunte estrictamente
        al archivo 'jarvis_system.log' en la raíz del proyecto (junto a main.py).
        """
        self.assertTrue(os.path.isabs(LOG_FILE_PATH), "La ruta de log debe ser absoluta.")
        self.assertEqual(
            os.path.basename(LOG_FILE_PATH),
            "jarvis_system.log",
            "El nombre del archivo debe ser 'jarvis_system.log'.",
        )
        # Comprobamos que resida en el mismo directorio donde está main.py
        main_py_path = os.path.join(PROJECT_ROOT, "main.py")
        self.assertTrue(
            os.path.isfile(main_py_path),
            "PROJECT_ROOT debe contener 'main.py' en su raíz.",
        )
        self.assertEqual(
            os.path.dirname(LOG_FILE_PATH),
            PROJECT_ROOT,
            "El archivo de log debe estar en la misma carpeta que main.py.",
        )

    def test_file_handler_uses_utf8_encoding(self):
        """
        Verifica que al menos uno de los FileHandler configurados utilice codificación UTF-8.
        """
        file_handlers = [h for h in logger.handlers if isinstance(h, logging.FileHandler)]
        self.assertGreater(len(file_handlers), 0, "El logger debe tener al menos un FileHandler registrado.")
        
        utf8_handlers = [h for h in file_handlers if h.encoding and h.encoding.lower() in ("utf-8", "utf_8")]
        self.assertGreater(
            len(utf8_handlers),
            0,
            "El FileHandler debe estar configurado con encoding='utf-8' para soportar caracteres en español.",
        )

    def test_logging_creates_file_and_writes_utf8(self):
        """
        Valida que emitir un mensaje de log cree físicamente el archivo en disco
        y registre correctamente caracteres con tildes y símbolos en español.
        """
        test_message = "Prueba de inicialización y verificación con tildes: ¿Configuración de J.A.R.V.I.S. año 2026 exitosa?"
        logger.info(test_message)

        # Verificamos que el archivo exista físicamente en disco
        self.assertTrue(
            os.path.exists(LOG_FILE_PATH),
            f"El archivo de log debe existir físicamente en: {LOG_FILE_PATH}",
        )

        # Leemos el archivo asegurando codificación UTF-8
        with open(LOG_FILE_PATH, "r", encoding="utf-8") as log_file:
            content = log_file.read()

        self.assertIn(test_message, content, "El mensaje con caracteres especiales debe encontrarse dentro del archivo de log.")


if __name__ == "__main__":
    unittest.main()

