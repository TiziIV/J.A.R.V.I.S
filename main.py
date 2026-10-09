import os
from src.audio.recorder import AudioRecorder
from src.audio.stt import SpeechToText
from src.brain.ollama_client import OllamaBrain
from src.audio.tts import TextToSpeech

def main():
    """
    Script principal de J.A.R.V.I.S. que unifica el pipeline completo:
    1. Captura de audio (AudioRecorder) -> 2. Transcripción (SpeechToText / Whisper) -> 
    3. Inferencia de IA local (OllamaBrain) -> 4. Síntesis y reproducción de voz (TextToSpeech).
    """
    print("=== Inicializando Sistema J.A.R.V.I.S. (Fase 2) ===")
    
    # Instanciamos cada módulo modular del sistema con comentarios didácticos
    # - Recorder: captura el audio del micrófono a 16kHz
    recorder = AudioRecorder(sample_rate=16000)
    
    # - SpeechToText: procesa el audio con faster-whisper en GPU
    stt = SpeechToText()
    
    # - OllamaBrain: procesa el texto con el LLM local (Qwen 2.5 Coder 3B)
    brain = OllamaBrain()
    
    # - TextToSpeech: sintetiza la respuesta de texto a voz neuronal
    tts = TextToSpeech(voice="es-AR-TomasNeural")

    audio_path = None
    try:
        print("\n[+] Sistema listo. Por favor hable claro al micrófono...")
        print("--- Grabando 4 segundos de voz ---")
        
        # Grabamos 4 segundos de audio desde consola y obtenemos la ruta absoluta del archivo temporal .wav
        audio_path = recorder.record_seconds(seconds=4)
        print(f"Audio capturado exitosamente en: {audio_path}")

        print("\n--- Transcribiendo audio con Whisper (GPU) ---")
        # Transcribimos el archivo de audio usando faster-whisper (idioma español)
        user_transcript = stt.transcribe(audio_path)
        
        # Verificamos si se obtuvo texto válido tras la transcripción
        if user_transcript and len(user_transcript.strip()) > 0:
            print(f"\nUsuario: \"{user_transcript}\"")

            print("\n--- Consultando al cerebro local (Ollama) ---")
            # Enviamos el texto transcrito al LLM local para obtener la respuesta de J.A.R.V.I.S.
            jarvis_response = brain.generate_response(user_transcript)
            print(f"JARVIS: \"{jarvis_response}\"")

            print("\n--- Reproduciendo respuesta por voz (TTS) ---")
            # Convertimos la respuesta de texto a voz y la reproducimos en los altavoces
            tts.speak(jarvis_response)
        else:
            print("\n[!] No se detectó voz clara o el audio estaba vacío. Por favor intente nuevamente.")
            tts.speak("Lo siento señor, no pude escucharle con claridad. Intente de nuevo.")

    except Exception as e:
        # Manejo global de excepciones para evitar caídas abruptas del script principal
        print(f"\n[X] Ocurrió un error crítico en el flujo principal: {e}")

    finally:
        # Limpieza final del archivo de audio temporal generado durante la grabación
        if audio_path and os.path.exists(audio_path):
            try:
                os.remove(audio_path)
                print(f"\n[i] Archivo de audio temporal limpiado correctamente: {audio_path}")
            except Exception as cleanup_error:
                print(f"[!] No se pudo limpiar el archivo temporal de audio: {cleanup_error}")

    print("\n=== Sesión de J.A.R.V.I.S. finalizada ===")

if __name__ == "__main__":
    main()
