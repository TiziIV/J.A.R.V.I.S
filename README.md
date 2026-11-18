# J.A.R.V.I.S. (Just A Rather Very Intelligent System)

> Asistente personal autónomo, modular, privado y *local-first* para Windows, diseñado para operar con fluidez en hardware de consumo (NVIDIA GeForce GTX 1660 de 6 GB VRAM) combinando arquitectura **Multi-Brain**, gobernanza transaccional (**N5**), planificación autónoma supervisada (**N6**), conciencia contextual del sistema en solo lectura (**N7**) e interacción opcional por voz acelerada en GPU.

---

## 📌 1. Descripción General y Arquitectura Multi-Brain

**J.A.R.V.I.S.** es un asistente local desarrollado en Python para Windows que prioriza la soberanía de datos, la ejecución determinista bajo políticas de seguridad verificables y el uso eficiente de recursos locales.

### Arquitectura Multi-Brain (`src/brain/brain_manager.py`)
El núcleo cognitivo coordina múltiples motores locales y en la nube bajo una identidad común definida en [config/prompts.py](config/prompts.py):
- **Motores Locales Primarios:** `OLLAMA` (modelo estructurado principal) y `OLLAMA_UNCENSORED` (modelo local secundario), ejecutándose localmente con bajo consumo de VRAM.
- **Motores Cloud Opcionales / Respaldo:** `GEMINI` (`gemini-2.0-flash` con fallback a `gemini-1.5-flash`), `CLAUDE`, `GPT` y `CODEX` (`codex exec` en sandbox de solo lectura).
- **Enrutamiento y Deliberación (`AUTO`):** Clasificación automática de complejidad, deliberación multi-agente (`AutoDeliberator`) y políticas de degradación controlada cuando una clave API opcional no está configurada o el servicio local no responde.

---

## 📊 2. Estado Actual del Proyecto

| Fase / Capa | Estado | Resumen de implementación y validación |
| :--- | :--- | :--- |
| **Core Voz, VAD, TTS y System Tray** (`v3.0.0`) | **Completada** | Wake Word en CPU (`openwakeword`), captura mono 16 kHz con VAD adaptativo, STT GPU (`faster-whisper`), TTS (`edge-tts` + Circuit Breaker a `System.Speech`) y bandeja del sistema (`pystray`). |
| **Multi-Brain, Exportación Documental y Extracción Web** (`v5.1.0`) | **Completada** | `BrainManager`, `DocumentExportSkill` (con validación anti-reparse/TOCTOU) y `WebExtractionSkill` (defensa SSRF, DNS rebinding y sobre de aislamiento de contenido externo). |
| **N5 — Gobernanza Transaccional y Ejecución Segura** (`v5.2.0`) | **Cerrada y Auditada** | `TransactionManager` WAL-First encadenado por SHA-256, `PolicyEngine`, `ChainOrchestrator`, `FileEditorSkill`, tokens de aprobación interactiva de un solo uso y aislamiento determinista de memoria (`JARVIS_ENV=test`). Documentado en [docs/audits/INDEPENDENT_SECURITY_AUDIT_N5_FINAL.md](docs/audits/INDEPENDENT_SECURITY_AUDIT_N5_FINAL.md). |
| **N6 — Autonomous Goal & Planning Engine** | **Cerrada y Verificada** | Comprensión de objetivos (`GoalUnderstandingEngine`), descomposición y validación DAG (`GoalPlanningEngine`), selector de riesgos (`GoalSkillSelector`), supervisión con timeouts/reintentos/replanificación (`ExecutionSupervisor`) y persistencia aislada (`GoalStore`). Documentado en [docs/N6_AUTONOMOUS_GOAL_ENGINE.md](docs/N6_AUTONOMOUS_GOAL_ENGINE.md). |
| **N7 — Context Awareness Engine** | **Remediada y en verificación final** | Observación estructurada y estrictamente de **solo lectura** del entorno Windows, integrada opcionalmente con N6; remediaciones M1/M2 y B1–B4 en validación, documentadas en [docs/N7_CONTEXT_AWARENESS_ENGINE.md](docs/N7_CONTEXT_AWARENESS_ENGINE.md). |
| **N8.1 — Persistencia Base de Memoria a Largo Plazo** | **Implementada y Verificada** | Almacén SQLite independiente (`data/experiences.db`, modo WAL + `synchronous=FULL` + `secure_delete=ON`), FTS5 con respaldo SQL `LIKE`, políticas estrictas (`INFERENCE` bloqueada, `sanitize_memory_text()`, validación `EXECUTION_VERIFIED`, rechazo de secretos y modo privado) y protocolo `ExperienceStoreLock` (C4) en `src/memory/`. Documentado en [docs/N8_LONG_TERM_MEMORY.md](docs/N8_LONG_TERM_MEMORY.md); N8.2 y N8.3 siguen reservadas. |

