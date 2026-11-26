# J.A.R.V.I.S. — N8: Long-Term Memory & Experience (N8.1 + N8.2 + N8.3 Completa)

> [!IMPORTANT]
> **Alcance implementado actualmente:** Este documento cubre **N8.1** (persistencia base, modelos, políticas y locking), **N8.2** (recuperación determinista, API central, consola `/memoria` y `/privado`/`/incognito`, importación manual de preferencias, purga física y backups/restauración), **N8.3 Subfases 1 a 4** (`sqlite v2` + `store_version_token`, contratos v2.0 + `ExperienceCaptureService` + tombstones por rollback N5, integración consultiva N6 + `EphemeralGoalStore` + fronteras estructurales, y `MemorySnapshotGuard` + protección pre-dispatch en `BrainManager` y fallbacks) y la **ronda integral de remediación de auditoría independiente (F-01 a F-09)**.
> **Estado de N8.3:** Hallazgos F-01 a F-09 remediados y verificados en suite global aislada, **pendientes de verificación independiente final por Antigravity Argon** antes del cierre oficial. Permanecen fuera de alcance actual los embeddings semánticos y la consolidación automática por LLM.

---

## 1. Arquitectura N8.1 + N8.2 y Separación entre Capas

```mermaid
flowchart LR
    U["Consola (/memoria, /privado, /incognito)"] --> CON["MemoryConsoleHandler (src/memory/console.py)"]
    BM["BrainManager (canal untrusted consultivo)"] -->|"solo lectura si allow_history"| ENG["MemoryEngine (src/memory/engine.py)"]
    CON --> ENG
    CON -->|"aislamiento RAM"| MEM["ConversationMemory (src/brain/memory.py)"]
    ENG --> RET["MemoryRetriever (src/memory/retriever.py)"]
    ENG --> POL["MemoryPolicy (src/memory/policy.py)"]
    RET --> ST["ExperienceStore + ExperienceStoreLock (src/memory/store.py)"]
    ENG --> ST
    ST --> DB["data/experiences.db (SQLite WAL + FTS5)"]
    ST --> BKP["data/backups/memory/ (.db + .backup.json SHA-256)"]
```

**Reglas arquitectónicas innegociables:**
- N8 es estrictamente consultivo: **nunca** autoriza operaciones, aprueba planes, modifica políticas de N5 ni escribe archivos de usuario.
- Los recuerdos recuperados se entregan encapsulados como **`untrusted_long_term_memory`** (`allow_system_prompt_promotion=False`, `allow_tool_execution_from_content=False`, `allow_permission_grant=False`); jamás ingresan al `system_prompt` como instrucciones privilegiadas.
- N8 no duplica `GoalStore` (N6) ni altera `data/memory_store.json` de forma automática.
- Inicializar `MemoryEngine`, `MemoryRetriever` o `MemoryConsoleHandler` **no carga** Whisper, micrófono, `AudioRecorder`, `WakeWordDetector` ni TTS.

---

## 2. Módulos del Paquete (`src/memory/`)

| Archivo | Fase | Responsabilidad |
| :--- | :---: | :--- |
| [models.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/memory/models.py) | N8.1/N8.2/N8.3-S1 | Enumeraciones (`MemoryAuditAction.REJECT_IDEMPOTENCY_CONFLICT`), máquina de estados, excepciones tipadas (`MemoryIdempotencyConflictError`, etc.) y contratos DTO (`ExecutionVerificationEvidence` con `canonical_unit_id`, `MemoryCreateInput`, `MemoryRecord`, `ExecutionInvalidationResult`, `MemoryRetrievalResult` y `StoreStats` con `store_version_token`). |
| [policy.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/memory/policy.py) | N8.1/N8.2/N8.3-S1 | `MemoryPolicy`, `sanitize_memory_text()`, detección sin exposición de secretos (`assert_no_secrets` + `redact_secrets`), bloqueo de `INFERENCE`, validación `EXECUTION_VERIFIED`, derivación determinista `compute_default_canonical_unit_id()` / `compute_execution_idempotency_key()` y sandbox `JARVIS_ENV=test`. |
| [migrations.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/memory/migrations.py) | N8.1/N8.3-S1 | Esquema SQLite versionado v1 -> v2 (`canonical_unit_id`, `idempotency_key`, reconstrucción de `memory_audit`, índices de idempotencia/ejecución, `store_version_epoch` / `store_version_seq` / `store_version_token`), tokenizer `unicode61 remove_diacritics 2` y pragmas `WAL` + `synchronous=FULL` + `secure_delete=ON` + `foreign_keys=ON`. |
| [store.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/memory/store.py) | N8.1/N8.2/N8.3-S1/S2 | `ExperienceStoreLock` y `ExperienceStore`: CRUD transaccional (`BEGIN IMMEDIATE`), idempotencia estricta `EXECUTION_VERIFIED` sin fusión entre ejecuciones, dedupe convencional por `content_fingerprint`, actualización atómica de `store_version_token`, `invalidate_by_execution()`, tombstones persistentes de rollback (`is_execution_invalidated()`, `mark_execution_rollback_tombstone()`), backups y restauración compatible v1/v2 con verificación inmediata. |
| [retriever.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/memory/retriever.py) | N8.2/N8.3-S1 | Recuperación determinista FTS5/BM25 + fallback SQL `LIKE` junto con lectura atómica del `store_version_token` del snapshot recuperado. |
| [engine.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/memory/engine.py) | N8.2/N8.3-S1..S4 | API central `MemoryEngine`: coordinación de políticas, modo privado, ciclo de vida completo, `get_store_version_token()`, `create_snapshot_guard()`, `invalidate_by_execution()`, tombstones de rollback (`is_execution_invalidated()`, `mark_execution_rollback_tombstone()`), backups/restauración e importación manual read-only desde JSON legacy. |
| [snapshot_guard.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/memory/snapshot_guard.py) | N8.3-S4 | `MemorySnapshotGuard`, `MemorySnapshot`, `MemorySnapshotReference`, `SnapshotMemoryPayload`, `SnapshotValidationStatus` y `SnapshotValidationResult`: vinculación inmutable por solicitud, comprobación de `store_version_token`, revalidación acotada (máx. 1 revalidación / 1 refresh) y fail-closed sin locks SQLite retenidos. |
| [capture_contracts.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/memory/capture_contracts.py) | N8.3-S2 | Contrato versionado `CAPTURE_CONTRACT_VERSION="2.0"`, referencias inmutables (`VerifiedOperationReference`), validación de verificadores autorizados (`AUTHORIZED_VERIFIERS_BY_LAYER`), digests `evidence_sha256` / `contract_sha256` deterministas y rechazo de `verified=True`, LLM/síntesis, transcripts, web cruda o secretos (`MemoryEvidenceContractError`). |
| [capture.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/memory/capture.py) | N8.3-S2 | `N5ExecutionEvidenceAdapter`, `N6GoalEvidenceAdapter` (`COMPLETED` / `PARTIAL` tarea a tarea verificada) y coordinador desacoplado `ExperienceCaptureService` con puertas de privacidad/flags, aislamiento total de fallos e invalidación/tombstone por rollback N5 confirmado. |
| [console.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/memory/console.py) | N8.2/N8.3-S3 | Comandos de consola `/memoria`, `/privado` e `/incognito` con callback `on_private_mode_changed` hacia N6, validación de argumentos, vistas previas y confirmación interactiva antes de acciones destructivas. |
| [brain_manager.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/brain/brain_manager.py) | Multi-Brain/N8.3-S4 | Coordinador Multi-Brain (`process_turn`, `query_raw`, `_generate_with_fallback_policy`, `_invoke_single_mode`): validación pre-dispatch de `MemorySnapshotGuard` inmediatamente antes de cada envío físico, reconstrucción segura en fallbacks/reintentos y verificación de aislamiento de roles (`system_prompt` / `prompt`). |
| [goal_memory.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/brain/goal_memory.py) | N8.3-S3/S4 | Puente consultivo `GoalMemoryBridge`: sobres `RuntimeN8Envelope` en RAM con conversión a `MemorySnapshot` (`to_memory_snapshot()`, `build_dispatch_guard()`), referencias persistibles mínimas `N8MemoryReference`, revalidación `STRICT_INVALIDATE` y separación estructural `persistable_skeleton` vs `ephemeral_annotations`. |
| [goal_persistence.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/brain/goal_persistence.py) | N6/N8.3-S3 | `GoalStore` endurecido con saneado recursivo contra texto N8/N7 volátil (`extract_persistable_plan_dict` / `sanitize_wal_payload`) y `EphemeralGoalStore` 100 % RAM-only para modo privado. |

