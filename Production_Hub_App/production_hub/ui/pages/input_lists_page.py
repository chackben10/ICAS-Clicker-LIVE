from __future__ import annotations

import json
import re
from typing import Any

from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from production_hub.core.config.input_lists import (
    all_input_lists,
    display_cell,
    input_list_by_key,
    poll_input_list_by_key,
    poll_input_list_row_by_key,
    row_cell,
    static_cell,
)
from production_hub.core.config.models import (
    InputListCell,
    InputListColumn,
    InputListDefinition,
    InputListRow,
    MacroMapping,
    ObsLookRuleConfig,
    ServiceLogoMapping,
)
from production_hub.core.endpoints.catalog import ACTION_SPECS
from production_hub.ui.pages.common import PAGE_MARGIN, configure_table, run_background, set_table_row, title


INPUT_LIST_DATA_TYPES = ["string", "int", "float", "bool", "array_string", "array_int", "dictionary", "json"]


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", text.strip().lower()).strip("_")
    return slug or "new_list"


def parse_array(value: object, item_type: str = "string") -> list[Any]:
    if isinstance(value, list):
        raw = value
    else:
        text = str(value or "").strip()
        raw = [item.strip() for item in text.split(",") if item.strip()]
    if item_type == "int":
        parsed = []
        for item in raw:
            try:
                parsed.append(int(item))
            except Exception:
                continue
        return parsed
    return [str(item) for item in raw]


def parse_dictionary(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items() if str(key).strip()}
    if isinstance(value, list):
        return {str(item): "" for item in value if str(item).strip()}
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return {str(key): item for key, item in parsed.items() if str(key).strip()}
    except (TypeError, ValueError):
        pass
    result: dict[str, Any] = {}
    for line in text.splitlines():
        key, separator, item = line.partition("=")
        if separator and key.strip():
            result[key.strip()] = item.strip()
    return result


def full_preview_text(value: object) -> str:
    if isinstance(value, dict):
        return "\n".join(f"{key} → {item}" for key, item in value.items())
    if isinstance(value, list):
        return "\n".join(f"{index}. {item}" for index, item in enumerate(value))
    return str(value if value is not None else "")


def one_line_preview(value: object, limit: int = 72) -> str:
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            parts.append(f"{key}: {item}")
            if len(", ".join(parts)) >= limit:
                break
        text = ", ".join(parts)
        if len(parts) < len(value):
            text += ", …"
    elif isinstance(value, list):
        parts = []
        for item in value:
            parts.append(str(item))
            if len(", ".join(parts)) >= limit:
                break
        text = ", ".join(parts)
        if len(parts) < len(value):
            text += ", …"
    else:
        text = str(value if value is not None else "")
    return text if len(text) <= limit else f"{text[: limit - 1]}..."


def preview_source(value: object, fallback: object) -> object:
    if value is None:
        return fallback
    if isinstance(value, str) and value == "":
        return fallback
    return value


class CellDetailsDialog(QDialog):
    def __init__(self, title_text: str, cell: InputListCell, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title_text)
        self.setMinimumSize(560, 360)
        layout = QVBoxLayout(self)
        details = QTextEdit()
        details.setReadOnly(True)
        if cell.mode == "polled":
            text = full_preview_text(cell.value)
        else:
            text = str(cell.value)
        details.setPlainText(text)
        layout.addWidget(details)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(close)
        layout.addLayout(row)


class StaticCellDialog(QDialog):
    def __init__(self, title_text: str, cell: InputListCell, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title_text)
        self.setMinimumSize(520, 320)
        self.editor = QTextEdit()
        if isinstance(cell.value, dict):
            self.editor.setPlainText(json.dumps(cell.value, indent=2, ensure_ascii=False))
        else:
            self.editor.setPlainText(str(cell.value if cell.value is not None else ""))
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Static Value"))
        layout.addWidget(self.editor)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        apply = QPushButton("Apply")
        apply.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(apply)
        layout.addLayout(buttons)

    def value(self) -> str:
        return self.editor.toPlainText().strip()


