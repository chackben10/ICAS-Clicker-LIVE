from __future__ import annotations

import asyncio
import socket
from typing import Any

from production_hub.core.config.models import ObsConfig
from production_hub.integrations.base import IntegrationBase


class ObsClient(IntegrationBase):
    def __init__(self, config: ObsConfig) -> None:
        super().__init__("OBS", config.enabled, f"{config.host}:{config.port}")
        self.config = config
        self._client: Any = None
        self.connected = False
        self._lock = asyncio.Lock()

    async def connect(self) -> bool:
        async with self._lock:
            try:
                from obsws_python import ReqClient
            except Exception as exc:
                self.connected = False
                self.mark_error(f"obsws-python is not installed: {exc}")
                return False

            try:
                await asyncio.to_thread(
                    lambda: socket.create_connection(
                        (self.config.host, self.config.port),
                        timeout=self.config.connection_timeout_seconds,
                    ).close()
                )
            except Exception as exc:
                self.connected = False
                self.mark_error(str(exc))
                return False

            try:
                self._client = await asyncio.to_thread(
                    ReqClient,
                    host=self.config.host,
                    port=self.config.port,
                    password=self.config.password or None,
                    timeout=self.config.connection_timeout_seconds,
                )
                self.connected = True
                self.mark_success()
                return True
            except Exception as exc:
                self.connected = False
                self.mark_error(str(exc))
                return False

    async def call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        if not self._client:
            await self.connect()
        if not self._client:
            raise RuntimeError(self.last_error or "OBS is not connected")
        method = getattr(self._client, method_name)
        try:
            result = await asyncio.to_thread(method, *args, **kwargs)
            self.connected = True
            self.mark_success()
            return result
        except Exception as exc:
            self.connected = False
            self.mark_error(str(exc))
            raise
