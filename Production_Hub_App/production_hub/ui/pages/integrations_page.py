from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from production_hub.core.health.status_models import STATUS_CONNECTED, STATUS_DISABLED
from production_hub.ui.pages.common import configure_table, responsive_grid, scroll_page, title


class IntegrationsPage(QWidget):
    def __init__(self, context) -> None:
        super().__init__()
        self.context = context
        self.cards: dict[str, IntegrationCard] = {}
        self.request_table = QTableWidget(0, 6)
        self.build()
        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self.refresh_live_state)
        self.timer.start()
        self.refresh_live_state()

    def build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        scroll, _body, layout = scroll_page()
        root.addWidget(scroll)
        layout.addWidget(title("Integrations", "Live status and connection settings for connected systems."))

        specs = self.integration_specs()
        cards = []
        for spec in specs:
            card = IntegrationCard(self.context, spec, self)
            self.cards[spec["health_name"]] = card
            cards.append(card)
        layout.addWidget(responsive_grid(cards, min_column_width=380, max_columns=2))

        requests = QFrame()
        requests.setObjectName("Card")
        requests_layout = QVBoxLayout(requests)
        requests_layout.setContentsMargins(14, 12, 14, 12)
        heading = QLabel("Recent Endpoint Requests")
        heading.setObjectName("CardTitle")
        requests_layout.addWidget(heading)
        self.request_table.setHorizontalHeaderLabels(["Timestamp", "Method", "Route", "Status", "Caller", "Duration ms"])
        configure_table(self.request_table)
        self.request_table.setMinimumHeight(260)
        requests_layout.addWidget(self.request_table)
        layout.addWidget(requests)
        layout.addStretch()

    def integration_specs(self) -> list[dict]:
        cfg = self.context.config.integrations
        return [
            {
                "title": "ProPresenter",
                "health_name": "ProPresenter",
                "config": cfg.propresenter,
                "fields": [
                    ("Host", lambda: cfg.propresenter.host),
                    ("Port", lambda: str(cfg.propresenter.port)),
                    ("API base", lambda: cfg.propresenter.base_url),
                ],
                "configure": self.configure_propresenter,
            },
            {
                "title": "OBS Studio",
                "health_name": "OBS",
                "config": cfg.obs,
                "fields": [
                    ("Host", lambda: cfg.obs.host),
                    ("Port", lambda: str(cfg.obs.port)),
                ],
                "configure": self.configure_obs,
            },
            {
                "title": "Panasonic AWP Camera",
                "health_name": "Panasonic AWP",
                "config": cfg.panasonic,
                "fields": [
                    ("Camera IP", lambda: cfg.panasonic.camera_ip),
                    ("User", lambda: cfg.panasonic.username),
                    ("Password", lambda: cfg.panasonic.password),
                ],
                "configure": self.configure_panasonic,
            },
            {
                "title": "VISCA Bridge",
                "health_name": "VISCA Bridge",
                "config": cfg.visca,
                "fields": [
                    ("Listen IP", lambda: cfg.visca.listen_ip),
                    ("UDP port", lambda: str(cfg.visca.udp_port)),
                ],
                "configure": self.configure_visca,
            },
            {
                "title": "Scoreboard Service",
                "health_name": "Scoreboard Service",
                "config": cfg.scoreboard,
                "fields": [],
                "configure": None,
            },
            {
                "title": "MIDI",
                "health_name": "MIDI",
                "config": cfg.midi,
                "fields": [
                    ("Input", lambda: cfg.midi.input_name or "Auto"),
                    ("Mappings", lambda: str(len(cfg.midi.mappings))),
                ],
                "configure": self.configure_midi,
                "extra_buttons": [("MIDI Log", self.show_midi_log)],
            },
        ]

    def refresh_live_state(self) -> None:
        health = {item.name: item for item in self.context.health_monitor.integration_list()}
        for card in self.cards.values():
            card.refresh(health.get(card.health_name))
        self.refresh_requests()

    def refresh_requests(self) -> None:
        state = self.context.runtime_state_repo.load()
        requests = state.endpoint_request_history[-100:]
        self.request_table.setRowCount(len(requests))
        for row, item in enumerate(requests):
            values = [
                readable_timestamp(item.timestamp),
                item.method,
                item.route,
                str(item.status_code),
                item.caller_ip,
                str(item.duration_ms),
            ]
            for column, value in enumerate(values):
                cell = self.request_table.item(row, column)
                if cell is None:
                    cell = QTableWidgetItem()
                    self.request_table.setItem(row, column, cell)
                cell.setText(value)

    def set_enabled(self, config, enabled: bool) -> None:
        config.enabled = enabled
        self.save_config()
        scoreboard = getattr(self.context, "scoreboard", None)
        if config is self.context.config.integrations.scoreboard and scoreboard is not None:
            scoreboard.set_enabled(enabled)
            self.context.health_monitor.update(scoreboard.health())
        midi_receiver = getattr(self.context, "midi", None)
        if config is self.context.config.integrations.midi and midi_receiver is not None:
            if enabled:
                midi_receiver.start()
            else:
                midi_receiver.stop()
            self.context.health_monitor.update(midi_receiver.health())
        self.refresh_live_state()

    def save_config(self) -> None:
        self.context.config_repository.save_app_config(self.context.config)

    def configure_propresenter(self) -> None:
        cfg = self.context.config.integrations.propresenter
        dialog = ConfigDialog("Configure ProPresenter", self)
        host = dialog.line("Host", cfg.host)
        port = dialog.port("Port", cfg.port)
        base = QLabel("Uses Host and Port above: http://host:port/v1")
        base.setObjectName("HelpText")
        dialog.form.addRow("API base", base)
        if dialog.accepted():
            cfg.host = host.text().strip()
            cfg.port = port.value()
            self.save_config()
            self.refresh_live_state()

    def configure_obs(self) -> None:
        cfg = self.context.config.integrations.obs
        dialog = ConfigDialog("Configure OBS Studio", self)
        host = dialog.line("Host", cfg.host)
        port = dialog.port("Port", cfg.port)
        if dialog.accepted():
            cfg.host = host.text().strip()
            cfg.port = port.value()
            self.save_config()
            self.refresh_live_state()

    def configure_panasonic(self) -> None:
        cfg = self.context.config.integrations.panasonic
        dialog = ConfigDialog("Configure Panasonic AWP Camera", self)
        camera_ip = dialog.line("Camera IP", cfg.camera_ip)
        user = dialog.line("User", cfg.username)
        password = dialog.line("Password", cfg.password)
        if dialog.accepted():
            cfg.camera_ip = camera_ip.text().strip()
            cfg.username = user.text().strip()
            cfg.password = password.text()
            self.save_config()
            self.refresh_live_state()

    def configure_visca(self) -> None:
        cfg = self.context.config.integrations.visca
        dialog = ConfigDialog("Configure VISCA Bridge", self)
        listen_ip = dialog.line("Listen IP", cfg.listen_ip)
        udp_port = dialog.port("UDP port", cfg.udp_port)
        if dialog.accepted():
            cfg.listen_ip = listen_ip.text().strip()
            cfg.udp_port = udp_port.value()
            self.save_config()
            self.refresh_live_state()

    def configure_midi(self) -> None:
        cfg = self.context.config.integrations.midi
        dialog = ConfigDialog("Configure MIDI", self)
        input_name = QComboBox()
        input_name.setEditable(True)
        inputs = detect_midi_inputs()
        for item in inputs:
            input_name.addItem(item)
        if cfg.input_name:
            index = input_name.findText(cfg.input_name)
            if index >= 0:
                input_name.setCurrentIndex(index)
            else:
                input_name.setEditText(cfg.input_name)
        elif inputs:
            input_name.setCurrentIndex(0)
        input_name.setMinimumWidth(360)
        dialog.form.addRow("Input", input_name)
        if not inputs:
            note = QLabel("No MIDI inputs detected. You can still type an input name.")
            note.setObjectName("HelpText")
            note.setWordWrap(True)
            dialog.form.addRow("", note)
        if dialog.accepted():
            cfg.input_name = input_name.currentText().strip()
            cfg.input_devices = inputs
            self.save_config()
            midi_receiver = getattr(self.context, "midi", None)
            if midi_receiver is not None:
                midi_receiver.stop()
                midi_receiver.start()
                self.context.health_monitor.update(midi_receiver.health())
            self.refresh_live_state()

    def show_midi_log(self) -> None:
        dialog = MidiLogDialog(self.context, self)
        dialog.exec()


