from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from production_hub.core.config.models import AppPaths
from production_hub.core.config.repository import ConfigRepository


class ConfigRepositoryTests(unittest.TestCase):
    def test_seeds_default_profile_endpoints_and_automations(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = ConfigRepository(AppPaths(Path(temp)))
            config = repo.load_app_config()
            endpoints = repo.load_endpoints()
            automations = repo.load_automations()

            self.assertEqual(config.active_profile, "Default Profile")
            self.assertEqual(config.integrations.obs.host, "192.168.1.156")
            self.assertTrue(any(p.label == "PTZ Camera" for p in config.integrations.propresenter.presentations))
            self.assertTrue(any(endpoint.key == "next_slide" for endpoint in endpoints))
            self.assertTrue(any(automation.key == "obs_look_sync" for automation in automations))

    def test_save_creates_automatic_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = AppPaths(Path(temp))
            repo = ConfigRepository(paths)
            config = repo.load_app_config()
            config.subtitle = "Changed"
            repo.save_app_config(config)

            backups = list(paths.automatic_backups_dir.glob("default_profile-*.json"))
            self.assertGreaterEqual(len(backups), 1)


if __name__ == "__main__":
    unittest.main()

