from __future__ import annotations

import sys
from collections.abc import Sequence
from math import hypot

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from production_hub.tracking.models import NormalizedRect, PersonCandidate


class VisionUnavailableError(RuntimeError):
    pass


class AppleVisionPersonDetector:
    """Thin PyObjC adapter around Apple's hardware-aware Vision framework."""

    backend_name = "Apple Vision"
    supports_region_cropping = True

    def __init__(
        self,
        *,
        maximum_width: int = 960,
        minimum_confidence: float = 0.25,
        upper_body_only: bool = True,
        include_body_pose: bool = True,
        minimum_pose_joint_confidence: float = 0.20,
        conservative_pose_merging: bool = False,
    ) -> None:
        self.maximum_width = max(320, int(maximum_width))
        self.minimum_confidence = max(0.0, min(1.0, float(minimum_confidence)))
        self.upper_body_only = bool(upper_body_only)
        self.include_body_pose = bool(include_body_pose)
        self.conservative_pose_merging = conservative_pose_merging
        self.minimum_pose_joint_confidence = max(
            0.05,
            min(0.90, float(minimum_pose_joint_confidence)),
        )
        if sys.platform != "darwin":
            raise VisionUnavailableError("Apple Vision person detection requires macOS.")
        try:
            import Quartz
            import Vision
            import objc
        except Exception as exc:  # pragma: no cover - exercised on non-production hosts
            raise VisionUnavailableError(
                "Apple Vision bindings are unavailable. Install pyobjc-framework-Vision."
            ) from exc
        self.Quartz = Quartz
        self.Vision = Vision
        self.objc = objc
        with self.objc.autorelease_pool():
            self._request = self.Vision.VNDetectHumanRectanglesRequest.alloc().init()
            self._request.setUpperBodyOnly_(self.upper_body_only)
            if hasattr(self._request, "setRevision_") and hasattr(
                self.Vision,
                "VNDetectHumanRectanglesRequestRevision2",
            ):
                self._request.setRevision_(self.Vision.VNDetectHumanRectanglesRequestRevision2)
            self._body_pose_request = (
                self.Vision.VNDetectHumanBodyPoseRequest.alloc().init()
                if self.include_body_pose
                and hasattr(self.Vision, "VNDetectHumanBodyPoseRequest")
                else None
            )
        self._color_space = self.Quartz.CGColorSpaceCreateDeviceRGB()

    def detect(self, image: QImage) -> list[PersonCandidate]:
        if image.isNull():
            return []
        prepared = self._prepare_image(image)
        with self.objc.autorelease_pool():
            cg_image = self._to_cg_image(prepared)
            handler = self.Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(
                cg_image,
                {},
            )
            requests = [self._request]
            if self._body_pose_request is not None:
                requests.append(self._body_pose_request)
            succeeded, error = handler.performRequests_error_(requests, None)
            if not succeeded:
                raise RuntimeError(f"Apple Vision person detection failed: {error}")
            candidates = self._candidates(self._request.results() or [])
            if self._body_pose_request is None:
                return candidates
            poses = self._pose_observations(self._body_pose_request.results() or [])
            return merge_pose_envelopes(
                candidates,
                [envelope for envelope, _person in poses],
                conservative=self.conservative_pose_merging,
                standalone=[person for _envelope, person in poses],
            )

    def _prepare_image(self, image: QImage) -> QImage:
        prepared = image
        if image.width() > self.maximum_width:
            prepared = image.scaledToWidth(
                self.maximum_width,
                Qt.TransformationMode.FastTransformation,
            )
        return prepared.convertToFormat(QImage.Format.Format_RGBA8888)

    def _to_cg_image(self, image: QImage):
        quartz = self.Quartz
        pixel_bytes = bytes(image.constBits())
        provider = quartz.CGDataProviderCreateWithCFData(pixel_bytes)
        cg_image = quartz.CGImageCreate(
            image.width(),
            image.height(),
            8,
            32,
            image.bytesPerLine(),
            self._color_space,
            quartz.kCGBitmapByteOrderDefault | quartz.kCGImageAlphaLast,
            provider,
            None,
            False,
            quartz.kCGRenderingIntentDefault,
        )
        if cg_image is None:
            raise RuntimeError("Apple Vision could not create an image from the video frame.")
        return cg_image

    def _candidates(self, observations: Sequence[object]) -> list[PersonCandidate]:
        candidates: list[PersonCandidate] = []
        for observation in observations:
            confidence = float(observation.confidence())
            if confidence < self.minimum_confidence:
                continue
            bounds = observation.boundingBox()
            # Vision uses a lower-left origin; Production Hub overlays use top-left.
            rectangle = NormalizedRect(
                float(bounds.origin.x),
                1.0 - float(bounds.origin.y + bounds.size.height),
                float(bounds.size.width),
                float(bounds.size.height),
            ).clamped()
            if rectangle.area > 0:
                candidates.append(PersonCandidate(rectangle, confidence))
        return sorted(candidates, key=lambda item: (item.bounds.x, item.bounds.y))

    def _pose_envelopes(self, observations: Sequence[object]) -> list[NormalizedRect]:
        return [envelope for envelope, _person in self._pose_observations(observations)]

    def _pose_observations(
        self,
        observations: Sequence[object],
    ) -> list[tuple[NormalizedRect, bool]]:
        """Pose envelopes, each with whether the pose alone is evidence of a person."""

        group = getattr(
            self.Vision,
            "VNHumanBodyPoseObservationJointsGroupNameAll",
            None,
        )
        if group is None:
            return []
        envelopes: list[tuple[NormalizedRect, bool]] = []
        for observation in observations:
            try:
                result = observation.recognizedPointsForGroupKey_error_(group, None)
                points = result[0] if isinstance(result, tuple) else result
                joints: dict[str, tuple[float, float]] = {}
                confidences: dict[str, float] = {}
                for name, point in (points or {}).items():
                    confidence = float(point.confidence())
                    confidences[str(name)] = confidence
                    if confidence < self.minimum_pose_joint_confidence:
                        continue
                    location = point.location()
                    joints[str(name)] = (
                        float(location.x),
                        1.0 - float(location.y),
                    )
                locations = list(joints.values())
                locations.extend(_raised_hand_extension_points(joints))
                if len(locations) < 4:
                    continue
                xs = [item[0] for item in locations]
                ys = [item[1] for item in locations]
                padding_x = max(0.015, (max(xs) - min(xs)) * 0.08)
                padding_y = max(0.015, (max(ys) - min(ys)) * 0.06)
                envelope = NormalizedRect(
                    min(xs) - padding_x,
                    min(ys) - padding_y,
                    max(xs) - min(xs) + padding_x * 2.0,
                    max(ys) - min(ys) + padding_y * 2.0,
                ).clamped()
                if envelope.area > 0:
                    envelopes.append((envelope, pose_is_person(envelope, confidences, joints)))
            except Exception:
                # Pose enrichment is optional. Rectangle detection remains the
                # fail-soft baseline on older macOS/Vision revisions.
                continue
        return envelopes