---

## 3. Recuperación Determinista, Ranking y Presupuestos (`MemoryRetriever`)

1. **Motores:** FTS5 con `bm25(memory_fts)` y tokenizer `unicode61 remove_diacritics 2`, con respaldo automático parametrizado SQL `LIKE ESCAPE '\'` sobre `normalized_text`.
2. **Filtros obligatorios previos al ranking:**
   - Solo estado `ACTIVE` (salvo consultas administrativas explícitas en `store.list_memories`).
   - Excluye `REVOKED`, `SUPERSEDED`, `EXPIRED`, recuerdos con `expires_at` vencido y recuerdos en espera de confirmación (`pending_confirmation=True`) hasta su confirmación explícita.
   - Respeta el ámbito solicitado: `GLOBAL`, `PROJECT` o `APP_OR_GAME` sin mezclar proyectos ni juegos ajenos.
3. **Límite de tokens de búsqueda:** `tokenize_query()` normaliza Unicode y acota la consulta a `N8_SEARCH_MAX_TOKENS` (`10` tokens por defecto).
4. **Fórmula de ranking determinista** *(sin LLM ni embeddings; no fusiona dos recuerdos con distinto `memory_id` por similitud textual)*:
   - `0.40` relevancia textual (cobertura de tokens + señal BM25/SQL)
   - `0.20` coincidencia exacta de ámbito (`PROJECT` / `APP_OR_GAME` supera a `GLOBAL`)
   - `0.15` confianza declarada (`0.0..1.0`)
   - `0.15` confiabilidad de fuente (`EXECUTION_VERIFIED=1.00`, `DECISION_CONFIRMED=0.90`, `USER_EXPLICIT=0.85`)
   - `0.07` vigencia/recencia e importancia
   - `0.03` estado verificado (`last_verified_at`).
5. **Presupuestos iniciales (`config/settings.py`):**

| Destino (`MemoryRetrievalTarget`) | Máximo de recuerdos | Máximo de caracteres |
| :--- | :---: | :---: |
| `NORMAL` (consulta estándar) | `5` (`N8_TEXT_MAX_ITEMS`) | `1500` (`N8_TEXT_MAX_CHARS`) |
| `VOICE` / `LOCAL_3B` (voz y modelo local 3B) | `3` (`N8_VOICE_MAX_ITEMS`) | `600` (`N8_VOICE_MAX_CHARS`) |

---

## 4. Ciclo de Vida Completo y Comandos de Consola

| Comando | Operación | Confirmación previa | Efecto principal |
| :--- | :--- | :---: | :--- |
| `/memoria ayuda` | Ayuda | No | Lista sintaxis de comandos N8.2 y modo privado. |
| `/memoria buscar <consulta>` | Consulta rankeada | No | Recupera `ACTIVE` confirmados dentro de presupuesto (`--voz`, `--proyecto`, `--app`). |
| `/memoria listar [filtro]` | Listado administrativo | No | Lista `activos`, `pendientes`, `revocados`, `expirados` o `todos`. |
| `/memoria ver <id>` | Detalle e historial | No | Muestra contenido, metadatos, revisión actual e historial previo. |
| `/memoria corregir <id> [texto]` | Revisión inmutable | No | Incrementa `revision_number` y guarda el estado anterior en `memory_revisions`. |
| `/memoria confirmar <id>` | Confirmación | No | Promueve recuerdos pendientes (o revalida activos) con `last_verified_at`. |
| `/memoria olvidar <id> [motivo]` | Revocación lógica | No | Cambia a `REVOKED`, purga fila de `memory_fts` y conserva trazabilidad legal/auditoría. |
| `/memoria purgar <id>` | Eliminación física SQLite | **Sí** | Borra registro, `memory_fts`, `memory_revisions` y filas de `memory_audit` asociadas. |
| `/memoria importar-preferencias [ruta]` | Importación manual JSON | **Sí (preview)** | Lee JSON en solo lectura, clasifica `NEW`/`DUPLICATE`/`CONFLICT`/`REJECTED` y aplica solo tras confirmar. |
| `/memoria backup` | Snapshot consistente | No | Ejecuta `sqlite3.Connection.backup()`, valida integridad y guarda SHA-256. |
| `/memoria restaurar [id\|num]` | Restauración atómica | **Sí** | Valida SHA-256 + SQLite, preserva DB previa y reemplaza mediante `os.replace()`. |
| `/privado` / `/incognito` | Alternar privacidad | No | Activa Opción A (lectura permitida, cero escrituras/auto-save) o sale descartando buffers RAM. |
| `/privado --sin-historial` / `/incognito --sin-historial` | Aislamiento estricto | No | Modo privado sin lectura de recuerdos históricos N8 ni memoria conversacional previa. |

