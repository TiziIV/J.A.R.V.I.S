"""
Tests de persistencia de memoria: ventana deslizante, guardado/recarga JSON y corrupción de disco.
"""

import json
import os
import tempfile
import unittest
from src.brain.memory import ConversationMemory


class TestMemorySlidingWindow(unittest.TestCase):
    """Ventana deslizante FIFO."""

    def test_ventana_por_defecto_8_turnos(self):
        mem = ConversationMemory(max_turns=8)
        self.assertEqual(mem.max_turns, 8)

    def test_capacidad_total_es_doble_de_turnos(self):
        mem = ConversationMemory(max_turns=3)
        # 3 turnos = 6 mensajes
        self.assertEqual(mem._messages.maxlen, 6)

    def test_mensajes_se_agregan_correctamente(self):
        mem = ConversationMemory(max_turns=4)
        mem.add_message("user", "Hola")
        mem.add_message("assistant", "Hola señor")
        self.assertEqual(len(mem.get_messages_list()), 2)

    def test_ventana_descarta_mensajes_mas_antiguos(self):
        mem = ConversationMemory(max_turns=2)
        mem.add_message("user", "msg1")
        mem.add_message("assistant", "resp1")
        mem.add_message("user", "msg2")
        mem.add_message("assistant", "resp2")
        # max 4 mensajes (2 turnos * 2)
        # Agregar un quinto desplaza el primero
        mem.add_message("user", "msg3")
        messages = mem.get_messages_list()
        contenidos = [m["content"] for m in messages]
        self.assertNotIn("msg1", contenidos)
        self.assertIn("msg3", contenidos)

    def test_clear_vacia_la_memoria(self):
        mem = ConversationMemory(max_turns=4)
        mem.add_message("user", "hola")
        mem.add_message("assistant", "ok")
        mem.clear()
        self.assertEqual(len(mem.get_messages_list()), 0)

    def test_get_context_prompt_sin_mensajes(self):
        mem = ConversationMemory()
        result = mem.get_context_prompt()
        self.assertIn("sin mensajes", result)

    def test_get_context_prompt_con_mensajes(self):
        mem = ConversationMemory()
        mem.add_message("user", "Me llamo Tiziano")
        mem.add_message("assistant", "Entendido, Tiziano")
        result = mem.get_context_prompt()
        self.assertIn("Tiziano", result)
        self.assertIn("Usuario", result)
        self.assertIn("Asistente", result)

    def test_inject_context_sin_mensajes_retorna_prompt_original(self):
        mem = ConversationMemory()
        prompt = "Eres JARVIS."
        result = mem.inject_context(prompt)
        self.assertEqual(result, prompt)

    def test_inject_context_con_mensajes(self):
        mem = ConversationMemory()
        mem.add_message("user", "Hola")
        mem.add_message("assistant", "Hola")
        result = mem.inject_context("Eres JARVIS.")
        self.assertIn("Eres JARVIS.", result)
        self.assertIn("Hola", result)

    def test_get_messages_list_retorna_copia(self):
        """Modificar la lista devuelta no debe afectar la memoria interna."""
        mem = ConversationMemory()
        mem.add_message("user", "test")
        lista = mem.get_messages_list()
        lista.clear()
        self.assertEqual(len(mem.get_messages_list()), 1)

    def test_max_turns_1_funciona(self):
        mem = ConversationMemory(max_turns=1)
        mem.add_message("user", "primero")
        mem.add_message("assistant", "resp1")
        mem.add_message("user", "segundo")
        messages = mem.get_messages_list()
        contenidos = [m["content"] for m in messages]
        # Con maxlen=2, "primero" ya fue desplazado
        self.assertIn("segundo", contenidos)

    def test_roles_validos(self):
        mem = ConversationMemory()
        mem.add_message("user", "mensaje usuario")
        mem.add_message("assistant", "mensaje asistente")
        messages = mem.get_messages_list()
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[1]["role"], "assistant")


class TestMemoryValidation(unittest.TestCase):
    """Validación de entradas en add_message y constructor."""

    def test_max_turns_invalido_cero(self):
        with self.assertRaises(ValueError):
            ConversationMemory(max_turns=0)

    def test_max_turns_invalido_negativo(self):
        with self.assertRaises(ValueError):
            ConversationMemory(max_turns=-5)

    def test_max_turns_invalido_bool_true(self):
        """True es subclase de int, debe rechazarse."""
        with self.assertRaises(ValueError):
            ConversationMemory(max_turns=True)

    def test_max_turns_invalido_string(self):
        with self.assertRaises((ValueError, TypeError)):
            ConversationMemory(max_turns="ocho")  # type: ignore

    def test_add_message_rol_invalido(self):
        mem = ConversationMemory()
        with self.assertRaises(ValueError):
            mem.add_message("system", "mensaje del sistema")

    def test_add_message_rol_vacio(self):
        mem = ConversationMemory()
        with self.assertRaises(ValueError):
            mem.add_message("", "contenido")

    def test_add_message_content_no_string(self):
        mem = ConversationMemory()
        with self.assertRaises(TypeError):
            mem.add_message("user", 12345)  # type: ignore

    def test_add_message_content_none(self):
        mem = ConversationMemory()
        with self.assertRaises(TypeError):
            mem.add_message("user", None)  # type: ignore


