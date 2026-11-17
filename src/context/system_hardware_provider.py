"""Proveedores de información de sistema, hardware, almacenamiento y recursos (N7).

Todos los proveedores de este módulo operan bajo demanda y en estricto modo de solo lectura.
No mantienen hilos ni bucles de monitorización permanente, no importan modelos de IA ni
bibliotecas pesadas en frío (como `torch`), y distinguen explícitamente entre un dato no
disponible (`AvailabilityStatus.UNAVAILABLE`, `value=None`) y un valor numérico cero (`0.0`).
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Optional

import psutil

from config import settings
from src.context.models import (
    AvailabilityStatus,
    GpuDeviceInfo,
    HardwareContextInfo,
    MetricReading,
    ResourceUsageInfo,
    StorageVolumeInfo,
    SystemContextInfo,
    utc_now_iso,
)
from src.context.policy import ContextAccessPolicy, sanitize_untrusted_label


def _parse_optional_float(raw: Any) -> Optional[float]:
    """Convierte un texto numérico a float, devolviendo `None` ante `N/A`, `[Not Supported]` o vacío."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text.upper() in {"N/A", "NA", "[NOT SUPPORTED]", "NONE", "NULL"}:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _detect_vendor_from_name(gpu_name: str) -> Optional[str]:
    """Infiere el fabricante del adaptador gráfico a partir de su descripción."""
    lowered = (gpu_name or "").lower()
    if any(tok in lowered for tok in ("nvidia", "geforce", "quadro", "tesla", "rtx", "gtx")):
        return "NVIDIA"
    if any(tok in lowered for tok in ("amd", "radeon", "rx ")):
        return "AMD"
    if any(tok in lowered for tok in ("intel", "arc", "uhd", "iris")):
        return "Intel"
    return None


class SystemInfoProvider:
    """Proveedor de información general del sistema operativo y entorno de J.A.R.V.I.S."""

    def __init__(self, policy: Optional[ContextAccessPolicy] = None) -> None:
        self.policy: ContextAccessPolicy = policy or ContextAccessPolicy()

    def collect(self) -> SystemContextInfo:
        """Obtiene datos generales del sistema operativo sin exponer identificadores personales."""
        self.policy.assert_read_only_operation("collect_system_info")
        now_iso = utc_now_iso()

        os_name = sanitize_untrusted_label(platform.system() or "Unknown", max_len=64)
        os_release = sanitize_untrusted_label(platform.release() or "Unknown", max_len=64)
        os_version = sanitize_untrusted_label(platform.version() or "Unknown", max_len=120)
        architecture = sanitize_untrusted_label(platform.machine() or "Unknown", max_len=32)
        py_ver = sanitize_untrusted_label(platform.python_version(), max_len=32)

        os_build: Optional[str] = None
        if hasattr(sys, "getwindowsversion"):
            try:
                win_ver = sys.getwindowsversion()
                os_build = str(getattr(win_ver, "build", "")) or None
            except OSError:
                os_build = None

        uptime_sec: Optional[float] = None
        try:
            boot_ts = float(psutil.boot_time())
            uptime_val = time.time() - boot_ts
            if uptime_val >= 0:
                uptime_sec = round(uptime_val, 1)
        except (OSError, AttributeError, ValueError, TypeError):
            uptime_sec = None

        hostname: Optional[str] = None
        if self.policy.include_hostname:
            try:
                raw_node = platform.node()
                hostname = sanitize_untrusted_label(raw_node, max_len=64) or None
            except OSError:
                hostname = None

        env_raw = os.environ.get("JARVIS_ENV", "").strip().lower()
        jarvis_mode = "test" if env_raw in {"test", "testing"} else "production"

        status = (
            AvailabilityStatus.AVAILABLE
            if (os_name and os_name != "Unknown")
            else AvailabilityStatus.PARTIAL
        )
        return SystemContextInfo(
            os_name=os_name,
            os_release=os_release,
            os_version=os_version,
            os_build=os_build,
            architecture=architecture,
            python_version=py_ver,
            uptime_seconds=uptime_sec,
            hostname=hostname,
            jarvis_env_mode=jarvis_mode,
            captured_at=now_iso,
            status=status,
        )


