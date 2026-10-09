import os
import subprocess
from datetime import datetime
from skills.base_skill import BaseSkill

class TimeSkill(BaseSkill):
    """
    Skill concreta para consultar la fecha y hora actual del sistema.
    """
    
    def __init__(self):
        # Definimos nombre, descripción y parámetros vacíos
        super().__init__(
            name="system_time",
            description="Devuelve la hora y la fecha actual del sistema en formato de lenguaje natural.",
            parameters={}
        )

    def execute(self, **kwargs) -> str:
        """
        Obtiene la fecha y hora actual y la formatea en español amigable para voz.
        """
        try:
            # Obtenemos la fecha y hora actual del sistema operativo
            now = datetime.now()
            
            # Diccionarios para traducir meses al español de forma natural
            meses = {
                1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
                5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
                9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
            }
            
            hora = now.strftime("%H")
            minutos = now.strftime("%M")
            dia = now.day
            mes = meses[now.month]
            
            # Construimos una oración fluida para el TTS
            resultado = f"Son las {hora} con {minutos} minutos del {dia} de {mes}."
            return resultado
            
        except Exception as e:
            print(f"Error al obtener la hora del sistema: {e}")
            return "Lo siento señor, no pude consultar la hora actual del sistema."


class AppLauncherSkill(BaseSkill):
    """
    Skill concreta para abrir aplicaciones comunes de Windows de manera segura mediante una lista blanca (whitelist).
    """
    
    def __init__(self):
        # Definimos nombre, descripción y el parámetro esperado 'app_name'
        super().__init__(
            name="app_launcher",
            description="Abre aplicaciones comunes de Windows como el Bloc de notas, Calculadora o navegador web.",
            parameters={
                "app_name": "Nombre de la aplicación que se desea abrir (ej. 'bloc de notas', 'calculadora', 'code')."
            }
        )
        
        # Diccionario seguro (Whitelist): mapea nombres comunes normalizados a sus ejecutables o comandos reales en Windows
        self.safe_apps = {
            "bloc de notas": "notepad.exe",
            "notepad": "notepad.exe",
            "calculadora": "calc.exe",
            "calc": "calc.exe",
            "visual studio code": "code",
            "vs code": "code",
            "code": "code",
            "navegador": "start msedge",
            "edge": "start msedge",
            "chrome": "start chrome",
            "explorador": "explorer.exe",
            "paint": "mspaint.exe"
        }

    def execute(self, **kwargs) -> str:
        """
        Busca la aplicación solicitada en la lista blanca y la ejecuta en segundo plano.
        
        Args:
            **kwargs: Debe contener 'app_name' (str).
        """
        # Extraemos el nombre de la app de los argumentos recibidos
        app_name = kwargs.get("app_name", "").lower().strip()
        
        if not app_name:
            return "Por favor especifique qué aplicación desea abrir, señor."

        # Verificamos si la aplicación solicitada está dentro de nuestra whitelist segura
        matched_command = None
        matched_display_name = app_name

        for key, cmd in self.safe_apps.items():
            if key in app_name or app_name in key:
                matched_command = cmd
                matched_display_name = key
                break

        if not matched_command:
            return f"Lo siento señor, no tengo registrada la aplicación '{app_name}' en mi lista de programas seguros."

        try:
            # Ejecutamos el comando de forma segura en segundo plano en Windows
            if matched_command.startswith("start "):
                actual_cmd = matched_command.replace("start ", "")
                subprocess.Popen(f"start {actual_cmd}", shell=True)
            else:
                subprocess.Popen(matched_command, shell=True)

            return f"Abriendo {matched_display_name}, señor."
            
        except Exception as e:
            print(f"Error al intentar abrir la aplicación '{app_name}': {e}")
            return f"Lo siento señor, ocurrió un error al intentar abrir {matched_display_name}."