---

## 5. Modo Privado — Aislamiento Real (Opción A y `--sin-historial`)

La protección no depende de instrucciones al modelo LLM sino de controles estructurales en código:

1. **Buffers separados en RAM (`ConversationMemory`):** `_private_mode`, `_private_allow_history`, `_private_messages`, `_private_message_metadata`, `_private_external_references` y `_private_metadata`.
2. **Bloqueo incondicional de persistencia:** mientras `_private_mode=True`:
   - `ConversationMemory._save_to_disk()` y `save_to_disk()` retornan `False` inmediatamente sin tocar archivos ni crear `.tmp`.
   - `MemoryEngine` bloquea toda escritura (`create_memory`, `correct_memory`, `confirm_memory`, `revoke_memory`, `purge_memory`, `create_backup`, `restore_backup`, `commit_preferences_import`) lanzando `MemoryPrivateModeError`.
   - En `main.py`, operaciones persistentes de archivos como `/optimizar` y `/rollback` se rechazan durante `/privado`, mientras que `/goal` y `/plan` operan en RAM sobre `EphemeralGoalStore` (descartándose por completo en `/privado off`).
3. **Sin historial (`--sin-historial`):** desactiva lecturas previas en `ConversationMemory` y bloquea `MemoryEngine.retrieve()` devolviendo resultado vacío con `blocked_by_history_policy=True`.
4. **Cero fugas en logs y herramientas:** `SkillManager.set_private_mode(True)` y `IntentRouter` omiten argumentos crudos, respuestas crudas y `traceback` cuando el modo privado está activo.
5. **Salida limpia:** `exit_private_mode()` vacía y descarta todos los buffers `_private_*` sin mezclarlos jamás con `self._messages` ni `self.preferences`; los guardados posteriores (`auto_save=True`) excluyen todo turno privado previo.

---

## 6. Importación Manual de Preferencias (`data/memory_store.json`)

- **Nunca automática al iniciar.** Solo se ejecuta bajo `/memoria importar-preferencias`.
- Abre el JSON origen estrictamente en **solo lectura** y **jamás modifica el archivo origen**.
- Rechaza JSON corrupto o estructuras inesperadas sin campo diccionario `preferences` sin sobrescribir nada.
- No importa mensajes conversacionales sueltos (`messages`), objetos complejos, claves sensibles (`api_key`, `password`, `token`, etc.), secretos ni marcadores de inferencia.
- Muestra vista previa diferenciando `NEW`, `DUPLICATE`, `CONFLICT` y `REJECTED`, exigiendo confirmación explícita (`skip` conservador por defecto o `sobrescribir` marcando el previo como `SUPERSEDED`).
- En `JARVIS_ENV=test`, rechaza por política intentar leer el archivo real `data/memory_store.json` (`MemoryPolicyError`).

---

## 7. Revocación Lógica, Purga Física y Backups / Restauración

### Purga física (`purge_memory`) y limitaciones reales
- Bajo `ExperienceStoreLock`, borra transaccionalmente (`BEGIN IMMEDIATE`) filas en `memory_fts`, referencias `superseded_by`, `memory_revisions`, filas de `memory_audit` vinculadas al `memory_id` (dejando solo un tombstone anonimizado con SHA-256 del ID sin texto del recuerdo) y el registro en `memories`; luego ejecuta `PRAGMA wal_checkpoint(TRUNCATE)` (con `PRAGMA secure_delete=ON` activo en todas las conexiones) y `VACUUM` opcional.
- **Limitación documentada honestamente:** aunque SQLite sobrescribe con ceros sus páginas libres, ningún software de aplicación puede garantizar borrado irrecuperable frente a wear-leveling de SSDs, journals/MFT de NTFS, instantáneas Shadow Copies (VSS) de Windows ni backups previos creados fuera o antes de la purga.

### Backups (`data/backups/memory/`) y restauración atómica
- **Creación (`create_backup`):** usa `sqlite3.Connection.backup()` bajo `ExperienceStoreLock` hacia archivo temporal, valida `PRAGMA quick_check == 'ok'`, `integrity_check == 'ok'`, `foreign_key_check == []`, calcula `SHA-256`, publica con `os.replace()` junto a su descriptor `.backup.json` sin credenciales y **solo tras validar el nuevo backup** aplica retención máxima (`N8_BACKUP_MAX_RETENTION = 5`).
- **Restauración (`restore_from_backup`):** verifica `SHA-256` + integridad SQLite del snapshot antes de adquirir bloqueo exclusivo; dentro de `ExperienceStoreLock`, consolida WAL si la DB actual es legible, guarda una copia previa verificada por SHA-256 en `preserved_pre_restore_<uuid>/` (incluso si la DB activa estaba corrupta), limpia `-wal`/`-shm`/`-journal` obsoletos, sustituye atómicamente vía archivo staging sincronizado con `os.fsync()` + `os.replace()` y verifica integridad post-swap (revirtiendo a la copia preservada si fallara).
- **Limitación en sistema de archivos:** en Windows, la sustitución atómica `os.replace()` exige que el archivo temporal de staging resida en el mismo volumen que `data/experiences.db` (garantizado ubicándolo en `db_path.parent`) y presupone que ningún proceso externo ajeno a J.A.R.V.I.S. mantenga abierto sin lock el archivo `.db`.

---

## 8. Concurrencia, Configuración, Protección de Datos y Límites N8.3

- **Concurrencia real:** Todas las operaciones (`create`, `revise`, `confirm`, `revoke`, `purge`, `backup`, `restore`, `verify_integrity`, `retrieve`) comparten `ExperienceStoreLock` (RLock modular por ruta canónica + `msvcrt.locking` en Windows / `fcntl.flock` en POSIX). Validado mediante subprocesos independientes reales en `writer-vs-purge` y `writer-vs-restore`.
- **Protección Git:** `.gitignore` y `src/utils/git_guard.py` (`GitSafetyGuard`) bloquean `data/`, `experiences.db*`, `-wal`, `-shm`, `-journal`, `.db.lock`, `.backup.json`, `.db.sha256`, `data/backups/memory/`, `.restore_staging_*`, `.pre_restore_*` y `*.corrupt.*`.
- **Aislamiento de tests (`JARVIS_ENV=test`):** redirige DB y backups bajo `temp/jarvis_test_isolated/` y prohíbe acceder a `data/memory_store.json`, `data/experiences.db` o `data/backups/` reales.
- **Límites respecto a funcionalidades futuras:** las Subfases 1–4 de N8.3 están implementadas (`N8_AUTO_CAPTURE_ENABLED` permanece en `False` por defecto en producción) y quedan pendientes únicamente la auditoría integral independiente de N8.3, los embeddings semánticos y la consolidación por LLM.

