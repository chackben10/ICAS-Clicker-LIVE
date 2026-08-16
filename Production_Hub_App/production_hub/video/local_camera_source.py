from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass

from PySide6.QtCore import QCameraPermission, QCoreApplication, QObject, QTimer, Qt, Slot
from PySide6.QtMultimedia import QCamera, QCameraDevice, QCameraFormat, QMediaCaptureSession, QMediaDevices, QVideoFrame, QVideoSink

from production_hub.video.frame_broker import LatestFrameBroker
from production_hub.video.models import VideoSourceKey, VideoSourceState


@dataclass(frozen=True, slots=True)
class LocalCameraDevice:
    id: str
    name: str
    is_default: bool


def encode_device_id(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


class _LatestFrameConverter:
    def __init__(
        self,
        broker: LatestFrameBroker,
        source: VideoSourceKey,
        display_name: str,
    ) -> None:
        self.broker = broker
        self.source = source
        self.display_name = display_name
        self._condition = threading.Condition()
        self._pending: QVideoFrame | None = None
        self._stop = False
        self._received = 0
        self._thread = threading.Thread(
            target=self._run,
            name=f"production-hub-{source.value}-camera-convert",
            daemon=True,
        )
        self._thread.start()

    def submit(self, frame: QVideoFrame, received_frames: int) -> None:
        with self._condition:
            self._pending = QVideoFrame(frame)
            self._received = received_frames
            self._condition.notify()

    def stop(self) -> None:
        with self._condition:
            self._stop = True
            self._pending = None
            self._condition.notify()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._stop:
                    self._condition.wait()
                if self._stop:
                    return
                frame = self._pending
                received = self._received
                self._pending = None
            assert frame is not None
            image = frame.toImage()
            if image.isNull():
                self.broker.set_status(
                    self.source,
                    VideoSourceState.ERROR,
                    f"The {self.display_name} frame could not be converted.",
                    last_error="QVideoFrame.toImage returned a null image",
                )
                continue
            rate = 0.0
            start = frame.startTime()
            end = frame.endTime()
            if start >= 0 and end > start:
                rate = 1_000_000.0 / (end - start)
            self.broker.publish(
                self.source,
                image.copy(),
                frame_rate=rate,
                source_timestamp=max(0, start),
                received_frames=received,
            )


class LocalCameraVideoSource(QObject):
    """Qt Multimedia capture with conversion isolated from the GUI thread."""

    def __init__(
        self,
        broker: LatestFrameBroker,
        *,
        source: VideoSourceKey = VideoSourceKey.PTZ,
        display_name: str = "PTZ Cam",
        publish_fps: float = 12.0,
        preferred_width: int = 1920,
        preferred_height: int = 1080,
        preferred_fps: float = 30.0,
        stale_after_seconds: float = 1.5,
    ) -> None:
        super().__init__()
        self.broker = broker
        self.source = source
        self.display_name = display_name
        self.publish_fps = max(1.0, float(publish_fps))
        self.preferred_width = max(320, int(preferred_width))
        self.preferred_height = max(240, int(preferred_height))
        self.preferred_fps = max(1.0, float(preferred_fps))
        self.stale_after_seconds = max(0.5, float(stale_after_seconds))
        self.camera: QCamera | None = None
        self.capture_session: QMediaCaptureSession | None = None
        self.video_sink: QVideoSink | None = None
        self.converter: _LatestFrameConverter | None = None
        self.device_id = ""
        self._last_submit = 0.0
        self._received_frames = 0
        self._last_native_frame = 0.0
        self._start_requested_at = 0.0
        self._output_active = False
        self._permission_generation = 0
        self.watchdog = QTimer(self)
        self.watchdog.setInterval(500)
        self.watchdog.timeout.connect(self._check_stale)

    @staticmethod
    def available_devices() -> list[LocalCameraDevice]:
        default_id = bytes(QMediaDevices.defaultVideoInput().id())
        return [
            LocalCameraDevice(
                id=encode_device_id(bytes(device.id())),
                name=device.description(),
                is_default=bytes(device.id()) == default_id,
            )
            for device in QMediaDevices.videoInputs()
        ]

    @property
    def running(self) -> bool:
        return bool(self.camera and self.camera.isActive())

    def set_output_active(self, active: bool) -> None:
        self._output_active = bool(active)

    def start(self, device_id: str) -> None:
        self.stop()
        self._permission_generation += 1
        generation = self._permission_generation
        application = QCoreApplication.instance()
        if application is None:
            self._set_error("Camera capture requires the Production Hub desktop application.")
            return
        permission = QCameraPermission()
        permission_status = self._camera_permission_status()
        if permission_status == Qt.PermissionStatus.Undetermined:
            self.broker.set_status(
                self.source,
                VideoSourceState.STARTING,
                "Waiting for macOS camera permission…",
                source_name=device_id,
            )
            application.requestPermission(
                permission,
                self,
                lambda result: self._permission_updated(generation, device_id, result.status()),
            )
            return
        if permission_status == Qt.PermissionStatus.Denied:
            self._set_permission_denied()
            return
        self._open_device(device_id)

    def _permission_updated(
        self,
        generation: int,
        device_id: str,
        status: Qt.PermissionStatus,
    ) -> None:
        if generation != self._permission_generation:
            return
        if status == Qt.PermissionStatus.Granted:
            self._open_device(device_id)
        else:
            self._set_permission_denied()

    def _open_device(self, device_id: str) -> None:
        device = self._find_device(device_id)
        if device is None:
            self.broker.set_status(
                self.source,
                VideoSourceState.MISSING,
                f"Select a connected camera source for {self.display_name}.",
                source_name=device_id,
            )
            return
        self.device_id = encode_device_id(bytes(device.id()))
        self.broker.set_status(
            self.source,
            VideoSourceState.STARTING,
            f"Opening {device.description()}…",
            source_name=device.description(),
        )
        self.converter = _LatestFrameConverter(self.broker, self.source, self.display_name)
        self.video_sink = QVideoSink(self)
        self.video_sink.videoFrameChanged.connect(self._on_video_frame)
        self.capture_session = QMediaCaptureSession(self)
        self.camera = QCamera(device, self)
        selected_format = self._select_format(device)
        if not selected_format.isNull():
            self.camera.setCameraFormat(selected_format)
        self.camera.errorOccurred.connect(self._on_error)
        self.camera.activeChanged.connect(self._on_active_changed)
        self.capture_session.setCamera(self.camera)
        self.capture_session.setVideoSink(self.video_sink)
        self._last_submit = 0.0
        self._received_frames = 0
        self._last_native_frame = time.monotonic()
        self._start_requested_at = self._last_native_frame
        self.watchdog.start()
        self.camera.start()

    def stop(self) -> None:
        self._permission_generation += 1
        if self.camera:
            self.camera.stop()
        if self.capture_session:
            self.capture_session.setVideoSink(None)
            self.capture_session.setCamera(None)
        if self.converter:
            self.converter.stop()
        for item in (self.video_sink, self.capture_session, self.camera):
            if item:
                item.deleteLater()
        self.camera = None
        self.capture_session = None
        self.video_sink = None
        self.converter = None
        self._last_submit = 0.0
        self._start_requested_at = 0.0
        self.watchdog.stop()
        self.broker.set_status(self.source, VideoSourceState.STOPPED, "Stopped")

    @Slot(QVideoFrame)
    def _on_video_frame(self, frame: QVideoFrame) -> None:
        if not frame.isValid() or self.converter is None:
            return
        self._received_frames += 1
        now = time.monotonic()
        self._last_native_frame = now
        if not self._output_active:
            if self._received_frames % 30 == 0:
                self.broker.set_status(
                    self.source,
                    VideoSourceState.RUNNING,
                    "Receiving video; preview is suspended",
                    received_frames=self._received_frames,
                )
            return
        if now - self._last_submit < 1.0 / self.publish_fps:
            return
        self._last_submit = now
        self.converter.submit(frame, self._received_frames)

    @Slot(bool)
    def _on_active_changed(self, active: bool) -> None:
        if active:
            source_name = self.camera.cameraDevice().description() if self.camera else self.display_name
            self.broker.set_status(
                self.source,
                VideoSourceState.STARTING,
                f"Waiting for the first {self.display_name} frame…",
                source_name=source_name,
            )
        elif self.camera and self.camera.error() != QCamera.Error.NoError:
            self._on_error(self.camera.error(), self.camera.errorString())

    @Slot(QCamera.Error, str)
    def _on_error(self, _error: QCamera.Error, message: str) -> None:
        detail = message or f"The {self.display_name} device could not be opened. It may be busy."
        state = VideoSourceState.BUSY if "busy" in detail.casefold() or "use" in detail.casefold() else VideoSourceState.ERROR
        self.broker.set_status(self.source, state, detail, last_error=detail)

    def _check_stale(self) -> None:
        if not self.camera:
            return
        now = time.monotonic()
        if self._received_frames == 0 and self._start_requested_at > 0:
            age = now - self._start_requested_at
            if age >= max(5.0, self.stale_after_seconds * 2):
                application = QCoreApplication.instance()
                if (
                    application is not None
                    and self._camera_permission_status()
                    != Qt.PermissionStatus.Granted
                ):
                    self._set_permission_denied()
                else:
                    self.broker.set_status(
                        self.source,
                        VideoSourceState.ERROR,
                        (
                            f"{self.display_name} did not deliver a frame. The device may be in use "
                            "or its current video format may be unavailable."
                        ),
                        last_error="Camera start timed out before the first frame",
                    )
                self.watchdog.stop()
            return
        if not self.camera.isActive() or self._last_native_frame <= 0:
            return
        age = now - self._last_native_frame
        if age >= self.stale_after_seconds:
            self.broker.set_status(
                self.source,
                VideoSourceState.RECONNECTING,
                f"{self.display_name} is active but no frame has arrived for {age:.1f} seconds.",
                received_frames=self._received_frames,
            )

    def _set_permission_denied(self) -> None:
        message = (
            "Camera access is denied. Enable Production Hub in System Settings → "
            "Privacy & Security → Camera, then reconnect."
        )
        self.broker.set_status(
            self.source,
            VideoSourceState.PERMISSION_DENIED,
            message,
            last_error=message,
        )

    def _set_error(self, message: str) -> None:
        self.broker.set_status(
            self.source,
            VideoSourceState.ERROR,
            message,
            last_error=message,
        )

    @staticmethod
    def _camera_permission_status() -> Qt.PermissionStatus:
        application = QCoreApplication.instance()
        if application is None:
            return Qt.PermissionStatus.Denied
        return application.checkPermission(QCameraPermission())

    def _find_device(self, configured_id: str) -> QCameraDevice | None:
        devices = QMediaDevices.videoInputs()
        if configured_id:
            for device in devices:
                if encode_device_id(bytes(device.id())) == configured_id:
                    return device
            return None
        default = QMediaDevices.defaultVideoInput()
        return default if not default.isNull() else (devices[0] if devices else None)

    def _select_format(self, device: QCameraDevice) -> QCameraFormat:
        formats = device.videoFormats()
        if not formats:
            return QCameraFormat()

        def score(item: QCameraFormat) -> tuple[float, float, float]:
            resolution = item.resolution()
            oversize_penalty = 1.0 if (
                resolution.width() > self.preferred_width or resolution.height() > self.preferred_height
            ) else 0.0
            size_delta = abs(resolution.width() - self.preferred_width) + abs(
                resolution.height() - self.preferred_height
            )
            fps = min(item.maxFrameRate(), self.preferred_fps)
            return oversize_penalty, float(size_delta), abs(fps - self.preferred_fps)

        return min(formats, key=score)


# Backward-compatible name for integrations/tests written against Phase 1.0.
LocalPTZCameraSource = LocalCameraVideoSource
