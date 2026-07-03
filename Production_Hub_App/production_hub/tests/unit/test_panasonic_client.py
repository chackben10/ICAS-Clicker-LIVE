from __future__ import annotations

import unittest

from production_hub.core.config.models import PanasonicConfig
from production_hub.integrations.panasonic_awp.client import PanasonicAwpClient


class PanasonicClientTests(unittest.TestCase):
    def test_cgi_request_construction(self) -> None:
        client = PanasonicAwpClient(PanasonicConfig())
        url = client.build_url("#PTS5050")
        self.assertEqual(url, "http://192.168.50.80/cgi-bin/aw_ptz?cmd=%23PTS5050&res=1")
        cam_url = client.build_url("DUS:1", "aw_cam")
        self.assertEqual(cam_url, "http://192.168.50.80/cgi-bin/aw_cam?cmd=DUS%3A1&res=1")


if __name__ == "__main__":
    unittest.main()

