from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from production_hub.core.config.models import JsonModel


@dataclass
class MidiMapping(JsonModel):
    event_type: str
    channel: int
    number: int
    endpoint_key: str
    metadata: dict[str, Any] = field(default_factory=dict)

