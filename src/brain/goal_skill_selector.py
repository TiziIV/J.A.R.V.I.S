"""Selector y validador de capacidades reales para el motor de objetivos N6.

Inspecciona el manifiesto dinámico de `SkillManager` y clasifica el riesgo de cada
habilidad en las tres categorías de política de J.A.R.V.I.S.:
- SAFE_PLANNING
- LOW_RISK_EXECUTION
- SENSITIVE_OPERATION

Regla arquitectónica: nunca inventa herramientas inexistentes ni suaviza el nivel
de riesgo declarado por el motor de políticas.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from src.brain.goal_models import (
    Constraints,
    GoalPlanningError,
    GoalPolicyViolationError,
    GoalTask,
    TaskRiskCategory,
)
from src.tools.skill_manager import SkillManager


# Mapa base de riesgo por nombre exacto de skill registrada en J.A.R.V.I.S.
# Toda skill no listada explícitamente recibe clasificación conservadora SENSITIVE_OPERATION.
BASE_SKILL_RISK_MAP: dict[str, TaskRiskCategory] = {
    "system_time": TaskRiskCategory.LOW_RISK_EXECUTION,
    "system_info": TaskRiskCategory.LOW_RISK_EXECUTION,
    "web_search": TaskRiskCategory.LOW_RISK_EXECUTION,
    "web_extraction": TaskRiskCategory.LOW_RISK_EXECUTION,
    "clipboard_manager": TaskRiskCategory.LOW_RISK_EXECUTION,  # Sube a SENSITIVE si action == "write"
    "document_export": TaskRiskCategory.SENSITIVE_OPERATION,
    "file_editor": TaskRiskCategory.SENSITIVE_OPERATION,
    "app_launcher": TaskRiskCategory.SENSITIVE_OPERATION,
    "activate_protocol": TaskRiskCategory.SENSITIVE_OPERATION,
    "volume_control": TaskRiskCategory.SENSITIVE_OPERATION,
    "media_control": TaskRiskCategory.SENSITIVE_OPERATION,
    "system_power": TaskRiskCategory.SENSITIVE_OPERATION,
}

# Skills que modifican archivos locales o escriben artefactos persistentes en disco.
FILE_MUTATING_SKILLS: frozenset[str] = frozenset({"file_editor", "document_export"})

# Skills que requieren conectividad de red saliente.
NETWORK_SKILLS: frozenset[str] = frozenset({"web_search", "web_extraction"})

# Skills que modifican estado del sistema operativo aunque no editen archivos del usuario.
SYSTEM_MUTATING_SKILLS: frozenset[str] = frozenset(
    {"app_launcher", "activate_protocol", "volume_control", "media_control", "system_power"}
)


@dataclass(frozen=True)
class SkillSelectionResult:
    """Resultado trazable de selección o validación de una skill."""

    skill_name: Optional[str]
    risk_category: TaskRiskCategory
    requires_approval: bool
    requires_chain_orchestrator: bool
    reason: str
    validated_params: dict[str, Any]


class GoalSkillSelector:
    """Consulta `SkillManager` para elegir y validar skills compatibles con cada subtarea."""

    def __init__(self, skill_manager: Optional[SkillManager] = None) -> None:
        # Reutiliza `SkillManager` sin acoplar módulos de audio ni clientes LLM.
        self.skill_manager: SkillManager = skill_manager or SkillManager()

    def available_skill_names(self) -> list[str]:
        """Devuelve los nombres reales de todas las skills registradas actualmente."""
        return sorted(self.skill_manager.skills.keys())

    def get_manifest_summary(self) -> list[dict[str, Any]]:
        """Expone el manifiesto completo de herramientas registradas para el planificador."""
        return self.skill_manager.get_skills_manifest()

    def has_skill(self, skill_name: Optional[str]) -> bool:
        """Verifica si una skill existe realmente en el registro activo."""
        if not skill_name:
            return False
        return skill_name in self.skill_manager.skills

    def classify_skill_risk(
        self,
        skill_name: Optional[str],
        params: Optional[dict[str, Any]] = None,
    ) -> TaskRiskCategory:
        """Determina el nivel de riesgo conservador según herramienta y parámetros."""
        if skill_name is None:
            return TaskRiskCategory.SAFE_PLANNING

        clean_name = skill_name.strip()
        if clean_name == "clipboard_manager":
            action = str((params or {}).get("action", "read")).strip().lower()
            if action == "write":
                return TaskRiskCategory.SENSITIVE_OPERATION
            return TaskRiskCategory.LOW_RISK_EXECUTION

        return BASE_SKILL_RISK_MAP.get(clean_name, TaskRiskCategory.SENSITIVE_OPERATION)

    def requires_chain_orchestrator(self, skill_name: Optional[str], metadata: Optional[dict[str, Any]] = None) -> bool:
        """Indica si la tarea debe delegarse obligatoriamente a `ChainOrchestrator`."""
        if skill_name == "file_editor":
            return True
        if isinstance(metadata, dict) and metadata.get("use_chain_orchestrator"):
            return True
        return False

    def validate_skill_parameters(
        self,
        skill_name: str,
        params: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        """Valida existencia de la skill y presencia/tipo básico de parámetros obligatorios."""
        if not self.has_skill(skill_name):
            available = ", ".join(self.available_skill_names()) or "(ninguna)"
            raise GoalPlanningError(
                f"La skill solicitada '{skill_name}' no existe en SkillManager. "
                f"Disponibles: {available}."
            )

        skill = self.skill_manager.skills[skill_name]
        schema = getattr(skill, "parameters", None) or {}
        clean_params = dict(params or {})

        if isinstance(schema, dict):
            required_fields = schema.get("required") or []
            properties = schema.get("properties") or {}
            for req in required_fields:
                if req not in clean_params or clean_params[req] in (None, ""):
                    raise GoalPlanningError(
                        f"Falta el parámetro obligatorio '{req}' para la skill '{skill_name}'."
                    )
            # Verifica enumeraciones declaradas en el esquema cuando existan.
            for key, value in clean_params.items():
                prop_spec = properties.get(key)
                if isinstance(prop_spec, dict) and "enum" in prop_spec:
                    allowed_values = prop_spec["enum"]
                    if value not in allowed_values:
                        raise GoalPlanningError(
                            f"Valor inválido '{value}' para parámetro '{key}' en '{skill_name}'. "
                            f"Permitidos: {allowed_values}."
                        )

        return clean_params

    def check_constraints_compliance(
        self,
        *,
        skill_name: Optional[str],
        params: Optional[dict[str, Any]],
        constraints: Constraints,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Rechaza tareas incompatibles con restricciones explícitas del usuario o de política."""
        if skill_name is None and not (metadata or {}).get("use_chain_orchestrator"):
            return

        clean_name = (skill_name or "").strip()
        if clean_name in [item.strip() for item in constraints.forbidden_actions]:
            raise GoalPolicyViolationError(
                f"La acción '{clean_name}' está explícitamente prohibida por las restricciones del objetivo."
            )

        is_file_mutation = (
            clean_name in FILE_MUTATING_SKILLS
            or bool((metadata or {}).get("modifies_files"))
            or bool((metadata or {}).get("use_chain_orchestrator"))
        )
        if is_file_mutation and (constraints.read_only or not constraints.allow_file_modifications):
            raise GoalPolicyViolationError(
                f"La tarea con '{clean_name or 'chain_orchestrator'}' intenta modificar archivos, "
                "pero el objetivo tiene restricción de solo lectura / sin modificación de archivos."
            )

        if constraints.read_only and clean_name in SYSTEM_MUTATING_SKILLS:
            raise GoalPolicyViolationError(
                f"La skill '{clean_name}' genera cambios de estado y está bloqueada en modo solo lectura."
            )

        if constraints.read_only and clean_name == "clipboard_manager":
            if str((params or {}).get("action", "read")).strip().lower() == "write":
                raise GoalPolicyViolationError(
                    "No se permite escribir en el portapapeles bajo restricción de solo lectura."
                )

        if not constraints.allow_network and clean_name in NETWORK_SKILLS:
            raise GoalPolicyViolationError(
                f"La skill '{clean_name}' requiere red y el objetivo prohibió acceso de red."
            )

    def validate_task(self, task: GoalTask, constraints: Constraints) -> SkillSelectionResult:
        """Valida una `GoalTask` completa y normaliza su categoría de riesgo efectiva."""
        if task.skill_name is not None:
            validated_params = self.validate_skill_parameters(task.skill_name, task.skill_params)
        else:
            validated_params = dict(task.skill_params)

        self.check_constraints_compliance(
            skill_name=task.skill_name,
            params=validated_params,
            constraints=constraints,
            metadata=task.metadata,
        )

        computed_risk = self.classify_skill_risk(task.skill_name, validated_params)
        # Si la tarea invoca ChainOrchestrator o marca modificación de archivos, siempre es SENSITIVE.
        use_chain = self.requires_chain_orchestrator(task.skill_name, task.metadata)
        if use_chain or task.metadata.get("modifies_files"):
            computed_risk = TaskRiskCategory.SENSITIVE_OPERATION

        # Principio de menor privilegio: nunca degradar riesgo ni quitar exigencia de aprobación.
        risk_rank = {
            TaskRiskCategory.SAFE_PLANNING: 0,
            TaskRiskCategory.LOW_RISK_EXECUTION: 1,
            TaskRiskCategory.SENSITIVE_OPERATION: 2,
        }
        effective_risk = (
            computed_risk
            if risk_rank[computed_risk] >= risk_rank[task.risk_category]
            else task.risk_category
        )
        effective_approval = (
            task.requires_approval
            or effective_risk == TaskRiskCategory.SENSITIVE_OPERATION
            or (constraints.require_explicit_approval_for_changes and use_chain)
        )

        task.skill_params = validated_params
        task.risk_category = effective_risk
        task.requires_approval = effective_approval

        return SkillSelectionResult(
            skill_name=task.skill_name,
            risk_category=effective_risk,
            requires_approval=effective_approval,
            requires_chain_orchestrator=use_chain,
            reason=f"Validada contra SkillManager ({task.skill_name or 'razonamiento interno'}).",
            validated_params=validated_params,
        )

    def select_skill_for_intent(
        self,
        capability_hint: str,
        *,
        constraints: Constraints,
        params: Optional[dict[str, Any]] = None,
    ) -> SkillSelectionResult:
        """Mapea una necesidad funcional abstracta a una skill existente verificada."""
        hint = (capability_hint or "").strip().lower()
        mapping: dict[str, str] = {
            "hardware_inspection": "system_info",
            "system_diagnostics": "system_info",
            "time": "system_time",
            "web_research": "web_search",
            "web_read": "web_extraction",
            "clipboard_read": "clipboard_manager",
            "clipboard_write": "clipboard_manager",
            "file_modification": "file_editor",
            "document_export": "document_export",
            "launch_app": "app_launcher",
            "protocol": "activate_protocol",
            "volume": "volume_control",
            "media": "media_control",
            "power": "system_power",
        }

        if hint in {"analysis", "synthesis", "planning", "internal_reasoning"}:
            return SkillSelectionResult(
                skill_name=None,
                risk_category=TaskRiskCategory.SAFE_PLANNING,
                requires_approval=False,
                requires_chain_orchestrator=False,
                reason="Tarea cognitiva interna sin efectos secundarios externos.",
                validated_params={},
            )

        selected_name = mapping.get(hint, hint)
        if not self.has_skill(selected_name):
            available = ", ".join(self.available_skill_names()) or "(ninguna)"
            raise GoalPlanningError(
                f"No existe ninguna skill registrada para la capacidad '{capability_hint}' "
                f"(resuelta a '{selected_name}'). Disponibles: {available}."
            )

        validated_params = self.validate_skill_parameters(selected_name, params)
        self.check_constraints_compliance(
            skill_name=selected_name,
            params=validated_params,
            constraints=constraints,
        )
        risk = self.classify_skill_risk(selected_name, validated_params)
        use_chain = self.requires_chain_orchestrator(selected_name)
        return SkillSelectionResult(
            skill_name=selected_name,
            risk_category=risk,
            requires_approval=(risk == TaskRiskCategory.SENSITIVE_OPERATION),
            requires_chain_orchestrator=use_chain,
            reason=f"Capacidad '{capability_hint}' resuelta a skill real '{selected_name}'.",
            validated_params=validated_params,
        )
