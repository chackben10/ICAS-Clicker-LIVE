from __future__ import annotations

import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from production_hub.core.config.input_lists import input_list_by_key, source_labels
from production_hub.core.endpoints.models import ActionDefinition, EndpointDefinition, EndpointInputDefinition
from production_hub.ui.pages.action_builder import ActionSequenceEditor
from production_hub.ui.pages.common import PAGE_MARGIN, configure_table, responsive_grid, responsive_two_pane, run_background, set_table_row, title


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", text.strip().lower()).strip("-")
    return slug or "new-endpoint"


class EndpointsPage(QWidget):
    def __init__(self, context) -> None:
        super().__init__()
        self.context = context
        self.table = QTableWidget()
        self.status = QLabel("Ready")
        self.status.setObjectName("StatusText")
        self.key_edit = QLineEdit()
        self.name_edit = QLineEdit()
        self.route_edit = QLineEdit()
        self.enabled_check = QCheckBox("Enabled")
        self.dangerous_check = QCheckBox("Requires extra care")
        self.get_check = QCheckBox("GET")
        self.post_check = QCheckBox("POST")
        self.description_edit = QTextEdit()
        self.inputs_table = QTableWidget()
        self.preview = QTextEdit()
        self.actions_editor = ActionSequenceEditor(context, self.inputs_from_table)
        self._loading = False
        self.build()
        self.reload()

    def build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(PAGE_MARGIN, 20, PAGE_MARGIN, PAGE_MARGIN)
        root.setSpacing(14)
        root.addWidget(
            title(
                "Endpoint Builder",
                "Create browser/API endpoints from reusable modules. Volunteers choose actions and fill in fields; no code or JSON editing is required.",
            )
        )

        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Endpoint", "Route", "Steps"])
        configure_table(self.table)
        self.table.setObjectName("BuilderList")
        self.table.setMinimumWidth(300)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.selection_changed)

        left_panel = QWidget()
        left_panel.setObjectName("BuilderSidebarPanel")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(14, 14, 14, 14)
        left_layout.setSpacing(10)
        left_header = QLabel("Saved Endpoints")
        left_header.setObjectName("BuilderPanelTitle")
        left_layout.addWidget(left_header)
        left_layout.addWidget(self.table, 1)
        left_buttons = QHBoxLayout()
        for label, handler, danger in [
            ("New", self.new_endpoint, False),
            ("Duplicate", self.duplicate_current, False),
            ("Delete", self.delete_current, True),
            ("Reload", self.reload, False),
        ]:
            button = QPushButton(label)
            button.clicked.connect(handler)
            if danger:
                button.setObjectName("DangerButton")
            left_buttons.addWidget(button)
        left_layout.addLayout(left_buttons)

        editor = QWidget()
        editor.setObjectName("BuilderEditor")
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(14)

        top_action_buttons = []
        for label, handler in [("Run Test", self.run_test), ("Save Endpoint", self.save_current)]:
            button = QPushButton(label)
            button.clicked.connect(handler)
            top_action_buttons.append(button)
        editor_layout.addWidget(responsive_grid(top_action_buttons, min_column_width=150, max_columns=2))

        details_box, details_layout = self.section("Endpoint Details")
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        form.addRow("Internal key", self.key_edit)
        form.addRow("Display name", self.name_edit)
        form.addRow("Route", self.route_edit)
        method_row = QHBoxLayout()
        method_row.addWidget(self.get_check)
        method_row.addWidget(self.post_check)
        method_row.addStretch()
        form.addRow("Allowed methods", method_row)
        flags = QHBoxLayout()
        flags.addWidget(self.enabled_check)
        flags.addWidget(self.dangerous_check)
        flags.addStretch()
        form.addRow("Status", flags)
        details_layout.addLayout(form)
        self.description_edit.setFixedHeight(82)
        self.description_edit.setPlaceholderText("What should a volunteer know before using this endpoint?")
        details_layout.addWidget(QLabel("Description"))
        details_layout.addWidget(self.description_edit)
        helper = QLabel("Route variables use braces, for example /camera/{preset:int}. Action fields can bind to inputs from their dropdowns.")
        helper.setObjectName("HelpText")
        helper.setWordWrap(True)
        details_layout.addWidget(helper)
        editor_layout.addWidget(details_box)

        inputs_box, inputs_layout = self.section("Endpoint Inputs")
        input_help = QLabel("Define what callers can send. Select inputs reference live Input Lists by key, so list edits update this endpoint automatically.")
        input_help.setObjectName("HelpText")
        input_help.setWordWrap(True)
        inputs_layout.addWidget(input_help)
        self.configure_inputs_table()
        inputs_layout.addWidget(self.inputs_table)
        input_buttons = QHBoxLayout()
        for label, handler in [("Add Input", self.add_input_row), ("Remove Input", self.remove_input_row)]:
            button = QPushButton(label)
            button.clicked.connect(handler)
            input_buttons.addWidget(button)
        input_buttons.addStretch()
        inputs_layout.addLayout(input_buttons)
        editor_layout.addWidget(inputs_box)

        actions_box, actions_layout = self.section("Action Sequence")
        action_help = QLabel("Right-click the steps list to add actions. For endpoints with inputs, action fields offer Use input choices.")
        action_help.setObjectName("HelpText")
        action_help.setWordWrap(True)
        actions_layout.addWidget(action_help)
        self.actions_editor.setMinimumHeight(380)
        actions_layout.addWidget(self.actions_editor)
        editor_layout.addWidget(actions_box)

        preview_box, preview_layout = self.section("Generated Request Preview")
        self.preview.setReadOnly(True)
        self.preview.setFixedHeight(150)
        preview_layout.addWidget(self.preview)
        editor_layout.addWidget(preview_box)
        editor_layout.addWidget(self.status)
        editor_layout.addStretch()

        editor_scroll = QScrollArea()
        editor_scroll.setWidgetResizable(True)
        editor_scroll.setObjectName("BuilderEditorScroll")
        editor_scroll.setWidget(editor)

        root.addWidget(
            responsive_two_pane(
                left_panel,
                editor_scroll,
                collapse_width=1050,
                left_min_width=420,
                left_max_width=560,
            ),
            1,
        )
        for widget in [self.name_edit, self.key_edit, self.route_edit]:
            widget.textChanged.connect(self.update_preview)
        self.description_edit.textChanged.connect(self.update_preview)
        self.get_check.toggled.connect(self.update_preview)
        self.post_check.toggled.connect(self.update_preview)
        self.inputs_table.itemChanged.connect(lambda _item: self.update_preview())

    def section(self, heading: str) -> tuple[QGroupBox, QVBoxLayout]:
        box = QGroupBox(heading)
        box.setObjectName("BuilderSection")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setSpacing(10)
        return box, layout

    def configure_inputs_table(self) -> None:
        self.inputs_table.setColumnCount(6)
        self.inputs_table.setHorizontalHeaderLabels(["Name", "Label", "Type", "Required", "List Source", "Default"])
        configure_table(self.inputs_table)
        self.inputs_table.setMinimumHeight(150)
        self.inputs_table.horizontalHeader().setStretchLastSection(True)

    def add_input_row(self) -> None:
        row = self.inputs_table.rowCount()
        self.inputs_table.insertRow(row)
        self.set_input_row(row, EndpointInputDefinition("input_name", "Input label", "text"))
        self.update_preview()

    def remove_input_row(self) -> None:
        row = self.inputs_table.currentRow()
        if row >= 0:
            self.inputs_table.removeRow(row)
            self.update_preview()

    def table_item(self, value: object):
        return QTableWidgetItem(str(value if value is not None else ""))

    def set_input_row(self, row: int, input_def: EndpointInputDefinition) -> None:
        self.inputs_table.setItem(row, 0, self.table_item(input_def.name))
        self.inputs_table.setItem(row, 1, self.table_item(input_def.label))
        type_combo = QComboBox()
        type_combo.addItems(["text", "number", "bool", "select"])
        type_combo.setCurrentText(input_def.kind)
        type_combo.currentIndexChanged.connect(self.update_preview)
        self.inputs_table.setCellWidget(row, 2, type_combo)
        required_combo = QComboBox()
        required_combo.addItems(["No", "Yes"])
        required_combo.setCurrentText("Yes" if input_def.required else "No")
        required_combo.currentIndexChanged.connect(self.update_preview)
        self.inputs_table.setCellWidget(row, 3, required_combo)
        source_combo = QComboBox()
        source_combo.addItem("", "")
        for key, label in source_labels(self.context.config):
            source_combo.addItem(label, key)
        index = source_combo.findData(input_def.option_source)
        if index >= 0:
            source_combo.setCurrentIndex(index)
        source_combo.currentIndexChanged.connect(self.update_preview)
        self.inputs_table.setCellWidget(row, 4, source_combo)
        self.inputs_table.setItem(row, 5, self.table_item(input_def.default))

    def load_inputs(self, inputs: list[EndpointInputDefinition]) -> None:
        self.inputs_table.setRowCount(0)
        for input_def in inputs:
            row = self.inputs_table.rowCount()
            self.inputs_table.insertRow(row)
            self.set_input_row(row, input_def)
        self.update_preview()

    def inputs_from_table(self) -> list[EndpointInputDefinition]:
        inputs: list[EndpointInputDefinition] = []
        for row in range(self.inputs_table.rowCount()):
            name = self.input_cell(row, 0)
            if not name:
                continue
            inputs.append(
                EndpointInputDefinition(
                    name=name,
                    label=self.input_cell(row, 1) or name,
                    kind=(self.input_cell(row, 2) or "text").lower(),
                    required=self.input_cell(row, 3).lower() in {"yes", "true", "1", "required"},
                    option_source=self.input_cell(row, 4),
                    default=self.input_cell(row, 5),
                )
            )
        return inputs

    def input_cell(self, row: int, column: int) -> str:
        widget = self.inputs_table.cellWidget(row, column)
        if isinstance(widget, QComboBox):
            data = widget.currentData()
            if data is not None:
                return str(data)
            return widget.currentText().strip()
        item = self.inputs_table.item(row, column)
        return str(item.text() if item else "").strip()

    def example_value(self, input_def: EndpointInputDefinition) -> object:
        if input_def.default != "":
            return input_def.default
        if input_def.option_source:
            input_list = input_list_by_key(self.context.config, input_def.option_source)
            if input_list:
                item = next((candidate for candidate in input_list.items if candidate.enabled), None)
                if item:
                    return item.value
        if input_def.kind == "number":
            return 1
        if input_def.kind == "bool":
            return True
        return input_def.name

    def update_preview(self, *_args) -> None:
        if self._loading:
            return
        route = self.route_edit.text().strip() or "/new-endpoint"
        inputs = self.inputs_from_table()
        body = {input_def.name: self.example_value(input_def) for input_def in inputs if input_def.name}
        lines = []
        methods = []
        if self.get_check.isChecked():
            methods.append("GET")
        if self.post_check.isChecked():
            methods.append("POST")
        lines.append(f"Route: {route}")
        lines.append(f"Methods: {', '.join(methods) if methods else 'GET'}")
        if inputs:
            lines.append("")
            lines.append("JSON body or query values:")
            for name, value in body.items():
                lines.append(f"  {name}: {value}")
            lines.append("")
            lines.append("Action bindings:")
            for input_def in inputs:
                lines.append(f"  {input_def.label}: Use input: {input_def.name} -> {{{{{input_def.name}}}}}")
        else:
            lines.append("")
            lines.append("No caller input required.")
        self.preview.setPlainText("\n".join(lines))

    def endpoints(self) -> list[EndpointDefinition]:
        return sorted(self.context.endpoint_registry.all(), key=lambda item: item.name.lower())

    def endpoint_snapshot(self) -> list[EndpointDefinition]:
        return [
            EndpointDefinition.from_dict(endpoint.to_dict())
            for endpoint in sorted(self.context.endpoint_registry.all(), key=lambda item: item.key)
        ]

    def apply_endpoint_snapshot(self, endpoints: list[EndpointDefinition], message: str = "") -> None:
        restored = [EndpointDefinition.from_dict(endpoint.to_dict()) for endpoint in endpoints]
        self.context.endpoint_registry.replace_all(restored)
        self.context.config_repository.save_endpoints(self.context.endpoint_registry.all())
        self.reload()
        if message:
            self.status.setText(message)

    def record_endpoint_change(
        self,
        label: str,
        before: list[EndpointDefinition],
        after: list[EndpointDefinition],
    ) -> None:
        before_data = [EndpointDefinition.from_dict(endpoint.to_dict()) for endpoint in before]
        after_data = [EndpointDefinition.from_dict(endpoint.to_dict()) for endpoint in after]
        if [item.to_dict() for item in before_data] == [item.to_dict() for item in after_data]:
            return
        self.context.undo_manager.record(
            label,
            lambda: self.apply_endpoint_snapshot(before_data, f"Undid: {label}"),
            lambda: self.apply_endpoint_snapshot(after_data, f"Redid: {label}"),
        )

    def reload(self) -> None:
        self._loading = True
        endpoints = self.endpoints()
        self.table.setRowCount(len(endpoints))
        for row, endpoint in enumerate(endpoints):
            set_table_row(
                self.table,
                row,
                [
                    endpoint.name + ("" if endpoint.enabled else " (disabled)"),
                    endpoint.route,
                    len(endpoint.actions),
                ],
            )
            self.table.item(row, 0).setData(Qt.ItemDataRole.UserRole, endpoint.key)
        self._loading = False
        if endpoints:
            self.table.selectRow(0)
        else:
            self.new_endpoint()
        self.status.setText(f"Loaded {len(endpoints)} endpoints.")

    def selection_changed(self) -> None:
        if self._loading:
            return
        row = self.table.currentRow()
        if row < 0 or not self.table.item(row, 0):
            return
        key = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        endpoint = self.context.endpoint_registry.get(key)
        if endpoint:
            self.load_endpoint(endpoint)

    def load_endpoint(self, endpoint: EndpointDefinition) -> None:
        self._loading = True
        self.key_edit.setText(endpoint.key)
        self.name_edit.setText(endpoint.name)
        self.route_edit.setText(endpoint.route)
        methods = {item.upper() for item in endpoint.allowed_methods}
        self.get_check.setChecked("GET" in methods)
        self.post_check.setChecked("POST" in methods)
        self.enabled_check.setChecked(endpoint.enabled)
        self.dangerous_check.setChecked(endpoint.dangerous)
        self.description_edit.setPlainText(endpoint.description)
        self.actions_editor.set_actions(endpoint.actions)
        self.load_inputs(endpoint.inputs)
        self._loading = False
        self.update_preview()

    def new_endpoint(self) -> None:
        self.table.clearSelection()
        endpoint = EndpointDefinition(
            key="new_endpoint",
            name="New Endpoint",
            route="/new-endpoint",
            actions=[ActionDefinition("propresenter.next_slide")],
            description="",
        )
        self.load_endpoint(endpoint)
        self.status.setText("New endpoint ready. Give it a clear name, route, and action sequence.")

    def duplicate_current(self) -> None:
        try:
            endpoint = self.current_endpoint_from_form()
            endpoint.key = f"{endpoint.key}_copy"
            endpoint.name = f"{endpoint.name} Copy"
            endpoint.route = f"{endpoint.route.rstrip('/')}-copy"
            self.load_endpoint(endpoint)
            self.status.setText("Duplicated endpoint. Save when ready.")
        except Exception as exc:
            self.status.setText(f"Duplicate failed: {exc}")

    def current_endpoint_from_form(self) -> EndpointDefinition:
        name = self.name_edit.text().strip() or "New Endpoint"
        key = self.key_edit.text().strip() or slugify(name).replace("-", "_")
        route = self.route_edit.text().strip() or f"/{slugify(name)}"
        methods = []
        if self.get_check.isChecked():
            methods.append("GET")
        if self.post_check.isChecked():
            methods.append("POST")
        if not methods:
            methods = ["GET"]
        return EndpointDefinition(
            key=key,
            name=name,
            route=route,
            actions=self.actions_editor.actions(),
            enabled=self.enabled_check.isChecked(),
            dangerous=self.dangerous_check.isChecked(),
            description=self.description_edit.toPlainText().strip(),
            allowed_methods=methods,
            inputs=self.inputs_from_table(),
        )

    def save_current(self) -> None:
        try:
            before = self.endpoint_snapshot()
            row = self.table.currentRow()
            old_key = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) if row >= 0 and self.table.item(row, 0) else ""
            endpoint = self.current_endpoint_from_form()
            if old_key and old_key != endpoint.key:
                self.context.endpoint_registry.remove(old_key)
            self.context.endpoint_registry.register(endpoint)
            self.context.config_repository.save_endpoints(self.context.endpoint_registry.all())
            after = self.endpoint_snapshot()
            self.record_endpoint_change(f"Save endpoint {endpoint.name}", before, after)
            self.reload()
            self.select_key(endpoint.key)
            self.status.setText(f"Saved endpoint {endpoint.name}. It is available at {endpoint.route}.")
        except Exception as exc:
            self.status.setText(f"Save failed: {exc}")

    def run_test(self) -> None:
        try:
            endpoint = self.current_endpoint_from_form()
        except Exception as exc:
            self.status.setText(f"Cannot run test: {exc}")
            return
        self.status.setText("Running endpoint test...")

        async def run():
            test_context = {input_def.name: self.example_value(input_def) for input_def in endpoint.inputs}
            result = await self.context.endpoint_executor.execute(endpoint, test_context)
            return "Test passed." if result.ok else f"Test failed: {result.error}"

        run_background(run, lambda _ok, message: self.status.setText(message))

    def delete_current(self) -> None:
        row = self.table.currentRow()
        if row < 0 or not self.table.item(row, 0):
            self.status.setText("Select an endpoint to delete.")
            return
        before = self.endpoint_snapshot()
        key = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        self.context.endpoint_registry.remove(key)
        self.context.config_repository.save_endpoints(self.context.endpoint_registry.all())
        after = self.endpoint_snapshot()
        self.record_endpoint_change(f"Delete endpoint {key}", before, after)
        self.reload()
        self.status.setText(f"Deleted endpoint {key}.")

    def select_key(self, key: str) -> None:
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == key:
                self.table.selectRow(row)
                break


def build_page(context) -> QWidget:
    return EndpointsPage(context)
