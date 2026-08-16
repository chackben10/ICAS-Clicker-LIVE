from __future__ import annotations

import ctypes
import threading
import time
from dataclasses import dataclass

from PySide6.QtGui import QImage

from production_hub.video.frame_broker import LatestFrameBroker
from production_hub.video.models import VideoSourceKey, VideoSourceState
from production_hub.video.ndi_native import NDIUnavailableError, NativeNDI, NativePerformance, NativeVideoFrame


def fourcc(value: str) -> int:
    raw = value.encode("ascii")
    if len(raw) != 4:
        raise ValueError("FourCC values must contain exactly four ASCII characters")
    return raw[0] | raw[1] << 8 | raw[2] << 16 | raw[3] << 24


NDI_BGRX = fourcc("BGRX")
NDI_BGRA = fourcc("BGRA")


@dataclass(frozen=True, slots=True)
class NDISourceSettings:
    source_name: str
    source: VideoSourceKey = VideoSourceKey.AUDIENCE
    display_name: str = "Audience Cam"
    highest_bandwidth: bool = True
    publish_fps: float = 12.0
    stale_after_seconds: float = 1.5


class NDIVideoSource:
    """Continuously drains NDI on one worker and publishes only fresh snapshots."""

    def __init__(self, broker: LatestFrameBroker, settings: NDISourceSettings) -> None:
        self.broker = broker
        self.settings = settings
        self._stop_event = threading.Event()
        self._output_active = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._discovered_sources: list[str] = []
        self._runtime_version = "not loaded"

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def discovered_sources(self) -> list[str]:
        with self._lock:
            return list(self._discovered_sources)

    @property
    def runtime_version(self) -> str:
        with self._lock:
            return self._runtime_version

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self.broker.set_status(
            self.settings.source,
            VideoSourceState.STARTING,
            "Loading the NDI receiver…",
            source_name=self.settings.source_name,
        )
        self._thread = threading.Thread(
            target=self._run,
            name=f"production-hub-{self.settings.source.value}-ndi",
            daemon=True,
        )
        self._thread.start()

    def set_output_active(self, active: bool) -> None:
        if active:
            self._output_active.set()
        else:
            self._output_active.clear()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3)
        self._thread = None
        self.broker.set_status(
            self.settings.source,
            VideoSourceState.STOPPED,
            "Stopped",
            source_name=self.settings.source_name,
        )

    def discover_now(self, wait_ms: int = 500) -> list[str]:
        runtime = NativeNDI.shared()
        sources = runtime.discover_sources(wait_ms)
        with self._lock:
            self._runtime_version = runtime.version
            self._discovered_sources = sources
        return sources

    def _run(self) -> None:
        receiver = None
        try:
            runtime = NativeNDI.shared()
            with self._lock:
                self._runtime_version = runtime.version
            while not self._stop_event.is_set():
                if receiver is None:
                    receiver = self._discover_and_connect(runtime)
                    if receiver is None:
                        self._stop_event.wait(0.5)
                        continue
                receiver = self._capture_until_reconnect(runtime, receiver)
        except NDIUnavailableError as exc:
            self._set_error(str(exc))
        except Exception as exc:
            self._set_error(f"Unexpected NDI receiver failure: {exc}")
        finally:
            if receiver:
                runtime.library.ph_ndi_receiver_destroy(receiver)

    def _discover_and_connect(self, runtime: NativeNDI):
        self.broker.set_status(
            self.settings.source,
            VideoSourceState.DISCOVERING,
            f"Looking for {self.settings.source_name}…",
            source_name=self.settings.source_name,
        )
        sources = runtime.discover_sources(750)
        with self._lock:
            self._discovered_sources = sources
        match = next((item for item in sources if self._matches_configured_source(item)), None)
        if not match:
            self.broker.set_status(
                self.settings.source,
                VideoSourceState.MISSING,
                f"NDI source not found: {self.settings.source_name}",
                source_name=self.settings.source_name,
            )
            return None
        receiver = runtime.create_receiver(
            match,
            highest_bandwidth=self.settings.highest_bandwidth,
            receiver_name=f"Production Hub - {self.settings.display_name} Receiver",
        )
        self.broker.set_status(
            self.settings.source,
            VideoSourceState.STARTING,
            f"Connecting to {self.settings.display_name}…",
            source_name=match,
        )
        return receiver

    def _capture_until_reconnect(self, runtime: NativeNDI, receiver):
        last_native_frame = time.monotonic()
        last_publish = 0.0
        last_performance_check = 0.0
        last_status_publish = 0.0
        received_frames = 0
        dropped_frames = 0
        minimum_interval = 1.0 / max(1.0, self.settings.publish_fps)

        while not self._stop_event.is_set():
            frame = NativeVideoFrame()
            result = int(runtime.library.ph_ndi_receiver_capture_video(receiver, 250, ctypes.byref(frame)))
            now = time.monotonic()
            if result < 0:
                runtime.library.ph_ndi_receiver_destroy(receiver)
                self.broker.set_status(
                    self.settings.source,
                    VideoSourceState.RECONNECTING,
                    "NDI capture failed; reconnecting…",
                    last_error=runtime.last_error,
                )
                self._stop_event.wait(0.5)
                return None
            if result == 0:
                if now - last_native_frame >= self.settings.stale_after_seconds:
                    self.broker.set_status(
                        self.settings.source,
                        VideoSourceState.RECONNECTING,
                        f"{self.settings.display_name} is connected but video is stale…",
                        received_frames=received_frames,
                        dropped_frames=dropped_frames,
                    )
                if now - last_native_frame >= max(8.0, self.settings.stale_after_seconds * 4):
                    runtime.library.ph_ndi_receiver_destroy(receiver)
                    return None
                continue

            try:
                received_frames += 1
                last_native_frame = now
                if now - last_performance_check >= 1.0:
                    performance = NativePerformance()
                    if runtime.library.ph_ndi_receiver_performance(receiver, ctypes.byref(performance)):
                        received_frames = max(received_frames, int(performance.total_video_frames))
                        dropped_frames = max(0, int(performance.dropped_video_frames))
                    last_performance_check = now
                if now - last_status_publish >= 1.0 and not self._output_active.is_set():
                    self.broker.set_status(
                        self.settings.source,
                        VideoSourceState.RUNNING,
                        "Receiving video; preview is suspended",
                        received_frames=received_frames,
                        dropped_frames=dropped_frames,
                    )
                    last_status_publish = now
                if not self._output_active.is_set():
                    continue
                if now - last_publish < minimum_interval:
                    continue
                image = self._copy_qimage(frame)
                frame_rate = (
                    frame.frame_rate_numerator / frame.frame_rate_denominator
                    if frame.frame_rate_denominator > 0
                    else 0.0
                )
                self.broker.publish(
                    self.settings.source,
                    image,
                    frame_rate=frame_rate,
                    source_timestamp=frame.timestamp,
                    received_frames=received_frames,
                    dropped_frames=dropped_frames,
                    source_name=self.broker.snapshot(self.settings.source).source_name,
                )
                last_publish = now
            finally:
                runtime.library.ph_ndi_receiver_release_video(receiver, ctypes.byref(frame))

        runtime.library.ph_ndi_receiver_destroy(receiver)
        return None

    def _matches_configured_source(self, discovered: str) -> bool:
        wanted = self.settings.source_name.strip().casefold()
        candidate = discovered.strip().casefold()
        return candidate == wanted or candidate.endswith(f"({wanted})")

    @staticmethod
    def _copy_qimage(frame: NativeVideoFrame) -> QImage:
        if frame.width <= 0 or frame.height <= 0 or frame.line_stride_bytes <= 0 or not frame.data:
            raise NDIUnavailableError("NDI returned an invalid video frame")
        if frame.fourcc == NDI_BGRX:
            image_format = QImage.Format.Format_RGB32
        elif frame.fourcc == NDI_BGRA:
            image_format = QImage.Format.Format_ARGB32
        else:
            label = int(frame.fourcc).to_bytes(4, "little", signed=False).decode("ascii", errors="replace")
            raise NDIUnavailableError(f"Unsupported NDI pixel format: {label}")
        byte_count = frame.line_stride_bytes * frame.height
        pixels = ctypes.string_at(frame.data, byte_count)
        image = QImage(pixels, frame.width, frame.height, frame.line_stride_bytes, image_format).copy()
        if image.isNull():
            raise NDIUnavailableError("NDI frame could not be converted for display")
        return image

    def _set_error(self, message: str) -> None:
        self.broker.set_status(
            self.settings.source,
            VideoSourceState.ERROR,
            message,
            source_name=self.settings.source_name,
            last_error=message,
        )


# Backward-compatible name for integrations/tests written against Phase 1.0.
AudienceNDISource = NDIVideoSource