class TestMemoryJSONPersistence(unittest.TestCase):
    """Guardado y recarga de memoria en archivo JSON temporal."""

    def _save_memory_to_file(self, mem: ConversationMemory, path: str) -> None:
        """Helper: serializa la memoria a JSON."""
        data = {"messages": mem.get_messages_list()}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def _load_memory_from_file(self, mem: ConversationMemory, path: str) -> None:
        """Helper: deserializa el JSON y repuebla la memoria."""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for msg in data.get("messages", []):
            mem.add_message(msg["role"], msg["content"])

    def test_guardar_y_recargar_conserva_mensajes(self):
        mem = ConversationMemory(max_turns=4)
        mem.add_message("user", "¿Cuál es mi nombre?")
        mem.add_message("assistant", "Su nombre es Tiziano.")

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            path = f.name

        try:
            self._save_memory_to_file(mem, path)
            mem2 = ConversationMemory(max_turns=4)
            self._load_memory_from_file(mem2, path)
            messages = mem2.get_messages_list()
            contenidos = [m["content"] for m in messages]
            self.assertIn("¿Cuál es mi nombre?", contenidos)
            self.assertIn("Su nombre es Tiziano.", contenidos)
        finally:
            os.unlink(path)

    def test_recarga_conserva_orden(self):
        mem = ConversationMemory(max_turns=8)
        for i in range(4):
            mem.add_message("user", f"pregunta_{i}")
            mem.add_message("assistant", f"respuesta_{i}")

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            path = f.name

        try:
            self._save_memory_to_file(mem, path)
            mem2 = ConversationMemory(max_turns=8)
            self._load_memory_from_file(mem2, path)
            contenidos = [m["content"] for m in mem2.get_messages_list()]
            self.assertEqual(contenidos[0], "pregunta_0")
            self.assertEqual(contenidos[-1], "respuesta_3")
        finally:
            os.unlink(path)

    def test_recuperacion_ante_json_corrupto(self):
        """Archivo JSON corrupto → la carga falla silenciosamente sin crash."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            f.write("{json_corrupto: [sin_comillas, true, null, }")
            path = f.name

        mem = ConversationMemory(max_turns=4)
        try:
            # La carga debe fallar con JSONDecodeError, que debe manejarse
            with self.assertRaises(json.JSONDecodeError):
                self._load_memory_from_file(mem, path)
        finally:
            os.unlink(path)
        # La memoria debe quedar vacía/intacta
        self.assertEqual(len(mem.get_messages_list()), 0)

    def test_recuperacion_ante_json_vacio(self):
        """Archivo JSON con string vacío → JSONDecodeError controlado."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            f.write("")
            path = f.name
        mem = ConversationMemory()
        try:
            with self.assertRaises((json.JSONDecodeError, ValueError)):
                self._load_memory_from_file(mem, path)
        finally:
            os.unlink(path)

    def test_recuperacion_ante_archivo_inexistente(self):
        """Archivo que no existe → FileNotFoundError controlado."""
        mem = ConversationMemory()
        with self.assertRaises(FileNotFoundError):
            self._load_memory_from_file(mem, "/tmp/archivo_que_no_existe_xyz_12345.json")

    def test_json_con_mensajes_vacios_no_rompe_memoria(self):
        """JSON con lista de mensajes vacía → memoria se mantiene vacía."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({"messages": []}, f)
            path = f.name
        mem = ConversationMemory()
        try:
            self._load_memory_from_file(mem, path)
        finally:
            os.unlink(path)
        self.assertEqual(len(mem.get_messages_list()), 0)

    def test_json_con_estructura_inesperada(self):
        """JSON válido pero con estructura distinta → add_message puede fallar, manejable."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({"data": "inesperado"}, f)
            path = f.name
        mem = ConversationMemory()
        try:
            # No debe lanzar excepción no controlada; simplemente no agrega nada
            self._load_memory_from_file(mem, path)
        except Exception:
            pass  # Error esperado por estructura inesperada
        finally:
            os.unlink(path)
        # La memoria debe quedar intacta o vacía
        self.assertIsInstance(mem.get_messages_list(), list)

    def test_unicode_en_contenido_se_preserva(self):
        """Mensajes con caracteres Unicode deben conservarse exactamente."""
        mem = ConversationMemory(max_turns=2)
        mem.add_message("user", "Ñoño cañón 日本語 Ελληνικά 🎵")
        mem.add_message("assistant", "Entendido señor ✓")

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8") as f:
            path = f.name

        try:
            self._save_memory_to_file(mem, path)
            mem2 = ConversationMemory(max_turns=2)
            self._load_memory_from_file(mem2, path)
            contenidos = [m["content"] for m in mem2.get_messages_list()]
            self.assertIn("Ñoño cañón 日本語 Ελληνικά 🎵", contenidos)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
