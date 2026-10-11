from __future__ import annotations

import asyncio
import math
import threading
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from production_hub.core.config.models import CameraTrackingConfig
from production_hub.tracking.frame_alignment import PtzFrameAlignment
from production_hub.tracking.framing import (
    FramingDecision,
    FramingEngine,
    FramingState,
)
from production_hub.tracking.models import (
    NormalizedRect,
    SubjectReferenceTarget,
    TrackedSubject,
    TrackingSnapshot,
)
from production_hub.tracking.ptz_geometry import (
    PtzGeometryModel,
    PtzMotorPose,
    transform_live_rect_to_reference,
)
from production_hub.tracking.target_lock import SubjectTargetLock
from production_hub.tracking.velocity_controller import (
    PtzVelocityCommand,
    SmoothPtzVelocityController,
)
from production_hub.video.models import VideoSourceKey


class AutomationState(StrEnum):
    DISARMED = "disarmed"
    SHADOW = "shadow"
    ARMED_WAITING = "armed_waiting"
    ARMED_TRACKING = "armed_tracking"
    BLOCKED = "blocked"
    FAULT = "fault"


@dataclass(slots=True)
class PtzAutomationSnapshot:
    state: AutomationState = AutomationState.DISARMED
    armed: bool = False
    motion_authority: bool = False
    mode: str = "off"
    message: str = "PTZ automation is disarmed"
    decision: FramingDecision | None = None
    actual_pose: PtzMotorPose | None = None
    commanded_pose: PtzMotorPose | None = None
    geometry_path: str = ""
    last_decision_monotonic: float = 0.0
    last_command_monotonic: float = 0.0
    commands_sent: int = 0
    last_error: str = ""
    tracking_source: str = ""
    locked_subject_count: int = 0
    matched_subject_count: int = 0
    # Where the supervisor believes each locked person is, in calibrated
    # reference coordinates. Surfaced so an operator can see who the camera is
    # actually following before it goes to air.
    locked_targets: tuple[SubjectReferenceTarget, ...] = ()
    # Actual matched Audience detections, in live image coordinates. The motor
    # map and its learned parallax offsets are unsuitable for a visual overlay.
    locked_audience_subjects: tuple[TrackedSubject, ...] = ()

    def copy(self) -> PtzAutomationSnapshot:
        return replace(
            self,
            locked_targets=tuple(self.locked_targets),
            locked_audience_subjects=tuple(self.locked_audience_subjects),
        )


