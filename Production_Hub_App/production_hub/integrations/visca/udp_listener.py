from __future__ import annotations

import asyncio
import socket
from collections.abc import Awaitable, Callable

from production_hub.core.config.models import ViscaConfig
from production_hub.integrations.panasonic_awp.models import PanasonicCommand
from production_hub.integrations.visca.command_mapper import map_packet_to_panasonic
from production_hub.integrations.visca.parser import parse_visca_packets
from production_hub.integrations.visca.response_builder import build_ack_completion

CommandHandler = Callable[[PanasonicCommand], Awaitable[None]]


class ViscaDatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self, config: ViscaConfig, handler: CommandHandler) -> None:
        self.config = config
        self.handler = handler
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if self.transport:
            for response in build_ack_completion(data, self.config.ack_response_enabled, self.config.completion_response_enabled):
                self.transport.sendto(response, addr)
        for packet in parse_visca_packets(data):
            for command in map_packet_to_panasonic(packet):
                asyncio.create_task(self.handler(command))


class ViscaUdpListener:
    def __init__(self, config: ViscaConfig, handler: CommandHandler) -> None:
        self.config = config
        self.handler = handler
        self.transport: asyncio.DatagramTransport | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if self.config.reuse_address:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if self.config.reuse_port and hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        try:
            sock.bind((self.config.listen_ip, self.config.udp_port))
        except OSError as exc:
            sock.close()
            raise RuntimeError(
                f"VISCA UDP port {self.config.udp_port} is unavailable. "
                "Choose another port or explicitly enable shared-port mode."
            ) from exc
        self.transport, _ = await loop.create_datagram_endpoint(
            lambda: ViscaDatagramProtocol(self.config, self.handler),
            sock=sock,
        )

    async def stop(self) -> None:
        if self.transport:
            self.transport.close()
            self.transport = None

