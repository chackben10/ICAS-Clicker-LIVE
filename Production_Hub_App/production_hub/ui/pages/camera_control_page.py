from __future__ import annotations

import asyncio
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from production_hub.ui.pages.common import responsive_grid, run_background, scroll_page, title
from production_hub.video.models import VideoSourceKey, VideoSourceSnapshot, VideoSourceState


class VideoPreview(QFrame):
    def __init__(self, empty_text: str) -> None:
        super().__init__()
        self.setObjectName("VideoPreview")
        self.setMinimumSize(320, 180)
        self.setStyleSheet("QFrame#VideoPreview { background: #090b0f; border: 1px solid #343a46; border-radius: 8px; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.image_label = QLabel(empty_text)
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet("color: #9298a5; background: transparent;")
        self.image_label.setMinimumSize(300, 168)
        layout.addWidget(self.image_label)
        self._image: QImage | None = None
        self.sequence = 0

    def set_frame(self, image: QImage, sequence: int) -> None:
        if sequence == self.sequence:
            return
        self.sequence = sequence
        self._image = QImage(image)
        self._render()

    def clear(self, message: str) -> None:
        self.sequence = 0
        self._image = None
        self.image_label.setPixmap(QPixmap())
        self.image_label.setText(message)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._render()

    def _render(self) -> None:
        if self._image is None or self._image.isNull():
            return
        size = self.image_label.size()
        if size.width() <= 0 or size.height() <= 0:
            return
        pixmap = QPixmap.fromImage(self._image).scaled(
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.image_label.setText("")
        self.image_label.setPixmap(pixmap)


class CameraControlPage(QWidget):
    def __init__(self, context) -> None:
        super().__init__()
        self.context = context
        self.status = QLabel("Ready")
        self.status.setObjectName("StatusText")
        self.context.video.initialize_qt()
        self._last_recording_state = False
        self.build()
        self.refresh_local_devices()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(100)
        self.refresh_timer.timeout.connect(self.refresh_video_status)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if hasattr(self, "refresh_timer"):
            self.context.video.set_preview_active(True)
            self.refresh_timer.start()
            self.refresh_video_status()

    def hideEvent(self, event) -> None:
        if hasattr(self, "refresh_timer"):
            self.refresh_timer.stop()
            self.context.video.set_preview_active(False)
        super().hideEvent(event)

    def build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        scroll, _body, layout = scroll_page()
        root.addWidget(scroll)
        layout.addWidget(
            title(
                "Camera Control",
                "Live PTZ and Audience video diagnostics. Tracking and automated camera motion remain disabled in Phase 1.",
            )
        )

        layout.addWidget(self.video_group())
        layout.addWidget(self.diagnostics_group())

        layout.addWidget(
            responsive_grid(
                [self.system_group(), self.ptz_group(), self.preset_group()],
                min_column_width=300,
                max_columns=3,
            )
        )
        layout.addWidget(self.status)
        layout.addStretch()

    def video_group(self) -> QGroupBox:
        group = QGroupBox("Video Inputs")
        layout = QGridLayout(group)
        layout.setHorizontalSpacing(16)
        layout.setVerticalSpacing(12)

        audience = QWidget()
        audience_layout = QVBoxLayout(audience)
        audience_layout.setContentsMargins(0, 0, 0, 0)
        audience_heading = QLabel("Audience Cam · NDI")
        audience_heading.setObjectName("CardTitle")
        audience_layout.addWidget(audience_heading)
        self.audience_preview = VideoPreview("Waiting for Audience Cam")
        audience_layout.addWidget(self.audience_preview)
        self.audience_detail = QLabel("Not connected")
        self.audience_detail.setWordWrap(True)
        audience_layout.addWidget(self.audience_detail)
        audience_form = QFormLayout()
        self.ndi_source = QComboBox()
        self.ndi_source.setEditable(True)
        self.ndi_source.addItem(self.context.config.integrations.video.audience_ndi_source_name)
        self.ndi_source.setCurrentText(self.context.config.integrations.video.audience_ndi_source_name)
        audience_form.addRow("NDI source", self.ndi_source)
        self.ndi_highest_bandwidth = QCheckBox("Full-bandwidth video")
        self.ndi_highest_bandwidth.setChecked(self.context.config.integrations.video.audience_highest_bandwidth)
        audience_form.addRow("Quality", self.ndi_highest_bandwidth)
        audience_layout.addLayout(audience_form)
        audience_buttons = QHBoxLayout()
        self.ndi_refresh_button = QPushButton("Refresh")
        self.ndi_connect_button = QPushButton("Connect")
        self.ndi_disconnect_button = QPushButton("Disconnect")
        self.ndi_refresh_button.clicked.connect(self.refresh_ndi_sources)
        self.ndi_connect_button.clicked.connect(self.connect_audience)
        self.ndi_disconnect_button.clicked.connect(self.context.video.stop_audience)
        audience_buttons.addWidget(self.ndi_refresh_button)
        audience_buttons.addStretch()
        audience_buttons.addWidget(self.ndi_disconnect_button)
        audience_buttons.addWidget(self.ndi_connect_button)
        audience_layout.addLayout(audience_buttons)

        ptz = QWidget()
        ptz_layout = QVBoxLayout(ptz)
        ptz_layout.setContentsMargins(0, 0, 0, 0)
        ptz_heading = QLabel("PTZ Cam · Local Capture")
        ptz_heading.setObjectName("CardTitle")
        ptz_layout.addWidget(ptz_heading)
        self.ptz_preview = VideoPreview("Select the PTZ capture device")
        ptz_layout.addWidget(self.ptz_preview)
        self.ptz_detail = QLabel("Not connected")
        self.ptz_detail.setWordWrap(True)
        ptz_layout.addWidget(self.ptz_detail)
        ptz_form = QFormLayout()
        self.ptz_device = QComboBox()
        ptz_form.addRow("Capture device", self.ptz_device)
        ptz_layout.addLayout(ptz_form)
        ptz_buttons = QHBoxLayout()
        self.ptz_refresh_button = QPushButton("Refresh")
        self.ptz_connect_button = QPushButton("Connect")
        self.ptz_disconnect_button = QPushButton("Disconnect")
        self.ptz_refresh_button.clicked.connect(self.refresh_local_devices)
        self.ptz_connect_button.clicked.connect(self.connect_ptz)
        self.ptz_disconnect_button.clicked.connect(self.context.video.stop_ptz)
        ptz_buttons.addWidget(self.ptz_refresh_button)
        ptz_buttons.addStretch()
        ptz_buttons.addWidget(self.ptz_disconnect_button)
        ptz_buttons.addWidget(self.ptz_connect_button)
        ptz_layout.addLayout(ptz_buttons)

        layout.addWidget(audience, 0, 0)
        layout.addWidget(ptz, 0, 1)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        return group

    def diagnostics_group(self) -> QGroupBox:
        group = QGroupBox("Video Diagnostics and Replay")
        layout = QVBoxLayout(group)
        self.video_summary = QLabel("Initializing video services…")
        self.video_summary.setWordWrap(True)
        layout.addWidget(self.video_summary)

        controls = QHBoxLayout()
        self.record_button = QPushButton("Start Diagnostic Recording")
        self.record_button.setEnabled(self.context.video.recording_available)
        self.record_button.clicked.connect(self.toggle_recording)
        replay_button = QPushButton("Replay Recording…")
        replay_button.setEnabled(self.context.video.recording_available)
        replay_button.clicked.connect(self.choose_replay)
        stop_replay_button = QPushButton("Stop Replay")
        stop_replay_button.clicked.connect(self.context.video.stop_replay)
        reveal_button = QPushButton("Show Recordings")
        reveal_button.clicked.connect(self.reveal_recordings)
        controls.addWidget(self.record_button)
        controls.addWidget(replay_button)
        controls.addWidget(stop_replay_button)
        controls.addStretch()
        controls.addWidget(reveal_button)
        layout.addLayout(controls)

        if not self.context.video.recording_available:
            missing = QLabel("Recording/replay is unavailable because Qt Multimedia could not initialize.")
            missing.setWordWrap(True)
            layout.addWidget(missing)

        self.replay_preview = VideoPreview("No diagnostic replay loaded")
        self.replay_preview.setMaximumHeight(260)
        layout.addWidget(self.replay_preview)
        self.replay_detail = QLabel("Replay stopped")
        layout.addWidget(self.replay_detail)
        return group

    def refresh_video_status(self) -> None:
        for source, preview, detail in (
            (VideoSourceKey.AUDIENCE, self.audience_preview, self.audience_detail),
            (VideoSourceKey.PTZ, self.ptz_preview, self.ptz_detail),
            (VideoSourceKey.REPLAY, self.replay_preview, self.replay_detail),
        ):
            packet = self.context.video.frame(source)
            if packet is not None:
                preview.set_frame(packet.image, packet.sequence)
            snapshot = self.context.video.snapshot(source)
            detail.setText(self.format_video_status(snapshot))

        discovered = self.context.video.audience.discovered_sources
        existing = {self.ndi_source.itemText(index) for index in range(self.ndi_source.count())}
        current = self.ndi_source.currentText()
        for source_name in discovered:
            if source_name not in existing:
                self.ndi_source.addItem(source_name)
        self.ndi_source.setCurrentText(current)

        audience = self.context.video.snapshot(VideoSourceKey.AUDIENCE)
        ptz = self.context.video.snapshot(VideoSourceKey.PTZ)
        ndi_version = self.context.video.audience.runtime_version
        recording = self.context.video.recorder.recording
        recording_text = "recording" if recording else "not recording"
        self.video_summary.setText(
            f"NDI {ndi_version} · Audience {audience.state.value} · PTZ {ptz.state.value} · {recording_text}. "
            "Pixel conversion and encoding work is bounded and isolated from the UI thread."
        )
        if recording != self._last_recording_state:
            self._last_recording_state = recording
            self.record_button.setText("Stop Diagnostic Recording" if recording else "Start Diagnostic Recording")

    @staticmethod
    def format_video_status(snapshot: VideoSourceSnapshot) -> str:
        parts = [snapshot.state.value.replace("_", " ").title()]
        if snapshot.negotiated_format:
            parts.append(snapshot.negotiated_format)
        if snapshot.effective_fps > 0:
            parts.append(f"preview {snapshot.effective_fps:.1f} fps")
        age = snapshot.frame_age_seconds
        if age is not None:
            parts.append(f"age {age * 1000:.0f} ms")
        if snapshot.dropped_frames:
            parts.append(f"NDI drops {snapshot.dropped_frames}")
        if snapshot.message and snapshot.message != "Receiving video":
            parts.append(snapshot.message)
        return " · ".join(parts)

    def refresh_ndi_sources(self) -> None:
        self.ndi_refresh_button.setEnabled(False)
        self.status.setText("Discovering NDI sources…")

        async def work() -> str:
            sources = await asyncio.to_thread(self.context.video.audience.discover_now, 800)
            return f"Found {len(sources)} NDI source(s)."

        def done(ok: bool, message: str) -> None:
            self.ndi_refresh_button.setEnabled(True)
            self.status.setText(message if ok else f"NDI discovery failed: {message}")

        run_background(work, done)

    def refresh_local_devices(self) -> None:
        current = self.ptz_device.currentData()
        configured = self.context.config.integrations.video.ptz_device_id
        self.ptz_device.clear()
        devices = self.context.video.local_devices()
        for device in devices:
            suffix = " (Default)" if device.is_default else ""
            self.ptz_device.addItem(f"{device.name}{suffix}", device.id)
        wanted = current or configured
        if wanted:
            index = self.ptz_device.findData(wanted)
            if index >= 0:
                self.ptz_device.setCurrentIndex(index)
        self.ptz_connect_button.setEnabled(bool(devices))
        if not devices:
            self.ptz_detail.setText("No local video capture devices were found.")

    def connect_audience(self) -> None:
        source_name = self.ndi_source.currentText().strip()
        if not source_name:
            self.status.setText("Choose an NDI source first.")
            return
        current = self.context.config.integrations.video
        config = type(current).from_dict(current.to_dict())
        config.audience_ndi_source_name = source_name
        config.audience_highest_bandwidth = self.ndi_highest_bandwidth.isChecked()
        config.audience_enabled = True
        self.context.config.integrations.video = config
        self.context.config_repository.save_app_config(self.context.config)
        self.context.video.reconfigure(config)
        self.context.video.start_audience()
        self.status.setText(f"Connecting to {source_name}…")

    def connect_ptz(self) -> None:
        device_id = self.ptz_device.currentData()
        if not device_id:
            self.status.setText("Choose a PTZ capture device first.")
            return
        current = self.context.config.integrations.video
        config = type(current).from_dict(current.to_dict())
        config.ptz_device_id = str(device_id)
        config.ptz_enabled = True
        self.context.config.integrations.video = config
        self.context.config_repository.save_app_config(self.context.config)
        self.context.video.reconfigure(config)
        self.context.video.start_ptz(str(device_id))
        self.status.setText(f"Opening {self.ptz_device.currentText()}…")

    def toggle_recording(self) -> None:
        try:
            if self.context.video.recorder.recording:
                directory = self.context.video.stop_recording()
                self.status.setText(f"Diagnostic recording saved to {directory}" if directory else "Recording stopped.")
            else:
                directory = self.context.video.start_recording()
                self.status.setText(f"Recording Audience and PTZ diagnostics to {directory}")
        except Exception as exc:
            self.status.setText(f"Recording failed: {exc}")

    def choose_replay(self) -> None:
        start = self.context.video.recorder.last_directory or self.context.video.recordings_root
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Replay Diagnostic Video",
            str(start),
            "Video files (*.mp4 *.mov *.mkv);;All files (*)",
        )
        if not path:
            return
        try:
            self.context.video.start_replay(Path(path))
            self.status.setText(f"Replaying {Path(path).name}")
        except Exception as exc:
            self.status.setText(f"Replay failed: {exc}")

    def reveal_recordings(self) -> None:
        root = self.context.video.recordings_root
        root.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(root)))

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
        group = QGroupBox("PTZ and Lens")
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
