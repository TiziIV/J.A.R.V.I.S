"""Modelos de dominio, lecturas tipadas, estados de disponibilidad y excepciones de N7.

Este módulo define la estructura formal e inmutable de todos los datos contextuales
que el motor N7 (`ContextAwarenessEngine`) recopila del entorno Windows local.

Garantías arquitectónicas:
1. Sin operaciones de entrada/salida (IO), red ni llamadas a modelos de IA.
2. Inmutabilidad estructural (`@dataclass(frozen=True)`) para impedir que un consumidor
   altere un snapshot después de su captura.
3. Distinción explícita entre dato no disponible (`AvailabilityStatus.UNAVAILABLE` con
   `value=None`) y un valor numérico cero (`0.0`).
4. Representación formal de errores parciales (`ProviderIssue`) y campos faltantes
   (`unavailable_fields`) para que un fallo aislado no invalide el resto del contexto.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import re
from typing import Any, Optional
import uuid


def utc_now_iso() -> str:
    """Devuelve la fecha y hora UTC actual en formato ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


def generate_context_id(prefix: str = "ctx") -> str:
    """Genera un identificador corto, único y seguro para snapshots de contexto."""
    clean = re.sub(r"[^a-zA-Z0-9_-]", "", prefix).lower() or "ctx"
    return f"{clean}-{uuid.uuid4().hex[:12]}"


class ContextError(Exception):
    """Excepción base para errores controlados del motor de contexto N7."""


class ContextPolicyError(ContextError):
    """Error cuando una consulta viola las políticas de acceso o categorías habilitadas."""


class ContextAccessDeniedError(ContextPolicyError, PermissionError):
    """Error cuando se intenta consultar una ruta prohibida, fuera de raíces o mutar el sistema."""


class ContextProviderError(ContextError):
    """Error interno controlado dentro de un proveedor de contexto específico."""


class ContextTimeoutError(ContextError):
    """Error cuando un proveedor de contexto supera el tiempo máximo de respuesta permitido."""


class ContextCategory(str, Enum):
    """Categorías de observación contextual disponibles en N7."""

    SYSTEM = "system"
    HARDWARE = "hardware"
    RESOURCES = "resources"
    STORAGE = "storage"
    PROCESSES = "processes"
    APPLICATIONS = "applications"
    FILESYSTEM = "filesystem"


ALL_CONTEXT_CATEGORIES: tuple[ContextCategory, ...] = (
    ContextCategory.SYSTEM,
    ContextCategory.HARDWARE,
    ContextCategory.RESOURCES,
    ContextCategory.STORAGE,
    ContextCategory.PROCESSES,
    ContextCategory.APPLICATIONS,
    ContextCategory.FILESYSTEM,
)


class AvailabilityStatus(str, Enum):
    """Estado de disponibilidad de una métrica o categoría contextual."""

    AVAILABLE = "available"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    RESTRICTED = "restricted"
    ERROR = "error"


@dataclass(frozen=True)
class MetricReading:
    """Lectura individual de una métrica de recursos con trazabilidad de fuente y disponibilidad.

    Importante: cuando `status` es `UNAVAILABLE` o `ERROR`, `value` debe ser `None`
    (jamás `0.0`), evitando confundir la falta de telemetría con carga nula.
    """

    name: str
    value: Optional[float]
    unit: str
    captured_at: str = field(default_factory=utc_now_iso)
    status: AvailabilityStatus = AvailabilityStatus.AVAILABLE
    source: str = "psutil"
    notes: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.name or not str(self.name).strip():
            raise ValueError("Toda `MetricReading` requiere un `name` no vacío.")
        if self.status in {AvailabilityStatus.UNAVAILABLE, AvailabilityStatus.ERROR} and self.value is not None:
            raise ValueError(
                f"La métrica '{self.name}' está en estado '{self.status.value}' y no puede tener un valor numérico ({self.value})."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "captured_at": self.captured_at,
            "status": self.status.value,
            "source": self.source,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MetricReading":
        raw_val = data.get("value")
        return cls(
            name=str(data["name"]),
            value=float(raw_val) if raw_val is not None else None,
            unit=str(data.get("unit", "")),
            captured_at=str(data.get("captured_at") or utc_now_iso()),
            status=AvailabilityStatus(str(data.get("status", AvailabilityStatus.AVAILABLE.value))),
            source=str(data.get("source", "unknown")),
            notes=data.get("notes"),
        )


@dataclass(frozen=True)
class SystemContextInfo:
    """Información general del sistema operativo y entorno de ejecución de J.A.R.V.I.S."""

    os_name: str
    os_release: str
    os_version: str
    architecture: str
    python_version: str
    os_build: Optional[str] = None
    uptime_seconds: Optional[float] = None
    hostname: Optional[str] = None
    jarvis_env_mode: str = "production"
    captured_at: str = field(default_factory=utc_now_iso)
    status: AvailabilityStatus = AvailabilityStatus.AVAILABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "os_name": self.os_name,
            "os_release": self.os_release,
            "os_version": self.os_version,
            "os_build": self.os_build,
            "architecture": self.architecture,
            "python_version": self.python_version,
            "uptime_seconds": self.uptime_seconds,
            "hostname": self.hostname,
            "jarvis_env_mode": self.jarvis_env_mode,
            "captured_at": self.captured_at,
            "status": self.status.value,
        }