class IntegrationCard(QFrame):
    def __init__(self, context, spec: dict, page: IntegrationsPage) -> None:
        super().__init__()
        self.context = context
        self.spec = spec
        self.page = page
        self.health_name = spec["health_name"]
        self.config = spec["config"]
        self.field_labels: dict[str, QLabel] = {}
        self.setObjectName("IntegrationCard")
        self.setProperty("disabledIntegration", False)
        self.build()

    def build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        header = QHBoxLayout()
        self.enabled = QCheckBox()
        self.enabled.setToolTip("Enable or disable this integration")
        self.enabled.setVisible(self.config is not None)
        if self.config is not None:
            self.enabled.setChecked(bool(self.config.enabled))
            self.enabled.toggled.connect(lambda checked: self.page.set_enabled(self.config, checked))
        header.addWidget(self.enabled)
        title_label = QLabel(self.spec["title"])
        title_label.setObjectName("CardTitle")
        header.addWidget(title_label, 1)
        self.dot = QLabel("●")
        self.dot.setObjectName("IntegrationStatusDot")
        self.dot.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.dot)
        layout.addLayout(header)

        self.body = QWidget()
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(8)
        self.status = QLabel("Status unknown")
        self.status.setObjectName("SummaryText")
        body_layout.addWidget(self.status)
        self.target = QLabel("")
        self.target.setObjectName("MetaLabel")
        self.target.setWordWrap(True)
        body_layout.addWidget(self.target)
        self.last_success = QLabel("")
        self.last_success.setObjectName("MetaLabel")
        self.last_success.setWordWrap(True)
        body_layout.addWidget(self.last_success)
        self.last_error = QLabel("")
        self.last_error.setObjectName("MetaLabel")
        self.last_error.setWordWrap(True)
        body_layout.addWidget(self.last_error)

        fields = QGridLayout()
        fields.setHorizontalSpacing(12)
        fields.setVerticalSpacing(6)
        for row, (label, _getter) in enumerate(self.spec["fields"]):
            left = QLabel(label)
            left.setObjectName("MetaLabel")
            right = QLabel("")
            right.setWordWrap(True)
            fields.addWidget(left, row, 0, Qt.AlignmentFlag.AlignTop)
            fields.addWidget(right, row, 1)
            self.field_labels[label] = right
        body_layout.addLayout(fields)
        layout.addWidget(self.body)

        configure = self.spec["configure"]
        extra_buttons = self.spec.get("extra_buttons", [])
        if configure is not None or extra_buttons:
            row = QHBoxLayout()
            row.addStretch()
            if configure is not None:
                button = QPushButton("Configure")
                button.clicked.connect(configure)
                row.addWidget(button)
            for label, handler in extra_buttons:
                button = QPushButton(label)
                button.clicked.connect(handler)
                row.addWidget(button)
            layout.addLayout(row)

    def refresh(self, health) -> None:
        enabled = bool(getattr(self.config, "enabled", True)) if self.config is not None else True
        if self.config is not None and self.enabled.isChecked() != enabled:
            self.enabled.blockSignals(True)
            self.enabled.setChecked(enabled)
            self.enabled.blockSignals(False)
        disabled = not enabled
        self.setProperty("disabledIntegration", disabled)
        self.body.setEnabled(enabled)
        self.style().unpolish(self)
        self.style().polish(self)

        status = STATUS_DISABLED if disabled else (health.status if health else "Unknown")
        connected = status == STATUS_CONNECTED
        color = "#22c55e" if connected else ("#7d8793" if disabled else "#ef4444")
        self.dot.setStyleSheet(f"color: {color}; font-size: 18px;")
        self.status.setText(status)
        self.target.setText(f"Target: {health.target}" if health and health.target else "")
        self.last_success.setText(f"Last success: {readable_timestamp(health.last_success_at)}" if health and health.last_success_at else "Last success: -")
        self.last_error.setText(f"Last error: {health.last_error}" if health and health.last_error else "Last error: -")
        for label, getter in self.spec["fields"]:
            self.field_labels[label].setText(str(getter() or "-"))


