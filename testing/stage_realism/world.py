"""A closed-loop model of the ICAS Seattle stage around the real tracking code.

Only the physical world is simulated: people walking on the measured stage,
the Panasonic motors, the camera optics, video/command latency, and what the
Apple Vision detector returns for a frame. Everything that decides where the
camera goes is production code, driven on a virtual clock:

    simulated frame -> PersonTrackingService._analyze (real association)
                    -> PtzAutomationService._cycle (real lock, framing, velocity)
                    -> simulated Panasonic speed commands -> motors move

Where the simulation deliberately differs from the old harness
(`testing/harness/ptz_tracking.py`):

* The stage, calibration, and live settings come from this church's approved
  calibration and `default_profile.json` (`fixtures/icas_stage.json`).
* The camera moves as fast as real Sunday logs show it moving, not ~5x faster.
* The optics differ from the tracker's calibration model at sermon zoom, as
  the recordings show they do; the tracker is not told the true field of view.
* Detections arrive late (capture, inference, command latency) and carry the
  jitter and upper-body/full-body flicker measured on real footage.
* The wide camera sees the congregation too, so an audience hand-off can pick
  the wrong person.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Sequence

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


STAGE = load_fixture("icas_stage.json")
MEASURED = load_fixture("icas_footage_measurements.json")
SCALE_X = MEASURED["stage_scale"]["reference_width_per_metre"]
SCALE_Y = MEASURED["stage_scale"]["reference_height_per_metre"]
LAYOUT = MEASURED["stage_layout_reference"]
LATENCY = MEASURED["latency_s"]


def metres_to_reference_x(metres: float) -> float:
    """Stage position in metres from the left edge of the deck (camera-left)."""

    return LAYOUT["deck_left_x"] + metres * SCALE_X


def reference_x_to_metres(x: float) -> float:
    return (x - LAYOUT["deck_left_x"]) / SCALE_X


STAGE_WIDTH_M = reference_x_to_metres(LAYOUT["deck_right_x"])
LECTERN_M = reference_x_to_metres(LAYOUT["lectern_x"])
LEFT_EXIT_M = reference_x_to_metres(LAYOUT["left_exit_x"])
RIGHT_EXIT_M = reference_x_to_metres(LAYOUT["right_exit_x"])


# --- people ---------------------------------------------------------------

# Detector box geometry per posture, in metres: (width, box height measured
# down from the top of the head, head drop from standing, detection chance).
# The production detector is upper-body + pose-envelope, so a standing
# speaker's box is roughly head-to-thigh, as the recordings show.
POSTURES: dict[str, tuple[float, float, float, float]] = {
    "standing": (0.60, 1.20, 0.00, 0.99),
    "at_lectern": (0.60, 0.85, 0.00, 0.97),
    "hand_mic_gesture": (0.80, 1.20, 0.00, 0.98),
    "arms_raised": (0.95, 1.55, -0.40, 0.95),
    "turned_away": (0.50, 1.15, 0.00, 0.65),
    "bending": (0.65, 0.80, 0.55, 0.50),
    "seated": (0.50, 0.70, 0.55, 0.80),
}
HEAD_HEIGHT_M = 0.24
HEAD_WIDTH_M = 0.20
PERSON_HEIGHT_M = 1.70


@dataclass(frozen=True, slots=True)
class Segment:
    start: float
    end: float
    x0: float
    x1: float
    y0: float
    y1: float
    posture: str
    ramp: float


@dataclass(slots=True)
class Person:
    """One person with a scripted path, in metres and stage feet positions."""

    name: str
    x: float
    feet_y: float = LAYOUT["deck_feet_y"]
    posture: str = "standing"
    detector_profile: str = "youth0920_a"
    visible_from: float = 0.0
    visible_until: float = math.inf
    height_m: float = PERSON_HEIGHT_M
    # Rendered mode: which cut-out person from the streams plays this role.
    actor: str = ""
    segments: list[Segment] = field(default_factory=list)
    _cursor: float = 0.0

    # Script builders ---------------------------------------------------
    def hold(self, seconds: float, posture: str | None = None) -> Person:
        if posture:
            self.posture = posture
        self._add(self.x, self.feet_y, seconds, self.posture, 0.0)
        return self

    def walk_to(
        self,
        x: float,
        speed: float,
        *,
        feet_y: float | None = None,
        posture: str = "standing",
        ramp: float | None = None,
    ) -> Person:
        distance = abs(x - self.x)
        ramp = MEASURED["walking_speeds_m_per_s"]["time_to_full_speed_s"] if ramp is None else ramp
        # Trapezoidal profile: accelerate for `ramp`, cruise, decelerate.
        cruise = max(0.0, distance / max(1e-6, speed) - ramp)
        duration = max(0.05, cruise + 2.0 * ramp) if distance > 0 else 0.05
        self.posture = posture
        target_y = self.feet_y if feet_y is None else feet_y
        self._add(x, target_y, duration, posture, ramp if distance > 0 else 0.0)
        return self

    def leave_at(self, seconds: float) -> Person:
        self.visible_until = seconds
        return self

    def appear_at(self, seconds: float) -> Person:
        self.visible_from = seconds
        return self

    def _add(self, x: float, feet_y: float, seconds: float, posture: str, ramp: float) -> None:
        start = self._cursor
        end = start + seconds
        previous_x = self.segments[-1].x1 if self.segments else self.x
        previous_y = self.segments[-1].y1 if self.segments else self.feet_y
        self.segments.append(Segment(start, end, previous_x, x, previous_y, feet_y, posture, ramp))
        self._cursor = end
        self.x = x
        self.feet_y = feet_y

    @property
    def script_end(self) -> float:
        return self._cursor

    # Queries -----------------------------------------------------------
    def state(self, t: float) -> tuple[float, float, str, float]:
        """(x metres, feet_y reference, posture, speed m/s) at time t."""

        if not self.segments:
            return self.x, self.feet_y, self.posture, 0.0
        for segment in self.segments:
            if t <= segment.end:
                break
        else:
            last = self.segments[-1]
            return last.x1, last.y1, last.posture, 0.0
        if t < segment.start:
            return segment.x0, segment.y0, segment.posture, 0.0
        duration = max(1e-9, segment.end - segment.start)
        ramp = min(0.5, segment.ramp / duration)
        progress, speed_fraction = _trapezoid((t - segment.start) / duration, ramp)
        x = segment.x0 + (segment.x1 - segment.x0) * progress
        y = segment.y0 + (segment.y1 - segment.y0) * progress
        distance = math.hypot(segment.x1 - segment.x0, (segment.y1 - segment.y0) / SCALE_Y * SCALE_X)
        cruise_speed = distance / duration / (1.0 - ramp)
        speed = cruise_speed * speed_fraction
        return x, y, segment.posture, speed

    def present(self, t: float) -> bool:
        return self.visible_from <= t < self.visible_until

    def body_rect(self, t: float) -> tuple[float, float, float, float]:
        """Detector-shaped box in the Audience reference frame."""

        x, feet_y, posture, _speed = self.state(t)
        width, height, drop, _chance = POSTURES[posture]
        head_top = feet_y - (self.height_m - drop) * SCALE_Y
        if posture == "arms_raised":
            head_top = feet_y - self.height_m * SCALE_Y
            top = head_top + drop * SCALE_Y
        else:
            top = head_top
        centre = metres_to_reference_x(x)
        return (centre - width * SCALE_X / 2.0, top, width * SCALE_X, height * SCALE_Y)

    def head_rect(self, t: float) -> tuple[float, float, float, float]:
        x, feet_y, posture, _speed = self.state(t)
        drop = POSTURES[posture][2] if posture != "arms_raised" else 0.0
        top = feet_y - (self.height_m - drop) * SCALE_Y
        centre = metres_to_reference_x(x)
        return (centre - HEAD_WIDTH_M * SCALE_X / 2.0, top, HEAD_WIDTH_M * SCALE_X, HEAD_HEIGHT_M * SCALE_Y)


def _trapezoid(u: float, ramp: float) -> tuple[float, float]:
    """Normalized position and speed fraction for a trapezoidal velocity profile."""

    u = max(0.0, min(1.0, u))
    ramp = max(0.0, min(0.5, ramp))
    if ramp <= 0:
        return u, 1.0
    peak = 1.0 / (1.0 - ramp)
    if u < ramp:
        return 0.5 * peak * u * u / ramp, u / ramp
    if u > 1.0 - ramp:
        remaining = 1.0 - u
        return 1.0 - 0.5 * peak * remaining * remaining / ramp, remaining / ramp
    return 0.5 * peak * ramp + peak * (u - ramp), 1.0


def congregation(seed: int = 7) -> list[Person]:
    """Seated people in the front rows; only the wide camera sees them."""

    rng = random.Random(seed)
    people = []
    for row, feet_y in enumerate((0.36, 0.42, 0.50, 0.60)):
        for column in range(4):
            x_ref = rng.uniform(0.08, 0.92)
            person = Person(
                f"pew-{row}-{column}",
                x=reference_x_to_metres(x_ref),
                feet_y=feet_y + 0.10,
                posture="seated",
            )
            people.append(person)
    return people


# --- camera ---------------------------------------------------------------


def _log_interp(zoom: float, points: Sequence[Sequence[float]]) -> float:
    points = sorted(points)
    if zoom <= points[0][0]:
        (z0, f0), (z1, f1) = points[0], points[1]
    elif zoom >= points[-1][0]:
        (z0, f0), (z1, f1) = points[-2], points[-1]
    else:
        for (z0, f0), (z1, f1) in zip(points, points[1:]):
            if z0 <= zoom <= z1:
                break
    alpha = (zoom - z0) / (z1 - z0)
    return math.exp(math.log(f0) + alpha * (math.log(f1) - math.log(f0)))


# --- motor: measured on the real AW-HE120 -------------------------------------
#
# fixtures/icas_motor_measured.json holds position samples recorded while the
# camera ran each speed (tools/measure_motor_speed.py). The camera's "speed
# with zoom position" feature slows pan/tilt when zoomed in, so the curve is
# measured at two zooms and interpolated between (log-linear) and beyond them.
MOTOR_MEASURED = load_fixture("icas_motor_measured.json")


def _motor_tables() -> dict[str, dict[int, list[tuple[int, float]]]]:
    tables: dict[str, dict[int, dict[int, list[float]]]] = {}
    for run in MOTOR_MEASURED["runs"]:
        rate = abs(run["rate_units_per_second"])
        tables.setdefault(run["axis"], {}).setdefault(run["zoom"], {}).setdefault(run["offset"], []).append(rate)
    out: dict[str, dict[int, list[tuple[int, float]]]] = {}
    for axis, zooms in tables.items():
        out[axis] = {}
        for zoom, offsets in zooms.items():
            points, highest = [(0, 0.0)], 0.0
            for offset in sorted(offsets):
                # A pulse can end early against a limit; speed never falls
                # as the command rises.
                highest = max(highest, sum(offsets[offset]) / len(offsets[offset]))
                points.append((offset, highest))
            out[axis][zoom] = points
    return out


MOTOR_TABLES = _motor_tables()


def _curve(points: list[tuple[int, float]], offset: float) -> float:
    offset = min(abs(offset), points[-1][0])
    for (o0, r0), (o1, r1) in zip(points, points[1:]):
        if offset <= o1:
            if r0 <= 0 or r1 <= 0:
                return r0 + (r1 - r0) * (offset - o0) / max(1e-9, o1 - o0)
            return math.exp(math.log(r0) + (math.log(r1) - math.log(r0)) * (offset - o0) / (o1 - o0))
    return points[-1][1]


def axis_rate(axis: str, offset: float, zoom: float, speed_scale: float = 1.0) -> float:
    """Motor units/s for an AW speed offset from neutral at this zoom.

    Signed like the Panasonic convention: for pan and tilt a speed above 50
    decreases the absolute coordinate; zoom above 50 increases it.
    """

    if offset == 0:
        return 0.0
    curves = MOTOR_TABLES[axis]
    zooms = sorted(curves)
    # Piecewise log-linear between measured zooms, flat beyond them: the
    # camera's speed-with-zoom slowdown has levelled off by 3500 (3600 measured
    # within 1% of 3500), so extrapolating past the last measurement is wrong.
    zoom = max(zooms[0], min(zooms[-1], zoom))
    magnitude = _curve(curves[zooms[-1]], offset)
    for low, high in zip(zooms, zooms[1:]):
        if low <= zoom <= high:
            a, b = _curve(curves[low], offset), _curve(curves[high], offset)
            fraction = (zoom - low) / (high - low)
            magnitude = math.exp(math.log(max(a, 1e-6)) + (math.log(max(b, 1e-6)) - math.log(max(a, 1e-6))) * fraction)
            break
    magnitude *= speed_scale
    sign = -1.0 if axis in ("pan", "tilt") else 1.0
    return math.copysign(magnitude, offset) * sign


def speed_offset_for_rate(axis: str, rate: float, zoom: float, speed_scale: float, limit: int) -> int:
    """AW offset (|offset| <= limit) whose rate is nearest |rate|, signed to produce `rate`."""

    wanted = abs(rate)
    best = min(range(0, limit + 1), key=lambda offset: abs(abs(axis_rate(axis, offset, zoom, speed_scale)) - wanted))
    produced = axis_rate(axis, best, zoom, speed_scale)
    return best if produced * rate >= 0 else -best


def zoom_rate_for_offset(offset: float, speed_scale: float) -> float:
    return axis_rate("zoom", offset, 2238, speed_scale)


def true_footprint(zoom: float) -> float:
    return _log_interp(zoom, MEASURED["true_ptz_footprint"]["zoom_points"])


def zoom_for_footprint(footprint: float) -> float:
    low, high = 0x555, 0xFFF
    for _ in range(30):
        middle = (low + high) / 2.0
        if true_footprint(middle) > footprint:
            low = middle
        else:
            high = middle
    return low


@dataclass(slots=True)
class _Pose:
    pan: float
    tilt: float
    zoom: float


class SimulatedPanasonic:
    """Latched speed commands, applied after the measured command latency."""

    def __init__(self, pan: int, tilt: int, zoom: int, clock: Callable[[], float], *, speed_scale: float) -> None:
        self.config = SimpleNamespace(enabled=True)
        self._pose = _Pose(float(pan), float(tilt), float(zoom))
        self._clock = clock
        self._speed_scale = speed_scale
        self._active = (50, 50, 50)
        self._pending: list[tuple[float, int | None, int | None, int | None]] = []
        self.commands: list[tuple[float, int, int, int]] = []

    # Physics ---------------------------------------------------------
    def advance(self, now: float, seconds: float) -> None:
        while self._pending and self._pending[0][0] <= now:
            _at, pan, tilt, zoom = self._pending.pop(0)
            current = list(self._active)
            if pan is not None:
                current[0] = pan
            if tilt is not None:
                current[1] = tilt
            if zoom is not None:
                current[2] = zoom
            self._active = (current[0], current[1], current[2])
        pan_speed, tilt_speed, zoom_speed = self._active
        zoom_now = self._pose.zoom
        pan_rate = axis_rate("pan", pan_speed - 50, zoom_now, self._speed_scale)
        tilt_rate = axis_rate("tilt", tilt_speed - 50, zoom_now, self._speed_scale)
        zoom_rate = zoom_rate_for_offset(zoom_speed - 50, self._speed_scale)
        pose = self._pose
        pose.pan = max(0.0, min(65535.0, pose.pan + pan_rate * seconds))
        pose.tilt = max(0.0, min(65535.0, pose.tilt + tilt_rate * seconds))
        pose.zoom = max(0x555, min(0xFFF, pose.zoom + zoom_rate * seconds))

    @property
    def speeds(self) -> tuple[int, int, int]:
        return self._active

    def pose_tuple(self) -> tuple[int, int, int]:
        return round(self._pose.pan), round(self._pose.tilt), round(self._pose.zoom)

    def _queue(self, pan: int | None, tilt: int | None, zoom: int | None) -> None:
        now = self._clock()
        self._pending.append((now + LATENCY["camera_command"], pan, tilt, zoom))
        self._pending.sort(key=lambda item: item[0])
        self.commands.append((now, -1 if pan is None else pan, -1 if tilt is None else tilt, -1 if zoom is None else zoom))

    # Production-facing API -----------------------------------------------
    async def query_pan_tilt_position(self) -> tuple[int, int]:
        pan, tilt, _zoom = self.pose_tuple()
        return pan, tilt

    async def query_zoom_position(self) -> int:
        return self.pose_tuple()[2]

    async def pan_tilt(self, pan: int, tilt: int) -> bool:
        self._queue(int(pan), int(tilt), None)
        return True

    async def zoom(self, zoom: int) -> bool:
        self._queue(None, None, int(zoom))
        return True

    async def stop_pan_tilt(self) -> bool:
        self._queue(50, 50, None)
        return True

    async def stop_zoom(self) -> bool:
        self._queue(None, None, 50)
        return True

    async def absolute_pan_tilt(self, pan: int, tilt: int) -> bool:
        self._pose.pan, self._pose.tilt = float(pan), float(tilt)
        return True

    async def absolute_zoom(self, zoom: int) -> bool:
        self._pose.zoom = float(zoom)
        return True


# --- detector noise ----------------------------------------------------------

# Measured on real detector output (see fixtures/tracks). `short_box` is how
# often the upper-body/pose merge returns a box ~40% shorter than usual.
DETECTOR_PROFILES = {
    "youth0920_a": {"centre_sd": 0.006, "top_sd": 0.02, "short_box": 0.07, "confidence": 0.62},
    "youth0920_b": {"centre_sd": 0.003, "top_sd": 0.002, "short_box": 0.0, "confidence": 0.65},
    "sermon0906": {"centre_sd": 0.012, "top_sd": 0.01, "short_box": 0.05, "confidence": 0.60},
}


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class _ClockModule:
    """Replaces `time` inside production modules; only `monotonic` is virtual."""

    def __init__(self, clock: _Clock) -> None:
        import time as real_time

        self._clock = clock
        self._real = real_time

    def monotonic(self) -> float:
        return self._clock.now

    def __getattr__(self, name: str):
        return getattr(self._real, name)


@contextmanager
def virtual_clock():
    """Run production tracking code on a clock the test advances by hand."""

    import importlib
    import sys
    import time as real_time

    # Load every module the loop touches before swapping in the clock.
    for name in (
        "production_hub.tracking.association",
        "production_hub.tracking.ptz_automation",
        "production_hub.tracking.service",
        "production_hub.tracking.target_lock",
        "production_hub.video.models",
    ):
        importlib.import_module(name)
    clock = _Clock()
    # Both `import time` and `from time import monotonic` appear in
    # production modules; replace every reference with the virtual clock.
    patched: list[tuple[object, str, object]] = []
    for name, module in list(sys.modules.items()):
        if not name.startswith("production_hub") or module is None:
            continue
        if getattr(module, "time", None) is real_time:
            patched.append((module, "time", real_time))
            module.time = _ClockModule(clock)
        if getattr(module, "monotonic", None) is real_time.monotonic:
            patched.append((module, "monotonic", real_time.monotonic))
            module.monotonic = clock.monotonic
    try:
        yield clock
    finally:
        for module, attribute, original in patched:
            setattr(module, attribute, original)


# --- the closed loop ---------------------------------------------------------


@dataclass(slots=True)
class Sample:
    t: float
    pan: int
    tilt: int
    zoom: int
    view: tuple[float, float, float, float]
    speeds: tuple[int, int, int]
    state: str
    source: str
    target_bounds: tuple[float, float, float, float] | None
    ptz_boxes: int


@dataclass(slots=True)
class RunResult:
    armed: bool
    arm_message: str
    samples: list[Sample]
    people: list[Person]
    targets: list[str]
    disarm_reason: str = ""
    errors: list[str] = field(default_factory=list)


def _intersection_fraction(rect, view) -> float:
    x, y, w, h = rect
    vx, vy, vw, vh = view
    left, right = max(x, vx), min(x + w, vx + vw)
    top, bottom = max(y, vy), min(y + h, vy + vh)
    if right <= left or bottom <= top:
        return 0.0
    return (right - left) * (bottom - top) / max(1e-12, w * h)


class StageWorld:
    """Runs one scenario through the production tracking stack."""

    PHYSICS_STEP = 0.02
    SAMPLE_STEP = 0.05

    def __init__(
        self,
        people: Sequence[Person],
        targets: Sequence[str],
        *,
        initial_zoom: int | None,
        duration: float,
        speed_scale: float = 1.0,
        seed: int = 1,
        include_congregation: bool = True,
        rendered: bool = False,
        record: Path | None = None,
        automation_overrides: dict | None = None,
        tracking_overrides: dict | None = None,
    ) -> None:
        self.people = list(people)
        self.distractors = congregation(seed) if include_congregation else []
        self.targets = list(targets)
        self.initial_zoom = initial_zoom
        self.duration = duration
        self.speed_scale = speed_scale
        self.rng = random.Random(seed)
        self.automation_overrides = automation_overrides or {}
        self.tracking_overrides = tracking_overrides or {}
        # Rendered mode: real stage imagery + real people from the streams,
        # judged by the production Apple Vision detector.
        self.rendered = rendered
        # Optional MP4 of the PTZ program output (rendered mode only).
        self.record = record if rendered else None
        if rendered:
            # The rendered wide view has no congregation cut-outs yet.
            self.distractors = []

    # Production objects ------------------------------------------------
    def _build(self):
        from production_hub.core.config.models import CameraTrackingConfig
        from production_hub.tracking.ptz_geometry import (
            CalibrationPoseAnchor,
            PtzGeometryModel,
            PtzMotorPose,
        )

        anchors = []
        for item in STAGE["calibration"]["anchors"]:
            anchors.append(
                CalibrationPoseAnchor(
                    name=item["name"],
                    center_x=item["center_x"],
                    center_y=item["center_y"],
                    footprint_width=item["footprint_width"],
                    footprint_height=item["footprint_height"],
                    motor=PtzMotorPose(**item["motor"]),
                    ptz_size=tuple(item["ptz_size"]),
                    audience_size=tuple(item["audience_size"]),
                    ptz_to_audience=tuple(tuple(row) for row in item["ptz_to_audience"]),
                )
            )
        geometry = PtzGeometryModel(
            Path("/stage-realism/icas_stage.json"),
            STAGE["calibration"]["created_at"],
            anchors,
        )
        payload = dict(STAGE["tracking_config"])
        payload["enabled"] = True
        payload.update(self.tracking_overrides)
        automation = dict(STAGE["automation_config"])
        automation["mode"] = "subject"
        automation.update(self.automation_overrides)
        payload["automation"] = automation
        config = CameraTrackingConfig.from_dict(payload)
        return geometry, config, PtzMotorPose

    def _view(self, geometry, pose_tuple) -> tuple[float, float, float, float]:
        from production_hub.tracking.ptz_geometry import PtzMotorPose

        pan, tilt, zoom = pose_tuple
        corners = geometry.reference_polygon_for_pose(PtzMotorPose(pan, tilt, zoom))
        cx = sum(x for x, _ in corners) / len(corners)
        cy = sum(y for _, y in corners) / len(corners)
        footprint = true_footprint(zoom)
        return (cx - footprint / 2.0, cy - footprint / 2.0, footprint, footprint)

    def _operator_pose(self, geometry, PtzMotorPose, t: float) -> tuple[int, int, int]:
        """The shot an operator sets by hand before pressing the arm button."""

        from production_hub.tracking.models import NormalizedRect

        chosen = [person for person in self.people if person.name in self.targets]
        rects = [person.body_rect(t) for person in chosen]
        left = min(r[0] for r in rects)
        top = min(r[1] for r in rects)
        right = max(r[0] + r[2] for r in rects)
        bottom = max(r[1] + r[3] for r in rects)
        # Solve pan/tilt for the true optical centre by bisection against the
        # calibrated centre mapping; zoom is the operator's sermon zoom.
        target_cx = (left + right) / 2.0
        target_cy = top + (bottom - top) * 0.45
        zoom = self.initial_zoom
        if zoom is None:
            # A group shot: the whole group spans ~70% of the frame width.
            needed = max((right - left) / 0.70, (bottom - top) / 0.85)
            zoom = round(zoom_for_footprint(needed))
        seed = geometry.pose_for_target(
            NormalizedRect(left, top, right - left, bottom - top), desired_frame_height=0.8
        )
        pan, tilt = seed.pan, seed.tilt
        for _ in range(40):
            view = self._view(geometry, (pan, tilt, zoom))
            cx, cy = view[0] + view[2] / 2.0, view[1] + view[3] / 2.0
            pan += round((cx - target_cx) * 11000)
            tilt += round((target_cy - cy) * 9000)
        return pan, tilt, zoom

    # Detection ----------------------------------------------------------
    def _ptz_candidates(self, view, t: float):
        from production_hub.tracking.models import NormalizedRect, PersonCandidate

        vx, vy, vw, vh = view
        candidates = []
        for person in self.people:
            if not person.present(t):
                continue
            rect = person.body_rect(t)
            if _intersection_fraction(rect, view) < 0.45:
                continue
            _x, _y, posture, _speed = person.state(t)
            profile = DETECTOR_PROFILES[person.detector_profile]
            if self.rng.random() > POSTURES[posture][3]:
                continue
            x, y, w, h = rect
            image = [(x - vx) / vw, (y - vy) / vh, w / vw, h / vh]
            image[0] += self.rng.gauss(0.0, profile["centre_sd"]) * image[2]
            image[1] += self.rng.gauss(0.0, profile["top_sd"]) * image[3]
            if self.rng.random() < profile["short_box"]:
                image[1] += 0.25 * image[3]
                image[3] *= 0.62
            left, top = max(0.0, image[0]), max(0.0, image[1])
            right, bottom = min(1.0, image[0] + image[2]), min(1.0, image[1] + image[3])
            if right - left < 0.02 or bottom - top < 0.02:
                continue
            candidates.append(
                PersonCandidate(NormalizedRect(left, top, right - left, bottom - top), profile["confidence"])
            )
        return candidates

    def _audience_candidates(self, t: float):
        from production_hub.tracking.models import NormalizedRect, PersonCandidate

        candidates = []
        for person in [*self.people, *self.distractors]:
            if not person.present(t):
                continue
            _x, _y, posture, speed = person.state(t)
            # Small, distant people blur when they walk; Vision misses more.
            chance = POSTURES[posture][3] * (0.9 if speed < 0.5 else 0.75)
            if self.rng.random() > chance:
                continue
            x, y, w, h = person.body_rect(t)
            x += self.rng.gauss(0.0, 0.004)
            y += self.rng.gauss(0.0, 0.004)
            candidates.append(PersonCandidate(NormalizedRect(x, y, w, h).clamped(), 0.5))
        return candidates

    # Main loop ---------------------------------------------------------------
    def run(self) -> RunResult:
        from production_hub.calibration.relocalization import (
            RelocalizationSnapshot,
            RelocalizationState,
        )
        from production_hub.tracking.ptz_automation import PtzAutomationService
        from production_hub.tracking.service import PersonTrackingService
        from production_hub.video.models import (
            VideoSourceKey,
            VideoSourceSnapshot,
            VideoSourceState,
        )
        from PySide6.QtGui import QImage

        with virtual_clock() as clock:
            return self._run(
                clock,
                PtzAutomationService,
                PersonTrackingService,
                RelocalizationSnapshot,
                RelocalizationState,
                VideoSourceKey,
                VideoSourceSnapshot,
                VideoSourceState,
                QImage,
            )

    def _run(
        self,
        clock,
        PtzAutomationService,
        PersonTrackingService,
        RelocalizationSnapshot,
        RelocalizationState,
        VideoSourceKey,
        VideoSourceSnapshot,
        VideoSourceState,
        QImage,
    ) -> RunResult:
        geometry, config, PtzMotorPose = self._build()
        start = clock.now
        pan, tilt, zoom = self._operator_pose(geometry, PtzMotorPose, 0.0)
        camera = SimulatedPanasonic(pan, tilt, zoom, clock.monotonic, speed_scale=self.speed_scale)
        history: list[tuple[float, tuple[int, int, int]]] = [(clock.now, camera.pose_tuple())]

        pending_candidates: list = []

        class _Detector:
            backend_name = "stage-realism replay"
            supports_region_cropping = False

            def detect(self, _image):
                return list(pending_candidates)

        detector = _Detector()
        tracking = PersonTrackingService(None, config, detector_factory=lambda _config: detector)
        dummy = QImage(16, 9, QImage.Format.Format_RGB888)

        class _Reloc:
            def snapshot(self_inner):
                return RelocalizationSnapshot(
                    state=RelocalizationState.LOCKED,
                    approval_status="approved",
                    reference_size=(1920, 1080),
                    live_size=(1920, 1080),
                    last_analysis_monotonic=clock.now,
                    message="Locked",
                )

        class _Video:
            def __init__(self_inner) -> None:
                self_inner.tracking = tracking
                self_inner.relocalization = _Reloc()

            def snapshot(self_inner, source):
                return VideoSourceSnapshot(
                    source,
                    VideoSourceState.RUNNING,
                    last_frame_monotonic=clock.now,
                    effective_fps=29.97,
                )

            def set_tracking_activity(self_inner, *_args, **_kwargs) -> None:
                return None

            def set_calibration_activity(self_inner, *_args, **_kwargs) -> None:
                return None

        service = PtzAutomationService(
            _Video(),
            camera,
            config,
            Path("/tmp/stage-realism"),
            geometry_loader=lambda _root: geometry,
        )
        from .target import apply_app_settings

        apply_app_settings(tracking, service)

        def pose_at(when: float) -> tuple[int, int, int]:
            for stamp, pose in reversed(history):
                if stamp <= when:
                    return pose
            return history[0][1]

        sequence = {VideoSourceKey.PTZ: 0, VideoSourceKey.AUDIENCE: 0}

        real_detector = None
        if self.rendered:
            import numpy as np

            from . import render
            from production_hub.tracking import service as service_module

            real_detector = service_module._default_detector_factory(config)
            noise = np.random.default_rng(self.rng.randrange(1 << 30))

        def analyze(source) -> None:
            nonlocal pending_candidates
            captured = clock.now - LATENCY["video_to_detector"]
            t_world = captured - start
            if self.rendered:
                if source == VideoSourceKey.PTZ:
                    view = self._view(geometry, pose_at(captured))
                    earlier = self._view(geometry, pose_at(captured - 0.05))
                    velocity = ((view[0] + view[2] / 2) - (earlier[0] + earlier[2] / 2)) / 0.05 / view[2]
                    frame = render.render_ptz(view, self.people, t_world, camera_velocity=velocity, rng=noise)
                else:
                    frame = render.render_audience(self.people, t_world)
                image, used = render.to_qimage(frame), real_detector
            else:
                if source == VideoSourceKey.PTZ:
                    view = self._view(geometry, pose_at(captured))
                    pending_candidates = self._ptz_candidates(view, t_world)
                else:
                    pending_candidates = self._audience_candidates(t_world)
                image, used = dummy, detector
            sequence[source] += 1
            saved = clock.now
            clock.now = saved + LATENCY["inference"]
            try:
                tracking._analyze(source, sequence[source], image, used)
            finally:
                clock.now = saved

        # Warm the detector for a second, as the live app has been running
        # before the operator presses the arm button.
        analysis_interval = 1.0 / config.analysis_fps
        # A build may pace its own decisions (Dev ties them to the analysis
        # rate); older builds use the configured rate.
        rate = getattr(service, "decision_rate", None)
        decision_interval = 1.0 / (rate() if callable(rate) else config.automation.decision_fps)
        for _ in range(int(1.2 / analysis_interval)):
            clock.now += analysis_interval / 2.0
            analyze(VideoSourceKey.PTZ)
            clock.now += analysis_interval / 2.0
            analyze(VideoSourceKey.AUDIENCE)
            history.append((clock.now, camera.pose_tuple()))
        start = clock.now
        armed, message = service.arm()
        result = RunResult(armed, message, [], self.people, self.targets)
        if not armed:
            return result

        next_ptz = clock.now + 0.01
        next_audience = clock.now + analysis_interval / 2.0
        next_decision = clock.now + 0.13
        next_sample = clock.now
        recorder = _Recorder(self.record) if self.record else None
        next_frame = clock.now
        end = start + self.duration
        while clock.now < end:
            clock.now += self.PHYSICS_STEP
            camera.advance(clock.now, self.PHYSICS_STEP)
            history.append((clock.now, camera.pose_tuple()))
            if len(history) > 400:
                del history[:100]
            if clock.now >= next_ptz:
                analyze(VideoSourceKey.PTZ)
                next_ptz += analysis_interval
            if clock.now >= next_audience:
                analyze(VideoSourceKey.AUDIENCE)
                next_audience += analysis_interval
            if clock.now >= next_decision:
                try:
                    service._cycle(clock.now)
                except Exception as exc:  # recorded, as the live worker would log it
                    result.errors.append(f"{clock.now - start:.2f}s {type(exc).__name__}: {exc}")
                next_decision += decision_interval
                if not service.armed:
                    snap = service.snapshot()
                    result.disarm_reason = snap.message
            if recorder is not None and clock.now >= next_frame:
                next_frame += 0.1
                recorder.write(
                    self._view(geometry, camera.pose_tuple()),
                    self.people,
                    clock.now - start,
                    tracking.snapshot(VideoSourceKey.PTZ),
                    service.snapshot(),
                    self.targets,
                )
            if clock.now >= next_sample:
                next_sample += self.SAMPLE_STEP
                snap = service.snapshot()
                decision = snap.decision
                bounds = None
                if decision is not None and decision.target_bounds is not None:
                    b = decision.target_bounds
                    bounds = (b.x, b.y, b.width, b.height)
                pose = camera.pose_tuple()
                result.samples.append(
                    Sample(
                        t=round(clock.now - start, 3),
                        pan=pose[0],
                        tilt=pose[1],
                        zoom=pose[2],
                        view=self._view(geometry, pose),
                        speeds=camera.speeds,
                        state=str(getattr(snap.state, "value", snap.state)),
                        source=getattr(snap, "tracking_source", ""),
                        target_bounds=bounds,
                        ptz_boxes=len(tracking.snapshot(VideoSourceKey.PTZ).subjects),
                    )
                )
            if not service.armed and result.disarm_reason:
                break
        if recorder is not None:
            recorder.close()
        return result


class _Recorder:
    """Writes the simulated PTZ program feed with the tracker's view on top."""

    def __init__(self, path: Path) -> None:
        import cv2

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._raw = self.path.with_suffix(".raw.mp4")
        self._writer = cv2.VideoWriter(str(self._raw), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (960, 540))

    def write(self, view, people, t, ptz_snapshot, automation, targets) -> None:
        import cv2

        from . import render

        frame = render.render_ptz(view, people, t)
        for subject in ptz_snapshot.subjects:
            b = subject.bounds
            cv2.rectangle(frame, (int(b.x * 960), int(b.y * 540)), (int((b.x + b.width) * 960), int((b.y + b.height) * 540)),
                          (0, 220, 0), 2)
        # Where the true targets' heads are, so a viewer can see who should be framed.
        for person in people:
            if person.name in targets and person.present(t):
                x, y, w, h = person.head_rect(t)
                cx = int((x + w / 2 - view[0]) / view[2] * 960)
                cy = int((y - view[1]) / view[3] * 540)
                cv2.drawMarker(frame, (cx, max(8, min(532, cy))), (0, 200, 255), cv2.MARKER_TRIANGLE_DOWN, 18, 2)
        source = getattr(automation, "tracking_source", "")
        label = f"{t:5.1f}s  {getattr(automation.state, 'value', automation.state)}  {source}"
        cv2.rectangle(frame, (0, 0), (960, 30), (0, 0, 0), -1)
        cv2.putText(frame, label, (8, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        self._writer.write(frame)

    def close(self) -> None:
        import subprocess

        self._writer.release()
        # H.264 so QuickTime and browsers play it.
        done = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", str(self._raw), "-c:v", "libx264", "-pix_fmt", "yuv420p",
             str(self.path)],
            check=False,
        )
        if done.returncode == 0:
            self._raw.unlink(missing_ok=True)
        else:
            self._raw.rename(self.path)


