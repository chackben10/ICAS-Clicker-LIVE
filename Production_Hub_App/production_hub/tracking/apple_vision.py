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
    ) -> None:
        self.maximum_width = max(320, int(maximum_width))
        self.minimum_confidence = max(0.0, min(1.0, float(minimum_confidence)))
        self.upper_body_only = bool(upper_body_only)
        self.include_body_pose = bool(include_body_pose)
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
            envelopes = self._pose_envelopes(self._body_pose_request.results() or [])
            return merge_pose_envelopes(candidates, envelopes)

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
        group = getattr(
            self.Vision,
            "VNHumanBodyPoseObservationJointsGroupNameAll",
            None,
        )
        if group is None:
            return []
        envelopes: list[NormalizedRect] = []
        for observation in observations:
            try:
                result = observation.recognizedPointsForGroupKey_error_(group, None)
                points = result[0] if isinstance(result, tuple) else result
                joints: dict[str, tuple[float, float]] = {}
                for name, point in (points or {}).items():
                    if float(point.confidence()) < self.minimum_pose_joint_confidence:
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
                    envelopes.append(envelope)
            except Exception:
                # Pose enrichment is optional. Rectangle detection remains the
                # fail-soft baseline on older macOS/Vision revisions.
                continue
        return envelopes


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
) -> list[PersonCandidate]:
    """Assign poses to rectangles and retain standalone pose detections.

    Human rectangles are an excellent baseline at normal shot sizes, while a
    body-pose request can still resolve a small, unobstructed speaker in a very
    wide room view.  A valid pose envelope must therefore remain a candidate
    when the rectangle request misses it.  Scene-region admission in the
    tracking service remains the second, conservative false-positive filter.
    """

    merged = list(candidates)
    for envelope in envelopes:
        if not merged:
            merged.append(PersonCandidate(envelope, 0.50))
            continue
        distances = [item.bounds.center_distance(envelope) for item in merged]
        index = min(range(len(merged)), key=distances.__getitem__)
        candidate = merged[index]
        diagonal = (candidate.bounds.width**2 + candidate.bounds.height**2) ** 0.5
        if distances[index] > max(0.12, diagonal * 0.9):
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
