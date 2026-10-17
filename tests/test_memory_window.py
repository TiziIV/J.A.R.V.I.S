import unittest

from src.brain.memory import ConversationMemory


class TestMemoryWindow(unittest.TestCase):
    """
    Pruebas unitarias para validar la ventana deslizante de ConversationMemory
    y la retención estricta de los últimos 8 turnos (16 mensajes) ante 20 entradas.
    """

    def test_fill_memory_with_20_messages_trims_oldest(self) -> None:
        """
        Llena la memoria con 20 mensajes (10 pares usuario/asistente) y verifica
        que se mantengan estrictamente los 16 más recientes, recortando los primeros 4.
        """
        memory = ConversationMemory(max_turns=8)  # Capacidad: 8 * 2 = 16 mensajes

        # Inyectamos 10 turnos completos (20 mensajes en total)
        for i in range(1, 11):
            memory.add_message("user", f"Pregunta {i}")
            memory.add_message("assistant", f"Respuesta {i}")

        messages = memory.get_messages_list()

        # Comprobamos que el tamaño máximo no exceda 16 mensajes
        self.assertEqual(len(messages), 16, "La memoria con max_turns=8 debe contener exactamente 16 mensajes.")

        # Los mensajes 1 y 2 (primeros 4 mensajes: U1, A1, U2, A2) deben haber sido eliminados
        # El primer mensaje retenido debe ser el turno 3 del usuario
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["content"], "Pregunta 3")
        self.assertEqual(messages[1]["role"], "assistant")
        self.assertEqual(messages[1]["content"], "Respuesta 3")

        # El último mensaje debe ser el turno 10 del asistente
        self.assertEqual(messages[-1]["role"], "assistant")
        self.assertEqual(messages[-1]["content"], "Respuesta 10")

    def test_inject_context_method(self) -> None:
        """Verifica que inject_context concatena el prompt maestro con el historial reciente."""
        memory = ConversationMemory(max_turns=8)
        base_prompt = "Eres J.A.R.V.I.S., un asistente técnico."

        # Sin mensajes, inject_context debe retornar el base_prompt sin alterar
        self.assertEqual(memory.inject_context(base_prompt), base_prompt)

        # Con mensajes agregados, debe incorporar el historial
        memory.add_message("user", "Mi color favorito es azul")
        memory.add_message("assistant", "Entendido señor")

        injected = memory.inject_context(base_prompt)
        self.assertIn(base_prompt, injected)
        self.assertIn("Mi color favorito es azul", injected)
        self.assertIn("Entendido señor", injected)


if __name__ == "__main__":
    unittest.main()

