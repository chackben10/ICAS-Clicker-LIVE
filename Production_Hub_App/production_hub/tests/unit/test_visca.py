from __future__ import annotations

import unittest

from production_hub.integrations.visca.command_mapper import map_packet_to_panasonic
from production_hub.integrations.visca.parser import parse_visca_packets
from production_hub.integrations.visca.response_builder import build_ack_completion


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


if __name__ == "__main__":
    unittest.main()