# A pose that fills a real share of the frame but arrives without a human
# rectangle must look like a person: a confident face and shoulders, and a
# skeleton wider than a pole. Stage hardware can produce a convincing
# skeleton. On the ICAS stage the electronic kick-drum tower yields hips,
# knees, and feet at 0.5-0.7, but measured over 39 empty-stage PTZ views its
# best face joint was 0.33-0.54 and its joint spread only 0.10-0.15 as wide as
# tall. Real speakers in 94 poses from our streams: face 0.67-0.93 (median
# 0.88), width/height 0.24-0.88 (median 0.42). Small poses keep the lenient
# path, which recovers distant speakers in the wide camera.
POSE_ONLY_EVIDENCE_MINIMUM_HEIGHT = 0.20
POSE_ONLY_FACE_CONFIDENCE = 0.60
POSE_ONLY_SHOULDER_CONFIDENCE = 0.40
POSE_ONLY_MINIMUM_ASPECT = 0.18
_FACE_JOINTS = (
    "head_joint",
    "nose_joint",
    "left_eye_joint",
    "right_eye_joint",
    "left_ear_joint",
    "right_ear_joint",
)
_SHOULDER_JOINTS = ("left_shoulder_1_joint", "right_shoulder_1_joint", "neck_1_joint")


def pose_is_person(
    envelope: NormalizedRect,
    confidences: dict[str, float],
    joints: dict[str, tuple[float, float]] | None = None,
) -> bool:
    """Whether a pose may stand as a person without a supporting rectangle."""

    if envelope.height < POSE_ONLY_EVIDENCE_MINIMUM_HEIGHT:
        return True
    face = max((confidences.get(name, 0.0) for name in _FACE_JOINTS), default=0.0)
    shoulders = sum(
        confidences.get(name, 0.0) >= POSE_ONLY_SHOULDER_CONFIDENCE for name in _SHOULDER_JOINTS
    )
    if face < POSE_ONLY_FACE_CONFIDENCE or shoulders < 2:
        return False
    if joints:
        xs = [point[0] for point in joints.values()]
        ys = [point[1] for point in joints.values()]
        spread = (max(xs) - min(xs)) / max(1e-6, max(ys) - min(ys))
        if spread < POSE_ONLY_MINIMUM_ASPECT:
            return False
    return True


