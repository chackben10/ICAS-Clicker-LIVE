"""Replay real stream moments, frame by frame, through the tracker.

The recorded PTZ frames go through the build's own detector, association, and
identity lock at the live analysis rate. The camera in a recording cannot
respond, so this is open loop: it judges what the tracker *sees and decides*,
not how it moves. Camera motion in the recording (the live tracker's or the
operator's) is measured from the background and fed back as the motor pose.

Checks, per the operator's rules:

never_switches   The lock never lands on anyone but the armed subject (the
                 next speaker, a front-row head, anyone).
keeps_lock       While the subject is clearly in the shot, the lock never loses
                 them for more than 1 s at a time.
reacts           When the subject heads for the frame edge, the tracker asks
                 to pan toward them within 0.5 s.

Limitations: the wide Audience camera is not in the stream, so the wide-camera
backup sees an empty frame, and the recording's own framing (operator
takeovers included) decides what is in view.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .world import LATENCY, STAGE, StageWorld, true_footprint, virtual_clock

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "moments"
ASSETS = HERE / "assets" / "moments"

# The motor pose Production Hub logged when Subject Tracking was armed for
# each moment (ptz_tracking_source_changed events).
ARMED_POSE = {
    "youth0920_reader_walks_off_left": (45048, 33994, 3635),
    "youth0920_speaker_walks_off_left": (46010, 34139, 3548),
    "sermon0913_preacher_walks_off_right": (45528, 33450, 3410),
}

LOST_LIMIT_SECONDS = 1.0
REACT_LIMIT_SECONDS = 0.5
EDGE = 0.30


@dataclass(slots=True)
class MomentResult:
    name: str
    source: str
    note: str
    log: str
    armed: bool
    rules: dict[str, tuple[bool, str]] = field(default_factory=dict)
    timeline: list[dict] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.armed and all(ok for ok, _ in self.rules.values())


def available() -> list[str]:
    return sorted(
        path.stem
        for path in FIXTURES.glob("*.json")
        if (ASSETS / f"{path.stem}.mp4").exists() and path.stem in ARMED_POSE
    )


def _who(box, people, target: int) -> str:
    """Which annotated person a locked PTZ box belongs to."""

    cx = box.x + box.width / 2.0
    cy = box.y + box.height * 0.3
    best, best_area = "nobody", None
    for person in people:
        x, y, w, h = person["box"]
        if x <= cx <= x + w and y - 0.05 <= cy <= y + h:
            area = w * h
            if best_area is None or area < best_area:
                best = "subject" if person["track"] == target else "someone_else"
                best_area = area
    return best


def replay(name: str) -> MomentResult:
    from production_hub.calibration.relocalization import RelocalizationSnapshot, RelocalizationState
    from production_hub.tracking import service as service_module
    from production_hub.tracking.ptz_automation import PtzAutomationService
    from production_hub.tracking.ptz_geometry import PtzMotorPose
    from production_hub.tracking.service import PersonTrackingService
    from production_hub.video.models import VideoSourceKey, VideoSourceSnapshot, VideoSourceState
    from PySide6.QtGui import QImage

    moment = json.loads((FIXTURES / f"{name}.json").read_text())
    target = moment["target_track"]
    frames = moment["frames"]
    world = StageWorld([], [], initial_zoom=3500, duration=0.0)
    geometry, config, _ = world._build()
    capture = cv2.VideoCapture(str(ASSETS / f"{name}.mp4"))

    # Motor pose over the clip: the armed pose plus the camera motion the
    # background shows (the recording's pan/tilt, whoever drove it).
    pan, tilt, zoom = ARMED_POSE[name]
    poses = []
    for item in frames:
        view = world._view(geometry, (round(pan), round(tilt), zoom))
        probe_pan = world._view(geometry, (round(pan) + 200, round(tilt), zoom))
        probe_tilt = world._view(geometry, (round(pan), round(tilt) + 200, zoom))
        per_pan = (probe_pan[0] - view[0]) / 200.0
        per_tilt = (probe_tilt[1] - view[1]) / 200.0
        dx, dy = item["camera_shift"]
        footprint = true_footprint(zoom)
        # The background moving right means the view moved left.
        pan += (-dx * footprint) / per_pan if per_pan else 0.0
        tilt += (-dy * footprint) / per_tilt if per_tilt else 0.0
        poses.append((item["t"], round(pan), round(tilt), zoom))

    def pose_at(t: float):
        best = min(poses, key=lambda item: abs(item[0] - t))
        return best[1:]

    def annotation_at(t: float):
        return min(frames, key=lambda item: abs(item["t"] - t))

    result = MomentResult(name, moment["source"]["url"], moment["note"], moment["log"], False)
    with virtual_clock() as clock:
        detector = service_module._default_detector_factory(config)
        tracking = PersonTrackingService(None, config, detector_factory=lambda _config: detector)
        blank = QImage(960, 540, QImage.Format.Format_RGB888)
        blank.fill(0)

        class _Camera:
            def __init__(self, pose) -> None:
                self.config = type("Config", (), {"enabled": True})()
                self.current = pose
                self.commands: list = []

            async def query_pan_tilt_position(self):
                return self.current[0], self.current[1]

            async def query_zoom_position(self):
                return self.current[2]

            async def pan_tilt(self, p, t):
                self.commands.append((clock.now, p, t))
                return True

            async def zoom(self, z):
                return True

            async def stop_pan_tilt(self):
                self.commands.append((clock.now, 50, 50))
                return True

            async def stop_zoom(self):
                return True

        camera = _Camera(pose_at(0.0))

        class _Reloc:
            def snapshot(self):
                return RelocalizationSnapshot(
                    state=RelocalizationState.LOCKED, approval_status="approved",
                    reference_size=(1920, 1080), live_size=(1920, 1080),
                    last_analysis_monotonic=clock.now, message="Locked",
                )

        class _Video:
            relocalization = _Reloc()

            def __init__(self):
                self.tracking = tracking

            def snapshot(self, source):
                return VideoSourceSnapshot(source, VideoSourceState.RUNNING, last_frame_monotonic=clock.now)

            def set_tracking_activity(self, *args, **kwargs):
                return None

            def set_calibration_activity(self, *args, **kwargs):
                return None

        service = PtzAutomationService(_Video(), camera, config, Path("/tmp/stage-realism"), geometry_loader=lambda _r: geometry)
        from .target import apply_app_settings

        apply_app_settings(tracking, service)
        observed = {}
        original_update = service._subject_lock.update

        def spy(*args, **kwargs):
            outcome = original_update(*args, **kwargs)
            observed["lock"] = outcome
            return outcome

        service._subject_lock.update = spy
        start = clock.now
        interval = 1.0 / config.analysis_fps
        sequence = 0

        def analyze_at(t: float) -> None:
            nonlocal sequence
            capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t) * 1000.0)
            ok, frame = capture.read()
            if not ok:
                return
            frame = cv2.resize(frame, (960, 540), interpolation=cv2.INTER_AREA)
            if moment.get("mask"):
                # A stream graphic (slide picture-in-picture) the PTZ never saw.
                mx, my, mw, mh = moment["mask"]
                frame[int(my * 540) : int((my + mh) * 540), int(mx * 960) : int((mx + mw) * 960)] = 110
            image = QImage(frame.data, 960, 540, frame.strides[0], QImage.Format.Format_BGR888).copy()
            camera.current = pose_at(t)
            sequence += 1
            saved = clock.now
            clock.now = saved + LATENCY["inference"]
            tracking._analyze(VideoSourceKey.PTZ, sequence, image, detector)
            tracking._analyze(VideoSourceKey.AUDIENCE, sequence, blank, detector)
            clock.now = saved

        # The detector had been running before the operator pressed arm.
        for warm in range(3):
            clock.now = start + warm * interval
            analyze_at(0.0)
        armed, message = service.arm()
        result.armed = armed
        if not armed:
            result.rules["armed"] = (False, message)
            return result

        duration = frames[-1]["t"]
        t = 0.0
        next_decision = 0.13
        while t <= duration:
            clock.now = start + 3 * interval + t
            analyze_at(max(0.0, t - LATENCY["video_to_detector"]))
            while next_decision <= t + interval:
                clock.now = start + 3 * interval + next_decision
                camera.current = pose_at(next_decision)
                try:
                    service._cycle(clock.now)
                except Exception as exc:  # recorded like the live worker
                    result.rules.setdefault("errors", (False, f"{type(exc).__name__}: {exc}"))
                next_decision += 1.0 / config.automation.decision_fps
            lock = observed.get("lock")
            locked = list(lock.ptz.subjects) if lock is not None else []
            annotation = annotation_at(t)
            whom = [_who(item.bounds, annotation["people"], target) for item in locked]
            decision = service.snapshot().decision
            desired = None
            if decision is not None and decision.desired_pose is not None:
                desired = decision.desired_pose.pan - camera.current[0]
            subject = [p for p in annotation["people"] if p["track"] == target]
            result.timeline.append(
                {
                    "t": round(t, 2),
                    "locked": whom,
                    "subject_x": None if not subject else round(subject[0]["box"][0] + subject[0]["box"][2] / 2, 3),
                    "subject_visible": bool(subject and subject[0]["box"][3] > 0.3
                                             and 0.05 < subject[0]["box"][0] + subject[0]["box"][2] / 2 < 0.95),
                    "desired_pan_delta": desired,
                    "source": service.snapshot().tracking_source,
                }
            )
            t += interval

    _judge(result, interval)
    return result


def _judge(result: MomentResult, interval: float) -> None:
    rows = result.timeline
    wrong = [row["t"] for row in rows if "someone_else" in row["locked"]]
    result.rules["never_switches"] = (
        not wrong,
        "never locked onto anyone else" if not wrong
        else f"locked onto someone else for {len(wrong) * interval:.1f} s, first at {wrong[0]:.1f} s",
    )
    longest = current = 0.0
    at = None
    for row in rows:
        lost = row["subject_visible"] and "subject" not in row["locked"]
        current = current + interval if lost else 0.0
        if current > longest:
            longest, at = current, row["t"] - current + interval
    result.rules["keeps_lock"] = (
        longest <= LOST_LIMIT_SECONDS,
        f"longest loss while the subject was in shot: {longest:.1f} s" + (f" from {at:.1f} s" if at is not None and longest else ""),
    )
    late = []
    edge_since = None
    for row in rows:
        x = row["subject_x"]
        if x is None or not (x < EDGE or x > 1 - EDGE):
            edge_since = None
            continue
        edge_since = row["t"] if edge_since is None else edge_since
        # Pan increases to move the view left (Panasonic convention here).
        toward = row["desired_pan_delta"] is not None and (
            (x < 0.5 and row["desired_pan_delta"] > 0) or (x > 0.5 and row["desired_pan_delta"] < 0)
        )
        if toward:
            edge_since = None
        elif row["t"] - edge_since > REACT_LIMIT_SECONDS:
            late.append(row["t"])
            edge_since = None
    result.rules["reacts"] = (
        not late,
        "asked to pan toward the subject whenever they neared the edge" if not late
        else f"did not ask to pan toward the subject near the edge within {REACT_LIMIT_SECONDS} s (at {', '.join(f'{t:.1f}' for t in late[:4])} s)",
    )
