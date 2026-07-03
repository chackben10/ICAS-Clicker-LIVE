from __future__ import annotations

from production_hub.integrations.midi.models import MidiMapping


class MidiMappingService:
    def __init__(self, mappings: list[MidiMapping] | None = None) -> None:
        self.mappings = mappings or []

    def endpoint_for(self, event_type: str, channel: int, number: int) -> str | None:
        for mapping in self.mappings:
            if mapping.event_type == event_type and mapping.channel == channel and mapping.number == number:
                return mapping.endpoint_key
        return None