---

## 🏗️ 3. Arquitectura: Observación (N7), Planificación (N6) y Ejecución (N5)

El diseño de J.A.R.V.I.S. impone una separación estricta de responsabilidades entre observar el entorno, planificar objetivos y ejecutar modificaciones:

```text
[ N7: Context Awareness Engine (src/context/) ]
   └── Solo lectura estricta: observa SO, CPU/RAM/GPU, discos, procesos, apps y metadatos de archivos autorizados.
   └── Empaqueta datos en ContextUntrustedEnvelope (trust_level="untrusted_system_context").
          │
          ▼
[ N6: Autonomous Goal & Planning Engine (src/brain/goal_*.py) ]
   └── Interpreta objetivos (/goal), descompone subtareas en un DAG acotado y clasifica riesgos:
       • SAFE_PLANNING -> Síntesis cognitiva interna sin efectos secundarios.
       • LOW_RISK_EXECUTION -> Skills de consulta autorizadas.
       • SENSITIVE_OPERATION -> Requiere aprobación humana explícita y delega en N5.
          │
          ▼
[ N5: PolicyEngine + ChainOrchestrator + FileEditorSkill (src/brain/chain_orchestrator.py) ]
   └── Valida raíces autorizadas, genera backups verificados por SHA-256, registra bitácora WAL
       y aplica o revierte cambios transaccionales únicamente tras confirmación humana.
```

### Frontera de datos no confiables
- **Contenido web (`WebExtractionSkill`):** marcado como `untrusted_external_content`; aislado en memoria efímera sin promoción a instrucciones del sistema ni disparo de herramientas.
- **Contexto del sistema (`N7 ContextAwarenessEngine`):** marcado como `untrusted_system_context` con `allow_tool_execution_from_content=False`, `allow_system_prompt_promotion=False` y `allow_direct_mutation=False`.

---

## 🔒 4. Seguridad, Privacidad y Límites Reales

1. **Naturaleza de solo lectura en N7:**
   - Los módulos de `src/context/` carecen de funciones de escritura, creación, edición o borrado de archivos, terminación de procesos (`kill`/`terminate`), modificación del Registro (`winreg` se usa solo con `KEY_READ`) o ejecución con `shell=True`.
2. **Raíces autorizadas y exclusiones específicas de N7 (F3):**
   - `ContextAccessPolicy` restringe toda consulta de archivos a `AUTHORIZED_FILE_ROOTS` y bloquea `FORBIDDEN_SYSTEM_PREFIXES` (`C:\Windows`, `System32`, `SysWOW64`, `WindowsApps`, etc.).
   - Sin alterar las raíces compartidas que `FileEditorSkill` requiere en N5, N7 excluye adicionalmente subárboles y archivos sensibles tanto en inspección puntual (`inspect_path`) como en recorridos (`list_directory`, `find_relevant_files`): `BASE_DIR/data` completo (incluyendo `data/goals`, `data/transactions`, `data/backups`, `data/other_private.json`, `memory_store.json`), `.git`, `.ssh`, `id_*.pub`, `.aws`, `.gnupg`, `.kube`, `file_editor_audit.jsonl`, `*.log`, `.npmrc`, `.netrc`, `.envrc`, `.htpasswd`, `secrets.json`, `.env*`, claves privadas (`*.pem`, `*.key`, `*.pfx`, `*.ppk`, `*.kdbx`), bases de datos locales (`*.sqlite`, `*.sqlite3`, `*.db`, `wallet.db`, `wallet.dat`, `wallet.json`) y almacenes de credenciales/perfiles completos de navegadores (`Credentials`, `User Data`, `Opera Software`, `Opera GX Stable`, `Google/Chrome`, `Mozilla/Firefox`, `Microsoft/Edge`).
