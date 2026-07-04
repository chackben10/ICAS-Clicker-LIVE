from __future__ import annotations

import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from production_hub.core.endpoints.models import ActionDefinition, EndpointDefinition
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
        self.actions_editor = ActionSequenceEditor(context)
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
        helper = QLabel("Route variables use braces, for example /camera/{preset:int}. Any action field can use {{preset}}.")
        helper.setObjectName("HelpText")
        helper.setWordWrap(True)
        details_layout.addWidget(helper)
        editor_layout.addWidget(details_box)

        actions_box, actions_layout = self.section("Action Sequence")
        self.actions_editor.setMinimumHeight(380)
        actions_layout.addWidget(self.actions_editor)
        editor_layout.addWidget(actions_box)
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

    def section(self, heading: str) -> tuple[QGroupBox, QVBoxLayout]:
        box = QGroupBox(heading)
        box.setObjectName("BuilderSection")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setSpacing(10)
        return box, layout

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
        self._loading = False

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
            result = await self.context.endpoint_executor.execute(endpoint, {})
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
