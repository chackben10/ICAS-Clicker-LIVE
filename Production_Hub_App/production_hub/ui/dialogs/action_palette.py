from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from production_hub.core.endpoints.catalog import (
    ACTION_SPECS,
    FieldSpec,
    action_options,
    action_spec,
    action_tree_path,
    default_action_params,
    normalize_select_value,
)
from production_hub.core.endpoints.models import ActionDefinition


ACTION_ROLE = Qt.ItemDataRole.UserRole


def action_summary(action: ActionDefinition) -> str:
    spec = action_spec(action.action_type)
    detail = ", ".join(f"{key}={value}" for key, value in action.params.items() if value not in {"", None})
    return f"{spec.category} - {spec.label}" + (f" ({detail})" if detail else "")


class ParameterEditor(QWidget):
    def __init__(self, context, endpoint_inputs=None) -> None:
        super().__init__()
        self.context = context
        self.endpoint_inputs = endpoint_inputs or (lambda: [])
        self.fields: tuple[FieldSpec, ...] = ()
        self.widgets: dict[str, QWidget] = {}
        self.form = QFormLayout(self)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setVerticalSpacing(8)

    def set_action(self, action_type: str, values: dict[str, Any] | None = None) -> None:
        self.clear()
        values = values or default_action_params(action_type)
        self.fields = action_spec(action_type).fields
        if not self.fields:
            label = QLabel("No parameters needed.")
            label.setObjectName("HelpText")
            self.form.addRow(label)
            return
        for field in self.fields:
            widget = self.widget_for(field, values.get(field.name, field.default))
            self.widgets[field.name] = widget
            self.form.addRow(field.label, widget)

    def clear(self) -> None:
        while self.form.count():
            item = self.form.takeAt(0)
            widget = item.widget()
            child_layout = item.layout()
            if widget:
                widget.deleteLater()
            if child_layout:
                while child_layout.count():
                    child = child_layout.takeAt(0)
                    if child.widget():
                        child.widget().deleteLater()
        self.fields = ()
        self.widgets = {}

    def widget_for(self, field: FieldSpec, value: Any) -> QWidget:
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
            widget.setToolTip(field.help_text)
        return widget

    def values(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for field in self.fields:
            widget = self.widgets.get(field.name)
            if isinstance(widget, QCheckBox):
                out[field.name] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                data = widget.currentData()
                out[field.name] = str(data) if data else normalize_select_value(field, widget.currentText())
            elif isinstance(widget, QLineEdit):
                out[field.name] = widget.text().strip()
        return out

    def focus_first_field(self) -> None:
        for widget in self.widgets.values():
            if isinstance(widget, QLineEdit):
                widget.setFocus(Qt.FocusReason.OtherFocusReason)
                widget.selectAll()
                return
            if isinstance(widget, QComboBox):
                widget.setFocus(Qt.FocusReason.OtherFocusReason)
                return


class ActionPaletteDialog(QDialog):
    def __init__(self, context, action: ActionDefinition | None = None, parent: QWidget | None = None, endpoint_inputs=None) -> None:
        super().__init__(parent)
        self.context = context
        self.selected_action = action
        self.current_action_type = action.action_type if action else ""
        self.setWindowTitle("Select Action")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumSize(720, 520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        body = QHBoxLayout()
        body.setSpacing(12)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setMinimumWidth(300)
        body.addWidget(self.tree, 1)

        right = QVBoxLayout()
        self.title = QLabel("Choose an action")
        self.title.setObjectName("InlineSectionLabel")
        right.addWidget(self.title)
        self.description = QLabel("")
        self.description.setWordWrap(True)
        self.description.setObjectName("HelpText")
        right.addWidget(self.description)
        self.params = ParameterEditor(context, endpoint_inputs=endpoint_inputs)
        right.addWidget(self.params)
        right.addStretch()
        body.addLayout(right, 1)
        layout.addLayout(body)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        choose = QPushButton("Choose")
        choose.clicked.connect(self.accept_selection)
        buttons.addWidget(cancel)
        buttons.addWidget(choose)
        layout.addLayout(buttons)

        self.populate()
        self.tree.currentItemChanged.connect(lambda current, _previous: self.select_item(current))
        self.tree.itemDoubleClicked.connect(lambda item, _column: self.accept_selection() if item.data(0, ACTION_ROLE) else None)

    def populate(self) -> None:
        roots: dict[tuple[str, ...], QTreeWidgetItem] = {}
        for spec in ACTION_SPECS:
            parent = None
            path_so_far: list[str] = []
            for part in action_tree_path(spec.action_type):
                path_so_far.append(part)
                key = tuple(path_so_far)
                if key not in roots:
                    item = QTreeWidgetItem([part])
                    item.setExpanded(True)
                    roots[key] = item
                    if parent is None:
                        self.tree.addTopLevelItem(item)
                    else:
                        parent.addChild(item)
                parent = roots[key]
            action_item = QTreeWidgetItem([spec.label])
            action_item.setData(0, ACTION_ROLE, spec.action_type)
            action_item.setToolTip(0, spec.description)
            assert parent is not None
            parent.addChild(action_item)
            if spec.action_type == self.current_action_type:
                self.tree.setCurrentItem(action_item)
        if self.tree.currentItem() is None and self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

    def select_item(self, item: QTreeWidgetItem | None) -> None:
        action_type = str(item.data(0, ACTION_ROLE) or "") if item else ""
        if not action_type:
            self.title.setText("Choose an action")
            self.description.setText("")
            self.params.clear()
            return
        self.current_action_type = action_type
        spec = action_spec(action_type)
        self.title.setText(f"{spec.category} - {spec.label}")
        self.description.setText(spec.description)
        values = self.selected_action.params if self.selected_action and self.selected_action.action_type == action_type else None
        self.params.set_action(action_type, values)

    def accept_selection(self) -> None:
        if not self.current_action_type:
            return
        self.selected_action = ActionDefinition(self.current_action_type, self.params.values())
        self.accept()


class ActionParameterDialog(QDialog):
    def __init__(self, context, action_type: str, action: ActionDefinition | None = None, parent: QWidget | None = None, endpoint_inputs=None) -> None:
        super().__init__(parent)
        self.context = context
        self.action_type = action_type
        self.selected_action: ActionDefinition | None = None
        spec = action_spec(action_type)
        self.setWindowTitle(spec.label)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        title = QLabel(f"{spec.category} - {spec.label}")
        title.setObjectName("InlineSectionLabel")
        layout.addWidget(title)

        description = QLabel(spec.description)
        description.setWordWrap(True)
        description.setObjectName("HelpText")
        layout.addWidget(description)

        self.params = ParameterEditor(context, endpoint_inputs=endpoint_inputs)
        self.params.set_action(action_type, action.params if action else None)
        layout.addWidget(self.params)
        QTimer.singleShot(0, self.params.focus_first_field)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        choose = QPushButton("Apply")
        choose.clicked.connect(self.accept_selection)
        buttons.addWidget(cancel)
        buttons.addWidget(choose)
        layout.addLayout(buttons)

    def accept_selection(self) -> None:
        self.selected_action = ActionDefinition(self.action_type, self.params.values())
        self.accept()
