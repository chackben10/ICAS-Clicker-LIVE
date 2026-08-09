from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass

from PySide6.QtCore import QObject, QTimer, Slot
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
    def __init__(self, broker: LatestFrameBroker) -> None:
        self.broker = broker
        self._condition = threading.Condition()
        self._pending: QVideoFrame | None = None
        self._stop = False
        self._received = 0
        self._thread = threading.Thread(target=self._run, name="production-hub-ptz-convert", daemon=True)
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
                    VideoSourceKey.PTZ,
                    VideoSourceState.ERROR,
                    "The PTZ capture frame could not be converted.",
                    last_error="QVideoFrame.toImage returned a null image",
                )
                continue
            rate = 0.0
            start = frame.startTime()
            end = frame.endTime()
            if start >= 0 and end > start:
                rate = 1_000_000.0 / (end - start)
            self.broker.publish(
                VideoSourceKey.PTZ,
                image.copy(),
                frame_rate=rate,
                source_timestamp=max(0, start),
                received_frames=received,
            )


class LocalPTZCameraSource(QObject):
    """Qt Multimedia capture with conversion isolated from the GUI thread."""

    def __init__(
        self,
        broker: LatestFrameBroker,
        *,
        publish_fps: float = 12.0,
        preferred_width: int = 1920,
        preferred_height: int = 1080,
        preferred_fps: float = 30.0,
        stale_after_seconds: float = 1.5,
    ) -> None:
        super().__init__()
        self.broker = broker
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
        self._output_active = False
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
        device = self._find_device(device_id)
        if device is None:
            self.broker.set_status(
                VideoSourceKey.PTZ,
                VideoSourceState.MISSING,
                "Select a connected PTZ capture device.",
                source_name=device_id,
            )
            return
        self.device_id = encode_device_id(bytes(device.id()))
        self.broker.set_status(
            VideoSourceKey.PTZ,
            VideoSourceState.STARTING,
            f"Opening {device.description()}…",
            source_name=device.description(),
        )
        self.converter = _LatestFrameConverter(self.broker)
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
        self.watchdog.start()
        self.camera.start()

    def stop(self) -> None:
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
        self.watchdog.stop()
        self.broker.set_status(VideoSourceKey.PTZ, VideoSourceState.STOPPED, "Stopped")

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
                    VideoSourceKey.PTZ,
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
            source_name = self.camera.cameraDevice().description() if self.camera else "PTZ capture"
            self.broker.set_status(
                VideoSourceKey.PTZ,
                VideoSourceState.STARTING,
                "Waiting for the first PTZ frame…",
                source_name=source_name,
            )
        elif self.camera and self.camera.error() != QCamera.Error.NoError:
            self._on_error(self.camera.error(), self.camera.errorString())

    @Slot(QCamera.Error, str)
    def _on_error(self, _error: QCamera.Error, message: str) -> None:
        detail = message or "The PTZ capture device could not be opened. It may be busy."
        state = VideoSourceState.BUSY if "busy" in detail.casefold() or "use" in detail.casefold() else VideoSourceState.ERROR
        self.broker.set_status(VideoSourceKey.PTZ, state, detail, last_error=detail)

    def _check_stale(self) -> None:
        if not self.camera or not self.camera.isActive() or self._last_native_frame <= 0:
            return
        age = time.monotonic() - self._last_native_frame
        if age >= self.stale_after_seconds:
            self.broker.set_status(
                VideoSourceKey.PTZ,
                VideoSourceState.RECONNECTING,
                f"PTZ capture is active but no frame has arrived for {age:.1f} seconds.",
                received_frames=self._received_frames,
            )

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
