"""Persistencia dedicada y transaccional de objetivos N6 (aislada de `memory_store.json`).

Garantías de arquitectura y seguridad:
1. Nunca lee ni modifica `data/memory_store.json`.
2. Cuando `JARVIS_ENV` es `test` o `testing`, confina estrictamente toda escritura
   dentro de `temp/jarvis_test_isolated/` (respetando `JARVIS_TEST_TEMP_ROOT` acotado).
3. Combina bitácora append-only (`.wal.jsonl`) + instantáneas atómicas (`.goal.json`)
   usando `flush()`, `os.fsync()` y `os.replace()` tolerante a bloqueos breves en Windows.
4. Sanitiza mensajes y errores con `redact_secrets` antes de escribir a disco.
5. Verifica integridad estructural (`plan_hash`) al recargar un plan persistido.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Mapping, Optional
import uuid

from config.settings import BASE_DIR, GOAL_STORE_DIR
from src.brain.exceptions import is_sensitive_key, redact_secrets
from src.brain.goal_memory import (
    FORBIDDEN_PERSISTED_N8_KEYS,
    build_skeleton_task_labels,
    collect_grounded_anchors,
    is_canonical_n8_task_id,
    sanitize_n8_context_extra_for_persistence,
    separate_grounded_skill_params,
    strip_volatile_n8_keys_recursive,
)
from src.brain.goal_models import (
    Constraints,
    GoalPersistenceError,
    GoalPlan,
    GoalTask,
    utc_now_iso,
)


RUNTIME_ENV_ENV: str = "JARVIS_ENV"
TEST_TEMP_ROOT_ENV: str = "JARVIS_TEST_TEMP_ROOT"
_SAFE_ID_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")


def is_test_environment() -> bool:
    """Detecta si el entorno se declaró explícitamente como de pruebas."""
    env_value = os.environ.get(RUNTIME_ENV_ENV, "").strip().lower()
    return env_value in {"test", "testing"}


def _authorized_test_root() -> Path:
    """Devuelve la raíz temporal única autorizada para tests aislados."""
    base_isolated = (BASE_DIR / "temp" / "jarvis_test_isolated").resolve()
    configured = os.environ.get(TEST_TEMP_ROOT_ENV, "").strip()
    target = Path(configured).resolve() if configured else base_isolated
    try:
        target.relative_to(base_isolated)
    except ValueError as err:
        raise GoalPersistenceError(
            f"{TEST_TEMP_ROOT_ENV} debe permanecer estrictamente dentro de {base_isolated}."
        ) from err
    target.mkdir(parents=True, exist_ok=True)
    return target


def _validate_store_dir(candidate: Path | str | None) -> Path:
    """Resuelve y valida el directorio de almacenamiento de objetivos N6."""
    test_mode = is_test_environment()
    memory_file = (BASE_DIR / "data" / "memory_store.json").resolve()

    if candidate is None:
        resolved = (
            (_authorized_test_root() / "goals").resolve()
            if test_mode
            else Path(GOAL_STORE_DIR).resolve()
        )
    else:
        resolved = Path(candidate).resolve()

    if resolved == memory_file or resolved == memory_file.parent and str(resolved).endswith("memory_store.json"):
        raise GoalPersistenceError(
            "La persistencia de objetivos N6 no puede compartir ruta con memory_store.json."
        )

    if test_mode:
        test_root = _authorized_test_root()
        try:
            resolved.relative_to(test_root)
        except ValueError as err:
            raise GoalPersistenceError(
                f"En modo test ({RUNTIME_ENV_ENV}=test), GoalStore solo puede escribir dentro de {test_root}. "
                f"Ruta rechazada: {resolved}"
            ) from err

    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _sanitize_identifier(identifier: str, label: str = "id") -> str:
    clean = (identifier or "").strip()
    if not _SAFE_ID_RE.match(clean):
        raise GoalPersistenceError(
            f"Identificador de {label} inválido para persistencia segura: '{identifier}'."
        )
    return clean


# Campos de contexto volátil/privado y de memoria RAM N8 que no deben persistirse en archivos .goal.json.
_VOLATILE_CONTEXT_KEYS: frozenset[str] = frozenset(
    {
        "visible_windows",
        "foreground_window",
        "top_processes",
        *FORBIDDEN_PERSISTED_N8_KEYS,
    }
)

_SAFE_PERSISTED_TASK_META_KEYS: frozenset[str] = frozenset(
    {
        "replan_for",
        "proposed_by_ai",
        "use_chain_orchestrator",
        "modifies_files",
        "provenance_origin",
        "n8_influenced",
        "n8_query_id",
        "ephemeral_details_in_ram",
    }
)


def _redact_nested_data(value: Any) -> Any:
    """Redacta recursivamente posibles secretos y suprime campos volátiles antes de persistir."""
    cleaned_n8 = strip_volatile_n8_keys_recursive(value)
    if isinstance(cleaned_n8, dict):
        cleaned: dict[str, Any] = {}
        for k, v in cleaned_n8.items():
            if str(k) in _VOLATILE_CONTEXT_KEYS:
                continue
            if is_sensitive_key(str(k)) and isinstance(v, (str, int, float)) and not isinstance(v, bool) and v != "":
                cleaned[str(k)] = "[REDACTED]"
            else:
                cleaned[str(k)] = _redact_nested_data(v)
        return cleaned
    return redact_secrets(cleaned_n8) if isinstance(cleaned_n8, (str, list)) else cleaned_n8


def sanitize_wal_payload(
    payload: Mapping[str, Any] | dict[str, Any], *, n8_context_exposed: bool = False
) -> dict[str, Any]:
    """Limpia un payload WAL excluyendo claves N8 volátiles, textos de recuerdos y redactando secretos."""
    if not isinstance(payload, Mapping):
        return {}
    if n8_context_exposed:
        # Una excepción o motivo narrativo puede incorporar texto de una subtarea influida por N8.
        # Conservamos códigos, estados e IDs técnicos permitidos y omitimos los campos narrativos.
        safe_keys = {
            "task_id", "plan_id", "goal_id", "execution_id", "retry", "status",
            "from_status", "to_status", "error_code", "error_category", "attempt",
            "n8_query_id", "n8_ref_count",
        }
        return {
            str(key): _redact_nested_data(value) if str(key) in safe_keys else "[DETALLE N8 OMITIDO]"
            for key, value in payload.items()
        }
    cleaned = strip_volatile_n8_keys_recursive(dict(payload))
    if not isinstance(cleaned, dict):
        return {}
    if cleaned.get("n8_influenced") or cleaned.get("provenance_origin") == "llm_with_n8":
        if "reason" in cleaned:
            cleaned["reason"] = "Detalle contextual N8 mantenido solo en RAM."
        if "message" in cleaned:
            cleaned["message"] = "Evento contextual N8 mantenido solo en RAM."
        if "summary" in cleaned:
            cleaned["summary"] = "Resumen contextual N8 mantenido solo en RAM."
    out: dict[str, Any] = {}
    for k, v in cleaned.items():
        if str(k) in _VOLATILE_CONTEXT_KEYS:
            continue
        out[str(k)] = _redact_nested_data(v)
    return out


def extract_persistable_plan_dict(
    plan: GoalPlan,
    *,
    allow_private_ram: bool = False,
) -> dict[str, Any]:
    """
    Frontera estructural determinista entre `persistable_skeleton` y `ephemeral_annotations`.

    Garantías:
    1. Rechaza persistir en disco planes creados durante el modo privado (`is_ephemeral_private`).
    2. Elimina cualquier sobre RAM o clave volátil N8 (`_VOLATILE_CONTEXT_KEYS`).
    3. En `goal.context.extra` conserva exclusivamente referencias mínimas verificadas (`n8_query_id`,
       `n8_memory_refs`, `n8_trust_boundary`, `n8_last_revalidated_at`) junto a metadatos N7 válidos.
    4. Si el objetivo, una tarea o una replanificación estuvieron expuestos a N8 y el dato no posee
       procedencia independiente probada (`anchors` previos al LLM/N8), descarta prosa/paráfrasis
       o parámetros no anclados de la serialización persistente y preserva el esqueleto válido.
    """
    if not allow_private_ram and getattr(plan, "is_ephemeral_private", False):
        raise GoalPersistenceError(
            "Violación de aislamiento privado: no se permite transferir ni persistir un plan privado en GoalStore."
        )

    inferred_exposed = (
        plan.infer_n8_exposure() if hasattr(plan, "infer_n8_exposure") else False
    )
    n8_exposed = bool(getattr(plan, "n8_context_exposed", False) or inferred_exposed)
    if n8_exposed:
        plan.n8_context_exposed = True
    cloned = _sanitize_runtime_strings(plan.to_dict(), n8_context_exposed=n8_exposed)
    cloned["n8_context_exposed"] = n8_exposed
    anchors = collect_grounded_anchors(plan.goal)

    # 1. Frontera estructural de Goal
    goal_dict = cloned.get("goal")
    n8_goal_exposed = bool(
        getattr(plan.goal, "_n8_exposed_during_planning", False)
        or n8_exposed
    )
    if isinstance(goal_dict, dict):
        ctx_dict = goal_dict.get("context")
        if isinstance(ctx_dict, dict):
            ctx_dict["extra"] = sanitize_n8_context_extra_for_persistence(ctx_dict.get("extra"))
            ctx_dict["hardware"] = _redact_nested_data(ctx_dict.get("hardware") or {})
            ctx_dict["environment"] = _redact_nested_data(ctx_dict.get("environment") or {})

        baseline = getattr(plan.goal, "_deterministic_baseline", None)
        if n8_goal_exposed and isinstance(baseline, Mapping):
            goal_dict["interpreted_summary"] = str(baseline.get("interpreted_summary") or goal_dict.get("raw_input") or "")
            goal_dict["missing_info"] = [str(x) for x in (baseline.get("missing_info") or [])]
            if isinstance(goal_dict.get("success_criteria"), dict):
                goal_dict["success_criteria"]["required_outcomes"] = [
                    str(x) for x in (baseline.get("required_outcomes") or [])
                ]
            if isinstance(ctx_dict, dict):
                ctx_dict["target_app"] = baseline.get("target_app")

        if n8_goal_exposed and isinstance(goal_dict.get("error_summary"), str) and goal_dict["error_summary"].strip():
            goal_dict["error_summary"] = "Ejecución con incidencias (detalle contextual N8 mantenido solo en RAM)."
        if (
            n8_goal_exposed
            and isinstance(goal_dict.get("cancellation_reason"), str)
            and goal_dict["cancellation_reason"].strip()
        ):
            goal_dict["cancellation_reason"] = (
                "Objetivo cancelado bajo contexto consultivo N8 (detalle mantenido solo en RAM)."
            )

    # 2. Frontera estructural de GoalTask (`persistable_skeleton`)
    task_objs = {t.task_id: t for t in plan.tasks}
    canonical_dirty = False
    clean_task_models: list[GoalTask] = []

    # Defensa en profundidad F-01: si alguna tarea influenciada por N8 llegara a persistencia
    # con un `task_id` no canónico (p. ej. construido fuera de TaskDecomposer/ExecutionSupervisor),
    # generamos un mapa de canonización determinista antes de serializar el grafo completo.
    raw_task_dicts = [td for td in (cloned.get("tasks") or []) if isinstance(td, dict)]
    tainted_id_remap: dict[str, str] = {}
    used_persist_ids: set[str] = set()
    for td in raw_task_dicts:
        tid_raw = str(td.get("task_id") or "").strip()
        t_ref = task_objs.get(tid_raw)
        meta_ref = td.get("metadata") if isinstance(td.get("metadata"), dict) else {}
        is_t_tainted = bool(
            meta_ref.get("n8_influenced")
            or meta_ref.get("provenance_origin") == "llm_with_n8"
            or (t_ref is not None and (t_ref.n8_influenced or t_ref.provenance_origin == "llm_with_n8"))
        )
        if not is_t_tainted or is_canonical_n8_task_id(tid_raw):
            used_persist_ids.add(tid_raw)

    seq_plan_idx = 1
    seq_replan_counters: dict[int, int] = {}
    for td in raw_task_dicts:
        tid_raw = str(td.get("task_id") or "").strip()
        t_ref = task_objs.get(tid_raw)
        meta_ref = td.get("metadata") if isinstance(td.get("metadata"), dict) else {}
        is_t_tainted = bool(
            meta_ref.get("n8_influenced")
            or meta_ref.get("provenance_origin") == "llm_with_n8"
            or (t_ref is not None and (t_ref.n8_influenced or t_ref.provenance_origin == "llm_with_n8"))
        )
        if is_t_tainted and not is_canonical_n8_task_id(tid_raw):
            gen_val = int(td.get("replan_generation") or 0)
            while True:
                if gen_val > 0:
                    cnt = seq_replan_counters.get(gen_val, 0) + 1
                    seq_replan_counters[gen_val] = cnt
                    cand_tid = f"t_r{gen_val}_{cnt}"
                else:
                    cand_tid = f"t{seq_plan_idx}"
                    seq_plan_idx += 1
                if cand_tid not in used_persist_ids:
                    used_persist_ids.add(cand_tid)
                    tainted_id_remap[tid_raw] = cand_tid
                    break

    if tainted_id_remap:
        canonical_dirty = True

    def _remap_tid(val: str) -> str:
        return tainted_id_remap.get(val, val)

    def _remap_nested_ids(obj: Any) -> Any:
        if not tainted_id_remap:
            return obj
        if isinstance(obj, str):
            out_str = obj
            for old_id, new_id in tainted_id_remap.items():
                if old_id and old_id in out_str:
                    out_str = out_str.replace(old_id, new_id)
            return out_str
        if isinstance(obj, list):
            return [_remap_nested_ids(item) for item in obj]
        if isinstance(obj, dict):
            return {k: _remap_nested_ids(v) for k, v in obj.items()}
        return obj

    for task_dict in raw_task_dicts:
        orig_tid = str(task_dict.get("task_id") or "")
        t_obj = task_objs.get(orig_tid)
        tid = _remap_tid(orig_tid)
        if tid != orig_tid:
            task_dict["task_id"] = tid
        raw_deps_list = task_dict.get("depends_on") or []
        if isinstance(raw_deps_list, list) and tainted_id_remap:
            remapped_deps = [_remap_tid(str(d)) for d in raw_deps_list]
            if remapped_deps != raw_deps_list:
                task_dict["depends_on"] = remapped_deps
                canonical_dirty = True

        meta = dict(task_dict.get("metadata") or {})
        meta_cleaned = _redact_nested_data(meta)
        if not isinstance(meta_cleaned, dict):
            meta_cleaned = {}

        task_tainted = bool(
            meta_cleaned.get("n8_influenced")
            or meta_cleaned.get("provenance_origin") == "llm_with_n8"
            or (t_obj is not None and (t_obj.n8_influenced or t_obj.provenance_origin == "llm_with_n8"))
        )
        exec_tainted = bool(
            task_tainted
            or (t_obj is not None and getattr(t_obj, "n8_exposed_at_exec", False))
            or (
                isinstance(task_dict.get("execution_evidence"), dict)
                and (
                    task_dict["execution_evidence"].get("n8_exposed_at_exec")
                    or task_dict["execution_evidence"].get("n8_tainted")
                )
            )
            or (
                isinstance(task_dict.get("untrusted_envelope"), dict)
                and (
                    task_dict["untrusted_envelope"].get("n8_tainted")
                    or task_dict["untrusted_envelope"].get("trust_level") == "untrusted_long_term_memory"
                    or task_dict["untrusted_envelope"].get("provenance") in {"n8_memory", "n8_and_n7_mixed"}
                    or bool(task_dict["untrusted_envelope"].get("historical_references"))
                )
            )
            or (
                task_dict.get("skill_name") is None
                and n8_exposed
            )
        )

        # Limpieza incondicional de untrusted_envelope si contiene o propaga datos de memoria N8 (H-05).
        env_raw = task_dict.get("untrusted_envelope")
        if isinstance(env_raw, dict):
            if (
                env_raw.get("trust_level") == "untrusted_long_term_memory"
                or env_raw.get("provenance") in {"n8_memory", "n8_and_n7_mixed"}
                or env_raw.get("n8_tainted")
                or bool(env_raw.get("historical_references"))
            ):
                # Si existe telemetría N7 pura separada, conservamos solo los datos estructurales N7
                # excluyendo cualquier observación narrativa o referencia histórica N8.
                pure_hw = env_raw.get("hardware")
                if isinstance(pure_hw, dict) and pure_hw:
                    task_dict["untrusted_envelope"] = {
                        "provenance": "n7_context_awareness",
                        "trust_level": "untrusted_system_context",
                        "allow_system_prompt_promotion": False,
                        "allow_tool_execution_from_content": False,
                        "allow_permission_grant": False,
                        "hardware": _redact_nested_data(pure_hw),
                    }
                else:
                    task_dict["untrusted_envelope"] = None
            elif exec_tainted or n8_goal_exposed:
                cleaned_env = _redact_nested_data(env_raw)
                if isinstance(cleaned_env, dict):
                    cleaned_env.pop("observations", None)
                    cleaned_env.pop("historical_references", None)
                    task_dict["untrusted_envelope"] = cleaned_env if cleaned_env else None
                else:
                    task_dict["untrusted_envelope"] = None
            else:
                task_dict["untrusted_envelope"] = _redact_nested_data(env_raw)

        if task_tainted:
            stage_label = "replanning" if int(task_dict.get("replan_generation") or 0) > 0 else "planning"
            skel_title, skel_desc, skel_exp = build_skeleton_task_labels(
                task_id=tid,
                skill_name=task_dict.get("skill_name"),
                stage=stage_label,
            )
            grounded_params, _ = separate_grounded_skill_params(
                task_dict.get("skill_params"),
                anchors=anchors,
            )
            if (
                task_dict.get("title") != skel_title
                or task_dict.get("description") != skel_desc
                or task_dict.get("expected_output") != skel_exp
                or (task_dict.get("skill_params") or {}) != grounded_params
            ):
                canonical_dirty = True
                task_dict["title"] = skel_title
                task_dict["description"] = skel_desc
                task_dict["expected_output"] = skel_exp
                task_dict["skill_params"] = grounded_params

            safe_meta: dict[str, Any] = {}
            for mk, mv in meta_cleaned.items():
                if mk in _SAFE_PERSISTED_TASK_META_KEYS:
                    safe_meta[mk] = _remap_nested_ids(mv)
                elif mk == "target_app" and isinstance(mv, str) and mv.lower() in anchors.get("grounded_scalars", set()):
                    safe_meta[mk] = mv
                elif mk == "file_path" and isinstance(mv, str) and mv in (anchors.get("target_paths") or []):
                    safe_meta[mk] = mv
            safe_meta["ephemeral_details_in_ram"] = True
            task_dict["metadata"] = safe_meta
        else:
            task_dict["skill_params"] = _redact_nested_data(task_dict.get("skill_params") or {})
            task_dict["metadata"] = _remap_nested_ids(meta_cleaned)

        if exec_tainted:
            if isinstance(task_dict.get("output_summary"), str) and task_dict["output_summary"].strip():
                task_dict["output_summary"] = (
                    "Resultado procesado bajo frontera consultiva N8 (contenido mantenido solo en RAM)."
                )
            if isinstance(task_dict.get("last_error"), str) and task_dict["last_error"].strip():
                task_dict["last_error"] = "Error de ejecución aislado por frontera N8 (ver estado en RAM)."
            if task_dict.get("error_history"):
                task_dict["error_history"] = ["Error aislado por frontera N8"]
            if isinstance(task_dict.get("execution_evidence"), dict):
                ev_clean = dict(task_dict["execution_evidence"])
                for noisy_k in ("summary", "notes", "upstream_snippets"):
                    if noisy_k in ev_clean:
                        ev_clean[noisy_k] = "[RAM-ONLY]"
                task_dict["execution_evidence"] = _remap_nested_ids(_redact_nested_data(ev_clean))

        clean_task_models.append(GoalTask.from_dict(task_dict))

    # Si un llamador construyó un plan con tareas contaminadas sin separarlas previamente,
    # jamás mutamos silenciosamente un hash aprobado: rechazamos o recalculamos el esqueleto limpio.
    if canonical_dirty:
        if cloned.get("approval_bound_hash") is not None:
            raise GoalPersistenceError(
                "No se puede persistir un plan aprobado cuya estructura contiene campos N8 sin aislar."
            )
        canonical_payload = {
            "plan_id": cloned["plan_id"],
            "goal_id": plan.goal.goal_id,
            "goal_type": plan.goal.goal_type.value,
            "constraints": Constraints.from_dict((goal_dict or {}).get("constraints")).to_dict(),
            "version": int(cloned.get("version", 1)),
            "tasks": [t.canonical_definition_dict() for t in clean_task_models],
        }
        serialized = json.dumps(
            canonical_payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        cloned["plan_hash"] = hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    # 3. Frontera en replan_history e history
    if n8_goal_exposed:
        # Los fallos de skills se guardan en los modelos de tareas antes de llegar al historial.
        # El plan completo queda bajo frontera N8 para que excepciones anidadas no escapen.
        for task_dict in cloned.get("tasks") or []:
            if not isinstance(task_dict, dict):
                continue
            if task_dict.get("last_error"):
                task_dict["last_error"] = "Error aislado por frontera N8 (detalle conservado solo en RAM)."
            if task_dict.get("error_history"):
                task_dict["error_history"] = [
                    "Error aislado por frontera N8 (detalle conservado solo en RAM)."
                ]
            if task_dict.get("output_summary"):
                task_dict["output_summary"] = "Resultado contextual N8 conservado solo en RAM."
            evidence = task_dict.get("execution_evidence")
            if isinstance(evidence, dict):
                for key in ("summary", "notes", "upstream_snippets"):
                    if key in evidence:
                        evidence[key] = "[DETALLE N8 OMITIDO]"

    for decision in cloned.get("replan_history") or []:
        if not isinstance(decision, dict):
            continue
        if tainted_id_remap:
            for key_list in ("preserved_task_ids", "added_task_ids", "cancelled_task_ids"):
                if isinstance(decision.get(key_list), list):
                    decision[key_list] = [_remap_tid(str(x)) for x in decision[key_list]]
            if isinstance(decision.get("trigger_task_id"), str):
                decision["trigger_task_id"] = _remap_tid(decision["trigger_task_id"])
        if n8_goal_exposed or "n8" in str(decision.get("strategy") or "").lower():
            decision["reason"] = "Replanificación ejecutada bajo contexto consultivo N8 (detalle mantenido en RAM)."

    for entry in cloned.get("history") or []:
        if not isinstance(entry, dict):
            continue
        if tainted_id_remap:
            if isinstance(entry.get("task_id"), str):
                entry["task_id"] = _remap_tid(entry["task_id"])
            if isinstance(entry.get("message"), str):
                entry["message"] = _remap_nested_ids(entry["message"])
        details = entry.get("details")
        if isinstance(details, dict):
            cleaned_details = sanitize_wal_payload(_remap_nested_ids(details))
            entry["details"] = cleaned_details
        if entry.get("event_type") == "plan_replanned" and n8_goal_exposed:
            entry["message"] = "Replanificación controlada ejecutada (anotaciones N8 mantenidas solo en RAM)."
            if isinstance(entry.get("details"), dict) and "reason" in entry["details"]:
                entry["details"]["reason"] = "Replanificación ejecutada bajo contexto consultivo N8 (detalle mantenido en RAM)."
        if n8_goal_exposed:
            entry["message"] = "Evento de ejecución bajo contexto consultivo N8 (detalle mantenido en RAM)."
            entry["details"] = sanitize_wal_payload(
                entry.get("details") if isinstance(entry.get("details"), dict) else {},
                n8_context_exposed=True,
            )

    return cloned


def _sanitize_runtime_strings(
    payload: dict[str, Any], *, n8_context_exposed: bool = False
) -> dict[str, Any]:
    """Redacta posibles secretos en mensajes de error, contexto e historial antes de persistir."""
    precleaned = strip_volatile_n8_keys_recursive(payload)
    cloned = json.loads(json.dumps(precleaned, ensure_ascii=False))
    goal_dict = cloned.get("goal")
    if isinstance(goal_dict, dict):
        if isinstance(goal_dict.get("error_summary"), str):
            goal_dict["error_summary"] = (
                "Ejecución con incidencias (detalle contextual N8 mantenido solo en RAM)."
                if n8_context_exposed and goal_dict["error_summary"].strip()
                else redact_secrets(goal_dict["error_summary"])
            )
        if isinstance(goal_dict.get("cancellation_reason"), str):
            goal_dict["cancellation_reason"] = (
                "Objetivo cancelado bajo contexto consultivo N8 (detalle mantenido solo en RAM)."
                if n8_context_exposed and goal_dict["cancellation_reason"].strip()
                else redact_secrets(goal_dict["cancellation_reason"])
            )
        if isinstance(goal_dict.get("context"), dict):
            goal_dict["context"] = _redact_nested_data(goal_dict["context"])
        if isinstance(goal_dict.get("missing_info"), list):
            goal_dict["missing_info"] = [
                redact_secrets(str(item)) for item in goal_dict["missing_info"]
            ]

    for task in cloned.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        if isinstance(task.get("last_error"), str):
            task["last_error"] = redact_secrets(task["last_error"])
        if isinstance(task.get("error_history"), list):
            task["error_history"] = [
                redact_secrets(str(item)) for item in task["error_history"]
            ]
        if isinstance(task.get("output_summary"), str):
            task["output_summary"] = redact_secrets(task["output_summary"])

    for entry in cloned.get("history") or []:
        if isinstance(entry, dict) and isinstance(entry.get("message"), str):
            entry["message"] = (
                "Evento de ejecución bajo contexto consultivo N8 (detalle mantenido en RAM)."
                if n8_context_exposed
                else redact_secrets(entry["message"])
            )
            if n8_context_exposed and isinstance(entry.get("details"), dict):
                entry["details"] = sanitize_wal_payload(entry["details"], n8_context_exposed=True)

    return cloned


def _atomic_write_json(target_path: Path, data: dict[str, Any]) -> None:
    """Escribe JSON de forma atómica con flush + fsync y reintentos breves en Windows."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex[:8]}.tmp")
    encoded = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())

        last_err: Optional[OSError] = None
        for attempt in range(4):
            try:
                os.replace(tmp_path, target_path)
                last_err = None
                break
            except PermissionError as err:
                last_err = err
                time.sleep(0.05 * (attempt + 1))
        if last_err is not None:
            raise last_err
    except Exception as err:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise GoalPersistenceError(f"Error al escribir archivo atómico '{target_path}': {err}") from err


