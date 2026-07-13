from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from production_hub.core.automation.catalog import CONDITION_SPECS, condition_params, condition_spec
from production_hub.core.endpoints.catalog import (
    ACTION_SPECS,
    FieldSpec,
    action_options,
    action_spec,
    default_action_params,
    normalize_select_value,
)
from production_hub.core.endpoints.models import ActionDefinition
from production_hub.ui.dialogs.action_palette import ActionPaletteDialog
from production_hub.ui.pages.common import responsive_grid


def _default_params(fields: tuple[FieldSpec, ...]) -> dict[str, Any]:
    return {field.name: field.default for field in fields}


def _clear_layout(layout: QFormLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        child_layout = item.layout()
        if widget:
            widget.deleteLater()
        if child_layout:
            while child_layout.count():
                child_item = child_layout.takeAt(0)
                if child_item.widget():
                    child_item.widget().deleteLater()


class FieldSetEditor(QWidget):
    def __init__(self, context, endpoint_inputs: Callable[[], list[Any]] | None = None) -> None:
        super().__init__()
        self.context = context
        self.endpoint_inputs = endpoint_inputs or (lambda: [])
        self.fields: tuple[FieldSpec, ...] = ()
        self.widgets: dict[str, QWidget] = {}
        self.form = QFormLayout(self)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setVerticalSpacing(8)

    def set_fields(self, fields: tuple[FieldSpec, ...], values: dict[str, Any]) -> None:
        _clear_layout(self.form)
        self.fields = fields
        self.widgets = {}
        if not fields:
            label = QLabel("No settings needed for this module.")
            label.setObjectName("HelpText")
            self.form.addRow(label)
            return
        for field in fields:
            widget = self._widget_for(field, values.get(field.name, field.default))
            self.widgets[field.name] = widget
            label = field.label
            if field.help_text:
                label = f"{field.label}"
                widget.setToolTip(field.help_text)
            self.form.addRow(label, widget)

    def _widget_for(self, field: FieldSpec, value: Any) -> QWidget:
        input_options = [(input_def.label or input_def.name, f"{{{{{input_def.name}}}}}") for input_def in self.endpoint_inputs()]
        if field.kind == "bool":
            widget = QCheckBox()
            widget.setChecked(str(value).lower() in {"1", "true", "yes", "on"})
            return widget
        if field.kind == "select" or input_options:
            widget = QComboBox()
            widget.setEditable(True)
            if input_options:
                for label, template in input_options:
                    widget.addItem(f"Use input: {label}", template)
                widget.insertSeparator(len(input_options))
            widget.addItems(action_options(self.context, field))
            text = str(value or "")
            if text:
                index = next((i for i in range(widget.count()) if widget.itemData(i) == text), -1)
                if index < 0:
                    index = widget.findText(text)
                if index >= 0:
                    widget.setCurrentIndex(index)
                else:
                    widget.setEditText(text)
            return widget
        widget = QLineEdit(str(value if value is not None else ""))
        if field.help_text:
            widget.setPlaceholderText(field.help_text)
        return widget

    def values(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for field in self.fields:
            widget = self.widgets.get(field.name)
            if widget is None:
                continue
            if isinstance(widget, QCheckBox):
                out[field.name] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                data = widget.currentData()
                out[field.name] = str(data) if data else normalize_select_value(field, widget.currentText())
            elif isinstance(widget, QLineEdit):
                out[field.name] = widget.text().strip()
        return out


class ActionSequenceEditor(QWidget):
    def __init__(self, context, endpoint_inputs: Callable[[], list[Any]] | None = None) -> None:
        super().__init__()
        self.context = context
        self.endpoint_inputs = endpoint_inputs or (lambda: [])
        self._actions: list[ActionDefinition] = []
        self._current_index = -1
        self._loading = False
        self.list_widget = QListWidget()
        self.list_widget.setObjectName("BuilderStepList")
        self.list_widget.setMinimumWidth(240)
        self.list_widget.setMinimumHeight(190)
        self.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self.show_action_menu)
        self.type_combo = QComboBox()
        self.type_combo.addItems([f"{spec.category} - {spec.label}" for spec in ACTION_SPECS])
        self.description = QLabel("")
        self.description.setWordWrap(True)
        self.description.setObjectName("HelpText")
        self.fields = FieldSetEditor(context, self.endpoint_inputs)
        self.delay_spin = QDoubleSpinBox()
        self.delay_spin.setRange(0, 3600)
        self.delay_spin.setDecimals(2)
        self.delay_spin.setSuffix(" sec")
        self.retries_spin = QSpinBox()
        self.retries_spin.setRange(0, 10)
        self.retry_delay_spin = QDoubleSpinBox()
        self.retry_delay_spin.setRange(0, 60)
        self.retry_delay_spin.setDecimals(2)
        self.retry_delay_spin.setSuffix(" sec")
        self.build()

    def build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        left_panel = QWidget()
        left_panel.setObjectName("SequenceListPanel")
        left = QVBoxLayout(left_panel)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(8)
        left_title = QLabel("Steps")
        left_title.setObjectName("InlineSectionLabel")
        left.addWidget(left_title)
        left.addWidget(self.list_widget)
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        for label, handler in [
            ("Add", self.add_action),
            ("Remove", self.remove_action),
            ("Up", self.move_up),
            ("Down", self.move_down),
        ]:
            button = QPushButton(label)
            button.clicked.connect(handler)
            buttons.addWidget(button)
        left.addLayout(buttons)

        right_widget = QWidget()
        right_widget.setObjectName("SequenceEditorPanel")
        right = QVBoxLayout(right_widget)
        right.setContentsMargins(12, 12, 12, 12)
        right.setSpacing(10)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(9)
        form.addRow("Module", self.type_combo)
        form.addRow("Start delay", self.delay_spin)
        form.addRow("Retries", self.retries_spin)
        form.addRow("Retry delay", self.retry_delay_spin)
        right.addLayout(form)
        right.addWidget(self.description)
        right.addWidget(self.fields)
        right.addStretch()

        root.addWidget(responsive_grid([left_panel, right_widget], min_column_width=300, max_columns=2))
        self.list_widget.currentRowChanged.connect(self.selection_changed)
        self.type_combo.currentIndexChanged.connect(self.action_type_changed)

    def set_actions(self, actions: list[ActionDefinition]) -> None:
        self._actions = [ActionDefinition.from_dict(action.to_dict()) for action in actions]
        self._current_index = -1
        self.reload_list()
        if self._actions:
            self.list_widget.setCurrentRow(0)
        else:
            self.load_action(-1)

    def actions(self) -> list[ActionDefinition]:
        self.save_current()
        return [ActionDefinition.from_dict(action.to_dict()) for action in self._actions]

    def current_spec(self):
        combo_index = max(0, self.type_combo.currentIndex())
        return ACTION_SPECS[combo_index]

    def add_action(self) -> None:
        dialog = ActionPaletteDialog(self.context, parent=self, endpoint_inputs=self.endpoint_inputs)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.selected_action:
            return
        action = dialog.selected_action
        self.save_current()
        self._actions.append(action)
        self.reload_list()
        self.list_widget.setCurrentRow(len(self._actions) - 1)

    def show_action_menu(self, position) -> None:
        row = self.list_widget.indexAt(position).row()
        if row >= 0:
            self.list_widget.setCurrentRow(row)
        menu = QMenu(self)
        add = menu.addAction("Add Action")
        add.triggered.connect(self.add_action)
        remove = menu.addAction("Remove Action")
        remove.setEnabled(row >= 0)
        remove.triggered.connect(self.remove_action)
        if row >= 0:
            menu.addSeparator()
            up = menu.addAction("Move Up")
            up.setEnabled(row > 0)
            up.triggered.connect(self.move_up)
            down = menu.addAction("Move Down")
            down.setEnabled(row < len(self._actions) - 1)
            down.triggered.connect(self.move_down)
        menu.exec(self.list_widget.viewport().mapToGlobal(position))

    def remove_action(self) -> None:
        row = self.list_widget.currentRow()
        if row < 0:
            return
        self._actions.pop(row)
        self._current_index = -1
        self.reload_list()
        self.list_widget.setCurrentRow(min(row, len(self._actions) - 1))

    def move_up(self) -> None:
        row = self.list_widget.currentRow()
        if row <= 0:
            return
        self.save_current()
        self._actions[row - 1], self._actions[row] = self._actions[row], self._actions[row - 1]
        self.reload_list()
        self.list_widget.setCurrentRow(row - 1)

    def move_down(self) -> None:
        row = self.list_widget.currentRow()
        if row < 0 or row >= len(self._actions) - 1:
            return
        self.save_current()
        self._actions[row + 1], self._actions[row] = self._actions[row], self._actions[row + 1]
        self.reload_list()
        self.list_widget.setCurrentRow(row + 1)

    def reload_list(self) -> None:
        self._loading = True
        self.list_widget.clear()
        for index, action in enumerate(self._actions, start=1):
            self.list_widget.addItem(QListWidgetItem(f"{index}. {self.summary(action)}"))
        self._loading = False

    def summary(self, action: ActionDefinition) -> str:
        spec = action_spec(action.action_type)
        detail = ", ".join(f"{key}={value}" for key, value in action.params.items() if value not in {"", None})
        return f"{spec.label}" + (f" ({detail})" if detail else "")

    def selection_changed(self, row: int) -> None:
        if self._loading:
            return
        self.save_current()
        self.load_action(row)

    def load_action(self, row: int) -> None:
        self._loading = True
        self._current_index = row
        if row < 0 or row >= len(self._actions):
            self.description.setText("Add a step to start building this sequence.")
            self.fields.set_fields((), {})
            self._loading = False
            return
        action = self._actions[row]
        combo_index = next((i for i, spec in enumerate(ACTION_SPECS) if spec.action_type == action.action_type), 0)
        self.type_combo.setCurrentIndex(combo_index)
        spec = ACTION_SPECS[combo_index]
        self.description.setText(spec.description)
        self.delay_spin.setValue(float(action.delay_seconds))
        self.retries_spin.setValue(int(action.retries))
        self.retry_delay_spin.setValue(float(action.retry_delay_seconds))
        self.fields.set_fields(spec.fields, action.params)
        self._loading = False

    def save_current(self) -> None:
        row = self._current_index
        if self._loading or row < 0 or row >= len(self._actions):
            return
        spec = self.current_spec()
        self._actions[row] = ActionDefinition(
            spec.action_type,
            params=self.fields.values(),
            delay_seconds=self.delay_spin.value(),
            retries=self.retries_spin.value(),
            retry_delay_seconds=self.retry_delay_spin.value(),
        )

    def action_type_changed(self) -> None:
        if self._loading or self._current_index < 0:
            return
        spec = self.current_spec()
        self.description.setText(spec.description)
        self.fields.set_fields(spec.fields, default_action_params(spec.action_type))
        self.save_current()
        self.reload_list()
        self.list_widget.setCurrentRow(self._current_index)


class ConditionSequenceEditor(QWidget):
    def __init__(self, context) -> None:
        super().__init__()
        self.context = context
        self._conditions: list[dict[str, Any]] = []
        self._current_index = -1
        self._loading = False
        self.list_widget = QListWidget()
        self.list_widget.setObjectName("BuilderStepList")
        self.list_widget.setMinimumWidth(240)
        self.list_widget.setMinimumHeight(160)
        self.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self.show_condition_menu)
        self.type_combo = QComboBox()
        self.type_combo.addItems([f"{spec.category} - {spec.label}" for spec in CONDITION_SPECS])
        self.description = QLabel("")
        self.description.setWordWrap(True)
        self.description.setObjectName("HelpText")
        self.fields = FieldSetEditor(context)
        self.build()

    def build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        left_panel = QWidget()
        left_panel.setObjectName("SequenceListPanel")
        left = QVBoxLayout(left_panel)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(8)
        left_title = QLabel("Conditions")
        left_title.setObjectName("InlineSectionLabel")
        left.addWidget(left_title)
        left.addWidget(self.list_widget)
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        for label, handler in [("Add", self.add_condition), ("Remove", self.remove_condition)]:
            button = QPushButton(label)
            button.clicked.connect(handler)
            buttons.addWidget(button)
        left.addLayout(buttons)
        right_widget = QWidget()
        right_widget.setObjectName("SequenceEditorPanel")
        right = QVBoxLayout(right_widget)
        right.setContentsMargins(12, 12, 12, 12)
        right.setSpacing(10)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(9)
        form.addRow("Condition", self.type_combo)
        right.addLayout(form)
        right.addWidget(self.description)
        right.addWidget(self.fields)
        right.addStretch()
        root.addWidget(responsive_grid([left_panel, right_widget], min_column_width=300, max_columns=2))
        self.list_widget.currentRowChanged.connect(self.selection_changed)
        self.type_combo.currentIndexChanged.connect(self.condition_type_changed)

    def set_conditions(self, conditions: list[dict[str, Any]]) -> None:
        self._conditions = [dict(condition) for condition in conditions]
        self._current_index = -1
        self.reload_list()
        if self._conditions:
            self.list_widget.setCurrentRow(0)
        else:
            self.load_condition(-1)

    def conditions(self) -> list[dict[str, Any]]:
        self.save_current()
        return [dict(condition) for condition in self._conditions]

    def add_condition(self) -> None:
        spec = CONDITION_SPECS[0]
        self.save_current()
        self._conditions.append({"condition_type": spec.condition_type, "params": _default_params(spec.fields)})
        self.reload_list()
        self.list_widget.setCurrentRow(len(self._conditions) - 1)

    def show_condition_menu(self, position) -> None:
        row = self.list_widget.indexAt(position).row()
        if row >= 0:
            self.list_widget.setCurrentRow(row)
        menu = QMenu(self)
        add = menu.addAction("Add Condition")
        add.triggered.connect(self.add_condition)
        remove = menu.addAction("Remove Condition")
        remove.setEnabled(row >= 0)
        remove.triggered.connect(self.remove_condition)
        menu.exec(self.list_widget.viewport().mapToGlobal(position))

    def remove_condition(self) -> None:
        row = self.list_widget.currentRow()
        if row < 0:
            return
        self._conditions.pop(row)
        self._current_index = -1
        self.reload_list()
        self.list_widget.setCurrentRow(min(row, len(self._conditions) - 1))

    def reload_list(self) -> None:
        self._loading = True
        self.list_widget.clear()
        for index, condition in enumerate(self._conditions, start=1):
            self.list_widget.addItem(QListWidgetItem(f"{index}. {self.summary(condition)}"))
        self._loading = False

    def summary(self, condition: dict[str, Any]) -> str:
        spec = condition_spec(str(condition.get("condition_type") or condition.get("type") or "always"))
        params = condition_params(condition)
        detail = ", ".join(f"{key}={value}" for key, value in params.items() if value not in {"", None})
        return f"{spec.label}" + (f" ({detail})" if detail else "")

    def selection_changed(self, row: int) -> None:
        if self._loading:
            return
        self.save_current()
        self.load_condition(row)

    def load_condition(self, row: int) -> None:
        self._loading = True
        self._current_index = row
        if row < 0 or row >= len(self._conditions):
            self.description.setText("No condition means the automation can run every time its trigger fires.")
            self.fields.set_fields((), {})
            self._loading = False
            return
        condition = self._conditions[row]
        condition_type = str(condition.get("condition_type") or condition.get("type") or "always")
        combo_index = next((i for i, spec in enumerate(CONDITION_SPECS) if spec.condition_type == condition_type), 0)
        self.type_combo.setCurrentIndex(combo_index)
        spec = CONDITION_SPECS[combo_index]
        self.description.setText(spec.description)
        self.fields.set_fields(spec.fields, condition_params(condition))
        self._loading = False

    def save_current(self) -> None:
        row = self._current_index
        if self._loading or row < 0 or row >= len(self._conditions):
            return
        spec = CONDITION_SPECS[max(0, self.type_combo.currentIndex())]
        self._conditions[row] = {"condition_type": spec.condition_type, "params": self.fields.values()}

    def condition_type_changed(self) -> None:
        if self._loading or self._current_index < 0:
            return
        spec = CONDITION_SPECS[max(0, self.type_combo.currentIndex())]
        self.description.setText(spec.description)
        self.fields.set_fields(spec.fields, _default_params(spec.fields))
        self.save_current()
        self.reload_list()
        self.list_widget.setCurrentRow(self._current_index)