class PolledCellDialog(QDialog):
    def __init__(self, cell: InputListCell, data_type: str = "string", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Polled Cell")
        self.setMinimumWidth(720)
        self.url = QLineEdit(cell.url)
        self.json_path = QLineEdit(cell.json_path)
        self.json_key_path = QLineEdit(cell.json_key_path or cell.json_path)
        self.json_value_path = QLineEdit(cell.json_value_path)
        self.dictionary_mode = data_type == "dictionary"
        self.url.setMinimumWidth(480)
        self.json_path.setMinimumWidth(480)
        self.json_key_path.setMinimumWidth(480)
        self.json_value_path.setMinimumWidth(480)
        self.url.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.json_path.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.current_value = cell.value
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFixedHeight(160)
        self.preview.setPlainText(full_preview_text(preview_source(cell.value, cell.preview)))

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.addRow("GET URL", self.url)
        if self.dictionary_mode:
            form.addRow("Key JSON path", self.json_key_path)
            form.addRow("Value JSON path", self.json_value_path)
        else:
            form.addRow("JSON path", self.json_path)
        layout.addLayout(form)
        if self.dictionary_mode:
            message = "Both paths must return arrays in the same order. Production Hub stores each key-path item with the value-path item at the same index."
        else:
            message = "The URL and JSON path define how Production Hub fills this cell. The preview updates after polling."
        help_text = QLabel(message)
        help_text.setObjectName("HelpText")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        layout.addWidget(QLabel("Preview"))
        layout.addWidget(self.preview)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        apply = QPushButton("Apply")
        apply.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(apply)
        layout.addLayout(buttons)

    def cell(self) -> InputListCell:
        return InputListCell(
            mode="polled",
            value=self.current_value,
            url=self.url.text().strip(),
            json_path="" if self.dictionary_mode else self.json_path.text().strip(),
            json_key_path=self.json_key_path.text().strip() if self.dictionary_mode else "",
            json_value_path=self.json_value_path.text().strip() if self.dictionary_mode else "",
        )


def cell_value_from_preview(text: str) -> object:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if all(re.match(r"^\d+\.\s+", line) for line in lines):
        return [re.sub(r"^\d+\.\s+", "", line) for line in lines]
    return text.strip()


class ResponsiveInputListsPane(QWidget):
    def __init__(self, left: QWidget, editor: QWidget, collapse_width: int = 1150) -> None:
        super().__init__()
        self.left = left
        self.editor = editor
        self.collapse_width = collapse_width
        self._stacked: bool | None = None

        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(PAGE_MARGIN, 0, PAGE_MARGIN, PAGE_MARGIN)
        self.root.setSpacing(0)

        self.wide_body = QWidget()
        self.wide_layout = QHBoxLayout(self.wide_body)
        self.wide_layout.setContentsMargins(0, 0, 0, 0)
        self.wide_layout.setSpacing(18)

        self.editor_scroll = QScrollArea()
        self.editor_scroll.setWidgetResizable(True)
        self.editor_scroll.setObjectName("PageScroll")
        self.editor_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.editor_scroll.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.editor_body = QWidget()
        self.editor_body.setObjectName("BuilderEditor")
        self.editor_layout = QVBoxLayout(self.editor_body)
        self.editor_layout.setContentsMargins(0, 0, 0, 0)
        self.editor_layout.setSpacing(14)
        self.editor_scroll.setWidget(self.editor_body)

        self.stacked_scroll = QScrollArea()
        self.stacked_scroll.setWidgetResizable(True)
        self.stacked_scroll.setObjectName("PageScroll")
        self.stacked_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.stacked_scroll.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.stacked_body = QWidget()
        self.stacked_body.setObjectName("BuilderEditor")
        self.stacked_layout = QVBoxLayout(self.stacked_body)
        self.stacked_layout.setContentsMargins(0, 0, 0, 0)
        self.stacked_layout.setSpacing(14)
        self.stacked_scroll.setWidget(self.stacked_body)

        self.reflow()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.reflow()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.reflow()

    def clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)

    def reflow(self) -> None:
        stacked = self.width() < self.collapse_width
        if stacked == self._stacked and self.root.count():
            return
        self.clear_layout(self.root)
        self.clear_layout(self.wide_layout)
        self.clear_layout(self.editor_layout)
        self.clear_layout(self.stacked_layout)
        if stacked:
            self.left.setMinimumWidth(0)
            self.left.setMaximumWidth(16777215)
            self.stacked_layout.addWidget(self.left, 0, Qt.AlignmentFlag.AlignTop)
            self.stacked_layout.addWidget(self.editor, 0, Qt.AlignmentFlag.AlignTop)
            self.stacked_layout.addStretch()
            self.root.addWidget(self.stacked_scroll)
        else:
            self.left.setMinimumWidth(420)
            self.left.setMaximumWidth(620)
            self.editor_layout.addWidget(self.editor, 0, Qt.AlignmentFlag.AlignTop)
            self.editor_layout.addStretch()
            self.wide_layout.addWidget(self.left, 0, Qt.AlignmentFlag.AlignTop)
            self.wide_layout.addWidget(self.editor_scroll, 1)
            self.root.addWidget(self.wide_body)
        self._stacked = stacked
        self.sync_content_heights()

    def sync_content_heights(self) -> None:
        editor_height = max(self.editor.minimumSizeHint().height(), self.editor.sizeHint().height())
        self.editor.setMinimumHeight(editor_height)
        self.editor.updateGeometry()

        active_body = self.stacked_body if self._stacked else self.editor_body
        inactive_body = self.editor_body if self._stacked else self.stacked_body
        active_height = max(active_body.minimumSizeHint().height(), active_body.sizeHint().height())
        active_body.setMinimumHeight(active_height)
        active_body.setMaximumHeight(16777215)
        active_body.updateGeometry()
        inactive_body.setMinimumHeight(0)
        inactive_body.updateGeometry()

        self.wide_body.setMinimumHeight(0)
        self.wide_body.updateGeometry()
        self.editor_scroll.updateGeometry()
        self.stacked_scroll.updateGeometry()


