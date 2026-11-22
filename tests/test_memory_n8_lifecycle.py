"""
Suite aislada e integral de pruebas para N8.2:
recuperación determinista, API central, comandos de consola, aislamiento real de modo privado,
importación manual, purga física, backups/restauración y concurrencia multiproceso real
(`writer-vs-purge` y `writer-vs-restore`).

Aislamiento obligatorio:
- Se ejecuta bajo `JARVIS_ENV=test` usando `temp/jarvis_test_isolated/`.
- Jamás lee, escribe ni modifica `data/memory_store.json`, `data/experiences.db` ni `data/backups/`.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

from config import settings
from src.brain.brain_manager import BrainManager
from src.brain.memory import ConversationMemory
from src.memory import (
    ExecutionVerificationEvidence,
    ExperienceStore,
    ExperienceStoreLock,
    MemoryCategory,
    MemoryConsoleHandler,
    MemoryCreateInput,
    MemoryEngine,
    MemoryLockTimeoutError,
    MemoryNotFoundError,
    MemoryPolicy,
    MemoryPolicyError,
    MemoryPrivateModeError,
    MemoryRestoreError,
    MemoryRetrievalTarget,
    MemoryRetriever,
    MemoryScopeContext,
    MemoryScopeType,
    MemorySource,
    MemoryStatus,
    MemoryValidationError,
    resolve_test_sandbox_root,
    utc_now_iso,
)
from src.tools.skill_manager import SkillManager
from src.utils.git_guard import GitSafetyGuard, is_sensitive_path


PROJECT_ROOT = settings.BASE_DIR.resolve()
REAL_MEMORY_JSON = PROJECT_ROOT / "data" / "memory_store.json"
REAL_EXPERIENCES_DB = PROJECT_ROOT / "data" / "experiences.db"
REAL_BACKUPS_DIR = PROJECT_ROOT / "data" / "backups"


def _sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().upper()


class _FakeDummyLLM:
    """LLM determinista en memoria para validar aislamiento y canales de contexto sin red."""

    def __init__(self, reply: str = "Respuesta controlada del modelo.") -> None:
        self.reply = reply
        self.calls: list[dict[str, str | None]] = []
        self.model_name = "mock-local-3b"

    def generate_response(
        self,
        prompt: str,
        system_prompt: str | None = None,
        *,
        untrusted_context: str | None = None,
        **kwargs,
    ) -> str:
        self.calls.append(
            {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "untrusted_context": untrusted_context,
            }
        )
        return self.reply


class N8LifecycleAndPrivacyTests(unittest.TestCase):
    """Cobertura completa N8.2 bajo aislamiento estricto de pruebas."""

    _prev_jarvis_env: str | None = None
    _baseline_json_sha: str | None = None
    _baseline_real_db_exists: bool = False
    _baseline_backups_exists: bool = False

    @classmethod
    def setUpClass(cls) -> None:
        cls._prev_jarvis_env = os.environ.get("JARVIS_ENV")
        os.environ["JARVIS_ENV"] = "test"
        cls._baseline_json_sha = _sha256_file(REAL_MEMORY_JSON)
        cls._baseline_real_db_exists = REAL_EXPERIENCES_DB.exists()
        cls._baseline_backups_exists = REAL_BACKUPS_DIR.exists()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._prev_jarvis_env is None:
            os.environ.pop("JARVIS_ENV", None)
        else:
            os.environ["JARVIS_ENV"] = cls._prev_jarvis_env
        assert _sha256_file(REAL_MEMORY_JSON) == cls._baseline_json_sha
        assert REAL_EXPERIENCES_DB.exists() == cls._baseline_real_db_exists
        assert REAL_BACKUPS_DIR.exists() == cls._baseline_backups_exists

    def setUp(self) -> None:
        sandbox_root = resolve_test_sandbox_root()
        sandbox_root.mkdir(parents=True, exist_ok=True)
        self.work_dir = sandbox_root / f"n82_case_{uuid4().hex[:10]}"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.work_dir / "experiences.db"
        self.backup_dir = self.work_dir / "backups" / "memory"
        self.store = ExperienceStore(self.db_path)
        self.engine = MemoryEngine(store=self.store, backup_dir=self.backup_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.assertEqual(_sha256_file(REAL_MEMORY_JSON), self._baseline_json_sha)
        self.assertEqual(REAL_EXPERIENCES_DB.exists(), self._baseline_real_db_exists)
        self.assertEqual(REAL_BACKUPS_DIR.exists(), self._baseline_backups_exists)

    # ------------------------------------------------------------------
    # 1. Recuperación determinista, ranking, ámbitos, estados y presupuestos
    # ------------------------------------------------------------------

    def test_retrieval_fts5_sql_fallback_scopes_status_and_budgets(self) -> None:
        """Valida FTS5, respaldo SQL, exclusión de estados, ámbitos y presupuestos N8.2."""
        # Recuerdo global activo
        global_mem = self.engine.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.PREFERENCE,
                source=MemorySource.USER_EXPLICIT,
                title="Compilación Python global",
                content="Usar siempre entorno virtual pytest y unittest para compilar Python.",
                scope_type=MemoryScopeType.GLOBAL,
                importance=60,
                confidence=0.80,
            )
        ).record

        # Recuerdo de proyecto exacto verificado (debe rankear por encima de GLOBAL)
        ev = ExecutionVerificationEvidence(
            source_layer="N5_CHAIN_ORCHESTRATOR",
            source_execution_id="exec_n82_1",
            source_plan_id="plan_n82_1",
            source_goal_id="goal_n82_1",
            source_task_id="task_n82_1",
            approval_hash="c" * 64,
            executor_component="FileEditorSkill",
            operation_type="file_write",
            execution_outcome="verified",
            expected_outcome="Tests Python verificados",
            verifier_component="ChainOrchestrator",
            verified_at=utc_now_iso(),
            verified_artifacts=("temp/jarvis_test_isolated/verify.txt",),
            scope_type=MemoryScopeType.PROJECT,
            scope_key="jarvis-core",
        )
        proj_mem = self.engine.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.EXPERIENCE,
                source=MemorySource.EXECUTION_VERIFIED,
                title="Suite Python en Jarvis",
                content="La suite Python de jarvis-core valida unittest y compilar sin errores.",
                scope_type=MemoryScopeType.PROJECT,
                scope_key="jarvis-core",
                importance=90,
                confidence=0.95,
                verification_evidence=ev,
            )
        ).record

        # Otro proyecto distinto (debe quedar excluido por scope_ctx de jarvis-core)
        other_proj = self.engine.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.FACT,
                source=MemorySource.USER_EXPLICIT,
                title="Suite Python en Otro Proyecto",
                content="El proyecto externo tambien usa python para compilar.",
                scope_type=MemoryScopeType.PROJECT,
                scope_key="otro-repo",
            )
        ).record

        # Recuerdo revocado (debe excluirse siempre)
        revoked_mem = self.engine.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.FACT,
                source=MemorySource.USER_EXPLICIT,
                title="Nota Python obsoleta",
                content="Python 2.7 usado para compilar antiguamente.",
                scope_type=MemoryScopeType.GLOBAL,
            )
        ).record
        self.engine.revoke_memory(revoked_mem.memory_id, reason="Obsoleto")

        # Recuerdo expirado por fecha pasada (debe excluirse)
        past_iso = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        self.engine.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.FACT,
                source=MemorySource.USER_EXPLICIT,
                title="Python temporal expirado",
                content="Token temporal para compilar Python expirado.",
                scope_type=MemoryScopeType.GLOBAL,
                expires_at=past_iso,
            )
        )

        # Recuerdo pendiente de confirmación (debe excluirse hasta confirmar)
        pending_mem = self.engine.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.FACT,
                source=MemorySource.USER_EXPLICIT,
                title="Python pendiente",
                content="Nota pendiente de confirmar sobre compilar Python.",
                scope_type=MemoryScopeType.GLOBAL,
                pending_confirmation=True,
            )
        ).record
        self.assertTrue(pending_mem.is_pending_confirmation)

        ctx = MemoryScopeContext(project_key="jarvis-core", include_global=True)
        res_fts = self.engine.retrieve("compilar python suite", target=MemoryRetrievalTarget.NORMAL, context_scope=ctx)
        res_fts_2 = self.engine.retrieve("compilar python suite", target=MemoryRetrievalTarget.NORMAL, context_scope=ctx)
        self.assertEqual([i.record.memory_id for i in res_fts.items], [i.record.memory_id for i in res_fts_2.items])
        self.assertEqual(res_fts.backend_used, "fts5")

        ids_fts = [i.record.memory_id for i in res_fts.items]
        self.assertEqual(ids_fts[0], proj_mem.memory_id)
        self.assertIn(global_mem.memory_id, ids_fts)
        self.assertNotIn(other_proj.memory_id, ids_fts)
        self.assertNotIn(revoked_mem.memory_id, ids_fts)
        self.assertNotIn(pending_mem.memory_id, ids_fts)

        # Respaldo SQL explícito conserva el mismo filtrado.
        res_sql = self.engine.retrieve(
            "compilar python suite",
            target=MemoryRetrievalTarget.NORMAL,
            context_scope=ctx,
            force_sql_fallback=True,
        )
        self.assertEqual(res_sql.backend_used, "sql_like")
        self.assertIn(proj_mem.memory_id, [i.record.memory_id for i in res_sql.items])
        self.assertNotIn(other_proj.memory_id, [i.record.memory_id for i in res_sql.items])

        # Confirmar recuerdo pendiente lo vuelve recuperable.
        self.engine.confirm_memory(pending_mem.memory_id)
        res_after_confirm = self.engine.retrieve("pendiente compilar python", context_scope=ctx)
        self.assertIn(pending_mem.memory_id, [i.record.memory_id for i in res_after_confirm.items])

        # Sobre consultivo marcado como no confiable.
        advisory = json.loads(res_fts.untrusted_advisory_json)
        self.assertEqual(advisory["trust_level"], "untrusted_long_term_memory")
        self.assertFalse(advisory["allow_system_prompt_promotion"])
        self.assertFalse(advisory["allow_permission_grant"])

    def test_retrieval_budgets_tokens_invalid_queries_and_no_experience_merging(self) -> None:
        """Verifica presupuestos 5/1500 vs 3/600, límite de tokens, consultas inválidas y no fusión."""
        # Creamos 7 experiencias parecidas sobre "shader vulkan" — ninguna debe fusionarse.
        for idx in range(7):
            self.engine.create_memory(
                MemoryCreateInput(
                    category=MemoryCategory.EXPERIENCE,
                    source=MemorySource.USER_EXPLICIT,
                    title=f"Shader Vulkan ensayo número {idx}",
                    content=f"Configuración de shader vulkan variante {idx}: compilación estable sin stutter {'x' * 120}.",
                    scope_type=MemoryScopeType.APP_OR_GAME,
                    scope_key=f"game-{idx}",
                    importance=60 + idx,
                )
            )

        normal = self.engine.retrieve("shader vulkan", target=MemoryRetrievalTarget.NORMAL)
        self.assertLessEqual(normal.returned_count, 5)
        self.assertLessEqual(normal.total_chars, 1500)
        self.assertEqual(normal.budget_max_items, 5)
        self.assertEqual(normal.budget_max_chars, 1500)
        self.assertEqual(normal.total_candidates, 7)
        self.assertTrue(normal.truncated_by_budget)
        # Cada ítem preserva su memory_id único.
        self.assertEqual(len({i.record.memory_id for i in normal.items}), normal.returned_count)

        voice = self.engine.retrieve("shader vulkan", target=MemoryRetrievalTarget.VOICE)
        local_3b = self.engine.retrieve("shader vulkan", target=MemoryRetrievalTarget.LOCAL_3B)
        self.assertLessEqual(voice.returned_count, 3)
        self.assertLessEqual(voice.total_chars, 600)
        self.assertLessEqual(local_3b.returned_count, 3)
        self.assertLessEqual(local_3b.total_chars, 600)

        # Límite de tokens de consulta.
        many_words = " ".join(f"palabra{i}" for i in range(25))
        tokens = MemoryRetriever.tokenize_query(many_words)
        self.assertEqual(len(tokens), settings.N8_SEARCH_MAX_TOKENS)

        # Consultas inválidas o vacías.
        with self.assertRaises(MemoryValidationError):
            self.engine.retrieve(12345)  # type: ignore[arg-type]
        with self.assertRaises(MemoryValidationError):
            self.engine.retrieve("bad\x00query")
        empty_res = self.engine.retrieve("   ... !!! ")
        self.assertEqual(empty_res.returned_count, 0)

    # ------------------------------------------------------------------
    # 2. Modo privado — Aislamiento real end-to-end
    # ------------------------------------------------------------------

    def test_private_mode_real_isolation_option_a_and_no_history_and_post_exit(self) -> None:
        """
        Verifica buffers privados separados, bloqueo incondicional de auto_save/N8,
        opción `--sin-historial`, y prueba crítica de no persistencia tras salir de `/privado`.
        """
        legacy_path = self.work_dir / "conv_memory.json"
        conv_mem = ConversationMemory(max_turns=8, storage_file=str(legacy_path), auto_save=True)
        conv_mem.add_message("user", "Mensaje histórico normal antes de privado.")
        conv_mem.add_message("assistant", "Entendido, guardado en historial previo.")
        conv_mem.set_preference("nombre_usuario", "Tiziano")

        hist_n8 = self.engine.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.FACT,
                source=MemorySource.USER_EXPLICIT,
                title="Proyecto principal",
                content="Proyecto jarvis-core ubicado en escritorio local.",
                scope_type=MemoryScopeType.GLOBAL,
            )
        ).record

        llm = _FakeDummyLLM(reply="Respuesta privada en RAM.")
        skill_mgr = SkillManager()
        bm = BrainManager(
            initial_mode="OLLAMA",
            ollama_brain=llm,  # type: ignore[arg-type]
            skill_manager=skill_mgr,
            memory=conv_mem,
            memory_engine=self.engine,
            lazy_init=True,
        )
        console = MemoryConsoleHandler(
            memory_engine=self.engine,
            conversation_memory=conv_mem,
            skill_manager=skill_mgr,
        )

        outputs: list[str] = []
        # Entrar a /privado (Opción A: lectura histórica permitida, escritura bloqueada).
        console.handle_command("/privado", output_fn=outputs.append)
        self.assertTrue(conv_mem.is_private_mode)
        self.assertTrue(self.engine.is_private_mode)
        self.assertTrue(self.engine.allow_history_reads)

        # Escrituras N8 bloqueadas explícitamente en modo privado.
        with self.assertRaises(MemoryPrivateModeError):
            self.engine.create_memory(
                MemoryCreateInput(
                    category=MemoryCategory.FACT,
                    source=MemorySource.USER_EXPLICIT,
                    title="Secreto temporal",
                    content="No debe persistirse en SQLite.",
                )
            )
        with self.assertRaises(MemoryPrivateModeError):
            self.engine.correct_memory(hist_n8.memory_id, content="Edición prohibida en privado")
        with self.assertRaises(MemoryPrivateModeError):
            self.engine.revoke_memory(hist_n8.memory_id, reason="Prohibido en privado")
        with self.assertRaises(MemoryPrivateModeError):
            self.engine.purge_memory(hist_n8.memory_id, confirmed=True)
        with self.assertRaises(MemoryPrivateModeError):
            self.engine.create_backup()

        # Bloqueo incondicional de _save_to_disk() mientras _private_mode esté activo.
        self.assertFalse(conv_mem._save_to_disk())
        self.assertFalse(conv_mem.save_to_disk())

        # Conversación privada + external reference + preferencia privada quedan solo en buffers privados.
        conv_mem.set_preference("clave_temporal_privada", "solo_ram_privado")
        conv_mem.add_external_reference("Resumen web privado temporal", tool_name="web_extraction")
        turn_priv = bm.process_turn("Consulta privada confidencial XYZ-999 sobre jarvis-core")
        self.assertFalse(turn_priv.is_error)
        # Opción A sí pudo consultar recuerdo previo de N8 en canal untrusted.
        self.assertIn("Proyecto principal", llm.calls[-1]["untrusted_context"] or "")
        # El contenido consultivo jamás entra al system_prompt como instrucción privilegiada.
        self.assertNotIn("Proyecto principal", llm.calls[-1]["system_prompt"] or "")

        self.assertTrue(any("XYZ-999" in m["content"] for m in conv_mem._private_messages))
        self.assertIn("clave_temporal_privada", conv_mem._private_metadata)
        self.assertEqual(len(conv_mem._private_external_references), 1)

        # Verificamos en disco que el JSON NO contiene ningún dato privado.
        disk_raw_during = legacy_path.read_text(encoding="utf-8")
        self.assertNotIn("XYZ-999", disk_raw_during)
        self.assertNotIn("solo_ram_privado", disk_raw_during)

        # Cambiamos a `/incognito --sin-historial`: bloquea lecturas de N8 y oculta historial previo.
        console.handle_command("/incognito --sin-historial", output_fn=outputs.append)
        self.assertTrue(conv_mem.is_private_mode)
        self.assertFalse(conv_mem.private_allow_history)
        self.assertFalse(self.engine.allow_history_reads)

        res_no_hist = self.engine.retrieve("jarvis-core")
        self.assertTrue(res_no_hist.blocked_by_history_policy)
        self.assertEqual(res_no_hist.returned_count, 0)

        llm.calls.clear()
        bm.process_turn("Pregunta aislada SIGMA-777 sin historial")
        self.assertNotIn("Mensaje histórico normal", llm.calls[-1]["system_prompt"] or "")
        self.assertNotIn("Tiziano", llm.calls[-1]["system_prompt"] or "")
        self.assertIsNone(llm.calls[-1]["untrusted_context"])

        # Salida del modo privado + reactivación normal con auto_save=True -> Prueba crítica post-salida.
        console.handle_command("/privado", output_fn=outputs.append)
        self.assertFalse(conv_mem.is_private_mode)
        self.assertFalse(self.engine.is_private_mode)
        self.assertEqual(len(conv_mem._private_messages), 0)
        self.assertEqual(len(conv_mem._private_metadata), 0)
        self.assertEqual(len(conv_mem._private_external_references), 0)

        conv_mem.add_message("user", "Mensaje normal posterior a salir de privado.")
        conv_mem.add_message("assistant", "Respuesta normal posterior confirmada.")

        disk_final = legacy_path.read_text(encoding="utf-8")
        self.assertIn("Mensaje histórico normal antes de privado.", disk_final)
        self.assertIn("Mensaje normal posterior a salir de privado.", disk_final)
        self.assertNotIn("XYZ-999", disk_final)
        self.assertNotIn("SIGMA-777", disk_final)
        self.assertNotIn("solo_ram_privado", disk_final)
        self.assertNotIn("Respuesta privada en RAM.", disk_final)

    def test_private_mode_no_log_or_exception_leaks_and_blocks_persistent_commands(self) -> None:
        """
        Skills, IntentRouter y main.run_console_session no filtran texto privado en logs/trazas
        y rechazan operaciones persistentes (/optimizar, /goal, /rollback) durante modo privado.
        """
        conv_mem = ConversationMemory(
            max_turns=4,
            storage_file=str(self.work_dir / "conv_noleak.json"),
            auto_save=True,
        )
        conv_mem.enter_private_mode(allow_history=False)

        skill_mgr = SkillManager()
        skill_mgr.set_private_mode(True)

        from skills.base_skill import BaseSkill

        class _BoomSkill(BaseSkill):
            def __init__(self) -> None:
                super().__init__(
                    name="boom_privado",
                    description="Skill de prueba que falla",
                    parameters={"texto": "str"},
                )

            def execute(self, texto: str = "") -> str:
                raise RuntimeError(f"BOOM con argumento privado={texto}")

        skill_mgr.register_skill(_BoomSkill())

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            skill_mgr.execute_skill("boom_privado", texto="DATO_CONFIDENCIAL_ALPHA_42")
        logged = buf.getvalue()
        self.assertNotIn("DATO_CONFIDENCIAL_ALPHA_42", logged)
        self.assertIn("modo privado", logged.lower())

        # Verificar que IntentRouter tampoco filtra raw_response privado en error/fallback.
        class _BrokenJsonLLM:
            def generate_response(self, prompt: str, system_prompt: str | None = None) -> str:
                return "{broken json con DATO_CONFIDENCIAL_BETA_88"

        from src.brain.intent_router import IntentRouter

        router = IntentRouter(llm=_BrokenJsonLLM(), tools=skill_mgr.get_skills_manifest(), memory=conv_mem)
        buf_router = io.StringIO()
        with patch("sys.stdout", buf_router):
            router.analyze_intent("Analizar DATO_CONFIDENCIAL_BETA_88")
        router_log = buf_router.getvalue()
        self.assertNotIn("DATO_CONFIDENCIAL_BETA_88", router_log)

        # En run_console_session, `/optimizar` y `/rollback` se rechazan en `/privado`, mientras
        # `/goal` opera exclusivamente sobre EphemeralGoalStore en RAM sin tocar disco y se descarta al salir.
        from main import run_console_session

        bm = BrainManager(initial_mode="OLLAMA", ollama_brain=_FakeDummyLLM(), skill_manager=skill_mgr, memory=conv_mem)
        inputs = iter(["/optimizar CyberGame", "/rollback exec-1", "/goal diagnosticar uso de cpu y ram", "/privado off", "/plan", "salir"])
        out_lines: list[str] = []
        case_tmp = self.work_dir / "console_priv_isolated"
        case_tmp.mkdir(parents=True, exist_ok=True)
        with patch.dict(os.environ, {"JARVIS_TEST_TEMP_ROOT": str(case_tmp)}):
            run_console_session(
                bm,
                enable_tts=False,
                input_fn=lambda _: next(inputs),
                output_fn=out_lines.append,
                memory_engine=self.engine,
            )
        joined_out = "\n".join(out_lines)
        self.assertIn("operación '/optimizar' rechazada durante el modo privado", joined_out.lower())
        self.assertIn("operación '/rollback' rechazada durante el modo privado", joined_out.lower())
        self.assertIn("ephemeralgoalstore", joined_out.lower())
        self.assertIn("no hay ningún objetivo activo actualmente", joined_out.lower())
        self.assertIn("salga de `/privado`", joined_out.lower())
        goals_dir = case_tmp / "goals"
        if goals_dir.exists():
            self.assertEqual(list(goals_dir.rglob("*.goal.json")), [])
            self.assertEqual(list(goals_dir.rglob("*.wal.jsonl")), [])

    # ------------------------------------------------------------------
    # 3. Importación manual de preferencias (segura y sin tocar original)
    # ------------------------------------------------------------------

    def test_manual_preference_import_workflow_and_isolation(self) -> None:
        """Prueba JSON válido, corrupto, estructura inesperada, duplicados, conflictos y aislamiento."""
        # Aislamiento obligatorio en JARVIS_ENV=test: rechaza ruta real `data/memory_store.json`.
        with self.assertRaises(MemoryPolicyError):
            self.engine.preview_preferences_import(None)
        with self.assertRaises(MemoryPolicyError):
            self.engine.preview_preferences_import(REAL_MEMORY_JSON)

        # Sembramos una preferencia duplicada y otra conflictiva en N8.
        self.engine.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.PREFERENCE,
                source=MemorySource.USER_EXPLICIT,
                title="Preferencia: idioma",
                content="Preferencia histórica 'idioma': español",
                scope_type=MemoryScopeType.GLOBAL,
                metadata=(("preference_key", "idioma"),),
            )
        )
        self.engine.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.PREFERENCE,
                source=MemorySource.USER_EXPLICIT,
                title="Preferencia: tema_editor",
                content="Preferencia histórica 'tema_editor': claro",
                scope_type=MemoryScopeType.GLOBAL,
                metadata=(("preference_key", "tema_editor"),),
            )
        )

        # JSON válido en carpeta temporal aislada.
        sk_dummy = "sk-" + "proj-1234567890abcdef"
        legacy_copy = self.work_dir / "legacy_prefs_copy.json"
        legacy_payload = {
            "version": 1,
            "messages": [
                {"role": "user", "content": "Esto es charla suelta y no debe importarse como preferencia"}
            ],
            "preferences": {
                "idioma": "español",              # DUPLICATE
                "tema_editor": "oscuro",          # CONFLICT
                "ciudad": "Córdoba",              # NEW
                "api_key": "no_importar",         # REJECTED (clave sensible)
                "nota_secreta": sk_dummy,         # REJECTED (patrón token)
                "objeto_complejo": {"a": 1},      # REJECTED (no escalar)
                "inferred_mood": "alegre",        # REJECTED (marcador de inferencia)
            },
        }
        legacy_copy.write_text(json.dumps(legacy_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        orig_sha = _sha256_file(legacy_copy)

        preview = self.engine.preview_preferences_import(legacy_copy)
        self.assertTrue(preview.valid)
        self.assertEqual([i.key for i in preview.new_items], ["ciudad"])
        self.assertEqual([i.key for i in preview.duplicates], ["idioma"])
        self.assertEqual([i.key for i in preview.conflicts], ["tema_editor"])
        self.assertEqual(len(preview.rejected), 4)

        # Cancelación mediante consola no modifica nada.
        console = MemoryConsoleHandler(memory_engine=self.engine)
        cancel_out: list[str] = []
        cancel_inputs = iter(["n"])
        console.handle_command(
            f'/memoria importar-preferencias "{legacy_copy}"',
            input_fn=lambda _: next(cancel_inputs),
            output_fn=cancel_out.append,
        )
        self.assertEqual(self.engine.get_stats().active_memories, 2)

        # Confirmación conservadora (conflictos omitidos).
        ok_res = self.engine.commit_preferences_import(legacy_copy, confirmed=True, resolve_conflicts="skip")
        self.assertEqual(ok_res.imported_count, 1)
        self.assertEqual(ok_res.skipped_duplicates, 1)
        self.assertEqual(ok_res.conflicts_skipped, 1)
        self.assertEqual(ok_res.rejected_count, 4)

        # Confirmación resolviendo conflictos (sustituye tema_editor: claro -> oscuro).
        sup_res = self.engine.commit_preferences_import(legacy_copy, confirmed=True, resolve_conflicts="supersede")
        self.assertEqual(sup_res.conflicts_resolved, 1)
        # Archivo JSON origen nunca fue modificado.
        self.assertEqual(_sha256_file(legacy_copy), orig_sha)

        # JSON corrupto y estructura inesperada rechazados limpiamente.
        corrupt_json = self.work_dir / "corrupt.json"
        corrupt_json.write_text("{json_malformado", encoding="utf-8")
        p_corrupt = self.engine.preview_preferences_import(corrupt_json)
        self.assertFalse(p_corrupt.valid)

        unexpected_json = self.work_dir / "unexpected.json"
        unexpected_json.write_text('{"other_field": [1, 2, 3]}', encoding="utf-8")
        p_unexp = self.engine.preview_preferences_import(unexpected_json)
        self.assertFalse(p_unexp.valid)

    # ------------------------------------------------------------------
    # 4. Purga física, backups y restauración atómica
    # ------------------------------------------------------------------

    def test_physical_purge_clears_fts_revisions_and_audit_content(self) -> None:
        """Purga física elimina FTS5, revisiones, auditoría de ese recuerdo y mantiene integridad."""
        created = self.engine.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.FACT,
                source=MemorySource.USER_EXPLICIT,
                title="Dato sensible temporal del usuario",
                content="Dirección temporal privada XYZZY_PURGE_TOKEN_987 para prueba.",
                scope_type=MemoryScopeType.GLOBAL,
            )
        ).record
        self.engine.correct_memory(
            created.memory_id,
            content="Dirección actualizada privada XYZZY_PURGE_TOKEN_REV2 para prueba.",
            reason="Corrección previa a purga",
        )

        # Sin confirmación explícita se rechaza.
        with self.assertRaises(MemoryPolicyError):
            self.engine.purge_memory(created.memory_id, confirmed=False)

        purge_res = self.engine.purge_memory(created.memory_id, confirmed=True, vacuum=True)
        self.assertTrue(purge_res.purged)
        self.assertEqual(purge_res.revisions_deleted, 1)
        self.assertTrue(purge_res.wal_checkpointed)
        self.assertTrue(purge_res.vacuum_executed)
        self.assertIn("SSD", purge_res.limitations_notice)

        self.assertIsNone(self.engine.get_memory(created.memory_id))
        self.assertEqual(self.engine.get_revisions(created.memory_id), [])
        self.assertEqual(self.engine.retrieve("XYZZY_PURGE_TOKEN_987").returned_count, 0)
        self.assertEqual(self.engine.retrieve("XYZZY_PURGE_TOKEN_REV2").returned_count, 0)

        # Verificamos directamente en SQLite que no quede rastro textual ni en FTS5 ni en audit/revisions.
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_fts WHERE memory_id = ?", (created.memory_id,)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_revisions WHERE memory_id = ?", (created.memory_id,)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_audit WHERE memory_id = ?", (created.memory_id,)).fetchone()[0], 0)
        self.assertTrue(self.engine.verify_integrity().ok)

    def test_backup_retention_verification_and_safe_restore(self) -> None:
        """Backups verificados con SHA-256, retención máxima=5 y restauración atómica ante corrupción."""
        # Creamos recuerdos base y backups hasta superar retención=3.
        self.engine.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.DECISION,
                source=MemorySource.DECISION_CONFIRMED,
                title="Backup Base",
                content="Estado base verificable antes de respaldos.",
            )
        )
        backups = []
        for idx in range(4):
            self.engine.create_memory(
                MemoryCreateInput(
                    category=MemoryCategory.FACT,
                    source=MemorySource.USER_EXPLICIT,
                    title=f"Hecho paso {idx}",
                    content=f"Registro persistido paso número {idx}.",
                )
            )
            backups.append(self.engine.create_backup(retention=3))

        listed = self.engine.list_backups()
        self.assertEqual(len(listed), 3)
        latest_bkp = listed[0]
        self.assertEqual(_sha256_file(Path(latest_bkp.backup_path)).lower(), latest_bkp.sha256)

        # Si el backup falla por directorio prohibido o hash manipulado, no destruye previos ni DB actual.
        tampered_bkp = Path(listed[-1].backup_path)
        tampered_bytes = bytearray(tampered_bkp.read_bytes())
        tampered_bytes[100] ^= 0xFF
        tampered_bkp.write_bytes(bytes(tampered_bytes))
        with self.assertRaises(MemoryRestoreError):
            self.engine.restore_backup(listed[-1].backup_id, confirmed=True)
        self.assertEqual(self.engine.get_stats().active_memories, 5)

        # Corrompemos la DB activa y verificamos que restore desde backup íntegro rescate todo y preserve previa.
        self.db_path.write_bytes(b"CORRUPTED_DB_PAYLOAD_FOR_TEST" + b"\x00" * 512)
        restore_res = self.engine.restore_backup(latest_bkp.backup_id, confirmed=True)
        self.assertTrue(restore_res.restored)
        self.assertTrue(restore_res.integrity_report.ok)
        self.assertGreaterEqual(len(restore_res.preserved_previous_paths), 1)
        self.assertTrue(all(Path(p).exists() for p in restore_res.preserved_previous_paths))
        self.assertEqual(self.engine.get_stats().active_memories, 5)

    # ------------------------------------------------------------------
    # 5. Concurrencia multiproceso REAL obligatoria (writer-vs-purge y writer-vs-restore)
    # ------------------------------------------------------------------

    def test_multiprocess_writer_vs_purge_real_exclusion(self) -> None:
        """
        Subproceso escritor repite escrituras mientras otro subproceso purga físicamente registros
        existentes usando el protocolo común `ExperienceStoreLock`.
        Valida exclusión real, comportamiento de timeout, cero estados parciales e integridad final.
        """
        seed_ids: list[str] = []
        for idx in range(6):
            rec = self.engine.create_memory(
                MemoryCreateInput(
                    category=MemoryCategory.FACT,
                    source=MemorySource.USER_EXPLICIT,
                    title=f"Objetivo purga {idx}",
                    content=f"Registro candidato a purga multiproceso índice {idx}.",
                )
            ).record
            seed_ids.append(rec.memory_id)

        writer_script = self.work_dir / "mp_write_purge.py"
        purge_script = self.work_dir / "mp_purge_worker.py"
        child_env = {**os.environ, "JARVIS_ENV": "test", "PYTHONPATH": str(PROJECT_ROOT)}

        writer_script.write_text(
            """
