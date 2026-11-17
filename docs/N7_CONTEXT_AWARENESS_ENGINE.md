# PROYECTO J.A.R.V.I.S. — N7: CONTEXT AWARENESS ENGINE

## 1. Propósito y Posición en el Roadmap

La fase **N7 — Context Awareness Engine** incorpora en J.A.R.V.I.S. una capa de observación contextual modular, tipada y estrictamente de **solo lectura** sobre el entorno Windows local. Funciona con independencia total de los proveedores de IA (Ollama, Gemini, Claude, GPT y Codex) y suministra evidencias estructuradas del sistema al motor autónomo de objetivos **N6 (`GoalEngine`)** y a la consola interactiva (`/contexto`).

**Principio rector:**
> **N7 observa. N6 interpreta y planifica. Los mecanismos autorizados existentes (`ChainOrchestrator` + `PolicyEngine` + aprobación humana) ejecutan.**

N7 **no es** ni puede actuar como:
- Un motor de ejecución de comandos ni un administrador autónomo del sistema operativo.
- Un sustituto de `ChainOrchestrator` ni de `PolicyEngine`.
- Un mecanismo para crear, editar o eliminar archivos.
- Una vía para detener procesos, cambiar prioridades o elevar privilegios.
- Un mecanismo para eludir aprobaciones humanas en operaciones sensibles.

---

## 2. Arquitectura Modular (`src/context/`)

```mermaid
flowchart TD
    U[Usuario Consola / Voz] --> CC[ContextConsoleHandler<br/>src/context/console.py]
    U --> GE[N6 GoalEngine<br/>src/brain/goal_engine.py]
    CC --> CAE[N7 ContextAwarenessEngine<br/>src/context/engine.py]
    GE -->|Consulta selectiva bajo demanda| CAE
    CAE --> POL[ContextAccessPolicy<br/>src/context/policy.py]
    CAE --> SHP[SystemInfoProvider<br/>HardwareInfoProvider<br/>StorageInfoProvider<br/>ResourceMonitoringProvider<br/>src/context/system_hardware_provider.py]
    CAE --> PAP[ProcessAwarenessProvider<br/>ApplicationAwarenessProvider<br/>src/context/process_app_provider.py]
    CAE --> FSP[FileSystemContextProvider<br/>src/context/filesystem_provider.py]
    CAE -->|ContextSnapshot + ContextUntrustedEnvelope| GE
    GE --> GP[GoalPlanningEngine / ExecutionSupervisor]
    GP -->|SENSITIVE_OPERATION| CO[N5 ChainOrchestrator + PolicyEngine]
```

### Módulos implementados

| Módulo | Responsabilidad | Restricción de Seguridad |
|---|---|---|
| [src/context/models.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/context/models.py) | Modelos de dominio congelados (`frozen=True`): `ContextCategory`, `AvailabilityStatus`, `MetricReading`, `SystemContextInfo`, `GpuDeviceInfo`, `StorageVolumeInfo`, `HardwareContextInfo`, `ResourceUsageInfo`, `ProcessContextItem`, `ProcessAwarenessInfo`, `ActiveWindowInfo`, `ApplicationItem`, `ApplicationAwarenessInfo`, `FileMetadataItem`, `DirectorySnapshot`, `FileSystemContextInfo`, `ProviderIssue`, `ContextUntrustedEnvelope`, `ContextSnapshot` y excepciones jerárquicas (`ContextError`, `ContextPolicyError`, `ContextAccessDeniedError`, `ContextProviderError`, `ContextTimeoutError`). | Sin IO, sin llamadas al SO ni a modelos LLM; prohíbe `value=0.0` cuando `status` es `UNAVAILABLE`. |
| [src/context/policy.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/context/policy.py) | `ContextAccessPolicy`: control de categorías habilitadas, validación defensiva de rutas en raíces autorizadas (`AUTHORIZED_FILE_ROOTS`), bloqueo de `FORBIDDEN_SYSTEM_PREFIXES`, traversal `..`, rutas relativas, UNC, variables `%...%` sin expandir, dispositivos reservados (`CON`, `NUL`, `COM1..9`, `LPT1..9`), symlinks/junctions NTFS (`FILE_ATTRIBUTE_REPARSE_POINT`) y archivos sensibles (`.env`, `prompts_history.txt`, `memory_store.json`, claves), redacción de secretos (`redact_secrets`) y sanitización anti-inyección (`sanitize_untrusted_label`). | Denegación por defecto (`default-deny`). |
| [src/context/system_hardware_provider.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/context/system_hardware_provider.py) | `SystemInfoProvider`, `StorageInfoProvider`, `HardwareInfoProvider` y `ResourceMonitoringProvider`. Detección dinámica de CPU, núcleos físicos/lógicos, RAM, unidades y GPU/VRAM con cascada tolerante a fallos (`nvidia-smi` sin shell -> `torch.cuda` ya cargado -> descriptor `winreg.KEY_READ` -> `UNAVAILABLE`). | Solo lectura; sin bucles en segundo plano ni importación en frío de `torch`. |
| [src/context/process_app_provider.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/context/process_app_provider.py) | `ProcessAwarenessProvider` y `ApplicationAwarenessProvider`. Enumeración acotada de procesos (`psutil.process_iter`), ventanas visibles en primer/segundo plano vía `user32.dll` en solo lectura y separación entre aplicaciones instaladas, procesos en ejecución y ventanas activas. | Jamás consulta `cmdline()` ni `environ()`; nunca invoca `kill`, `terminate` ni `nice`. |
| [src/context/filesystem_provider.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/context/filesystem_provider.py) | `FileSystemContextProvider`: consulta de existencia, metadatos básicos, tipos/extensiones, estructura acotada de directorios y búsqueda de archivos relevantes dentro de raíces autorizadas. | Cero métodos de escritura, creación, edición o borrado. |
| [src/context/engine.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/context/engine.py) | `ContextAwarenessEngine`: selección de categorías bajo demanda, caché con TTL configurable, timeouts por proveedor, aislamiento de errores parciales, inferencia de categorías relevantes para N6 (`infer_relevant_categories_for_goal`) y empaquetado de datos no confiables (`build_untrusted_envelope`, `build_n6_context_payload`). | No bloquea N6 ante fallos ni ejecuta operaciones mutantes. |
| [src/context/console.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/context/console.py) | `ContextConsoleHandler`: manejo de `/contexto` y subcomandos en consola de texto. | Sin importación de componentes de audio (`faster_whisper`, `sounddevice`, `stt`, `tts`, `wake_word`). |

