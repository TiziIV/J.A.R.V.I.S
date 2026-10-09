import os
import subprocess
import urllib.parse
import webbrowser
from datetime import datetime
import psutil
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


class SystemInfoSkill(BaseSkill):
    """
    Skill concreta para consultar el estado del hardware del sistema (CPU y memoria RAM).
    Utiliza la librería psutil para medir el uso del procesador y la memoria disponible.
    """
    
    def __init__(self):
        # Inicializamos la clase base con el nombre 'system_info', descripción clara y sin parámetros requeridos
        super().__init__(
            name="system_info",
            description="Devuelve el porcentaje de uso de CPU y la memoria RAM libre del sistema en lenguaje natural.",
            parameters={}
        )

    def execute(self, **kwargs) -> str:
        """
        Obtiene las métricas de CPU y memoria RAM y devuelve un mensaje en lenguaje natural listo para TTS.
        """
        try:
            # Medimos el uso de CPU durante 0.5 segundos para obtener un valor promedio representativo
            cpu_usage = psutil.cpu_percent(interval=0.5)

            # Obtenemos información completa de la memoria RAM del sistema
            mem = psutil.virtual_memory()

            # Convertimos la memoria disponible y usada de bytes a Gigabytes (1 GB = 1024^3 bytes)
            ram_available_gb = round(mem.available / (1024 ** 3), 1)
            ram_used_gb = round(mem.used / (1024 ** 3), 1)

            # Redondeamos el porcentaje de CPU a entero para una lectura natural por voz
            cpu_int = int(round(cpu_usage))

            # Construimos la respuesta en lenguaje natural para el asistente
            resultado = f"El procesador está al {cpu_int}% y dispone de {ram_available_gb} gigabytes de RAM libres, señor."
            return resultado

        except Exception as e:
            # Capturamos excepciones para evitar que un fallo de lectura interrumpa el ciclo del asistente
            print(f"Error al obtener información del sistema: {e}")
            return "Lo siento señor, no pude consultar el estado del procesador y la memoria en este momento."


class WebSearchSkill(BaseSkill):
    """
    Skill concreta para realizar búsquedas en la web (Google o YouTube) abriendo el navegador predeterminado.
    Utiliza las librerías estándar 'webbrowser' y 'urllib.parse'.
    """
    
    def __init__(self):
        # Definimos nombre, descripción y los parámetros 'query' (obligatorio) y 'platform' (opcional)
        super().__init__(
            name="web_search",
            description="Abre el navegador web para buscar términos en Google o YouTube.",
            parameters={
                "query": "Término o consulta que se desea buscar en internet.",
                "platform": "Plataforma de búsqueda: 'google' o 'youtube' (opcional, por defecto 'google')."
            }
        )

    def execute(self, **kwargs) -> str:
        """
        Construye la URL de búsqueda codificando los parámetros y la abre en el navegador.
        
        Args:
            **kwargs: Puede contener 'query' (str) y 'platform' (str).
        """
        # Extraemos el término de búsqueda y la plataforma solicitada
        query = kwargs.get("query", "").strip()
        platform = kwargs.get("platform", "google").lower().strip()

        # Si no se pasó una consulta válida, notificamos al usuario
        if not query:
            return "Por favor especifique qué término desea buscar en el navegador, señor."

        try:
            # Codificamos la consulta para que sea segura en una URL (escapa espacios y caracteres especiales)
            encoded_query = urllib.parse.quote_plus(query)

            # Determinamos el motor de búsqueda según la plataforma elegida
            if platform == "youtube":
                url = f"https://www.youtube.com/results?search_query={encoded_query}"
            else:
                url = f"https://www.google.com/search?q={encoded_query}"

            # Abrimos la URL en el navegador web predeterminado del sistema operativo
            webbrowser.open(url)

            # Retornamos el mensaje esperado para confirmación por voz
            return f"Buscando '{query}' en el navegador, señor."

        except Exception as e:
            # Manejo de error seguro en caso de fallar la apertura del navegador
            print(f"Error al intentar abrir la búsqueda web: {e}")
            return f"Lo siento señor, ocurrió un error al intentar realizar la búsqueda en el navegador."

