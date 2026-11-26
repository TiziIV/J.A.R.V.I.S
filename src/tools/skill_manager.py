import traceback
from skills.base_skill import BaseSkill
from skills.file_editor_skill import FileEditorSkill
from skills.document_export_skill import DocumentExportSkill
from skills.web_extraction_skill import WebExtractionSkill
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
        self._private_mode: bool = False

        # Registramos las habilidades del sistema por defecto
        self._register_default_skills()

        print(f"SkillManager inicializado con {len(self.skills)} habilidades registradas.")

    @property
    def is_private_mode(self) -> bool:
        """Indica si el aislamiento de logs/trazas de modo privado está activo."""
        return self._private_mode

    def set_private_mode(self, enabled: bool) -> None:
        """Habilita o deshabilita el filtrado estricto de logs y trazas durante modo privado."""
        self._private_mode = bool(enabled)

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
        # Exportación documental y Modo Estudio para Markdown y Word (.docx):
        self.register_skill(DocumentExportSkill())
        # Extracción web segura, estructurada y normalizada:
        self.register_skill(WebExtractionSkill())

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

    @staticmethod
    def _is_file_mutating_skill(skill_name: str, skill_instance: BaseSkill) -> bool:
        """Detecta skills que escriben o editan archivos en disco (incluso por alias o wrapper)."""
        if skill_name in {"file_editor", "document_export"}:
            return True
        if getattr(skill_instance, "name", None) in {"file_editor", "document_export"}:
            return True
        if isinstance(skill_instance, (FileEditorSkill, DocumentExportSkill)):
            return True
        if bool(getattr(skill_instance, "mutates_files", False)) or bool(getattr(skill_instance, "modifies_files", False)):
            return True
        wrapped = getattr(skill_instance, "wrapped_skill", None) or getattr(skill_instance, "_skill", None)
        if isinstance(wrapped, BaseSkill) and wrapped is not skill_instance:
            return SkillManager._is_file_mutating_skill(wrapped.name, wrapped)
        return False

    def execute_skill(self, skill_name: str, params: dict | None = None, **kwargs) -> str:
        """
        Busca una skill registrada por su nombre y ejecuta su método execute con los argumentos dados.
        
        Args:
            skill_name (str): Nombre identificador de la skill (ej. 'system_time', 'app_launcher', 'document_export').
            params (dict, optional): Parámetros empaquetados en diccionario (opcional).
            **kwargs: Argumentos directos por clave necesarios para la ejecución de la skill.
            
        Returns:
            str: Resultado de la ejecución en lenguaje natural listo para TTS.
        """
        # Verificamos si la skill existe en nuestro registro
        if skill_name not in self.skills:
            return f"Lo siento señor, la habilidad '{skill_name}' no está registrada en mis sistemas."

        skill_instance = self.skills[skill_name]
        if self._private_mode and self._is_file_mutating_skill(skill_name, skill_instance):
            return (
                f"Error: La habilidad de escritura en disco '{skill_name}' está bloqueada "
                "durante el modo privado para impedir persistencia de datos de la sesión."
            )
        
        # Consolidar parámetros recibidos como diccionario posicional o vía kwargs
        exec_args: dict = {}
        if isinstance(params, dict):
            exec_args.update(params)
        exec_args.update(kwargs)

        try:
            if self._private_mode:
                print(f"[SkillManager] Ejecutando skill: '{skill_name}' (argumentos omitidos por modo privado)")
            else:
                print(f"[SkillManager] Ejecutando skill: '{skill_name}' con argumentos: {exec_args}")
            # Ejecutamos el método execute de la skill concreta
            result = skill_instance.execute(**exec_args)
            return result

        except Exception as e:
            # Capturamos cualquier error interno de la skill para mantener la resiliencia de la app
            if self._private_mode:
                print(f"[X] Error al ejecutar la skill '{skill_name}' (detalle omitido por modo privado).")
            else:
                print(f"[X] Error al ejecutar la skill '{skill_name}': {e}")
                traceback.print_exc()
            return f"Lo siento señor, ocurrió un error interno al ejecutar la habilidad {skill_name}."

    def execute_skill_with_envelope(
        self,
        skill_name: str,
        params: dict | None = None,
        **kwargs,
    ) -> tuple[str, dict | None]:
        """
        Ejecuta una skill registrada y devuelve `(texto_respuesta, envelope_aislado)`.
        Para skills que manejan datos externos no confiables (como `web_extraction`),
        el segundo elemento contiene el sobre `build_llm_envelope` con nivel de confianza
        `untrusted_external_content` y procedencia explícita (F-05).
        """
        if skill_name not in self.skills:
            return (
                f"Lo siento señor, la habilidad '{skill_name}' no está registrada en mis sistemas.",
                None,
            )

        skill_instance = self.skills[skill_name]
        if self._private_mode and self._is_file_mutating_skill(skill_name, skill_instance):
            return (
                f"Error: La habilidad de escritura en disco '{skill_name}' está bloqueada "
                "durante el modo privado para impedir persistencia de datos de la sesión.",
                None,
            )
        exec_args: dict = {}
        if isinstance(params, dict):
            exec_args.update(params)
        exec_args.update(kwargs)

        try:
            if self._private_mode:
                print(f"[SkillManager] Ejecutando skill: '{skill_name}' (argumentos omitidos por modo privado)")
            else:
                print(f"[SkillManager] Ejecutando skill: '{skill_name}' con argumentos: {exec_args}")
            if hasattr(skill_instance, "execute_with_envelope"):
                text_out, envelope, _report = skill_instance.execute_with_envelope(exec_args)
                return text_out, envelope
            result = skill_instance.execute(**exec_args)
            return result, None
        except Exception as e:
            if self._private_mode:
                print(f"[X] Error al ejecutar la skill '{skill_name}' (detalle omitido por modo privado).")
            else:
                print(f"[X] Error al ejecutar la skill '{skill_name}': {e}")
                traceback.print_exc()
            return (
                f"Lo siento señor, ocurrió un error interno al ejecutar la habilidad {skill_name}.",
                None,
            )