---

## 3. Capacidades, Permisos y Límites de Observación

### 3.1 Qué información puede obtener N7 (`ContextCategory`)

1. **`system` (`sistema`):**
   - Sistema operativo (`os_name`, `os_release`, `os_version`, `os_build`), arquitectura (`AMD64`), versión de Python, tiempo de actividad (`uptime_seconds`) y modo del entorno de J.A.R.V.I.S. (`production` / `test`).
2. **`hardware` (`hardware`):**
   - Modelo real de CPU, núcleos físicos y lógicos, frecuencia en MHz, memoria RAM total y disponible en GB, unidades de almacenamiento y GPUs detectadas dinámicamente (nombre, fabricante, VRAM total/usada/libre, porcentaje de utilización y versión de driver cuando la fuente lo expone).
3. **`resources` (`recursos`):**
   - Métricas puntuales bajo demanda (`MetricReading` con `value`, `unit`, `captured_at`, `status`, `source`) para uso de CPU (%), RAM usada/disponible/porcentaje, utilización de GPU (%), VRAM usada/libre (MB) y espacio libre en el disco de trabajo (GB).
4. **`storage` (`almacenamiento`):**
   - Unidades y particiones montadas (`device`, `mountpoint`, `filesystem`, `total_gb`, `used_gb`, `free_gb`, `percent_used`, `status`).
5. **`processes` (`procesos`):**
   - Procesos activos acotados por `CONTEXT_MAX_PROCESSES` y ordenados por relevancia de consumo (`pid`, `name` sanitizado, `status`, `cpu_percent`, `memory_mb`), contabilizando procesos inaccesibles (`access_denied_count`) o efímeros (`vanished_count`).
6. **`applications` (`aplicaciones`):**
   - Diferencia explícitamente:
     - `foreground_window`: ventana activa en primer plano (`hwnd`, `title`, `pid`, `process_name`).
     - `visible_windows`: ventanas visibles de nivel superior acotadas por `CONTEXT_MAX_WINDOWS`.
     - `running_applications`: aplicaciones de usuario con procesos activos (excluyendo procesos internos de Windows como `svchost.exe`, `dwm.exe`, `csrss.exe`, `System`).
     - `installed_applications`: disponibilidad verificada sobre el catálogo seguro de aplicaciones comunes (`which` y rutas públicas estándar) indicando si además se encuentran en ejecución.
7. **`filesystem` (`archivos`):**
   - Existencia, categoría (`config`, `code`, `document`, `data`, `directory`, `other`), extensión, tamaño en bytes y fecha de modificación de archivos o directorios ubicados estrictamente dentro de `AUTHORIZED_FILE_ROOTS`.

### 3.2 Qué información NO puede obtener N7 (por diseño de seguridad y privacidad)

