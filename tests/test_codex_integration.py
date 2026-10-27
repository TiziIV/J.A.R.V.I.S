"""
Suite de pruebas unitarias para la integración oficial de OpenAI Codex CLI (`src/brain/codex_client.py`),
su integración con `BrainManager` / `AutoDeliberator` y la guardia de seguridad de Git (`src/utils/git_guard.py`).

Todas las pruebas se ejecutan 100% offline mediante `unittest.mock`, sin requerir conexión a Internet,
ni tener Codex CLI instalado ni una sesión autenticada real en la máquina.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from config.settings import BASE_DIR, BRAIN_MODES
from src.brain.brain_manager import ENGINE_SELECTOR_PROMPT, BrainManager
from src.brain.codex_client import (
    CodexAuthStatus,
    CodexBrain,
    classify_codex_task,
    sanitize_output,
)
from src.brain.deliberation import AutoDeliberator
from src.brain.exceptions import BrainError, BrainErrorKind
from src.utils.git_guard import (
    GitSafetyGuard,
    GitTreeSnapshot,
    is_sensitive_path,
    scan_text_for_secrets,
)


def _completed_proc(
    args: list[str] | None = None,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    """Crea un `CompletedProcess` simulado para pruebas de `subprocess.run`."""
    return subprocess.CompletedProcess(
        args=args or ["codex"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class TestCodexClientUnit(unittest.TestCase):
    """Pruebas unitarias aisladas de `CodexBrain` (`src/brain/codex_client.py`)."""

    def setUp(self) -> None:
        self.client = CodexBrain(
            enabled=True,
            timeout=30.0,
            workspace_dir=BASE_DIR,
            allowed_root=BASE_DIR,
            sandbox_mode="workspace-write",
            model_name="",
            allow_auto=True,
        )

    def test_codex_is_installed_and_get_version(self) -> None:
        """Verifica detección del ejecutable en Windows y obtención de versión sin shell=True."""
        with (
            patch("src.brain.codex_client.shutil.which", return_value=str(BASE_DIR / "main.py")),
            patch(
                "src.brain.codex_client.subprocess.run",
                return_value=_completed_proc(returncode=0, stdout="codex-cli 0.104.0\n"),
            ) as mock_run,
        ):
            self.assertTrue(self.client.is_installed())
            version = self.client.get_version()
            self.assertEqual(version, "codex-cli 0.104.0")
            _, kwargs = mock_run.call_args
            self.assertFalse(kwargs.get("shell"), "Jamás debe ejecutarse con shell=True")

    def test_codex_absent_returns_clear_install_hint(self) -> None:
        """Comprueba que si Codex no está instalado se informe claramente sin intentar instalarlo."""
        with patch("src.brain.codex_client.shutil.which", return_value=None):
            self.assertFalse(self.client.is_installed())
            self.assertIsNone(self.client.get_version())
            auth = self.client.check_auth_status()
            self.assertFalse(auth.authenticated)
            self.assertEqual(auth.method, "not_installed")
            self.assertIn("codex login", auth.message)

            with self.assertRaises(BrainError) as ctx:
                self.client.run_readonly_task("Explica main.py", raise_on_error=True)
            self.assertEqual(ctx.exception.kind, BrainErrorKind.UNCONFIGURED)
            self.assertIn("https://developers.openai.com/codex/cli/", ctx.exception.user_message)

    def test_codex_auth_status_available_chatgpt_and_api_key_masked(self) -> None:
        """Comprueba detección de sesión activa mediante ChatGPT y enmascaramiento de secretos."""
        with (
            patch.object(self.client, "resolve_executable", return_value=str(BASE_DIR / "main.py")),
            patch(
                "src.brain.codex_client.subprocess.run",
                return_value=_completed_proc(returncode=0, stdout="Logged in using ChatGPT (plus)\n"),
            ),
        ):
            status = self.client.check_auth_status()
            self.assertTrue(bool(status))
            self.assertTrue(status.authenticated)
            self.assertEqual(status.method, "chatgpt")

        # Si el CLI reporta una clave API, sanitize_output no debe filtrarla
        secret = "sk-" + "proj-SUPERSECRETKEY1234567890"
        sanitized = sanitize_output(f"Logged in using an API key: {secret}")
        self.assertNotIn(secret, sanitized)
        self.assertIn("[REDACTED]", sanitized)

    def test_codex_auth_status_missing_prompts_manual_codex_login(self) -> None:
        """Comprueba que si no hay sesión activa se indique ejecutar `codex login` manualmente."""
        with (
            patch.object(self.client, "resolve_executable", return_value=str(BASE_DIR / "main.py")),
            patch(
                "src.brain.codex_client.subprocess.run",
                return_value=_completed_proc(returncode=1, stdout="Not logged in\n"),
            ),
        ):
            status = self.client.check_auth_status()
            self.assertFalse(status.authenticated)
            self.assertEqual(status.method, "unauthenticated")
            self.assertIn("codex login", status.message)

            with self.assertRaises(BrainError) as ctx:
                self.client.run_readonly_task("Analiza este archivo", raise_on_error=True)
            self.assertEqual(ctx.exception.kind, BrainErrorKind.AUTH)
            self.assertIn("codex login", ctx.exception.user_message)

    def test_codex_timeout_handled_cleanly(self) -> None:
        """Comprueba que un TimeoutExpired de subprocess se convierta en BrainErrorKind.TIMEOUT."""
        with (
            patch.object(self.client, "resolve_executable", return_value=str(BASE_DIR / "main.py")),
            patch.object(
                self.client,
                "check_auth_status",
                return_value=CodexAuthStatus(True, "chatgpt", "OK", 0),
            ),
            patch(
                "src.brain.codex_client.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd=["codex", "exec"], timeout=15.0),
            ),
        ):
            with self.assertRaises(BrainError) as ctx:
                self.client.run_readonly_task("Analiza todo el proyecto", timeout=15.0, raise_on_error=True)
            self.assertEqual(ctx.exception.kind, BrainErrorKind.TIMEOUT)
            self.assertEqual(ctx.exception.status_code, 504)

    def test_codex_nonzero_exit_code_handled(self) -> None:
        """Comprueba que un código de salida distinto de cero se clasifique como EXECUTION o AUTH."""
        with (
            patch.object(self.client, "resolve_executable", return_value=str(BASE_DIR / "main.py")),
            patch.object(
                self.client,
                "check_auth_status",
                return_value=CodexAuthStatus(True, "chatgpt", "OK", 0),
            ),
            patch(
                "src.brain.codex_client.subprocess.run",
                return_value=_completed_proc(returncode=2, stderr="Error interno de ejecución"),
            ),
        ):
            with self.assertRaises(BrainError) as ctx:
                self.client.run_readonly_task("Explica esta función", raise_on_error=True)
            self.assertEqual(ctx.exception.kind, BrainErrorKind.EXECUTION)
            self.assertEqual(ctx.exception.status_code, 2)

    def test_codex_valid_jsonl_stream_extracts_only_agent_message(self) -> None:
        """
        Comprueba que `codex exec --json` extraiga únicamente el evento `item.completed` de tipo
        `agent_message` e ignore eventos internos (`thread.started`, `turn.started`, `reasoning`, `turn.completed`).
        """
        jsonl_lines = [
            json.dumps({"type": "thread.started", "thread_id": "thread_123"}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "item_0", "type": "reasoning", "text": "Pensamiento interno que no es respuesta final"},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_1",
                        "type": "agent_message",
                        "text": "La función `parse_args` valida `--voice` y `--tts` correctamente.",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 120, "cached_input_tokens": 0, "output_tokens": 25},
                }
            ),
        ]
        stdout_payload = "\n".join(jsonl_lines)

        with (
            patch.object(self.client, "resolve_executable", return_value=str(BASE_DIR / "main.py")),
            patch.object(
                self.client,
                "check_auth_status",
                return_value=CodexAuthStatus(True, "chatgpt", "OK", 0),
            ),
            patch(
                "src.brain.codex_client.subprocess.run",
                return_value=_completed_proc(returncode=0, stdout=stdout_payload),
            ) as mock_run,
        ):
            reply = self.client.run_readonly_task("Explica parse_args en main.py", raise_on_error=True)
            self.assertEqual(reply, "La función `parse_args` valida `--voice` y `--tts` correctamente.")
            self.assertNotIn("Pensamiento interno", reply)
            cmd_used = mock_run.call_args[0][0]
            self.assertIn("exec", cmd_used)
            self.assertIn("--json", cmd_used)
            self.assertIn("--sandbox", cmd_used)
            self.assertIn("read-only", cmd_used)

    def test_codex_invalid_or_incomplete_json_output_handled(self) -> None:
        """Comprueba que salidas no-JSON o streams truncados sin completar turno lancen INVALID_RESPONSE."""
        # Caso 1: Texto corrupto no JSON
        with self.assertRaises(BrainError) as ctx_bad:
            self.client._parse_codex_jsonl_output("ESTO NO ES JSON EN ABSOLUTO {{{")
        self.assertEqual(ctx_bad.exception.kind, BrainErrorKind.INVALID_RESPONSE)

        # Caso 2: Stream truncado después de turn.started sin mensaje ni turn.completed
        truncated_stream = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "t1"}),
                json.dumps({"type": "turn.started"}),
            ]
        )
        with self.assertRaises(BrainError) as ctx_inc:
            self.client._parse_codex_jsonl_output(truncated_stream)
        self.assertEqual(ctx_inc.exception.kind, BrainErrorKind.INVALID_RESPONSE)

    def test_codex_empty_output_raises_empty_response(self) -> None:
        """Comprueba que una salida vacía o sin mensaje final lance BrainErrorKind.EMPTY_RESPONSE."""
        with self.assertRaises(BrainError) as ctx_empty:
            self.client._parse_codex_jsonl_output("   \n  ")
        self.assertEqual(ctx_empty.exception.kind, BrainErrorKind.EMPTY_RESPONSE)

    def test_codex_os_error_and_keyboard_interrupt_do_not_crash(self) -> None:
        """Comprueba que errores de sistema operativo o interrupciones se capturen como mensajes controlados."""
        with (
            patch.object(self.client, "resolve_executable", return_value=str(BASE_DIR / "main.py")),
            patch.object(
                self.client,
                "check_auth_status",
                return_value=CodexAuthStatus(True, "chatgpt", "OK", 0),
            ),
            patch("src.brain.codex_client.subprocess.run", side_effect=OSError("Permiso denegado en Windows")),
        ):
            msg = self.client.run_readonly_task("Revisa este archivo", raise_on_error=False)
            self.assertIn("Error del sistema operativo", msg)
            self.assertIsNotNone(self.client.last_error)
            assert self.client.last_error is not None
            self.assertEqual(self.client.last_error.kind, BrainErrorKind.EXECUTION)

    def test_readonly_task_uses_read_only_sandbox_and_review_never_writes(self) -> None:
        """
        Verifica que una solicitud explícita de revisión ('Usá Codex para revisar este archivo')
        se clasifique como `readonly` y jamás utilice `workspace-write`.
        """
        cls_review = classify_codex_task("revisar este archivo y encontrar posibles bugs")
        self.assertEqual(cls_review.task_type, "readonly")
        self.assertEqual(cls_review.sandbox, "read-only")
        self.assertFalse(cls_review.authorized_write)

        ok_jsonl = json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "1", "type": "agent_message", "text": "Revisión completada sin tocar archivos."},
            }
        )
        with (
            patch.object(self.client, "resolve_executable", return_value=str(BASE_DIR / "main.py")),
            patch.object(
                self.client,
                "check_auth_status",
                return_value=CodexAuthStatus(True, "chatgpt", "OK", 0),
            ),
            patch(
                "src.brain.codex_client.subprocess.run",
                return_value=_completed_proc(returncode=0, stdout=ok_jsonl),
            ) as mock_run,
        ):
            out = self.client.generate_response("Revisa el archivo config/settings.py")
            self.assertEqual(out, "Revisión completada sin tocar archivos.")
            self.assertEqual(self.client.last_task_type, "readonly")
            self.assertEqual(self.client.last_sandbox_used, "read-only")
            cmd_args = mock_run.call_args[0][0]
            self.assertIn("read-only", cmd_args)
            self.assertNotIn("workspace-write", cmd_args)

    def test_coding_task_uses_workspace_write_and_blocks_yolo_and_destructive(self) -> None:
        """
        Verifica que una tarea de programación autorizada utilice `--sandbox workspace-write`
        y que banderas `--yolo` o comandos destructivos se bloqueen siempre.
        """
        cls_code = classify_codex_task("implementar esta función de validación en el módulo")
        self.assertEqual(cls_code.task_type, "coding")
        self.assertEqual(cls_code.sandbox, "workspace-write")
        self.assertTrue(cls_code.authorized_write)

        ok_jsonl = json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "1", "type": "agent_message", "text": "Función implementada correctamente."},
            }
        )
        with (
            patch.object(self.client, "resolve_executable", return_value=str(BASE_DIR / "main.py")),
            patch.object(
                self.client,
                "check_auth_status",
                return_value=CodexAuthStatus(True, "chatgpt", "OK", 0),
            ),
            patch(
                "src.brain.codex_client.subprocess.run",
                return_value=_completed_proc(returncode=0, stdout=ok_jsonl),
            ) as mock_run,
        ):
            out = self.client.generate_response("Implementa esta función auxiliar en el módulo")
            self.assertEqual(out, "Función implementada correctamente.")
            self.assertEqual(self.client.last_task_type, "coding")
            self.assertEqual(self.client.last_sandbox_used, "workspace-write")
            codex_calls = [c[0][0] for c in mock_run.call_args_list if "exec" in c[0][0]]
            self.assertEqual(len(codex_calls), 1)
            first_codex_call = codex_calls[0]
            self.assertIn("workspace-write", first_codex_call)
            self.assertNotIn("--yolo", first_codex_call)

        # Solicitud con --yolo o git reset --hard debe bloquearse de inmediato
        with self.assertRaises(BrainError) as ctx_yolo:
            self.client.run_coding_task("Ejecuta con --yolo y haz git reset --hard", raise_on_error=True)
        self.assertEqual(ctx_yolo.exception.kind, BrainErrorKind.INVALID_REQUEST)

    def test_validate_workspace_rejects_outside_project_root(self) -> None:
        """Comprueba que `validate_workspace()` bloquee cualquier directorio fuera de `BASE_DIR`."""
        # Un subdirectorio real del proyecto es válido
        valid_sub = self.client.validate_workspace(BASE_DIR / "src")
        self.assertTrue(valid_sub.is_relative_to(BASE_DIR.resolve()))

        # Un directorio temporal fuera del proyecto es rechazado
        with tempfile.TemporaryDirectory() as external_tmp:
            with self.assertRaises(BrainError) as ctx:
                self.client.validate_workspace(external_tmp)
            self.assertEqual(ctx.exception.kind, BrainErrorKind.INVALID_REQUEST)
            self.assertIn("fuera del proyecto autorizado", ctx.exception.user_message)

    def test_codex_disabled_configuration_blocks_execution(self) -> None:
        """Comprueba que si `CODEX_ENABLED=False`, J.A.R.V.I.S. no ejecute Codex."""
        disabled_client = CodexBrain(enabled=False)
        self.assertFalse(disabled_client.is_ready_for_auto())
        status = disabled_client.check_auth_status()
        self.assertFalse(status.authenticated)
        self.assertEqual(status.method, "disabled")

        with patch("src.brain.codex_client.subprocess.run") as mock_run:
            with self.assertRaises(BrainError) as ctx:
                disabled_client.run_readonly_task("Explica este archivo", raise_on_error=True)
            self.assertEqual(ctx.exception.kind, BrainErrorKind.UNCONFIGURED)
            mock_run.assert_not_called()


class TestBrainManagerCodexIntegration(unittest.TestCase):
    """Pruebas de integración de `CODEX` con `BrainManager`, selector, One-Shot y modo `AUTO`."""

    def test_codex_in_brain_modes_and_selector_and_one_shot(self) -> None:
        """
        Verifica que `CODEX` esté en `BRAIN_MODES`, que el selector muestre la pregunta exacta
        y que One-Shot acepte frases como 'Usá Codex para revisar este archivo'.
        """
        self.assertIn("CODEX", BRAIN_MODES)

        mock_codex = MagicMock()
        mock_codex.last_used_model = "codex-cli"
        mock_codex.last_fallback_note = None
        mock_codex.last_git_summary = None
        mock_codex.generate_response.return_value = "Análisis de Codex completado."

        mgr = BrainManager(
            initial_mode="AUTO",
            codex_brain=mock_codex,
            lazy_init=True,
        )

        # 1. Apertura del selector muestra la pregunta exacta requerida
        res_open = mgr.process_turn("cambiar motor")
        self.assertEqual(res_open.response, ENGINE_SELECTOR_PROMPT)
        self.assertTrue(mgr.awaiting_mode_choice)

        # 2. Selección manual de CODEX en el selector
        res_pick = mgr.process_turn("Codex")
        self.assertEqual(mgr.global_mode, "CODEX")
        self.assertIn("CODEX", res_pick.response)

        # 3. Volvemos a AUTO y probamos One-Shot con "Usá Codex para revisar este archivo"
        mgr.set_mode_by_choice("AUTO")
        self.assertEqual(mgr.global_mode, "AUTO")

        turn_os = mgr.process_turn("Usá Codex para revisar este archivo")
        self.assertEqual(turn_os.selection_source, "ONE_SHOT")
        self.assertEqual(turn_os.effective_mode, "CODEX")
        self.assertEqual(turn_os.final_provider, "Codex")
        self.assertEqual(mgr.global_mode, "AUTO", "One-Shot no debe alterar global_mode")

    def test_auto_mode_selects_codex_when_ready_and_falls_back_when_unavailable(self) -> None:
        """
        Verifica que `AUTO` elija `CODEX` para tareas de programación en el repositorio solo cuando
        está disponible, y caiga a `GEMINI` / `OLLAMA` cuando Codex no está disponible.
        """
        query = "Refactoriza la función de validación en src/brain/brain_manager.py y agrega una prueba unitaria."

        # Caso 1: Codex habilitado, instalado y autenticado -> AUTO elige CODEX
        delib_ready = AutoDeliberator(
            default_cloud_provider="GEMINI",
            code_cloud_provider="GEMINI",
            codex_enabled=True,
            codex_allow_auto=True,
            codex_ready_fn=lambda: True,
        )
        dec_ready = delib_ready.deliberate(query)
        self.assertEqual(dec_ready.target_mode, "CODEX")

        # Caso 2: Codex no instalado o sin autenticar -> AUTO no inventa disponibilidad, usa motor alternativo
        delib_not_ready = AutoDeliberator(
            default_cloud_provider="GEMINI",
            code_cloud_provider="GEMINI",
            codex_enabled=True,
            codex_allow_auto=True,
            codex_ready_fn=lambda: False,
        )
        dec_fallback = delib_not_ready.deliberate(query)
        self.assertNotEqual(dec_fallback.target_mode, "CODEX")
        self.assertIn(dec_fallback.target_mode, ("GEMINI", "OLLAMA"))

        # Caso 3: Si CODEX es seleccionado pero falla en ejecución/configuración, BrainManager hace fallback a Gemini
        unconfigured_codex = CodexBrain(enabled=False)
        mock_gemini = MagicMock()
        mock_gemini.model_name = "gemini-3-flash-preview"
        mock_gemini.last_used_model = "gemini-3-flash-preview"
        mock_gemini.last_fallback_note = None
        mock_gemini.generate_response.return_value = "Respuesta de respaldo desde Gemini."

        mgr = BrainManager(
            initial_mode="CODEX",
            codex_brain=unconfigured_codex,
            gemini_brain=mock_gemini,
            lazy_init=True,
        )
        turn = mgr.process_turn("Revisa el módulo de sesión")
        self.assertFalse(turn.is_error)
        self.assertEqual(turn.final_provider, "Gemini")
        self.assertIn("Codex", turn.fallback_info or "")


class TestGitSafetyGuardUnit(unittest.TestCase):
    """Pruebas unitarias de protección de cambios preexistentes y prevención de archivos sensibles en Git."""

    def test_git_guard_blocks_sensitive_files_and_secrets_and_git_add_dot(self) -> None:
        """
        Verifica que `.env`, `.env.*`, `prompts_history.txt`, `data/*`, `*.log` y comodines (`.` / `-A`)
        sean bloqueados antes de llegar a `git add`, permitiendo `.env.example`.
        """
        self.assertTrue(is_sensitive_path(".env"))
        self.assertTrue(is_sensitive_path(".env.local"))
        self.assertTrue(is_sensitive_path(".env.production"))
        self.assertTrue(is_sensitive_path("prompts_history.txt"))
        self.assertTrue(is_sensitive_path("data/conversations.json"))
        self.assertTrue(is_sensitive_path("logs/jarvis.log"))
        self.assertFalse(is_sensitive_path(".env.example"))
        self.assertFalse(is_sensitive_path("src/brain/codex_client.py"))

        guard = GitSafetyGuard(repo_dir=BASE_DIR)

        # Bloqueo de `git add .`
        ok_dot, msg_dot = guard.validate_commit_candidates(["."])
        self.assertFalse(ok_dot)
        self.assertIn("indiscriminada", msg_dot)

        # Bloqueo de `.env` y `prompts_history.txt`
        ok_env, msg_env = guard.validate_commit_candidates([".env"])
        self.assertFalse(ok_env)
        self.assertIn("sensible", msg_env)

        ok_hist, msg_hist = guard.validate_commit_candidates(["prompts_history.txt"])
        self.assertFalse(ok_hist)
        self.assertIn("sensible", msg_hist)

        # Escaneo de secretos en diff
        fake_diff = (
            "+GEMINI_API_KEY=" + "AIza" + "SyFakeKeyForUnitTest123456789\n"
            "+sk-" + "ant-api03-1234567890abcdef"
        )
        findings = scan_text_for_secrets(fake_diff)
        self.assertGreaterEqual(len(findings), 2)

    def test_git_guard_protects_preexisting_changes_and_separates_commit_push(self) -> None:
        """
        Verifica que los archivos con cambios preexistentes en `baseline` no puedan mezclarse
        en el commit de una tarea nueva y que commit/push exijan autorización explícita.
        """
        baseline = GitTreeSnapshot(
            branch="main",
            root_dir=BASE_DIR,
            remotes={"origin": "https://github.com/TiziIV/J.A.R.V.I.S.git"},
            modified_files=("config/settings.py", "main.py"),
            untracked_files=("src/brain/brain_manager.py",),
            staged_files=(),
            tracked_files=("config/settings.py", "main.py"),
            raw_status="## main...origin/main",
        )
        after = GitTreeSnapshot(
            branch="main",
            root_dir=BASE_DIR,
            remotes={"origin": "https://github.com/TiziIV/J.A.R.V.I.S.git"},
            modified_files=("config/settings.py", "main.py"),
            untracked_files=("src/brain/brain_manager.py", "src/brain/codex_client.py"),
            staged_files=(),
            tracked_files=("config/settings.py", "main.py"),
            raw_status="## main...origin/main",
        )

        guard = GitSafetyGuard(repo_dir=BASE_DIR)
        task_only, preexisting = guard.detect_task_changes(baseline, after)
        self.assertEqual(task_only, ["src/brain/codex_client.py"])
        self.assertIn("config/settings.py", preexisting)
        self.assertIn("src/brain/brain_manager.py", preexisting)

        # Intentar preparar un archivo que tenía cambios preexistentes se detiene de forma segura
        ok_pre, msg_pre = guard.validate_commit_candidates(
            ["config/settings.py", "src/brain/codex_client.py"],
            baseline=baseline,
        )
        self.assertFalse(ok_pre)
        self.assertIn("cambios preexistentes", msg_pre)

        # Commit y Push sin autorización explícita del usuario son rechazados siempre
        ok_commit, msg_commit, commit_hash = guard.commit_staged_changes(
            "feat(codex): integrar Codex CLI como motor de programación",
            user_authorized=False,
            tests_passed=True,
        )
        self.assertFalse(ok_commit)
        self.assertIsNone(commit_hash)
        self.assertIn("autorización", msg_commit)

        ok_push, msg_push = guard.push_to_origin_main(user_authorized=False)
        self.assertFalse(ok_push)
        self.assertIn("autorización", msg_push)


if __name__ == "__main__":
    unittest.main()
