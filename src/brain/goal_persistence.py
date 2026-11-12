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

import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Optional
import uuid

from config.settings import BASE_DIR, GOAL_STORE_DIR
from src.brain.exceptions import redact_secrets
from src.brain.goal_models import (
    GoalPersistenceError,
    GoalPlan,
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


def _sanitize_runtime_strings(payload: dict[str, Any]) -> dict[str, Any]:
    """Redacta posibles secretos en mensajes de error e historial antes de persistir."""
    cloned = json.loads(json.dumps(payload, ensure_ascii=False))
    goal_dict = cloned.get("goal")
    if isinstance(goal_dict, dict):
        if isinstance(goal_dict.get("error_summary"), str):
            goal_dict["error_summary"] = redact_secrets(goal_dict["error_summary"])
        if isinstance(goal_dict.get("cancellation_reason"), str):
            goal_dict["cancellation_reason"] = redact_secrets(goal_dict["cancellation_reason"])

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
            entry["message"] = redact_secrets(entry["message"])

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
            sanitized = _sanitize_runtime_strings(plan.to_dict())
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

    def append_wal_event(self, plan_id: str, event_type: str, payload: dict[str, Any]) -> None:
        """Agrega un evento incremental redactado al WAL JSONL del plan."""
        with self._lock:
            wal_path = self._plan_wal_path(plan_id)
            record = {
                "timestamp": utc_now_iso(),
                "plan_id": _sanitize_identifier(plan_id, "plan_id"),
                "event_type": str(event_type),
                "payload": payload,
            }
            line = redact_secrets(json.dumps(record, ensure_ascii=False)) + "\n"
            try:
                with open(wal_path, "a", encoding="utf-8") as fh:
                    fh.write(line)
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError as err:
                raise GoalPersistenceError(
                    f"No se pudo escribir evento WAL para '{plan_id}': {err}"
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