@dataclass(frozen=True)
class GpuDeviceInfo:
    """Información detectada dinámicamente sobre un adaptador gráfico (GPU)."""

    name: str
    vendor: Optional[str] = None
    vram_total_mb: Optional[float] = None
    vram_used_mb: Optional[float] = None
    vram_free_mb: Optional[float] = None
    utilization_percent: Optional[float] = None
    driver_version: Optional[str] = None
    detection_source: str = "unknown"
    status: AvailabilityStatus = AvailabilityStatus.AVAILABLE

    @property
    def vram_total_gb(self) -> Optional[float]:
        if self.vram_total_mb is None:
            return None
        return round(self.vram_total_mb / 1024.0, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "vendor": self.vendor,
            "vram_total_mb": self.vram_total_mb,
            "vram_total_gb": self.vram_total_gb,
            "vram_used_mb": self.vram_used_mb,
            "vram_free_mb": self.vram_free_mb,
            "utilization_percent": self.utilization_percent,
            "driver_version": self.driver_version,
            "detection_source": self.detection_source,
            "status": self.status.value,
        }


@dataclass(frozen=True)
class StorageVolumeInfo:
    """Estado y capacidad de una unidad o partición de almacenamiento local."""

    device: str
    mountpoint: str
    filesystem: str
    total_gb: Optional[float]
    used_gb: Optional[float]
    free_gb: Optional[float]
    percent_used: Optional[float]
    status: AvailabilityStatus = AvailabilityStatus.AVAILABLE
    notes: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "mountpoint": self.mountpoint,
            "filesystem": self.filesystem,
            "total_gb": self.total_gb,
            "used_gb": self.used_gb,
            "free_gb": self.free_gb,
            "percent_used": self.percent_used,
            "status": self.status.value,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class HardwareContextInfo:
    """Perfil consolidado de hardware disponible en el equipo."""

    cpu_model: Optional[str]
    cpu_architecture: str
    cpu_cores_physical: Optional[int]
    cpu_cores_logical: Optional[int]
    cpu_freq_mhz: Optional[float]
    ram_total_gb: Optional[float]
    ram_available_gb: Optional[float]
    gpus: tuple[GpuDeviceInfo, ...] = ()
    gpu_detection_status: AvailabilityStatus = AvailabilityStatus.UNAVAILABLE
    gpu_detection_notes: Optional[str] = None
    storage_volumes: tuple[StorageVolumeInfo, ...] = ()
    captured_at: str = field(default_factory=utc_now_iso)
    status: AvailabilityStatus = AvailabilityStatus.AVAILABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpu_model": self.cpu_model,
            "cpu_architecture": self.cpu_architecture,
            "cpu_cores_physical": self.cpu_cores_physical,
            "cpu_cores_logical": self.cpu_cores_logical,
            "cpu_freq_mhz": self.cpu_freq_mhz,
            "ram_total_gb": self.ram_total_gb,
            "ram_available_gb": self.ram_available_gb,
            "gpus": [gpu.to_dict() for gpu in self.gpus],
            "gpu_detection_status": self.gpu_detection_status.value,
            "gpu_detection_notes": self.gpu_detection_notes,
            "storage_volumes": [vol.to_dict() for vol in self.storage_volumes],
            "captured_at": self.captured_at,
            "status": self.status.value,
        }


@dataclass(frozen=True)
class ResourceUsageInfo:
    """Instantánea puntual de consumo de recursos bajo demanda."""

    cpu_percent: MetricReading
    ram_used_gb: MetricReading
    ram_available_gb: MetricReading
    ram_percent: MetricReading
    gpu_utilization_percent: MetricReading
    vram_used_mb: MetricReading
    vram_free_mb: MetricReading
    workspace_disk_free_gb: MetricReading
    captured_at: str = field(default_factory=utc_now_iso)
    status: AvailabilityStatus = AvailabilityStatus.AVAILABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpu_percent": self.cpu_percent.to_dict(),
            "ram_used_gb": self.ram_used_gb.to_dict(),
            "ram_available_gb": self.ram_available_gb.to_dict(),
            "ram_percent": self.ram_percent.to_dict(),
            "gpu_utilization_percent": self.gpu_utilization_percent.to_dict(),
            "vram_used_mb": self.vram_used_mb.to_dict(),
            "vram_free_mb": self.vram_free_mb.to_dict(),
            "workspace_disk_free_gb": self.workspace_disk_free_gb.to_dict(),
            "captured_at": self.captured_at,
            "status": self.status.value,
        }