class PtzAutomationService:
    """Safety supervisor for absolute framing and smooth subject tracking.

    Arming is intentionally runtime-only and is never restored at startup. The
    worker computes shadow decisions while the Camera page is open, but it only
    sends commands after explicit arming and continuous health validation.
    """

    ACTIVITY_OWNER = "ptz_automation"

    def __init__(
        self,
        video: Any,
        panasonic: Any,
        config: CameraTrackingConfig,
        data_root: Path,
        logger: Any | None = None,
        *,
        geometry_loader: Any = PtzGeometryModel.load_active,
        panorama_geometry_loader: Any = PtzGeometryModel.load_active_panorama,
    ) -> None:
        self.video = video
        self.panasonic = panasonic
        self.config = config
        self.data_root = Path(data_root)
        self.logger = logger
        self.geometry_loader = geometry_loader
        self.panorama_geometry_loader = panorama_geometry_loader
        self.framing = FramingEngine()
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._armed = False
        self._authority_generation = 0
        self._shadow_owners: set[str] = set()
        self._arm_request_owners: set[str] = set()
        self._geometry: PtzGeometryModel | None = None
        self._click_target_reference: tuple[float, float] | None = None
        self._target_key: tuple[str, ...] = ()
        self._last_target_bounds: NormalizedRect | None = None
        self._target_since = 0.0
        self._last_target_seen = 0.0
        self._last_command = 0.0
        self._last_recommended: PtzMotorPose | None = None
        self._group_zoom_in_since: float | None = None
        self._subject_lock = SubjectTargetLock(
            maximum_match_distance=config.automation.subject_lock_maximum_match_distance,
            maximum_locked_subjects=config.automation.maximum_locked_subjects,
            formation_grace_seconds=config.automation.subject_lock_formation_grace_seconds,
            retire_seconds=config.automation.subject_lock_retire_seconds,
            hold_seconds=config.automation.subject_lock_hold_seconds,
        )
        self._velocity = SmoothPtzVelocityController()
        self._frame_alignment = PtzFrameAlignment()
        # Runtime experiment, enabled by the source Dev launcher only. Never
        # persist it into the profile shared with the installed application.
        self.motion_easing_enabled = False
        self.tracking_refinements_enabled = False
        self._last_velocity_command = PtzVelocityCommand()
        self._last_velocity_sent = 0.0
        self._consecutive_cycle_errors = 0
        self._last_tracking_source = ""
        self._handoff_motor_pose: PtzMotorPose | None = None
        self._handoff_mapped_pose: PtzMotorPose | None = None
        self._handoff_origin_pose: PtzMotorPose | None = None
        self._handoff_started_at = 0.0
        self._handoff_pan_direction = 0
        self._handoff_tilt_direction = 0
        self._pending_stop_tasks: set[asyncio.Task[None]] = set()
        self._snapshot = PtzAutomationSnapshot(mode=config.automation.mode)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def armed(self) -> bool:
        with self._lock:
            return self._armed

    @property
    def arm_requested(self) -> bool:
        """Whether a control surface is waiting for prerequisites before arming."""

        with self._lock:
            return self._armed or bool(self._arm_request_owners)

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="production-hub-ptz-automation",
            daemon=True,
        )
        self._thread.start()

    def shutdown(self) -> None:
        self.disarm("Production Hub is shutting down")
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._thread = None

    def snapshot(self) -> PtzAutomationSnapshot:
        with self._lock:
            return self._snapshot.copy()

    def query_current_pose(self) -> PtzMotorPose:
        """Read the camera pose without acquiring or changing motion authority."""

        return self._query_pose()

    def reconfigure(self, config: CameraTrackingConfig) -> None:
        with self._lock:
            previous_mode = self.config.automation.mode
            self.config = config
            self._snapshot.mode = config.automation.mode
            self._subject_lock.maximum_match_distance = (
                config.automation.subject_lock_maximum_match_distance
            )
            self._subject_lock.maximum_locked_subjects = (
                config.automation.maximum_locked_subjects
            )
            self._subject_lock.formation_grace_seconds = (
                config.automation.subject_lock_formation_grace_seconds
            )
            self._subject_lock.retire_seconds = (
                config.automation.subject_lock_retire_seconds
            )
            self._subject_lock.hold_seconds = (
                config.automation.subject_lock_hold_seconds
            )
            if previous_mode != config.automation.mode:
                self._target_key = ()
                self._target_since = 0.0
                self._subject_lock.reset()
                self._group_zoom_in_since = None
                self._velocity.reset()
                self._handoff_motor_pose = None
                self._handoff_mapped_pose = None
                self._reset_handoff_guard()
            if config.automation.mode != "click":
                self._click_target_reference = None
        if self.armed and not config.enabled:
            self.disarm("Person detection was disabled")
        elif self.armed and config.automation.mode == "off":
            self.disarm("Framing mode was turned off")
        self._wake.set()

    def set_shadow_active(self, active: bool, *, owner: str = "ui") -> None:
        with self._lock:
            if active:
                self._shadow_owners.add(str(owner or "ui"))
            else:
                self._shadow_owners.discard(str(owner or "ui"))
            if not self._armed:
                self._snapshot.state = (
                    AutomationState.SHADOW
                    if self._shadow_owners and self.config.automation.mode != "off"
                    else AutomationState.DISARMED
                )
        self._wake.set()

    def set_arm_requested(self, active: bool, *, owner: str) -> None:
        """Coordinate asynchronous arm requests across UI and hardware controls."""

        normalized_owner = str(owner or "external")
        with self._lock:
            if active:
                self._arm_request_owners.add(normalized_owner)
            else:
                self._arm_request_owners.discard(normalized_owner)
        self._wake.set()

    def set_click_target_live(self, x: float, y: float) -> tuple[float, float]:
        lock = self.video.relocalization.snapshot()
        if not lock.motion_safe:
            raise ValueError("Live Audience calibration must be locked before click-to-frame.")
        tiny = NormalizedRect(float(x), float(y), 1e-6, 1e-6).clamped()
        reference = transform_live_rect_to_reference(
            tiny,
            lock.live_to_reference,
            lock.live_size,
            lock.reference_size,
        )
        target = reference.x, reference.y
        with self._lock:
            self._click_target_reference = target
            self._target_key = ()
            self._last_target_bounds = None
            self._target_since = 0.0
        self._wake.set()
        return target

    def clear_click_target(self) -> None:
        with self._lock:
            self._click_target_reference = None
        self._wake.set()

    def frame_live_target(
        self,
        x: float,
        y: float,
        width: float = 0.0,
        height: float = 0.0,
    ) -> PtzMotorPose:
        """Execute one calibrated Audience click/drag framing command.

        A point preserves the current optical zoom. A dragged rectangle uses
        its size to choose zoom. This operation deliberately disarms continuous
        tracking before it acquires motion authority.
        """

        lock = self.video.relocalization.snapshot()
        if not lock.motion_safe:
            raise ValueError("Live Audience calibration must be locked before click-to-frame.")
        audience = self.video.snapshot(VideoSourceKey.AUDIENCE)
        if audience.frame_age_seconds is None or audience.frame_age_seconds > 1.5:
            raise ValueError("The Audience camera does not have a fresh frame.")

        dragged = float(width) >= 0.015 and float(height) >= 0.015
        if dragged:
            live_target = NormalizedRect(x, y, width, height).clamped()
        else:
            target_width = max(0.01, self.config.automation.click_target_width)
            target_height = max(0.01, self.config.automation.click_target_height)
            live_target = NormalizedRect(
                float(x) - target_width / 2.0,
                float(y) - target_height / 2.0,
                target_width,
                target_height,
            ).clamped()
        reference_target = transform_live_rect_to_reference(
            live_target,
            lock.live_to_reference,
            lock.live_size,
            lock.reference_size,
        )
        return self._frame_reference_bounds(reference_target, dragged=dragged)

    def frame_reference_target(
        self,
        x: float,
        y: float,
        width: float = 0.0,
        height: float = 0.0,
    ) -> PtzMotorPose:
        """Frame a point or box on the extended calibration-reference canvas."""

        dragged = float(width) >= 0.015 and float(height) >= 0.015
        if dragged:
            reference_target = NormalizedRect(
                float(x),
                float(y),
                float(width),
                float(height),
            )
        else:
            target_width = max(0.01, self.config.automation.click_target_width)
            target_height = max(0.01, self.config.automation.click_target_height)
            reference_target = NormalizedRect(
                float(x) - target_width / 2.0,
                float(y) - target_height / 2.0,
                target_width,
                target_height,
            )
        return self._frame_reference_bounds(reference_target, dragged=dragged)

    def frame_panorama_target(
        self,
        x: float,
        y: float,
        width: float = 0.0,
        height: float = 0.0,
    ) -> PtzMotorPose:
        """Frame a point or box in the canonical PTZ-panorama coordinates."""

        dragged = float(width) >= 0.015 and float(height) >= 0.015
        if dragged:
            target = NormalizedRect(float(x), float(y), float(width), float(height))
        else:
            target_width = max(0.01, self.config.automation.click_target_width)
            target_height = max(0.01, self.config.automation.click_target_height)
            target = NormalizedRect(
                float(x) - target_width / 2.0,
                float(y) - target_height / 2.0,
                target_width,
                target_height,
            )
        return self._frame_reference_bounds(
            target,
            dragged=dragged,
            geometry_loader=self.panorama_geometry_loader,
        )

    def _frame_reference_bounds(
        self,
        reference_target: NormalizedRect,
        *,
        dragged: bool,
        geometry_loader: Any | None = None,
    ) -> PtzMotorPose:
        self.disarm("Click-to-frame command")
        if not getattr(self.panasonic.config, "enabled", False):
            raise ValueError("The Panasonic AWP integration is disabled.")
        geometry = (geometry_loader or self.geometry_loader)(self.data_root)
        actual = self._query_pose()
        desired = geometry.pose_for_target(
            reference_target,
            desired_frame_height=0.88 if dragged else 0.70,
        )
        if not dragged:
            desired = PtzMotorPose(desired.pan, desired.tilt, actual.zoom)

        async def send() -> PtzMotorPose:
            if not await self.panasonic.absolute_pan_tilt(desired.pan, desired.tilt):
                raise RuntimeError("Panasonic rejected the click-to-frame pan/tilt command.")
            if dragged and not await self.panasonic.absolute_zoom(desired.zoom):
                raise RuntimeError("Panasonic rejected the click-to-frame zoom command.")
            movement = max(
                abs(desired.pan - actual.pan) / 420.0,
                abs(desired.tilt - actual.tilt) / 280.0,
                abs(desired.zoom - actual.zoom) / 180.0 if dragged else 0.0,
            )
            deadline = time.monotonic() + max(8.0, min(20.0, 5.0 + movement))
            reached = actual
            while time.monotonic() < deadline:
                pan, tilt = await self.panasonic.query_pan_tilt_position()
                zoom = await self.panasonic.query_zoom_position()
                reached = PtzMotorPose(pan, tilt, zoom)
                if (
                    abs(pan - desired.pan) <= self.config.automation.pan_deadband_units
                    and abs(tilt - desired.tilt) <= self.config.automation.tilt_deadband_units
                    and (
                        not dragged
                        or abs(zoom - desired.zoom)
                        <= self.config.automation.zoom_deadband_units
                    )
                ):
                    return reached
                await asyncio.sleep(0.2)
            raise RuntimeError(
                "The PTZ accepted the framing command but stopped at "
                f"{reached.pan:04X}/{reached.tilt:04X}/{reached.zoom:03X}, "
                f"short of {desired.pan:04X}/{desired.tilt:04X}/{desired.zoom:03X}."
            )

        reached = asyncio.run(send())
        with self._lock:
            self._snapshot.actual_pose = reached
            self._snapshot.commanded_pose = desired
            self._snapshot.last_command_monotonic = time.monotonic()
            self._snapshot.commands_sent += 1
            self._snapshot.message = "Click-to-frame command sent; Subject Tracking remains off"
        self._log(
            "info",
            "ptz_click_to_frame_command",
            "Calibrated click-to-frame command sent",
            pan=desired.pan,
            tilt=desired.tilt,
            zoom=desired.zoom,
            dragged=dragged,
        )
        return reached

    def arm(self) -> tuple[bool, str]:
        reason = self._arm_blocker()
        if reason:
            with self._lock:
                self._armed = False
                self._snapshot.state = AutomationState.BLOCKED
                self._snapshot.armed = False
                self._snapshot.motion_authority = False
                self._snapshot.message = reason
            return False, reason
        try:
            geometry = self.geometry_loader(self.data_root)
        except Exception as exc:
            reason = f"Approved PTZ geometry could not be loaded: {exc}"
            with self._lock:
                self._snapshot.state = AutomationState.BLOCKED
                self._snapshot.message = reason
                self._snapshot.last_error = str(exc)
            return False, reason
        with self._lock:
            self._geometry = geometry
            self._armed = True
            self._authority_generation += 1
            now = time.monotonic()
            self._last_target_seen = now
            self._last_command = 0.0
            self._last_recommended = None
            self._subject_lock.reset()
            self._group_zoom_in_since = None
            self._velocity.reset()
            self._frame_alignment.reset()
            self._last_velocity_command = PtzVelocityCommand()
            self._last_velocity_sent = 0.0
            self._last_tracking_source = ""
            self._handoff_motor_pose = None
            self._handoff_mapped_pose = None
            self._reset_handoff_guard()
            self._target_key = ()
            self._last_target_bounds = None
            self._target_since = 0.0
            self._snapshot = PtzAutomationSnapshot(
                state=AutomationState.ARMED_WAITING,
                armed=True,
                motion_authority=True,
                mode=self.config.automation.mode,
                message="Armed; waiting for a stable framing target",
                geometry_path=str(geometry.map_path),
            )
        self.video.set_tracking_activity(True, owner=self.ACTIVITY_OWNER)
        self.video.set_calibration_activity(True, owner=self.ACTIVITY_OWNER)
        self._log("warning", "ptz_automation_armed", "PTZ automation was explicitly armed")
        self._wake.set()
        return True, "PTZ automation armed"

    def disarm(self, reason: str = "Operator disarmed PTZ automation") -> None:
        with self._lock:
            was_armed = self._armed
            self._armed = False
            self._authority_generation += 1
            self._target_key = ()
            self._last_target_bounds = None
            self._target_since = 0.0
            self._last_recommended = None
            self._subject_lock.reset()
            self._group_zoom_in_since = None
            self._handoff_motor_pose = None
            self._handoff_mapped_pose = None
            self._reset_handoff_guard()
            self._snapshot.armed = False
            self._snapshot.motion_authority = False
            self._snapshot.state = (
                AutomationState.SHADOW
                if self._shadow_owners and self.config.automation.mode != "off"
                else AutomationState.DISARMED
            )
            self._snapshot.message = str(reason)
        if was_armed:
            self._stop_continuous_motion()
        self.video.set_tracking_activity(False, owner=self.ACTIVITY_OWNER)
        self.video.set_calibration_activity(False, owner=self.ACTIVITY_OWNER)
        if was_armed:
            self._log("warning", "ptz_automation_disarmed", str(reason))
        self._wake.set()

    def manual_override(self, reason: str = "Manual camera control") -> None:
        if self.armed:
            self.disarm(f"Disarmed by manual override: {reason}")

    async def disarm_async(
        self,
        reason: str = "Operator disarmed PTZ automation",
    ) -> None:
        """Disarm and await any neutral command on the caller's event loop."""

        self.disarm(reason)
        await self._wait_for_pending_stops()

    async def manual_override_async(
        self,
        reason: str = "Manual camera control",
    ) -> None:
        """Revoke automation and confirm neutral speed before manual motion."""

        if self.armed:
            self.disarm(f"Disarmed by manual override: {reason}")
        await self._wait_for_pending_stops()

    async def _wait_for_pending_stops(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            with self._lock:
                pending = tuple(
                    task
                    for task in self._pending_stop_tasks
                    if task.get_loop() is loop
                )
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    def _arm_blocker(self) -> str:
        if not self.config.enabled:
            return "Enable person detection before arming PTZ automation."
        if not getattr(self.panasonic.config, "enabled", False):
            return "The Panasonic AWP integration is disabled."
        if self.config.automation.mode == "off":
            return "Choose a framing mode before arming."
        lock = self.video.relocalization.snapshot()
        if not lock.motion_safe:
            return "Live Audience calibration is not locked to an approved map."
        audience_video = self.video.snapshot(VideoSourceKey.AUDIENCE)
        if audience_video.frame_age_seconds is None or audience_video.frame_age_seconds > 1.5:
            return "The Audience camera does not have a fresh frame."
        audience_tracking = self.video.tracking.snapshot(VideoSourceKey.AUDIENCE)
        ptz_tracking = self.video.tracking.snapshot(VideoSourceKey.PTZ)
        audience_fresh = (
            audience_tracking.analysis_age_seconds is not None
            and audience_tracking.analysis_age_seconds <= 2.0
        )
        ptz_fresh = (
            ptz_tracking.analysis_age_seconds is not None
            and ptz_tracking.analysis_age_seconds <= 2.0
        )
        if self.config.automation.mode == "subject":
            if not ptz_fresh and not audience_fresh:
                return "PTZ and Audience person tracking do not have a fresh analysis."
        elif not audience_fresh:
            return "Audience person tracking does not have a fresh analysis."
        region_kinds = {
            region.kind
            for region in self.config.scene_regions
            if region.enabled and region.source == "audience"
        }
        if (
            self.config.automation.mode != "subject"
            and not region_kinds.intersection({"stage", "front_stage", "altar", "podium"})
        ):
            return "Create and enable at least one Stage, Altar, or Podium region."
        if self.config.automation.mode == "click" and self._click_target_reference is None:
            return "Click a calibrated Audience location before arming click-to-frame."
        return ""

    def _runtime_blocker(self) -> str:
        """Return only hard blockers once motion authority was granted.

        Frame, detector, and NDI freshness are transient health signals. The
        subject loop stops and waits when an observation is missing; it must
        not silently turn the operator's toggle off. Initial arming still uses
        the stricter `_arm_blocker` validation.
        """

        if not self.config.enabled:
            return "Person detection was disabled"
        if not getattr(self.panasonic.config, "enabled", False):
            return "The Panasonic AWP integration was disabled"
        if self.config.automation.mode == "off":
            return "Framing mode was turned off"
        if self.config.automation.mode != "subject":
            return self._arm_blocker()
        return ""

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                active = self._armed or bool(self._shadow_owners)
                rate = self.config.automation.decision_fps
            if not active:
                self._wake.wait(timeout=1.0)
                self._wake.clear()
                continue
            started = time.monotonic()
            try:
                self._cycle(started)
                self._consecutive_cycle_errors = 0
            except Exception as exc:
                self._consecutive_cycle_errors += 1
                if self._consecutive_cycle_errors >= 3:
                    self._fault(exc)
                else:
                    self._recoverable_cycle_error(exc)
            elapsed = time.monotonic() - started
            self._wake.wait(timeout=max(0.02, 1.0 / rate - elapsed))
            self._wake.clear()

    def _cycle(self, now: float) -> None:
        blocker = self._runtime_blocker() if self.armed else ""
        if blocker:
            self.disarm(blocker)
            with self._lock:
                self._snapshot.state = AutomationState.BLOCKED
            return
        with self._lock:
            geometry = self._geometry
            armed = self._armed
            authority_generation = self._authority_generation
            config = self.config
            click_target = self._click_target_reference
        if geometry is None:
            try:
                geometry = self.geometry_loader(self.data_root)
            except Exception as exc:
                with self._lock:
                    self._snapshot.state = AutomationState.BLOCKED
                    self._snapshot.message = f"Approved PTZ geometry is unavailable: {exc}"
                    self._snapshot.last_error = str(exc)
                return
            with self._lock:
                self._geometry = geometry

        actual = self._query_pose() if armed else None
        if armed and actual is not None and self._manual_motion_detected(actual, now):
            self.disarm("External or manual PTZ movement was detected")
            return
        audience_snapshot = self.video.tracking.snapshot(VideoSourceKey.AUDIENCE)
        ptz_snapshot = self.video.tracking.snapshot(VideoSourceKey.PTZ)
        if armed and actual is not None and self.tracking_refinements_enabled:
            ptz_snapshot = self._frame_alignment.align(ptz_snapshot, geometry, actual, now)
        relocalization = self.video.relocalization.snapshot()
        subject_targets_locked = False
        all_locked_targets_in_ptz = False
        locked_reference_targets = None
        locked_subject_count = 0
        locked_ptz_matches = 0
        locked_audience_matches = 0
        locked_audience_coasts = 0
        locked_held = 0
        if armed and config.automation.mode == "subject" and actual is not None:
            with self._lock:
                if (
                    not self._armed
                    or authority_generation != self._authority_generation
                ):
                    return
                locked = self._subject_lock.update(
                    ptz_snapshot,
                    audience_snapshot,
                    relocalization,
                    geometry,
                    actual,
                    minimum_age_frames=config.automation.minimum_subject_age_frames,
                    now=now,
                )
            audience_snapshot = locked.audience
            ptz_snapshot = locked.ptz
            subject_targets_locked = locked.initialized
            locked_reference_targets = locked.reference_targets
            locked_subject_count = locked.lock_count
            locked_ptz_matches = locked.ptz_matches
            locked_audience_matches = locked.audience_matches
            locked_audience_coasts = locked.audience_coasts
            locked_held = locked.held_locks
            all_locked_targets_in_ptz = bool(
                locked.initialized and locked.ptz_matches == locked.lock_count
            )
        decision = self.framing.decide(
            config.automation,
            audience_snapshot,
            ptz_snapshot,
            relocalization,
            config.scene_regions,
            geometry,
            current_pose=actual,
            click_target=click_target,
            subject_targets_locked=subject_targets_locked,
            locked_reference_targets=locked_reference_targets,
            two_person_portrait_enabled=self.tracking_refinements_enabled,
        )
        handoff_guard_reason = ""
        handoff_bounds = _reference_target_union(locked_reference_targets)
        mapped_handoff: PtzMotorPose | None = None
        if (
            armed
            and config.automation.mode == "subject"
            and actual is not None
            and handoff_bounds is not None
        ):
            try:
                mapped_handoff = geometry.pose_for_target(
                    handoff_bounds,
                    desired_frame_height=config.automation.recovery_frame_height,
                    desired_frame_width=config.automation.group_maximum_width_occupancy,
                )
            except ValueError:
                mapped_handoff = None
            if locked_ptz_matches > 0:
                # Any locked person in the PTZ image still gives the guard a
                # verified direction to remember. For a single subject this is
                # the same condition as before.
                self._prepare_handoff_guard(actual)
            if all_locked_targets_in_ptz and mapped_handoff is not None:
                # Refresh the local wide-camera origin whenever the PTZ has a
                # trusted live observation. Relative motor deltas cancel the
                # fixed inverse-map bias that otherwise reverses direction at
                # the exact moment a subject crosses the PTZ image edge.
                self._handoff_motor_pose = (
                    _centred_motor_pose(actual, ptz_snapshot, geometry)
                    if self.tracking_refinements_enabled
                    else actual
                )
                self._handoff_mapped_pose = mapped_handoff
            elif (
                # Audience handoff is recovery for a PTZ image with nobody
                # locked in it. While part of the group is still in shot the
                # camera keeps image-space authority, and routing through the
                # guard here would replace the composed pose -- zoom included
                # -- with a motor-space estimate, which is exactly what stopped
                # the shot widening to hold the people at the edge.
                locked_ptz_matches == 0
                and (locked_audience_matches > 0 or locked_audience_coasts > 0)
                and mapped_handoff is not None
                and self._handoff_motor_pose is not None
                and self._handoff_mapped_pose is not None
                and decision.desired_pose is not None
            ):
                relative_pose = _relative_handoff_pose(
                    motor_anchor=self._handoff_motor_pose,
                    mapped_anchor=self._handoff_mapped_pose,
                    mapped_target=mapped_handoff,
                    current_zoom=actual.zoom,
                )
                guarded_pose, handoff_guard_reason = self._guard_audience_handoff(
                    relative_pose,
                    actual=actual,
                    now=now,
                    audience_fresh=bool(
                        self.tracking_refinements_enabled
                        and locked_audience_matches > 0
                    ),
                )
                decision = replace(
                    decision,
                    state=(
                        FramingState.WAITING
                        if guarded_pose is None
                        else decision.state
                    ),
                    reason=handoff_guard_reason or decision.reason,
                    desired_pose=guarded_pose,
                )
        partial_group_visible = bool(
            locked_ptz_matches > 0
            and locked_subject_count > locked_ptz_matches
            and decision.subjects_outside_frame > 0
        )
        if (
            armed
            and config.automation.mode == "subject"
            and actual is not None
            and decision.desired_pose is not None
            and not all_locked_targets_in_ptz
        ):
            if partial_group_visible:
                # Part of the group is still in shot and the rest has a known
                # position, so the composition measured how far outside they
                # are. Widening is the one move that recovers them; tightening
                # on an estimate is still forbidden, so this is bounded to a
                # single direction. Panasonic zoom counts up toward telephoto.
                decision = replace(
                    decision,
                    desired_pose=replace(
                        decision.desired_pose,
                        zoom=min(decision.desired_pose.zoom, actual.zoom),
                    ),
                )
            elif mapped_handoff is not None:
                # Nobody locked is in the PTZ image, so there is no optical
                # scale to measure -- but the wide camera still knows roughly
                # where these people are, and `mapped_handoff` is the
                # calibrated zoom that frames those positions. Widening toward
                # it opens the shot by exactly enough to find them again and
                # not one step further: the bound comes from the Audience
                # observation, not from a guess, and it does not move as the
                # camera widens, so the widening stops on its own. Tightening
                # on an estimate stays forbidden.
                recovery_zoom = mapped_handoff.zoom
                if self.tracking_refinements_enabled and self._handoff_motor_pose is not None:
                    # The person left a tight shot faster than it could follow.
                    # Searching at that same tight zoom is what kept the live
                    # camera blind for 10 s at a time; open to ~1.6x the field
                    # of view it last saw them in, which also shows the feet
                    # of someone who stepped down off the platform.
                    try:
                        recovery_zoom = min(
                            recovery_zoom,
                            geometry.zoom_for_relative_footprint(
                                self._handoff_motor_pose.zoom, _HANDOFF_WIDENING,
                            ),
                        )
                    except (AttributeError, ValueError):
                        pass
                decision = replace(
                    decision,
                    desired_pose=replace(
                        decision.desired_pose,
                        zoom=min(recovery_zoom, actual.zoom),
                    ),
                )
            else:
                # No wide-camera position for anybody locked. With nothing to
                # widen toward, hold the operator's zoom.
                decision = replace(
                    decision,
                    desired_pose=replace(decision.desired_pose, zoom=actual.zoom),
                )
        if armed and config.automation.mode == "subject":
            handoff_searching = bool(
                handoff_guard_reason and decision.desired_pose is not None
            )
            tracking_source = (
                "holding_handoff_guard"
                if handoff_guard_reason and not handoff_searching
                else "audience_search"
                if handoff_searching
                else "ptz"
                if all_locked_targets_in_ptz
                else "ptz_group_partial"
                if partial_group_visible
                else "audience_handoff"
                if locked_audience_matches > 0
                else "ptz_group_hold"
                if locked_held > 0
                else "audience_coast"
                if locked_audience_coasts > 0
                else "holding"
                if subject_targets_locked
                else "waiting_for_initial_subject"
            )
            self._record_tracking_source(
                tracking_source,
                actual=actual,
                desired=decision.desired_pose,
                lock_count=locked_subject_count,
                ptz_matches=locked_ptz_matches,
                audience_matches=locked_audience_matches,
                audience_coasts=locked_audience_coasts,
            )
        with self._lock:
            # A UI, VISCA, preset, or API action may revoke motion authority
            # while detector inference is in flight. Never let that stale
            # decision rewrite the Off state or send a post-disarm command.
            if armed and (
                not self._armed
                or authority_generation != self._authority_generation
            ):
                return
            if armed and locked_subject_count > 1 and actual is not None:
                if locked_subject_count < self._snapshot.locked_subject_count:
                    # Retirement already required a sustained absence. Do not
                    # add another size-stability wait before framing survivors.
                    self._group_zoom_in_since = now - 2.0
                if decision.desired_pose is not None:
                    decision = replace(decision, desired_pose=self._guard_group_zoom_in(
                        actual, decision.desired_pose, now,
                    ))
            else:
                self._group_zoom_in_since = None
            self._snapshot.decision = decision
            self._snapshot.last_decision_monotonic = now
            self._snapshot.actual_pose = actual
            self._snapshot.mode = config.automation.mode
            self._snapshot.last_error = ""
            if armed and config.automation.mode == "subject":
                self._snapshot.tracking_source = tracking_source
                self._snapshot.locked_subject_count = locked_subject_count
                self._snapshot.matched_subject_count = locked.matched_lock_count
                self._snapshot.locked_targets = tuple(locked_reference_targets or ())
                self._snapshot.locked_audience_subjects = tuple(audience_snapshot.subjects)
            if not armed:
                self._snapshot.state = (
                    AutomationState.SHADOW
                    if config.automation.mode != "off"
                    else AutomationState.DISARMED
                )
                self._snapshot.message = f"Shadow: {decision.reason}"
                return

        if decision.state != FramingState.READY or decision.desired_pose is None:
            self._handle_target_loss(now, decision.reason)
            return
        self._last_target_seen = now
        same_target = _target_is_continuous(
            self._last_target_bounds,
            decision.target_bounds,
            len(self._target_key),
            len(decision.target_ids),
        )
        if decision.target_ids != self._target_key and not same_target:
            self._target_key = decision.target_ids
            self._target_since = now
        elif decision.target_ids != self._target_key:
            self._target_key = decision.target_ids
        self._last_target_bounds = decision.target_bounds
        if now - self._target_since < config.automation.target_dwell_seconds:
            self._stop_continuous_motion()
            with self._lock:
                self._snapshot.state = AutomationState.ARMED_WAITING
                self._snapshot.message = "Waiting for the target selection to stabilize"
            return
        with self._lock:
            if not self._armed or authority_generation != self._authority_generation:
                return
            self._snapshot.state = AutomationState.ARMED_TRACKING
            self._snapshot.message = (
                f"Tracking {len(decision.target_ids)} target(s)"
                + (" with podium framing" if decision.podium_framing else "")
            )
        if actual is not None:
            if config.automation.mode == "subject":
                self._send_continuous_command(
                    actual,
                    decision.desired_pose,
                    now,
                    live_bounds=(
                        decision.live_subject_bounds
                        if all_locked_targets_in_ptz and decision.two_person_portrait
                        else
                        _subject_union(ptz_snapshot)
                        if all_locked_targets_in_ptz
                        # Losing one of three is a framing problem, not a lost
                        # subject: steer on the union that includes the people
                        # who are off the edge rather than dropping to
                        # motor-space recovery. While the frame is still too
                        # tight to hold them all, steer on the people actually
                        # in shot so widening -- not panning -- does the work.
                        else _subject_union(ptz_snapshot)
                        if partial_group_visible and decision.widen_before_recentering
                        else decision.live_subject_bounds
                        if partial_group_visible
                        else None
                    ),
                    authority_generation=authority_generation,
                    group_tracking=locked_subject_count > 1,
                )
            else:
                self._send_bounded_command(
                    actual,
                    self._smoothed_pose(decision.desired_pose, actual),
                    now,
                    authority_generation=authority_generation,
                )

    def _guard_group_zoom_in(
        self, actual: PtzMotorPose, desired: PtzMotorPose, now: float,
    ) -> PtzMotorPose:
        """Require a consistently smaller group before narrowing the view.

        Full-body detections briefly becoming torso-only, or two walkers
        crossing, can collapse the measured envelope for several frames.
        Wait for two seconds of consistent tightening requests, then follow
        the live target without repeatedly delaying each motor increment.
        Widening requests always pass through immediately.
        """

        if desired.zoom <= actual.zoom + self.config.automation.zoom_deadband_units:
            self._group_zoom_in_since = None
            return desired
        if self._group_zoom_in_since is None:
            self._group_zoom_in_since = now
        if now - self._group_zoom_in_since < 2.0:
            return replace(desired, zoom=actual.zoom)
        return desired

    def _prepare_handoff_guard(self, actual: PtzMotorPose) -> None:
        """Remember the last PTZ-authoritative motion before an edge loss."""

        self._handoff_origin_pose = actual
        self._handoff_started_at = 0.0
        # Panasonic continuous pan speed and the absolute-position coordinate
        # use opposite signs. Convert the last image-space/AW command into
        # absolute motor-coordinate direction before guarding the handoff.
        self._handoff_pan_direction = -_direction(
            self._last_velocity_command.pan - 50,
        )
        # Panasonic tilt speed has the inverse sign of motor-position error.
        self._handoff_tilt_direction = -_direction(
            self._last_velocity_command.tilt - 50,
        )

    def _reset_handoff_guard(self) -> None:
        self._handoff_origin_pose = None
        self._handoff_started_at = 0.0
        self._handoff_pan_direction = 0
        self._handoff_tilt_direction = 0

    def _guard_audience_handoff(
        self,
        desired: PtzMotorPose,
        *,
        actual: PtzMotorPose,
        now: float,
        audience_fresh: bool = False,
    ) -> tuple[PtzMotorPose | None, str]:
        """Bound wide-camera recovery and reject unverified reversals.

        The Audience image remains the active recovery sensor, but it is less
        precise than the PTZ image and can briefly return a shifted rectangle
        at a crop boundary. A handoff may continue the last PTZ-verified
        direction; it may not reverse the camera or traverse the sanctuary
        without seeing the locked person in the PTZ again.
        """

        limits = self.config.automation
        origin = self._handoff_origin_pose
        if origin is None:
            origin = actual
            self._handoff_origin_pose = origin
        if self._handoff_started_at <= 0.0:
            self._handoff_started_at = now
        if audience_fresh:
            # The wide camera is matching the locked person right now, not
            # coasting on an old estimate. Follow them -- including when they
            # turn back or walk further than the first edge search allowed --
            # and let the safety limits count from the last fresh match. A
            # stale or coasting estimate still meets every limit below.
            self._handoff_started_at = now
            self._handoff_origin_pose = actual
            pan_direction = _direction(desired.pan - actual.pan, limits.audience_handoff_reversal_deadband_units)
            tilt_direction = _direction(desired.tilt - actual.tilt, limits.audience_handoff_reversal_deadband_units)
            if pan_direction:
                self._handoff_pan_direction = pan_direction
            if tilt_direction:
                self._handoff_tilt_direction = tilt_direction
            return PtzMotorPose(desired.pan, desired.tilt, actual.zoom), ""
        if now - self._handoff_started_at > limits.audience_handoff_max_seconds:
            return None, "Holding camera; Audience handoff reached its safe time limit"

        reversal_deadband = limits.audience_handoff_reversal_deadband_units
        pan_error = desired.pan - actual.pan
        tilt_error = desired.tilt - actual.tilt
        requested_pan_direction = _direction(pan_error, reversal_deadband)
        requested_tilt_direction = _direction(tilt_error, reversal_deadband)
        if self._handoff_pan_direction == 0 and requested_pan_direction != 0:
            self._handoff_pan_direction = requested_pan_direction
        if self._handoff_tilt_direction == 0 and requested_tilt_direction != 0:
            self._handoff_tilt_direction = requested_tilt_direction
        if (
            requested_pan_direction != 0
            and self._handoff_pan_direction != 0
            and requested_pan_direction != self._handoff_pan_direction
        ):
            search = self._verified_edge_search_pose(actual, origin)
            if search is None:
                return None, "Holding camera; Audience recovery reached its safe pan limit"
            return (
                search,
                "Audience estimate reversed; continuing the last PTZ-verified edge direction",
            )
        tilt_contradicted = (
            requested_tilt_direction != 0
            and self._handoff_tilt_direction != 0
            and requested_tilt_direction != self._handoff_tilt_direction
        )

        maximum_pan = limits.audience_handoff_max_pan_units
        maximum_tilt = limits.audience_handoff_max_tilt_units
        if (
            abs(actual.pan - origin.pan) > maximum_pan + reversal_deadband
            or abs(actual.tilt - origin.tilt) > maximum_tilt + reversal_deadband
        ):
            return None, "Holding camera; Audience handoff reached its safe travel limit"

        pan_low, pan_high = origin.pan - maximum_pan, origin.pan + maximum_pan
        if self._handoff_pan_direction > 0:
            pan_low = origin.pan
        elif self._handoff_pan_direction < 0:
            pan_high = origin.pan
        tilt_low, tilt_high = origin.tilt - maximum_tilt, origin.tilt + maximum_tilt
        if self._handoff_tilt_direction > 0:
            tilt_low = origin.tilt
        elif self._handoff_tilt_direction < 0:
            tilt_high = origin.tilt
        selected = PtzMotorPose(
            max(pan_low, min(pan_high, desired.pan)),
            (
                actual.tilt
                if tilt_contradicted
                else max(tilt_low, min(tilt_high, desired.tilt))
            ),
            actual.zoom,
        )
        return (
            selected,
            (
                "Audience tilt estimate reversed; continuing pan recovery with tilt held"
                if tilt_contradicted
                else ""
            ),
        )

    def _verified_edge_search_pose(
        self,
        actual: PtzMotorPose,
        origin: PtzMotorPose,
    ) -> PtzMotorPose | None:
        direction = self._handoff_pan_direction
        if direction == 0:
            return None
        maximum = self.config.automation.audience_handoff_max_pan_units
        boundary = origin.pan + direction * maximum
        remaining = (boundary - actual.pan) * direction
        if remaining <= self.config.automation.pan_deadband_units:
            return None
        # Aim at the bounded recovery edge rather than advancing a fixed tiny
        # step. The continuous controller turns the larger error into a fast
        # initial pursuit and automatically tapers speed near the boundary.
        # No absolute-position jump is sent; this remains a closed-loop speed
        # command with the same acceleration and hard travel limits.
        return PtzMotorPose(
            boundary,
            actual.tilt,
            actual.zoom,
        )

    def _handle_target_loss(self, now: float, reason: str) -> None:
        elapsed = now - self._last_target_seen
        limits = self.config.automation
        self._stop_continuous_motion()
        # A locked subject remains the operator's target through a detector
        # dropout. Never disarm and never adopt a different person simply
        # because the subject crossed the PTZ edge or left the stage polygon.
        if self.config.automation.mode == "subject":
            with self._lock:
                self._snapshot.state = AutomationState.ARMED_WAITING
                self._snapshot.message = (
                    f"Holding camera; reacquiring locked subject ({reason})"
                    if self._subject_lock.initialized
                    else f"Tracking remains on; waiting to lock the initial PTZ subject ({reason})"
                )
            return
        if elapsed >= limits.target_loss_disarm_seconds:
            self.disarm(f"Target lost: {reason}")
            return
        with self._lock:
            self._snapshot.state = AutomationState.ARMED_WAITING
            self._snapshot.message = (
                f"Holding last position ({reason})"
                if elapsed >= limits.target_loss_hold_seconds
                else f"Target temporarily unavailable ({reason})"
            )

    def _query_pose(self) -> PtzMotorPose:
        async def query() -> PtzMotorPose:
            pan, tilt = await self.panasonic.query_pan_tilt_position()
            zoom = await self.panasonic.query_zoom_position()
            return PtzMotorPose(pan, tilt, zoom)

        return asyncio.run(query())

    def _manual_motion_detected(self, actual: PtzMotorPose, now: float) -> bool:
        commanded = self._snapshot.commanded_pose
        if commanded is None:
            return False
        limits = self.config.automation
        # Panasonic reports intermediate motor positions while an absolute
        # command is settling.  Those positions can briefly be farther from
        # the target than the manual-override thresholds, especially when pan,
        # tilt, and zoom start at slightly different times.  The explicit
        # grace setting exists for this interval; outside it, unexpected
        # displacement still fails closed immediately.
        if now - self._last_command <= _settling_timeout(limits):
            return False
        # Each automatic command is bounded below these override thresholds.
        # A camera position farther away than the threshold therefore cannot
        # have been requested by the most recent automatic step, even while
        # that step is still settling.
        return (
            abs(actual.pan - commanded.pan) > limits.manual_override_pan_units
            or abs(actual.tilt - commanded.tilt) > limits.manual_override_tilt_units
            or abs(actual.zoom - commanded.zoom) > limits.manual_override_zoom_units
        )

    def _smoothed_pose(
        self,
        desired: PtzMotorPose,
        actual: PtzMotorPose | None = None,
    ) -> PtzMotorPose:
        previous = self._last_recommended
        if previous is None:
            self._last_recommended = desired
            return desired
        alpha = 0.38
        selected = PtzMotorPose(
            round(previous.pan + (desired.pan - previous.pan) * alpha),
            round(previous.tilt + (desired.tilt - previous.tilt) * alpha),
            round(previous.zoom + (desired.zoom - previous.zoom) * alpha),
        )
        if actual is not None:
            limits = self.config.automation
            selected = PtzMotorPose(
                actual.pan
                if abs(desired.pan - actual.pan) <= limits.pan_deadband_units
                else selected.pan,
                actual.tilt
                if abs(desired.tilt - actual.tilt) <= limits.tilt_deadband_units
                else selected.tilt,
                actual.zoom
                if abs(desired.zoom - actual.zoom) <= limits.zoom_deadband_units
                else selected.zoom,
            )
        self._last_recommended = selected
        return selected

    def _send_continuous_command(
        self,
        actual: PtzMotorPose,
        desired: PtzMotorPose,
        now: float,
        *,
        live_bounds: NormalizedRect | None,
        authority_generation: int,
        group_tracking: bool = False,
    ) -> None:
        with self._lock:
            if (
                not self._armed
                or authority_generation != self._authority_generation
            ):
                return
            image_motion_scale = None
            frame_spans = None
            if self.tracking_refinements_enabled and self._geometry is not None:
                try:
                    pan_span, tilt_span = self._geometry.image_axis_motor_units(actual)
                    frame_spans = (pan_span, tilt_span)
                    if not group_tracking:
                        image_motion_scale = (min(1.0, max(.10, pan_span/1600.0)),
                                              min(1.0, max(.10, tilt_span/900.0)))
                except (ValueError, AttributeError):
                    pass
            command = self._velocity.update(
                actual=actual,
                desired=desired,
                config=self.config.automation,
                now=now,
                live_bounds=live_bounds,
                group_tracking=group_tracking,
                ease_motion=self.motion_easing_enabled,
                responsive_tracking=self.tracking_refinements_enabled,
                image_motion_scale=image_motion_scale,
                frame_spans=frame_spans,
            )
            previous = self._last_velocity_command
            refresh = self.config.automation.continuous_command_refresh_seconds
            refresh_due = command.moving and now - self._last_velocity_sent >= refresh
            should_send = command != previous or refresh_due
        if not should_send:
            return

        async def send() -> None:
            if (command.pan, command.tilt) != (previous.pan, previous.tilt):
                if not await self.panasonic.pan_tilt(command.pan, command.tilt):
                    raise RuntimeError("Panasonic rejected the tracking pan/tilt speed.")
            elif refresh_due and (command.pan, command.tilt) != (
                50,
                50,
            ):
                if not await self.panasonic.pan_tilt(command.pan, command.tilt):
                    raise RuntimeError("Panasonic rejected the tracking pan/tilt refresh.")
            if command.zoom != previous.zoom:
                if not await self.panasonic.zoom(command.zoom):
                    raise RuntimeError("Panasonic rejected the tracking zoom speed.")
            elif refresh_due and command.zoom != 50:
                if not await self.panasonic.zoom(command.zoom):
                    raise RuntimeError("Panasonic rejected the tracking zoom refresh.")

        asyncio.run(send())
        revoked = False
        with self._lock:
            revoked = (
                not self._armed
                or authority_generation != self._authority_generation
            )
            # Record an in-flight command even after revocation so the stop
            # path knows exactly which axes must be neutralized.
            self._last_velocity_command = command
            if not revoked:
                self._last_velocity_sent = now
                self._last_command = now
                self._snapshot.commanded_pose = None
                self._snapshot.last_command_monotonic = now
                self._snapshot.commands_sent += 1
        if revoked:
            # A manual command may have revoked authority while the network
            # request was in flight. Stop our velocity before returning.
            self._stop_continuous_motion()
            return
        self._log(
            "info",
            "ptz_automation_velocity",
            "Continuous PTZ tracking speed updated",
            pan_speed=command.pan,
            tilt_speed=command.tilt,
            zoom_speed=command.zoom,
        )

    def _stop_continuous_motion(self) -> None:
        with self._lock:
            previous = self._last_velocity_command
            self._velocity.halt()
            self._last_velocity_command = PtzVelocityCommand()
            self._last_velocity_sent = 0.0
        if not previous.moving:
            return

        async def stop() -> None:
            if (previous.pan, previous.tilt) != (50, 50):
                await self.panasonic.stop_pan_tilt()
            if previous.zoom != 50:
                await self.panasonic.stop_zoom()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                asyncio.run(stop())
            except Exception as exc:
                self._log_stop_failure(exc)
            return

        # VISCA and API callbacks already execute inside an asyncio loop. A
        # nested asyncio.run() raises before sending the neutral command and
        # leaves Panasonic's last velocity latched. Schedule the stop on that
        # live loop and retain the task until it has completed.
        task = loop.create_task(stop())
        with self._lock:
            self._pending_stop_tasks.add(task)
        task.add_done_callback(self._stop_task_finished)

    def _stop_task_finished(self, task: asyncio.Task[None]) -> None:
        with self._lock:
            self._pending_stop_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            self._log_stop_failure(RuntimeError("PTZ stop task was cancelled"))
        except Exception as exc:
            self._log_stop_failure(exc)

    def _log_stop_failure(self, exc: Exception) -> None:
        # Revoking motion authority must still complete even if the camera is
        # temporarily unreachable; surface the failed stop in logs.
        self._log(
            "warning",
            "ptz_automation_stop_failed",
            "Could not confirm the PTZ tracking stop command",
            error=str(exc),
        )

    def _record_tracking_source(
        self,
        source: str,
        *,
        actual: PtzMotorPose | None,
        desired: PtzMotorPose | None,
        lock_count: int,
        ptz_matches: int,
        audience_matches: int,
        audience_coasts: int,
    ) -> None:
        if source == self._last_tracking_source:
            return
        self._last_tracking_source = source
        self._log(
            "info",
            "ptz_tracking_source_changed",
            f"Subject Tracking source changed to {source}",
            source=source,
            lock_count=lock_count,
            ptz_matches=ptz_matches,
            audience_matches=audience_matches,
            audience_coasts=audience_coasts,
            actual=(
                {"pan": actual.pan, "tilt": actual.tilt, "zoom": actual.zoom}
                if actual is not None
                else None
            ),
            desired=(
                {"pan": desired.pan, "tilt": desired.tilt, "zoom": desired.zoom}
                if desired is not None
                else None
            ),
        )

    def _send_bounded_command(
        self,
        actual: PtzMotorPose,
        desired: PtzMotorPose,
        now: float,
        *,
        authority_generation: int | None = None,
    ) -> None:
        if authority_generation is not None:
            with self._lock:
                if (
                    not self._armed
                    or authority_generation != self._authority_generation
                ):
                    return
        limits = self.config.automation
        if now - self._last_command < limits.minimum_command_interval_seconds:
            return
        pending = self._snapshot.commanded_pose
        if (
            pending is not None
            and self._last_command > 0
            and not _pose_is_settled(actual, pending, limits)
            and now - self._last_command < _settling_timeout(limits)
        ):
            # Do not queue a new absolute target while Panasonic is still
            # traversing the previous bounded step.  Coalescing here keeps the
            # motor trajectory smooth and prevents an old queued command from
            # looking like external movement after the target reverses.
            return
        pan_delta = desired.pan - actual.pan
        tilt_delta = desired.tilt - actual.tilt
        zoom_delta = desired.zoom - actual.zoom
        pan = actual.pan + _bounded_delta(
            pan_delta,
            limits.pan_deadband_units,
            limits.maximum_pan_step_units,
        )
        tilt = actual.tilt + _bounded_delta(
            tilt_delta,
            limits.tilt_deadband_units,
            limits.maximum_tilt_step_units,
        )
        zoom = actual.zoom + _bounded_delta(
            zoom_delta,
            limits.zoom_deadband_units,
            limits.maximum_zoom_step_units,
        )
        move_pan_tilt = (pan, tilt) != (actual.pan, actual.tilt)
        move_zoom = zoom != actual.zoom
        if not move_pan_tilt and not move_zoom:
            return

        async def send() -> None:
            if move_pan_tilt and not await self.panasonic.absolute_pan_tilt(pan, tilt):
                raise RuntimeError("Panasonic rejected the absolute pan/tilt command.")
            if move_zoom and not await self.panasonic.absolute_zoom(zoom):
                raise RuntimeError("Panasonic rejected the absolute zoom command.")

        asyncio.run(send())
        commanded = PtzMotorPose(pan, tilt, zoom)
        with self._lock:
            if authority_generation is not None and (
                not self._armed
                or authority_generation != self._authority_generation
            ):
                return
            self._last_command = now
            self._snapshot.commanded_pose = commanded
            self._snapshot.last_command_monotonic = now
            self._snapshot.commands_sent += 1
        self._log(
            "info",
            "ptz_automation_command",
            "Bounded absolute PTZ command sent",
            pan=pan,
            tilt=tilt,
            zoom=zoom,
        )

    def _recoverable_cycle_error(self, exc: Exception) -> None:
        self._stop_continuous_motion()
        with self._lock:
            if self._armed:
                self._snapshot.state = AutomationState.ARMED_WAITING
                self._snapshot.message = (
                    "Holding camera after a temporary PTZ communication error "
                    f"({self._consecutive_cycle_errors}/3)"
                )
                self._snapshot.last_error = str(exc)
        self._log(
            "warning",
            "ptz_automation_cycle_retry",
            "PTZ tracking cycle failed; holding before retry",
            attempt=self._consecutive_cycle_errors,
            error=str(exc),
        )

    def _fault(self, exc: Exception) -> None:
        message = f"PTZ automation fault: {exc}"
        self.disarm(message)
        with self._lock:
            self._snapshot.state = AutomationState.FAULT
            self._snapshot.last_error = str(exc)
        self._log("error", "ptz_automation_fault", message)

    def _log(self, level: str, event: str, message: str, **metadata: object) -> None:
        if self.logger is None:
            return
        callback = getattr(self.logger, level, None)
        if callback is not None:
            callback(event, message, **metadata)


def _bounded_delta(delta: int, deadband: int, maximum: int) -> int:
    if abs(delta) <= deadband:
        return 0
    return max(-maximum, min(maximum, int(delta)))


def _settling_timeout(limits) -> float:
    return max(6.0, float(limits.manual_override_grace_seconds))


def _pose_is_settled(actual: PtzMotorPose, commanded: PtzMotorPose, limits) -> bool:
    return (
        abs(actual.pan - commanded.pan) <= limits.pan_deadband_units
        and abs(actual.tilt - commanded.tilt) <= limits.tilt_deadband_units
        and abs(actual.zoom - commanded.zoom) <= limits.zoom_deadband_units
    )


def _target_is_continuous(
    previous: NormalizedRect | None,
    current: NormalizedRect | None,
    previous_count: int,
    current_count: int,
) -> bool:
    if previous is None or current is None or previous_count != current_count:
        return False
    return (
        previous.intersection_over_union(current) >= 0.08
        or previous.center_distance(current) <= 0.12
    )


def _centred_motor_pose(
    actual: PtzMotorPose,
    ptz_snapshot: TrackingSnapshot,
    geometry: PtzGeometryModel,
) -> PtzMotorPose:
    """The motor pose that would put the locked people at image centre.

    The handoff anchor pairs this pose with the Audience-mapped pose for the
    same people, and recovery moves by the difference. The live anchor is
    refreshed until the last frame the PTZ sees them -- typically with them
    at the very edge -- so using the raw pose biased recovery by up to half a
    frame, short of a walker in one direction.
    """

    bounds = _subject_union(ptz_snapshot)
    if bounds is None:
        return actual
    try:
        pan_span, tilt_span = geometry.image_axis_motor_units(actual)
    except (AttributeError, ValueError):
        return actual
    center_x, center_y = bounds.center
    # Aiming left raises Panasonic pan; aiming down raises tilt.
    return PtzMotorPose(
        round(actual.pan - (center_x - 0.5) * pan_span),
        round(actual.tilt + (center_y - 0.48) * tilt_span),
        actual.zoom,
    )


# Field-of-view multiple the PTZ opens to when a locked person leaves the shot.
_HANDOFF_WIDENING = 1.6


def _subject_union(snapshot: TrackingSnapshot) -> NormalizedRect | None:
    subjects = tuple(snapshot.subjects)
    if not subjects:
        return None
    left = min(item.bounds.x for item in subjects)
    top = min(item.bounds.y for item in subjects)
    right = max(item.bounds.x + item.bounds.width for item in subjects)
    bottom = max(item.bounds.y + item.bounds.height for item in subjects)
    return NormalizedRect(left, top, right - left, bottom - top).clamped()


def _reference_target_union(
    targets: tuple[SubjectReferenceTarget, ...] | None,
) -> NormalizedRect | None:
    if not targets:
        return None
    left = min(item.bounds.x for item in targets)
    top = min(item.bounds.y for item in targets)
    right = max(item.bounds.x + item.bounds.width for item in targets)
    bottom = max(item.bounds.y + item.bounds.height for item in targets)
    return NormalizedRect(left, top, right - left, bottom - top)


def _relative_handoff_pose(
    *,
    motor_anchor: PtzMotorPose,
    mapped_anchor: PtzMotorPose,
    mapped_target: PtzMotorPose,
    current_zoom: int,
) -> PtzMotorPose:
    """Translate calibrated target motion from the last trusted motor pose."""

    return PtzMotorPose(
        pan=max(
            0,
            min(
                0xFFFF,
                motor_anchor.pan + mapped_target.pan - mapped_anchor.pan,
            ),
        ),
        tilt=max(
            0,
            min(
                0xFFFF,
                motor_anchor.tilt + mapped_target.tilt - mapped_anchor.tilt,
            ),
        ),
        zoom=int(current_zoom),
    )


def _direction(value: int | float, deadband: int | float = 0) -> int:
    if abs(value) <= abs(deadband):
        return 0
    return 1 if value > 0 else -1
