from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Signal, Qt
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
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QBoxLayout,
    QVBoxLayout,
    QWidget,
)


MAX_WIDGET_WIDTH = 16777215
PAGE_MARGIN = 24
SECTION_GAP = 16
PANE_GAP = 18


class _BackgroundResultBridge(QObject):
    completed = Signal(object, bool, str)

    def __init__(self) -> None:
        super().__init__()
        self.completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def _deliver(self, callback: Callable[[bool, str], None], ok: bool, message: str) -> None:
        callback(ok, message)


_background_result_bridge: _BackgroundResultBridge | None = None


def _result_bridge() -> _BackgroundResultBridge:
    global _background_result_bridge
    if _background_result_bridge is None:
        _background_result_bridge = _BackgroundResultBridge()
    return _background_result_bridge


def scroll_page() -> tuple[QScrollArea, QWidget, QVBoxLayout]:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setObjectName("PageScroll")
    body = QWidget()
    body.setObjectName("PageBody")
    layout = QVBoxLayout(body)
    layout.setContentsMargins(PAGE_MARGIN, 20, PAGE_MARGIN, PAGE_MARGIN)
    layout.setSpacing(SECTION_GAP)
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
    return responsive_grid(widgets, min_column_width=340, max_columns=2)


def responsive_grid(widgets: list[QWidget], min_column_width: int = 340, max_columns: int = 2) -> QWidget:
    return ResponsiveGrid(widgets, min_column_width=min_column_width, max_columns=max_columns)


def responsive_two_pane(
    left: QWidget,
    right: QWidget,
    collapse_width: int = 980,
    left_min_width: int = 360,
    left_max_width: int = 520,
    spacing: int = PANE_GAP,
) -> QWidget:
    return ResponsiveTwoPane(
        left,
        right,
        collapse_width=collapse_width,
        left_min_width=left_min_width,
        left_max_width=left_max_width,
        spacing=spacing,
    )


class ResponsiveGrid(QWidget):
    def __init__(self, widgets: list[QWidget], min_column_width: int = 340, max_columns: int = 2) -> None:
        super().__init__()
        self.widgets = widgets
        self.min_column_width = max(160, min_column_width)
        self.max_columns = max(1, max_columns)
        self._current_columns = 0
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(SECTION_GAP)
        self.setObjectName("GridHolder")
        for widget in self.widgets:
            widget.setSizePolicy(QSizePolicy.Expanding, widget.sizePolicy().verticalPolicy())
        self.reflow()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.reflow()

    def reflow(self) -> None:
        width = max(self.width(), self.min_column_width)
        columns = max(1, min(self.max_columns, width // self.min_column_width))
        if columns == self._current_columns and self.grid.count() == len(self.widgets):
            return
        while self.grid.count():
            self.grid.takeAt(0)
        for index, widget in enumerate(self.widgets):
            self.grid.addWidget(widget, index // columns, index % columns)
        for column in range(self.max_columns):
            self.grid.setColumnStretch(column, 1 if column < columns else 0)
        self._current_columns = columns


class ResponsiveTwoPane(QWidget):
    def __init__(
        self,
        left: QWidget,
        right: QWidget,
        collapse_width: int = 980,
        left_min_width: int = 360,
        left_max_width: int = 520,
        spacing: int = PANE_GAP,
    ) -> None:
        super().__init__()
        self.left = left
        self.right = right
        self.collapse_width = collapse_width
        self.left_min_width = left_min_width
        self.left_max_width = max(left_min_width, left_max_width)
        self.spacing = spacing
        self._stacked: bool | None = None
        self.layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(self.spacing)
        self.setObjectName("ResponsiveTwoPane")
        self.reflow()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.reflow()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.reflow()

    def reflow(self) -> None:
        stacked = self.width() < self.collapse_width
        if stacked == self._stacked and self.layout.count() == 2:
            return

        while self.layout.count():
            self.layout.takeAt(0)

        if stacked:
            self.layout.setDirection(QBoxLayout.Direction.TopToBottom)
            self.left.setMinimumWidth(0)
            self.left.setMaximumWidth(MAX_WIDGET_WIDTH)
            self.right.setMinimumWidth(0)
            self.layout.addWidget(self.left)
            self.layout.addWidget(self.right)
        else:
            self.layout.setDirection(QBoxLayout.Direction.LeftToRight)
            self.left.setMinimumWidth(self.left_min_width)
            self.left.setMaximumWidth(self.left_max_width)
            self.right.setMinimumWidth(0)
            self.layout.addWidget(self.left, 0)
            self.layout.addWidget(self.right, 1)

        self._stacked = stacked


class ResponsiveSplitter(QSplitter):
    def __init__(
        self,
        collapse_width: int = 900,
        wide_sizes: list[int] | None = None,
        stacked_sizes: list[int] | None = None,
    ) -> None:
        super().__init__(Qt.Orientation.Horizontal)
        self.collapse_width = collapse_width
        self.wide_sizes = wide_sizes or [420, 760]
        self.stacked_sizes = stacked_sizes or [260, 620]
        self._responsive_orientation = self.orientation()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.update_responsive_orientation()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.update_responsive_orientation()

    def update_responsive_orientation(self) -> None:
        orientation = Qt.Orientation.Vertical if self.width() < self.collapse_width else Qt.Orientation.Horizontal
        if orientation == self._responsive_orientation:
            return
        self.setOrientation(orientation)
        self.setSizes(self.stacked_sizes if orientation == Qt.Orientation.Vertical else self.wide_sizes)
        self._responsive_orientation = orientation


def fixed_two_column_grid(widgets: list[QWidget]) -> QWidget:
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
    bridge = _result_bridge() if on_done else None

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
        if on_done and bridge is not None:
            bridge.completed.emit(on_done, ok, message)

    threading.Thread(target=worker, daemon=True).start()
