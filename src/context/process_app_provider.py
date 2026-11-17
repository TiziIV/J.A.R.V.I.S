"""Proveedores de observación de procesos y aplicaciones en Windows (N7).

Garantías estrictas de seguridad y privacidad:
1. Solo lectura absoluta: jamás invoca `kill()`, `terminate()`, `suspend()`, `resume()`,
   `nice()`, `ionice()`, `SendMessage`, `PostMessage` ni `TerminateProcess`.
2. Protección de secretos en procesos: nunca consulta `cmdline()` ni `environ()` de ningún
   proceso para impedir la filtración de tokens, claves API o contraseñas pasadas por CLI.
3. Manejo resiliente de `psutil.NoSuchProcess`, `psutil.AccessDenied`, `psutil.ZombieProcess`
   y `OSError`, contabilizando accesos restringidos y procesos efímeros sin interrumpir la captura.
4. Diferenciación explícita entre procesos del sistema, aplicaciones de usuario en ejecución,
   ventanas visibles/activas y aplicaciones instaladas verificables.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Callable, Iterable, Optional

import psutil

from src.context.models import (
    ActiveWindowInfo,
    ApplicationAwarenessInfo,
    ApplicationItem,
    AvailabilityStatus,
    ProcessAwarenessInfo,
    ProcessContextItem,
    utc_now_iso,
)
from src.context.policy import ContextAccessPolicy, sanitize_untrusted_label


# Procesos internos o de subsistema de Windows que NO deben confundirse con aplicaciones de usuario.
WINDOWS_BACKGROUND_SYSTEM_PROCESSES: frozenset[str] = frozenset(
    {
        "system idle process",
        "system",
        "registry",
        "memcompression",
        "smss.exe",
        "csrss.exe",
        "wininit.exe",
        "services.exe",
        "lsass.exe",
        "svchost.exe",
        "fontdrvhost.exe",
        "winlogon.exe",
        "dwm.exe",
        "sihost.exe",
        "taskhostw.exe",
        "conhost.exe",
        "runtimebroker.exe",
        "searchindexer.exe",
        "searchhost.exe",
        "startmenuexperiencehost.exe",
        "shellexperiencehost.exe",
        "dllhost.exe",
        "wmiprvse.exe",
        "spoolsv.exe",
        "audiodg.exe",
        "ctfmon.exe",
        "smartscreen.exe",
        "securityhealthservice.exe",
        "securityhealthsystray.exe",
        "msmpeng.exe",
        "nissrv.exe",
        "aggregatorhost.exe",
        "backgroundtaskhost.exe",
        "textinputhost.exe",
        "widgetservice.exe",
        "widgets.exe",
    }
)

# Catálogo curado y seguro de aplicaciones conocidas para comprobar disponibilidad instalada
# sin escanear indiscriminadamente el registro de Windows ni carpetas privadas del usuario.
KNOWN_APPLICATION_CATALOG: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "Visual Studio Code",
        ("code", "code.cmd", "Code.exe"),
        (
            r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
            r"C:\Program Files\Microsoft VS Code\Code.exe",
        ),
    ),
    (
        "Google Chrome",
        ("chrome", "chrome.exe"),
        (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ),
    ),
    (
        "Microsoft Edge",
        ("msedge", "msedge.exe"),
        (
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ),
    ),
    (
        "Mozilla Firefox",
        ("firefox", "firefox.exe"),
        (
            r"C:\Program Files\Mozilla Firefox\firefox.exe",
            r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
        ),
    ),
    (
        "Steam",
        ("steam", "steam.exe"),
        (
            r"C:\Program Files (x86)\Steam\steam.exe",
            r"C:\Program Files\Steam\steam.exe",
        ),
    ),
    (
        "OBS Studio",
        ("obs64", "obs64.exe"),
        (
            r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
        ),
    ),
    (
        "Discord",
        ("Discord.exe",),
        (
            r"%LOCALAPPDATA%\Discord\Update.exe",
        ),
    ),
    (
        "Ollama",
        ("ollama", "ollama.exe"),
        (
            r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe",
        ),
    ),
    (
        "OpenAI Codex CLI",
        ("codex", "codex.cmd"),
        (),
    ),
    (
        "Git",
        ("git", "git.exe"),
        (
            r"C:\Program Files\Git\cmd\git.exe",
        ),
    ),
    (
        "Python",
        ("python", "python.exe"),
        (),
    ),
    (
        "Node.js",
        ("node", "node.exe"),
        (
            r"C:\Program Files\nodejs\node.exe",
        ),
    ),
    (
        "Notepad (Bloc de notas)",
        ("notepad", "notepad.exe"),
        (
            r"C:\Windows\System32\notepad.exe",
        ),
    ),
    (
        "Calculadora de Windows",
        ("calc", "calc.exe"),
        (
            r"C:\Windows\System32\calc.exe",
        ),
    ),
)


class ProcessAwarenessProvider:
    """Proveedor de observación en solo lectura de procesos en ejecución."""

    # Lista cerrada de atributos seguros solicitados a psutil.
    # PROHIBIDO incluir 'cmdline' o 'environ' por política de protección de secretos.
    _SAFE_BASE_ATTRS: tuple[str, ...] = ("pid", "name", "status", "cpu_percent", "memory_info")

    def __init__(
        self,
        policy: Optional[ContextAccessPolicy] = None,
        *,
        process_iter_fn: Optional[Callable[..., Iterable[Any]]] = None,
    ) -> None:
        self.policy: ContextAccessPolicy = policy or ContextAccessPolicy()
        self._process_iter_fn = process_iter_fn or psutil.process_iter

    def collect(
        self,
        *,
        filter_name: Optional[str] = None,
        max_results: Optional[int] = None,
    ) -> ProcessAwarenessInfo:
        """Consulta los procesos activos aplicando cotas de cantidad y sanitización."""
        self.policy.assert_read_only_operation("collect_processes")
        now_iso = utc_now_iso()

        # F6.A: Cuando se solicita explícitamente un max_results (p. ej. desde ApplicationAwarenessProvider),
        # permitir hasta max(max_processes, max_applications * 4) para que el límite de procesos
        # no trunque prematuramente las aplicaciones de usuario detectables.
        upper_cap = (
            max(self.policy.max_processes, self.policy.max_applications * 4)
            if max_results is not None
            else self.policy.max_processes
        )
        limit = min(
            max(1, int(max_results)) if max_results is not None else self.policy.max_processes,
            upper_cap,
        )
        name_query = (filter_name or "").strip().lower()

        attrs = list(self._SAFE_BASE_ATTRS)
        if self.policy.include_process_user:
            attrs.append("username")
        if self.policy.include_process_exe:
            attrs.append("exe")

        items: list[ProcessContextItem] = []
        total_sampled = 0
        access_denied_count = 0
        vanished_count = 0

        try:
            iterator = self._process_iter_fn(attrs)
        except OSError:
            return ProcessAwarenessInfo(
                processes=(),
                total_sampled=0,
                returned_count=0,
                access_denied_count=0,
                vanished_count=0,
                truncated=False,
                captured_at=now_iso,
                status=AvailabilityStatus.UNAVAILABLE,
            )

        for proc in iterator:
            total_sampled += 1
            try:
                info = getattr(proc, "info", None)
                if not isinstance(info, dict):
                    info = proc.as_dict(attrs=attrs)

                raw_pid = info.get("pid")
                if raw_pid is None:
                    raw_pid = getattr(proc, "pid", 0)
                pid = int(raw_pid)

                raw_name = info.get("name") or ""
                clean_name = sanitize_untrusted_label(raw_name, max_len=80) or f"pid-{pid}"

                if name_query and name_query not in clean_name.lower():
                    continue

                raw_status = info.get("status") or "running"
                clean_status = sanitize_untrusted_label(raw_status, max_len=24) or "unknown"

                raw_cpu = info.get("cpu_percent")
                cpu_val = round(float(raw_cpu), 1) if isinstance(raw_cpu, (int, float)) else None

                mem_info = info.get("memory_info")
                mem_mb: Optional[float] = None
                if mem_info is not None:
                    rss = getattr(mem_info, "rss", None)
                    if isinstance(rss, (int, float)) and rss >= 0:
                        mem_mb = round(float(rss) / (1024**2), 2)

                username_val: Optional[str] = None
                if self.policy.include_process_user and info.get("username"):
                    # Solo conserva el nombre corto de cuenta sin dominio completo si es posible
                    raw_user = str(info["username"]).split("\\")[-1]
                    username_val = sanitize_untrusted_label(raw_user, max_len=48) or None

                exe_val: Optional[str] = None
                if self.policy.include_process_exe and info.get("exe"):
                    # Sanitiza y evita exponer argumentos (exe solo contiene ruta de binario)
                    raw_exe = str(info["exe"]).strip()
                    exe_val = sanitize_untrusted_label(Path(raw_exe).name, max_len=80) or None

                items.append(
                    ProcessContextItem(
                        pid=pid,
                        name=clean_name,
                        status=clean_status,
                        cpu_percent=cpu_val,
                        memory_mb=mem_mb,
                        username=username_val,
                        exe_path=exe_val,
                    )
                )
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                vanished_count += 1
            except (psutil.AccessDenied, PermissionError):
                access_denied_count += 1
            except (OSError, ValueError, TypeError):
                access_denied_count += 1

        # Ordenar por uso de memoria (descendente) y luego CPU (descendente) para priorizar relevancia.
        items.sort(
            key=lambda p: (
                p.memory_mb if p.memory_mb is not None else -1.0,
                p.cpu_percent if p.cpu_percent is not None else -1.0,
                -p.pid,
            ),
            reverse=True,
        )

        truncated = len(items) > limit
        bounded_items = tuple(items[:limit])

        status = AvailabilityStatus.AVAILABLE
        if not bounded_items and access_denied_count > 0:
            status = AvailabilityStatus.RESTRICTED
        elif access_denied_count > 0:
            status = AvailabilityStatus.PARTIAL

        return ProcessAwarenessInfo(
            processes=bounded_items,
            total_sampled=total_sampled,
            returned_count=len(bounded_items),
            access_denied_count=access_denied_count,
            vanished_count=vanished_count,
            truncated=truncated,
            captured_at=now_iso,
            status=status,
        )


class ApplicationAwarenessProvider:
    """Proveedor de aplicaciones instaladas, aplicaciones en ejecución y ventanas visibles."""

    def __init__(
        self,
        policy: Optional[ContextAccessPolicy] = None,
        *,
        process_provider: Optional[ProcessAwarenessProvider] = None,
        which_fn: Callable[[str], Optional[str]] = shutil.which,
        isfile_fn: Callable[[str], bool] = os.path.isfile,
        window_enumerator: Optional[
            Callable[[], tuple[Optional[ActiveWindowInfo], tuple[ActiveWindowInfo, ...], int]]
        ] = None,
    ) -> None:
        self.policy: ContextAccessPolicy = policy or ContextAccessPolicy()
        self.process_provider: ProcessAwarenessProvider = (
            process_provider or ProcessAwarenessProvider(self.policy)
        )
        self._which_fn = which_fn
        self._isfile_fn = isfile_fn
        self._window_enumerator = window_enumerator

    def collect(
        self,
        *,
        include_windows: bool = True,
        target_app: Optional[str] = None,
    ) -> ApplicationAwarenessInfo:
        """Diferencia aplicaciones instaladas, aplicaciones en ejecución y ventanas activas.

        Args:
            include_windows: Si es False, omite desde el origen la enumeración de títulos de
                ventanas de escritorio (minimización F4 cuando solo se requiere estado de apps).
            target_app: Si se especifica, filtra los resultados únicamente a la aplicación
                solicitada evitando recolectar o exponer aplicaciones ajenas al objetivo.
        """
        self.policy.assert_read_only_operation("collect_applications")
        now_iso = utc_now_iso()
        notes: list[str] = []
        target_low = (target_app or "").strip().lower()

        # 1. Consultar mapa de procesos activos para cruzar con aplicaciones y ventanas (F6.A)
        proc_snapshot = self.process_provider.collect(
            max_results=max(self.policy.max_processes, self.policy.max_applications * 4)
        )
        pid_to_name: dict[int, str] = {p.pid: p.name for p in proc_snapshot.processes}
        running_by_name: dict[str, list[int]] = {}
        for p in proc_snapshot.processes:
            low_name = p.name.lower()
            if low_name in WINDOWS_BACKGROUND_SYSTEM_PROCESSES or p.pid in (0, 4):
                continue
            running_by_name.setdefault(p.name, []).append(p.pid)

        # 2. Consultar ventanas activas/visibles de forma segura solo cuando se requiera (F4)
        if not include_windows:
            fg_win, vis_wins, win_unavail = None, (), 0
        elif self._window_enumerator is not None:
            fg_win, vis_wins, win_unavail = self._window_enumerator()
        else:
            fg_win, vis_wins, win_unavail = self._enumerate_windows_safe(pid_to_name)

        if target_low and vis_wins:
            vis_wins = tuple(
                w
                for w in vis_wins
                if target_low in w.title.lower()
                or (w.process_name and target_low in w.process_name.lower())
            )
            if fg_win is not None and not (
                target_low in fg_win.title.lower()
                or (fg_win.process_name and target_low in fg_win.process_name.lower())
            ):
                fg_win = None

        bounded_windows = vis_wins[: self.policy.max_windows]

        # 3. Construir lista de aplicaciones en ejecución (excluyendo servicios del sistema)
        running_apps: list[ApplicationItem] = []
        for exe_name, pids in sorted(running_apps_items := list(running_by_name.items()), key=lambda kv: kv[0].lower()):
            display = exe_name[:-4] if exe_name.lower().endswith(".exe") else exe_name
            if target_low and target_low not in display.lower() and target_low not in exe_name.lower():
                continue
            if len(running_apps) >= self.policy.max_applications:
                break
            running_apps.append(
                ApplicationItem(
                    name=sanitize_untrusted_label(display, max_len=80),
                    app_kind="running",
                    is_running=True,
                    is_installed=True,
                    executable_name=sanitize_untrusted_label(exe_name, max_len=80),
                    pids=tuple(pids[:10]),
                    source="process_table",
                )
            )
        del running_apps_items

        # 4. Detectar aplicaciones instaladas del catálogo seguro sin escanear registros privados
        installed_apps = self._detect_installed_catalog(running_by_name)
        if target_low and installed_apps:
            installed_apps = tuple(
                a
                for a in installed_apps
                if target_low in a.name.lower()
                or (a.executable_name and target_low in a.executable_name.lower())
            )

        unavailable_total = proc_snapshot.access_denied_count + win_unavail
        if unavailable_total > 0:
            notes.append(
                f"{unavailable_total} elemento(s) del sistema no pudieron inspeccionarse por restricciones de permisos."
            )

        status = AvailabilityStatus.AVAILABLE
        if not running_apps and not installed_apps and not bounded_windows:
            status = AvailabilityStatus.UNAVAILABLE
        elif win_unavail > 0 or proc_snapshot.status != AvailabilityStatus.AVAILABLE:
            status = AvailabilityStatus.PARTIAL

        return ApplicationAwarenessInfo(
            foreground_window=fg_win,
            visible_windows=bounded_windows,
            running_applications=tuple(running_apps),
            installed_applications=installed_apps,
            unavailable_count=unavailable_total,
            notes=tuple(notes),
            captured_at=now_iso,
            status=status,
        )

    def _detect_installed_catalog(
        self,
        running_by_name: dict[str, list[int]],
    ) -> tuple[ApplicationItem, ...]:
        """Comprueba disponibilidad de aplicaciones conocidas mediante `which` y rutas públicas."""
        lower_running_map: dict[str, list[int]] = {
            k.lower(): v for k, v in running_by_name.items()
        }
        discovered: list[ApplicationItem] = []

        for display_name, commands, known_paths in KNOWN_APPLICATION_CATALOG:
            if len(discovered) >= self.policy.max_applications:
                break

            found_exe: Optional[str] = None
            detection_source = "catalog"

            for cmd in commands:
                resolved = self._which_fn(cmd)
                if resolved:
                    found_exe = Path(resolved).name
                    detection_source = "path_lookup"
                    break

            if found_exe is None:
                for raw_path in known_paths:
                    expanded = os.path.expandvars(raw_path)
                    if "%" not in expanded and self._isfile_fn(expanded):
                        found_exe = Path(expanded).name
                        detection_source = "known_public_path"
                        break

            # Verificar si está actualmente en ejecución
            matched_pids: list[int] = []
            for cmd in commands:
                cmd_low = cmd.lower()
                if not cmd_low.endswith(".exe"):
                    cmd_exe = f"{cmd_low}.exe"
                else:
                    cmd_exe = cmd_low
                if cmd_low in lower_running_map:
                    matched_pids.extend(lower_running_map[cmd_low])
                elif cmd_exe in lower_running_map:
                    matched_pids.extend(lower_running_map[cmd_exe])

            if found_exe is None and matched_pids:
                found_exe = commands[0]
                detection_source = "running_process"

            if found_exe is not None:
                unique_pids = tuple(dict.fromkeys(matched_pids))
                discovered.append(
                    ApplicationItem(
                        name=sanitize_untrusted_label(display_name, max_len=80),
                        app_kind="installed",
                        is_running=bool(unique_pids),
                        is_installed=True,
                        executable_name=sanitize_untrusted_label(found_exe, max_len=80),
                        pids=unique_pids[:10],
                        source=detection_source,
                    )
                )

        return tuple(discovered)

    def _enumerate_windows_safe(
        self,
        pid_to_name: dict[int, str],
    ) -> tuple[Optional[ActiveWindowInfo], tuple[ActiveWindowInfo, ...], int]:
        """Enumera ventanas visibles en Windows mediante `user32.dll` en estricto modo lectura."""
        if sys.platform != "win32" or not hasattr(ctypes, "windll"):
            return None, (), 0

        try:
            user32 = ctypes.windll.user32
            fg_hwnd = int(user32.GetForegroundWindow() or 0)
        except Exception:
            return None, (), 1

        visible_list: list[ActiveWindowInfo] = []
        foreground_info: Optional[ActiveWindowInfo] = None
        unavailable_count = 0

        def _read_window_info(hwnd: int, is_fg: bool) -> Optional[ActiveWindowInfo]:
            nonlocal unavailable_count
            try:
                length = int(user32.GetWindowTextLengthW(hwnd))
                if length <= 0:
                    return None
                buf = ctypes.create_unicode_buffer(min(length + 1, 260))
                user32.GetWindowTextW(hwnd, buf, len(buf))
                raw_title = buf.value.strip()
                if not raw_title:
                    return None

                pid_dword = ctypes.c_ulong(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_dword))
                win_pid = int(pid_dword.value) if pid_dword.value else None
                proc_name = pid_to_name.get(win_pid) if win_pid is not None else None
                if proc_name is None and win_pid is not None:
                    try:
                        proc_name = sanitize_untrusted_label(psutil.Process(win_pid).name(), max_len=64)
                    except (psutil.Error, OSError):
                        proc_name = None

                clean_title = sanitize_untrusted_label(raw_title, max_len=120)
                if not clean_title:
                    return None

                return ActiveWindowInfo(
                    hwnd=int(hwnd),
                    title=clean_title,
                    pid=win_pid,
                    process_name=proc_name,
                    is_foreground=is_fg,
                )
            except Exception:
                unavailable_count += 1
                return None

        if fg_hwnd:
            foreground_info = _read_window_info(fg_hwnd, True)

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def _enum_cb(hwnd_ptr: int, _lparam: int) -> bool:
            if len(visible_list) >= self.policy.max_windows:
                return False
            hwnd = int(hwnd_ptr or 0)
            if not hwnd:
                return True
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
            except Exception:
                return True

            info = _read_window_info(hwnd, hwnd == fg_hwnd)
            if info is not None:
                visible_list.append(info)
            return True

        try:
            cb_func = WNDENUMPROC(_enum_cb)
            user32.EnumWindows(cb_func, 0)
        except Exception:
            unavailable_count += 1

        if foreground_info is None and visible_list:
            for w in visible_list:
                if w.is_foreground:
                    foreground_info = w
                    break

        return foreground_info, tuple(visible_list), unavailable_count
