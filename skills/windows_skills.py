"""
Habilidades avanzadas de control del sistema Windows para J.A.R.V.I.S.
Incluye: control de volumen, teclas multimedia, portapapeles y energía del sistema.
"""

import ctypes
import subprocess
import time
from typing import Any

from skills.base_skill import BaseSkill


class VolumeControlSkill(BaseSkill):
    """
    Habilidad para subir, bajar o silenciar el volumen maestro de Windows.
    Usa PowerShell con AudioDeviceCmdlets o nircmd como alternativa robusta.
    """

    def __init__(self) -> None:
        # Registramos la skill con nombre, descripción y parámetros esperados
        super().__init__(
            name="volume_control",
            description="Controla el volumen del sistema: subir, bajar o silenciar el audio maestro de Windows.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Acción a realizar: 'subir', 'bajar' o 'silenciar'."
                    }
                },
                "required": ["action"],
            },
        )

    def execute(self, **kwargs: Any) -> str:
        """Ejecuta el control de volumen mediante teclas multimedia de Windows."""
        raw_action = kwargs.get("action")
        action = raw_action.lower().strip() if isinstance(raw_action, str) else ""

        if not action:
            return "Por favor indique si desea subir, bajar o silenciar el volumen, señor."

        try:
            # Constantes de la API SendInput de Windows para teclas multimedia
            VK_VOLUME_UP = 0xAF
            VK_VOLUME_DOWN = 0xAE
            VK_VOLUME_MUTE = 0xAD
            KEYEVENTF_KEYUP = 0x0002

            # Seleccionamos la tecla virtual según la acción solicitada
            if action in ("subir", "aumentar", "mas", "up"):
                vk = VK_VOLUME_UP
                # Pulsamos varias veces para un cambio perceptible
                for _ in range(5):
                    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
                    ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
                    time.sleep(0.05)
                return "Volumen subido, señor."

            elif action in ("bajar", "reducir", "menos", "down"):
                vk = VK_VOLUME_DOWN
                for _ in range(5):
                    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
                    ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
                    time.sleep(0.05)
                return "Volumen bajado, señor."

            elif action in ("silenciar", "mute", "mutear", "silencio"):
                ctypes.windll.user32.keybd_event(VK_VOLUME_MUTE, 0, 0, 0)
                ctypes.windll.user32.keybd_event(VK_VOLUME_MUTE, 0, KEYEVENTF_KEYUP, 0)
                return "Volumen silenciado, señor."

            else:
                return f"No reconozco la acción '{action}'. Puedo subir, bajar o silenciar el volumen."

        except Exception as e:
            print(f"[VolumeControl Error] {e}")
            return "Lo siento señor, no pude controlar el volumen del sistema."


class MediaControlSkill(BaseSkill):
    """
    Habilidad para controlar la reproducción multimedia de Windows.
    Simula teclas multimedia (play/pause, next, previous) mediante la API de Windows.
    """

    def __init__(self) -> None:
        super().__init__(
            name="media_control",
            description="Controla la reproducción multimedia: pausar, reproducir, siguiente o anterior canción.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Acción multimedia: 'pausar', 'reproducir', 'siguiente' o 'anterior'."
                    }
                },
                "required": ["action"],
            },
        )

    def execute(self, **kwargs: Any) -> str:
        """Simula teclas multimedia de Windows para controlar la reproducción."""
        raw_action = kwargs.get("action")
        action = raw_action.lower().strip() if isinstance(raw_action, str) else ""

        if not action:
            return "Por favor indique qué acción multimedia desea: pausar, reproducir, siguiente o anterior."

        try:
            # Códigos de teclas virtuales multimedia de Windows
            VK_MEDIA_PLAY_PAUSE = 0xB3
            VK_MEDIA_NEXT_TRACK = 0xB0
            VK_MEDIA_PREV_TRACK = 0xB1
            KEYEVENTF_KEYUP = 0x0002

            if action in ("pausar", "pausa", "pause", "reproducir", "play", "reanudar"):
                vk = VK_MEDIA_PLAY_PAUSE
                label = "Reproducción alternada"
            elif action in ("siguiente", "next", "skip", "adelantar"):
                vk = VK_MEDIA_NEXT_TRACK
                label = "Siguiente canción"
            elif action in ("anterior", "previous", "prev", "atrasar"):
                vk = VK_MEDIA_PREV_TRACK
                label = "Canción anterior"
            else:
                return f"No reconozco la acción multimedia '{action}'. Use pausar, reproducir, siguiente o anterior."

            # Simulamos la pulsación y liberación de la tecla multimedia
            ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
            ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            return f"{label}, señor."

        except Exception as e:
            print(f"[MediaControl Error] {e}")
            return "Lo siento señor, no pude controlar la reproducción multimedia."


