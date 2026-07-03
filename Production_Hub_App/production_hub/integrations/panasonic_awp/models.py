from __future__ import annotations

from dataclasses import dataclass

from production_hub.core.config.models import JsonModel


@dataclass
class PanasonicCommand(JsonModel):
    command: str
    endpoint: str = "aw_ptz"
    source: str = "system"
    description: str = ""

