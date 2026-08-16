from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from time import monotonic

from PySide6.QtGui import QImage


class VideoSourceKey(StrEnum):
    AUDIENCE = "audience"
    PTZ = "ptz"
    REPLAY = "replay"


class VideoSourceState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    DISCOVERING = "discovering"
    RUNNING = "running"
    RECONNECTING = "reconnecting"
    MISSING = "missing"
    BUSY = "busy"
    PERMISSION_DENIED = "permission_denied"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class VideoFramePacket:
    source: VideoSourceKey
    sequence: int
    captured_monotonic: float
    source_timestamp: int
    image: QImage
    width: int
    height: int
    frame_rate: float


@dataclass(slots=True)
class VideoSourceSnapshot:
    source: VideoSourceKey
    state: VideoSourceState = VideoSourceState.STOPPED
    message: str = "Stopped"
    source_name: str = ""
    negotiated_format: str = ""
    received_frames: int = 0
    published_frames: int = 0
    dropped_frames: int = 0
    effective_fps: float = 0.0
    last_frame_monotonic: float = 0.0
    last_error: str = ""

    @property
    def frame_age_seconds(self) -> float | None:
        if self.last_frame_monotonic <= 0:
            return None
        return max(0.0, monotonic() - self.last_frame_monotonic)

    def copy(self) -> VideoSourceSnapshot:
        return replace(self)
