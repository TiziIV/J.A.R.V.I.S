from src.utils.logger import logger
import os
import string
import time
import unicodedata
from concurrent.futures import Future
from src.audio.recorder import AudioRecorder
from src.audio.stt import SpeechToText
from src.audio.wake_word import WakeWordDetector
from src.brain.ollama_client import OllamaBrain
from src.brain.gemini_client import GeminiBrain
from src.brain.intent_router import IntentRouter, generate_with_fallback
from src.brain.memory import ConversationMemory
from config.tools_schema import TOOLS_SCHEMA
from config.wake_config import (
    WAKE_WORD_MODEL,
    WAKE_WORD_THRESHOLD,
    WAKE_SAMPLE_RATE,
    WAKE_CHUNK_SIZE,
)
from src.audio.tts import TextToSpeech
from src.tools.skill_manager import SkillManager
from src.ui.tray import JarvisTray

def normalize_text(text: str) -> str:
    """
    Función auxiliar para normalizar el texto transcrito por Whisper.
    ¿Por qué se hace? Convierte a minúsculas, elimina tildes (acentos) y remueve signos de puntuación
    para que la detección de comandos e intenciones sea robusta y tolerante a variaciones de voz.
    
    Args:
        text (str): Texto crudo proveniente del STT.
        
    Returns:
        str: Texto limpio y normalizado.
    """
    if not text:
        return ""
    
    # 1. Convertimos todo el texto a minúsculas
    text = text.lower()
    
    # 2. Descomponemos caracteres Unicode para separar las tildes de las letras base (NFKD)
    nfkd_form = unicodedata.normalize('NFKD', text)
    
    # 3. Filtramos los caracteres de combinación (tildes/diacríticos)
    without_accents = "".join([c for c in nfkd_form if not unicodedata.combining(c)])
    
    # 4. Removemos todos los signos de puntuación del sistema (incluyendo signos en español ¿, ¡)
    punctuation_to_remove = string.punctuation + "¿¡«»“”"
    clean_text = without_accents.translate(str.maketrans("", "", punctuation_to_remove))
    
    # 5. Unimos las palabras separadas por múltiples espacios en un único espacio estándar
    return " ".join(clean_text.split())

