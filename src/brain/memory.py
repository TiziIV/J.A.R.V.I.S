"""Memoria conversacional temporal, limitada a los últimos intercambios."""

from collections import deque
from typing import Deque, Literal, TypedDict


class ConversationMessage(TypedDict):
    """Describe el formato de mensaje que entienden las APIs de chat."""

    role: Literal["user", "assistant"]
    content: str


class ConversationMemory:
    """Guarda un número acotado de mensajes para dar contexto a la conversación."""

    def __init__(self, max_turns: int = 5):
        # Un turno normal contiene un mensaje del usuario y otro del asistente.
        if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1:
            raise ValueError("max_turns debe ser un entero mayor que cero.")

        self.max_turns = max_turns
        # La capacidad en mensajes es el doble porque cada turno incluye dos participantes.
        self._messages: Deque[ConversationMessage] = deque(maxlen=max_turns * 2)

    def add_message(self, role: str, content: str) -> None:
        """Agrega un mensaje y elimina automáticamente el más antiguo si se llena."""
        # Validamos el rol para que las APIs y el formato de contexto reciban valores conocidos.
        if role not in ("user", "assistant"):
            raise ValueError("role debe ser 'user' o 'assistant'.")
        if not isinstance(content, str):
            raise TypeError("content debe ser texto.")

        # deque conserva el orden de llegada y descarta el mensaje más viejo al superar el límite.
        self._messages.append({"role": role, "content": content})

    def get_context_prompt(self) -> str:
        """Devuelve el historial como texto simple para incluirlo en un prompt del modelo."""
        # Indicamos explícitamente cuando aún no hay contexto para que la salida sea predecible.
        if not self._messages:
            return "Historial reciente: (sin mensajes)."

        # Traducimos los roles técnicos a etiquetas fáciles de leer en español.
        role_labels = {"user": "Usuario", "assistant": "Asistente"}
        formatted_messages = [
            f"{role_labels[message['role']]}: {message['content']}"
            for message in self._messages
        ]

        # La cabecera separa claramente la memoria de las instrucciones del sistema.
        return "Historial reciente:\n" + "\n".join(formatted_messages)

    def get_messages_list(self) -> list[ConversationMessage]:
        """Retorna copias de mensajes en formato compatible con APIs de chat."""
        # Copiar los diccionarios evita que un cliente externo modifique la memoria interna.
        return [
            {"role": message["role"], "content": message["content"]}
            for message in self._messages
        ]

    def clear(self) -> None:
        """Elimina todo el contexto, por ejemplo cuando el usuario pide reiniciar."""
        # clear vacía el deque y mantiene su capacidad para los siguientes turnos.
        self._messages.clear()
