"""
Script de Diagnóstico de Pre-Vuelo (Health Check) de J.A.R.V.I.S.
Verifica en menos de 5 segundos la integridad completa del entorno:
1. Versión de Python y aceleración por GPU CUDA (CTranslate2 / PyTorch).
2. Accesibilidad de bibliotecas dinámicas de NVIDIA (cublas y cudnn).
3. Conexión con el daemon local de Ollama y estado del modelo LLM configurado.
4. Validación segura de variables en '.env' (sin exponer secretos en pantalla).
5. Detección de micrófono de entrada y voces en español en Windows (System.Speech).
"""

import os
import sys
import json
import urllib.request
import subprocess
from pathlib import Path
from typing import NamedTuple

# ==============================================================================
# REGISTRO PREVENTIVO DE DLLs DE CUDA EN WINDOWS
# ==============================================================================
if sys.platform == "win32":
    site_packages: Path = Path(sys.prefix) / "Lib" / "site-packages"
    cublas_dir: Path = site_packages / "nvidia" / "cublas" / "bin"
    cudnn_dir: Path = site_packages / "nvidia" / "cudnn" / "bin"
    if cublas_dir.exists():
        os.add_dll_directory(str(cublas_dir))
        os.environ["PATH"] = str(cublas_dir) + os.pathsep + os.environ["PATH"]
    if cudnn_dir.exists():
        os.add_dll_directory(str(cudnn_dir))
        os.environ["PATH"] = str(cudnn_dir) + os.pathsep + os.environ["PATH"]


class DiagnosticResult(NamedTuple):
    """Estructura de datos tipada para cada ítem verificado en el diagnóstico."""
    componente: str
    estado: str  # "OK", "WARN" o "ERROR"
    detalle: str


def check_python_version() -> DiagnosticResult:
    """Verifica que la versión de Python sea compatible (3.10 a 3.12 recomendadas)."""
    major: int = sys.version_info.major
    minor: int = sys.version_info.minor
    micro: int = sys.version_info.micro
    v_str: str = f"{major}.{minor}.{micro}"

    if major == 3 and 10 <= minor <= 12:
        return DiagnosticResult("Python Runtime", "OK", f"Python {v_str} ({sys.executable})")
    elif major == 3 and minor > 12:
        return DiagnosticResult("Python Runtime", "WARN", f"Python {v_str} (versión muy reciente, verificar dependencias CUDA)")
    else:
        return DiagnosticResult("Python Runtime", "ERROR", f"Python {v_str} (se recomienda Python 3.10 - 3.12 de 64 bits)")


def check_cuda_ctranslate2() -> DiagnosticResult:
    """Verifica si CTranslate2 y PyTorch detectan la GPU NVIDIA CUDA para aceleración."""
    details: list[str] = []
    has_cuda: bool = False

    # 1. Comprobar CTranslate2 (el motor de faster-whisper)
    try:
        import ctranslate2
        cuda_count: int = ctranslate2.get_cuda_device_count()
        if cuda_count > 0:
            has_cuda = True
            details.append(f"CTranslate2 detectó {cuda_count} GPU(s) CUDA")
        else:
            details.append("CTranslate2: Sin GPUs CUDA detectadas (usará CPU)")
    except ImportError:
        details.append("CTranslate2 no instalado")

    # 2. Comprobar PyTorch si está presente
    try:
        import torch
        if torch.cuda.is_available():
            gpu_name: str = torch.cuda.get_device_name(0)
            details.append(f"PyTorch: {gpu_name}")
            has_cuda = True
        else:
            details.append("PyTorch: CUDA no activo")
    except ImportError:
        pass

    if has_cuda:
        return DiagnosticResult("Aceleración GPU (CUDA)", "OK", "; ".join(details))
    return DiagnosticResult("Aceleración GPU (CUDA)", "WARN", "; ".join(details) + " [Modo CPU]")


def check_nvidia_dlls() -> DiagnosticResult:
    """Verifica que las DLLs de CUBLAS y CUDNN de NVIDIA estén presentes en el entorno."""
    site_packages: Path = Path(sys.prefix) / "Lib" / "site-packages"
    cublas_dir: Path = site_packages / "nvidia" / "cublas" / "bin"
    cudnn_dir: Path = site_packages / "nvidia" / "cudnn" / "bin"

    cublas_ok: bool = False
    cudnn_ok: bool = False

    if cublas_dir.exists():
        dlls = list(cublas_dir.glob("cublas*.dll"))
        if dlls:
            cublas_ok = True

    if cudnn_dir.exists():
        dlls = list(cudnn_dir.glob("cudnn*.dll"))
        if dlls:
            cudnn_ok = True

    if cublas_ok and cudnn_ok:
        return DiagnosticResult("NVIDIA DLLs (cublas/cudnn)", "OK", "Bibliotecas dinámicas CUDA registradas correctamente")
    elif cublas_ok or cudnn_ok:
        return DiagnosticResult("NVIDIA DLLs (cublas/cudnn)", "WARN", "Parcialmente encontradas (posible fallback en CPU)")
    else:
        return DiagnosticResult("NVIDIA DLLs (cublas/cudnn)", "WARN", "Paquetes nvidia-* no hallados en site-packages (inferencia en CPU)")


