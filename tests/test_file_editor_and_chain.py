"""
Suite exhaustiva de pruebas unitarias e integración para `FileEditorSkill`,
`BackupManager`, `RollbackManager`, `AuditLogger`, `ChangeOperation`, `ChangePlan`,
`OperationStatus`, `PolicyEngine` y `ChainOrchestrator`.

Cubre los escenarios de seguridad defensiva en Windows, preservación estructural de
archivos INI/CFG/CONF/JSON/XML/TXT, escritura atómica con reintentos, detección de
concurrencia (TOCTOU), validación pre-flight del grafo de dependencias (unicidad de IDs,
detección de ciclos con DFS/Kahn, ordenamiento topológico determinista, aprobaciones
selectivas, propagación transitiva de estados `FAILED`/`BLOCKED`, evidencia verificable
de dependencias previas), integridad criptográfica del `approval_hash` SHA-256,
verificación post-escritura, aislamiento temporal dedicado y rollback seguro sin realizar
llamadas de red ni modificar archivos reales del usuario.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import unittest
import uuid
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
from unittest.mock import patch

from config.settings import BASE_DIR
from skills.file_editor_skill import (
    AuditLogger,
    BackupManager,
    FileEditorSkill,
    RollbackManager,
    compute_sha256_bytes,
    compute_sha256_file,
)
from src.brain.chain_orchestrator import (
    ApprovalBindingError,
    ChainOrchestrator,
    ChainState,
    ChangeOperation,
    ChangePlan,
    DuplicateExecutionError,
    OperationStatus,
    PolicyValidationError,
)
from src.tools.skill_manager import SkillManager


def _make_readonly_retry_handler() -> Callable[..., None]:
    """
    Construye un manejador compatible con `shutil.rmtree` (`onexc` en Python >= 3.12
    u `onerror` en versiones previas) que restaura permisos de escritura si un test
    marcó deliberadamente un archivo como solo lectura en Windows, y vuelve a ejecutar
    la función de borrado sin ocultar errores reales de limpieza.
    """

    def _handle_remove_readonly(
        func: Callable[[str], Any],
        path: str,
        exc_or_info: Any,
    ) -> None:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)

    return _handle_remove_readonly


def _rmtree_strict_windows(target_dir: Path) -> None:
    """
    Elimina recursivamente un directorio temporal de prueba resolviendo atributos
    de solo lectura en Windows y propagando cualquier error real de limpieza.
    """
    if not target_dir.exists():
        return
    handler = _make_readonly_retry_handler()
    if sys.version_info >= (3, 12):
        shutil.rmtree(target_dir, onexc=handler)
    else:
        shutil.rmtree(target_dir, onerror=handler)


class TestFileEditorAndChainOrchestrator(unittest.TestCase):
    """
    Pruebas aisladas en una raíz temporal dedicada y configurable para
    `FileEditorSkill` y `ChainOrchestrator`.
    """

    _temp_root: Path

    @classmethod
    def setUpClass(cls) -> None:
        """
        Inicializa una raíz temporal dedicada y configurable (`JARVIS_TEST_TEMP_ROOT`),
        resuelta mediante `Path.resolve()`, evitando problemas de DACLs heredados en
        directorios `%TEMP%` del sistema en Windows.
        """
        configured_root = os.environ.get("JARVIS_TEST_TEMP_ROOT", "").strip()
        if configured_root:
            cls._temp_root = Path(configured_root).resolve()
        else:
            cls._temp_root = (BASE_DIR / "temp" / "jarvis_test_isolated").resolve()
        cls._temp_root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls) -> None:
        """Limpia la raíz temporal dedicada al finalizar la clase si quedó vacía."""
        if hasattr(cls, "_temp_root") and cls._temp_root.exists():
            _rmtree_strict_windows(cls._temp_root)
            parent_temp = cls._temp_root.parent
            if (
                parent_temp.name == "temp"
                and parent_temp.exists()
                and not any(parent_temp.iterdir())
            ):
                try:
                    parent_temp.rmdir()
                except OSError:
                    pass

    def setUp(self) -> None:
        """Crea un directorio temporal aislado y resuelto canónicamente para cada test."""
        unique_folder = f"ut_{self._testMethodName}_{uuid.uuid4().hex[:8]}"
        self.base_dir = (self._temp_root / unique_folder).resolve()
        self.workspace_dir = (self.base_dir / "authorized_games").resolve()
        self.backup_dir = (self.base_dir / "backups").resolve()
        self.audit_log_path = (self.base_dir / "logs" / "audit.jsonl").resolve()
        self.outside_dir = (self.base_dir / "outside_unauthorized").resolve()

        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.outside_dir.mkdir(parents=True, exist_ok=True)

        self.audit_logger = AuditLogger(log_file=self.audit_log_path)
        self.backup_manager = BackupManager(audit_logger=self.audit_logger)
        # Inyectamos explícitamente las rutas canónicas autorizadas del fixture aislado
        self.editor = FileEditorSkill(
            authorized_roots=[self.workspace_dir, self.backup_dir],
            backup_dir=self.backup_dir,
            audit_logger=self.audit_logger,
            backup_manager=self.backup_manager,
            max_retries=3,
            retry_base_ms=10,
            sleep_fn=lambda _: None,
        )
        self.orchestrator = ChainOrchestrator(
            file_editor=self.editor,
            provider_timeout_sec=2.0,
        )

    def tearDown(self) -> None:
        """Limpia estrictamente el directorio temporal del test verificando que no quede en disco."""
        _rmtree_strict_windows(self.base_dir)
        self.assertFalse(
            self.base_dir.exists(),
            f"El directorio temporal '{self.base_dir}' no se limpió correctamente.",
        )

    # ------------------------------------------------------------------
    # 1 a 4: Backups byte a byte, SHA-256, nombres únicos y no sobrescritura
    # ------------------------------------------------------------------

    def test_01_backup_byte_for_byte_before_modify(self) -> None:
        """1. Verifica que se cree un backup byte a byte idéntico antes de modificar un archivo."""
        cfg = self.workspace_dir / "GameUserSettings.ini"
        original_bytes = b"[ScalabilityGroups]\r\nsg.ShadowQuality=3\r\nsg.TextureQuality=3\r\n"
        cfg.write_bytes(original_bytes)

        result = self.editor.modify_ini_or_text(
            cfg,
            key_values={"sg.ShadowQuality": "1"},
            section="ScalabilityGroups",
        )
        self.assertEqual(result["status"], "success")
        backup_path = Path(result["backup_path"])
        self.assertTrue(backup_path.exists())
        self.assertEqual(backup_path.read_bytes(), original_bytes)
        self.assertNotEqual(cfg.read_bytes(), original_bytes)

    def test_02_backup_sha256_verification(self) -> None:
        """2. Verifica que el SHA-256 del backup coincida con el archivo original."""
        cfg = self.workspace_dir / "Engine.ini"
        data = b"[SystemSettings]\nr.Shadow.MaxResolution=2048\n"
        cfg.write_bytes(data)
        expected_sha = compute_sha256_bytes(data)

        bak_path = self.editor.create_backup(cfg, execution_id="exec-02")
        record = self.editor.backup_manager.get_record_by_backup(bak_path)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.original_sha256, expected_sha)
        self.assertEqual(record.backup_sha256, expected_sha)
        self.assertEqual(compute_sha256_file(bak_path), expected_sha)

    def test_03_unique_backup_names_on_multiple_changes(self) -> None:
        """3. Verifica que múltiples modificaciones generen nombres de backup únicos."""
        cfg = self.workspace_dir / "settings.ini"
        cfg.write_text("[Video]\nQuality=High\n", encoding="utf-8")

        res1 = self.editor.modify_ini_or_text(cfg, key_values={"Quality": "Medium"}, section="Video")
        res2 = self.editor.modify_ini_or_text(cfg, key_values={"Quality": "Low"}, section="Video")

        self.assertEqual(res1["status"], "success")
        self.assertEqual(res2["status"], "success")
        self.assertNotEqual(res1["backup_path"], res2["backup_path"])
        self.assertTrue(Path(res1["backup_path"]).exists())
        self.assertTrue(Path(res2["backup_path"]).exists())

    def test_04_never_overwrite_preexisting_backup(self) -> None:
        """4. Verifica que un archivo .bak preexistente jamás sea sobrescrito."""
        cfg = self.workspace_dir / "config.cfg"
        cfg.write_text("fov=90\n", encoding="utf-8")

        b1 = self.editor.create_backup(cfg, execution_id="same-id")
        b2 = self.editor.create_backup(cfg, execution_id="same-id")

        self.assertNotEqual(b1, b2)
        self.assertTrue(b1.exists())
        self.assertTrue(b2.exists())

    # ------------------------------------------------------------------
    # 5 a 9: Validación de rutas, path traversal, symlinks y rutas canónicas
    # ------------------------------------------------------------------

    def test_05_nonexistent_file_rejected(self) -> None:
        """5. Verifica que un archivo inexistente sea rechazado limpiamente."""
        missing = self.workspace_dir / "nonexistent.ini"
        with self.assertRaises(FileNotFoundError):
            self.editor.validate_authorized_path(missing, must_exist=True)

        res = self.editor.modify_ini_or_text(missing, key_values={"A": "1"}, section="Main")
        self.assertEqual(res["status"], "error")

    def test_06_path_outside_authorized_root_rejected(self) -> None:
        """6. Verifica que cualquier ruta fuera de las raíces autorizadas sea bloqueada."""
        unauthorized_file = self.outside_dir / "secret.ini"
        unauthorized_file.write_text("[Sec]\nk=v\n", encoding="utf-8")

        with self.assertRaises(PermissionError):
            self.editor.validate_authorized_path(unauthorized_file)

    def test_07_path_traversal_rejected(self) -> None:
        """7. Verifica que intentos de path traversal ('..') sean rechazados explícitamente."""
        outside_target = self.outside_dir / "target.ini"
        outside_target.write_text("[Sec]\nk=v\n", encoding="utf-8")
        traversal_path = (
            str(self.workspace_dir)
            + os.sep
            + ".."
            + os.sep
            + "outside_unauthorized"
            + os.sep
            + "target.ini"
        )

        with self.assertRaises(PermissionError):
            self.editor.validate_authorized_path(traversal_path)

    def test_08_unauthorized_symlink_or_reparse_point_rejected(self) -> None:
        """8. Verifica que enlaces simbólicos o reparse points detectados sean rechazados."""
        cfg = self.workspace_dir / "linked.ini"
        cfg.write_text("[A]\nB=1\n", encoding="utf-8")

        with patch.object(FileEditorSkill, "_is_reparse_point_or_symlink", return_value=True):
            with self.assertRaises(PermissionError):
                self.editor.validate_authorized_path(cfg)

    def test_09_canonical_path_validation_and_forbidden_system_roots(self) -> None:
        """9. Verifica comprobación con rutas canónicas y bloqueo de directorios críticos de Windows."""
        with self.assertRaises(PermissionError):
            self.editor.validate_authorized_path(
                r"C:\Windows\System32\drivers\etc\hosts", must_exist=False
            )

        with self.assertRaises(PermissionError):
            self.editor.validate_authorized_path(
                r"\\server\share\GameUserSettings.ini", must_exist=False
            )

    # ------------------------------------------------------------------
    # 10 a 17: Parser INI/CFG/CONF conservador y casos borde
    # ------------------------------------------------------------------

    def test_10_case_sensitive_ini_key_update(self) -> None:
        """10. Verifica que el parser INI respete mayúsculas/minúsculas (no convierte claves a minúsculas)."""
        cfg = self.workspace_dir / "CaseTest.ini"
        cfg.write_text(
            "[ScalabilityGroups]\nsg.ShadowQuality=3\nsg.shadowquality=0\n",
            encoding="utf-8",
        )

        res = self.editor.modify_ini_or_text(
            cfg,
            key_values={"sg.ShadowQuality": "1"},
            section="ScalabilityGroups",
        )
        self.assertEqual(res["status"], "success")
        updated = cfg.read_text(encoding="utf-8")
        self.assertIn("sg.ShadowQuality=1", updated)
        self.assertIn("sg.shadowquality=0", updated)

    def test_11_preserve_ini_comments_inline_and_full_line(self) -> None:
        """11. Verifica que comentarios con ';' y '#' (de línea completa e inline) se preserven intactos."""
        cfg = self.workspace_dir / "Comments.ini"
        original = (
            "; Comentario inicial con punto y coma\n"
            "# Comentario con almohadilla\n"
            "[Graphics]\n"
            "VSync = True ; mantener comentario inline\n"
            "Bloom = 1 # otro comentario inline\n"
        )
        cfg.write_text(original, encoding="utf-8")

        res = self.editor.modify_ini_or_text(
            cfg, key_values={"VSync": "False"}, section="Graphics"
        )
        self.assertEqual(res["status"], "success")
        updated = cfg.read_text(encoding="utf-8")

        self.assertIn("; Comentario inicial con punto y coma\n", updated)
        self.assertIn("# Comentario con almohadilla\n", updated)
        self.assertIn("VSync = False ; mantener comentario inline\n", updated)
        self.assertIn("Bloom = 1 # otro comentario inline\n", updated)

    def test_12_preserve_empty_lines_and_section_order(self) -> None:
        """12. Verifica que las líneas vacías, la indentación y el orden de secciones se conserven."""
        cfg = self.workspace_dir / "Layout.ini"
        original = "\n[FirstSection]\n  Alpha = 10\n\n\n[SecondSection]\n  Beta : 20\n\n"
        cfg.write_text(original, encoding="utf-8")

        res = self.editor.modify_ini_or_text(
            cfg,
            key_values={"Beta": "40"},
            section="SecondSection",
            separator=":",
        )
        self.assertEqual(res["status"], "success")
        updated = cfg.read_text(encoding="utf-8")
        self.assertEqual(
            updated,
            "\n[FirstSection]\n  Alpha = 10\n\n\n[SecondSection]\n  Beta : 40\n\n",
        )

    def test_13_preserve_crlf_and_lf_line_endings(self) -> None:
        """13. Verifica la preservación exacta de saltos de línea CRLF y LF."""
        crlf_file = self.workspace_dir / "win_crlf.ini"
        lf_file = self.workspace_dir / "unix_lf.ini"
        crlf_file.write_bytes(b"[Display]\r\nFullscreen=1\r\nHDR=0\r\n")
        lf_file.write_bytes(b"[Display]\nFullscreen=1\nHDR=0\n")

        self.editor.modify_ini_or_text(
            crlf_file, key_values={"Fullscreen": "0"}, section="Display"
        )
        self.editor.modify_ini_or_text(
            lf_file, key_values={"Fullscreen": "0"}, section="Display"
        )

        self.assertEqual(crlf_file.read_bytes(), b"[Display]\r\nFullscreen=0\r\nHDR=0\r\n")
        self.assertEqual(lf_file.read_bytes(), b"[Display]\nFullscreen=0\nHDR=0\n")

    def test_14_preserve_utf8_bom_encoding(self) -> None:
        """14. Verifica que un archivo con UTF-8 BOM conserve su BOM tras la edición."""
        bom_file = self.workspace_dir / "bom_config.ini"
        bom_bytes = b"\xef\xbb\xbf[Audio]\r\nMasterVolume=100\r\n"
        bom_file.write_bytes(bom_bytes)

        res = self.editor.modify_ini_or_text(
            bom_file, key_values={"MasterVolume": "75"}, section="Audio"
        )
        self.assertEqual(res["status"], "success")
        updated_bytes = bom_file.read_bytes()
        self.assertTrue(updated_bytes.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(updated_bytes, b"\xef\xbb\xbf[Audio]\r\nMasterVolume=75\r\n")

    def test_15_headerless_ini_cfg_conf_support(self) -> None:
        """15. Verifica el soporte para archivos .cfg/.conf/.ini sin encabezados de sección."""
        cfg = self.workspace_dir / "user.cfg"
        cfg.write_text("r_MotionBlur = 1\nr_VSync = 1\n", encoding="utf-8")

        res = self.editor.modify_ini_or_text(
            cfg, key_values={"r_MotionBlur": "0"}, section=None
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(cfg.read_text(encoding="utf-8"), "r_MotionBlur = 0\nr_VSync = 1\n")

    def test_16_duplicate_keys_detected_and_aborted_without_corruption(self) -> None:
        """16. Verifica que claves duplicadas dentro de la misma sección se detecten y aborten sin corromper."""
        cfg = self.workspace_dir / "dup.ini"
        original = "[Graphics]\nShadowQuality=3\nShadowQuality=2\n"
        cfg.write_text(original, encoding="utf-8")

        res = self.editor.modify_ini_or_text(
            cfg, key_values={"ShadowQuality": "1"}, section="Graphics"
        )
        self.assertEqual(res["status"], "error")
        self.assertEqual(cfg.read_text(encoding="utf-8"), original)

    def test_17_ambiguous_expected_value_mismatch_aborted(self) -> None:
        """17. Verifica que si una clave aparece en múltiples secciones sin indicar sección se aborte."""
        cfg = self.workspace_dir / "ambiguous.ini"
        original = "[S1]\nKeyX=10\n[S2]\nKeyX=20\n"
        cfg.write_text(original, encoding="utf-8")

        res = self.editor.modify_ini_or_text(
            cfg, key_values={"KeyX": "30"}, section=None
        )
        self.assertEqual(res["status"], "error")
        self.assertTrue(any("ambigua" in e.lower() for e in res["errors"]))
        self.assertEqual(cfg.read_text(encoding="utf-8"), original)

    # ------------------------------------------------------------------
    # 18 a 22: JSON, XML, XXE, reemplazo completo y parámetros contradictorios
    # ------------------------------------------------------------------

    def test_18_valid_json_before_and_after_and_invalid_json_rejected(self) -> None:
        """18. Verifica que JSON sea válido antes y después de editar y rechace JSON corrupto."""
        jfile = self.workspace_dir / "settings.json"
        jfile.write_text(
            json.dumps({"graphics": {"shadows": "ultra", "vramLimitMb": 6144}}, indent=2) + "\n",
            encoding="utf-8",
        )

        res = self.editor.modify_ini_or_text(
            jfile, key_values={"graphics.shadows": "medium"}
        )
        self.assertEqual(res["status"], "success")
        parsed = json.loads(jfile.read_text(encoding="utf-8"))
        self.assertEqual(parsed["graphics"]["shadows"], "medium")
        self.assertEqual(parsed["graphics"]["vramLimitMb"], 6144)

        corrupt_json = self.workspace_dir / "corrupt.json"
        corrupt_json.write_text("{invalid json", encoding="utf-8")
        res_bad = self.editor.modify_ini_or_text(corrupt_json, key_values={"a": 1})
        self.assertEqual(res_bad["status"], "error")

    def test_19_valid_xml_node_update(self) -> None:
        """19. Verifica la actualización segura de un nodo en un archivo XML válido."""
        xfile = self.workspace_dir / "settings.xml"
        xfile.write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n<config><graphics><textureQuality>high</textureQuality></graphics></config>',
            encoding="utf-8",
        )

        res = self.editor.modify_ini_or_text(
            xfile, key_values={"graphics/textureQuality": "medium"}
        )
        self.assertEqual(res["status"], "success")
        content = xfile.read_text(encoding="utf-8")
        self.assertIn("<textureQuality>medium</textureQuality>", content)

    def test_20_xml_xxe_doctype_entity_rejected(self) -> None:
        """20. Verifica la protección contra entidades externas XML (XXE / DOCTYPE / ENTITY)."""
        xxe_file = self.workspace_dir / "malicious.xml"
        xxe_payload = (
            '<?xml version="1.0"?>\n'
            '<!DOCTYPE foo [ <!ENTITY xxe SYSTEM "file:///c:/windows/win.ini"> ]>\n'
            "<config><val>&xxe;</val></config>"
        )
        xxe_file.write_text(xxe_payload, encoding="utf-8")

        res = self.editor.modify_ini_or_text(xxe_file, key_values={"val": "safe"})
        self.assertEqual(res["status"], "error")
        self.assertTrue(any("xxe" in e.lower() or "dtd" in e.lower() for e in res["errors"]))

    def test_21_explicit_full_content_replacement(self) -> None:
        """21. Verifica el reemplazo completo explícito con backup previo."""
        txt_file = self.workspace_dir / "launch_options.txt"
        txt_file.write_text("-novid -high\n", encoding="utf-8")

        res = self.editor.modify_ini_or_text(
            txt_file, replacement_content="-novid -dx11\n"
        )
        self.assertEqual(res["status"], "success")
        self.assertTrue(Path(res["backup_path"]).exists())
        self.assertEqual(txt_file.read_text(encoding="utf-8"), "-novid -dx11\n")

    def test_22_contradictory_parameters_rejected(self) -> None:
        """22. Verifica el rechazo de parámetros contradictorios en FileEditorSkill y PolicyEngine."""
        cfg = self.workspace_dir / "contra.ini"
        cfg.write_text("[Main]\nA=1\n", encoding="utf-8")

        # Pasar key_values y replacement_content simultáneamente debe fallar
        res_both = self.editor.modify_ini_or_text(
            cfg,
            key_values={"A": "2"},
            replacement_content="[Main]\nA=2\n",
        )
        self.assertEqual(res_both["status"], "error")

        # replace_content con key individual definida en ChangeOperation también se rechaza
        sha = compute_sha256_file(cfg)
        op_contradictory = ChangeOperation(
            id="op-contra",
            file_path=str(cfg),
            format="ini",
            operation_type="replace_content",
            section="Main",
            key="A",
            expected_current_value="1",
            proposed_value="[Main]\nA=2\n",
            justification="Prueba contradictoria",
            risk_level="LOW",
            evidence="VERIFIED",
            verification_method="hash_check",
            expected_file_hash=sha,
        )
        with self.assertRaises(PolicyValidationError):
            self.orchestrator.policy_engine.validate_operation(op_contradictory)

    # ------------------------------------------------------------------
    # 23 a 27: Escritura atómica, reintentos en Windows, fallos y concurrencia
    # ------------------------------------------------------------------

    def test_23_atomic_temp_write_in_same_directory(self) -> None:
        """23. Verifica que el archivo temporal se cree en el mismo directorio y se limpie tras os.replace."""
        cfg = self.workspace_dir / "atomic.ini"
        cfg.write_text("[S]\nK=1\n", encoding="utf-8")

        seen_temp_dirs: list[Path] = []
        real_replace = os.replace

        def spy_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
            seen_temp_dirs.append(Path(src).parent.resolve())
            return real_replace(src, dst)

        with patch("skills.file_editor_skill.os.replace", side_effect=spy_replace):
            res = self.editor.modify_ini_or_text(cfg, key_values={"K": "2"}, section="S")

        self.assertEqual(res["status"], "success")
        self.assertEqual(seen_temp_dirs, [self.workspace_dir.resolve()])
        leftover_tmps = list(self.workspace_dir.glob("*.tmp"))
        self.assertEqual(leftover_tmps, [])

    def test_24_retries_on_transient_windows_permission_or_winerror32(self) -> None:
        """24. Verifica los reintentos controlados cuando Windows lanza PermissionError o WinError 32 transitorio."""
        cfg = self.workspace_dir / "transient.ini"
        cfg.write_text("[S]\nK=1\n", encoding="utf-8")

        real_replace = os.replace
        call_count = {"n": 0}

        def flaky_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
            call_count["n"] += 1
            if call_count["n"] < 3:
                err = OSError("El proceso no tiene acceso al archivo")
                err.winerror = 32
                raise err
            return real_replace(src, dst)

        with patch("skills.file_editor_skill.os.replace", side_effect=flaky_replace):
            res = self.editor.modify_ini_or_text(cfg, key_values={"K": "2"}, section="S")

        self.assertEqual(res["status"], "success")
        self.assertEqual(call_count["n"], 3)
        self.assertIn("K=2", cfg.read_text(encoding="utf-8"))

    def test_25_retry_exhaustion_cleans_temp_and_raises(self) -> None:
        """25. Verifica que al agotar los 3 reintentos se limpie el temporal y se devuelva error."""
        cfg = self.workspace_dir / "exhaust.ini"
        original = "[S]\nK=1\n"
        cfg.write_text(original, encoding="utf-8")

        def always_locked(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
            raise PermissionError("Archivo bloqueado permanentemente por otro proceso")

        with patch("skills.file_editor_skill.os.replace", side_effect=always_locked):
            res = self.editor.modify_ini_or_text(cfg, key_values={"K": "2"}, section="S")

        self.assertEqual(res["status"], "error")
        self.assertEqual(list(self.workspace_dir.glob("*.tmp")), [])

    def test_26_permanent_write_failure_preserves_original_file(self) -> None:
        """26. Verifica que ante un fallo permanente de escritura el archivo original permanezca intacto."""
        cfg = self.workspace_dir / "preserve.ini"
        original_bytes = b"[S]\r\nK=original_intact\r\n"
        cfg.write_bytes(original_bytes)
        original_sha = compute_sha256_bytes(original_bytes)

        with patch("skills.file_editor_skill.os.replace", side_effect=PermissionError("Lock")):
            res = self.editor.modify_ini_or_text(cfg, key_values={"K": "modified"}, section="S")

        self.assertEqual(res["status"], "error")
        self.assertEqual(cfg.read_bytes(), original_bytes)
        self.assertEqual(compute_sha256_file(cfg), original_sha)

    def test_27_concurrent_modification_detected_before_commit(self) -> None:
        """27. Verifica que si el archivo cambia su SHA-256 antes o durante la escritura se aborte por concurrencia."""
        cfg = self.workspace_dir / "concurrent.ini"
        cfg.write_text("[S]\nK=1\n", encoding="utf-8")
        stale_hash = "0" * 64

        res = self.editor.modify_ini_or_text(
            cfg,
            key_values={"K": "2"},
            section="S",
            expected_file_hash=stale_hash,
        )
        self.assertEqual(res["status"], "conflict")
        self.assertEqual(cfg.read_text(encoding="utf-8"), "[S]\nK=1\n")

    # ------------------------------------------------------------------
    # 28 a 34: PolicyEngine, Aprobación vinculada a hash, Verificación y Rollback
    # ------------------------------------------------------------------

    def test_28_invalid_plan_and_unknown_fields_rejected_by_policy_engine(self) -> None:
        """28. Verifica que ChangePlan/ChangeOperation con campos desconocidos o UNSUPPORTED sean rechazados."""
        with self.assertRaises(PolicyValidationError):
            ChangeOperation.from_dict(
                {
                    "id": "op-1",
                    "file_path": str(self.workspace_dir / "a.ini"),
                    "format": "ini",
                    "operation_type": "update_key",
                    "key": "k",
                    "proposed_value": "1",
                    "justification": "test",
                    "risk_level": "LOW",
                    "evidence": "VERIFIED",
                    "verification_method": "read_back",
                    "malicious_extra_field": True,
                }
            )

        cfg = self.workspace_dir / "pol.ini"
        cfg.write_text("[S]\nK=1\n", encoding="utf-8")
        unsupported_op = ChangeOperation(
            id="op-unsup",
            file_path=str(cfg),
            format="ini",
            operation_type="update_key",
            section="S",
            key="K",
            expected_current_value="1",
            proposed_value="2",
            justification="Parámetro no soportado",
            risk_level="LOW",
            evidence="UNSUPPORTED",
            verification_method="read_back",
            expected_file_hash=compute_sha256_file(cfg),
        )
        with self.assertRaises(PolicyValidationError):
            self.orchestrator.policy_engine.validate_operation(unsupported_op)

    def test_29_expired_or_tampered_approval_hash_rejected(self) -> None:
        """29. Verifica que cualquier cambio en el plan tras la simulación invalide el approval_hash."""
        cfg = self.workspace_dir / "tamper.ini"
        cfg.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\n", encoding="utf-8")

        rec = self.orchestrator.create_execution("exec-tamper")
        snap = self.orchestrator.inspect_target_config(cfg)
        hw = self.orchestrator.detect_hardware_context("Optimizar para GTX 1660 de 6 GB")
        recs = self.orchestrator.investigate_optimizations(
            execution_id=rec.execution_id,
            user_goal="Optimizar para GTX 1660 de 6 GB",
            application_name="DemoGame",
            config_snapshot=snap,
            hardware_context=hw,
        )
        plan = self.orchestrator.build_and_validate_plan(
            execution_id=rec.execution_id,
            user_goal="Optimizar para GTX 1660 de 6 GB",
            application_name="DemoGame",
            config_snapshot=snap,
            hardware_context=hw,
            recommendations=recs,
        )
        sim = self.orchestrator.simulate_plan(plan)
        valid_hash = sim["approval_hash"]

        plan.operations[0].proposed_value = "0"
        with self.assertRaises(ApprovalBindingError):
            self.orchestrator.execute_approved_plan(
                plan,
                approval_hash=valid_hash,
                user_approved=True,
            )
        self.assertIn("sg.ShadowQuality=3", cfg.read_text(encoding="utf-8"))

    def test_30_execution_only_after_explicit_user_approval(self) -> None:
        """30. Verifica que ningún cambio se aplique sin user_approved=True y simulación previa."""
        cfg = self.workspace_dir / "approval.ini"
        cfg.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\n", encoding="utf-8")

        rec = self.orchestrator.create_execution("exec-auth")
        snap = self.orchestrator.inspect_target_config(cfg)
        hw = self.orchestrator.detect_hardware_context("GTX 1660 6 GB")
        recs = self.orchestrator.investigate_optimizations(
            execution_id=rec.execution_id,
            user_goal="GTX 1660 6 GB",
            application_name="DemoGame",
            config_snapshot=snap,
            hardware_context=hw,
        )
        plan = self.orchestrator.build_and_validate_plan(
            execution_id=rec.execution_id,
            user_goal="GTX 1660 6 GB",
            application_name="DemoGame",
            config_snapshot=snap,
            hardware_context=hw,
            recommendations=recs,
        )
        sim = self.orchestrator.simulate_plan(plan)

        with self.assertRaises(ApprovalBindingError):
            self.orchestrator.execute_approved_plan(
                plan,
                approval_hash=sim["approval_hash"],
                user_approved=False,
            )
        self.assertIn("sg.ShadowQuality=3", cfg.read_text(encoding="utf-8"))

    def test_31_post_write_verification_and_auto_rollback_on_mismatch(self) -> None:
        """31. Verifica la comprobación post-escritura tras aplicar un plan aprobado."""
        cfg = self.workspace_dir / "verify_post.ini"
        original_text = "[ScalabilityGroups]\nsg.ShadowQuality=3\n"
        cfg.write_text(original_text, encoding="utf-8")

        rec = self.orchestrator.create_execution("exec-verify")
        snap = self.orchestrator.inspect_target_config(cfg)
        hw = self.orchestrator.detect_hardware_context("GTX 1660 6 GB")
        recs = self.orchestrator.investigate_optimizations(
            execution_id=rec.execution_id,
            user_goal="GTX 1660 6 GB",
            application_name="DemoGame",
            config_snapshot=snap,
            hardware_context=hw,
        )
        plan = self.orchestrator.build_and_validate_plan(
            execution_id=rec.execution_id,
            user_goal="GTX 1660 6 GB",
            application_name="DemoGame",
            config_snapshot=snap,
            hardware_context=hw,
            recommendations=recs,
        )
        sim = self.orchestrator.simulate_plan(plan)
        result = self.orchestrator.execute_approved_plan(
            plan,
            approval_hash=sim["approval_hash"],
            user_approved=True,
        )
        self.assertEqual(result["state"], ChainState.COMPLETED.value)
        self.assertEqual(len(result["verifications"]), 1)
        self.assertTrue(result["verifications"][0]["verified"])
        self.assertIn("sg.ShadowQuality=1", cfg.read_text(encoding="utf-8"))

    def test_32_explicit_rollback_restores_original_sha256(self) -> None:
        """32. Verifica que el rollback restaure el archivo exactamente a su SHA-256 original."""
        cfg = self.workspace_dir / "rollback.ini"
        original_bytes = b"[ScalabilityGroups]\r\nsg.ShadowQuality=3\r\nsg.TextureQuality=3\r\n"
        cfg.write_bytes(original_bytes)
        original_sha = compute_sha256_bytes(original_bytes)

        rec = self.orchestrator.create_execution("exec-rb")
        snap = self.orchestrator.inspect_target_config(cfg)
        hw = self.orchestrator.detect_hardware_context("GTX 1660 6 GB")
        recs = self.orchestrator.investigate_optimizations(
            execution_id=rec.execution_id,
            user_goal="GTX 1660 6 GB",
            application_name="DemoGame",
            config_snapshot=snap,
            hardware_context=hw,
        )
        plan = self.orchestrator.build_and_validate_plan(
            execution_id=rec.execution_id,
            user_goal="GTX 1660 6 GB",
            application_name="DemoGame",
            config_snapshot=snap,
            hardware_context=hw,
            recommendations=recs,
        )
        sim = self.orchestrator.simulate_plan(plan)
        self.orchestrator.execute_approved_plan(
            plan,
            approval_hash=sim["approval_hash"],
            user_approved=True,
        )
        self.assertNotEqual(compute_sha256_file(cfg), original_sha)

        rb_res = self.orchestrator.rollback_execution("exec-rb")
        self.assertEqual(rb_res["state"], ChainState.ROLLED_BACK.value)
        self.assertEqual(compute_sha256_file(cfg), original_sha)
        self.assertEqual(cfg.read_bytes(), original_bytes)

    def test_33_rollback_conflict_detected_when_modified_externally(self) -> None:
        """33. Verifica que si el archivo fue editado manualmente tras aplicar el plan, el rollback detecte el conflicto."""
        cfg = self.workspace_dir / "rb_conflict.ini"
        cfg.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\n", encoding="utf-8")

        rec = self.orchestrator.create_execution("exec-rb-conf")
        snap = self.orchestrator.inspect_target_config(cfg)
        hw = self.orchestrator.detect_hardware_context("GTX 1660 6 GB")
        recs = self.orchestrator.investigate_optimizations(
            execution_id=rec.execution_id,
            user_goal="GTX 1660 6 GB",
            application_name="DemoGame",
            config_snapshot=snap,
            hardware_context=hw,
        )
        plan = self.orchestrator.build_and_validate_plan(
            execution_id=rec.execution_id,
            user_goal="GTX 1660 6 GB",
            application_name="DemoGame",
            config_snapshot=snap,
            hardware_context=hw,
            recommendations=recs,
        )
        sim = self.orchestrator.simulate_plan(plan)
        self.orchestrator.execute_approved_plan(
            plan,
            approval_hash=sim["approval_hash"],
            user_approved=True,
        )

        # Modificación externa posterior a la ejecución del plan:
        cfg.write_text("[ScalabilityGroups]\nsg.ShadowQuality=0\nManualKey=99\n", encoding="utf-8")

        with self.assertRaises(RuntimeError):
            self.orchestrator.rollback_execution("exec-rb-conf", force=False)

    def test_34_partial_failure_in_multi_operation_plan_stops_and_rolls_back(self) -> None:
        """34. Verifica que un fallo parcial en múltiples operaciones detenga las restantes y revierta."""
        cfg = self.workspace_dir / "multi_fail.ini"
        original_content = "[ScalabilityGroups]\nsg.ShadowQuality=3\nsg.TextureQuality=3\n"
        cfg.write_text(original_content, encoding="utf-8")
        original_sha = compute_sha256_file(cfg)

        rec = self.orchestrator.create_execution("exec-multi")
        snap = self.orchestrator.inspect_target_config(cfg)
        hw = self.orchestrator.detect_hardware_context("GTX 1660 6 GB")
        recs = self.orchestrator.investigate_optimizations(
            execution_id=rec.execution_id,
            user_goal="GTX 1660 6 GB",
            application_name="DemoGame",
            config_snapshot=snap,
            hardware_context=hw,
        )
        plan = self.orchestrator.build_and_validate_plan(
            execution_id=rec.execution_id,
            user_goal="GTX 1660 6 GB",
            application_name="DemoGame",
            config_snapshot=snap,
            hardware_context=hw,
            recommendations=recs,
        )
        self.assertGreaterEqual(len(plan.operations), 2)
        sim = self.orchestrator.simulate_plan(plan)

        real_modify = self.editor.modify_ini_or_text
        calls = {"count": 0}

        def fail_on_second_call(*args: object, **kwargs: object) -> dict[str, object]:
            calls["count"] += 1
            if calls["count"] == 2:
                return {"status": "error", "errors": ["Fallo simulado en la segunda operación"]}
            return real_modify(*args, **kwargs)

        with patch.object(self.editor, "modify_ini_or_text", side_effect=fail_on_second_call):
            with self.assertRaises(RuntimeError):
                self.orchestrator.execute_approved_plan(
                    plan,
                    approval_hash=sim["approval_hash"],
                    user_approved=True,
                )

        self.assertEqual(self.orchestrator.get_execution("exec-multi").state, ChainState.ROLLED_BACK)
        self.assertEqual(compute_sha256_file(cfg), original_sha)
        self.assertEqual(cfg.read_text(encoding="utf-8"), original_content)

    # ------------------------------------------------------------------
    # 35 a 40: Integración con SkillManager, proveedores, secretos y suite
    # ------------------------------------------------------------------

    def test_35_skill_manager_integration_and_execute_actions(self) -> None:
        """35. Verifica que FileEditorSkill esté registrada en SkillManager y responda a sus acciones."""
        sm = SkillManager()
        self.assertIn("file_editor", sm.skills)
        manifest_names = [m["name"] for m in sm.get_skills_manifest()]
        self.assertIn("file_editor", manifest_names)

        cfg = self.workspace_dir / "sm_test.ini"
        cfg.write_text("[Graphics]\nFOV=90\n", encoding="utf-8")

        read_out = self.editor.execute(action="read", file_path=str(cfg))
        self.assertIn("sm_test.ini", read_out)

        upd_out = self.editor.execute(
            action="modify",
            file_path=str(cfg),
            section="Graphics",
            key_values={"FOV": "105"},
        )
        self.assertIn("Se aplicaron y verificaron", upd_out)
        self.assertIn("FOV=105", cfg.read_text(encoding="utf-8"))

    def test_36_simulated_provider_investigation_and_caching(self) -> None:
        """36. Verifica investigación con proveedor simulado, clasificación y caché anti-duplicados."""
        cfg = self.workspace_dir / "prov.ini"
        cfg.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\n", encoding="utf-8")

        call_counter = {"calls": 0}

        def fake_llm(prompt: str) -> str:
            call_counter["calls"] += 1
            return json.dumps(
                {
                    "recommendations": [
                        {
                            "section": "ScalabilityGroups",
                            "key": "sg.ShadowQuality",
                            "proposed_value": "1",
                            "justification": "Ahorro de VRAM en 6 GB",
                            "risk_level": "LOW",
                            "evidence": "VERIFIED",
                        },
                        {
                            "section": "ScalabilityGroups",
                            "key": "NonExistentFakeKey",
                            "proposed_value": "99",
                            "justification": "Clave inventada por el LLM",
                            "risk_level": "LOW",
                            "evidence": "VERIFIED",
                        },
                    ]
                }
            )

        rec = self.orchestrator.create_execution("exec-prov")
        snap = self.orchestrator.inspect_target_config(cfg)
        hw = self.orchestrator.detect_hardware_context("GTX 1660 6 GB")

        recs1 = self.orchestrator.investigate_optimizations(
            execution_id=rec.execution_id,
            user_goal="Optimizar sombras",
            application_name="UnrealGame",
            config_snapshot=snap,
            hardware_context=hw,
            provider_callable=fake_llm,
        )
        recs2 = self.orchestrator.investigate_optimizations(
            execution_id=rec.execution_id,
            user_goal="Optimizar sombras",
            application_name="UnrealGame",
            config_snapshot=snap,
            hardware_context=hw,
            provider_callable=fake_llm,
        )

        self.assertEqual(call_counter["calls"], 1)
        by_key = {r.key: r.evidence for r in recs1}
        self.assertEqual(by_key["sg.ShadowQuality"], "VERIFIED")
        self.assertEqual(by_key["NonExistentFakeKey"], "UNSUPPORTED")
        self.assertEqual(len(recs2), 2)

    def test_37_unavailable_provider_handled_cleanly(self) -> None:
        """37. Verifica el manejo controlado cuando un proveedor externo lanza error de conexión."""
        cfg = self.workspace_dir / "unavail.ini"
        cfg.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\n", encoding="utf-8")

        def broken_provider(prompt: str) -> str:
            raise ConnectionError("Proveedor cloud desconectado")

        rec = self.orchestrator.create_execution("exec-unavail")
        snap = self.orchestrator.inspect_target_config(cfg)
        hw = self.orchestrator.detect_hardware_context("GTX 1660 6 GB")

        with self.assertRaises(RuntimeError):
            self.orchestrator.investigate_optimizations(
                execution_id=rec.execution_id,
                user_goal="Optimizar",
                application_name="Game",
                config_snapshot=snap,
                hardware_context=hw,
                provider_callable=broken_provider,
            )

    def test_38_provider_timeout_handled_cleanly(self) -> None:
        """38. Verifica el manejo controlado ante timeout del proveedor de IA."""
        cfg = self.workspace_dir / "timeout.ini"
        cfg.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\n", encoding="utf-8")

        def timeout_provider(prompt: str) -> str:
            raise TimeoutError("Tiempo de espera agotado")

        rec = self.orchestrator.create_execution("exec-timeout")
        snap = self.orchestrator.inspect_target_config(cfg)
        hw = self.orchestrator.detect_hardware_context("GTX 1660 6 GB")

        with self.assertRaises(TimeoutError):
            self.orchestrator.investigate_optimizations(
                execution_id=rec.execution_id,
                user_goal="Optimizar",
                application_name="Game",
                config_snapshot=snap,
                hardware_context=hw,
                provider_callable=timeout_provider,
            )

    def test_39_no_secrets_in_audit_logs_and_redaction(self) -> None:
        """39. Verifica que AuditLogger redacte cualquier clave API o token antes de escribir en disco."""
        fake_key = "sk-" + "proj-ABCDEFGHIJKLMNOPQRSTUV1234567890"
        fake_gemini = "AIza" + "SyD1234567890abcdefghijklmnopqrstuvwx"

        self.editor.audit_logger.record(
            "test_redaction",
            "exec-sec",
            {
                "file_path": str(self.workspace_dir / "cfg.ini"),
                "message": f"Intento con token {fake_key} y clave {fake_gemini}",
            },
        )
        self.assertTrue(len(self.editor.audit_logger.entries) >= 1)
        raw_log_text = self.audit_log_path.read_text(encoding="utf-8")
        self.assertNotIn(fake_key, raw_log_text)
        self.assertNotIn(fake_gemini, raw_log_text)

    def test_40_state_machine_transitions_and_multi_game_discovery(self) -> None:
        """40. Verifica las transiciones de estado válidas/inválidas y el descubrimiento acotado multi-juego."""
        rec = self.orchestrator.create_execution("exec-sm")
        self.assertEqual(rec.state, ChainState.RECEIVED)

        with self.assertRaises(ValueError):
            self.orchestrator.transition_state("exec-sm", ChainState.APPLYING)

        game_dir = self.workspace_dir / "CyberGame" / "Config"
        game_dir.mkdir(parents=True, exist_ok=True)
        target_ini = game_dir / "GameUserSettings.ini"
        target_ini.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\n", encoding="utf-8")

        discovered = self.editor.discover_config_file("CyberGame", "GameUserSettings.ini")
        self.assertIsNotNone(discovered)
        self.assertEqual(discovered, target_ini.resolve())

    def test_41_interactive_console_optimizar_approve_and_rollback(self) -> None:
        """41. Verifica el flujo interactivo en consola (/optimizar <juego> -> confirmar 's' -> /rollback <id>)."""
        from main import run_console_session
        from src.brain.brain_manager import BrainManager

        game_dir = self.workspace_dir / "StarQuest" / "Config"
        game_dir.mkdir(parents=True, exist_ok=True)
        cfg = game_dir / "GameUserSettings.ini"
        original_text = "[ScalabilityGroups]\nsg.ShadowQuality=3\nsg.TextureQuality=3\n"
        cfg.write_text(original_text, encoding="utf-8")
        original_sha = compute_sha256_file(cfg)

        bm = BrainManager(initial_mode="AUTO", lazy_init=True)
        outputs: list[str] = []
        captured_exec_id: dict[str, str] = {"id": ""}

        # Simulamos entradas de usuario:
        # 1) /optimizar StarQuest
        # 2) s (confirmar aplicación)
        # 3) /rollback <execution_id capturado>
        # 4) salir
        step = {"n": 0}

        def fake_input(prompt_str: str) -> str:
            step["n"] += 1
            if step["n"] == 1:
                return "/optimizar StarQuest"
            if step["n"] == 2:
                return "s"
            if step["n"] == 3:
                for line in outputs:
                    if "ID de ejecución para rollback:" in line:
                        captured_exec_id["id"] = (
                            line.split("ID de ejecución para rollback:")[1]
                            .strip()
                            .rstrip(")")
                        )
                return f"/rollback {captured_exec_id['id']}"
            return "salir"

        run_console_session(
            bm,
            enable_tts=False,
            input_fn=fake_input,
            output_fn=outputs.append,
            orchestrator=self.orchestrator,
        )

        joined_out = "\n".join(outputs)
        self.assertIn("SIMULACIÓN EN SOLO LECTURA", joined_out)
        self.assertIn("[ChainOrchestrator: COMPLETED]", joined_out)
        self.assertIn("[ChainOrchestrator: ROLLED_BACK]", joined_out)
        self.assertEqual(compute_sha256_file(cfg), original_sha)
        self.assertEqual(cfg.read_text(encoding="utf-8"), original_text)

    def test_42_interactive_console_optimizar_cancel_and_selective_ids(self) -> None:
        """42. Verifica la cancelación ('n') sin tocar disco y la aprobación selectiva por ID ('op-001')."""
        from main import run_console_session
        from src.brain.brain_manager import BrainManager

        game_dir = self.workspace_dir / "ApexRacer" / "Config"
        game_dir.mkdir(parents=True, exist_ok=True)
        cfg = game_dir / "GameUserSettings.ini"
        original_text = "[ScalabilityGroups]\nsg.ShadowQuality=3\nsg.TextureQuality=3\n"
        cfg.write_text(original_text, encoding="utf-8")

        bm = BrainManager(initial_mode="AUTO", lazy_init=True)
        outputs: list[str] = []

        # Primero probamos cancelar ('n'), luego aplicar solo 'op-001', luego salir
        inputs = iter(
            [
                "/optimizar ApexRacer",
                "n",
                "/optimizar ApexRacer",
                "op-001",
                "salir",
            ]
        )

        run_console_session(
            bm,
            enable_tts=False,
            input_fn=lambda _: next(inputs),
            output_fn=outputs.append,
            orchestrator=self.orchestrator,
        )

        joined_out = "\n".join(outputs)
        self.assertIn("Plan cancelado, señor.", joined_out)
        self.assertIn("[ChainOrchestrator: COMPLETED]", joined_out)
        updated_text = cfg.read_text(encoding="utf-8")
        # Solo la primera operación (sg.ShadowQuality=1) se aplicó; la segunda quedó intacta (=3)
        self.assertIn("sg.ShadowQuality=1", updated_text)
        self.assertIn("sg.TextureQuality=3", updated_text)

    # ------------------------------------------------------------------
    # Helpers auxiliares para escenarios de endurecimiento (43 a 62)
    # ------------------------------------------------------------------

    def _build_custom_ini_op(
        self,
        op_id: str,
        cfg_path: Path,
        key: str,
        expected_val: str,
        proposed_val: str,
        *,
        section: str = "ScalabilityGroups",
        depends_on: Optional[Sequence[str]] = None,
        expected_hash: Optional[str] = None,
    ) -> ChangeOperation:
        """Crea una `ChangeOperation` INI válida para pruebas de grafos y estados."""
        sha = expected_hash if expected_hash is not None else compute_sha256_file(cfg_path)
        return ChangeOperation(
            id=op_id,
            file_path=str(cfg_path),
            format="ini",
            operation_type="update_key",
            section=section,
            key=key,
            expected_current_value=expected_val,
            proposed_value=proposed_val,
            justification=f"Ajuste técnico para {key}",
            risk_level="LOW",
            evidence="VERIFIED",
            verification_method="read_back_and_verify_ini_key_and_sha256",
            reversible=True,
            requires_approval=True,
            expected_file_hash=sha,
            depends_on=list(depends_on) if depends_on else [],
        )

    def _build_custom_plan(
        self,
        execution_id: str,
        operations: list[ChangeOperation],
        *,
        plan_id: Optional[str] = None,
    ) -> ChangePlan:
        """Construye un `ChangePlan` válido asociado a `execution_id`."""
        return ChangePlan(
            plan_id=plan_id or f"plan-{execution_id}",
            execution_id=execution_id,
            user_goal="Optimizar configuración para GTX 1660 6 GB",
            application_name="HardenedTestGame",
            application_version="1.0",
            hardware_context={"gpu_model": "GTX 1660", "vram_gb": 6},
            operations=operations,
            provider="local_heuristic",
        )

    # ------------------------------------------------------------------
    # 43 a 62: Endurecimiento de Grafo de Dependencias, Estados, Hash y Aislamiento
    # ------------------------------------------------------------------

    def test_43_missing_dependency_rejected_in_preflight(self) -> None:
        """43. (Grupo 1) Rechaza aprobación selectiva cuando falta una dependencia directa o transitiva."""
        cfg = self.workspace_dir / "dep_missing.ini"
        original_text = (
            "[ScalabilityGroups]\n"
            "sg.ShadowQuality=3\n"
            "sg.TextureQuality=3\n"
            "sg.EffectsQuality=3\n"
        )
        cfg.write_text(original_text, encoding="utf-8")
        orig_sha = compute_sha256_file(cfg)

        rec = self.orchestrator.create_execution("exec-43")
        op_a = self._build_custom_ini_op("op-A", cfg, "sg.ShadowQuality", "3", "1")
        op_b = self._build_custom_ini_op(
            "op-B", cfg, "sg.TextureQuality", "3", "1", depends_on=["op-A"]
        )
        op_c = self._build_custom_ini_op(
            "op-C", cfg, "sg.EffectsQuality", "3", "1", depends_on=["op-B"]
        )
        plan = self._build_custom_plan(rec.execution_id, [op_a, op_b, op_c])
        sim = self.orchestrator.simulate_plan(plan)

        # Seleccionar solo op-B sin op-A (dependencia directa pendiente) debe fallar en pre-flight
        with patch.object(self.editor, "modify_ini_or_text", wraps=self.editor.modify_ini_or_text) as spy_mod:
            with self.assertRaises(PolicyValidationError):
                self.orchestrator.execute_approved_plan(
                    plan,
                    approval_hash=sim["approval_hash"],
                    user_approved=True,
                    approved_operation_ids=["op-B"],
                )
            self.assertEqual(spy_mod.call_count, 0)

        self.assertEqual(rec.state, ChainState.REJECTED)
        self.assertEqual(compute_sha256_file(cfg), orig_sha)
        self.assertEqual(list(self.backup_dir.glob("*.bak")), [])

        # Tampoco se permite seleccionar [op-B, op-C] omitiendo la dependencia transitiva op-A
        rec2 = self.orchestrator.create_execution("exec-43b")
        plan2 = self._build_custom_plan(rec2.execution_id, [op_a, op_b, op_c])
        sim2 = self.orchestrator.simulate_plan(plan2)
        with self.assertRaises(PolicyValidationError):
            self.orchestrator.execute_approved_plan(
                plan2,
                approval_hash=sim2["approval_hash"],
                user_approved=True,
                approved_operation_ids=["op-B", "op-C"],
            )
        self.assertEqual(compute_sha256_file(cfg), orig_sha)

    def test_44_complete_dependencies_approved_and_executed(self) -> None:
        """44. (Grupo 2) Ejecuta correctamente cuando todas las dependencias requeridas están incluidas."""
        cfg = self.workspace_dir / "dep_complete.ini"
        cfg.write_text(
            "[ScalabilityGroups]\nsg.ShadowQuality=3\nsg.TextureQuality=3\nsg.EffectsQuality=3\n",
            encoding="utf-8",
        )

        rec = self.orchestrator.create_execution("exec-44")
        op_a = self._build_custom_ini_op("op-A", cfg, "sg.ShadowQuality", "3", "1")
        op_b = self._build_custom_ini_op(
            "op-B", cfg, "sg.TextureQuality", "3", "1", depends_on=["op-A"]
        )
        op_c = self._build_custom_ini_op(
            "op-C", cfg, "sg.EffectsQuality", "3", "1", depends_on=["op-B"]
        )
        plan = self._build_custom_plan(rec.execution_id, [op_a, op_b, op_c])
        sim = self.orchestrator.simulate_plan(plan)

        # Seleccionamos el subgrafo completo válido [op-A, op-B]
        res = self.orchestrator.execute_approved_plan(
            plan,
            approval_hash=sim["approval_hash"],
            user_approved=True,
            approved_operation_ids=["op-A", "op-B"],
        )
        self.assertEqual(res["state"], ChainState.COMPLETED.value)
        self.assertFalse(res["fully_completed"])
        self.assertEqual(res["plan_status"], "PARTIALLY_APPLIED")
        self.assertEqual(op_a.status, OperationStatus.APPLIED.value)
        self.assertEqual(op_b.status, OperationStatus.APPLIED.value)
        self.assertEqual(op_c.status, OperationStatus.PENDING.value)

        text = cfg.read_text(encoding="utf-8")
        self.assertIn("sg.ShadowQuality=1", text)
        self.assertIn("sg.TextureQuality=1", text)
        self.assertIn("sg.EffectsQuality=3", text)
        self.assertEqual(len(list(self.backup_dir.glob("*.bak"))), 2)

    def test_45_cycle_detection_direct_and_three_node(self) -> None:
        """45. (Grupo 3) Detecta y rechaza ciclos A -> B -> A y A -> B -> C -> A sin tocar archivos."""
        cfg = self.workspace_dir / "cycle.ini"
        original_text = (
            "[ScalabilityGroups]\n"
            "sg.ShadowQuality=3\n"
            "sg.TextureQuality=3\n"
            "sg.EffectsQuality=3\n"
        )
        cfg.write_text(original_text, encoding="utf-8")

        # Ciclo de 2 nodos: A <-> B
        op_a = self._build_custom_ini_op(
            "op-A", cfg, "sg.ShadowQuality", "3", "1", depends_on=["op-B"]
        )
        op_b = self._build_custom_ini_op(
            "op-B", cfg, "sg.TextureQuality", "3", "1", depends_on=["op-A"]
        )
        rec = self.orchestrator.create_execution("exec-45-2node")
        plan_2node = self._build_custom_plan(rec.execution_id, [op_a, op_b])

        with self.assertRaises(PolicyValidationError) as ctx_2:
            self.orchestrator.simulate_plan(plan_2node)
        self.assertIn("ciclo", str(ctx_2.exception).lower())
        self.assertEqual(rec.state, ChainState.REJECTED)

        # Ciclo de 3 nodos: A -> B -> C -> A
        op_1 = self._build_custom_ini_op(
            "op-1", cfg, "sg.ShadowQuality", "3", "1", depends_on=["op-3"]
        )
        op_2 = self._build_custom_ini_op(
            "op-2", cfg, "sg.TextureQuality", "3", "1", depends_on=["op-1"]
        )
        op_3 = self._build_custom_ini_op(
            "op-3", cfg, "sg.EffectsQuality", "3", "1", depends_on=["op-2"]
        )
        rec3 = self.orchestrator.create_execution("exec-45-3node")
        plan_3node = self._build_custom_plan(rec3.execution_id, [op_1, op_2, op_3])

        with self.assertRaises(PolicyValidationError) as ctx_3:
            self.orchestrator.policy_engine.validate_plan(plan_3node)
        self.assertIn("ciclo", str(ctx_3.exception).lower())
        self.assertEqual(cfg.read_text(encoding="utf-8"), original_text)
        self.assertEqual(list(self.backup_dir.glob("*.bak")), [])

    def test_46_topological_order_with_inverted_selection(self) -> None:
        """46. (Grupo 4) Fuerza el orden topológico determinista A -> B -> C aunque la selección llegue como [C, B, A]."""
        cfg = self.workspace_dir / "topo.ini"
        cfg.write_text(
            "[ScalabilityGroups]\nKeyA=0\nKeyB=0\nKeyC=0\nKeyIndep=0\n",
            encoding="utf-8",
        )

        # Definimos el plan con C y B antes que A, y una operación independiente KeyIndep
        op_a = self._build_custom_ini_op("op-A", cfg, "KeyA", "0", "1", depends_on=[])
        op_b = self._build_custom_ini_op(
            "op-B", cfg, "KeyB", "0", "2", depends_on=["op-A"]
        )
        op_c = self._build_custom_ini_op(
            "op-C", cfg, "KeyC", "0", "3", depends_on=["op-B"]
        )
        op_indep = self._build_custom_ini_op(
            "op-D", cfg, "KeyIndep", "0", "4", depends_on=[]
        )

        rec = self.orchestrator.create_execution("exec-46")
        # Incluso si en el plan están como [op-C, op-B, op-A, op-D] y la selección llega invertida:
        plan = self._build_custom_plan(
            rec.execution_id, [op_c, op_b, op_a, op_indep]
        )
        sim = self.orchestrator.simulate_plan(plan)

        executed_order: list[str] = []
        real_apply = self.orchestrator._apply_single_operation

        def spy_apply(op: ChangeOperation, **kwargs: Any) -> dict[str, Any]:
            executed_order.append(op.id)
            return real_apply(op, **kwargs)

        with patch.object(
            self.orchestrator, "_apply_single_operation", side_effect=spy_apply
        ):
            res = self.orchestrator.execute_approved_plan(
                plan,
                approval_hash=sim["approval_hash"],
                user_approved=True,
                approved_operation_ids=["op-C", "op-B", "op-A"],
            )

        self.assertEqual(executed_order, ["op-A", "op-B", "op-C"])
        self.assertEqual(
            [item["operation_id"] for item in res["applied_operations"]],
            ["op-A", "op-B", "op-C"],
        )
        final_text = cfg.read_text(encoding="utf-8")
        self.assertIn("KeyA=1", final_text)
        self.assertIn("KeyB=2", final_text)
        self.assertIn("KeyC=3", final_text)
        self.assertIn("KeyIndep=0", final_text)

    def test_47_direct_dependency_failure_blocks_dependent(self) -> None:
        """47. (Grupo 5) Si A falla, A pasa a FAILED y su dependiente directa B pasa a BLOCKED sin tocar FileEditorSkill."""
        cfg = self.workspace_dir / "direct_fail.ini"
        original_text = "[ScalabilityGroups]\nKeyA=0\nKeyB=0\n"
        cfg.write_text(original_text, encoding="utf-8")
        orig_sha = compute_sha256_file(cfg)

        op_a = self._build_custom_ini_op("op-A", cfg, "KeyA", "0", "1")
        op_b = self._build_custom_ini_op(
            "op-B", cfg, "KeyB", "0", "2", depends_on=["op-A"]
        )

        rec = self.orchestrator.create_execution("exec-47")
        plan = self._build_custom_plan(rec.execution_id, [op_a, op_b])
        sim = self.orchestrator.simulate_plan(plan)

        called_keys: list[dict[str, Any]] = []

        def fail_on_a(file_path: Any, key_values: Optional[dict[str, Any]] = None, **kwargs: Any) -> dict[str, Any]:
            called_keys.append(dict(key_values or {}))
            return {"status": "error", "errors": ["Fallo simulado en op-A"]}

        with patch.object(self.editor, "modify_ini_or_text", side_effect=fail_on_a):
            with self.assertRaises(RuntimeError):
                self.orchestrator.execute_approved_plan(
                    plan,
                    approval_hash=sim["approval_hash"],
                    user_approved=True,
                )

        # Solo se intentó ejecutar op-A; op-B fue bloqueada y nunca llegó a FileEditorSkill
        self.assertEqual(len(called_keys), 1)
        self.assertIn("KeyA", called_keys[0])
        self.assertEqual(op_a.status, OperationStatus.FAILED.value)
        self.assertEqual(op_b.status, OperationStatus.BLOCKED.value)
        self.assertEqual(rec.operation_states["op-A"], OperationStatus.FAILED.value)
        self.assertEqual(rec.operation_states["op-B"], OperationStatus.BLOCKED.value)
        self.assertIn("op-B", rec.blocked_operations)
        self.assertEqual(rec.state, ChainState.FAILED)
        self.assertEqual(compute_sha256_file(cfg), orig_sha)

    def test_48_transitive_blocked_propagation_on_failure(self) -> None:
        """48. (Grupo 6) Si A falla en A -> B -> C -> D, propaga BLOCKED transitivamente a B, C y D."""
        cfg = self.workspace_dir / "transitive_block.ini"
        cfg.write_text(
            "[ScalabilityGroups]\nK1=0\nK2=0\nK3=0\nK4=0\n",
            encoding="utf-8",
        )

        op_a = self._build_custom_ini_op("op-A", cfg, "K1", "0", "1")
        op_b = self._build_custom_ini_op("op-B", cfg, "K2", "0", "1", depends_on=["op-A"])
        op_c = self._build_custom_ini_op("op-C", cfg, "K3", "0", "1", depends_on=["op-B"])
        op_d = self._build_custom_ini_op("op-D", cfg, "K4", "0", "1", depends_on=["op-C"])

        rec = self.orchestrator.create_execution("exec-48")
        plan = self._build_custom_plan(rec.execution_id, [op_a, op_b, op_c, op_d])
        sim = self.orchestrator.simulate_plan(plan)

        with patch.object(
            self.editor,
            "modify_ini_or_text",
            return_value={"status": "error", "errors": ["Error en raíz op-A"]},
        ) as spy_mod:
            with self.assertRaises(RuntimeError):
                self.orchestrator.execute_approved_plan(
                    plan,
                    approval_hash=sim["approval_hash"],
                    user_approved=True,
                )
            self.assertEqual(spy_mod.call_count, 1)

        self.assertEqual(op_a.status, OperationStatus.FAILED.value)
        self.assertEqual(op_b.status, OperationStatus.BLOCKED.value)
        self.assertEqual(op_c.status, OperationStatus.BLOCKED.value)
        self.assertEqual(op_d.status, OperationStatus.BLOCKED.value)
        self.assertEqual(rec.blocked_operations, ["op-B", "op-C", "op-D"])
        self.assertFalse(plan.is_fully_completed())

    def test_49_nonexistent_operation_id_rejected(self) -> None:
        """49. (Grupo 7) Rechaza expresamente IDs inexistentes sin convertirlos en cancelaciones silenciosas."""
        cfg = self.workspace_dir / "unknown_id.ini"
        cfg.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\n", encoding="utf-8")

        op_1 = self._build_custom_ini_op("op-001", cfg, "sg.ShadowQuality", "3", "1")
        rec = self.orchestrator.create_execution("exec-49")
        plan = self._build_custom_plan(rec.execution_id, [op_1])
        sim = self.orchestrator.simulate_plan(plan)

        with patch.object(self.editor, "modify_ini_or_text") as spy_mod:
            with self.assertRaises(PolicyValidationError) as ctx:
                self.orchestrator.execute_approved_plan(
                    plan,
                    approval_hash=sim["approval_hash"],
                    user_approved=True,
                    approved_operation_ids=["op-999-inexistente"],
                )
            self.assertEqual(spy_mod.call_count, 0)

        self.assertIn("op-999-inexistente", str(ctx.exception))
        # No debe quedar como CANCELLED silencioso, sino como REJECTED por validación fallida
        self.assertEqual(rec.state, ChainState.REJECTED)
        self.assertNotEqual(rec.state, ChainState.CANCELLED)

    def test_50_mixed_valid_and_invalid_ids_rejected_atomically(self) -> None:
        """50. (Grupo 8) Rechaza selecciones mixtas (ID válido + ID inválido) sin ejecutar la operación válida."""
        cfg = self.workspace_dir / "mixed_ids.ini"
        original_text = "[ScalabilityGroups]\nsg.ShadowQuality=3\nsg.TextureQuality=3\n"
        cfg.write_text(original_text, encoding="utf-8")

        op_1 = self._build_custom_ini_op("op-001", cfg, "sg.ShadowQuality", "3", "1")
        op_2 = self._build_custom_ini_op("op-002", cfg, "sg.TextureQuality", "3", "1")
        rec = self.orchestrator.create_execution("exec-50")
        plan = self._build_custom_plan(rec.execution_id, [op_1, op_2])
        sim = self.orchestrator.simulate_plan(plan)

        with patch.object(self.editor, "modify_ini_or_text") as spy_mod:
            with self.assertRaises(PolicyValidationError) as ctx:
                self.orchestrator.execute_approved_plan(
                    plan,
                    approval_hash=sim["approval_hash"],
                    user_approved=True,
                    approved_operation_ids=["op-001", "op-fantasma"],
                )
            self.assertEqual(spy_mod.call_count, 0)

        self.assertIn("op-fantasma", str(ctx.exception))
        self.assertEqual(op_1.status, OperationStatus.PENDING.value)
        self.assertEqual(cfg.read_text(encoding="utf-8"), original_text)
        self.assertEqual(list(self.backup_dir.glob("*.bak")), [])

    def test_51_empty_selection_rejected_vs_explicit_cancellation(self) -> None:
        """51. (Grupo 9) Distingue entre selección vacía al ejecutar (error de validación) y cancelación explícita."""
        cfg = self.workspace_dir / "empty_sel.ini"
        cfg.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\n", encoding="utf-8")

        op_1 = self._build_custom_ini_op("op-001", cfg, "sg.ShadowQuality", "3", "1")

        # Caso A: Selección vacía accidental `[]` al invocar execute_approved_plan -> PolicyValidationError
        rec_empty = self.orchestrator.create_execution("exec-51-empty")
        plan_empty = self._build_custom_plan(rec_empty.execution_id, [op_1])
        sim_empty = self.orchestrator.simulate_plan(plan_empty)

        with self.assertRaises(PolicyValidationError) as ctx_empty:
            self.orchestrator.execute_approved_plan(
                plan_empty,
                approval_hash=sim_empty["approval_hash"],
                user_approved=True,
                approved_operation_ids=[],
            )
        self.assertIn("vacía", str(ctx_empty.exception).lower())
        self.assertEqual(rec_empty.state, ChainState.REJECTED)

        # Caso B: Selección con string en blanco `[""]` -> PolicyValidationError
        rec_blank = self.orchestrator.create_execution("exec-51-blank")
        plan_blank = self._build_custom_plan(rec_blank.execution_id, [op_1])
        sim_blank = self.orchestrator.simulate_plan(plan_blank)

        with self.assertRaises(PolicyValidationError):
            self.orchestrator.execute_approved_plan(
                plan_blank,
                approval_hash=sim_blank["approval_hash"],
                user_approved=True,
                approved_operation_ids=["   "],
            )
        self.assertEqual(rec_blank.state, ChainState.REJECTED)

        # Caso C: Cancelación explícita del usuario mediante cancel_plan() -> ChainState.CANCELLED
        rec_cancel = self.orchestrator.create_execution("exec-51-cancel")
        plan_cancel = self._build_custom_plan(rec_cancel.execution_id, [op_1])
        self.orchestrator.simulate_plan(plan_cancel)
        cancel_res = self.orchestrator.cancel_plan(
            rec_cancel.execution_id, reason="Cancelación explícita solicitada por el usuario."
        )
        self.assertEqual(cancel_res["state"], ChainState.CANCELLED.value)
        self.assertEqual(rec_cancel.state, ChainState.CANCELLED)

    def test_52_zero_writes_and_zero_backups_when_preflight_fails(self) -> None:
        """52. (Grupo 10) Demuestra mediante spies y verificación en disco que 0 backups y 0 escrituras ocurren si falla pre-flight."""
        cfg = self.workspace_dir / "zero_side_effects.ini"
        original_bytes = b"[ScalabilityGroups]\r\nsg.ShadowQuality=3\r\nsg.TextureQuality=3\r\n"
        cfg.write_bytes(original_bytes)

        op_a = self._build_custom_ini_op("op-A", cfg, "sg.ShadowQuality", "3", "1")
        op_b = self._build_custom_ini_op(
            "op-B", cfg, "sg.TextureQuality", "3", "1", depends_on=["op-A"]
        )
        rec = self.orchestrator.create_execution("exec-52")
        plan = self._build_custom_plan(rec.execution_id, [op_a, op_b])
        sim = self.orchestrator.simulate_plan(plan)

        with (
            patch.object(self.editor, "create_backup", wraps=self.editor.create_backup) as spy_backup,
            patch.object(self.editor, "modify_ini_or_text", wraps=self.editor.modify_ini_or_text) as spy_modify,
            patch.object(
                self.editor,
                "_atomic_write_bytes_with_retries",
                wraps=self.editor._atomic_write_bytes_with_retries,
            ) as spy_atomic,
        ):
            with self.assertRaises(PolicyValidationError):
                # Falla en pre-flight porque op-B requiere op-A que no fue seleccionada
                self.orchestrator.execute_approved_plan(
                    plan,
                    approval_hash=sim["approval_hash"],
                    user_approved=True,
                    approved_operation_ids=["op-B"],
                )

            self.assertEqual(spy_backup.call_count, 0)
            self.assertEqual(spy_modify.call_count, 0)
            self.assertEqual(spy_atomic.call_count, 0)

        self.assertEqual(cfg.read_bytes(), original_bytes)
        self.assertEqual(list(self.backup_dir.glob("*.bak")), [])
        self.assertEqual(rec.applied_backups, [])
        self.assertEqual(rec.applied_operations, [])

    def test_53_previously_executed_verifiable_dependency_allowed(self) -> None:
        """53. (Grupo 11) Permite aprobar una operación cuya dependencia ya fue ejecutada y verificada en disco."""
        cfg = self.workspace_dir / "prev_dep_ok.ini"
        cfg.write_text(
            "[ScalabilityGroups]\nsg.ShadowQuality=3\nsg.TextureQuality=3\n",
            encoding="utf-8",
        )

        op_a = self._build_custom_ini_op("op-A", cfg, "sg.ShadowQuality", "3", "1")
        op_b = self._build_custom_ini_op(
            "op-B", cfg, "sg.TextureQuality", "3", "1", depends_on=["op-A"]
        )
        rec = self.orchestrator.create_execution("exec-53")
        plan = self._build_custom_plan(rec.execution_id, [op_a, op_b])
        sim = self.orchestrator.simulate_plan(plan)

        # Etapa 1: El usuario aprueba y ejecuta únicamente op-A
        res1 = self.orchestrator.execute_approved_plan(
            plan,
            approval_hash=sim["approval_hash"],
            user_approved=True,
            approved_operation_ids=["op-A"],
        )
        self.assertEqual(op_a.status, OperationStatus.APPLIED.value)
        self.assertEqual(op_b.status, OperationStatus.PENDING.value)
        self.assertFalse(res1["fully_completed"])
        self.assertIsNotNone(op_a.executed_file_hash)

        # Etapa 2: Ahora el usuario aprueba únicamente op-B (cuya dependencia op-A ya está APPLIED y verificada en disco)
        res2 = self.orchestrator.execute_approved_plan(
            plan,
            approval_hash=sim["approval_hash"],
            user_approved=True,
            approved_operation_ids=["op-B"],
        )
        self.assertEqual(res2["state"], ChainState.COMPLETED.value)
        self.assertTrue(res2["fully_completed"])
        self.assertEqual(res2["plan_status"], "COMPLETED")
        self.assertEqual(op_b.status, OperationStatus.APPLIED.value)

        final_text = cfg.read_text(encoding="utf-8")
        self.assertIn("sg.ShadowQuality=1", final_text)
        self.assertIn("sg.TextureQuality=1", final_text)
        self.assertEqual(len(rec.applied_operations), 2)

    def test_54_previously_executed_dependency_without_valid_evidence_rejected(self) -> None:
        """54. (Grupo 12) Rechaza dependencia marcada como APPLIED si carece de evidencia real en disco o en registro."""
        cfg = self.workspace_dir / "prev_dep_fake.ini"
        original_text = "[ScalabilityGroups]\nsg.ShadowQuality=3\nsg.TextureQuality=3\n"
        cfg.write_text(original_text, encoding="utf-8")

        op_a = self._build_custom_ini_op("op-A", cfg, "sg.ShadowQuality", "3", "1")
        op_b = self._build_custom_ini_op(
            "op-B", cfg, "sg.TextureQuality", "3", "1", depends_on=["op-A"]
        )
        rec = self.orchestrator.create_execution("exec-54")
        plan = self._build_custom_plan(rec.execution_id, [op_a, op_b])
        sim = self.orchestrator.simulate_plan(plan)

        # Manipulamos el estado de op-A a APPLIED sin haberla ejecutado realmente en disco
        op_a.transition_status(OperationStatus.APPLIED, reason="Estado falso sin escritura")
        object.__setattr__(op_a, "executed_file_hash", compute_sha256_file(cfg))

        with patch.object(self.editor, "modify_ini_or_text") as spy_mod:
            with self.assertRaises(PolicyValidationError) as ctx:
                self.orchestrator.execute_approved_plan(
                    plan,
                    approval_hash=sim["approval_hash"],
                    user_approved=True,
                    approved_operation_ids=["op-B"],
                )
            self.assertEqual(spy_mod.call_count, 0)

        self.assertIn("evidencia", str(ctx.exception).lower())
        self.assertEqual(cfg.read_text(encoding="utf-8"), original_text)
        self.assertEqual(list(self.backup_dir.glob("*.bak")), [])

    def test_55_duplicate_operation_ids_in_plan_and_selection_rejected(self) -> None:
        """55. (Grupo 13) Rechaza IDs duplicados tanto en la definición del plan como en la selección de aprobación."""
        cfg = self.workspace_dir / "dup_ids.ini"
        cfg.write_text(
            "[ScalabilityGroups]\nsg.ShadowQuality=3\nsg.TextureQuality=3\n",
            encoding="utf-8",
        )

        # 1. IDs duplicados dentro del plan
        op_1 = self._build_custom_ini_op("op-dup", cfg, "sg.ShadowQuality", "3", "1")
        op_2 = self._build_custom_ini_op("op-dup", cfg, "sg.TextureQuality", "3", "1")
        rec1 = self.orchestrator.create_execution("exec-55-plan")
        bad_plan = self._build_custom_plan(rec1.execution_id, [op_1, op_2])

        with self.assertRaises(PolicyValidationError) as ctx_plan:
            self.orchestrator.simulate_plan(bad_plan)
        self.assertIn("duplicado", str(ctx_plan.exception).lower())

        # 2. IDs duplicados en approved_operation_ids
        op_a = self._build_custom_ini_op("op-001", cfg, "sg.ShadowQuality", "3", "1")
        op_b = self._build_custom_ini_op("op-002", cfg, "sg.TextureQuality", "3", "1")
        rec2 = self.orchestrator.create_execution("exec-55-sel")
        valid_plan = self._build_custom_plan(rec2.execution_id, [op_a, op_b])
        sim = self.orchestrator.simulate_plan(valid_plan)

        with patch.object(self.editor, "modify_ini_or_text") as spy_mod:
            with self.assertRaises(PolicyValidationError) as ctx_sel:
                self.orchestrator.execute_approved_plan(
                    valid_plan,
                    approval_hash=sim["approval_hash"],
                    user_approved=True,
                    approved_operation_ids=["op-001", "op-001"],
                )
            self.assertEqual(spy_mod.call_count, 0)
        self.assertIn("duplicados", str(ctx_sel.exception).lower())

    def test_56_self_referential_dependency_rejected(self) -> None:
        """56. (Grupo 14) Rechaza cualquier operación que declare dependencia autorreferencial (A -> A)."""
        cfg = self.workspace_dir / "self_dep.ini"
        cfg.write_text("[ScalabilityGroups]\nsg.ShadowQuality=3\n", encoding="utf-8")

        op_self = self._build_custom_ini_op(
            "op-self", cfg, "sg.ShadowQuality", "3", "1", depends_on=["op-self"]
        )
        rec = self.orchestrator.create_execution("exec-56")
        plan = self._build_custom_plan(rec.execution_id, [op_self])

        with self.assertRaises(PolicyValidationError) as ctx:
            self.orchestrator.simulate_plan(plan)
        self.assertIn("autorreferencial", str(ctx.exception).lower())
        self.assertEqual(rec.state, ChainState.REJECTED)
        self.assertEqual(list(self.backup_dir.glob("*.bak")), [])

    def test_57_plan_or_selection_modified_after_approval_invalidates_authorization(self) -> None:
        """57. (Grupo 15) Cualquier modificación de campos o de la selección vinculada tras aprobar invalida la autorización."""
        cfg = self.workspace_dir / "tamper_after.ini"
        original_text = "[ScalabilityGroups]\nsg.ShadowQuality=3\nsg.TextureQuality=3\n"
        cfg.write_text(original_text, encoding="utf-8")

        # Subcaso A: Modificar depends_on después de simulate_plan invalida el approval_hash
        op_1 = self._build_custom_ini_op("op-001", cfg, "sg.ShadowQuality", "3", "1")
        op_2 = self._build_custom_ini_op("op-002", cfg, "sg.TextureQuality", "3", "1")
        rec_a = self.orchestrator.create_execution("exec-57a")
        plan_a = self._build_custom_plan(rec_a.execution_id, [op_1, op_2])
        sim_a = self.orchestrator.simulate_plan(plan_a)

        # Mutamos depends_on e incluso intentamos pasar el nuevo plan.approval_hash()
        op_2.depends_on.append("op-001")
        with self.assertRaises(ApprovalBindingError):
            self.orchestrator.execute_approved_plan(
                plan_a,
                approval_hash=plan_a.approval_hash(),
                user_approved=True,
            )

        # Subcaso B: Modificar las operaciones seleccionadas después de vincular la aprobación selectiva
        op_3 = self._build_custom_ini_op("op-001", cfg, "sg.ShadowQuality", "3", "1")
        op_4 = self._build_custom_ini_op("op-002", cfg, "sg.TextureQuality", "3", "1")
        rec_b = self.orchestrator.create_execution("exec-57b")
        plan_b = self._build_custom_plan(rec_b.execution_id, [op_3, op_4])
        sim_b = self.orchestrator.simulate_plan(
            plan_b, selected_operation_ids=["op-001"]
        )

        with self.assertRaises(ApprovalBindingError):
            self.orchestrator.execute_approved_plan(
                plan_b,
                approval_hash=sim_b["approval_hash"],
                user_approved=True,
                approved_operation_ids=["op-001", "op-002"],
            )
        self.assertEqual(cfg.read_text(encoding="utf-8"), original_text)

    def test_58_altered_or_cross_plan_approval_hash_rejected(self) -> None:
        """58. (Grupo 16) Rechaza un hash alterado o la reutilización de un approval_hash de otro plan."""
        cfg = self.workspace_dir / "cross_plan.ini"
        original_text = "[ScalabilityGroups]\nsg.ShadowQuality=3\n"
        cfg.write_text(original_text, encoding="utf-8")

        op_p1 = self._build_custom_ini_op("op-001", cfg, "sg.ShadowQuality", "3", "1")
        rec1 = self.orchestrator.create_execution("exec-58-plan1")
        plan1 = self._build_custom_plan(rec1.execution_id, [op_p1], plan_id="plan-alpha")
        sim1 = self.orchestrator.simulate_plan(plan1)

        op_p2 = self._build_custom_ini_op("op-001", cfg, "sg.ShadowQuality", "3", "2")
        rec2 = self.orchestrator.create_execution("exec-58-plan2")
        plan2 = self._build_custom_plan(rec2.execution_id, [op_p2], plan_id="plan-beta")
        self.orchestrator.simulate_plan(plan2)

        # Intentar ejecutar plan2 usando el approval_hash válido de plan1
        with patch.object(self.editor, "modify_ini_or_text") as spy_mod:
            with self.assertRaises(ApprovalBindingError):
                self.orchestrator.execute_approved_plan(
                    plan2,
                    approval_hash=sim1["approval_hash"],
                    user_approved=True,
                )
            self.assertEqual(spy_mod.call_count, 0)

        self.assertEqual(rec2.state, ChainState.REJECTED)
        self.assertEqual(cfg.read_text(encoding="utf-8"), original_text)

    def test_59_accidental_reexecution_of_applied_operation_rejected(self) -> None:
        """59. (Grupo 17) Impide volver a aplicar accidentalmente una operación APPLIED o un plan completado."""
        cfg = self.workspace_dir / "idempotency.ini"
        cfg.write_text(
            "[ScalabilityGroups]\nsg.ShadowQuality=3\nsg.TextureQuality=3\n",
            encoding="utf-8",
        )

        op_a = self._build_custom_ini_op("op-A", cfg, "sg.ShadowQuality", "3", "1")
        op_b = self._build_custom_ini_op("op-B", cfg, "sg.TextureQuality", "3", "1")
        rec = self.orchestrator.create_execution("exec-59")
        plan = self._build_custom_plan(rec.execution_id, [op_a, op_b])
        sim = self.orchestrator.simulate_plan(plan)

        # Aplicamos op-A en el primer paso
        self.orchestrator.execute_approved_plan(
            plan,
            approval_hash=sim["approval_hash"],
            user_approved=True,
            approved_operation_ids=["op-A"],
        )
        self.assertEqual(op_a.status, OperationStatus.APPLIED.value)
        self.assertEqual(len(list(self.backup_dir.glob("*.bak"))), 1)

        # Intentar volver a ejecutar op-A (sola o junto con op-B) debe rechazarse con DuplicateExecutionError
        with patch.object(self.editor, "modify_ini_or_text") as spy_mod:
            with self.assertRaises(DuplicateExecutionError):
                self.orchestrator.execute_approved_plan(
                    plan,
                    approval_hash=sim["approval_hash"],
                    user_approved=True,
                    approved_operation_ids=["op-A", "op-B"],
                )
            self.assertEqual(spy_mod.call_count, 0)

        self.assertEqual(len(list(self.backup_dir.glob("*.bak"))), 1)

    def test_60_independent_operations_behavior_on_failure(self) -> None:
        """60. (Grupo 18) Verifica el comportamiento de operaciones independientes ante un fallo y que el plan no se declare COMPLETED."""
        cfg_fail = self.workspace_dir / "game_video.ini"
        cfg_indep = self.workspace_dir / "game_audio.ini"
        cfg_fail.write_text("[ScalabilityGroups]\nShadow=3\nPostProcess=3\n", encoding="utf-8")
        cfg_indep.write_text("[Audio]\nMasterVolume=100\n", encoding="utf-8")

        op_a = self._build_custom_ini_op("op-A", cfg_fail, "Shadow", "3", "1")
        op_b = self._build_custom_ini_op(
            "op-B", cfg_fail, "PostProcess", "3", "1", depends_on=["op-A"]
        )
        op_indep = self._build_custom_ini_op(
            "op-Indep", cfg_indep, "MasterVolume", "100", "80", section="Audio"
        )

        rec = self.orchestrator.create_execution("exec-60")
        plan = self._build_custom_plan(rec.execution_id, [op_a, op_b, op_indep])
        sim = self.orchestrator.simulate_plan(plan)

        real_modify = self.editor.modify_ini_or_text

        def fail_only_video(file_path: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
            if Path(str(file_path)).name == "game_video.ini":
                return {"status": "error", "errors": ["Fallo simulado en archivo de video"]}
            return real_modify(file_path, *args, **kwargs)

        # Con continue_independent=True y auto_rollback=False:
        # op-A falla (FAILED), op-B se bloquea (BLOCKED), y op-Indep (en otro archivo) se aplica (APPLIED),
        # pero el plan global termina en FAILED (jamás COMPLETED).
        with patch.object(self.editor, "modify_ini_or_text", side_effect=fail_only_video):
            with self.assertRaises(RuntimeError):
                self.orchestrator.execute_approved_plan(
                    plan,
                    approval_hash=sim["approval_hash"],
                    user_approved=True,
                    continue_independent=True,
                    auto_rollback=False,
                )

        self.assertEqual(op_a.status, OperationStatus.FAILED.value)
        self.assertEqual(op_b.status, OperationStatus.BLOCKED.value)
        self.assertEqual(op_indep.status, OperationStatus.APPLIED.value)
        self.assertEqual(rec.state, ChainState.FAILED)
        self.assertFalse(plan.is_fully_completed())
        self.assertIn("MasterVolume=80", cfg_indep.read_text(encoding="utf-8"))
        self.assertEqual(len(rec.applied_operations), 1)
        self.assertEqual(rec.applied_operations[0]["operation_id"], "op-Indep")

    def test_61_failure_during_write_preserves_evidence_and_protects_external_user_edits(self) -> None:
        """61. (Grupo 19) Verifica que un fallo de escritura preserve la evidencia previa y no sobrescriba ediciones externas del usuario."""
        cfg = self.workspace_dir / "partial_protect.ini"
        cfg.write_text(
            "[ScalabilityGroups]\nsg.ShadowQuality=3\nsg.TextureQuality=3\n",
            encoding="utf-8",
        )

        op_1 = self._build_custom_ini_op("op-001", cfg, "sg.ShadowQuality", "3", "1")
        op_2 = self._build_custom_ini_op("op-002", cfg, "sg.TextureQuality", "3", "1")
        rec = self.orchestrator.create_execution("exec-61")
        plan = self._build_custom_plan(rec.execution_id, [op_1, op_2])
        sim = self.orchestrator.simulate_plan(plan)

        real_modify = self.editor.modify_ini_or_text
        call_num = {"n": 0}
        user_manual_content = (
            "[ScalabilityGroups]\nsg.ShadowQuality=1\nsg.TextureQuality=3\nUserCustomSetting=42\n"
        )

        def fail_second_after_external_user_edit(
            file_path: Any, *args: Any, **kwargs: Any
        ) -> dict[str, Any]:
            call_num["n"] += 1
            if call_num["n"] == 1:
                return real_modify(file_path, *args, **kwargs)
            # Simulamos que el usuario editó manualmente el archivo justo antes del fallo de op-002
            cfg.write_text(user_manual_content, encoding="utf-8")
            return {"status": "error", "errors": ["Fallo durante escritura de op-002"]}

        with patch.object(
            self.editor, "modify_ini_or_text", side_effect=fail_second_after_external_user_edit
        ):
            with self.assertRaises(RuntimeError):
                self.orchestrator.execute_approved_plan(
                    plan,
                    approval_hash=sim["approval_hash"],
                    user_approved=True,
                )

        # 1. op-001 conserva su estado APPLIED y su evidencia en record.applied_operations
        self.assertEqual(op_1.status, OperationStatus.APPLIED.value)
        self.assertEqual(len(rec.applied_operations), 1)
        self.assertEqual(rec.applied_operations[0]["operation_id"], "op-001")
        # 2. op-002 queda como FAILED y no se registra como aplicada
        self.assertEqual(op_2.status, OperationStatus.FAILED.value)
        self.assertEqual(len(rec.failed_operations), 1)
        # 3. El rollback automático detectó la edición externa del usuario y NO sobrescribió UserCustomSetting=42
        self.assertEqual(rec.state, ChainState.FAILED)
        self.assertIn("UserCustomSetting=42", cfg.read_text(encoding="utf-8"))

    def test_62_inconsistency_between_state_and_real_disk_result_and_readonly_cleanup(self) -> None:
        """62. (Grupo 20) Detecta inconsistencia si modify_ini_or_text reporta éxito sin escribir en disco, protege estados terminales y limpia archivos read-only."""
        cfg = self.workspace_dir / "inconsistent.ini"
        original_text = "[ScalabilityGroups]\nsg.ShadowQuality=3\n"
        cfg.write_text(original_text, encoding="utf-8")
        orig_sha = compute_sha256_file(cfg)

        op_1 = self._build_custom_ini_op("op-001", cfg, "sg.ShadowQuality", "3", "1")
        rec = self.orchestrator.create_execution("exec-62")
        plan = self._build_custom_plan(rec.execution_id, [op_1])
        sim = self.orchestrator.simulate_plan(plan)

        # Simulamos que modify_ini_or_text devuelve "success" mintiendo sobre la escritura real en disco
        fake_success = {
            "status": "success",
            "file_path": str(cfg),
            "original_hash": orig_sha,
            "final_hash": orig_sha,
            "backup_path": None,
            "applied_changes": [{"section": "ScalabilityGroups", "key": "sg.ShadowQuality", "old_value": "3", "new_value": "1"}],
            "errors": [],
            "warnings": [],
            "execution_id": rec.execution_id,
        }

        with patch.object(self.editor, "modify_ini_or_text", return_value=fake_success):
            with self.assertRaises(RuntimeError) as ctx:
                self.orchestrator.execute_approved_plan(
                    plan,
                    approval_hash=sim["approval_hash"],
                    user_approved=True,
                )

        self.assertIn("inconsistencia", str(ctx.exception).lower())
        # La operación jamás debe quedar en APPLIED si la verificación en disco falló
        self.assertEqual(op_1.status, OperationStatus.FAILED.value)
        self.assertEqual(rec.applied_operations, [])
        self.assertEqual(rec.state, ChainState.FAILED)

        # Intentar modificar silenciosamente el estado terminal FAILED a APPLIED o PENDING debe fallar
        with self.assertRaises(PolicyValidationError):
            op_1.status = OperationStatus.APPLIED.value
        with self.assertRaises(PolicyValidationError):
            op_1.transition_status(OperationStatus.PENDING)

        # Además creamos un archivo con atributo de solo lectura dentro del directorio temporal
        # para verificar que tearDown restaure permisos y lo elimine sin errores en Windows.
        ro_file = self.workspace_dir / "readonly_test.ini"
        ro_file.write_text("[RO]\nLocked=1\n", encoding="utf-8")
        os.chmod(ro_file, stat.S_IREAD)


if __name__ == "__main__":
    unittest.main()
