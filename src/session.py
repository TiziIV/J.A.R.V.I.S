"""
Sesión conversacional continua de J.A.R.V.I.S. ("Continuous Active Session").

Tras la palabra de activación, la sesión escucha, responde y vuelve a escuchar sin
límite de turnos. Solo regresa a Modo Reposo por:
  a) silencio total durante el plazo de inactividad (el grabador devuelve None), o
  b) un comando explícito de descanso ("descansa", "modo reposo", "eso es todo"...).
También termina si se pide apagar el asistente o si el micrófono falla repetidamente.

La clase no conoce micrófono, Whisper, LLM ni TTS: recibe funciones inyectadas desde
`main.py`, lo que permite probar el flujo completo sin hardware.
"""

from __future__ import annotations

import re
import string
import time
import unicodedata
from enum import Enum
from typing import Callable, Optional, Protocol


class SessionEnd(str, Enum):
    """Motivo por el que terminó una sesión activa."""
    SILENCE = "silence"   # Inactividad: nadie habló dentro del plazo.
    STANDBY = "standby"   # Comando de descanso explícito.
    EXIT = "exit"         # Apagado completo del asistente.
    NOISE = "noise"       # Capturas repetidas sin palabras comprensibles.
    ERROR = "error"       # Fallos consecutivos de periférico/proceso.
    ABORT = "abort"       # Cierre externo (p. ej. bandeja del sistema).


# Frases de descanso: se buscan con límites de palabra (\b) sobre texto normalizado.
# Si el usuario dice "jarvis descansa un rato", la presencia de "descansa" activa el reposo.
STANDBY_PHRASES: tuple[str, ...] = (
    "descansa", "modo reposo", "modo espera", "eso es todo", "eso seria todo",
    "es todo por ahora", "es todo por hoy", "gracias es todo", "jarvis es todo",
    "espera en segundo plano", "quedate en segundo plano", "quedate en espera",
    "quedate a la espera", "a dormir", "puedes retirarte", "retirate",
)
# Si la frase completa (quitando "jarvis" o muletillas de cortesía) es exactamente una de estas,
# se interpreta como orden de descanso. Evita que un simple "muchas gracias por la ayuda"
# duerma al asistente por error.
STANDBY_EXACT: tuple[str, ...] = (
    "es todo", "gracias", "listo gracias", "nada mas", "nada mas gracias", "gracias es todo",
)

# Apagado completo: frases inequívocas que pueden aparecer en cualquier parte del texto...
EXIT_PHRASES: tuple[str, ...] = (
    "apagate", "apaga los sistemas", "apagar sistemas", "apaga sistemas", "apagar el sistema",
    "cierra jarvis", "cerrar jarvis", "terminar programa", "desconectate",
)
# ...y palabras de salida que SOLO apagan si son la orden completa.
# Ejemplo: "salir" apaga J.A.R.V.I.S., pero "quiero salir a correr mañana" continúa en conversación.
EXIT_EXACT: tuple[str, ...] = ("salir", "adios", "chau", "bye", "hasta luego", "terminar", "cerrar")

# Respuestas rotativas para dar variedad cinematográfica al entrar en reposo
STANDBY_REPLIES: tuple[str, ...] = (
    "En espera, señor.",
    "Quedo a la escucha, señor.",
    "Pasando a segundo plano, señor.",
)
EXIT_REPLY: str = "Entendido, señor. Apagando sistemas."


def normalize_text(text: str) -> str:
    """
    Normaliza el texto a minúsculas, sin tildes ni signos de puntuación.
    
    ¿Por qué hacemos esto?
    Whisper transcribe con ligeras variaciones según entonación y ruido (a veces "adiós",
    a veces "adios", o con comas intermedias). Usamos la descomposición NFKD de Unicode
    para separar las letras base de sus marcas diacríticas (acentos) y filtrarlas.
    Así, una comparación simple '==' o regex funciona con total certeza.
    """
    if not text:
        return ""
    # NFKD descompone caracteres compuestos: 'ó' -> 'o' + '\u0301' (acento combinable)
    nfkd = unicodedata.normalize("NFKD", text.lower())
    # Filtramos las marcas combinables (los acentos descompuestos)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Eliminamos puntuación estándar y signos tipográficos de apertura en español (¿ ¡ « »)
    clean = no_accents.translate(str.maketrans("", "", string.punctuation + "¿¡«»“”"))
    # Reensamblamos para colapsar múltiples espacios contiguos en uno solo
    return " ".join(clean.split())


