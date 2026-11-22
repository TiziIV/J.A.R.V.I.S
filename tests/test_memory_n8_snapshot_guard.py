"""Suite integral aislada N8.3 Subfase 4: MemorySnapshotGuard, frescura y protección pre-dispatch en BrainManager.

Invariantes de aislamiento:
- Corre bajo `JARVIS_ENV=test` con directorios únicos bajo `temp/jarvis_test_isolated/`.
- Jamás lee ni modifica `data/memory_store.json`, `data/experiences.db`, `data/backups/` ni `data/goals/`.
- Prueba directamente adaptadores reales (`OllamaBrain`, `GeminiBrain`, `ClaudeBrain`, `OpenAIBrain`, `CodexBrain`)
  interceptando únicamente transporte HTTP/SDK/subproceso.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
from unittest.mock import MagicMock, patch
import urllib.error
from uuid import uuid4

from config import settings
from src.tools.skill_manager import SkillManager
from src.brain.brain_manager import BrainManager
from src.brain.claude_client import ClaudeBrain
from src.brain.codex_client import CodexAuthStatus, CodexBrain
from src.brain.exceptions import BrainError, BrainErrorKind
from src.brain.gemini_client import GeminiBrain
from src.brain.goal_engine import GoalEngine
from src.brain.goal_models import Constraints, GoalContext
from src.brain.goal_persistence import EphemeralGoalStore, GoalStore
from src.brain.memory import ConversationMemory
from src.brain.ollama_client import OllamaBrain
from src.brain.openai_client import OpenAIBrain
from src.memory import (
    ExperienceStore,
    ExperienceStoreLock,
    MemoryCategory,
    MemoryCreateInput,
    MemoryEngine,
    MemoryPersistenceError,
    MemoryPrivateModeError,
    MemoryRetrievalItem,
    MemoryRetrievalResult,
    MemoryRetrievalTarget,
    MemoryScopeContext,
    MemoryScopeType,
    MemorySnapshot,
    MemorySnapshotGuard,
    MemorySnapshotReference,
    MemorySource,
    MemoryStatus,
    SnapshotValidationStatus,
    compute_record_version_hash,
)


TEST_SANDBOX_ROOT = (settings.BASE_DIR / "temp" / "jarvis_test_isolated").resolve()
REAL_MEMORY_JSON = (settings.BASE_DIR / "data" / "memory_store.json").resolve()
REAL_EXPERIENCES_DB = (settings.BASE_DIR / "data" / "experiences.db").resolve()
REAL_GOALS_DIR = (settings.BASE_DIR / "data" / "goals").resolve()


def _file_sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().upper()


class _FakeHttpResponse:
    """Respuesta HTTP mínima compatible con `with urllib.request.urlopen(...) as resp`."""

    def __init__(self, body_dict: dict) -> None:
        self._raw = json.dumps(body_dict, ensure_ascii=False).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def _http_404_error(url: str, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url=url,
        code=404,
        msg="Not Found",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(json.dumps(body).encode("utf-8")),
    )


class N8SnapshotGuardAndBrainDispatchTests(unittest.TestCase):
    """Batería completa de pruebas unitarias, de adaptadores, fallbacks, concurrencia y privacidad N8.3 Subfase 4."""

    _prev_jarvis_env: str | None = None
    _baseline_json_sha: str | None = None
    _baseline_db_exists: bool = False
    _baseline_goals_exists: bool = False

    @classmethod
    def setUpClass(cls) -> None:
        cls._prev_jarvis_env = os.environ.get("JARVIS_ENV")
        os.environ["JARVIS_ENV"] = "test"
        cls._baseline_json_sha = _file_sha256(REAL_MEMORY_JSON)
        cls._baseline_db_exists = REAL_EXPERIENCES_DB.exists()
        cls._baseline_goals_exists = REAL_GOALS_DIR.exists()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._prev_jarvis_env is None:
            os.environ.pop("JARVIS_ENV", None)
        else:
            os.environ["JARVIS_ENV"] = cls._prev_jarvis_env

    def setUp(self) -> None:
        self.work_dir = (TEST_SANDBOX_ROOT / f"s4_{uuid4().hex[:10]}").resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.work_dir / "experiences.db"
        self.backup_dir = self.work_dir / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.goal_dir = self.work_dir / "goals"
        self.store = ExperienceStore(self.db_path)
        self.conv_memory = ConversationMemory(
            max_turns=8,
            storage_file=str(self.work_dir / "conv.json"),
            auto_save=True,
        )
        self.engine = MemoryEngine(
            store=self.store,
            backup_dir=self.backup_dir,
            is_private_mode_fn=lambda: self.conv_memory.is_private_mode,
            allow_history_in_private_fn=lambda: self.conv_memory.private_allow_history,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.assertEqual(_file_sha256(REAL_MEMORY_JSON), self._baseline_json_sha)
        self.assertEqual(REAL_EXPERIENCES_DB.exists(), self._baseline_db_exists)
        self.assertEqual(REAL_GOALS_DIR.exists(), self._baseline_goals_exists)

    def _seed_memory(
        self,
        *,
        title: str,
        content: str,
        summary: str | None = None,
        scope_type: MemoryScopeType = MemoryScopeType.GLOBAL,
        scope_key: str = "global",
        category: MemoryCategory = MemoryCategory.FACT,
        tags: tuple[str, ...] = ("valorant",),
        importance: int = 85,
        expires_at: str | None = None,
    ) -> str:
        res = self.engine.create_memory(
            MemoryCreateInput(
                title=title,
                content=content,
                summary=summary or content[:180],
                category=category,
                source=MemorySource.USER_EXPLICIT,
                scope_type=scope_type,
                scope_key=scope_key,
                confidence=0.95,
                importance=importance,
                tags=tags,
                expires_at=expires_at,
            )
        )
        return res.record.memory_id

    # ------------------------------------------------------------------
    # 1. MemorySnapshotGuard: contratos, estados, límites y rechazos
    # ------------------------------------------------------------------

    def test_snapshot_guard_valid_token_change_lifecycle_and_bounded_revalidation(self) -> None:
        """Valida snapshot válido, token estable/modificado, revocado, expirado, purgado, revisión, mismatch, malformado y token inestable."""
        m_valid = self._seed_memory(
            title="Perfil estable Valorant",
            content="VAL_ESTABLE_OK: límite FPS 144 en laptop.",
            importance=95,
        )
        m_revoke = self._seed_memory(
            title="Dato revocable Valorant",
            content="VAL_A_REVOCAR: parámetro obsoleto.",
            importance=90,
        )
        m_expire = self._seed_memory(
            title="Dato expirable Valorant",
            content="VAL_A_EXPIRAR: vigencia temporal.",
            importance=88,
            expires_at="2099-01-01T00:00:00+00:00",
        )
        m_purge = self._seed_memory(
            title="Dato purgable Valorant",
            content="VAL_A_PURGAR: borrado físico.",
            importance=86,
        )
        m_revise = self._seed_memory(
            title="Dato revisable Valorant",
            content="VAL_REV_1: valor inicial.",
            importance=84,
        )

        guard = MemorySnapshotGuard(memory_engine=self.engine, request_id="req-001")
        ret = self.engine.retrieve("Valorant")
        snap = guard.capture_from_retrieval(ret, request_id="req-001", n8_query_id="n8q-fixed1")

        # Inmutabilidad exterior y no exposición textual en __repr__ / __str__.
        with self.assertRaises(Exception):
            snap.request_id = "req-tampered"  # type: ignore[misc]
        self.assertFalse(snap.validated_before_dispatch)
        self.assertIn("RAM-ONLY REDACTED", repr(snap))
        self.assertNotIn("VAL_ESTABLE_OK", repr(snap))
        self.assertNotIn("VAL_ESTABLE_OK", str(snap))

        # 1a) Token sin cambios -> válido completo.
        res_ok = guard.validate_before_dispatch(snap, request_id="req-001")
        self.assertEqual(res_ok.status, SnapshotValidationStatus.VALID_UNCHANGED_TOKEN)
        self.assertTrue(res_ok.is_valid_for_dispatch)
        self.assertTrue(res_ok.n8_allowed)
        assert res_ok.snapshot is not None
        self.assertTrue(res_ok.snapshot.validated_before_dispatch)
        self.assertEqual(res_ok.revalidations_used, 0)
        self.assertIn("VAL_ESTABLE_OK", res_ok.untrusted_n8_context or "")

        # 1b) Snapshot perteneciente a otra solicitud -> rechazado.
        res_mismatch = guard.validate_before_dispatch(snap, request_id="req-OTRA")
        self.assertEqual(res_mismatch.status, SnapshotValidationStatus.FAILED_REQUEST_MISMATCH)
        self.assertFalse(res_mismatch.is_valid_for_dispatch)
        self.assertFalse(res_mismatch.n8_allowed)
        self.assertIsNone(res_mismatch.untrusted_n8_context)

        # 1c) Snapshot malformado -> rechazado (`FAILED_MALFORMED` y `FAILED_TOKEN_INVALID`).
        malformed_no_refs = MemorySnapshot(
            snapshot_id="n8s-bad1",
            n8_query_id="n8q-bad1",
            request_id="req-001",
            store_version_token=ret.store_version_token,
            references=(),
            items=snap.items[:1],
            scope={},
            retrieved_at=snap.retrieved_at,
        )
        res_mal = guard.validate_before_dispatch(malformed_no_refs, request_id="req-001")
        self.assertEqual(res_mal.status, SnapshotValidationStatus.FAILED_MALFORMED)
        self.assertFalse(res_mal.n8_allowed)

        bad_tok_snap = MemorySnapshot(
            snapshot_id="n8s-badtok",
            n8_query_id="n8q-badtok",
            request_id="req-001",
            store_version_token="token_ilegible_sin_dos_puntos",
            references=snap.references,
            items=snap.items,
            scope={},
            retrieved_at=snap.retrieved_at,
        )
        res_badtok = guard.validate_before_dispatch(bad_tok_snap, request_id="req-001")
        self.assertEqual(res_badtok.status, SnapshotValidationStatus.FAILED_TOKEN_INVALID)
        self.assertFalse(res_badtok.n8_allowed)

        # 1d) Mutamos: revocado, expirado, purgado y modificado (rev=2). Agregamos además un similar no referenciado.
        m_similar_trap = self._seed_memory(
            title="Similar Valorant trap",
            content="VAL_TRAP_SIMILAR: no debe sustituir.",
            importance=99,
        )
        self.engine.revoke_memory(m_revoke, reason="Obsoleto")
        self.store.transition_status(m_expire, new_status=MemoryStatus.EXPIRED, reason="Expirado")
        self.engine.purge_memory(m_purge, confirmed=True, reason="Eliminado")
        self.engine.correct_memory(m_revise, content="VAL_REV_2: editado posteriormente.")

        res_mut = guard.validate_before_dispatch(snap, request_id="req-001")
        self.assertEqual(res_mut.status, SnapshotValidationStatus.REFRESHED_AFTER_TOKEN_CHANGE)
        self.assertTrue(res_mut.is_valid_for_dispatch)
        self.assertTrue(res_mut.n8_allowed)
        self.assertEqual(res_mut.revalidations_used, 1)
        self.assertEqual(res_mut.refreshes_used, 0)  # No hace refresh si hubo inválidos -> cero sustitución por similar.
        self.assertEqual(res_mut.excluded_ids[m_revoke], "status_revoked")
        self.assertEqual(res_mut.excluded_ids[m_expire], "status_expired")
        self.assertEqual(res_mut.excluded_ids[m_purge], "purged_or_missing")
        self.assertEqual(res_mut.excluded_ids[m_revise], "revision_modified")
        assert res_mut.snapshot is not None
        self.assertEqual(res_mut.snapshot.memory_ids, (m_valid,))
        self.assertNotIn(m_similar_trap, res_mut.snapshot.memory_ids)
        self.assertIn("VAL_ESTABLE_OK", res_mut.untrusted_n8_context or "")
        self.assertNotIn("VAL_A_REVOCAR", res_mut.untrusted_n8_context or "")
        self.assertNotIn("VAL_REV_1", res_mut.untrusted_n8_context or "")
        self.assertNotIn("VAL_TRAP_SIMILAR", res_mut.untrusted_n8_context or "")

        # 1e) Mutación ajena al snapshot con `allow_refresh_retrieval=True`: ejecuta a lo sumo 1 refresh acotado.
        guard_dyn = MemorySnapshotGuard(
            memory_engine=self.engine,
            request_id="req-dyn",
            max_revalidations=1,
            max_refreshes=1,
        )
        ret_before_add = self.engine.retrieve("Valorant")
        snap_before_add = guard_dyn.capture_from_retrieval(ret_before_add, request_id="req-dyn")
        self._seed_memory(
            title="Nuevo dato Valorant",
            content="VAL_NUEVO_DINAMICO: perfil GPU actualizado.",
            importance=96,
        )
        res_dyn = guard_dyn.validate_before_dispatch(
            snap_before_add,
            request_id="req-dyn",
            allow_refresh_retrieval=True,
        )
        self.assertEqual(res_dyn.status, SnapshotValidationStatus.REFRESHED_AFTER_TOKEN_CHANGE)
        self.assertEqual(res_dyn.revalidations_used, 1)
        self.assertEqual(res_dyn.refreshes_used, 1)
        self.assertIn("VAL_NUEVO_DINAMICO", res_dyn.untrusted_n8_context or "")

        # 1f) Token continuamente inestable (`T1 -> T2` antes y `T2 -> T3` al terminar de revalidar):
        #     respeta límite fijo y falla cerrado respecto de N8 (`FAILED_TOKEN_UNSTABLE`).
        guard_unstable = MemorySnapshotGuard(
            memory_engine=self.engine,
            request_id="req-unstable",
            max_revalidations=1,
            max_refreshes=1,
        )
        ret_u = self.engine.retrieve("Valorant")
        snap_u = guard_unstable.capture_from_retrieval(ret_u, request_id="req-unstable")
        base_tok = self.engine.get_store_version_token()
        epoch = base_tok.split(":")[0]
        tok_seq = iter([f"{epoch}:998", f"{epoch}:999", f"{epoch}:1000"])
        with patch.object(self.engine, "get_store_version_token", side_effect=lambda: next(tok_seq)):
            res_u = guard_unstable.validate_before_dispatch(
                snap_u,
                request_id="req-unstable",
                allow_refresh_retrieval=False,
            )
        self.assertEqual(res_u.status, SnapshotValidationStatus.FAILED_TOKEN_UNSTABLE)
        self.assertFalse(res_u.is_valid_for_dispatch)
        self.assertFalse(res_u.n8_allowed)
        self.assertIsNone(res_u.untrusted_n8_context)
        self.assertIsNone(res_u.snapshot)

    # ------------------------------------------------------------------
    # 2. BrainManager + adaptadores reales (sin recuerdos, válidos, revocados, fallos SQLite, aislamiento)
    # ------------------------------------------------------------------

    def test_brain_manager_real_adapters_pre_dispatch_and_system_prompt_isolation(self) -> None:
        """Comprueba llamadas reales a adaptadores sin recuerdos, válidos, revocados pre-dispatch, fallo SQLite y aborto ante fuga."""
        # A) Sin recuerdos -> flujo normal sobre OllamaBrain real.
        ollama_real = OllamaBrain()
        ollama_chats: list[dict] = []

        def _fake_ollama_chat(*, model, messages, stream, options):
            ollama_chats.append({"model": model, "messages": list(messages), "options": dict(options)})
            return iter([{"message": {"content": "Respuesta limpia Ollama."}}])

        ollama_real.client = MagicMock()
        ollama_real.client.chat.side_effect = _fake_ollama_chat

        bm_no_mem = BrainManager(
            initial_mode="OLLAMA",
            ollama_brain=ollama_real,
            memory=self.conv_memory,
            memory_engine=self.engine,
            lazy_init=True,
        )
        turn_empty = bm_no_mem.process_turn("Consulta sin recuerdos previos omega987")
        self.assertFalse(turn_empty.is_error)
        self.assertEqual(len(ollama_chats), 1)
        self.assertEqual(len(ollama_chats[0]["messages"]), 2)  # solo system + user
        self.assertEqual(bm_no_mem.last_n8_guard_status, SnapshotValidationStatus.VALID_NO_MEMORIES.value)

        # B) Con recuerdos válidos y uno revocado justo entre retrieve y dispatch físico -> solo válido en user; cero en system.
        m_keep = self._seed_memory(
            title="Configuración audio",
            content="MEM_AUDIO_VALIDA: volumen base 65.",
            tags=("audio", "configuracion"),
            importance=95,
        )
        m_late_revoke = self._seed_memory(
            title="Audio revocado tardío",
            content="MEM_AUDIO_REVOCADO_SECRETO: valor antiguo 10.",
            tags=("audio", "configuracion"),
            importance=90,
        )

        claude_real = ClaudeBrain(api_key="dummy-claude-offline-cred")
        claude_payloads: list[dict] = []

        def _fake_urlopen(req, timeout=15.0):
            body = json.loads(req.data.decode("utf-8"))
            claude_payloads.append(body)
            return _FakeHttpResponse(
                {"model": "claude-sonnet-4-6", "content": [{"type": "text", "text": "Respuesta Claude OK."}]}
            )

        orig_retrieve = self.engine.retrieve

        def _retrieve_then_revoke(q, **kw):
            res = orig_retrieve(q, **kw)
            # Revocación que ocurre inmediatamente después de retrieve() pero ANTES de _invoke_single_mode().
            self.engine.revoke_memory(m_late_revoke, reason="Revocado tras retrieve")
            return res

        bm_claude = BrainManager(
            initial_mode="CLAUDE",
            claude_brain=claude_real,
            memory=self.conv_memory,
            memory_engine=self.engine,
            lazy_init=True,
        )
        log_buf = io.StringIO()
        with patch.object(self.engine, "retrieve", side_effect=_retrieve_then_revoke), \
             patch("urllib.request.urlopen", side_effect=_fake_urlopen), \
             contextlib.redirect_stdout(log_buf):
            turn_c = bm_claude.process_turn("Revisa mi configuracion audio por favor")

        self.assertFalse(turn_c.is_error)
        self.assertEqual(len(claude_payloads), 1)
        sent = claude_payloads[0]
        # Frontera estricta: `system` libre de recuerdos; `messages` contiene válido y excluye revocado.
        self.assertNotIn("MEM_AUDIO_VALIDA", sent["system"])
        self.assertNotIn("MEM_AUDIO_REVOCADO_SECRETO", sent["system"])
        user_dump = json.dumps(sent["messages"], ensure_ascii=False)
        self.assertIn("MEM_AUDIO_VALIDA", user_dump)
        self.assertNotIn("MEM_AUDIO_REVOCADO_SECRETO", user_dump)
        self.assertNotIn("MEM_AUDIO_REVOCADO_SECRETO", log_buf.getvalue())
        self.assertNotIn("MEM_AUDIO_VALIDA", log_buf.getvalue())

        # C) Fallo de SQLite justo en comprobación pre-dispatch -> NO envía contexto obsoleto; degrada limpio sin N8.
        ollama_chats.clear()
        orig_get_tok = self.engine.get_store_version_token
        call_n = {"count": 0}

        def _tok_fails_pre_dispatch() -> str:
            call_n["count"] += 1
            if call_n["count"] >= 1:
                raise MemoryPersistenceError("sqlite_io_error")
            return orig_get_tok()

        bm_fail_sql = BrainManager(
            initial_mode="OLLAMA",
            ollama_brain=ollama_real,
            memory=ConversationMemory(max_turns=4, storage_file=None, auto_save=False),
            memory_engine=self.engine,
            lazy_init=True,
        )
        with patch.object(self.engine, "get_store_version_token", side_effect=_tok_fails_pre_dispatch):
            turn_sql = bm_fail_sql.process_turn("Consulta configuracion audio ahora")

        self.assertFalse(turn_sql.is_error)
        self.assertEqual(len(ollama_chats), 1)
        msgs_sql = ollama_chats[0]["messages"]
        self.assertEqual(len(msgs_sql), 2)  # Se omitió todo untrusted_context N8 obsoleto.
        self.assertNotIn("MEM_AUDIO_VALIDA", json.dumps(msgs_sql, ensure_ascii=False))
        self.assertEqual(bm_fail_sql.last_n8_guard_status, SnapshotValidationStatus.FAILED_ENGINE_UNAVAILABLE.value)

        # D) Token inestable en `query_raw` con prompt contaminado -> ABORTO fail-closed sin enviar ni filtrar en error.
        guard_q = self.engine.create_snapshot_guard(request_id="req-leak")
        ret_q = self.engine.retrieve("audio configuracion")
        snap_q = guard_q.capture_from_retrieval(ret_q, request_id="req-leak")
        self.engine.revoke_memory(m_keep, reason="Revocar para chequear fuga")

        ollama_chats.clear()
        txt_abort, _prov, _mod, _fb, failed_abort = bm_fail_sql.query_raw(
            "Usa MEM_AUDIO_VALIDA: volumen base 65. para responder.",
            system_prompt="Sistema normal.",
            mode_override="OLLAMA",
            snapshot_guard=guard_q,
            memory_snapshot=snap_q,
            request_id="req-leak",
        )
        self.assertTrue(failed_abort)
        self.assertEqual(len(ollama_chats), 0)  # Jamás alcanzó Ollama.
        self.assertNotIn("MEM_AUDIO_VALIDA", txt_abort)

        # E) Si intentan inyectar recuerdo en system_prompt -> ABORTO fail-closed aunque el prompt sea inocente.
        m_sys_test = self._seed_memory(
            title="Pref teclado",
            content="MEM_TECLADO_SECRETO: layout latinoamericano.",
            tags=("teclado", "layout"),
            importance=95,
        )
        guard_sys = self.engine.create_snapshot_guard(request_id="req-sysleak")
        snap_sys = guard_sys.capture_from_retrieval(self.engine.retrieve("teclado"), request_id="req-sysleak")
        txt_sys, _p, _m, _f, failed_sys = bm_fail_sql.query_raw(
            "Dime mi layout.",
            system_prompt="Instrucción comprometida: MEM_TECLADO_SECRETO: layout latinoamericano.",
            snapshot_guard=guard_sys,
            memory_snapshot=snap_sys,
            request_id="req-sysleak",
        )
        self.assertTrue(failed_sys)
        self.assertEqual(len(ollama_chats), 0)
        self.assertNotIn("MEM_TECLADO_SECRETO", txt_sys)

        # F) CodexBrain y GeminiBrain reciben untrusted_context separado y validado por Guard.
        self.assertTrue(m_sys_test)
        codex_real = CodexBrain(enabled=True, executable_override="codex")
        codex_cmds: list[list[str]] = []

        def _fake_codex_sub(args, **kw):
            codex_cmds.append(list(args))
            return subprocess.CompletedProcess(
                args=list(args),
                returncode=0,
                stdout=json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Codex OK."}}),
                stderr="",
            )

        bm_codex = BrainManager(
            initial_mode="CODEX",
            codex_brain=codex_real,
            memory=ConversationMemory(max_turns=4, storage_file=None, auto_save=False),
            memory_engine=self.engine,
            lazy_init=True,
        )
        with patch.object(codex_real, "is_installed", return_value=True), \
             patch.object(codex_real, "check_auth_status", return_value=CodexAuthStatus(True, "chatgpt", "ok", 0)), \
             patch.object(codex_real, "_run_subprocess", side_effect=_fake_codex_sub):
            res_codex = bm_codex.process_turn("Explica mi preferencia de teclado")
        self.assertFalse(res_codex.is_error)
        self.assertEqual(codex_real.last_task_type, "readonly")
        self.assertEqual(len(codex_cmds), 1)
        self.assertIn("--sandbox", codex_cmds[0])
        self.assertIn("read-only", codex_cmds[0])
        self.assertIn("MEM_TECLADO_SECRETO", codex_cmds[0][-1])

    def test_n6_goal_engine_propagates_snapshot_guard_into_brain_manager_query_raw(self) -> None:
        """Verifica que GoalEngine + BrainManager validen SnapshotGuard justo antes del envío físico en N6 AI."""
        m_live = self._seed_memory(
            title="GPU real N6",
            content="N6_LIVE_REC: mantener modo bajo consumo en Valor.",
            scope_type=MemoryScopeType.APP_OR_GAME,
            scope_key="valorant",
            tags=("valorant", "rendimiento"),
            importance=96,
        )
        m_rev = self._seed_memory(
            title="Parámetro revocable N6",
            content="N6_REVOKED_PRE_DISPATCH: dato caducado antes de proveedor.",
            scope_type=MemoryScopeType.APP_OR_GAME,
            scope_key="valorant",
            tags=("valorant", "rendimiento"),
            importance=94,
        )
        ollama_real = OllamaBrain()
        captured_msgs: list[list[dict]] = []

        def _fake_chat(*, model, messages, stream, options):
            captured_msgs.append(list(messages))
            # La 1ra llamada (understanding) solicita JSON; la 2da (planner) array JSON.
            user_prompt = messages[-1]["content"]
            if "Analiza el siguiente objetivo" in user_prompt:
                reply = '{"interpreted_summary":"Resumen N6 AI limpio"}'
            else:
                reply = '[{"task_id":"t1","title":"Check","description":"Check","skill_name":"system_info","skill_params":{},"depends_on":[],"expected_output":"OK"}]'
            return iter([{"message": {"content": reply}}])

        ollama_real.client = MagicMock()
        ollama_real.client.chat.side_effect = _fake_chat

        bm = BrainManager(
            initial_mode="OLLAMA",
            ollama_brain=ollama_real,
            memory=ConversationMemory(max_turns=4, storage_file=None, auto_save=False),
            memory_engine=self.engine,
            lazy_init=True,
        )
        sm = SkillManager()
        ge = GoalEngine(
            skill_manager=sm,
            goal_store=GoalStore(store_dir=self.goal_dir),
            brain_manager=bm,
            memory_engine=self.engine,
            use_ai=True,
        )

        # Hook: justo al invocar `query_raw` (ya después de `_consult_n8_before_planning`), revocamos `m_rev`
        # antes del dispatch físico en `_invoke_single_mode`.
        orig_query_raw = bm.query_raw
        revoked_done = {"done": False}

        def _revoke_right_before_query_raw(*args, **kwargs):
            if not revoked_done["done"]:
                revoked_done["done"] = True
                self.engine.revoke_memory(m_rev, reason="Revocado entre N6 y dispatch")
            return orig_query_raw(*args, **kwargs)

        with patch.object(bm, "query_raw", side_effect=_revoke_right_before_query_raw):
            plan = ge.create_goal_and_plan(
                "Analizar rendimiento Valorant",
                context=GoalContext(target_app="Valorant"),
                constraints=Constraints(read_only=True, allow_file_modifications=False),
            )

        self.assertTrue(plan.tasks)
        self.assertGreaterEqual(len(captured_msgs), 1)
        all_sent_json = json.dumps(captured_msgs, ensure_ascii=False)
        self.assertIn("N6_LIVE_REC", all_sent_json)
        self.assertNotIn("N6_REVOKED_PRE_DISPATCH", all_sent_json)
        # Ningún texto de N8 llega a `.goal.json`.
        goal_disk = (self.goal_dir / "plans" / f"{plan.plan_id}.goal.json").read_text(encoding="utf-8")
        self.assertNotIn("N6_LIVE_REC", goal_disk)
        self.assertNotIn("N6_REVOKED_PRE_DISPATCH", goal_disk)
        self.assertIn(m_live, goal_disk)

        # `query_raw(..., mode_override=...)` también protege el dispatch con `SnapshotGuard`.
        captured_msgs.clear()
        guard_mo = self.engine.create_snapshot_guard(request_id="req-mo")
        snap_mo = guard_mo.capture_from_retrieval(self.engine.retrieve("Valorant"), request_id="req-mo")
        txt_mo, prov_mo, _m, _f, fail_mo = bm.query_raw(
            "Resumen breve Valorant",
            mode_override="OLLAMA",
            snapshot_guard=guard_mo,
            memory_snapshot=snap_mo,
            request_id="req-mo",
        )
        self.assertFalse(fail_mo)
        self.assertEqual(prov_mo, "Ollama")
        self.assertTrue(txt_mo)
        self.assertIn("N6_LIVE_REC", json.dumps(captured_msgs, ensure_ascii=False))

    # ------------------------------------------------------------------
    # 3. Fallbacks, reintentos y selección AUTO / proveedores reales
    # ------------------------------------------------------------------

    def test_fallbacks_revalidation_between_providers_and_404_model_retries(self) -> None:
        """Valida 1 validación en primario OK, nueva validación en fallback, revocación intermedia, AUTO y retries 404."""
        m_stay = self._seed_memory(
            title="Nota estable fallback",
            content="FB_STABLE_CONTENT: usa perfil equilibrado.",
            tags=("rendimiento", "gpu"),
            importance=95,
        )
        m_drop = self._seed_memory(
            title="Nota revocada entre primario y fallback",
            content="FB_DROPPED_CONTENT: overclock arriesgado obsoleto.",
            tags=("rendimiento", "gpu"),
            importance=92,
        )

        # 3a) Primario exitoso (`OllamaBrain` real): exactamente 1 validación pre-dispatch.
        ollama_ok = OllamaBrain()
        ollama_ok.client = MagicMock()
        ollama_ok.client.chat.return_value = iter([{"message": {"content": "OK primario."}}])
        bm_single = BrainManager(
            initial_mode="OLLAMA",
            ollama_brain=ollama_ok,
            memory=ConversationMemory(max_turns=4, storage_file=None, auto_save=False),
            memory_engine=self.engine,
            lazy_init=True,
        )
        orig_val_fn = MemorySnapshotGuard.validate_before_dispatch
        with patch.object(
            MemorySnapshotGuard,
            "validate_before_dispatch",
            autospec=True,
            side_effect=orig_val_fn,
        ) as spy_val:
            res_single = bm_single.process_turn("Consejo rendimiento gpu")
            self.assertFalse(res_single.is_error)
            self.assertEqual(spy_val.call_count, 1)

        # 3b) Primario (`ClaudeBrain`) falla con 503 y al fallar se revoca `m_drop` ANTES de fallback (`GeminiBrain` real -> 404 -> `OllamaBrain` real).
        #     `ClaudeBrain` recibe ambos en T1; `GeminiBrain` (tras revocación) NO recibe `FB_DROPPED_CONTENT` y sí `FB_STABLE_CONTENT`;
        #     además `GeminiBrain` hace retry 404 y vuelve a revalidar en el 2do modelo tras cambio de token T2 -> T3!
        claude_fail = ClaudeBrain(api_key="dummy-claude-offline-cred")
        gemini_real = GeminiBrain(api_key="dummy-gemini-offline-cred", model_name="gemini-2.0-flash", fallback_model_name="gemini-1.5-flash")
        gemini_real.client = MagicMock()

        claude_seen: list[dict] = []
        gemini_seen: list[dict] = []

        def _urlopen_claude_fails(req, timeout=15.0):
            payload = json.loads(req.data.decode("utf-8"))
            claude_seen.append(payload)
            # Justo cuando falla Claude, el usuario revoca `m_drop` en otro hilo/turno.
            self.engine.revoke_memory(m_drop, reason="Revocado tras fallo Claude")
            raise _http_404_error(req.full_url, {"error": {"type": "api_error", "message": "Overloaded"}}) if False else urllib.error.HTTPError(
                url=req.full_url,
                code=503,
                msg="Service Unavailable",
                hdrs=None,  # type: ignore[arg-type]
                fp=io.BytesIO(b'{"error":{"type":"api_error","message":"overloaded"}}'),
            )

        m_extra_between_models = self._seed_memory(
            title="Otro recuerdo previo",
            content="OTRO_RECUERDO_AJENO: no relacionado.",
            tags=("otro_tema",),
            importance=50,
        )

        def _gemini_generate(*, model, contents, config):
            gemini_seen.append(
                {
                    "model": model,
                    "contents": str(contents),
                    "system_instruction": str(getattr(config, "system_instruction", "") or ""),
                }
            )
            if model == "gemini-2.0-flash":
                # Al fallar el 1er modelo Gemini con 404, cambiamos también el token (`T2 -> T3`) revocando un recuerdo ajeno
                # o incluso `m_stay` en otro test: aquí revocamos `m_extra_between_models` para forzar revalidación en retry 404!
                self.engine.revoke_memory(m_extra_between_models, reason="Cambio token antes de 1.5-flash")
                raise RuntimeError("404 NOT_FOUND")
            return MagicMock(text="Respuesta rescatada por Gemini 1.5.")

        gemini_real.client.models.generate_content.side_effect = _gemini_generate

        bm_fb = BrainManager(
            initial_mode="CLAUDE",
            claude_brain=claude_fail,
            gemini_brain=gemini_real,
            ollama_brain=ollama_ok,
            memory=ConversationMemory(max_turns=4, storage_file=None, auto_save=False),
            memory_engine=self.engine,
            lazy_init=True,
        )
        with patch("urllib.request.urlopen", side_effect=_urlopen_claude_fails):
            res_fb = bm_fb.process_turn("Optimiza rendimiento gpu")

        self.assertFalse(res_fb.is_error)
        self.assertEqual(res_fb.final_provider, "Gemini")
        self.assertEqual(len(claude_seen), 1)
        claude_str = json.dumps(claude_seen[0]["messages"], ensure_ascii=False)
        self.assertIn("FB_STABLE_CONTENT", claude_str)
        self.assertIn("FB_DROPPED_CONTENT", claude_str)

        # En ambos modelos de Gemini (2.0-flash y 1.5-flash), `m_drop` NUNCA se envió tras ser revocado.
        self.assertEqual(len(gemini_seen), 2)
        for attempt in gemini_seen:
            self.assertNotIn("FB_DROPPED_CONTENT", attempt["contents"])
            self.assertNotIn("FB_DROPPED_CONTENT", attempt["system_instruction"])
            self.assertIn("FB_STABLE_CONTENT", attempt["contents"])
            self.assertNotIn("FB_STABLE_CONTENT", attempt["system_instruction"])

        # 3c) Fallo de SQLite en fallback: envía el fallback sin N8 preservando web_ctx y jamás reutiliza el snapshot previo!
        m_web_stay = self._seed_memory(
            title="Rendimiento fallback N8",
            content="MEM_PARA_FALLBACK_SQL: dato válido inicial.",
            tags=("shader", "valorant"),
            importance=95,
        )
        self.assertTrue(m_stay and m_web_stay)
        mem_with_web = ConversationMemory(max_turns=4, storage_file=None, auto_save=False)
        mem_with_web.add_external_reference("WEB_EXT_PRESERVADA: nota del día.", tool_name="web_extraction")

        gpt_fail = OpenAIBrain(api_key="dummy-openai-offline-cred", model_name="gpt-5.4", fallback_model_name="gpt-4o")
        ollama_fb_spy = OllamaBrain()
        ollama_fb_msgs: list[list[dict]] = []

        def _ollama_chat_fb(*, model, messages, stream, options):
            ollama_fb_msgs.append(list(messages))
            return iter([{"message": {"content": "Fallback Ollama sin N8."}}])

        ollama_fb_spy.client = MagicMock()
        ollama_fb_spy.client.chat.side_effect = _ollama_chat_fb

        gemini_fail = GeminiBrain(api_key="")  # UNCONFIGURED -> salta directo a OLLAMA en GPT -> GEMINI -> OLLAMA

        gpt_attempts = {"n": 0}
        orig_get_tok_fb = self.engine.get_store_version_token

        def _tok_breaks_before_fallback() -> str:
            if gpt_attempts["n"] >= 2:
                raise MemoryPersistenceError("sqlite_locked_in_fallback")
            return orig_get_tok_fb()

        def _urlopen_gpt_404_then_500(req, timeout=15.0):
            gpt_attempts["n"] += 1
            body = json.loads(req.data.decode("utf-8"))
            if body.get("model") == "gpt-5.4":
                raise _http_404_error(
                    req.full_url,
                    {"error": {"code": "model_not_found", "param": "model", "message": "Model not found"}},
                )
            raise urllib.error.HTTPError(
                url=req.full_url,
                code=500,
                msg="Server Error",
                hdrs=None,  # type: ignore[arg-type]
                fp=io.BytesIO(b'{"error":{"type":"server_error","message":"boom"}}'),
            )

        bm_sql_fb = BrainManager(
            initial_mode="GPT",
            openai_brain=gpt_fail,
            gemini_brain=gemini_fail,
            ollama_brain=ollama_fb_spy,
            memory=mem_with_web,
            memory_engine=self.engine,
            lazy_init=True,
        )
        with patch("urllib.request.urlopen", side_effect=_urlopen_gpt_404_then_500), \
             patch.object(self.engine, "get_store_version_token", side_effect=_tok_breaks_before_fallback):
            turn_fb_sql = bm_sql_fb.process_turn("Revisa shader valorant")

        self.assertFalse(turn_fb_sql.is_error)
        self.assertEqual(turn_fb_sql.final_provider, "Ollama")
        self.assertEqual(len(ollama_fb_msgs), 1)
        fb_dump = json.dumps(ollama_fb_msgs[0], ensure_ascii=False)
        self.assertIn("WEB_EXT_PRESERVADA", fb_dump)
        self.assertNotIn("MEM_PARA_FALLBACK_SQL", fb_dump)  # Descartado fail-closed cuando falló SQLite en fallback.

        # 3d) AUTO seleccionando proveedor alternativo por deliberación -> validación pre-dispatch en cada envío.
        bm_auto = BrainManager(
            initial_mode="AUTO",
            gemini_brain=GeminiBrain(api_key=""),
            claude_brain=ClaudeBrain(api_key=""),
            openai_brain=OpenAIBrain(api_key=""),
            ollama_brain=ollama_fb_spy,
            memory=ConversationMemory(max_turns=4, storage_file=None, auto_save=False),
            memory_engine=self.engine,
            lazy_init=True,
        )
        ollama_fb_msgs.clear()
        orig_val_auto = MemorySnapshotGuard.validate_before_dispatch
        with patch.object(
            MemorySnapshotGuard,
            "validate_before_dispatch",
            autospec=True,
            side_effect=orig_val_auto,
        ) as spy_auto_val:
            # Consulta de arquitectura compleja -> AUTO intenta cloud (Gemini/Claude -> fallback Ollama).
            res_auto = bm_auto.process_turn(
                "Analiza en profundidad la arquitectura de shader valorant comparando ventajas y desventajas"
            )
        self.assertFalse(res_auto.is_error)
        self.assertGreaterEqual(spy_auto_val.call_count, 2)  # Primario cloud + fallback pasando por validación.

        # 3e) Retry interno de modelo en OpenAIBrain (`gpt-5.4` 404 -> `gpt-4o` OK) con revocación ENTRE modelos.
        m_gpt_keep = self._seed_memory(
            title="Regla GPT estable",
            content="GPT_STABLE_RULE: mantener límite termal 80C.",
            tags=("termal", "nvidia"),
            importance=95,
        )
        m_gpt_rev = self._seed_memory(
            title="Regla GPT revocable 404",
            content="GPT_REVOKED_404_RULE: configuración obsoleta 110C.",
            tags=("termal", "nvidia"),
            importance=92,
        )
        openai_real = OpenAIBrain(
            api_key="dummy-openai-offline-cred",
            model_name="gpt-5.4",
            fallback_model_name="gpt-4o",
        )
        openai_payloads: list[dict] = []

        def _urlopen_gpt_404_then_ok(req, timeout=15.0):
            body = json.loads(req.data.decode("utf-8"))
            openai_payloads.append(body)
            if body.get("model") == "gpt-5.4":
                self.engine.revoke_memory(m_gpt_rev, reason="Revocado entre 404 y gpt-4o")
                raise _http_404_error(
                    req.full_url,
                    {"error": {"code": "model_not_found", "param": "model", "message": "Model not found"}},
                )
            return _FakeHttpResponse(
                {"model": "gpt-4o", "choices": [{"message": {"content": "OpenAI GPT-4o OK."}}]}
            )

        bm_gpt_retry = BrainManager(
            initial_mode="GPT",
            openai_brain=openai_real,
            memory=ConversationMemory(max_turns=4, storage_file=None, auto_save=False),
            memory_engine=self.engine,
            lazy_init=True,
        )
        with patch("urllib.request.urlopen", side_effect=_urlopen_gpt_404_then_ok):
            res_gpt_retry = bm_gpt_retry.process_turn("Revisa limite termal nvidia")
        self.assertFalse(res_gpt_retry.is_error)
        self.assertEqual(len(openai_payloads), 2)
        p_first = json.dumps(openai_payloads[0]["messages"], ensure_ascii=False)
        p_second = json.dumps(openai_payloads[1]["messages"], ensure_ascii=False)
        self.assertIn("GPT_REVOKED_404_RULE", p_first)
        self.assertNotIn("GPT_REVOKED_404_RULE", p_second)
        self.assertIn("GPT_STABLE_RULE", p_second)
        self.assertTrue(m_gpt_keep)

    # ------------------------------------------------------------------
    # 4. Concurrencia, revocación sincronizada y no retención de locks SQLite
    # ------------------------------------------------------------------

    def test_concurrency_revocation_during_provider_and_no_sqlite_locks_held(self) -> None:
        """Verifica no bloqueo SQLite durante llamada al proveedor, revocación durante revalidación y cero cruce entre 2 hilos."""
        m_a = self._seed_memory(
            title="Hilo A memoria",
            content="CONC_ALPHA_DATA: parámetro exclusivo hilo alpha.",
            tags=("alpha",),
            importance=95,
        )
        m_b = self._seed_memory(
            title="Hilo B memoria",
            content="CONC_BETA_DATA: parámetro exclusivo hilo beta.",
            tags=("beta",),
            importance=95,
        )

        # 4a) Cero lock SQLite retenido mientras el proveedor LLM está bloqueado esperando red/respuesta.
        provider_entered = threading.Event()
        allow_provider_exit = threading.Event()
        revoked_while_provider_blocked = {"ok": False, "lock_was_free": False}

        ollama_slow = OllamaBrain()

        def _slow_chat(*, model, messages, stream, options):
            provider_entered.set()
            self.assertTrue(allow_provider_exit.wait(timeout=5.0))
            return iter([{"message": {"content": "Slow LLM done."}}])

        ollama_slow.client = MagicMock()
        ollama_slow.client.chat.side_effect = _slow_chat

        bm_lock_test = BrainManager(
            initial_mode="OLLAMA",
            ollama_brain=ollama_slow,
            memory=ConversationMemory(max_turns=4, storage_file=None, auto_save=False),
            memory_engine=self.engine,
            lazy_init=True,
        )

        def _writer_during_llm():
            if not provider_entered.wait(timeout=5.0):
                return
            try:
                probe_lock = ExperienceStoreLock(self.db_path, timeout_sec=0.8)
                with probe_lock:
                    revoked_while_provider_blocked["lock_was_free"] = True
                self.engine.revoke_memory(m_a, reason="Revocación concurrente durante inferencia")
                revoked_while_provider_blocked["ok"] = True
            finally:
                allow_provider_exit.set()

        t_writer = threading.Thread(target=_writer_during_llm, daemon=True)
        t_writer.start()
        res_slow = bm_lock_test.process_turn("Consulta parametro alpha")
        t_writer.join(timeout=5.0)
        self.assertFalse(res_slow.is_error)
        self.assertTrue(revoked_while_provider_blocked["lock_was_free"])
        self.assertTrue(revoked_while_provider_blocked["ok"])

        # Confirmamos ahora en un turno nuevo que el recuerdo ya revocado `m_a` no entra.
        m_a_fresh = self._seed_memory(
            title="Hilo A2 memoria",
            content="CONC_ALPHA2_DATA: parámetro nuevo alpha.",
            tags=("alpha2",),
            importance=95,
        )

        # 4b) Revocación DURANTE revalidación (`get_memory` OK en el paso 1, revocado justo antes del post-check de token).
        guard_during_reval = self.engine.create_snapshot_guard(request_id="req-reval-race")
        ret_race = self.engine.retrieve("alpha2")
        snap_race = guard_during_reval.capture_from_retrieval(ret_race, request_id="req-reval-race")

        orig_get_mem = self.engine.get_memory
        injected_mid_reval = {"done": False}

        def _get_mem_with_concurrent_revoke(mid: str):
            rec = orig_get_mem(mid)
            if mid == m_a_fresh and not injected_mid_reval["done"]:
                injected_mid_reval["done"] = True
                self.engine.revoke_memory(m_a_fresh, reason="Revocado en plena revalidación")
            return rec

        with patch.object(self.engine, "get_memory", side_effect=_get_mem_with_concurrent_revoke):
            res_race = guard_during_reval.validate_before_dispatch(snap_race, request_id="req-reval-race")
        self.assertEqual(res_race.excluded_ids.get(m_a_fresh), "status_revoked")
        self.assertFalse(res_race.n8_allowed)
        self.assertIsNone(res_race.untrusted_n8_context)

        # 4c) Dos solicitudes simultáneas sobre el mismo MemoryEngine con sobres/snapshots independientes -> cero cruce.
        m_alpha_live = self._seed_memory(
            title="Hilo Gamma memoria",
            content="CONC_GAMMA_ONLY: contenido exclusivo gamma.",
            tags=("gamma_only",),
            importance=95,
        )
        self.assertTrue(m_alpha_live)
        barrier = threading.Barrier(2)
        thread_results: dict[str, str] = {}

        def _run_req(label: str, query: str) -> None:
            local_ollama = OllamaBrain()
            seen: list[str] = []

            def _chat(*, model, messages, stream, options):
                seen.append(json.dumps(messages, ensure_ascii=False))
                return iter([{"message": {"content": f"OK {label}"}}])

            local_ollama.client = MagicMock()
            local_ollama.client.chat.side_effect = _chat
            local_bm = BrainManager(
                initial_mode="OLLAMA",
                ollama_brain=local_ollama,
                memory=ConversationMemory(max_turns=4, storage_file=None, auto_save=False),
                memory_engine=self.engine,
                lazy_init=True,
            )
            barrier.wait(timeout=5.0)
            local_bm.process_turn(query)
            thread_results[label] = seen[-1] if seen else ""

        t1 = threading.Thread(target=_run_req, args=("beta", "Dame info beta"))
        t2 = threading.Thread(target=_run_req, args=("gamma", "Dame info gamma_only"))
        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)
        self.assertIn("CONC_BETA_DATA", thread_results["beta"])
        self.assertNotIn("CONC_GAMMA_ONLY", thread_results["beta"])
        self.assertIn("CONC_GAMMA_ONLY", thread_results["gamma"])
        self.assertNotIn("CONC_BETA_DATA", thread_results["gamma"])
        self.assertTrue(m_b)

        # 4d) Revocación concurrente DURANTE recuperación (`retrieve` vs hilo revocador) +
        #     cancelación durante revalidación + fallback interno `OLLAMA_UNCENSORED` -> estándar.
        m_during_ret = self._seed_memory(
            title="Memoria carrera recuperación",
            content="RACE_RET_DATA: debe quedar excluida tras recuperarse.",
            tags=("delta_race",),
            importance=95,
        )
        m_stay_unc = self._seed_memory(
            title="Memoria estable uncensored",
            content="UNC_STABLE_DATA: detalle válido local.",
            tags=("unc_test",),
            importance=95,
        )
        orig_ret_delta = self.engine.retrieve

        def _ret_with_concurrent_thread_revoke(query, **kw):
            res = orig_ret_delta(query, **kw)
            if "delta_race" in str(query):
                t_rev = threading.Thread(
                    target=lambda: self.engine.revoke_memory(m_during_ret, reason="Carrera durante retrieve"),
                    daemon=True,
                )
                t_rev.start()
                t_rev.join(timeout=5.0)
            return res

        ollama_unc = OllamaBrain(
            model_name="qwen2.5-coder:3b",
            uncensored_model_name="uncensored-missing:3b",
        )
        unc_calls: list[dict] = []

        def _unc_chat(*, model, messages, stream, options):
            unc_calls.append({"model": model, "messages": list(messages)})
            if model == "uncensored-missing:3b":
                self.engine.revoke_memory(m_during_ret, reason="Revocada antes de estándar") if self.engine.get_memory(m_during_ret).status == MemoryStatus.ACTIVE else None
                err = RuntimeError("model 'uncensored-missing:3b' not found")
                err.status_code = 404  # type: ignore[attr-defined]
                raise err
            return iter([{"message": {"content": "Fallback estándar Ollama OK."}}])

        ollama_unc.client = MagicMock()
        ollama_unc.client.chat.side_effect = _unc_chat

        bm_race = BrainManager(
            initial_mode="OLLAMA",
            ollama_brain=ollama_unc,
            memory=ConversationMemory(max_turns=4, storage_file=None, auto_save=False),
            memory_engine=self.engine,
            lazy_init=True,
        )
        with patch.object(self.engine, "retrieve", side_effect=_ret_with_concurrent_thread_revoke):
            res_race_turn = bm_race.process_turn("Consulta delta_race urgente")
        self.assertFalse(res_race_turn.is_error)
        self.assertNotIn("RACE_RET_DATA", json.dumps(unc_calls, ensure_ascii=False))

        # Cancelación/interrupción durante validación: jamás marca el snapshot válido ni filtra texto obsoleto.
        m_cancel = self._seed_memory(
            title="Memoria cancelación",
            content="CANCEL_SECRET_TXT: dato durante cancelación.",
            tags=("cancel_test",),
            importance=95,
        )
        g_cancel = self.engine.create_snapshot_guard(request_id="req-cancel")
        s_cancel = g_cancel.capture_from_retrieval(self.engine.retrieve("cancel_test"), request_id="req-cancel")
        orig_gmem = self.engine.get_memory

        def _raise_cancel(mid: str):
            if mid == m_cancel:
                raise RuntimeError("cancelled_by_caller")
            return orig_gmem(mid)

        with patch.object(self.engine, "get_memory", side_effect=_raise_cancel):
            out_cancel = g_cancel.validate_before_dispatch(s_cancel, request_id="req-cancel")
        self.assertFalse(out_cancel.n8_allowed)
        self.assertIsNone(out_cancel.untrusted_n8_context)
        self.assertFalse(s_cancel.validated_before_dispatch)
        assert out_cancel.snapshot is None or not out_cancel.snapshot.items
        self.assertNotIn("CANCEL_SECRET_TXT", str(out_cancel))

        # También probamos `OLLAMA_UNCENSORED` 404 -> `OLLAMA` estándar con revocación en medio del retry interno.
        m_unc_drop = self._seed_memory(
            title="Memoria drop uncensored",
            content="UNC_DROPPED_DATA: debe caer antes de estándar.",
            tags=("unc_test",),
            importance=92,
        )
        unc_calls.clear()

        def _unc_404_then_standard(*, model, messages, stream, options):
            unc_calls.append({"model": model, "messages": list(messages)})
            if model == "uncensored-missing:3b":
                self.engine.revoke_memory(m_unc_drop, reason="Revocada entre uncensored y estándar")
                err = RuntimeError("model 'uncensored-missing:3b' not found")
                err.status_code = 404  # type: ignore[attr-defined]
                raise err
            return iter([{"message": {"content": "Estándar limpio."}}])

        ollama_unc.client.chat.side_effect = _unc_404_then_standard
        bm_race.set_mode_by_choice("OLLAMA_UNCENSORED")
        res_unc = bm_race.process_turn("Consulta unc_test ahora")
        self.assertFalse(res_unc.is_error)
        self.assertEqual(len(unc_calls), 2)
        self.assertIn("UNC_DROPPED_DATA", json.dumps(unc_calls[0]["messages"], ensure_ascii=False))
        self.assertNotIn("UNC_DROPPED_DATA", json.dumps(unc_calls[1]["messages"], ensure_ascii=False))
        self.assertIn("UNC_STABLE_DATA", json.dumps(unc_calls[1]["messages"], ensure_ascii=False))
        self.assertTrue(m_stay_unc)

    # ------------------------------------------------------------------
    # 5. Privacidad en el punto efectivo de dispatch + anti-bypass programático
    # ------------------------------------------------------------------

    def test_private_mode_enforcement_at_effective_dispatch_and_anti_bypass(self) -> None:
        """Verifica /privado (frescura con cero persistencia) y /privado --sin-historial anti-bypass en dispatch."""
        m_hist = self._seed_memory(
            title="Historial privado seguro",
            content="PRIV_HIST_DATA: referencia previa permitida en /privado.",
            tags=("privado_test",),
            importance=95,
        )
        m_hist_rev = self._seed_memory(
            title="Historial revocable en privado",
            content="PRIV_HIST_REVOCAR: debe excluirse si cambia.",
            tags=("privado_test",),
            importance=90,
        )

        # Opción A: `/privado` (allow_history=True) permite histórico con validación pre-dispatch y revocación respetada.
        # Nota: salimos brevemente solo para revocar entre primario y fallback usando una mutación administrativa
        # o antes del fallback en engine interno: aquí entramos luego de capturar en `/privado`.
        self.conv_memory.enter_private_mode(allow_history=True)
        self.engine.set_private_mode(enabled=True, allow_history=True)

        ollama_priv = OllamaBrain()
        priv_msgs: list[list[dict]] = []

        def _chat_priv(*, model, messages, stream, options):
            priv_msgs.append(list(messages))
            return iter([{"message": {"content": "Privado OK."}}])

        ollama_priv.client = MagicMock()
        ollama_priv.client.chat.side_effect = _chat_priv

        bm_priv = BrainManager(
            initial_mode="OLLAMA",
            ollama_brain=ollama_priv,
            memory=self.conv_memory,
            memory_engine=self.engine,
            lazy_init=True,
        )
        turn_opt_a = bm_priv.process_turn("Consulta privado_test confidencial")
        self.assertFalse(turn_opt_a.is_error)
        self.assertIn("PRIV_HIST_DATA", json.dumps(priv_msgs[0], ensure_ascii=False))
        self.assertTrue(self.conv_memory._save_to_disk() is False)
        self.assertTrue(m_hist_rev and m_hist)

        # Opción B: `--sin-historial` -> no crea snapshots, bloquea bypass programático en `query_raw` incluso
        # si un llamador pasa `allow_history=True` y un `MemorySnapshot` previamente capturado!
        self.conv_memory.exit_private_mode()
        self.engine.set_private_mode(enabled=False, allow_history=True)
        guard_pre = self.engine.create_snapshot_guard(request_id="req-pre-incognito")
        snap_pre = guard_pre.capture_from_retrieval(
            self.engine.retrieve("privado_test"),
            request_id="req-pre-incognito",
        )

        # Activamos ahora `--sin-historial`.
        self.conv_memory.enter_private_mode(allow_history=False)
        self.engine.set_private_mode(enabled=True, allow_history=False)

        with self.assertRaises(MemoryPrivateModeError):
            guard_pre.capture_from_retrieval(
                self.engine.retrieve("privado_test"),
                request_id="req-pre-incognito",
                allow_history=False,
            )

        priv_msgs.clear()
        txt_bypass, _p, _m, _f, fail_bypass = bm_priv.query_raw(
            "Responde sin revelar historia previa.",
            snapshot_guard=guard_pre,
            memory_snapshot=snap_pre,
            request_id="req-pre-incognito",
            allow_history=True,  # Intento programático explícito de eludir `--sin-historial`.
        )
        self.assertFalse(fail_bypass)
        self.assertEqual(len(priv_msgs), 1)
        sent_dump = json.dumps(priv_msgs[0], ensure_ascii=False)
        self.assertNotIn("PRIV_HIST_DATA", sent_dump)
        self.assertNotIn("PRIV_HIST_REVOCAR", sent_dump)
        self.assertEqual(bm_priv.last_n8_guard_status, SnapshotValidationStatus.BLOCKED_PRIVACY.value)
        self.assertIn("Privado OK.", txt_bypass)

        # Verificamos además en modo privado con N6 `EphemeralGoalStore` -> 0 archivos creados en disco.
        ge_priv = GoalEngine(
            skill_manager=SkillManager(),
            goal_store=GoalStore(store_dir=self.goal_dir),
            brain_manager=bm_priv,
            memory_engine=self.engine,
            use_ai=True,
            is_private_mode_fn=lambda: self.conv_memory.is_private_mode,
            allow_history_in_private_fn=lambda: self.conv_memory.private_allow_history,
        )
        self.assertIsInstance(ge_priv.active_goal_store, EphemeralGoalStore)
        priv_msgs.clear()
        plan_priv = ge_priv.create_goal_and_plan(
            "Analizar rendimiento sistema",
            constraints=Constraints(read_only=True, allow_file_modifications=False),
        )
        self.assertTrue(plan_priv.is_ephemeral_private)
        self.assertEqual(list(self.goal_dir.rglob("*.goal.json")), [])
        self.assertEqual(list(self.goal_dir.rglob("*.wal.jsonl")), [])
        self.assertFalse((self.goal_dir / "active_goal.json").exists())
        self.assertNotIn("PRIV_HIST_DATA", json.dumps(priv_msgs, ensure_ascii=False))

        # Si el turno arranca bajo Opción A (`/privado`), falla el proveedor primario (Claude) y
        # antes del fallback se activa `--sin-historial`, Ollama (fallback) NO recibe memoria N8.
        self.conv_memory.enter_private_mode(allow_history=True)
        self.engine.set_private_mode(enabled=True, allow_history=True)
        claude_priv_fail = ClaudeBrain(api_key="dummy-claude-offline-cred")
        ollama_priv_fb = OllamaBrain()
        fb_priv_msgs: list[list[dict]] = []

        def _chat_priv_fb(*, model, messages, stream, options):
            fb_priv_msgs.append(list(messages))
            return iter([{"message": {"content": "Fallback privado limpio."}}])

        ollama_priv_fb.client = MagicMock()
        ollama_priv_fb.client.chat.side_effect = _chat_priv_fb

        def _urlopen_switch_no_hist(req, timeout=15.0):
            self.conv_memory.enter_private_mode(allow_history=False)
            self.engine.set_private_mode(enabled=True, allow_history=False)
            raise urllib.error.HTTPError(
                url=req.full_url,
                code=503,
                msg="Unavailable",
                hdrs=None,  # type: ignore[arg-type]
                fp=io.BytesIO(b'{"error":{"type":"api_error","message":"down"}}'),
            )

        bm_switch_priv = BrainManager(
            initial_mode="CLAUDE",
            claude_brain=claude_priv_fail,
            gemini_brain=GeminiBrain(api_key=""),
            ollama_brain=ollama_priv_fb,
            memory=self.conv_memory,
            memory_engine=self.engine,
            lazy_init=True,
        )
        with patch("urllib.request.urlopen", side_effect=_urlopen_switch_no_hist):
            turn_sw = bm_switch_priv.process_turn("Consulta privado_test confidencial")
        self.assertFalse(turn_sw.is_error)
        self.assertEqual(len(fb_priv_msgs), 1)
        self.assertNotIn("PRIV_HIST_DATA", json.dumps(fb_priv_msgs[0], ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
