"""Deterministic, hardware-free simulation of armed PTZ Subject Tracking.

Subject Tracking is a closed loop: the camera moves, which changes what the PTZ
detector sees, which changes the next command. Replaying it from a list of
fixed image-space rectangles would freeze the picture while the motors moved
and make every recorded velocity a fiction. This harness therefore places
people in calibrated reference coordinates and projects them through the
simulated motor pose on every step, so panning really does move the subject
across the PTZ image.

Everything is synthetic and time-injected. Scenarios advance a caller-supplied
monotonic clock and drive `PtzAutomationService._cycle` directly rather than
its worker thread, so a recording made on one machine reproduces exactly on
another.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable, Sequence

from production_hub.calibration.relocalization import (
    RelocalizationSnapshot,
    RelocalizationState,
)
from production_hub.core.config.models import (
    CameraSceneRegion,
    CameraTrackingConfig,
    SceneRegionPoint,
)
from production_hub.tracking.models import (
    NormalizedRect,
    TrackedSubject,
    TrackingSnapshot,
    TrackingState,
)
from production_hub.tracking.ptz_automation import PtzAutomationService
from production_hub.tracking.ptz_geometry import (
    CalibrationPoseAnchor,
    PtzGeometryModel,
    PtzMotorPose,
)
from production_hub.video.models import (
    VideoSourceKey,
    VideoSourceSnapshot,
    VideoSourceState,
)

REFERENCE_SIZE = (1600, 900)
DECISION_INTERVAL = 0.25

# Panasonic reports the pan axis mirrored with respect to the reference image:
# an AW speed above 50 decreases the absolute pan coordinate while moving the
# view to the right. The calibration anchors below encode that convention, and
# `test_ptz_tracking_harness.py` fails closed if it is ever inverted, because
# the image-space controller would then drive away from the subject.
_PAN_AT_ORIGIN = 46000
_PAN_UNITS_PER_REFERENCE_X = -9000
_TILT_AT_ORIGIN = 32000
_TILT_UNITS_PER_REFERENCE_Y = 5200

_ZOOM_WIDE = 1500
_ZOOM_TELE = 3500
_FOOTPRINT_AT_WIDE = 0.62
_FOOTPRINT_AT_TELE = 0.045


# Reference-space (width, height) of a person in a handful of real postures.
# A standing adult is 0.10 of the reference frame height, which is the scale a
# calibrated sanctuary sweep produces for a wide Audience camera.
POSES: dict[str, tuple[float, float]] = {
    "standing": (0.034, 0.100),
    "turned_away": (0.030, 0.098),
    "upper_body": (0.040, 0.055),
    "arms_spread": (0.092, 0.078),
    "wide_gesture": (0.078, 0.052),
    "leaning_over_lectern": (0.070, 0.048),
    "raised_hand": (0.048, 0.126),
    "crouching": (0.046, 0.058),
    "seated": (0.044, 0.062),
}

STAGE_FLOOR = 0.62


@dataclass(frozen=True, slots=True)
class WorldSubject:
    """One person, positioned on the stage floor in reference coordinates."""

    name: str
    pose: str = "standing"
    x: float = 0.50
    floor: float = STAGE_FLOOR
    confidence: float = 0.92
    seen_by_ptz: bool = True
    seen_by_audience: bool = True
    detector_suffix: int = 0

    @property
    def rect(self) -> NormalizedRect:
        width, height = POSES[self.pose]
        return NormalizedRect(
            self.x - width / 2.0,
            self.floor - height,
            width,
            height,
        ).clamped()

    def moved(self, **changes: object) -> WorldSubject:
        return replace(self, **changes)


def person(
    name: str,
    *,
    pose: str = "standing",
    x: float = 0.50,
    floor: float = STAGE_FLOOR,
    confidence: float = 0.92,
    seen_by_ptz: bool = True,
    seen_by_audience: bool = True,
    detector_suffix: int = 0,
) -> WorldSubject:
    if pose not in POSES:
        raise ValueError(f"Unknown pose {pose!r}; add it to POSES.")
    return WorldSubject(
        name=name,
        pose=pose,
        x=x,
        floor=floor,
        confidence=confidence,
        seen_by_ptz=seen_by_ptz,
        seen_by_audience=seen_by_audience,
        detector_suffix=detector_suffix,
    )


def build_geometry() -> PtzGeometryModel:
    """A synthetic sanctuary sweep with a usable zoom model.

    Four pan/tilt positions at each of two zooms. Both image planes are 16:9
    and the reference canvas is normalized to the unit square, so a view
    covering fraction f of the reference height covers fraction f of its width
    too -- the property the production occupancy maths gets wrong today.
    """

    anchors: list[CalibrationPoseAnchor] = []
    for zoom, footprint in ((_ZOOM_WIDE, _FOOTPRINT_AT_WIDE), (_ZOOM_TELE, _FOOTPRINT_AT_TELE)):
        for center_x, center_y in ((0.30, 0.35), (0.70, 0.35), (0.30, 0.65), (0.70, 0.65)):
            anchors.append(
                CalibrationPoseAnchor(
                    name=f"{center_x}-{center_y}@{zoom}",
                    center_x=center_x,
                    center_y=center_y,
                    footprint_width=footprint,
                    footprint_height=footprint,
                    motor=PtzMotorPose(
                        round(_PAN_AT_ORIGIN + center_x * _PAN_UNITS_PER_REFERENCE_X),
                        round(_TILT_AT_ORIGIN + center_y * _TILT_UNITS_PER_REFERENCE_Y),
                        zoom,
                    ),
                    ptz_size=REFERENCE_SIZE,
                    audience_size=REFERENCE_SIZE,
                    ptz_to_audience=(
                        (footprint, 0.0, (center_x - footprint / 2.0) * REFERENCE_SIZE[0]),
                        (0.0, footprint, (center_y - footprint / 2.0) * REFERENCE_SIZE[1]),
                        (0.0, 0.0, 1.0),
                    ),
                )
            )
    return PtzGeometryModel(Path("/approved/harness_sweep.json"), "harness", anchors)


def locked_relocalization() -> RelocalizationSnapshot:
    return RelocalizationSnapshot(
        state=RelocalizationState.LOCKED,
        approval_status="approved",
        reference_size=REFERENCE_SIZE,
        live_size=REFERENCE_SIZE,
    )


def stage_regions() -> list[CameraSceneRegion]:
    return [
        CameraSceneRegion(
            id="stage",
            name="Stage",
            kind="stage",
            points=[
                SceneRegionPoint(0.10, 0.30),
                SceneRegionPoint(0.90, 0.30),
                SceneRegionPoint(0.90, 0.75),
                SceneRegionPoint(0.10, 0.75),
            ],
        ),
        CameraSceneRegion(
            id="podium",
            name="Podium",
            kind="podium",
            points=[
                SceneRegionPoint(0.44, 0.50),
                SceneRegionPoint(0.56, 0.50),
                SceneRegionPoint(0.56, 0.70),
                SceneRegionPoint(0.44, 0.70),
            ],
        ),
    ]


# --- simulated camera -------------------------------------------------------

# Full-scale AW speed (an offset of 49 from neutral) in motor units per second.
_PAN_UNITS_PER_SECOND = 2600.0
_TILT_UNITS_PER_SECOND = 1200.0
_ZOOM_UNITS_PER_SECOND = 900.0


@dataclass(slots=True)
class _MotorState:
    pan: int
    tilt: int
    zoom: int
    pan_speed: int = 50
    tilt_speed: int = 50
    zoom_speed: int = 50


class SimulatedPanasonic:
    """Accepts the production Panasonic calls and integrates motor motion.

    Only the calls Subject Tracking makes are implemented. Speeds latch exactly
    as the real camera latches them, so a missing stop command shows up in a
    recording as continued motion rather than as a silent no-op.
    """

    def __init__(self, pose: PtzMotorPose) -> None:
        self.config = SimpleNamespace(enabled=True)
        self._state = _MotorState(pose.pan, pose.tilt, pose.zoom)
        self.pan_tilt_speed_commands: list[tuple[int, int]] = []
        self.zoom_speed_commands: list[int] = []
        self.absolute_commands: list[tuple[int, int, int]] = []

    @property
    def pose(self) -> PtzMotorPose:
        return PtzMotorPose(
            round(self._state.pan),
            round(self._state.tilt),
            round(self._state.zoom),
        )

    def advance(self, seconds: float) -> None:
        state = self._state
        # An AW speed above 50 drives the absolute pan and tilt coordinates
        # downward; zoom is not inverted. See velocity_controller.update.
        state.pan -= (state.pan_speed - 50) / 49.0 * _PAN_UNITS_PER_SECOND * seconds
        state.tilt -= (state.tilt_speed - 50) / 49.0 * _TILT_UNITS_PER_SECOND * seconds
        state.zoom += (state.zoom_speed - 50) / 49.0 * _ZOOM_UNITS_PER_SECOND * seconds
        state.pan = max(0, min(0xFFFF, state.pan))
        state.tilt = max(0, min(0xFFFF, state.tilt))
        state.zoom = max(0x555, min(0xE50, state.zoom))

    async def query_pan_tilt_position(self) -> tuple[int, int]:
        return self.pose.pan, self.pose.tilt

    async def query_zoom_position(self) -> int:
        return self.pose.zoom

    async def pan_tilt(self, pan: int, tilt: int) -> bool:
        self.pan_tilt_speed_commands.append((pan, tilt))
        self._state.pan_speed = int(pan)
        self._state.tilt_speed = int(tilt)
        return True

    async def zoom(self, zoom: int) -> bool:
        self.zoom_speed_commands.append(int(zoom))
        self._state.zoom_speed = int(zoom)
        return True

    async def stop_pan_tilt(self) -> bool:
        self.pan_tilt_speed_commands.append((50, 50))
        self._state.pan_speed = 50
        self._state.tilt_speed = 50
        return True

    async def stop_zoom(self) -> bool:
        self.zoom_speed_commands.append(50)
        self._state.zoom_speed = 50
        return True

    async def absolute_pan_tilt(self, pan: int, tilt: int) -> bool:
        self.absolute_commands.append((int(pan), int(tilt), self.pose.zoom))
        self._state.pan, self._state.tilt = int(pan), int(tilt)
        return True

    async def absolute_zoom(self, zoom: int) -> bool:
        self._state.zoom = int(zoom)
        return True


class _ScriptedTracking:
    def __init__(self) -> None:
        self.values: dict[VideoSourceKey, TrackingSnapshot] = {
            VideoSourceKey.AUDIENCE: TrackingSnapshot(VideoSourceKey.AUDIENCE),
            VideoSourceKey.PTZ: TrackingSnapshot(VideoSourceKey.PTZ),
        }

    def snapshot(self, source: VideoSourceKey) -> TrackingSnapshot:
        return self.values[source].copy()


class SimulatedVideo:
    def __init__(self) -> None:
        self.tracking = _ScriptedTracking()
        self._relocalization = locked_relocalization()
        self.activity: list[tuple[str, bool, str]] = []

    class _Reloc:
        def __init__(self, value: RelocalizationSnapshot) -> None:
            self.value = value

        def snapshot(self) -> RelocalizationSnapshot:
            return self.value.copy()

    @property
    def relocalization(self) -> "SimulatedVideo._Reloc":
        return SimulatedVideo._Reloc(self._relocalization)

    def snapshot(self, _source: VideoSourceKey) -> VideoSourceSnapshot:
        return VideoSourceSnapshot(
            VideoSourceKey.AUDIENCE,
            VideoSourceState.RUNNING,
            last_frame_monotonic=time.monotonic(),
        )

    def set_tracking_activity(self, active: bool, *, owner: str = "ui") -> None:
        self.activity.append(("tracking", active, owner))

    def set_calibration_activity(self, active: bool, *, owner: str = "ui") -> None:
        self.activity.append(("calibration", active, owner))


# --- detector simulation ----------------------------------------------------

MINIMUM_VISIBLE_FRACTION = 0.45


def ptz_view_box(geometry: PtzGeometryModel, pose: PtzMotorPose) -> NormalizedRect:
    corners = geometry.reference_polygon_for_pose(pose)
    left = min(x for x, _y in corners)
    right = max(x for x, _y in corners)
    top = min(y for _x, y in corners)
    bottom = max(y for _x, y in corners)
    return NormalizedRect(left, top, right - left, bottom - top)


def reference_rect_to_ptz_image(
    bounds: NormalizedRect,
    view: NormalizedRect,
) -> NormalizedRect:
    """Inverse of `PtzGeometryModel.ptz_rect_to_reference` for this sweep.

    The harness calibration is a pure scale-and-translate, so the inverse is
    exact. Production has no such helper yet; adding one is Phase 3 work.
    """

    if view.width <= 0 or view.height <= 0:
        raise ValueError("The PTZ view has no area.")
    return NormalizedRect(
        (bounds.x - view.x) / view.width,
        (bounds.y - view.y) / view.height,
        bounds.width / view.width,
        bounds.height / view.height,
    )


def _visible_fraction(image_rect: NormalizedRect) -> float:
    clamped = image_rect.clamped()
    full = max(1e-9, image_rect.area)
    return clamped.area / full


class _DetectorAges:
    """Per-camera `age_frames`, reset when a person leaves the view."""

    def __init__(self) -> None:
        self._ages: dict[str, int] = {}

    def observe(self, names: Iterable[str]) -> dict[str, int]:
        present = set(names)
        for name in list(self._ages):
            if name not in present:
                self._ages.pop(name)
        for name in present:
            self._ages[name] = self._ages.get(name, 0) + 1
        return dict(self._ages)


# --- scenarios --------------------------------------------------------------


def zoom_for_footprint(footprint: float) -> int:
    """Invert the log-linear zoom model the harness anchors encode."""

    slope = (math.log(_FOOTPRINT_AT_TELE) - math.log(_FOOTPRINT_AT_WIDE)) / (
        _ZOOM_TELE - _ZOOM_WIDE
    )
    zoom = _ZOOM_WIDE + (math.log(max(1e-4, footprint)) - math.log(_FOOTPRINT_AT_WIDE)) / slope
    return int(max(0x555, min(0xE50, round(zoom))))


def union_rect(rectangles: Sequence[NormalizedRect]) -> NormalizedRect:
    left = min(item.x for item in rectangles)
    top = min(item.y for item in rectangles)
    right = max(item.x + item.width for item in rectangles)
    bottom = max(item.y + item.height for item in rectangles)
    return NormalizedRect(left, top, right - left, bottom - top)


def pose_framing(subjects: Sequence[WorldSubject], *, occupancy: float = 0.78) -> PtzMotorPose:
    """The shot an operator would set up by hand before arming."""

    bounds = union_rect([item.rect for item in subjects])
    center_x, center_y = bounds.center
    footprint = max(bounds.height, bounds.width) / max(0.05, occupancy)
    return PtzMotorPose(
        pan=round(_PAN_AT_ORIGIN + center_x * _PAN_UNITS_PER_REFERENCE_X),
        tilt=round(_TILT_AT_ORIGIN + center_y * _TILT_UNITS_PER_REFERENCE_Y),
        zoom=zoom_for_footprint(footprint),
    )


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    steps: tuple[tuple[WorldSubject, ...], ...]
    initial_pose: PtzMotorPose
    mode: str = "subject"
    dwell_seconds: float = 0.0
    minimum_subject_age_frames: int = 2
    warmup_steps: int = 2
    motion_easing_enabled: bool = False
    tracking_refinements_enabled: bool = False
    safe_zone_tracking_enabled: bool = False
    observation_delay_steps: int = 0

    @property
    def all_steps(self) -> tuple[tuple[WorldSubject, ...], ...]:
        # Detector `age_frames` must reach the configured minimum before the
        # lock can form, exactly as it must on a real arm.
        if not self.steps:
            return ()
        return (self.steps[0],) * self.warmup_steps + self.steps


def scenario(
    name: str,
    steps: Sequence[Sequence[WorldSubject]],
    *,
    occupancy: float = 0.78,
    initial_pose: PtzMotorPose | None = None,
    **options: object,
) -> Scenario:
    frames = tuple(tuple(item) for item in steps)
    if not frames:
        raise ValueError("A scenario needs at least one step.")
    return Scenario(
        name=name,
        steps=frames,
        initial_pose=initial_pose or pose_framing(frames[0], occupancy=occupancy),
        **options,  # type: ignore[arg-type]
    )


@dataclass(slots=True)
class StepRecord:
    step: int
    pan: int
    tilt: int
    zoom: int
    automation_state: str
    decision_state: str
    tracking_source: str
    target_ids: tuple[str, ...]
    target_bounds: tuple[float, float, float, float] | None
    desired_pose: tuple[int, int, int] | None
    podium_framing: bool
    pan_speed: int
    tilt_speed: int
    zoom_speed: int
    ptz_detections: tuple[tuple[float, float, float, float], ...]

    def as_json(self) -> dict[str, object]:
        return {
            "step": self.step,
            "pose": [self.pan, self.tilt, self.zoom],
            "automationState": self.automation_state,
            "decisionState": self.decision_state,
            "trackingSource": self.tracking_source,
            "targetIds": list(self.target_ids),
            "targetBounds": (
                None if self.target_bounds is None else list(self.target_bounds)
            ),
            "desiredPose": (
                None if self.desired_pose is None else list(self.desired_pose)
            ),
            "podiumFraming": self.podium_framing,
            "speeds": [self.pan_speed, self.tilt_speed, self.zoom_speed],
            "ptzDetections": [list(item) for item in self.ptz_detections],
        }


@dataclass(slots=True)
class ScenarioRecording:
    name: str
    armed: bool
    arm_message: str
    steps: list[StepRecord] = field(default_factory=list)

    def as_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "armed": self.armed,
            "steps": [item.as_json() for item in self.steps],
        }

    @property
    def final(self) -> StepRecord:
        return self.steps[-1]


def _round4(value: float) -> float:
    return round(float(value) + 0.0, 4)


def _rect_tuple(bounds: NormalizedRect | None) -> tuple[float, float, float, float] | None:
    if bounds is None:
        return None
    return (
        _round4(bounds.x),
        _round4(bounds.y),
        _round4(bounds.width),
        _round4(bounds.height),
    )


class _TrackIds:
    """Stable detector IDs per camera, renewed when a scenario asks for it."""

    def __init__(self) -> None:
        self._issued: dict[tuple[str, int], int] = {}
        self._next = 1

    def id_for(self, subject: WorldSubject) -> int:
        key = (subject.name, subject.detector_suffix)
        if key not in self._issued:
            self._issued[key] = self._next
            self._next += 1
        return self._issued[key]


def _detector_snapshot(
    source: VideoSourceKey,
    observations: Sequence[tuple[int, NormalizedRect, float, int]],
    now: float,
) -> TrackingSnapshot:
    subjects = tuple(
        TrackedSubject(
            track_id=track_id,
            bounds=bounds.clamped(),
            confidence=confidence,
            selected=False,
            age_frames=age,
            last_seen_monotonic=now,
        )
        for track_id, bounds, confidence, age in observations
    )
    return TrackingSnapshot(
        source=source,
        state=TrackingState.RUNNING,
        message=f"Detected {len(subjects)} subject(s)",
        subjects=subjects,
        last_analysis_monotonic=now,
    )


def run_scenario(scenario: Scenario) -> ScenarioRecording:
    """Drive the real supervisor through a scripted scene, step by step.

    `_cycle` is called directly instead of starting the worker thread: the
    thread paces itself off the wall clock, which would make every recording
    machine-dependent.
    """

    geometry = build_geometry()
    video = SimulatedVideo()
    panasonic = SimulatedPanasonic(scenario.initial_pose)
    config = CameraTrackingConfig(enabled=True, scene_regions=stage_regions())
    config.automation.mode = scenario.mode
    config.automation.target_dwell_seconds = scenario.dwell_seconds
    config.automation.minimum_subject_age_frames = scenario.minimum_subject_age_frames
    service = PtzAutomationService(
        video,
        panasonic,
        config,
        Path("/tmp/production-hub-harness"),
        geometry_loader=lambda _root: geometry,
    )

    ptz_ages = _DetectorAges()
    service.motion_easing_enabled = scenario.motion_easing_enabled
    service.tracking_refinements_enabled = scenario.tracking_refinements_enabled
    service.safe_zone_tracking_enabled = scenario.safe_zone_tracking_enabled
    audience_ages = _DetectorAges()
    ptz_ids = _TrackIds()
    audience_ids = _TrackIds()
    steps = scenario.all_steps

    def observe(subjects: Sequence[WorldSubject], pose: PtzMotorPose, now: float) -> None:
        view = ptz_view_box(geometry, pose)
        ptz_visible: list[tuple[WorldSubject, NormalizedRect]] = []
        for subject in subjects:
            if not subject.seen_by_ptz:
                continue
            image_rect = reference_rect_to_ptz_image(subject.rect, view)
            if _visible_fraction(image_rect) < MINIMUM_VISIBLE_FRACTION:
                continue
            ptz_visible.append((subject, image_rect.clamped()))
        ptz_age = ptz_ages.observe(item[0].name for item in ptz_visible)
        video.tracking.values[VideoSourceKey.PTZ] = _detector_snapshot(
            VideoSourceKey.PTZ,
            [
                (ptz_ids.id_for(subject), rect, subject.confidence, ptz_age[subject.name])
                for subject, rect in ptz_visible
            ],
            now,
        )
        wide = [item for item in subjects if item.seen_by_audience]
        audience_age = audience_ages.observe(item.name for item in wide)
        video.tracking.values[VideoSourceKey.AUDIENCE] = _detector_snapshot(
            VideoSourceKey.AUDIENCE,
            [
                (
                    audience_ids.id_for(subject),
                    subject.rect,
                    subject.confidence,
                    audience_age[subject.name],
                )
                for subject in wide
            ],
            now,
        )

    start = time.monotonic()
    observe(steps[0], panasonic.pose, start)
    armed, message = service.arm()
    recording = ScenarioRecording(scenario.name, armed, message)
    if not armed:
        return recording

    observations = []
    try:
        for index, subjects in enumerate(steps):
            now = start + index * DECISION_INTERVAL
            if index:
                panasonic.advance(DECISION_INTERVAL)
            pose = panasonic.pose
            observations.append((subjects, pose))
            observed_subjects, observed_pose = observations[max(0,index-max(0,scenario.observation_delay_steps))]
            observe(observed_subjects, observed_pose, now)
            service._cycle(now)
            snapshot = service.snapshot()
            decision = snapshot.decision
            command = service._last_velocity_command
            ptz_snapshot = video.tracking.values[VideoSourceKey.PTZ]
            recording.steps.append(
                StepRecord(
                    step=index,
                    pan=pose.pan,
                    tilt=pose.tilt,
                    zoom=pose.zoom,
                    automation_state=snapshot.state.value,
                    decision_state="none" if decision is None else decision.state.value,
                    tracking_source=snapshot.tracking_source,
                    target_ids=() if decision is None else tuple(decision.target_ids),
                    target_bounds=(
                        None if decision is None else _rect_tuple(decision.target_bounds)
                    ),
                    desired_pose=(
                        None
                        if decision is None or decision.desired_pose is None
                        else (
                            decision.desired_pose.pan,
                            decision.desired_pose.tilt,
                            decision.desired_pose.zoom,
                        )
                    ),
                    podium_framing=bool(decision is not None and decision.podium_framing),
                    pan_speed=command.pan,
                    tilt_speed=command.tilt,
                    zoom_speed=command.zoom,
                    ptz_detections=tuple(
                        _rect_tuple(item.bounds) for item in ptz_snapshot.subjects
                    ),
                )
            )
    finally:
        service.disarm("harness finished")
    return recording


def ptz_image_rects(
    subjects: Sequence[WorldSubject],
    pose: PtzMotorPose,
    geometry: PtzGeometryModel | None = None,
) -> list[NormalizedRect]:
    """What the PTZ detector would report for these people at this pose."""

    view = ptz_view_box(geometry or build_geometry(), pose)
    return [reference_rect_to_ptz_image(item.rect, view) for item in subjects]
