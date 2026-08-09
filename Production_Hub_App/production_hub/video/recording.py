from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QCoreApplication, QObject, QSize, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import (
    QMediaCaptureSession,
    QMediaFormat,
    QMediaPlayer,
    QMediaRecorder,
    QVideoFrame,
    QVideoFrameInput,
    QVideoSink,
)

from production_hub.video.frame_broker import LatestFrameBroker
from production_hub.video.models import VideoSourceKey, VideoSourceState


class RecordingUnavailableError(RuntimeError):
    pass


def qt_recording_available() -> bool:
    """Return whether the Qt application needed by the media backend exists."""

    return QCoreApplication.instance() is not None


class _RecorderSignalBridge(QObject):
    frame_ready = Signal(int, object, QImage)


class _QtSourceRecorder(QObject):
    """One Qt Multimedia encoder with a single replaceable pending frame."""

    def __init__(
        self,
        source: VideoSourceKey,
        path: Path,
        frame_rate: float,
        size: QSize,
        on_error,
        on_stopped,
        parent: QObject,
    ) -> None:
        super().__init__(parent)
        self.source = source
        self.path = path
        self.frame_rate = max(1.0, float(frame_rate))
        self._on_error_callback = on_error
        self._on_stopped_callback = on_stopped
        self._pending: QImage | None = None
        self._frame_index = 0
        self._stopping = False
        self._reported_stopped = False

        media_format = QMediaFormat()
        media_format.setFileFormat(QMediaFormat.FileFormat.MPEG4)
        media_format.setVideoCodec(QMediaFormat.VideoCodec.MPEG4)
        if not media_format.isSupported(QMediaFormat.ConversionMode.Encode):
            raise RecordingUnavailableError("Qt Multimedia cannot encode MPEG-4 video on this computer")

        self.capture_session = QMediaCaptureSession(self)
        self.video_input = QVideoFrameInput(self)
        self.recorder = QMediaRecorder(self)
        self.recorder.setMediaFormat(media_format)
        self.recorder.setEncodingMode(QMediaRecorder.EncodingMode.ConstantQualityEncoding)
        self.recorder.setQuality(QMediaRecorder.Quality.NormalQuality)
        self.recorder.setVideoFrameRate(self.frame_rate)
        self.recorder.setVideoResolution(size)
        self.recorder.setOutputLocation(QUrl.fromLocalFile(str(path)))
        self.capture_session.setVideoFrameInput(self.video_input)
        self.capture_session.setRecorder(self.recorder)
        self.video_input.readyToSendVideoFrame.connect(self._try_send)
        self.recorder.errorOccurred.connect(self._on_error)
        self.recorder.recorderStateChanged.connect(self._on_state_changed)
        self.recorder.record()

    def offer(self, image: QImage) -> None:
        if self._stopping:
            return
        self._pending = QImage(image)
        self._try_send()

    @Slot()
    def _try_send(self) -> None:
        if self._pending is None or self._stopping:
            return
        if self.recorder.recorderState() != QMediaRecorder.RecorderState.RecordingState:
            return
        image = self._pending
        frame = QVideoFrame(image)
        start_time = int(self._frame_index * 1_000_000 / self.frame_rate)
        end_time = int((self._frame_index + 1) * 1_000_000 / self.frame_rate)
        frame.setStartTime(start_time)
        frame.setEndTime(end_time)
        if self.video_input.sendVideoFrame(frame):
            if self._pending is image:
                self._pending = None
            self._frame_index += 1

    def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._pending = None
        if self.recorder.recorderState() == QMediaRecorder.RecorderState.StoppedState:
            self._report_stopped()
        else:
            self.recorder.stop()

    @Slot(QMediaRecorder.Error, str)
    def _on_error(self, _error: QMediaRecorder.Error, message: str) -> None:
        self._on_error_callback(self.source, message or "Qt Multimedia recording failed")

    @Slot(QMediaRecorder.RecorderState)
    def _on_state_changed(self, state: QMediaRecorder.RecorderState) -> None:
        if state == QMediaRecorder.RecorderState.RecordingState:
            self._try_send()
        elif state == QMediaRecorder.RecorderState.StoppedState and self._stopping:
            self._report_stopped()

    def _report_stopped(self) -> None:
        if self._reported_stopped:
            return
        self._reported_stopped = True
        self._on_stopped_callback(self.source)