import os
import sys
import time

os.environ["JARVIS_ENV"] = "test"
from src.memory import ExperienceStore, MemoryCategory, MemoryCreateInput, MemorySource

db_path = sys.argv[1]
store = ExperienceStore(db_path, lock_timeout_sec=12.0, auto_initialize=False)
for idx in range(8):
    res = store.create_memory(
        MemoryCreateInput(
            category=MemoryCategory.EXPERIENCE,
            source=MemorySource.USER_EXPLICIT,
            title=f"Writer concurrente purge {idx}",
            content=f"Escritura concurrente número {idx} durante purga externa.",
        )
    )
    assert res.created
    time.sleep(0.015)
print("WRITER_OK", flush=True)
""".strip(),
            encoding="utf-8",
        )

        purge_script.write_text(
            """
import json
import os
import sys
import time

os.environ["JARVIS_ENV"] = "test"
from src.memory import ExperienceStore

db_path = sys.argv[1]
targets = json.loads(sys.argv[2])
store = ExperienceStore(db_path, lock_timeout_sec=12.0, auto_initialize=False)
purged = 0
for mem_id in targets:
    res = store.purge_memory(mem_id, reason="Purga concurrente", actor="purge_proc")
    assert res.purged
    purged += 1
    time.sleep(0.015)