def main():
    """
    Script principal de J.A.R.V.I.S. con bucle de activación manos libres de dos estados:
    - Estado 1 (Pasivo/Reposo): Detección local en CPU de palabra de activación ("Hey Jarvis").
    - Estado 2 (Activo/Procesamiento): VAD inteligente -> Whisper GPU -> IntentRouter/Skills -> TTS.
    """
    logger.info("=== Inicializando Sistema J.A.R.V.I.S. ===")
    
    # ---------------------------------------------------------------------------
    # PASO 1: Inicialización única de componentes (evita recargar modelos en VRAM)
    # ---------------------------------------------------------------------------
    # Instanciamos el detector de Wake Word con el umbral configurado (openWakeWord en CPU)
    wake_detector = WakeWordDetector(threshold=WAKE_WORD_THRESHOLD)
    recorder = AudioRecorder(sample_rate=16000)    # Grabador con VAD inteligente (16 kHz mono)
    stt = SpeechToText()                           # Transcriptor faster-whisper en GPU
    brain = OllamaBrain()                          # Cerebro local principal (Ollama)
    gemini_brain = GeminiBrain()                   # Cerebro de respaldo en la nube (Gemini)
    tts = TextToSpeech()                             # Síntesis local offline con System.Speech
    skill_manager = SkillManager()                 # Gestor de habilidades y herramientas locales
    
    # Inicializamos la memoria conversacional con ventana deslizante de 8 turnos (16 mensajes)
    memory = ConversationMemory(max_turns=8)

    # Inicializamos la bandeja del sistema (System Tray) con callbacks para acciones rápidas
    def on_tray_clear_memory():
        memory.clear()
        logger.info("[Tray] Memoria conversacional reseteada por el usuario.")

    tray = JarvisTray(
        on_clear_memory=on_tray_clear_memory,
    )
    try:
        tray.start()
    except Exception as tray_error:
        logger.warning(f"No se pudo iniciar el System Tray: {tray_error}")
    
    # Instanciamos el IntentRouter con el LLM principal, el manifiesto de herramientas, memoria y fallback en la nube
    intent_router = IntentRouter(
        llm=brain,
        tools=skill_manager.get_skills_manifest(),
        memory=memory,
        fallback_llm=gemini_brain,
    )
    pending_speech: Future[None] | None = None     # Futuro para controlar la asincronía del TTS

    logger.info("Sistema J.A.R.V.I.S. activo y en modo escucha pasiva.")
    logger.info("Diga 'Hey Jarvis' para activar.")
    logger.info("Comandos de prueba: '¿Qué hora es?', 'Abre la calculadora', 'Estado del sistema', o 'Olvida lo que hablamos'.")

    # Lista de palabras clave para detectar comandos de salida del usuario
    exit_keywords = ["salir", "adios", "chau", "bye", "apagate", "cerrar", "terminar", "hasta luego"]

    try:
        # =========================================================================
        # BUCLE PRINCIPAL DE DOS ESTADOS
        # =========================================================================
        while True:
            # Verificamos si el usuario solicitó la salida desde la bandeja del sistema
            if tray.is_exit_requested:
                print("\n[+] Cierre ordenado solicitado desde System Tray. Finalizando...")
                farewell_msg = "Entendido señor. Apagando sistemas desde la bandeja del sistema."
                print(f"JARVIS: \"{farewell_msg}\"")
                tray.set_state("hablando")
                pending_speech = tts.speak(farewell_msg)
                if pending_speech is not None:
                    pending_speech.result()
                    pending_speech = None
                break

            # ---------------------------------------------------------------------
            # ESTADO 1 (Pasivo / Reposo):
            # Escucha en CPU con consumo ultra bajo de recursos esperando "Hey Jarvis".
            # ---------------------------------------------------------------------
            # Esperamos que cualquier locución previa termine antes de abrir el detector
            if pending_speech is not None:
                pending_speech.result()
                pending_speech = None

            # REGLA DE ORO ANTI-ECO: Esperar a que el sintetizador finalice completamente
            while tts.is_speaking:
                time.sleep(0.05)

            # Damos 0.5s para que el hardware y el sistema operativo vacíen el búfer de salida
            time.sleep(0.5)

            # Notificamos al icono del System Tray el retorno al estado reposo (azul)
            tray.set_state("reposo")

            print("-" * 50)
            print("[Modo Reposo] Esperando palabra de activación... Diga 'Hey Jarvis' para activar.")

            # Escucha continua en bloques de 80ms hasta detectar la palabra clave
            is_wake_detected = wake_detector.listen_for_wake_word()
            if not is_wake_detected:
                # Si la escucha terminó sin detección (ej. stop o timeout), continuamos en reposo
                continue

            # ---------------------------------------------------------------------
            # TRANSICIÓN AL ACTIVARSE:
            # Pitido suave de confirmación (sin locución repetitiva) + escucha inmediata.
            # ---------------------------------------------------------------------
            logger.info("[+] Wake Word detectada. Activando J.A.R.V.I.S...")
            # Emitimos un pitido breve de 120 ms a 900 Hz como acuse de recibo no hablado.
            # Usar winsound evita activar el motor TTS y arranca el micrófono mucho más rápido.
            try:
                import winsound
                winsound.Beep(900, 120)
            except Exception:
                pass  # En entornos sin audio o no-Windows se omite silenciosamente

            tray.set_state("escuchando")

            # ---------------------------------------------------------------------
            # ESTADO 2 (Activo / Procesamiento):
            # Grabamos la petición -> Transcribimos -> IntentRouter / LLM -> TTS
            # ---------------------------------------------------------------------
            audio_path = None  # Ruta del archivo temporal grabado

            try:
                print("[Micro Activo] Hable ahora, señor...")
                tray.set_state("escuchando")
                # REGLA DE ORO: Validar que no haya habla activa antes de abrir el stream del micrófono
                while tts.is_speaking:
                    time.sleep(0.05)

                # El grabador calibra el ambiente en 400ms y espera hasta 6s inicio y 2.5s de silencio
                audio_path = recorder.record_command(initial_timeout=6.0, silence_duration=2.5)

                # Si no hay ruta utilizable, evitamos llamar a Whisper con una grabación inexistente.
                if not audio_path or not os.path.isfile(audio_path):
                    print("[!] No se detectó voz; vuelvo al modo reposo.")
                    continue
                
                print("[Transcribiendo audio con Whisper GPU...]")
                tray.set_state("procesando")
                # b) Transcripción en GPU con faster-whisper
                user_transcript = stt.transcribe(audio_path)
                
                # c) Filtro de ruido o audio sin contenido verbal
                meaningful_characters = sum(character.isalnum() for character in user_transcript)
                if not user_transcript or not user_transcript.strip() or meaningful_characters < 2:
                    print("[!] No se detectó una orden comprensible. Volviendo a modo reposo...")
                    continue

                print(f"Usuario: \"{user_transcript}\"")
                
                # Normalizamos el texto (minúsculas, sin tildes, sin puntuación)
                norm_text = normalize_text(user_transcript)

                # d) Detección de comandos de apagado / salida
                is_exit_requested = any(keyword in norm_text for keyword in exit_keywords)
                if is_exit_requested:
                    farewell_msg = "Entendido señor. Apagando sistemas. Hasta luego."
                    print(f"JARVIS: \"{farewell_msg}\"")
                    pending_speech = tts.speak(farewell_msg)
                    if pending_speech is not None:
                        pending_speech.result()
                        pending_speech = None
                    break

                # e) Detección de comando para olvidar/reiniciar la memoria conversacional
                clear_memory_triggers = ["olvida lo que hablamos", "reiniciar contexto", "olvidar contexto", "olvida todo", "borrar memoria"]
                if any(trigger in norm_text for trigger in clear_memory_triggers):
                    memory.clear()
                    print(f"[DEBUG Memoria] Turnos almacenados: {len(memory.get_messages_list())}")
                    reset_msg = "Entendido señor. He reiniciado la memoria de nuestra conversación."
                    print(f"JARVIS: \"{reset_msg}\"")
                    pending_speech = tts.speak(reset_msg)
                    continue

                # Registramos el mensaje del usuario en la memoria antes del análisis
                memory.add_message(role="user", content=user_transcript)

                # f) Clasificación de Intenciones y Function Calling con IntentRouter
                logger.info("[Analizando intención con IntentRouter...]")
                intent_decision = intent_router.analyze_intent(user_transcript)
                action = intent_decision.get("action", "chat")
                
                jarvis_response = None

                # Si es llamada a herramienta, la ejecutamos mediante SkillManager
                if action == "call_tool":
                    tool_name = intent_decision.get("tool_name", "")
                    parameters = intent_decision.get("parameters", {})
                    logger.info(f"[Tool Call Detectado] Ejecutando: '{tool_name}' con parámetros: {parameters}")
                    jarvis_response = skill_manager.execute_skill(tool_name, **parameters)

                # Si es conversación o chat, procesamos la respuesta generada
                else:
                    chat_text = intent_decision.get("response", "")
                    
                    # Si el router no generó respuesta directa o devolvió la misma entrada, consultamos al cerebro LLM
                    if not chat_text or chat_text == user_transcript:
                        logger.info("[IntentRouter derivó a chat directo. Consultando cerebro LLM con contexto...]")
                        system_instruction = (
                            "Eres J.A.R.V.I.S., un asistente virtual. Recuerda los datos personales y preferencias que el usuario te comparta en la conversación. Si el usuario te pregunta por sus gustos o datos previos, responde usando la información guardada en el historial reciente."
                        )
                        system_prompt_with_ctx = memory.inject_context(system_instruction)
                        jarvis_response = brain.generate_response(
                            user_transcript,
                            system_prompt=system_prompt_with_ctx,
                        )
                    else:
                        jarvis_response = chat_text

                # Registramos la respuesta final del asistente en la memoria
                if jarvis_response:
                    memory.add_message(role="assistant", content=jarvis_response)
                    logger.info(f"[DEBUG Memoria] Turnos almacenados: {len(memory.get_messages_list())}")

                logger.info(f"JARVIS: \"{jarvis_response}\"")
                
                # REPRODUCCIÓN POR VOZ (TTS) GARANTIZADA DE FORMA BLOQUEANTE
                # Asegura al 100% que J.A.R.V.I.S. hable todas sus respuestas antes de continuar
                if jarvis_response and jarvis_response.strip():
                    tray.set_state("hablando")
                    pending_speech = tts.speak(jarvis_response)
                    if pending_speech is not None:
                        pending_speech.result()
                        pending_speech = None

                # REGLA DE ORO ANTI-ECO: Esperar a que el habla cese y purgar búfer del hardware
                while tts.is_speaking:
                    time.sleep(0.05)
                time.sleep(0.5)

            except Exception as iteration_error:
                print(f"[X] Error en el procesamiento del comando: {iteration_error}")
                
            finally:
                # Eliminación segura del archivo temporal de audio en Windows
                if audio_path and os.path.exists(audio_path):
                    try:
                        os.remove(audio_path)
                    except Exception as cleanup_error:
                        print(f"[!] No se pudo eliminar el archivo temporal: {cleanup_error}")

    except KeyboardInterrupt:
        print("\n\n[i] Interrupción de teclado detectada (Ctrl + C).")
        try:
            wake_detector.stop()
            tray.set_state("hablando")
            pending_farewell = tts.speak("Apagando sistemas por comando de usuario. Que tenga un excelente día.")
            if pending_farewell is not None:
                pending_farewell.result()
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
