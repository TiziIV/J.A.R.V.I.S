import os
import soundfile as sf
from faster_whisper import WhisperModel
from config.settings import (
    WHISPER_MODEL_SIZE,
    WHISPER_DEVICE,
    WHISPER_COMPUTE_TYPE,
)

class SpeechToText:
    """
    Transcriptor de voz a texto (STT) de alta velocidad basado en faster-whisper (CTranslate2).
    
    Justificación técnica junior:
    OpenAI Whisper original en PyTorch es pesado y consume varios gigabytes de VRAM.
    faster-whisper reimplementa la arquitectura Transformer en C++ mediante CTranslate2,
    logrando una velocidad hasta 4 veces superior y reduciendo el consumo de memoria VRAM
    a menos de 1 GB, ideal para convivir con un LLM en GPUs de 6 GB como la GTX 1660.
    """

    def __init__(
        self,
        model_size: str = WHISPER_MODEL_SIZE,
        device: str = WHISPER_DEVICE,
        compute_type: str = WHISPER_COMPUTE_TYPE,
    ) -> None:
        """
        Inicializa el motor Whisper con tolerancia a fallos en GPU.

        Args:
            model_size (str): Tamaño del modelo ('base', 'small', etc.). 'base' ofrece gran rapidez.
            device (str): Dispositivo de cómputo ('cuda' para GPU o 'cpu').
            compute_type (str): Precisión numérica ('float16' para GPU moderna o 'int8' para CPU).
        """
        self.model_size: str = model_size
        self.device: str = device
        self.compute_type: str = compute_type
        print(f"Cargando modelo Whisper '{self.model_size}' en dispositivo '{self.device}' ({self.compute_type})...")
        try:
            # Intentamos cargar el modelo en el dispositivo preferido (GPU CUDA)
            self.model: WhisperModel = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type
            )
        except Exception as e:
            # Si falla CUDA (ej. falta de drivers NVIDIA o sin GPU), conmutamos a CPU int8
            if self.device != "cpu":
                print(f"Advertencia: Falló al cargar en {self.device} ({e}). Intentando en 'cpu' con compute_type 'int8'...")
                self.device = "cpu"
                self.compute_type = "int8"
                self.model = WhisperModel(
                    self.model_size,
                    device=self.device,
                    compute_type=self.compute_type
                )
            else:
                raise e

    def transcribe(self, audio_file_path: str) -> str:
        """
        Carga el archivo de audio WAV y ejecuta la inferencia de Whisper forzando idioma español.

        Justificación técnica junior:
        - Se carga con soundfile en memoria en lugar de pasar la ruta a Whisper para evitar que
          Windows mantenga descriptores de archivo abiertos o bloqueados ante eliminaciones rápidas.
        - 'language="es"' y 'task="transcribe"': Evita que Whisper intente adivinar el idioma o traducir al inglés.
        - 'condition_on_previous_text=False': Crucial para un asistente por voz; evita que el modelo
          se quede en un bucle alucinatorio repitiendo lo que escuchó en turnos conversacionales previos.
        - 'vad_filter=True': Aplica Silero VAD interno en Whisper para omitir silencios residuales.

        Args:
            audio_file_path (str): Ruta absoluta al archivo .wav grabado por AudioRecorder.

        Returns:
            str: Texto limpio y normalizado reconocido en español.
        """
        if not os.path.exists(audio_file_path):
            raise FileNotFoundError(f"El archivo de audio no existe: {audio_file_path}")

        # Cargamos el audio como un arreglo unidimensional de float32 normalizado (-1.0 a 1.0)
        audio_data, _sample_rate = sf.read(audio_file_path, dtype="float32")

        # Inferencia optimizada con beam_size=5 (equilibrio óptimo entre precisión y latencia <400ms)
        segments, _ = self.model.transcribe(
            audio_data,
            beam_size=5,
            language="es",
            task="transcribe",
            vad_filter=True,
            condition_on_previous_text=False,
        )

        text_segments: list[str] = []
        for segment in segments:
            text: str = segment.text.strip()
            # Filtro probabilístico anti-alucinaciones:
            # - no_speech_prob > 0.65: Alta probabilidad de que solo haya sido ruido ambiente o respiración.
            # - avg_logprob < -1.0: Confianza acústica excesivamente baja, suele ser texto inventado.
            if segment.no_speech_prob > 0.65 or segment.avg_logprob < -1.0:
                continue
            # Una sola letra suele ser un chasquido o artefacto, pero permitimos respuestas breves ("sí", "no")
            if len("".join(character for character in text if character.isalnum())) < 2:
                continue
            text_segments.append(text)

        full_text: str = " ".join(text_segments).strip()
        # Normalizamos espacios internos duplicados o saltos de línea
        cleaned_text: str = " ".join(full_text.split())
        return cleaned_text