print(f"PURGED_{purged}", flush=True)
""".strip(),
            encoding="utf-8",
        )

        # Fase A: exclusión serializada sin pérdida ni corrupción.
        procs: list[subprocess.Popen[str]] = []
        results: list[tuple[int, str, str]] = []
        try:
            procs.append(
                subprocess.Popen(
                    [sys.executable, str(writer_script), str(self.db_path)],
                    cwd=str(PROJECT_ROOT),
                    env=child_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
            procs.append(
                subprocess.Popen(
                    [sys.executable, str(purge_script), str(self.db_path), json.dumps(seed_ids[:4])],
                    cwd=str(PROJECT_ROOT),
                    env=child_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        finally:
            for p in procs:
                out, err = p.communicate(timeout=45)
                results.append((p.returncode, out, err))

        for code, out, err in results:
            self.assertEqual(code, 0, msg=f"Subproceso falló.\nSTDOUT:{out}\nSTDERR:{err}")

        stats = self.engine.get_stats()
        self.assertEqual(stats.active_memories, 10)  # 6 iniciales - 4 purgados + 8 escritos
        for pid in seed_ids[:4]:
            self.assertIsNone(self.engine.get_memory(pid))
        self.assertEqual(self.engine.verify_integrity().integrity_check_result.lower(), "ok")

        # Fase B: exclusión por timeout cuando un escritor sostiene el lock y otro proceso intenta purgar.
        hold_writer = self.work_dir / "mp_hold_writer.py"
        hold_writer.write_text(
            """
