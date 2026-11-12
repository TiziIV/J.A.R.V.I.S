"""Descomposición en subtareas y motor de planificación (DAG) para N6.

Responsabilidades:
1. Convertir un `Goal` estructurado en un conjunto ordenado de `GoalTask`.
2. Verificar dependencias mediante ordenamiento topológico (algoritmo de Kahn)
   detectando ciclos, dependencias inexistentes y exceso de profundidad.
3. Validar límites de cantidad de subtareas (`max_tasks`) y profundidad (`max_depth`).
4. Garantizar mediante `GoalSkillSelector` que solo se utilicen skills existentes,
   respetando las restricciones del usuario y clasificando operaciones sensibles.
5. Mantener separación absoluta entre planificación y autorización: planificar jamás
   otorga permiso de ejecución automática sobre operaciones sensibles.
"""

from __future__ import annotations

from collections import deque
import json
import re
from typing import Any, Optional, TYPE_CHECKING

from src.brain.goal_models import (
    Goal,
    GoalEngineLimits,
    GoalPlan,
    GoalPlanningError,
    GoalPolicyViolationError,
    GoalStatus,
    GoalType,
    GoalTask,
    TaskRiskCategory,
    TaskStatus,
)
from src.brain.goal_skill_selector import GoalSkillSelector

if TYPE_CHECKING:
    from src.brain.brain_manager import BrainManager


def validate_plan_dag(
    tasks: list[GoalTask],
    limits: GoalEngineLimits,
) -> tuple[list[str], int]:
    """Valida el grafo dirigido acíclico (DAG) de subtareas con Kahn y cota de profundidad.

    Retorna `(orden_topologico, profundidad_maxima)` o lanza `GoalPlanningError` ante
    planes vacíos, ciclos, dependencias rotas o violaciones de límites.
    """
    if not tasks:
        raise GoalPlanningError("Un plan ejecutable debe contener al menos una subtarea.")

    if len(tasks) > limits.max_tasks:
        raise GoalPlanningError(
            f"El plan supera el límite máximo de subtareas ({len(tasks)} > {limits.max_tasks})."
        )

    task_map: dict[str, GoalTask] = {}
    for task in tasks:
        if task.task_id in task_map:
            raise GoalPlanningError(f"Identificador de tarea duplicado en el plan: '{task.task_id}'.")
        task_map[task.task_id] = task

    in_degree: dict[str, int] = {task.task_id: 0 for task in tasks}
    dependents: dict[str, list[str]] = {task.task_id: [] for task in tasks}

    for task in tasks:
        for dep_id in task.depends_on:
            if dep_id == task.task_id:
                raise GoalPlanningError(
                    f"Dependencia circular directa detectada: '{task.task_id}' depende de sí misma."
                )
            if dep_id not in task_map:
                raise GoalPlanningError(
                    f"La tarea '{task.task_id}' depende de una tarea inexistente '{dep_id}'."
                )
            dependents[dep_id].append(task.task_id)
            in_degree[task.task_id] += 1

    # Algoritmo de Kahn manteniendo el orden declarativo estable para desempates deterministas.
    ready_queue: deque[str] = deque(
        task.task_id for task in tasks if in_degree[task.task_id] == 0
    )
    topo_order: list[str] = []
    depths: dict[str, int] = {}

    while ready_queue:
        current_id = ready_queue.popleft()
        topo_order.append(current_id)
        current_task = task_map[current_id]
        if not current_task.depends_on:
            depths[current_id] = 1
        else:
            depths[current_id] = 1 + max(depths[dep] for dep in current_task.depends_on)

        if depths[current_id] > limits.max_depth:
            raise GoalPlanningError(
                f"El plan supera la profundidad máxima de dependencias permitida "
                f"({depths[current_id]} > {limits.max_depth})."
            )

        for child_id in dependents[current_id]:
            in_degree[child_id] -= 1
            if in_degree[child_id] == 0:
                ready_queue.append(child_id)

    if len(topo_order) != len(tasks):
        cyclic_ids = [tid for tid in task_map if tid not in set(topo_order)]
        raise GoalPlanningError(
            f"Se detectó una dependencia circular en las subtareas: {', '.join(cyclic_ids)}."
        )

    max_depth = max(depths.values()) if depths else 0
    return topo_order, max_depth


