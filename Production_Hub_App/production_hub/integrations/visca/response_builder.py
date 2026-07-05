from __future__ import annotations

ACK = bytes.fromhex("9041FF")
COMPLETION = bytes.fromhex("9051FF")
VISCA_OVER_IP_RESPONSE_HEADER = bytes.fromhex("01110003")


def build_ack_completion(data: bytes, ack: bool = True, completion: bool = True) -> list[bytes]:
    raw_hex = data.hex().upper()
    responses: list[bytes] = []
    if raw_hex.startswith("0100") and len(data) >= 8:
        sequence = data[4:8]
        command_count = max(1, data[8:].count(b"\xff"))
        for _ in range(command_count):
            if ack:
                responses.append(VISCA_OVER_IP_RESPONSE_HEADER + sequence + ACK)
            if completion:
                responses.append(VISCA_OVER_IP_RESPONSE_HEADER + sequence + COMPLETION)
    elif raw_hex.startswith("8101") or raw_hex.startswith("8109"):
        command_count = max(1, data.count(b"\xff"))
        for _ in range(command_count):
            if ack:
                responses.append(ACK)
            if completion:
                responses.append(COMPLETION)
    return responses