---

## 9. Esquema SQLite v2, Idempotencia Estricta y Coherencia de Snapshot (N8.3 Subfase 1)

1. **Migración incremental v1 -> v2 (`apply_migrations`):**
   - Añade columnas `canonical_unit_id` e `idempotency_key` a `memories` y ejecuta backfill determinista sobre filas `EXECUTION_VERIFIED` previas respetando unidades activas e inactivas.
   - Reconstruye `memory_audit` preservando filas históricas para ampliar el `CHECK` con `REJECT_IDEMPOTENCY_CONFLICT`.
   - Reemplaza `idx_memories_active_exec_provenance` por:
     - `idx_memories_idempotency_key` (`UNIQUE` sobre `idempotency_key WHERE idempotency_key IS NOT NULL`, cubriendo todos los estados para impedir resurrecciones silenciosas tras rollback).
     - `idx_memories_active_exec_unit` (`UNIQUE` sobre `(source_layer, source_execution_id, COALESCE(source_plan_id, ''), COALESCE(source_task_id, ''), canonical_unit_id)` cuando `source = 'EXECUTION_VERIFIED' AND status = 'ACTIVE'`).
     - `idx_memories_source_execution_id` para invalidación indexada en rollbacks.
   - Restringe `idx_memories_active_fingerprint` a `WHERE status = 'ACTIVE' AND source != 'EXECUTION_VERIFIED'`.

2. **Separación entre dedupe convencional e idempotencia `EXECUTION_VERIFIED`:**
   - Orígenes convencionales (`USER_EXPLICIT`, `DECISION_CONFIRMED`): continúan aplicando deduplicación canónica por `content_fingerprint` únicamente frente a registros convencionales `ACTIVE`.
   - Origen `EXECUTION_VERIFIED`: nunca colisiona por `content_fingerprint` entre ejecuciones diferentes; primero valida `idempotency_key`:
     - Si existe y el payload + evidencia coinciden -> devuelve `DEDUPE_HIT` (`matched_reason="idempotency_key"`) sin mutar estado ni avanzar `store_version_token`.
     - Si existe con evidencia o payload incompatible -> registra `REJECT_IDEMPOTENCY_CONFLICT` en `memory_audit` sin exponer secretos, mantiene `memories` y `store_version_token` intactos, y lanza `MemoryIdempotencyConflictError`.
     - Una misma tarea/ejecución puede registrar múltiples unidades independientes (`canonical_unit_id` distintos, e incluso `EXPERIENCE` + `FACT` por unidad predeterminada) sin colapsar.

3. **`store_version_token` (`<epoch>:<seq>`) persistente y atómico:**
   - Persistido en `store_metadata` (`store_version_epoch`, `store_version_seq`, `store_version_token`).
   - Las operaciones de solo lectura, `DEDUPE_HIT`, `REJECT_IDEMPOTENCY_CONFLICT`, `invalidate_by_execution` con 0 afectadas y los rollbacks transaccionales dejan el token intacto.
   - Toda mutación efectiva (`create_memory`, `update_memory_with_revision`, `confirm_memory`, `transition_status`, `revoke_memory`, `invalidate_by_execution` > 0, `purge_memory`) incrementa atómicamente `store_version_seq` dentro de la misma transacción `BEGIN IMMEDIATE`.
   - `restore_from_backup()` valida y migra automáticamente snapshots v1 o v2, verifica sincronía FTS5, rota `store_version_epoch` garantizando `seq > prev_live_seq` (sin reutilizar jamás tokens previos aun restaurando backups antiguos) y deja `verify_integrity()` listo para aprobar de inmediato.
   - `MemoryRetriever.retrieve()` lee candidatos y `store_version_token` bajo el mismo snapshot bloqueado para habilitar validación futura en N8.3.

4. **Invalidación por ejecución revertida (`invalidate_by_execution`):**
   - `MemoryEngine.invalidate_by_execution()` / `ExperienceStore.invalidate_by_execution()` revoca lógicamente (`REVOKED`) los recuerdos `EXECUTION_VERIFIED` activos asociados a `source_execution_id`, los purga de inmediato de `memory_fts`, conserva la auditoría histórica (`REVOKE`) y retorna `ExecutionInvalidationResult`.
   - Repetir la llamada sobre la misma ejecución es idempotente (sin revocaciones adicionales y sin modificar `store_version_token`).
   - Está bloqueada en modo privado (`MemoryPrivateModeError`). Un retry idempotente posterior con la misma clave devuelve el registro `REVOKED` sin resucitarlo.

---

## 10. Contratos de Evidencia, Captura Verificada N5/N6 e Invalidación por Rollback (N8.3 Subfase 2)

1. **Contrato estricto y versionado (`src/memory/capture_contracts.py`):**
   - Define `CAPTURE_CONTRACT_VERSION = "2.0"`, `VerifiedOperationReference` y `VerifiedExecutionEvidenceContract` (`@dataclass(frozen=True)`), deserialización estricta con rechazo de campos desconocidos, listas blancas de verificadores objetivos (`AUTHORIZED_VERIFIERS_BY_LAYER`) y listas negras de marcadores especulativos/LLM/síntesis (`FORBIDDEN_VERIFIER_MARKERS`, `FORBIDDEN_EVIDENCE_KEYS`).
   - Rechaza explícitamente booleanos simples (`{"verified": True}`), transcripciones, prompts, salidas crudas LLM/web (`web_extraction` nunca persiste HTML/texto externo) y secretos (`MemoryEvidenceContractError` / `MemoryPolicyError`).
   - Calcula `evidence_sha256` y `contract_sha256` deterministas mediante JSON canónico y exige verificación post-escritura con `after_sha256` en toda referencia N5.

2. **Adaptadores desacoplados N5 y N6 (`src/memory/capture.py`):**
   - `N5ExecutionEvidenceAdapter`: adapta registros reales de `ChainOrchestrator` únicamente si `state == COMPLETED`, `approval_granted=True`, `approval_hash` SHA-256 presente, operaciones aplicadas (`APPLIED`) con verificación objetiva confirmada (`after_sha256`) y sin `rolled_back_at` ni ejecución ya invalidada.
   - `N6GoalEvidenceAdapter`: evalúa planes de `GoalEngine` / `ExecutionSupervisor` (`COMPLETED` y `PARTIAL`) tarea a tarea mediante `AdaptationReport`, aceptando solo tareas `TaskStatus.COMPLETED` con verificador autorizado (`ExecutionSupervisorSkillVerifier`, `ExecutionSupervisorChainVerifier`, `ChainOrchestratorVerifier`), IDs consistentes (`task_id`/`plan_id`/`goal_id`), entrada `task_completed` real en `plan.history` y artefactos verificables. Nunca marca un plan `PARTIAL` como éxito global ni acepta `internal_synthesis` o texto libre del modelo. Cuando una tarea N6 delega en N5 (`chain_execution_id`), enlaza `source_execution_id` al `chain_execution_id` real.