def _contains_phrase(norm_text: str, phrases: tuple[str, ...]) -> bool:
    """
    Verifica si alguna de las frases está presente como palabra completa (\b).
    Evita falsos positivos como detectar 'descansa' dentro de una palabra compuesta desconocida.
    """
    return any(re.search(rf"\b{re.escape(p)}\b", norm_text) for p in phrases)


def _core_command(norm_text: str) -> str:
    """
    Elimina vocativos y fórmulas de cortesía para evaluar la orden nuclear del usuario.
    Ejemplo didáctico: 'jarvis, por favor salir' -> se limpia a 'salir', coincidiendo con EXIT_EXACT.
    """
    words = [w for w in norm_text.split() if w not in ("jarvis", "hey", "oye", "por", "favor", "senor")]
    return " ".join(words)


def is_standby_command(text: str) -> bool:
    """Determina si el usuario solicitó pasar a modo reposo/espera."""
    norm = normalize_text(text)
    return _contains_phrase(norm, STANDBY_PHRASES) or _core_command(norm) in STANDBY_EXACT


def is_exit_command(text: str) -> bool:
    """Determina si el usuario ordenó apagar completamente la aplicación."""
    norm = normalize_text(text)
    return _contains_phrase(norm, EXIT_PHRASES) or _core_command(norm) in EXIT_EXACT


def is_meaningful(text: Optional[str]) -> bool:
    """
    Determina si la transcripción contiene información verbal útil.
    
    ¿Por qué 2 caracteres alfanuméricos?
    1. Descarta alucinaciones comunes de Whisper en silencios (ej. '.', '?', '-', 'uh').
    2. Acepta respuestas cortas esenciales en español como 'sí', 'no', 'ya' u 'ok'.
    """
    return bool(text) and sum(c.isalnum() for c in text) >= 2


class SoundEffects(Protocol):
    def standby(self) -> None: ...