class StorageInfoProvider:
    """Proveedor de información de unidades y particiones de almacenamiento local."""

    def __init__(self, policy: Optional[ContextAccessPolicy] = None) -> None:
        self.policy: ContextAccessPolicy = policy or ContextAccessPolicy()

    def collect(self) -> tuple[StorageVolumeInfo, ...]:
        """Enumera las unidades montadas y su espacio total/usado/libre en modo solo lectura."""
        self.policy.assert_read_only_operation("collect_storage_info")
        volumes: list[StorageVolumeInfo] = []

        try:
            partitions = psutil.disk_partitions(all=False)
        except OSError:
            return ()

        for part in partitions:
            device = sanitize_untrusted_label(getattr(part, "device", ""), max_len=64)
            mountpoint = sanitize_untrusted_label(getattr(part, "mountpoint", ""), max_len=120)
            fstype = sanitize_untrusted_label(getattr(part, "fstype", "") or "unknown", max_len=32)
            opts = str(getattr(part, "opts", "") or "").lower()

            if "cdrom" in opts or not mountpoint:
                continue

            try:
                usage = psutil.disk_usage(part.mountpoint)
                total_gb = round(usage.total / (1024**3), 2)
                used_gb = round(usage.used / (1024**3), 2)
                free_gb = round(usage.free / (1024**3), 2)
                pct = round(float(usage.percent), 1)
                volumes.append(
                    StorageVolumeInfo(
                        device=device or mountpoint,
                        mountpoint=mountpoint,
                        filesystem=fstype,
                        total_gb=total_gb,
                        used_gb=used_gb,
                        free_gb=free_gb,
                        percent_used=pct,
                        status=AvailabilityStatus.AVAILABLE,
                    )
                )
            except PermissionError:
                volumes.append(
                    StorageVolumeInfo(
                        device=device or mountpoint,
                        mountpoint=mountpoint,
                        filesystem=fstype,
                        total_gb=None,
                        used_gb=None,
                        free_gb=None,
                        percent_used=None,
                        status=AvailabilityStatus.RESTRICTED,
                        notes="Permiso denegado al consultar capacidad de la unidad.",
                    )
                )
            except OSError as err:
                volumes.append(
                    StorageVolumeInfo(
                        device=device or mountpoint,
                        mountpoint=mountpoint,
                        filesystem=fstype,
                        total_gb=None,
                        used_gb=None,
                        free_gb=None,
                        percent_used=None,
                        status=AvailabilityStatus.UNAVAILABLE,
                        notes=f"Unidad no disponible ({type(err).__name__}).",
                    )
                )

        return tuple(volumes)