3. **Coordinador `ExperienceCaptureService` e invariantes de seguridad:**
   - Captura exclusivamente con `MemorySource.EXECUTION_VERIFIED` y `MemoryCategory.FACT` bajo puertas estrictas: `N8_ENABLED`, `N8_AUTO_CAPTURE_ENABLED` (apagada por defecto), rechazo absoluto en modo privado (`is_private_mode`) sin leer ni contaminar buffers privados en RAM, deduplicación/idempotencia v2 y bloqueo de ejecuciones revertidas.
   - Todo fallo de SQLite, bloqueo o conflicto se aísla por completo (`CaptureBatchResult` / `CaptureUnitOutcome`): jamás altera ni hace fallar la ejecución de N5 ni de N6 ni el arranque liviano de `main.py` sin voz.

4. **Invalidación por rollback N5 confirmado y tombstone persistente:**
   - `ChainOrchestrator` dispara `handle_n5_rollback_confirmed()` únicamente cuando `rollback_execution()` o `recover_pending_transactions()` confirman transición real a `ROLLED_BACK` (nunca en `ROLLBACK_FAILED` ni ante conflictos externos).
   - `ExperienceCaptureService` ejecuta invalidación idempotente vía `MemoryEngine.invalidate_by_execution(execution_id)` y registra tombstone persistente en `memory_audit` (`mark_execution_rollback_tombstone()` / `is_execution_invalidated()`) para impedir capturas tardías o en paralelo incluso tras reiniciar el proceso. También está bloqueada cuando el modo privado está activo (`blocked_private_mode`).

---

## 11. Integración Consultiva con N6, Aislamiento de Contexto y Modo Privado (N8.3 Subfase 3)

1. **`EphemeralGoalStore` (`src/brain/goal_persistence.py`):**
   - Almacén 100 % en RAM con interfaz equivalente a `GoalStore` (`save_plan`, `append_wal_event`, `load_plan`, `load_active_plan`, `list_plan_ids`, `read_wal_events`, `clear_active_pointer`, `clear`, `discard`).
   - En modo privado (`/privado`, `/privado --sin-historial` o vía API programática `set_private_mode` / `is_private_mode_fn`), `GoalEngine.active_goal_store` cambia automáticamente a `EphemeralGoalStore`, evitando generar archivos `.goal.json`, `.wal.jsonl` ni temporales auxiliares en `data/goals/` o `temp/jarvis_test_isolated/`.
   - Al salir del modo privado (`on_private_mode_exited()` / `/privado off`) o cerrar el proceso, los objetivos privados se descartan por completo sin transferencia a `GoalStore` persistente; los planes `is_ephemeral_private=True` quedan bloqueados para persistirse en `GoalStore`.

2. **Integración consultiva acotada en el ciclo de vida de N6 (`src/brain/goal_memory.py` y `src/brain/goal_engine.py`):**
   - Disparo en los puntos reales de planificación inicial (`create_goal_and_plan` / `clarify_pending_goal` / `interpret_goal`), replanificación (`ExecutionSupervisor._attempt_replan`) y reanudación (`resume_plan`, consola `/plan reanudar`), evitando consultas duplicadas innecesarias sobre un mismo objetivo.
   - Ámbitos derivados de contexto (`PROJECT` > `APP_OR_GAME` > `GLOBAL`) y presupuestos diferenciados según el perfil del modelo activo: `NORMAL` (`N8_TEXT_MAX_ITEMS=5`, `N8_TEXT_MAX_CHARS=1500`) y modelos locales pequeños tipo 3B (`OLLAMA_MODEL` / `3b`, perfil `LOCAL_3B`: `N8_VOICE_MAX_ITEMS=3`, `N8_VOICE_MAX_CHARS=600`). Todo error de consulta o revalidación en N8 se aísla sin bloquear la planificación o ejecución de N6.

3. **Sobres volátiles RAM-only y frontera estructural de persistencia:**
   - `GoalMemoryBridge` almacena los contenidos recuperados en `_runtime_n8_envelopes` (`RuntimeN8Envelope` / `RuntimeN8MemoryItem`), con redacción en `__repr__`/`__str__` para no filtrar texto de memoria en logs o excepciones, aislados por `plan_id`/`goal_id` y liberados al completar, cancelar o limpiar el objetivo.
   - En `GoalStore` (y en copias WAL/snapshot) solo se admiten referencias mínimas trazables en `goal.context.extra` (`ALLOWED_PERSISTED_N8_EXTRA_KEYS`: `n8_query_id`, `n8_memory_refs`, `n8_trust_boundary`, `n8_last_revalidated_at`). Todo campo prohibido (`FORBIDDEN_PERSISTED_N8_KEYS`), anotaciones LLM+N8 (`ephemeral_annotations`), paráfrasis y parámetros de skills no anclados independientemente en `collect_grounded_anchors()` (`separate_grounded_skill_params()`) se excluyen determinísticamente sin depender de heurísticas n-gram y sin romper `n7_untrusted_envelope` de N7.

4. **Revalidación estricta y semántica TOCTOU / revocación entre planificación y ejecución:**
   - Antes de consumir recuerdos en síntesis o planificación LLM (`_prepare_n8_context_for_execution`, `_execute_internal_synthesis`, `_attempt_replan`, `resume_plan`), `GoalMemoryBridge.revalidate_references()` consulta caso a caso la API pública `MemoryEngine.get_memory()` (sin acceso SQL directo desde N6).
   - Se excluyen con registro trazable (`RevalidationReport`) los registros `REVOKED`, `EXPIRED` (por estado o timestamp vencido `expires_at`), `SUPERSEDED`, purgados/inexistentes, `pending_confirmation=True`, o con revisión/hash modificados bajo `RevisionMismatchPolicy.STRICT_INVALIDATE`, **sin auto-sustituir jamás** un recuerdo descartado por otro similar.
   - **Semántica de frontera TOCTOU / revocación en Subfase 3:** la revalidación verifica frescura al iniciar cada etapa/consumo dentro de N6 y refleja de inmediato revocaciones o cambios ocurridos en `MemoryEngine` antes del consumo siguiente; las operaciones de herramientas deterministas ya en curso con parámetros anclados en el usuario/N7 no leen N8 en tiempo real. La verificación pre-dispatch en `BrainManager` mediante `MemorySnapshotGuard` queda implementada en la Sección 12 (Subfase 4).

