"""Align detector rectangles with the camera pose at frame capture time."""
from collections import deque
from dataclasses import replace

from production_hub.tracking.ptz_geometry import PtzGeometryModel, PtzMotorPose
from production_hub.tracking.models import NormalizedRect, TrackingSnapshot


class PtzFrameAlignment:
    def __init__(self) -> None:
        self.poses: deque[tuple[float, PtzMotorPose]] = deque(maxlen=64)
        self.observations: dict[int, tuple[float, NormalizedRect, float, float]] = {}

    def reset(self) -> None:
        self.poses.clear()
        self.observations.clear()

    def align(
        self, snapshot: TrackingSnapshot, geometry: PtzGeometryModel,
        actual: PtzMotorPose, now: float,
    ) -> TrackingSnapshot:
        if self.poses and now <= self.poses[-1][0]:
            self.reset()
        self.poses.append((now, actual))
        captured = snapshot.frame_captured_monotonic or snapshot.last_analysis_monotonic
        history = tuple(self.poses)
        if not captured or not 0 < now - captured <= 1.0 or captured < history[0][0]:
            return snapshot
        old_pose = None
        for (left_t, left), (right_t, right) in zip(history, history[1:]):
            if left_t <= captured <= right_t:
                fraction = (captured - left_t) / (right_t - left_t)
                old_pose = PtzMotorPose(*(round(a+(b-a)*fraction) for a, b in
                                         zip((left.pan,left.tilt,left.zoom),
                                             (right.pan,right.tilt,right.zoom))))
                break
        if old_pose is None:
            return snapshot
        subjects = []
        unpredicted = []
        leads = []
        for subject in snapshot.subjects:
            # Association can retain an old box through a missed detection.
            # Its observation belongs to an older frame, not this capture.
            if subject.last_seen_monotonic < snapshot.last_analysis_monotonic - .001:
                subjects.append(subject)
                unpredicted.append(subject)
                leads.append(0.0)
                continue
            reference = geometry.ptz_rect_to_reference(subject.bounds, old_pose)
            # Estimate subject motion in the fixed calibration plane: camera
            # pan/tilt must not be mistaken for the person walking. Only fresh
            # observations advance this estimate; repeated frames do not.
            previous = self.observations.get(subject.track_id)
            vx = vy = 0.0
            if previous is not None:
                stamp, prior, prior_vx, prior_vy = previous
                dt = captured - stamp
                if 0 < dt <= .5:
                    dx = reference.center[0] - prior.center[0]
                    dy = reference.center[1] - prior.center[1]
                    # Reject abrupt identity/size changes instead of predicting
                    # from a new person or a merged detector rectangle.
                    ratio = reference.area / max(prior.area, 1e-9)
                    if .5 <= ratio <= 2 and abs(dx) <= reference.width * 2:
                        vx = dx / dt
                        if abs(vx) > reference.width * 3:
                            vx = 0.0
                        vy = dy / dt
                elif dt == 0:
                    vx, vy = prior_vx, prior_vy
            if previous is None or captured > previous[0]:
                self.observations[subject.track_id] = (captured, reference, vx, vy)
            # Use a conservative horizontal lead. Vertical pose changes
            # (ducking/standing) are not reliable walking velocity estimates.
            horizon = min(.20, now - captured) * .5
            vy = 0.0
            lead_x = max(-reference.width, min(reference.width, vx * horizon))
            lead_y = max(-reference.height * .25, min(reference.height * .25, vy * horizon))
            predicted = NormalizedRect(reference.x + lead_x, reference.y + lead_y,
                                       reference.width, reference.height)
            current = geometry.reference_rect_to_ptz_image(predicted, actual)
            subjects.append(replace(subject, bounds=current))
            unpredicted.append(replace(subject, bounds=geometry.reference_rect_to_ptz_image(reference, actual)))
            leads.append(lead_x)
        # Predict a group only when its members move together. Independent
        # gestures or opposing movement should affect framing, not initiate
        # a pan based on an extrapolated group center.
        if len(leads) > 1 and not (all(x > 1e-6 for x in leads) or all(x < -1e-6 for x in leads)):
            subjects = unpredicted
        self.observations = {key: value for key, value in self.observations.items()
                             if now - value[0] <= 1.0}
        return replace(snapshot, subjects=tuple(subjects))
