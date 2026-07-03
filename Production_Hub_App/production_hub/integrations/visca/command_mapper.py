from __future__ import annotations

from production_hub.integrations.panasonic_awp.models import PanasonicCommand
from production_hub.integrations.panasonic_awp.service import clamp_aw_value
from production_hub.integrations.visca.parser import ViscaPacket


def _scaled_offset(value: int, denominator: float) -> int:
    return int((value / denominator) * 49) if value > 0 else 0


def map_packet_to_panasonic(
    packet: ViscaPacket,
    zoom_speed: int = 20,
    focus_speed: int = 20,
) -> list[PanasonicCommand]:
    visca = packet.hex
    commands: list[PanasonicCommand] = []

    if visca.startswith("8101060602FF"):
        return [PanasonicCommand("DUS:1", "aw_cam", "TENVEO", "Menu on")]
    if visca.startswith("81010604FF"):
        return [PanasonicCommand("#R00", "aw_ptz", "TENVEO", "Home preset")]
    if visca.startswith("8101043802FF"):
        return [PanasonicCommand("#D11", "aw_ptz", "TENVEO", "Auto focus")]
    if visca.startswith("8101043803FF"):
        return [PanasonicCommand("#D10", "aw_ptz", "TENVEO", "Manual focus")]
    if visca.startswith("81010435"):
        return [PanasonicCommand("#AWA", "aw_ptz", "TENVEO", "Auto white balance")]

    if visca.startswith("81010601") and len(visca) >= 18:
        pan_speed = int(visca[8:10], 16)
        tilt_speed = int(visca[10:12], 16)
        pan_direction = visca[12:14]
        tilt_direction = visca[14:16]
        pan = 50
        tilt = 50
        pan_offset = _scaled_offset(pan_speed, 24.0)
        tilt_offset = _scaled_offset(tilt_speed, 20.0)
        if pan_direction == "01":
            pan = 50 - pan_offset
        elif pan_direction == "02":
            pan = 50 + pan_offset
        if tilt_direction == "01":
            tilt = 50 + tilt_offset
        elif tilt_direction == "02":
            tilt = 50 - tilt_offset
        commands.append(PanasonicCommand(f"#PTS{clamp_aw_value(pan):02d}{clamp_aw_value(tilt):02d}", description="Pan/tilt"))

    elif visca.startswith("81010407") and len(visca) >= 12:
        command_byte = visca[8:10]
        if command_byte == "00":
            commands.append(PanasonicCommand("#Z50", description="Stop zoom"))
        elif command_byte == "02":
            commands.append(PanasonicCommand(f"#Z{clamp_aw_value(50 + zoom_speed):02d}", description="Zoom tele"))
        elif command_byte == "03":
            commands.append(PanasonicCommand(f"#Z{clamp_aw_value(50 - zoom_speed):02d}", description="Zoom wide"))
        elif command_byte.startswith("2"):
            speed = int(command_byte[1], 16)
            commands.append(PanasonicCommand(f"#Z{clamp_aw_value(50 + int((speed / 7.0) * 49) if speed > 0 else 75):02d}"))
        elif command_byte.startswith("3"):
            speed = int(command_byte[1], 16)
            commands.append(PanasonicCommand(f"#Z{clamp_aw_value(50 - int((speed / 7.0) * 49) if speed > 0 else 25):02d}"))

    elif visca.startswith("81010408") and len(visca) >= 12:
        command_byte = visca[8:10]
        if command_byte == "00":
            commands.append(PanasonicCommand("#F50", description="Stop focus"))
        elif command_byte == "02":
            commands.append(PanasonicCommand(f"#F{clamp_aw_value(50 + focus_speed):02d}", description="Focus far"))
        elif command_byte == "03":
            commands.append(PanasonicCommand(f"#F{clamp_aw_value(50 - focus_speed):02d}", description="Focus near"))

    elif visca.startswith("8101043F02") and len(visca) >= 14:
        preset = int(visca[10:12], 16)
        commands.append(PanasonicCommand(f"#R{preset:02d}", description="Recall preset"))
    elif visca.startswith("8101043F01") and len(visca) >= 14:
        preset = int(visca[10:12], 16)
        commands.append(PanasonicCommand(f"#M{preset:02d}", description="Save preset"))

    return commands