class ActiveSession:
    """
    Bucle de conversación activa sin límite de mensajes.

    Args:
        listen: graba y transcribe una frase. Devuelve None si no hubo voz en el plazo de
            inactividad, "" (o texto sin contenido) si hubo sonido incomprensible.
        respond: procesa una orden válida y devuelve el texto a locutar (o None).
        speak: locuta un texto de forma BLOQUEANTE (anti-eco).
        sfx: efectos HUD opcionales (se usa `standby()` al dormir).
        set_state: callback opcional para la bandeja ("escuchando", "procesando", ...).
        should_abort: consultado antes de cada escucha para cierres externos.
        post_speech_pause: pausa tras hablar para vaciar el búfer de salida antes de escuchar.
        max_unintelligible: capturas incomprensibles seguidas toleradas antes de dormir.
        max_errors: errores consecutivos tolerados antes de dormir.
    """

    def __init__(
        self,
        listen: Callable[[], Optional[str]],
        respond: Callable[[str], Optional[str]],
        speak: Callable[[str], None],
        sfx: Optional[SoundEffects] = None,
        set_state: Optional[Callable[[str], None]] = None,
        should_abort: Optional[Callable[[], bool]] = None,
        post_speech_pause: float = 0.35,
        max_unintelligible: int = 2,
        max_errors: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._listen = listen
        self._respond = respond
        self._speak = speak
        self._sfx = sfx
        self._set_state = set_state or (lambda _state: None)
        self._should_abort = should_abort or (lambda: False)
        self.post_speech_pause = post_speech_pause
        self.max_unintelligible = max(1, max_unintelligible)
        self.max_errors = max(1, max_errors)
        self._sleep = sleep
        self.turn_count: int = 0  # Métrica informativa: NO es un límite.
        self._standby_index: int = 0

    def _say(self, text: Optional[str]) -> None:
        """
        Locución segura con anti-eco acústico.
        
        Flujo de ejecución:
        1. Actualiza el estado visual del tray/HUD a 'hablando'.
        2. Llama a 'self._speak(text)' que bloquea el hilo hasta terminar de reproducir el audio.
        3. Realiza una pausa breve ('post_speech_pause' = 0.35s): esto permite que la reverberación
           en la habitación y el búfer del altavoz se extingan por completo antes de reabrir
           el micrófono en el siguiente turno, evitando que J.A.R.V.I.S. se escuche a sí mismo.
        """
        if text and text.strip():
            self._set_state("hablando")
            self._speak(text)
            self._sleep(self.post_speech_pause)

    def _go_standby(self, reply: Optional[str]) -> None:
        """
        Transición ordenada a Modo Reposo.
        
        Locuta la despedida si corresponde y reproduce el tono descendente SFX
        sin bloquear el flujo si el subsistema de audio tuviese un micro-retraso.
        """
        if reply:
            self._say(reply)
        if self._sfx is not None:
            try:
                self._sfx.standby()
            except Exception:
                pass

    def _next_standby_reply(self) -> str:
        """Obtiene la siguiente respuesta de descanso rotativa para variar el diálogo."""
        reply = STANDBY_REPLIES[self._standby_index % len(STANDBY_REPLIES)]
        self._standby_index += 1
        return reply

    def run(self) -> SessionEnd:
        """
        Mantiene la conversación activa continua sin límite de mensajes.
        
        Ciclo de vida del bucle:
        1. Consulta cancelación externa (ej. si el usuario cerró el icono de la bandeja de Windows).
        2. Cambia estado a 'escuchando' y graba con VAD + timeout de inactividad (SESSION_IDLE_TIMEOUT).
        3. Si transcript es None -> Nadie habló en 7s -> Transiciona suavemente a Reposo (SILENCE).
        4. Si es ininteligible -> Incrementa contador de ruido; si persiste, duerme (NOISE).
        5. Si coincide con comando de salida -> Locuta despedida y cierra la app (EXIT).
        6. Si coincide con comando de descanso -> Responde elegancia Stark y duerme (STANDBY).
        7. En cualquier otro caso:
           - Incrementa 'turn_count' (métrica de sesión).
           - Responde vía LLM/Skill ('procesando').
           - Habla la respuesta con TTS bloqueante ('hablando').
           - Continúa inmediatamente a la siguiente escucha SIN pedir 'Hey Jarvis' de nuevo.
        """
        unintelligible = 0
        errors = 0
        self.turn_count = 0

        while True:
            # 1. Comprobación de cierre preventivo
            if self._should_abort():
                return SessionEnd.ABORT

            # 2. Captura de audio y transcripción por Whisper
            self._set_state("escuchando")
            try:
                transcript = self._listen()
            except Exception as error:
                errors += 1
                print(f"[Sesión] Error al escuchar ({errors}/{self.max_errors}): {error}")
                if errors >= self.max_errors:
                    self._go_standby("Tengo problemas con el micrófono, señor. Paso a reposo.")
                    return SessionEnd.ERROR
                self._sleep(self.post_speech_pause)
                continue
            errors = 0

            # 3. Timeout por inactividad del usuario (7 segundos de silencio)
            if transcript is None:
                print("[Sesión] Inactividad detectada; vuelvo a Modo Reposo.")
                self._go_standby(None)
                return SessionEnd.SILENCE

            # 4. Filtrado de ruido o chasquidos sin contenido lingüístico
            if not is_meaningful(transcript):
                unintelligible += 1
                print(f"[Sesión] Audio sin orden comprensible ({unintelligible}/{self.max_unintelligible}).")
                if unintelligible >= self.max_unintelligible:
                    self._go_standby(None)
                    return SessionEnd.NOISE
                continue
            unintelligible = 0

            print(f"Usuario: \"{transcript}\"")

            # 5. Comandos de apagado
            if is_exit_command(transcript):
                self._say(EXIT_REPLY)
                return SessionEnd.EXIT

            # 6. Comandos de descanso explícito
            if is_standby_command(transcript):
                self._go_standby(self._next_standby_reply())
                return SessionEnd.STANDBY

            # 7. Procesamiento normal de intenciones y ejecución de skills
            self.turn_count += 1
            self._set_state("procesando")
            try:
                reply = self._respond(transcript)
            except Exception as error:
                print(f"[Sesión] Error procesando la orden: {error}")
                reply = "Disculpe, señor, ese proceso ha fallado. ¿Lo intentamos de nuevo?"

            print(f"JARVIS: \"{reply}\"")
            try:
                self._say(reply)
            except Exception as error:
                print(f"[Sesión] Error al locutar: {error}")
            # Nótese: NO hay condición de salida por turn_count. El bucle continúa de inmediato.
