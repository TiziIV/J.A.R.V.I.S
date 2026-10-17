"""
Memoria conversacional temporal de J.A.R.V.I.S., limitada a los últimos intercambios (ventana deslizante).
"""

from collections import deque
from typing import Deque, Literal, TypedDict


class ConversationMessage(TypedDict):
    """Describe el formato de mensaje que entienden las APIs de chat."""

    role: Literal["user", "assistant"]
    content: str


class ConversationMemory:
    """
    Guarda un número acotado de turnos de conversación para proporcionar
    contexto reciente al LLM sin desbordar la ventana de tokens.
    """

    def __init__(self, max_turns: int = 8) -> None:
        """
        Inicializa la memoria con un límite de turnos (por defecto 8 turnos = 16 mensajes).

        Args:
            max_turns (int): Número máximo de turnos (pares de usuario/asistente) a retener.
        """
        if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1:
            raise ValueError("max_turns debe ser un entero mayor que cero.")

        self.max_turns = max_turns
        # La capacidad en mensajes individuales es el doble porque cada turno incluye usuario y asistente.
        self._messages: Deque[ConversationMessage] = deque(maxlen=max_turns * 2)

    def add_message(self, role: str, content: str) -> None:
        """Agrega un mensaje y elimina automáticamente el más antiguo si la cola se llena."""
        if role not in ("user", "assistant"):
            raise ValueError("role debe ser 'user' o 'assistant'.")
        if not isinstance(content, str):
            raise TypeError("content debe ser texto.")

        self._messages.append({"role": role, "content": content})

    def get_context_prompt(self) -> str:
        """Devuelve el historial como texto simple formateado para incluirlo en un prompt."""
        if not self._messages:
            return "Historial reciente: (sin mensajes)."

        role_labels = {"user": "Usuario", "assistant": "Asistente"}
        formatted_messages = [
            f"{role_labels[message['role']]}: {message['content']}"
            for message in self._messages
        ]
        return "Historial reciente:\n" + "\n".join(formatted_messages)

    def inject_context(self, system_prompt: str) -> str:
        """
        Inyecta el historial conversacional formateado en el prompt maestro del sistema.

        Args:
            system_prompt (str): Prompt maestro base con instrucciones del sistema.

        Returns:
            str: Prompt completo con las instrucciones y el contexto inyectado.
        """
        context = self.get_context_prompt()
        if not context or "sin mensajes" in context:
            return system_prompt
        return f"{system_prompt}\n\n{context}\nUsa este historial previo para resolver referencias y responder contextualmente."

    def get_messages_list(self) -> list[ConversationMessage]:
        """Retorna copias de los mensajes en formato de lista de diccionarios para APIs de chat."""
        return [
            {"role": message["role"], "content": message["content"]}
            for message in self._messages
        ]

    def clear(self) -> None:
        """Elimina todo el historial conversacional acumulado."""
        self._messages.clear()
