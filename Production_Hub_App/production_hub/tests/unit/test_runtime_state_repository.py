from __future__ import annotations

import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from production_hub.state.state_repository import RuntimeStateRepository


class RuntimeStateRepositoryTests(unittest.TestCase):
    def test_legacy_runtime_state_without_clicker_setting_defaults_enabled(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            state_dir.mkdir(parents=True)
            (state_dir / "runtime_state.json").write_text(
                '{"auto_show_enabled": true, "health_state": {}}',
                encoding="utf-8",
            )

            state = RuntimeStateRepository(state_dir, root / "backups").load()

            self.assertTrue(state.auto_show_enabled)
            self.assertTrue(state.clicker_presentation_activation_enabled)

    def test_clicker_presentation_activation_defaults_true_and_persists(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = RuntimeStateRepository(root / "state", root / "backups")

            self.assertTrue(repository.load().clicker_presentation_activation_enabled)
            repository.update(
                lambda state: setattr(state, "clicker_presentation_activation_enabled", False)
            )

            reloaded = RuntimeStateRepository(root / "state", root / "backups").load()
            self.assertFalse(reloaded.clicker_presentation_activation_enabled)

    def test_update_keeps_concurrent_read_modify_write_operations_atomic(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = RuntimeStateRepository(root / "state", root / "backups")
            repository.load()

            def increment(_index: int) -> None:
                def mutate(state) -> None:
                    current = int(state.health_state.get("atomic_test_count", 0))
                    time.sleep(0.001)
                    state.health_state["atomic_test_count"] = current + 1

                repository.update(mutate)

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(increment, range(24)))

            self.assertEqual(24, repository.load().health_state["atomic_test_count"])


if __name__ == "__main__":
    unittest.main()