def _raised_hand_extension_points(
    joints: dict[str, tuple[float, float]],
) -> list[tuple[float, float]]:
    """Estimate fingertip reach beyond Vision's wrist landmark.

    VNDetectHumanBodyPoseRequest ends each arm at the wrist. A raised hand can
    therefore leave the picture while the reported pose still appears safely
    inside it. Extending the elbow-to-wrist vector supplies framing headroom
    without adding a second, substantially more expensive hand-pose request.
    """

    extensions: list[tuple[float, float]] = []
    for side in ("left", "right"):
        shoulder = _first_joint(
            joints,
            f"{side}_shoulder_1_joint",
            f"{side}_shoulder_joint",
        )
        elbow = _first_joint(
            joints,
            f"{side}_forearm_joint",
            f"{side}_elbow_joint",
        )
        wrist = _first_joint(
            joints,
            f"{side}_hand_joint",
            f"{side}_wrist_joint",
        )
        if shoulder is None or elbow is None or wrist is None:
            continue
        vector_x = wrist[0] - elbow[0]
        vector_y = wrist[1] - elbow[1]
        if (
            wrist[1] > shoulder[1] - 0.06
            or wrist[1] > elbow[1] - 0.02
            or hypot(vector_x, vector_y) < 0.025
        ):
            continue
        extensions.append(
            (
                max(-0.15, min(1.15, wrist[0] + vector_x * 0.85)),
                max(-0.15, min(1.15, wrist[1] + vector_y * 0.85)),
            )
        )
    return extensions


def _first_joint(
    joints: dict[str, tuple[float, float]],
    *names: str,
) -> tuple[float, float] | None:
    for name in names:
        if name in joints:
            return joints[name]
    return None


