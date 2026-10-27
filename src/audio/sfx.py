"""
Efectos sonoros HUD de J.A.R.V.I.S. sintetizados dinámicamente en memoria (RAM).

¿Por qué sintetizar en memoria con NumPy en lugar de archivos .wav en disco?
1. Cero dependencias de archivos externos: Evita errores de ejecución si faltan archivos de audio en disco
   o si las rutas relativas fallan al compilar la aplicación con PyInstaller.
2. Latencia ultrabaja (0 ms de I/O): Los datos PCM ya residen en memoria RAM listos para reproducirse.
3. Resiliencia: Si el subsistema de audio de Windows no está disponible o la app corre en Linux,
   todas las funciones fallan de forma silenciosa y segura sin interrumpir el flujo del asistente.
"""

from __future__ import annotations

import io
import threading
import wave

import numpy as np

try:
    # winsound es el módulo nativo de Windows para reproducción de audio de bajo nivel
    import winsound
except ImportError:  # pragma: no cover - solo fuera de Windows
    winsound = None  # type: ignore[assignment]

# Frecuencia de muestreo estándar de alta calidad para efectos (44.1 kHz).
# Nota didáctica junior: Aunque Whisper y el micrófono graban a 16 kHz para optimizar el STT,
# los efectos HUD de salida se sintetizan a 44.1 kHz para obtener un sonido nítido y sin distorsión.
SFX_SAMPLE_RATE: int = 44100
# Volumen calibrado al 22%: debe sonar como una confirmación táctil de interfaz (HUD), no una alarma invasiva.
SFX_VOLUME: float = 0.22


def _sweep(start_hz: float, end_hz: float, duration: float, volume: float = SFX_VOLUME) -> np.ndarray:
    """
    Genera un barrido de frecuencia senoidal (chirp) con envolvente suave de ataque y decaimiento.
    
    ¿Cómo funciona matemáticamente?
    - np.linspace(start_hz, end_hz, n): interpola la frecuencia en el tiempo.
    - np.cumsum: integra la frecuencia instantánea para obtener la fase de la onda senoidal.
    - Armónico leve (2 * phase): añade un sobretono sutil para lograr el timbre metálico "Stark".
    - Envolvente exponencial (env): atenúa el inicio y fin suavemente para evitar clics acústicos molestos.
    """
    n = max(1, int(SFX_SAMPLE_RATE * duration))
    freqs = np.linspace(start_hz, end_hz, n)
    phase = 2 * np.pi * np.cumsum(freqs) / SFX_SAMPLE_RATE
    tone = np.sin(phase) + 0.25 * np.sin(2 * phase)  # Armónico secundario brillante
    attack = max(1, int(n * 0.12))
    env = np.ones(n)
    env[:attack] = np.linspace(0.0, 1.0, attack)
    env[attack:] = np.exp(-4.0 * np.linspace(0.0, 1.0, n - attack))
    return (tone / 1.25) * env * volume


def _silence(duration: float) -> np.ndarray:
    """Genera un fragmento de silencio de duración determinada en segundos."""
    return np.zeros(int(SFX_SAMPLE_RATE * duration))


def _to_wav_bytes(signal: np.ndarray) -> bytes:
    """
    Convierte un arreglo NumPy de punto flotante [-1.0, 1.0] a un archivo WAV válido en memoria (bytes).
    
    ¿Cómo funciona?
    1. Escala los valores float a enteros de 16 bits (rango -32768 a 32767).
    2. Utiliza la librería estándar 'wave' escribiendo directamente sobre un búfer 'io.BytesIO()'.
    3. Retorna los bytes puros del archivo WAV (incluyendo encabezados RIFF/WAVE estándar).
    """
    pcm = (np.clip(signal, -1.0, 1.0) * 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)  # Canal Mono
        wav.setsampwidth(2)   # 16 bits (2 bytes por muestra)
        wav.setframerate(SFX_SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())
    return buffer.getvalue()