class TaskDecomposer:
    """Descompone un `Goal` en subtareas ejecutables y compatibles con `SkillManager`."""

    def __init__(
        self,
        *,
        skill_selector: Optional[GoalSkillSelector] = None,
        brain_manager: Optional["BrainManager"] = None,
        use_ai: bool = False,
    ) -> None:
        self.skill_selector: GoalSkillSelector = skill_selector or GoalSkillSelector()
        self.brain_manager: Optional["BrainManager"] = brain_manager
        self.use_ai: bool = bool(use_ai and brain_manager is not None)

    def decompose(self, goal: Goal, *, limits: GoalEngineLimits) -> list[GoalTask]:
        """Genera una lista ordenada de `GoalTask` validada contra políticas y límites."""
        if self.use_ai and self.brain_manager is not None:
            ai_tasks = self._try_ai_decomposition(goal, limits=limits)
            if ai_tasks:
                return ai_tasks
        return self._deterministic_decompose(goal, limits=limits)

    def _deterministic_decompose(
        self,
        goal: Goal,
        *,
        limits: GoalEngineLimits,
    ) -> list[GoalTask]:
        """Construye planes trazables sin depender de disponibilidad de internet ni LLM."""
        constraints = goal.constraints
        goal_type = goal.goal_type

        if goal_type == GoalType.OPTIMIZATION:
            return self._build_optimization_tasks(goal, limits)
        if goal_type == GoalType.DIAGNOSTIC:
            return self._build_diagnostic_tasks(goal, limits)
        if goal_type == GoalType.RESEARCH:
            return self._build_research_tasks(goal, limits)
        if goal_type == GoalType.DOCUMENTATION:
            return self._build_documentation_tasks(goal, limits)
        if goal_type == GoalType.AUTOMATION:
            return self._build_automation_tasks(goal, limits)

        # Objetivo general: inspecciona señales básicas del pedido y sintetiza respuesta.
        tasks: list[GoalTask] = []
        lowered = goal.raw_input.lower()
        deps: list[str] = []

        if any(tok in lowered for tok in ("hora", "fecha")):
            tasks.append(
                GoalTask(
                    task_id="t1_time",
                    title="Consultar fecha y hora actual del sistema",
                    description="Obtiene fecha y hora locales mediante system_time.",
                    skill_name="system_time",
                    skill_params={},
                    risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                    requires_approval=False,
                    depends_on=[],
                    expected_output="Fecha y hora del sistema.",
                    max_retries=limits.max_retries,
                    timeout_sec=limits.task_timeout_sec,
                )
            )
            deps.append("t1_time")

        if any(tok in lowered for tok in ("cpu", "ram", "memoria", "hardware", "pc", "sistema")) or not tasks:
            tid = f"t{len(tasks) + 1}_sysinfo"
            tasks.append(
                GoalTask(
                    task_id=tid,
                    title="Relevar métricas base del sistema",
                    description="Consulta CPU y memoria RAM libre sin modificar configuraciones.",
                    skill_name="system_info",
                    skill_params={},
                    risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                    requires_approval=False,
                    depends_on=[],
                    expected_output="Estado actual de CPU y memoria RAM.",
                    max_retries=limits.max_retries,
                    timeout_sec=limits.task_timeout_sec,
                )
            )
            deps.append(tid)

        if any(tok in lowered for tok in ("portapapeles", "clipboard")):
            tid = f"t{len(tasks) + 1}_clipboard"
            tasks.append(
                GoalTask(
                    task_id=tid,
                    title="Leer contenido del portapapeles",
                    description="Lectura segura del portapapeles en modo solo lectura.",
                    skill_name="clipboard_manager",
                    skill_params={"action": "read"},
                    risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                    requires_approval=False,
                    depends_on=[],
                    expected_output="Texto actual en el portapapeles.",
                    max_retries=limits.max_retries,
                    timeout_sec=limits.task_timeout_sec,
                )
            )
            deps.append(tid)

        tasks.append(
            GoalTask(
                task_id=f"t{len(tasks) + 1}_summary",
                title="Consolidar resultado para el objetivo",
                description=f"Integra los datos obtenidos para responder: {goal.raw_input}",
                skill_name=None,
                skill_params={},
                risk_category=TaskRiskCategory.SAFE_PLANNING,
                requires_approval=False,
                depends_on=deps,
                expected_output="Resumen accionable del objetivo.",
                max_retries=limits.max_retries,
                timeout_sec=limits.task_timeout_sec,
            )
        )
        return tasks

    def _build_optimization_tasks(self, goal: Goal, limits: GoalEngineLimits) -> list[GoalTask]:
        constraints = goal.constraints
        tasks: list[GoalTask] = [
            GoalTask(
                task_id="t1_diag_hw",
                title="Inspeccionar estado actual de CPU y memoria RAM",
                description="Releva uso actual de CPU y RAM libre antes de recomendar o preparar ajustes.",
                skill_name="system_info",
                skill_params={},
                risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                requires_approval=False,
                depends_on=[],
                expected_output="Lectura base de CPU y RAM del equipo.",
                max_retries=limits.max_retries,
                timeout_sec=limits.task_timeout_sec,
            )
        ]
        analysis_deps = ["t1_diag_hw"]

        urls = list(goal.context.extra.get("urls") or [])
        if urls and constraints.allow_network:
            tasks.append(
                GoalTask(
                    task_id="t2_web_ref",
                    title="Consultar guía técnica externa de referencia",
                    description="Extrae contenido estructurado no confiable de la URL provista.",
                    skill_name="web_extraction",
                    skill_params={"urls": urls},
                    risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                    requires_approval=False,
                    depends_on=[],
                    expected_output="Evidencia web estructurada encapsulada como no confiable.",
                    max_retries=limits.max_retries,
                    timeout_sec=limits.task_timeout_sec,
                )
            )
            analysis_deps.append("t2_web_ref")

        target_label = goal.context.target_app or "PC para gaming"
        tasks.append(
            GoalTask(
                task_id="t3_analyze_opt",
                title=f"Analizar cuello de botella y plan de optimización ({target_label})",
                description=(
                    "Cruza métricas de CPU/RAM con hardware conocido del contexto para priorizar "
                    "ajustes seguros (temperatura, procesos en segundo plano, VRAM/calidad gráfica)."
                ),
                skill_name=None,
                skill_params={},
                risk_category=TaskRiskCategory.SAFE_PLANNING,
                requires_approval=False,
                depends_on=analysis_deps,
                expected_output="Diagnóstico técnico y lista priorizada de optimizaciones.",
                max_retries=limits.max_retries,
                timeout_sec=limits.task_timeout_sec,
            )
        )

        # Solo se agrega tarea sensible de modificación si no hay restricción read-only y existe
        # un objetivo concreto (app/juego o archivo de configuración).
        has_target = bool(goal.context.target_app or goal.context.target_paths)
        if not constraints.read_only and constraints.allow_file_modifications and has_target:
            target_path = goal.context.target_paths[0] if goal.context.target_paths else None
            params: dict[str, Any] = (
                {"action": "modify", "file_path": target_path}
                if target_path
                else {"action": "discover", "app_name": goal.context.target_app}
            )
            tasks.append(
                GoalTask(
                    task_id="t4_prepare_apply_opt",
                    title=f"Preparar y aplicar optimización controlada ({goal.context.target_app or target_path})",
                    description=(
                        "Prepara un ChangePlan validado por PolicyEngine mediante ChainOrchestrator; "
                        "cualquier edición en disco exige confirmación explícita previa."
                    ),
                    skill_name="file_editor",
                    skill_params=params,
                    risk_category=TaskRiskCategory.SENSITIVE_OPERATION,
                    requires_approval=True,
                    depends_on=["t3_analyze_opt"],
                    expected_output="ChangePlan simulado, autorizado y aplicado con backup SHA-256.",
                    max_retries=0,  # Operaciones sensibles nunca se reintentan ciegamente sin nueva validación
                    timeout_sec=limits.task_timeout_sec,
                    metadata={
                        "use_chain_orchestrator": True,
                        "modifies_files": True,
                        "target_app": goal.context.target_app,
                        "file_path": target_path,
                    },
                )
            )
        return tasks

    def _build_diagnostic_tasks(self, goal: Goal, limits: GoalEngineLimits) -> list[GoalTask]:
        return [
            GoalTask(
                task_id="t1_cpu_ram",
                title="Medir uso de CPU y memoria RAM",
                description="Ejecuta diagnóstico de hardware local sin alterar archivos ni procesos.",
                skill_name="system_info",
                skill_params={},
                risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                requires_approval=False,
                depends_on=[],
                expected_output="Porcentaje de CPU y RAM disponible.",
                max_retries=limits.max_retries,
                timeout_sec=limits.task_timeout_sec,
            ),
            GoalTask(
                task_id="t2_timestamp",
                title="Registrar hora exacta del diagnóstico",
                description="Captura marca temporal del sistema para trazabilidad.",
                skill_name="system_time",
                skill_params={},
                risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                requires_approval=False,
                depends_on=[],
                expected_output="Fecha y hora local del relevamiento.",
                max_retries=limits.max_retries,
                timeout_sec=limits.task_timeout_sec,
            ),
            GoalTask(
                task_id="t3_report",
                title="Consolidar informe diagnóstico de solo lectura",
                description="Resume hallazgos técnicos respetando modo de solo lectura.",
                skill_name=None,
                skill_params={},
                risk_category=TaskRiskCategory.SAFE_PLANNING,
                requires_approval=False,
                depends_on=["t1_cpu_ram", "t2_timestamp"],
                expected_output="Informe de estado del sistema listo para el usuario.",
                max_retries=limits.max_retries,
                timeout_sec=limits.task_timeout_sec,
            ),
        ]

    def _build_research_tasks(self, goal: Goal, limits: GoalEngineLimits) -> list[GoalTask]:
        if not goal.constraints.allow_network:
            raise GoalPolicyViolationError(
                "El objetivo requiere investigación en red, pero está prohibido el acceso de red."
            )
        urls = list(goal.context.extra.get("urls") or [])
        tasks: list[GoalTask] = []
        if urls:
            tasks.append(
                GoalTask(
                    task_id="t1_extract_web",
                    title="Extraer fuentes web de forma segura",
                    description="Descarga y normaliza el contenido de las URLs verificando SSRF y aislamiento.",
                    skill_name="web_extraction",
                    skill_params={"urls": urls},
                    risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                    requires_approval=False,
                    depends_on=[],
                    expected_output="Sobre estructurado de datos externos no confiables.",
                    max_retries=limits.max_retries,
                    timeout_sec=limits.task_timeout_sec,
                )
            )
        else:
            tasks.append(
                GoalTask(
                    task_id="t1_search_web",
                    title="Ejecutar búsqueda web acotada",
                    description="Realiza consulta de búsqueda web según el objetivo solicitado.",
                    skill_name="web_search",
                    skill_params={"query": goal.raw_input, "platform": "google"},
                    risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                    requires_approval=False,
                    depends_on=[],
                    expected_output="Confirmación de búsqueda iniciada.",
                    max_retries=limits.max_retries,
                    timeout_sec=limits.task_timeout_sec,
                )
            )

        tasks.append(
            GoalTask(
                task_id="t2_synthesize_research",
                title="Sintetizar evidencia obtenida",
                description="Consolida la evidencia recopilada aislando cualquier instrucción externa.",
                skill_name=None,
                skill_params={},
                risk_category=TaskRiskCategory.SAFE_PLANNING,
                requires_approval=False,
                depends_on=[tasks[0].task_id],
                expected_output="Síntesis verificable para el usuario.",
                max_retries=limits.max_retries,
                timeout_sec=limits.task_timeout_sec,
            )
        )
        return tasks

    def _build_documentation_tasks(self, goal: Goal, limits: GoalEngineLimits) -> list[GoalTask]:
        if goal.constraints.read_only or not goal.constraints.allow_file_modifications:
            raise GoalPolicyViolationError(
                "No se puede planificar exportación de archivos bajo restricción de solo lectura."
            )
        doc_format = "docx" if "docx" in goal.raw_input.lower() or "word" in goal.raw_input.lower() else "markdown"
        mode = "study" if "estudio" in goal.raw_input.lower() else "general"
        return [
            GoalTask(
                task_id="t1_gather_data",
                title="Recopilar contexto técnico del sistema",
                description="Releva estado de CPU y RAM para incorporar datos reales al informe.",
                skill_name="system_info",
                skill_params={},
                risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                requires_approval=False,
                depends_on=[],
                expected_output="Métricas del sistema para el documento.",
                max_retries=limits.max_retries,
                timeout_sec=limits.task_timeout_sec,
            ),
            GoalTask(
                task_id="t2_draft_doc",
                title="Estructurar borrador del documento",
                description="Organiza título, secciones y contenido antes de exportar a disco.",
                skill_name=None,
                skill_params={},
                risk_category=TaskRiskCategory.SAFE_PLANNING,
                requires_approval=False,
                depends_on=["t1_gather_data"],
                expected_output="Borrador estructurado listo para exportación.",
                max_retries=limits.max_retries,
                timeout_sec=limits.task_timeout_sec,
            ),
            GoalTask(
                task_id="t3_export_doc",
                title=f"Exportar documento en formato {doc_format}",
                description="Escribe el documento en raíz autorizada previa aprobación explícita.",
                skill_name="document_export",
                skill_params={
                    "title": "Informe J.A.R.V.I.S.",
                    "content": f"# Informe de Objetivo\n\nObjetivo: {goal.raw_input}\n",
                    "format": doc_format,
                    "mode": mode,
                },
                risk_category=TaskRiskCategory.SENSITIVE_OPERATION,
                requires_approval=True,
                depends_on=["t2_draft_doc"],
                expected_output="Archivo exportado de forma atómica en exports/.",
                max_retries=0,
                timeout_sec=limits.task_timeout_sec,
                metadata={"modifies_files": True},
            ),
        ]

    def _build_automation_tasks(self, goal: Goal, limits: GoalEngineLimits) -> list[GoalTask]:
        constraints = goal.constraints
        if goal.context.target_paths or goal.context.target_app:
            if constraints.read_only or not constraints.allow_file_modifications:
                raise GoalPolicyViolationError(
                    "El objetivo solicita edición pero las restricciones prohíben modificar archivos."
                )
            target_path = goal.context.target_paths[0] if goal.context.target_paths else None
            params: dict[str, Any] = (
                {"action": "modify", "file_path": target_path}
                if target_path
                else {"action": "discover", "app_name": goal.context.target_app}
            )
            return [
                GoalTask(
                    task_id="t1_inspect_context",
                    title="Verificar estado del sistema previo al cambio",
                    description="Consulta métricas locales antes de preparar el ChangePlan.",
                    skill_name="system_info",
                    skill_params={},
                    risk_category=TaskRiskCategory.LOW_RISK_EXECUTION,
                    requires_approval=False,
                    depends_on=[],
                    expected_output="Estado base del sistema.",
                    max_retries=limits.max_retries,
                    timeout_sec=limits.task_timeout_sec,
                ),
                GoalTask(
                    task_id="t2_chain_edit",
                    title=f"Preparar y ejecutar cambio seguro en {target_path or goal.context.target_app}",
                    description="Delega validación, simulación, aprobación y backup a ChainOrchestrator.",
                    skill_name="file_editor",
                    skill_params=params,
                    risk_category=TaskRiskCategory.SENSITIVE_OPERATION,
                    requires_approval=True,
                    depends_on=["t1_inspect_context"],
                    expected_output="Cambio validado por PolicyEngine y aplicado con respaldo.",
                    max_retries=0,
                    timeout_sec=limits.task_timeout_sec,
                    metadata={
                        "use_chain_orchestrator": True,
                        "modifies_files": True,
                        "target_app": goal.context.target_app,
                        "file_path": target_path,
                    },
                ),
            ]

        lowered = goal.raw_input.lower()
        if "protocolo" in lowered:
            mode_val = "diagnostico"
            for candidate in ("trabajo", "diagnostico", "silencio"):
                if candidate in lowered:
                    mode_val = candidate
                    break
            return [
                GoalTask(
                    task_id="t1_protocol",
                    title=f"Activar protocolo '{mode_val}'",
                    description="Ejecuta protocolo de entorno previa aprobación explícita.",
                    skill_name="activate_protocol",
                    skill_params={"mode": mode_val},
                    risk_category=TaskRiskCategory.SENSITIVE_OPERATION,
                    requires_approval=True,
                    depends_on=[],
                    expected_output=f"Protocolo '{mode_val}' activado.",
                    max_retries=0,
                    timeout_sec=limits.task_timeout_sec,
                )
            ]

        raise GoalPlanningError(
            "No fue posible descomponer la automatización sin especificar archivo o acción registrada compatible."
        )

    def _try_ai_decomposition(
        self,
        goal: Goal,
        *,
        limits: GoalEngineLimits,
    ) -> Optional[list[GoalTask]]:
        """Solicita propuesta estructurada a Multi-Brain y valida estrictamente cada tarea."""
        assert self.brain_manager is not None
        manifest = self.skill_selector.get_manifest_summary()
        allowed_names = [item["name"] for item in manifest]
        prompt = (
            "Propón un plan en formato JSON (lista de subtareas, máximo "
            f"{limits.max_tasks} tareas, profundidad máxima {limits.max_depth}). "
            "Cada elemento debe tener: task_id, title, description, skill_name (null o una de "
            f"{allowed_names}), skill_params (objeto), depends_on (lista de task_id), expected_output.\n"
            f"Objetivo: {goal.raw_input}\n"
            f"Restricciones: read_only={goal.constraints.read_only}, "
            f"allow_file_modifications={goal.constraints.allow_file_modifications}, "
            f"allow_network={goal.constraints.allow_network}."
        )
        response_text, _provider, _model, _fallback, failed = self.brain_manager.query_raw(
            prompt,
            system_prompt=(
                "Eres el planificador técnico de J.A.R.V.I.S. "
                "Devuelve exclusivamente un array JSON válido de subtareas sin explicaciones."
            ),
            voice_mode=False,
        )
        if failed or not response_text:
            return None

        cleaned = response_text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.DOTALL).strip()
        try:
            raw_list = json.loads(cleaned)
        except Exception:
            return None
        if not isinstance(raw_list, list) or not raw_list:
            return None

        candidate_tasks: list[GoalTask] = []
        try:
            for idx, item in enumerate(raw_list, start=1):
                if not isinstance(item, dict):
                    return None
                skill_name = item.get("skill_name")
                if skill_name is not None:
                    skill_name = str(skill_name).strip() or None
                task = GoalTask(
                    task_id=str(item.get("task_id") or f"t{idx}"),
                    title=str(item.get("title") or f"Subtarea {idx}"),
                    description=str(item.get("description") or ""),
                    skill_name=skill_name,
                    skill_params=dict(item.get("skill_params") or {}),
                    risk_category=self.skill_selector.classify_skill_risk(
                        skill_name, dict(item.get("skill_params") or {})
                    ),
                    requires_approval=False,
                    depends_on=[str(d) for d in (item.get("depends_on") or [])],
                    expected_output=str(item.get("expected_output") or "Resultado verificado."),
                    max_retries=limits.max_retries,
                    timeout_sec=limits.task_timeout_sec,
                    metadata={"proposed_by_ai": True},
                )
                self.skill_selector.validate_task(task, goal.constraints)
                candidate_tasks.append(task)
            validate_plan_dag(candidate_tasks, limits)
            return candidate_tasks
        except Exception:
            # Si el modelo propuso skills inventadas, ciclos o violaciones de política,
            # el motor descarta la propuesta y recurre al plan determinista seguro.
            return None


