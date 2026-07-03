from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSplitter, QTableWidget, QVBoxLayout, QWidget

from production_hub.core.automation.models import AutomationDefinition, AutomationRunState
from production_hub.ui.pages.common import code_editor, configure_table, pretty_json, scroll_page, set_table_row, title


AUTOMATION_TEMPLATE = AutomationDefinition(
    key="new_automation",
    name="New Automation",
    trigger="manual",
    enabled=True,
    description="Describe the trigger, conditions, actions, and safety behavior.",
)


class AutomationsPage(QWidget):
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
        layout.addWidget(title("Automations", "No-code automation definitions with editable JSON and run-state inspection."))

        buttons = QHBoxLayout()
        for label, handler in [
            ("New", self.new_automation),
            ("Save Definition", self.save_current),
            ("Delete", self.delete_current),
            ("Pause All", self.pause_all),
            ("Resume All", self.resume_all),
            ("Reload", self.reload),
        ]:
            button = QPushButton(label)
            button.clicked.connect(handler)
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)

        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels(["Key", "Name", "Trigger", "Enabled", "Cooldown", "Debounce", "Runs", "Last Error"])
        configure_table(self.table)
        self.table.itemSelectionChanged.connect(self.selection_changed)

        editor_holder = QWidget()
        editor_layout = QVBoxLayout(editor_holder)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.addWidget(QLabel("Automation Definition JSON"))
        editor_layout.addWidget(self.editor)

        splitter = QSplitter()
        splitter.addWidget(self.table)
        splitter.addWidget(editor_holder)
        splitter.setSizes([680, 520])
        layout.addWidget(splitter)
        help_text = QLabel(
            "Current first-party handlers are wired for Bible Look Enforcement, OBS Look Sync, "
            "Slide Label Audio Sync, Auto Show Slides, and OBS Connection Watchdog. New definitions "
            "can be saved here and then connected to a handler as new modules are implemented."
        )
        help_text.setWordWrap(True)
        help_text.setObjectName("HelpText")
        layout.addWidget(help_text)
        layout.addWidget(self.status)

    def definitions(self) -> list[AutomationDefinition]:
        return sorted(self.context.automation_engine.definitions.values(), key=lambda item: item.key)

    def reload(self) -> None:
        self._loading = True
        items = self.definitions()
        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            state = self.context.automation_engine.states.get(item.key, AutomationRunState(item.key, item.enabled))
            set_table_row(
                self.table,
                row,
                [
                    item.key,
                    item.name,
                    item.trigger,
                    "Yes" if item.enabled else "No",
                    item.cooldown_seconds,
                    item.debounce_seconds,
                    state.run_count,
                    state.last_error,
                ],
            )
            self.table.item(row, 0).setData(Qt.ItemDataRole.UserRole, item.key)
        self._loading = False
        if items:
            self.table.selectRow(0)
        else:
            self.editor.setPlainText("")
        paused = "paused" if self.context.automation_engine.paused else "running"
        self.status.setText(f"Loaded {len(items)} automations. Engine is {paused}.")

    def selection_changed(self) -> None:
        if self._loading:
            return
        row = self.table.currentRow()
        if row < 0 or not self.table.item(row, 0):
            return
        key = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        definition = self.context.automation_engine.definitions.get(key)
        if definition:
            self.editor.setPlainText(pretty_json(definition))

    def new_automation(self) -> None:
        self.table.clearSelection()
        self.editor.setPlainText(pretty_json(AUTOMATION_TEMPLATE))
        self.status.setText("New automation template ready. Change key/name before saving.")

    def parse_editor(self) -> AutomationDefinition:
        data = json.loads(self.editor.toPlainText())
        return AutomationDefinition.from_dict(data)

    def save_current(self) -> None:
        try:
            definition = self.parse_editor()
            self.context.automation_engine.definitions[definition.key] = definition
            self.context.automation_engine.states.setdefault(definition.key, AutomationRunState(definition.key, definition.enabled))
            self.context.automation_engine.states[definition.key].enabled = definition.enabled
            self.context.config_repository.save_automations(list(self.context.automation_engine.definitions.values()))
            self.reload()
            for row in range(self.table.rowCount()):
                if self.table.item(row, 0).text() == definition.key:
                    self.table.selectRow(row)
                    break
            self.status.setText(f"Saved automation {definition.key}.")
        except Exception as exc:
            self.status.setText(f"Save failed: {exc}")

    def delete_current(self) -> None:
        row = self.table.currentRow()
        if row < 0 or not self.table.item(row, 0):
            self.status.setText("Select an automation to delete.")
            return
        key = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        self.context.automation_engine.definitions.pop(key, None)
        self.context.automation_engine.states.pop(key, None)
        self.context.config_repository.save_automations(list(self.context.automation_engine.definitions.values()))
        self.reload()
        self.status.setText(f"Deleted automation {key}.")

    def pause_all(self) -> None:
        self.context.automation_engine.pause_all()
        self.status.setText("All automations paused.")

    def resume_all(self) -> None:
        self.context.automation_engine.resume_all()
        self.status.setText("Automations resumed.")


def build_page(context) -> QWidget:
    return AutomationsPage(context)