class ClipboardSkill(BaseSkill):
    """
    Habilidad para leer o escribir en el portapapeles de Windows.
    Usa ctypes para acceder a la API nativa del portapapeles sin dependencias externas.
    """

    def __init__(self) -> None:
        super().__init__(
            name="clipboard_manager",
            description="Lee el contenido actual del portapapeles o copia un texto dictado al portapapeles de Windows.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Acción: 'leer' para ver el contenido o 'copiar' para guardar texto."
                    },
                    "text": {
                        "type": "string",
                        "description": "Texto a copiar al portapapeles (solo cuando action es 'copiar')."
                    },
                },
                "required": ["action"],
            },
        )

    def execute(self, **kwargs: Any) -> str:
        """Lee o escribe en el portapapeles de Windows usando ctypes."""
        raw_action = kwargs.get("action")
        action = raw_action.lower().strip() if isinstance(raw_action, str) else ""
        raw_text = kwargs.get("text")
        text = raw_text.strip() if isinstance(raw_text, str) else ""

        if not action:
            return "Por favor indique si desea leer o copiar en el portapapeles, señor."

        try:
            if action in ("leer", "read", "ver", "mostrar"):
                return self._read_clipboard()
            elif action in ("copiar", "copy", "escribir", "guardar"):
                if not text or not text.strip():
                    return "No recibí texto para copiar al portapapeles, señor."
                return self._write_clipboard(text.strip())
            else:
                return f"No reconozco la acción '{action}'. Puedo leer o copiar en el portapapeles."

        except Exception as e:
            print(f"[Clipboard Error] {e}")
            return "Lo siento señor, no pude acceder al portapapeles del sistema."

    @staticmethod
    def _read_clipboard() -> str:
        """Lee texto del portapapeles usando la API nativa de Windows."""
        CF_UNICODETEXT = 13
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        if not user32.OpenClipboard(0):
            return "No pude abrir el portapapeles, señor."

        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return "El portapapeles está vacío o no contiene texto, señor."

            # Bloqueamos la memoria global para leer el contenido
            kernel32.GlobalLock.restype = ctypes.c_wchar_p
            data = kernel32.GlobalLock(handle)
            if data:
                # Truncamos a 200 caracteres para lectura por voz
                preview = data[:200] if len(data) > 200 else data
                kernel32.GlobalUnlock(handle)
                return f"En el portapapeles hay: {preview}"
            else:
                return "No pude leer el contenido del portapapeles, señor."
        finally:
            user32.CloseClipboard()

    @staticmethod
    def _write_clipboard(text: str) -> str:
        """Escribe texto al portapapeles usando la API nativa de Windows."""
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        if not user32.OpenClipboard(0):
            return "No pude abrir el portapapeles, señor."

        try:
            user32.EmptyClipboard()
            # Reservamos memoria global para el texto en formato wide-char
            encoded = text.encode("utf-16-le") + b"\x00\x00"
            h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
            if not h_mem:
                return "No pude reservar memoria para el portapapeles, señor."

            kernel32.GlobalLock.restype = ctypes.c_void_p
            ptr = kernel32.GlobalLock(h_mem)
            ctypes.memmove(ptr, encoded, len(encoded))
            kernel32.GlobalUnlock(h_mem)

            user32.SetClipboardData(CF_UNICODETEXT, h_mem)
            return "Texto copiado al portapapeles, señor."
        finally:
            user32.CloseClipboard()


class SystemPowerSkill(BaseSkill):
    """
    Habilidad para bloquear la pantalla de Windows o cancelar apagados programados.
    Usa la API LockWorkStation y el comando shutdown /a del sistema.
    """

    def __init__(self) -> None:
        super().__init__(
            name="system_power",
            description="Bloquea la pantalla de Windows o cancela un apagado programado del sistema.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Acción: 'bloquear' para bloquear pantalla o 'cancelar_apagado' para abortar un shutdown programado."
                    }
                },
                "required": ["action"],
            },
        )

    def execute(self, **kwargs: Any) -> str:
        """Ejecuta la acción de energía solicitada sobre el sistema Windows."""
        raw_action = kwargs.get("action")
        action = raw_action.lower().strip() if isinstance(raw_action, str) else ""

        if not action:
            return "Por favor indique si desea bloquear la pantalla o cancelar un apagado, señor."

        try:
            if action in ("bloquear", "lock", "bloqueo", "bloquear pantalla"):
                # Llamamos a LockWorkStation de la API user32 de Windows
                result = ctypes.windll.user32.LockWorkStation()
                if result:
                    return "Pantalla bloqueada, señor."
                else:
                    return "No pude bloquear la pantalla. Puede requerir privilegios elevados, señor."

            elif action in ("cancelar_apagado", "cancelar apagado", "cancel_shutdown", "abort"):
                # Ejecutamos shutdown /a para abortar cualquier apagado programado
                proc = subprocess.run(
                    ["shutdown", "/a"],
                    capture_output=True,
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
                )
                if proc.returncode == 0:
                    return "Apagado programado cancelado exitosamente, señor."
                else:
                    return "No hay ningún apagado programado activo para cancelar, señor."

            else:
                return f"No reconozco la acción '{action}'. Puedo bloquear la pantalla o cancelar un apagado."

        except Exception as e:
            print(f"[SystemPower Error] {e}")
            return "Lo siento señor, no pude ejecutar la acción de energía solicitada."
