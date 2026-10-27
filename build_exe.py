"""
Script de compilación y empaquetado autónomo de J.A.R.V.I.S. con PyInstaller.

Genera una distribución ejecutable en modo directorio (--onedir) para Windows,
garantizando la inclusión de todas las dependencias críticas y modelos:
faster_whisper, ctranslate2, openwakeword, pystray, PIL, pygame, psutil, scipy,
edge_tts, y los recursos de datos locales ('config/', 'skills/').
"""

import os
import sys
from pathlib import Path
import PyInstaller.__main__


def build_executable() -> None:
    """
    Ejecuta el empaquetado de J.A.R.V.I.S. llamando a PyInstaller.__main__.run().
    
    Argumentos configurados:
    - 'main.py': Punto de entrada principal de la aplicación.
    - '--onedir': Genera una carpeta autocontenida de distribución en vez de un solo binario
      (recomendado para proyectos con librerías nativas como CUDA y CTranslate2).
    - '--noconsole': Suprime la ventana de consola secundaria para funcionamiento silencioso en System Tray.
    - '--hidden-import': Garantiza que PyInstaller incluya módulos importados dinámicamente.
    - '--add-data': Empaqueta carpetas esenciales del proyecto ('config/' y 'skills/').
    """
    project_root = Path(__file__).resolve().parent
    path_sep = os.pathsep  # ';' en Windows, ':' en plataformas Unix

    # Configuración de carpetas de recursos adicionales requeridos por J.A.R.V.I.S.
    config_data = f"config{path_sep}config"
    skills_data = f"skills{path_sep}skills"

    # Lista exhaustiva de módulos con carga diferida o empaquetado especial
    hidden_imports = [
        "faster_whisper",
        "ctranslate2",
        "openwakeword",
        "openwakeword.model",
        "openwakeword.utils",
        "pystray",
        "pystray._win32",
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
        "pygame",
        "psutil",
        "scipy",
        "scipy.io",
        "scipy.io.wavfile",
        "sounddevice",
        "soundfile",
        "edge_tts",
        "ollama",
        "google.genai",
        "dotenv",
        "src.utils.logger",
    ]

    pyinstaller_args = [
        "main.py",
        "--name=JARVIS",
        "--onedir",
        "--noconsole",
        "--clean",
        f"--add-data={config_data}",
        f"--add-data={skills_data}",
        "--distpath=dist",
        "--workpath=build",
        "--specpath=.",
    ]

    # Añadimos los hidden-imports a los argumentos
    for imp in hidden_imports:
        pyinstaller_args.append(f"--hidden-import={imp}")

    # Si se pasa el flag '--console' en la invocación de build_exe.py, se mantiene la consola visible
    if "--console" in sys.argv:
        pyinstaller_args.remove("--noconsole")
        print("[!] Modo CONSOLA activado para depuración.")

    print("=" * 70)
    print(" INICIANDO COMPILACIÓN Y EMPAQUETADO DE J.A.R.V.I.S.")
    print("=" * 70)
    print(f"Raíz del proyecto: {project_root}")
    print("Modo de distribución: Directorio (--onedir)")
    print("Módulos ocultos incluidos:")
    for imp in hidden_imports:
        print(f"  - {imp}")
    print(f"Datos añadidos: config/ y skills/")
    print("-" * 70)

    try:
        PyInstaller.__main__.run(pyinstaller_args)
        print("\n[+] Compilación completada con éxito.")
        dist_folder = project_root / "dist" / "JARVIS"
        print(f"[+] Carpeta ejecutable generada en: {dist_folder}")
        print(f"[+] Ejecutable listo en: {dist_folder / 'JARVIS.exe'}")
    except SystemExit as exit_code:
        if exit_code.code != 0:
            print(f"\n[!] Error durante la compilación. Código de salida: {exit_code.code}")
            sys.exit(exit_code.code)
        else:
            print("\n[+] Proceso de PyInstaller completado correctamente.")
    except Exception as exc:
        print(f"\n[X] Error fatal durante el empaquetado: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    build_executable()
