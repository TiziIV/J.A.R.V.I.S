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
    Speech-to-Text implementation using faster-whisper.
    """
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

        audio_data, sample_rate = sf.read(audio_file_path, dtype="float32")
        segments, _ = self.model.transcribe(audio_data, beam_size=5, language="es")
        
        text_segments = [segment.text.strip() for segment in segments]
        full_text = " ".join(text_segments).strip()
        
        # Clean up any extra internal whitespace/newlines
        cleaned_text = " ".join(full_text.split())
        return cleaned_text
