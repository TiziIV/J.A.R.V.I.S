"""
Adaptadores N5/N6 y coordinador `ExperienceCaptureService` para N8.3 Subfase 2.

Justificación técnica junior:
1. N5 (`ChainOrchestrator`) y N6 (`ExecutionSupervisor`) nunca escriben directamente en SQLite ni
   dependen de detalles internos de almacenamiento de N8.
2. `N5ExecutionEvidenceAdapter` y `N6GoalEvidenceAdapter` traducen únicamente resultados
   estructuralmente verificados en el sistema real (hashes SHA-256 post-escritura, aprobaciones,
   verificadores objetivos de skills/orquestador y trazas de plan), rechazando estados
   declarativos sueltos (`COMPLETED`), `internal_synthesis` o afirmaciones de un modelo LLM.
3. `ExperienceCaptureService` valida políticas (`N8_ENABLED`, `N8_AUTO_CAPTURE_ENABLED`),
   privacidad en el punto efectivo de escritura, bloquea ejecuciones ya revertidas por rollback,
   y aísla fallos de N8 para no alterar jamás el resultado ni el flujo crítico de N5 o N6.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Mapping, Sequence, TYPE_CHECKING

from config import settings
from src.brain.exceptions import redact_secrets
from src.memory.capture_contracts import (
    AdaptationReport,
    CAPTURE_CONTRACT_VERSION,
    CaptureBatchResult,
    CaptureUnitOutcome,
    FORBIDDEN_VERIFIER_MARKERS,
    MemoryEvidenceContractError,
    RollbackInvalidationOutcome,
    VerifiedExecutionEvidenceContract,
    VerifiedOperationReference,
)
from src.memory.models import (
    MemoryCategory,
    MemoryCorruptionError,
    MemoryCreateInput,
    MemoryIdempotencyConflictError,
    MemoryLockTimeoutError,
    MemoryPersistenceError,
    MemoryPolicyError,
    MemoryPrivateModeError,
    MemoryScopeType,
    MemorySource,
    MemoryStatus,
    MemoryValidationError,
    is_valid_sha256_hex,
    parse_iso_utc,
    utc_now_iso,
)
from src.memory.policy import assert_no_secrets, normalize_scope

if TYPE_CHECKING:
    from src.memory.engine import MemoryEngine


logger = logging.getLogger("jarvis.memory.capture")

_SAFE_SCOPE_RE: re.Pattern[str] = re.compile(r"[^a-z0-9_.:-]+")


def _normalize_scope_key_from_label(raw: str | None, *, fallback: str) -> str:
    clean = (raw or "").strip().lower()
    if not clean or clean in {"sistema", "default", "aplicación", "aplicacion"}:
        return fallback
    normalized = _SAFE_SCOPE_RE.sub("_", clean).strip("_.:-")
    return (normalized or fallback)[:80]


def _resolve_default_n5_scope(
    application_name: str | None,
    *,
    scope_type: MemoryScopeType | None = None,
    scope_key: str | None = None,
) -> tuple[MemoryScopeType, str]:
    if scope_type is not None:
        eff_key = scope_key or ("global" if scope_type == MemoryScopeType.GLOBAL else (application_name or "default"))
        return normalize_scope(scope_type, eff_key)
    norm_app = _normalize_scope_key_from_label(application_name, fallback="")
    if norm_app:
        return normalize_scope(MemoryScopeType.APP_OR_GAME, norm_app)
    return normalize_scope(MemoryScopeType.PROJECT, "jarvis-core")


def _resolve_default_n6_scope(
    target_app: str | None,
    *,
    scope_type: MemoryScopeType | None = None,
    scope_key: str | None = None,
) -> tuple[MemoryScopeType, str]:
    if scope_type is not None:
        eff_key = scope_key or ("global" if scope_type == MemoryScopeType.GLOBAL else (target_app or "jarvis-core"))
        return normalize_scope(scope_type, eff_key)
    norm_app = _normalize_scope_key_from_label(target_app, fallback="")
    if norm_app:
        return normalize_scope(MemoryScopeType.APP_OR_GAME, norm_app)
    return normalize_scope(MemoryScopeType.PROJECT, "jarvis-core")


def _safe_artifact_label(file_path: str) -> str:
    """Devuelve solo el nombre del archivo o etiqueta segura sin exponer texto libre ni rutas UNC."""
    clean = (file_path or "").strip()
    if not clean:
        return "recurso_verificado"
    try:
        return Path(clean).name or clean
    except Exception:
        return clean


# ------------------------------------------------------------------------------
# Adaptador N5 (`ChainOrchestrator` + `TransactionManager`)
# ------------------------------------------------------------------------------


class N5ExecutionEvidenceAdapter:
    """
    Traduce ejecuciones reales confirmadas de `ChainOrchestrator` a contratos verificados N8.

    Rechaza de forma cerrada cualquier ejecución sin verificación post-escritura, sin `approval_hash`
    coincidente, con operaciones pendientes/fallidas inconsistentes, con evidencia de otra ejecución
    o con rastro de rollback.
    """

    def adapt_execution(
        self,
        record: Any,
        *,
        summary: Mapping[str, Any] | None = None,
        transaction_records: Sequence[Any] | None = None,
        scope_type: MemoryScopeType | None = None,
        scope_key: str | None = None,
    ) -> list[VerifiedExecutionEvidenceContract]:
        """Valida estructuralmente un `ChainExecutionRecord` N5 y emite contratos por operación aplicada."""
        from src.brain.chain_orchestrator import (
            ChainExecutionRecord,
            ChainState,
            ChangePlan,
            OperationStatus,
        )
        from src.brain.transaction_manager import TransactionState

        if not isinstance(record, ChainExecutionRecord):
            raise MemoryEvidenceContractError(
                "El adaptador N5 requiere una instancia real de ChainExecutionRecord; "
                "no se aceptan diccionarios declarativos aislados."
            )

        exec_id = (record.execution_id or "").strip()
        if not exec_id:
            raise MemoryEvidenceContractError("ChainExecutionRecord carece de execution_id válido.")

        if record.state == ChainState.ROLLED_BACK or record.rollback_summary is not None:
            raise MemoryEvidenceContractError(
                f"La ejecución N5 '{exec_id}' ya fue revertida o registra rollback_summary."
            )
        if any(str(st) == ChainState.ROLLED_BACK.value for st, _ in (record.state_history or [])):
            raise MemoryEvidenceContractError(
                f"La ejecución N5 '{exec_id}' contiene transición histórica a ROLLED_BACK."
            )
        if record.state != ChainState.COMPLETED:
            raise MemoryEvidenceContractError(
                f"La ejecución N5 '{exec_id}' no está en estado final COMPLETED (actual: {record.state.value})."
            )

        plan = record.plan
        if not isinstance(plan, ChangePlan):
            raise MemoryEvidenceContractError(
                f"La ejecución N5 '{exec_id}' carece de ChangePlan vinculado."
            )
        if plan.execution_id != exec_id or record.approved_plan_id != plan.plan_id:
            raise MemoryEvidenceContractError(
                f"Inconsistencia de identificadores entre record y plan para '{exec_id}'."
            )
        if plan.status not in {"COMPLETED", "PARTIALLY_APPLIED"}:
            raise MemoryEvidenceContractError(
                f"Estado de ChangePlan incompatible para captura ({plan.status!r})."
            )

        # Verificación criptográfica del plan aprobado contra su instantánea y approval_hash.
        if not is_valid_sha256_hex(record.approval_hash):
            raise MemoryEvidenceContractError(
                f"La ejecución N5 '{exec_id}' carece de approval_hash SHA-256 válido."
            )
        expected_plan_hash = plan.approval_hash()
        if expected_plan_hash != (record.approval_hash or "").lower():
            raise MemoryEvidenceContractError(
                f"El approval_hash de '{exec_id}' no coincide con el contenido canónico del ChangePlan."
            )
        if record.approved_plan_snapshot is not None:
            try:
                current_snap = json.loads(plan.canonical_json())
            except Exception as exc:
                raise MemoryEvidenceContractError(f"Error serializando plan '{plan.plan_id}': {exc}") from exc
            if current_snap != record.approved_plan_snapshot:
                raise MemoryEvidenceContractError(
                    f"El ChangePlan '{plan.plan_id}' difiere de approved_plan_snapshot."
                )

        # Validación contra TransactionManager cuando las transacciones están disponibles.
        verifier_component = "ChainOrchestratorVerifier"
        if transaction_records is not None:
            if not transaction_records:
                raise MemoryEvidenceContractError(
                    f"No existen TransactionRecord verificados para '{exec_id}'."
                )
            has_committed_tx = False
            tx_ops_by_id: dict[str, Any] = {}
            for tx in transaction_records:
                if getattr(tx, "execution_id", None) != exec_id:
                    raise MemoryEvidenceContractError(
                        f"TransactionRecord pertenece a otra ejecución ('{getattr(tx, 'execution_id', None)}' != '{exec_id}')."
                    )
                if getattr(tx, "approval_hash", None) != record.approval_hash:
                    raise MemoryEvidenceContractError(
                        f"TransactionRecord de '{exec_id}' posee approval_hash inconsistente."
                    )
                tx_state = str(getattr(tx, "state", ""))
                if tx_state in {
                    TransactionState.ROLLED_BACK.value,
                    TransactionState.ROLLING_BACK.value,
                    TransactionState.FAILED.value,
                    TransactionState.RECOVERY_FAILED.value,
                    TransactionState.RECOVERY_REQUIRED.value,
                }:
                    raise MemoryEvidenceContractError(
                        f"Transacción N5 de '{exec_id}' en estado incompatible para captura ({tx_state})."
                    )
                if tx_state in {
                    TransactionState.COMMITTED.value,
                    TransactionState.PARTIALLY_APPLIED.value,
                }:
                    has_committed_tx = True
                if getattr(tx, "recovery_attempts", None):
                    verifier_component = "TransactionRecoveryVerifier"
                for op_id_k, op_entry in (getattr(tx, "operations", {}) or {}).items():
                    tx_ops_by_id[str(op_id_k)] = op_entry
            if not has_committed_tx:
                raise MemoryEvidenceContractError(
                    f"Ninguna transacción de '{exec_id}' alcanzó COMMITTED ni PARTIALLY_APPLIED."
                )
        else:
            tx_ops_by_id = {}

        if not record.applied_operations:
            raise MemoryEvidenceContractError(
                f"La ejecución N5 '{exec_id}' figura COMPLETED pero no tiene operaciones aplicadas."
            )
        if not record.verification_results:
            raise MemoryEvidenceContractError(
                f"La ejecución N5 '{exec_id}' carece de verification_results post-escritura."
            )

        # Revisar que no haya operaciones fallidas ni revertidas ni verificaciones de otra ejecución.
        for op in plan.operations:
            if op.status in {
                OperationStatus.FAILED.value,
                OperationStatus.ROLLBACK_FAILED.value,
                OperationStatus.ROLLED_BACK.value,
            }:
                raise MemoryEvidenceContractError(
                    f"El plan '{plan.plan_id}' contiene operación en estado '{op.status}' ({op.id})."
                )

        if summary is not None:
            if not isinstance(summary, Mapping):
                raise MemoryEvidenceContractError("El summary N5 debe ser un Mapping.")
            if summary.get("execution_id") != exec_id or summary.get("plan_id") != plan.plan_id:
                raise MemoryEvidenceContractError(
                    "El resumen N5 pertenece a otra ejecución o plan distinto."
                )
            if summary.get("state") != ChainState.COMPLETED.value:
                raise MemoryEvidenceContractError(
                    f"El resumen N5 no confirma estado COMPLETED ({summary.get('state')!r})."
                )
            sum_verifs = summary.get("verifications")
            if not isinstance(sum_verifs, Sequence) or not sum_verifs:
                raise MemoryEvidenceContractError("El resumen N5 carece de verificaciones post-write.")
            if summary.get("fully_completed") is True and not plan.is_fully_completed():
                raise MemoryEvidenceContractError(
                    "El resumen N5 declara fully_completed=True pero existen operaciones sin evidencia válida en el plan."
                )

        # Validar que todos los backups registrados pertenezcan a esta ejecución.
        backup_by_path: dict[str, Any] = {}
        for bkp in record.applied_backups:
            if getattr(bkp, "execution_id", None) != exec_id:
                raise MemoryEvidenceContractError(
                    f"BackupRecord de '{exec_id}' pertenece a otra ejecución ({getattr(bkp, 'execution_id', None)})."
                )
            b_path = str(getattr(bkp, "backup_path", "") or "").strip()
            if b_path:
                backup_by_path[b_path] = bkp

        # Validar tabla de verificaciones post-escritura.
        verif_by_op: dict[str, Mapping[str, Any]] = {}
        for v in record.verification_results:
            if not isinstance(v, Mapping):
                raise MemoryEvidenceContractError("Entrada inválida en verification_results.")
            v_exec = v.get("execution_id")
            if v_exec is not None and str(v_exec).strip() != exec_id:
                raise MemoryEvidenceContractError(
                    f"Evidencia de verificación pertenece a otra ejecución ({v_exec!r} != {exec_id!r})."
                )
            op_id_v = str(v.get("operation_id") or "").strip()
            if not op_id_v:
                raise MemoryEvidenceContractError("Entrada en verification_results sin operation_id.")
            if v.get("verified") is not True:
                raise MemoryEvidenceContractError(
                    f"La operación N5 '{op_id_v}' no tiene verified=True tras verificación post-escritura."
                )
            if not is_valid_sha256_hex(v.get("sha256")):
                raise MemoryEvidenceContractError(
                    f"La operación N5 '{op_id_v}' carece de SHA-256 verificable en verification_results."
                )
            verif_by_op[op_id_v] = v

        # Calcular hash final por archivo para soportar cadenas H0 -> H1 -> H2 sobre un mismo archivo.
        op_by_id = {op.id: op for op in plan.operations}
        bound_set = set(record.bound_operation_ids) if record.bound_operation_ids is not None else None
        last_hash_for_file: dict[str, str] = {}
        applied_op_ids: list[str] = []
        for entry in record.applied_operations:
            if not isinstance(entry, Mapping):
                raise MemoryEvidenceContractError("Entrada inválida en applied_operations.")
            op_id = str(entry.get("operation_id") or "").strip()
            fpath = str(entry.get("file_path") or "").strip()
            new_hash = str(entry.get("new_hash") or "").strip().lower()
            if not op_id or not fpath or not is_valid_sha256_hex(new_hash):
                raise MemoryEvidenceContractError(
                    f"Operación aplicada incompleta o sin SHA-256 en applied_operations ({op_id!r})."
                )
            applied_op_ids.append(op_id)
            last_hash_for_file[fpath] = new_hash

        # Si hay selección aprobada (`bound_operation_ids`), toda operación seleccionada debe figurar aplicada y verificada,
        # y ninguna operación no seleccionada debe colarse en applied_operations.
        if bound_set is not None:
            for applied_id in applied_op_ids:
                if applied_id not in bound_set:
                    raise MemoryEvidenceContractError(
                        f"Operación aplicada '{applied_id}' fuera de bound_operation_ids."
                    )
            for bound_id in bound_set:
                if bound_id not in applied_op_ids:
                    raise MemoryEvidenceContractError(
                        f"Operación aprobada '{bound_id}' propuesta pero no aplicada efectivamente."
                    )

        resolved_scope_type, resolved_scope_key = _resolve_default_n5_scope(
            plan.application_name,
            scope_type=scope_type,
            scope_key=scope_key,
        )
        app_label = _normalize_scope_key_from_label(plan.application_name, fallback="aplicacion")

        contracts: list[VerifiedExecutionEvidenceContract] = []
        for entry in record.applied_operations:
            op_id = str(entry["operation_id"]).strip()
            op = op_by_id.get(op_id)
            if op is None:
                raise MemoryEvidenceContractError(
                    f"Operación aplicada '{op_id}' no existe en el ChangePlan aprobado '{plan.plan_id}'."
                )
            if op.status != OperationStatus.APPLIED.value or not op._execution_evidence_validated:
                raise MemoryEvidenceContractError(
                    f"Operación '{op_id}' no posee estado APPLIED con evidencia validada interna."
                )
            new_hash = str(entry["new_hash"]).strip().lower()
            orig_hash = str(entry.get("original_hash") or "").strip().lower()
            if not is_valid_sha256_hex(orig_hash):
                raise MemoryEvidenceContractError(
                    f"Operación '{op_id}' carece de original_hash SHA-256 válido."
                )
            if (op.executed_file_hash or "").lower() != new_hash:
                raise MemoryEvidenceContractError(
                    f"Discrepancia SHA-256 en '{op_id}': executed_file_hash != applied_operations.new_hash."
                )
            if not any(item[1] == OperationStatus.APPLIED.value for item in (op.status_history or [])):
                raise MemoryEvidenceContractError(
                    f"Operación '{op_id}' carece de transición histórica verificada hacia APPLIED."
                )

            verif = verif_by_op.get(op_id)
            if verif is None:
                raise MemoryEvidenceContractError(
                    f"La operación aplicada '{op_id}' carece de verificación post-escritura en verification_results."
                )
            fpath = str(entry["file_path"]).strip()
            verif_sha = str(verif["sha256"]).strip().lower()
            verif_path = str(verif.get("file_path") or fpath).strip()
            expected_chain_hash = last_hash_for_file.get(fpath, new_hash)
            if verif_sha not in {new_hash, expected_chain_hash}:
                raise MemoryEvidenceContractError(
                    f"Hash de verificación post-write en '{op_id}' no coincide con el hash ejecutado."
                )
            if verif_path and Path(verif_path).name != Path(fpath).name:
                raise MemoryEvidenceContractError(
                    f"Archivo verificado para '{op_id}' difiere del archivo aplicado."
                )

            if op_id in tx_ops_by_id:
                tx_op = tx_ops_by_id[op_id]
                if str(getattr(tx_op, "status", "")) != "APPLIED" or (
                    str(getattr(tx_op, "actual_after_sha256", "") or "").lower() != new_hash
                ):
                    raise MemoryEvidenceContractError(
                        f"El TransactionRecord para '{op_id}' diverge de la ejecución aplicada."
                    )

            bkp_path = str(entry.get("backup_path") or "").strip()
            if backup_by_path and bkp_path and bkp_path not in backup_by_path:
                # Comprobamos por ruta resuelta en Windows por si difiere casing/barra.
                resolved_bkp_map = {str(Path(k).resolve()): v for k, v in backup_by_path.items()}
                if str(Path(bkp_path).resolve()) not in resolved_bkp_map:
                    raise MemoryEvidenceContractError(
                        f"Backup declarado para '{op_id}' no figura en applied_backups verificados."
                    )

            verified_ts = ""
            for _prev_s, target_s, ts_val, _reason in reversed(op.status_history or []):
                if target_s == OperationStatus.APPLIED.value and ts_val:
                    verified_ts = str(ts_val).strip()
                    break
            if not verified_ts:
                verified_ts = str(plan.created_at or utc_now_iso()).strip()
            parse_iso_utc(verified_ts, field_name="verified_at")

            safe_file = _safe_artifact_label(fpath)
            unit_id = f"n5:{plan.plan_id}:{op.id}:{op.operation_type}"[:128]
            op_ref = VerifiedOperationReference(
                ref_id=op.id,
                operation_type=op.operation_type,
                target_ref=fpath,
                verification_status="VERIFIED",
                before_sha256=orig_hash,
                after_sha256=new_hash,
            )

            key_hint = f"[{op.section}] {op.key}" if op.section and op.key else (op.key or safe_file)
            assert_no_secrets(key_hint, field_name="operation.target_key")

            title = f"Cambio verificado N5 ({op.operation_type}) en {safe_file}"[:120]
            content = (
                f"Ejecución N5 '{exec_id}' (plan '{plan.plan_id}') aplicó y verificó "
                f"la operación '{op.id}' ({op.operation_type}) sobre '{safe_file}' "
                f"[{key_hint}] para '{app_label}' con hash SHA-256 post-escritura {new_hash[:16]}."
            )[:900]
            summary_txt = (
                f"Operación '{op.id}' ({op.operation_type}) verificada en '{safe_file}' "
                f"(sha256:{new_hash[:12]})."
            )[:280]

            contract = VerifiedExecutionEvidenceContract(
                contract_version=CAPTURE_CONTRACT_VERSION,
                source_layer="N5_CHAIN_ORCHESTRATOR",
                source_execution_id=exec_id,
                canonical_unit_id=unit_id,
                source_goal_id=f"n5_plan:{plan.plan_id}",
                source_plan_id=plan.plan_id,
                source_task_id=op.id,
                observed_final_state=ChainState.COMPLETED.value,
                verification_outcome="verified",
                verifier_component=verifier_component,
                executor_component="FileEditorSkill",
                operation_type=op.operation_type,
                expected_outcome=f"Operación {op.operation_type} en {safe_file} aplicada y verificada por SHA-256.",
                fact_title=title,
                fact_content=content,
                fact_summary=summary_txt,
                approval_hash=record.approval_hash,
                verified_operation_refs=(op_ref,),
                verified_artifacts=(fpath,),
                structured_evidence={
                    "execution_id": exec_id,
                    "plan_id": plan.plan_id,
                    "plan_status": plan.status,
                    "operation_id": op.id,
                    "operation_type": op.operation_type,
                    "approval_hash": record.approval_hash or "",
                    "applied_count": str(len(record.applied_operations)),
                    "verified_count": str(len(record.verification_results)),
                    "post_edit_sha256": new_hash,
                    "rollback_present": "false",
                },
                verified_at=verified_ts,
                scope_type=resolved_scope_type,
                scope_key=resolved_scope_key,
            )
            contracts.append(contract)

        return contracts


# ------------------------------------------------------------------------------
# Adaptador N6 (`GoalPlan` + `GoalTask` + `ExecutionResult`)
# ------------------------------------------------------------------------------


class N6GoalEvidenceAdapter:
    """
    Traduce subtareas verificadas de planes N6 (`GoalPlan`) a contratos verificados N8.

    Reglas estrictas:
    - En objetivos `COMPLETED`: requiere al menos una subtarea con evidencia objetiva y verificador
      autorizado; jamás captura un `COMPLETED` vacío o meramente declarativo.
    - En objetivos `PARTIAL`: captura únicamente subtareas individuales verificadas, nunca un hecho
      que declare éxito global del objetivo ni tareas fallidas/pendientes/canceladas/bloqueadas.
    - Rechaza síntesis interna (`internal_synthesis`), afirmaciones de LLM sin verificador objetivo,
      `{"verified": True}` aislado o evidencia perteneciente a otra tarea/objetivo.
    """

    @staticmethod
    def _validate_completed_task_evidence(
        plan: Any,
        task: Any,
        *,
        scope_type: MemoryScopeType,
        scope_key: str,
    ) -> VerifiedExecutionEvidenceContract:
        from src.brain.goal_models import GoalStatus, TaskStatus

        ev = task.execution_evidence
        if not isinstance(ev, Mapping) or not ev:
            raise MemoryEvidenceContractError(
                f"La tarea N6 '{task.task_id}' carece de execution_evidence estructurada."
            )

        ev_kind = str(ev.get("kind") or "").strip()
        if ev_kind == "internal_synthesis":
            raise MemoryEvidenceContractError(
                f"La tarea '{task.task_id}' es internal_synthesis sin verificador objetivo; no se captura."
            )
        if ev_kind not in {"skill_execution", "chain_orchestrator"}:
            raise MemoryEvidenceContractError(
                f"La tarea '{task.task_id}' tiene kind de evidencia no verificable ({ev_kind!r})."
            )

        if "verified" in ev and len(ev) <= 2 and "verifier_component" not in ev:
            raise MemoryEvidenceContractError(
                f"Evidencia fabricada con booleano aislado rechazada en tarea '{task.task_id}'."
            )

        verifier = str(ev.get("verifier_component") or "").strip()
        if not verifier:
            raise MemoryEvidenceContractError(
                f"La evidencia de la tarea N6 '{task.task_id}' carece de verifier_component objetivo."
            )
        verifier_low = verifier.lower()
        for bad_marker in FORBIDDEN_VERIFIER_MARKERS:
            if bad_marker in verifier_low:
                raise MemoryEvidenceContractError(
                    f"Verificador especulativo/LLM rechazado en tarea '{task.task_id}': '{verifier}'."
                )

        ev_task_id = str(ev.get("task_id") or "").strip()
        if ev_task_id != task.task_id:
            raise MemoryEvidenceContractError(
                f"Evidencia en '{task.task_id}' corresponde a otra tarea distinta ({ev_task_id!r})."
            )
        if ev.get("goal_id") is not None and str(ev.get("goal_id")).strip() != plan.goal.goal_id:
            raise MemoryEvidenceContractError(
                f"Evidencia de '{task.task_id}' corresponde a otro objetivo distinto."
            )
        if ev.get("plan_id") is not None and str(ev.get("plan_id")).strip() != plan.plan_id:
            raise MemoryEvidenceContractError(
                f"Evidencia de '{task.task_id}' corresponde a otro plan distinto."
            )

        # Debe existir evento real `task_completed` registrado en el historial persistido del plan.
        has_completion_event = any(
            getattr(entry, "event_type", None) == "task_completed"
            and getattr(entry, "task_id", None) == task.task_id
            and getattr(entry, "to_status", None) == TaskStatus.COMPLETED.value
            for entry in (plan.history or [])
        )
        if not has_completion_event:
            raise MemoryEvidenceContractError(
                f"La tarea '{task.task_id}' figura COMPLETED sin evento 'task_completed' real en el historial del plan."
            )

        goal_status_val = plan.goal.status.value
        scope_note = (
            "objetivo completo"
            if plan.goal.status == GoalStatus.COMPLETED
            else "subtarea aislada dentro de objetivo parcial"
        )

        if ev_kind == "skill_execution":
            skill_name = (task.skill_name or "").strip()
            ev_skill = str(ev.get("skill_name") or "").strip()
            if not skill_name or ev_skill != skill_name:
                raise MemoryEvidenceContractError(
                    f"Inconsistencia de skill en evidencia de '{task.task_id}' ({ev_skill!r} vs {skill_name!r})."
                )
            try:
                out_len = int(ev.get("output_length") or 0)
            except (TypeError, ValueError) as exc:
                raise MemoryEvidenceContractError(
                    f"output_length inválido en evidencia de '{task.task_id}'."
                ) from exc
            if out_len <= 0:
                raise MemoryEvidenceContractError(
                    f"La skill '{skill_name}' en '{task.task_id}' no produjo salida verificable."
                )
            verified_at = str(ev.get("verified_at") or task.finished_at or "").strip()
            parse_iso_utc(verified_at, field_name="verified_at")

            source_exec_id = f"n6:{plan.plan_id}"
            unit_id = f"n6:{plan.plan_id}:{task.task_id}:{skill_name}"[:128]
            op_type = f"n6_skill:{skill_name}"[:64]
            target_ref = f"n6_skill/{plan.plan_id}/{task.task_id}/{skill_name}"
            op_ref = VerifiedOperationReference(
                ref_id=task.task_id,
                operation_type=op_type,
                target_ref=target_ref,
                verification_status="VERIFIED",
            )
            title = f"Subtarea N6 verificada ({skill_name}): {task.task_id}"[:120]
            content = (
                f"En plan N6 '{plan.plan_id}' ({scope_note}; estado_objetivo={goal_status_val}), "
                f"la subtarea '{task.task_id}' ejecutó la skill '{skill_name}' "
                f"(verificador: {verifier}, longitud_salida: {out_len} caracteres)."
            )[:900]
            summary_txt = (
                f"Subtarea N6 '{task.task_id}' completó skill '{skill_name}' ({scope_note})."
            )[:280]

            return VerifiedExecutionEvidenceContract(
                contract_version=CAPTURE_CONTRACT_VERSION,
                source_layer="N6_EXECUTION_SUPERVISOR",
                source_execution_id=source_exec_id,
                canonical_unit_id=unit_id,
                source_goal_id=plan.goal.goal_id,
                source_plan_id=plan.plan_id,
                source_task_id=task.task_id,
                observed_final_state=goal_status_val,
                verification_outcome="verified",
                verifier_component=verifier,
                executor_component=f"SkillManager:{skill_name}"[:96],
                operation_type=op_type,
                expected_outcome=f"Ejecución verificada de skill {skill_name} en tarea {task.task_id}.",
                fact_title=title,
                fact_content=content,
                fact_summary=summary_txt,
                approval_hash=task.approval_hash if is_valid_sha256_hex(task.approval_hash) else None,
                verified_operation_refs=(op_ref,),
                verified_artifacts=(target_ref,),
                structured_evidence={
                    "goal_id": plan.goal.goal_id,
                    "plan_id": plan.plan_id,
                    "task_id": task.task_id,
                    "goal_status": goal_status_val,
                    "task_status": task.status.value,
                    "evidence_kind": "skill_execution",
                    "skill_name": skill_name,
                    "output_length": str(out_len),
                },
                verified_at=verified_at,
                scope_type=scope_type,
                scope_key=scope_key,
            )

        # Rama chain_orchestrator dentro de N6: debe incluir execution_id, approval_hash y verifications reales.
        exec_id = str(ev.get("execution_id") or "").strip()
        if not exec_id or (task.chain_execution_id and task.chain_execution_id != exec_id):
            raise MemoryEvidenceContractError(
                f"Tarea N6 chain_orchestrator '{task.task_id}' sin execution_id coincidente."
            )
        app_hash = str(ev.get("approval_hash") or "").strip().lower()
        if not is_valid_sha256_hex(app_hash) or (
            task.approval_hash and task.approval_hash.lower() != app_hash
        ):
            raise MemoryEvidenceContractError(
                f"Tarea N6 chain_orchestrator '{task.task_id}' sin approval_hash SHA-256 coincidente."
            )
        summary_obj = ev.get("summary")
        if not isinstance(summary_obj, Mapping) or summary_obj.get("state") != "COMPLETED":
            raise MemoryEvidenceContractError(
                f"Tarea N6 chain_orchestrator '{task.task_id}' sin summary COMPLETED real."
            )
        if summary_obj.get("execution_id") != exec_id:
            raise MemoryEvidenceContractError(
                f"Resumen de '{task.task_id}' apunta a otra ejecución N5."
            )
        verifs = summary_obj.get("verifications")
        applied_ops = summary_obj.get("applied_operations")
        if not isinstance(verifs, Sequence) or not verifs or not isinstance(applied_ops, Sequence) or not applied_ops:
            raise MemoryEvidenceContractError(
                f"Tarea N6 chain_orchestrator '{task.task_id}' sin operaciones aplicadas/verificadas."
            )
        op_refs: list[VerifiedOperationReference] = []
        artifacts: list[str] = []
        verif_map = {
            str(item.get("operation_id") or "").strip(): item
            for item in verifs
            if isinstance(item, Mapping)
        }
        for ap in applied_ops:
            if not isinstance(ap, Mapping):
                raise MemoryEvidenceContractError("Operación aplicada inválida en evidencia N6 chain.")
            ap_id = str(ap.get("operation_id") or "").strip()
            fpath = str(ap.get("file_path") or "").strip()
            new_hash = str(ap.get("new_hash") or "").strip().lower()
            orig_hash = str(ap.get("original_hash") or "").strip().lower()
            v_item = verif_map.get(ap_id)
            if (
                not ap_id
                or not fpath
                or not is_valid_sha256_hex(new_hash)
                or not v_item
                or v_item.get("verified") is not True
                or not is_valid_sha256_hex(v_item.get("sha256"))
            ):
                raise MemoryEvidenceContractError(
                    f"Operación '{ap_id}' en tarea N6 '{task.task_id}' carece de verificación SHA-256."
                )
            op_refs.append(
                VerifiedOperationReference(
                    ref_id=ap_id,
                    operation_type="chain_operation",
                    target_ref=fpath,
                    verification_status="VERIFIED",
                    before_sha256=orig_hash if is_valid_sha256_hex(orig_hash) else None,
                    after_sha256=new_hash,
                )
            )
            artifacts.append(fpath)

        verified_at = str(ev.get("verified_at") or task.finished_at or plan.updated_at or utc_now_iso()).strip()
        parse_iso_utc(verified_at, field_name="verified_at")
        unit_id = f"n6:{plan.plan_id}:{task.task_id}:chain_orchestrator"[:128]
        title = f"Subtarea N6 con ChainOrchestrator verificada: {task.task_id}"[:120]
        content = (
            f"En plan N6 '{plan.plan_id}' ({scope_note}; estado_objetivo={goal_status_val}), "
            f"la subtarea '{task.task_id}' delegó ejecución '{exec_id}' en ChainOrchestrator "
            f"y verificó {len(op_refs)} operación(es) con aprobación SHA-256 {app_hash[:12]}."
        )[:900]
        summary_txt = (
            f"Subtarea N6 '{task.task_id}' verificó ejecución N5 '{exec_id}' ({scope_note})."
        )[:280]

        return VerifiedExecutionEvidenceContract(
            contract_version=CAPTURE_CONTRACT_VERSION,
            source_layer="N6_EXECUTION_SUPERVISOR",
            source_execution_id=exec_id,
            canonical_unit_id=unit_id,
            source_goal_id=plan.goal.goal_id,
            source_plan_id=plan.plan_id,
            source_task_id=task.task_id,
            observed_final_state=goal_status_val,
            verification_outcome="verified",
            verifier_component=verifier,
            executor_component="ChainOrchestrator",
            operation_type="chain_orchestrator",
            expected_outcome=f"Delegación N5 verificada para subtarea {task.task_id}.",
            fact_title=title,
            fact_content=content,
            fact_summary=summary_txt,
            approval_hash=app_hash,
            verified_operation_refs=tuple(op_refs),
            verified_artifacts=tuple(dict.fromkeys(artifacts)),
            structured_evidence={
                "goal_id": plan.goal.goal_id,
                "plan_id": plan.plan_id,
                "task_id": task.task_id,
                "goal_status": goal_status_val,
                "task_status": task.status.value,
                "evidence_kind": "chain_orchestrator",
                "chain_execution_id": exec_id,
                "approval_hash": app_hash,
            },
            verified_at=verified_at,
            scope_type=scope_type,
            scope_key=scope_key,
        )

    def adapt_with_report(
        self,
        plan: Any,
        *,
        execution_result: Any = None,
        scope_type: MemoryScopeType | None = None,
        scope_key: str | None = None,
    ) -> AdaptationReport:
        """Evalúa cada subtarea de un `GoalPlan` real retornando contratos válidos, omitidas y rechazadas."""
        from src.brain.goal_models import GoalPlan, GoalStatus, TaskStatus
        from src.brain.goal_supervisor import ExecutionResult

        if not isinstance(plan, GoalPlan):
            raise MemoryEvidenceContractError(
                "El adaptador N6 requiere un GoalPlan real; no se admiten diccionarios ni texto de modelo."
            )
        if not plan.verify_integrity():
            raise MemoryEvidenceContractError(
                f"Integridad estructural comprometida en GoalPlan '{plan.plan_id}'."
            )

        if execution_result is not None:
            if not isinstance(execution_result, ExecutionResult):
                raise MemoryEvidenceContractError("execution_result debe ser ExecutionResult.")
            if execution_result.plan is not plan or execution_result.final_status != plan.goal.status:
                raise MemoryEvidenceContractError(
                    f"Inconsistencia entre ExecutionResult y GoalPlan '{plan.plan_id}'."
                )
            if execution_result.cancelled or execution_result.stopped_for_approval:
                raise MemoryEvidenceContractError(
                    f"El objetivo '{plan.goal.goal_id}' está cancelado o detenido esperando aprobación."
                )

        if plan.goal.status not in {GoalStatus.COMPLETED, GoalStatus.PARTIAL}:
            raise MemoryEvidenceContractError(
                f"Estado del objetivo '{plan.goal.status.value}' no habilita captura verificada."
            )

        resolved_scope_type, resolved_scope_key = _resolve_default_n6_scope(
            plan.goal.context.target_app,
            scope_type=scope_type,
            scope_key=scope_key,
        )

        contracts: list[VerifiedExecutionEvidenceContract] = []
        accepted_ids: list[str] = []
        skipped: list[tuple[str, str]] = []
        rejected: list[tuple[str, str]] = []

        for task in plan.tasks:
            if task.status != TaskStatus.COMPLETED:
                skipped.append((task.task_id, f"task_status={task.status.value}"))
                continue
            try:
                contract = self._validate_completed_task_evidence(
                    plan,
                    task,
                    scope_type=resolved_scope_type,
                    scope_key=resolved_scope_key,
                )
                contracts.append(contract)
                accepted_ids.append(task.task_id)
            except MemoryEvidenceContractError as err:
                rejected.append((task.task_id, str(err)))

        return AdaptationReport(
            source_layer="N6_EXECUTION_SUPERVISOR",
            source_execution_id=f"n6:{plan.plan_id}",
            contracts=tuple(contracts),
            accepted_unit_ids=tuple(accepted_ids),
            skipped_units=tuple(skipped),
            rejected_units=tuple(rejected),
        )

    def adapt_execution(
        self,
        plan: Any,
        *,
        execution_result: Any = None,
        scope_type: MemoryScopeType | None = None,
        scope_key: str | None = None,
    ) -> list[VerifiedExecutionEvidenceContract]:
        """Versión estricta: falla con `MemoryEvidenceContractError` si ninguna subtarea aporta evidencia objetiva."""
        report = self.adapt_with_report(
            plan,
            execution_result=execution_result,
            scope_type=scope_type,
            scope_key=scope_key,
        )
        if not report.contracts:
            detail = "; ".join(f"{uid}: {rsn}" for uid, rsn in report.rejected_units) or "sin subtareas verificadas"
            raise MemoryEvidenceContractError(
                f"El objetivo '{plan.goal.goal_id}' ({plan.goal.status.value}) no aporta evidencia objetiva válida: {detail}"
            )
        return list(report.contracts)


# ------------------------------------------------------------------------------
# Coordinador de captura automática N8 (`ExperienceCaptureService`)
# ------------------------------------------------------------------------------


class ExperienceCaptureService:
    """
    Coordinador desacoplado de captura automática e invalidación por rollback entre N5/N6 y N8.

    Justificación técnica junior:
    - Solo recibe contratos o resultados validados mediante los adaptadores N5/N6 y persiste
      exclusivamente hechos objetivos `EXECUTION_VERIFIED` (`MemoryCategory.FACT`) usando `MemoryEngine`.
    - Comprueba `N8_ENABLED`, `N8_AUTO_CAPTURE_ENABLED` y el bloqueo por modo privado en el punto
      efectivo de escritura.
    - Serializa captura y rollback mediante un lock interno e impide capturas tardías si la ejecución
      ya sufrió rollback confirmado.
    - Aísla errores de SQLite o política para que un fallo en N8 jamás altere el resultado de N5/N6.
    """

    def __init__(
        self,
        *,
        memory_engine: "MemoryEngine",
        n5_adapter: N5ExecutionEvidenceAdapter | None = None,
        n6_adapter: N6GoalEvidenceAdapter | None = None,
        enabled: bool | None = None,
        auto_capture_enabled: bool | None = None,
    ) -> None:
        self.memory_engine: "MemoryEngine" = memory_engine
        self.n5_adapter: N5ExecutionEvidenceAdapter = n5_adapter or N5ExecutionEvidenceAdapter()
        self.n6_adapter: N6GoalEvidenceAdapter = n6_adapter or N6GoalEvidenceAdapter()
        self._enabled_override: bool | None = enabled
        self._auto_capture_override: bool | None = auto_capture_enabled
        self._lock = threading.RLock()
        self._invalidated_executions: set[str] = set()

    @property
    def is_enabled(self) -> bool:
        if self._enabled_override is not None:
            return bool(self._enabled_override)
        return bool(settings.N8_ENABLED) and bool(self.memory_engine.policy.enabled)

    @property
    def auto_capture_enabled(self) -> bool:
        if self._auto_capture_override is not None:
            return bool(self._auto_capture_override)
        return bool(settings.N8_AUTO_CAPTURE_ENABLED)

    def _check_capture_gate(self) -> tuple[bool, str, str]:
        """Evalúa todas las barreras de habilitación y privacidad en el punto efectivo de escritura."""
        if not self.is_enabled:
            return False, "disabled", "Subsistema N8 deshabilitado (N8_ENABLED=False)."
        if not self.auto_capture_enabled:
            return False, "disabled", "Captura automática N8.3 deshabilitada (N8_AUTO_CAPTURE_ENABLED=False)."
        if self.memory_engine.is_private_mode:
            return False, "blocked_private_mode", "Modo privado activo: captura automática bloqueada sin escritura."
        return True, "allowed", "ok"

    def _is_execution_invalidated_locked(self, execution_id: str) -> bool:
        if execution_id in self._invalidated_executions:
            return True
        if self.memory_engine.is_execution_invalidated(execution_id):
            self._invalidated_executions.add(execution_id)
            return True
        return False

    def is_execution_invalidated(self, execution_id: str) -> bool:
        """Consulta segura (RAM + SQLite) indicando si `execution_id` fue invalidada por rollback."""
        clean_id = (execution_id or "").strip()
        if not clean_id:
            return False
        with self._lock:
            if not self.is_enabled:
                return clean_id in self._invalidated_executions
            return self._is_execution_invalidated_locked(clean_id)

    def capture_contract(
        self,
        contract: VerifiedExecutionEvidenceContract,
        *,
        actor: str = "capture_service",
    ) -> CaptureUnitOutcome:
        """Persiste de forma cerrada e idempotente una unidad de evidencia contractual como `FACT` verificado."""
        if not isinstance(contract, VerifiedExecutionEvidenceContract):
            return CaptureUnitOutcome(
                canonical_unit_id="invalid_contract",
                source_execution_id="unknown",
                status="rejected",
                reason="Se requiere VerifiedExecutionEvidenceContract inmutable.",
            )

        try:
            contract.verify_integrity()
        except Exception as exc:
            return CaptureUnitOutcome(
                canonical_unit_id=contract.canonical_unit_id,
                source_execution_id=contract.source_execution_id,
                status="rejected",
                reason=redact_secrets(str(exc)),
            )

        with self._lock:
            allowed, gate_status, gate_reason = self._check_capture_gate()
            if not allowed:
                return CaptureUnitOutcome(
                    canonical_unit_id=contract.canonical_unit_id,
                    source_execution_id=contract.source_execution_id,
                    status=gate_status,
                    reason=gate_reason,
                )

            try:
                if self._is_execution_invalidated_locked(contract.source_execution_id):
                    return CaptureUnitOutcome(
                        canonical_unit_id=contract.canonical_unit_id,
                        source_execution_id=contract.source_execution_id,
                        status="invalidated_execution",
                        reason=(
                            f"Ejecución '{contract.source_execution_id}' invalidada por rollback previo; "
                            "captura rechazada de forma cerrada."
                        ),
                    )

                ev = contract.to_execution_verification_evidence()
                meta_pairs: list[tuple[str, str]] = [
                    ("contract_version", contract.contract_version),
                    ("contract_sha256", contract.contract_sha256),
                    ("observed_final_state", contract.observed_final_state),
                    ("evidence_kind", dict(contract.structured_evidence).get("evidence_kind", "n5_chain")),
                ]
                tags = ("n8_auto_capture", contract.source_layer.lower(), contract.operation_type.lower())
                payload = MemoryCreateInput(
                    category=MemoryCategory.FACT,
                    source=MemorySource.EXECUTION_VERIFIED,
                    title=contract.fact_title,
                    content=contract.fact_content,
                    summary=contract.fact_summary,
                    scope_type=contract.scope_type,
                    scope_key=contract.scope_key,
                    confidence=0.98,
                    importance=80,
                    tags=tags,
                    metadata=tuple(meta_pairs),
                    verification_evidence=ev,
                    canonical_unit_id=contract.canonical_unit_id,
                )
                write_res = self.memory_engine.create_memory(payload, actor=actor)
                if write_res.record.status == MemoryStatus.REVOKED:
                    self._invalidated_executions.add(contract.source_execution_id)
                    return CaptureUnitOutcome(
                        canonical_unit_id=contract.canonical_unit_id,
                        source_execution_id=contract.source_execution_id,
                        status="invalidated_execution",
                        memory_id=write_res.record.memory_id,
                        idempotency_key=write_res.record.idempotency_key,
                        reason="La unidad ya figuraba REVOKED en disco; no se reactiva.",
                    )
                if write_res.created:
                    return CaptureUnitOutcome(
                        canonical_unit_id=contract.canonical_unit_id,
                        source_execution_id=contract.source_execution_id,
                        status="captured",
                        memory_id=write_res.record.memory_id,
                        idempotency_key=write_res.record.idempotency_key,
                        reason="created",
                    )
                return CaptureUnitOutcome(
                    canonical_unit_id=contract.canonical_unit_id,
                    source_execution_id=contract.source_execution_id,
                    status="deduplicated",
                    memory_id=write_res.record.memory_id,
                    idempotency_key=write_res.record.idempotency_key,
                    reason=write_res.matched_reason or "idempotency_key",
                )

            except MemoryPrivateModeError as exc:
                return CaptureUnitOutcome(
                    canonical_unit_id=contract.canonical_unit_id,
                    source_execution_id=contract.source_execution_id,
                    status="blocked_private_mode",
                    reason=redact_secrets(str(exc)),
                )
            except MemoryIdempotencyConflictError as exc:
                msg = redact_secrets(str(exc))
                logger.warning("Conflicto de idempotencia auditado durante captura N8: %s", msg)
                return CaptureUnitOutcome(
                    canonical_unit_id=contract.canonical_unit_id,
                    source_execution_id=contract.source_execution_id,
                    status="idempotency_conflict",
                    reason=msg,
                )
            except (MemoryEvidenceContractError, MemoryPolicyError, MemoryValidationError) as exc:
                return CaptureUnitOutcome(
                    canonical_unit_id=contract.canonical_unit_id,
                    source_execution_id=contract.source_execution_id,
                    status="rejected",
                    reason=redact_secrets(str(exc)),
                )
            except (
                MemoryPersistenceError,
                MemoryCorruptionError,
                MemoryLockTimeoutError,
                sqlite3.Error,
                OSError,
            ) as exc:
                msg = redact_secrets(str(exc))
                logger.error("Fallo de almacenamiento aislado en captura N8 (N5/N6 intacto): %s", msg)
                return CaptureUnitOutcome(
                    canonical_unit_id=contract.canonical_unit_id,
                    source_execution_id=contract.source_execution_id,
                    status="failed",
                    reason=msg,
                )
            except Exception as exc:
                msg = redact_secrets(str(exc)) or type(exc).__name__
                logger.error("Error inesperado aislado en captura N8: %s", msg)
                return CaptureUnitOutcome(
                    canonical_unit_id=contract.canonical_unit_id,
                    source_execution_id=contract.source_execution_id,
                    status="failed",
                    reason=msg,
                )

    def capture_contracts(
        self,
        contracts: Sequence[VerifiedExecutionEvidenceContract],
        *,
        source_layer: str = "N8",
        execution_id: str = "",
        actor: str = "capture_service",
        pre_rejected: Sequence[tuple[str, str]] = (),
        pre_skipped_count: int = 0,
    ) -> CaptureBatchResult:
        """Procesa un lote de contratos retornando métricas exactas y sin falsificar éxito ante errores."""
        outcomes: list[CaptureUnitOutcome] = []
        eff_exec_id = execution_id or (contracts[0].source_execution_id if contracts else "unknown")

        for unit_id, reason in pre_rejected:
            outcomes.append(
                CaptureUnitOutcome(
                    canonical_unit_id=unit_id,
                    source_execution_id=eff_exec_id,
                    status="rejected",
                    reason=redact_secrets(reason),
                )
            )

        allowed, gate_status, gate_reason = self._check_capture_gate()
        if not allowed and not pre_rejected:
            return CaptureBatchResult(
                ok=False,
                source_layer=source_layer,
                execution_id=eff_exec_id,
                status=gate_status,
                skipped_count=pre_skipped_count,
                error=gate_reason,
            )

        for contract in contracts:
            outcomes.append(self.capture_contract(contract, actor=actor))

        captured_ids = tuple(
            out.memory_id
            for out in outcomes
            if out.status in {"captured", "deduplicated"} and out.memory_id
        )
        captured_count = sum(1 for o in outcomes if o.status == "captured")
        dedup_count = sum(1 for o in outcomes if o.status == "deduplicated")
        rejected_count = sum(
            1 for o in outcomes if o.status in {"rejected", "idempotency_conflict", "invalidated_execution"}
        )
        failed_count = sum(1 for o in outcomes if o.status == "failed")
        blocked_priv = sum(1 for o in outcomes if o.status == "blocked_private_mode")
        disabled_cnt = sum(1 for o in outcomes if o.status == "disabled")

        if failed_count > 0:
            batch_status = "failed"
            batch_ok = False
        elif blocked_priv > 0 and captured_count == 0 and dedup_count == 0:
            batch_status = "blocked_private_mode"
            batch_ok = False
        elif disabled_cnt > 0 and captured_count == 0 and dedup_count == 0:
            batch_status = "disabled"
            batch_ok = False
        elif rejected_count > 0 and captured_count == 0 and dedup_count == 0:
            batch_status = outcomes[0].status if outcomes else "rejected"
            batch_ok = False
        elif rejected_count > 0:
            batch_status = "partial"
            batch_ok = True
        else:
            batch_status = "captured" if captured_count > 0 else "deduplicated"
            batch_ok = (captured_count + dedup_count) > 0

        err_summary = "; ".join(
            f"{o.canonical_unit_id}: {o.reason}"
            for o in outcomes
            if o.status in {"failed", "rejected", "idempotency_conflict", "invalidated_execution", "blocked_private_mode", "disabled"}
            and o.reason
        ) or None

        return CaptureBatchResult(
            ok=batch_ok,
            source_layer=source_layer,
            execution_id=eff_exec_id,
            status=batch_status,
            captured_count=captured_count,
            deduplicated_count=dedup_count,
            rejected_count=rejected_count,
            failed_count=failed_count,
            skipped_count=pre_skipped_count,
            memory_ids=captured_ids,
            outcomes=tuple(outcomes),
            error=err_summary,
        )

    def handle_n5_execution_completed(
        self,
        *,
        record: Any,
        summary: Mapping[str, Any] | None = None,
        transaction_records: Sequence[Any] | None = None,
        scope_type: MemoryScopeType | None = None,
        scope_key: str | None = None,
    ) -> CaptureBatchResult:
        """Punto de entrada seguro para N5 al completar una ejecución (`ChainOrchestrator`)."""
        exec_id = str(getattr(record, "execution_id", "") or "unknown")
        try:
            allowed, gate_status, gate_reason = self._check_capture_gate()
            if not allowed:
                return CaptureBatchResult(
                    ok=False,
                    source_layer="N5_CHAIN_ORCHESTRATOR",
                    execution_id=exec_id,
                    status=gate_status,
                    error=gate_reason,
                )

            contracts = self.n5_adapter.adapt_execution(
                record,
                summary=summary,
                transaction_records=transaction_records,
                scope_type=scope_type,
                scope_key=scope_key,
            )
            return self.capture_contracts(
                contracts,
                source_layer="N5_CHAIN_ORCHESTRATOR",
                execution_id=exec_id,
                actor="n5_chain_orchestrator",
            )
        except (MemoryEvidenceContractError, MemoryPolicyError, MemoryValidationError) as exc:
            msg = redact_secrets(str(exc))
            logger.warning("Captura N5 rechazada para '%s': %s", exec_id, msg)
            return CaptureBatchResult(
                ok=False,
                source_layer="N5_CHAIN_ORCHESTRATOR",
                execution_id=exec_id,
                status="rejected",
                rejected_count=1,
                error=msg,
            )
        except Exception as exc:
            msg = redact_secrets(str(exc)) or type(exc).__name__
            logger.error("Error aislado al capturar ejecución N5 '%s': %s", exec_id, msg)
            return CaptureBatchResult(
                ok=False,
                source_layer="N5_CHAIN_ORCHESTRATOR",
                execution_id=exec_id,
                status="failed",
                failed_count=1,
                error=msg,
            )

    def handle_n6_execution_completed(
        self,
        *,
        plan: Any,
        execution_result: Any = None,
        scope_type: MemoryScopeType | None = None,
        scope_key: str | None = None,
    ) -> CaptureBatchResult:
        """Punto de entrada seguro para N6 al concluir un `GoalPlan` (`COMPLETED` o `PARTIAL`)."""
        plan_id = str(getattr(plan, "plan_id", "") or "unknown")
        exec_id = f"n6:{plan_id}"
        try:
            allowed, gate_status, gate_reason = self._check_capture_gate()
            if not allowed:
                return CaptureBatchResult(
                    ok=False,
                    source_layer="N6_EXECUTION_SUPERVISOR",
                    execution_id=exec_id,
                    status=gate_status,
                    error=gate_reason,
                )

            report = self.n6_adapter.adapt_with_report(
                plan,
                execution_result=execution_result,
                scope_type=scope_type,
                scope_key=scope_key,
            )
            if not report.contracts and not report.rejected_units:
                return CaptureBatchResult(
                    ok=False,
                    source_layer="N6_EXECUTION_SUPERVISOR",
                    execution_id=exec_id,
                    status="rejected",
                    rejected_count=1,
                    skipped_count=len(report.skipped_units),
                    error="Ninguna subtarea del objetivo aportó evidencia objetiva verificable.",
                )

            return self.capture_contracts(
                report.contracts,
                source_layer="N6_EXECUTION_SUPERVISOR",
                execution_id=exec_id,
                actor="n6_execution_supervisor",
                pre_rejected=report.rejected_units,
                pre_skipped_count=len(report.skipped_units),
            )
        except (MemoryEvidenceContractError, MemoryPolicyError, MemoryValidationError) as exc:
            msg = redact_secrets(str(exc))
            logger.warning("Captura N6 rechazada para '%s': %s", exec_id, msg)
            return CaptureBatchResult(
                ok=False,
                source_layer="N6_EXECUTION_SUPERVISOR",
                execution_id=exec_id,
                status="rejected",
                rejected_count=1,
                error=msg,
            )
        except Exception as exc:
            msg = redact_secrets(str(exc)) or type(exc).__name__
            logger.error("Error aislado al capturar objetivo N6 '%s': %s", exec_id, msg)
            return CaptureBatchResult(
                ok=False,
                source_layer="N6_EXECUTION_SUPERVISOR",
                execution_id=exec_id,
                status="failed",
                failed_count=1,
                error=msg,
            )

    def handle_n5_rollback_confirmed(
        self,
        *,
        execution_id: str,
        record: Any = None,
        rollback_result: Mapping[str, Any] | None = None,
        recovery_report: Any = None,
        reason: str = "n5_rollback_confirmed",
        actor: str = "n5_chain_orchestrator",
    ) -> RollbackInvalidationOutcome:
        """
        Invalida recuerdos asociados a `execution_id` ÚNICAMENTE tras verificar confirmación real
        de rollback completado en N5 (`ChainOrchestrator.rollback_execution` o `RecoveryReport`).
        Nunca altera el resultado de N5 si SQLite falla o si el modo privado bloquea escritura.
        """
        clean_exec_id = (execution_id or "").strip()
        if not clean_exec_id:
            return RollbackInvalidationOutcome(
                execution_id="",
                ok=False,
                status="not_confirmed",
                error="execution_id vacío en notificación de rollback.",
            )

        # Validación estricta de evidencia estructural de que N5 completó realmente el rollback.
        from src.brain.chain_orchestrator import ChainExecutionRecord, ChainState
        from src.brain.transaction_manager import RecoveryReport, TransactionState

        confirmed_evidence = False
        if record is not None:
            if not isinstance(record, ChainExecutionRecord):
                return RollbackInvalidationOutcome(
                    execution_id=clean_exec_id,
                    ok=False,
                    status="not_confirmed",
                    error="record de rollback inválido.",
                )
            if record.execution_id != clean_exec_id or record.state != ChainState.ROLLED_BACK:
                return RollbackInvalidationOutcome(
                    execution_id=clean_exec_id,
                    ok=False,
                    status="not_confirmed",
                    error=(
                        f"El estado N5 actual ({record.state.value}) o execution_id ({record.execution_id}) "
                        "no confirma un rollback completado."
                    ),
                )
            if record.rollback_summary is not None and not bool(record.rollback_summary.get("all_restored")):
                return RollbackInvalidationOutcome(
                    execution_id=clean_exec_id,
                    ok=False,
                    status="not_confirmed",
                    error="rollback_summary indica restauración incompleta (all_restored=False).",
                )
            confirmed_evidence = True

        if rollback_result is not None:
            if (
                not isinstance(rollback_result, Mapping)
                or rollback_result.get("execution_id") != clean_exec_id
                or rollback_result.get("state") != ChainState.ROLLED_BACK.value
            ):
                return RollbackInvalidationOutcome(
                    execution_id=clean_exec_id,
                    ok=False,
                    status="not_confirmed",
                    error="rollback_result no confirma estado ROLLED_BACK para el execution_id indicado.",
                )
            confirmed_evidence = True

        if recovery_report is not None:
            if (
                not isinstance(recovery_report, RecoveryReport)
                or recovery_report.execution_id != clean_exec_id
                or recovery_report.final_state != TransactionState.ROLLED_BACK.value
            ):
                return RollbackInvalidationOutcome(
                    execution_id=clean_exec_id,
                    ok=False,
                    status="not_confirmed",
                    error="RecoveryReport no confirma final_state=ROLLED_BACK para esa ejecución.",
                )
            confirmed_evidence = True

        if not confirmed_evidence:
            return RollbackInvalidationOutcome(
                execution_id=clean_exec_id,
                ok=False,
                status="not_confirmed",
                error="No se aportó prueba estructural N5 de rollback confirmado (ni record, ni rollback_result, ni RecoveryReport).",
            )

        with self._lock:
            self._invalidated_executions.add(clean_exec_id)

            if not self.is_enabled:
                return RollbackInvalidationOutcome(
                    execution_id=clean_exec_id,
                    ok=False,
                    status="disabled",
                    error="Subsistema N8 deshabilitado; marca local registrada.",
                )

            if self.memory_engine.is_private_mode:
                return RollbackInvalidationOutcome(
                    execution_id=clean_exec_id,
                    ok=False,
                    status="blocked_private_mode",
                    error="Modo privado activo: invalidación en disco bloqueada sin alterar buffers.",
                )

            try:
                inv = self.memory_engine.invalidate_by_execution(
                    clean_exec_id,
                    reason=reason,
                    actor=actor,
                )
                if inv.invalidated_count > 0:
                    return RollbackInvalidationOutcome(
                        execution_id=clean_exec_id,
                        ok=True,
                        status="invalidated",
                        invalidated_count=inv.invalidated_count,
                        invalidated_memory_ids=inv.invalidated_memory_ids,
                        already_inactive_count=inv.already_inactive_count,
                        store_version_token=inv.store_version_token,
                    )

                # Si no había recuerdos activos aún, persistimos tombstone idempotente si ya existía
                # captura habilitada o para prevenir capturas tardías futuras tras un reinicio.
                tombstone_created = False
                if inv.already_inactive_count == 0:
                    tombstone_created = self.memory_engine.mark_execution_rollback_tombstone(
                        clean_exec_id,
                        reason=reason,
                        actor=actor,
                    )
                return RollbackInvalidationOutcome(
                    execution_id=clean_exec_id,
                    ok=True,
                    status="tombstone_marked" if tombstone_created else "idempotent_noop",
                    invalidated_count=0,
                    invalidated_memory_ids=(),
                    already_inactive_count=inv.already_inactive_count,
                    store_version_token=inv.store_version_token,
                )

            except MemoryPrivateModeError as exc:
                return RollbackInvalidationOutcome(
                    execution_id=clean_exec_id,
                    ok=False,
                    status="blocked_private_mode",
                    error=redact_secrets(str(exc)),
                )
            except Exception as exc:
                msg = redact_secrets(str(exc)) or type(exc).__name__
                logger.error("Error aislado durante invalidación N8 por rollback '%s': %s", clean_exec_id, msg)
                return RollbackInvalidationOutcome(
                    execution_id=clean_exec_id,
                    ok=False,
                    status="failed",
                    error=msg,
                )