3. **Protección contra Symlinks, Junctions NTFS, Reparse Points y TOCTOU (F2, F5):**
   - Validación de cada segmento y sus ancestros mediante `os.path.lexists`, `os.path.isjunction` y `os.lstat` verificando `stat.S_ISLNK` y el atributo nativo `FILE_ATTRIBUTE_REPARSE_POINT` (incluyendo junctions rotos cuyo destino no existe).
   - Los errores de permisos (`PermissionError` / `OSError`) se capturan y convierten en `ContextAccessDeniedError` sin interrumpir el bucle principal de consola.
   - `inspect_path` y `list_directory` ejecutan `os.lstat` / `entry.stat(follow_symlinks=False)` directamente sin depender de `Path.exists()` previo, volviendo a verificar `FILE_ATTRIBUTE_REPARSE_POINT` sobre el `stat_result` para reducir ventanas TOCTOU.
4. **Protección contra NTFS Alternate Data Streams (ADS) y sufijos encadenados (F1):**
   - Rechazo conservador de cualquier sintaxis `:` posterior al identificador de unidad de Windows (p. ej. `.env:secretstream`, `credentials.json:backup`, `config.ini:stream`).
   - Normalización iterativa de sufijos temporales y de respaldo encadenados (`.bak`, `.tmp`, `.backup`, `.old`, `.orig`, `.swp`, marcas temporales N5 `.YYYYMMDD_HHMMSS_ffffff`), bloqueando variantes como `credentials.json.bak.tmp` o `credentials.json.tmp.bak`.
5. **Minimización de datos y ventanas desactivadas por defecto (F4.A):**
   - N7 jamás consulta `cmdline` ni `environ` de procesos (`_SAFE_BASE_ATTRS` cerrado), y deshabilita por defecto `hostname`, `username` y `exe_path`.
   - `ContextAwarenessEngine.get_context()` y `get_context_safe()` utilizan `include_windows=False` como comportamiento predeterminado: ni `/contexto` general, ni `get_context()` sin argumentos, ni las consultas automáticas de N6 invocan el enumerador de ventanas de escritorio. La enumeración de ventanas solo se activa mediante opt-in explícito (`include_windows=True`, como en `/contexto aplicaciones`).
   - Los archivos persistidos `.goal.json` y `.wal.jsonl` excluyen `visible_windows`, `foreground_window` y `top_processes`.
6. **Redacción estructural de secretos y aislamiento de datos no confiables (F4.B, F4.C):**
   - `redact_secrets` procesa estructuras anidadas antes de serializarlas; cubre credenciales Basic cortas, claves Authorization compuestas, JSON con comillas escapadas y estructuras cíclicas, sin falsos positivos sobre prosa común como `"password: incorrect"`.
   - `sanitize_untrusted_label` es defensa complementaria (normalización Unicode, caracteres invisibles, tildes y homoglifos conocidos). La síntesis N6 transporta las observaciones de N7 en JSON con procedencia y delimitadores escapados; no las agrega como instrucciones. La ejecución continúa sujeta a `PolicyEngine`, `ChainOrchestrator` y aprobación humana de N5.
7. **Límites reales de las protecciones:**
   - En Python estándar sobre Windows, las operaciones de ruta por nombre mantienen una ventana teórica mínima en el kernel entre `lstat` y `scandir`; N7 lo mitiga no abriendo ni leyendo jamás el contenido de ningún archivo y revalidando `FILE_ATTRIBUTE_REPARSE_POINT` en cada `stat_result`.
   - En GPUs sin `nvidia-smi`, N7 detecta el adaptador por `winreg.KEY_READ` y reporta las métricas dinámicas de uso como `UNAVAILABLE` (`None`, nunca `0.0`).

---

## ⌨️ 5. Comandos Disponibles en Consola

En el modo consola interactiva (`.\venv\Scripts\python.exe main.py`):