- Argumentos de línea de comandos (`cmdline`) ni variables de entorno (`environ`) de ningún proceso.
- Contenido ni metadatos de archivos sensibles (`.env`, `.env.*`, `prompts_history.txt`, `memory_store.json`, `*.pem`, `*.key`, `*.pfx`, `credentials.json`, `id_rsa`).
- Archivos o directorios fuera de `AUTHORIZED_FILE_ROOTS` o dentro de `FORBIDDEN_SYSTEM_PREFIXES` (`C:\Windows`, `System32`, `SysWOW64`, `WindowsApps`, etc.).
- Historiales de navegación, cookies, sesiones de usuario, contraseñas ni claves privadas.
- Nombre de host del equipo (`hostname`), usuario propietario de procesos (`username`) o rutas de ejecutables (`exe_path`) por defecto (deshabilitados salvo activación explícita en configuración).

### 3.3 Permisos necesarios

- N7 opera íntegramente con los permisos de usuario estándar del proceso actual de Python.
- **No solicita ni intenta elevación administrativa (UAC).** Cuando el sistema operativo restringe el acceso a un proceso o unidad (`PermissionError` / `psutil.AccessDenied`), N7 registra el conteo o marca `AvailabilityStatus.RESTRICTED` y continúa normalmente.

---

## 4. Políticas de Acceso, Raíces Autorizadas y Datos Enviados a IA

### 4.1 Autorización de raíces de archivos

`ContextAccessPolicy` reutiliza `AUTHORIZED_FILE_ROOTS` y `FORBIDDEN_SYSTEM_PREFIXES` definidos en [config/settings.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/config/settings.py):
- Raíces predeterminadas: directorio del proyecto (`BASE_DIR`), `%USERPROFILE%\Documents`, `%USERPROFILE%\Saved Games`, `%LOCALAPPDATA%` y `%APPDATA%` (más raíces adicionales válidas en `JARVIS_EXTRA_AUTHORIZED_ROOTS`).
- Toda consulta a `FileSystemContextProvider` valida previamente la ruta mediante `policy.validate_read_path()`, rechazando rutas relativas, segmentos `..`, rutas UNC (`\\server\share`), variables `%...%` sin expandir, dispositivos reservados de Windows y enlaces simbólicos o junctions NTFS (`FILE_ATTRIBUTE_REPARSE_POINT`).

### 4.2 Minimización y aislamiento para IA (`ContextUntrustedEnvelope`)

- Todo dato observado del sistema se empaqueta con `trust_level="untrusted_system_context"`, `allow_tool_execution_from_content=False`, `allow_system_prompt_promotion=False` y `allow_direct_mutation=False`.
- Con `CONTEXT_MINIMIZE_FOR_LLM=True` (valor predeterminado), el sobre enviado a N6/IA incluye únicamente el subconjunto resumido de las categorías solicitadas, acotando listas y aplicando `redact_secrets` y `sanitize_untrusted_label` para neutralizar posibles intentos de prompt injection en nombres de procesos, ventanas o archivos.

### 4.3 Comportamiento ante fallos parciales

- Cada proveedor se ejecuta bajo un timeout independiente (`CONTEXT_PROVIDER_TIMEOUT_SEC`, por defecto `3.0` s).
- Si una categoría falla o agota su tiempo de espera:
  1. Se registra un `ProviderIssue` en `snapshot.partial_errors`.
  2. La categoría o métrica afectada se incluye en `snapshot.unavailable_fields` con valor `None` (nunca `0.0`).
  3. Las demás categorías solicitadas se entregan intactas.
  4. En N6, los campos no disponibles se agregan a `goal.missing_info` sin bloquear la planificación ni inventar datos.

---

## 5. Configuración (`config/settings.py`)

