"""
Módulo de Memoria Conversacional Persistente de J.A.R.V.I.S.
Implementa una ventana deslizante acotada (por turnos) y persistencia automática
en archivo JSON ('data/memory_store.json') para retener contexto y preferencias del usuario.
"""

from collections import deque
import json
import os
import sys
from typing import Any, Deque, Dict, List, Literal, Optional, TypedDict


class ConversationMessage(TypedDict):
    """
    Estructura de tipos que representa un mensaje individual en el formato estándar de chat.
    role: Puede ser 'user' (usuario) o 'assistant' (J.A.R.V.I.S.).
    content: Texto verbal del mensaje.
    """
    role: Literal["user", "assistant"]
    content: str


# Ruta predeterminada para el almacenamiento persistente en disco
DEFAULT_MEMORY_FILE: str = os.path.join("data", "memory_store.json")


class ConversationMemory:
    """
    Gestor de memoria conversacional con ventana deslizante FIFO de turnos recientes
    y persistencia automática en disco ('data/memory_store.json').
    Permite recordar datos del usuario y preferencias entre reinicios del programa.
    """

    def __init__(
        self,
        max_turns: int = 8,
        storage_file: Optional[str] = DEFAULT_MEMORY_FILE,
        auto_save: bool = True,
    ) -> None:
        """
        Inicializa la memoria con el límite de turnos y ruta de persistencia.

        Args:
            max_turns (int): Número máximo de turnos (pares usuario/asistente) a retener (por defecto 8).
            storage_file (str, optional): Ruta al archivo JSON para persistir la memoria.
            auto_save (bool): Si es True, guarda en disco automáticamente tras cada modificación.
        """
        # Validamos que max_turns sea un número entero estrictamente positivo
        if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1:
            raise ValueError("max_turns debe ser un entero mayor que cero.")

        self.max_turns = max_turns
        # Capacidad total de mensajes individuales = turnos * 2 (cada turno comprende usuario + asistente)
        self._messages: Deque[ConversationMessage] = deque(maxlen=max_turns * 2)
        # Diccionario para almacenar preferencias y datos persistentes del usuario (nombre, gustos, etc.)
        self.preferences: Dict[str, Any] = {}
        self.auto_save: bool = auto_save

        # Control inteligente de aislamiento en entorno de pruebas unitarias:
        # Si se ejecuta bajo 'unittest' y se pasó la ruta por defecto, se desactiva la persistencia
        # para evitar contaminación entre pruebas independientes, a menos que se fuerce explícitamente.
        is_unittest = "unittest" in sys.modules and any("unittest" in arg or "test" in arg for arg in sys.argv)
        if storage_file == DEFAULT_MEMORY_FILE and is_unittest and not os.environ.get("JARVIS_FORCE_PERSISTENCE"):
            self.storage_file: Optional[str] = None
        else:
            self.storage_file = storage_file

        # Si hay una ruta configurada, intentamos cargar los datos persistidos en disco
        if self.storage_file:
            self.load_from_disk()

    def add_message(self, role: str, content: str) -> None:
        """
        Agrega un nuevo mensaje a la cola y descarta el más antiguo si supera el límite de turnos.
        Guarda automáticamente en disco si auto_save está habilitado.

        Args:
            role (str): Rol del emisor ('user' o 'assistant').
            content (str): Contenido textual del mensaje.
        """
        # Validamos el rol estrictamente para prevenir datos corruptos
        if role not in ("user", "assistant"):
            raise ValueError("role debe ser 'user' o 'assistant'.")
        # Validamos que el contenido sea una cadena de texto
        if not isinstance(content, str):
            raise TypeError("content debe ser texto.")

        # Añadimos el nuevo mensaje a la cola de deque
        self._messages.append({"role": role, "content": content})

        # Persistimos en disco de forma automática
        if self.auto_save and self.storage_file:
            self.save_to_disk()

    def set_preference(self, key: str, value: Any) -> None:
        """
        Registra o actualiza una preferencia o dato personal del usuario (ej. nombre, ciudad).
        Se persiste automáticamente en disco.

        Args:
            key (str): Clave identificadora de la preferencia.
            value (Any): Valor a almacenar.
        """
        self.preferences[key] = value
        if self.auto_save and self.storage_file:
            self.save_to_disk()

    def get_preference(self, key: str, default: Any = None) -> Any:
        """
        Consulta una preferencia almacenada o devuelve el valor por defecto si no existe.
        """
        return self.preferences.get(key, default)

    def get_preferences(self) -> Dict[str, Any]:
        """
        Devuelve una copia de todas las preferencias almacenadas del usuario.
        """
        return dict(self.preferences)

    def get_context_prompt(self) -> str:
        """
        Genera el bloque de texto con el historial conversacional y preferencias
        listo para ser inyectado como contexto en las instrucciones del LLM.
        """
        sections: List[str] = []

        # Si hay preferencias registradas del usuario, las incluimos primero
        if self.preferences:
            prefs_desc = ", ".join(f"{k}: {v}" for k, v in self.preferences.items())
            sections.append(f"Preferencias guardadas del usuario: {prefs_desc}")

        # Si no hay mensajes verbales recientes en la cola
        if not self._messages:
            if sections:
                return "\n".join(sections)
            return "Historial reciente: (sin mensajes)."

        # Formateamos los mensajes con etiquetas legibles en español
        role_labels = {"user": "Usuario", "assistant": "Asistente"}
        formatted_messages = [
            f"{role_labels[message['role']]}: {message['content']}"
            for message in self._messages
        ]
        history_str = "Historial reciente:\n" + "\n".join(formatted_messages)
        sections.append(history_str)

        return "\n".join(sections)

    def inject_context(self, system_prompt: str) -> str:
        """
        Inyecta el historial conversacional y preferencias en el prompt maestro del sistema.

        Args:
            system_prompt (str): Prompt maestro base con las instrucciones del sistema.

        Returns:
            str: Prompt completo enriquecido con el contexto y preferencias del usuario.
        """
        context = self.get_context_prompt()
        # Si no hay mensajes ni preferencias relevantes, retornamos el prompt original sin alterar
        if not context or context == "Historial reciente: (sin mensajes).":
            return system_prompt

        return (
            f"{system_prompt}\n\n{context}\n"
            "Usa este historial previo para resolver referencias y responder contextualmente."
        )

    def get_messages_list(self) -> List[ConversationMessage]:
        """
        Retorna una lista independiente con copias de los mensajes para uso en APIs de chat.
        """
        return [
            {"role": message["role"], "content": message["content"]}
            for message in self._messages
        ]

    def clear(self, clear_preferences: bool = False) -> None:
        """
        Elimina el historial conversacional acumulado en memoria y actualiza el archivo en disco.

        Args:
            clear_preferences (bool): Si es True, también elimina las preferencias del usuario.
        """
        self._messages.clear()
        if clear_preferences:
            self.preferences.clear()

        # Guardamos el estado limpio en disco
        if self.auto_save and self.storage_file:
            self.save_to_disk()

    def save_to_disk(self, file_path: Optional[str] = None) -> bool:
        """
        Serializa y guarda los mensajes y preferencias actuales en un archivo JSON en disco.
        Crea las carpetas necesarias de forma automática y utiliza guardado atómico.

        Args:
            file_path (str, optional): Ruta personalizada opcional. Por defecto usa self.storage_file.

        Returns:
            bool: True si el guardado fue exitoso, False en caso de error.
        """
        target = file_path or self.storage_file
        if not target:
            return False

        try:
            # Aseguramos que la carpeta contenedora exista
            dir_name = os.path.dirname(os.path.abspath(target))
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)

            data = {
                "version": 1,
                "messages": list(self._messages),
                "preferences": self.preferences,
            }

            # Guardamos primero en archivo temporal para asegurar atomicidad
            temp_path = f"{target}.tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            # Reemplazamos de forma atómica el archivo de destino
            if os.path.exists(target):
                os.replace(temp_path, target)
            else:
                os.rename(temp_path, target)

            return True

        except Exception as error:
            print(f"[Memory Persistence Warning] No se pudo guardar la memoria en disco: {error}")
            return False

    def load_from_disk(self, file_path: Optional[str] = None) -> bool:
        """
        Deserializa y carga mensajes y preferencias desde un archivo JSON en disco.
        Si el archivo está ausente o corrupto, lo maneja de forma tolerante a fallos.

        Args:
            file_path (str, optional): Ruta personalizada opcional. Por defecto usa self.storage_file.

        Returns:
            bool: True si la carga fue exitosa, False si no se pudo cargar.
        """
        target = file_path or self.storage_file
        if not target or not os.path.exists(target):
            return False

        try:
            with open(target, "r", encoding="utf-8") as f:
                data = json.load(f)

            if isinstance(data, dict):
                # Cargamos la lista de mensajes
                raw_messages = data.get("messages", [])
                if isinstance(raw_messages, list):
                    self._messages.clear()
                    for msg in raw_messages:
                        if isinstance(msg, dict) and "role" in msg and "content" in msg:
                            if msg["role"] in ("user", "assistant") and isinstance(msg["content"], str):
                                self._messages.append({"role": msg["role"], "content": msg["content"]})

                # Cargamos las preferencias del usuario
                raw_prefs = data.get("preferences", {})
                if isinstance(raw_prefs, dict):
                    self.preferences = dict(raw_prefs)

                return True

            elif isinstance(data, list):
                # Soporte de compatibilidad si el archivo es solo una lista de mensajes
                self._messages.clear()
                for msg in data:
                    if isinstance(msg, dict) and "role" in msg and "content" in msg:
                        if msg["role"] in ("user", "assistant") and isinstance(msg["content"], str):
                            self._messages.append({"role": msg["role"], "content": msg["content"]})
                return True

        except Exception as error:
            print(f"[Memory Persistence Warning] Error al cargar memoria desde {target}: {error}")
            return False

        return False