def check_ollama_service() -> DiagnosticResult:
    """Verifica la conectividad con el servidor local de Ollama y el modelo configurado."""
    base_url: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    model_name: str = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:3b")

    try:
        url: str = f"{base_url.rstrip('/')}/api/tags"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = [m.get("name", "") for m in data.get("models", [])]

            # Verificamos si el modelo está descargado
            match = any(model_name in m or m in model_name for m in models)
            if match:
                return DiagnosticResult("Ollama Local Brain", "OK", f"Conectado a '{base_url}'. Modelo '{model_name}' disponible")
            else:
                model_list: str = ", ".join(models) if models else "ninguno"
                return DiagnosticResult(
                    "Ollama Local Brain",
                    "WARN",
                    f"Servidor activo pero '{model_name}' no está instalado. Disponibles: [{model_list}]. Ejecuta 'ollama pull {model_name}'"
                )
    except Exception as e:
        return DiagnosticResult(
            "Ollama Local Brain",
            "WARN",
            f"No se pudo conectar a {base_url} ({type(e).__name__}). Se usará Gemini como respaldo"
        )


def check_env_configuration() -> DiagnosticResult:
    """Verifica la existencia y validez de variables clave en .env sin mostrar secretos."""
    env_path: Path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return DiagnosticResult("Archivo .env", "WARN", "Archivo .env no encontrado en la raíz (se usan valores por defecto)")

    gemini_key: str = os.getenv("GEMINI_API_KEY", "").strip()
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

    if gemini_key:
        # Enmascaramos la clave para seguridad: primeros 4 y últimos 4 caracteres
        masked_key: str = gemini_key[:4] + "..." + gemini_key[-4:] if len(gemini_key) > 8 else "***"
        return DiagnosticResult("Configuración (.env)", "OK", f"GEMINI_API_KEY activa ({masked_key}, {len(gemini_key)} chars). Modelo: {gemini_model}")
    else:
        return DiagnosticResult("Configuración (.env)", "WARN", "GEMINI_API_KEY no configurada (modo 100% offline local)")


def check_audio_microphone() -> DiagnosticResult:
    """Verifica que el sistema operativo exponga al menos un micrófono de entrada."""
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        default_in = sd.default.device[0]

        if default_in is not None and default_in >= 0:
            dev_info = sd.query_devices(default_in, 'input')
            name: str = dev_info.get("name", "Predeterminado")
            sr: float = dev_info.get("default_samplerate", 16000.0)
            return DiagnosticResult("Micrófono de Entrada", "OK", f"'{name}' ({int(sr)} Hz predeterminado)")
        else:
            return DiagnosticResult("Micrófono de Entrada", "ERROR", "No hay ningún micrófono de entrada predeterminado seleccionado")
    except Exception as e:
        return DiagnosticResult("Micrófono de Entrada", "ERROR", f"Error al consultar dispositivos de audio: {e}")


def check_windows_spanish_voices() -> DiagnosticResult:
    """Verifica si Windows tiene instaladas voces en español en System.Speech."""
    if sys.platform != "win32":
        return DiagnosticResult("Voces System.Speech", "WARN", "Entorno no-Windows; se utilizará síntesis de red")

    ps_script: str = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$v = $s.GetInstalledVoices() | Where-Object { "
        "  $_.VoiceInfo.Culture.TwoLetterISOLanguageName -eq 'es' -or "
        "  $_.VoiceInfo.Culture.Name -like 'es-*' -or "
        "  $_.VoiceInfo.Name -match 'Spanish|Helena|Sabina|Laura|Pablo|Raul' "
        "} | ForEach-Object { $_.VoiceInfo.Name }; "
        "$s.Dispose(); "
        "$v -join ', '"
    )

    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=3.0,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        voices: str = proc.stdout.strip()
        if voices:
            return DiagnosticResult("Voces Windows (System.Speech)", "OK", f"Voces hispanas: [{voices}]")
        else:
            return DiagnosticResult(
                "Voces Windows (System.Speech)",
                "WARN",
                "No se detectaron voces españolas en System.Speech (instalar voz de España o México en Configuración de Windows)"
            )
    except Exception as e:
        return DiagnosticResult("Voces Windows (System.Speech)", "WARN", f"No se pudo consultar PowerShell: {e}")


def run_all_checks() -> None:
    """Ejecuta todos los diagnósticos y renderiza una tabla visual en consola."""
    # Cargamos .env usando python-dotenv si está disponible
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    print("\n" + "=" * 80)
    print("        J.A.R.V.I.S. - DIAGNÓSTICO DE PRE-VUELO (SISTEMA Y HARDWARE)")
    print("=" * 80)

    checks = [
        check_python_version(),
        check_cuda_ctranslate2(),
        check_nvidia_dlls(),
        check_ollama_service(),
        check_env_configuration(),
        check_audio_microphone(),
        check_windows_spanish_voices(),
    ]

    # Encabezado de la tabla formateada
    print(f"\n{'COMPONENTE':<32} | {'ESTADO':<8} | {'DETALLE'}")
    print("-" * 80)

    ok_count: int = 0
    warn_count: int = 0
    error_count: int = 0

    for item in checks:
        if item.estado == "OK":
            ok_count += 1
            badge = "[ OK ]  "
        elif item.estado == "WARN":
            warn_count += 1
            badge = "[ WARN ]"
        else:
            error_count += 1
            badge = "[ERROR] "

        # Truncar detalle si excede 60 caracteres para no romper la tabla
        print(f"{item.componente:<32} | {badge:<8} | {item.detalle}")

    print("-" * 80)
    print(f"Resumen: {ok_count} OK, {warn_count} Advertencias, {error_count} Errores críticos.\n")

    if error_count == 0:
        print("[>] VEREDICTO: ¡SISTEMA OPERATIVO Y LISTO PARA VOLAR! (Vuelo autorizado)")
    else:
        print("[X] VEREDICTO: Se detectaron fallos críticos que deben resolverse antes de iniciar.")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    run_all_checks()
