from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from production_hub.core.config.models import AppConfig
from production_hub.integrations.panasonic_awp.models import PanasonicCommand
from production_hub.integrations.visca.command_mapper import map_packet_to_panasonic
from production_hub.integrations.visca.parser import parse_visca_packets
from production_hub.integrations.visca.response_builder import build_ack_completion
from production_hub.integrations.visca.tracking_toggle import (
    ViscaAutofocusTrackingToggle,
)


class _Repository:
    def __init__(self) -> None:
        self.saved = 0

    def save_app_config(self, _config) -> None:
        self.saved += 1


class _Video:
    def __init__(self) -> None:
        self.activity: list[tuple[bool, str]] = []
        self.configs = []

    def set_tracking_activity(self, active: bool, *, owner: str) -> None:
        self.activity.append((active, owner))

    def reconfigure_tracking(self, config) -> None:
        self.configs.append(config)


class _Automation:
    def __init__(self) -> None:
        self.armed = False
        self.disarmed: list[str] = []
        self.configs = []

    def arm(self) -> tuple[bool, str]:
        self.armed = True
        return True, "PTZ automation armed"

    def disarm(self, reason: str) -> None:
        self.armed = False
        self.disarmed.append(reason)

    def reconfigure(self, config) -> None:
        self.configs.append(config)


class _Logger:
    def info(self, *_args, **_kwargs) -> None:
        return None

    def warning(self, *_args, **_kwargs) -> None:
        return None


class ViscaTests(unittest.TestCase):
    def test_raw_packet_response_and_pan_tilt_mapping(self) -> None:
        data = bytes.fromhex("8101060118140201FF")
        responses = build_ack_completion(data)
        self.assertEqual([item.hex().upper() for item in responses], ["9041FF", "9051FF"])
        packets = parse_visca_packets(data)
        self.assertEqual(len(packets), 1)
        commands = map_packet_to_panasonic(packets[0])
        self.assertEqual(commands[0].command, "#PTS9999")

    def test_visca_over_ip_sequence_response(self) -> None:
        data = bytes.fromhex("0100000900000001810104072FFF")
        responses = build_ack_completion(data)
        self.assertEqual(responses[0].hex().upper(), "01110003000000019041FF")
        packets = parse_visca_packets(data)
        commands = map_packet_to_panasonic(packets[0])
        self.assertEqual(commands[0].command, "#Z99")

    def test_coalesced_raw_packets_get_per_command_responses(self) -> None:
        data = bytes.fromhex("810104072FFF8101060118140201FF")
        self.assertEqual(len(build_ack_completion(data)), 4)
        packets = parse_visca_packets(data)
        self.assertEqual(len(packets), 2)

    def test_preset_and_tenveo_mapping(self) -> None:
        recall = parse_visca_packets(bytes.fromhex("8101043F020AFF"))[0]
        self.assertEqual(map_packet_to_panasonic(recall)[0].command, "#R10")
        menu = parse_visca_packets(bytes.fromhex("8101060602FF"))[0]
        mapped = map_packet_to_panasonic(menu)[0]
        self.assertEqual(mapped.command, "DUS:1")
        self.assertEqual(mapped.endpoint, "aw_cam")
        autofocus = parse_visca_packets(bytes.fromhex("8101043802FF"))[0]
        mapped = map_packet_to_panasonic(autofocus)[0]
        self.assertEqual(mapped.command, "#D11")
        self.assertEqual(mapped.description, "Auto focus")

    def test_tenveo_autofocus_toggles_tracking_and_consumes_repeats(self) -> None:
        async def exercise() -> None:
            config = AppConfig()
            video = _Video()
            automation = _Automation()
            repository = _Repository()
            timestamps = iter((10.0, 10.1, 11.0))
            context = SimpleNamespace(
                config=config,
                video=video,
                ptz_automation=automation,
                config_repository=repository,
                logger=_Logger(),
            )
            toggle = ViscaAutofocusTrackingToggle(
                context,
                clock=lambda: next(timestamps),
            )
            command = PanasonicCommand(
                "#D11",
                "aw_ptz",
                "TENVEO",
                "Auto focus",
            )

            self.assertTrue(await toggle.consume(command))
            await asyncio.sleep(0)
            self.assertTrue(automation.armed)
            self.assertEqual(
                "subject",
                config.integrations.camera_tracking.automation.mode,
            )

            # A repeated controller packet from the same press is consumed but
            # does not immediately reverse the requested state.
            self.assertTrue(await toggle.consume(command))
            self.assertTrue(automation.armed)

            self.assertTrue(await toggle.consume(command))
            self.assertFalse(automation.armed)
            self.assertEqual(
                "off",
                config.integrations.camera_tracking.automation.mode,
            )
            self.assertEqual(2, repository.saved)
            toggle.shutdown()

        asyncio.run(exercise())

    def test_non_autofocus_visca_command_is_not_consumed(self) -> None:
        async def exercise() -> None:
            context = SimpleNamespace(
                config=AppConfig(),
                video=_Video(),
                ptz_automation=_Automation(),
                config_repository=_Repository(),
                logger=_Logger(),
            )
            toggle = ViscaAutofocusTrackingToggle(context)
            self.assertFalse(
                await toggle.consume(
                    PanasonicCommand("#Z75", description="Zoom tele")
                )
            )

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