import os
import sys
import time

os.environ["JARVIS_ENV"] = "test"
from src.memory import ExperienceStore, MemoryCategory, MemoryCreateInput, MemorySource

db_path = sys.argv[1]
store = ExperienceStore(db_path, lock_timeout_sec=5.0, auto_initialize=False)
with store.acquire_lock():
    store.create_memory(
        MemoryCreateInput(
            category=MemoryCategory.FACT,
            source=MemorySource.USER_EXPLICIT,
            title="Escritor reteniendo lock",
            content="Sostiene ExperienceStoreLock para verificar exclusión de purga.",
        )
    )
    print("WRITER_LOCK_HELD", flush=True)
    time.sleep(1.5)
print("WRITER_LOCK_RELEASED", flush=True)
""".strip(),
            encoding="utf-8",
        )
        p_hold = subprocess.Popen(
            [sys.executable, str(hold_writer), str(self.db_path)],
            cwd=str(PROJECT_ROOT),
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert p_hold.stdout is not None
            self.assertEqual(p_hold.stdout.readline().strip(), "WRITER_LOCK_HELD")
            contending_purge = ExperienceStore(self.db_path, lock_timeout_sec=0.25, auto_initialize=False)
            with self.assertRaises(MemoryLockTimeoutError):
                contending_purge.purge_memory(seed_ids[4], reason="Timeout esperado")
        finally:
            _, err_h = p_hold.communicate(timeout=15)
            self.assertEqual(p_hold.returncode, 0, msg=err_h)

        # El registro no fue purgado parcialmente ni quedó corrupción.
        self.assertIsNotNone(self.engine.get_memory(seed_ids[4]))
        self.assertEqual(self.engine.verify_integrity().integrity_check_result.lower(), "ok")

    def test_multiprocess_writer_vs_restore_real_exclusion(self) -> None:
        """
        Subproceso escritor y subproceso restaurador operan sobre la misma DB compartiendo
        `ExperienceStoreLock`, demostrando exclusión mutua real, timeout y consistencia final.
        """
        for idx in range(3):
            self.engine.create_memory(
                MemoryCreateInput(
                    category=MemoryCategory.FACT,
                    source=MemorySource.USER_EXPLICIT,
                    title=f"Semilla backup restore {idx}",
                    content=f"Dato base en snapshot consistente número {idx}.",
                )
            )
        bkp = self.engine.create_backup()
        child_env = {**os.environ, "JARVIS_ENV": "test", "PYTHONPATH": str(PROJECT_ROOT)}

        # Fase A: mientras proceso escritor sostiene el lock, otro proceso intentando restaurar vence por timeout
        # sin alterar la base activa ni escribir archivos parciales.
        hold_writer = self.work_dir / "mp_hold_for_restore.py"
        hold_writer.write_text(
            """
