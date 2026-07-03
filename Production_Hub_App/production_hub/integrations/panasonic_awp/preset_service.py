from __future__ import annotations

from production_hub.core.config.models import PanasonicConfig


class PanasonicPresetService:
    def __init__(self, config: PanasonicConfig) -> None:
        self.config = config

    def list_presets(self) -> list[dict[str, object]]:
        presets = [{"number": 0, "name": self.config.preset_names.get("0", "Home"), "readonly": True}]
        for number in range(1, 101):
            presets.append({"number": number, "name": self.config.preset_names.get(str(number), ""), "readonly": False})
        return presets

    def rename(self, number: int, name: str) -> None:
        if number == 0:
            raise ValueError("Preset 00 is Home and cannot be renamed")
        if number < 0 or number > 100:
            raise ValueError("Preset must be between 00 and 100")
        self.config.preset_names[str(number)] = str(name).strip()

