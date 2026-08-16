"""Bounded, in-process video ingestion and diagnostic recording services."""

from typing import TYPE_CHECKING

from production_hub.video.models import VideoSourceKey, VideoSourceSnapshot, VideoSourceState

if TYPE_CHECKING:
    from production_hub.video.service import VideoService

__all__ = ["VideoService", "VideoSourceKey", "VideoSourceSnapshot", "VideoSourceState"]


def __getattr__(name: str):
    if name == "VideoService":
        from production_hub.video.service import VideoService

        return VideoService
    raise AttributeError(name)
