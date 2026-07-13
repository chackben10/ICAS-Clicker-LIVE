from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from production_hub.core.health.status_models import STATUS_CONNECTED, STATUS_DISABLED, STATUS_RECONNECTING
from production_hub.ui.pages.common import scroll_page, title


class OverviewPage(QWidget):
    def __init__(self, context) -> None:
        super().__init__()
        self.context = context
        self.app_fields: dict[str, QLabel] = {}
        self.api_fields: dict[str, QLabel] = {}
        self.integration_rows: dict[str, tuple[QLabel, QLabel]] = {}
        self.build()
        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        scroll, _body, layout = scroll_page()
        root.addWidget(scroll)
        layout.addWidget(title("Overview", "Current Production Hub health at a glance."))
        layout.addWidget(self.application_card())
        layout.addWidget(self.remote_api_card())
        layout.addWidget(self.integrations_card())
        layout.addWidget(self.actions_row())
        layout.addStretch()

    def application_card(self) -> QFrame:
        fields = [
            "Running state",
            "Active profile",
            "Uptime",
            "Enabled endpoints",
            "Active automations",
            "Remote pages",
            "Recent API errors",
        ]
        return self.metric_card("Application Overview", fields, self.app_fields)

    def remote_api_card(self) -> QFrame:
        fields = [
            "Status",
            "Base URL",
            "Bind host",
            "Port",
            "LAN access",
            "Token required",
            "Public read-only",
            "CORS origins",
            "Recent requests",
            "Last request",
            "Average duration",
        ]
        return self.metric_card("Remote API Server", fields, self.api_fields)

    def integrations_card(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("Card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)
        heading = QLabel("Integration Health")
        heading.setObjectName("CardTitle")
        layout.addWidget(heading)
        hint = QLabel("Detailed targets, timestamps, errors, and configuration live in the Integrations tab.")
        hint.setObjectName("HelpText")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)
        for row, name in enumerate(["ProPresenter", "OBS", "Panasonic AWP", "VISCA Bridge", "Scoreboard Service", "MIDI"]):
            dot = QLabel("●")
            dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label = QLabel(name)
            label.setObjectName("MetaLabel")
            status = QLabel("Unknown")
            grid.addWidget(dot, row, 0)
            grid.addWidget(label, row, 1)
            grid.addWidget(status, row, 2)
            grid.setColumnStretch(2, 1)
            self.integration_rows[name] = (dot, status)
        layout.addLayout(grid)
        return frame

    def actions_row(self) -> QWidget:
        wrapper = QWidget()
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(0, 0, 0, 0)
        pause = QPushButton("Pause All Automations")
        pause.clicked.connect(self.pause_automations)
        resume = QPushButton("Resume Automations")
        resume.clicked.connect(self.resume_automations)
        integrations = QPushButton("Open Integrations")
        integrations.clicked.connect(
            lambda: integrations.window().show_page_by_name("Integrations")
            if hasattr(integrations.window(), "show_page_by_name")
            else None
        )
        settings = QPushButton("Open Settings")
        settings.clicked.connect(
            lambda: settings.window().show_page_by_name("Settings")
            if hasattr(settings.window(), "show_page_by_name")
            else None
        )
        for button in (pause, resume, integrations, settings):
            row.addWidget(button)
        row.addStretch()
        return wrapper

    def metric_card(self, heading_text: str, fields: list[str], target: dict[str, QLabel]) -> QFrame:
        frame = QFrame()
        frame.setObjectName("Card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)
        heading = QLabel(heading_text)
        heading.setObjectName("CardTitle")
        layout.addWidget(heading)
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(7)
        for row, field in enumerate(fields):
            left = QLabel(field)
            left.setObjectName("MetaLabel")
            right = QLabel("-")
            right.setWordWrap(True)
            grid.addWidget(left, row, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(right, row, 1)
            target[field] = right
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)
        return frame

    def refresh(self) -> None:
        endpoints = list(self.context.endpoint_registry.all())
        automations = list(self.context.automation_engine.definitions.values())
        health = self.context.health_monitor.snapshot(endpoints, automations)
        runtime = self.context.runtime_state_repo.load()
        requests = runtime.endpoint_request_history[-100:]
        recent_errors = [item for item in requests if int(item.status_code) >= 400 or item.error]
        remote_pages = len(getattr(self.context.config, "remote_pages", []))

        self.set_field(self.app_fields, "Running state", "Running" if health.app_running else "Stopped")
        self.set_field(self.app_fields, "Active profile", health.active_profile)
        self.set_field(self.app_fields, "Uptime", format_duration(health.uptime_seconds))
        self.set_field(self.app_fields, "Enabled endpoints", str(health.enabled_endpoints))
        self.set_field(self.app_fields, "Active automations", str(health.active_automations))
        self.set_field(self.app_fields, "Remote pages", str(remote_pages))
        self.set_field(self.app_fields, "Recent API errors", str(len(recent_errors)))

        api_health = next((item for item in health.integrations if item.name == "Remote API Server"), None)
        cfg = self.context.config.api
        self.set_field(self.api_fields, "Status", api_health.status if api_health else health.api_status)
        self.set_field(self.api_fields, "Base URL", cfg.base_url)
        self.set_field(self.api_fields, "Bind host", cfg.bind_host)
        self.set_field(self.api_fields, "Port", str(cfg.port))
        self.set_field(self.api_fields, "LAN access", "Enabled" if cfg.lan_access_enabled else "Disabled")
        self.set_field(self.api_fields, "Token required", "Yes" if cfg.require_token_for_privileged else "No")
        self.set_field(self.api_fields, "Public read-only", "Yes" if cfg.read_only_public else "No")
        self.set_field(self.api_fields, "CORS origins", str(len(cfg.cors_allow_origins or [])))
        self.set_field(self.api_fields, "Recent requests", str(len(requests)))
        self.set_field(self.api_fields, "Last request", last_request_summary(requests))
        self.set_field(self.api_fields, "Average duration", average_duration(requests))

        health_by_name = {item.name: item for item in health.integrations}
        for name, (dot, status_label) in self.integration_rows.items():
            item = health_by_name.get(name)
            status = item.status if item else "Unknown"
            dot.setStyleSheet(f"color: {status_color(status)}; font-size: 18px;")
            status_label.setText(status)

    def set_field(self, fields: dict[str, QLabel], name: str, value: str) -> None:
        fields[name].setText(value or "-")

    def pause_automations(self) -> None:
        self.context.automation_engine.pause_all()
        self.refresh()

    def resume_automations(self) -> None:
        self.context.automation_engine.resume_all()
        self.refresh()


def status_color(status: str) -> str:
    if status == STATUS_CONNECTED:
        return "#22c55e"
    if status == STATUS_DISABLED:
        return "#7d8793"
    if status == STATUS_RECONNECTING:
        return "#f59e0b"
    return "#ef4444"


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def last_request_summary(requests: list) -> str:
    if not requests:
        return "No requests recorded"
    item = requests[-1]
    return f"{readable_timestamp(item.timestamp)}  {item.method} {item.route}  {item.status_code}"


def average_duration(requests: list) -> str:
    if not requests:
        return "-"
    average = sum(float(item.duration_ms or 0) for item in requests) / len(requests)
    return f"{average:.1f} ms"


def readable_timestamp(value: str) -> str:
    if not value:
        return "-"
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%b %-d, %-I:%M:%S %p")
    except Exception:
        return text


def build_page(context) -> QWidget:
    return OverviewPage(context)
