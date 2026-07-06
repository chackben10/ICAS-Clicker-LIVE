from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from production_hub.core.endpoints.catalog import ACTION_SPECS, action_spec, default_action_params
from production_hub.core.endpoints.models import ActionDefinition
from production_hub.integrations.midi.models import MidiMapping
from production_hub.ui.pages.common import configure_table, scroll_page, title


NOTE_NAMES = ("A", "A#", "B", "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#")
NO_ACTION = ""
MUTED = QColor("#7d8793")
MUTED_BRUSH = QBrush(MUTED)
NORMAL_BRUSH = QBrush()
ACTION_ROLE = Qt.ItemDataRole.UserRole


class ActionPickerDialog(QDialog):
    def __init__(self, current_action: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select Action")
        self.setMinimumSize(420, 520)
        self.selected_action = current_action

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self.list_widget = QListWidget()
        none_item = QListWidgetItem("No action")
        none_item.setData(ACTION_ROLE, NO_ACTION)
        self.list_widget.addItem(none_item)
        for spec in ACTION_SPECS:
            item = QListWidgetItem(f"{spec.category} - {spec.label}")
            item.setData(ACTION_ROLE, spec.action_type)
            item.setToolTip(spec.description)
            self.list_widget.addItem(item)
        layout.addWidget(self.list_widget)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        choose = QPushButton("Choose")
        choose.clicked.connect(self.accept_selection)
        buttons.addWidget(cancel)
        buttons.addWidget(choose)
        layout.addLayout(buttons)

        self.list_widget.itemDoubleClicked.connect(lambda _item: self.accept_selection())
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            if item.data(ACTION_ROLE) == current_action:
                self.list_widget.setCurrentRow(row)
                break

    def accept_selection(self) -> None:
        item = self.list_widget.currentItem()
        self.selected_action = str(item.data(ACTION_ROLE) if item else "")
        self.accept()


class MidiPage(QWidget):
    def __init__(self, context) -> None:
        super().__init__()
        self.context = context
        self.cfg = context.config.integrations.midi
        self.detected_inputs: list[str] = []
        self._loading = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        scroll, _body, layout = scroll_page()
        layout.addWidget(title("MIDI", "Map incoming MIDI notes to any Production Hub action."))

        controls = QWidget()
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(10)

        self.enabled = QCheckBox("Enabled")
        self.enabled.setChecked(bool(self.cfg.enabled))
        controls_layout.addWidget(self.enabled)

        controls_layout.addWidget(QLabel("Input"))
        self.input_name = QLineEdit(self.cfg.input_name)
        self.input_name.setMinimumWidth(300)
        self.input_name.setPlaceholderText("Production Hub MIDI")
        controls_layout.addWidget(self.input_name)

        refresh = QPushButton("Detect Inputs")
        refresh.clicked.connect(self.refresh_inputs)
        controls_layout.addWidget(refresh)

        save = QPushButton("Save")
        save.clicked.connect(self.save)
        controls_layout.addWidget(save)
        controls_layout.addStretch()
        layout.addWidget(controls)

        self.detected_row = QWidget()
        self.detected_layout = QHBoxLayout(self.detected_row)
        self.detected_layout.setContentsMargins(0, 0, 0, 0)
        self.detected_layout.setSpacing(8)
        layout.addWidget(self.detected_row)

        self.status = QLabel("Choose an action for a note; the JSON column fills with that action's parameters.")
        self.status.setObjectName("PageSubtitle")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        button_row = QWidget()
        button_layout = QHBoxLayout(button_row)
        button_layout.setContentsMargins(0, 0, 0, 0)
        clear = QPushButton("Clear Selected")
        clear.clicked.connect(self.clear_selected)
        defaults = QPushButton("Load Pad Defaults")
        defaults.clicked.connect(self.load_pad_defaults)
        button_layout.addWidget(clear)
        button_layout.addWidget(defaults)
        button_layout.addStretch()
        layout.addWidget(button_row)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Event", "Channel", "Note", "Action", "Parameters JSON"])
        configure_table(self.table)
        self.table.setMinimumHeight(520)
        layout.addWidget(self.table)

        layout.addStretch()
        root.addWidget(scroll)

        self.refresh_inputs()
        self.load_mappings()

    def refresh_inputs(self) -> None:
        try:
            import mido

            self.detected_inputs = list(mido.get_input_names())
        except Exception as exc:
            self.detected_inputs = []
            self.status.setText(f"MIDI inputs unavailable: {exc}")
        self.render_detected_inputs()
        if self.detected_inputs and not self.input_name.text().strip():
            self.input_name.setText(self.detected_inputs[0])
        if self.detected_inputs:
            self.status.setText(f"Detected {len(self.detected_inputs)} MIDI input(s).")

    def render_detected_inputs(self) -> None:
        while self.detected_layout.count():
            item = self.detected_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.detected_layout.addWidget(QLabel("Detected"))
        if not self.detected_inputs:
            self.detected_layout.addWidget(QLabel("No visible MIDI inputs"))
            self.detected_layout.addStretch()
            return
        for name in self.detected_inputs:
            button = QPushButton(name)
            button.clicked.connect(lambda _checked=False, value=name: self.input_name.setText(value))
            self.detected_layout.addWidget(button)
        self.detected_layout.addStretch()

    def load_mappings(self) -> None:
        self._loading = True
        self.table.setRowCount(0)
        by_note = self.mappings_by_note()
        for note in range(128):
            mapping = by_note.get(note)
            if mapping:
                self.append_mapping_row(mapping.event_type, mapping.channel, mapping.number, mapping.action.action_type, mapping.action.params)
            else:
                self.append_mapping_row("note_on", -1, note, NO_ACTION, {})
        self._loading = False
        self.refresh_row_styles()

    def mappings_by_note(self) -> dict[int, MidiMapping]:
        mappings: dict[int, MidiMapping] = {}
        for item in self.cfg.mappings:
            mapping = MidiMapping.from_dict(item)
            if mapping.event_type == "note_on" and 0 <= int(mapping.number) <= 127 and int(mapping.number) not in mappings:
                mappings[int(mapping.number)] = mapping
        return mappings

    def append_mapping_row(self, event: str, channel: int, note: int, action_type: str, params: dict[str, Any]) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        values = [event, "Any" if int(channel) < 0 else str(channel), str(note)]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, column, item)

        note_item = self.table.item(row, 2)
        if note_item:
            note_item.setFlags(note_item.flags() & ~Qt.ItemFlag.ItemIsEditable)

        button = self.action_button(action_type)
        button.clicked.connect(lambda _checked=False, row_ref=row: self.pick_action(row_ref))
        self.table.setCellWidget(row, 3, button)

        params_item = QTableWidgetItem(self.params_text(action_type, params))
        params_item.setFlags(params_item.flags() | Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(row, 4, params_item)

    def action_button(self, selected_action: str) -> QPushButton:
        button = QPushButton(self.action_label(selected_action))
        button.setProperty("action_type", selected_action)
        if selected_action:
            button.setToolTip(action_spec(selected_action).description)
        return button

    def action_label(self, action_type: str) -> str:
        if not action_type:
            return "No action"
        spec = action_spec(action_type)
        return f"{spec.category} - {spec.label}"

    def pick_action(self, row: int) -> None:
        if self._loading:
            return
        dialog = ActionPickerDialog(self.row_action_type(row), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        action_type = dialog.selected_action
        button = self.table.cellWidget(row, 3)
        if isinstance(button, QPushButton):
            button.setText(self.action_label(action_type))
            button.setProperty("action_type", action_type)
            button.setToolTip(action_spec(action_type).description if action_type else "")
        item = self.table.item(row, 4)
        if item is None:
            item = QTableWidgetItem()
            self.table.setItem(row, 4, item)
        item.setText(self.params_text(action_type, default_action_params(action_type) if action_type else {}))
        self.refresh_row_style(row)

    def params_text(self, action_type: str, params: dict[str, Any]) -> str:
        if not action_type:
            return ""
        if not params:
            params = default_action_params(action_type)
        return json.dumps(params, sort_keys=True)

    def clear_selected(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        for row in rows:
            button = self.table.cellWidget(row, 3)
            if isinstance(button, QPushButton):
                button.setText(self.action_label(NO_ACTION))
                button.setProperty("action_type", NO_ACTION)
                button.setToolTip("")
            item = self.table.item(row, 4)
            if item:
                item.setText("")
            self.refresh_row_style(row)

    def load_pad_defaults(self) -> None:
        self._loading = True
        for row in range(self.table.rowCount()):
            note = int(self.cell(row, 2) or 0)
            action_type = NO_ACTION
            params: dict[str, Any] = {}
            if 9 <= note <= 20:
                action_type = "propresenter.audio_trigger"
                params = {"playlist": "Major Pads", "track": f"{NOTE_NAMES[note - 9]} Major Pads"}
            elif 21 <= note <= 32:
                action_type = "propresenter.audio_trigger"
                params = {"playlist": "Minor Pads", "track": f"{NOTE_NAMES[note - 21]} Minor Pads"}
            elif 33 <= note <= 44:
                action_type = "propresenter.audio_trigger"
                params = {"playlist": "Neutral Pads", "track": f"{NOTE_NAMES[note - 33]} Neutral Pads"}
            button = self.table.cellWidget(row, 3)
            if isinstance(button, QPushButton):
                button.setText(self.action_label(action_type))
                button.setProperty("action_type", action_type)
                button.setToolTip(action_spec(action_type).description if action_type else "")
            item = self.table.item(row, 4)
            if item:
                item.setText(self.params_text(action_type, params))
        self._loading = False
        self.refresh_row_styles()

    def save(self) -> None:
        self.cfg.enabled = self.enabled.isChecked()
        self.cfg.input_name = self.input_name.text().strip()
        mappings: list[dict[str, Any]] = []
        try:
            for row in range(self.table.rowCount()):
                action_type = self.row_action_type(row)
                if not action_type:
                    continue
                event = self.cell(row, 0) or "note_on"
                channel_text = self.cell(row, 1)
                channel = -1 if channel_text.lower() in {"", "any", "*"} else int(channel_text)
                note = int(self.cell(row, 2))
                params = self.parse_params(self.cell(row, 4))
                mapping = MidiMapping(
                    event_type=event,
                    channel=channel,
                    number=note,
                    action=ActionDefinition(action_type, params),
                    metadata={"label": self.action_summary(action_type, params)},
                )
                mappings.append(mapping.to_dict())
        except Exception as exc:
            self.status.setText(f"Could not save mapping: {exc}")
            return
        mappings.sort(key=lambda item: (int(item.get("number", 0)), int(item.get("channel", -1)), str(item.get("event_type", ""))))
        self.cfg.mappings = mappings
        self.context.config_repository.save_app_config(self.context.config)
        self.status.setText("MIDI settings saved. Restart Production Hub for listener changes to take effect.")
        self.refresh_row_styles()

    def refresh_row_styles(self) -> None:
        for row in range(self.table.rowCount()):
            self.refresh_row_style(row)

    def refresh_row_style(self, row: int) -> None:
        brush = NORMAL_BRUSH if self.row_action_type(row) else MUTED_BRUSH
        for column in (0, 1, 2, 4):
            item = self.table.item(row, column)
            if item:
                item.setForeground(brush)

    def row_action_type(self, row: int) -> str:
        button = self.table.cellWidget(row, 3)
        if isinstance(button, QPushButton):
            return str(button.property("action_type") or "")
        return ""

    def parse_params(self, text: str) -> dict[str, Any]:
        if not text:
            return {}
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("Parameters JSON must be an object")
        return data

    def action_summary(self, action_type: str, params: dict[str, Any]) -> str:
        spec = action_spec(action_type)
        detail = ", ".join(f"{key}={value}" for key, value in params.items() if value not in {"", None})
        return f"{spec.label}" + (f" ({detail})" if detail else "")

    def cell(self, row: int, column: int) -> str:
        item = self.table.item(row, column)
        return str(item.text() if item else "").strip()


def build_page(context) -> QWidget:
    return MidiPage(context)
