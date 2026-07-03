from __future__ import annotations

import unittest
from pathlib import Path

from production_hub.core.config.defaults import build_default_config
from production_hub.core.config.remote_pages import discover_remote_pages


class RemotePageDiscoveryTests(unittest.TestCase):
    def test_discovers_all_repository_html_pages(self) -> None:
        workspace = Path(__file__).resolve().parents[4]
        pages = discover_remote_pages(workspace, build_default_config().remote_pages)
        paths = {str(page["path"]) for page in pages}

        self.assertIn("ipad-control.html", paths)
        self.assertIn("pads-control.html", paths)
        self.assertIn("scoreboard/large.html", paths)
        self.assertIn("displays/current-audio.html", paths)


if __name__ == "__main__":
    unittest.main()
