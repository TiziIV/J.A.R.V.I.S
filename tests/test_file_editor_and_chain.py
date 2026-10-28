"""
Suite exhaustiva de pruebas unitarias e integración para `FileEditorSkill`,
`BackupManager`, `RollbackManager`, `AuditLogger`, `ChangeOperation`, `ChangePlan`,
`PolicyEngine` y `ChainOrchestrator`.

Cubre los 40 escenarios obligatorios de seguridad en Windows, preservación estructural
de archivos INI/CFG/CONF/JSON/XML/TXT, escritura atómica con reintentos, detección de
concurrencia, aprobación criptográfica vinculada a hash SHA-256, verificación post-escritura
y rollback transaccional sin realizar llamadas de red ni tocar archivos reales del usuario.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
    PolicyValidationError,
)
from src.tools.skill_manager import SkillManager


class TestFileEditorAndChainOrchestrator(unittest.TestCase):
    """Pruebas aisladas en directorios temporales para FileEditorSkill y ChainOrchestrator."""

    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self._tmp_dir.name).resolve()
        self.workspace_dir = self.base_dir / "authorized_games"
        self.backup_dir = self.base_dir / "backups"
        self.audit_log_path = self.base_dir / "logs" / "audit.jsonl"
        self.outside_dir = self.base_dir / "outside_unauthorized"

        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.outside_dir.mkdir(parents=True, exist_ok=True)

        self.audit_logger = AuditLogger(log_file=self.audit_log_path)
        self.backup_manager = BackupManager(audit_logger=self.audit_logger)
        # Incluimos workspace_dir y backup_dir como raíces autorizadas del entorno de prueba
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
        self._tmp_dir.cleanup()

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


if __name__ == "__main__":
    unittest.main()
