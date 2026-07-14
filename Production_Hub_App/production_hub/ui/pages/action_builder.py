from __future__ import annotations

from copy import deepcopy
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
    QTreeWidget,
    QTreeWidgetItem,
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
        self.condition_edit = QLineEdit()
        self.condition_edit.setPlaceholderText("Optional, for example {{clearslide}}")
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
        form.addRow("Run when", self.condition_edit)
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
        self.condition_edit.setText(action.condition)
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
            condition=self.condition_edit.text().strip(),
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


class RuleTreeEditor(QWidget):
    """Nested boolean rule editor used by the automation macro builder."""

    GROUP_LABELS = {"and": "All rules (AND)", "or": "Any rule (OR)", "none": "No rules (NOT)"}

    def __init__(self, context) -> None:
        super().__init__()
        self.context = context
        self._rules: dict[str, Any] = {"operator": "and", "children": []}
        self._current_path: tuple[int, ...] | None = None
        self._loading = False
        self.tree = QTreeWidget()
        self.tree.setObjectName("BuilderStepList")
        self.tree.setHeaderLabels(["Rule logic"])
        self.tree.setMinimumHeight(230)
        self.tree.setIndentation(24)
        self.group_combo = QComboBox()
        for operator in ("and", "or", "none"):
            self.group_combo.addItem(self.GROUP_LABELS[operator], operator)
        self.negate_check = QCheckBox("If not (invert this rule)")
        self.type_combo = QComboBox()
        self.type_combo.addItems([f"{spec.category} - {spec.label}" for spec in CONDITION_SPECS])
        self.description = QLabel("")
        self.description.setWordWrap(True)
        self.description.setObjectName("HelpText")
        self.fields = FieldSetEditor(context)
        self.group_editor = QWidget()
        group_form = QFormLayout(self.group_editor)
        group_form.setContentsMargins(0, 0, 0, 0)
        group_form.addRow("Group logic", self.group_combo)
        self.rule_editor = QWidget()
        rule_layout = QVBoxLayout(self.rule_editor)
        rule_layout.setContentsMargins(0, 0, 0, 0)
        rule_form = QFormLayout()
        rule_form.addRow("Rule", self.type_combo)
        rule_form.addRow("Logic", self.negate_check)
        rule_layout.addLayout(rule_form)
        rule_layout.addWidget(self.description)
        rule_layout.addWidget(self.fields)
        self.build()

    def build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        helper = QLabel("Nest groups to combine ALL (and), ANY (or), and NONE (not) logic. Invert an individual rule with If not.")
        helper.setObjectName("HelpText")
        helper.setWordWrap(True)
        root.addWidget(helper)
        root.addWidget(self.tree)
        buttons = QHBoxLayout()
        for label, handler in [
            ("Add Rule", self.add_rule),
            ("Add Group", self.add_group),
            ("Remove", self.remove_node),
            ("Up", lambda: self.move_node(-1)),
            ("Down", lambda: self.move_node(1)),
        ]:
            button = QPushButton(label)
            button.clicked.connect(handler)
            buttons.addWidget(button)
        root.addLayout(buttons)
        editor = QWidget()
        editor.setObjectName("SequenceEditorPanel")
        editor_layout = QVBoxLayout(editor)
        editor_layout.addWidget(self.group_editor)
        editor_layout.addWidget(self.rule_editor)
        root.addWidget(editor)
        self.tree.currentItemChanged.connect(self.selection_changed)
        self.type_combo.currentIndexChanged.connect(self.condition_type_changed)
        self.group_combo.currentIndexChanged.connect(self.group_type_changed)

    def set_rules(self, rules: dict[str, Any]) -> None:
        self._rules = deepcopy(rules or {"operator": "and", "children": []})
        if "children" not in self._rules:
            self._rules = {"operator": "and", "children": [self._rules]}
        self._current_path = None
        self.reload_tree(())

    def rules(self) -> dict[str, Any]:
        self.save_current()
        return deepcopy(self._rules)

    def _node(self, path: tuple[int, ...]) -> dict[str, Any]:
        node = self._rules
        for index in path:
            node = node["children"][index]
        return node

    def _parent_path(self, path: tuple[int, ...]) -> tuple[int, ...]:
        return path[:-1]

    def _target_group_path(self) -> tuple[int, ...]:
        path = self._current_path or ()
        node = self._node(path)
        return path if "children" in node else self._parent_path(path)

    def reload_tree(self, select_path: tuple[int, ...] | None = None) -> None:
        self._loading = True
        self._current_path = None
        self.tree.clear()

        def add(parent, node: dict[str, Any], path: tuple[int, ...]) -> None:
            item = QTreeWidgetItem([self.summary(node, path)])
            item.setData(0, Qt.ItemDataRole.UserRole, list(path))
            if parent is None:
                self.tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            for index, child in enumerate(node.get("children") or []):
                if isinstance(child, dict):
                    add(item, child, (*path, index))
            item.setExpanded(True)

        add(None, self._rules, ())
        self._loading = False
        wanted = () if select_path is None else select_path
        matches = self.tree.findItems("*", Qt.MatchFlag.MatchWildcard | Qt.MatchFlag.MatchRecursive)
        selected = next((item for item in matches if tuple(item.data(0, Qt.ItemDataRole.UserRole) or []) == wanted), None)
        if selected:
            self.tree.setCurrentItem(selected)

    def summary(self, node: dict[str, Any], path: tuple[int, ...]) -> str:
        if "children" in node:
            label = self.GROUP_LABELS.get(str(node.get("operator") or "and"), "All rules (AND)")
            return ("WHEN " if not path else "GROUP: ") + label
        spec = condition_spec(str(node.get("condition_type") or "always"))
        params = condition_params(node)
        detail = ", ".join(f"{key}={value}" for key, value in params.items() if value not in {"", None})
        prefix = "IF NOT " if node.get("negate") else "IF "
        return prefix + spec.label + (f" ({detail})" if detail else "")

    def selection_changed(self, current, _previous) -> None:
        if self._loading or current is None:
            return
        self.save_current()
        self._current_path = tuple(current.data(0, Qt.ItemDataRole.UserRole) or [])
        self.load_current()

    def load_current(self) -> None:
        self._loading = True
        node = self._node(self._current_path or ())
        is_group = "children" in node
        self.group_editor.setVisible(is_group)
        self.rule_editor.setVisible(not is_group)
        if is_group:
            index = self.group_combo.findData(str(node.get("operator") or "and"))
            self.group_combo.setCurrentIndex(max(0, index))
        else:
            condition_type = str(node.get("condition_type") or "always")
            index = next((i for i, spec in enumerate(CONDITION_SPECS) if spec.condition_type == condition_type), 0)
            self.type_combo.setCurrentIndex(index)
            spec = CONDITION_SPECS[index]
            self.negate_check.setChecked(bool(node.get("negate", False)))
            self.description.setText(spec.description)
            self.fields.set_fields(spec.fields, condition_params(node))
        self._loading = False

    def save_current(self) -> None:
        if self._loading or self._current_path is None:
            return
        node = self._node(self._current_path)
        if "children" in node:
            node["operator"] = str(self.group_combo.currentData() or "and")
        else:
            spec = CONDITION_SPECS[max(0, self.type_combo.currentIndex())]
            node.clear()
            node.update(
                {
                    "condition_type": spec.condition_type,
                    "params": self.fields.values(),
                    "negate": self.negate_check.isChecked(),
                }
            )

    def add_rule(self) -> None:
        self.save_current()
        parent_path = self._target_group_path()
        parent = self._node(parent_path)
        spec = CONDITION_SPECS[0]
        parent.setdefault("children", []).append(
            {"condition_type": spec.condition_type, "params": _default_params(spec.fields), "negate": False}
        )
        self.reload_tree((*parent_path, len(parent["children"]) - 1))

    def add_group(self) -> None:
        self.save_current()
        parent_path = self._target_group_path()
        parent = self._node(parent_path)
        parent.setdefault("children", []).append({"operator": "and", "children": []})
        self.reload_tree((*parent_path, len(parent["children"]) - 1))

    def remove_node(self) -> None:
        path = self._current_path
        if not path:
            return
        parent = self._node(self._parent_path(path))
        parent["children"].pop(path[-1])
        self.reload_tree(self._parent_path(path))

    def move_node(self, offset: int) -> None:
        path = self._current_path
        if not path:
            return
        parent = self._node(self._parent_path(path))
        target = path[-1] + offset
        if target < 0 or target >= len(parent["children"]):
            return
        self.save_current()
        parent["children"][path[-1]], parent["children"][target] = parent["children"][target], parent["children"][path[-1]]
        self.reload_tree((*self._parent_path(path), target))

    def condition_type_changed(self) -> None:
        if self._loading or self._current_path is None:
            return
        spec = CONDITION_SPECS[max(0, self.type_combo.currentIndex())]
        self.description.setText(spec.description)
        self.fields.set_fields(spec.fields, _default_params(spec.fields))
        self.save_current()
        self.reload_tree(self._current_path)

    def group_type_changed(self) -> None:
        if self._loading or self._current_path is None:
            return
        self.save_current()
        self.reload_tree(self._current_path)
