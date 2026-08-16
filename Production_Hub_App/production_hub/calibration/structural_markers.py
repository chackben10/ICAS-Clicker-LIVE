from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import cv2
import numpy as np


@dataclass(frozen=True, slots=True)
class StructuralFeatureIndex:
    image_size: tuple[int, int]
    points: np.ndarray
    descriptors: np.ndarray
    structure_scores: np.ndarray


def select_structural_markers(
    correspondences: Iterable[dict[str, Any]],
    reference_audience_bgr: np.ndarray,
    *,
    audience_to_reference: Sequence[Sequence[float]] | None = None,
    maximum_markers: int = 48,
    grid_columns: int = 8,
    grid_rows: int = 6,
) -> list[dict[str, Any]]:
    """Select repeatable, corner-like matches with balanced image coverage.

    Multi-sample calibration contains repeated observations of the same SIFT
    landmark. Clustering those observations rejects transient features, while
    a grid quota prevents one detailed object from consuming the marker set.
    """

    image = _validated_image(reference_audience_bgr)
    height, width = image.shape[:2]
    transform = _homography(audience_to_reference)
    observations: list[dict[str, float]] = []
    for raw in correspondences:
        try:
            source_x = float(raw["audience_x"])
            source_y = float(raw["audience_y"])
            ptz_x = float(raw["ptz_x"])
            ptz_y = float(raw["ptz_y"])
            error = float(raw.get("error_pixels", 0.0))
        except (KeyError, TypeError, ValueError):
            continue
        reference_point = _project(transform, source_x, source_y)
        if reference_point is None:
            continue
        reference_x, reference_y = reference_point
        if not (0 <= reference_x < width and 0 <= reference_y < height):
            continue
        observations.append(
            {
                "audience_x": reference_x,
                "audience_y": reference_y,
                "ptz_x": ptz_x,
                "ptz_y": ptz_y,
                "error_pixels": max(0.0, error),
            }
        )

    clusters: list[list[dict[str, float]]] = []
    for point in sorted(observations, key=lambda item: item["error_pixels"]):
        selected = next(
            (
                cluster
                for cluster in clusters
                if _distance(point, cluster[0], "audience") <= 10.0
                and _distance(point, cluster[0], "ptz") <= 12.0
            ),
            None,
        )
        if selected is None:
            clusters.append([point])
        else:
            selected.append(point)

    structural_response = _structural_response(image)
    candidates: list[dict[str, Any]] = []
    for cluster in clusters:
        audience_x = float(np.median([item["audience_x"] for item in cluster]))
        audience_y = float(np.median([item["audience_y"] for item in cluster]))
        ptz_x = float(np.median([item["ptz_x"] for item in cluster]))
        ptz_y = float(np.median([item["ptz_y"] for item in cluster]))
        error = float(np.median([item["error_pixels"] for item in cluster]))
        pixel_x = min(width - 1, max(0, round(audience_x)))
        pixel_y = min(height - 1, max(0, round(audience_y)))
        structure = float(structural_response[pixel_y, pixel_x])
        repeatability = len(cluster)
        candidates.append(
            {
                "audience_x": audience_x,
                "audience_y": audience_y,
                "ptz_x": ptz_x,
                "ptz_y": ptz_y,
                "error_pixels": error,
                "repeatability": repeatability,
                "structure_score": round(structure, 5),
                "stability": (
                    "temporal_repeat" if repeatability >= 2 else "single_observation"
                ),
                "selection_score": (
                    repeatability * 3.0 + structure * 5.0 - min(error, 5.0) * 0.35
                ),
            }
        )

    repeated = [item for item in candidates if item["repeatability"] >= 2]
    # Prefer only temporally repeated landmarks when enough survived. Sparse
    # views retain a few single observations rather than becoming unusable.
    pool = repeated if len(repeated) >= 8 else candidates
    line_supported = [item for item in pool if item["structure_score"] >= 0.12]
    if len(line_supported) >= 8:
        pool = line_supported
    cells: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for item in pool:
        column = min(grid_columns - 1, int(item["audience_x"] / width * grid_columns))
        row = min(grid_rows - 1, int(item["audience_y"] / height * grid_rows))
        cells[(column, row)].append(item)

    per_cell = max(1, min(5, maximum_markers // max(1, len(cells))))
    selected: list[dict[str, Any]] = []
    for key in sorted(cells, key=lambda item: (item[1], item[0])):
        ranked = sorted(
            cells[key],
            key=lambda item: (
                -float(item["selection_score"]),
                float(item["error_pixels"]),
            ),
        )
        selected.extend(ranked[:per_cell])
    if len(selected) < maximum_markers:
        selected_ids = {id(item) for item in selected}
        remaining = sorted(
            (item for item in pool if id(item) not in selected_ids),
            key=lambda item: -float(item["selection_score"]),
        )
        selected.extend(remaining[: maximum_markers - len(selected)])
    selected = selected[:maximum_markers]
    for item in selected:
        item.pop("selection_score", None)
    return sorted(selected, key=lambda item: (item["audience_y"], item["audience_x"]))


def assign_global_marker_ids(
    poses: list[dict[str, Any]],
    *,
    merge_distance_pixels: float = 12.0,
) -> int:
    """Assign one stable ID to re-observations of a physical Audience landmark."""

    atlas: list[dict[str, float | int]] = []
    all_markers = [
        marker
        for pose in poses
        for marker in pose.get("structural_markers", [])
        if isinstance(marker, dict)
    ]
    for marker in sorted(
        all_markers,
        key=lambda item: (float(item["audience_y"]), float(item["audience_x"])),
    ):
        match = next(
            (
                item
                for item in atlas
                if (
                    (float(marker["audience_x"]) - float(item["audience_x"])) ** 2
                    + (float(marker["audience_y"]) - float(item["audience_y"])) ** 2
                )
                <= merge_distance_pixels**2
            ),
            None,
        )
        if match is None:
            match = {
                "marker_id": len(atlas) + 1,
                "audience_x": float(marker["audience_x"]),
                "audience_y": float(marker["audience_y"]),
            }
            atlas.append(match)
        marker["marker_id"] = int(match["marker_id"])
    return len(atlas)


def build_structural_feature_index(
    reference_audience_bgr: np.ndarray,
    *,
    maximum_features: int = 16000,
) -> StructuralFeatureIndex:
    """Precompute Audience descriptors supported by long structural edges."""

    image = _validated_image(reference_audience_bgr)
    response = _structural_response(image)
    mask = np.uint8(response >= 0.08) * 255
    mask = cv2.dilate(mask, np.ones((7, 7), dtype=np.uint8), iterations=1)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    detector = cv2.SIFT_create(
        nfeatures=max(1000, min(30000, int(maximum_features))),
        contrastThreshold=0.018,
        edgeThreshold=12,
    )
    keypoints, descriptors = detector.detectAndCompute(gray, mask)
    if descriptors is None or not keypoints:
        raise ValueError("Audience image has no structural SIFT features.")
    points = np.float32([item.pt for item in keypoints])
    height, width = response.shape
    scores = np.float32(
        [
            response[
                min(height - 1, max(0, round(float(y)))),
                min(width - 1, max(0, round(float(x)))),
            ]
            for x, y in points
        ]
    )
    return StructuralFeatureIndex(
        image_size=(image.shape[1], image.shape[0]),
        points=points,
        descriptors=np.asarray(descriptors, dtype=np.float32),
        structure_scores=scores,
    )


def guided_structural_markers(
    audience_index: StructuralFeatureIndex,
    ptz_bgr: np.ndarray,
    audience_to_ptz: Sequence[Sequence[float]],
    *,
    maximum_markers: int = 64,
    search_radius_pixels: float = 90.0,
) -> list[dict[str, Any]]:
    """Resolve repetitive structure by matching only near calibrated projections."""

    ptz = _validated_image(ptz_bgr)
    gray = cv2.cvtColor(ptz, cv2.COLOR_BGR2GRAY)
    gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    detector = cv2.SIFT_create(
        nfeatures=16000,
        contrastThreshold=0.018,
        edgeThreshold=12,
    )
    keypoints, descriptors = detector.detectAndCompute(gray, None)
    if descriptors is None or not keypoints:
        return []
    ptz_points = np.float32([item.pt for item in keypoints])
    ptz_descriptors = np.asarray(descriptors, dtype=np.float32)
    matrix = _homography(audience_to_ptz)
    source = audience_index.points[:, None, :]
    projected = cv2.perspectiveTransform(source, matrix)[:, 0, :]
    ptz_height, ptz_width = ptz.shape[:2]
    cell_size = max(48, round(search_radius_pixels))
    cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, (x, y) in enumerate(ptz_points):
        cells[(int(x) // cell_size, int(y) // cell_size)].append(index)

    matches: list[dict[str, Any]] = []
    for audience_index_value, expected in enumerate(projected):
        expected_x, expected_y = (float(expected[0]), float(expected[1]))
        if not (8 <= expected_x < ptz_width - 8 and 8 <= expected_y < ptz_height - 8):
            continue
        cell_x = int(expected_x) // cell_size
        cell_y = int(expected_y) // cell_size
        candidate_indices = [
            item
            for x_offset in (-1, 0, 1)
            for y_offset in (-1, 0, 1)
            for item in cells.get((cell_x + x_offset, cell_y + y_offset), ())
        ]
        if len(candidate_indices) < 2:
            continue
        candidate_points = ptz_points[candidate_indices]
        spatial_errors = np.linalg.norm(candidate_points - expected, axis=1)
        nearby_mask = spatial_errors <= search_radius_pixels
        if int(nearby_mask.sum()) < 2:
            continue
        nearby_indices = np.asarray(candidate_indices, dtype=np.int32)[nearby_mask]
        nearby_errors = spatial_errors[nearby_mask]
        descriptor = audience_index.descriptors[audience_index_value]
        distances = np.linalg.norm(ptz_descriptors[nearby_indices] - descriptor, axis=1)
        order = np.argsort(distances)
        best_local, second_local = int(order[0]), int(order[1])
        if float(distances[best_local]) >= 0.84 * max(1e-6, float(distances[second_local])):
            continue
        target_index = int(nearby_indices[best_local])
        spatial_error = float(nearby_errors[best_local])
        source_x, source_y = audience_index.points[audience_index_value]
        target_x, target_y = ptz_points[target_index]
        structure_score = float(audience_index.structure_scores[audience_index_value])
        matches.append(
            {
                "audience_x": float(source_x),
                "audience_y": float(source_y),
                "ptz_x": float(target_x),
                "ptz_y": float(target_y),
                "error_pixels": spatial_error,
                "repeatability": 1,
                "structure_score": round(structure_score, 5),
                "stability": "guided_structural_match",
                "descriptor_distance": round(float(distances[best_local]), 3),
                "selection_score": (
                    structure_score * 5.0
                    + max(0.0, 1.0 - spatial_error / search_radius_pixels) * 2.0
                    + max(
                        0.0,
                        1.0
                        - float(distances[best_local])
                        / max(1.0, float(distances[second_local])),
                    )
                ),
                "target_index": target_index,
            }
        )

    # A PTZ feature can only represent one Audience landmark.
    unique_targets: dict[int, dict[str, Any]] = {}
    for item in sorted(matches, key=lambda value: -float(value["selection_score"])):
        unique_targets.setdefault(int(item["target_index"]), item)
    pool = list(unique_targets.values())
    selected = _balanced_select(
        pool,
        audience_index.image_size[0],
        audience_index.image_size[1],
        maximum_markers,
        grid_columns=8,
        grid_rows=6,
    )
    for item in selected:
        item.pop("selection_score", None)
        item.pop("target_index", None)
    return selected


def merge_structural_markers(
    direct: Iterable[dict[str, Any]],
    guided: Iterable[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    maximum_markers: int = 64,
) -> list[dict[str, Any]]:
    """Merge verified temporal matches with guided structural coverage."""

    combined: list[dict[str, Any]] = []
    for marker in [*direct, *guided]:
        duplicate = any(
            _distance(marker, existing, "audience") <= 10.0
            and _distance(marker, existing, "ptz") <= 14.0
            for existing in combined
        )
        if duplicate:
            continue
        selected = dict(marker)
        selected["selection_score"] = (
            int(selected.get("repeatability", 1)) * 3.0
            + float(selected.get("structure_score", 0.0)) * 5.0
            - min(float(selected.get("error_pixels", 0.0)), 90.0) * 0.02
        )
        combined.append(selected)
    selected = _balanced_select(
        combined,
        image_size[0],
        image_size[1],
        maximum_markers,
        grid_columns=8,
        grid_rows=6,
    )
    for item in selected:
        item.pop("selection_score", None)
    return selected


def marker_atlas(poses: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the best observation for every globally assigned marker ID."""

    selected: dict[int, dict[str, Any]] = {}
    for pose in poses:
        for marker in pose.get("structural_markers", []):
            marker_id = int(marker.get("marker_id", 0))
            if marker_id <= 0:
                continue
            existing = selected.get(marker_id)
            rank = (
                int(marker.get("repeatability", 1)),
                -float(marker.get("error_pixels", 0.0)),
            )
            existing_rank = (
                int(existing.get("repeatability", 1)),
                -float(existing.get("error_pixels", 0.0)),
            ) if existing is not None else (-1, float("-inf"))
            if existing is None or rank > existing_rank:
                selected[marker_id] = dict(marker)
    return [selected[key] for key in sorted(selected)]


def prune_global_marker_atlas(
    poses: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    maximum_markers: int = 200,
) -> int:
    """Keep a spatially balanced global atlas and compact marker IDs."""

    atlas = marker_atlas(poses)
    for marker in atlas:
        stability_bonus = (
            2.0 if marker.get("stability") == "temporal_repeat" else 0.0
        )
        marker["selection_score"] = (
            int(marker.get("repeatability", 1)) * 1.5
            + float(marker.get("structure_score", 0.0)) * 8.0
            + stability_bonus
            - min(float(marker.get("error_pixels", 0.0)), 90.0) * 0.025
        )
    selected = _balanced_select(
        atlas,
        image_size[0],
        image_size[1],
        maximum_markers,
        grid_columns=8,
        grid_rows=6,
    )
    selected_ids = {int(item["marker_id"]) for item in selected}
    ordered_ids = {
        int(item["marker_id"]): index
        for index, item in enumerate(
            sorted(
                selected,
                key=lambda item: (
                    float(item["audience_y"]),
                    float(item["audience_x"]),
                ),
            ),
            start=1,
        )
    }
    for pose in poses:
        retained = [
            marker
            for marker in pose.get("structural_markers", [])
            if int(marker.get("marker_id", 0)) in selected_ids
        ]
        for marker in retained:
            marker["marker_id"] = ordered_ids[int(marker["marker_id"])]
        pose["structural_markers"] = retained
    return len(selected_ids)


def _validated_image(image: np.ndarray) -> np.ndarray:
    if not isinstance(image, np.ndarray) or image.ndim not in {2, 3} or image.size == 0:
        raise ValueError("Reference Audience image is invalid.")
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    return image[:, :, :3]


def _homography(value: Sequence[Sequence[float]] | None) -> np.ndarray:
    if value is None:
        return np.eye(3, dtype=np.float64)
    matrix = np.asarray(value, dtype=np.float64)
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        raise ValueError("Structural marker transform must be a finite 3x3 matrix.")
    return matrix


def _project(matrix: np.ndarray, x: float, y: float) -> tuple[float, float] | None:
    projected = matrix @ np.asarray([x, y, 1.0], dtype=np.float64)
    if abs(float(projected[2])) < 1e-9:
        return None
    return float(projected[0] / projected[2]), float(projected[1] / projected[2])


def _distance(first: dict[str, float], second: dict[str, float], prefix: str) -> float:
    return float(
        np.hypot(
            first[f"{prefix}_x"] - second[f"{prefix}_x"],
            first[f"{prefix}_y"] - second[f"{prefix}_y"],
        )
    )


def _structural_response(image: np.ndarray) -> np.ndarray:
    """Score corners supported by architectural-scale horizontal/vertical edges.

    Standalone corner detectors strongly prefer books, music equipment, and
    other small objects that happen to be stationary during one sweep.  A
    calibration landmark must instead sit on a long, dominant scene line.
    This retains door/window frames, stage and step edges, wall boundaries,
    and the long members/end caps of fixed pews while suppressing most props.
    """

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    corner = cv2.cornerMinEigenVal(gray, blockSize=5, ksize=3)
    corner_maximum = float(corner.max())
    if corner_maximum > 1e-12:
        corner = corner / corner_maximum

    edges = cv2.Canny(gray, 60, 150, apertureSize=3)
    minimum_length = max(72, round(min(image.shape[:2]) * 0.085))
    lines = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 180.0,
        threshold=45,
        minLineLength=minimum_length,
        maxLineGap=18,
    )
    line_map = np.zeros(gray.shape, dtype=np.float32)
    if lines is not None:
        diagonal = float(np.hypot(image.shape[1], image.shape[0]))
        for raw in lines[:, 0, :]:
            x1, y1, x2, y2 = (int(value) for value in raw)
            length = float(np.hypot(x2 - x1, y2 - y1))
            angle = abs(float(np.arctan2(y2 - y1, x2 - x1))) % (np.pi / 2.0)
            axis_delta = min(angle, (np.pi / 2.0) - angle)
            if axis_delta > np.deg2rad(16.0):
                continue
            weight = min(1.0, length / max(1.0, diagonal * 0.18))
            cv2.line(line_map, (x1, y1), (x2, y2), weight, 7, cv2.LINE_AA)
            cv2.circle(line_map, (x1, y1), 12, min(1.0, weight + 0.18), -1, cv2.LINE_AA)
            cv2.circle(line_map, (x2, y2), 12, min(1.0, weight + 0.18), -1, cv2.LINE_AA)
    line_map = cv2.GaussianBlur(line_map, (0, 0), sigmaX=2.5, sigmaY=2.5)
    maximum = float(line_map.max())
    if maximum > 1e-12:
        line_map = line_map / maximum
    # Line support is mandatory. Corner strength only ranks features already
    # attached to a dominant scene edge; it can never admit a small object by
    # itself.
    return line_map * (0.55 + np.sqrt(np.clip(corner, 0.0, 1.0)) * 0.45)


def _balanced_select(
    pool: list[dict[str, Any]],
    width: int,
    height: int,
    maximum_markers: int,
    *,
    grid_columns: int,
    grid_rows: int,
) -> list[dict[str, Any]]:
    cells: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for item in pool:
        column = min(
            grid_columns - 1,
            max(0, int(float(item["audience_x"]) / width * grid_columns)),
        )
        row = min(
            grid_rows - 1,
            max(0, int(float(item["audience_y"]) / height * grid_rows)),
        )
        cells[(column, row)].append(item)
    per_cell = max(1, min(6, maximum_markers // max(1, len(cells))))
    selected: list[dict[str, Any]] = []
    for key in sorted(cells, key=lambda item: (item[1], item[0])):
        selected.extend(
            sorted(
                cells[key],
                key=lambda item: (
                    -float(item.get("selection_score", 0.0)),
                    float(item.get("error_pixels", 0.0)),
                ),
            )[:per_cell]
        )
    if len(selected) < maximum_markers:
        selected_ids = {id(item) for item in selected}
        remaining = sorted(
            (item for item in pool if id(item) not in selected_ids),
            key=lambda item: -float(item.get("selection_score", 0.0)),
        )
        selected.extend(remaining[: maximum_markers - len(selected)])
    return sorted(
        selected[:maximum_markers],
        key=lambda item: (float(item["audience_y"]), float(item["audience_x"])),
    )
