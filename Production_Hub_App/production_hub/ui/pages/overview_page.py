from __future__ import annotations

from PySide6.QtWidgets import QPushButton, QWidget

from production_hub.ui.pages.common import card, responsive_grid, scroll_page, title, two_column_grid


def build_page(context) -> QWidget:
    scroll, _body, layout = scroll_page()
    layout.addWidget(title("Overview", context.config.subtitle))

    health = context.health_monitor.snapshot(
        context.endpoint_registry.all(),
        context.automation_engine.definitions.values(),
    )
    layout.addWidget(
        card(
            "Application",
            [
                ("Running state", "Running" if health.app_running else "Stopped"),
                ("Active configuration", health.active_profile),
                ("API server", health.api_status),
                ("API bind", health.api_target),
                ("Uptime", f"{health.uptime_seconds:.0f} sec"),
                ("Enabled endpoints", str(health.enabled_endpoints)),
                ("Active automations", str(health.active_automations)),
                ("Recent errors", str(health.recent_errors)),
            ],
        )
    )

    integration_cards = [
        card(
            item.name,
            [
                ("Status", item.status),
                ("Target", item.target),
                ("Last success", item.last_success_at),
                ("Last error", item.last_error),
            ],
            ["Test", "Reconnect"],
        )
        for item in health.integrations
    ]
    layout.addWidget(two_column_grid(integration_cards))

    pause = QPushButton("Pause All Automations")
    restart = QPushButton("Restart Background Services")
    diagnostics = QPushButton("Open Diagnostics")
    layout.addWidget(responsive_grid([pause, restart, diagnostics], min_column_width=190, max_columns=3))
    layout.addStretch()
    return scroll
