"""
Módulo Principal de J.A.R.V.I.S.
Controla el ciclo de vida del asistente mediante una arquitectura de dos niveles:
- Nivel 1 (Modo Reposo / Standby): Detección local en CPU de palabra de activación ("Hey Jarvis").
- Nivel 2 (Sesión Activa Continua): Bucle fluido sin límite de mensajes mientras el usuario responda,
  con retorno a reposo ante inactividad (7s) o comando explícito de descanso.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import Future

from config.prompts import JARVIS_SYSTEM_PROMPT
from config.settings import (
    SESSION_IDLE_TIMEOUT,
    SESSION_POST_SPEECH_PAUSE,
    SESSION_SILENCE_DURATION,
    SFX_ENABLED,
)
from config.wake_config import (
    WAKE_CHUNK_SIZE,
    WAKE_SAMPLE_RATE,
    WAKE_WORD_MODEL,
    WAKE_WORD_THRESHOLD,
)
from src.audio.recorder import AudioRecorder
from src.audio.sfx import SoundFX
from src.audio.stt import SpeechToText
from src.audio.tts import TextToSpeech
from src.audio.wake_word import WakeWordDetector
from src.brain.gemini_client import GeminiBrain
from src.brain.intent_router import IntentRouter
from src.brain.memory import ConversationMemory
from src.brain.ollama_client import OllamaBrain
from src.session import ActiveSession, SessionEnd, normalize_text
from src.tools.skill_manager import SkillManager
from src.ui.tray import JarvisTray
from src.utils.logger import logger


def main() -> None:
    """
    Punto de entrada principal de J.A.R.V.I.S.
    """
    logger.info("=== Inicializando Sistema J.A.R.V.I.S. ===")

    # ---------------------------------------------------------------------------
    # PASO 1: Inicialización única de componentes persistentes
    # ---------------------------------------------------------------------------
    wake_detector = WakeWordDetector(threshold=WAKE_WORD_THRESHOLD)
    recorder = AudioRecorder(sample_rate=16000)
    stt = SpeechToText()
    brain = OllamaBrain()
    gemini_brain = GeminiBrain()
    tts = TextToSpeech()
    skill_manager = SkillManager()
    sfx = SoundFX(enabled=SFX_ENABLED)
    memory = ConversationMemory(max_turns=8)

    # System Tray reactivo con callback de limpieza de memoria
    def on_tray_clear_memory():
        memory.clear()
        logger.info("[Tray] Memoria conversacional reseteada por el usuario.")

    tray = JarvisTray(on_clear_memory=on_tray_clear_memory)
    try:
        tray.start()
    except Exception as tray_error:
        logger.warning(f"No se pudo iniciar el System Tray: {tray_error}")

    intent_router = IntentRouter(
        llm=brain,
        tools=skill_manager.get_skills_manifest(),
        memory=memory,
        fallback_llm=gemini_brain,
    )

    logger.info("Sistema J.A.R.V.I.S. en línea y en espera pasiva.")
    logger.info("Diga 'Hey Jarvis' una sola vez para iniciar una conversación continua.")
    logger.info("Protocolos disponibles: 'Protocolo de trabajo', 'Protocolo diagnóstico', 'Protocolo silencio'.")

    def speak_blocking(text: str) -> None:
        """
        Locución por voz garantizada y síncrona con control anti-eco estricto.
        
        ¿Por qué bloqueante?
        Si la locución corriera en segundo plano mientras el micrófono ya está grabando,
        el micrófono capturaría las propias palabras de J.A.R.V.I.S. saliendo por los altavoces
        (bucle de eco acústico). Al esperar con 'pending.result()' y chequear 'tts.is_speaking',
        aseguramos que el asistente termine completamente de hablar antes de volver a escuchar.
        """
        if not text or not text.strip():
            return
        pending = tts.speak(text)
        if pending is not None:
            pending.result()
        while tts.is_speaking:
            time.sleep(0.04)

    def listen_step() -> str | None:
        """
        Captura audio del usuario respetando el timeout de inactividad de la sesión.
        
        Retorna:
            str: Texto transcrito por Whisper si el usuario habló.
            None: Si transcurrieron los 7 segundos de 'SESSION_IDLE_TIMEOUT' en silencio absoluto.
        """
        while tts.is_speaking:
            time.sleep(0.04)

        # Grabamos con VAD (Voice Activity Detection): espera voz inicial hasta 7s
        audio_path = recorder.record_command(
            initial_timeout=SESSION_IDLE_TIMEOUT,
            silence_duration=SESSION_SILENCE_DURATION,
        )
        if not audio_path or not os.path.isfile(audio_path):
            return None

        try:
            transcript = stt.transcribe(audio_path)
            return transcript
        finally:
            # Limpieza inmediata y segura del archivo temporal WAV en disco
            if os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except Exception as err:
                    logger.debug(f"Aviso eliminando temporal: {err}")

    def respond_step(user_transcript: str) -> str:
        """
        Procesa una orden dentro de la sesión activa y retorna la respuesta para TTS.
        
        Flujo didáctico:
        1. Normaliza texto y verifica comandos de reseteo de memoria ("olvida todo", "borrar memoria").
        2. Registra el mensaje del usuario en el historial conversacional (ConversationMemory).
        3. Consulta a 'IntentRouter': clasifica si la orden es una Skill del sistema o Chat general.
        4. Si es Skill: reproduce beep corto SFX y ejecuta la habilidad (ej. hora, protocolos Stark).
        5. Si es Chat: responde con la personalidad Stark definida en JARVIS_SYSTEM_PROMPT.
        6. Guarda la respuesta en memoria para mantener coherencia en turnos siguientes.
        """
        norm = normalize_text(user_transcript)

        # Triggers de reinicio de contexto conversacional
        clear_memory_triggers = (
            "olvida lo que hablamos", "reiniciar contexto", "olvidar contexto",
            "olvida todo", "borrar memoria", "reinicia memoria",
        )
        if any(trigger in norm for trigger in clear_memory_triggers):
            memory.clear()
            sfx.confirm()
            return "Entendido, señor. He reiniciado la memoria de nuestra conversación."

        # Registrar el mensaje del usuario en memoria
        memory.add_message(role="user", content=user_transcript)

        # Analizar intención mediante IntentRouter
        intent_decision = intent_router.analyze_intent(user_transcript)
        action = intent_decision.get("action", "chat")

        jarvis_response: str | None = None

        if action == "call_tool":
            tool_name = intent_decision.get("tool_name", "")
            parameters = intent_decision.get("parameters", {})
            logger.info(f"[Tool Call] Ejecutando: '{tool_name}' con parámetros: {parameters}")
            sfx.confirm()
            jarvis_response = skill_manager.execute_skill(tool_name, **parameters)
        else:
            chat_text = intent_decision.get("response", "")
            if not chat_text or chat_text == user_transcript:
                logger.info("[IntentRouter] Derivando a chat con contexto histórico...")
                prompt_with_ctx = memory.inject_context(JARVIS_SYSTEM_PROMPT)
                jarvis_response = brain.generate_response(user_transcript, system_prompt=prompt_with_ctx)
            else:
                jarvis_response = chat_text

        if jarvis_response:
            memory.add_message(role="assistant", content=jarvis_response)

        return jarvis_response or "A su disposición, señor."

    try:
        # =====================================================================
        # BUCLE PRINCIPAL DE DOS NIVELES
        # =====================================================================
        while True:
            if tray.is_exit_requested:
                print("\n[+] Cierre ordenado solicitado desde la bandeja del sistema.")
                tray.set_state("hablando")
                speak_blocking("Entendido señor. Apagando sistemas desde la bandeja del sistema.")
                break

            # -----------------------------------------------------------------
            # NIVEL 1: Modo Reposo (Escucha ultra-ligera en CPU para Wake Word)
            # -----------------------------------------------------------------
            while tts.is_speaking:
                time.sleep(0.05)
            time.sleep(0.35)

            tray.set_state("reposo")
            print("-" * 55)
            print("[Modo Reposo] En espera pasiva. Diga 'Hey Jarvis' para activar.")

            is_wake_detected = wake_detector.listen_for_wake_word()
            if not is_wake_detected:
                continue

            # -----------------------------------------------------------------
            # TRANSICIÓN DE DESPERTAR: Sonido futurista HUD (sin decir '¿Sí, señor?')
            # -----------------------------------------------------------------
            logger.info("[+] Wake Word detectada. Activando sesión continua...")
            sfx.wake()
            tray.set_state("escuchando")
            print("[Sesión Activa] J.A.R.V.I.S. en línea y escuchando continuamente...")

            # -----------------------------------------------------------------
            # NIVEL 2: Bucle de Sesión Activa Continua (sin límite de 3 mensajes)
            # -----------------------------------------------------------------
            session = ActiveSession(
                listen=listen_step,
                respond=respond_step,
                speak=speak_blocking,
                sfx=sfx,
                set_state=tray.set_state,
                should_abort=lambda: tray.is_exit_requested,
                post_speech_pause=SESSION_POST_SPEECH_PAUSE,
            )
            session_result = session.run()

            if session_result == SessionEnd.EXIT:
                logger.info("[Sesión] Salida solicitada por voz.")
                break
            elif session_result == SessionEnd.ABORT:
                logger.info("[Sesión] Cierre abortado por la bandeja del sistema.")
                break
            else:
                logger.info(f"[Sesión] Fin de sesión continua ({session_result.value}). Volviendo a Modo Reposo.")

    except KeyboardInterrupt:
        print("\n\n[i] Interrupción de teclado detectada (Ctrl + C).")
        try:
            wake_detector.stop()
            tray.set_state("hablando")
            speak_blocking("Apagando sistemas por comando de usuario. Que tenga un buen día, señor.")
        except Exception:
            pass

    finally:
        wake_detector.stop()
        tts.close()
        try:
            tray.stop()
        except Exception:
            pass

    print("\n=== Sesión de J.A.R.V.I.S. finalizada correctamente ===")


if __name__ == "__main__":
    main()