| Variable de entorno | Constante en `settings.py` | Valor por defecto | Descripción |
|---|---|---|---|
| `JARVIS_CONTEXT_ENABLED` | `CONTEXT_ENGINE_ENABLED` | `True` (`1`) | Habilita o deshabilita globalmente el motor N7 |
| `JARVIS_CONTEXT_CATEGORIES` | `CONTEXT_ENABLED_CATEGORIES` | Todas las 7 categorías | Lista separada por comas de categorías permitidas |
| `JARVIS_CONTEXT_MAX_PROCESSES` | `CONTEXT_MAX_PROCESSES` | `25` | Cantidad máxima de procesos devueltos por consulta |
| `JARVIS_CONTEXT_MAX_APPLICATIONS` | `CONTEXT_MAX_APPLICATIONS` | `40` | Cantidad máxima de aplicaciones listadas |
| `JARVIS_CONTEXT_MAX_WINDOWS` | `CONTEXT_MAX_WINDOWS` | `25` | Cantidad máxima de ventanas visibles inspeccionadas |
| `JARVIS_CONTEXT_MAX_DIR_DEPTH` | `CONTEXT_MAX_DIR_DEPTH` | `3` | Profundidad máxima al explorar un directorio autorizado |
| `JARVIS_CONTEXT_MAX_DIR_ENTRIES` | `CONTEXT_MAX_DIR_ENTRIES` | `150` | Cantidad máxima de entradas listadas en un directorio |
| `JARVIS_CONTEXT_CACHE_TTL_SEC` | `CONTEXT_CACHE_TTL_SEC` | `5.0` | Tiempo de vida (segundos) de la caché bajo demanda |
| `JARVIS_CONTEXT_PROVIDER_TIMEOUT_SEC` | `CONTEXT_PROVIDER_TIMEOUT_SEC` | `3.0` | Timeout máximo (segundos) por proveedor individual |
| `JARVIS_CONTEXT_MINIMIZE_FOR_LLM` | `CONTEXT_MINIMIZE_FOR_LLM` | `True` (`1`) | Activa minimización de datos en sobres para N6/IA |
| `JARVIS_CONTEXT_INCLUDE_HOSTNAME` | `CONTEXT_INCLUDE_HOSTNAME` | `False` (`0`) | Incluye o anonimiza el nombre del equipo (`hostname`) |
| `JARVIS_CONTEXT_INCLUDE_PROCESS_USER` | `CONTEXT_INCLUDE_PROCESS_USER` | `False` (`0`) | Permite o bloquea incluir el usuario propietario del proceso |
| `JARVIS_CONTEXT_INCLUDE_PROCESS_EXE` | `CONTEXT_INCLUDE_PROCESS_EXE` | `False` (`0`) | Permite o bloquea incluir el nombre del ejecutable del proceso |

---

## 6. Comandos de Consola y Ejemplos Reales de Uso

En el modo consola (`.\venv\Scripts\python.exe main.py`):

- `/contexto` — Muestra un resumen consolidado de sistema, hardware, recursos y almacenamiento.
- `/contexto sistema` — Consulta sistema operativo, versión, build, arquitectura y tiempo activo.
- `/contexto hardware` — Consulta procesador, núcleos físicos/lógicos, RAM total/libre y GPU/VRAM.
- `/contexto recursos` — Consulta uso actual de CPU, RAM, GPU/VRAM y espacio libre en disco.
- `/contexto almacenamiento` — Lista las unidades montadas y su espacio total/libre.
- `/contexto procesos [filtro]` — Lista los procesos en ejecución ordenados por consumo de memoria/CPU (opcionalmente filtrados por nombre, ej. `/contexto procesos python`).
- `/contexto aplicaciones` — Muestra la ventana activa, ventanas visibles, aplicaciones en ejecución y aplicaciones instaladas verificadas.
- `/contexto archivos <ruta_absoluta>` — Consulta metadatos de un archivo o el contenido acotado de un directorio dentro de las raíces autorizadas.
- `/contexto ayuda` — Muestra la guía rápida de subcomandos.

### Ejemplo de uso desde Python e integración con N6

```python
from src.context import ContextAwarenessEngine
from src.brain.goal_engine import GoalEngine

ctx_engine = ContextAwarenessEngine()
snapshot = ctx_engine.get_context(categories=["hardware", "resources", "processes"])

goal_engine = GoalEngine(context_engine=ctx_engine)
submission = goal_engine.submit_goal(
    "Jarvis, analizá si mi PC puede ejecutar determinado programa y qué limitaciones tendría"
)
```

---

## 7. Ejecución de Pruebas y Limitaciones Conocidas en Windows

### Comandos de verificación

```powershell
# Suite específica de N7 (funcional, seguridad, consola e integración N6+N7):
$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest tests.test_context_awareness_n7 -v

# Suite específica de N6 (regresión de objetivos y planificación):
$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest tests.test_goal_engine -v

# Suite global completa del repositorio:
$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

### Limitaciones conocidas en Windows

1. **Telemetría dinámica de GPU no-NVIDIA:** Cuando `nvidia-smi` no está disponible (por ejemplo, en GPUs integradas o AMD/Intel), `HardwareInfoProvider` identifica el modelo y la VRAM estática mediante el descriptor de clase de pantalla de Windows (`winreg.KEY_READ`), pero las métricas de uso en vivo (`gpu_utilization_percent`, `vram_used_mb`, `vram_free_mb`) se reportan como `AvailabilityStatus.UNAVAILABLE` (`value=None`) sin asumir valores cero.
2. **Procesos protegidos del sistema:** En ejecución sin privilegios administrativos, Windows restringe el acceso a ciertos procesos de servicio (`psutil.AccessDenied`), los cuales se contabilizan de forma transparente en `access_denied_count`.
3. **Sesiones sin escritorio interactivo (servicios/CI headless):** En entornos sin estación de ventanas interactiva, `EnumWindows` / `GetForegroundWindow` puede devolver cero ventanas visibles sin provocar errores.