@dataclass(frozen=True)
class ProcessContextItem:
    """Resumen seguro y en solo lectura de un proceso en ejecución.

    Nota de seguridad: jamás incluye `cmdline` ni variables de entorno (`environ`)
    para impedir la exposición de contraseñas, tokens o claves pasadas por CLI.
    """

    pid: int
    name: str
    status: str
    cpu_percent: Optional[float]
    memory_mb: Optional[float]
    username: Optional[str] = None
    exe_path: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "pid": self.pid,
            "name": self.name,
            "status": self.status,
            "cpu_percent": self.cpu_percent,
            "memory_mb": self.memory_mb,
        }
        if self.username is not None:
            payload["username"] = self.username
        if self.exe_path is not None:
            payload["exe_path"] = self.exe_path
        return payload


@dataclass(frozen=True)
class ProcessAwarenessInfo:
    """Resultado acotado de la observación de procesos activos del sistema."""

    processes: tuple[ProcessContextItem, ...]
    total_sampled: int
    returned_count: int
    access_denied_count: int
    vanished_count: int
    truncated: bool
    captured_at: str = field(default_factory=utc_now_iso)
    status: AvailabilityStatus = AvailabilityStatus.AVAILABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "processes": [p.to_dict() for p in self.processes],
            "total_sampled": self.total_sampled,
            "returned_count": self.returned_count,
            "access_denied_count": self.access_denied_count,
            "vanished_count": self.vanished_count,
            "truncated": self.truncated,
            "captured_at": self.captured_at,
            "status": self.status.value,
        }


@dataclass(frozen=True)
class ActiveWindowInfo:
    """Información básica y sanitizada de una ventana visible en Windows."""

    hwnd: int
    title: str
    pid: Optional[int] = None
    process_name: Optional[str] = None
    is_foreground: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "pid": self.pid,
            "process_name": self.process_name,
            "is_foreground": self.is_foreground,
        }


@dataclass(frozen=True)
class ApplicationItem:
    """Representa una aplicación diferenciando claramente instalada vs en ejecución."""

    name: str
    app_kind: str  # 'installed' | 'running' | 'active_window'
    is_running: bool = False
    is_installed: bool = False
    executable_name: Optional[str] = None
    pids: tuple[int, ...] = ()
    source: str = "observer"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "app_kind": self.app_kind,
            "is_running": self.is_running,
            "is_installed": self.is_installed,
            "executable_name": self.executable_name,
            "pids": list(self.pids),
            "source": self.source,
        }


@dataclass(frozen=True)
class ApplicationAwarenessInfo:
    """Resumen contextual de ventanas activas, aplicaciones en ejecución e instaladas."""

    foreground_window: Optional[ActiveWindowInfo]
    visible_windows: tuple[ActiveWindowInfo, ...]
    running_applications: tuple[ApplicationItem, ...]
    installed_applications: tuple[ApplicationItem, ...]
    unavailable_count: int = 0
    notes: tuple[str, ...] = ()
    captured_at: str = field(default_factory=utc_now_iso)
    status: AvailabilityStatus = AvailabilityStatus.AVAILABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "foreground_window": self.foreground_window.to_dict() if self.foreground_window else None,
            "visible_windows": [w.to_dict() for w in self.visible_windows],
            "running_applications": [a.to_dict() for a in self.running_applications],
            "installed_applications": [a.to_dict() for a in self.installed_applications],
            "unavailable_count": self.unavailable_count,
            "notes": list(self.notes),
            "captured_at": self.captured_at,
            "status": self.status.value,
        }


@dataclass(frozen=True)
class FileMetadataItem:
    """Metadatos básicos de un archivo o directorio dentro de una raíz autorizada."""

    path: str
    name: str
    exists: bool
    is_dir: bool
    is_file: bool
    extension: str
    file_category: str
    size_bytes: Optional[int]
    modified_at: Optional[str]
    authorized_root: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "exists": self.exists,
            "is_dir": self.is_dir,
            "is_file": self.is_file,
            "extension": self.extension,
            "file_category": self.file_category,
            "size_bytes": self.size_bytes,
            "modified_at": self.modified_at,
            "authorized_root": self.authorized_root,
        }


