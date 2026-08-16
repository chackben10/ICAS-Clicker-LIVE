from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import cv2
import numpy as np


class AlignmentError(RuntimeError):
    """Raised when two images cannot produce a defensible alignment."""


@dataclass(frozen=True, slots=True)
class AlignmentSettings:
    maximum_width: int = 1600
    maximum_features: int = 8000
    ratio_threshold: float = 0.70
    ransac_reprojection_threshold: float = 3.0
    minimum_matches: int = 30
    minimum_inliers: int = 24
    minimum_inlier_ratio: float = 0.28
    minimum_audience_coverage: float = 0.025
    minimum_ptz_coverage: float = 0.10
    maximum_median_error: float = 3.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "maximum_width", max(640, min(3840, int(self.maximum_width))))
        object.__setattr__(self, "maximum_features", max(500, min(30000, int(self.maximum_features))))
        object.__setattr__(self, "ratio_threshold", max(0.4, min(0.95, float(self.ratio_threshold))))
        object.__setattr__(
            self,
            "ransac_reprojection_threshold",
            max(0.5, min(15.0, float(self.ransac_reprojection_threshold))),
        )
        object.__setattr__(self, "minimum_matches", max(8, int(self.minimum_matches)))
        object.__setattr__(self, "minimum_inliers", max(8, int(self.minimum_inliers)))
        object.__setattr__(
            self,
            "minimum_inlier_ratio",
            max(0.05, min(1.0, float(self.minimum_inlier_ratio))),
        )
        object.__setattr__(
            self,
            "minimum_audience_coverage",
            max(0.001, min(1.0, float(self.minimum_audience_coverage))),
        )
        object.__setattr__(
            self,
            "minimum_ptz_coverage",
            max(0.001, min(1.0, float(self.minimum_ptz_coverage))),
        )
        object.__setattr__(
            self,
            "maximum_median_error",
            max(0.25, min(20.0, float(self.maximum_median_error))),
        )


@dataclass(frozen=True, slots=True)
class PointCorrespondence:
    audience_x: float
    audience_y: float
    ptz_x: float
    ptz_y: float
    error_pixels: float


@dataclass(frozen=True, slots=True)
class AlignmentResult:
    status: str
    confidence_score: float
    reasons: tuple[str, ...]
    method: str
    audience_size: tuple[int, int]
    ptz_size: tuple[int, int]
    audience_keypoints: int
    ptz_keypoints: int
    candidate_matches: int
    inliers: int
    inlier_ratio: float
    median_error_pixels: float
    p95_error_pixels: float
    audience_coverage: float
    ptz_coverage: float
    audience_to_ptz: tuple[tuple[float, float, float], ...]
    ptz_to_audience: tuple[tuple[float, float, float], ...]
    correspondences: tuple[PointCorrespondence, ...] = field(default_factory=tuple)

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reasons"] = list(self.reasons)
        payload["audience_size"] = list(self.audience_size)
        payload["ptz_size"] = list(self.ptz_size)
        payload["audience_to_ptz"] = [list(row) for row in self.audience_to_ptz]
        payload["ptz_to_audience"] = [list(row) for row in self.ptz_to_audience]
        payload["correspondences"] = [asdict(item) for item in self.correspondences]
        return payload


