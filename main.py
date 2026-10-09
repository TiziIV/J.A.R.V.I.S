import os
from src.audio.recorder import AudioRecorder
from src.audio.stt import SpeechToText

def main():
    print("=== Inicializando módulo de prueba de Audio y STT ===")
    
    recorder = AudioRecorder(sample_rate=16000)
    stt = SpeechToText()

    audio_path = None
    try:
        print("\n--- Grabando 4 segundos de voz ---")
        audio_path = recorder.record_seconds(seconds=4)
        print(f"Archivo de audio temporal guardado en: {audio_path}")

        print("\n--- Transcribiendo audio ---")
        transcript = stt.transcribe(audio_path)
        
        print("\n--- Resultado de la Transcripción ---")
        print(f'"{transcript}"')

    except Exception as e:
        print(f"Ocurrió un error durante la ejecución: {e}")
    finally:
        if audio_path and os.path.exists(audio_path):
            try:
                os.remove(audio_path)
                print(f"\nArchivo temporal limpiado: {audio_path}")
            except Exception as cleanup_error:
                print(f"No se pudo limpiar el archivo temporal: {cleanup_error}")

if __name__ == "__main__":
    main()