class GoalPlanningEngine:
    """Construye, valida, hashea y serializa planes de ejecución completos (`GoalPlan`)."""

    def __init__(
        self,
        *,
        skill_selector: Optional[GoalSkillSelector] = None,
        decomposer: Optional[TaskDecomposer] = None,
        default_limits: Optional[GoalEngineLimits] = None,
    ) -> None:
        self.skill_selector: GoalSkillSelector = skill_selector or GoalSkillSelector()
        self.decomposer: TaskDecomposer = decomposer or TaskDecomposer(
            skill_selector=self.skill_selector
        )
        self.default_limits: GoalEngineLimits = default_limits or GoalEngineLimits()

    def build_plan(
        self,
        goal: Goal,
        *,
        limits: Optional[GoalEngineLimits] = None,
        custom_tasks: Optional[list[GoalTask]] = None,
    ) -> GoalPlan:
        """Genera y valida un `GoalPlan` listo para revisión humana o ejecución segura."""
        effective_limits = limits or self.default_limits

        if goal.needs_clarification or goal.status == GoalStatus.AWAITING_CLARIFICATION:
            questions = "; ".join(goal.clarification_questions) or "Se requiere mayor detalle."
            raise GoalPlanningError(
                f"El objetivo requiere aclaración del usuario antes de planificarse: {questions}"
            )

        if custom_tasks is not None:
            tasks = list(custom_tasks)
        else:
            tasks = self.decomposer.decompose(goal, limits=effective_limits)

        # Validación obligatoria de cada subtarea contra SkillManager y restricciones.
        for task in tasks:
            task.max_retries = min(task.max_retries, effective_limits.max_retries)
            task.timeout_sec = min(task.timeout_sec, effective_limits.task_timeout_sec)
            self.skill_selector.validate_task(task, goal.constraints)

        topo_order, max_depth = validate_plan_dag(tasks, effective_limits)

        # Normaliza orden topológico e inicializa estados de preparación sin otorgar aprobación.
        task_by_id = {task.task_id: task for task in tasks}
        ordered_tasks = [task_by_id[tid] for tid in topo_order]

        for task in ordered_tasks:
            if not task.depends_on:
                target_status = (
                    TaskStatus.REQUIRES_APPROVAL
                    if task.requires_approval
                    else TaskStatus.READY
                )
                if task.status == TaskStatus.PENDING:
                    task.transition_to(target_status)

        if goal.status == GoalStatus.DRAFT:
            goal.transition_to(GoalStatus.PLANNED)
        has_sensitive = any(
            task.requires_approval or task.risk_category == TaskRiskCategory.SENSITIVE_OPERATION
            for task in ordered_tasks
        )
        if has_sensitive and goal.status == GoalStatus.PLANNED:
            goal.transition_to(GoalStatus.AWAITING_APPROVAL)

        plan = GoalPlan(
            goal=goal,
            tasks=ordered_tasks,
            limits=effective_limits,
        )
        plan.refresh_hash()
        plan.record_event(
            "plan_created",
            (
                f"Plan generado con {len(ordered_tasks)} subtarea(s), profundidad {max_depth}. "
                + (
                    "Contiene operaciones sensibles pendientes de aprobación explícita."
                    if has_sensitive
                    else "Solo contiene acciones de bajo riesgo / planificación."
                )
            ),
            to_status=goal.status.value,
            details={"topo_order": topo_order, "max_depth": max_depth},
        )
        return plan