import os
import sys
import time

os.environ["JARVIS_ENV"] = "test"
from src.memory import ExperienceStore, MemoryCategory, MemoryCreateInput, MemorySource

db_path = sys.argv[1]
store = ExperienceStore(db_path, lock_timeout_sec=5.0, auto_initialize=False)
with store.acquire_lock():
    store.create_memory(
        MemoryCreateInput(
            category=MemoryCategory.FACT,
            source=MemorySource.USER_EXPLICIT,
            title="Escritura incompatible con restore",
            content="Mantiene el lock exclusivo mientras otro proceso intenta restaurar.",
        )
    )
    print("HOLDING_FOR_RESTORE", flush=True)
    time.sleep(1.5)
""".strip(),
            encoding="utf-8",
        )
        p_hold = subprocess.Popen(
            [sys.executable, str(hold_writer), str(self.db_path)],
            cwd=str(PROJECT_ROOT),
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert p_hold.stdout is not None
            self.assertEqual(p_hold.stdout.readline().strip(), "HOLDING_FOR_RESTORE")
            restore_proc_code = (
                "import os\n"
                "os.environ['JARVIS_ENV'] = 'test'\n"
                "from src.memory import ExperienceStore, MemoryLockTimeoutError\n"
                f"s = ExperienceStore(r'{self.db_path}', lock_timeout_sec=0.25, auto_initialize=False)\n"
                "try:\n"
                f"    s.restore_from_backup(r'{bkp.backup_path}', backup_dir=r'{self.backup_dir}')\n"
                "    raise SystemExit(2)\n"
                "except MemoryLockTimeoutError:\n"
                "    print('TIMEOUT_EXPECTED')\n"
            )
            r_timeout = subprocess.run(
                [sys.executable, "-c", restore_proc_code],
                cwd=str(PROJECT_ROOT),
                env=child_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(r_timeout.returncode, 0, msg=r_timeout.stderr)
            self.assertIn("TIMEOUT_EXPECTED", r_timeout.stdout)
        finally:
            _, err_w = p_hold.communicate(timeout=15)
            self.assertEqual(p_hold.returncode, 0, msg=err_w)

        # Fase B: exclusión serializada entre escritor concurrente y restaurador en procesos reales.
        writer_loop = self.work_dir / "mp_writer_loop.py"
        restore_worker = self.work_dir / "mp_restore_worker.py"
        writer_loop.write_text(
            """
