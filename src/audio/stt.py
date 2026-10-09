import os
import soundfile as sf
from faster_whisper import WhisperModel
from config.settings import (
    WHISPER_MODEL_SIZE,
    WHISPER_DEVICE,
    WHISPER_COMPUTE_TYPE,
)

class SpeechToText:
    """Transcribe audio and discard segments with strong signs of hallucination."""
    def __init__(
        self,
        model_size: str = WHISPER_MODEL_SIZE,
        device: str = WHISPER_DEVICE,
        compute_type: str = WHISPER_COMPUTE_TYPE,
    ):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        print(f"Cargando modelo Whisper '{self.model_size}' en dispositivo '{self.device}' ({self.compute_type})...")
        try:
            self.model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type
            )
        except Exception as e:
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
        Transcribe the audio file in Spanish using soundfile to load numpy array
        and returns the cleaned text.
        """
        if not os.path.exists(audio_file_path):
            raise FileNotFoundError(f"El archivo de audio no existe: {audio_file_path}")

        audio_data, _sample_rate = sf.read(audio_file_path, dtype="float32")
        # beam_size=5 equilibra calidad y latencia; las métricas ayudan a descartar ruido.
        segments, _ = self.model.transcribe(
            audio_data,
            beam_size=5,
            language="es",
            vad_filter=True,
            condition_on_previous_text=False,
        )

        text_segments = []
        for segment in segments:
            text = segment.text.strip()
            # no_speech_prob alta indica silencio; avg_logprob muy baja indica texto poco confiable.
            if segment.no_speech_prob > 0.65 or segment.avg_logprob < -1.0:
                continue
            # Una sola letra suele ser residuo acústico, pero conservamos respuestas cortas como "sí".
            if len("".join(character for character in text if character.isalnum())) < 2:
                continue
            text_segments.append(text)
        full_text = " ".join(text_segments).strip()
        
        # Clean up any extra internal whitespace/newlines
        cleaned_text = " ".join(full_text.split())
        return cleaned_text
