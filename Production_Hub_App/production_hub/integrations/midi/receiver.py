from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from production_hub.core.config.models import MidiConfig
from production_hub.core.endpoints.models import ActionDefinition
from production_hub.core.health.status_models import IntegrationHealth, STATUS_CONNECTED, STATUS_DISABLED, STATUS_OFFLINE
from production_hub.integrations.midi.mapping_service import MidiMappingService
from production_hub.integrations.midi.models import MidiMapping


MidiActionHandler = Callable[[ActionDefinition, dict[str, Any]], None]


class MidiReceiver:
    name = "MIDI"

    def __init__(self, config: MidiConfig, mappings: list[MidiMapping], handler: MidiActionHandler) -> None:
        self.config = config
        self.mapping_service = MidiMappingService(mappings)
        self.handler = handler
        self._mido = None
        self._port = None
        self._input_name = ""
        self._last_error = ""
        self._last_message_at = ""

    def set_handler(self, handler: MidiActionHandler) -> None:
        self.handler = handler

    @property
    def input_name(self) -> str:
        return self._input_name or self.config.input_name or "Auto"

    def start(self) -> bool:
        if not self.config.enabled:
            return False
        try:
            import mido
        except Exception as exc:
            self._last_error = f"mido unavailable: {exc}"
            return False

        self._mido = mido
        try:
            names = list(mido.get_input_names())
            self.config.input_devices = names
            input_name = self._select_input(names)
            if not input_name:
                self._last_error = "No MIDI input devices found"
                return False
            self._port = mido.open_input(input_name, callback=self._handle_message)
            self._input_name = input_name
            self._last_error = ""
            return True
        except Exception as exc:
            self._last_error = str(exc)
            return False

    def stop(self) -> None:
        if self._port is not None:
            self._port.close()
        self._port = None

    def health(self) -> IntegrationHealth:
        if not self.config.enabled:
            return IntegrationHealth(self.name, STATUS_DISABLED, target="Disabled")
        if self._port is not None:
            return IntegrationHealth(
                self.name,
                STATUS_CONNECTED,
                target=self.input_name,
                last_success_at=self._last_message_at,
                metadata={"mappings": len(self.mapping_service.mappings)},
            )
        return IntegrationHealth(self.name, STATUS_OFFLINE, target=self.input_name, last_error=self._last_error)

    def _select_input(self, names: list[str]) -> str:
        wanted = self.config.input_name.strip()
        if wanted and wanted in names:
            return wanted
        if wanted:
            partial = next((name for name in names if wanted.lower() in name.lower()), "")
            if partial:
                return partial
        return names[0] if self.config.auto_open_first_input and names else ""

    def _handle_message(self, message) -> None:
        message_type = str(getattr(message, "type", ""))
        if message_type != "note_on":
            return
        velocity = int(getattr(message, "velocity", 0) or 0)
        if velocity <= 0:
            return
        channel = int(getattr(message, "channel", -1))
        note = int(getattr(message, "note", -1))
        action = self.mapping_service.action_for("note_on", channel, note)
        if not action:
            return
        self._last_message_at = datetime.now(UTC).isoformat()
        self.handler(
            action,
            {
                "midi_event": "note_on",
                "midi_channel": channel,
                "midi_note": note,
                "midi_velocity": velocity,
                "midi_input": self.input_name,
            },
        )
