from __future__ import annotations

from PySide6.QtWidgets import QWidget

from production_hub.ui.pages.common import card, scroll_page, title, two_column_grid


def build_page(context) -> QWidget:
    scroll, _body, layout = scroll_page()
    layout.addWidget(title("Extensions", "Future modules that will use the shared integration and endpoint engine."))
    names = ["MIDI", "Stream Deck", "NDI Monitoring", "DMX Lighting", "YouTube Live", "Twitch", "REST/Webhooks", "Serial Devices", "GPIO/Relay"]
    layout.addWidget(two_column_grid([card(name, [("Status", "Planned" if name != "MIDI" else "Not Configured")]) for name in names]))
    layout.addStretch()
    return scroll

