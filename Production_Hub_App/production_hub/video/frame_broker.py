from __future__ import annotations

import threading
import time
from collections import deque

from PySide6.QtGui import QImage

from production_hub.video.models import (
    VideoFramePacket,
    VideoSourceKey,
    VideoSourceSnapshot,
    VideoSourceState,
)


class LatestFrameBroker:
    """Thread-safe one-frame slots with source health and bounded rate history."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._frames: dict[VideoSourceKey, VideoFramePacket] = {}
        self._snapshots = {key: VideoSourceSnapshot(key) for key in VideoSourceKey}
        self._sequences = {key: 0 for key in VideoSourceKey}
        self._publish_times = {key: deque(maxlen=90) for key in VideoSourceKey}

    def publish(
        self,
        source: VideoSourceKey,
        image: QImage,
        *,
        frame_rate: float,
        source_timestamp: int = 0,
        received_frames: int | None = None,
        dropped_frames: int | None = None,
        source_name: str | None = None,
    ) -> VideoFramePacket:
        if image.isNull():
            raise ValueError("Cannot publish a null video image")
        now = time.monotonic()
        with self._lock:
            sequence = self._sequences[source] + 1
            self._sequences[source] = sequence
            packet = VideoFramePacket(
                source=source,
                sequence=sequence,
                captured_monotonic=now,
                source_timestamp=int(source_timestamp),
                image=QImage(image),
                width=image.width(),
                height=image.height(),
                frame_rate=max(0.0, float(frame_rate)),
            )
            self._frames[source] = packet
            times = self._publish_times[source]
            times.append(now)
            snapshot = self._snapshots[source]
            snapshot.state = VideoSourceState.RUNNING
            snapshot.message = "Receiving video"
            snapshot.negotiated_format = (
                f"{image.width()}×{image.height()} @ {frame_rate:.2f} fps" if frame_rate > 0
                else f"{image.width()}×{image.height()}"
            )
            snapshot.last_frame_monotonic = now
            snapshot.published_frames = sequence
            if received_frames is not None:
                snapshot.received_frames = max(snapshot.received_frames, int(received_frames))
            else:
                snapshot.received_frames = max(snapshot.received_frames, sequence)
            if dropped_frames is not None:
                snapshot.dropped_frames = max(0, int(dropped_frames))
            if source_name is not None:
                snapshot.source_name = source_name
            snapshot.effective_fps = self._calculate_rate(times)
            snapshot.last_error = ""
            return packet

    def set_status(
        self,
        source: VideoSourceKey,
        state: VideoSourceState,
        message: str,
        *,
        source_name: str | None = None,
        last_error: str | None = None,
        received_frames: int | None = None,
        dropped_frames: int | None = None,
    ) -> None:
        with self._lock:
            snapshot = self._snapshots[source]
            snapshot.state = state
            snapshot.message = str(message)
            if source_name is not None:
                snapshot.source_name = source_name
            if last_error is not None:
                snapshot.last_error = str(last_error)
            if received_frames is not None:
                snapshot.received_frames = max(0, int(received_frames))
            if dropped_frames is not None:
                snapshot.dropped_frames = max(0, int(dropped_frames))

    def frame(self, source: VideoSourceKey) -> VideoFramePacket | None:
        with self._lock:
            packet = self._frames.get(source)
            if packet is None:
                return None
            return VideoFramePacket(
                source=packet.source,
                sequence=packet.sequence,
                captured_monotonic=packet.captured_monotonic,
                source_timestamp=packet.source_timestamp,
                image=QImage(packet.image),
                width=packet.width,
                height=packet.height,
                frame_rate=packet.frame_rate,
            )

    def snapshot(self, source: VideoSourceKey) -> VideoSourceSnapshot:
        with self._lock:
            return self._snapshots[source].copy()

    def clear_frame(self, source: VideoSourceKey) -> None:
        with self._lock:
            self._frames.pop(source, None)
            self._publish_times[source].clear()

    @staticmethod
    def _calculate_rate(times: deque[float]) -> float:
        if len(times) < 2:
            return 0.0
        elapsed = times[-1] - times[0]
        return (len(times) - 1) / elapsed if elapsed > 0 else 0.0