5. **Modo privado y frontera de confianza:**
   - `/privado` (Opción A: lectura histórica previa permitida, cero escrituras N8, `EphemeralGoalStore` en RAM) y `/privado --sin-historial` (lectura y escritura bloqueadas, limpieza inmediata de sobres RAM y cero inyección en prompts) aplican tanto por consola como en API Python directa (`MemoryEngine` + `GoalEngine`).
   - Los recuerdos revalidados se inyectan exclusivamente por `BrainManager.query_raw(..., untrusted_context=...)` marcados con `trust_boundary="untrusted_long_term_memory"`, subordinados al `system_prompt`, a las restricciones de N5 (`read_only`, `requires_approval`) y al estado actual observado por N7 (si N7 observa otra GPU/entorno, prevalece N7).

---

## 12. `MemorySnapshotGuard`, Control de Frescura y Protección Pre-Dispatch en `BrainManager` (N8.3 Subfase 4)

> [!NOTE]
> **Cierre de implementación N8.3:** Con la Subfase 4 se completa la implementación técnica planificada de N8.3, quedando **pendiente de auditoría integral independiente**.

1. **Diseño de `MemorySnapshotGuard` (`src/memory/snapshot_guard.py`):**
   - Representa una instantánea consultiva inmutable (`MemorySnapshot`) vinculada de forma estricta al identificador único de solicitud (`request_id`, más `goal_id`/`plan_id` en flujo N6), nunca compartida ni reutilizada entre conversaciones u objetivos diferentes y residente **exclusivamente en RAM** (todo `__repr__`/`__str__` redacta contenido con `[RAM-ONLY REDACTED]`).
   - Conserva: `snapshot_id`, `n8_query_id`, `request_id`, `store_version_token`, tuplas de referencias inmutables (`MemorySnapshotReference` con `memory_id`, `revision_number`, `version_hash`), `scope`, `target`, `query_text`, `retrieved_at`, `last_validated_at` y `validated_before_dispatch` (inicializado siempre en `False` hasta superar todas las comprobaciones reales).

2. **Semántica de `store_version_token` y validación acotada de frescura:**
   - Todo recuerdo recuperado captura el `store_version_token` (`<epoch>:<seq>`) leído atómicamente bajo el mismo snapshot SQLite en `MemoryRetriever.retrieve()`. Tokens vacíos con ítems o con formato ilegible fallan de inmediato como `FAILED_TOKEN_INVALID`.
   - En `validate_before_dispatch()`:
     - Si el token vivo (`MemoryEngine.get_store_version_token()`) coincide con `snapshot.store_version_token`: resuelve cada referencia vía `MemoryEngine.get_memory()` (excluyendo `REVOKED`, `EXPIRED` por estado o `expires_at <= now`, `SUPERSEDED`, purgados, pendientes o con revisión/hash modificados) y ejecuta un **post-check** de token inmediatamente al finalizar para detectar mutaciones ocurridas en plena revalidación.
     - Si el token cambió (antes o durante la revalidación): no envía el contexto antiguo, consume como máximo **1 revalidación** (`DEFAULT_MAX_REVALIDATIONS = 1`) y a lo sumo **1 recuperación nueva acotada** (`DEFAULT_MAX_REFRESH_RETRIEVALS = 1`) únicamente cuando corresponde sin sustituir jamás referencias excluidas por recuerdos similares.
     - Si el token sigue cambiando durante esa única revalidación/refresh, o SQLite/token no está disponible, falla de forma **fail-closed respecto del contexto N8** (`FAILED_TOKEN_UNSTABLE`, `FAILED_ENGINE_UNAVAILABLE`, `n8_allowed=False`, `untrusted_n8_context=None`).

3. **Punto exacto pre-dispatch y protección de fallbacks/reintentos en `BrainManager`:**
   - Todos los envíos físicos de `process_turn()`, `query_raw()` (incluidos N6 `GoalUnderstandingEngine`, `TaskDecomposer`/`GoalPlanningEngine`, `mode_override` y selección `AUTO`) convergen en `_generate_with_fallback_policy()` -> `_invoke_single_mode()` -> `_validate_n8_for_physical_dispatch()`, ejecutado **inmediatamente antes de invocar al adaptador físico**.
   - **Fallbacks y reintentos:**
     - Proveedor primario exitoso -> `1` validación pre-dispatch.
     - Cada salto entre proveedores (`Claude -> Gemini -> Ollama`, `GPT -> Gemini -> Ollama`, `Codex -> Gemini -> Ollama`, `AUTO`) vuelve a validar frescura antes de enviar y reconstruye `untrusted_context` preservando evidencia no-N8 (p. ej. web de `ConversationMemory`). Si N8 falla o bloquea en algún intento, ese snapshot inválido se descarta (`snapshot = None`) para no reutilizarlo en fallbacks siguientes.
     - Adicionalmente, `OllamaBrain`, `GeminiBrain` y `OpenAIBrain` aceptan el callback opcional `pre_dispatch_refresh` para revalidar frescura incluso dentro de sus reintentos internos de modelo por HTTP 404 (`OLLAMA_UNCENSORED -> OLLAMA`, `gemini-2.0 -> 1.5`, `gpt-5.4 -> gpt-4o`).
   - Cuando un llamador externo pasa JSON N8 directamente en `query_raw(..., untrusted_context=...)` sin objeto `MemorySnapshot`, `BrainManager` detecta estructuralmente los marcadores `"untrusted_long_term_memory"` / `"n8_query_id"`, exige `store_version_token` y referencias verificables en `MemoryEngine`, descartando el bloque N8 de forma fail-closed si no puede acreditarse su vigencia.

4. **Semántica realista de revocación, concurrencia y frontera TOCTOU:**
   - **Antes de validar / durante revalidación:** las revocaciones, expiraciones, purgas o ediciones ocurridas antes de `_validate_n8_for_physical_dispatch()` (o detectadas en el post-check de token tras `get_memory`) son interceptadas antes del envío físico.
   - **Después del último check / proveedor en curso:** la comprobación de frescura ocurre inmediatamente antes del dispatch físico; **la revocación no es retroactiva ni instantánea sobre bytes ya enviados a una API cloud o subproceso en ejecución, ni puede retirar contenido que un modelo ya recibió.**
   - **Cero retención de locks durante inferencia o red:** `ExperienceStoreLock` y la conexión SQLite efímera se liberan completamente al terminar cada lectura puntual de `get_store_version_token()` / `get_memory()` / `retrieve()`, antes de iniciar cualquier llamada HTTP, socket local o subproceso CLI. Otros hilos escritores pueden revocar recuerdos concurrentemente sin quedar bloqueados por el proveedor LLM.