import os
import sys
import time

os.environ["JARVIS_ENV"] = "test"
from src.memory import ExperienceStore, MemoryCategory, MemoryCreateInput, MemorySource

db_path = sys.argv[1]
store = ExperienceStore(db_path, lock_timeout_sec=12.0, auto_initialize=False)
for i in range(6):
    store.create_memory(
        MemoryCreateInput(
            category=MemoryCategory.EXPERIENCE,
            source=MemorySource.USER_EXPLICIT,
            title=f"Post snapshot write {i}",
            content=f"Escritura concurrente serializada con restore índice {i}.",
        )
    )
    time.sleep(0.015)
print("WRITER_LOOP_OK", flush=True)
""".strip(),
            encoding="utf-8",
        )
        restore_worker.write_text(
            """
import os
import sys
import time

os.environ["JARVIS_ENV"] = "test"
from src.memory import ExperienceStore

db_path, bkp_path, bkp_dir = sys.argv[1], sys.argv[2], sys.argv[3]
time.sleep(0.02)
store = ExperienceStore(db_path, lock_timeout_sec=12.0, auto_initialize=False)
res = store.restore_from_backup(bkp_path, backup_dir=bkp_dir, actor="restore_proc")
assert res.restored and res.integrity_report.ok
print("RESTORE_WORKER_OK", flush=True)
""".strip(),
            encoding="utf-8",
        )

        procs_b: list[subprocess.Popen[str]] = []
        res_b: list[tuple[int, str, str]] = []
        try:
            procs_b.append(
                subprocess.Popen(
                    [sys.executable, str(writer_loop), str(self.db_path)],
                    cwd=str(PROJECT_ROOT),
                    env=child_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
            procs_b.append(
                subprocess.Popen(
                    [sys.executable, str(restore_worker), str(self.db_path), bkp.backup_path, str(self.backup_dir)],
                    cwd=str(PROJECT_ROOT),
                    env=child_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        finally:
            for p in procs_b:
                out, err = p.communicate(timeout=45)
                res_b.append((p.returncode, out, err))

        for code, out, err in res_b:
            self.assertEqual(code, 0, msg=f"Subproceso falló.\nSTDOUT:{out}\nSTDERR:{err}")

        # Estado final consistente: sin staging residual y `PRAGMA integrity_check == 'ok'`.
        self.assertEqual(list(self.work_dir.glob("*.restore_staging_*")), [])
        final_integrity = self.engine.verify_integrity()
        self.assertTrue(final_integrity.ok)
        self.assertEqual(final_integrity.integrity_check_result.lower(), "ok")
        self.assertGreaterEqual(self.engine.get_stats().active_memories, 3)

    # ------------------------------------------------------------------
    # 6. Consola interactiva, arranque liviano (sin audio/Whisper) y Git guard
    # ------------------------------------------------------------------

    def test_console_commands_flow_lightweight_startup_and_git_protection(self) -> None:
        """Verifica todos los comandos `/memoria`, no carga de voz/Whisper y protección Git."""
        # Arranque liviano en subproceso limpio comprobando que N8.2 NO importa módulos pesados de voz.
        probe_light = (
            "import os, sys; os.environ['JARVIS_ENV']='test'; "
            "from src.memory import MemoryEngine, MemoryConsoleHandler, MemoryRetriever; "
            "forbidden = [m for m in ('faster_whisper', 'src.audio.stt', 'src.audio.recorder', 'src.audio.wake_word', 'src.audio.tts') if m in sys.modules]; "
            "assert not forbidden, f'Se cargaron módulos de audio: {forbidden}'; "
            "print('LIGHTWEIGHT_OK')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe_light],
            cwd=str(PROJECT_ROOT),
            env={**os.environ, "JARVIS_ENV": "test", "PYTHONPATH": str(PROJECT_ROOT)},
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("LIGHTWEIGHT_OK", proc.stdout)

        # Protección de artefactos N8.2 en GitSafetyGuard y `.gitignore`.
        for rel in (
            "data/backups/memory/bkp_20261120.db",
            "data/backups/memory/bkp_20261120.backup.json",
            "experiences.db.restore_staging_abcd",
            "experiences.db.pre_restore_1234",
            "local.db.sha256",
        ):
            self.assertTrue(is_sensitive_path(rel), msg=f"GitSafetyGuard no protegió '{rel}'")

        guard = GitSafetyGuard(PROJECT_ROOT)
        ok_bkp, _ = guard.validate_commit_candidates(["src/memory/engine.py", "data/backups/memory/bkp_1.db"])
        self.assertFalse(ok_bkp)

        # Flujo integral de comandos de consola `/memoria`.
        mem = self.engine.create_memory(
            MemoryCreateInput(
                category=MemoryCategory.PREFERENCE,
                source=MemorySource.USER_EXPLICIT,
                title="Editor preferido",
                content="Prefiero usar VS Code en modo oscuro.",
                pending_confirmation=True,
            )
        ).record

        out: list[str] = []
        console = MemoryConsoleHandler(memory_engine=self.engine, output_fn=out.append)
        self.assertIn("/memoria backup", console.handle_command("/memoria ayuda"))
        self.assertIn(mem.memory_id, console.handle_command("/memoria listar pendientes"))
        self.assertIn("Editor preferido", console.handle_command(f"/memoria ver {mem.memory_id}"))
        self.assertIn("confirmado", console.handle_command(f"/memoria confirmar {mem.memory_id}").lower())
        self.assertIn("VS Code", console.handle_command("/memoria buscar editor oscuro"))
        self.assertIn("revisión #3", console.handle_command(f"/memoria corregir {mem.memory_id} Prefiero usar VS Code con tema Nord."))
        bkp_msg = console.handle_command("/memoria backup")
        self.assertIn("SHA-256", bkp_msg)
        self.assertIn("revocado", console.handle_command(f"/memoria olvidar {mem.memory_id} Cambio de editor").lower())

        # Purga rechazada si no se confirma.
        deny_in = iter(["n"])
        self.assertIn("cancelada", console.handle_command(f"/memoria purgar {mem.memory_id}", input_fn=lambda _: next(deny_in)).lower())
        self.assertIsNotNone(self.engine.get_memory(mem.memory_id))

        # Purga aceptada elimina físicamente.
        ok_in = iter(["sí"])
        self.assertIn("purgado físicamente", console.handle_command(f"/memoria purgar {mem.memory_id}", input_fn=lambda _: next(ok_in)).lower())
        self.assertIsNone(self.engine.get_memory(mem.memory_id))

        # Restauración confirmada recupera el registro del backup.
        restore_in = iter(["1", "s"])
        res_restore_txt = console.handle_command("/memoria restaurar", input_fn=lambda _: next(restore_in))
        self.assertIn("integrity_check: ok", res_restore_txt.lower())
        self.assertIsNotNone(self.engine.get_memory(mem.memory_id))

        # ID inexistente no expone tracebacks al usuario.
        not_found_txt = console.handle_command("/memoria ver mem_no_existe")
        self.assertIn("[Error]", not_found_txt)
        self.assertNotIn("Traceback", not_found_txt)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