| Comando | Fase | Descripción |
| :--- | :--- | :--- |
| `/modo` | Multi-Brain | Consulta o cambia el modo de inteligencia (`AUTO`, `OLLAMA`, `OLLAMA_UNCENSORED`, `GEMINI`, `CLAUDE`, `GPT`, `CODEX`). |
| `/voz [on\|off]` | Audio | Activa o desactiva la lectura por síntesis de voz (TTS) de las respuestas en la consola. |
| `/contexto` (o `/context`) | **N7** | Resumen general en solo lectura (sistema, hardware, recursos frescos y almacenamiento). |
| `/contexto sistema` | **N7** | Sistema operativo, versión, build, arquitectura, Python y tiempo de actividad. |
| `/contexto hardware` | **N7** | Procesador, núcleos físicos/lógicos, frecuencia, RAM y GPU/VRAM detectadas. |
| `/contexto recursos` | **N7** | Uso actual en tiempo real de CPU, RAM, GPU/VRAM y espacio libre en el disco de trabajo. |
| `/contexto almacenamiento` | **N7** | Unidades montadas, sistema de archivos y capacidad total/libre. |
| `/contexto procesos [filtro]` | **N7** | Procesos activos acotados por consumo (opcionalmente filtrados por nombre, sin `cmdline`/`environ`). |
| `/contexto aplicaciones` | **N7** | Ventanas visibles, aplicaciones de usuario en ejecución y catálogo seguro de apps instaladas. |
| `/contexto archivos <ruta>` | **N7** | Metadatos básicos o listado acotado dentro de una raíz autorizada y no excluida. |
| `/contexto ayuda` | **N7** | Muestra la ayuda de subcomandos de observación contextual. |
| `/goal <objetivo>` | **N6** | Interpreta un objetivo, consulta contexto N7 pertinente y construye un plan DAG supervisado. |
| `/plan` / `/plan estado` | **N6** | Muestra el plan activo, sus subtareas, categorías de riesgo y estado de aprobación. |
| `/plan ejecutar` | **N6** | Ejecuta el plan activo solicitando confirmación interactiva para tareas `SENSITIVE_OPERATION`. |
| `/plan aclarar <respuesta>` | **N6** | Responde preguntas de aclaración cuando un objetivo fue clasificado como ambiguo. |
| `/plan cancelar` | **N6** | Cancela de forma limpia el objetivo o plan activo preservando la trazabilidad. |
| `/optimizar <juego>` | **N5** | Planifica y simula ajustes de configuración sobre archivos autorizados bajo `ChainOrchestrator`. |
| `/rollback <id>` | **N5** | Revierte transaccionalmente un cambio aplicado verificando hashes SHA-256 y backups. |
| `/limpiar` (o `/clear`) | Memoria | Limpia el historial conversacional de corto plazo en memoria. |
| `salir` (o `exit` / `quit`) | Sistema | Cierra de forma ordenada la sesión de consola. |

---

## 🚀 6. Instalación y Modos de Ejecución

