from __future__ import annotations

from production_hub.integrations.midi.models import MidiMapping


class MidiMappingService:
    def __init__(self, mappings: list[MidiMapping] | None = None) -> None:
        self.mappings = mappings or []

    def mapping_for(self, event_type: str, channel: int, number: int) -> MidiMapping | None:
        for mapping in self.mappings:
            if mapping.event_type == event_type and mapping.number == number and mapping.channel in {-1, channel}:
                return mapping
        return None

    def action_for(self, event_type: str, channel: int, number: int):
        mapping = self.mapping_for(event_type, channel, number)
        return mapping.action if mapping else None

    def actions_for(self, event_type: str, channel: int, number: int):
        mapping = self.mapping_for(event_type, channel, number)
        return list(mapping.actions) if mapping else []
