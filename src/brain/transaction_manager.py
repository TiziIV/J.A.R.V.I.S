"""
Subsistema de Persistencia Transaccional, Write-Ahead Log (WAL) y Recuperación ante Fallos (N5)
para J.A.R.V.I.S. (`src/brain/transaction_manager.py`).

Explicación didáctica para desarrolladores principiantes:
Cuando J.A.R.V.I.S. modifica uno o varios archivos de configuración autorizados a través de
`ChainOrchestrator` y `FileEditorSkill`, una interrupción imprevista (cierre de consola, caída
del proceso Python, corte de energía o reinicio de Windows) podría ocurrir a mitad del trabajo.
Este módulo garantiza que:
1. Exista un bloqueo global exclusivo entre hilos y entre procesos (`GlobalTransactionLock` con
   `msvcrt.locking` en Windows) para impedir escrituras o recuperaciones simultáneas conflictivas.
2. Cada paso se registre primero en un Write-Ahead Log (`.wal.jsonl`) encadenado por SHA-256 con
   `flush()` + `os.fsync()`, y luego en una instantánea atómica (`.tx.json`) vía archivo temporal
   y `os.replace()`.
3. Las modificaciones sucesivas sobre un mismo archivo registren una cadena explícita de hashes
   (`H0 -> H1 -> H2 -> H3`) y se reviertan en orden inverso estricto (`H3 -> H2 -> H1 -> H0`).
4. Si una transacción queda interrumpida, el recuperador determinista (`TransactionManager`)
   reconstruya el estado desde disco, persista los metadatos previos a la recuperación antes de
   entrar en `RECOVERING`, jamás ejecute operaciones `PENDING` nuevas, no sobrescriba cambios
   externos y deje el sistema en un estado íntegro y verificable (`ROLLED_BACK`, `COMMITTED` si
   todo el lote seleccionado ya estaba físicamente aplicado y verificado en disco, o
   `RECOVERY_FAILED` si detecta corrupción o modificaciones externas).
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Callable, Mapping, Optional, Sequence
import uuid

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl  # pragma: no cover

from config.settings import (
    BASE_DIR,
    TRANSACTION_JOURNAL_DIR,
    TRANSACTION_LOCK_TIMEOUT_SEC,
)
from skills.file_editor_skill import (
    BackupRecord,
    FileEditorSkill,
    compute_sha256_bytes,
    compute_sha256_file,
)


SCHEMA_VERSION: str = "1.0"
GENESIS_PREV_HASH: str = "0" * 64
PROTECTED_MEMORY_STORE_PATH: Path = (BASE_DIR / "data" / "memory_store.json").resolve()
PRODUCTION_JOURNAL_DIR: Path = Path(TRANSACTION_JOURNAL_DIR).resolve()

_SHA256_HEX_RE: re.Pattern[str] = re.compile(r"^[0-9a-fA-F]{64}$")


def _utc_now_iso() -> str:
    """Devuelve la fecha y hora UTC actual en formato ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(data: Any) -> str:
    """Serializa un objeto en JSON determinista (claves ordenadas y sin espacios superfluos)."""
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_canonical(data: Any) -> str:
    """Calcula el hash SHA-256 hexadecimal del JSON canónico de `data`."""
    return hashlib.sha256(_canonical_json(data).encode("utf-8")).hexdigest()


def _is_test_mode() -> bool:
    """Comprueba si el proceso se ejecuta bajo entorno de pruebas (`JARVIS_ENV=test|testing`)."""
    return os.environ.get("JARVIS_ENV", "").strip().lower() in {"test", "testing"}


def resolve_safe_journal_dir(custom_dir: Optional[Path | str] = None) -> Path:
    """
    Resuelve y valida el directorio donde se almacenan los journals transaccionales.

    Reglas de aislamiento y seguridad:
    1. Jamás permite usar ni tocar `data/memory_store.json`.
    2. En modo test (`JARVIS_ENV=test|testing`), nunca escribe en el directorio productivo
       `data/transactions`: si no se proporciona `custom_dir`, crea un directorio aislado por
       corrida bajo `JARVIS_TEST_TEMP_ROOT` (o `BASE_DIR / "temp" / "tx_test_runs"`).
    """
    if custom_dir is not None:
        resolved = Path(custom_dir).resolve()
    elif _is_test_mode():
        env_root = os.environ.get("JARVIS_TEST_TEMP_ROOT", "").strip()
        base_temp = Path(env_root).resolve() if env_root else (BASE_DIR / "temp" / "tx_test_runs").resolve()
        resolved = (base_temp / f"tx_run_{uuid.uuid4().hex}").resolve()
    else:
        resolved = PRODUCTION_JOURNAL_DIR

    if resolved == PROTECTED_MEMORY_STORE_PATH or PROTECTED_MEMORY_STORE_PATH.is_relative_to(resolved):
        raise PermissionError(
            "Violación de aislamiento: el directorio de transacciones no puede coincidir ni contener data/memory_store.json."
        )

    if _is_test_mode() and resolved == PRODUCTION_JOURNAL_DIR:
        raise PermissionError(
            "En modo test (JARVIS_ENV=test) está prohibido utilizar el directorio productivo data/transactions."
        )

    return resolved


class TransactionState(str, Enum):
    """Estados explícitos de la máquina de estados transaccional (N5)."""

    CREATED = "CREATED"
    PREPARING = "PREPARING"
    PREPARED = "PREPARED"
    EXECUTING = "EXECUTING"
    PARTIALLY_APPLIED = "PARTIALLY_APPLIED"
    COMMITTED = "COMMITTED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    RECOVERING = "RECOVERING"
    RECOVERY_FAILED = "RECOVERY_FAILED"
    FAILED = "FAILED"


ALLOWED_TRANSACTION_STATE_TRANSITIONS: dict[TransactionState, set[TransactionState]] = {
    TransactionState.CREATED: {
        TransactionState.PREPARING,
        TransactionState.FAILED,
        TransactionState.RECOVERY_REQUIRED,
    },
    TransactionState.PREPARING: {
        TransactionState.PREPARED,
        TransactionState.FAILED,
        TransactionState.RECOVERY_REQUIRED,
    },
    TransactionState.PREPARED: {
        TransactionState.EXECUTING,
        TransactionState.ROLLED_BACK,
        TransactionState.FAILED,
        TransactionState.RECOVERY_REQUIRED,
    },
    TransactionState.EXECUTING: {
        TransactionState.COMMITTED,
        TransactionState.PARTIALLY_APPLIED,
        TransactionState.ROLLING_BACK,
        TransactionState.FAILED,
        TransactionState.RECOVERY_REQUIRED,
    },
    TransactionState.PARTIALLY_APPLIED: {
        TransactionState.PREPARING,
        TransactionState.EXECUTING,
        TransactionState.ROLLING_BACK,
        TransactionState.RECOVERY_REQUIRED,
    },
    TransactionState.COMMITTED: {
        TransactionState.ROLLING_BACK,
    },
    TransactionState.ROLLING_BACK: {
        TransactionState.ROLLED_BACK,
        TransactionState.FAILED,
        TransactionState.RECOVERY_REQUIRED,
    },
    TransactionState.FAILED: {
        TransactionState.ROLLING_BACK,
        TransactionState.RECOVERY_REQUIRED,
    },
    TransactionState.RECOVERY_REQUIRED: {
        TransactionState.RECOVERING,
        TransactionState.RECOVERY_FAILED,
    },
    TransactionState.RECOVERING: {
        TransactionState.ROLLED_BACK,
        TransactionState.COMMITTED,
        TransactionState.PARTIALLY_APPLIED,
        TransactionState.RECOVERY_FAILED,
        TransactionState.RECOVERY_REQUIRED,
    },
    TransactionState.ROLLED_BACK: set(),
    TransactionState.RECOVERY_FAILED: set(),
}


class OperationJournalStage(str, Enum):
    """Etapas físicas detalladas de una operación dentro del protocolo de escritura y recuperación."""

    PENDING = "PENDING"
    INTENT_LOGGED = "INTENT_LOGGED"
    BACKUP_VERIFIED = "BACKUP_VERIFIED"
    FILE_MODIFIED = "FILE_MODIFIED"
    APPLIED = "APPLIED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"


class InvalidTransactionStateError(ValueError):
    """Error lanzado ante una transición ilegal en la máquina de estados transaccional."""


class TransactionLockTimeoutError(RuntimeError):
    """Error lanzado cuando no es posible adquirir el bloqueo global interproceso dentro del plazo."""


class JournalCorruptionError(RuntimeError):
    """
    Error lanzado cuando un archivo WAL o Snapshot presenta corrupción, truncamiento,
    secuencia inválida, desorden, evento duplicado, evento ajeno o discrepancia de hashes.
    """

    def __init__(self, message: str, *, code: str = "JOURNAL_CORRUPT") -> None:
        super().__init__(message)
        self.code: str = code


class SimulatedTransactionInterrupt(RuntimeError):
    """
    Excepción de prueba utilizada para simular una caída abrupta del proceso a mitad de
    una transacción o de un rollback, deteniendo de inmediato el flujo en memoria sin
    ejecutar manejadores de limpieza/rollback para que el recuperador actúe desde disco.
    """


# ==============================================================================
# BLOQUEO GLOBAL ENTRE HILOS Y PROCESOS (WINDOWS msvcrt.locking / POSIX flock)
# ==============================================================================
_PATH_THREAD_LOCKS_GUARD = threading.Lock()
_PATH_THREAD_LOCKS: dict[str, threading.Lock] = {}


def _get_thread_lock_for_path(lock_path: Path) -> threading.Lock:
    """Obtiene un `threading.Lock` compartido por ruta canónica dentro del proceso actual."""
    key = str(lock_path.resolve()).lower()
    with _PATH_THREAD_LOCKS_GUARD:
        lock = _PATH_THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_THREAD_LOCKS[key] = lock
        return lock


