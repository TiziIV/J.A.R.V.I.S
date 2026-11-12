"""Defensas contra persistencia accidental de pruebas en la memoria real."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

os.environ["JARVIS_ENV"] = "test"
from src.brain.memory import ConversationMemory, DEFAULT_MEMORY_FILE


class TestMemoryPersistenceIsolation(unittest.TestCase):
    """Cada caso crea su propio sandbox temporal y conserva intacta la memoria local."""

    def setUp(self) -> None:
        authorized_root = Path(__file__).resolve().parents[1] / "temp" / "jarvis_test_isolated"
        self.run_root = authorized_root / f"run_{os.urandom(8).hex()}"
        self.test_root = self.run_root / f"test_{self._testMethodName}_{os.urandom(4).hex()}"
        self.test_root.mkdir(parents=True, exist_ok=True)
        self._environment = patch.dict(
            os.environ,
            {"JARVIS_ENV": "test", "JARVIS_TEST_TEMP_ROOT": str(self.test_root)},
        )
        self._environment.start()
        self.real_path = Path(DEFAULT_MEMORY_FILE)
        self.real_fingerprint = self._fingerprint(self.real_path)

    def tearDown(self) -> None:
        try:
            self.assertEqual(self._fingerprint(self.real_path), self.real_fingerprint)
        finally:
            if self.test_root.exists():
                shutil.rmtree(self.test_root)
            if self.run_root.exists():
                self.run_root.rmdir()
            self._environment.stop()

    @staticmethod
    def _fingerprint(path: Path) -> tuple[int, str]:
        """Lee únicamente metadatos y bytes para comparar integridad, nunca imprime el contenido."""
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as memory_file:
            for chunk in iter(lambda: memory_file.read(65536), b""):
                size += len(chunk)
                digest.update(chunk)
        return size, digest.hexdigest()

    def test_default_memory_uses_unique_windows_safe_temp_paths(self) -> None:
        first = ConversationMemory()
        second = ConversationMemory()

        self.assertIsNotNone(first.storage_file)
        self.assertTrue(Path(first.storage_file).is_relative_to(self.test_root))
        self.assertTrue(Path(first.storage_file).parent.exists())
        self.assertNotEqual(first.storage_file, second.storage_file)

    def test_create_update_and_delete_only_change_temp_storage(self) -> None:
        memory = ConversationMemory(max_turns=2)
        memory.add_message("user", "dato de prueba")
        memory.set_preference("tema", "temporal")
        self.assertTrue(Path(memory.storage_file).exists())
        memory.set_preference("tema", "actualizado")
        memory.clear(clear_preferences=True)

        self.assertEqual(memory.get_messages_list(), [])
        self.assertEqual(memory.get_preferences(), {})
        self.assertEqual(self._fingerprint(self.real_path), self.real_fingerprint)

    def test_reinitialization_does_not_reuse_a_production_or_stale_path(self) -> None:
        first = ConversationMemory()
        first.add_message("user", "solo en esta instancia")
        reinitialized = ConversationMemory()

        self.assertNotEqual(first.storage_file, reinitialized.storage_file)
        self.assertEqual(reinitialized.get_messages_list(), [])
        self.assertNotEqual(Path(first.storage_file).resolve(), self.real_path.resolve())

    def test_production_path_is_rejected_for_constructor_and_file_override(self) -> None:
        with self.assertRaises(ValueError):
            ConversationMemory(storage_file=str(self.real_path))

        memory = ConversationMemory()
        self.assertFalse(memory.save_to_disk(str(self.real_path)))
        self.assertFalse(memory.load_from_disk(str(self.real_path)))

    def test_force_persistence_environment_variable_cannot_bypass_path_guard(self) -> None:
        with patch.dict(os.environ, {"JARVIS_FORCE_PERSISTENCE": "1"}):
            with self.assertRaises(ValueError):
                ConversationMemory(storage_file=str(self.real_path))

    def test_save_override_outside_authorized_root_is_not_written(self) -> None:
        memory = ConversationMemory()
        outside_path = self.test_root.parent / "jarvis-unauthorized-memory.json"

        self.assertFalse(memory.save_to_disk(str(outside_path)))
        self.assertFalse(outside_path.exists())

    def test_environment_cannot_redefine_authorized_root_to_data(self) -> None:
        with patch.dict(os.environ, {"JARVIS_TEST_TEMP_ROOT": str(self.real_path.parent)}):
            with self.assertRaises(ValueError):
                ConversationMemory()

    def test_imported_module_has_no_captured_memory_instance(self) -> None:
        import src.brain.memory as memory_module

        self.assertFalse(any(isinstance(value, ConversationMemory) for value in vars(memory_module).values()))

    def test_runner_import_does_not_enable_test_mode_without_explicit_environment(self) -> None:
        from src.brain.memory import _is_test_environment
        with patch.dict(os.environ, {"JARVIS_ENV": ""}):
            import sys
            self.assertIn("unittest", sys.modules)
            self.assertFalse(_is_test_environment())

    def test_imported_openwakeword_name_does_not_change_production_mode(self) -> None:
        from src.brain.memory import _is_test_environment
        with patch.dict(os.environ, {"JARVIS_ENV": ""}):
            import openwakeword
            import tqdm
            self.assertFalse(_is_test_environment())
            with patch.object(ConversationMemory, "load_from_disk", return_value=False):
                production_memory = ConversationMemory()
            self.assertFalse(production_memory._test_mode)
            self.assertEqual(production_memory.storage_file, DEFAULT_MEMORY_FILE)

    def test_corrupt_store_is_preserved_and_autosave_is_blocked(self) -> None:
        corrupt = self.test_root / "corrupt.json"
        corrupt.write_text("{broken", encoding="utf-8")
        original = corrupt.read_bytes()
        memory = ConversationMemory(storage_file=str(corrupt), auto_save=True)
        self.assertEqual(memory.storage_status, "corrupt")
        memory.add_message("user", "nuevo")
        self.assertEqual(corrupt.read_bytes(), original)
        self.assertFalse(memory.save_to_disk())

    def test_alternate_save_does_not_unblock_corrupt_primary_store(self) -> None:
        corrupt = self.test_root / "corrupt_primary.json"
        alternate = self.test_root / "alternate.json"
        corrupt.write_text("{broken", encoding="utf-8")
        original = corrupt.read_bytes()
        memory = ConversationMemory(storage_file=str(corrupt), auto_save=False)
        self.assertTrue(memory.save_to_disk(str(alternate)))
        self.assertEqual(memory.storage_status, "corrupt")
        self.assertTrue(memory._storage_blocked)
        memory.auto_save = True
        memory.add_message("user", "safe")
        self.assertEqual(corrupt.read_bytes(), original)

    def test_alternate_load_never_unblocks_corrupt_primary(self) -> None:
        primary = self.test_root / "primary_corrupt.json"
        alternate = self.test_root / "alternate_valid.json"
        primary.write_bytes(b"{broken-primary")
        alternate.write_text('{"messages": [], "preferences": {}}', encoding="utf-8")
        original = primary.read_bytes()
        memory = ConversationMemory(storage_file=str(primary))
        original_error = memory.last_storage_error

        self.assertTrue(memory.load_from_disk(str(alternate)))
        self.assertTrue(memory._storage_blocked)
        self.assertEqual(memory.storage_status, "corrupt")
        self.assertEqual(memory.last_storage_error, original_error)
        memory.add_message("user", "not persisted to primary")
        self.assertEqual(primary.read_bytes(), original)
        self.assertEqual(hashlib.sha256(primary.read_bytes()).digest(), hashlib.sha256(original).digest())
        self.assertFalse(list(self.test_root.glob("primary_corrupt.json.corrupt.*")))

    def test_missing_and_corrupt_alternate_loads_preserve_primary_block(self) -> None:
        primary = self.test_root / "primary_for_bad_alternates.json"
        corrupt_alternate = self.test_root / "alternate_corrupt.json"
        primary.write_text("{bad-primary", encoding="utf-8")
        corrupt_alternate.write_text("{bad-alternate", encoding="utf-8")
        memory = ConversationMemory(storage_file=str(primary))
        primary_bytes = primary.read_bytes()
        primary_error = memory.last_storage_error

        self.assertFalse(memory.load_from_disk(str(self.test_root / "absent.json")))
        self.assertFalse(memory.load_from_disk(str(corrupt_alternate)))
        self.assertTrue(memory._storage_blocked)
        self.assertEqual(memory.last_storage_error, primary_error)
        memory.add_message("user", "still blocked")
        self.assertEqual(primary.read_bytes(), primary_bytes)

    def test_recovery_preserves_configured_turn_window_above_default(self) -> None:
        primary = self.test_root / "wide_window_primary.json"
        replacement = self.test_root / "wide_window_replacement.json"
        primary.write_text("{damaged", encoding="utf-8")
        source_messages = [
            {"role": "user", "content": f"turn-{index}"}
            for index in range(26)
        ]
        replacement.write_text(
            json.dumps({"messages": source_messages, "preferences": {}}),
            encoding="utf-8",
        )
        memory = ConversationMemory(max_turns=10, storage_file=str(primary))
        self.assertTrue(memory.recover_from_file(replacement))
        self.assertEqual(memory._messages.maxlen, 20)
        self.assertEqual(len(memory.get_messages_list()), 20)
        self.assertEqual(memory.get_messages_list()[0]["content"], "turn-6")

    def test_recovery_original_read_error_keeps_memory_and_primary_bytes(self) -> None:
        primary = self.test_root / "unreadable_recovery_primary.json"
        replacement = self.test_root / "readable_replacement.json"
        primary.write_bytes(b"{preserve-this")
        replacement.write_text('{"messages": [{"role": "user", "content": "candidate"}], "preferences": {}}', encoding="utf-8")
        memory = ConversationMemory(storage_file=str(primary), auto_save=False)
        memory._messages.append({"role": "assistant", "content": "current state"})
        original_messages = memory.get_messages_list()
        original_bytes = primary.read_bytes()
        builtins_open = open

        def fail_primary_read(path: str | os.PathLike[str], mode: str = "r", *args: object, **kwargs: object):
            if Path(path) == primary and mode == "rb":
                raise PermissionError("denied original recovery read")
            return builtins_open(path, mode, *args, **kwargs)

        with patch("builtins.open", side_effect=fail_primary_read):
            self.assertFalse(memory.recover_from_file(replacement))
        self.assertTrue(memory._storage_blocked)
        self.assertIn("denied original recovery read", memory.last_storage_error or "")
        self.assertEqual(memory.get_messages_list(), original_messages)
        self.assertEqual(primary.read_bytes(), original_bytes)

    def test_valid_empty_store_loads_and_durable_save_uses_fsync(self) -> None:
        empty = self.test_root / "empty.json"
        empty.write_text('{"version": 1, "messages": [], "preferences": {}}', encoding="utf-8")
        memory = ConversationMemory(storage_file=str(empty))
        self.assertEqual(memory.storage_status, "loaded")
        memory.add_message("user", "durable")
        self.assertTrue(memory.save_to_disk())

    def test_recovery_preserves_corrupt_original_before_replacement(self) -> None:
        damaged = self.test_root / "damaged.json"
        replacement = self.test_root / "replacement.json"
        damaged.write_text("{bad", encoding="utf-8")
        replacement.write_text('{"messages": [{"role": "user", "content": "restored"}], "preferences": {}}', encoding="utf-8")
        memory = ConversationMemory(storage_file=str(damaged))
        self.assertTrue(memory.recover_from_file(replacement))
        preserved = list(self.test_root.glob("damaged.json.corrupt.*"))
        self.assertTrue(preserved)
        self.assertTrue(any(item.read_text(encoding="utf-8") == "{bad" for item in preserved))
        self.assertEqual(memory.get_messages_list()[0]["content"], "restored")

    def test_replace_failure_preserves_original_corrupt_file(self) -> None:
        damaged = self.test_root / "replace_failure.json"
        damaged.write_text("{bad", encoding="utf-8")
        replacement = self.test_root / "replacement_valid.json"
        replacement.write_text('{"messages": [], "preferences": {}}', encoding="utf-8")
        memory = ConversationMemory(storage_file=str(damaged))
        with patch("src.brain.memory.os.replace", side_effect=OSError("simulated replace failure")):
            self.assertFalse(memory.recover_from_file(replacement))
        self.assertEqual(damaged.read_text(encoding="utf-8"), "{bad")

    def test_unreadable_file_is_distinguished_and_not_overwritten(self) -> None:
        target = self.test_root / "unreadable.json"
        target.write_text('{"messages": [], "preferences": {}}', encoding="utf-8")
        original = target.read_bytes()
        with patch("builtins.open", side_effect=PermissionError("denied")):
            memory = ConversationMemory(storage_file=str(target))
        self.assertEqual(memory.storage_status, "unreadable")
        memory.add_message("user", "must not overwrite")
        self.assertEqual(target.read_bytes(), original)

    def test_write_failure_keeps_existing_store_intact(self) -> None:
        target = self.test_root / "write_failure.json"
        target.write_text('{"messages": [], "preferences": {}}', encoding="utf-8")
        original = target.read_bytes()
        memory = ConversationMemory(storage_file=str(target))
        with patch("src.brain.memory.json.dump", side_effect=OSError("simulated write failure")):
            self.assertFalse(memory.save_to_disk())
        self.assertEqual(target.read_bytes(), original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
