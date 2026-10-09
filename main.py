import os
import string
from concurrent.futures import Future
from src.audio.recorder import AudioRecorder
from src.audio.stt import SpeechToText
from src.brain.ollama_client import OllamaBrain
from src.brain.gemini_client import GeminiBrain
from src.audio.tts import TextToSpeech

def main():
    """
    Script principal de J.A.R.V.I.S. con bucle continuo, detección flexible de salida,
    cerebro local (Ollama) y cerebro de respaldo en la nube (Gemini).
    """
    print("=== Inicializando Sistema J.A.R.V.I.S. (Fase 2 - Multi-Cerebro & Fallback) ===")
    
    # ---------------------------------------------------------------------------
    # PASO 1: Inicialización única de componentes (evita recargar modelos en VRAM)
    # ---------------------------------------------------------------------------
    recorder = AudioRecorder(sample_rate=16000)  # Grabador de audio a 16kHz
    stt = SpeechToText()                         # faster-whisper en GPU
    brain = OllamaBrain()                        # Cerebro local principal (Ollama)
    gemini_brain = GeminiBrain()                 # Cerebro de respaldo en la nube (Gemini)
    tts = TextToSpeech(voice="es-AR-TomasNeural")  # Sintetizador de voz neuronal
    pending_speech: Future[None] | None = None

    print("\n[+] Sistema J.A.R.V.I.S. activo y escuchando.")
    print("[i] Diga 'salir', 'adiós', 'chau', 'apágate', etc. para terminar o presione Ctrl + C.\n")

    # Lista ampliada de palabras clave para detectar comandos de salida del usuario
    exit_keywords = ["salir", "adiós", "adios", "chau", "bye", "apágate", "apagate", "cerrar", "terminar", "hasta luego"]

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
                
                # c) Detección de silencio o audio vacío
                # Ignoramos residuos muy breves, pero permitimos respuestas cortas como "sí" o "no".
                meaningful_characters = sum(character.isalnum() for character in user_transcript)
                if not user_transcript or not user_transcript.strip() or meaningful_characters < 2:
                    print("[!] No se obtuvo una transcripción confiable. Continuando escucha...")
                    continue

                print(f"Usuario: \"{user_transcript}\"")
                
                # Limpiamos signos de puntuación y pasamos a minúsculas para una detección de salida robusta
                clean_text = user_transcript.lower().translate(str.maketrans("", "", string.punctuation)).strip()

                # d) Detección flexible de comandos de salida
                is_exit_requested = any(keyword in clean_text for keyword in exit_keywords)
                
                if is_exit_requested:
                    farewell_msg = "Entendido señor. Apagando sistemas. Hasta luego."
                    print(f"JARVIS: \"{farewell_msg}\"")
                    # Reproducimos la despedida por voz y rompemos el bucle
                    pending_speech = tts.speak(farewell_msg)
                    break

                # e) Inferencia con Ollama (local) y fallback automático a Gemini (nube)
                jarvis_response = None
                
                try:
                    print("[Consultando al cerebro local (Ollama)...]")
                    jarvis_response = brain.generate_response(user_transcript)
                    
                    # Verificamos si Ollama devolvió un mensaje de error interno
                    if not jarvis_response or "no responde" in jarvis_response.lower() or "error" in jarvis_response.lower():
                        raise Exception("Ollama local no disponible o respuesta inválida.")

                except Exception as ollama_error:
                    print(f"[!] Fallo en Ollama local ({ollama_error}). Derivando a GeminiBrain (Cloud Fallback)...")
                    try:
                        print("[Consultando al cerebro en la nube (Gemini)...]")
                        jarvis_response = gemini_brain.generate_response(user_transcript)
                    except Exception as gemini_error:
                        print(f"[X] Fallo también en GeminiBrain: {gemini_error}")
                        jarvis_response = "Lo siento señor, ninguno de mis cerebros responde en este momento."

                print(f"JARVIS: \"{jarvis_response}\"")
                
                print("[Reproduciendo respuesta por voz (TTS)...]")
                # f) Reproducimos la respuesta generada mediante síntesis de voz neuronal
                pending_speech = tts.speak(jarvis_response)

            except Exception as iteration_error:
                print(f"[X] Error en esta iteración del bucle: {iteration_error}")
                
            finally:
                # Limpieza obligatoria del archivo temporal de audio
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
        # Cerramos el trabajador TTS de forma ordenada para no dejar audio o archivos pendientes.
        tts.close()

    print("\n=== Sesión de J.A.R.V.I.S. finalizada correctamente ===")

if __name__ == "__main__":
    main()
