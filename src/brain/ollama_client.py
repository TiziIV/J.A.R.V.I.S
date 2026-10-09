import ollama
from config.settings import OLLAMA_BASE_URL, OLLAMA_MODEL

class OllamaBrain:
    """
    Clase encargada de gestionar la inteligencia artificial local utilizando Ollama.
    Actúa como el 'cerebro' del asistente J.A.R.V.I.S, procesando texto y generando respuestas.
    """
    
    def __init__(self, model_name: str = OLLAMA_MODEL, base_url: str = OLLAMA_BASE_URL):
        """
        Inicializa el cliente de Ollama configurando el modelo y la URL base.
        
        Args:
            model_name (str): Nombre del modelo LLM local (ej. 'qwen2.5-coder:3b').
            base_url (str): Dirección donde corre el servidor de Ollama (ej. 'http://localhost:11434').
        """
        self.model_name = model_name
        self.base_url = base_url
        
        # Configuramos el cliente oficial de Ollama apuntando a la URL personalizada si es necesario
        # Nota: La librería ollama lee OLLAMA_HOST del entorno automáticamente, pero podemos inicializar parámetros.
        print(f"Inicializando cerebro local Ollama con modelo '{self.model_name}' en '{self.base_url}'...")

    def generate_response(self, prompt: str, system_prompt: str = None) -> str:
        """
        Genera una respuesta de texto utilizando el LLM local a partir de un prompt del usuario.
        
        Args:
            prompt (str): Texto o pregunta ingresada por el usuario.
            system_prompt (str, optional): Instrucciones de comportamiento para definir la identidad del asistente.
            
        Returns:
            str: Respuesta generada por el modelo, limpia y optimizada para ser hablada.
        """
        # Definimos una identidad predeterminada alineada con J.A.R.V.I.S: formal, concisa, respetuosa y directa.
        if system_prompt is None:
            system_prompt = (
                "Eres J.A.R.V.I.S., un asistente virtual técnico, formal, conciso, respetuoso y directo al grano. "
                "Tus respuestas deben ser breves, naturales y optimizadas para ser reproducidas por síntesis de voz (TTS). "
                "Evita introducciones largas o viñetas innecesarias a menos que se te pida explícitamente."
            )

        try:
            # Construimos la estructura de mensajes requerida por la API de Ollama (chat endpoint)
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ]

            # Llamada síncrona al cliente de ollama para generar el chat completion
            response = ollama.chat(
                model=self.model_name,
                messages=messages,
                options={
                    "temperature": 0.3,  # Temperatura baja para respuestas más deterministas y precisas
                    "num_predict": 150     # Limitamos la cantidad de tokens para asegurar respuestas cortas y fluidas
                }
            )

            # Extraemos el contenido de la respuesta del asistente
            answer = response["message"]["content"].strip()
            return answer

        except Exception as e:
            # Capturamos cualquier error (ej. Ollama apagado, modelo no encontrado, timeout de red)
            print(f"Error al comunicarse con Ollama: {e}")
            return "Lo siento señor, el servidor local de Ollama no responde o no está disponible en este momento."