class HardwareInfoProvider:
    """Proveedor de perfil de hardware (CPU, núcleos, RAM, GPU/VRAM y unidades)."""

    # Claves públicas de descripción de hardware en Windows (solo lectura con KEY_READ).
    _CPU_REG_SUBKEY: str = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
    _GPU_CLASS_REG_SUBKEY: str = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"

    def __init__(
        self,
        policy: Optional[ContextAccessPolicy] = None,
        *,
        storage_provider: Optional[StorageInfoProvider] = None,
        timeout_sec: Optional[float] = None,
        which_fn: Callable[[str], Optional[str]] = shutil.which,
        subprocess_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.policy: ContextAccessPolicy = policy or ContextAccessPolicy()
        self.storage_provider: StorageInfoProvider = storage_provider or StorageInfoProvider(
            self.policy
        )
        self.timeout_sec: float = max(
            0.2,
            float(
                timeout_sec
                if timeout_sec is not None
                else settings.CONTEXT_PROVIDER_TIMEOUT_SEC
            ),
        )
        self._which_fn = which_fn
        self._subprocess_runner = subprocess_runner

    def collect(self, *, include_storage: bool = True) -> HardwareContextInfo:
        """Recopila información real de CPU, RAM, GPU y almacenamiento disponible."""
        self.policy.assert_read_only_operation("collect_hardware_info")
        now_iso = utc_now_iso()

        cpu_model = self._detect_cpu_model()
        cpu_arch = sanitize_untrusted_label(platform.machine() or "Unknown", max_len=32)

        try:
            phys_cores = psutil.cpu_count(logical=False)
        except OSError:
            phys_cores = None

        try:
            log_cores = psutil.cpu_count(logical=True)
        except OSError:
            log_cores = None

        cpu_freq_mhz: Optional[float] = None
        try:
            freq = psutil.cpu_freq()
            if freq is not None:
                raw_mhz = getattr(freq, "max", None) or getattr(freq, "current", None)
                if raw_mhz and raw_mhz > 0:
                    cpu_freq_mhz = round(float(raw_mhz), 1)
        except (OSError, AttributeError, NotImplementedError):
            cpu_freq_mhz = None

        ram_total_gb: Optional[float] = None
        ram_avail_gb: Optional[float] = None
        try:
            vm = psutil.virtual_memory()
            ram_total_gb = round(vm.total / (1024**3), 2)
            ram_avail_gb = round(vm.available / (1024**3), 2)
        except OSError:
            pass

        gpus, gpu_status, gpu_notes = self.detect_gpus()
        volumes = self.storage_provider.collect() if include_storage else ()

        overall_status = AvailabilityStatus.AVAILABLE
        if ram_total_gb is None or log_cores is None:
            overall_status = AvailabilityStatus.PARTIAL

        return HardwareContextInfo(
            cpu_model=cpu_model,
            cpu_architecture=cpu_arch,
            cpu_cores_physical=phys_cores,
            cpu_cores_logical=log_cores,
            cpu_freq_mhz=cpu_freq_mhz,
            ram_total_gb=ram_total_gb,
            ram_available_gb=ram_avail_gb,
            gpus=gpus,
            gpu_detection_status=gpu_status,
            gpu_detection_notes=gpu_notes,
            storage_volumes=volumes,
            captured_at=now_iso,
            status=overall_status,
        )

    def _detect_cpu_model(self) -> Optional[str]:
        """Obtiene el nombre descriptivo del procesador en Windows o el identificador estándar."""
        if sys.platform == "win32":
            try:
                import winreg

                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    self._CPU_REG_SUBKEY,
                    0,
                    winreg.KEY_READ,
                ) as key:
                    val, _ = winreg.QueryValueEx(key, "ProcessorNameString")
                    if isinstance(val, str) and val.strip():
                        return sanitize_untrusted_label(val.strip(), max_len=120)
            except OSError:
                pass

        fallback = platform.processor()
        if fallback and fallback.strip():
            return sanitize_untrusted_label(fallback.strip(), max_len=120)
        return None

    def detect_gpus(self) -> tuple[tuple[GpuDeviceInfo, ...], AvailabilityStatus, Optional[str]]:
        """Detecta dinámicamente GPUs y VRAM con degradación tolerante a fallos.

        Orden de estrategias:
        1. `nvidia-smi` (ejecución directa sin shell y con timeout estricto) si existe.
        2. `torch.cuda` únicamente si `torch` ya está cargado en `sys.modules` (sin import en frío).
        3. Descriptor de clase de adaptador de video de Windows (`winreg.KEY_READ`).
        4. Estado `UNAVAILABLE` explícito si ninguna fuente está disponible.
        """
        # Estrategia 1: nvidia-smi
        smi_gpus = self._query_nvidia_smi()
        if smi_gpus:
            return smi_gpus, AvailabilityStatus.AVAILABLE, "Telemetría obtenida mediante nvidia-smi."

        # Estrategia 2: torch.cuda ya cargado en memoria (sin importar torch en frío)
        torch_gpus = self._query_loaded_torch_cuda()
        if torch_gpus:
            return (
                torch_gpus,
                AvailabilityStatus.PARTIAL,
                "GPU detectada mediante runtime CUDA activo (sin métricas de uso en vivo de nvidia-smi).",
            )

        # Estrategia 3: descriptor de adaptador en registro de hardware de Windows (solo lectura)
        reg_gpus = self._query_windows_display_class()
        if reg_gpus:
            return (
                reg_gpus,
                AvailabilityStatus.PARTIAL,
                "Adaptador gráfico detectado por descriptor de hardware de Windows (telemetría dinámica de uso no disponible).",
            )

        return (
            (),
            AvailabilityStatus.UNAVAILABLE,
            "No fue posible detectar una GPU dedicada mediante nvidia-smi ni descriptores locales.",
        )

    def _query_nvidia_smi(self) -> tuple[GpuDeviceInfo, ...]:
        """Consulta `nvidia-smi` de forma segura (`shell=False`) si el ejecutable existe."""
        exe_path = self._which_fn("nvidia-smi")
        if not exe_path:
            return ()

        cmd = [
            exe_path,
            "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu,driver_version",
            "--format=csv,noheader,nounits",
        ]
        try:
            proc = self._subprocess_runner(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_sec,
                shell=False,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ()

        if proc.returncode != 0 or not (proc.stdout or "").strip():
            return ()

        devices: list[GpuDeviceInfo] = []
        for raw_line in proc.stdout.strip().splitlines():
            parts = [p.strip() for p in raw_line.split(",")]
            if len(parts) < 2 or not parts[0]:
                continue
            name = sanitize_untrusted_label(parts[0], max_len=120)
            total_mb = _parse_optional_float(parts[1]) if len(parts) > 1 else None
            used_mb = _parse_optional_float(parts[2]) if len(parts) > 2 else None
            free_mb = _parse_optional_float(parts[3]) if len(parts) > 3 else None
            util_pct = _parse_optional_float(parts[4]) if len(parts) > 4 else None
            driver_ver = (
                sanitize_untrusted_label(parts[5], max_len=48)
                if len(parts) > 5 and parts[5]
                else None
            )
            status = (
                AvailabilityStatus.AVAILABLE
                if (total_mb is not None and util_pct is not None)
                else AvailabilityStatus.PARTIAL
            )
            devices.append(
                GpuDeviceInfo(
                    name=name,
                    vendor=_detect_vendor_from_name(name) or "NVIDIA",
                    vram_total_mb=total_mb,
                    vram_used_mb=used_mb,
                    vram_free_mb=free_mb,
                    utilization_percent=util_pct,
                    driver_version=driver_ver,
                    detection_source="nvidia-smi",
                    status=status,
                )
            )
        return tuple(devices)

    @staticmethod
    def _query_loaded_torch_cuda() -> tuple[GpuDeviceInfo, ...]:
        """Inspecciona `torch.cuda` únicamente si `torch` ya fue cargado por el proceso."""
        torch_mod = sys.modules.get("torch")
        if torch_mod is None:
            return ()
        try:
            cuda_mod = getattr(torch_mod, "cuda", None)
            if cuda_mod is None or not cuda_mod.is_available():
                return ()
            count = int(cuda_mod.device_count())
            devices: list[GpuDeviceInfo] = []
            for idx in range(min(count, 4)):
                raw_name = str(cuda_mod.get_device_name(idx))
                name = sanitize_untrusted_label(raw_name, max_len=120)
                props = cuda_mod.get_device_properties(idx)
                total_bytes = getattr(props, "total_memory", None)
                total_mb = round(float(total_bytes) / (1024**2), 1) if total_bytes else None
                devices.append(
                    GpuDeviceInfo(
                        name=name,
                        vendor=_detect_vendor_from_name(name) or "NVIDIA",
                        vram_total_mb=total_mb,
                        vram_used_mb=None,
                        vram_free_mb=None,
                        utilization_percent=None,
                        driver_version=None,
                        detection_source="torch.cuda",
                        status=AvailabilityStatus.PARTIAL,
                    )
                )
            return tuple(devices)
        except Exception:
            return ()

    def _query_windows_display_class(self) -> tuple[GpuDeviceInfo, ...]:
        """Lee en modo `KEY_READ` los descriptores básicos de adaptador de pantalla en Windows."""
        if sys.platform != "win32":
            return ()
        try:
            import winreg
        except ImportError:
            return ()

        devices: list[GpuDeviceInfo] = []
        for sub_idx in ("0000", "0001", "0002", "0003"):
            subkey_path = f"{self._GPU_CLASS_REG_SUBKEY}\\{sub_idx}"
            try:
                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    subkey_path,
                    0,
                    winreg.KEY_READ,
                ) as key:
                    desc, _ = winreg.QueryValueEx(key, "DriverDesc")
                    if not isinstance(desc, str) or not desc.strip():
                        continue
                    clean_name = sanitize_untrusted_label(desc.strip(), max_len=120)
                    if "basic display" in clean_name.lower() or "remote" in clean_name.lower():
                        continue

                    driver_ver: Optional[str] = None
                    try:
                        dv, _ = winreg.QueryValueEx(key, "DriverVersion")
                        if isinstance(dv, str) and dv.strip():
                            driver_ver = sanitize_untrusted_label(dv.strip(), max_len=48)
                    except OSError:
                        driver_ver = None

                    vram_mb: Optional[float] = None
                    for mem_value_name in (
                        "HardwareInformation.qwMemorySize",
                        "HardwareInformation.MemorySize",
                    ):
                        try:
                            raw_mem, _ = winreg.QueryValueEx(key, mem_value_name)
                            if isinstance(raw_mem, int) and raw_mem > 0:
                                vram_mb = round(raw_mem / (1024**2), 1)
                                break
                            if isinstance(raw_mem, bytes) and len(raw_mem) in (4, 8):
                                int_mem = int.from_bytes(raw_mem, byteorder="little", signed=False)
                                if int_mem > 0:
                                    vram_mb = round(int_mem / (1024**2), 1)
                                    break
                        except OSError:
                            continue

                    devices.append(
                        GpuDeviceInfo(
                            name=clean_name,
                            vendor=_detect_vendor_from_name(clean_name),
                            vram_total_mb=vram_mb,
                            vram_used_mb=None,
                            vram_free_mb=None,
                            utilization_percent=None,
                            driver_version=driver_ver,
                            detection_source="winreg_display_class",
                            status=AvailabilityStatus.PARTIAL,
                        )
                    )
            except OSError:
                continue

        return tuple(devices)


