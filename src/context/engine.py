"""Coordinador central del Context Awareness Engine (N7).

Responsabilidades principales:
1. Gestionar de forma modular los proveedores de contexto (`SystemInfoProvider`,
   `HardwareInfoProvider`, `StorageInfoProvider`, `ResourceMonitoringProvider`,
   `ProcessAwarenessProvider`, `ApplicationAwarenessProvider`, `FileSystemContextProvider`).
2. Aplicar `ContextAccessPolicy` (categorías habilitadas, raíces autorizadas, límites
   de cantidad/profundidad y modo estricto de solo lectura).
3. Ejecutar consultas selectivas bajo demanda únicamente para las categorías solicitadas.
4. Aislar errores parciales y timeouts por proveedor sin destruir el resto del snapshot.
5. Mantener una caché ligera con TTL configurable para evitar sondeos repetitivos.
6. Construir sobres de datos no confiables (`ContextUntrustedEnvelope`) y resúmenes
   minimizados y redactados para su integración segura con N6 (`GoalEngine`).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import threading
import time
from typing import Any, Callable, Optional, Sequence

from config import settings
from src.brain.exceptions import redact_secrets
from src.context.filesystem_provider import FileSystemContextProvider
from src.context.models import (
    ApplicationAwarenessInfo,
    AvailabilityStatus,
    ContextAccessDeniedError,
    ContextCategory,
    ContextPolicyError,
    ContextSnapshot,
    ContextTimeoutError,
    ContextUntrustedEnvelope,
    FileSystemContextInfo,
    HardwareContextInfo,
    ProcessAwarenessInfo,
    ProviderIssue,
    ResourceUsageInfo,
    StorageVolumeInfo,
    SystemContextInfo,
    generate_context_id,
    utc_now_iso,
)
from src.context.policy import ContextAccessPolicy, sanitize_untrusted_label
from src.context.process_app_provider import (
    ApplicationAwarenessProvider,
    ProcessAwarenessProvider,
)
from src.context.system_hardware_provider import (
    HardwareInfoProvider,
    ResourceMonitoringProvider,
    StorageInfoProvider,
    SystemInfoProvider,
)


class ContextAwarenessEngine:
    """Motor N7 de observación contextual en solo lectura para Windows."""

    def __init__(
        self,
        *,
        policy: Optional[ContextAccessPolicy] = None,
        enabled: Optional[bool] = None,
        cache_ttl_sec: Optional[float] = None,
        provider_timeout_sec: Optional[float] = None,
        system_provider: Optional[SystemInfoProvider] = None,
        storage_provider: Optional[StorageInfoProvider] = None,
        hardware_provider: Optional[HardwareInfoProvider] = None,
        resource_provider: Optional[ResourceMonitoringProvider] = None,
        process_provider: Optional[ProcessAwarenessProvider] = None,
        application_provider: Optional[ApplicationAwarenessProvider] = None,
        filesystem_provider: Optional[FileSystemContextProvider] = None,
    ) -> None:
        self.policy: ContextAccessPolicy = policy or ContextAccessPolicy()
        self.enabled: bool = (
            bool(enabled)
            if enabled is not None
            else bool(settings.CONTEXT_ENGINE_ENABLED)
        )
        self.cache_ttl_sec: float = max(
            0.0,
            float(
                cache_ttl_sec
                if cache_ttl_sec is not None
                else settings.CONTEXT_CACHE_TTL_SEC
            ),
        )
        self.provider_timeout_sec: float = max(
            0.2,
            float(
                provider_timeout_sec
                if provider_timeout_sec is not None
                else settings.CONTEXT_PROVIDER_TIMEOUT_SEC
            ),
        )

        self.system_provider: SystemInfoProvider = system_provider or SystemInfoProvider(
            self.policy
        )
        self.storage_provider: StorageInfoProvider = storage_provider or StorageInfoProvider(
            self.policy
        )
        self.hardware_provider: HardwareInfoProvider = hardware_provider or HardwareInfoProvider(
            self.policy,
            storage_provider=self.storage_provider,
            timeout_sec=self.provider_timeout_sec,
        )
        self.resource_provider: ResourceMonitoringProvider = (
            resource_provider
            or ResourceMonitoringProvider(
                self.policy,
                hardware_provider=self.hardware_provider,
            )
        )
        self.process_provider: ProcessAwarenessProvider = (
            process_provider or ProcessAwarenessProvider(self.policy)
        )
        self.application_provider: ApplicationAwarenessProvider = (
            application_provider
            or ApplicationAwarenessProvider(
                self.policy,
                process_provider=self.process_provider,
            )
        )
        self.filesystem_provider: FileSystemContextProvider = (
            filesystem_provider or FileSystemContextProvider(self.policy)
        )

        self._cache_lock = threading.RLock()
        self._cache: dict[ContextCategory, tuple[float, Any]] = {}
        # F6.B: Locks independientes por categoría para coalescer solicitudes concurrentes
        # ante un cache miss sin bloquear proveedores de categorías distintas ni causar deadlocks.
        self._category_locks: dict[ContextCategory, threading.RLock] = {
            cat: threading.RLock() for cat in ContextCategory
        }

    def clear_cache(self) -> None:
        """Invalida todas las entradas almacenadas en la caché temporal de N7."""
        with self._cache_lock:
            self._cache.clear()

    def execute_operation(self, operation_name: str, **_kwargs: Any) -> None:
        """Punto de control defensivo: N7 rechaza cualquier intento de invocar operaciones mutantes."""
        self.policy.assert_read_only_operation(operation_name)
        raise ContextAccessDeniedError(
            f"N7 ContextAwarenessEngine no ejecuta acciones ni comandos ('{operation_name}'). "
            "Toda operación debe canalizarse mediante N6, PolicyEngine y ChainOrchestrator."
        )

    def get_context(
        self,
        categories: Optional[Sequence[str | ContextCategory]] = None,
        *,
        target_paths: Optional[Sequence[str]] = None,
        search_root: Optional[str] = None,
        search_query: str = "",
        process_filter: Optional[str] = None,
        target_app: Optional[str] = None,
        include_windows: bool = True,
        force_refresh: bool = False,
        refresh_resources: bool = False,
    ) -> ContextSnapshot:
        """Obtiene un `ContextSnapshot` consultando únicamente las categorías solicitadas.

        Aisla cualquier fallo individual de un proveedor en `partial_errors` y `unavailable_fields`
        para que el resto del contexto siga disponible.
        """
        if not self.enabled:
            raise ContextPolicyError(
                "El motor de observación contextual N7 se encuentra deshabilitado por configuración."
            )

        validated_cats = self.policy.validate_requested_categories(categories)
        now_iso = utc_now_iso()

        sources_consulted: list[str] = []
        unavailable_fields: list[str] = []
        partial_errors: list[ProviderIssue] = []
        cached_categories: list[str] = []

        sys_info: Optional[SystemContextInfo] = None
        hw_info: Optional[HardwareContextInfo] = None
        res_info: Optional[ResourceUsageInfo] = None
        storage_info: Optional[tuple[StorageVolumeInfo, ...]] = None
        proc_info: Optional[ProcessAwarenessInfo] = None
        app_info: Optional[ApplicationAwarenessInfo] = None
        fs_info: Optional[FileSystemContextInfo] = None

        for cat in validated_cats:
            if cat == ContextCategory.SYSTEM:
                sources_consulted.append("SystemInfoProvider")
                sys_info = self._collect_with_isolation(
                    cat,
                    "SystemInfoProvider",
                    lambda: self.system_provider.collect(),
                    use_cache=not force_refresh,
                    partial_errors=partial_errors,
                    unavailable_fields=unavailable_fields,
                    cached_categories=cached_categories,
                )
                if sys_info is not None and sys_info.uptime_seconds is None:
                    unavailable_fields.append("system.uptime_seconds")

            elif cat == ContextCategory.HARDWARE:
                sources_consulted.append("HardwareInfoProvider")
                hw_info = self._collect_with_isolation(
                    cat,
                    "HardwareInfoProvider",
                    lambda: self.hardware_provider.collect(include_storage=True),
                    use_cache=not force_refresh,
                    partial_errors=partial_errors,
                    unavailable_fields=unavailable_fields,
                    cached_categories=cached_categories,
                )
                if hw_info is not None:
                    if hw_info.gpu_detection_status == AvailabilityStatus.UNAVAILABLE or not hw_info.gpus:
                        unavailable_fields.append("hardware.gpus")
                    if hw_info.cpu_freq_mhz is None:
                        unavailable_fields.append("hardware.cpu_freq_mhz")

            elif cat == ContextCategory.STORAGE:
                sources_consulted.append("StorageInfoProvider")
                if hw_info is not None and hw_info.storage_volumes:
                    storage_info = hw_info.storage_volumes
                    if ContextCategory.HARDWARE.value in cached_categories:
                        cached_categories.append(cat.value)
                else:
                    storage_info = self._collect_with_isolation(
                        cat,
                        "StorageInfoProvider",
                        lambda: self.storage_provider.collect(),
                        use_cache=not force_refresh,
                        partial_errors=partial_errors,
                        unavailable_fields=unavailable_fields,
                        cached_categories=cached_categories,
                    )
                if storage_info is not None and len(storage_info) == 0:
                    unavailable_fields.append("storage.volumes")

            elif cat == ContextCategory.RESOURCES:
                sources_consulted.append("ResourceMonitoringProvider")
                use_res_cache = (not force_refresh) and (not refresh_resources)
                res_info = self._collect_with_isolation(
                    cat,
                    "ResourceMonitoringProvider",
                    lambda: self.resource_provider.collect(),
                    use_cache=use_res_cache,
                    partial_errors=partial_errors,
                    unavailable_fields=unavailable_fields,
                    cached_categories=cached_categories,
                )
                if res_info is not None:
                    for metric_attr in (
                        "cpu_percent",
                        "ram_used_gb",
                        "ram_available_gb",
                        "ram_percent",
                        "gpu_utilization_percent",
                        "vram_used_mb",
                        "vram_free_mb",
                        "workspace_disk_free_gb",
                    ):
                        reading = getattr(res_info, metric_attr, None)
                        if reading is not None and reading.status != AvailabilityStatus.AVAILABLE:
                            unavailable_fields.append(f"resources.{metric_attr}")

            elif cat == ContextCategory.PROCESSES:
                sources_consulted.append("ProcessAwarenessProvider")
                can_cache_proc = (not force_refresh) and (not process_filter)
                proc_info = self._collect_with_isolation(
                    cat,
                    "ProcessAwarenessProvider",
                    lambda: self.process_provider.collect(filter_name=process_filter),
                    use_cache=can_cache_proc,
                    partial_errors=partial_errors,
                    unavailable_fields=unavailable_fields,
                    cached_categories=cached_categories,
                )

            elif cat == ContextCategory.APPLICATIONS:
                sources_consulted.append("ApplicationAwarenessProvider")
                can_cache_app = (not force_refresh) and include_windows and (not target_app)

                def _collect_apps() -> ApplicationAwarenessInfo:
                    if include_windows and not target_app:
                        raw_app = self.application_provider.collect()
                    else:
                        try:
                            raw_app = self.application_provider.collect(
                                include_windows=include_windows,
                                target_app=target_app,
                            )
                        except TypeError:
                            raw_app = self.application_provider.collect()
                    return self._apply_app_minimization(
                        raw_app,
                        include_windows=include_windows,
                        target_app=target_app,
                    )

                app_info = self._collect_with_isolation(
                    cat,
                    "ApplicationAwarenessProvider",
                    _collect_apps,
                    use_cache=can_cache_app,
                    partial_errors=partial_errors,
                    unavailable_fields=unavailable_fields,
                    cached_categories=cached_categories,
                )

            elif cat == ContextCategory.FILESYSTEM:
                sources_consulted.append("FileSystemContextProvider")
                fs_info = self._collect_with_isolation(
                    cat,
                    "FileSystemContextProvider",
                    lambda: self.filesystem_provider.collect(
                        target_paths=target_paths,
                        search_root=search_root,
                        search_query=search_query,
                    ),
                    use_cache=False,
                    partial_errors=partial_errors,
                    unavailable_fields=unavailable_fields,
                    cached_categories=cached_categories,
                )
                if fs_info is not None and fs_info.rejected_paths:
                    unavailable_fields.append("filesystem.rejected_paths")

        return ContextSnapshot(
            snapshot_id=generate_context_id("ctx"),
            captured_at=now_iso,
            requested_categories=tuple(c.value for c in validated_cats),
            sources_consulted=tuple(dict.fromkeys(sources_consulted)),
            system=sys_info,
            hardware=hw_info,
            resources=res_info,
            storage=storage_info,
            processes=proc_info,
            applications=app_info,
            filesystem=fs_info,
            unavailable_fields=tuple(dict.fromkeys(unavailable_fields)),
            partial_errors=tuple(partial_errors),
            cached_categories=tuple(dict.fromkeys(cached_categories)),
            read_only_verified=True,
        )

    @staticmethod
    def _apply_app_minimization(
        app_info: Optional[ApplicationAwarenessInfo],
        *,
        include_windows: bool,
        target_app: Optional[str],
    ) -> Optional[ApplicationAwarenessInfo]:
        """Refuerza minimización F4 sobre `ApplicationAwarenessInfo` incluso si el proveedor fue simulado."""
        if app_info is None:
            return None
        target_low = (target_app or "").strip().lower()
        if include_windows and not target_low:
            return app_info

        fg_win = None if not include_windows else app_info.foreground_window
        vis_wins = () if not include_windows else app_info.visible_windows
        run_apps = app_info.running_applications
        inst_apps = app_info.installed_applications

        if target_low:
            if fg_win is not None and not (
                target_low in fg_win.title.lower()
                or (fg_win.process_name and target_low in fg_win.process_name.lower())
            ):
                fg_win = None
            vis_wins = tuple(
                w
                for w in vis_wins
                if target_low in w.title.lower()
                or (w.process_name and target_low in w.process_name.lower())
            )
            run_apps = tuple(
                a
                for a in run_apps
                if target_low in a.name.lower()
                or (a.executable_name and target_low in a.executable_name.lower())
            )
            inst_apps = tuple(
                a
                for a in inst_apps
                if target_low in a.name.lower()
                or (a.executable_name and target_low in a.executable_name.lower())
            )

        return ApplicationAwarenessInfo(
            foreground_window=fg_win,
            visible_windows=vis_wins,
            running_applications=run_apps,
            installed_applications=inst_apps,
            unavailable_count=app_info.unavailable_count,
            notes=app_info.notes,
            captured_at=app_info.captured_at,
            status=app_info.status,
        )

    def get_context_safe(
        self,
        categories: Optional[Sequence[str | ContextCategory]] = None,
        *,
        target_paths: Optional[Sequence[str]] = None,
        search_root: Optional[str] = None,
        search_query: str = "",
        process_filter: Optional[str] = None,
        target_app: Optional[str] = None,
        include_windows: bool = True,
        force_refresh: bool = False,
        refresh_resources: bool = False,
    ) -> ContextSnapshot:
        """Variante tolerante a fallos para N6: nunca lanza excepción si N7 está deshabilitado o restringido."""
        try:
            return self.get_context(
                categories=categories,
                target_paths=target_paths,
                search_root=search_root,
                search_query=search_query,
                process_filter=process_filter,
                target_app=target_app,
                include_windows=include_windows,
                force_refresh=force_refresh,
                refresh_resources=refresh_resources,
            )
        except Exception as err:
            now_iso = utc_now_iso()
            req_cats: tuple[str, ...] = ()
            if categories:
                req_cats = tuple(
                    c.value if isinstance(c, ContextCategory) else str(c)
                    for c in categories
                )
            issue = ProviderIssue(
                category="engine",
                provider_name="ContextAwarenessEngine",
                error_type=type(err).__name__,
                message=redact_secrets(str(err)),
                captured_at=now_iso,
            )
            return ContextSnapshot(
                snapshot_id=generate_context_id("ctx"),
                captured_at=now_iso,
                requested_categories=req_cats,
                sources_consulted=(),
                unavailable_fields=req_cats or ("all",),
                partial_errors=(issue,),
                read_only_verified=True,
            )

    def _collect_with_isolation(
        self,
        category: ContextCategory,
        provider_name: str,
        fn: Callable[[], Any],
        *,
        use_cache: bool,
        partial_errors: list[ProviderIssue],
        unavailable_fields: list[str],
        cached_categories: Optional[list[str]] = None,
    ) -> Any:
        """Ejecuta un proveedor con coalescencia concurrente por categoría, caché TTL y timeout."""
        if use_cache and self.cache_ttl_sec > 0.0:
            with self._cache_lock:
                cached = self._cache.get(category)
                if cached is not None:
                    ts, val = cached
                    if (time.monotonic() - ts) <= self.cache_ttl_sec:
                        if cached_categories is not None:
                            cached_categories.append(category.value)
                        return val

            # F6.B: Serializar por categoría cuando use_cache=True para evitar recolecciones duplicadas
            # ante múltiples solicitudes concurrentes sobre un cache miss, sin bloquear otras categorías.
            cat_lock = self._category_locks[category]
            with cat_lock:
                with self._cache_lock:
                    cached = self._cache.get(category)
                    if cached is not None:
                        ts, val = cached
                        if (time.monotonic() - ts) <= self.cache_ttl_sec:
                            if cached_categories is not None:
                                cached_categories.append(category.value)
                            return val
                return self._execute_provider_call(
                    category,
                    provider_name,
                    fn,
                    use_cache=use_cache,
                    partial_errors=partial_errors,
                    unavailable_fields=unavailable_fields,
                )

        return self._execute_provider_call(
            category,
            provider_name,
            fn,
            use_cache=False,
            partial_errors=partial_errors,
            unavailable_fields=unavailable_fields,
        )

    def _execute_provider_call(
        self,
        category: ContextCategory,
        provider_name: str,
        fn: Callable[[], Any],
        *,
        use_cache: bool,
        partial_errors: list[ProviderIssue],
        unavailable_fields: list[str],
    ) -> Any:
        """Ejecuta `fn` dentro de un `ThreadPoolExecutor` con timeout estricto y captura de errores."""
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"n7-{category.value}")
        future = executor.submit(fn)
        try:
            result = future.result(timeout=self.provider_timeout_sec)
            if use_cache and self.cache_ttl_sec > 0.0 and result is not None:
                with self._cache_lock:
                    self._cache[category] = (time.monotonic(), result)
            return result
        except FutureTimeoutError:
            future.cancel()
            msg = (
                f"El proveedor '{provider_name}' excedió el timeout de "
                f"{self.provider_timeout_sec:.2f}s."
            )
            partial_errors.append(
                ProviderIssue(
                    category=category.value,
                    provider_name=provider_name,
                    error_type=ContextTimeoutError.__name__,
                    message=msg,
                )
            )
            unavailable_fields.append(category.value)
            return None
        except Exception as exc:
            clean_msg = redact_secrets(str(exc)) or type(exc).__name__
            partial_errors.append(
                ProviderIssue(
                    category=category.value,
                    provider_name=provider_name,
                    error_type=type(exc).__name__,
                    message=clean_msg,
                )
            )
            unavailable_fields.append(category.value)
            return None
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def infer_relevant_categories_for_goal(
        self,
        raw_goal: str,
        *,
        target_paths: Optional[Sequence[str]] = None,
        target_app: Optional[str] = None,
    ) -> tuple[ContextCategory, ...]:
        """Determina qué categorías de N7 son relevantes para un objetivo de N6.

        Evita consultas innecesarias: si el objetivo no requiere datos del entorno local,
        devuelve una tupla vacía `()`.
        """
        if not self.enabled:
            return ()

        lowered = (raw_goal or "").lower()
        selected: list[ContextCategory] = []

        def _add_if_enabled(cat: ContextCategory) -> None:
            if cat in self.policy.enabled_categories and cat not in selected:
                selected.append(cat)

        hw_keywords = (
            "optimiz",
            "rendimiento",
            "fps",
            "jugar",
            "juego",
            "gaming",
            "puede ejecutar",
            "puedo ejecutar",
            "corre ",
            "correr ",
            "limitacion",
            "limitación",
            "requisito",
            "hardware",
            "pc",
            "equipo",
            "computadora",
            "gpu",
            "placa de video",
            "tarjetas grafica",
            "tarjeta grafica",
            "tarjeta gráfica",
            "gtx",
            "rtx",
            "radeon",
            "vram",
            "cpu",
            "procesador",
            "ram",
            "memoria",
            "diagnost",
            "diagnóstico",
            "estado del sistema",
            "recursos",
            "cuello de botella",
        )
        if any(kw in lowered for kw in hw_keywords):
            _add_if_enabled(ContextCategory.SYSTEM)
            _add_if_enabled(ContextCategory.HARDWARE)
            _add_if_enabled(ContextCategory.RESOURCES)

        storage_keywords = (
            "disco",
            "almacenamiento",
            "espacio",
            "gb libre",
            "gigas libre",
            "unidad",
            "particion",
            "partición",
            "instalar",
            "puede ejecutar",
            "puedo ejecutar",
        )
        if any(kw in lowered for kw in storage_keywords):
            _add_if_enabled(ContextCategory.STORAGE)

        process_keywords = (
            "proceso",
            "procesos",
            "segundo plano",
            "consumiendo",
            "abierto",
            "ejecutando",
            "ejecutándose",
            "lento",
            "saturado",
        )
        if any(kw in lowered for kw in process_keywords):
            _add_if_enabled(ContextCategory.PROCESSES)

        app_keywords = (
            "aplicacion",
            "aplicación",
            "aplicaciones",
            "programa",
            "programas",
            "ventana",
            "ventanas",
            "instalado",
            "instalada",
        )
        if target_app or any(kw in lowered for kw in app_keywords):
            _add_if_enabled(ContextCategory.APPLICATIONS)

        if target_paths:
            _add_if_enabled(ContextCategory.FILESYSTEM)

        return tuple(selected)

    def build_untrusted_envelope(
        self,
        snapshot: ContextSnapshot,
        *,
        target_app: Optional[str] = None,
        for_persistence: bool = False,
    ) -> ContextUntrustedEnvelope:
        """Empaqueta un `ContextSnapshot` con minimización estricta y frontera de datos no confiables (F4).

        Criterios de privacidad F4:
        - No incluye títulos arbitrarios de ventanas (`visible_windows`, `foreground_window`)
          cuando `minimize_for_llm` o `for_persistence` están activos.
        - Cuando `for_persistence=True` (entrega a N6 / `.goal.json`), elimina `top_processes`
          y listas de aplicaciones no relacionadas, conservando únicamente el estado de `target_app`
          cuando fue solicitado explícitamente.
        """
        minimized: dict[str, Any] = {}
        target_low = (target_app or "").strip().lower()

        if snapshot.system is not None:
            minimized["system"] = {
                "os": f"{snapshot.system.os_name} {snapshot.system.os_release}",
                "version": snapshot.system.os_version,
                "build": snapshot.system.os_build,
                "architecture": snapshot.system.architecture,
                "python": snapshot.system.python_version,
                "uptime_seconds": snapshot.system.uptime_seconds,
            }

        if snapshot.hardware is not None:
            gpus_summary = [
                {
                    "name": g.name,
                    "vendor": g.vendor,
                    "vram_total_mb": g.vram_total_mb,
                    "vram_total_gb": g.vram_total_gb,
                    "driver_version": g.driver_version,
                    "source": g.detection_source,
                }
                for g in snapshot.hardware.gpus[:2]
            ]
            minimized["hardware"] = {
                "cpu_model": snapshot.hardware.cpu_model,
                "cpu_cores_physical": snapshot.hardware.cpu_cores_physical,
                "cpu_cores_logical": snapshot.hardware.cpu_cores_logical,
                "ram_total_gb": snapshot.hardware.ram_total_gb,
                "ram_available_gb": snapshot.hardware.ram_available_gb,
                "gpus": gpus_summary,
                "gpu_detection_status": snapshot.hardware.gpu_detection_status.value,
            }

        if snapshot.resources is not None:
            minimized["resources"] = {
                "cpu_percent": snapshot.resources.cpu_percent.value,
                "ram_used_gb": snapshot.resources.ram_used_gb.value,
                "ram_available_gb": snapshot.resources.ram_available_gb.value,
                "ram_percent": snapshot.resources.ram_percent.value,
                "gpu_utilization_percent": snapshot.resources.gpu_utilization_percent.value,
                "vram_used_mb": snapshot.resources.vram_used_mb.value,
                "vram_free_mb": snapshot.resources.vram_free_mb.value,
                "workspace_disk_free_gb": snapshot.resources.workspace_disk_free_gb.value,
            }

        if snapshot.storage is not None:
            minimized["storage"] = [
                {
                    "mountpoint": vol.mountpoint,
                    "filesystem": vol.filesystem,
                    "total_gb": vol.total_gb,
                    "free_gb": vol.free_gb,
                    "percent_used": vol.percent_used,
                    "status": vol.status.value,
                }
                for vol in snapshot.storage[:6]
            ]

        if snapshot.processes is not None:
            if for_persistence:
                proc_summary: dict[str, Any] = {
                    "total_sampled": snapshot.processes.total_sampled,
                    "returned_count": snapshot.processes.returned_count,
                    "access_denied_count": snapshot.processes.access_denied_count,
                }
                if target_low:
                    matched_procs = [
                        {
                            "pid": p.pid,
                            "name": sanitize_untrusted_label(p.name, max_len=64),
                            "memory_mb": p.memory_mb,
                            "cpu_percent": p.cpu_percent,
                        }
                        for p in snapshot.processes.processes
                        if target_low in p.name.lower()
                    ]
                    proc_summary["target_app_processes"] = matched_procs[:4]
                minimized["processes"] = proc_summary
            else:
                proc_cap = 8 if self.policy.minimize_for_llm else len(snapshot.processes.processes)
                proc_source = (
                    [p for p in snapshot.processes.processes if target_low in p.name.lower()]
                    if target_low
                    else list(snapshot.processes.processes)
                )
                minimized["processes"] = {
                    "top_processes": [
                        {
                            "pid": p.pid,
                            "name": sanitize_untrusted_label(p.name, max_len=64),
                            "memory_mb": p.memory_mb,
                            "cpu_percent": p.cpu_percent,
                        }
                        for p in proc_source[:proc_cap]
                    ],
                    "total_sampled": snapshot.processes.total_sampled,
                    "access_denied_count": snapshot.processes.access_denied_count,
                }

        if snapshot.applications is not None:
            if target_low:
                is_running = any(
                    target_low in a.name.lower()
                    or (a.executable_name and target_low in a.executable_name.lower())
                    for a in snapshot.applications.running_applications
                )
                is_installed = is_running or any(
                    target_low in a.name.lower()
                    or (a.executable_name and target_low in a.executable_name.lower())
                    for a in snapshot.applications.installed_applications
                )
                minimized["applications"] = {
                    "target_app": sanitize_untrusted_label(target_app, max_len=64),
                    "is_running": is_running,
                    "is_installed": is_installed,
                }
            elif for_persistence:
                minimized["applications"] = {
                    "running_apps_count": len(snapshot.applications.running_applications),
                    "installed_apps_count": len(snapshot.applications.installed_applications),
                }
            else:
                app_cap = 10 if self.policy.minimize_for_llm else len(snapshot.applications.installed_applications)
                app_payload: dict[str, Any] = {
                    "running_apps": [
                        sanitize_untrusted_label(a.name, max_len=64)
                        for a in snapshot.applications.running_applications[:app_cap]
                    ],
                    "installed_apps": [
                        {
                            "name": sanitize_untrusted_label(a.name, max_len=64),
                            "is_running": a.is_running,
                        }
                        for a in snapshot.applications.installed_applications[:app_cap]
                    ],
                }
                if not self.policy.minimize_for_llm:
                    win_cap = len(snapshot.applications.visible_windows)
                    app_payload["foreground_window"] = (
                        sanitize_untrusted_label(snapshot.applications.foreground_window.title, max_len=80)
                        if snapshot.applications.foreground_window
                        else None
                    )
                    app_payload["visible_windows"] = [
                        sanitize_untrusted_label(w.title, max_len=80)
                        for w in snapshot.applications.visible_windows[:win_cap]
                    ]
                minimized["applications"] = app_payload

        if snapshot.filesystem is not None:
            minimized["filesystem"] = {
                "queried_files": [
                    {
                        "name": sanitize_untrusted_label(f.name, max_len=80),
                        "exists": f.exists,
                        "is_dir": f.is_dir,
                        "extension": f.extension,
                        "category": f.file_category,
                        "size_bytes": f.size_bytes,
                    }
                    for f in snapshot.filesystem.queried_files[:15]
                ],
                "relevant_matches_count": len(snapshot.filesystem.relevant_matches),
                "rejected_paths_count": len(snapshot.filesystem.rejected_paths),
            }

        if snapshot.unavailable_fields:
            minimized["unavailable_fields"] = list(snapshot.unavailable_fields)
        if snapshot.partial_errors:
            minimized["partial_errors"] = [
                {"category": e.category, "error_type": e.error_type, "message": e.message}
                for e in snapshot.partial_errors
            ]

        return ContextUntrustedEnvelope(
            categories_included=snapshot.requested_categories,
            minimized_data=minimized,
            captured_at=snapshot.captured_at,
        )

    def build_n6_context_payload(
        self,
        snapshot: ContextSnapshot,
        *,
        target_app: Optional[str] = None,
    ) -> dict[str, Any]:
        """Traduce un `ContextSnapshot` en un diccionario seguro, minimizado y sin datos volátiles para N6."""
        envelope = self.build_untrusted_envelope(
            snapshot,
            target_app=target_app,
            for_persistence=True,
        )
        hw_dict: dict[str, Any] = {}

        if snapshot.hardware is not None:
            if snapshot.hardware.cpu_model:
                hw_dict["cpu_model"] = snapshot.hardware.cpu_model
            if snapshot.hardware.cpu_cores_physical is not None:
                hw_dict["cpu_cores_physical"] = snapshot.hardware.cpu_cores_physical
            if snapshot.hardware.cpu_cores_logical is not None:
                hw_dict["cpu_cores_logical"] = snapshot.hardware.cpu_cores_logical
            if snapshot.hardware.ram_total_gb is not None:
                hw_dict["ram_total_gb"] = snapshot.hardware.ram_total_gb
            if snapshot.hardware.ram_available_gb is not None:
                hw_dict["ram_available_gb"] = snapshot.hardware.ram_available_gb
            if snapshot.hardware.gpus:
                primary = snapshot.hardware.gpus[0]
                hw_dict["gpu"] = primary.name
                if primary.vram_total_gb is not None:
                    hw_dict["vram_gb"] = primary.vram_total_gb
                if primary.vram_total_mb is not None:
                    hw_dict["vram_mb"] = primary.vram_total_mb
                if primary.driver_version:
                    hw_dict["gpu_driver"] = primary.driver_version

        if snapshot.resources is not None:
            if snapshot.resources.cpu_percent.value is not None:
                hw_dict["cpu_usage_percent"] = snapshot.resources.cpu_percent.value
            if snapshot.resources.ram_available_gb.value is not None:
                hw_dict["ram_available_gb"] = snapshot.resources.ram_available_gb.value
            if snapshot.resources.gpu_utilization_percent.value is not None:
                hw_dict["gpu_usage_percent"] = snapshot.resources.gpu_utilization_percent.value
            if snapshot.resources.vram_free_mb.value is not None:
                hw_dict["vram_free_mb"] = snapshot.resources.vram_free_mb.value
            if snapshot.resources.workspace_disk_free_gb.value is not None:
                hw_dict["workspace_disk_free_gb"] = snapshot.resources.workspace_disk_free_gb.value

        env_summary: dict[str, str] = {}
        if snapshot.system is not None:
            env_summary["os"] = f"{snapshot.system.os_name} {snapshot.system.os_release}"
            env_summary["os_version"] = snapshot.system.os_version
            env_summary["architecture"] = snapshot.system.architecture
            env_summary["python_version"] = snapshot.system.python_version

        return {
            "snapshot_id": snapshot.snapshot_id,
            "captured_at": snapshot.captured_at,
            "requested_categories": list(snapshot.requested_categories),
            "hardware": hw_dict,
            "environment": env_summary,
            "unavailable_fields": list(snapshot.unavailable_fields),
            "partial_errors": [e.to_dict() for e in snapshot.partial_errors],
            "untrusted_envelope": envelope.to_dict(),
        }