5. **Degradación segura y frontera estructural de proveedores:**
   - Si falla N8 (SQLite no disponible, token ilegible/inestable, snapshot ajeno o malformado) o se bloquean recuerdos: `BrainManager._assert_dispatch_isolation()` comprueba que `system_prompt` no contenga marcadores ni fragmentos N8 y que `prompt` base no retenga texto de recuerdos excluidos. Si el resto de la solicitud está limpio, el asistente **continúa normalmente sin contexto N8**; ante cualquier indicio de filtración o contaminación en roles privilegiados, **aborta de inmediato el despacho** (`BrainErrorKind.INVALID_REQUEST`, sin fallback y sin registrar texto del recuerdo).
   - En el punto efectivo de dispatch, `/privado --sin-historial` bloquea lecturas N8, impide crear `MemorySnapshot` (`MemoryPrivateModeError`) e ignora cualquier intento programático (`query_raw(..., allow_history=True)`).
   - **Frontera estructural por proveedor:**
     - `OllamaBrain`: separa `messages` con `role="system"` y bloque `role="user"` distinto para `untrusted_context`.
     - `GeminiBrain`: separa `system_instruction` en `GenerateContentConfig` frente a `contents`.
     - `ClaudeBrain`: separa campo raíz `"system"` frente al bloque `user` dentro de `"messages"`.
     - `OpenAIBrain`: separa `"instructions"` / `"role": "system"` frente a `"input"` / `"role": "user"`.
     - **`CodexBrain` (restricción CLI estricta y mitigación por sandbox, remediación F-02):** `codex exec` acepta un único argumento CLI posicional sin canal de rol `system` independiente en transporte; los delimitadores textuales **no** se asumen suficientes para aislar instrucciones cuando el agente tiene escritura. Por ello:
       1. En tareas de programación con `--sandbox workspace-write` (`run_coding_task`), `CodexBrain` **prohíbe y omite por completo** todo `untrusted_context` (N8, `web_extraction` u otro contenido externo).
       2. `classify_codex_task()` se ejecuta **una sola vez** exclusivamente sobre el `prompt` confiable del usuario antes de componer argumentos; los recuerdos no pueden alterar la clasificación ni disparar abortos falsos por palabras clave destructivas.
       3. El contexto no confiable solo se transporta separado hacia `_execute_codex_task(..., sandbox="read-only")` donde `--sandbox read-only` impone contención a nivel de proceso CLI, siempre precedido por `MemorySnapshotGuard`.

---

## 13. Remediación de Auditoría Independiente N8.3 (F-01 a F-09)

Los hallazgos identificados en la auditoría independiente de Antigravity Argon sobre `v5.4.0` (`3a868a8`) fueron remediados de forma integral y quedan **pendientes de verificación independiente**:

1. **F-01 — Canonización de `task_id` bajo exposición N8 (`goal_models.py`, `goal_memory.py`, `goal_planner.py`, `goal_supervisor.py`, `goal_persistence.py`):**
   - `GoalTask` aplica `SAFE_TASK_ID_RE` (`^[a-zA-Z0-9_-]{1,64}$`) sobre `task_id` y `depends_on` en toda creación y deserialización.
   - Cuando `n8_exposed=True` o `replan_saw_n8=True`, el motor **jamás** acepta `task_id` elegidos libremente por el LLM: genera identificadores canónicos secuenciales (`t1`, `t2`, … / `t_r{gen}_{idx}`) mediante `canonicalize_n8_task_id_map()`, reescribe `depends_on` con mapa biyectivo estricto, rechaza duplicados/dependencias rotas y aplica canonización de respaldo en `extract_persistable_plan_dict()` (rechazando mutar planes ya aprobados). Ningún fragmento del identificador original aparece en `.goal.json`, `.wal.jsonl`, `title`, `history` ni `metadata`.
2. **F-02 — Aislamiento de `CodexBrain` en `workspace-write` y clasificación única (`codex_client.py`, `brain_manager.py`):**
   - Prohibición estructural de inyectar `untrusted_context` (N8 o web) con `--sandbox workspace-write`, verificación en `_execute_codex_task()` y eliminación de la segunda llamada a `classify_codex_task()` sobre prompts concatenados.
3. **F-03 — Bloqueo de skills mutantes en modo privado y exclusión N8 en `document_export` (`goal_supervisor.py`, `skill_manager.py`):**
   - `_execute_registered_skill()` (y `SkillManager` como defensa en profundidad) bloquean `FILE_MUTATING_SKILLS`, skills con `modifies_files`/`mutates_files`, subclases y wrappers de `FileEditorSkill` y `DocumentExportSkill` durante `/privado` y `/privado --sin-historial`.
   - Se rastrea linaje transitivo `n8_tainted` y referencias `n8_refs_used_at_exec` en RAM a través del DAG: `document_export` excluye estructuralmente cualquier salida contaminada por N8 (incluso si el recuerdo sigue activo) y revalida referencias N8 del plan y del linaje upstream antes de escribir en disco, rechazando la exportación ante recuerdos revocados, expirados, modificados o no verificables.
4. **F-04 — Estado privado en `MemoryConsoleHandler` perezoso (`console.py`):**
   - `MemoryConsoleHandler` conserva `_private_mode` y `_allow_history` localmente sin crear `experiences.db` al activar `/privado` o `/privado --sin-historial`, y los propaga atómicamente al inicializar perezosamente `self.engine`.
5. **F-05 — Invalidaciones de rollback N5 durante modo privado y eliminación del latch privado en `BrainManager` (`engine.py`, `capture.py`, `brain_manager.py`, `main.py`):**
   - Un rollback N5 confirmado durante modo privado no escribe datos privados en disco ni pierde la invalidación: `ExperienceCaptureService` encola únicamente `(execution_id, reason, actor)` en RAM vía `MemoryEngine.queue_pending_rollback_invalidation()`, suprimiendo de inmediato sus recuerdos de `retrieve()`, `list_memories()` y `MemorySnapshotGuard` durante la sesión privada y drenándolos hacia SQLite al salir de `/privado` o al cerrar la sesión. Si el drenaje falla, `MemoryEngine` y `MemorySnapshotGuard` operan **fail-closed** hasta confirmar la invalidación.
   - `BrainManager._sync_and_resolve_privacy_flags()` elimina el *latch* unidireccional de `private_active`, permitiendo transiciones limpias `/privado` ↔ `/privado --sin-historial` ↔ `/privado off` y recuperación tras excepciones.