class ResourceMonitoringProvider:
    """Proveedor de consumo actual de recursos bajo demanda (sin bucles permanentes)."""

    def __init__(
        self,
        policy: Optional[ContextAccessPolicy] = None,
        *,
        hardware_provider: Optional[HardwareInfoProvider] = None,
        cpu_sample_interval: float = 0.05,
    ) -> None:
        self.policy: ContextAccessPolicy = policy or ContextAccessPolicy()
        self.hardware_provider: HardwareInfoProvider = hardware_provider or HardwareInfoProvider(
            self.policy
        )
        self.cpu_sample_interval: float = max(0.0, min(float(cpu_sample_interval), 0.5))

    def collect(self) -> ResourceUsageInfo:
        """Captura una lectura puntual de CPU, RAM, GPU/VRAM y disco libre."""
        self.policy.assert_read_only_operation("collect_resource_usage")
        now_iso = utc_now_iso()

        # 1. CPU %
        try:
            raw_cpu = float(psutil.cpu_percent(interval=self.cpu_sample_interval))
            cpu_metric = MetricReading(
                name="cpu_percent",
                value=round(raw_cpu, 1),
                unit="%",
                captured_at=now_iso,
                status=AvailabilityStatus.AVAILABLE,
                source="psutil.cpu_percent",
            )
        except OSError as err:
            cpu_metric = MetricReading(
                name="cpu_percent",
                value=None,
                unit="%",
                captured_at=now_iso,
                status=AvailabilityStatus.UNAVAILABLE,
                source="psutil.cpu_percent",
                notes=f"No disponible ({type(err).__name__}).",
            )

        # 2. RAM
        try:
            vm = psutil.virtual_memory()
            ram_used = MetricReading(
                name="ram_used_gb",
                value=round(vm.used / (1024**3), 2),
                unit="GB",
                captured_at=now_iso,
                status=AvailabilityStatus.AVAILABLE,
                source="psutil.virtual_memory",
            )
            ram_avail = MetricReading(
                name="ram_available_gb",
                value=round(vm.available / (1024**3), 2),
                unit="GB",
                captured_at=now_iso,
                status=AvailabilityStatus.AVAILABLE,
                source="psutil.virtual_memory",
            )
            ram_pct = MetricReading(
                name="ram_percent",
                value=round(float(vm.percent), 1),
                unit="%",
                captured_at=now_iso,
                status=AvailabilityStatus.AVAILABLE,
                source="psutil.virtual_memory",
            )
        except OSError as err:
            note = f"No disponible ({type(err).__name__})."
            ram_used = MetricReading(
                name="ram_used_gb",
                value=None,
                unit="GB",
                captured_at=now_iso,
                status=AvailabilityStatus.UNAVAILABLE,
                source="psutil.virtual_memory",
                notes=note,
            )
            ram_avail = MetricReading(
                name="ram_available_gb",
                value=None,
                unit="GB",
                captured_at=now_iso,
                status=AvailabilityStatus.UNAVAILABLE,
                source="psutil.virtual_memory",
                notes=note,
            )
            ram_pct = MetricReading(
                name="ram_percent",
                value=None,
                unit="%",
                captured_at=now_iso,
                status=AvailabilityStatus.UNAVAILABLE,
                source="psutil.virtual_memory",
                notes=note,
            )

        # 3. GPU & VRAM (distinguiendo explícitamente UNAVAILABLE de 0.0)
        gpus, _gpu_status, gpu_note = self.hardware_provider.detect_gpus()
        primary_gpu = gpus[0] if gpus else None

        if primary_gpu is not None and primary_gpu.utilization_percent is not None:
            gpu_util_metric = MetricReading(
                name="gpu_utilization_percent",
                value=round(primary_gpu.utilization_percent, 1),
                unit="%",
                captured_at=now_iso,
                status=AvailabilityStatus.AVAILABLE,
                source=primary_gpu.detection_source,
            )
        else:
            gpu_util_metric = MetricReading(
                name="gpu_utilization_percent",
                value=None,
                unit="%",
                captured_at=now_iso,
                status=AvailabilityStatus.UNAVAILABLE,
                source=primary_gpu.detection_source if primary_gpu else "none",
                notes=gpu_note or "Uso de GPU en vivo no disponible.",
            )

        if primary_gpu is not None and primary_gpu.vram_used_mb is not None:
            vram_used_metric = MetricReading(
                name="vram_used_mb",
                value=round(primary_gpu.vram_used_mb, 1),
                unit="MB",
                captured_at=now_iso,
                status=AvailabilityStatus.AVAILABLE,
                source=primary_gpu.detection_source,
            )
        else:
            vram_used_metric = MetricReading(
                name="vram_used_mb",
                value=None,
                unit="MB",
                captured_at=now_iso,
                status=AvailabilityStatus.UNAVAILABLE,
                source=primary_gpu.detection_source if primary_gpu else "none",
                notes="VRAM utilizada en vivo no disponible.",
            )

        if primary_gpu is not None and primary_gpu.vram_free_mb is not None:
            vram_free_metric = MetricReading(
                name="vram_free_mb",
                value=round(primary_gpu.vram_free_mb, 1),
                unit="MB",
                captured_at=now_iso,
                status=AvailabilityStatus.AVAILABLE,
                source=primary_gpu.detection_source,
            )
        else:
            vram_free_metric = MetricReading(
                name="vram_free_mb",
                value=None,
                unit="MB",
                captured_at=now_iso,
                status=AvailabilityStatus.UNAVAILABLE,
                source=primary_gpu.detection_source if primary_gpu else "none",
                notes="VRAM libre en vivo no disponible.",
            )

        # 4. Espacio libre en disco de trabajo
        try:
            du = psutil.disk_usage(str(settings.BASE_DIR))
            disk_free_metric = MetricReading(
                name="workspace_disk_free_gb",
                value=round(du.free / (1024**3), 2),
                unit="GB",
                captured_at=now_iso,
                status=AvailabilityStatus.AVAILABLE,
                source="psutil.disk_usage",
            )
        except OSError as err:
            disk_free_metric = MetricReading(
                name="workspace_disk_free_gb",
                value=None,
                unit="GB",
                captured_at=now_iso,
                status=AvailabilityStatus.UNAVAILABLE,
                source="psutil.disk_usage",
                notes=f"No disponible ({type(err).__name__}).",
            )

        all_metrics = (
            cpu_metric,
            ram_used,
            ram_avail,
            ram_pct,
            gpu_util_metric,
            vram_used_metric,
            vram_free_metric,
            disk_free_metric,
        )
        available_count = sum(
            1 for m in all_metrics if m.status == AvailabilityStatus.AVAILABLE
        )
        if available_count == len(all_metrics):
            overall = AvailabilityStatus.AVAILABLE
        elif available_count > 0:
            overall = AvailabilityStatus.PARTIAL
        else:
            overall = AvailabilityStatus.UNAVAILABLE

        return ResourceUsageInfo(
            cpu_percent=cpu_metric,
            ram_used_gb=ram_used,
            ram_available_gb=ram_avail,
            ram_percent=ram_pct,
            gpu_utilization_percent=gpu_util_metric,
            vram_used_mb=vram_used_metric,
            vram_free_mb=vram_free_metric,
            workspace_disk_free_gb=disk_free_metric,
            captured_at=now_iso,
            status=overall,
        )
