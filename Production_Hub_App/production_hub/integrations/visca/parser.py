from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ViscaPacket:
    payload: bytes
    raw: bytes
    sequence: bytes | None = None

    @property
    def hex(self) -> str:
        return self.payload.hex().upper()


def parse_visca_packets(data: bytes) -> list[ViscaPacket]:
    if not data:
        return []
    sequence: bytes | None = None
    search_data = data
    if data.hex().upper().startswith("0100") and len(data) >= 8:
        sequence = data[4:8]
        start = data.find(b"\x81", 8)
        search_data = data[start:] if start >= 0 else b""

    packets: list[ViscaPacket] = []
    index = 0
    while index < len(search_data):
        start = search_data.find(b"\x81", index)
        if start < 0:
            break
        end = search_data.find(b"\xff", start)
        if end < 0:
            break
        payload = search_data[start : end + 1]
        packets.append(ViscaPacket(payload=payload, raw=data, sequence=sequence))
        index = end + 1
    return packets