class _QtRecorderController(QObject):
    """Owns recorder QObjects on the GUI thread."""

    def __init__(self, owner: DiagnosticRecorder) -> None:
        super().__init__()
        self.owner = owner
        self.token = 0
        self.directory: Path | None = None
        self.sources: set[VideoSourceKey] = set()
        self.outputs: dict[VideoSourceKey, _QtSourceRecorder] = {}
        self.stopped_outputs: set[VideoSourceKey] = set()
        self.accepting = False

    def begin_session(
        self,
        token: int,
        directory: Path,
        sources: tuple[VideoSourceKey, ...],
    ) -> None:
        if self.accepting or self.outputs:
            raise RuntimeError("A Qt recording session is already active")
        self.token = token
        self.directory = directory
        self.sources = set(sources)
        self.stopped_outputs.clear()
        self.accepting = True

    @Slot(int, object, QImage)
    def accept_frame(self, token: int, source: object, image: QImage) -> None:
        try:
            if token != self.token or not self.accepting or source not in self.sources:
                return
            assert isinstance(source, VideoSourceKey)
            output = self.outputs.get(source)
            if output is None:
                assert self.directory is not None
                path = self.directory / f"{source.value}.mp4"
                output = _QtSourceRecorder(
                    source,
                    path,
                    self.owner._session_frame_rate,
                    image.size(),
                    self._on_output_error,
                    self._on_output_stopped,
                    self,
                )
                self.outputs[source] = output
                self.owner._register_file(token, source, path.name)
            output.offer(image)
        except Exception as exc:
            self.owner._set_error(token, str(exc))
        finally:
            if isinstance(source, VideoSourceKey):
                self.owner._delivery_ack(token, source)

    def stop_session(self, token: int) -> None:
        if token != self.token:
            return
        self.accepting = False
        if not self.outputs:
            self._finish()
            return
        for output in list(self.outputs.values()):
            output.stop()

    def _on_output_error(self, source: VideoSourceKey, message: str) -> None:
        self.owner._set_error(self.token, f"{source.value}: {message}")

    def _on_output_stopped(self, source: VideoSourceKey) -> None:
        self.stopped_outputs.add(source)
        if not self.accepting and self.stopped_outputs == set(self.outputs):
            self._finish()

    def _finish(self) -> None:
        token = self.token
        for output in self.outputs.values():
            output.deleteLater()
        self.outputs.clear()
        self.stopped_outputs.clear()
        self.sources.clear()
        self.directory = None
        self.accepting = False
        self.owner._finalize_session(token)


