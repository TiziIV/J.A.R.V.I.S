from abc import ABC, abstractmethod

class BaseSkill(ABC):
    """
    Clase abstracta base (contrato) para todas las habilidades (skills) del sistema J.A.R.V.I.S.
    Cada skill representa una capacidad o herramienta ejecutable (ej. consultar hora, abrir apps).
    """
    
    def __init__(self, name: str, description: str, parameters: dict = None):
        """
        Inicializa los metadatos descriptivos de la skill.
        
        Args:
            name (str): Identificador único de la skill (ej. 'system_time', 'app_launcher').
            description (str): Explicación breve de lo que hace (usada para que el LLM sepa cuándo invocarla).
            parameters (dict, optional): Esquema de argumentos que requiere la skill (por defecto {}).
        """
        self.name: str = name
        self.description: str = description
        self.parameters: dict = parameters or {}

    @abstractmethod
    def execute(self, **kwargs) -> str:
        """
        Método abstracto que contiene la lógica de ejecución de la skill.
        Debe ser implementado obligatoriamente por cada skill concreta.
        
        Args:
            **kwargs: Argumentos dinámicos necesarios para ejecutar la acción.
            
        Returns:
            str: Mensaje en lenguaje natural explicando el resultado, optimizado para ser leído por voz (TTS).
        """
        pass
