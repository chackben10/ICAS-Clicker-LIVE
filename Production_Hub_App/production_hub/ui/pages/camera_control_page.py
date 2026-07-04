from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from production_hub.ui.pages.common import int_from_line_edit, integer_line_edit, responsive_grid, run_background, scroll_page, title


class CameraControlPage(QWidget):
    def __init__(self, context) -> None:
        super().__init__()
        self.context = context
        self.status = QLabel("Ready")
        self.status.setObjectName("StatusText")
        self.build()

    def build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        scroll, _body, layout = scroll_page()
        root.addWidget(scroll)
        layout.addWidget(title("Camera Control", "Panasonic AWP setup, PTZ diagnostics, VISCA bridge settings, and presets."))
        layout.addWidget(self.connection_group())

        layout.addWidget(
            responsive_grid(
                [self.system_group(), self.ptz_group(), self.preset_group()],
                min_column_width=300,
                max_columns=3,
            )
        )
        layout.addWidget(self.status)
        layout.addStretch()

    def connection_group(self) -> QGroupBox:
        cfg = self.context.config.integrations
        group = QGroupBox("Network Configuration")
        form = QFormLayout(group)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        self.camera_ip = QLineEdit(cfg.panasonic.camera_ip)
        self.camera_user = QLineEdit(cfg.panasonic.username)
        self.camera_password = QLineEdit(cfg.panasonic.password)
        self.camera_password.setEchoMode(QLineEdit.EchoMode.Password)
        show_password = QCheckBox("Show")
        show_password.toggled.connect(
            lambda checked: self.camera_password.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        self.visca_ip = QLineEdit(cfg.visca.listen_ip)
        self.visca_port = integer_line_edit(cfg.visca.udp_port, 1, 65535)
        self.reuse_port = QCheckBox("Shared port")
        self.reuse_port.setChecked(cfg.visca.reuse_port)

        password_row = QWidget()
        password_layout = QHBoxLayout(password_row)
        password_layout.setContentsMargins(0, 0, 0, 0)
        password_layout.setSpacing(8)
        password_layout.addWidget(self.camera_password, 1)
        password_layout.addWidget(show_password)

        visca_row = QWidget()
        visca_layout = QHBoxLayout(visca_row)
        visca_layout.setContentsMargins(0, 0, 0, 0)
        visca_layout.setSpacing(8)
        visca_layout.addWidget(self.visca_port)
        visca_layout.addWidget(self.reuse_port)
        visca_layout.addStretch()

        form.addRow("Camera IP", self.camera_ip)
        form.addRow("User", self.camera_user)
        form.addRow("Password", password_row)
        form.addRow("VISCA listen IP", self.visca_ip)
        form.addRow("UDP port", visca_row)
        save = QPushButton("Save Settings")
        save.clicked.connect(self.save_settings)
        form.addRow("", save)
        return group

    def system_group(self) -> QGroupBox:
        group = QGroupBox("System")
        layout = QGridLayout(group)
        buttons = [
            ("Menu ON", "DUS:1", "aw_cam"),
            ("Menu OFF", "DUS:0", "aw_cam"),
            ("Camera Feed", "DCB:0", "aw_cam"),
            ("Color Bars", "DCB:1", "aw_cam"),
            ("Power ON", "#On", "aw_ptz"),
            ("Standby", "#Of", "aw_ptz"),
            ("AWB", "#AWA", "aw_ptz"),
            ("Menu Up", "DUP:1", "aw_cam"),
            ("Menu Left", "DLT:1", "aw_cam"),
            ("Menu OK", "DIT:1", "aw_cam"),
            ("Menu Right", "DRT:1", "aw_cam"),
            ("Menu Down", "DDW:1", "aw_cam"),
        ]
        for idx, (label, command, endpoint) in enumerate(buttons):
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, c=command, e=endpoint: self.send_command(c, e))
            layout.addWidget(button, idx // 2, idx % 2)
        return group

    def ptz_group(self) -> QGroupBox:
        group = QGroupBox("PTZ & Lens")
        layout = QVBoxLayout(group)

        self.pan_speed = self.slider("Pan/Tilt Speed", 1, 49, self.context.config.integrations.panasonic.default_pan_tilt_speed, layout)
        arrows = QWidget()
        arrows_layout = QGridLayout(arrows)
        for label, row, col, command_factory in [
            ("Up", 0, 1, lambda: f"#PTS50{50 + self.pan_speed.value():02d}"),
            ("Left", 1, 0, lambda: f"#PTS{50 - self.pan_speed.value():02d}50"),
            ("Stop", 1, 1, lambda: "#PTS5050"),
            ("Right", 1, 2, lambda: f"#PTS{50 + self.pan_speed.value():02d}50"),
            ("Down", 2, 1, lambda: f"#PTS50{50 - self.pan_speed.value():02d}"),
        ]:
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, factory=command_factory: self.send_command(factory()))
            arrows_layout.addWidget(button, row, col)
        layout.addWidget(arrows)

        self.zoom_speed = self.slider("Zoom Speed", 1, 49, self.context.config.integrations.panasonic.default_zoom_speed, layout)
        zoom_row = QHBoxLayout()
        for label, command_factory in [
            ("Zoom Out", lambda: f"#Z{50 - self.zoom_speed.value():02d}"),
            ("Zoom Stop", lambda: "#Z50"),
            ("Zoom In", lambda: f"#Z{50 + self.zoom_speed.value():02d}"),
        ]:
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, factory=command_factory: self.send_command(factory()))
            zoom_row.addWidget(button)
        layout.addLayout(zoom_row)

        self.focus_speed = self.slider("Focus Speed", 1, 49, self.context.config.integrations.panasonic.default_focus_speed, layout)
        focus_row = QHBoxLayout()
        for label, command_factory in [
            ("Auto", lambda: "#D11"),
            ("Manual", lambda: "#D10"),
            ("Near", lambda: f"#F{50 - self.focus_speed.value():02d}"),
            ("Stop", lambda: "#F50"),
            ("Far", lambda: f"#F{50 + self.focus_speed.value():02d}"),
        ]:
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, factory=command_factory: self.send_command(factory()))
            focus_row.addWidget(button)
        layout.addLayout(focus_row)
        return group

    def slider(self, label: str, low: int, high: int, value: int, parent_layout: QVBoxLayout) -> QSlider:
        row = QHBoxLayout()
        text = QLabel(label)
        value_label = QLabel(str(value))
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(low, high)
        slider.setValue(value)
        slider.valueChanged.connect(lambda val: value_label.setText(str(val)))
        row.addWidget(text)
        row.addWidget(slider, 1)
        row.addWidget(value_label)
        parent_layout.addLayout(row)
        return slider

    def preset_group(self) -> QGroupBox:
        group = QGroupBox("Position Presets")
        layout = QVBoxLayout(group)
        self.preset_list = QListWidget()
        self.reload_presets()
        layout.addWidget(self.preset_list)
        row = QHBoxLayout()
        recall = QPushButton("Recall")
        save = QPushButton("Save")
        rename = QPushButton("Rename")
        recall.clicked.connect(self.recall_selected_preset)
        save.clicked.connect(self.save_selected_preset)
        rename.clicked.connect(self.rename_selected_preset)
        row.addWidget(recall)
        row.addWidget(save)
        row.addWidget(rename)
        layout.addLayout(row)
        return group

    def reload_presets(self) -> None:
        if not hasattr(self, "preset_list"):
            self.preset_list = QListWidget()
        self.preset_list.clear()
        for item in self.context.panasonic_presets.list_presets():
            text = f"Preset {int(item['number']):02d}"
            if item["name"]:
                text += f" - {item['name']}"
            row = QListWidgetItem(text)
            row.setData(Qt.ItemDataRole.UserRole, int(item["number"]))
            self.preset_list.addItem(row)

    def selected_preset(self) -> int | None:
        item = self.preset_list.currentItem()
        if item is None:
            self.status.setText("Select a preset first.")
            return None
        return int(item.data(Qt.ItemDataRole.UserRole))

    def save_settings(self) -> None:
        cfg = self.context.config.integrations
        cfg.panasonic.camera_ip = self.camera_ip.text().strip()
        cfg.panasonic.username = self.camera_user.text().strip()
        cfg.panasonic.password = self.camera_password.text()
        cfg.panasonic.default_pan_tilt_speed = self.pan_speed.value()
        cfg.panasonic.default_zoom_speed = self.zoom_speed.value()
        cfg.panasonic.default_focus_speed = self.focus_speed.value()
        cfg.visca.listen_ip = self.visca_ip.text().strip()
        cfg.visca.udp_port = int_from_line_edit(self.visca_port, cfg.visca.udp_port)
        cfg.visca.reuse_port = self.reuse_port.isChecked()
        self.context.config_repository.save_app_config(self.context.config)
        self.status.setText("Camera and VISCA settings saved.")

    def send_command(self, command: str, endpoint: str = "aw_ptz") -> None:
        self.status.setText(f"Sending {command}...")
        run_background(
            lambda: self.context.panasonic.send_command(command, endpoint),
            lambda ok, message: self.status.setText("Command sent." if ok else f"Command failed: {message}"),
        )

    def recall_selected_preset(self) -> None:
        number = self.selected_preset()
        if number is not None:
            self.send_command(f"#R{number:02d}")

    def save_selected_preset(self) -> None:
        number = self.selected_preset()
        if number is None:
            return
        if number == 0:
            self.status.setText("Preset 00 is Home and cannot be overwritten.")
            return
        self.send_command(f"#M{number:02d}")

    def rename_selected_preset(self) -> None:
        number = self.selected_preset()
        if number is None or number == 0:
            self.status.setText("Preset 00 is Home and cannot be renamed.")
            return
        current = self.context.config.integrations.panasonic.preset_names.get(str(number), "")
        name, ok = QInputDialog.getText(self, "Rename Preset", f"Name for Preset {number:02d}:", text=current)
        if ok:
            self.context.panasonic_presets.rename(number, name)
            self.context.config_repository.save_app_config(self.context.config)
            self.reload_presets()
            self.status.setText(f"Preset {number:02d} renamed.")


def build_page(context) -> QWidget:
    return CameraControlPage(context)
