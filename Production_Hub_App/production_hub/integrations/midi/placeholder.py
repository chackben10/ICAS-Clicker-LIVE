from __future__ import annotations

from production_hub.core.health.status_models import IntegrationHealth, STATUS_DISABLED


class MidiPlaceholder:
    name = "MIDI"

    def health(self) -> IntegrationHealth:
        return IntegrationHealth(self.name, STATUS_DISABLED, target="Not Configured")

