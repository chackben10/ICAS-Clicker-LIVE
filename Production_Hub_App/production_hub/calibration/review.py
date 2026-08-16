from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from production_hub.calibration.store import CalibrationRegistry


@dataclass(frozen=True, slots=True)
class CalibrationReviewMarker:
    marker_id: int
    audience_x: float
    audience_y: float
    ptz_x: float
    ptz_y: float
    reference_error_pixels: float
    repeatability: int = 1
    structure_score: float = 0.0
    stability: str = ""


@dataclass(frozen=True, slots=True)
class CalibrationReviewPose:
    index: int
    name: str
    status: str
    image_path: Path
    motor_position: dict[str, Any]
    markers: tuple[CalibrationReviewMarker, ...]
    link_inliers: int
    link_error_pixels: float


@dataclass(frozen=True, slots=True)
class CalibrationReviewData:
    map_path: Path
    created_at: str
    status: str
    audience_image_path: Path
    reference_calibration_path: Path
    audience_size: tuple[int, int]
    audience_markers: tuple[CalibrationReviewMarker, ...]
    excluded_markers: tuple[CalibrationReviewMarker, ...]
    poses: tuple[CalibrationReviewPose, ...]
    approval_status: str = "legacy_approved"
    approved_at: str = ""

    @property
    def marker_count(self) -> int:
        return len(self.audience_markers)

    @property
    def total_marker_count(self) -> int:
        return len(self.audience_markers) + len(self.excluded_markers)

    @property
    def approved(self) -> bool:
        return self.approval_status in {"approved", "legacy_approved"}


def load_latest_calibration_review(data_root: Path) -> CalibrationReviewData | None:
    registry = CalibrationRegistry(data_root)
    candidates = [
        *data_root.glob("calibration-sweeps/*/full_sync.json"),
        *data_root.glob("calibration/*/calibration.json"),
    ]
    for path in sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            payload = _load_json(path)
            if "poses" in payload:
                review = _load_sync_map(path, payload, registry)
            else:
                review = _load_single_alignment(path, payload)
            if review is not None:
                return review
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return None


