from __future__ import annotations

from PySide6.QtWidgets import QWidget

from production_hub.ui.pages.common import card, scroll_page, title


def storage_paths_card(context) -> QWidget:
    return card(
        "Data & Storage",
        [
            ("Root", str(context.paths.root)),
            ("Config", str(context.paths.config_dir)),
            ("State", str(context.paths.state_dir)),
            ("Logs", str(context.paths.logs_dir)),
            ("Automatic backups", str(context.paths.automatic_backups_dir)),
            ("Manual backups", str(context.paths.manual_backups_dir)),
        ],
        ["Export", "Import", "Manual Backup"],
    )


def build_page(context) -> QWidget:
    scroll, _body, layout = scroll_page()
    layout.addWidget(title("Data & Storage", "Configuration, state, logs, and backups."))
    layout.addWidget(storage_paths_card(context))
    layout.addStretch()
    return scroll