@dataclass(frozen=True)
class DirectorySnapshot:
    """Estructura acotada de un directorio autorizado."""

    root_path: str
    entries: tuple[FileMetadataItem, ...]
    total_entries_scanned: int
    max_depth_reached: int
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_path": self.root_path,
            "entries": [e.to_dict() for e in self.entries],
            "total_entries_scanned": self.total_entries_scanned,
            "max_depth_reached": self.max_depth_reached,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class FileSystemContextInfo:
    """Contexto de archivos y directorios consultados bajo raíces autorizadas."""

    authorized_roots: tuple[str, ...]
    queried_files: tuple[FileMetadataItem, ...] = ()
    directories: tuple[DirectorySnapshot, ...] = ()
    relevant_matches: tuple[FileMetadataItem, ...] = ()
    rejected_paths: tuple[str, ...] = ()
    captured_at: str = field(default_factory=utc_now_iso)
    status: AvailabilityStatus = AvailabilityStatus.AVAILABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "authorized_roots": list(self.authorized_roots),
            "queried_files": [f.to_dict() for f in self.queried_files],
            "directories": [d.to_dict() for d in self.directories],
            "relevant_matches": [m.to_dict() for m in self.relevant_matches],
            "rejected_paths": list(self.rejected_paths),
            "captured_at": self.captured_at,
            "status": self.status.value,
        }


@dataclass(frozen=True)
class ProviderIssue:
    """Registro estructurado de un fallo o restricción parcial en un proveedor N7."""

    category: str
    provider_name: str
    error_type: str
    message: str
    captured_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "provider_name": self.provider_name,
            "error_type": self.error_type,
            "message": self.message,
            "captured_at": self.captured_at,
        }


@dataclass(frozen=True)
class ContextUntrustedEnvelope:
    """Sobre de aislamiento para datos contextuales del sistema enviados a N6 o a un LLM.

    Garantiza que nombres de procesos, títulos de ventanas o nombres de archivos sean
    tratados exclusivamente como datos pasivos de observación y jamás como instrucciones
    ejecutables ni permisos de modificación.
    """

    categories_included: tuple[str, ...]
    minimized_data: dict[str, Any]
    trust_level: str = "untrusted_system_context"
    source_type: str = "local_windows_observer"
    origin_engine: str = "n7_context_awareness"
    allow_tool_execution_from_content: bool = False
    allow_system_prompt_promotion: bool = False
    allow_direct_mutation: bool = False
    captured_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trust_level": self.trust_level,
            "source_type": self.source_type,
            "origin_engine": self.origin_engine,
            "allow_tool_execution_from_content": self.allow_tool_execution_from_content,
            "allow_system_prompt_promotion": self.allow_system_prompt_promotion,
            "allow_direct_mutation": self.allow_direct_mutation,
            "categories_included": list(self.categories_included),
            "minimized_data": dict(self.minimized_data),
            "captured_at": self.captured_at,
        }


@dataclass(frozen=True)
class ContextSnapshot:
    """Modelo consolidado e inmutable de una consulta contextual N7."""

    snapshot_id: str
    captured_at: str
    requested_categories: tuple[str, ...]
    sources_consulted: tuple[str, ...]
    system: Optional[SystemContextInfo] = None
    hardware: Optional[HardwareContextInfo] = None
    resources: Optional[ResourceUsageInfo] = None
    storage: Optional[tuple[StorageVolumeInfo, ...]] = None
    processes: Optional[ProcessAwarenessInfo] = None
    applications: Optional[ApplicationAwarenessInfo] = None
    filesystem: Optional[FileSystemContextInfo] = None
    unavailable_fields: tuple[str, ...] = ()
    partial_errors: tuple[ProviderIssue, ...] = ()
    cached_categories: tuple[str, ...] = ()
    read_only_verified: bool = True

    @property
    def has_errors(self) -> bool:
        return len(self.partial_errors) > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "captured_at": self.captured_at,
            "requested_categories": list(self.requested_categories),
            "sources_consulted": list(self.sources_consulted),
            "system": self.system.to_dict() if self.system else None,
            "hardware": self.hardware.to_dict() if self.hardware else None,
            "resources": self.resources.to_dict() if self.resources else None,
            "storage": [vol.to_dict() for vol in self.storage] if self.storage is not None else None,
            "processes": self.processes.to_dict() if self.processes else None,
            "applications": self.applications.to_dict() if self.applications else None,
            "filesystem": self.filesystem.to_dict() if self.filesystem else None,
            "unavailable_fields": list(self.unavailable_fields),
            "partial_errors": [err.to_dict() for err in self.partial_errors],
            "cached_categories": list(self.cached_categories),
            "read_only_verified": self.read_only_verified,
        }
