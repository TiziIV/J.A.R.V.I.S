from abc import ABC, abstractmethod
from typing import Any

class BaseSkill(ABC):
    """
    Clase abstracta base para definir habilidades (skills) modulares en J.A.R.V.I.S.
    Cada skill representa una capacidad específica que el asistente puede ejecutar
    (por ejemplo, control del sistema, consultas web, automatización, etc.).
    """
    
    def __init__(self, name: str, description: str):
        """
        Inicializa una skill con su nombre identificativo y descripción funcional.
        
        Args:
            name (str): Nombre único de la skill.
            description (str): Breve explicación de lo que hace la skill.
        """
        self.name: str = name
        self.description: str = description

    @abstractmethod
    def execute(self, **kwargs: Any) -> Any:
        """
        Método abstracto que debe ser implementado por cada skill concreta.
        Contiene la lógica de ejecución principal de la tarea.
        
        Args:
            **kwargs: Argumentos dinámicos necesarios para la ejecución de la skill.
            
        Returns:
            Any: Resultado de la ejecución de la skill concreta.
        """
        pass