6. **F-06 — Inspección previa de firma sin `except TypeError:` sobre envíos (`brain_manager.py`, `goal_understanding.py`, `goal_planner.py`):**
   - Reemplazados todos los bloques `except TypeError:` envolviendo invocaciones a proveedores o `query_raw` por `_filter_supported_kwargs()` (`inspect.signature`), asegurando una única llamada compatible y evitando segundos dispatches no validados ante un `TypeError` interno del proveedor.
7. **F-07 — Consulta exacta de auditoría rollback sin `LIMIT 100` (`store.py`):**
   - `ExperienceStore._has_rollback_audit_tx()` emplea `json_valid(details_json) = 1 AND json_extract(details_json, '$.source_execution_id') = ?` con parámetros vinculados y respaldo exhaustivo en Python, eliminando el límite de 100 filas y las coincidencias ambiguas por prefijo o comodines `LIKE`.
8. **F-08 — Orden determinista de reconstrucción y frescura de sobres N8 (`goal_engine.py`, `goal_memory.py`):**
   - `_prepare_n8_context_for_execution()` comprueba primero si existe un sobre vivo en RAM; al reconstruir desde referencias persistidas, `revalidate_envelope()` obtiene el `store_version_token` real vigente con doble comprobación anti-TOCTOU (antes y después de `get_memory()`), excluyendo recuerdos revocados/expirados sin auto-sustitución y aplicando comportamiento **fail-closed** si el token cambia o es ilegible durante la reconstrucción.
9. **F-09 — Cableado perezoso único de `MemoryEngine` en consola y voz (`main.py`):**
   - `attach_existing_memory_engine()` enlaza perezosamente `MemoryEngine` a `BrainManager` antes de `process_turn()` en consola y en `run_voice_session()` únicamente si `N8_ENABLED` está activo y `experiences.db` ya existe (sin crearla al arrancar, sin duplicar instancias y sin importar módulos de voz/GPU en consola).
10. **Aislamiento de auditoría en pruebas (`skills/file_editor_skill.py`):**
    - `AuditLogger` redirige automáticamente `AUDIT_LOG_FILE` hacia `temp/jarvis_test_isolated/` cuando `JARVIS_ENV=test`/`testing`, evitando escrituras colaterales en `data/file_editor_audit.jsonl` durante la ejecución de pruebas.

### Ejecución de tests

```powershell
$env:JARVIS_ENV = "test"
.\venv\Scripts\python.exe -B -m unittest tests.test_memory_n8_audit_remediation -v
.\venv\Scripts\python.exe -B -m unittest tests.test_memory_n8_persistence tests.test_memory_n8_lifecycle tests.test_memory_n8_v2_persistence tests.test_memory_n8_capture_and_rollback tests.test_memory_n8_consultative_n6 tests.test_memory_n8_snapshot_guard tests.test_memory_n8_audit_remediation tests.test_goal_engine tests.test_brain_deliberation tests.test_llm_routing tests.test_codex_integration -v
.\venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_*.py" -q
```

---

## Proyecto J.A.R.V.I.S. — Reauditoría focalizada N8.3 F-01/F-04/F-05/F-09 (2026-11-26)

**Estado:** Remediación focalizada implementada y verificada con pruebas locales. N8.3 sigue pendiente de aprobación independiente final por Antigravity Argon.

### Causa raíz y correcciones

- **F-05 — rollback durable N5/N8:** la cola RAM no sobrevive a `os._exit` y no puede ser la fuente de verdad. `TransactionJournal.list_confirmed_rollback_execution_ids()` vuelve a leer los journals terminales `ROLLED_BACK`; `MemoryEngine.reconcile_confirmed_n5_rollbacks()` invalida solo `EXECUTION_VERIFIED` del `execution_id` confirmado y verifica `is_execution_invalidated()` antes de completar. La reconciliación corre en `ChainOrchestrator.recover_pending_transactions()` y, en la integración de aplicación, antes de entregar lecturas N8 mediante el proveedor read-only del journal. SQLite puede fallar por ejecución: quedan los estados `ROLLED_BACK` en N5 y cada lectura/recuperación vuelve a intentar; las demás ejecuciones siguen avanzando. Journals corruptos bloquean la lectura en lugar de asumir que no hay rollback.
- La reconciliación de seguridad se permite durante modo privado porque solo escribe revocaciones/tombstones ligados a IDs N5 confirmados; el modo privado continúa bloqueando captura nueva y no envía contenido de conversación al reconciliador. La cola RAM sigue dando supresión inmediata durante la sesión y es auxiliar, no durable.
- **F-01 — persistencia N6:** si el plan estuvo expuesto a N8, snapshots sustituyen mensajes del historial, errores/outputs de tareas y detalles narrativos por marcadores seguros. El WAL mantiene estados, códigos e IDs técnicos permitidos y sustituye el resto del payload. La prueba usa `N8_CANARY_F01_7F29A1` en un error arbitrario de skill tras retry y revisa todos los archivos generados en el GoalStore temporal.
- **F-04/F-09 — inicialización perezosa:** construir el handler de memoria no construye `MemoryEngine`; `/privado` mantiene sus flags en RAM. `main.py` adjunta un motor preexistente si ya hay DB, y usa una fábrica para crear una única instancia solo cuando un comando consulta N8. La captura automática N8 se crea al comenzar una ejecución de plan o al inicializar el orquestador para una operación que lo requiera. `/goal` sin ejecución y `/privado --sin-historial` no crean `experiences.db`; voz conserva su enlace a la instancia existente.

### Pruebas y límites comprobados

- Prueba adversarial F-05: el subproceso confirma rollback N5 y sale con `os._exit(99)` antes de invalidar SQLite; el proceso reiniciado repara antes de servir lecturas. Incluye dos ejecuciones revertidas, falla transitoria en una, recuperación de la otra, idempotencia y una ejecución `COMPLETED` que conserva recuerdos activos.
- Suite adversarial `tests.test_memory_n8_audit_remediation`: **12 pruebas en 33.600 s, OK**.
- Regresiones N8/N5/N6 seleccionadas: **188 pruebas en 158.869 s, OK**.
- Suite global aislada: **658 pruebas en 252.895 s, OK**.
- `py_compile` de los módulos modificados: **OK**; `git diff --check`: **OK**.
- La reconciliación vuelve a consultar el journal completo para priorizar recuperación ante estados terminales; en instalaciones con journals extensos, esa lectura tiene costo proporcional a la cantidad de transacciones. No se cambia el esquema de N8 ni se crea un journal paralelo.
- Pruebas físicas de micrófono/altavoz no ejecutadas; el flujo de voz fue verificado en la suite con dobles de prueba.
