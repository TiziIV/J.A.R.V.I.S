"""
Script de compilación y empaquetado de J.A.R.V.I.S. con PyInstaller.

Genera una distribución ejecutable en modo directorio (--onedir) sin ventana de consola (--noconsole),
garantizando la inclusión de 'faster_whisper', recursos de configuración 'config/' y dependencias del sistema.
"""

import os
import sys
import PyInstaller.__main__


def build_executable() -> None:
    """
    Ejecuta el empaquetado de J.A.R.V.I.S. llamando a PyInstaller.__main__.run().
    
    Argumentos configurados:
    - 'main.py': Punto de entrada principal de la aplicación.
    - '--onedir': Genera una carpeta autocontenida de distribución en vez de un único archivo comprimido.
    - '--noconsole': Suprime la ventana de consola secundaria para funcionamiento silencioso en segundo plano.
    - '--hidden-import=faster_whisper': Asegura que el motor de reconocimiento de voz sea empaquetado.
    - '--add-data': Añade la carpeta 'config/' con sus constantes y esquemas.
    """
    project_root = os.path.dirname(os.path.abspath(__file__))
    path_sep = os.pathsep  # ';' en Windows, ':' en plataformas Unix

    # Configuración de carpetas de recursos adicionales requeridos por J.A.R.V.I.S.
    config_data = f"config{path_sep}config"
    skills_data = f"skills{path_sep}skills"

    pyinstaller_args = [
        "main.py",
        "--name=JARVIS",
        "--onedir",
        "--noconsole",
        "--clean",
        "--hidden-import=faster_whisper",
        f"--add-data={config_data}",
        f"--add-data={skills_data}",
        "--hidden-import=pystray",
        "--hidden-import=pystray._win32",
        "--hidden-import=PIL",
        "--hidden-import=sounddevice",
        "--hidden-import=scipy",
        "--hidden-import=src.utils.logger",
        "--distpath=dist",
        "--workpath=build",
        "--specpath=.",
    ]

    print("==================================================================")
    print(" INICIANDO COMPILACIÓN DE J.A.R.V.I.S. CON PYINSTALLER")
    print("==================================================================")
    print(f"Raíz del proyecto: {project_root}")
    print("Modo de distribución: Directorio (--onedir)")
    print("Ventana de consola: Desactivada (--noconsole)")
    print("Importación oculta obligatoria: --hidden-import=faster_whisper")
    print(f"Datos añadidos: config/ ({config_data})")
    print("------------------------------------------------------------------")

    try:
        PyInstaller.__main__.run(pyinstaller_args)
        print("\n[+] Compilación completada con éxito.")
        dist_folder = os.path.join(project_root, "dist", "JARVIS")
        print(f"[+] Carpeta ejecutable generada en: {dist_folder}")
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