class InputListsPage(QWidget):
    def __init__(self, context) -> None:
        super().__init__()
        self.context = context
        self.lists_table = QTableWidget()
        self.columns_table = QTableWidget()
        self.rows_table = QTableWidget()
        self.status = QLabel("Ready")
        self.status.setObjectName("StatusText")
        self.name_edit = QLineEdit()
        self.description_edit = QLineEdit()
        self.polling_rate_edit = QLineEdit()
        self.current_key = ""
        self.current_builtin = False
        self.current_columns: list[InputListColumn] = []
        self.current_rows: list[InputListRow] = []
        self._last_ui_definition: InputListDefinition | None = None
        self._applying_undo = False
        self._updating_table = False
        self._loading = False
        self._refit_pending = False
        self.build()
        self.refresh_lists()

    def event(self, event) -> bool:
        result = super().event(event)
        if event.type() in {QEvent.Type.Show, QEvent.Type.Resize}:
            self.schedule_refit()
        return result

    def schedule_refit(self) -> None:
        if self._refit_pending:
            return
        self._refit_pending = True

        def run() -> None:
            if not isValid(self):
                return
            self._refit_pending = False
            if self.isVisible():
                self.refit_tables()

        QTimer.singleShot(0, run)

    def config_snapshot(self) -> dict[str, Any]:
        cfg = self.context.config
        return {
            "input_lists": [item.to_dict() for item in cfg.ui.input_lists],
            "audio_playlists": list(cfg.integrations.propresenter.audio.playlists),
            "audio_cache_ttl_seconds": cfg.integrations.propresenter.audio.cache_ttl_seconds,
            "service_logos": [item.to_dict() for item in cfg.integrations.propresenter.service_logos],
            "macros": [item.to_dict() for item in cfg.integrations.propresenter.macros],
            "obs_look_rules": [item.to_dict() for item in cfg.integrations.obs.look_rules],
            "obs_scenes": list(cfg.integrations.obs.known_scenes),
            "input_lists_initialized": cfg.ui.input_lists_initialized,
        }

    def apply_config_snapshot(self, snapshot: dict[str, Any], message: str = "") -> None:
        cfg = self.context.config
        cfg.ui.input_lists = [InputListDefinition.from_dict(item) for item in snapshot["input_lists"]]
        cfg.integrations.propresenter.audio.playlists = list(snapshot["audio_playlists"])
        cfg.integrations.propresenter.audio.cache_ttl_seconds = float(snapshot["audio_cache_ttl_seconds"])
        cfg.integrations.propresenter.service_logos = [ServiceLogoMapping.from_dict(item) for item in snapshot["service_logos"]]
        cfg.integrations.propresenter.macros = [MacroMapping.from_dict(item) for item in snapshot["macros"]]
        cfg.integrations.obs.look_rules = [ObsLookRuleConfig.from_dict(item) for item in snapshot["obs_look_rules"]]
        cfg.integrations.obs.known_scenes = list(snapshot["obs_scenes"])
        cfg.ui.input_lists_initialized = bool(snapshot.get("input_lists_initialized", cfg.ui.input_lists_initialized))
        self.context.config_repository.save_app_config(cfg)
        self.refresh_lists(self.current_key)
        if message:
            self.status.setText(message)

    def record_config_change(self, label: str, before: dict[str, Any], after: dict[str, Any]) -> None:
        if before == after:
            return
        self.context.undo_manager.record(
            label,
            lambda: self.apply_config_snapshot(before, f"Undid: {label}"),
            lambda: self.apply_config_snapshot(after, f"Redid: {label}"),
        )

    def ui_definition_snapshot(self, apply_columns: bool = False) -> InputListDefinition:
        return InputListDefinition.from_dict(self.current_definition(apply_columns=apply_columns).to_dict())

    def apply_ui_definition(self, definition: InputListDefinition, message: str = "") -> None:
        self._applying_undo = True
        try:
            self.current_key = definition.key
            self.current_builtin = False
            self.name_edit.setText(definition.name)
            self.description_edit.setText(definition.description)
            self.polling_rate_edit.setText(str(definition.polling_rate_seconds or ""))
            self.current_columns = [InputListColumn.from_dict(column.to_dict()) for column in definition.columns]
            self.current_rows = [InputListRow.from_dict(row.to_dict()) for row in definition.rows]
            self.load_columns_table()
            self.load_rows_table()
            self._last_ui_definition = InputListDefinition.from_dict(definition.to_dict())
        finally:
            self._applying_undo = False
        self.persist_current_definition(definition)
        if message:
            self.status.setText(message)

    def record_ui_change(self, label: str, before: InputListDefinition, after: InputListDefinition) -> None:
        before_data = before.to_dict()
        after_data = after.to_dict()
        self._last_ui_definition = InputListDefinition.from_dict(after_data)
        if before_data == after_data or self._applying_undo:
            return
        self.context.undo_manager.record(
            label,
            lambda: self.apply_ui_definition(InputListDefinition.from_dict(before_data), f"Undid: {label}"),
            lambda: self.apply_ui_definition(InputListDefinition.from_dict(after_data), f"Redid: {label}"),
        )
        self.persist_current_definition(after)

    def persist_current_definition(self, item: InputListDefinition) -> None:
        if self._applying_undo or not item.key:
            return
        item.builtin = False
        self.save_known_list(item)
        custom = [candidate for candidate in self.custom_lists() if candidate.key != item.key]
        custom.append(item)
        self.context.config.ui.input_lists = custom
        self.context.config_repository.save_app_config(self.context.config)

    def build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 20, 0, 0)
        root.setSpacing(14)
        title_block = title(
            "Input Lists",
            "Configurable data tables for endpoint inputs and action parameters. Rows can mix static values and polled cells.",
        )
        title_block.setContentsMargins(PAGE_MARGIN, 0, PAGE_MARGIN, 0)
        root.addWidget(
            title_block
        )

        self.configure_lists_table()
        left = QWidget()
        left.setObjectName("BuilderSidebarPanel")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(14, 14, 14, 14)
        left_layout.setSpacing(10)
        header = QLabel("Input Lists")
        header.setObjectName("BuilderPanelTitle")
        left_layout.addWidget(header)
        left_layout.addWidget(self.lists_table)
        left_buttons = QHBoxLayout()
        for label, handler in [("New", self.new_list), ("Delete", self.delete_current)]:
            button = QPushButton(label)
            button.clicked.connect(handler)
            if label == "Delete":
                button.setObjectName("DangerButton")
            left_buttons.addWidget(button)
        left_layout.addLayout(left_buttons)
        left.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        editor = QWidget()
        editor.setObjectName("BuilderEditor")
        editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(14)
        editor_layout.addWidget(self.details_section())
        editor_layout.addWidget(self.columns_section())
        editor_layout.addWidget(self.rows_section())
        self.status.hide()
        root.addWidget(ResponsiveInputListsPane(left, editor, collapse_width=1150), 1)

    def configure_lists_table(self) -> None:
        self.lists_table.setColumnCount(1)
        self.lists_table.setHorizontalHeaderLabels(["List"])
        configure_table(self.lists_table)
        self.lists_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.lists_table.itemSelectionChanged.connect(self.selection_changed)
        self.lists_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.lists_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def details_section(self) -> QWidget:
        box = QWidget()
        box.setObjectName("BuilderSection")
        box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        heading = QLabel("List Settings")
        heading.setObjectName("InlineSectionLabel")
        layout.addWidget(heading)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for editor in [self.name_edit, self.description_edit, self.polling_rate_edit]:
            editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            editor.editingFinished.connect(self.detail_field_changed)
        form.addRow("Name", self.name_edit)
        form.addRow("Description", self.description_edit)
        self.polling_rate_edit.setPlaceholderText("Only used when one or more cells are polled")
        form.addRow("Polling rate seconds", self.polling_rate_edit)
        layout.addLayout(form)
        help_text = QLabel("The visible list defines the server-facing data contract. Keys are managed internally so users do not need to think in code identifiers.")
        help_text.setObjectName("HelpText")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        poll_now = QPushButton("Poll Now")
        poll_now.clicked.connect(self.poll_now)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(poll_now)
        layout.addLayout(row)
        return box

    def columns_section(self) -> QWidget:
        box = QWidget()
        box.setObjectName("BuilderSection")
        box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        heading = QLabel("Table Configuration")
        heading.setObjectName("InlineSectionLabel")
        layout.addWidget(heading)
        self.columns_table.setColumnCount(2)
        self.columns_table.setHorizontalHeaderLabels(["Column Title", "Data Type"])
        configure_table(self.columns_table)
        self.columns_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.columns_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.columns_table.itemChanged.connect(lambda _item: self.auto_apply_columns())
        self.columns_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.columns_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.columns_table)
        buttons = QHBoxLayout()
        for label, handler in [("Add Column", self.add_column), ("Delete Column", self.delete_column)]:
            button = QPushButton(label)
            button.clicked.connect(handler)
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        return box

    def rows_section(self) -> QWidget:
        box = QWidget()
        box.setObjectName("BuilderSection")
        box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        heading = QLabel("Rows")
        heading.setObjectName("InlineSectionLabel")
        layout.addWidget(heading)
        buttons = QHBoxLayout()
        for label, handler in [("Add Row", self.add_row), ("Delete Row", self.delete_row)]:
            button = QPushButton(label)
            button.clicked.connect(handler)
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.rows_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.rows_table.customContextMenuRequested.connect(self.show_cell_menu)
        self.rows_table.itemDoubleClicked.connect(lambda item: self.edit_or_show_cell(item.row(), item.column()))
        self.rows_table.itemChanged.connect(lambda item: self.row_item_changed(item))
        self.rows_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.rows_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        configure_table(self.rows_table)
        layout.addWidget(self.rows_table)
        return box

    def refresh_lists(self, select_key: str = "") -> None:
        self._loading = True
        lists = all_input_lists(self.context.config)
        self.lists_table.setRowCount(len(lists))
        for row, item in enumerate(lists):
            set_table_row(self.lists_table, row, [item.name])
            self.lists_table.item(row, 0).setFlags(
                self.lists_table.item(row, 0).flags() & ~Qt.ItemFlag.ItemIsEditable
            )
            self.lists_table.item(row, 0).setData(Qt.ItemDataRole.UserRole, item.key)
        self.fit_table_height(self.lists_table)
        self._loading = False
        target = select_key or self.current_key
        if target:
            self.select_key(target)
        elif lists:
            self.lists_table.selectRow(0)
        self.schedule_refit()

    def selection_changed(self) -> None:
        if self._loading:
            return
        row = self.lists_table.currentRow()
        if row < 0 or not self.lists_table.item(row, 0):
            return
        key = self.lists_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        item = next((candidate for candidate in all_input_lists(self.context.config) if candidate.key == key), None)
        if item:
            self.load_list(item)

    def load_list(self, item: InputListDefinition) -> None:
        self._loading = True
        self.current_key = item.key
        self.current_builtin = False
        self.current_columns = [InputListColumn.from_dict(column.to_dict()) for column in item.columns]
        self.current_rows = [InputListRow.from_dict(row.to_dict()) for row in item.rows]
        self.name_edit.setText(item.name)
        self.description_edit.setText(item.description)
        self.polling_rate_edit.setText(str(item.polling_rate_seconds or ""))
        self.load_columns_table()
        self.load_rows_table()
        self._last_ui_definition = self.ui_definition_snapshot()
        self._loading = False
        self.status.setText(f"Editing {item.name}.")
        self.schedule_refit()

    def detail_field_changed(self) -> None:
        if self._loading or self._applying_undo:
            return
        before = self._last_ui_definition or self.ui_definition_snapshot()
        self.record_ui_change("Edit input-list settings", before, self.ui_definition_snapshot())

    def load_columns_table(self) -> None:
        self.columns_table.setRowCount(0)
        for column in self.current_columns:
            row = self.columns_table.rowCount()
            self.columns_table.insertRow(row)
            title = QTableWidgetItem(column.title)
            title.setData(Qt.ItemDataRole.UserRole, column.key)
            title.setData(Qt.ItemDataRole.UserRole + 1, column.role)
            self.columns_table.setItem(row, 0, title)
            type_combo = QComboBox()
            type_combo.addItems(INPUT_LIST_DATA_TYPES)
            type_combo.setCurrentText(column.data_type)
            type_combo.currentIndexChanged.connect(lambda _index: self.auto_apply_columns())
            self.columns_table.setCellWidget(row, 1, type_combo)
        self.fit_table_height(self.columns_table)

    def load_rows_table(self) -> None:
        self.rows_table.setColumnCount(len(self.current_columns) + 1)
        self.rows_table.setHorizontalHeaderLabels([""] + [column.title for column in self.current_columns])
        self.rows_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for column_index in range(1, self.rows_table.columnCount()):
            self.rows_table.horizontalHeader().setSectionResizeMode(column_index, QHeaderView.ResizeMode.Stretch)
        self.rows_table.setRowCount(0)
        for row_def in self.current_rows:
            self.insert_row(row_def)
        self.fit_table_height(self.rows_table)

    def insert_row(self, row_def: InputListRow | None = None) -> None:
        row_def = row_def or InputListRow(True, {column.key: InputListCell() for column in self.current_columns})
        row = self.rows_table.rowCount()
        self.rows_table.insertRow(row)
        check = QCheckBox()
        check.setChecked(row_def.enabled)
        check.setToolTip("Enabled")
        check.stateChanged.connect(lambda _state, row_index=row: self.set_row_enabled(row_index))
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(check, 0, Qt.AlignmentFlag.AlignCenter)
        self.rows_table.setCellWidget(row, 0, holder)
        for index, column in enumerate(self.current_columns, start=1):
            cell = row_cell(row_def, column.key)
            self.set_table_cell(row, index, cell)
        self.rows_table.resizeRowsToContents()
        self.fit_table_height(self.rows_table)

    def fit_table_height(self, table: QTableWidget) -> None:
        table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.setWordWrap(True)
        table.resizeRowsToContents()
        for row in range(table.rowCount()):
            row_height = max(table.sizeHintForRow(row), table.rowHeight(row), 30)
            for column in range(table.columnCount()):
                widget = table.cellWidget(row, column)
                if widget is not None:
                    row_height = max(row_height, widget.sizeHint().height() + 14)
            table.setRowHeight(row, row_height)
        header = table.horizontalHeader().height() if table.horizontalHeader().isVisible() else 0
        rows = sum(table.rowHeight(row) for row in range(table.rowCount()))
        frame = table.frameWidth() * 2
        margins = table.contentsMargins()
        height = max(header + rows + frame + margins.top() + margins.bottom() + 18, header + 56)
        table.setFixedHeight(height)
        table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        table.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)
        self.fit_section_for_table(table)

    def fit_section_for_table(self, table: QTableWidget) -> None:
        section = table.parentWidget()
        if section is None or not isValid(section):
            return
        section.layout().activate() if section.layout() is not None else None
        section_height = section.sizeHint().height()
        section.setMinimumHeight(section_height)
        section.setMaximumHeight(section_height)
        section.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        section.updateGeometry()
        parent = section.parentWidget()
        if parent is not None and isValid(parent) and parent.layout() is not None:
            parent.layout().invalidate()
            parent.layout().activate()
            parent.updateGeometry()

    def refit_tables(self) -> None:
        for table in (self.lists_table, self.columns_table, self.rows_table):
            if not isValid(table):
                return
            self.fit_table_height(table)
        for widget in (self.lists_table.parentWidget(), self.columns_table.parentWidget(), self.rows_table.parentWidget()):
            if widget is not None and isValid(widget):
                widget.updateGeometry()
        editor = self.rows_table.parentWidget().parentWidget() if self.rows_table.parentWidget() is not None else None
        if editor is not None and isValid(editor) and editor.layout() is not None:
            editor.layout().invalidate()
            editor.layout().activate()
            editor.updateGeometry()
        self.updateGeometry()
        pane = self.findChild(ResponsiveInputListsPane)
        if pane is not None:
            pane.sync_content_heights()

    def cell_tooltip(self, cell: InputListCell) -> str:
        if cell.mode == "polled":
            if cell.json_key_path or cell.json_value_path:
                return (
                    f"GET {cell.url}\nKey JSON path: {cell.json_key_path}\n"
                    f"Value JSON path: {cell.json_value_path}\nDouble-click to inspect."
                )
            return f"GET {cell.url}\nJSON path: {cell.json_path}\nDouble-click to inspect."
        return "Double-click to inspect."

    def add_column(self) -> None:
        before = self.ui_definition_snapshot()
        row = self.columns_table.rowCount()
        self.columns_table.insertRow(row)
        title = QTableWidgetItem("New Column")
        title.setData(Qt.ItemDataRole.UserRole, "")
        title.setData(Qt.ItemDataRole.UserRole + 1, "" if self.columns_table.rowCount() else "label")
        self.columns_table.setItem(row, 0, title)
        type_combo = QComboBox()
        type_combo.addItems(INPUT_LIST_DATA_TYPES)
        type_combo.currentIndexChanged.connect(lambda _index: self.auto_apply_columns())
        self.columns_table.setCellWidget(row, 1, type_combo)
        self.fit_table_height(self.columns_table)
        self.auto_apply_columns()
        self.record_ui_change("Add input-list column", before, self.ui_definition_snapshot())

    def delete_column(self) -> None:
        row = self.columns_table.currentRow()
        if row >= 0:
            before = self.ui_definition_snapshot()
            self.columns_table.removeRow(row)
            self.fit_table_height(self.columns_table)
            self.auto_apply_columns()
            self.record_ui_change("Delete input-list column", before, self.ui_definition_snapshot())

    def apply_columns(self) -> None:
        old_rows = self.rows_from_table()
        new_columns = self.columns_from_table()
        for row_def in old_rows:
            for column in new_columns:
                row_def.cells.setdefault(column.key, InputListCell())
            row_def.cells = {column.key: row_def.cells.get(column.key, InputListCell()) for column in new_columns}
        self.current_columns = new_columns
        self.current_rows = old_rows
        self.load_rows_table()
        self.status.setText("Table columns applied.")

    def auto_apply_columns(self) -> None:
        if self._loading or self._applying_undo:
            return
        before = self._last_ui_definition or self.ui_definition_snapshot()
        self.apply_columns()
        self.record_ui_change("Edit input-list columns", before, self.ui_definition_snapshot())

    def columns_from_table(self) -> list[InputListColumn]:
        columns = []
        used: set[str] = set()
        for row in range(self.columns_table.rowCount()):
            title_item = self.columns_table.item(row, 0)
            title = str(title_item.text() if title_item else "").strip()
            if not title:
                continue
            key = str(title_item.data(Qt.ItemDataRole.UserRole) or "").strip() or slugify(title)
            role = str(title_item.data(Qt.ItemDataRole.UserRole + 1) or "").strip()
            if not role and not columns:
                role = "label"
            base = key
            number = 2
            while key in used:
                key = f"{base}_{number}"
                number += 1
            used.add(key)
            type_widget = self.columns_table.cellWidget(row, 1)
            columns.append(
                InputListColumn(
                    key,
                    title,
                    type_widget.currentText() if isinstance(type_widget, QComboBox) else "string",
                    role,
                )
            )
        return columns or [InputListColumn("value", "Value", "string", "label")]

    def add_row(self) -> None:
        before = self.ui_definition_snapshot()
        self.insert_row()
        self.current_rows = self.rows_from_table()
        self.record_ui_change("Add input-list row", before, self.ui_definition_snapshot())

    def delete_row(self) -> None:
        row = self.rows_table.currentRow()
        if row >= 0:
            before = self.ui_definition_snapshot()
            self.rows_table.removeRow(row)
            self.fit_table_height(self.rows_table)
            self.current_rows = self.rows_from_table()
            self.record_ui_change("Delete input-list row", before, self.ui_definition_snapshot())

    def set_row_enabled(self, row: int) -> None:
        if row < self.rows_table.rowCount() and not self._loading and not self._applying_undo:
            before = self._last_ui_definition or self.ui_definition_snapshot()
            self.current_rows = self.rows_from_table()
            after = self.ui_definition_snapshot()
            was_enabled = row < len(before.rows) and before.rows[row].enabled
            is_enabled = row < len(after.rows) and after.rows[row].enabled
            self.record_ui_change("Toggle input-list row", before, after)
            if is_enabled and not was_enabled and any(
                cell.mode == "polled" for cell in after.rows[row].cells.values()
            ):
                self.poll_enabled_row(after.key, row)
            else:
                self.status.setText("Row enabled state changed.")

    def show_cell_menu(self, position) -> None:
        row = self.rows_table.indexAt(position).row()
        column = self.rows_table.indexAt(position).column()
        if row < 0 or column <= 0:
            return
        self.rows_table.setCurrentCell(row, column)
        self.show_cell_menu_at(row, column, self.rows_table.viewport().mapToGlobal(position))

    def show_cell_menu_at(self, row: int, column: int, global_position) -> None:
        cell = self.cell_from_table(row, column)
        menu = QMenu(self)
        edit_action = None
        if cell.mode == "polled":
            edit_action = menu.addAction("Edit Polling Cell")
            menu.addSeparator()
        mode_menu = menu.addMenu("Cell Type")
        static_action = mode_menu.addAction("Static")
        static_action.setCheckable(True)
        static_action.setChecked(cell.mode == "static")
        polled_action = mode_menu.addAction("Polling")
        polled_action.setCheckable(True)
        polled_action.setChecked(cell.mode == "polled")
        inspect_action = menu.addAction("Show Full Value")
        selected = menu.exec(global_position)
        if selected is None:
            return
        if selected == edit_action or selected == polled_action:
            self.open_polled_cell_editor(row, column)
        elif selected == static_action:
            self.set_static_cell(row, column)
        elif selected == inspect_action:
            self.show_cell_details(row, column)

    def set_static_cell(self, row: int, column: int) -> None:
        before = self.ui_definition_snapshot()
        data_type = self.current_columns[column - 1].data_type if column - 1 < len(self.current_columns) else "string"
        if data_type == "dictionary":
            value = parse_dictionary(self.cell_from_table(row, column).value)
        else:
            item = self.rows_table.item(row, column)
            value = item.text().splitlines()[-1] if item else ""
        cell = InputListCell("static", value)
        self.set_table_cell(row, column, cell)
        self.current_rows = self.rows_from_table()
        self.record_ui_change("Set input-list cell static", before, self.ui_definition_snapshot())

    def set_polled_cell(self, row: int, column: int) -> None:
        cell = self.cell_from_table(row, column)
        data_type = self.current_columns[column - 1].data_type if column - 1 < len(self.current_columns) else "string"
        dialog = PolledCellDialog(cell, data_type, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            before = self.ui_definition_snapshot()
            self.set_table_cell(row, column, dialog.cell())
            self.polling_rate_edit.setText(self.polling_rate_edit.text().strip() or "60")
            self.current_rows = self.rows_from_table()
            self.record_ui_change("Edit input-list polling cell", before, self.ui_definition_snapshot())

    def open_polled_cell_editor(self, row: int, column: int) -> None:
        QTimer.singleShot(0, lambda: self.set_polled_cell(row, column))

    def set_table_cell(self, row: int, column: int, cell: InputListCell) -> None:
        self._updating_table = True
        self.rows_table.removeCellWidget(row, column)
        try:
            if cell.mode == "polled":
                widget = self.polled_cell_widget(row, column, cell)
                self.rows_table.setCellWidget(row, column, widget)
                item = QTableWidgetItem("")
                item.setData(Qt.ItemDataRole.UserRole, cell.to_dict())
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setToolTip(self.cell_tooltip(cell))
                self.rows_table.setItem(row, column, item)
                self.rows_table.resizeRowToContents(row)
                self.fit_table_height(self.rows_table)
                return
            item = self.rows_table.item(row, column) or QTableWidgetItem()
            item.setText(display_cell(cell))
            item.setData(Qt.ItemDataRole.UserRole, cell.to_dict())
            item.setToolTip(self.cell_tooltip(cell))
            data_type = self.current_columns[column - 1].data_type if column - 1 < len(self.current_columns) else "string"
            if data_type == "dictionary":
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            else:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.rows_table.setItem(row, column, item)
            self.fit_table_height(self.rows_table)
        finally:
            self._updating_table = False

    def row_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading or self._applying_undo or self._updating_table or item.column() <= 0:
            return
        cell = self.cell_from_table(item.row(), item.column())
        if cell.mode != "static":
            return
        before = self._last_ui_definition or self.ui_definition_snapshot()
        self.current_rows = self.rows_from_table()
        self.record_ui_change("Edit input-list row cell", before, self.ui_definition_snapshot())

    def polled_cell_widget(self, row: int, column: int, cell: InputListCell) -> QWidget:
        widget = QWidget()
        widget.setProperty("cell", cell.to_dict())
        widget.setToolTip(self.cell_tooltip(cell))
        widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        widget.customContextMenuRequested.connect(lambda position: self.show_cell_menu_at(row, column, widget.mapToGlobal(position)))
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(3)
        url = QLabel(f"<b>Polling URL</b><br>{cell.url or 'Polled request not configured'}")
        url.setObjectName("PollingCellURL")
        url.setTextFormat(Qt.TextFormat.RichText)
        url.setWordWrap(False)
        url.setToolTip(self.cell_tooltip(cell))
        url.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        url.customContextMenuRequested.connect(lambda position: self.show_cell_menu_at(row, column, url.mapToGlobal(position)))
        preview = QLabel(f"<b>Preview</b>  {one_line_preview(cell.value)}")
        preview.setObjectName("PollingCellPreview")
        preview.setTextFormat(Qt.TextFormat.RichText)
        preview.setWordWrap(False)
        preview.setToolTip(full_preview_text(preview_source(cell.value, cell.preview)))
        preview_font = QFont(preview.font())
        preview_font.setPointSize(max(8, preview_font.pointSize() - 2))
        preview_font.setItalic(True)
        preview.setFont(preview_font)
        preview.setObjectName("HelpText")
        preview.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        preview.customContextMenuRequested.connect(lambda position: self.show_cell_menu_at(row, column, preview.mapToGlobal(position)))
        layout.addWidget(url)
        layout.addWidget(preview)
        return widget

    def sync_current_polled_values(self, key: str) -> None:
        if self.current_key != key:
            return
        definition = input_list_by_key(self.context.config, key)
        if definition is None:
            return
        if len(definition.rows) != self.rows_table.rowCount() or [item.key for item in definition.columns] != [
            item.key for item in self.current_columns
        ]:
            self.load_list(definition)
            return

        self._loading = True
        try:
            self.current_rows = [InputListRow.from_dict(row.to_dict()) for row in definition.rows]
            for row_index, row_def in enumerate(definition.rows):
                for column_index, column in enumerate(definition.columns, start=1):
                    cell = row_cell(row_def, column.key)
                    if cell.mode != "polled":
                        continue
                    widget = self.rows_table.cellWidget(row_index, column_index)
                    item = self.rows_table.item(row_index, column_index)
                    if widget is not None:
                        widget.setProperty("cell", cell.to_dict())
                        widget.setToolTip(self.cell_tooltip(cell))
                        preview = widget.findChild(QLabel, "PollingCellPreview")
                        if preview is not None:
                            preview.setText(f"<b>Preview</b>  {one_line_preview(cell.value)}")
                            preview.setToolTip(full_preview_text(preview_source(cell.value, cell.preview)))
                    if item is not None:
                        item.setData(Qt.ItemDataRole.UserRole, cell.to_dict())
                        item.setToolTip(self.cell_tooltip(cell))
            self._last_ui_definition = InputListDefinition.from_dict(definition.to_dict())
        finally:
            self._loading = False

    def poll_enabled_row(self, key: str, row: int) -> None:
        label = "row"
        if row < len(self.current_rows):
            label = str(row_cell(self.current_rows[row], "library_name").value or f"row {row + 1}")
        self.status.setText(f"{label} enabled. Loading its values...")

        async def run():
            changed = await poll_input_list_row_by_key(self.context, key, row)
            return "updated" if changed else "unchanged"

        def done(ok: bool, message: str) -> None:
            if ok:
                self.sync_current_polled_values(key)
                self.status.setText(f"{label} enabled. Search data is ready.")
            else:
                self.status.setText(f"{label} enabled, but polling failed: {message}")

        run_background(run, done)

    def cell_from_table(self, row: int, column: int) -> InputListCell:
        widget = self.rows_table.cellWidget(row, column)
        if widget and widget.property("cell"):
            return InputListCell.from_dict(widget.property("cell"))
        item = self.rows_table.item(row, column)
        if item:
            data = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(data, dict):
                cell = InputListCell.from_dict(data)
                data_type = self.current_columns[column - 1].data_type if column - 1 < len(self.current_columns) else "string"
                if cell.mode == "static" and data_type != "dictionary":
                    cell.value = item.text().strip()
                return cell
            return InputListCell("static", item.text().strip())
        return InputListCell()

    def show_cell_details(self, row: int, column: int) -> None:
        if row < 0 or column <= 0:
            return
        header = self.rows_table.horizontalHeaderItem(column).text()
        cell = self.cell_from_table(row, column)
        if cell.mode == "polled":
            CellDetailsDialog(f"{header} Preview", cell, self).exec()
            return
        dialog = StaticCellDialog(header, cell, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            before = self.ui_definition_snapshot()
            self.set_table_cell(row, column, InputListCell("static", dialog.value()))
            self.current_rows = self.rows_from_table()
            self.record_ui_change("Edit input-list static value", before, self.ui_definition_snapshot())

    def edit_or_show_cell(self, row: int, column: int) -> None:
        if row < 0 or column <= 0:
            return
        cell = self.cell_from_table(row, column)
        if cell.mode == "polled":
            self.set_polled_cell(row, column)
            return
        self.show_cell_details(row, column)

    def rows_from_table(self) -> list[InputListRow]:
        rows = []
        for row_index in range(self.rows_table.rowCount()):
            enabled = True
            holder = self.rows_table.cellWidget(row_index, 0)
            if holder:
                checkbox = holder.findChild(QCheckBox)
                enabled = checkbox.isChecked() if checkbox else True
            cells = {}
            for column_index, column in enumerate(self.current_columns, start=1):
                cell = self.cell_from_table(row_index, column_index)
                cell.value = self.coerce_value(cell.value, column.data_type)
                cells[column.key] = cell
            rows.append(InputListRow(enabled, cells))
        return rows

    def coerce_value(self, value: object, data_type: str) -> object:
        if data_type == "array_string":
            return parse_array(value, "string")
        if data_type == "array_int":
            return parse_array(value, "int")
        if data_type == "dictionary":
            return parse_dictionary(value)
        if data_type == "int":
            try:
                return int(str(value).strip())
            except Exception:
                return 0
        if data_type == "float":
            try:
                return float(str(value).strip())
            except Exception:
                return 0.0
        if data_type == "bool":
            return str(value).lower() in {"1", "true", "yes", "on"}
        return value

    def current_definition(self, apply_columns: bool = True) -> InputListDefinition:
        if apply_columns:
            self.apply_columns()
        name = self.name_edit.text().strip() or "New List"
        key = self.current_key or slugify(name)
        try:
            polling_rate = float(self.polling_rate_edit.text().strip() or 0)
        except Exception:
            polling_rate = 0
        return InputListDefinition(
            key=key,
            name=name,
            description=self.description_edit.text().strip(),
            builtin=False,
            columns=self.current_columns,
            rows=self.rows_from_table(),
            polling_rate_seconds=polling_rate,
        )

    def save_current(self) -> None:
        if not self.current_key:
            return
        try:
            before = self.config_snapshot()
            item = self.current_definition()
            item.builtin = False
            self.save_known_list(item)
            custom = [candidate for candidate in self.custom_lists() if candidate.key != item.key]
            custom.append(item)
            self.context.config.ui.input_lists = custom
            self.context.config_repository.save_app_config(self.context.config)
            after = self.config_snapshot()
            self.record_config_change(f"Save input list {item.name}", before, after)
            self.refresh_lists(item.key)
            self.status.setText(f"Saved {item.name}.")
        except Exception as exc:
            self.status.setText(f"Save failed: {exc}")

    def poll_now(self) -> None:
        if not self.current_key:
            return
        before = self.config_snapshot()
        item = self.current_definition()
        self.current_rows = [InputListRow.from_dict(row.to_dict()) for row in item.rows]
        self._last_ui_definition = InputListDefinition.from_dict(item.to_dict())
        self.persist_current_definition(item)
        key = self.current_key
        self.status.setText("Polling list...")

        async def run():
            changed = await poll_input_list_by_key(self.context, key)
            return "Poll complete. Values updated." if changed else "Poll complete. No changes."

        def done(ok: bool, message: str) -> None:
            if ok:
                self.sync_current_polled_values(key)
                self.record_config_change(f"Poll input list {key}", before, self.config_snapshot())
            self.status.setText(message if ok else f"Poll failed: {message}")

        run_background(run, done)

    def custom_lists(self) -> list[InputListDefinition]:
        return [InputListDefinition.from_dict(item.to_dict()) for item in self.context.config.ui.input_lists]

    def save_known_list(self, item: InputListDefinition) -> None:
        cfg = self.context.config
        rows = [row_def for row_def in item.rows if row_def.enabled]
        if item.key == "audio_playlists":
            cfg.integrations.propresenter.audio.playlists = [str(row_cell(row_def, "playlist_name").value) for row_def in rows]
            cfg.integrations.propresenter.audio.cache_ttl_seconds = item.polling_rate_seconds or cfg.integrations.propresenter.audio.cache_ttl_seconds
        elif item.key == "service_logos":
            cfg.integrations.propresenter.service_logos = [
                ServiceLogoMapping(str(row_cell(row_def, "name").value), str(row_cell(row_def, "uuid").value))
                for row_def in rows
            ]
        elif item.key == "macros":
            cfg.integrations.propresenter.macros = [
                MacroMapping(str(row_cell(row_def, "macro").value), str(row_cell(row_def, "macro").value))
                for row_def in rows
            ]
        elif item.key == "obs_looks":
            existing = {rule.look_name: rule for rule in cfg.integrations.obs.look_rules}
            rules = []
            for row_def in rows:
                look_name = str(row_cell(row_def, "macro").value)
                show_ids = parse_array(row_cell(row_def, "enabled_sources").value, "int")
                existing_rule = existing.get(look_name)
                if existing_rule:
                    existing_rule.show_ids = show_ids
                    rules.append(existing_rule)
                else:
                    rules.append(ObsLookRuleConfig(look_name, cfg.integrations.obs.main_layout_scene, show_ids, []))
            cfg.integrations.obs.look_rules = rules
        elif item.key == "obs_scenes":
            cfg.integrations.obs.known_scenes = [str(row_cell(row_def, "scene").value) for row_def in rows]

    def new_list(self) -> None:
        before = self.config_snapshot()
        existing = {item.key for item in all_input_lists(self.context.config)}
        key = "new_list"
        number = 2
        while key in existing:
            key = f"new_list_{number}"
            number += 1
        item = InputListDefinition(
            key=key,
            name="New List",
            description="",
            columns=[InputListColumn("value", "Value", "string", "label")],
            rows=[InputListRow(True, {"value": static_cell("New Item")})],
        )
        self.context.config.ui.input_lists = [*self.custom_lists(), item]
        self.context.config_repository.save_app_config(self.context.config)
        self.record_config_change(f"Create input list {item.name}", before, self.config_snapshot())
        self.refresh_lists(key)
        self.status.setText("New list added.")

    def input_list_usage(self, key: str) -> list[str]:
        uses: list[str] = []
        try:
            endpoints = self.context.config_repository.load_endpoints()
        except Exception:
            endpoints = []
        for endpoint in endpoints:
            for input_def in endpoint.inputs:
                if input_def.option_source == key:
                    uses.append(f"Endpoint input: {endpoint.name} -> {input_def.label or input_def.name}")
        for spec in ACTION_SPECS:
            for field in spec.fields:
                if field.context_options == key:
                    path = " / ".join(spec.path or (spec.category,))
                    uses.append(f"Action palette: {path} -> {spec.label} -> {field.label}")
        return uses

    def confirm_delete_current(self, name: str) -> bool:
        uses = self.input_list_usage(self.current_key)
        box = QMessageBox(self)
        box.setWindowTitle("Delete Input List")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"Delete {name}?")
        if uses:
            visible = "\n".join(f"- {item}" for item in uses[:8])
            suffix = f"\n- ...and {len(uses) - 8} more" if len(uses) > 8 else ""
            box.setInformativeText(f"These places currently use this list and may need to be updated:\n{visible}{suffix}")
            box.setDetailedText("\n".join(uses))
        else:
            box.setInformativeText("No current endpoint inputs or action-palette fields reference this list.")
        delete_button = box.addButton("Delete", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return box.clickedButton() == delete_button

    def delete_current(self) -> None:
        if not self.current_key:
            return
        before = self.config_snapshot()
        name = self.name_edit.text().strip() or self.current_key
        if not self.confirm_delete_current(name):
            self.status.setText("Delete canceled.")
            return
        self.context.config.ui.input_lists = [item for item in self.custom_lists() if item.key != self.current_key]
        self.context.config_repository.save_app_config(self.context.config)
        self.record_config_change(f"Delete input list {name}", before, self.config_snapshot())
        self.current_key = ""
        self.refresh_lists()
        self.status.setText("List deleted.")

    def select_key(self, key: str) -> None:
        for row in range(self.lists_table.rowCount()):
            if self.lists_table.item(row, 0).data(Qt.ItemDataRole.UserRole) == key:
                self.lists_table.selectRow(row)
                return


def build_page(context) -> QWidget:
    return InputListsPage(context)
