@echo off
chcp 65001 > nul
title J.A.R.V.I.S. - Asistente de Inteligencia Artificial

:: ==============================================================================
:: LANZADOR RÁPIDO AUTÓNOMO DE J.A.R.V.I.S. (MODO CONSOLA)
:: ==============================================================================
:: Explicación didáctica:
:: %~dp0 apunta dinámicamente al directorio exacto donde reside este archivo .bat.
:: Esto garantiza que el script siempre encuentre la carpeta del proyecto y su
:: entorno virtual sin importar desde dónde se haya hecho doble clic o ejecutado.

cd /d "%~dp0"

echo ==============================================================================
echo                      INICIANDO ASISTENTE J.A.R.V.I.S.
echo ==============================================================================
echo [*] Directorio de trabajo: %~dp0

:: 1. Verificamos la existencia del entorno virtual 'venv'
if not exist "venv\Scripts\activate.bat" (
    echo.
    echo [ERROR] No se encontró el entorno virtual en 'venv\Scripts\activate.bat'.
    echo Por favor crea el entorno virtual ejecutando:
    echo     python -m venv venv
    echo     .\venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

:: 2. Activamos el entorno virtual automáticamente
echo [*] Activando entorno virtual 'venv'...
call venv\Scripts\activate.bat

:: 3. Verificamos que las dependencias mínimas respondan
echo [*] Entorno virtual activo. Lanzando el sistema principal...
echo ==============================================================================
echo.

:: 4. Ejecutamos main.py pasando el control al bucle conversacional
python main.py

:: 5. Si el asistente finaliza o ocurre un error, pausamos para ver el mensaje
if %ERRORLEVEL% neq 0 (
    echo.
    echo [!] El sistema finalizó con código de salida %ERRORLEVEL%.
    pause
)
