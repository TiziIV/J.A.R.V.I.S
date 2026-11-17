"""Adaptador de consola para consultas contextuales en solo lectura de N7 (`/contexto`).

Permite inspeccionar desde la consola interactiva de J.A.R.V.I.S. las categorías del
Context Awareness Engine sin iniciar módulos de audio (`faster_whisper`, `sounddevice`,
`SpeechToText`, `TextToSpeech`, `WakeWordDetector`) y sin generar volcados masivos.
"""

from __future__ import annotations

from typing import Callable, Optional

from src.context.engine import ContextAwarenessEngine
from src.context.models import (
    AvailabilityStatus,
    ContextCategory,
    ContextError,
    ContextSnapshot,
    MetricReading,
)


def _format_metric(reading: MetricReading) -> str:
    """Formatea una `MetricReading` distinguiendo explícitamente `UNAVAILABLE` de `0`."""
    if reading.status != AvailabilityStatus.AVAILABLE or reading.value is None:
        reason = f" ({reading.notes})" if reading.notes else ""
        return f"No disponible [{reading.status.value}]{reason}"
    return f"{reading.value} {reading.unit} (fuente: {reading.source})"


def _format_uptime(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return "No disponible"
    total_sec = int(seconds)
    hours, rem = divmod(total_sec, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}h {minutes}m {secs}s"


class ContextConsoleHandler:
    """Procesa los comandos `/contexto` (y `/context`) en la consola de J.A.R.V.I.S."""

    def __init__(
        self,
        *,
        context_engine: Optional[ContextAwarenessEngine] = None,
        output_fn: Callable[[str], None] = print,
    ) -> None:
        self._context_engine: Optional[ContextAwarenessEngine] = context_engine
        self._output_fn: Callable[[str], None] = output_fn

    @property
    def engine(self) -> ContextAwarenessEngine:
        if self._context_engine is None:
            self._context_engine = ContextAwarenessEngine()
        return self._context_engine

    @staticmethod
    def is_context_command(text: str) -> bool:
        """Indica si el comando ingresado corresponde a `/contexto` o `/context`."""
        stripped = (text or "").strip().lower()
        return (
            stripped == "/contexto"
            or stripped.startswith("/contexto ")
            or stripped == "/context"
            or stripped.startswith("/context ")
        )

    def handle_command(
        self,
        text: str,
        *,
        output_fn: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Ejecuta la consulta contextual solicitada y devuelve el reporte estructurado."""
        active_out = output_fn or self._output_fn
        stripped = (text or "").strip()
        lowered = stripped.lower()

        if lowered.startswith("/contexto"):
            rest = stripped[len("/contexto") :].strip()
        elif lowered.startswith("/context"):
            rest = stripped[len("/context") :].strip()
        else:
            rest = ""

        parts = rest.split(maxsplit=1) if rest else []
        subcmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        try:
            if subcmd in {"ayuda", "help", "?"}:
                msg = (
                    "Comandos de contexto N7 disponibles (solo lectura):\n"
                    "  /contexto                        -> Resumen general (sistema, hardware, recursos, almacenamiento)\n"
                    "  /contexto sistema                -> Sistema operativo, versión, arquitectura y actividad\n"
                    "  /contexto hardware               -> CPU, núcleos/hilos, RAM, GPU/VRAM y unidades\n"
                    "  /contexto recursos               -> Uso actual de CPU, RAM, GPU/VRAM y espacio libre\n"
                    "  /contexto almacenamiento         -> Unidades montadas y capacidad libre/total\n"
                    "  /contexto procesos [filtro]      -> Procesos activos acotados por consumo\n"
                    "  /contexto aplicaciones           -> Ventanas visibles, apps en ejecución e instaladas\n"
                    "  /contexto archivos <ruta>        -> Metadatos o listado acotado en raíz autorizada"
                )
                active_out(f"JARVIS:\n{msg}")
                return msg

            if not subcmd or subcmd in {"resumen", "general"}:
                snap = self.engine.get_context(
                    categories=[
                        ContextCategory.SYSTEM,
                        ContextCategory.HARDWARE,
                        ContextCategory.RESOURCES,
                        ContextCategory.STORAGE,
                    ],
                    refresh_resources=True,
                )
                report = self.format_snapshot(snap)
                active_out(f"JARVIS:\n{report}")
                return report

            if subcmd in {"sistema", "system", "so", "os"}:
                snap = self.engine.get_context(categories=[ContextCategory.SYSTEM])
                report = self.format_snapshot(snap)
                active_out(f"JARVIS:\n{report}")
                return report

            if subcmd in {"hardware", "hw"}:
                snap = self.engine.get_context(categories=[ContextCategory.HARDWARE])
                report = self.format_snapshot(snap)
                active_out(f"JARVIS:\n{report}")
                return report

            if subcmd in {"recursos", "resources", "uso"}:
                snap = self.engine.get_context(
                    categories=[ContextCategory.RESOURCES],
                    force_refresh=True,
                )
                report = self.format_snapshot(snap)
                active_out(f"JARVIS:\n{report}")
                return report

            if subcmd in {"almacenamiento", "storage", "disco", "discos", "unidades"}:
                snap = self.engine.get_context(categories=[ContextCategory.STORAGE])
                report = self.format_snapshot(snap)
                active_out(f"JARVIS:\n{report}")
                return report

            if subcmd in {"procesos", "proceso", "processes"}:
                snap = self.engine.get_context(
                    categories=[ContextCategory.PROCESSES],
                    process_filter=arg or None,
                    force_refresh=True,
                )
                report = self.format_snapshot(snap)
                active_out(f"JARVIS:\n{report}")
                return report

            if subcmd in {"aplicaciones", "applications", "apps", "ventanas"}:
                snap = self.engine.get_context(
                    categories=[ContextCategory.APPLICATIONS],
                    force_refresh=True,
                )
                report = self.format_snapshot(snap)
                active_out(f"JARVIS:\n{report}")
                return report

            if subcmd in {"archivos", "filesystem", "files", "directorio", "directorios"}:
                if not arg:
                    roots_preview = ", ".join(str(r) for r in self.engine.policy.authorized_roots[:4])
                    msg = (
                        "Uso: /contexto archivos <ruta_absoluta_autorizada>\n"
                        f"Raíces autorizadas actuales: {roots_preview}"
                    )
                    active_out(f"JARVIS:\n{msg}")
                    return msg
                # Validar explícitamente con la política para dar un error claro si fue rechazada
                self.engine.policy.validate_read_path(arg, must_exist=False)
                snap = self.engine.get_context(
                    categories=[ContextCategory.FILESYSTEM],
                    target_paths=[arg],
                )
                report = self.format_snapshot(snap)
                active_out(f"JARVIS:\n{report}")
                return report

            msg = (
                f"Categoría contextual no reconocida: '{subcmd}'. "
                "Opciones válidas: sistema, hardware, recursos, almacenamiento, procesos, aplicaciones, archivos."
            )
            active_out(f"JARVIS: {msg}")
            return msg

        except (ContextError, PermissionError, OSError) as err:
            msg = f"Consulta contextual rechazada o no disponible: {err}"
            active_out(f"JARVIS: {msg}")
            return msg

    def format_snapshot(self, snapshot: ContextSnapshot) -> str:
        """Convierte un `ContextSnapshot` en un reporte legible y acotado para consola."""
        lines: list[str] = [
            f"[Contexto N7 — Snapshot {snapshot.snapshot_id} | Solo lectura verificada]",
            f"Categorías consultadas: {', '.join(snapshot.requested_categories)}",
        ]
        if snapshot.cached_categories:
            lines.append(
                f"Categorías servidas desde caché TTL: {', '.join(snapshot.cached_categories)}"
            )

        if snapshot.system is not None:
            s = snapshot.system
            build_part = f" (Build {s.os_build})" if s.os_build else ""
            lines.append("\n[Sistema]")
            lines.append(f"  - SO: {s.os_name} {s.os_release} — versión {s.os_version}{build_part}")
            lines.append(f"  - Arquitectura: {s.architecture} | Python: {s.python_version}")
            lines.append(f"  - Tiempo activo: {_format_uptime(s.uptime_seconds)} | Entorno J.A.R.V.I.S.: {s.jarvis_env_mode}")

        if snapshot.hardware is not None:
            h = snapshot.hardware
            lines.append("\n[Hardware]")
            lines.append(f"  - CPU: {h.cpu_model or 'No disponible'} ({h.cpu_architecture})")
            lines.append(
                f"  - Núcleos: físicos={h.cpu_cores_physical if h.cpu_cores_physical is not None else 'N/D'}, "
                f"lógicos={h.cpu_cores_logical if h.cpu_cores_logical is not None else 'N/D'}"
                + (f" | Frecuencia: {h.cpu_freq_mhz} MHz" if h.cpu_freq_mhz else "")
            )
            lines.append(
                f"  - Memoria RAM: total={h.ram_total_gb if h.ram_total_gb is not None else 'N/D'} GB | "
                f"disponible={h.ram_available_gb if h.ram_available_gb is not None else 'N/D'} GB"
            )
            if h.gpus:
                for idx, gpu in enumerate(h.gpus, start=1):
                    vram_str = (
                        f"{gpu.vram_total_mb:.0f} MB ({gpu.vram_total_gb} GB)"
                        if gpu.vram_total_mb is not None
                        else "VRAM N/D"
                    )
                    drv_str = f" | Driver: {gpu.driver_version}" if gpu.driver_version else ""
                    lines.append(
                        f"  - GPU #{idx}: {gpu.name} | VRAM: {vram_str}{drv_str} [fuente: {gpu.detection_source}]"
                    )
            else:
                lines.append(
                    f"  - GPU: No disponible [{h.gpu_detection_status.value}]"
                    + (f" ({h.gpu_detection_notes})" if h.gpu_detection_notes else "")
                )

        if snapshot.resources is not None:
            r = snapshot.resources
            lines.append("\n[Recursos (bajo demanda)]")
            lines.append(f"  - Uso CPU: {_format_metric(r.cpu_percent)}")
            lines.append(f"  - RAM usada: {_format_metric(r.ram_used_gb)} ({_format_metric(r.ram_percent)})")
            lines.append(f"  - RAM libre: {_format_metric(r.ram_available_gb)}")
            lines.append(f"  - Uso GPU: {_format_metric(r.gpu_utilization_percent)}")
            lines.append(f"  - VRAM usada: {_format_metric(r.vram_used_mb)} | VRAM libre: {_format_metric(r.vram_free_mb)}")
            lines.append(f"  - Disco libre (workspace): {_format_metric(r.workspace_disk_free_gb)}")

        storage_vols = snapshot.storage
        if storage_vols is None and snapshot.hardware is not None and "storage" in snapshot.requested_categories:
            storage_vols = snapshot.hardware.storage_volumes
        if storage_vols is not None:
            lines.append("\n[Almacenamiento]")
            if not storage_vols:
                lines.append("  - No se detectaron unidades accesibles.")
            for vol in storage_vols:
                if vol.status == AvailabilityStatus.AVAILABLE and vol.total_gb is not None:
                    lines.append(
                        f"  - {vol.mountpoint} ({vol.filesystem}): "
                        f"{vol.free_gb} GB libres de {vol.total_gb} GB ({vol.percent_used}% usado)"
                    )
                else:
                    lines.append(
                        f"  - {vol.mountpoint} ({vol.filesystem}): [{vol.status.value}] {vol.notes or ''}"
                    )

        if snapshot.processes is not None:
            p_info = snapshot.processes
            lines.append(
                f"\n[Procesos en ejecución (mostrando {p_info.returned_count} de {p_info.total_sampled} muestreados"
                f", restringidos={p_info.access_denied_count})]"
            )
            for item in p_info.processes[:15]:
                mem_str = f"{item.memory_mb:.1f} MB" if item.memory_mb is not None else "N/D"
                cpu_str = f"{item.cpu_percent:.1f}%" if item.cpu_percent is not None else "N/D"
                lines.append(
                    f"  - [PID {item.pid}] {item.name} | RAM={mem_str} | CPU={cpu_str} | estado={item.status}"
                )

        if snapshot.applications is not None:
            a_info = snapshot.applications
            lines.append("\n[Aplicaciones y Ventanas]")
            if a_info.foreground_window:
                fg = a_info.foreground_window
                lines.append(f"  - Ventana activa: '{fg.title}' (PID={fg.pid}, proc={fg.process_name or 'N/D'})")
            else:
                lines.append("  - Ventana activa: No disponible")
            lines.append(f"  - Ventanas visibles detectadas: {len(a_info.visible_windows)}")
            for w in a_info.visible_windows[:8]:
                lines.append(f"      * {w.title} ({w.process_name or 'proceso desconocido'})")
            running_names = ", ".join(a.name for a in a_info.running_applications[:12]) or "ninguna"
            lines.append(f"  - Aplicaciones en ejecución (muestra): {running_names}")
            installed_names = (
                ", ".join(
                    f"{a.name}{' [activa]' if a.is_running else ''}"
                    for a in a_info.installed_applications[:12]
                )
                or "ninguna"
            )
            lines.append(f"  - Aplicaciones instaladas verificadas: {installed_names}")

        if snapshot.filesystem is not None:
            fs = snapshot.filesystem
            lines.append("\n[Sistema de Archivos (Raíces Autorizadas)]")
            for f_meta in fs.queried_files:
                if not f_meta.exists:
                    lines.append(f"  - {f_meta.path}: NO EXISTE (raíz autorizada: {f_meta.authorized_root})")
                elif f_meta.is_dir:
                    lines.append(f"  - {f_meta.path}: DIRECTORIO (raíz autorizada: {f_meta.authorized_root})")
                else:
                    lines.append(
                        f"  - {f_meta.path}: ARCHIVO ({f_meta.file_category}, ext='{f_meta.extension}', "
                        f"{f_meta.size_bytes} bytes)"
                    )
            for d_snap in fs.directories:
                lines.append(
                    f"  - Directorio explorado '{d_snap.root_path}': "
                    f"{len(d_snap.entries)} entradas listadas (truncado={d_snap.truncated})"
                )
                for entry in d_snap.entries[:12]:
                    kind_label = "DIR" if entry.is_dir else f"FILE ({entry.size_bytes or 0} B)"
                    lines.append(f"      * {entry.name} [{kind_label}]")

        if snapshot.partial_errors:
            lines.append("\n[Errores parciales aislados]")
            for err in snapshot.partial_errors:
                lines.append(f"  - [{err.category}/{err.provider_name}] {err.error_type}: {err.message}")

        return "\n".join(lines)
