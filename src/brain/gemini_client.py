from google import genai
from google.genai import types
from config.settings import GEMINI_API_KEY, GEMINI_MODEL
from config.prompts import JARVIS_SYSTEM_PROMPT

class GeminiBrain:
    """
    Clase encargada de gestionar el cerebro en la nube utilizando la API de Google Gemini.
    Actúa como respaldo (fallback) robusto cuando el servicio local de Ollama no está disponible o falla.
    """
    
    def __init__(self, api_key: str = GEMINI_API_KEY, model_name: str = GEMINI_MODEL):
        """
        Inicializa el cliente oficial de Google GenAI con la API Key configurada.
        
        Args:
            api_key (str): Clave de API de Google Gemini obtenida de config/settings.py.
            model_name (str): Nombre del modelo Gemini a utilizar (por defecto 'gemini-2.5-flash').
        """
        self.api_key = api_key
        self.model_name = model_name
        
        # Verificamos si la API Key está presente en la configuración
        if not self.api_key or self.api_key.strip() == "":
            print("[!] Advertencia: GEMINI_API_KEY no está configurada en .env. GeminiBrain funcionará en modo degradado.")
            self.client = None
        else:
            try:
                # Inicializamos el cliente oficial de la librería 'google-genai'
                self.client = genai.Client(api_key=self.api_key)
                print(f"Cerebro Gemini inicializado correctamente con modelo '{self.model_name}'.")
            except Exception as e:
                print(f"Error al inicializar el cliente de Google GenAI: {e}")
                self.client = None

    def generate_response(self, prompt: str, system_prompt: str = None) -> str:
        """
        Genera una respuesta de texto utilizando la API en la nube de Google Gemini.
        
        Args:
            prompt (str): Pregunta o instrucción del usuario.
            system_prompt (str, optional): Instrucciones de comportamiento para definir la identidad de J.A.R.V.I.S.
            
        Returns:
            str: Respuesta generada por Gemini, limpia y optimizada para voz.
        """
        # Verificamos si el cliente está listo (si hay API Key válida y inicializada)
        if not self.client:
            return "Lo siento señor, el cliente de Gemini no está configurado por falta de API Key válida."

        # Definimos el system prompt con la identidad formal, concisa y directa de J.A.R.V.I.S.
        if system_prompt is None:
            system_prompt = JARVIS_SYSTEM_PROMPT

        try:
            # Configuramos los parámetros de contenido (incluyendo la instrucción de sistema)
            config = types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.3,         # Temperatura baja para respuestas precisas y consistentes
                max_output_tokens=150    # Límite de tokens para mantener respuestas cortas y fluidas
            )

            # Llamada al modelo Gemini en la nube usando el SDK oficial google-genai
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=config
            )

            # Retornamos el texto de la respuesta limpio de espacios sobrantes
            answer = response.text.strip() if response.text else "Respuesta vacía recibida de Gemini."
            return answer

        except Exception as e:
            # Capturamos cualquier error de red, cuota excedida o fallo de autenticación
            print(f"Error al comunicarse con la API de Gemini: {e}")
            return "Lo siento señor, ha ocurrido un error al conectar con el servicio en la nube de Gemini."
