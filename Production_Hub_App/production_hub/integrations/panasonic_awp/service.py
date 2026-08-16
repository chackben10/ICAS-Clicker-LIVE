from __future__ import annotations

import re

from production_hub.core.config.models import PanasonicConfig
from production_hub.integrations.panasonic_awp.client import PanasonicAwpClient


def clamp_aw_value(value: int) -> int:
    return max(1, min(99, int(value)))


def clamp_absolute_position(value: int) -> int:
    return max(0x0000, min(0xFFFF, int(value)))


def clamp_zoom_position(value: int) -> int:
    return max(0x555, min(0xFFF, int(value)))


class PanasonicAwpService:
    def __init__(self, config: PanasonicConfig) -> None:
        self.config = config
        self.client = PanasonicAwpClient(config)

    async def test_connection(self) -> bool:
        return await self.client.send("#O", "aw_ptz")

    async def send_command(self, command: str, endpoint: str = "aw_ptz") -> bool:
        return await self.client.send(command, endpoint)

    async def query_pan_tilt_position(self) -> tuple[int, int]:
        payload = await self.client.request("#APC")
        match = re.fullmatch(r"aPC([0-9A-Fa-f]{4})([0-9A-Fa-f]{4})", payload)
        if match is None:
            raise RuntimeError(f"Unexpected Panasonic pan/tilt response: {payload!r}")
        return int(match.group(1), 16), int(match.group(2), 16)

    async def query_zoom_position(self) -> int:
        payload = await self.client.request("#GZ")
        match = re.fullmatch(r"gz([0-9A-Fa-f]{3})", payload, re.IGNORECASE)
        if match is None:
            raise RuntimeError(f"Unexpected Panasonic zoom response: {payload!r}")
        return int(match.group(1), 16)

    async def absolute_pan_tilt(self, pan: int, tilt: int) -> bool:
        command = (
            f"#APC{clamp_absolute_position(pan):04X}"
            f"{clamp_absolute_position(tilt):04X}"
        )
        return await self.send_command(command)

    async def absolute_zoom(self, position: int) -> bool:
        return await self.send_command(f"#AXZ{clamp_zoom_position(position):03X}")

    async def pan_tilt(self, pan: int = 50, tilt: int = 50) -> bool:
        return await self.send_command(f"#PTS{clamp_aw_value(pan):02d}{clamp_aw_value(tilt):02d}")

    async def stop_pan_tilt(self) -> bool:
        return await self.pan_tilt(50, 50)

    async def zoom(self, value: int) -> bool:
        return await self.send_command(f"#Z{clamp_aw_value(value):02d}")

    async def stop_zoom(self) -> bool:
        return await self.zoom(50)

    async def focus(self, value: int) -> bool:
        return await self.send_command(f"#F{clamp_aw_value(value):02d}")

    async def stop_focus(self) -> bool:
        return await self.focus(50)

    async def focus_auto(self) -> bool:
        return await self.send_command("#D11")

    async def focus_manual(self) -> bool:
        return await self.send_command("#D10")

    async def menu_on(self) -> bool:
        return await self.send_command("DUS:1", "aw_cam")

    async def menu_off(self) -> bool:
        return await self.send_command("DUS:0", "aw_cam")

    async def color_bars(self, enabled: bool) -> bool:
        return await self.send_command(f"DCB:{1 if enabled else 0}", "aw_cam")

    async def power_on(self) -> bool:
        return await self.send_command("#On")

    async def standby(self) -> bool:
        return await self.send_command("#Of")

    async def auto_white_balance(self) -> bool:
        return await self.send_command("#AWA")

    async def recall_preset(self, preset: int) -> bool:
        return await self.send_command(f"#R{int(preset):02d}")

    async def save_preset(self, preset: int) -> bool:
        if int(preset) == 0:
            raise ValueError("Preset 00 is Home and cannot be overwritten")
        return await self.send_command(f"#M{int(preset):02d}")
