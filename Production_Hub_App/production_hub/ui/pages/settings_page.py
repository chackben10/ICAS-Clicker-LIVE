from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from production_hub.ui.pages.common import card, scroll_page, title


class SettingsPage(QWidget):
    def __init__(self, context) -> None:
        super().__init__()
        self.context = context
        self.status = QLabel("Ready")
        self.status.setObjectName("StatusText")
        self.keep_running = QCheckBox("Keep Production Hub running when the window is closed")
        self.menu_bar_icon = QCheckBox("Show menu-bar status icon")
        self.launch_at_login = QCheckBox("Launch Production Hub at login")
        self.build()

    def build(self) -> None:
        scroll, _body, layout = scroll_page()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        layout.addWidget(title("Settings", "Default Profile, local API settings, and application preferences."))
        layout.addWidget(
            card(
                "API",
                [
                    ("Bind host", self.context.config.api.bind_host),
                    ("Port", str(self.context.config.api.port)),
                    ("LAN access", "Enabled" if self.context.config.api.lan_access_enabled else "Disabled"),
                    ("Token required", "Yes" if self.context.config.api.require_token_for_privileged else "No"),
                    ("CORS allow-list", ", ".join(self.context.config.api.cors_allow_origins)),
                ],
                ["Save", "Generate Token"],
            )
        )
        layout.addWidget(
            card(
                "Profile",
                [
                    ("Active profile", self.context.config.active_profile),
                    ("Schema version", str(self.context.config.schema_version)),
                    ("Last saved", self.context.config.last_saved_at),
                ],
                ["Validate", "Export"],
            )
        )
        layout.addWidget(self.preferences_group())
        layout.addWidget(self.status)
        layout.addStretch()

    def preferences_group(self) -> QGroupBox:
        cfg = self.context.config.ui
        group = QGroupBox("Application Preferences")
        layout = QVBoxLayout(group)
        self.keep_running.setChecked(cfg.keep_running_after_window_close)
        self.menu_bar_icon.setChecked(cfg.show_menu_bar_icon)
        self.launch_at_login.setChecked(cfg.launch_at_login)
        layout.addWidget(self.keep_running)
        layout.addWidget(self.menu_bar_icon)
        layout.addWidget(self.launch_at_login)
        note = QLabel(
            "Launch at login is saved as a preference here. Installing the macOS LaunchAgent is a packaging step "
            "so the app can use the final bundle identifier and executable path."
        )
        note.setWordWrap(True)
        note.setObjectName("HelpText")
        layout.addWidget(note)
        save = QPushButton("Save Preferences")
        save.clicked.connect(self.save_preferences)
        row = QHBoxLayout()
        row.addWidget(save)
        row.addStretch()
        layout.addLayout(row)
        return group

    def save_preferences(self) -> None:
        cfg = self.context.config.ui
        cfg.keep_running_after_window_close = self.keep_running.isChecked()
        cfg.show_menu_bar_icon = self.menu_bar_icon.isChecked()
        cfg.launch_at_login = self.launch_at_login.isChecked()
        self.context.config_repository.save_app_config(self.context.config)
        self.status.setText("Preferences saved. Restart the app to apply menu-bar icon changes.")


def build_page(context) -> QWidget:
    return SettingsPage(context)