def build_wake_sound() -> bytes:
    """
    Genera la firma acústica de activación: doble chirp ascendente agudo (1150Hz -> 2250Hz).
    Simula la interfaz HUD de Iron Man encendiendo sus sensores.
    """
    return _to_wav_bytes(np.concatenate([
        _sweep(1150, 1650, 0.06), _silence(0.025), _sweep(1550, 2250, 0.09),
    ]))


def build_confirm_sound() -> bytes:
    """
    Genera un blip corto y nítido (1750Hz -> 1900Hz en 45ms).
    Acuse de recibo no verbal al ejecutar una herramienta o protocolo.
    """
    return _to_wav_bytes(_sweep(1750, 1900, 0.045, volume=SFX_VOLUME * 0.9))


def build_standby_sound() -> bytes:
    """
    Genera un glissando descendente suave (1250Hz -> 520Hz).
    Señal acústica clara que indica al usuario que J.A.R.V.I.S. ha vuelto al Modo Reposo.
    """
    return _to_wav_bytes(np.concatenate([
        _sweep(1250, 950, 0.08), _silence(0.02), _sweep(900, 520, 0.16),
    ]))


class SoundFX:
    """
    Gestor centralizado de efectos sonoros HUD con caché en memoria y tolerancia total a fallos.
    """

    def __init__(self, enabled: bool = True) -> None:
        """
        Inicializa el reproductor de efectos.
        
        Args:
            enabled (bool): Si es False o no estamos en Windows, el reproductor opera en modo silencioso.
        """
        self.enabled: bool = enabled and winsound is not None
        # Diccionario en memoria para cachear los bytes WAV ya sintetizados y evitar recalcularlos
        self._cache: dict[str, bytes] = {}
        self._builders = {
            "wake": build_wake_sound,
            "confirm": build_confirm_sound,
            "standby": build_standby_sound,
        }
        # Bloqueo para evitar colisiones: winsound.PlaySound no permite superponer sonidos simultáneos
        self._lock = threading.Lock()

    def _get(self, name: str) -> bytes:
        """Obtiene el archivo WAV desde la caché en memoria o lo sintetiza si es la primera vez."""
        if name not in self._cache:
            self._cache[name] = self._builders[name]()
        return self._cache[name]

    def _play_sync(self, name: str) -> None:
        """Reproduce los bytes WAV de forma síncrona mediante la API nativa de Windows."""
        try:
            data = self._get(name)
            with self._lock:
                # SND_MEMORY indica que los datos están en RAM; SND_NODEFAULT evita emitir el bip estándar de error
                winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
        except Exception as error:
            # Nunca dejamos que un fallo de audio interrumpa una acción del asistente
            print(f"[SFX] Sonido '{name}' omitido: {error}")

    def play(self, name: str, blocking: bool = False) -> None:
        """
        Reproduce un efecto HUD por su nombre.
        
        Args:
            name (str): 'wake', 'confirm' o 'standby'.
            blocking (bool): Si es True, bloquea el hilo actual hasta concluir. Si es False,
                            se delega a un hilo daemon para no retrasar la ejecución principal.
        """
        if not self.enabled or name not in self._builders:
            return
        if blocking:
            self._play_sync(name)
        else:
            threading.Thread(target=self._play_sync, args=(name,), daemon=True, name=f"sfx-{name}").start()

    def wake(self) -> None:
        """
        Reproduce el sonido de activación.
        Nota técnica junior: Es deliberadamente bloqueante (~0.18s) para asegurar que el pitido
        termine por completo antes de que el micrófono empiece a calibrar el ruido de fondo (VAD).
        """
        self.play("wake", blocking=True)

    def confirm(self) -> None:
        """Reproduce el blip de confirmación de herramientas en segundo plano."""
        self.play("confirm")

    def standby(self) -> None:
        """Reproduce el tono descendente de entrada en modo reposo en segundo plano."""
        self.play("standby")