class DiagnosticRecorder:
    """Record bounded diagnostic streams with Qt's in-process media backend."""

    def __init__(
        self,
        broker: LatestFrameBroker,
        recordings_root: Path,
        frame_rate: float = 10.0,
        max_width: int = 1280,
    ) -> None:
        self.broker = broker
        self.recordings_root = recordings_root
        self.frame_rate = max(1.0, min(30.0, float(frame_rate)))
        self.max_width = max(320, min(3840, int(max_width)))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._bridge: _RecorderSignalBridge | None = None
        self._controller: _QtRecorderController | None = None
        self._token = 0
        self._active = False
        self._delivery_inflight: dict[VideoSourceKey, bool] = {}
        self._record_sizes: dict[VideoSourceKey, QSize] = {}
        self._files: dict[str, str] = {}
        self._session_frame_rate = self.frame_rate
        self._session_max_width = self.max_width
        self.current_directory: Path | None = None
        self.last_directory: Path | None = None
        self.last_error = ""
        self.started_at = ""
        self.state_changed_callback: Callable[[], None] | None = None

    @property
    def available(self) -> bool:
        return self._controller is not None

    @property
    def recording(self) -> bool:
        with self._lock:
            return self._active

    def initialize_qt(self) -> None:
        if self._controller is not None:
            return
        if not qt_recording_available():
            raise RecordingUnavailableError("Initialize recording after creating QApplication")
        self._bridge = _RecorderSignalBridge()
        self._controller = _QtRecorderController(self)
        self._bridge.frame_ready.connect(
            self._controller.accept_frame,
            Qt.ConnectionType.QueuedConnection,
        )

    def start(
        self,
        sources: tuple[VideoSourceKey, ...] = (VideoSourceKey.AUDIENCE, VideoSourceKey.PTZ),
    ) -> Path:
        if self._controller is None or self._bridge is None:
            raise RecordingUnavailableError("Qt video recording has not been initialized")
        with self._lock:
            if self._active:
                assert self.current_directory is not None
                return self.current_directory
            stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
            directory = self.recordings_root / stamp
            directory.mkdir(parents=True, exist_ok=False)
            self._token += 1
            token = self._token
            self.current_directory = directory
            self.last_error = ""
            self.started_at = datetime.now(UTC).isoformat()
            self._delivery_inflight = {source: False for source in sources}
            self._record_sizes.clear()
            self._files.clear()
            self._session_frame_rate = self.frame_rate
            self._session_max_width = self.max_width
            self._active = True
        self._controller.begin_session(token, directory, sources)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(token, sources),
            name="production-hub-video-record-prep",
            daemon=True,
        )
        self._thread.start()
        return directory

    def stop(self, *, wait: bool = False) -> Path | None:
        with self._lock:
            if not self._active:
                return self.last_directory
            token = self._token
            directory = self.current_directory
        self._stop_event.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3)
        self._thread = None
        assert self._controller is not None
        self._controller.stop_session(token)
        if wait:
            deadline = time.monotonic() + 5.0
            app = QCoreApplication.instance()
            while self.recording and time.monotonic() < deadline:
                if app is not None:
                    app.processEvents()
                time.sleep(0.01)
        return directory

    def _run(self, token: int, sources: tuple[VideoSourceKey, ...]) -> None:
        last_sequences = {source: 0 for source in sources}
        interval = 1.0 / self._session_frame_rate
        next_due = time.monotonic()
        while not self._stop_event.is_set():
            now = time.monotonic()
            if now < next_due:
                self._stop_event.wait(next_due - now)
                continue
            next_due = max(next_due + interval, now)
            for source in sources:
                with self._lock:
                    if token != self._token or self._delivery_inflight.get(source, False):
                        continue
                packet = self.broker.frame(source)
                if packet is None or packet.sequence == last_sequences[source]:
                    continue
                try:
                    image = self._prepare_image(source, packet.image)
                except Exception as exc:
                    self._set_error(token, f"{source.value}: frame preparation failed: {exc}")
                    continue
                with self._lock:
                    if token != self._token or not self._active:
                        return
                    self._delivery_inflight[source] = True
                assert self._bridge is not None
                self._bridge.frame_ready.emit(token, source, image)
                last_sequences[source] = packet.sequence

    def _prepare_image(self, source: VideoSourceKey, image: QImage) -> QImage:
        with self._lock:
            target = self._record_sizes.get(source)
            if target is None:
                scale = min(1.0, self._session_max_width / max(1, image.width()))
                width = max(2, int(image.width() * scale))
                height = max(2, int(image.height() * scale))
                width -= width % 2
                height -= height % 2
                target = QSize(width, height)
                self._record_sizes[source] = target
        prepared = image.convertToFormat(QImage.Format.Format_ARGB32)
        if prepared.size() != target:
            prepared = prepared.scaled(
                target,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
        return prepared

    def _delivery_ack(self, token: int, source: VideoSourceKey) -> None:
        with self._lock:
            if token == self._token:
                self._delivery_inflight[source] = False

    def _register_file(self, token: int, source: VideoSourceKey, filename: str) -> None:
        with self._lock:
            if token == self._token:
                self._files[source.value] = filename

    def _set_error(self, token: int, message: str) -> None:
        with self._lock:
            if token != self._token:
                return
            if self.last_error:
                if message not in self.last_error:
                    self.last_error += f"; {message}"
            else:
                self.last_error = message

    def _finalize_session(self, token: int) -> None:
        with self._lock:
            if token != self._token or not self._active:
                return
            directory = self.current_directory
            if directory is None:
                return
            manifest: dict[str, Any] = {
                "schema_version": 1,
                "started_at": self.started_at,
                "ended_at": datetime.now(UTC).isoformat(),
                "recording_fps": self._session_frame_rate,
                "recording_max_width": self._session_max_width,
                "encoder": "Qt Multimedia MPEG-4",
                "files": dict(self._files),
                "error": self.last_error,
            }
            self.last_directory = directory
            self.current_directory = None
            self._active = False
            self._delivery_inflight.clear()
            self._record_sizes.clear()
        try:
            temp_manifest = directory / ".manifest.json.tmp"
            temp_manifest.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temp_manifest.replace(directory / "manifest.json")
        except OSError as exc:
            self._set_error(token, f"Could not save the recording manifest: {exc}")
        finally:
            if self.state_changed_callback is not None:
                self.state_changed_callback()


class _ReplayFrameConverter:
    """Convert only the latest replay frame away from the GUI thread."""

    def __init__(self, broker: LatestFrameBroker, source_name: str) -> None:
        self.broker = broker
        self.source_name = source_name
        self._condition = threading.Condition()
        self._pending: QVideoFrame | None = None
        self._stop = False
        self._drain = False
        self._thread = threading.Thread(
            target=self._run,
            name="production-hub-video-replay-convert",
            daemon=True,
        )
        self._thread.start()

    def submit(self, frame: QVideoFrame) -> None:
        with self._condition:
            if self._stop:
                return
            self._pending = QVideoFrame(frame)
            self._condition.notify()

    def stop(self, *, drain: bool = False) -> None:
        with self._condition:
            self._stop = True
            self._drain = bool(drain)
            if not self._drain:
                self._pending = None
            self._condition.notify()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._stop:
                    self._condition.wait()
                if self._pending is None and self._stop:
                    return
                frame = self._pending
                self._pending = None
            assert frame is not None
            image = frame.toImage()
            if not image.isNull():
                start = frame.startTime()
                end = frame.endTime()
                frame_rate = 1_000_000.0 / (end - start) if start >= 0 and end > start else 0.0
                self.broker.publish(
                    VideoSourceKey.REPLAY,
                    image.copy(),
                    frame_rate=frame_rate,
                    source_timestamp=max(0, start),
                    source_name=self.source_name,
                )
            if self._stop and (not self._drain or self._pending is None):
                return


class _QtReplayBackend(QObject):
    def __init__(self, broker: LatestFrameBroker) -> None:
        super().__init__()
        self.broker = broker
        self.player = QMediaPlayer(self)
        self.sink = QVideoSink(self)
        self.player.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self._on_frame)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.errorOccurred.connect(self._on_error)
        self.converter: _ReplayFrameConverter | None = None
        self.path: Path | None = None
        self.active = False

    def start(self, path: Path) -> None:
        self.stop()
        self.path = path
        self.converter = _ReplayFrameConverter(self.broker, path.name)
        self.active = True
        self.broker.set_status(
            VideoSourceKey.REPLAY,
            VideoSourceState.STARTING,
            f"Opening {path.name}…",
            source_name=path.name,
        )
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()

    def stop(self) -> None:
        self.player.stop()
        converter = self.converter
        self.converter = None
        if converter is not None:
            converter.stop()
        self.active = False
        self.broker.set_status(VideoSourceKey.REPLAY, VideoSourceState.STOPPED, "Stopped")

    @Slot(QVideoFrame)
    def _on_frame(self, frame: QVideoFrame) -> None:
        if self.active and frame.isValid() and self.converter is not None:
            self.converter.submit(frame)

    @Slot(QMediaPlayer.MediaStatus)
    def _on_media_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if status != QMediaPlayer.MediaStatus.EndOfMedia or not self.active:
            return
        converter = self.converter
        self.converter = None
        if converter is not None:
            converter.stop(drain=True)
        self.active = False
        self.broker.set_status(VideoSourceKey.REPLAY, VideoSourceState.STOPPED, "Replay finished")

    @Slot(QMediaPlayer.Error, str)
    def _on_error(self, _error: QMediaPlayer.Error, message: str) -> None:
        if not self.active:
            return
        converter = self.converter
        self.converter = None
        if converter is not None:
            converter.stop()
        self.active = False
        detail = message or "Qt Multimedia could not replay this recording"
        self.broker.set_status(
            VideoSourceKey.REPLAY,
            VideoSourceState.ERROR,
            f"Replay failed: {detail}",
            last_error=detail,
        )


class ReplayVideoSource:
    """Replay one diagnostic file through Qt into a bounded broker slot."""

    def __init__(self, broker: LatestFrameBroker) -> None:
        self.broker = broker
        self._backend: _QtReplayBackend | None = None
        self.path: Path | None = None

    @property
    def running(self) -> bool:
        return bool(self._backend and self._backend.active)

    def initialize_qt(self) -> None:
        if self._backend is not None:
            return
        if not qt_recording_available():
            raise RecordingUnavailableError("Initialize replay after creating QApplication")
        self._backend = _QtReplayBackend(self.broker)

    def start(self, path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError(path)
        if self._backend is None:
            raise RecordingUnavailableError("Qt video replay has not been initialized")
        self.path = path
        self._backend.start(path)

    def stop(self) -> None:
        if self._backend is not None:
            self._backend.stop()
        else:
            self.broker.set_status(VideoSourceKey.REPLAY, VideoSourceState.STOPPED, "Stopped")
