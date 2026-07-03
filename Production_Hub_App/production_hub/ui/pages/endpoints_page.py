from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSplitter, QTableWidget, QVBoxLayout, QWidget

from production_hub.core.endpoints.models import ActionDefinition, EndpointDefinition
from production_hub.ui.pages.common import code_editor, configure_table, pretty_json, scroll_page, set_table_row, title


ENDPOINT_TEMPLATE = EndpointDefinition(
    key="new_endpoint",
    name="New Endpoint",
    route="/new-endpoint",
    actions=[ActionDefinition("propresenter.next_slide")],
    description="Describe what this endpoint does.",
)


class EndpointsPage(QWidget):
    def __init__(self, context) -> None:
        super().__init__()
        self.context = context
        self.table = QTableWidget()
        self.editor = code_editor()
        self.status = QLabel("Ready")
        self.status.setObjectName("StatusText")
        self._loading = False
        self.build()
        self.reload()

    def build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        scroll, _body, layout = scroll_page()
        root.addWidget(scroll)
        layout.addWidget(title("Endpoints", "Reusable named actions for browser pages, automations, MIDI, and future controllers."))

        buttons = QHBoxLayout()
        for label, handler in [
            ("New", self.new_endpoint),
            ("Save Definition", self.save_current),
            ("Delete", self.delete_current),
            ("Reload", self.reload),
        ]:
            button = QPushButton(label)
            button.clicked.connect(handler)
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)

        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["Key", "Name", "Route", "Enabled", "Dangerous", "Actions"])
        configure_table(self.table)
        self.table.itemSelectionChanged.connect(self.selection_changed)

        editor_holder = QWidget()
        editor_layout = QVBoxLayout(editor_holder)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.addWidget(QLabel("Endpoint Definition JSON"))
        editor_layout.addWidget(self.editor)

        splitter = QSplitter()
        splitter.addWidget(self.table)
        splitter.addWidget(editor_holder)
        splitter.setSizes([620, 560])
        layout.addWidget(splitter)

        layout.addWidget(self.handler_catalog())
        layout.addWidget(self.status)

    def handler_catalog(self) -> QTableWidget:
        handlers = [
            ("propresenter.next_slide", "Slides", "Advance the active/focused ProPresenter presentation."),
            ("propresenter.previous_slide", "Slides", "Go backward in the active/focused ProPresenter presentation."),
            ("propresenter.focus_slide", "Slides", "Trigger a specific slide index from request context."),
            ("propresenter.trigger_presentation", "Presentation", "Trigger a configured presentation mapping by friendly label."),
            ("propresenter.trigger_service_logo", "Presentation", "Trigger one of the configured service logo presentations."),
            ("propresenter.clear_announcements", "Layers", "Clear the ProPresenter announcements layer."),
            ("propresenter.clear_slide", "Layers", "Clear the ProPresenter slide layer."),
            ("propresenter.trigger_macro", "Macros", "Trigger an allow-listed ProPresenter macro by exact name."),
            ("propresenter.timer_start", "Timers", "Start the configured service countdown timer."),
            ("propresenter.timer_stop", "Timers", "Stop the configured service countdown timer."),
            ("propresenter.timer_reset", "Timers", "Reset the configured service countdown timer."),
            ("propresenter.audio_trigger", "Audio", "Trigger a validated playlist and track."),
            ("propresenter.audio_clear", "Audio", "Clear the ProPresenter audio layer."),
            ("obs.set_scene", "OBS", "Switch OBS to a scene using the configured transition policy."),
            ("runtime.auto_show", "Runtime", "Read or update the Auto Show runtime state."),
            ("delay", "Utility", "Pause endpoint execution for a number of seconds."),
        ]
        table = QTableWidget(len(handlers), 3)
        table.setHorizontalHeaderLabels(["Handler", "Area", "What it does"])
        for row, values in enumerate(handlers):
            set_table_row(table, row, list(values))
        configure_table(table)
        table.setMinimumHeight(300)
        return table

    def endpoints(self) -> list[EndpointDefinition]:
        return sorted(self.context.endpoint_registry.all(), key=lambda item: item.key)

    def reload(self) -> None:
        self._loading = True
        items = self.endpoints()
        self.table.setRowCount(len(items))
        for row, endpoint in enumerate(items):
            set_table_row(
                self.table,
                row,
                [
                    endpoint.key,
                    endpoint.name,
                    endpoint.route,
                    "Yes" if endpoint.enabled else "No",
                    "Yes" if endpoint.dangerous else "No",
                    ", ".join(action.action_type for action in endpoint.actions),
                ],
            )
            self.table.item(row, 0).setData(Qt.ItemDataRole.UserRole, endpoint.key)
        self._loading = False
        if items:
            self.table.selectRow(0)
        else:
            self.editor.setPlainText("")
        self.status.setText(f"Loaded {len(items)} endpoint definitions.")

    def selection_changed(self) -> None:
        if self._loading:
            return
        row = self.table.currentRow()
        if row < 0 or not self.table.item(row, 0):
            return
        key = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        endpoint = self.context.endpoint_registry.get(key)
        if endpoint:
            self.editor.setPlainText(pretty_json(endpoint))

    def new_endpoint(self) -> None:
        self.table.clearSelection()
        self.editor.setPlainText(pretty_json(ENDPOINT_TEMPLATE))
        self.status.setText("New endpoint template ready. Change key/route before saving.")

    def parse_editor(self) -> EndpointDefinition:
        data = json.loads(self.editor.toPlainText())
        return EndpointDefinition.from_dict(data)

    def save_current(self) -> None:
        try:
            endpoint = self.parse_editor()
            self.context.endpoint_registry.register(endpoint)
            self.context.config_repository.save_endpoints(self.context.endpoint_registry.all())
            self.reload()
            for row in range(self.table.rowCount()):
                if self.table.item(row, 0).text() == endpoint.key:
                    self.table.selectRow(row)
                    break
            self.status.setText(f"Saved endpoint {endpoint.key}.")
        except Exception as exc:
            self.status.setText(f"Save failed: {exc}")

    def delete_current(self) -> None:
        row = self.table.currentRow()
        if row < 0 or not self.table.item(row, 0):
            self.status.setText("Select an endpoint to delete.")
            return
        key = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        self.context.endpoint_registry.remove(key)
        self.context.config_repository.save_endpoints(self.context.endpoint_registry.all())
        self.reload()
        self.status.setText(f"Deleted endpoint {key}.")


def build_page(context) -> QWidget:
    return EndpointsPage(context)
