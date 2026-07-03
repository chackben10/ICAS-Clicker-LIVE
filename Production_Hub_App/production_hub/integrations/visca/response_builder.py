from __future__ import annotations

ACK = bytes.fromhex("9041FF")
COMPLETION = bytes.fromhex("9051FF")


def build_ack_completion(data: bytes, ack: bool = True, completion: bool = True) -> list[bytes]:
    raw_hex = data.hex().upper()
    responses: list[bytes] = []
    if raw_hex.startswith("0100") and len(data) >= 8:
        sequence = data[4:8]
        if ack:
            responses.append(bytes.fromhex("01110003") + sequence + ACK)
        if completion:
            responses.append(bytes.fromhex("01110003") + sequence + COMPLETION)
    elif raw_hex.startswith("8101") or raw_hex.startswith("8109"):
        if ack:
            responses.append(ACK)
        if completion:
            responses.append(COMPLETION)
    return responses

