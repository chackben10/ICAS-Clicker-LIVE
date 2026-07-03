from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


def scroll_page() -> tuple[QScrollArea, QWidget, QVBoxLayout]:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setObjectName("PageScroll")
    body = QWidget()
    body.setObjectName("PageBody")
    layout = QVBoxLayout(body)
    layout.setContentsMargins(24, 20, 24, 24)
    layout.setSpacing(16)
    scroll.setWidget(body)
    return scroll, body, layout


def title(text: str, subtitle: str = "") -> QWidget:
    widget = QWidget()
    widget.setObjectName("TitleBlock")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    heading = QLabel(text)
    heading.setObjectName("PageTitle")
    layout.addWidget(heading)
    if subtitle:
        sub = QLabel(subtitle)
        sub.setObjectName("PageSubtitle")
        sub.setWordWrap(True)
        layout.addWidget(sub)
    return widget


def card(title_text: str, rows: list[tuple[str, str]], buttons: list[str] | None = None) -> QFrame:
    frame = QFrame()
    frame.setObjectName("Card")
    frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 12, 14, 12)
    layout.setSpacing(8)
    heading = QLabel(title_text)
    heading.setObjectName("CardTitle")
    layout.addWidget(heading)
    grid = QGridLayout()
    grid.setHorizontalSpacing(12)
    grid.setVerticalSpacing(6)
    for row, (label, value) in enumerate(rows):
        left = QLabel(label)
        left.setObjectName("MetaLabel")
        right = QLabel(value or "-")
        right.setWordWrap(True)
        grid.addWidget(left, row, 0, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(right, row, 1)
    layout.addLayout(grid)
    if buttons:
        button_row = QHBoxLayout()
        button_row.addStretch()
        for text in buttons:
            button_row.addWidget(QPushButton(text))
        layout.addLayout(button_row)
    return frame


def two_column_grid(widgets: list[QWidget]) -> QWidget:
    holder = QWidget()
    holder.setObjectName("GridHolder")
    layout = QGridLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(12)
    for idx, widget in enumerate(widgets):
        layout.addWidget(widget, idx // 2, idx % 2)
    return holder


def configure_table(table: QTableWidget, stretch_last: bool = True) -> QTableWidget:
    table.setAlternatingRowColors(True)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setMinimumHeight(42)
    table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    if stretch_last and table.columnCount() > 0:
        table.horizontalHeader().setSectionResizeMode(table.columnCount() - 1, QHeaderView.ResizeMode.Stretch)
    table.setWordWrap(False)
    table.setMinimumHeight(220)
    table.resizeRowsToContents()
    return table


def set_table_row(table: QTableWidget, row: int, values: list[Any]) -> None:
    for column, value in enumerate(values):
        item = QTableWidgetItem(str(value))
        item.setToolTip(str(value))
        table.setItem(row, column, item)


def pretty_json(data: Any) -> str:
    if hasattr(data, "to_dict"):
        data = data.to_dict()
    return json.dumps(data, indent=2, sort_keys=True)


def code_editor(text: str = "") -> QTextEdit:
    editor = QTextEdit()
    editor.setObjectName("CodeEditor")
    editor.setPlainText(text)
    editor.setMinimumHeight(260)
    editor.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
    return editor


def integer_line_edit(value: int = 0, low: int = -9999, high: int = 9999, placeholder: str = "") -> QLineEdit:
    editor = QLineEdit(str(value))
    editor.setValidator(QIntValidator(low, high, editor))
    editor.setPlaceholderText(placeholder)
    editor.setObjectName("NumericLineEdit")
    return editor


def int_from_line_edit(editor: QLineEdit, default: int = 0) -> int:
    text = editor.text().strip()
    if text in {"", "+", "-"}:
        return default
    try:
        return int(text)
    except ValueError:
        return default


def run_background(coro_factory: Callable[[], Any], on_done: Callable[[bool, str], None] | None = None) -> None:
    def worker() -> None:
        import asyncio

        ok = True
        message = "OK"
        try:
            result = asyncio.run(coro_factory())
            if result is not None:
                message = str(result)
        except Exception as exc:
            ok = False
            message = str(exc)
        if on_done:
            QTimer.singleShot(0, lambda: on_done(ok, message))

    threading.Thread(target=worker, daemon=True).start()