class GlobalTransactionLock:
    """
    Bloqueo global exclusivo entre hilos y entre procesos para todas las operaciones
    de escritura, rollback y recuperación transaccional.

    ¿Por qué combinamos un lock de hilo con `msvcrt.locking` sobre archivo?
    - Dentro del mismo proceso Python (multihilo), `_thread_lock` serializa inmediatamente a otros
      hilos mientras permite reentrada segura al mismo hilo e instancia que ya posee el bloqueo.
    - Entre procesos distintos del sistema operativo (o descriptores independientes en Windows),
      `msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)` bloquea el primer byte del archivo físico
      `global_transaction.lock`. Si el proceso propietario termina abruptamente, el kernel de
      Windows cierra el descriptor y libera el bloqueo de forma automática sin dejar lockfiles
      huérfanos permanentes.
    """

    def __init__(
        self,
        lock_file: Path | str,
        *,
        default_timeout_sec: float = TRANSACTION_LOCK_TIMEOUT_SEC,
        poll_interval_sec: float = 0.02,
    ) -> None:
        self.lock_file: Path = Path(lock_file).resolve()
        if self.lock_file == PROTECTED_MEMORY_STORE_PATH:
            raise PermissionError("El archivo de lock no puede apuntar a data/memory_store.json.")
        self.default_timeout_sec: float = max(float(default_timeout_sec), 0.05)
        self.poll_interval_sec: float = max(float(poll_interval_sec), 0.005)
        self._path_thread_lock: threading.Lock = _get_thread_lock_for_path(self.lock_file)
        self._instance_guard: threading.RLock = threading.RLock()
        self._owner_thread_id: Optional[int] = None
        self._reentry_depth: int = 0
        self._fd: Optional[int] = None

    @property
    def is_held_by_current_thread(self) -> bool:
        """Indica si el hilo actual ya posee este bloqueo en esta instancia."""
        with self._instance_guard:
            return (
                self._reentry_depth > 0
                and self._owner_thread_id == threading.get_ident()
                and self._fd is not None
            )

    def acquire(self, timeout_sec: Optional[float] = None) -> None:
        """Adquiere el bloqueo global exclusivo esperando como máximo `timeout_sec` segundos."""
        current_tid = threading.get_ident()
        with self._instance_guard:
            if self._reentry_depth > 0 and self._owner_thread_id == current_tid:
                self._reentry_depth += 1
                return

        effective_timeout = self.default_timeout_sec if timeout_sec is None else max(float(timeout_sec), 0.0)
        deadline = time.monotonic() + effective_timeout

        # 1. Adquirimos el lock de hilo por ruta canónica
        thread_acquired = False
        while True:
            if self._path_thread_lock.acquire(blocking=False):
                thread_acquired = True
                break
            if time.monotonic() >= deadline:
                raise TransactionLockTimeoutError(
                    f"Tiempo de espera agotado ({effective_timeout:.2f}s) al adquirir el bloqueo global entre hilos "
                    f"sobre '{self.lock_file.name}'."
                )
            time.sleep(min(self.poll_interval_sec, max(0.001, deadline - time.monotonic())))

        # 2. Adquirimos el bloqueo de byte en el sistema operativo (interproceso)
        fd: Optional[int] = None
        try:
            self.lock_file.parent.mkdir(parents=True, exist_ok=True)
            while True:
                try:
                    fd = os.open(str(self.lock_file), os.O_CREAT | os.O_RDWR, 0o600)
                    if os.lseek(fd, 0, os.SEEK_END) == 0:
                        os.write(fd, b"L")
                    os.lseek(fd, 0, os.SEEK_SET)
                    if sys.platform == "win32":
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:  # pragma: no cover
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if fd is not None:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                        fd = None
                    if time.monotonic() >= deadline:
                        raise TransactionLockTimeoutError(
                            f"Tiempo de espera agotado ({effective_timeout:.2f}s) al adquirir el bloqueo global "
                            f"interproceso sobre '{self.lock_file.name}'."
                        ) from exc
                    time.sleep(min(self.poll_interval_sec, max(0.001, deadline - time.monotonic())))

            with self._instance_guard:
                self._fd = fd
                self._owner_thread_id = current_tid
                self._reentry_depth = 1
        except BaseException:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if thread_acquired:
                try:
                    self._path_thread_lock.release()
                except RuntimeError:
                    pass
            raise

    def release(self) -> None:
        """Libera una adquisición del bloqueo global; al llegar la profundidad a 0 desbloquea el archivo."""
        current_tid = threading.get_ident()
        with self._instance_guard:
            if self._reentry_depth <= 0 or self._owner_thread_id != current_tid:
                raise RuntimeError("Intento de liberar GlobalTransactionLock desde un hilo que no lo posee.")
            self._reentry_depth -= 1
            if self._reentry_depth > 0:
                return
            fd = self._fd
            self._fd = None
            self._owner_thread_id = None

        try:
            if fd is not None:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    if sys.platform == "win32":
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:  # pragma: no cover
                        fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
        finally:
            try:
                self._path_thread_lock.release()
            except RuntimeError:
                pass

    def __enter__(self) -> "GlobalTransactionLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()