def estimate_alignment(
    audience_bgr: np.ndarray,
    ptz_bgr: np.ndarray,
    settings: AlignmentSettings | None = None,
) -> AlignmentResult:
    """Estimate a read-only image-space transform from Audience into PTZ pixels."""

    selected = settings or AlignmentSettings()
    audience = _validated_image(audience_bgr, "Audience")
    ptz = _validated_image(ptz_bgr, "PTZ")
    audience_small, audience_scale = _scaled_image(audience, selected.maximum_width)
    ptz_small, ptz_scale = _scaled_image(ptz, selected.maximum_width)
    audience_gray = _feature_image(audience_small)
    ptz_gray = _feature_image(ptz_small)

    detector = cv2.SIFT_create(
        nfeatures=selected.maximum_features,
        contrastThreshold=0.018,
        edgeThreshold=12,
    )
    audience_points, audience_descriptors = detector.detectAndCompute(audience_gray, None)
    ptz_points, ptz_descriptors = detector.detectAndCompute(ptz_gray, None)
    if audience_descriptors is None or ptz_descriptors is None:
        raise AlignmentError("One or both camera images contain no usable SIFT features.")

    matches = _mutual_ratio_matches(
        audience_descriptors,
        ptz_descriptors,
        selected.ratio_threshold,
    )
    if len(matches) < 4:
        raise AlignmentError(
            f"Only {len(matches)} unambiguous reciprocal feature matches were found; at least 4 are required."
        )

    source = np.float32([audience_points[item.queryIdx].pt for item in matches])
    target = np.float32([ptz_points[item.trainIdx].pt for item in matches])
    robust_method = getattr(cv2, "USAC_MAGSAC", cv2.RANSAC)
    homography_small, mask = cv2.findHomography(
        source,
        target,
        robust_method,
        selected.ransac_reprojection_threshold,
        None,
        10000,
        0.999,
    )
    if homography_small is None or mask is None:
        raise AlignmentError("OpenCV could not estimate a robust homography from the matched features.")
    if not np.isfinite(homography_small).all() or abs(float(np.linalg.det(homography_small))) < 1e-12:
        raise AlignmentError("The estimated homography is singular or contains invalid values.")

    inlier_mask = mask.reshape(-1).astype(bool)
    inlier_source = source[inlier_mask]
    inlier_target = target[inlier_mask]
    inlier_count = int(inlier_mask.sum())
    if inlier_count < 4:
        raise AlignmentError("Fewer than four geometrically consistent matches survived robust fitting.")

    inverse_small = np.linalg.inv(homography_small)
    forward = cv2.perspectiveTransform(inlier_source[:, None, :], homography_small)[:, 0, :]
    backward = cv2.perspectiveTransform(inlier_target[:, None, :], inverse_small)[:, 0, :]
    forward_error = np.linalg.norm(forward - inlier_target, axis=1)
    backward_error = np.linalg.norm(backward - inlier_source, axis=1)
    symmetric_error = (forward_error + backward_error) / 2.0

    audience_height, audience_width = audience.shape[:2]
    ptz_height, ptz_width = ptz.shape[:2]
    audience_small_height, audience_small_width = audience_small.shape[:2]
    ptz_small_height, ptz_small_width = ptz_small.shape[:2]
    audience_coverage = _convex_coverage(
        inlier_source,
        audience_small_width,
        audience_small_height,
    )
    ptz_coverage = _convex_coverage(
        inlier_target,
        ptz_small_width,
        ptz_small_height,
    )

    audience_scale_matrix = np.array(
        [[audience_scale, 0.0, 0.0], [0.0, audience_scale, 0.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    ptz_scale_matrix = np.array(
        [[ptz_scale, 0.0, 0.0], [0.0, ptz_scale, 0.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    homography = np.linalg.inv(ptz_scale_matrix) @ homography_small @ audience_scale_matrix
    homography /= homography[2, 2]
    inverse = np.linalg.inv(homography)
    inverse /= inverse[2, 2]

    errors_in_ptz_pixels = symmetric_error / max(ptz_scale, 1e-9)
    median_error = float(np.median(errors_in_ptz_pixels))
    p95_error = float(np.percentile(errors_in_ptz_pixels, 95))
    inlier_ratio = inlier_count / len(matches)
    reasons = _quality_reasons(
        selected,
        len(matches),
        inlier_count,
        inlier_ratio,
        median_error,
        audience_coverage,
        ptz_coverage,
    )
    confidence = _confidence_score(
        selected,
        inlier_count,
        inlier_ratio,
        median_error,
        audience_coverage,
        ptz_coverage,
    )

    correspondences: list[PointCorrespondence] = []
    inlier_indices = np.flatnonzero(inlier_mask)
    for local_index, match_index in enumerate(inlier_indices):
        audience_x, audience_y = source[match_index] / audience_scale
        ptz_x, ptz_y = target[match_index] / ptz_scale
        correspondences.append(
            PointCorrespondence(
                audience_x=float(audience_x),
                audience_y=float(audience_y),
                ptz_x=float(ptz_x),
                ptz_y=float(ptz_y),
                error_pixels=float(errors_in_ptz_pixels[local_index]),
            )
        )
    correspondences.sort(key=lambda item: item.error_pixels)

    return AlignmentResult(
        status="accepted" if not reasons else "low_confidence",
        confidence_score=confidence,
        reasons=tuple(reasons),
        method="SIFT + reciprocal ratio matching + USAC_MAGSAC homography",
        audience_size=(audience_width, audience_height),
        ptz_size=(ptz_width, ptz_height),
        audience_keypoints=len(audience_points),
        ptz_keypoints=len(ptz_points),
        candidate_matches=len(matches),
        inliers=inlier_count,
        inlier_ratio=inlier_ratio,
        median_error_pixels=median_error,
        p95_error_pixels=p95_error,
        audience_coverage=audience_coverage,
        ptz_coverage=ptz_coverage,
        audience_to_ptz=_matrix_tuple(homography),
        ptz_to_audience=_matrix_tuple(inverse),
        correspondences=tuple(correspondences),
    )


def consolidate_alignments(
    results: list[AlignmentResult] | tuple[AlignmentResult, ...],
    settings: AlignmentSettings | None = None,
) -> AlignmentResult:
    """Refit one robust transform from several mutually consistent sample models."""

    selected = settings or AlignmentSettings()
    if len(results) < 2:
        raise AlignmentError("Multi-sample consolidation requires at least two alignment results.")
    audience_size = results[0].audience_size
    ptz_size = results[0].ptz_size
    if any(item.audience_size != audience_size or item.ptz_size != ptz_size for item in results):
        raise AlignmentError("All alignment samples must use the same Audience and PTZ resolutions.")
    correspondences = [
        point
        for result in results
        for point in result.correspondences
    ]
    if len(correspondences) < 4:
        raise AlignmentError("The sample models do not contain enough inliers to consolidate.")
    source = np.float32(
        [[point.audience_x, point.audience_y] for point in correspondences]
    )
    target = np.float32([[point.ptz_x, point.ptz_y] for point in correspondences])
    robust_method = getattr(cv2, "USAC_MAGSAC", cv2.RANSAC)
    homography, mask = cv2.findHomography(
        source,
        target,
        robust_method,
        selected.ransac_reprojection_threshold,
        None,
        20000,
        0.999,
    )
    if homography is None or mask is None:
        raise AlignmentError("OpenCV could not consolidate the sample homographies.")
    if not np.isfinite(homography).all() or abs(float(np.linalg.det(homography))) < 1e-12:
        raise AlignmentError("The consolidated homography is singular or invalid.")
    homography /= homography[2, 2]
    inverse = np.linalg.inv(homography)
    inverse /= inverse[2, 2]
    inlier_mask = mask.reshape(-1).astype(bool)
    inlier_source = source[inlier_mask]
    inlier_target = target[inlier_mask]
    if len(inlier_source) < 4:
        raise AlignmentError("Fewer than four correspondences survived multi-sample consolidation.")
    forward = cv2.perspectiveTransform(inlier_source[:, None, :], homography)[:, 0, :]
    backward = cv2.perspectiveTransform(inlier_target[:, None, :], inverse)[:, 0, :]
    errors = (
        np.linalg.norm(forward - inlier_target, axis=1)
        + np.linalg.norm(backward - inlier_source, axis=1)
    ) / 2.0
    audience_coverage = _convex_coverage(inlier_source, *audience_size)
    ptz_coverage = _convex_coverage(inlier_target, *ptz_size)
    inlier_count = int(inlier_mask.sum())
    inlier_ratio = inlier_count / len(correspondences)
    median_error = float(np.median(errors))
    p95_error = float(np.percentile(errors, 95))
    consensus_settings = (
        replace(
            selected,
            minimum_audience_coverage=selected.minimum_audience_coverage * 0.5,
            minimum_ptz_coverage=selected.minimum_ptz_coverage * 0.5,
        )
        if len(results) >= 3
        else selected
    )
    reasons = _quality_reasons(
        consensus_settings,
        len(correspondences),
        inlier_count,
        inlier_ratio,
        median_error,
        audience_coverage,
        ptz_coverage,
    )
    confidence = _confidence_score(
        consensus_settings,
        inlier_count,
        inlier_ratio,
        median_error,
        audience_coverage,
        ptz_coverage,
    )
    consolidated_points = [
        PointCorrespondence(
            audience_x=float(audience_point[0]),
            audience_y=float(audience_point[1]),
            ptz_x=float(ptz_point[0]),
            ptz_y=float(ptz_point[1]),
            error_pixels=float(error),
        )
        for audience_point, ptz_point, error in zip(inlier_source, inlier_target, errors)
    ]
    consolidated_points.sort(key=lambda item: item.error_pixels)
    return AlignmentResult(
        status="accepted" if not reasons else "low_confidence",
        confidence_score=confidence,
        reasons=tuple(reasons),
        method=(
            "multi-sample consensus of SIFT reciprocal matches + "
            "USAC_MAGSAC homography"
        ),
        audience_size=audience_size,
        ptz_size=ptz_size,
        audience_keypoints=sum(item.audience_keypoints for item in results),
        ptz_keypoints=sum(item.ptz_keypoints for item in results),
        candidate_matches=len(correspondences),
        inliers=inlier_count,
        inlier_ratio=inlier_ratio,
        median_error_pixels=median_error,
        p95_error_pixels=p95_error,
        audience_coverage=audience_coverage,
        ptz_coverage=ptz_coverage,
        audience_to_ptz=_matrix_tuple(homography),
        ptz_to_audience=_matrix_tuple(inverse),
        correspondences=tuple(consolidated_points),
    )


def render_alignment_diagnostics(
    audience_bgr: np.ndarray,
    ptz_bgr: np.ndarray,
    result: AlignmentResult,
    output_dir: Path,
) -> dict[str, str]:
    """Write reviewable source, match, and overlay images without mutating inputs."""

    output_dir.mkdir(parents=True, exist_ok=True)
    audience = _validated_image(audience_bgr, "Audience")
    ptz = _validated_image(ptz_bgr, "PTZ")
    paths = {
        "audience_image": output_dir / "audience.jpg",
        "ptz_image": output_dir / "ptz.jpg",
        "inlier_matches_image": output_dir / "inlier_matches.jpg",
        "alignment_overlay_image": output_dir / "alignment_overlay.jpg",
    }
    _write_image(paths["audience_image"], audience)
    _write_image(paths["ptz_image"], ptz)
    _write_image(paths["inlier_matches_image"], _draw_correspondences(audience, ptz, result))
    _write_image(paths["alignment_overlay_image"], _draw_overlay(audience, ptz, result))
    return {key: path.name for key, path in paths.items()}


def _validated_image(image: np.ndarray, label: str) -> np.ndarray:
    if not isinstance(image, np.ndarray) or image.ndim not in {2, 3}:
        raise AlignmentError(f"{label} image must be a grayscale or BGR NumPy array.")
    if image.size == 0 or image.shape[0] < 32 or image.shape[1] < 32:
        raise AlignmentError(f"{label} image is empty or too small for calibration.")
    if image.dtype != np.uint8:
        image = np.clip(image, 0, 255).astype(np.uint8)
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    if image.shape[2] != 3:
        raise AlignmentError(f"{label} image must have 1, 3, or 4 channels.")
    return image


def _scaled_image(image: np.ndarray, maximum_width: int) -> tuple[np.ndarray, float]:
    width = image.shape[1]
    scale = min(1.0, maximum_width / width)
    if scale >= 1.0:
        return image, 1.0
    height = max(1, round(image.shape[0] * scale))
    return cv2.resize(image, (maximum_width, height), interpolation=cv2.INTER_AREA), scale


def _feature_image(image: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)


def _mutual_ratio_matches(
    audience_descriptors: np.ndarray,
    ptz_descriptors: np.ndarray,
    ratio: float,
) -> list[cv2.DMatch]:
    matcher = cv2.BFMatcher(cv2.NORM_L2, crossCheck=False)
    forward = _ratio_matches(matcher.knnMatch(audience_descriptors, ptz_descriptors, k=2), ratio)
    reverse = _ratio_matches(matcher.knnMatch(ptz_descriptors, audience_descriptors, k=2), ratio)
    reverse_pairs = {(item.trainIdx, item.queryIdx) for item in reverse}
    mutual = [item for item in forward if (item.queryIdx, item.trainIdx) in reverse_pairs]
    return sorted(mutual, key=lambda item: item.distance)


def _ratio_matches(neighbors: tuple[Any, ...] | list[Any], ratio: float) -> list[cv2.DMatch]:
    accepted: list[cv2.DMatch] = []
    for options in neighbors:
        if len(options) < 2:
            continue
        best, second = options[0], options[1]
        if best.distance < ratio * second.distance:
            accepted.append(best)
    return accepted


def _convex_coverage(points: np.ndarray, width: int, height: int) -> float:
    if len(points) < 3:
        return 0.0
    hull = cv2.convexHull(points.astype(np.float32))
    return float(cv2.contourArea(hull) / max(1.0, float(width * height)))


def _quality_reasons(
    settings: AlignmentSettings,
    matches: int,
    inliers: int,
    inlier_ratio: float,
    median_error: float,
    audience_coverage: float,
    ptz_coverage: float,
) -> list[str]:
    reasons: list[str] = []
    if matches < settings.minimum_matches:
        reasons.append(f"Only {matches} reciprocal matches; require {settings.minimum_matches}.")
    if inliers < settings.minimum_inliers:
        reasons.append(f"Only {inliers} geometric inliers; require {settings.minimum_inliers}.")
    if inlier_ratio < settings.minimum_inlier_ratio:
        reasons.append(
            f"Inlier ratio {inlier_ratio:.1%} is below {settings.minimum_inlier_ratio:.1%}."
        )
    if median_error > settings.maximum_median_error:
        reasons.append(
            f"Median symmetric error {median_error:.2f}px exceeds {settings.maximum_median_error:.2f}px."
        )
    if audience_coverage < settings.minimum_audience_coverage:
        reasons.append(
            f"Audience feature coverage {audience_coverage:.1%} is below {settings.minimum_audience_coverage:.1%}."
        )
    if ptz_coverage < settings.minimum_ptz_coverage:
        reasons.append(
            f"PTZ feature coverage {ptz_coverage:.1%} is below {settings.minimum_ptz_coverage:.1%}."
        )
    return reasons


def _confidence_score(
    settings: AlignmentSettings,
    inliers: int,
    inlier_ratio: float,
    median_error: float,
    audience_coverage: float,
    ptz_coverage: float,
) -> float:
    inlier_score = min(1.0, inliers / max(1.0, settings.minimum_inliers * 2.0))
    ratio_score = min(1.0, inlier_ratio / max(0.01, settings.minimum_inlier_ratio * 1.5))
    error_score = max(0.0, 1.0 - median_error / max(0.01, settings.maximum_median_error * 2.0))
    audience_score = min(
        1.0,
        audience_coverage / max(0.001, settings.minimum_audience_coverage * 2.0),
    )
    ptz_score = min(1.0, ptz_coverage / max(0.001, settings.minimum_ptz_coverage * 2.0))
    score = (
        0.25 * inlier_score
        + 0.20 * ratio_score
        + 0.25 * error_score
        + 0.15 * audience_score
        + 0.15 * ptz_score
    )
    return round(max(0.0, min(1.0, score)), 4)


def _matrix_tuple(matrix: np.ndarray) -> tuple[tuple[float, float, float], ...]:
    return tuple(tuple(float(value) for value in row) for row in matrix)


def _draw_correspondences(
    audience: np.ndarray,
    ptz: np.ndarray,
    result: AlignmentResult,
) -> np.ndarray:
    maximum_height = max(audience.shape[0], ptz.shape[0])
    canvas = np.zeros((maximum_height, audience.shape[1] + ptz.shape[1], 3), dtype=np.uint8)
    canvas[: audience.shape[0], : audience.shape[1]] = audience
    canvas[: ptz.shape[0], audience.shape[1] :] = ptz
    points = result.correspondences
    if len(points) > 250:
        indexes = np.linspace(0, len(points) - 1, 250, dtype=int)
        points = tuple(points[index] for index in indexes)
    for index, point in enumerate(points):
        color = _diagnostic_color(index)
        start = (round(point.audience_x), round(point.audience_y))
        end = (round(point.ptz_x) + audience.shape[1], round(point.ptz_y))
        cv2.line(canvas, start, end, color, 1, cv2.LINE_AA)
        cv2.circle(canvas, start, 3, color, -1, cv2.LINE_AA)
        cv2.circle(canvas, end, 3, color, -1, cv2.LINE_AA)
    _diagnostic_label(
        canvas,
        f"{result.inliers} inliers | median {result.median_error_pixels:.2f}px | {result.status}",
    )
    return canvas


def _draw_overlay(
    audience: np.ndarray,
    ptz: np.ndarray,
    result: AlignmentResult,
) -> np.ndarray:
    matrix = np.asarray(result.audience_to_ptz, dtype=np.float64)
    size = (ptz.shape[1], ptz.shape[0])
    warped = cv2.warpPerspective(audience, matrix, size)
    source_mask = np.full(audience.shape[:2], 255, dtype=np.uint8)
    valid = cv2.warpPerspective(source_mask, matrix, size) > 0
    overlay = ptz.copy()
    overlay[valid] = cv2.addWeighted(ptz[valid], 0.5, warped[valid], 0.5, 0.0)
    for point in result.correspondences[:250]:
        cv2.circle(
            overlay,
            (round(point.ptz_x), round(point.ptz_y)),
            3,
            (60, 230, 120),
            -1,
            cv2.LINE_AA,
        )
    _diagnostic_label(overlay, "PTZ + 50% warped Audience")
    return overlay


def _diagnostic_color(index: int) -> tuple[int, int, int]:
    hue = int((index * 37) % 180)
    pixel = np.uint8([[[hue, 210, 255]]])
    bgr = cv2.cvtColor(pixel, cv2.COLOR_HSV2BGR)[0, 0]
    return int(bgr[0]), int(bgr[1]), int(bgr[2])


def _diagnostic_label(image: np.ndarray, label: str) -> None:
    cv2.rectangle(image, (0, 0), (min(image.shape[1], 720), 38), (10, 12, 16), -1)
    cv2.putText(
        image,
        label,
        (12, 26),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.68,
        (240, 245, 250),
        2,
        cv2.LINE_AA,
    )


def _write_image(path: Path, image: np.ndarray) -> None:
    if not cv2.imwrite(str(path), image, [cv2.IMWRITE_JPEG_QUALITY, 94]):
        raise AlignmentError(f"OpenCV could not write diagnostic image {path}.")
