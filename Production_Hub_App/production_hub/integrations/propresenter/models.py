from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from production_hub.core.config.models import JsonModel


@dataclass
class PresentationSummary(JsonModel):
    uuid: str
    name: str
    slide_count: int = 0
    raw: dict[str, Any] | None = None


@dataclass
class AudioTrack(JsonModel):
    playlist: str
    name: str

