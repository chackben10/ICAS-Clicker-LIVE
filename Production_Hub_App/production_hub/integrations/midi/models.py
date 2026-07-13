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
    actions: list[ActionDefinition] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MidiMapping":
        mapping = super().from_dict(data)
        legacy_action = data.get("action") if isinstance(data, dict) else None
        if legacy_action and not mapping.actions:
            mapping.actions = [ActionDefinition.from_dict(legacy_action)]
        return mapping

    @property
    def action(self) -> ActionDefinition:
        return self.actions[0]

    @classmethod
    def audio_pad(cls, number: int, playlist: str, track: str, channel: int = 0) -> "MidiMapping":
        return cls(
            event_type="note_on",
            channel=channel,
            number=number,
            actions=[ActionDefinition("propresenter.audio_trigger", {"playlist": playlist, "track": track})],
            metadata={"label": f"{track} / {playlist}"},
        )
