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
from production_hub.tracking.framing import (
    FramingDecision,
    FramingEngine,
    FramingState,
)
from production_hub.tracking.models import NormalizedRect
from production_hub.tracking.ptz_geometry import (
    PtzGeometryModel,
    PtzMotorPose,
    transform_live_rect_to_reference,
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

    def copy(self) -> PtzAutomationSnapshot:
        return replace(self)


class PtzAutomationService:
    """Safety supervisor and bounded absolute-position PTZ controller.

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
        self._shadow_owners: set[str] = set()
        self._geometry: PtzGeometryModel | None = None
        self._click_target_reference: tuple[float, float] | None = None
        self._target_key: tuple[str, ...] = ()
        self._target_since = 0.0
        self._last_target_seen = 0.0
        self._last_command = 0.0
        self._last_recommended: PtzMotorPose | None = None
        self._snapshot = PtzAutomationSnapshot(mode=config.automation.mode)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def armed(self) -> bool:
        with self._lock:
            return self._armed

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
            if previous_mode != config.automation.mode:
                self._target_key = ()
                self._target_since = 0.0
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
            now = time.monotonic()
            self._last_target_seen = now
            self._last_command = 0.0
            self._last_recommended = None
            self._target_key = ()
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
            self._target_key = ()
            self._target_since = 0.0
            self._last_recommended = None
            self._snapshot.armed = False
            self._snapshot.motion_authority = False
            self._snapshot.state = (
                AutomationState.SHADOW
                if self._shadow_owners and self.config.automation.mode != "off"
                else AutomationState.DISARMED
            )
            self._snapshot.message = str(reason)
        self.video.set_tracking_activity(False, owner=self.ACTIVITY_OWNER)
        self.video.set_calibration_activity(False, owner=self.ACTIVITY_OWNER)
        if was_armed:
            self._log("warning", "ptz_automation_disarmed", str(reason))
        self._wake.set()

    def manual_override(self, reason: str = "Manual camera control") -> None:
        if self.armed:
            self.disarm(f"Disarmed by manual override: {reason}")

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
            except Exception as exc:
                self._fault(exc)
            elapsed = time.monotonic() - started
            self._wake.wait(timeout=max(0.02, 1.0 / rate - elapsed))
            self._wake.clear()

    def _cycle(self, now: float) -> None:
        blocker = self._arm_blocker() if self.armed else ""
        if blocker:
            self.disarm(blocker)
            with self._lock:
                self._snapshot.state = AutomationState.BLOCKED
            return
        with self._lock:
            geometry = self._geometry
            armed = self._armed
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
        decision = self.framing.decide(
            config.automation,
            self.video.tracking.snapshot(VideoSourceKey.AUDIENCE),
            self.video.tracking.snapshot(VideoSourceKey.PTZ),
            self.video.relocalization.snapshot(),
            config.scene_regions,
            geometry,
            current_pose=actual,
            click_target=click_target,
        )
        with self._lock:
            self._snapshot.decision = decision
            self._snapshot.last_decision_monotonic = now
            self._snapshot.actual_pose = actual
            self._snapshot.mode = config.automation.mode
            self._snapshot.last_error = ""
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
        if decision.target_ids != self._target_key:
            self._target_key = decision.target_ids
            self._target_since = now
        if now - self._target_since < config.automation.target_dwell_seconds:
            with self._lock:
                self._snapshot.state = AutomationState.ARMED_WAITING
                self._snapshot.message = "Waiting for the target selection to stabilize"
            return
        smoothed = self._smoothed_pose(decision.desired_pose)
        with self._lock:
            self._snapshot.commanded_pose = smoothed
            self._snapshot.state = AutomationState.ARMED_TRACKING
            self._snapshot.message = (
                f"Tracking {len(decision.target_ids)} target(s)"
                + (" with podium framing" if decision.podium_framing else "")
            )
        if actual is not None:
            self._send_bounded_command(actual, smoothed, now)

    def _handle_target_loss(self, now: float, reason: str) -> None:
        elapsed = now - self._last_target_seen
        limits = self.config.automation
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

    def _smoothed_pose(self, desired: PtzMotorPose) -> PtzMotorPose:
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
        self._last_recommended = selected
        return selected

    def _send_bounded_command(
        self,
        actual: PtzMotorPose,
        desired: PtzMotorPose,
        now: float,
    ) -> None:
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