class ConfigDialog(QDialog):
    def __init__(self, window_title: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle(window_title)
        layout = QVBoxLayout(self)
        self.form = QFormLayout()
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.form.setHorizontalSpacing(14)
        self.form.setVerticalSpacing(10)
        layout.addLayout(self.form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Save)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def line(self, label: str, value: str) -> QLineEdit:
        field = QLineEdit(str(value or ""))
        field.setMinimumWidth(360)
        self.form.addRow(label, field)
        return field

    def port(self, label: str, value: int) -> QSpinBox:
        field = QSpinBox()
        field.setRange(1, 65535)
        field.setValue(int(value))
        self.form.addRow(label, field)
        return field

    def accepted(self) -> bool:
        return self.exec() == QDialog.DialogCode.Accepted


class MidiLogDialog(QDialog):
    def __init__(self, context, parent: QWidget) -> None:
        super().__init__(parent)
        self.context = context
        self.setWindowTitle("MIDI Log")
        self.resize(760, 460)
        layout = QVBoxLayout(self)
        note = QLabel("Recent MIDI messages seen by Production Hub.")
        note.setObjectName("HelpText")
        layout.addWidget(note)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.log, 1)
        row = QHBoxLayout()
        row.addStretch()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(refresh)
        row.addWidget(clear)
        row.addWidget(close)
        layout.addLayout(row)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def refresh(self) -> None:
        receiver = getattr(self.context, "midi", None)
        if receiver is None:
            self.log.setPlainText("MIDI receiver is not available in this app session.")
            return
        lines = receiver.event_log_lines()
        self.log.setPlainText("\n".join(lines) if lines else "No MIDI events seen yet.")
        self.log.moveCursor(QTextCursor.MoveOperation.End)

    def clear(self) -> None:
        receiver = getattr(self.context, "midi", None)
        if receiver is not None:
            receiver.clear_event_log()
        self.refresh()


def detect_midi_inputs() -> list[str]:
    try:
        import mido

        return list(mido.get_input_names())
    except Exception:
        return []


def readable_timestamp(value: str) -> str:
    if not value:
        return "-"
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%b %-d, %Y %-I:%M:%S %p")
    except Exception:
        return text


def build_page(context) -> QWidget:
    return IntegrationsPage(context)