class GoalStore:
    """Repositorio aislado para guardar, auditar y recuperar planes de objetivos N6."""

    is_ephemeral: bool = False

    def __init__(self, store_dir: Optional[Path | str] = None) -> None:
        self._lock = threading.RLock()
        self.store_dir: Path = _validate_store_dir(store_dir)
        self.plans_dir: Path = self.store_dir / "plans"
        self.wal_dir: Path = self.store_dir / "wal"
        self.index_path: Path = self.store_dir / "active_goal.json"
        self.plans_dir.mkdir(parents=True, exist_ok=True)
        self.wal_dir.mkdir(parents=True, exist_ok=True)

    def _plan_snapshot_path(self, plan_id: str) -> Path:
        safe_id = _sanitize_identifier(plan_id, "plan_id")
        return self.plans_dir / f"{safe_id}.goal.json"

    def _plan_wal_path(self, plan_id: str) -> Path:
        safe_id = _sanitize_identifier(plan_id, "plan_id")
        return self.wal_dir / f"{safe_id}.wal.jsonl"

    def save_plan(self, plan: GoalPlan, *, set_active: bool = True) -> Path:
        """Persiste una instantánea validada del `GoalPlan` y actualiza el índice activo."""
        with self._lock:
            if not plan.verify_integrity():
                raise GoalPersistenceError(
                    f"Se rechazó persistir GoalPlan '{plan.plan_id}': hash estructural inválido."
                )
            snapshot_path = self._plan_snapshot_path(plan.plan_id)
            sanitized = extract_persistable_plan_dict(plan, allow_private_ram=False)
            _atomic_write_json(snapshot_path, sanitized)

            if set_active:
                index_data = {
                    "active_plan_id": plan.plan_id,
                    "active_goal_id": plan.goal.goal_id,
                    "goal_status": plan.goal.status.value,
                    "updated_at": utc_now_iso(),
                }
                _atomic_write_json(self.index_path, index_data)
            return snapshot_path

    def _is_plan_persisted_n8_exposed(self, plan_id: str) -> bool:
        snap = self._plan_snapshot_path(plan_id)
        if not snap.exists():
            return False
        try:
            raw = json.loads(snap.read_text(encoding="utf-8"))
            return isinstance(raw, dict) and bool(raw.get("n8_context_exposed"))
        except Exception:
            return False

    def append_wal_event(
        self, plan_id: str, event_type: str, payload: dict[str, Any], *, n8_context_exposed: bool = False
    ) -> None:
        """Agrega un evento incremental redactado al WAL JSONL del plan."""
        with self._lock:
            wal_path = self._plan_wal_path(plan_id)
            eff_exposed = bool(
                n8_context_exposed
                or (isinstance(payload, Mapping) and bool(payload.get("n8_query_id")))
                or self._is_plan_persisted_n8_exposed(plan_id)
            )
            clean_payload = sanitize_wal_payload(payload, n8_context_exposed=eff_exposed)
            record = {
                "timestamp": utc_now_iso(),
                "plan_id": _sanitize_identifier(plan_id, "plan_id"),
                "event_type": str(event_type),
                "payload": clean_payload,
            }
            # Redactamos antes de serializar para que las claves sensibles sigan siendo estructura,
            # no texto JSON ambiguo que pueda evadir la detección.
            line = json.dumps(redact_secrets(record), ensure_ascii=False) + "\n"
            try:
                with open(wal_path, "a", encoding="utf-8") as fh:
                    fh.write(line)
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError as err:
                raise GoalPersistenceError(
                    redact_secrets(f"No se pudo escribir evento WAL para '{plan_id}': {err}")
                ) from err

    def load_plan(self, plan_id: str) -> GoalPlan:
        """Carga un `GoalPlan` desde disco validando integridad estructural."""
        with self._lock:
            snapshot_path = self._plan_snapshot_path(plan_id)
            if not snapshot_path.exists():
                raise GoalPersistenceError(f"No existe ningún plan persistido con id '{plan_id}'.")
            try:
                raw_text = snapshot_path.read_text(encoding="utf-8")
                data = json.loads(raw_text)
            except Exception as err:
                raise GoalPersistenceError(
                    f"Archivo corrupto o ilegible al recuperar '{plan_id}': {err}"
                ) from err
            if not isinstance(data, dict):
                raise GoalPersistenceError(f"Formato inválido en snapshot de '{plan_id}'.")
            return GoalPlan.from_dict(data)

    def load_active_plan(self) -> Optional[GoalPlan]:
        """Recupera el último plan activo persistido, o `None` si aún no existe ninguno."""
        with self._lock:
            if not self.index_path.exists():
                return None
            try:
                idx = json.loads(self.index_path.read_text(encoding="utf-8"))
            except Exception as err:
                raise GoalPersistenceError(f"Índice de objetivo activo corrupto: {err}") from err
            plan_id = (idx or {}).get("active_plan_id")
            if not plan_id:
                return None
            return self.load_plan(str(plan_id))

    def list_plan_ids(self) -> list[str]:
        """Lista todos los identificadores de planes persistidos ordenados alfabéticamente."""
        with self._lock:
            ids: list[str] = []
            for path in sorted(self.plans_dir.glob("*.goal.json")):
                # `name` termina en `.goal.json` (10 caracteres)
                ids.append(path.name[:-10])
            return ids

    def read_wal_events(self, plan_id: str) -> list[dict[str, Any]]:
        """Lee los eventos incrementales válidos de la bitácora WAL de un plan."""
        with self._lock:
            wal_path = self._plan_wal_path(plan_id)
            if not wal_path.exists():
                return []
            events: list[dict[str, Any]] = []
            for line in wal_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, dict):
                        events.append(parsed)
                except Exception:
                    # Ignora líneas truncadas al final tras un cierre abrupto.
                    continue
            return events

    def clear_active_pointer(self) -> None:
        """Limpia únicamente el puntero activo manteniendo trazabilidad histórica."""
        with self._lock:
            if self.index_path.exists():
                _atomic_write_json(
                    self.index_path,
                    {
                        "active_plan_id": None,
                        "active_goal_id": None,
                        "goal_status": None,
                        "updated_at": utc_now_iso(),
                    },
                )


