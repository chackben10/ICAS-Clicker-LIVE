from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from production_hub.core.config.models import VideoConfig
from production_hub.video.frame_broker import LatestFrameBroker
from production_hub.video.local_camera_source import LocalCameraDevice, LocalPTZCameraSource
from production_hub.video.models import VideoFramePacket, VideoSourceKey, VideoSourceSnapshot
from production_hub.video.ndi_source import AudienceNDISource, NDISourceSettings
from production_hub.video.recording import DiagnosticRecorder, ReplayVideoSource


class VideoService:
    """Owns Phase 1 video resources and keeps every queue bounded to one frame."""

    def __init__(self, config: VideoConfig, data_root: Path, logger: Any | None = None) -> None:
        self.config = config
        self.data_root = data_root
        self.logger = logger
        self.broker = LatestFrameBroker()
        self.audience = self._make_audience_source(config)
        self.local_ptz: LocalPTZCameraSource | None = None
        self.recorder = DiagnosticRecorder(
            self.broker,
            data_root / "recordings",
            config.recording_fps,
            config.recording_max_width,
        )
        self.replay = ReplayVideoSource(self.broker)
        self._qt_initialized = False
        self._preview_active = False
        self._shutdown_lock = threading.Lock()
        self._stopped = False
        self.recorder.state_changed_callback = self._update_output_activity

    @property
    def recording_available(self) -> bool:
        return self.recorder.available

    @property
    def recordings_root(self) -> Path:
        return self.data_root / "recordings"

    def initialize_qt(self) -> None:
        if self._qt_initialized:
            return
        self._qt_initialized = True
        self.recorder.initialize_qt()
        self.replay.initialize_qt()
        self.local_ptz = LocalPTZCameraSource(
            self.broker,
            publish_fps=self.config.preview_fps,
            preferred_width=self.config.preferred_width,
            preferred_height=self.config.preferred_height,
            preferred_fps=self.config.preferred_fps,
            stale_after_seconds=self.config.stale_after_seconds,
        )
        if self.config.enabled and self.config.audience_enabled and self.config.audience_auto_connect:
            self.audience.start()
        if (
            self.config.enabled
            and self.config.ptz_enabled
            and self.config.ptz_auto_connect
            and self.config.ptz_device_id
        ):
            self.local_ptz.start(self.config.ptz_device_id)
        self._update_output_activity()

    def set_preview_active(self, active: bool) -> None:
        self._preview_active = bool(active)
        self._update_output_activity()

    def reconfigure(self, config: VideoConfig) -> None:
        old_config = self.config
        audience_was_running = self.audience.running
        ptz_was_running = bool(self.local_ptz and self.local_ptz.running)
        audience_was_enabled = old_config.enabled and old_config.audience_enabled
        audience_is_enabled = config.enabled and config.audience_enabled
        audience_became_enabled = not audience_was_enabled and audience_is_enabled
        audience_auto_turned_on = (
            not old_config.audience_auto_connect and config.audience_auto_connect
        )
        ptz_was_enabled = old_config.enabled and old_config.ptz_enabled
        ptz_is_enabled = config.enabled and config.ptz_enabled
        ptz_became_enabled = not ptz_was_enabled and ptz_is_enabled
        ptz_auto_turned_on = not old_config.ptz_auto_connect and config.ptz_auto_connect
        audience_changed = (
            old_config.audience_ndi_source_name != config.audience_ndi_source_name
            or old_config.audience_highest_bandwidth != config.audience_highest_bandwidth
            or old_config.preview_fps != config.preview_fps
            or old_config.stale_after_seconds != config.stale_after_seconds
        )
        local_pipeline_changed = (
            old_config.preview_fps != config.preview_fps
            or old_config.preferred_width != config.preferred_width
            or old_config.preferred_height != config.preferred_height
            or old_config.preferred_fps != config.preferred_fps
            or old_config.stale_after_seconds != config.stale_after_seconds
        )
        local_device_changed = old_config.ptz_device_id != config.ptz_device_id
        self.config = config
        self.recorder.frame_rate = config.recording_fps
        self.recorder.max_width = config.recording_max_width

        if audience_changed:
            self.audience.stop()
            self.audience = self._make_audience_source(config)
        if not audience_is_enabled:
            self.audience.stop()
        elif audience_changed and (audience_was_running or config.audience_auto_connect):
            self.audience.start()
        elif (audience_became_enabled or audience_auto_turned_on) and config.audience_auto_connect:
            self.audience.start()

        if self.local_ptz and (local_pipeline_changed or local_device_changed):
            self.local_ptz.stop()
        if self._qt_initialized and (self.local_ptz is None or local_pipeline_changed):
            self.local_ptz = LocalPTZCameraSource(
                self.broker,
                publish_fps=config.preview_fps,
                preferred_width=config.preferred_width,
                preferred_height=config.preferred_height,
                preferred_fps=config.preferred_fps,
                stale_after_seconds=config.stale_after_seconds,
            )
        if self.local_ptz and not ptz_is_enabled:
            self.local_ptz.stop()
        if (
            self.local_ptz
            and ptz_is_enabled
            and config.ptz_device_id
            and (
                (
                    (local_pipeline_changed or local_device_changed)
                    and (ptz_was_running or config.ptz_auto_connect)
                )
                or (
                    (ptz_became_enabled or ptz_auto_turned_on)
                    and config.ptz_auto_connect
                )
            )
        ):
            self.local_ptz.start(config.ptz_device_id)
        self._update_output_activity()

    def local_devices(self) -> list[LocalCameraDevice]:
        return LocalPTZCameraSource.available_devices() if self._qt_initialized else []

    def start_audience(self) -> None:
        self.audience.start()
        self._update_output_activity()

    def stop_audience(self) -> None:
        self.audience.stop()

    def start_ptz(self, device_id: str | None = None) -> None:
        if not self._qt_initialized or self.local_ptz is None:
            raise RuntimeError("Qt video capture has not been initialized")
        self.local_ptz.start(device_id if device_id is not None else self.config.ptz_device_id)
        self._update_output_activity()

    def stop_ptz(self) -> None:
        if self.local_ptz:
            self.local_ptz.stop()

    def frame(self, source: VideoSourceKey) -> VideoFramePacket | None:
        return self.broker.frame(source)

    def snapshot(self, source: VideoSourceKey) -> VideoSourceSnapshot:
        return self.broker.snapshot(source)

    def start_recording(self) -> Path:
        result = self.recorder.start()
        self._update_output_activity()
        return result

    def stop_recording(self) -> Path | None:
        result = self.recorder.stop()
        self._update_output_activity()
        return result

    def start_replay(self, path: Path) -> None:
        self.replay.start(path)

    def stop_replay(self) -> None:
        self.replay.stop()

    def shutdown(self) -> None:
        with self._shutdown_lock:
            if self._stopped:
                return
            self._stopped = True
        self.recorder.stop(wait=True)
        self.replay.stop()
        self.audience.stop()
        if self.local_ptz:
            self.local_ptz.stop()

    def _make_audience_source(self, config: VideoConfig) -> AudienceNDISource:
        return AudienceNDISource(
            self.broker,
            NDISourceSettings(
                source_name=config.audience_ndi_source_name,
                highest_bandwidth=config.audience_highest_bandwidth,
                publish_fps=config.preview_fps,
                stale_after_seconds=config.stale_after_seconds,
            ),
        )

    def _update_output_activity(self) -> None:
        active = self._preview_active or self.recorder.recording
        self.audience.set_output_active(active)
        if self.local_ptz:
            self.local_ptz.set_output_active(active)
