from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from production_hub.core.config.models import JsonModel


@dataclass
class ObsSceneItem(JsonModel):
    scene_item_id: int
    source_name: str
    source_uuid: str = ""
    source_type: str = ""
    input_kind: str = ""
    enabled: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