class EphemeralGoalStore:
    """Almacén N6 exclusivamente en RAM para aislamiento real durante el modo privado.

    Garantías estrictas:
    - Cero operaciones de disco: jamás crea `.goal.json`, `.wal.jsonl`, `active_goal.json` ni directorios.
    - Nunca referencia ni muta ninguna instancia persistente de `GoalStore`.
    - Mantiene compatibilidad con la API de `GoalStore` para crear, actualizar, ejecutar,
      pausar y reanudar objetivos privados dentro de la misma sesión privada.
    - Al invocar `clear()` / `discard()` (al salir de `/privado` o finalizar la sesión),
      elimina íntegramente todos los planes, eventos WAL y punteros privados de RAM.
    """

    is_ephemeral: bool = True

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.store_dir: Optional[Path] = None
        self.plans_dir: Optional[Path] = None
        self.wal_dir: Optional[Path] = None
        self.index_path: Optional[Path] = None
        self._plans: dict[str, dict[str, Any]] = {}
        self._wal_events: dict[str, list[dict[str, Any]]] = {}
        self._active_index: Optional[dict[str, Any]] = None

    def save_plan(self, plan: GoalPlan, *, set_active: bool = True) -> Path:
        """Guarda una copia aislada en RAM marcando el plan como privado/no persistible."""
        with self._lock:
            if not plan.verify_integrity():
                raise GoalPersistenceError(
                    f"Se rechazó guardar GoalPlan efímero '{plan.plan_id}': hash estructural inválido."
                )
            plan.is_ephemeral_private = True
            safe_id = _sanitize_identifier(plan.plan_id, "plan_id")
            sanitized = extract_persistable_plan_dict(plan, allow_private_ram=True)
            self._plans[safe_id] = json.loads(json.dumps(sanitized, ensure_ascii=False))
            if set_active:
                self._active_index = {
                    "active_plan_id": safe_id,
                    "active_goal_id": plan.goal.goal_id,
                    "goal_status": plan.goal.status.value,
                    "updated_at": utc_now_iso(),
                }
            # Ruta virtual simbólica no utilizable como archivo real (cero escritura en disco).
            return Path(f"<ephemeral-ram>/plans/{safe_id}.goal.json")

    def append_wal_event(
        self, plan_id: str, event_type: str, payload: dict[str, Any], *, n8_context_exposed: bool = False
    ) -> None:
        """Registra evento WAL exclusivamente en RAM con redacción y limpieza N8 (H-06)."""
        with self._lock:
            safe_id = _sanitize_identifier(plan_id, "plan_id")
            stored_plan = self._plans.get(safe_id)
            eff_exposed = bool(
                n8_context_exposed
                or (isinstance(payload, Mapping) and bool(payload.get("n8_query_id")))
                or (isinstance(stored_plan, Mapping) and bool(stored_plan.get("n8_context_exposed")))
            )
            clean_payload = sanitize_wal_payload(payload, n8_context_exposed=eff_exposed)
            record = {
                "timestamp": utc_now_iso(),
                "plan_id": safe_id,
                "event_type": str(event_type),
                "payload": clean_payload,
            }
            bucket = self._wal_events.setdefault(safe_id, [])
            bucket.append(json.loads(json.dumps(redact_secrets(record), ensure_ascii=False)))

    def load_plan(self, plan_id: str) -> GoalPlan:
        """Reconstruye desde RAM un `GoalPlan` privado validando `plan_hash`."""
        with self._lock:
            safe_id = _sanitize_identifier(plan_id, "plan_id")
            data = self._plans.get(safe_id)
            if not isinstance(data, dict):
                raise GoalPersistenceError(f"No existe ningún plan efímero en RAM con id '{plan_id}'.")
            cloned = json.loads(json.dumps(data, ensure_ascii=False))
            plan = GoalPlan.from_dict(cloned)
            plan.is_ephemeral_private = True
            return plan

    def load_active_plan(self) -> Optional[GoalPlan]:
        """Recupera el último plan privado activo en RAM para reanudarlo durante la sesión privada."""
        with self._lock:
            if not self._active_index:
                return None
            plan_id = self._active_index.get("active_plan_id")
            if not plan_id:
                return None
            return self.load_plan(str(plan_id))

    def list_plan_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._plans.keys())

    def read_wal_events(self, plan_id: str) -> list[dict[str, Any]]:
        with self._lock:
            safe_id = _sanitize_identifier(plan_id, "plan_id")
            events = self._wal_events.get(safe_id) or []
            return [json.loads(json.dumps(ev, ensure_ascii=False)) for ev in events]

    def clear_active_pointer(self) -> None:
        with self._lock:
            if self._active_index is not None:
                self._active_index = {
                    "active_plan_id": None,
                    "active_goal_id": None,
                    "goal_status": None,
                    "updated_at": utc_now_iso(),
                }

    def clear(self) -> None:
        """Descarta todo rastro privado en RAM al salir del modo privado o cerrar sesión."""
        with self._lock:
            self._plans.clear()
            self._wal_events.clear()
            self._active_index = None

    def discard(self) -> None:
        """Alias explícito de `clear()` para cierre de sesión privada."""
        self.clear()

    def active_plan_count(self) -> int:
        with self._lock:
            return len(self._plans)
