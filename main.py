import os
import string
import unicodedata
from concurrent.futures import Future
from src.audio.recorder import AudioRecorder
from src.audio.stt import SpeechToText
from src.brain.ollama_client import OllamaBrain
from src.brain.gemini_client import GeminiBrain
from src.brain.intent_router import IntentRouter
from config.tools_schema import TOOLS_SCHEMA
from src.audio.tts import TextToSpeech
from src.tools.skill_manager import SkillManager

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
    Script principal de J.A.R.V.I.S. con bucle continuo, normalización léxica avanzada,
    motor de habilidades (SkillManager) y respaldo de cerebros (Ollama + Gemini).
    """
    print("=== Inicializando Sistema J.A.R.V.I.S. (Fase 5 - Function Calling con IntentRouter) ===")
    
    # ---------------------------------------------------------------------------
    # PASO 1: Inicialización única de componentes (evita recargar modelos en VRAM)
    # ---------------------------------------------------------------------------
    recorder = AudioRecorder(sample_rate=16000)  # Grabador con VAD inteligente
    stt = SpeechToText()                         # faster-whisper en GPU
    brain = OllamaBrain()                        # Cerebro local principal (Ollama)
    gemini_brain = GeminiBrain()                 # Cerebro de respaldo en la nube (Gemini)
    tts = TextToSpeech(voice="es-AR-TomasNeural")  # Sintetizador de voz neuronal
    skill_manager = SkillManager()               # Gestor de habilidades y herramientas locales
    
    # Instanciamos el IntentRouter con el LLM principal y el manifiesto de herramientas de SkillManager
    intent_router = IntentRouter(llm=brain, tools=skill_manager.get_skills_manifest())
    pending_speech: Future[None] | None = None   # Futuro para controlar la asincronía del TTS

    print("\n[+] Sistema J.A.R.V.I.S. activo y escuchando.")
    print("[i] Pruebe decir: '¿Qué hora es?', 'Estado del sistema', 'Busca en YouTube música...', o 'Abrí la calculadora'.\n")

    # Lista de palabras clave para detectar comandos de salida del usuario
    exit_keywords = ["salir", "adios", "chau", "bye", "apagate", "cerrar", "terminar", "hasta luego"]

    try:
        # Bucle principal de conversación continua (while True)
        while True:
            audio_path = None  # Ruta del archivo temporal de audio
            
            try:
                # Esperamos que termine la voz anterior antes de abrir el micrófono para evitar eco.
                if pending_speech is not None:
                    pending_speech.result()
                    pending_speech = None

                print("-" * 50)
                
                # a) Capturamos audio de manera inteligente con Detección de Actividad de Voz (VAD)
                audio_path = recorder.record_until_silence()
                
                print("[Transcribiendo audio con Whisper GPU...]")
                # b) Transcribimos el audio a texto con Whisper
                user_transcript = stt.transcribe(audio_path)
                
                # c) Detección de silencio o audio vacío (filtro de ruido residual)
                meaningful_characters = sum(character.isalnum() for character in user_transcript)
                if not user_transcript or not user_transcript.strip() or meaningful_characters < 2:
                    print("[!] No se obtuvo una transcripción confiable. Continuando escucha...")
                    continue

                print(f"Usuario: \"{user_transcript}\"")
                
                # Normalizamos el texto de manera avanzada (sin tildes, sin puntuación, minúsculas)
                norm_text = normalize_text(user_transcript)
                words = set(norm_text.split())

                # d) Detección flexible de comandos de salida
                is_exit_requested = any(keyword in norm_text for keyword in exit_keywords)
                
                if is_exit_requested:
                    farewell_msg = "Entendido señor. Apagando sistemas. Hasta luego."
                    print(f"JARVIS: \"{farewell_msg}\"")
                    # Reproducimos la despedida por voz y rompemos el bucle
                    pending_speech = tts.speak(farewell_msg)
                    break

                # -----------------------------------------------------------------
                # PASO 2: Clasificación de Intenciones y Function Calling (IntentRouter)
                # Reemplaza la detección manual por análisis autónomo de LLM en formato JSON.
                # -----------------------------------------------------------------
                print("[Analizando intención con IntentRouter...]")
                intent_decision = intent_router.analyze_intent(user_transcript)
                action = intent_decision.get("action", "chat")
                
                jarvis_response = None

                # Si la acción es 'call_tool', ejecutamos la herramienta a través de SkillManager
                if action == "call_tool":
                    tool_name = intent_decision.get("tool_name", "")
                    parameters = intent_decision.get("parameters", {})
                    print(f"[Tool Call Detectado] Ejecutando: '{tool_name}' con parámetros: {parameters}")
                    
                    jarvis_response = skill_manager.execute_skill(tool_name, **parameters)

                # Si la acción es 'chat', reproducimos directamente la respuesta generada
                else:
                    chat_text = intent_decision.get("response", "")
                    
                    # Si el router devolvió el texto crudo del usuario como fallback seguro de parsing,
                    # consultamos directamente a Ollama con respaldo en Gemini para asegurar respuesta fluida
                    if not chat_text or chat_text == user_transcript:
                        print("[IntentRouter derivó a chat directo. Consultando cerebro LLM...]")
                        try:
                            chat_text = brain.generate_response(user_transcript)
                            if not chat_text or "no responde" in chat_text.lower() or "error" in chat_text.lower():
                                raise Exception("Ollama local no disponible o respuesta inválida.")
                        except Exception as ollama_error:
                            print(f"[!] Fallo en Ollama local ({ollama_error}). Derivando a GeminiBrain (Cloud Fallback)...")
                            try:
                                print("[Consultando al cerebro en la nube (Gemini)...]")
                                chat_text = gemini_brain.generate_response(user_transcript)
                            except Exception as gemini_error:
                                print(f"[X] Fallo también en GeminiBrain: {gemini_error}")
                                chat_text = "Lo siento señor, ninguno de mis cerebros responde en este momento."

                    jarvis_response = chat_text

                print(f"JARVIS: \"{jarvis_response}\"")
                
                print("[Reproduciendo respuesta por voz (TTS)...]")
                pending_speech = tts.speak(jarvis_response)

            except Exception as iteration_error:
                print(f"[X] Error en esta iteración del bucle: {iteration_error}")
                
            finally:
                if audio_path and os.path.exists(audio_path):
                    try:
                        os.remove(audio_path)
                    except Exception as cleanup_error:
                        print(f"[!] No se pudo eliminar el archivo temporal: {cleanup_error}")

    except KeyboardInterrupt:
        print("\n\n[i] Interrupción de teclado detectada (Ctrl + C).")
        try:
            tts.speak("Apagando sistemas por comando de usuario. Que tenga un excelente día.")
        except Exception:
            pass

    finally:
        tts.close()

    print("\n=== Sesión de J.A.R.V.I.S. finalizada correctamente ===")

if __name__ == "__main__":
    main()