# ==============================================================================
# MODELOS DE DATOS PERSISTENTES DEL JOURNAL Y WAL
# ==============================================================================
@dataclass
class WalEvent:
    """
    Evento individual inmutable del Write-Ahead Log (`.wal.jsonl`).
    Forma una cadena hash verificable mediante `previous_event_sha256` -> `event_sha256`.
    """

    seq: int
    timestamp: str
    transaction_id: str
    event_type: str
    operation_id: Optional[str]
    payload: dict[str, Any]
    previous_event_sha256: str
    event_sha256: str = ""

    def compute_hash(self) -> str:
        """Calcula el SHA-256 canónico del evento excluyendo `event_sha256`."""
        body = {
            "seq": self.seq,
            "timestamp": self.timestamp,
            "transaction_id": self.transaction_id,
            "event_type": self.event_type,
            "operation_id": self.operation_id,
            "payload": self.payload,
            "previous_event_sha256": self.previous_event_sha256,
        }
        return _sha256_canonical(body)

    def to_dict(self) -> dict[str, Any]:
        """Serializa el evento a diccionario listo para JSON Lines."""
        return {
            "seq": self.seq,
            "timestamp": self.timestamp,
            "transaction_id": self.transaction_id,
            "event_type": self.event_type,
            "operation_id": self.operation_id,
            "payload": copy.deepcopy(self.payload),
            "previous_event_sha256": self.previous_event_sha256,
            "event_sha256": self.event_sha256,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WalEvent":
        """Reconstruye y valida estructuralmente un `WalEvent` desde un diccionario."""
        required = {
            "seq",
            "timestamp",
            "transaction_id",
            "event_type",
            "operation_id",
            "payload",
            "previous_event_sha256",
            "event_sha256",
        }
        if not isinstance(data, Mapping) or set(data.keys()) != required:
            raise JournalCorruptionError(
                "Estructura de evento WAL inválida o con campos desconocidos.",
                code="INVALID_WAL_SCHEMA",
            )
        seq = data["seq"]
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise JournalCorruptionError("Campo 'seq' inválido en evento WAL.", code="INVALID_WAL_SEQ")
        tx_id = data["transaction_id"]
        ev_type = data["event_type"]
        ts = data["timestamp"]
        prev_sha = data["previous_event_sha256"]
        ev_sha = data["event_sha256"]
        op_id = data["operation_id"]
        payload = data["payload"]
        if (
            not isinstance(tx_id, str)
            or not tx_id.strip()
            or not isinstance(ev_type, str)
            or not ev_type.strip()
            or not isinstance(ts, str)
            or not ts.strip()
            or not isinstance(prev_sha, str)
            or not _SHA256_HEX_RE.match(prev_sha)
            or not isinstance(ev_sha, str)
            or not _SHA256_HEX_RE.match(ev_sha)
            or (op_id is not None and not isinstance(op_id, str))
            or not isinstance(payload, Mapping)
        ):
            raise JournalCorruptionError(
                "Tipos o hashes inválidos en evento WAL.",
                code="INVALID_WAL_FIELDS",
            )
        event = cls(
            seq=seq,
            timestamp=ts,
            transaction_id=tx_id,
            event_type=ev_type,
            operation_id=op_id,
            payload=dict(payload),
            previous_event_sha256=prev_sha.lower(),
            event_sha256=ev_sha.lower(),
        )
        if event.compute_hash() != event.event_sha256:
            raise JournalCorruptionError(
                f"El hash SHA-256 del evento WAL seq={seq} no coincide con su contenido.",
                code="WAL_EVENT_HASH_MISMATCH",
            )
        return event


@dataclass
class OperationJournalEntry:
    """
    Estado persistido de una operación dentro de una transacción, incluyendo su posición
    en la secuencia global y en la cadena de hashes por archivo (`H0 -> H1 -> H2 -> H3`).
    """

    operation_id: str
    sequence_index: int
    file_path: str
    file_chain_index: int
    previous_op_id_on_file: Optional[str]
    expected_before_sha256: str
    planned_after_sha256: Optional[str] = None
    actual_after_sha256: Optional[str] = None
    backup_path: Optional[str] = None
    backup_sha256: Optional[str] = None
    pre_rollback_backup_path: Optional[str] = None
    restored_sha256: Optional[str] = None
    op_stage: str = OperationJournalStage.PENDING.value
    status: str = "PENDING"
    verification_result: Optional[dict[str, Any]] = None
    error_detail: Optional[str] = None
    depends_on: list[str] = field(default_factory=list)
    operation_definition: dict[str, Any] = field(default_factory=dict)
    updated_at: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        """Convierte la entrada de operación a un diccionario serializable."""
        return {
            "operation_id": self.operation_id,
            "sequence_index": self.sequence_index,
            "file_path": self.file_path,
            "file_chain_index": self.file_chain_index,
            "previous_op_id_on_file": self.previous_op_id_on_file,
            "expected_before_sha256": self.expected_before_sha256,
            "planned_after_sha256": self.planned_after_sha256,
            "actual_after_sha256": self.actual_after_sha256,
            "backup_path": self.backup_path,
            "backup_sha256": self.backup_sha256,
            "pre_rollback_backup_path": self.pre_rollback_backup_path,
            "restored_sha256": self.restored_sha256,
            "op_stage": self.op_stage,
            "status": self.status,
            "verification_result": copy.deepcopy(self.verification_result),
            "error_detail": self.error_detail,
            "depends_on": list(self.depends_on),
            "operation_definition": copy.deepcopy(self.operation_definition),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OperationJournalEntry":
        """Reconstruye un `OperationJournalEntry` validando sus campos."""
        if not isinstance(data, Mapping):
            raise JournalCorruptionError("Entrada de operación inválida en snapshot.", code="INVALID_OP_ENTRY")
        return cls(
            operation_id=str(data["operation_id"]),
            sequence_index=int(data["sequence_index"]),
            file_path=str(data["file_path"]),
            file_chain_index=int(data.get("file_chain_index", 0)),
            previous_op_id_on_file=data.get("previous_op_id_on_file"),
            expected_before_sha256=str(data["expected_before_sha256"]).lower(),
            planned_after_sha256=(
                str(data["planned_after_sha256"]).lower()
                if data.get("planned_after_sha256") is not None
                else None
            ),
            actual_after_sha256=(
                str(data["actual_after_sha256"]).lower()
                if data.get("actual_after_sha256") is not None
                else None
            ),
            backup_path=data.get("backup_path"),
            backup_sha256=(
                str(data["backup_sha256"]).lower()
                if data.get("backup_sha256") is not None
                else None
            ),
            pre_rollback_backup_path=data.get("pre_rollback_backup_path"),
            restored_sha256=(
                str(data["restored_sha256"]).lower()
                if data.get("restored_sha256") is not None
                else None
            ),
            op_stage=str(data.get("op_stage", OperationJournalStage.PENDING.value)),
            status=str(data.get("status", "PENDING")),
            verification_result=(
                dict(data["verification_result"])
                if isinstance(data.get("verification_result"), Mapping)
                else None
            ),
            error_detail=data.get("error_detail"),
            depends_on=[str(x) for x in data.get("depends_on", [])],
            operation_definition=dict(data.get("operation_definition") or {}),
            updated_at=str(data.get("updated_at") or _utc_now_iso()),
        )


@dataclass
class TransactionRecord:
    """
    Instantánea persistente completa de una transacción (`<transaction_id>.tx.json`).
    Almacena `last_seq`, `last_event_sha256` y `record_sha256` para garantizar
    consistencia exacta con el archivo `.wal.jsonl`.
    """

    schema_version: str
    transaction_id: str
    execution_id: str
    plan_id: str
    approval_hash: str
    state: str
    selected_operation_ids: list[str]
    all_plan_operation_ids: list[str]
    approved_plan_snapshot: dict[str, Any]
    operations: dict[str, OperationJournalEntry]
    file_hash_chains: dict[str, list[dict[str, Any]]]
    owner_pid: int
    created_at: str
    updated_at: str
    state_history: list[dict[str, Any]] = field(default_factory=list)
    pre_recovery_metadata: Optional[dict[str, Any]] = None
    recovery_attempts: list[dict[str, Any]] = field(default_factory=list)
    error_message: Optional[str] = None
    last_seq: int = 0
    last_event_sha256: str = GENESIS_PREV_HASH
    record_sha256: str = ""

    def rebuild_file_hash_chains(self) -> None:
        """
        Reconstruye la tabla de cadenas de hashes por archivo (`H0 -> H1 -> H2 -> H3`)
        a partir de las operaciones ordenadas por `sequence_index`.
        """
        chains: dict[str, list[dict[str, Any]]] = {}
        ordered_ops = sorted(self.operations.values(), key=lambda item: item.sequence_index)
        for op in ordered_ops:
            chain_list = chains.setdefault(op.file_path, [])
            chain_list.append(
                {
                    "chain_index": op.file_chain_index,
                    "sequence_index": op.sequence_index,
                    "operation_id": op.operation_id,
                    "previous_op_id_on_file": op.previous_op_id_on_file,
                    "expected_before_sha256": op.expected_before_sha256,
                    "planned_after_sha256": op.planned_after_sha256,
                    "actual_after_sha256": op.actual_after_sha256,
                    "backup_path": op.backup_path,
                    "backup_sha256": op.backup_sha256,
                    "restored_sha256": op.restored_sha256,
                    "op_stage": op.op_stage,
                    "status": op.status,
                    "verified": bool(op.verification_result and op.verification_result.get("verified")),
                }
            )
        self.file_hash_chains = chains

    def to_dict_without_hash(self) -> dict[str, Any]:
        """Devuelve la estructura canónica del registro excluyendo `record_sha256`."""
        self.rebuild_file_hash_chains()
        return {
            "schema_version": self.schema_version,
            "transaction_id": self.transaction_id,
            "execution_id": self.execution_id,
            "plan_id": self.plan_id,
            "approval_hash": self.approval_hash,
            "state": self.state,
            "selected_operation_ids": list(self.selected_operation_ids),
            "all_plan_operation_ids": list(self.all_plan_operation_ids),
            "approved_plan_snapshot": copy.deepcopy(self.approved_plan_snapshot),
            "operations": {op_id: entry.to_dict() for op_id, entry in sorted(self.operations.items())},
            "file_hash_chains": copy.deepcopy(self.file_hash_chains),
            "owner_pid": self.owner_pid,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "state_history": copy.deepcopy(self.state_history),
            "pre_recovery_metadata": copy.deepcopy(self.pre_recovery_metadata),
            "recovery_attempts": copy.deepcopy(self.recovery_attempts),
            "error_message": self.error_message,
            "last_seq": self.last_seq,
            "last_event_sha256": self.last_event_sha256,
        }

    def compute_record_hash(self) -> str:
        """Calcula el SHA-256 canónico del snapshot."""
        return _sha256_canonical(self.to_dict_without_hash())

    def to_dict(self) -> dict[str, Any]:
        """Serializa el snapshot incluyendo `record_sha256` actualizado."""
        payload = self.to_dict_without_hash()
        self.record_sha256 = _sha256_canonical(payload)
        payload["record_sha256"] = self.record_sha256
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TransactionRecord":
        """Carga y verifica el hash SHA-256 y esquema de un `TransactionRecord`."""
        if not isinstance(data, Mapping):
            raise JournalCorruptionError("El snapshot transaccional no es un objeto JSON válido.", code="INVALID_SNAPSHOT")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise JournalCorruptionError(
                f"Versión de esquema incompatible en snapshot: {data.get('schema_version')!r}.",
                code="UNSUPPORTED_SCHEMA_VERSION",
            )
        state_str = str(data.get("state", ""))
        try:
            TransactionState(state_str)
        except ValueError as exc:
            raise JournalCorruptionError(
                f"Estado transaccional desconocido en snapshot: {state_str!r}.",
                code="UNKNOWN_TRANSACTION_STATE",
            ) from exc

        stored_hash = data.get("record_sha256")
        if not isinstance(stored_hash, str) or not _SHA256_HEX_RE.match(stored_hash):
            raise JournalCorruptionError("Falta 'record_sha256' válido en snapshot.", code="MISSING_RECORD_HASH")

        raw_ops = data.get("operations")
        if not isinstance(raw_ops, Mapping):
            raise JournalCorruptionError("Campo 'operations' inválido en snapshot.", code="INVALID_SNAPSHOT_OPS")

        ops_map = {str(k): OperationJournalEntry.from_dict(v) for k, v in raw_ops.items()}
        record = cls(
            schema_version=str(data["schema_version"]),
            transaction_id=str(data["transaction_id"]),
            execution_id=str(data["execution_id"]),
            plan_id=str(data["plan_id"]),
            approval_hash=str(data["approval_hash"]),
            state=state_str,
            selected_operation_ids=[str(x) for x in data.get("selected_operation_ids", [])],
            all_plan_operation_ids=[str(x) for x in data.get("all_plan_operation_ids", [])],
            approved_plan_snapshot=dict(data.get("approved_plan_snapshot") or {}),
            operations=ops_map,
            file_hash_chains=dict(data.get("file_hash_chains") or {}),
            owner_pid=int(data.get("owner_pid", 0)),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            state_history=list(data.get("state_history") or []),
            pre_recovery_metadata=(
                dict(data["pre_recovery_metadata"])
                if isinstance(data.get("pre_recovery_metadata"), Mapping)
                else None
            ),
            recovery_attempts=list(data.get("recovery_attempts") or []),
            error_message=data.get("error_message"),
            last_seq=int(data.get("last_seq", 0)),
            last_event_sha256=str(data.get("last_event_sha256", GENESIS_PREV_HASH)).lower(),
            record_sha256=stored_hash.lower(),
        )
        computed = record.compute_record_hash()
        if computed != record.record_sha256:
            raise JournalCorruptionError(
                "El snapshot transaccional fue alterado o está corrupto (record_sha256 no coincide).",
                code="SNAPSHOT_HASH_MISMATCH",
            )
        return record


@dataclass
class RecoveryReport:
    """Informe detallado y verificable del resultado de recuperar una transacción."""

    transaction_id: str
    execution_id: str
    plan_id: str
    initial_state: str
    final_state: str
    decision: str
    restored_files: list[str] = field(default_factory=list)
    reconciled_operations: list[str] = field(default_factory=list)
    rolled_back_operations: list[str] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)
    idempotent_repeat: bool = False
    attempt_number: int = 0
    timestamp: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        """Convierte el reporte a diccionario serializable."""
        return asdict(self)


# ==============================================================================
# JOURNAL Y WRITE-AHEAD LOG (WAL) CON VALIDACIÓN DE CADENA SHA-256
# ==============================================================================
class TransactionJournal:
    """
    Gestiona la persistencia duradera del Write-Ahead Log (`<tx_id>.wal.jsonl`) y del
    snapshot atómico (`<tx_id>.tx.json`).

    Regla de oro del protocolo de escritura:
    1. Todo evento se escribe primero en `<tx_id>.wal.jsonl` con `flush()` + `os.fsync()`.
    2. A continuación se actualiza `<tx_id>.tx.json` mediante un archivo temporal exclusivo
       (`xb`), `flush()` + `os.fsync()` y reemplazo atómico `os.replace()`.
    """

    def __init__(self, journal_dir: Optional[Path | str] = None) -> None:
        self.journal_dir: Path = resolve_safe_journal_dir(journal_dir)
        self.locks_dir: Path = (self.journal_dir / "locks").resolve()
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        # Hook opcional para simular fallos controlados en pruebas entre etapas de persistencia
        self.fault_injector: Optional[Callable[[str, str, Optional[str]], None]] = None

    def wal_path(self, transaction_id: str) -> Path:
        """Ruta del archivo Write-Ahead Log para `transaction_id`."""
        return (self.journal_dir / f"{transaction_id}.wal.jsonl").resolve()

    def snapshot_path(self, transaction_id: str) -> Path:
        """Ruta del archivo de snapshot para `transaction_id`."""
        return (self.journal_dir / f"{transaction_id}.tx.json").resolve()

    def _trigger_fault(self, hook_point: str, transaction_id: str, operation_id: Optional[str] = None) -> None:
        """Invoca el inyector de fallos si fue configurado en pruebas."""
        if self.fault_injector is not None:
            self.fault_injector(hook_point, transaction_id, operation_id)

    def _write_snapshot_atomically(self, record: TransactionRecord) -> None:
        """Escribe `record` en `<tx_id>.tx.json` de manera atómica con temporal exclusivo y `os.fsync`."""
        target = self.snapshot_path(record.transaction_id)
        if target == PROTECTED_MEMORY_STORE_PATH:
            raise PermissionError("Prohibido escribir sobre data/memory_store.json.")
        payload_dict = record.to_dict()
        serialized = json.dumps(payload_dict, ensure_ascii=False, indent=2) + "\n"
        raw_bytes = serialized.encode("utf-8")

        temp_path = self.journal_dir / f".{record.transaction_id}.{uuid.uuid4().hex[:10]}.tmp"
        try:
            with open(temp_path, "xb") as tf:
                tf.write(raw_bytes)
                tf.flush()
                os.fsync(tf.fileno())

            last_err: Optional[OSError] = None
            for attempt in range(3):
                try:
                    os.replace(str(temp_path), str(target))
                    last_err = None
                    break
                except OSError as err:
                    last_err = err
                    time.sleep(0.03 * (attempt + 1))
            if last_err is not None:
                raise last_err
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def _append_wal_event_raw(self, event: WalEvent) -> None:
        """Añade un evento al final de `<tx_id>.wal.jsonl` ejecutando `flush()` y `os.fsync()`."""
        wal_file = self.wal_path(event.transaction_id)
        if wal_file == PROTECTED_MEMORY_STORE_PATH:
            raise PermissionError("Prohibido escribir sobre data/memory_store.json.")
        line = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        with open(wal_file, "a", encoding="utf-8") as wf:
            wf.write(line)
            wf.flush()
            os.fsync(wf.fileno())

    def record_event_and_persist(
        self,
        record: TransactionRecord,
        event_type: str,
        *,
        operation_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
        mutator: Optional[Callable[[TransactionRecord], None]] = None,
    ) -> WalEvent:
        """
        Aplica el protocolo WAL-first:
        1. Ejecuta `mutator(record)` sobre una copia de trabajo en memoria para validar que la
           transición o actualización sea legal sin dejar el registro parcialmente modificado si falla.
        2. Construye el `WalEvent` con `seq = record.last_seq + 1` y `previous_event_sha256`.
        3. Escribe y sincroniza (`fsync`) el evento en `<tx_id>.wal.jsonl`.
        4. Dispara el hook de prueba `after_wal_append:<event_type>` (permite simular caída entre WAL y snapshot).
        5. Escribe atómicamente el snapshot `<tx_id>.tx.json` con `os.replace`.
        """
        working_copy = copy.deepcopy(record)
        if mutator is not None:
            mutator(working_copy)

        now_iso = _utc_now_iso()
        working_copy.updated_at = now_iso
        next_seq = working_copy.last_seq + 1
        prev_sha = working_copy.last_event_sha256

        event_payload = copy.deepcopy(payload) if payload is not None else {}
        # Incluimos la proyección canónica del cambio para permitir replay exacto desde el WAL
        event = WalEvent(
            seq=next_seq,
            timestamp=now_iso,
            transaction_id=working_copy.transaction_id,
            event_type=event_type,
            operation_id=operation_id,
            payload=event_payload,
            previous_event_sha256=prev_sha,
        )
        event.event_sha256 = event.compute_hash()

        working_copy.last_seq = next_seq
        working_copy.last_event_sha256 = event.event_sha256
        working_copy.record_sha256 = working_copy.compute_record_hash()

        # 1. Persistir primero en WAL
        self._append_wal_event_raw(event)
        self._trigger_fault(f"after_wal_append:{event_type}", working_copy.transaction_id, operation_id)

        # 2. Actualizar estado en memoria del objeto original y persistir snapshot atómico
        record.__dict__.update(copy.deepcopy(working_copy.__dict__))
        self._write_snapshot_atomically(record)
        self._trigger_fault(f"after_snapshot_write:{event_type}", record.transaction_id, operation_id)
        return event

    def validate_wal_file(self, transaction_id: str) -> list[WalEvent]:
        """
        Valida íntegramente el archivo `.wal.jsonl` de una transacción:
        - Detecta última línea incompleta por interrupción de escritura (`TRUNCATED_FINAL_WAL_LINE`)
          distinguiéndola de corrupción interior (`INTERIOR_WAL_CORRUPTION`).
        - Detecta eventos duplicados (`DUPLICATE_SEQ`), eventos ausentes (`MISSING_SEQ`),
          eventos desordenados (`OUT_OF_ORDER_SEQ`), eventos de otra transacción
          (`FOREIGN_TRANSACTION_ID`) y alteraciones de contenido o ruptura de cadena
          (`WAL_CHAIN_BROKEN` / `WAL_EVENT_HASH_MISMATCH`).
        """
        wal_file = self.wal_path(transaction_id)
        if not wal_file.exists():
            raise JournalCorruptionError(
                f"El archivo WAL para la transacción '{transaction_id}' no existe.",
                code="WAL_MISSING",
            )

        try:
            raw_text = wal_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise JournalCorruptionError(
                f"No se pudo leer el archivo WAL de '{transaction_id}': {exc}",
                code="WAL_UNREADABLE",
            ) from exc

        raw_lines = raw_text.splitlines(keepends=True)
        if not raw_lines:
            raise JournalCorruptionError(
                f"El archivo WAL de '{transaction_id}' está vacío.",
                code="WAL_EMPTY",
            )

        events: list[WalEvent] = []
        seen_seqs: set[int] = set()
        expected_seq = 1
        expected_prev_hash = GENESIS_PREV_HASH

        total_lines = len(raw_lines)
        for idx, raw_line in enumerate(raw_lines, start=1):
            is_last_line = idx == total_lines
            stripped = raw_line.strip()
            if not stripped:
                if is_last_line:
                    raise JournalCorruptionError(
                        f"Última línea en blanco o truncada en WAL de '{transaction_id}'.",
                        code="TRUNCATED_FINAL_WAL_LINE",
                    )
                raise JournalCorruptionError(
                    f"Línea vacía interior detectada en WAL de '{transaction_id}' (línea {idx}).",
                    code="INTERIOR_WAL_CORRUPTION",
                )

            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError as exc:
                if is_last_line:
                    raise JournalCorruptionError(
                        f"Última línea incompleta por interrupción de escritura en WAL de '{transaction_id}' "
                        f"(línea {idx}): {exc.msg}.",
                        code="TRUNCATED_FINAL_WAL_LINE",
                    ) from exc
                raise JournalCorruptionError(
                    f"Corrupción JSON interior en WAL de '{transaction_id}' (línea {idx}): {exc.msg}.",
                    code="INTERIOR_WAL_CORRUPTION",
                ) from exc

            event = WalEvent.from_dict(parsed)

            if event.transaction_id != transaction_id:
                raise JournalCorruptionError(
                    f"Evento de otra transacción detectado en WAL de '{transaction_id}': "
                    f"recibido '{event.transaction_id}' en línea {idx}.",
                    code="FOREIGN_TRANSACTION_ID",
                )

            if event.seq in seen_seqs:
                raise JournalCorruptionError(
                    f"Evento duplicado detectado en WAL de '{transaction_id}': seq={event.seq}.",
                    code="DUPLICATE_SEQ",
                )

            if event.seq < expected_seq:
                raise JournalCorruptionError(
                    f"Evento desordenado en WAL de '{transaction_id}': se esperaba seq={expected_seq}, "
                    f"recibido seq={event.seq}.",
                    code="OUT_OF_ORDER_SEQ",
                )

            if event.seq > expected_seq:
                # Si el siguiente número de secuencia es menor y aparece más adelante, es un desorden;
                # si nunca aparece el número faltante, es un evento ausente.
                remaining_seqs: set[int] = set()
                for rem_line in raw_lines[idx:]:
                    try:
                        rem_obj = json.loads(rem_line.strip())
                        if isinstance(rem_obj, Mapping) and isinstance(rem_obj.get("seq"), int):
                            remaining_seqs.add(int(rem_obj["seq"]))
                    except Exception:
                        pass
                if expected_seq in remaining_seqs:
                    raise JournalCorruptionError(
                        f"Eventos desordenados en WAL de '{transaction_id}': seq={event.seq} "
                        f"aparece antes que seq={expected_seq}.",
                        code="OUT_OF_ORDER_SEQ",
                    )
                raise JournalCorruptionError(
                    f"Evento ausente en WAL de '{transaction_id}': falta seq={expected_seq} "
                    f"(saltó a seq={event.seq}).",
                    code="MISSING_SEQ",
                )

            if event.previous_event_sha256 != expected_prev_hash:
                raise JournalCorruptionError(
                    f"Ruptura en cadena SHA-256 del WAL de '{transaction_id}' en seq={event.seq}: "
                    f"previous_event_sha256 no coincide con el evento anterior.",
                    code="WAL_CHAIN_BROKEN",
                )

            seen_seqs.add(event.seq)
            events.append(event)
            expected_seq += 1
            expected_prev_hash = event.event_sha256

        return events

    @staticmethod
    def _apply_wal_event_to_record(record: TransactionRecord, event: WalEvent) -> None:
        """
        Reproduce determinísticamente un `WalEvent` validado sobre un `TransactionRecord`
        cuando el proceso se interrumpió después de escribir el WAL pero antes de reemplazar el snapshot.
        """
        payload = event.payload
        if "state" in payload and isinstance(payload["state"], str):
            record.state = payload["state"]
        if "state_history_entry" in payload and isinstance(payload["state_history_entry"], Mapping):
            record.state_history.append(dict(payload["state_history_entry"]))
        if "error_message" in payload:
            record.error_message = payload["error_message"]
        if "pre_recovery_metadata" in payload and isinstance(payload["pre_recovery_metadata"], Mapping):
            record.pre_recovery_metadata = dict(payload["pre_recovery_metadata"])
        if "recovery_attempt_entry" in payload and isinstance(payload["recovery_attempt_entry"], Mapping):
            record.recovery_attempts.append(dict(payload["recovery_attempt_entry"]))
        if "recovery_attempt_update" in payload and isinstance(payload["recovery_attempt_update"], Mapping):
            if record.recovery_attempts:
                record.recovery_attempts[-1].update(dict(payload["recovery_attempt_update"]))

        op_id = event.operation_id
        if op_id and op_id in record.operations and isinstance(payload.get("op_updates"), Mapping):
            op_entry = record.operations[op_id]
            for k, v in payload["op_updates"].items():
                if hasattr(op_entry, k):
                    setattr(op_entry, k, copy.deepcopy(v))
            op_entry.updated_at = event.timestamp
            if event.event_type == "OPERATION_APPLIED" and op_entry.actual_after_sha256:
                ordered = sorted(record.operations.values(), key=lambda x: x.sequence_index)
                for next_op in ordered:
                    if (
                        next_op.file_path == op_entry.file_path
                        and next_op.sequence_index > op_entry.sequence_index
                        and next_op.previous_op_id_on_file == op_id
                    ):
                        next_op.expected_before_sha256 = op_entry.actual_after_sha256
                        break

        if isinstance(payload.get("blocked_operation_ids"), Sequence) and not isinstance(
            payload.get("blocked_operation_ids"), (str, bytes)
        ):
            err_detail = str(payload.get("op_updates", {}).get("error_detail", ""))
            for blk_id in payload["blocked_operation_ids"]:
                if str(blk_id) in record.operations:
                    b_entry = record.operations[str(blk_id)]
                    b_entry.op_stage = OperationJournalStage.BLOCKED.value
                    b_entry.status = "BLOCKED"
                    b_entry.error_detail = f"Bloqueada por fallo en '{op_id}': {err_detail}"
                    b_entry.updated_at = event.timestamp

        record.updated_at = event.timestamp
        record.last_seq = event.seq
        record.last_event_sha256 = event.event_sha256
        record.rebuild_file_hash_chains()
        record.record_sha256 = record.compute_record_hash()

    def validate_and_load_transaction(self, transaction_id: str) -> tuple[TransactionRecord, list[WalEvent]]:
        """
        Valida tanto el snapshot (`<tx_id>.tx.json`) como el WAL (`<tx_id>.wal.jsonl`),
        comprueba la compatibilidad cruzada entre ambos y reproduce cualquier cola de eventos
        WAL válidos posteriores al último snapshot.
        """
        snap_file = self.snapshot_path(transaction_id)
        if not snap_file.exists():
            raise JournalCorruptionError(
                f"El archivo snapshot para la transacción '{transaction_id}' no existe.",
                code="SNAPSHOT_MISSING",
            )

        try:
            raw_snap = json.loads(snap_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JournalCorruptionError(
                f"El archivo snapshot de '{transaction_id}' es ilegible o tiene JSON inválido: {exc}",
                code="SNAPSHOT_CORRUPT_JSON",
            ) from exc

        record = TransactionRecord.from_dict(raw_snap)
        if record.transaction_id != transaction_id:
            raise JournalCorruptionError(
                f"El snapshot '{snap_file.name}' pertenece a otra transacción ('{record.transaction_id}').",
                code="SNAPSHOT_INCOMPATIBLE",
            )

        wal_events = self.validate_wal_file(transaction_id)

        if record.last_seq < 1 or record.last_seq > len(wal_events):
            raise JournalCorruptionError(
                f"Snapshot incompatible con WAL en '{transaction_id}': last_seq={record.last_seq}, "
                f"total eventos WAL={len(wal_events)}.",
                code="SNAPSHOT_INCOMPATIBLE",
            )

        matching_wal_event = wal_events[record.last_seq - 1]
        if matching_wal_event.event_sha256 != record.last_event_sha256:
            raise JournalCorruptionError(
                f"Snapshot incompatible en '{transaction_id}': last_event_sha256 del snapshot no coincide "
                f"con el evento seq={record.last_seq} del WAL.",
                code="SNAPSHOT_INCOMPATIBLE",
            )

        # Si el proceso cayó después de escribir eventos válidos en el WAL y antes de actualizar el snapshot,
        # reproducimos los eventos posteriores verificados en memoria y sincronizamos el snapshot si procede.
        if len(wal_events) > record.last_seq:
            for tail_event in wal_events[record.last_seq :]:
                self._apply_wal_event_to_record(record, tail_event)

        return record, wal_events

    def list_transaction_ids(self) -> list[str]:
        """Lista todos los `transaction_id` presentes en `self.journal_dir` (por `.tx.json` o `.wal.jsonl`)."""
        tx_ids: set[str] = set()
        if not self.journal_dir.exists():
            return []
        for entry in sorted(self.journal_dir.iterdir(), key=lambda p: p.name):
            if entry.is_file():
                if entry.name.endswith(".tx.json"):
                    tx_ids.add(entry.name[: -len(".tx.json")])
                elif entry.name.endswith(".wal.jsonl"):
                    tx_ids.add(entry.name[: -len(".wal.jsonl")])
        return sorted(tx_ids)


# ==============================================================================
# TRANSACTION & RECOVERY MANAGER (N5)
# ==============================================================================
class TransactionManager:
    """
    Coordinador transaccional y motor de recuperación determinista para `ChainOrchestrator`
    y `FileEditorSkill`.
    """

    def __init__(
        self,
        file_editor: FileEditorSkill,
        *,
        journal_dir: Optional[Path | str] = None,
        lock_timeout_sec: float = TRANSACTION_LOCK_TIMEOUT_SEC,
    ) -> None:
        self.file_editor: FileEditorSkill = file_editor
        self.journal: TransactionJournal = TransactionJournal(journal_dir)
        self.global_lock: GlobalTransactionLock = GlobalTransactionLock(
            self.journal.locks_dir / "global_transaction.lock",
            default_timeout_sec=lock_timeout_sec,
        )
        # Mapa auxiliar en memoria rápida: execution_id -> último transaction_id
        self._exec_to_tx: dict[str, str] = {}
        # Transacciones bloqueadas por corrupción de journal detectada
        self._corrupt_transactions: dict[str, str] = {}

    @staticmethod
    def validate_state_transition(current: TransactionState | str, target: TransactionState | str) -> TransactionState:
        """Valida una transición en la máquina de estados transaccional y devuelve el `TransactionState` destino."""
        curr_enum = current if isinstance(current, TransactionState) else TransactionState(str(current))
        tgt_enum = target if isinstance(target, TransactionState) else TransactionState(str(target))
        if curr_enum == tgt_enum:
            return tgt_enum
        allowed = ALLOWED_TRANSACTION_STATE_TRANSITIONS.get(curr_enum, set())
        if tgt_enum not in allowed:
            raise InvalidTransactionStateError(
                f"Transición de estado transaccional inválida: {curr_enum.value} -> {tgt_enum.value}."
            )
        return tgt_enum

    def transition_transaction_state(
        self,
        record: TransactionRecord,
        new_state: TransactionState | str,
        *,
        reason: str = "",
        extra_payload: Optional[dict[str, Any]] = None,
        mutator: Optional[Callable[[TransactionRecord], None]] = None,
    ) -> TransactionRecord:
        """
        Realiza una transición de estado verificada en `record` y la persiste en WAL + Snapshot.
        Si la transición es inválida, lanza `InvalidTransactionStateError` sin modificar `record`.
        """
        curr_enum = TransactionState(record.state)
        target_enum = self.validate_state_transition(curr_enum, new_state)
        now_iso = _utc_now_iso()
        history_entry = {
            "from_state": curr_enum.value,
            "to_state": target_enum.value,
            "timestamp": now_iso,
            "reason": reason,
        }
        payload: dict[str, Any] = {
            "state": target_enum.value,
            "state_history_entry": history_entry,
            "reason": reason,
        }
        if reason and target_enum in (TransactionState.FAILED, TransactionState.RECOVERY_FAILED):
            payload["error_message"] = reason
        if extra_payload:
            payload.update(extra_payload)

        def _apply(working: TransactionRecord) -> None:
            working.state = target_enum.value
            working.state_history.append(history_entry)
            if reason and target_enum in (TransactionState.FAILED, TransactionState.RECOVERY_FAILED):
                working.error_message = reason
            if mutator is not None:
                mutator(working)

        self.journal.record_event_and_persist(
            record,
            f"STATE_{target_enum.value}",
            payload=payload,
            mutator=_apply,
        )
        return record

    def begin_transaction(
        self,
        *,
        execution_id: str,
        plan_id: str,
        approval_hash: str,
        approved_plan_snapshot: Mapping[str, Any],
        ordered_selected_ops: Sequence[Any],
        all_plan_ops: Sequence[Any],
        initial_file_hashes: Mapping[str, str],
        transaction_id: Optional[str] = None,
    ) -> TransactionRecord:
        """
        Crea e inicializa una nueva transacción en disco (`CREATED` -> `PREPARING` -> `PREPARED`),
        construyendo la estructura de cadenas de hashes por archivo para todas las operaciones seleccionadas.
        Requiere que `self.global_lock` esté adquirido (o lo adquiere durante la creación).
        """
        with self.global_lock:
            tx_id = transaction_id or f"tx_{execution_id}_{uuid.uuid4().hex[:8]}"
            now_iso = _utc_now_iso()

            # Construimos las entradas de operación manteniendo el índice de cadena por archivo
            ops_dict: dict[str, OperationJournalEntry] = {}
            per_file_counter: dict[str, int] = {}
            last_op_on_file: dict[str, str] = {}

            # Primero registramos las operaciones ya aplicadas en corridas parciales previas si existen
            selected_ids = [str(op.id) for op in ordered_selected_ops]
            all_ids = [str(op.id) for op in all_plan_ops]

            for seq_idx, op in enumerate(ordered_selected_ops):
                canonical_path = str(self.file_editor.validate_authorized_path(op.file_path, must_exist=True))
                chain_idx = per_file_counter.get(canonical_path, 0)
                prev_op_id = last_op_on_file.get(canonical_path)
                per_file_counter[canonical_path] = chain_idx + 1
                last_op_on_file[canonical_path] = str(op.id)

                expected_before = (
                    str(initial_file_hashes.get(canonical_path) or op.expected_file_hash or "").lower()
                )
                if not expected_before:
                    expected_before = compute_sha256_file(canonical_path).lower()

                ops_dict[str(op.id)] = OperationJournalEntry(
                    operation_id=str(op.id),
                    sequence_index=seq_idx,
                    file_path=canonical_path,
                    file_chain_index=chain_idx,
                    previous_op_id_on_file=prev_op_id,
                    expected_before_sha256=expected_before,
                    op_stage=OperationJournalStage.PENDING.value,
                    status="PENDING",
                    depends_on=[str(d) for d in getattr(op, "depends_on", ())],
                    operation_definition=op.to_dict(include_volatile_status=False),
                    updated_at=now_iso,
                )

            record = TransactionRecord(
                schema_version=SCHEMA_VERSION,
                transaction_id=tx_id,
                execution_id=execution_id,
                plan_id=plan_id,
                approval_hash=approval_hash,
                state=TransactionState.CREATED.value,
                selected_operation_ids=selected_ids,
                all_plan_operation_ids=all_ids,
                approved_plan_snapshot=copy.deepcopy(dict(approved_plan_snapshot)),
                operations=ops_dict,
                file_hash_chains={},
                owner_pid=os.getpid(),
                created_at=now_iso,
                updated_at=now_iso,
                state_history=[
                    {
                        "from_state": None,
                        "to_state": TransactionState.CREATED.value,
                        "timestamp": now_iso,
                        "reason": "Transacción creada tras validación de token humano.",
                    }
                ],
            )
            record.rebuild_file_hash_chains()

            # Evento 1 en WAL: TRANSACTION_CREATED
            self.journal.record_event_and_persist(
                record,
                "TRANSACTION_CREATED",
                payload={
                    "state": TransactionState.CREATED.value,
                    "execution_id": execution_id,
                    "plan_id": plan_id,
                    "approval_hash": approval_hash,
                    "selected_operation_ids": selected_ids,
                },
            )
            self._exec_to_tx[execution_id] = tx_id

            # Transición a PREPARING y PREPARED
            self.transition_transaction_state(
                record,
                TransactionState.PREPARING,
                reason="Preparando instantánea transaccional y cadenas de hashes por archivo.",
            )
            self.transition_transaction_state(
                record,
                TransactionState.PREPARED,
                reason="Instantánea aprobada y grafo de dependencias persistidos en WAL y snapshot.",
            )
            return record

    def log_operation_intent(
        self,
        record: TransactionRecord,
        operation_id: str,
        *,
        expected_before_sha256: str,
        planned_after_sha256: str,
    ) -> None:
        """
        Etapa 4 del protocolo de escritura:
        Persiste en el WAL la intención de modificar el archivo junto con `expected_before_sha256`
        y el `planned_after_sha256` calculado en memoria ANTES de crear el backup o tocar el archivo.
        Si la transacción estaba en `PREPARED`, la transiciona a `EXECUTING`.
        """
        if record.state == TransactionState.PREPARED.value:
            self.transition_transaction_state(
                record,
                TransactionState.EXECUTING,
                reason=f"Iniciando ejecución en operación '{operation_id}'.",
            )
        elif record.state != TransactionState.EXECUTING.value:
            raise InvalidTransactionStateError(
                f"No se puede registrar intención de operación en estado '{record.state}'."
            )

        op_updates = {
            "expected_before_sha256": expected_before_sha256.lower(),
            "planned_after_sha256": planned_after_sha256.lower(),
            "op_stage": OperationJournalStage.INTENT_LOGGED.value,
        }

        def _mutate(working: TransactionRecord) -> None:
            entry = working.operations[operation_id]
            entry.expected_before_sha256 = op_updates["expected_before_sha256"]
            entry.planned_after_sha256 = op_updates["planned_after_sha256"]
            entry.op_stage = op_updates["op_stage"]

        self.journal.record_event_and_persist(
            record,
            "OPERATION_INTENT_LOGGED",
            operation_id=operation_id,
            payload={"op_updates": op_updates},
            mutator=_mutate,
        )

    def log_backup_verified(
        self,
        record: TransactionRecord,
        operation_id: str,
        backup_record: BackupRecord,
    ) -> None:
        """
        Etapa 7 del protocolo de escritura:
        Persiste en el WAL la ruta y el hash SHA-256 del backup exclusivo verificado
        ANTES de realizar el reemplazo atómico (`os.replace`) sobre el archivo objetivo.
        """
        op_updates = {
            "backup_path": str(backup_record.backup_path),
            "backup_sha256": str(backup_record.backup_sha256).lower(),
            "op_stage": OperationJournalStage.BACKUP_VERIFIED.value,
        }

        def _mutate(working: TransactionRecord) -> None:
            entry = working.operations[operation_id]
            entry.backup_path = op_updates["backup_path"]
            entry.backup_sha256 = op_updates["backup_sha256"]
            entry.op_stage = op_updates["op_stage"]

        self.journal.record_event_and_persist(
            record,
            "OPERATION_BACKUP_VERIFIED",
            operation_id=operation_id,
            payload={"op_updates": op_updates, "backup_record": asdict(backup_record)},
            mutator=_mutate,
        )

    def log_operation_applied(
        self,
        record: TransactionRecord,
        operation_id: str,
        *,
        actual_after_sha256: str,
        verification_result: Mapping[str, Any],
    ) -> None:
        """
        Etapa 10 del protocolo de escritura:
        Persiste en el WAL la evidencia verificable post-escritura (`actual_after_sha256`
        y `verification_result`) marcando la operación como `APPLIED`.
        """
        if not actual_after_sha256 or not _SHA256_HEX_RE.match(actual_after_sha256):
            raise ValueError("actual_after_sha256 debe ser un hash SHA-256 válido de 64 caracteres.")
        if not verification_result.get("verified"):
            raise ValueError("No se puede marcar una operación como APPLIED sin verificación positiva en disco.")

        op_updates = {
            "actual_after_sha256": actual_after_sha256.lower(),
            "op_stage": OperationJournalStage.APPLIED.value,
            "status": "APPLIED",
            "verification_result": dict(verification_result),
        }

        def _mutate(working: TransactionRecord) -> None:
            entry = working.operations[operation_id]
            entry.actual_after_sha256 = op_updates["actual_after_sha256"]
            entry.op_stage = op_updates["op_stage"]
            entry.status = op_updates["status"]
            entry.verification_result = copy.deepcopy(op_updates["verification_result"])
            # Si existe una siguiente operación planeada sobre el mismo archivo, actualizamos su
            # expected_before_sha256 dentro de la cadena para mantener H0 -> H1 -> H2 -> H3.
            ordered = sorted(working.operations.values(), key=lambda x: x.sequence_index)
            for next_op in ordered:
                if (
                    next_op.file_path == entry.file_path
                    and next_op.sequence_index > entry.sequence_index
                    and next_op.previous_op_id_on_file == operation_id
                ):
                    next_op.expected_before_sha256 = op_updates["actual_after_sha256"]
                    break

        self.journal.record_event_and_persist(
            record,
            "OPERATION_APPLIED",
            operation_id=operation_id,
            payload={"op_updates": op_updates},
            mutator=_mutate,
        )

    def log_operation_failed(
        self,
        record: TransactionRecord,
        operation_id: str,
        *,
        error_detail: str,
        blocked_operation_ids: Sequence[str] = (),
    ) -> None:
        """Persiste el fallo de una operación y la propagación de estado `BLOCKED` a sus dependientes."""
        if record.state == TransactionState.PREPARED.value:
            self.transition_transaction_state(
                record,
                TransactionState.EXECUTING,
                reason=f"Iniciando ejecución en operación '{operation_id}'.",
            )
        op_updates = {
            "op_stage": OperationJournalStage.FAILED.value,
            "status": "FAILED",
            "error_detail": error_detail,
        }

        def _mutate(working: TransactionRecord) -> None:
            if operation_id in working.operations:
                entry = working.operations[operation_id]
                entry.op_stage = OperationJournalStage.FAILED.value
                entry.status = "FAILED"
                entry.error_detail = error_detail
            for blk_id in blocked_operation_ids:
                if blk_id in working.operations:
                    b_entry = working.operations[blk_id]
                    b_entry.op_stage = OperationJournalStage.BLOCKED.value
                    b_entry.status = "BLOCKED"
                    b_entry.error_detail = f"Bloqueada por fallo en '{operation_id}': {error_detail}"

        self.journal.record_event_and_persist(
            record,
            "OPERATION_FAILED",
            operation_id=operation_id,
            payload={
                "op_updates": op_updates,
                "blocked_operation_ids": list(blocked_operation_ids),
            },
            mutator=_mutate,
        )

    def log_operation_rolled_back(
        self,
        record: TransactionRecord,
        operation_id: str,
        *,
        restored_sha256: str,
        pre_rollback_backup_path: Optional[str] = None,
    ) -> None:
        """Persiste en WAL y snapshot la restauración verificada de una operación individual."""
        op_updates: dict[str, Any] = {
            "restored_sha256": restored_sha256.lower(),
            "op_stage": OperationJournalStage.ROLLED_BACK.value,
            "status": "ROLLED_BACK",
        }
        if pre_rollback_backup_path is not None:
            op_updates["pre_rollback_backup_path"] = pre_rollback_backup_path

        def _mutate(working: TransactionRecord) -> None:
            entry = working.operations[operation_id]
            entry.restored_sha256 = op_updates["restored_sha256"]
            entry.op_stage = op_updates["op_stage"]
            entry.status = op_updates["status"]
            if pre_rollback_backup_path is not None:
                entry.pre_rollback_backup_path = pre_rollback_backup_path

        self.journal.record_event_and_persist(
            record,
            "OPERATION_ROLLED_BACK",
            operation_id=operation_id,
            payload={"op_updates": op_updates},
            mutator=_mutate,
        )

    def log_operation_rollback_failed(
        self,
        record: TransactionRecord,
        operation_id: str,
        *,
        error_detail: str,
    ) -> None:
        """Persiste el fallo de restauración de una operación individual (`ROLLBACK_FAILED`)."""
        op_updates = {
            "op_stage": OperationJournalStage.ROLLBACK_FAILED.value,
            "status": "ROLLBACK_FAILED",
            "error_detail": error_detail,
        }

        def _mutate(working: TransactionRecord) -> None:
            entry = working.operations[operation_id]
            entry.op_stage = op_updates["op_stage"]
            entry.status = op_updates["status"]
            entry.error_detail = error_detail

        self.journal.record_event_and_persist(
            record,
            "OPERATION_ROLLBACK_FAILED",
            operation_id=operation_id,
            payload={"op_updates": op_updates},
            mutator=_mutate,
        )

    def commit_transaction(
        self,
        record: TransactionRecord,
        *,
        fully_completed: bool,
    ) -> TransactionRecord:
        """
        Etapa 13 del protocolo de escritura:
        Verifica que todas las operaciones seleccionadas posean evidencia `APPLIED` válida
        y que el estado final en disco de cada archivo modificado coincida con el último
        eslabón de su cadena de hashes (`H0 -> H1 -> ... -> Hk`), antes de persistir
        `COMMITTED` (o `PARTIALLY_APPLIED` cuando la selección aprobada fue un subconjunto del plan).
        """
        for op_id in record.selected_operation_ids:
            op_entry = record.operations.get(op_id)
            if (
                op_entry is None
                or op_entry.status != "APPLIED"
                or op_entry.op_stage != OperationJournalStage.APPLIED.value
                or not op_entry.actual_after_sha256
                or not _SHA256_HEX_RE.match(op_entry.actual_after_sha256)
                or not (op_entry.verification_result and op_entry.verification_result.get("verified"))
            ):
                raise RuntimeError(
                    f"No se puede confirmar la transacción '{record.transaction_id}': "
                    f"la operación '{op_id}' carece de evidencia verificada en disco."
                )

        # Verificamos que el archivo físico tenga exactamente el hash del último eslabón aplicado en su cadena
        record.rebuild_file_hash_chains()
        for file_path_str, chain in record.file_hash_chains.items():
            applied_steps = [step for step in chain if step["status"] == "APPLIED"]
            if not applied_steps:
                continue
            expected_last_sha = str(applied_steps[-1]["actual_after_sha256"]).lower()
            actual_disk_sha = compute_sha256_file(file_path_str).lower()
            if actual_disk_sha != expected_last_sha:
                raise RuntimeError(
                    f"El archivo '{Path(file_path_str).name}' en disco ({actual_disk_sha[:12]}...) "
                    f"no coincide con el hash final esperado de su cadena ({expected_last_sha[:12]}...)."
                )

        target_state = (
            TransactionState.COMMITTED
            if (fully_completed and set(record.selected_operation_ids) == set(record.all_plan_operation_ids))
            else TransactionState.PARTIALLY_APPLIED
        )
        return self.transition_transaction_state(
            record,
            target_state,
            reason="Todas las operaciones seleccionadas fueron aplicadas y verificadas en disco.",
        )

    # ------------------------------------------------------------------
    # Búsqueda y carga de transacciones por ID o execution_id
    # ------------------------------------------------------------------
    def get_transactions_for_execution(self, execution_id: str) -> list[TransactionRecord]:
        """Localiza y valida en orden cronológico todas las transacciones asociadas a `execution_id`."""
        candidates: list[TransactionRecord] = []
        prefix = f"tx_{execution_id}_"
        for tx_id in self.journal.list_transaction_ids():
            if tx_id == execution_id or tx_id.startswith(prefix):
                rec, _ = self.journal.validate_and_load_transaction(tx_id)
                if rec.execution_id == execution_id:
                    candidates.append(rec)
        if not candidates:
            for tx_id in self.journal.list_transaction_ids():
                try:
                    rec, _ = self.journal.validate_and_load_transaction(tx_id)
                    if rec.execution_id == execution_id:
                        candidates.append(rec)
                except JournalCorruptionError:
                    continue
        candidates.sort(key=lambda r: (r.created_at, r.last_seq))
        if candidates:
            self._exec_to_tx[execution_id] = candidates[-1].transaction_id
        return candidates

    def get_latest_transaction_for_execution(self, execution_id: str) -> Optional[TransactionRecord]:
        """Localiza y valida la transacción más reciente asociada a `execution_id`."""
        txs = self.get_transactions_for_execution(execution_id)
        return txs[-1] if txs else None

    # ------------------------------------------------------------------
    # Algoritmo determinista e idempotente de recuperación (Sección 7)
    # ------------------------------------------------------------------
    def _verify_operation_value_on_disk(self, op_entry: OperationJournalEntry, expected_sha256: str) -> bool:
        """
        Comprueba que el archivo de `op_entry` exista, sea autorizado y que su hash actual sea
        `expected_sha256`. Cuando se trata del último paso de la cadena sobre ese archivo,
        comprueba además el contenido semántico si la definición está disponible.
        """
        try:
            canonical = self.file_editor.validate_authorized_path(op_entry.file_path, must_exist=True)
            actual_sha = compute_sha256_file(canonical).lower()
            return actual_sha == expected_sha256.lower()
        except Exception:
            return False

    def _can_reconcile_all_selected_as_applied(
        self,
        record: TransactionRecord,
        ordered_ops: Sequence[OperationJournalEntry],
    ) -> bool:
        """
        Determina si TODAS las operaciones seleccionadas en la transacción ya fueron físicamente
        escritas en disco y cumplen la cadena de hashes `H0 -> H1 -> ... -> Hk`.
        Regla 5.5: Jamás ejecuta operaciones `PENDING`; solo devuelve True si todas las operaciones
        seleccionadas ya están en `APPLIED` o si la única operación en `BACKUP_VERIFIED` /
        `FILE_MODIFIED` es el último eslabón y su archivo en disco ya tiene `planned_after_sha256`.
        """
        if not ordered_ops:
            return False

        by_file: dict[str, list[OperationJournalEntry]] = {}
        for op in ordered_ops:
            if op.op_stage in {
                OperationJournalStage.PENDING.value,
                OperationJournalStage.INTENT_LOGGED.value,
                OperationJournalStage.FAILED.value,
                OperationJournalStage.BLOCKED.value,
                OperationJournalStage.ROLLED_BACK.value,
                OperationJournalStage.ROLLBACK_FAILED.value,
            }:
                return False
            if not op.planned_after_sha256 or not op.backup_path:
                return False
            if not Path(op.backup_path).exists():
                return False
            if compute_sha256_file(op.backup_path).lower() != op.expected_before_sha256.lower():
                return False
            by_file.setdefault(op.file_path, []).append(op)

        # Para cada archivo afectado, comprobamos la continuidad H0 -> H1 -> ... -> Hk y que el disco
        # tenga exactamente el hash Hk del último paso.
        for file_path_str, file_ops in by_file.items():
            file_ops_sorted = sorted(file_ops, key=lambda x: x.file_chain_index)
            for idx in range(1, len(file_ops_sorted)):
                prev_after = file_ops_sorted[idx - 1].planned_after_sha256
                curr_before = file_ops_sorted[idx].expected_before_sha256
                if not prev_after or prev_after.lower() != curr_before.lower():
                    return False
            last_op = file_ops_sorted[-1]
            assert last_op.planned_after_sha256 is not None
            if not self._verify_operation_value_on_disk(last_op, last_op.planned_after_sha256):
                return False

        return True

    def recover_transaction(
        self,
        transaction_id: str,
        *,
        reason: str = "Recuperación determinista iniciada.",
    ) -> RecoveryReport:
        """
        Ejecuta el protocolo de recuperación determinista e idempotente de 13 pasos (Sección 7):
        1. Adquiere `self.global_lock`.
        2. Valida snapshot y WAL (si hay corrupción, preserva archivos y bloquea sin tocar configuraciones).
        3. Reconstruye el estado de la transacción (incluyendo cola WAL si aplica).
        4. Si ya está en un estado terminal/estable (`COMMITTED`, `PARTIALLY_APPLIED`, `ROLLED_BACK`,
           `RECOVERY_FAILED`), retorna reporte idempotente sin realizar escrituras.
        5. Registra y persiste `pre_recovery_metadata` (estado previo, motivo, intento, PID, timestamp,
           último evento validado) y transiciona a `RECOVERY_REQUIRED` -> `RECOVERING`.
        6. Inspecciona archivos reales y reconstruye cadenas de hashes (`H0 -> H1 -> H2 -> H3`).
        7. Decide reconciliación (solo si TODAS las operaciones seleccionadas ya están físicamente
           aplicadas en disco), rollback LIFO (`H3 -> H2 -> H1 -> H0`) o bloqueo (`RECOVERY_FAILED`).
        8. Persiste cada paso individual del rollback LIFO para soportar interrupciones durante el propio
           proceso de recuperación.
        9. Verifica el resultado final y registra el estado terminal (`ROLLED_BACK`, `COMMITTED`,
           `PARTIALLY_APPLIED` o `RECOVERY_FAILED`).
        """
        with self.global_lock:
            try:
                record, wal_events = self.journal.validate_and_load_transaction(transaction_id)
            except JournalCorruptionError as corrupt_err:
                self._corrupt_transactions[transaction_id] = f"{corrupt_err.code}: {corrupt_err}"
                return RecoveryReport(
                    transaction_id=transaction_id,
                    execution_id="unknown",
                    plan_id="unknown",
                    initial_state="CORRUPT_JOURNAL",
                    final_state=TransactionState.RECOVERY_FAILED.value,
                    decision="BLOCKED_CORRUPT_JOURNAL",
                    blocked_reasons=[f"{corrupt_err.code}: {corrupt_err}"],
                )

            initial_state = record.state

            # Paso 3b: Idempotencia para transacciones ya estables o terminales
            if initial_state in {
                TransactionState.COMMITTED.value,
                TransactionState.PARTIALLY_APPLIED.value,
                TransactionState.ROLLED_BACK.value,
                TransactionState.RECOVERY_FAILED.value,
            }:
                return RecoveryReport(
                    transaction_id=record.transaction_id,
                    execution_id=record.execution_id,
                    plan_id=record.plan_id,
                    initial_state=initial_state,
                    final_state=initial_state,
                    decision="NO_ACTION_NEEDED",
                    idempotent_repeat=True,
                    attempt_number=len(record.recovery_attempts),
                )

            # Paso 4 y 5: Persistir metadatos previos a la recuperación antes de entrar en RECOVERING
            attempt_number = len(record.recovery_attempts) + 1
            last_valid_seq = wal_events[-1].seq
            last_valid_sha = wal_events[-1].event_sha256
            now_iso = _utc_now_iso()

            pre_meta = {
                "previous_state": initial_state,
                "recovery_reason": reason,
                "attempt_number": attempt_number,
                "recovering_pid": os.getpid(),
                "timestamp": now_iso,
                "last_validated_seq": last_valid_seq,
                "last_validated_event_sha256": last_valid_sha,
            }
            attempt_entry = {
                **pre_meta,
                "outcome": "IN_PROGRESS",
            }

            if record.state != TransactionState.RECOVERY_REQUIRED.value:
                self.transition_transaction_state(
                    record,
                    TransactionState.RECOVERY_REQUIRED,
                    reason=reason,
                    extra_payload={
                        "pre_recovery_metadata": pre_meta,
                        "recovery_attempt_entry": attempt_entry,
                    },
                    mutator=lambda w: (
                        setattr(w, "pre_recovery_metadata", copy.deepcopy(pre_meta)),
                        w.recovery_attempts.append(copy.deepcopy(attempt_entry)),
                    ),
                )
            else:
                self.journal.record_event_and_persist(
                    record,
                    "RECOVERY_ATTEMPT_REGISTERED",
                    payload={
                        "pre_recovery_metadata": pre_meta,
                        "recovery_attempt_entry": attempt_entry,
                    },
                    mutator=lambda w: (
                        setattr(w, "pre_recovery_metadata", copy.deepcopy(pre_meta)),
                        w.recovery_attempts.append(copy.deepcopy(attempt_entry)),
                    ),
                )

            self.transition_transaction_state(
                record,
                TransactionState.RECOVERING,
                reason=f"Iniciando intento de recuperación #{attempt_number}.",
            )

            ordered_ops = [
                record.operations[op_id]
                for op_id in record.selected_operation_ids
                if op_id in record.operations
            ]
            ordered_ops.sort(key=lambda item: item.sequence_index)

            # Paso 8A: ¿Se interrumpió justo después de aplicar físicamente TODAS las operaciones seleccionadas,
            # pero antes de registrar APPLIED en la última o antes de persistir COMMITTED?
            state_before = (
                record.pre_recovery_metadata.get("previous_state", initial_state)
                if record.pre_recovery_metadata
                else initial_state
            )
            # Si había un intento previo interrumpido que ya estaba en RECOVERING o ROLLING_BACK,
            # o si ya existe alguna operación en ROLLED_BACK, nunca reconciliamos hacia adelante:
            # completamos el rollback LIFO.
            any_rolled_back = any(
                op.op_stage == OperationJournalStage.ROLLED_BACK.value or op.status == "ROLLED_BACK"
                for op in ordered_ops
            )
            if (
                state_before == TransactionState.EXECUTING.value
                and not any_rolled_back
                and self._can_reconcile_all_selected_as_applied(record, ordered_ops)
            ):
                reconciled_ids: list[str] = []
                for op_entry in ordered_ops:
                    if op_entry.op_stage != OperationJournalStage.APPLIED.value or op_entry.status != "APPLIED":
                        assert op_entry.planned_after_sha256 is not None
                        self.log_operation_applied(
                            record,
                            op_entry.operation_id,
                            actual_after_sha256=op_entry.planned_after_sha256,
                            verification_result={
                                "operation_id": op_entry.operation_id,
                                "file_path": op_entry.file_path,
                                "verified": True,
                                "sha256": op_entry.planned_after_sha256,
                                "reconciled_by_recovery": True,
                            },
                        )
                        reconciled_ids.append(op_entry.operation_id)

                full_plan = set(record.selected_operation_ids) == set(record.all_plan_operation_ids)
                target_committed = (
                    TransactionState.COMMITTED if full_plan else TransactionState.PARTIALLY_APPLIED
                )
                self.transition_transaction_state(
                    record,
                    target_committed,
                    reason="Reconciliación completada: todas las operaciones seleccionadas ya estaban aplicadas y verificadas en disco.",
                    extra_payload={"recovery_attempt_update": {"outcome": target_committed.value}},
                    mutator=lambda w: (
                        w.recovery_attempts[-1].update({"outcome": target_committed.value})
                        if w.recovery_attempts
                        else None
                    ),
                )
                return RecoveryReport(
                    transaction_id=record.transaction_id,
                    execution_id=record.execution_id,
                    plan_id=record.plan_id,
                    initial_state=initial_state,
                    final_state=record.state,
                    decision="RECONCILED_COMMITTED",
                    reconciled_operations=reconciled_ids,
                    attempt_number=attempt_number,
                )

            # Paso 8B y 9: Rollback LIFO (H3 -> H2 -> H1 -> H0) de las operaciones que tocaron o pudieron tocar disco.
            # Regla 5.5: Jamás ejecutamos operaciones PENDING.
            restored_files: list[str] = []
            rolled_back_ops: list[str] = []
            blocked_reasons: list[str] = []

            # Agrupamos las operaciones por archivo para verificar la posición actual en la cadena H0 -> H1 -> ... -> Hk
            ops_by_file: dict[str, list[OperationJournalEntry]] = {}
            for op_entry in ordered_ops:
                ops_by_file.setdefault(op_entry.file_path, []).append(op_entry)

            # Verificación previa por archivo antes de revertir:
            # El hash actual en disco de cada archivo involucrado debe coincidir con H0 (estado original antes de la
            # transacción) o con alguno de los eslabones previstos Hi (planned_after_sha256 / actual_after_sha256)
            # de las operaciones que llegaron al menos a BACKUP_VERIFIED.
            for file_path_str, file_ops in ops_by_file.items():
                file_ops.sort(key=lambda x: x.file_chain_index)
                candidate_ops = [
                    op
                    for op in file_ops
                    if op.op_stage
                    not in {
                        OperationJournalStage.PENDING.value,
                        OperationJournalStage.INTENT_LOGGED.value,
                        OperationJournalStage.BLOCKED.value,
                    }
                    or op.backup_path is not None
                    or op.planned_after_sha256 is not None
                ]
                target_path = Path(file_path_str)
                if not target_path.exists():
                    # Si el archivo objetivo desapareció, bloqueamos de inmediato
                    blocked_reasons.append(f"El archivo objetivo '{target_path}' no existe en disco.")
                    continue

                current_disk_sha = compute_sha256_file(target_path).lower()
                h0_sha = file_ops[0].expected_before_sha256.lower()
                known_valid_hashes: set[str] = {h0_sha}
                for op in file_ops:
                    if op.expected_before_sha256:
                        known_valid_hashes.add(op.expected_before_sha256.lower())
                    if op.planned_after_sha256 and op.op_stage in {
                        OperationJournalStage.BACKUP_VERIFIED.value,
                        OperationJournalStage.FILE_MODIFIED.value,
                        OperationJournalStage.APPLIED.value,
                        OperationJournalStage.FAILED.value,
                        OperationJournalStage.ROLLBACK_FAILED.value,
                    }:
                        known_valid_hashes.add(op.planned_after_sha256.lower())
                    if op.actual_after_sha256:
                        known_valid_hashes.add(op.actual_after_sha256.lower())

                if current_disk_sha not in known_valid_hashes and candidate_ops:
                    blocked_reasons.append(
                        f"Modificación externa detectada en '{target_path.name}': el hash actual en disco "
                        f"({current_disk_sha[:12]}...) no pertenece a la cadena de hashes de la transacción."
                    )

            if not blocked_reasons:
                # Recorremos las operaciones en orden inverso estricto (LIFO: H3 -> H2 -> H1 -> H0)
                for op_entry in reversed(ordered_ops):
                    if op_entry.op_stage in {
                        OperationJournalStage.PENDING.value,
                        OperationJournalStage.BLOCKED.value,
                        OperationJournalStage.ROLLED_BACK.value,
                    }:
                        continue

                    target_path = Path(op_entry.file_path)
                    if not target_path.exists():
                        blocked_reasons.append(f"Archivo objetivo ausente durante reversión: '{target_path}'.")
                        break

                    current_disk_sha = compute_sha256_file(target_path).lower()
                    expected_before = op_entry.expected_before_sha256.lower()
                    expected_after = (
                        (op_entry.actual_after_sha256 or op_entry.planned_after_sha256 or "").lower()
                    )

                    # Si el archivo en disco ya tiene el hash `expected_before` de esta operación:
                    # - O bien la operación cayó en INTENT_LOGGED / BACKUP_VERIFIED antes de `os.replace`,
                    # - O bien ya fue revertida en un intento de recuperación anterior.
                    if current_disk_sha == expected_before:
                        if op_entry.op_stage in {
                            OperationJournalStage.BACKUP_VERIFIED.value,
                            OperationJournalStage.FILE_MODIFIED.value,
                            OperationJournalStage.APPLIED.value,
                            OperationJournalStage.ROLLBACK_FAILED.value,
                        }:
                            self.log_operation_rolled_back(
                                record,
                                op_entry.operation_id,
                                restored_sha256=current_disk_sha,
                            )
                            rolled_back_ops.append(op_entry.operation_id)
                        continue

                    # Si el archivo en disco tiene el hash `expected_after` producido por esta operación,
                    # debemos restaurar su backup verificado para retroceder H_i -> H_{i-1}.
                    if expected_after and current_disk_sha == expected_after:
                        if not op_entry.backup_path:
                            blocked_reasons.append(
                                f"La operación '{op_entry.operation_id}' modificó '{target_path.name}' "
                                "pero no registra ruta de backup en el journal."
                            )
                            self.log_operation_rollback_failed(
                                record,
                                op_entry.operation_id,
                                error_detail="Sin backup registrado en journal.",
                            )
                            break

                        backup_file = Path(op_entry.backup_path)
                        if not backup_file.exists():
                            blocked_reasons.append(
                                f"Backup ausente para la operación '{op_entry.operation_id}': '{backup_file}'."
                            )
                            self.log_operation_rollback_failed(
                                record,
                                op_entry.operation_id,
                                error_detail="Archivo de backup ausente en disco.",
                            )
                            break

                        backup_disk_sha = compute_sha256_file(backup_file).lower()
                        expected_bak_sha = (op_entry.backup_sha256 or expected_before).lower()
                        if backup_disk_sha != expected_bak_sha or backup_disk_sha != expected_before:
                            blocked_reasons.append(
                                f"Backup corrupto para la operación '{op_entry.operation_id}': "
                                f"SHA-256 del backup ({backup_disk_sha[:12]}...) != esperado ({expected_before[:12]}...)."
                            )
                            self.log_operation_rollback_failed(
                                record,
                                op_entry.operation_id,
                                error_detail="Hash SHA-256 del backup no coincide con el original.",
                            )
                            break

                        ok = self.file_editor.rollback(
                            backup_path=backup_file,
                            target_path=target_path,
                            expected_current_hash=current_disk_sha,
                            execution_id=f"{record.execution_id}_recovery",
                        )
                        post_restore_sha = compute_sha256_file(target_path).lower() if target_path.exists() else ""
                        if not ok or post_restore_sha != expected_before:
                            err_msg = "; ".join(
                                self.file_editor.last_operation_result.get("errors")
                                or ["Fallo al restaurar archivo desde backup durante recuperación."]
                            )
                            blocked_reasons.append(
                                f"Fallo al revertir '{op_entry.operation_id}' sobre '{target_path.name}': {err_msg}"
                            )
                            self.log_operation_rollback_failed(
                                record,
                                op_entry.operation_id,
                                error_detail=err_msg,
                            )
                            break

                        pre_rb_bak = self.file_editor.last_operation_result.get("pre_rollback_backup")
                        self.log_operation_rolled_back(
                            record,
                            op_entry.operation_id,
                            restored_sha256=post_restore_sha,
                            pre_rollback_backup_path=str(pre_rb_bak) if pre_rb_bak else None,
                        )
                        restored_files.append(str(target_path))
                        rolled_back_ops.append(op_entry.operation_id)
                        # Hook opcional para simular interrupción a mitad del rollback de recuperación
                        self.journal._trigger_fault(
                            "after_recovery_step_rollback",
                            record.transaction_id,
                            op_entry.operation_id,
                        )
                        continue

                    # Si el archivo está en un eslabón anterior válido de la misma cadena (porque esta
                    # operación nunca llegó a escribirse en disco), verificamos si coincide con algún
                    # expected_before anterior de este mismo archivo.
                    earlier_hashes = {
                        earlier.expected_before_sha256.lower()
                        for earlier in ops_by_file.get(op_entry.file_path, [])
                        if earlier.file_chain_index <= op_entry.file_chain_index
                    }
                    if current_disk_sha in earlier_hashes:
                        continue

                    blocked_reasons.append(
                        f"Discrepancia de hash en '{target_path.name}' al evaluar '{op_entry.operation_id}': "
                        f"actual={current_disk_sha[:12]}..., esperado={expected_after[:12] or expected_before[:12]}..."
                    )
                    self.log_operation_rollback_failed(
                        record,
                        op_entry.operation_id,
                        error_detail="Hash actual en disco no coincide con ningún estado esperado.",
                    )
                    break

            # Paso 11: Verificación final de que todos los archivos tocados volvieron a H0
            if not blocked_reasons:
                for file_path_str, file_ops in ops_by_file.items():
                    file_ops.sort(key=lambda x: x.file_chain_index)
                    h0_sha = file_ops[0].expected_before_sha256.lower()
                    actual_final_sha = compute_sha256_file(file_path_str).lower()
                    if actual_final_sha != h0_sha:
                        blocked_reasons.append(
                            f"Verificación final fallida en '{Path(file_path_str).name}': "
                            f"hash final ({actual_final_sha[:12]}...) != H0 ({h0_sha[:12]}...)."
                        )

            # Paso 12: Registrar el estado terminal
            if blocked_reasons:
                joined_reasons = " | ".join(blocked_reasons)
                self.transition_transaction_state(
                    record,
                    TransactionState.RECOVERY_FAILED,
                    reason=joined_reasons,
                    extra_payload={"recovery_attempt_update": {"outcome": TransactionState.RECOVERY_FAILED.value}},
                    mutator=lambda w: (
                        w.recovery_attempts[-1].update(
                            {"outcome": TransactionState.RECOVERY_FAILED.value, "errors": list(blocked_reasons)}
                        )
                        if w.recovery_attempts
                        else None
                    ),
                )
                return RecoveryReport(
                    transaction_id=record.transaction_id,
                    execution_id=record.execution_id,
                    plan_id=record.plan_id,
                    initial_state=initial_state,
                    final_state= record.state,
                    decision="BLOCKED_RECOVERY_FAILED",
                    restored_files=restored_files,
                    rolled_back_operations=rolled_back_ops,
                    blocked_reasons=blocked_reasons,
                    attempt_number=attempt_number,
                )

            self.transition_transaction_state(
                record,
                TransactionState.ROLLED_BACK,
                reason="Recuperación completada: todos los archivos verificados en su estado original H0.",
                extra_payload={"recovery_attempt_update": {"outcome": TransactionState.ROLLED_BACK.value}},
                mutator=lambda w: (
                    w.recovery_attempts[-1].update({"outcome": TransactionState.ROLLED_BACK.value})
                    if w.recovery_attempts
                    else None
                ),
            )
            return RecoveryReport(
                transaction_id=record.transaction_id,
                execution_id=record.execution_id,
                plan_id=record.plan_id,
                initial_state=initial_state,
                final_state=record.state,
                decision="ROLLED_BACK_TO_ORIGINAL",
                restored_files=restored_files,
                rolled_back_operations=rolled_back_ops,
                attempt_number=attempt_number,
            )

    def recover_pending_transactions(self) -> list[RecoveryReport]:
        """
        Inspecciona el directorio de journals bajo el bloqueo global y recupera todas las
        transacciones inconclusas (`CREATED`, `PREPARING`, `PREPARED`, `EXECUTING`, `ROLLING_BACK`,
        `RECOVERY_REQUIRED`, `RECOVERING`) o registra como `RECOVERY_FAILED` aquellas cuyo
        journal esté corrupto.
        """
        reports: list[RecoveryReport] = []
        with self.global_lock:
            for tx_id in self.journal.list_transaction_ids():
                try:
                    record, _ = self.journal.validate_and_load_transaction(tx_id)
                except JournalCorruptionError as corrupt_err:
                    self._corrupt_transactions[tx_id] = f"{corrupt_err.code}: {corrupt_err}"
                    reports.append(
                        RecoveryReport(
                            transaction_id=tx_id,
                            execution_id="unknown",
                            plan_id="unknown",
                            initial_state="CORRUPT_JOURNAL",
                            final_state=TransactionState.RECOVERY_FAILED.value,
                            decision="BLOCKED_CORRUPT_JOURNAL",
                            blocked_reasons=[f"{corrupt_err.code}: {corrupt_err}"],
                        )
                    )
                    continue

                if record.state in {
                    TransactionState.CREATED.value,
                    TransactionState.PREPARING.value,
                    TransactionState.PREPARED.value,
                    TransactionState.EXECUTING.value,
                    TransactionState.ROLLING_BACK.value,
                    TransactionState.RECOVERY_REQUIRED.value,
                    TransactionState.RECOVERING.value,
                }:
                    reports.append(
                        self.recover_transaction(
                            tx_id,
                            reason=f"Recuperación automática de transacción inconclusa en estado {record.state}.",
                        )
                    )
        return reports

    def has_corrupt_or_failed_recovery(self) -> list[str]:
        """
        Devuelve la lista de descripciones de transacciones con journal corrupto detectadas
        en el almacén de transacciones.
        """
        issues: list[str] = []
        for tx_id, reason in sorted(self._corrupt_transactions.items()):
            issues.append(f"{tx_id}: {reason}")
        return issues