def load_active_calibration_review(data_root: Path) -> CalibrationReviewData | None:
    """Load only a review-approved map suitable for runtime relocalization."""

    registry = CalibrationRegistry(data_root)
    active = registry.active_map_path()
    if active is not None:
        try:
            review = _load_sync_map(active, _load_json(active), registry)
            if review is not None and review.approved:
                return review
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    candidates = sorted(
        data_root.glob("calibration-sweeps/*/full_sync.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            payload = _load_json(path)
            curation = registry.curation(path, payload)
            if not curation.approved:
                continue
            review = _load_sync_map(path, payload, registry)
            if review is not None:
                return review
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return None


def _load_sync_map(
    path: Path,
    payload: dict[str, Any],
    registry: CalibrationRegistry,
) -> CalibrationReviewData | None:
    if payload.get("status") != "accepted":
        return None
    reference_path = _resolved_json_path(path.parent, payload["reference_calibration"])
    reference = _load_json(reference_path)
    alignment = reference["alignment"]
    if alignment.get("status") != "accepted":
        return None
    audience_size = _size(alignment["audience_size"])
    reference_ptz_size = _size(alignment["ptz_size"])
    artifacts = reference.get("artifacts", {})
    audience_image = reference_path.parent / str(
        artifacts.get("audience_image", "audience.jpg")
    )
    reference_ptz_image = reference_path.parent / str(
        artifacts.get("ptz_image", "ptz.jpg")
    )
    base_points = _unique_correspondences(alignment.get("correspondences", ()))
    structural_atlas = payload.get("structural_markers", ())
    if not base_points and not structural_atlas:
        return None

    curation = registry.curation(path, payload)
    excluded_ids = set(curation.excluded_marker_ids)
    if structural_atlas:
        all_audience_markers = tuple(
            _structural_review_marker(item, audience_size, reference_ptz_size)
            for item in structural_atlas
        )
    else:
        all_audience_markers = tuple(
            CalibrationReviewMarker(
                marker_id=index,
                audience_x=float(point["audience_x"]) / audience_size[0],
                audience_y=float(point["audience_y"]) / audience_size[1],
                ptz_x=float(point["ptz_x"]) / reference_ptz_size[0],
                ptz_y=float(point["ptz_y"]) / reference_ptz_size[1],
                reference_error_pixels=float(point.get("error_pixels", 0.0)),
            )
            for index, point in enumerate(base_points, start=1)
        )
    audience_markers = tuple(
        item for item in all_audience_markers if item.marker_id not in excluded_ids
    )
    excluded_markers = tuple(
        item for item in all_audience_markers if item.marker_id in excluded_ids
    )

    poses: list[CalibrationReviewPose] = []
    for pose_payload in payload.get("poses", ()):  # one reference plus moved poses
        if pose_payload.get("status") != "accepted":
            continue
        index = int(pose_payload.get("index", len(poses) + 1))
        link = pose_payload.get("ptz_link", {})
        pose_size = _size(
            pose_payload.get("ptz_size")
            or link.get("ptz_size")
            or reference_ptz_size
        )
        image_path_value = pose_payload.get("pose_ptz_image")
        image_path = (
            _resolved_json_path(path.parent, image_path_value)
            if image_path_value
            else reference_ptz_image
        )
        structural_markers = pose_payload.get("structural_markers", ())
        if structural_markers:
            markers = tuple(
                _structural_review_marker(item, audience_size, pose_size)
                for item in structural_markers
                if int(item.get("marker_id", 0)) not in excluded_ids
            )
        else:
            matrix = pose_payload.get(
                "reference_ptz_to_pose",
                ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
            )
            markers = tuple(
                CalibrationReviewMarker(
                    marker_id=marker_id,
                    audience_x=float(point["audience_x"]) / audience_size[0],
                    audience_y=float(point["audience_y"]) / audience_size[1],
                    ptz_x=mapped[0] / pose_size[0],
                    ptz_y=mapped[1] / pose_size[1],
                    reference_error_pixels=float(point.get("error_pixels", 0.0)),
                )
                for marker_id, point in enumerate(base_points, start=1)
                if (
                    mapped := _project(
                        matrix,
                        float(point["ptz_x"]),
                        float(point["ptz_y"]),
                    )
                )
                is not None
            )
        poses.append(
            CalibrationReviewPose(
                index=index,
                name=str(pose_payload.get("name", f"Pose {index}")),
                status="accepted",
                image_path=image_path,
                motor_position=dict(pose_payload.get("motor_position", {})),
                markers=markers,
                link_inliers=int(
                    link.get("inliers", alignment.get("inliers", len(base_points)))
                ),
                link_error_pixels=float(
                    link.get(
                        "median_error_pixels",
                        alignment.get("median_error_pixels", 0.0),
                    )
                ),
            )
        )
    if not poses:
        return None
    return CalibrationReviewData(
        map_path=path,
        created_at=str(payload.get("created_at", "")),
        status=str(payload.get("status", "accepted")),
        audience_image_path=audience_image,
        reference_calibration_path=reference_path,
        audience_size=(round(audience_size[0]), round(audience_size[1])),
        audience_markers=audience_markers,
        excluded_markers=excluded_markers,
        poses=tuple(poses),
        approval_status=curation.approval_status,
        approved_at=curation.approved_at,
    )


def _load_single_alignment(
    path: Path,
    payload: dict[str, Any],
) -> CalibrationReviewData | None:
    alignment = payload.get("alignment", {})
    if alignment.get("status") != "accepted":
        return None
    audience_size = _size(alignment["audience_size"])
    ptz_size = _size(alignment["ptz_size"])
    artifacts = payload.get("artifacts", {})
    audience_image = path.parent / str(artifacts.get("audience_image", "audience.jpg"))
    ptz_image = path.parent / str(artifacts.get("ptz_image", "ptz.jpg"))
    markers = tuple(
        CalibrationReviewMarker(
            marker_id=index,
            audience_x=float(point["audience_x"]) / audience_size[0],
            audience_y=float(point["audience_y"]) / audience_size[1],
            ptz_x=float(point["ptz_x"]) / ptz_size[0],
            ptz_y=float(point["ptz_y"]) / ptz_size[1],
            reference_error_pixels=float(point.get("error_pixels", 0.0)),
        )
        for index, point in enumerate(
            _unique_correspondences(alignment.get("correspondences", ())),
            start=1,
        )
    )
    if not markers:
        return None
    pose = CalibrationReviewPose(
        index=1,
        name="Reference",
        status="accepted",
        image_path=ptz_image,
        motor_position={},
        markers=markers,
        link_inliers=int(alignment.get("inliers", len(markers))),
        link_error_pixels=float(alignment.get("median_error_pixels", 0.0)),
    )
    return CalibrationReviewData(
        map_path=path,
        created_at=str(payload.get("created_at", "")),
        status="accepted",
        audience_image_path=audience_image,
        reference_calibration_path=path,
        audience_size=(round(audience_size[0]), round(audience_size[1])),
        audience_markers=markers,
        excluded_markers=(),
        poses=(pose,),
    )


def _structural_review_marker(
    item: dict[str, Any],
    audience_size: tuple[float, float],
    ptz_size: tuple[float, float],
) -> CalibrationReviewMarker:
    return CalibrationReviewMarker(
        marker_id=int(item["marker_id"]),
        audience_x=float(item["audience_x"]) / audience_size[0],
        audience_y=float(item["audience_y"]) / audience_size[1],
        ptz_x=float(item.get("ptz_x", 0.0)) / ptz_size[0],
        ptz_y=float(item.get("ptz_y", 0.0)) / ptz_size[1],
        reference_error_pixels=float(item.get("error_pixels", 0.0)),
        repeatability=max(1, int(item.get("repeatability", 1))),
        structure_score=float(item.get("structure_score", 0.0)),
        stability=str(item.get("stability", "")),
    )


def _project(matrix: Any, x: float, y: float) -> tuple[float, float] | None:
    rows = [[float(value) for value in row] for row in matrix]
    if len(rows) != 3 or any(len(row) != 3 for row in rows):
        raise ValueError("Calibration homography must be 3x3.")
    denominator = rows[2][0] * x + rows[2][1] * y + rows[2][2]
    if abs(denominator) < 1e-9:
        return None
    return (
        (rows[0][0] * x + rows[0][1] * y + rows[0][2]) / denominator,
        (rows[1][0] * x + rows[1][1] * y + rows[1][2]) / denominator,
    )


def _unique_correspondences(points: Any) -> tuple[dict[str, Any], ...]:
    """Collapse temporal re-observations into unique physical review markers."""

    selected: list[dict[str, Any]] = []
    ordered = sorted(
        (dict(point) for point in points),
        key=lambda point: float(point.get("error_pixels", 0.0)),
    )
    for point in ordered:
        audience_x = float(point["audience_x"])
        audience_y = float(point["audience_y"])
        ptz_x = float(point["ptz_x"])
        ptz_y = float(point["ptz_y"])
        duplicate = any(
            (audience_x - float(existing["audience_x"])) ** 2
            + (audience_y - float(existing["audience_y"])) ** 2
            <= 12.0**2
            and (ptz_x - float(existing["ptz_x"])) ** 2
            + (ptz_y - float(existing["ptz_y"])) ** 2
            <= 12.0**2
            for existing in selected
        )
        if not duplicate:
            selected.append(point)
    return tuple(selected)


def _size(value: Any) -> tuple[float, float]:
    width, height = value
    width = float(width)
    height = float(height)
    if width <= 0 or height <= 0:
        raise ValueError("Calibration image size must be positive.")
    return width, height


def _resolved_json_path(parent: Path, value: Any) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else (parent / path).resolve()


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return payload
