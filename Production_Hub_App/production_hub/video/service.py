from __future__ import annotations

import threading
from pathlib import Path
from collections.abc import Callable
from typing import Any

from production_hub.calibration.relocalization import AudienceRelocalizationService
from production_hub.core.config.models import CameraTrackingConfig, VideoConfig
from production_hub.tracking.service import PersonTrackingService
from production_hub.video.frame_broker import LatestFrameBroker
from production_hub.video.local_camera_source import LocalCameraDevice, LocalCameraVideoSource
from production_hub.video.models import (
    VideoFramePacket,
    VideoSourceKey,
    VideoSourceSnapshot,
    VideoSourceState,
)
from production_hub.video.ndi_native import NativeNDI
from production_hub.video.ndi_source import NDISourceSettings, NDIVideoSource
from production_hub.video.recording import DiagnosticRecorder, ReplayVideoSource


VideoPipeline = NDIVideoSource | LocalCameraVideoSource


class VideoService:
    """Own both configurable video slots and keep every queue bounded."""

    def __init__(
        self,
        config: VideoConfig,
        data_root: Path,
        logger: Any | None = None,
        tracking_config: CameraTrackingConfig | None = None,
    ) -> None:
        self.config = config
        self.data_root = data_root
        self.logger = logger
        self.broker = LatestFrameBroker()
        self.recorder = DiagnosticRecorder(
            self.broker,
            data_root / "recordings",
            config.recording_fps,
            config.recording_max_width,
        )
        self.replay = ReplayVideoSource(self.broker)
        self.tracking = PersonTrackingService(
            self.broker,
            tracking_config or CameraTrackingConfig(),
            logger,
            initially_active=False,
        )
        self.relocalization = AudienceRelocalizationService(
            self.broker,
            data_root,
            tracking_config or CameraTrackingConfig(),
            logger,
            initially_active=False,
        )
        self.tracking.set_region_provider(
            lambda: self.relocalization.stabilized_regions(self.tracking.config.scene_regions)
        )
        self._sources: dict[VideoSourceKey, VideoPipeline] = {}
        self._discovered_ndi_sources: list[str] = []
        self._ndi_runtime_version = "not loaded"
        self._discovery_lock = threading.RLock()
        self._qt_initialized = False
        self._preview_active = False
        self._tracking_activity_owners: set[str] = set()
        self._calibration_activity_owners: set[str] = set()
        self._shutdown_lock = threading.Lock()
        self._shutdown_callbacks: list[Callable[[], None]] = []
        self._stopped = False
        self.recorder.state_changed_callback = self._update_output_activity

    @property
    def recording_available(self) -> bool:
        return self.recorder.available

    @property
    def recordings_root(self) -> Path:
        return self.data_root / "recordings"

    @property
    def audience(self) -> VideoPipeline | None:
        return self._sources.get(VideoSourceKey.AUDIENCE)

    @property
    def local_ptz(self) -> LocalCameraVideoSource | None:
        source = self._sources.get(VideoSourceKey.PTZ)
        return source if isinstance(source, LocalCameraVideoSource) else None

    @property
    def discovered_ndi_sources(self) -> list[str]:
        with self._discovery_lock:
            names = list(self._discovered_ndi_sources)
        for source in self._sources.values():
            if isinstance(source, NDIVideoSource):
                names.extend(source.discovered_sources)
        return sorted(set(names), key=str.casefold)

    @property
    def ndi_runtime_version(self) -> str:
        versions = [
            source.runtime_version
            for source in self._sources.values()
            if isinstance(source, NDIVideoSource) and source.runtime_version != "not loaded"
        ]
        return versions[0] if versions else self._ndi_runtime_version

    def initialize_qt(self) -> None:
        if self._qt_initialized:
            return
        self._qt_initialized = True
        self.recorder.initialize_qt()
        self.replay.initialize_qt()
        for source_key in (VideoSourceKey.AUDIENCE, VideoSourceKey.PTZ):
            self._sources[source_key] = self._make_source(source_key, self.config)
            if self._source_enabled(source_key, self.config) and self._source_auto_connect(
                source_key,
                self.config,
            ):
                self.start_source(source_key)
        self.tracking.start()
        self.relocalization.start()
        self._update_output_activity()

    def set_preview_active(self, active: bool) -> None:
        self._preview_active = bool(active)
        self._update_output_activity()

    def set_tracking_activity(self, active: bool, *, owner: str = "ui") -> None:
        """Grant runtime analysis activity without granting any PTZ motion authority."""

        selected_owner = str(owner or "ui")
        if active:
            self._tracking_activity_owners.add(selected_owner)
        else:
            self._tracking_activity_owners.discard(selected_owner)
        tracking_active = bool(self._tracking_activity_owners)
        self.tracking.set_active(tracking_active)
        # Live tracking needs current scene-plane coordinates when available.
        self.relocalization.set_active(
            bool(self._calibration_activity_owners) or tracking_active
        )
        self._update_output_activity()

    def set_calibration_activity(self, active: bool, *, owner: str = "ui") -> None:
        selected_owner = str(owner or "ui")
        if active:
            self._calibration_activity_owners.add(selected_owner)
        else:
            self._calibration_activity_owners.discard(selected_owner)
        self.relocalization.set_active(
            bool(self._calibration_activity_owners)
            or bool(self._tracking_activity_owners)
        )
        self._update_output_activity()

    def source_type(self, source: VideoSourceKey, config: VideoConfig | None = None) -> str:
        selected = config or self.config
        return (
            selected.audience_source_type
            if source == VideoSourceKey.AUDIENCE
            else selected.ptz_source_type
        )

    def source_identifier(self, source: VideoSourceKey, config: VideoConfig | None = None) -> str:
        selected = config or self.config
        if source == VideoSourceKey.AUDIENCE:
            return (
                selected.audience_ndi_source_name
                if selected.audience_source_type == "ndi"
                else selected.audience_device_id
            )
        return (
            selected.ptz_ndi_source_name
            if selected.ptz_source_type == "ndi"
            else selected.ptz_device_id
        )

    def source_pipeline(self, source: VideoSourceKey) -> VideoPipeline | None:
        return self._sources.get(source)

    def reconfigure(self, config: VideoConfig) -> None:
        old_config = self.config
        self.config = config
        self.recorder.frame_rate = config.recording_fps
        self.recorder.max_width = config.recording_max_width
        if not self._qt_initialized:
            return

        for source_key in (VideoSourceKey.AUDIENCE, VideoSourceKey.PTZ):
            existing = self._sources[source_key]
            was_running = existing.running
            signature_changed = self._source_signature(source_key, old_config) != self._source_signature(
                source_key,
                config,
            )
            was_enabled = self._source_enabled(source_key, old_config)
            is_enabled = self._source_enabled(source_key, config)
            auto_turned_on = (
                not self._source_auto_connect(source_key, old_config)
                and self._source_auto_connect(source_key, config)
            )

            if signature_changed:
                existing.stop()
                self.broker.clear_frame(source_key)
                existing = self._make_source(source_key, config)
                self._sources[source_key] = existing
                if is_enabled and (was_running or self._source_auto_connect(source_key, config)):
                    self.start_source(source_key)
            elif not is_enabled:
                existing.stop()
            elif (
                (not was_enabled or auto_turned_on)
                and self._source_auto_connect(source_key, config)
            ):
                self.start_source(source_key)
        self._update_output_activity()

    def reconfigure_tracking(self, config: CameraTrackingConfig) -> None:
        self.tracking.reconfigure(config)
        self.relocalization.reconfigure(config)
        tracking_active = bool(self._tracking_activity_owners)
        self.tracking.set_active(tracking_active and config.enabled)
        self.relocalization.set_active(
            (bool(self._calibration_activity_owners) or tracking_active)
            and config.relocalization_enabled
        )
        self._update_output_activity()

    def local_devices(self) -> list[LocalCameraDevice]:
        return LocalCameraVideoSource.available_devices() if self._qt_initialized else []

    def discover_ndi_sources(self, wait_ms: int = 800) -> list[str]:
        runtime = NativeNDI.shared()
        sources = runtime.discover_sources(wait_ms)
        with self._discovery_lock:
            self._discovered_ndi_sources = list(sources)
            self._ndi_runtime_version = runtime.version
        return sources

    def start_source(self, source: VideoSourceKey) -> None:
        if not self._qt_initialized:
            raise RuntimeError("Qt video capture has not been initialized")
        pipeline = self._sources[source]
        if isinstance(pipeline, LocalCameraVideoSource):
            device_id = self.source_identifier(source)
            if not device_id:
                self.broker.set_status(
                    source,
                    VideoSourceState.MISSING,
                    "Select a local camera source first.",
                )
                return
            pipeline.start(device_id)
        else:
            pipeline.start()
        self._update_output_activity()

    def stop_source(self, source: VideoSourceKey) -> None:
        pipeline = self._sources.get(source)
        if pipeline is not None:
            pipeline.stop()
        self.broker.clear_frame(source)

    def start_audience(self) -> None:
        self.start_source(VideoSourceKey.AUDIENCE)

    def stop_audience(self) -> None:
        self.stop_source(VideoSourceKey.AUDIENCE)

    def start_ptz(self, device_id: str | None = None) -> None:
        if device_id is not None and self.source_type(VideoSourceKey.PTZ) == "local":
            self.config.ptz_device_id = device_id
        self.start_source(VideoSourceKey.PTZ)

    def stop_ptz(self) -> None:
        self.stop_source(VideoSourceKey.PTZ)

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

    def add_shutdown_callback(self, callback: Callable[[], None]) -> None:
        self._shutdown_callbacks.append(callback)

    def stop_replay(self) -> None:
        self.replay.stop()

    def shutdown(self) -> None:
        with self._shutdown_lock:
            if self._stopped:
                return
            self._stopped = True
        for callback in reversed(self._shutdown_callbacks):
            try:
                callback()
            except Exception as exc:
                if self.logger is not None:
                    self.logger.warning(
                        "video_shutdown_callback_failed",
                        "A video-dependent service did not stop cleanly",
                        error=str(exc),
                    )
        self.recorder.stop(wait=True)
        self.replay.stop()
        self.tracking.stop()
        self.relocalization.stop()
        for source in self._sources.values():
            source.stop()

    def _make_source(self, source: VideoSourceKey, config: VideoConfig) -> VideoPipeline:
        display_name = "Audience Cam" if source == VideoSourceKey.AUDIENCE else "PTZ Cam"
        if self.source_type(source, config) == "ndi":
            highest_bandwidth = (
                config.audience_highest_bandwidth
                if source == VideoSourceKey.AUDIENCE
                else config.ptz_highest_bandwidth
            )
            return NDIVideoSource(
                self.broker,
                NDISourceSettings(
                    source_name=self.source_identifier(source, config),
                    source=source,
                    display_name=display_name,
                    highest_bandwidth=highest_bandwidth,
                    publish_fps=config.preview_fps,
                    stale_after_seconds=config.stale_after_seconds,
                ),
            )
        return LocalCameraVideoSource(
            self.broker,
            source=source,
            display_name=display_name,
            publish_fps=config.preview_fps,
            preferred_width=config.preferred_width,
            preferred_height=config.preferred_height,
            preferred_fps=config.preferred_fps,
            stale_after_seconds=config.stale_after_seconds,
        )

    def _source_enabled(self, source: VideoSourceKey, config: VideoConfig) -> bool:
        enabled = config.audience_enabled if source == VideoSourceKey.AUDIENCE else config.ptz_enabled
        return config.enabled and enabled

    @staticmethod
    def _source_auto_connect(source: VideoSourceKey, config: VideoConfig) -> bool:
        return (
            config.audience_auto_connect
            if source == VideoSourceKey.AUDIENCE
            else config.ptz_auto_connect
        )

    def _source_signature(self, source: VideoSourceKey, config: VideoConfig) -> tuple[object, ...]:
        highest_bandwidth = (
            config.audience_highest_bandwidth
            if source == VideoSourceKey.AUDIENCE
            else config.ptz_highest_bandwidth
        )
        return (
            self.source_type(source, config),
            self.source_identifier(source, config),
            highest_bandwidth,
            config.preview_fps,
            config.preferred_width,
            config.preferred_height,
            config.preferred_fps,
            config.stale_after_seconds,
        )

    def _update_output_activity(self) -> None:
        shared_active = self._preview_active or self.recorder.recording
        for source_key, source in self._sources.items():
            tracking_config = self.tracking.config
            tracking_active = bool(self._tracking_activity_owners) and tracking_config.enabled and (
                tracking_config.analyze_audience
                if source_key == VideoSourceKey.AUDIENCE
                else tracking_config.analyze_ptz
            )
            calibration_active = (
                (bool(self._calibration_activity_owners) or bool(self._tracking_activity_owners))
                and self.relocalization.config.relocalization_enabled
                and source_key == VideoSourceKey.AUDIENCE
            )
            source.set_output_active(shared_active or tracking_active or calibration_active)
