import os
import string
import unicodedata
from concurrent.futures import Future
from src.audio.recorder import AudioRecorder
from src.audio.stt import SpeechToText
from src.brain.ollama_client import OllamaBrain
from src.brain.gemini_client import GeminiBrain
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
    print("=== Inicializando Sistema J.A.R.V.I.S. (Fase 4 - System Skills Completas) ===")
    
    # ---------------------------------------------------------------------------
    # PASO 1: Inicialización única de componentes (evita recargar modelos en VRAM)
    # ---------------------------------------------------------------------------
    recorder = AudioRecorder(sample_rate=16000)  # Grabador con VAD inteligente
    stt = SpeechToText()                         # faster-whisper en GPU
    brain = OllamaBrain()                        # Cerebro local principal (Ollama)
    gemini_brain = GeminiBrain()                 # Cerebro de respaldo en la nube (Gemini)
    tts = TextToSpeech(voice="es-AR-TomasNeural")  # Sintetizador de voz neuronal
    skill_manager = SkillManager()               # Gestor de habilidades y herramientas locales
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
                # PASO 2: Enrutamiento mejorado y flexible para Herramientas (Skills)
                # Evaluamos las intenciones normalizadas antes de consultar a los LLMs.
                # -----------------------------------------------------------------
                handled_by_skill = False
                skill_response = None

                # 2.1. Detección flexible de consulta de Hora o Fecha (TimeSkill)
                time_keywords = ["hora", "fecha", "dia"]
                if any(kw in norm_text for kw in time_keywords):
                    print("[Intent Detected] Habilidad local: system_time")
                    skill_response = skill_manager.execute_skill("system_time")
                    handled_by_skill = True

                # 2.2. Detección de estado del sistema y recursos (SystemInfoSkill)
                # Si el usuario pregunta por CPU, RAM, memoria o estado del sistema, consultamos el hardware.
                # Evaluamos por palabras exactas para prevenir falsos positivos (por ejemplo: 'programar' contiene 'ram').
                elif any(kw in words for kw in ["recursos", "cpu", "ram", "memoria"]) or "estado del sistema" in norm_text:
                    print("[Intent Detected] Habilidad local: system_info")
                    skill_response = skill_manager.execute_skill("system_info")
                    handled_by_skill = True

                # 2.3. Detección de búsqueda en internet o YouTube (WebSearchSkill)
                # Si el texto contiene la intención de buscar en internet o youtube, extraemos el texto y ejecutamos
                elif any(kw in norm_text for kw in ["busca", "buscar", "buscame", "googlea", "googlear"]) or ("en youtube" in norm_text) or ("en google" in norm_text):
                    print("[Intent Detected] Habilidad local: web_search")
                    
                    # Determinamos la plataforma (YouTube si se menciona explícitamente, de lo contrario Google)
                    platform = "youtube" if "youtube" in norm_text else "google"
                    
                    # Extraemos el texto de búsqueda eliminando prefijos de comando conocidos
                    search_prefixes = [
                        "busca en youtube", "buscar en youtube", "buscame en youtube",
                        "busca en google", "buscar en google", "buscame en google",
                        "busca en internet", "buscar en internet", "buscame en internet",
                        "busca en la web", "buscar en la web", "buscame en la web",
                        "googlea", "googlear",
                        "busca", "buscar", "buscame"
                    ]
                    
                    query_text = norm_text
                    for prefix in search_prefixes:
                        if prefix in query_text:
                            query_text = query_text.replace(prefix, "", 1)
                            break
                    
                    # Removemos palabras de cortesía o relleno que no forman parte de la búsqueda real
                    for filler in ["por favor", "me podes", "me puedes", "podes", "puedes", "en youtube", "en google", "en internet", "sobre"]:
                        query_text = query_text.replace(filler, "")
                    
                    query_text = query_text.strip()
                    
                    # Ejecutamos la skill de búsqueda web pasando el término y la plataforma
                    skill_response = skill_manager.execute_skill("web_search", query=query_text, platform=platform)
                    handled_by_skill = True

                # 2.4. Detección flexible de lanzamiento de Aplicaciones (AppLauncherSkill)
                elif any(verb in norm_text for verb in ["abrir", "abri", "ejecutar", "lanzar", "iniciar", "pone", "pon"]):
                    print("[Intent Detected] Habilidad local: app_launcher")
                    
                    # Identificamos con precisión la aplicación solicitada buscando patrones comunes
                    app_target = ""
                    if "calculadora" in norm_text or "calc" in norm_text:
                        app_target = "calculadora"
                    elif "bloc de notas" in norm_text or "notepad" in norm_text:
                        app_target = "bloc de notas"
                    elif "codigo" in norm_text or "code" in norm_text or "visual studio code" in norm_text:
                        app_target = "code"
                    elif "navegador" in norm_text or "chrome" in norm_text or "edge" in norm_text:
                        app_target = "navegador"
                    else:
                        # Si no coincide con ninguna conocida, limpiamos verbos y artículos
                        app_target = norm_text
                        for verb in ["abrir", "abri", "ejecutar", "lanzar", "iniciar", "pone", "pon"]:
                            app_target = app_target.replace(verb, "")
                        for filler in ["por favor", "me", "puedes", "podes", "la", "el", "un", "una"]:
                            app_target = app_target.replace(filler, "")
                        app_target = app_target.strip()
                    
                    skill_response = skill_manager.execute_skill("app_launcher", app_name=app_target)
                    handled_by_skill = True

                # Si una skill manejó la petición, respondemos directamente sin pasar por el LLM
                if handled_by_skill and skill_response:
                    print(f"JARVIS: \"{skill_response}\"")
                    pending_speech = tts.speak(skill_response)
                    continue  # Saltamos al inicio del bucle

                # -----------------------------------------------------------------
                # PASO 3: Flujo de Conversación General (LLM con Fallback)
                # Si no es una skill local, consultamos al cerebro Ollama con respaldo en Gemini.
                # -----------------------------------------------------------------
                jarvis_response = None
                
                try:
                    print("[Consultando al cerebro local (Ollama)...]")
                    jarvis_response = brain.generate_response(user_transcript)
                    
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
