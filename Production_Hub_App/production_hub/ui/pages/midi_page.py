from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QBrush, QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QMenu,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from production_hub.core.endpoints.catalog import ACTION_SPECS, action_spec, action_tree_path
from production_hub.core.endpoints.models import ActionDefinition
from production_hub.integrations.midi.models import MidiMapping
from production_hub.ui.dialogs.action_palette import ActionParameterDialog, action_summary
from production_hub.ui.pages.common import configure_table, scroll_page, title


NOTE_NAMES = ("A", "A#", "B", "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#")
MUTED_BRUSH = QBrush(QColor("#7d8793"))
NORMAL_BRUSH = QBrush()


class MidiPage(QWidget):
    def __init__(self, context) -> None:
        super().__init__()
        self.context = context
        self.cfg = context.config.integrations.midi
        self.detected_inputs: list[str] = []
        self.actions_by_note: dict[int, list[ActionDefinition]] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        scroll, _body, layout = scroll_page()
        layout.addWidget(title("MIDI", "Map incoming MIDI notes to ordered Production Hub action sequences."))
        self.status = QLabel(
            "Right-click a note's Action cell to add, edit, remove, or clear actions. MIDI input selection is configured in Integrations."
        )
        self.status.setObjectName("PageSubtitle")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Event", "Channel", "Note", "Action"])
        configure_table(self.table)
        self.table.setIconSize(QSize(18, 18))
        self.table.setMinimumHeight(560)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_action_menu)
        layout.addWidget(self.table)
        layout.addStretch()
        root.addWidget(scroll)

        self.load_mappings()
        if self.table.rowCount():
            self.table.setCurrentCell(0, 0)

    def load_mappings(self) -> None:
        self.actions_by_note = {}
        for item in self.cfg.mappings:
            mapping = MidiMapping.from_dict(item)
            if mapping.event_type == "note_on" and 0 <= int(mapping.number) <= 127:
                self.actions_by_note.setdefault(int(mapping.number), []).extend(mapping.actions)
        self.table.setRowCount(0)
        for note in range(128):
            self.append_note_row(note)
        self.refresh_all_rows()

    def append_note_row(self, note: int) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        for column, value in enumerate(["note_on", "Any", str(note), self.actions_summary(note)]):
            item = QTableWidgetItem(value)
            flags = item.flags()
            if column in {2, 3}:
                flags &= ~Qt.ItemFlag.ItemIsEditable
            item.setFlags(flags)
            self.table.setItem(row, column, item)

    def actions_summary(self, note: int) -> str:
        actions = self.actions_by_note.get(note, [])
        if not actions:
            return "No actions"
        return "1 action" if len(actions) == 1 else f"{len(actions)} actions"

    def selected_note(self) -> int | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        return int(self.cell(row, 2))

    def show_action_menu(self, position) -> None:
        item = self.table.itemAt(position)
        if item is None:
            return
        row = item.row()
        if item.column() != 3:
            return
        self.table.setCurrentCell(row, 3)
        note = int(self.cell(row, 2))
        menu = QMenu(self)

        add_menu = menu.addMenu("Add Action")
        self.populate_add_action_menu(add_menu, note)

        clear_action = menu.addAction("Clear All Actions")
        clear_action.setEnabled(bool(self.actions_by_note.get(note)))
        clear_action.triggered.connect(lambda _checked=False, note_ref=note: self.clear_actions(note_ref))

        actions = self.actions_by_note.get(note, [])
        if actions:
            menu.addSeparator()
        for index, action in enumerate(actions):
            action_menu = menu.addMenu(f"{index + 1}. {action_summary(action)}")
            edit = action_menu.addAction("Edit Action")
            edit.triggered.connect(lambda _checked=False, note_ref=note, index_ref=index: self.defer_edit_action(note_ref, index_ref))
            remove = action_menu.addAction("Remove Action")
            remove.triggered.connect(lambda _checked=False, note_ref=note, index_ref=index: self.remove_action(note_ref, index_ref))

        menu.exec(self.table.viewport().mapToGlobal(position))

    def populate_add_action_menu(self, menu: QMenu, note: int) -> None:
        menus: dict[tuple[str, ...], QMenu] = {}
        for spec in ACTION_SPECS:
            parent = menu
            path_so_far: list[str] = []
            for part in action_tree_path(spec.action_type):
                path_so_far.append(part)
                key = tuple(path_so_far)
                if key not in menus:
                    menus[key] = parent.addMenu(part)
                parent = menus[key]
            leaf = QAction(spec.label, self)
            leaf.setToolTip(spec.description)
            leaf.triggered.connect(lambda _checked=False, note_ref=note, action_type=spec.action_type: self.defer_add_action(note_ref, action_type))
            parent.addAction(leaf)

    def defer_add_action(self, note: int, action_type: str) -> None:
        QTimer.singleShot(75, lambda note_ref=note, action_type_ref=action_type: self.add_action(note_ref, action_type_ref))

    def add_action(self, note: int, action_type: str) -> None:
        action = self.build_action(action_type)
        if action is None:
            return
        self.actions_by_note.setdefault(note, []).append(action)
        self.refresh_note(note)
        self.save("MIDI action added and applied live.")

    def build_action(self, action_type: str, existing: ActionDefinition | None = None) -> ActionDefinition | None:
        spec = action_spec(action_type)
        if not spec.fields and existing is None:
            return ActionDefinition(action_type, {})
        dialog = ActionParameterDialog(self.context, action_type, existing, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.selected_action:
            return None
        return dialog.selected_action

    def edit_action(self, note: int, index: int) -> None:
        actions = self.actions_by_note.get(note, [])
        if index >= len(actions):
            return
        action = self.build_action(actions[index].action_type, actions[index])
        if action is None:
            return
        actions[index] = action
        self.refresh_note(note)
        self.save("MIDI action updated and applied live.")

    def defer_edit_action(self, note: int, index: int) -> None:
        QTimer.singleShot(75, lambda note_ref=note, index_ref=index: self.edit_action(note_ref, index_ref))

    def remove_action(self, note: int, index: int) -> None:
        actions = self.actions_by_note.get(note, [])
        if index < len(actions):
            actions.pop(index)
        if not actions:
            self.actions_by_note.pop(note, None)
        self.refresh_note(note)
        self.save("MIDI action removed and applied live.")

    def clear_actions(self, note: int) -> None:
        self.actions_by_note.pop(note, None)
        self.refresh_note(note)
        self.save("MIDI actions cleared and applied live.")

    def load_pad_defaults(self) -> None:
        for note in range(9, 45):
            self.actions_by_note.pop(note, None)
        for offset, note_name in enumerate(NOTE_NAMES):
            self.actions_by_note[9 + offset] = [ActionDefinition("propresenter.audio_trigger", {"playlist": "Major Pads", "track": f"{note_name} Major Pads"})]
            self.actions_by_note[21 + offset] = [ActionDefinition("propresenter.audio_trigger", {"playlist": "Minor Pads", "track": f"{note_name} Minor Pads"})]
            self.actions_by_note[33 + offset] = [ActionDefinition("propresenter.audio_trigger", {"playlist": "Neutral Pads", "track": f"{note_name} Neutral Pads"})]
        self.refresh_all_rows()
        self.save("Pad defaults loaded and applied live.")

    def save(self, message: str = "MIDI settings saved and applied live.") -> None:
        previous_enabled = bool(self.cfg.enabled)
        previous_input = str(self.cfg.input_name or "").strip()
        mappings: list[dict[str, Any]] = []
        try:
            for row in range(self.table.rowCount()):
                note = int(self.cell(row, 2))
                actions = self.actions_by_note.get(note, [])
                if not actions:
                    continue
                event = self.cell(row, 0) or "note_on"
                channel_text = self.cell(row, 1)
                channel = -1 if channel_text.lower() in {"", "any", "*"} else int(channel_text)
                mapping = MidiMapping(event_type=event, channel=channel, number=note, actions=actions, metadata={"label": self.actions_summary(note)})
                mappings.append(mapping.to_dict())
        except Exception as exc:
            self.status.setText(f"Could not save mapping: {exc}")
            return
        mappings.sort(key=lambda item: (int(item.get("number", 0)), int(item.get("channel", -1)), str(item.get("event_type", ""))))
        self.cfg.mappings = mappings
        self.context.config_repository.save_app_config(self.context.config)
        midi_receiver = getattr(self.context, "midi", None)
        if midi_receiver is not None:
            midi_receiver.update_mappings([MidiMapping.from_dict(item) for item in mappings])
            if previous_enabled != bool(self.cfg.enabled) or previous_input != self.cfg.input_name:
                self.reconnect_receiver("MIDI settings saved. Receiver reconnected.")
                return
        self.status.setText(message)
        self.refresh_all_rows()

    def reconnect_receiver(self, message: str = "MIDI receiver reconnected.") -> None:
        midi_receiver = getattr(self.context, "midi", None)
        if midi_receiver is None:
            self.status.setText("MIDI receiver is not available in this app session.")
            return
        midi_receiver.stop()
        started = midi_receiver.start()
        health_monitor = getattr(self.context, "health_monitor", None)
        if health_monitor is not None:
            health_monitor.update(midi_receiver.health())
        if started:
            self.status.setText(f"{message} Listening on {midi_receiver.input_name}.")
            return
        self.status.setText(f"MIDI receiver is offline: {midi_receiver.health().last_error or 'disabled'}")

    def refresh_note(self, note: int) -> None:
        self.refresh_row(note)

    def refresh_all_rows(self) -> None:
        for row in range(self.table.rowCount()):
            self.refresh_row(row)

    def refresh_row(self, row: int) -> None:
        note = int(self.cell(row, 2))
        item = self.table.item(row, 3)
        if item:
            item.setText(self.actions_summary(note))
            item.setIcon(action_icon(self.actions_by_note.get(note, [])))
            item.setToolTip(self.actions_tooltip(note))
        brush = NORMAL_BRUSH if self.actions_by_note.get(note) else MUTED_BRUSH
        for column in range(self.table.columnCount()):
            cell = self.table.item(row, column)
            if cell:
                cell.setForeground(brush)

    def cell(self, row: int, column: int) -> str:
        item = self.table.item(row, column)
        return str(item.text() if item else "").strip()

    def actions_tooltip(self, note: int) -> str:
        actions = self.actions_by_note.get(note, [])
        if not actions:
            return "No actions"
        return "\n".join(action_summary(action) for action in actions)


def action_icon(actions: list[ActionDefinition]) -> QIcon:
    if not actions:
        return QIcon()
    categories = {action_category(action.action_type) for action in actions}
    category = action_category(actions[0].action_type) if len(categories) == 1 else "Mixed"
    return category_icon(category)


def action_category(action_type: str) -> str:
    category = action_spec(action_type).category
    if category == "Panasonic AWP":
        return "PTZ"
    return category


def category_icon(category: str) -> QIcon:
    colors = {
        "ProPresenter": "#2563eb",
        "OBS": "#7c3aed",
        "PTZ": "#0891b2",
        "Scoreboard": "#16a34a",
        "Runtime": "#ea580c",
        "Utility": "#64748b",
        "Mixed": "#334155",
    }
    color = QColor(colors.get(category, "#334155"))
    pixmap = QPixmap(22, 22)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(color, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(Qt.BrushStyle.NoBrush)

    if category == "ProPresenter":
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(QPolygonF([QPointF(8, 6), QPointF(8, 16), QPointF(16, 11)]))
    elif category == "OBS":
        painter.drawEllipse(QRectF(5, 5, 12, 12))
        painter.drawEllipse(QRectF(9, 9, 4, 4))
    elif category == "PTZ":
        painter.drawRoundedRect(QRectF(4, 8, 14, 9), 2, 2)
        painter.drawEllipse(QRectF(9, 10, 5, 5))
        painter.drawLine(7, 8, 10, 5)
        painter.drawLine(10, 5, 15, 5)
    elif category == "Scoreboard":
        painter.drawRoundedRect(QRectF(4, 5, 14, 12), 2, 2)
        painter.drawLine(11, 6, 11, 16)
        painter.drawLine(5, 11, 17, 11)
    elif category == "Runtime":
        painter.drawRoundedRect(QRectF(4, 7, 14, 8), 4, 4)
        painter.setBrush(color)
        painter.drawEllipse(QRectF(11, 8, 6, 6))
    elif category == "Utility":
        painter.drawEllipse(QRectF(5, 5, 12, 12))
        painter.drawLine(11, 7, 11, 11)
        painter.drawLine(11, 11, 14, 13)
    else:
        painter.drawEllipse(QRectF(5, 5, 4, 4))
        painter.drawEllipse(QRectF(13, 5, 4, 4))
        painter.drawEllipse(QRectF(9, 13, 4, 4))

    painter.end()
    return QIcon(pixmap)


def build_page(context) -> QWidget:
    return MidiPage(context)
