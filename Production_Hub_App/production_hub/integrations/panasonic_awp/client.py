from __future__ import annotations

import asyncio
import base64
import urllib.parse
import urllib.request

from production_hub.core.config.models import PanasonicConfig
from production_hub.integrations.base import IntegrationBase


class PanasonicAwpClient(IntegrationBase):
    def __init__(self, config: PanasonicConfig) -> None:
        super().__init__("Panasonic AWP", config.enabled, config.camera_ip)
        self.config = config

    def build_url(self, command: str, endpoint: str = "aw_ptz") -> str:
        path = self.config.aw_cam_path if endpoint == "aw_cam" else self.config.aw_ptz_path
        params = urllib.parse.urlencode({"cmd": command, "res": "1"})
        return f"http://{self.config.camera_ip}{path}?{params}"

    def build_request(self, command: str, endpoint: str = "aw_ptz") -> urllib.request.Request:
        request = urllib.request.Request(self.build_url(command, endpoint))
        auth = base64.b64encode(f"{self.config.username}:{self.config.password}".encode("utf-8")).decode("ascii")
        request.add_header("Authorization", f"Basic {auth}")
        return request

    async def send(self, command: str, endpoint: str = "aw_ptz") -> bool:
        def _request() -> None:
            with urllib.request.urlopen(self.build_request(command, endpoint), timeout=self.config.request_timeout_seconds):
                return None

        try:
            await asyncio.to_thread(_request)
            self.mark_success()
            return True
        except Exception as exc:
            self.mark_error(str(exc))
            raise

