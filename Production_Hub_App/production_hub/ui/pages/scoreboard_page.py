from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from production_hub.integrations.scoreboard.models import ScoreRow, ScoreboardState
from production_hub.ui.pages.common import configure_table, int_from_line_edit, integer_line_edit, scroll_page, title

HISTORY_CAP = 60


class ScoreboardPage(QWidget):
    def __init__(self, context) -> None:
        super().__init__()
        self.context = context
        self.state: ScoreboardState = context.scoreboard.get_state()
        self.queue = 0
        self.action_history: list[tuple[datetime, str]] = []

        self.status = QLabel("Ready")
        self.status.setObjectName("StatusText")
        self.revision_label = QLabel("")
        self.modified_label = QLabel("")
        self.row_count_label = QLabel("")
        self.queue_total_label = QLabel("+ 0")
        self.queue_target = QListWidget()
        self.queue_custom = integer_line_edit(0, -9999, 9999, "+/- amount")
        self.apply_queue_btn = QPushButton("Apply Queue")
        self.undo_btn = QPushButton("Undo")
        self.rows_table = QTableWidget()
        self.history_list = QListWidget()

        self.build()
        self.reload()

    def build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        scroll, _body, layout = scroll_page()
        root.addWidget(scroll)
        layout.addWidget(title("Scoreboard", "Edit live scores with the same controls as score.html."))
        layout.addWidget(self.summary_bar())
        layout.addLayout(self.toolbar())
        layout.addWidget(self.rows_group())
        layout.addWidget(self.queue_group())
        layout.addWidget(self.history_group())
        layout.addWidget(self.status)
        layout.addStretch()

    def summary_bar(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("ScoreSummaryBar")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(18)
        for label in (self.revision_label, self.modified_label, self.row_count_label):
            label.setObjectName("SummaryText")
            layout.addWidget(label)
        layout.addStretch()
        return frame

    def toolbar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.undo_btn.clicked.connect(self.undo)
        clear_btn = QPushButton("Clear All")
        clear_btn.setObjectName("DangerButton")
        clear_btn.clicked.connect(self.clear_all)
        add_btn = QPushButton("Add Row")
        add_btn.clicked.connect(self.add_row)
        reload_btn = QPushButton("Reload")
        reload_btn.clicked.connect(self.reload)
        for button in (add_btn, self.undo_btn, clear_btn, reload_btn):
            row.addWidget(button)
        row.addStretch()
        return row

    def rows_group(self) -> QGroupBox:
        group = QGroupBox("Rows")
        layout = QVBoxLayout(group)
        self.rows_table.setColumnCount(8)
        self.rows_table.setHorizontalHeaderLabels(["Name", "Score", "-1", "+1", "Custom", "Apply", "Clear", "Delete"])
        configure_table(self.rows_table, stretch_last=False)
        header = self.rows_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in {1: 92, 2: 58, 3: 58, 4: 104, 5: 78, 6: 78, 7: 84}.items():
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self.rows_table.setColumnWidth(column, width)
        self.rows_table.setMinimumHeight(360)
        layout.addWidget(self.rows_table)
        return group

    def queue_group(self) -> QGroupBox:
        group = QGroupBox("Queue")
        layout = QGridLayout(group)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(10)

        self.queue_total_label.setObjectName("QueueTotal")
        layout.addWidget(QLabel("Total"), 0, 0)
        layout.addWidget(self.queue_total_label, 0, 1)

        plus = QPushButton("+1")
        minus = QPushButton("-1")
        clear = QPushButton("Clear")
        for button, width in ((plus, 58), (minus, 58), (clear, 78)):
            button.setFixedWidth(width)
        plus.clicked.connect(lambda: self.queue_add(1))
        minus.clicked.connect(lambda: self.queue_add(-1))
        clear.clicked.connect(self.queue_clear)
        layout.addWidget(plus, 0, 2)
        layout.addWidget(minus, 0, 3)
        layout.addWidget(clear, 0, 4)

        self.queue_custom.setFixedWidth(128)
        add_custom = QPushButton("Add Custom")
        add_custom.setFixedWidth(112)
        add_custom.clicked.connect(self.queue_apply_custom)
        self.queue_custom.returnPressed.connect(self.queue_apply_custom)
        layout.addWidget(QLabel("Custom"), 1, 0)
        layout.addWidget(self.queue_custom, 1, 1)
        layout.addWidget(add_custom, 1, 2)

        self.queue_target.setObjectName("TargetList")
        self.queue_target.setMaximumHeight(130)
        self.queue_target.itemSelectionChanged.connect(self.update_queue_ui)
        self.apply_queue_btn.setFixedWidth(112)
        self.apply_queue_btn.clicked.connect(self.apply_queue)
        layout.addWidget(QLabel("Apply to"), 2, 0)
        layout.addWidget(self.queue_target, 2, 1, 1, 3)
        layout.addWidget(self.apply_queue_btn, 2, 4)
        layout.setColumnStretch(5, 1)
        return group

    def history_group(self) -> QGroupBox:
        group = QGroupBox("History")
        layout = QVBoxLayout(group)
        self.history_list.setMaximumHeight(170)
        layout.addWidget(self.history_list)
        return group

    def reload(self) -> None:
        self.state = self.context.scoreboard.get_state()
        self.render()
        self.status.setText("Scoreboard loaded.")

    def render(self) -> None:
        self.revision_label.setText(f"Revision {self.state.revision}")
        self.modified_label.setText(f"Modified {self.state.modified_at or '-'}")
        self.row_count_label.setText(f"{len(self.state.rows)} rows")
        self.undo_btn.setEnabled(bool(self.state.history))
        self.render_rows()
        self.update_queue_ui()
        self.render_history()

    def render_rows(self) -> None:
        self.rows_table.setRowCount(len(self.state.rows))
        if not self.state.rows:
            self.rows_table.setRowCount(1)
            empty = QLabel("No rows yet. Add a row to begin.")
            empty.setObjectName("HelpText")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.rows_table.setCellWidget(0, 0, empty)
            self.rows_table.setSpan(0, 0, 1, self.rows_table.columnCount())
            return

        self.rows_table.clearSpans()
        for row_index, score_row in enumerate(self.state.rows):
            self.rows_table.setCellWidget(row_index, 0, self.name_editor(score_row))
            self.rows_table.setCellWidget(row_index, 1, self.score_label(score_row))
            self.rows_table.setCellWidget(row_index, 2, self.row_button("-1", lambda _=False, row_id=score_row.id: self.update_score(row_id, -1, f"-1 -> {self.row_label(row_id)}")))
            self.rows_table.setCellWidget(row_index, 3, self.row_button("+1", lambda _=False, row_id=score_row.id: self.update_score(row_id, 1, f"+1 -> {self.row_label(row_id)}")))

            custom = integer_line_edit(0, -9999, 9999, "+/- N")
            custom.setObjectName("InlineNumericEdit")
            custom.returnPressed.connect(lambda row_id=score_row.id, editor=custom: self.apply_custom(row_id, editor))
            self.rows_table.setCellWidget(row_index, 4, custom)
            self.rows_table.setCellWidget(row_index, 5, self.row_button("Apply", lambda _=False, row_id=score_row.id, editor=custom: self.apply_custom(row_id, editor)))
            self.rows_table.setCellWidget(row_index, 6, self.row_button("Clear", lambda _=False, row_id=score_row.id: self.clear_row(row_id)))
            self.rows_table.setCellWidget(row_index, 7, self.row_button("Delete", lambda _=False, row_id=score_row.id: self.delete_row(row_id), danger=True))
        self.rows_table.resizeRowsToContents()

    def name_editor(self, row: ScoreRow) -> QLineEdit:
        editor = QLineEdit(row.name)
        editor.setPlaceholderText("Name")
        editor.editingFinished.connect(lambda row_id=row.id, widget=editor: self.update_name(row_id, widget.text()))
        return editor

    def score_label(self, row: ScoreRow) -> QLabel:
        label = QLabel(str(row.score))
        label.setObjectName("TableScoreValue")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return label

    def row_button(self, text: str, handler, danger: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setFixedWidth(58 if text in {"-1", "+1"} else 72)
        if danger:
            button.setObjectName("DangerButton")
        button.clicked.connect(handler)
        return button

    def row_label(self, row_id: str) -> str:
        row = self.find_row(row_id)
        if row is None:
            return "(Unknown)"
        return row.name or "(Unnamed)"

    def find_row(self, row_id: str) -> ScoreRow | None:
        for row in self.state.rows:
            if row.id == row_id:
                return row
        return None

    def rows_payload(self, rows: list[ScoreRow], include_history: bool = True) -> dict:
        history = self.state.legacy_payload().get("history", [])
        if include_history:
            history.append([row.to_dict() for row in self.state.rows])
        return {
            "rows": [row.to_dict() for row in rows],
            "history": history[-100:],
        }

    def save_rows(self, rows: list[ScoreRow], status: str, history_text: str | None = None, include_history: bool = True) -> bool:
        try:
            self.state = self.context.scoreboard.update_state(
                self.rows_payload(rows, include_history=include_history),
                writer={"source": "Production Hub scoreboard tab"},
                expected_revision=self.state.revision,
            )
        except Exception as exc:
            self.status.setText(f"Save failed: {exc}")
            self.reload()
            return False
        if history_text:
            self.add_history(history_text)
        self.render()
        self.status.setText(status)
        return True

    def update_name(self, row_id: str, new_name: str) -> None:
        row = self.find_row(row_id)
        if row is None:
            return
        cleaned = str(new_name or "").strip()
        if row.name == cleaned:
            return
        previous = row.name or "(Unnamed)"
        rows = [replace(item, name=cleaned) if item.id == row_id else item for item in self.state.rows]
        next_label = cleaned or "(Unnamed)"
        self.save_rows(rows, "Name updated", f"Renamed: {previous} -> {next_label}")

    def update_score(self, row_id: str, delta: int, history_text: str | None = None) -> None:
        row = self.find_row(row_id)
        if row is None or int(delta) == 0:
            return
        rows = [
            replace(item, score=(int(item.score) + int(delta))) if item.id == row_id else item
            for item in self.state.rows
        ]
        status = f"+{delta}" if delta > 0 else str(delta)
        self.save_rows(rows, status, history_text)

    def apply_custom(self, row_id: str, editor: QLineEdit) -> None:
        value = int_from_line_edit(editor, 0)
        if value == 0:
            return
        label = self.row_label(row_id)
        self.update_score(row_id, value, f"Set {label}: {'+' if value > 0 else ''}{value}")
        editor.setText("0")

    def clear_row(self, row_id: str) -> None:
        row = self.find_row(row_id)
        if row is None or int(row.score) == 0:
            return
        rows = [replace(item, score=0) if item.id == row_id else item for item in self.state.rows]
        self.save_rows(rows, "Row cleared", f"Cleared {self.row_label(row_id)}")

    def delete_row(self, row_id: str) -> None:
        label = self.row_label(row_id)
        rows = [row for row in self.state.rows if row.id != row_id]
        self.save_rows(rows, "Row deleted", f"Deleted {label}")

    def add_row(self) -> None:
        rows = [
            *self.state.rows,
            ScoreRow(id=f"row-{uuid4().hex[:10]}", name="", score=0),
        ]
        self.save_rows(rows, "Added row", "Added row")

    def clear_all(self) -> None:
        if not self.state.rows:
            return
        if self.save_rows([], "Cleared", "Cleared all rows"):
            self.action_history.clear()
            self.add_history("Cleared all rows")

    def undo(self) -> None:
        if self.action_history:
            self.action_history.pop()
        try:
            self.context.scoreboard.undo()
        except Exception as exc:
            self.status.setText(f"Undo failed: {exc}")
            return
        self.reload()
        self.status.setText("Undo")

    def format_queue(self) -> str:
        sign = "+" if self.queue >= 0 else "-"
        return f"{sign} {abs(self.queue)}"

    def update_queue_ui(self) -> None:
        self.queue_total_label.setText(self.format_queue())
        current = self.queue_target.currentItem()
        previous = current.data(Qt.ItemDataRole.UserRole) if current else ""
        self.queue_target.blockSignals(True)
        self.queue_target.clear()
        if not self.state.rows:
            self.queue_target.addItem("No rows yet")
        else:
            for row in self.state.rows:
                item = QListWidgetItem(row.name or "(Unnamed)")
                item.setData(Qt.ItemDataRole.UserRole, row.id)
                self.queue_target.addItem(item)
        if previous:
            for index in range(self.queue_target.count()):
                item = self.queue_target.item(index)
                if item.data(Qt.ItemDataRole.UserRole) == previous:
                    self.queue_target.setCurrentRow(index)
                    break
        elif self.state.rows:
            self.queue_target.setCurrentRow(0)
        self.queue_target.blockSignals(False)
        selected = self.queue_target.currentItem()
        target_id = selected.data(Qt.ItemDataRole.UserRole) if selected else ""
        self.apply_queue_btn.setEnabled(bool(self.queue and target_id))

    def queue_add(self, amount: int) -> None:
        if amount == 0:
            return
        self.queue += int(amount)
        self.update_queue_ui()
        self.status.setText(f"Queue {'+' if amount > 0 else ''}{amount}")
        self.add_history(f"Queue {'+' if amount > 0 else ''}{amount}")

    def queue_apply_custom(self) -> None:
        value = int_from_line_edit(self.queue_custom, 0)
        self.queue_custom.setText("0")
        if value != 0:
            self.queue_add(value)

    def queue_clear(self) -> None:
        if self.queue == 0:
            return
        self.queue = 0
        self.update_queue_ui()
        self.status.setText("Queue cleared")
        self.add_history("Queue cleared")

    def apply_queue(self) -> None:
        selected = self.queue_target.currentItem()
        row_id = selected.data(Qt.ItemDataRole.UserRole) if selected else ""
        if not row_id or self.queue == 0:
            return
        amount = self.queue
        label = self.row_label(row_id)
        self.update_score(row_id, amount, f"Applied {'+' if amount > 0 else ''}{amount} -> {label}")
        self.queue = 0
        self.update_queue_ui()
        self.status.setText("Applied")

    def add_history(self, text: str) -> None:
        self.action_history.append((datetime.now(), text))
        self.action_history = self.action_history[-HISTORY_CAP:]
        self.render_history()

    def render_history(self) -> None:
        self.history_list.clear()
        if not self.action_history:
            self.history_list.addItem("No history yet.")
            return
        for timestamp, text in reversed(self.action_history):
            self.history_list.addItem(f"{timestamp.strftime('%-I:%M %p')}  {text}")


def build_page(context) -> QWidget:
    return ScoreboardPage(context)
