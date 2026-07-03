from __future__ import annotations

from PySide6.QtWidgets import QWidget

from production_hub.ui.pages.common import card, scroll_page, title, two_column_grid


def build_page(context) -> QWidget:
    scroll, _body, layout = scroll_page()
    layout.addWidget(title("Integrations", "Connected systems and module diagnostics."))
    cfg = context.config.integrations
    cards = [
        card(
            "ProPresenter",
            [
                ("Enabled", str(cfg.propresenter.enabled)),
                ("Host", cfg.propresenter.host),
                ("Port", str(cfg.propresenter.port)),
                ("API base", cfg.propresenter.base_url),
                ("Presentations", str(len(cfg.propresenter.presentations))),
                ("Macros", str(len(cfg.propresenter.macros))),
            ],
            ["Configure", "Test", "Reconnect"],
        ),
        card(
            "OBS Studio",
            [
                ("Enabled", str(cfg.obs.enabled)),
                ("Host", cfg.obs.host),
                ("Port", str(cfg.obs.port)),
                ("Main layout", cfg.obs.main_layout_scene),
                ("Look rules", str(len(cfg.obs.look_rules))),
            ],
            ["Configure", "Discover", "Reconnect"],
        ),
        card(
            "Panasonic AWP Camera",
            [
                ("Enabled", str(cfg.panasonic.enabled)),
                ("Camera IP", cfg.panasonic.camera_ip),
                ("User", cfg.panasonic.username),
                ("Presets", "00-100"),
            ],
            ["Configure", "Test", "Presets"],
        ),
        card(
            "VISCA Bridge",
            [
                ("Enabled", str(cfg.visca.enabled)),
                ("Listen IP", cfg.visca.listen_ip),
                ("UDP port", str(cfg.visca.udp_port)),
                ("Shared port", str(cfg.visca.reuse_port)),
                ("Tenveo", str(cfg.visca.tenveo_compatibility_enabled)),
            ],
            ["Configure", "Test UDP", "Restart"],
        ),
        card(
            "Scoreboard Service",
            [
                ("State file", "scoreboard.json"),
                ("Compatibility routes", "GET /score, POST /score"),
                ("Revision", str(context.scoreboard.get_state().revision)),
            ],
            ["Read", "Write Test"],
        ),
        card(
            "MIDI",
            [("Status", cfg.midi.status_label), ("Mappings", str(len(cfg.midi.mappings)))],
            ["Configure"],
        ),
    ]
    layout.addWidget(two_column_grid(cards))
    layout.addStretch()
    return scroll

