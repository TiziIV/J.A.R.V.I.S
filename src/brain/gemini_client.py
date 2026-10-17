from google import genai
from google.genai import types

from config.prompts import JARVIS_SYSTEM_PROMPT
from config.settings import GEMINI_API_KEY, GEMINI_MODEL


# Google mantiene este modelo Flash como opción estable y rápida recomendada
DEFAULT_GEMINI_MODEL: str = "gemini-2.0-flash"
# Fallback a modelo probado y ampliamente disponible si el principal da 404
SECONDARY_GEMINI_MODEL: str = "gemini-1.5-flash"
# Modelos obsoletos o inexistentes que se redirigen automáticamente al modelo vigente
LEGACY_GEMINI_MODELS: set[str] = {
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
    "gemini-1.5-flash-latest",
    "gemini-2.0-flash-001",
}


class GeminiBrain:
    """Cliente del cerebro en la nube, usado como respaldo de Ollama local."""

    def __init__(self, api_key: str = GEMINI_API_KEY, model_name: str = GEMINI_MODEL):
        # Guardamos la clave solo en memoria y nunca la mostramos en los logs.
        self.api_key: str = api_key.strip() if api_key else ""
        # Normalizamos el nombre para sustituir configuraciones antiguas conocidas.
        self.model_name: str = self._normalize_model_name(model_name)
        # El cliente permanece None cuando falta configuración o falla su inicialización.
        self.client: genai.Client | None = None

        if not self.api_key:
            print("[!] GEMINI_API_KEY no está configurada; Gemini queda en modo degradado.")
            return

        try:
            # Usamos el SDK oficial google-genai y la clave leída desde config/settings.py.
            self.client = genai.Client(api_key=self.api_key)
            print(f"Cerebro Gemini inicializado con el modelo '{self.model_name}'.")
        except Exception as error:
            # El tipo de error ayuda a diagnosticar sin imprimir la clave ni detalles sensibles.
            print(f"[Gemini] No se pudo inicializar el cliente ({type(error).__name__}).")

    @staticmethod
    def _normalize_model_name(model_name: str | None) -> str:
        """Limpia prefijos de recurso y actualiza los nombres retirados conocidos."""
        # Algunos archivos de configuración copian el nombre completo `models/...` de la API.
        normalized_name: str = (model_name or "").strip()
        if normalized_name.startswith("models/"):
            normalized_name = normalized_name.removeprefix("models/")

        # Una opción vacía o un modelo legado debe llevar al modelo vigente predeterminado.
        if not normalized_name or normalized_name.lower() in LEGACY_GEMINI_MODELS:
            return DEFAULT_GEMINI_MODEL
        return normalized_name

    @staticmethod
    def _is_model_not_found(error: Exception) -> bool:
        """Reconoce respuestas 404 del API sin depender de una clase SDK específica."""
        # Las versiones del SDK pueden exponer el código con nombres de atributo distintos.
        error_code: object = getattr(error, "code", "")
        error_status: object = getattr(error, "status", "")
        error_details: str = f"{error_code} {error_status} {error}".lower()
        # El API suele expresar el mismo caso como código 404 o estado NOT_FOUND.
        return "404" in error_details or "not_found" in error_details

    def _generate_with_model(
        self,
        prompt: str,
        system_prompt: str,
        model_name: str,
    ) -> str:
        """Realiza una solicitud con el cliente oficial y devuelve texto limpio."""
        if self.client is None:
            raise RuntimeError("El cliente Gemini no está inicializado.")

        # Reutilizamos los límites conversacionales para mantener respuestas breves por voz.
        generation_config: types.GenerateContentConfig = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.3,
            max_output_tokens=150,
        )
        # generate_content es la interfaz oficial síncrona para respuestas de texto.
        response = self.client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=generation_config,
        )
        # Un resultado sin texto se informa claramente para que el llamador pueda responder.
        response_text: str = response.text.strip() if response.text else ""
        if not response_text:
            return "Gemini devolvió una respuesta vacía."
        return response_text

    def generate_response(self, prompt: str, system_prompt: str | None = None) -> str:
        """Genera una respuesta y reintenta con un modelo vigente solo ante un 404."""
        if self.client is None:
            return "Lo siento señor, Gemini no está configurado o no pudo inicializarse."

        # Si no se envía una instrucción específica, ambos cerebros comparten el prompt común.
        active_system_prompt: str = system_prompt or JARVIS_SYSTEM_PROMPT
        # Probamos primero el modelo elegido y dejamos dos alternativas actuales para 404.
        candidate_models: list[str] = list(
            dict.fromkeys(
                [
                    self.model_name,
                    DEFAULT_GEMINI_MODEL,
                    SECONDARY_GEMINI_MODEL,
                ]
            )
        )

        for model_index, model_name in enumerate(candidate_models):
            try:
                answer: str = self._generate_with_model(
                    prompt=prompt,
                    system_prompt=active_system_prompt,
                    model_name=model_name,
                )
                # Recordamos el modelo que respondió para evitar repetir un nombre inválido.
                self.model_name = model_name
                return answer
            except Exception as error:
                # Solo un modelo no encontrado justifica reintentar con otro identificador.
                has_next_model: bool = model_index + 1 < len(candidate_models)
                if self._is_model_not_found(error) and has_next_model:
                    next_model: str = candidate_models[model_index + 1]
                    print(f"[Gemini] El modelo '{model_name}' no está disponible; pruebo '{next_model}'.")
                    continue

                # Los errores de red, cuota o autenticación se devuelven sin romper el turno.
                print(f"[Gemini] Falló la generación ({type(error).__name__}) con '{model_name}'.")
                if self._is_model_not_found(error):
                    return "Lo siento señor, los modelos Gemini disponibles no aceptaron la solicitud."
                return "Lo siento señor, no pude conectar con Gemini en este momento."

        # Este retorno cubre defensivamente una lista de modelos vacía o agotada.
        return "Lo siento señor, Gemini no pudo generar una respuesta en este momento."