# --- feasibility oracle --------------------------------------------------------


def oracle_run(world: StageWorld, *, pan_limit: int, tilt_limit: int, zoom_limit: int) -> RunResult:
    """A best-case camera: perfect knowledge, no sensing delay, same motors.

    It still pays the command latency and the motor curve, and it may not
    exceed the given speed caps. For a group it also zooms out (never tighter
    than the operator's starting shot) to keep everyone in frame. If this camera cannot keep a head in frame,
    the scenario is impossible at those caps and the failure belongs to the
    speed limits, not to the tracking logic.
    """

    geometry, _config, PtzMotorPose = world._build()
    clock = _Clock()
    pan, tilt, zoom = world._operator_pose(geometry, PtzMotorPose, 0.0)
    camera = SimulatedPanasonic(pan, tilt, zoom, clock.monotonic, speed_scale=world.speed_scale)
    targets = [p for p in world.people if p.name in world.targets]
    result = RunResult(True, "oracle", [], world.people, world.targets)
    start = clock.now

    def extent(t: float) -> tuple[float, float, float, float]:
        rects = [p.body_rect(t) for p in targets if p.present(t)] or [p.body_rect(t) for p in targets]
        return (
            min(r[0] for r in rects),
            min(r[1] for r in rects),
            max(r[0] + r[2] for r in rects),
            max(r[1] + r[3] for r in rects),
        )

    def aim(t: float) -> tuple[float, float]:
        left, top, right, bottom = extent(t)
        return (left + right) / 2.0, top + (bottom - top) * 0.45

    group = len(targets) > 1

    next_command = clock.now
    next_sample = clock.now
    last_offsets = (0, 0)
    last_zoom = 0
    world.initial_zoom_used = zoom
    while clock.now < start + world.duration:
        clock.now += StageWorld.PHYSICS_STEP
        camera.advance(clock.now, StageWorld.PHYSICS_STEP)
        t = clock.now - start
        if clock.now >= next_command:
            next_command += 0.1
            pose = camera.pose_tuple()
            view = world._view(geometry, pose)
            pan_probe = world._view(geometry, (pose[0] + 200, pose[1], pose[2]))
            tilt_probe = world._view(geometry, (pose[0], pose[1] + 200, pose[2]))
            per_pan = (pan_probe[0] - view[0]) / 200.0
            per_tilt = (tilt_probe[1] - view[1]) / 200.0
            cx, cy = view[0] + view[2] / 2.0, view[1] + view[3] / 2.0
            # Deadbeat: predict where the in-flight command leaves the view
            # when this one lands, then be on the subject one period later.
            latency = LATENCY["camera_command"]
            period = 0.1
            # The previous command is still in flight and lands almost at once;
            # predict from it rather than from the speed active right now.
            pan_now = axis_rate("pan", last_offsets[0], pose[2], world.speed_scale)
            tilt_now = axis_rate("tilt", last_offsets[1], pose[2], world.speed_scale)
            predicted_x = cx + pan_now * per_pan * latency
            predicted_y = cy + tilt_now * per_tilt * latency
            ahead_x, ahead_y = aim(t + latency + period)
            rate_x = (ahead_x - predicted_x) / period
            rate_y = (ahead_y - predicted_y) / period
            pan_rate = rate_x / per_pan if per_pan else 0.0
            tilt_rate = rate_y / per_tilt if per_tilt else 0.0
            pan_offset = 0
            if abs(ahead_x - predicted_x) > 0.01 * view[2]:
                pan_offset = speed_offset_for_rate("pan", pan_rate, pose[2], world.speed_scale, pan_limit)
            tilt_offset = 0
            if abs(ahead_y - predicted_y) > 0.01 * view[3]:
                tilt_offset = speed_offset_for_rate("tilt", tilt_rate, pose[2], world.speed_scale, tilt_limit)
            last_offsets = (pan_offset, tilt_offset)
            asyncio.run(camera.pan_tilt(50 + pan_offset, 50 + tilt_offset))
            if group:
                # Keep the whole group inside ~75% of the frame width.
                left, top, right, bottom = extent(t + latency + period)
                wanted = zoom_for_footprint(max((right - left) / 0.75, (bottom - top) / 0.85, 1e-3))
                wanted = min(wanted, world.initial_zoom_used)
                predicted_zoom = pose[2] + zoom_rate_for_offset(last_zoom, world.speed_scale) * latency
                zoom_rate = (wanted - predicted_zoom) / period
                best = min(
                    range(-zoom_limit, zoom_limit + 1),
                    key=lambda offset: abs(zoom_rate_for_offset(offset, world.speed_scale) - zoom_rate),
                )
                if abs(wanted - predicted_zoom) < 15:
                    best = 0
                last_zoom = best
                asyncio.run(camera.zoom(50 + best))
        if clock.now >= next_sample:
            next_sample += StageWorld.SAMPLE_STEP
            pose = camera.pose_tuple()
            result.samples.append(
                Sample(round(t, 3), *pose, world._view(geometry, pose), camera.speeds, "oracle", "oracle", None, 0)
            )
    return result
