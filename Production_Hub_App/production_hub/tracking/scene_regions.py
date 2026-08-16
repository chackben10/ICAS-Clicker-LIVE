from __future__ import annotations

from typing import Any, Iterable, Sequence

import numpy as np

from production_hub.core.config.models import CameraSceneRegion, SceneRegionPoint


def suggested_church_scene_regions() -> list[CameraSceneRegion]:
    """Candidate polygons fitted to the current fixed Audience camera view."""

    definitions = [
        (
            "suggested-full-stage-v1",
            "Full Stage",
            "stage",
            "#25d0c8",
            [(0.205, 0.260), (0.275, 0.115), (0.735, 0.115), (0.795, 0.270), (0.705, 0.315), (0.300, 0.315)],
        ),
        (
            "suggested-main-stage-deck-v1",
            "Main Stage Deck",
            "stage",
            "#16a6c7",
            [(0.220, 0.225), (0.300, 0.155), (0.710, 0.155), (0.775, 0.255), (0.695, 0.310), (0.305, 0.310)],
        ),
        (
            "suggested-front-stage-v1",
            "Front of Stage",
            "front_stage",
            "#ffb020",
            [(0.250, 0.245), (0.745, 0.245), (0.700, 0.330), (0.300, 0.330)],
        ),
        (
            "suggested-altar-v1",
            "Altar Area",
            "altar",
            "#d467ff",
            [(0.405, 0.175), (0.590, 0.175), (0.585, 0.355), (0.415, 0.355)],
        ),
        (
            "suggested-podium-v1",
            "Podium Zone",
            "podium",
            "#ef5da8",
            [(0.425, 0.065), (0.555, 0.065), (0.555, 0.260), (0.425, 0.260)],
        ),
        (
            "suggested-center-steps-v1",
            "Center Steps",
            "front_stage",
            "#f97316",
            [(0.390, 0.275), (0.610, 0.275), (0.630, 0.405), (0.370, 0.405)],
        ),
        (
            "suggested-left-stage-v1",
            "Left Stage",
            "custom",
            "#5b8def",
            [(0.205, 0.255), (0.275, 0.115), (0.475, 0.115), (0.475, 0.310), (0.300, 0.315)],
        ),
        (
            "suggested-right-stage-v1",
            "Right Stage",
            "custom",
            "#8b5cf6",
            [(0.525, 0.115), (0.735, 0.115), (0.795, 0.270), (0.705, 0.315), (0.525, 0.310)],
        ),
        (
            "suggested-rear-stage-v1",
            "Rear Musicians",
            "custom",
            "#22c55e",
            [(0.350, 0.030), (0.700, 0.030), (0.725, 0.185), (0.335, 0.185)],
        ),
        (
            "suggested-stage-altar-v1",
            "Stage + Altar",
            "custom",
            "#eab308",
            [(0.205, 0.260), (0.275, 0.115), (0.735, 0.115), (0.795, 0.270), (0.705, 0.315), (0.595, 0.315), (0.585, 0.365), (0.415, 0.365), (0.405, 0.315), (0.300, 0.315)],
        ),
    ]
    return [
        CameraSceneRegion(
            id=region_id,
            name=name,
            kind=kind,
            source="audience",
            color=color,
            enabled=True,
            suggested=True,
            points=[SceneRegionPoint(x, y) for x, y in points],
        )
        for region_id, name, kind, color, points in definitions
    ]


def structural_plane_regions(payload: dict[str, Any]) -> list[CameraSceneRegion]:
    """Convert a reviewed cross-camera plane artifact into persisted scene regions."""

    calibration_reference = str(payload.get("calibration_reference", "")).strip()
    generated_at = str(payload.get("created_at", "")).strip()
    regions: list[CameraSceneRegion] = []
    for index, item in enumerate(payload.get("planes", ()), start=1):
        if not isinstance(item, dict):
            continue
        polygon = item.get("polygon", ())
        if not isinstance(polygon, (list, tuple)) or len(polygon) < 3:
            continue
        try:
            points = [
                SceneRegionPoint(float(point[0]), float(point[1]))
                for point in polygon
                if isinstance(point, (list, tuple)) and len(point) >= 2
            ]
            if len(points) < 3:
                continue
            poses = [str(value) for value in item.get("supporting_poses", ())]
            regions.append(
                CameraSceneRegion(
                    id=str(item.get("id") or f"generated-structural-plane-{index:02d}"),
                    name=str(item.get("name") or f"Structural Plane {index:02d}"),
                    points=points,
                    kind="custom",
                    source="audience",
                    color=str(item.get("color") or "#7c5cff"),
                    enabled=True,
                    suggested=True,
                    coordinate_space="calibration_reference",
                    calibration_reference=calibration_reference,
                    generation_method="cross_camera_structural_plane",
                    generated_at=generated_at,
                    supporting_poses=poses,
                    support_points=int(item.get("support_points", 0)),
                    confidence=float(item.get("confidence", 0.0)),
                )
            )
        except (TypeError, ValueError):
            continue
    return regions


def transform_scene_regions(
    regions: Iterable[CameraSceneRegion],
    reference_to_live: Sequence[Sequence[float]],
    reference_size: tuple[int, int],
    live_size: tuple[int, int],
) -> tuple[CameraSceneRegion, ...]:
    """Project calibration-reference polygons into the current Audience frame."""

    matrix = np.asarray(reference_to_live, dtype=np.float64)
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        raise ValueError("Scene-plane transform must be a finite 3x3 matrix.")
    if min(*reference_size, *live_size) <= 0:
        raise ValueError("Scene-plane image sizes must be positive.")
    selected: list[CameraSceneRegion] = []
    for region in regions:
        points: list[SceneRegionPoint] = []
        for point in region.points:
            projected = matrix @ np.asarray(
                [point.x * reference_size[0], point.y * reference_size[1], 1.0],
                dtype=np.float64,
            )
            if abs(float(projected[2])) < 1e-9:
                points = []
                break
            points.append(
                SceneRegionPoint(
                    float(projected[0] / projected[2]) / live_size[0],
                    float(projected[1] / projected[2]) / live_size[1],
                )
            )
        if len(points) < 3:
            continue
        copy = CameraSceneRegion.from_dict(region.to_dict())
        copy.points = points
        copy.coordinate_space = "live_audience"
        selected.append(copy)
    return tuple(selected)
