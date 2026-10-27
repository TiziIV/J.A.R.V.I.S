' ==============================================================================
' LANZADOR SILENCIOSO EN SEGUNDO PLANO DE J.A.R.V.I.S. (SIN VENTANA DE CONSOLA)
' ==============================================================================
' Explicación didáctica:
' Este script utiliza Windows Script Host (WScript.Shell) para invocar 'main.py'
' a través de pythonw.exe del entorno virtual con estilo de ventana 0 (oculto).
' Al ejecutarse, J.A.R.V.I.S. se inicia directamente en la bandeja del sistema (System Tray)
' con su icono circular azul sin abrir ninguna ventana de terminal molesta en pantalla.
'
' Guía de inicio automático con Windows:
' 1. Presiona Win + R, escribe 'shell:startup' y pulsa Enter.
' 2. Crea un acceso directo a este archivo 'run_jarvis_background.vbs' dentro de esa carpeta.
' 3. J.A.R.V.I.S. despertará automáticamente cada vez que inicies sesión en tu computadora.

Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")

' Obtenemos la ruta absoluta de la carpeta donde reside este script .vbs
CurrentDir = FSO.GetParentFolderName(WScript.ScriptFullName)

' Ruta al ejecutable pythonw.exe del virtualenv (pythonw ejecuta scripts Python sin ventana)
PythonwPath = CurrentDir & "\venv\Scripts\pythonw.exe"
MainScript = CurrentDir & "\main.py"

If FSO.FileExists(PythonwPath) Then
    ' 0 = Oculta la ventana de la consola por completo
    ' False = No bloquea el script, permitiendo que corra libremente en segundo plano
    WshShell.Run """" & PythonwPath & """ """ & MainScript & """", 0, False
Else
    ' Fallback en caso de que no exista el entorno virtual configurado
    MsgBox "No se encontró el entorno virtual en 'venv\Scripts\pythonw.exe'. Por favor ejecute primero 'run_jarvis.bat'.", 48, "J.A.R.V.I.S. Error"
End If

Set WshShell = Nothing
Set FSO = Nothing