def merge_pose_envelopes(
    candidates: Sequence[PersonCandidate],
    envelopes: Sequence[NormalizedRect],
    *, conservative: bool = False,
    standalone: Sequence[bool] | None = None,
) -> list[PersonCandidate]:
    """Assign poses to rectangles and retain standalone pose detections.

    Human rectangles are an excellent baseline at normal shot sizes, while a
    body-pose request can still resolve a small, unobstructed speaker in a very
    wide room view.  A valid pose envelope must therefore remain a candidate
    when the rectangle request misses it.  Scene-region admission in the
    tracking service remains the second, conservative false-positive filter.
    """

    # `standalone[i]` False: the pose may enrich a matching rectangle but is
    # not, on its own, evidence of a person (see `pose_is_person`).
    allowed = list(standalone) if standalone is not None else [True] * len(envelopes)
    if conservative:
        return _merge_supported_poses(candidates, envelopes, allowed)
    merged = list(candidates)
    for envelope, may_stand_alone in zip(envelopes, allowed):
        if not merged:
            if may_stand_alone:
                merged.append(PersonCandidate(envelope, 0.50))
            continue
        distances = [item.bounds.center_distance(envelope) for item in merged]
        index = min(range(len(merged)), key=distances.__getitem__)
        candidate = merged[index]
        diagonal = (candidate.bounds.width**2 + candidate.bounds.height**2) ** 0.5
        if distances[index] > max(0.12, diagonal * 0.9):
            if may_stand_alone:
                merged.append(PersonCandidate(envelope, 0.50))
            continue
        left = min(candidate.bounds.x, envelope.x)
        top = min(candidate.bounds.y, envelope.y)
        right = max(
            candidate.bounds.x + candidate.bounds.width,
            envelope.x + envelope.width,
        )
        bottom = max(
            candidate.bounds.y + candidate.bounds.height,
            envelope.y + envelope.height,
        )
        merged[index] = PersonCandidate(
            NormalizedRect(left, top, right - left, bottom - top).clamped(),
            candidate.confidence,
        )
    return sorted(merged, key=lambda item: (item.bounds.x, item.bounds.y))


def _intersection_fraction(a: NormalizedRect, b: NormalizedRect) -> float:
    width = max(0.0, min(a.x+a.width, b.x+b.width)-max(a.x,b.x))
    height = max(0.0, min(a.y+a.height, b.y+b.height)-max(a.y,b.y))
    return width*height / max(1e-9, min(a.area,b.area))


def _merge_supported_poses(candidates, envelopes, allowed=None) -> list[PersonCandidate]:
    # Vision can return both a torso rectangle and a larger rectangle for the
    # same person. Do not let these seed two locks at arm time.
    rectangles: list[PersonCandidate] = []
    for candidate in sorted(candidates, key=lambda c: c.confidence, reverse=True):
        b = candidate.bounds
        duplicate = any(
            _intersection_fraction(b, c.bounds) > .90
            and abs(b.center[0]-c.bounds.center[0]) < min(b.width,c.bounds.width)*.25
            and abs(b.y-c.bounds.y) < min(b.height,c.bounds.height)*.15
            for c in rectangles
        )
        if not duplicate:
            rectangles.append(candidate)
    matches = []
    for i, candidate in enumerate(rectangles):
        b = candidate.bounds
        for j, pose in enumerate(envelopes):
            # A tall rectangle must not authorize a huge horizontal shift.
            # This rejects the recorded right-edge subject + drum-kit pose,
            # while retaining centred raised hands and outstretched arms.
            if (
                _intersection_fraction(b, pose) >= .25
                and abs(b.center[0]-pose.center[0]) <= max(.025, b.width*.75)
                and pose.width <= b.width*3.0
                and pose.height <= b.height*3.0
            ):
                matches.append((b.center_distance(pose), i, j))
    merged = list(rectangles)
    used_rectangles, used_poses = set(), set()
    for _, i, j in sorted(matches):
        if i in used_rectangles or j in used_poses:
            continue
        used_rectangles.add(i)
        used_poses.add(j)
        candidate, pose = rectangles[i], envelopes[j]
        b = candidate.bounds
        left, top = min(b.x,pose.x), min(b.y,pose.y)
        merged[i] = PersonCandidate(NormalizedRect(
            left, top, max(b.x+b.width,pose.x+pose.width)-left,
            max(b.y+b.height,pose.y+pose.height)-top,
        ).clamped(), candidate.confidence)
    for j, pose in enumerate(envelopes):
        if j in used_poses:
            continue
        if allowed is not None and not allowed[j]:
            continue
        # A rejected pose overlapping an observed person is uncertain
        # enrichment, not evidence of another person. Independent poses remain
        # available for the distant Audience camera's recovery path.
        if any(_intersection_fraction(pose,c.bounds) >= .25 for c in rectangles):
            continue
        if any(_intersection_fraction(pose,c.bounds) > .75 for c in merged):
            continue
        merged.append(PersonCandidate(pose,.50))
    return sorted(merged, key=lambda c: (c.bounds.x,c.bounds.y))
