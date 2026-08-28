from __future__ import annotations

import unittest

from production_hub.core.config.models import PanasonicConfig
from production_hub.integrations.panasonic_awp.client import PanasonicAwpClient
from production_hub.integrations.panasonic_awp.service import (
    PanasonicAwpService,
    clamp_absolute_position,
    clamp_zoom_position,
)


class PanasonicClientTests(unittest.TestCase):
    def test_cgi_request_construction(self) -> None:
        client = PanasonicAwpClient(PanasonicConfig())
        url = client.build_url("#PTS5050")
        self.assertEqual(url, "http://192.168.50.80/cgi-bin/aw_ptz?cmd=%23PTS5050&res=1")
        cam_url = client.build_url("DUS:1", "aw_cam")
        self.assertEqual(cam_url, "http://192.168.50.80/cgi-bin/aw_cam?cmd=DUS%3A1&res=1")

    def test_absolute_position_clamps_match_panasonic_protocol_ranges(self) -> None:
        self.assertEqual(0x0000, clamp_absolute_position(-1))
        self.assertEqual(0xFFFF, clamp_absolute_position(0x10000))
        self.assertEqual(0x555, clamp_zoom_position(0))
        self.assertEqual(0xFFF, clamp_zoom_position(0x2000))

    def test_absolute_position_commands_use_fixed_width_uppercase_hex(self) -> None:
        service = PanasonicAwpService(PanasonicConfig())
        commands: list[str] = []

        async def record(command: str, endpoint: str = "aw_ptz") -> bool:
            commands.append(f"{endpoint}:{command}")
            return True

        service.send_command = record  # type: ignore[method-assign]
        import asyncio

        asyncio.run(service.absolute_pan_tilt(0x7ABC, 0x8123))
        asyncio.run(service.absolute_zoom(0x6EF))
        self.assertEqual(
            ["aw_ptz:#APC7ABC8123", "aw_ptz:#AXZ6EF"],
            commands,
        )


if __name__ == "__main__":
    unittest.main()
