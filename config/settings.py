import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Registrar DLLs de NVIDIA CUDA para CTranslate2 en Windows
if sys.platform == "win32":
    site_packages = Path(sys.prefix) / "Lib" / "site-packages"
    cublas_dir = site_packages / "nvidia" / "cublas" / "bin"
    cudnn_dir = site_packages / "nvidia" / "cudnn" / "bin"
    if cublas_dir.exists():
        os.add_dll_directory(str(cublas_dir))
        os.environ["PATH"] = str(cublas_dir) + os.pathsep + os.environ["PATH"]
    if cudnn_dir.exists():
        os.add_dll_directory(str(cudnn_dir))
        os.environ["PATH"] = str(cudnn_dir) + os.pathsep + os.environ["PATH"]

# Build path to .env in project root
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:3b")
WHISPER_MODEL_SIZE: str = os.getenv("WHISPER_MODEL_SIZE", "base")
WHISPER_DEVICE: str = os.getenv("WHISPER_DEVICE", "cuda")
WHISPER_COMPUTE_TYPE: str = os.getenv("WHISPER_COMPUTE_TYPE", "float16")