### Requisitos del entorno
- **Sistema Operativo:** Windows 10 u 11 (64 bits).
- **Python:** 3.12 (64-bit) en entorno virtual `venv/`.
- **GPU recomendada:** NVIDIA GeForce GTX 1660 (6 GB VRAM) o superior con soporte CUDA (el sistema dispone de fallback controlado a CPU cuando CUDA o `nvidia-smi` no están presentes).
- **Proveedores de IA:** Ollama local (primario); claves API en `.env` para proveedores cloud (`GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) son **opcionales** (el sistema opera en modo degradado local si no están definidas).

### Diagnóstico de pre-vuelo
```powershell
.\venv\Scripts\python.exe check_health.py
```

### Modos de ejecución (`main.py`)
1. **Modo Consola de Texto (predeterminado — sin carga de audio en VRAM):**
   ```powershell
   .\venv\Scripts\python.exe main.py
   ```
2. **Modo Consola con Síntesis de Voz (TTS activo, sin micrófono):**
   ```powershell
   .\venv\Scripts\python.exe main.py --tts
   ```
3. **Modo Manos Libres Completo (Wake Word + Micrófono VAD + Whisper STT + TTS + System Tray):**
   ```powershell
   .\venv\Scripts\python.exe main.py --voice
   ```
4. **Selección de modo cognitivo al iniciar:**
   ```powershell
   .\venv\Scripts\python.exe main.py --mode OLLAMA
   ```
5. **Lanzadores rápidos en Windows:** `run_jarvis.bat` (consola) y `run_jarvis_background.vbs` (bandeja del sistema).

---

## 🧪 7. Testing y Validación

Todas las pruebas deben ejecutarse con `JARVIS_ENV="test"` para garantizar el confinamiento estricto de `ConversationMemory`, `TransactionManager` y `GoalStore` dentro de `temp/jarvis_test_isolated/`, preservando intacto `data/memory_store.json`.

```powershell
# 1. Suite específica de N7 (funcionalidad, seguridad F1-F6, consola e integración N6+N7):
$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest tests.test_context_awareness_n7 -v

# 2. Suite específica de N6 (Autonomous Goal & Planning Engine):
$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest tests.test_goal_engine -v

# 3. Suites de gobernanza transaccional y seguridad N5:
$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest tests.test_file_editor_and_chain tests.test_transaction_recovery tests.test_memory_isolation -v

# 4. Comando oficial de validación de la suite global completa:
$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

---

## 💻 8. Hardware, Rendimiento y Carga Diferida

- **Carga diferida (Lazy Loading):** En modo consola (`python main.py`), los módulos de audio (`faster_whisper`, `sounddevice`, `SpeechToText`, `TextToSpeech`, `WakeWordDetector`) **no se importan ni inicializan**, manteniendo en `0 MB` el consumo de VRAM por modelos de voz.
- **Presupuesto de VRAM en modo voz (GTX 1660 de 6 GB):**
  - `faster-whisper` (`base`, `cuda`, `float16` con fallback automático a CPU `int8`): ~950 MB VRAM.
  - Modelo local en Ollama (`3b`): ~2.2 GB VRAM.
- **Coalescencia y cotas en N7:** Consultas acotadas por timeout (`CONTEXT_PROVIDER_TIMEOUT_SEC=3.0s`), límites de colección (`25` procesos, `40` aplicaciones, `25` ventanas, profundidad `3` y `150` entradas de directorio) y locks por categoría para evitar recolecciones duplicadas ante solicitudes concurrentes.

---

## 📁 9. Estructura del Repositorio y Documentación

```text
jarvis-core/
├── config/
│   ├── settings.py                  # Configuración central, raíces autorizadas y parámetros N5/N6/N7
│   ├── prompts.py                   # Identidad y prompt maestro compartido
│   ├── tools_schema.py              # Esquemas JSON para herramientas
│   └── wake_config.py               # Umbrales de detección Wake Word
├── src/
│   ├── audio/                       # Captura VAD (recorder.py), STT (stt.py), TTS (tts.py), Wake Word
│   ├── brain/                       # Multi-Brain, ChainOrchestrator (N5), TransactionManager (N5), GoalEngine (N6)
│   ├── context/                     # N7 Context Awareness Engine (models, policy, providers, engine, console)
│   ├── tools/                       # SkillManager y registro de herramientas
│   ├── ui/                          # Interfaz en bandeja del sistema (tray.py)
│   └── utils/                       # Logger UTF-8 y GitSafetyGuard (git_guard.py)
├── skills/                          # BaseSkill, system_skills, windows_skills, file_editor, document_export, web_extraction
├── docs/
│   ├── N6_AUTONOMOUS_GOAL_ENGINE.md # Especificación y arquitectura de N6
│   ├── N7_CONTEXT_AWARENESS_ENGINE.md # Especificación, seguridad y configuración de N7
│   └── audits/
│       └── INDEPENDENT_SECURITY_AUDIT_N5_FINAL.md # Auditoría de cierre de N5
├── tests/                           # Suite global automatizada (591 tests)
├── AGENTS.md                        # Reglas obligatorias de desarrollo y arquitectura
├── ARCHITECTURE.md                  # Diagramas y arquitectura base del proyecto
├── CONTEXT.md                       # Bitácora técnica evolutiva del proyecto
└── main.py                          # Punto de entrada CLI, consola interactiva y ciclo de voz
```

---

## 🗺️ 10. Roadmap

- [x] **Fases 1–9 (`v3.0.0`):** Pipeline de voz local (Wake Word, VAD, Whisper CUDA, Streaming TTS, System Tray, empaquetado).
- [x] **Multi-Brain + Skills Documentales/Web (`v5.1.0`):** Enrutamiento cognitivo multi-proveedor, exportación documental segura y extracción web aislada.
- [x] **Fase N5 (`v5.2.0`):** Gobernanza transaccional WAL-First, `PolicyEngine`, `ChainOrchestrator`, aprobación interactiva y aislamiento de memoria.
- [x] **Fase N6:** `Autonomous Goal & Planning Engine` (`/goal` y `/plan`) con validación DAG y supervisión de ejecución.
- [x] **Fase N7:** `Context Awareness Engine` (`/contexto`) en solo lectura e integración con N6, con remediación de seguridad F1–F6 verificada.
- [ ] **Fase N8 (Futura — No iniciada):** Memoria de largo plazo y aprendizaje basado en experiencias.

---

## 🛡️ Licencia y Créditos
Desarrollado para propósitos académicos y de uso personal. Todos los componentes priorizan la soberanía de datos, el principio de mínimo privilegio y la privacidad del usuario.
