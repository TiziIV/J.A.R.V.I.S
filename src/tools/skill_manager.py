import traceback
from skills.base_skill import BaseSkill
from skills.file_editor_skill import FileEditorSkill
from skills.system_skills import (
    TimeSkill,
    AppLauncherSkill,
    SystemInfoSkill,
    WebSearchSkill,
    StarkProtocolSkill,
    VolumeControlSkill,
    MediaControlSkill,
    ClipboardSkill,
    SystemPowerSkill,
)

class SkillManager:
    """
    Clase centralizadora para la gestión, registro y ejecución de todas las habilidades (skills)
    disponibles en el sistema J.A.R.V.I.S.
    """
    
    def __init__(self):
        """
        Inicializa el gestor de habilidades y registra automáticamente las skills disponibles.
        """
        # Diccionario interno para almacenar las instancias de las skills indexadas por su nombre
        self.skills: dict[str, BaseSkill] = {}
        
        # Registramos las habilidades del sistema por defecto
        self._register_default_skills()
        
        print(f"SkillManager inicializado con {len(self.skills)} habilidades registradas.")

    def register_skill(self, skill: BaseSkill) -> None:
        """
        Registra una nueva habilidad en el gestor.
        
        Args:
            skill (BaseSkill): Instancia de la skill a registrar.
        """
        if isinstance(skill, BaseSkill):
            self.skills[skill.name] = skill
            print(f"Skill registrada: '{skill.name}'")
        else:
            print(f"[!] Error: La skill proporcionada no hereda de BaseSkill.")

    def _register_default_skills(self) -> None:
        """
        Instancia y registra las habilidades nativas del sistema (básicas y avanzadas de Windows).
        """
        self.register_skill(TimeSkill())
        self.register_skill(AppLauncherSkill())
        self.register_skill(SystemInfoSkill())
        self.register_skill(WebSearchSkill())
        # Protocolos compuestos de Stark Industries ('activate_protocol'):
        # Permite ejecutar flujos multi-paso como desarrollo, diagnóstico de hardware y modo sigilo.
        self.register_skill(StarkProtocolSkill())
        self.register_skill(VolumeControlSkill())
        self.register_skill(MediaControlSkill())
        self.register_skill(ClipboardSkill())
        self.register_skill(SystemPowerSkill())
        # Editor seguro de archivos con backups SHA-256, validación de rutas y escritura atómica:
        self.register_skill(FileEditorSkill())

    def get_skills_manifest(self) -> list[dict]:
        """
        Retorna un listado estructurado (manifest) con los metadatos de todas las skills activas.
        ¿Para qué sirve? Este manifiesto puede inyectarse en el prompt del LLM para que este
        conozca las herramientas disponibles y sepas cuándo invocarlas.
        
        Returns:
            list[dict]: Lista de diccionarios con name, description y parameters de cada skill.
        """
        manifest = []
        for skill in self.skills.values():
            manifest.append({
                "name": skill.name,
                "description": skill.description,
                "parameters": skill.parameters
            })
        return manifest

    def execute_skill(self, skill_name: str, **kwargs) -> str:
        """
        Busca una skill registrada por su nombre y ejecuta su método execute con los argumentos dados.
        
        Args:
            skill_name (str): Nombre identificador de la skill (ej. 'system_time', 'app_launcher').
            **kwargs: Argumentos necesarios para la ejecución de la skill.
            
        Returns:
            str: Resultado de la ejecución en lenguaje natural listo para TTS.
        """
        # Verificamos si la skill existe en nuestro registro
        if skill_name not in self.skills:
            return f"Lo siento señor, la habilidad '{skill_name}' no está registrada en mis sistemas."

        skill_instance = self.skills[skill_name]
        
        try:
            print(f"[SkillManager] Ejecutando skill: '{skill_name}' con argumentos: {kwargs}")
            # Ejecutamos el método execute de la skill concreta
            result = skill_instance.execute(**kwargs)
            return result
            
        except Exception as e:
            # Capturamos cualquier error interno de la skill para mantener la resiliencia de la app
            print(f"[X] Error al ejecutar la skill '{skill_name}': {e}")
            traceback.print_exc()
            return f"Lo siento señor, ocurrió un error interno al ejecutar la habilidad {skill_name}."
