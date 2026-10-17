import unittest
from unittest.mock import MagicMock, patch

from src.ui.tray import JarvisTray


class TestJarvisTray(unittest.TestCase):
    """Pruebas unitarias para la interfaz en segundo plano de JarvisTray."""

    def test_circle_image_generation(self) -> None:
        """Verifica que la generación dinámica de íconos crea imágenes RGBA con las dimensiones correctas."""
        img_blue = JarvisTray._create_circle_image((30, 144, 255), (64, 64))
        self.assertEqual(img_blue.size, (64, 64))
        self.assertEqual(img_blue.mode, "RGBA")

    def test_state_color_transitions(self) -> None:
        """Verifica que cambiar de estado actualiza el estado interno y no arroja errores."""
        tray = JarvisTray()
        tray.set_state("hablando")
        self.assertEqual(tray.current_state, "hablando")
        tray.set_state("escuchando")
        self.assertEqual(tray.current_state, "escuchando")
        tray.set_state("reposo")
        self.assertEqual(tray.current_state, "reposo")

    def test_callbacks_invoked_on_menu_events(self) -> None:
        """Verifica que los manejadores de menú ejecutan los callbacks configurados."""
        status_mock = MagicMock()
        clear_mock = MagicMock()
        exit_mock = MagicMock()

        tray = JarvisTray(
            on_status=status_mock,
            on_clear_memory=clear_mock,
            on_exit=exit_mock,
        )

        dummy_icon = MagicMock()
        dummy_item = MagicMock()

        tray._handle_status(dummy_icon, dummy_item)
        status_mock.assert_called_once()

        tray._handle_clear_memory(dummy_icon, dummy_item)
        clear_mock.assert_called_once()

        tray._handle_exit(dummy_icon, dummy_item)
        exit_mock.assert_called_once()
        self.assertTrue(tray.is_exit_requested)


if __name__ == "__main__":
    unittest.main()

