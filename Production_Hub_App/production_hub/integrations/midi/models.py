from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from production_hub.core.endpoints.models import ActionDefinition
from production_hub.core.config.models import JsonModel


@dataclass
class MidiMapping(JsonModel):
    event_type: str
    channel: int
    number: int
    action: ActionDefinition
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def audio_pad(cls, number: int, playlist: str, track: str, channel: int = 0) -> "MidiMapping":
        return cls(
            event_type="note_on",
            channel=channel,
            number=number,
            action=ActionDefinition("propresenter.audio_trigger", {"playlist": playlist, "track": track}),
            metadata={"label": f"{track} / {playlist}"},
        )
