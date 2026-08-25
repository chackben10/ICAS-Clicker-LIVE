from __future__ import annotations

import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from production_hub.state.runtime_state import RequestRecord
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

    def test_load_is_cached_and_returns_an_isolated_snapshot(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = RuntimeStateRepository(root / "state", root / "backups")

            with patch.object(repository._repo, "load", wraps=repository._repo.load) as load:
                first = repository.load()
                first.auto_show_enabled = True
                second = repository.load()
                enabled = repository.auto_show_enabled()

            self.assertEqual(1, load.call_count)
            self.assertFalse(second.auto_show_enabled)
            self.assertFalse(enabled)

    def test_request_history_writes_are_coalesced_until_flush(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = RuntimeStateRepository(root / "state", root / "backups")
            repository.load()
            record = RequestRecord(
                timestamp=datetime.now(UTC).isoformat(),
                method="GET",
                route="/health",
                status_code=200,
            )

            with patch.object(repository._repo, "save", wraps=repository._repo.save) as save:
                for _index in range(10):
                    repository.record_request(record)
                self.assertEqual(0, save.call_count)
                repository.flush()
                self.assertEqual(1, save.call_count)

            self.assertEqual(10, len(repository.load().endpoint_request_history))
            self.assertEqual([], list((root / "backups").glob("runtime_state-*")))

    def test_failed_setting_write_does_not_change_cached_state(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = RuntimeStateRepository(root / "state", root / "backups")
            repository.load()

            with patch.object(repository._repo, "save", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    repository.update(
                        lambda state: setattr(state, "auto_show_enabled", True)
                    )

            self.assertFalse(repository.auto_show_enabled())


if __name__ == "__main__":
    unittest.main()
