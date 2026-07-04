from __future__ import annotations

import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
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

from production_hub.core.automation.evaluator import evaluate_conditions
from production_hub.core.automation.models import AutomationDefinition, AutomationRunState
from production_hub.core.endpoints.models import EndpointDefinition
from production_hub.ui.pages.action_builder import ActionSequenceEditor, ConditionSequenceEditor
from production_hub.ui.pages.common import PAGE_MARGIN, responsive_grid, responsive_two_pane, run_background, set_table_row, title


def keyify(text: str) -> str:
    key = re.sub(r"[^a-zA-Z0-9_]+", "_", text.strip().lower()).strip("_")
    return key or "new_automation"


class AutomationsPage(QWidget):
    def __init__(self, context) -> None:
        super().__init__()
        self.context = context
        self.table = QTableWidget()
        self.status = QLabel("Ready")
        self.status.setObjectName("StatusText")
        self.key_edit = QLineEdit()
        self.name_edit = QLineEdit()
        self.enabled_check = QCheckBox("Enabled")
        self.trigger_combo = QComboBox()
        self.trigger_combo.addItems(
            [
                "interval",
                "manual",
                "look_changed_or_poll",
                "active_slide_changed",
                "presentation_state_changed",
            ]
        )
        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(0, 86400)
        self.interval_spin.setDecimals(2)
        self.interval_spin.setSuffix(" sec")
        self.cooldown_spin = QDoubleSpinBox()
        self.cooldown_spin.setRange(0, 86400)
        self.cooldown_spin.setDecimals(2)
        self.cooldown_spin.setSuffix(" sec")
        self.debounce_spin = QDoubleSpinBox()
        self.debounce_spin.setRange(0, 3600)
        self.debounce_spin.setDecimals(2)
        self.debounce_spin.setSuffix(" sec")
        self.description_edit = QTextEdit()
        self.conditions_editor = ConditionSequenceEditor(context)
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
                "Automation Builder",
                "Create background workflows from plain-language triggers, conditions, and action modules.",
            )
        )

        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Automation", "Trigger", "Steps"])
        self.configure_list_table()
        self.table.setObjectName("BuilderList")
        self.table.itemSelectionChanged.connect(self.selection_changed)

        left_panel = QWidget()
        left_panel.setObjectName("BuilderSidebarPanel")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(14, 14, 14, 14)
        left_layout.setSpacing(10)
        left_header = QLabel("Saved Automations")
        left_header.setObjectName("BuilderPanelTitle")
        left_layout.addWidget(left_header)
        left_layout.addWidget(self.table, 1)

        primary_buttons = QHBoxLayout()
        for label, handler, danger in [
            ("New", self.new_automation, False),
            ("Duplicate", self.duplicate_current, False),
            ("Delete", self.delete_current, True),
        ]:
            button = QPushButton(label)
            button.clicked.connect(handler)
            if danger:
                button.setObjectName("DangerButton")
            primary_buttons.addWidget(button)
        left_layout.addLayout(primary_buttons)

        engine_buttons = QHBoxLayout()
        for label, handler in [
            ("Pause All", self.pause_all),
            ("Resume All", self.resume_all),
            ("Reload", self.reload),
        ]:
            button = QPushButton(label)
            button.clicked.connect(handler)
            engine_buttons.addWidget(button)
        left_layout.addLayout(engine_buttons)

        editor = QWidget()
        editor.setObjectName("BuilderEditor")
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(14)

        top_action_buttons = []
        for label, handler in [("Run Once", self.run_once), ("Save Automation", self.save_current)]:
            button = QPushButton(label)
            button.clicked.connect(handler)
            top_action_buttons.append(button)
        editor_layout.addWidget(responsive_grid(top_action_buttons, min_column_width=160, max_columns=2))

        details_box, details_layout = self.section("Automation Details")
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        form.addRow("Internal key", self.key_edit)
        form.addRow("Display name", self.name_edit)
        form.addRow("Trigger", self.trigger_combo)
        form.addRow("Run every", self.interval_spin)
        form.addRow("Cooldown after success", self.cooldown_spin)
        form.addRow("Debounce", self.debounce_spin)
        form.addRow("Status", self.enabled_check)
        details_layout.addLayout(form)
        self.description_edit.setFixedHeight(82)
        self.description_edit.setPlaceholderText("What does this automation do, and when should volunteers enable it?")
        details_layout.addWidget(QLabel("Description"))
        details_layout.addWidget(self.description_edit)
        helper = QLabel("Interval automations are picked up while the app is running. Add conditions to prevent actions from firing at the wrong time.")
        helper.setObjectName("HelpText")
        helper.setWordWrap(True)
        details_layout.addWidget(helper)
        editor_layout.addWidget(details_box)

        conditions_box, conditions_layout = self.section("Conditions")
        self.conditions_editor.setMinimumHeight(260)
        conditions_layout.addWidget(self.conditions_editor)
        editor_layout.addWidget(conditions_box)

        actions_box, actions_layout = self.section("Actions")
        self.actions_editor.setMinimumHeight(360)
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

    def configure_list_table(self) -> None:
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setMinimumHeight(42)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(2, 92)
        self.table.setWordWrap(False)
        self.table.setMinimumHeight(260)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def section(self, heading: str) -> tuple[QGroupBox, QVBoxLayout]:
        box = QGroupBox(heading)
        box.setObjectName("BuilderSection")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setSpacing(10)
        return box, layout

    def definitions(self) -> list[AutomationDefinition]:
        return sorted(self.context.automation_engine.definitions.values(), key=lambda item: item.name.lower())

    def automation_snapshot(self) -> list[AutomationDefinition]:
        return [
            AutomationDefinition.from_dict(definition.to_dict())
            for definition in sorted(self.context.automation_engine.definitions.values(), key=lambda item: item.key)
        ]

    def apply_automation_snapshot(self, automations: list[AutomationDefinition], message: str = "") -> None:
        restored = [AutomationDefinition.from_dict(definition.to_dict()) for definition in automations]
        self.context.automation_engine.definitions = {definition.key: definition for definition in restored}
        self.context.automation_engine.states = {
            key: self.context.automation_engine.states.get(key, AutomationRunState(key, definition.enabled))
            for key, definition in self.context.automation_engine.definitions.items()
        }
        for key, definition in self.context.automation_engine.definitions.items():
            self.context.automation_engine.states[key].enabled = definition.enabled
        self.context.config_repository.save_automations(list(self.context.automation_engine.definitions.values()))
        self.reload()
        if message:
            self.status.setText(message)

    def record_automation_change(
        self,
        label: str,
        before: list[AutomationDefinition],
        after: list[AutomationDefinition],
    ) -> None:
        before_data = [AutomationDefinition.from_dict(definition.to_dict()) for definition in before]
        after_data = [AutomationDefinition.from_dict(definition.to_dict()) for definition in after]
        if [item.to_dict() for item in before_data] == [item.to_dict() for item in after_data]:
            return
        self.context.undo_manager.record(
            label,
            lambda: self.apply_automation_snapshot(before_data, f"Undid: {label}"),
            lambda: self.apply_automation_snapshot(after_data, f"Redid: {label}"),
        )

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
                    item.name + ("" if item.enabled else " (disabled)"),
                    item.trigger,
                    f"{len(item.conditions)} cond / {len(item.actions)} act",
                ],
            )
            result = state.last_error or state.last_action_result or state.last_condition_result
            tooltip = (
                f"{item.name}\n"
                f"Trigger: {item.trigger}\n"
                f"Every: {item.interval_seconds:g} sec\n"
                f"Steps: {len(item.conditions)} conditions / {len(item.actions)} actions"
            )
            if result:
                tooltip += f"\nLast result: {result}"
            for column in range(self.table.columnCount()):
                self.table.item(row, column).setToolTip(tooltip)
            self.table.item(row, 0).setData(Qt.ItemDataRole.UserRole, item.key)
        self._loading = False
        if items:
            self.table.selectRow(0)
        else:
            self.new_automation()
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
            self.load_definition(definition)

    def load_definition(self, definition: AutomationDefinition) -> None:
        self._loading = True
        self.key_edit.setText(definition.key)
        self.name_edit.setText(definition.name)
        index = self.trigger_combo.findText(definition.trigger)
        if index < 0:
            self.trigger_combo.addItem(definition.trigger)
            index = self.trigger_combo.findText(definition.trigger)
        self.trigger_combo.setCurrentIndex(index)
        self.interval_spin.setValue(float(definition.interval_seconds))
        self.cooldown_spin.setValue(float(definition.cooldown_seconds))
        self.debounce_spin.setValue(float(definition.debounce_seconds))
        self.enabled_check.setChecked(definition.enabled)
        self.description_edit.setPlainText(definition.description)
        self.conditions_editor.set_conditions(definition.conditions)
        self.actions_editor.set_actions(definition.actions)
        self._loading = False

    def new_automation(self) -> None:
        self.table.clearSelection()
        definition = AutomationDefinition(
            key="new_automation",
            name="New Automation",
            trigger="interval",
            enabled=True,
            interval_seconds=1,
            description="",
            conditions=[{"condition_type": "always", "params": {}}],
            actions=[],
        )
        self.load_definition(definition)
        self.status.setText("New automation ready. Add conditions and actions, then save.")

    def duplicate_current(self) -> None:
        try:
            definition = self.current_definition_from_form()
            definition.key = f"{definition.key}_copy"
            definition.name = f"{definition.name} Copy"
            self.load_definition(definition)
            self.status.setText("Duplicated automation. Save when ready.")
        except Exception as exc:
            self.status.setText(f"Duplicate failed: {exc}")

    def current_definition_from_form(self) -> AutomationDefinition:
        name = self.name_edit.text().strip() or "New Automation"
        key = self.key_edit.text().strip() or keyify(name)
        return AutomationDefinition(
            key=key,
            name=name,
            trigger=self.trigger_combo.currentText(),
            enabled=self.enabled_check.isChecked(),
            interval_seconds=self.interval_spin.value(),
            cooldown_seconds=self.cooldown_spin.value(),
            debounce_seconds=self.debounce_spin.value(),
            conditions=self.conditions_editor.conditions(),
            actions=self.actions_editor.actions(),
            description=self.description_edit.toPlainText().strip(),
        )

    def save_current(self) -> None:
        try:
            before = self.automation_snapshot()
            row = self.table.currentRow()
            old_key = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) if row >= 0 and self.table.item(row, 0) else ""
            definition = self.current_definition_from_form()
            if old_key and old_key != definition.key:
                self.context.automation_engine.definitions.pop(old_key, None)
                self.context.automation_engine.states.pop(old_key, None)
            self.context.automation_engine.definitions[definition.key] = definition
            self.context.automation_engine.states.setdefault(definition.key, AutomationRunState(definition.key, definition.enabled))
            self.context.automation_engine.states[definition.key].enabled = definition.enabled
            self.context.config_repository.save_automations(list(self.context.automation_engine.definitions.values()))
            after = self.automation_snapshot()
            self.record_automation_change(f"Save automation {definition.name}", before, after)
            self.reload()
            self.select_key(definition.key)
            self.status.setText(f"Saved automation {definition.name}.")
        except Exception as exc:
            self.status.setText(f"Save failed: {exc}")

    def run_once(self) -> None:
        try:
            definition = self.current_definition_from_form()
        except Exception as exc:
            self.status.setText(f"Cannot run automation: {exc}")
            return
        self.status.setText("Running automation once...")

        async def run():
            ok, message = await evaluate_conditions(self.context, definition.conditions)
            if not ok:
                return f"Conditions not met: {message}"
            endpoint = EndpointDefinition(
                key=f"automation-test:{definition.key}",
                name=definition.name,
                route="/__automation_test",
                actions=definition.actions,
            )
            result = await self.context.endpoint_executor.execute(endpoint, {"automation_key": definition.key})
            return "Run completed." if result.ok else f"Run failed: {result.error}"

        run_background(run, lambda _ok, message: self.status.setText(message))

    def delete_current(self) -> None:
        row = self.table.currentRow()
        if row < 0 or not self.table.item(row, 0):
            self.status.setText("Select an automation to delete.")
            return
        before = self.automation_snapshot()
        key = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        self.context.automation_engine.definitions.pop(key, None)
        self.context.automation_engine.states.pop(key, None)
        self.context.config_repository.save_automations(list(self.context.automation_engine.definitions.values()))
        after = self.automation_snapshot()
        self.record_automation_change(f"Delete automation {key}", before, after)
        self.reload()
        self.status.setText(f"Deleted automation {key}.")

    def pause_all(self) -> None:
        self.context.automation_engine.pause_all()
        self.status.setText("All automations paused.")

    def resume_all(self) -> None:
        self.context.automation_engine.resume_all()
        self.status.setText("Automations resumed.")

    def select_key(self, key: str) -> None:
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == key:
                self.table.selectRow(row)
                break


def build_page(context) -> QWidget:
    return AutomationsPage(context)
