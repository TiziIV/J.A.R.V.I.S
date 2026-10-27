"""
Suite de pruebas unitarias para las nuevas capacidades de J.A.R.V.I.S.:
- Corte de voz (barge-in) y sanitizador anti-markdown de TTS.
- Reparador de JSON truncado en IntentRouter.
- Registro y ejecución de las 4 nuevas skills nativas de Windows.
- Persistencia automática en disco y gestión de preferencias de ConversationMemory.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from config.tools_schema import TOOLS_SCHEMA
from skills.windows_skills import (
    VolumeControlSkill,
    MediaControlSkill,
    ClipboardSkill,
    SystemPowerSkill,
)
from src.audio.tts import TextToSpeech
from src.brain.intent_router import IntentRouter
from src.brain.memory import ConversationMemory
from src.tools.skill_manager import SkillManager


class TestTTSSanitizerAndBargeIn(unittest.TestCase):
    """Pruebas del filtro anti-markdown y la interrupción de habla en TTS."""

    def test_sanitize_removes_code_blocks(self):
        text = "Aquí está el código:\n```python\nprint('hola')\n```\nEso es todo."
        clean = TextToSpeech._sanitize_for_speech(text)
        self.assertNotIn("print", clean)
        self.assertNotIn("```", clean)
        self.assertIn("Aquí está el código:", clean)

    def test_sanitize_removes_inline_code_and_headers(self):
        text = "### Instrucción\nEjecute el comando `dir /w` para listar."
        clean = TextToSpeech._sanitize_for_speech(text)
        self.assertNotIn("###", clean)
        self.assertNotIn("`", clean)
        self.assertIn("Instrucción", clean)

    def test_sanitize_removes_numbered_lists_and_bullets(self):
        text = "Pasos a seguir:\n1. Primer paso\n2. Segundo paso\n- Nota extra"
        clean = TextToSpeech._sanitize_for_speech(text)
        self.assertNotIn("1.", clean)
        self.assertNotIn("2.", clean)
        self.assertNotIn("- ", clean)
        self.assertIn("Primer paso", clean)

    def test_stop_terminates_active_process(self):
        tts = TextToSpeech()
        mock_process = MagicMock()
        tts.current_process = mock_process
        tts.is_speaking = True

        tts.stop()

        mock_process.terminate.assert_called_once()
        self.assertIsNone(tts.current_process)
        self.assertFalse(tts.is_speaking)
        tts.close()


class TestIntentRouterJSONRepair(unittest.TestCase):
    """Pruebas para el reparador automático de JSON truncado."""

    def test_recovers_truncated_chat_json(self):
        raw_truncated = '{"action":"chat","response":"Hola señor, el clima hoy está soleado y agradable'
        recovered = IntentRouter._parse_json_response(raw_truncated)
        self.assertEqual(recovered.get("action"), "chat")
        self.assertIn("Hola señor, el clima", recovered.get("response", ""))

    def test_recovers_truncated_chat_json_with_trailing_chars(self):
        raw_truncated = '{"action":"chat","response":"Mensaje cortado a la mitad...'
        recovered = IntentRouter._parse_json_response(raw_truncated)
        self.assertEqual(recovered.get("action"), "chat")
        self.assertEqual(recovered.get("response"), "Mensaje cortado a la mitad...")


class TestWindowsSkillsIntegration(unittest.TestCase):
    """Pruebas de instanciación, ejecución simulada y registro en SkillManager."""

    def test_all_new_skills_registered_in_skill_manager(self):
        manager = SkillManager()
        registered_names = set(manager.skills.keys())
        expected = {"volume_control", "media_control", "clipboard_manager", "system_power"}
        self.assertTrue(expected.issubset(registered_names))

    def test_all_new_skills_in_tools_schema(self):
        schema_names = {tool["name"] for tool in TOOLS_SCHEMA}
        expected = {"volume_control", "media_control", "clipboard_manager", "system_power"}
        self.assertTrue(expected.issubset(schema_names))

    @patch("ctypes.windll.user32.keybd_event")
    def test_volume_control_skill_subir(self, mock_keybd):
        skill = VolumeControlSkill()
        res = skill.execute(action="subir")
        self.assertIn("subido", res.lower())
        self.assertTrue(mock_keybd.called)

    @patch("ctypes.windll.user32.keybd_event")
    def test_media_control_skill_pausar(self, mock_keybd):
        skill = MediaControlSkill()
        res = skill.execute(action="pausar")
        self.assertIn("alternada", res.lower())
        self.assertTrue(mock_keybd.called)

    @patch("ctypes.windll.user32.LockWorkStation", return_value=1)
    def test_system_power_skill_bloquear(self, mock_lock):
        skill = SystemPowerSkill()
        res = skill.execute(action="bloquear")
        self.assertIn("bloqueada", res.lower())
        mock_lock.assert_called_once()

    @patch("subprocess.run")
    def test_system_power_skill_cancelar_apagado(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        skill = SystemPowerSkill()
        res = skill.execute(action="cancelar_apagado")
        self.assertIn("cancelado", res.lower())
        mock_run.assert_called_once()

    def test_clipboard_skill_validation_empty(self):
        skill = ClipboardSkill()
        res = skill.execute(action="copiar", text="")
        self.assertIn("no recibí texto", res.lower())


class TestMemoryPersistenceToDisk(unittest.TestCase):
    """Pruebas para persistencia automática de ConversationMemory a disco."""

    def test_save_and_reload_from_custom_file(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
            temp_path = tf.name

        try:
            mem = ConversationMemory(max_turns=4, storage_file=temp_path, auto_save=True)
            mem.add_message("user", "Recuerda que mi nombre es Tiziano")
            mem.add_message("assistant", "Entendido, señor Tiziano.")
            mem.set_preference("usuario", "Tiziano")
            mem.set_preference("ciudad", "Buenos Aires")

            # Verificamos que el archivo en disco exista y tenga contenido
            self.assertTrue(os.path.exists(temp_path))
            with open(temp_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(len(data.get("messages", [])), 2)
            self.assertEqual(data.get("preferences", {}).get("usuario"), "Tiziano")

            # Instanciamos una nueva memoria apuntando al mismo archivo
            mem2 = ConversationMemory(max_turns=4, storage_file=temp_path, auto_save=True)
            self.assertEqual(len(mem2.get_messages_list()), 2)
            self.assertEqual(mem2.get_preference("ciudad"), "Buenos Aires")
            context = mem2.get_context_prompt()
            self.assertIn("Tiziano", context)
            self.assertIn("Buenos Aires", context)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)


if __name__ == "__main__":
    unittest.main()
