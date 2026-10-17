import unittest

from src.brain.memory import ConversationMemory


class TestMemoryOverflow(unittest.TestCase):
    """
    Suite de pruebas para validar el recorte de ventana deslizante
    y protección contra desbordamiento de memoria en ConversationMemory.
    """

    def test_inject_100_turns_overflow_trimming(self) -> None:
        """
        Inyecta 100 turnos completos (200 mensajes) de conversación y valida que
        la memoria conserve únicamente los últimos max_turns (5 turnos = 10 mensajes).
        """
        max_turns = 5
        memory = ConversationMemory(max_turns=max_turns)

        # Inyectamos 100 turnos secuenciales
        for turn_index in range(1, 101):
            user_msg = f"Pregunta número {turn_index} del usuario"
            assistant_msg = f"Respuesta número {turn_index} de Jarvis"
            memory.add_message(role="user", content=user_msg)
            memory.add_message(role="assistant", content=assistant_msg)

        messages = memory.get_messages_list()

        # El límite exacto debe ser max_turns * 2 (10 mensajes)
        self.assertEqual(
            len(messages),
            max_turns * 2,
            f"La memoria debe recortarse estrictamente a {max_turns * 2} mensajes.",
        )

        # Los mensajes retenidos deben corresponder a los últimos 5 turnos (del 96 al 100)
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["content"], "Pregunta número 96 del usuario")
        self.assertEqual(messages[-1]["role"], "assistant")
        self.assertEqual(messages[-1]["content"], "Respuesta número 100 de Jarvis")

        # Comprobamos que el prompt de contexto contiene solo los turnos recientes
        context_prompt = memory.get_context_prompt()
        self.assertIn("Pregunta número 96", context_prompt)
        self.assertIn("Respuesta número 100", context_prompt)
        self.assertNotIn("Pregunta número 1 del usuario", context_prompt)
        self.assertNotIn("Pregunta número 50 del usuario", context_prompt)

    def test_custom_max_turns_trimming(self) -> None:
        """Valida que una capacidad personalizada (ej. max_turns=3) retenga exactamente 6 mensajes."""
        memory = ConversationMemory(max_turns=3)

        for i in range(1, 20):
            memory.add_message(role="user", content=f"U-{i}")
            memory.add_message(role="assistant", content=f"A-{i}")

        messages = memory.get_messages_list()
        self.assertEqual(len(messages), 6)
        self.assertEqual(messages[0]["content"], "U-17")
        self.assertEqual(messages[-1]["content"], "A-19")

    def test_memory_clear_after_overflow(self) -> None:
        """Valida que el método clear vacía el historial y restaura el estado inicial."""
        memory = ConversationMemory(max_turns=5)
        for i in range(50):
            memory.add_message(role="user", content=f"Msg {i}")

        self.assertGreater(len(memory.get_messages_list()), 0)
        memory.clear()

        self.assertEqual(len(memory.get_messages_list()), 0)
        self.assertEqual(memory.get_context_prompt(), "Historial reciente: (sin mensajes).")

    def test_invalid_parameters_raise_exceptions(self) -> None:
        """Valida que entradas o configuraciones inválidas levantan las excepciones esperadas."""
        with self.assertRaises(ValueError):
            ConversationMemory(max_turns=0)
        with self.assertRaises(ValueError):
            ConversationMemory(max_turns=-5)

        mem = ConversationMemory(max_turns=3)
        with self.assertRaises(ValueError):
            mem.add_message(role="system", content="Texto")
        with self.assertRaises(TypeError):
            mem.add_message(role="user", content=12345)  # type: ignore


if __name__ == "__main__":
    unittest.main()

