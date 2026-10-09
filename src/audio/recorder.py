import os
import tempfile
import sounddevice as sd
from scipy.io import wavfile

class AudioRecorder:
    """
    Audio recorder using sounddevice and scipy.io.wavfile.
    """
    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate

    def record_seconds(self, seconds: int = 5) -> str:
        """
        Captures microphone audio via console at sample_rate Hz,
        saves it to a temporary .wav file, and returns its absolute path.
        """
        print(f"Iniciando grabación por {seconds} segundos...")
        num_samples = int(seconds * self.sample_rate)
        
        # Record mono audio (channels=1), 16-bit PCM integer (dtype='int16')
        audio_data = sd.rec(
            num_samples,
            samplerate=self.sample_rate,
            channels=1,
            dtype='int16'
        )
        sd.wait()  # Wait until recording finishes
        print("Grabación finalizada.")

        # Create a temporary .wav file
        temp_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        temp_file.close()
        
        # Save audio data using scipy.io.wavfile
        wavfile.write(temp_file.name, self.sample_rate, audio_data)
        
        abs_path = os.path.abspath(temp_file.name)
        return abs_path
