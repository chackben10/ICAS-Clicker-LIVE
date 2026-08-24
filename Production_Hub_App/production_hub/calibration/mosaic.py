from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import cv2
import numpy as np

from production_hub.calibration.review import CalibrationReviewData


MOSAIC_SCHEMA_VERSION = 7


@dataclass(frozen=True, slots=True)
class AudienceWarpMesh:
    """Smooth mapping from the saved Audience frame into PTZ panorama space."""

    source_size: tuple[int, int]
    target_size: tuple[int, int]
    source_points: tuple[tuple[float, float], ...]
    target_points: tuple[tuple[float, float], ...]
    triangles: tuple[tuple[int, int, int], ...]
    control_points: int = 0
    grid_shape: tuple[int, int] = (0, 0)
    interpolation: str = "piecewise_affine"
    inverse_grid_shape: tuple[int, int] = (0, 0)
    inverse_source_points: tuple[tuple[float, float], ...] = ()
    inverse_validity: tuple[float, ...] = ()

    def reference_maps(
        self,
        mosaic: CalibrationMosaic,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Return target-canvas to Audience-reference remap arrays.

        These arrays are built once when the Click-to-Frame view loads. Each
        live frame then only needs the small relocalization homography and one
        OpenCV remap, rather than repainting hundreds of image fragments.
        """

        if (
            self.interpolation == "multi_pose_inverse_bicubic"
            and self.inverse_grid_shape[0] >= 4
            and self.inverse_grid_shape[1] >= 4
            and self.inverse_grid_shape[0] * self.inverse_grid_shape[1]
            == len(self.inverse_source_points)
            == len(self.inverse_validity)
        ):
            return self._multi_pose_reference_maps(mosaic)
        if (
            self.interpolation == "thin_plate_bicubic"
            and self.grid_shape[0] >= 4
            and self.grid_shape[1] >= 4
            and self.grid_shape[0] * self.grid_shape[1] == len(self.source_points)
        ):
            return self._smooth_reference_maps(mosaic)
        return self._piecewise_reference_maps(mosaic)

    def _multi_pose_reference_maps(
        self,
        mosaic: CalibrationMosaic,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Expand the calibrated per-PTZ-view inverse field smoothly.

        A panorama is not one flat camera plane: each patch came from a
        different pan/tilt/zoom pose. Camera Sync already measures a direct
        Audience-to-PTZ transform for every accepted pose, so the saved grid
        blends those exact inverse transforms with the same feathering used by
        the panorama. Bicubic evaluation keeps the result curved and seamless
        without doing any feature work while the operator is using the app.
        """

        columns, rows = self.inverse_grid_shape
        source = np.asarray(
            self.inverse_source_points,
            dtype=np.float32,
        ).reshape(rows, columns, 2)
        support = np.asarray(
            self.inverse_validity,
            dtype=np.float32,
        ).reshape(rows, columns)
        canvas_y, canvas_x = np.indices(
            (mosaic.height, mosaic.width),
            dtype=np.float32,
        )
        grid_x = (
            canvas_x / max(1.0, float(mosaic.width - 1)) * (columns - 1)
        ).astype(np.float32)
        grid_y = (
            canvas_y / max(1.0, float(mosaic.height - 1)) * (rows - 1)
        ).astype(np.float32)
        source_x = cv2.remap(
            source[..., 0],
            grid_x,
            grid_y,
            cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )
        source_y = cv2.remap(
            source[..., 1],
            grid_x,
            grid_y,
            cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )
        interpolated_support = cv2.remap(
            support,
            grid_x,
            grid_y,
            cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
        )
        valid = (
            (interpolated_support > 0.08)
            & np.isfinite(source_x)
            & np.isfinite(source_y)
            & (source_x >= 0.0)
            & (source_y >= 0.0)
            & (source_x < self.source_size[0] - 1)
            & (source_y < self.source_size[1] - 1)
        )
        return (
            np.where(valid, source_x, -1.0).astype(np.float32),
            np.where(valid, source_y, -1.0).astype(np.float32),
            valid.astype(np.uint8) * 255,
        )

    def _smooth_reference_maps(
        self,
        mosaic: CalibrationMosaic,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Invert a curved source-to-panorama field into OpenCV remap arrays.

        The calibration stores a regular grid whose offsets were fitted with
        a thin-plate spline. Runtime inversion starts from the global camera
        homography, samples that displacement grid bicubically, and refines
        the inverse three times. This keeps walls, arches, and pew lines
        smoothly curved instead of introducing straight triangle facets.
        """

        columns, rows = self.grid_shape
        source = np.asarray(self.source_points, dtype=np.float64).reshape(
            rows,
            columns,
            2,
        )
        targets = np.asarray(self.target_points, dtype=np.float64).reshape(
            rows,
            columns,
            2,
        )
        homography = np.asarray(
            mosaic.audience_to_reference_ptz,
            dtype=np.float64,
        )
        projected = _project_arrays(
            homography,
            source[..., 0],
            source[..., 1],
        )
        residual_x = (targets[..., 0] - projected[0]).astype(np.float32)
        residual_y = (targets[..., 1] - projected[1]).astype(np.float32)

        canvas_y, canvas_x = np.indices(
            (mosaic.height, mosaic.width),
            dtype=np.float32,
        )
        target_x = (
            mosaic.left
            + canvas_x / max(1.0, float(mosaic.width)) * mosaic.reference_width
        ) * mosaic.reference_ptz_size[0]
        target_y = (
            mosaic.top
            + canvas_y / max(1.0, float(mosaic.height)) * mosaic.reference_height
        ) * mosaic.reference_ptz_size[1]
        inverse = np.linalg.inv(homography)
        source_x, source_y = _project_arrays(inverse, target_x, target_y)
        source_width = max(1.0, float(self.source_size[0] - 1))
        source_height = max(1.0, float(self.source_size[1] - 1))

        for _iteration in range(3):
            grid_x = np.clip(
                source_x / source_width * (columns - 1),
                0.0,
                float(columns - 1),
            ).astype(np.float32)
            grid_y = np.clip(
                source_y / source_height * (rows - 1),
                0.0,
                float(rows - 1),
            ).astype(np.float32)
            correction_x = cv2.remap(
                residual_x,
                grid_x,
                grid_y,
                cv2.INTER_CUBIC,
                borderMode=cv2.BORDER_REPLICATE,
            )
            correction_y = cv2.remap(
                residual_y,
                grid_x,
                grid_y,
                cv2.INTER_CUBIC,
                borderMode=cv2.BORDER_REPLICATE,
            )
            source_x, source_y = _project_arrays(
                inverse,
                target_x - correction_x,
                target_y - correction_y,
            )

        valid = (
            np.isfinite(source_x)
            & np.isfinite(source_y)
            & (source_x >= 0.0)
            & (source_y >= 0.0)
            & (source_x < self.source_size[0] - 1)
            & (source_y < self.source_size[1] - 1)
        )
        return (
            np.where(valid, source_x, -1.0).astype(np.float32),
            np.where(valid, source_y, -1.0).astype(np.float32),
            valid.astype(np.uint8) * 255,
        )

    def _piecewise_reference_maps(
        self,
        mosaic: CalibrationMosaic,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Load legacy triangle meshes without invalidating old diagnostics."""

        map_x = np.full((mosaic.height, mosaic.width), -1.0, dtype=np.float32)
        map_y = np.full((mosaic.height, mosaic.width), -1.0, dtype=np.float32)
        valid = np.zeros((mosaic.height, mosaic.width), dtype=np.uint8)
        destination = np.asarray(
            [
                (
                    mosaic.reference_point_to_canvas(
                        point[0] / self.target_size[0],
                        point[1] / self.target_size[1],
                    )[0]
                    * mosaic.width,
                    mosaic.reference_point_to_canvas(
                        point[0] / self.target_size[0],
                        point[1] / self.target_size[1],
                    )[1]
                    * mosaic.height,
                )
                for point in self.target_points
            ],
            dtype=np.float32,
        )
        source = np.asarray(self.source_points, dtype=np.float32)
        for indices in self.triangles:
            target_triangle = destination[list(indices)]
            source_triangle = source[list(indices)]
            if abs(float(cv2.contourArea(target_triangle))) < 0.5:
                continue
            left = max(0, int(math.floor(float(target_triangle[:, 0].min()))))
            top = max(0, int(math.floor(float(target_triangle[:, 1].min()))))
            right = min(
                mosaic.width - 1,
                int(math.ceil(float(target_triangle[:, 0].max()))),
            )
            bottom = min(
                mosaic.height - 1,
                int(math.ceil(float(target_triangle[:, 1].max()))),
            )
            if right < left or bottom < top:
                continue
            local_triangle = np.rint(
                target_triangle - np.asarray((left, top), dtype=np.float32)
            ).astype(np.int32)
            mask = np.zeros((bottom - top + 1, right - left + 1), dtype=np.uint8)
            cv2.fillConvexPoly(mask, local_triangle, 255, lineType=cv2.LINE_8)
            rows, columns = np.nonzero(mask)
            if not len(rows):
                continue
            inverse = cv2.getAffineTransform(target_triangle, source_triangle)
            canvas_x = columns.astype(np.float64) + left
            canvas_y = rows.astype(np.float64) + top
            source_x = (
                inverse[0, 0] * canvas_x
                + inverse[0, 1] * canvas_y
                + inverse[0, 2]
            )
            source_y = (
                inverse[1, 0] * canvas_x
                + inverse[1, 1] * canvas_y
                + inverse[1, 2]
            )
            map_x[rows + top, columns + left] = source_x.astype(np.float32)
            map_y[rows + top, columns + left] = source_y.astype(np.float32)
            valid[rows + top, columns + left] = 255
        return map_x, map_y, valid


@dataclass(frozen=True, slots=True)
class CalibrationMosaic:
    image_path: Path
    map_path: Path
    left: float
    top: float
    right: float
    bottom: float
    width: int
    height: int
    audience_size: tuple[int, int]
    reference_ptz_size: tuple[int, int]
    audience_to_reference_ptz: tuple[tuple[float, float, float], ...]
    warp_mesh: AudienceWarpMesh
    coordinate_space: str = "reference_ptz"

    @property
    def reference_width(self) -> float:
        return self.right - self.left

    @property
    def reference_height(self) -> float:
        return self.bottom - self.top

    @property
    def audience_canvas_rect(self) -> tuple[float, float, float, float]:
        corners = _project_points(
            self.audience_to_reference_ptz,
            (
                (0.0, 0.0),
                (float(self.audience_size[0]), 0.0),
                (float(self.audience_size[0]), float(self.audience_size[1])),
                (0.0, float(self.audience_size[1])),
            ),
        )
        normalized = [
            self.reference_point_to_canvas(
                point[0] / self.reference_ptz_size[0],
                point[1] / self.reference_ptz_size[1],
            )
            for point in corners
        ]
        xs = [point[0] for point in normalized]
        ys = [point[1] for point in normalized]
        return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)

    def canvas_point_to_reference(self, x: float, y: float) -> tuple[float, float]:
        return (
            self.left + float(x) * self.reference_width,
            self.top + float(y) * self.reference_height,
        )

    def reference_point_to_canvas(self, x: float, y: float) -> tuple[float, float]:
        return (
            (float(x) - self.left) / self.reference_width,
            (float(y) - self.top) / self.reference_height,
        )

    def canvas_rect_to_reference(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
    ) -> tuple[float, float, float, float]:
        point = self.canvas_point_to_reference(x, y)
        return (
            point[0],
            point[1],
            float(width) * self.reference_width,
            float(height) * self.reference_height,
        )

    def live_to_canvas_homography(
        self,
        live_to_reference: Any,
        *,
        source_size: tuple[int, int],
        live_size: tuple[int, int],
        reference_size: tuple[int, int],
    ) -> tuple[tuple[float, float, float], ...]:
        """Return the global fallback transform into PTZ panorama coordinates.

        Runtime rendering uses ``warp_mesh`` to correct parallax locally. This
        homography remains useful as a safe fallback and for diagnostics.
        """

        source_width, source_height = _size(source_size)
        live_width, live_height = _size(live_size)
        reference_width, reference_height = _size(reference_size)
        if (reference_width, reference_height) != self.audience_size:
            reference_scale = np.asarray(
                (
                    (self.audience_size[0] / reference_width, 0.0, 0.0),
                    (0.0, self.audience_size[1] / reference_height, 0.0),
                    (0.0, 0.0, 1.0),
                ),
                dtype=np.float64,
            )
        else:
            reference_scale = np.eye(3, dtype=np.float64)
        source_to_live = np.asarray(
            (
                (live_width / source_width, 0.0, 0.0),
                (0.0, live_height / source_height, 0.0),
                (0.0, 0.0, 1.0),
            ),
            dtype=np.float64,
        )
        canonical_to_canvas = np.asarray(
            (
                (
                    self.width / (self.reference_width * self.reference_ptz_size[0]),
                    0.0,
                    -self.left * self.width / self.reference_width,
                ),
                (
                    0.0,
                    self.height / (self.reference_height * self.reference_ptz_size[1]),
                    -self.top * self.height / self.reference_height,
                ),
                (0.0, 0.0, 1.0),
            ),
            dtype=np.float64,
        )
        selected = (
            canonical_to_canvas
            @ np.asarray(self.audience_to_reference_ptz, dtype=np.float64)
            @ reference_scale
            @ np.asarray(_matrix(live_to_reference), dtype=np.float64)
            @ source_to_live
        )
        if abs(float(selected[2, 2])) < 1e-12:
            raise ValueError("The live Audience-to-panorama transform is singular.")
        selected /= selected[2, 2]
        if not np.isfinite(selected).all():
            raise ValueError("The live Audience-to-panorama transform is not finite.")
        return tuple(tuple(float(value) for value in row) for row in selected)


def ensure_calibration_mosaic(
    review: CalibrationReviewData,
    *,
    maximum_width: int = 1800,
) -> CalibrationMosaic:
    cached = load_calibration_mosaic(review)
    if cached is not None:
        return cached
    return _build_calibration_mosaic(review, maximum_width=maximum_width)


def load_calibration_mosaic(
    review: CalibrationReviewData,
) -> CalibrationMosaic | None:
    image_path, metadata_path = _cache_paths(review)
    if not image_path.is_file() or not metadata_path.is_file():
        return None
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        if int(payload.get("schema_version", 0)) != MOSAIC_SCHEMA_VERSION:
            return None
        if str(payload.get("map_path", "")) != str(review.map_path.resolve()):
            return None
        if int(payload.get("map_mtime_ns", 0)) != review.map_path.stat().st_mtime_ns:
            return None
        mesh_payload = payload["warp_mesh"]
        mesh = AudienceWarpMesh(
            source_size=_size(mesh_payload["source_size"]),
            target_size=_size(mesh_payload["target_size"]),
            source_points=tuple(_point(item) for item in mesh_payload["source_points"]),
            target_points=tuple(_point(item) for item in mesh_payload["target_points"]),
            triangles=tuple(
                tuple(int(index) for index in item)
                for item in mesh_payload["triangles"]
            ),
            control_points=int(mesh_payload.get("control_points", 0)),
            grid_shape=_size(mesh_payload.get("grid_shape", (0, 0))),
            interpolation=str(
                mesh_payload.get("interpolation", "piecewise_affine")
            ),
            inverse_grid_shape=_optional_grid_shape(
                mesh_payload.get("inverse_grid_shape", (0, 0))
            ),
            inverse_source_points=tuple(
                _point(item)
                for item in mesh_payload.get("inverse_source_points", ())
            ),
            inverse_validity=tuple(
                float(item)
                for item in mesh_payload.get("inverse_validity", ())
            ),
        )
        mosaic = CalibrationMosaic(
            image_path=image_path,
            map_path=review.map_path.resolve(),
            left=float(payload["left"]),
            top=float(payload["top"]),
            right=float(payload["right"]),
            bottom=float(payload["bottom"]),
            width=int(payload["width"]),
            height=int(payload["height"]),
            audience_size=_size(payload["audience_size"]),
            reference_ptz_size=_size(payload["reference_ptz_size"]),
            audience_to_reference_ptz=tuple(
                tuple(float(value) for value in row)
                for row in _matrix(payload["audience_to_reference_ptz"])
            ),
            warp_mesh=mesh,
            coordinate_space=str(payload.get("coordinate_space", "reference_ptz")),
        )
        if (
            mosaic.width <= 0
            or mosaic.height <= 0
            or mosaic.reference_width <= 0
            or mosaic.reference_height <= 0
            or not mesh.triangles
        ):
            return None
        return mosaic
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _build_calibration_mosaic(
    review: CalibrationReviewData,
    *,
    maximum_width: int,
) -> CalibrationMosaic:
    payload = _load_json(review.map_path)
    reference_payload = _load_json(review.reference_calibration_path)
    alignment = reference_payload["alignment"]
    audience_size = _size(alignment.get("audience_size", review.audience_size))
    reference_ptz_size = _size(alignment.get("ptz_size", (1920, 1080)))
    audience_to_reference_ptz = _matrix(alignment["audience_to_ptz"])
    pose_by_index = {pose.index: pose for pose in review.poses}
    sources: list[tuple[np.ndarray, np.ndarray]] = []
    warp_sources: list[
        tuple[np.ndarray, np.ndarray, tuple[int, int]]
    ] = []
    coverage = [
        (0.0, 0.0),
        (float(reference_ptz_size[0]), float(reference_ptz_size[1])),
    ]

    for pose_payload in payload.get("poses", ()):
        if not isinstance(pose_payload, dict) or pose_payload.get("status") != "accepted":
            continue
        index = int(pose_payload.get("index", 0))
        pose = pose_by_index.get(index)
        if pose is None:
            continue
        try:
            pose_to_reference = np.linalg.inv(_reference_to_pose(pose_payload))
        except (KeyError, TypeError, ValueError, np.linalg.LinAlgError):
            continue
        if not np.isfinite(pose_to_reference).all():
            continue
        image = cv2.imread(str(pose.image_path), cv2.IMREAD_COLOR)
        if image is None:
            continue
        link = pose_payload.get("ptz_link") or {}
        declared_size = _size(
            pose_payload.get("ptz_size")
            or link.get("ptz_size")
            or (image.shape[1], image.shape[0])
        )
        if (image.shape[1], image.shape[0]) != declared_size:
            image = cv2.resize(image, declared_size, interpolation=cv2.INTER_AREA)
        try:
            direct_audience_to_pose = pose_payload.get("audience_to_ptz")
            if direct_audience_to_pose is not None:
                audience_to_pose = _matrix(direct_audience_to_pose)
            else:
                audience_to_pose = (
                    np.linalg.inv(pose_to_reference)
                    @ audience_to_reference_ptz
                )
        except (TypeError, ValueError, np.linalg.LinAlgError):
            continue
        if not np.isfinite(audience_to_pose).all():
            continue
        corners = _project_points(
            pose_to_reference,
            (
                (0.0, 0.0),
                (float(declared_size[0]), 0.0),
                (float(declared_size[0]), float(declared_size[1])),
                (0.0, float(declared_size[1])),
            ),
        )
        coverage.extend(corners)
        sources.append((image, pose_to_reference))
        warp_sources.append(
            (pose_to_reference, audience_to_pose, declared_size)
        )

    if not sources:
        raise ValueError("The active calibration has no stitchable PTZ pose images.")
    xs = [point[0] for point in coverage]
    ys = [point[1] for point in coverage]
    padding = max(reference_ptz_size) * 0.015
    left_px = min(xs) - padding
    top_px = min(ys) - padding
    right_px = max(xs) + padding
    bottom_px = max(ys) + padding
    span_width = right_px - left_px
    span_height = bottom_px - top_px
    scale = min(1.0, max(640, int(maximum_width)) / span_width)
    output_width = max(640, round(span_width * scale))
    output_height = max(360, round(span_height * scale))
    canvas_transform = np.asarray(
        (
            (output_width / span_width, 0.0, -left_px * output_width / span_width),
            (0.0, output_height / span_height, -top_px * output_height / span_height),
            (0.0, 0.0, 1.0),
        ),
        dtype=np.float64,
    )
    accumulation = np.zeros((output_height, output_width, 3), dtype=np.float32)
    weights = np.zeros((output_height, output_width), dtype=np.float32)
    for image, matrix in sources:
        source_weight = _feather_weight(image.shape[1], image.shape[0])
        projected = canvas_transform @ matrix
        warped = cv2.warpPerspective(
            image,
            projected,
            (output_width, output_height),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
        )
        warped_weight = cv2.warpPerspective(
            source_weight,
            projected,
            (output_width, output_height),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
        )
        accumulation += warped.astype(np.float32) * warped_weight[..., None]
        weights += warped_weight
    result = np.full((output_height, output_width, 3), (22, 20, 18), dtype=np.uint8)
    valid = weights > 1e-4
    result[valid] = np.clip(
        accumulation[valid] / weights[valid, None],
        0,
        255,
    ).astype(np.uint8)
    # The PTZ snapshots provide edge context, while the live Audience warp is
    # the interaction surface. Muting this background makes old exposure and
    # overlap seams unobtrusive without discarding useful calibrated coverage.
    gray = cv2.cvtColor(result, cv2.COLOR_BGR2GRAY)
    gray_bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    result = cv2.addWeighted(result, 0.62, gray_bgr, 0.38, 0.0)
    result = cv2.GaussianBlur(result, (3, 3), 0.65)
    result = cv2.convertScaleAbs(result, alpha=0.74, beta=6.0)
    result[~valid] = (22, 20, 18)

    mesh = _build_audience_warp_mesh(
        payload,
        review,
        audience_size=audience_size,
        reference_ptz_size=reference_ptz_size,
        audience_to_reference_ptz=audience_to_reference_ptz,
        canvas_transform=canvas_transform,
        canvas_size=(output_width, output_height),
        warp_sources=warp_sources,
    )
    image_path, metadata_path = _cache_paths(review)
    image_path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(image_path), result, (cv2.IMWRITE_JPEG_QUALITY, 91)):
        raise OSError(f"Could not write calibration panorama: {image_path}")
    metadata = {
        "schema_version": MOSAIC_SCHEMA_VERSION,
        "coordinate_space": "reference_ptz",
        "map_path": str(review.map_path.resolve()),
        "map_mtime_ns": review.map_path.stat().st_mtime_ns,
        "left": left_px / reference_ptz_size[0],
        "top": top_px / reference_ptz_size[1],
        "right": right_px / reference_ptz_size[0],
        "bottom": bottom_px / reference_ptz_size[1],
        "width": output_width,
        "height": output_height,
        "audience_size": list(audience_size),
        "reference_ptz_size": list(reference_ptz_size),
        "audience_to_reference_ptz": audience_to_reference_ptz.tolist(),
        "warp_mesh": {
            "source_size": list(mesh.source_size),
            "target_size": list(mesh.target_size),
            "source_points": [list(item) for item in mesh.source_points],
            "target_points": [list(item) for item in mesh.target_points],
            "triangles": [list(item) for item in mesh.triangles],
            "control_points": mesh.control_points,
            "grid_shape": list(mesh.grid_shape),
            "interpolation": mesh.interpolation,
            "inverse_grid_shape": list(mesh.inverse_grid_shape),
            "inverse_source_points": [
                list(item) for item in mesh.inverse_source_points
            ],
            "inverse_validity": list(mesh.inverse_validity),
        },
    }
    _atomic_json(metadata_path, metadata)
    loaded = load_calibration_mosaic(review)
    if loaded is None:
        raise OSError("The generated PTZ panorama cache could not be validated.")
    return loaded


def _build_audience_warp_mesh(
    payload: dict[str, Any],
    review: CalibrationReviewData,
    *,
    audience_size: tuple[int, int],
    reference_ptz_size: tuple[int, int],
    audience_to_reference_ptz: np.ndarray,
    canvas_transform: np.ndarray,
    canvas_size: tuple[int, int],
    warp_sources: Sequence[
        tuple[np.ndarray, np.ndarray, tuple[int, int]]
    ],
) -> AudienceWarpMesh:
    excluded_ids = {item.marker_id for item in review.excluded_markers}
    observations: dict[int, list[tuple[float, float, float, float]]] = {}
    for pose_payload in payload.get("poses", ()):
        if not isinstance(pose_payload, dict) or pose_payload.get("status") != "accepted":
            continue
        try:
            pose_to_reference = np.linalg.inv(_reference_to_pose(pose_payload))
        except (KeyError, TypeError, ValueError, np.linalg.LinAlgError):
            continue
        per_pose: dict[int, list[tuple[float, float, float, float]]] = {}
        for marker in pose_payload.get("structural_markers") or ():
            try:
                marker_id = int(marker.get("marker_id", 0))
                if (
                    marker_id <= 0
                    or marker_id in excluded_ids
                    or str(marker.get("stability", "")) != "temporal_repeat"
                    or int(marker.get("repeatability", 0)) < 2
                    or float(marker.get("error_pixels", math.inf)) > 3.0
                ):
                    continue
                audience_x = float(marker["audience_x"])
                audience_y = float(marker["audience_y"])
                canonical = _project(
                    pose_to_reference,
                    float(marker["ptz_x"]),
                    float(marker["ptz_y"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if not (
                -1.0 <= audience_x <= audience_size[0] + 1.0
                and -1.0 <= audience_y <= audience_size[1] + 1.0
                and all(math.isfinite(value) for value in canonical)
            ):
                continue
            per_pose.setdefault(marker_id, []).append(
                (audience_x, audience_y, canonical[0], canonical[1])
            )
        for marker_id, values in per_pose.items():
            selected = np.median(np.asarray(values, dtype=np.float64), axis=0)
            observations.setdefault(marker_id, []).append(tuple(float(item) for item in selected))

    controls: list[tuple[float, float, float, float]] = []
    for values in observations.values():
        if len(values) < 2:
            continue
        samples = np.asarray(values, dtype=np.float64)
        selected = np.median(samples, axis=0)
        canonical_spread = np.linalg.norm(samples[:, 2:4] - selected[2:4], axis=1)
        if float(np.quantile(canonical_spread, 0.90)) > 8.0:
            continue
        global_target = _project(audience_to_reference_ptz, selected[0], selected[1])
        # Large, repeatable upper-frame residuals are the parallax correction,
        # not an error. Keep them while rejecting extreme/background-depth
        # matches; correction is faded out before the already-accurate bottom.
        if selected[1] > audience_size[1] * 0.62:
            continue
        if math.hypot(selected[2] - global_target[0], selected[3] - global_target[1]) > 180.0:
            continue
        controls.append(tuple(float(item) for item in selected))

    control_sources = np.asarray(
        [(item[0] / audience_size[0], item[1] / audience_size[1]) for item in controls],
        dtype=np.float64,
    )
    control_residuals = np.asarray(
        [
            (
                item[2] - _project(audience_to_reference_ptz, item[0], item[1])[0],
                item[3] - _project(audience_to_reference_ptz, item[0], item[1])[1],
            )
            for item in controls
        ],
        dtype=np.float64,
    )

    # Controls shape a regular mesh instead of becoming mesh vertices. That
    # prevents a single close or noisy landmark from folding a tiny triangle
    # and leaving holes in the live interaction surface.
    source_points: list[tuple[float, float]] = []
    target_points: list[tuple[float, float]] = []
    # A dense regular lattice lets OpenCV evaluate the fitted curved field
    # bicubically. Structural observations remain the only calibration input;
    # these samples are presentation controls, not additional guessed points.
    grid_x = np.linspace(0.0, 1.0, 17)
    grid_y = np.linspace(0.0, 1.0, 13)
    queries = np.asarray(
        [(x, y) for y in grid_y for x in grid_x],
        dtype=np.float64,
    )
    corrections = _thin_plate_residuals(
        control_sources,
        control_residuals,
        queries,
    )
    for (normalized_x, normalized_y), correction in zip(
        queries,
        corrections,
    ):
        source = (
            normalized_x * (audience_size[0] - 1),
            normalized_y * (audience_size[1] - 1),
        )
        global_target = _project(audience_to_reference_ptz, *source)
        source_points.append(source)
        target_points.append(
            (global_target[0] + correction[0], global_target[1] + correction[1])
        )

    triangles = _delaunay_triangles(source_points, target_points, audience_size)
    if not triangles:
        raise ValueError("Camera Sync could not construct a safe Audience warp mesh.")
    inverse_grid_shape, inverse_source_points, inverse_validity = (
        _build_multi_pose_inverse_grid(
            audience_size=audience_size,
            audience_to_reference_ptz=audience_to_reference_ptz,
            canvas_transform=canvas_transform,
            canvas_size=canvas_size,
            warp_sources=warp_sources,
        )
    )
    return AudienceWarpMesh(
        source_size=audience_size,
        target_size=reference_ptz_size,
        source_points=tuple(source_points),
        target_points=tuple(target_points),
        triangles=triangles,
        control_points=len(controls),
        grid_shape=(len(grid_x), len(grid_y)),
        interpolation=(
            "multi_pose_inverse_bicubic"
            if inverse_source_points
            else "thin_plate_bicubic"
        ),
        inverse_grid_shape=inverse_grid_shape,
        inverse_source_points=inverse_source_points,
        inverse_validity=inverse_validity,
    )


def _build_multi_pose_inverse_grid(
    *,
    audience_size: tuple[int, int],
    audience_to_reference_ptz: np.ndarray,
    canvas_transform: np.ndarray,
    canvas_size: tuple[int, int],
    warp_sources: Sequence[
        tuple[np.ndarray, np.ndarray, tuple[int, int]]
    ],
    columns: int = 81,
    weight_power: float = 2.5,
) -> tuple[
    tuple[int, int],
    tuple[tuple[float, float], ...],
    tuple[float, ...],
]:
    """Build a compact panorama-to-Audience field from every scanned pose.

    The height is derived from the panorama aspect ratio so the sampling
    density is roughly square. Invalid boundary cells retain the global
    calibration projection; their zero support keeps them invisible while
    preventing interpolation from pulling sentinel values into valid pixels.
    """

    canvas_width, canvas_height = _size(canvas_size)
    selected_columns = max(17, int(columns))
    selected_rows = max(
        13,
        round(selected_columns * canvas_height / canvas_width),
    )
    grid_x, grid_y = np.meshgrid(
        np.linspace(0.0, canvas_width - 1.0, selected_columns),
        np.linspace(0.0, canvas_height - 1.0, selected_rows),
    )
    try:
        canvas_to_global_audience = np.linalg.inv(
            canvas_transform @ audience_to_reference_ptz
        )
    except np.linalg.LinAlgError:
        return (0, 0), (), ()
    fallback_x, fallback_y = _project_arrays(
        canvas_to_global_audience,
        grid_x,
        grid_y,
    )
    accumulation_x = np.zeros_like(grid_x, dtype=np.float64)
    accumulation_y = np.zeros_like(grid_y, dtype=np.float64)
    accumulated_weight = np.zeros_like(grid_x, dtype=np.float64)

    for pose_to_reference, audience_to_pose, ptz_size in warp_sources:
        try:
            projected = canvas_transform @ pose_to_reference
            canvas_to_pose = np.linalg.inv(projected)
            canvas_to_audience = np.linalg.inv(projected @ audience_to_pose)
        except np.linalg.LinAlgError:
            continue
        pose_x, pose_y = _project_arrays(canvas_to_pose, grid_x, grid_y)
        source_x, source_y = _project_arrays(
            canvas_to_audience,
            grid_x,
            grid_y,
        )
        pose_width, pose_height = ptz_size
        inside = (
            np.isfinite(pose_x)
            & np.isfinite(pose_y)
            & np.isfinite(source_x)
            & np.isfinite(source_y)
            & (pose_x >= 0.0)
            & (pose_y >= 0.0)
            & (pose_x < pose_width - 1)
            & (pose_y < pose_height - 1)
            & (source_x >= 0.0)
            & (source_y >= 0.0)
            & (source_x < audience_size[0] - 1)
            & (source_y < audience_size[1] - 1)
        )
        edge_distance = np.minimum.reduce(
            (
                pose_x + 1.0,
                pose_width - pose_x,
                pose_y + 1.0,
                pose_height - pose_y,
            )
        )
        feather = max(24.0, min(pose_width, pose_height) * 0.08)
        weight = np.clip(edge_distance / feather, 0.03, 1.0)
        weight = np.where(inside, np.power(weight, weight_power), 0.0)
        accumulation_x += np.nan_to_num(source_x) * weight
        accumulation_y += np.nan_to_num(source_y) * weight
        accumulated_weight += weight

    supported = accumulated_weight > 1e-5
    if np.count_nonzero(supported) < 12:
        return (0, 0), (), ()
    selected_x = np.divide(
        accumulation_x,
        accumulated_weight,
        out=np.asarray(fallback_x, dtype=np.float64).copy(),
        where=supported,
    )
    selected_y = np.divide(
        accumulation_y,
        accumulated_weight,
        out=np.asarray(fallback_y, dtype=np.float64).copy(),
        where=supported,
    )
    selected_x, selected_y = _smooth_multi_pose_inverse_field(
        grid_x=grid_x,
        grid_y=grid_y,
        source_x=selected_x,
        source_y=selected_y,
        fallback_x=fallback_x,
        fallback_y=fallback_y,
        supported=supported,
    )
    points = tuple(
        (float(x), float(y))
        for x, y in np.column_stack(
            (selected_x.ravel(), selected_y.ravel())
        )
    )
    validity = tuple(float(value) for value in supported.astype(np.float32).ravel())
    return (selected_columns, selected_rows), points, validity


def _smooth_multi_pose_inverse_field(
    *,
    grid_x: np.ndarray,
    grid_y: np.ndarray,
    source_x: np.ndarray,
    source_y: np.ndarray,
    fallback_x: np.ndarray,
    fallback_y: np.ndarray,
    supported: np.ndarray,
    degree: int = 3,
    ridge: float = 1.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Regularize pose corrections into one lens-like curved field.

    Adjacent PTZ scans can disagree locally because the sanctuary is
    three-dimensional and the two cameras have different heights. Directly
    reproducing every disagreement creates ripples in straight walls and pews.
    A robust cubic surface retains the broad measured lens/parallax correction
    (including the upper corners) while rejecting those high-frequency seams.
    """

    if np.count_nonzero(supported) < 12:
        return source_x, source_y
    width = max(1.0, float(np.nanmax(grid_x) - np.nanmin(grid_x)))
    height = max(1.0, float(np.nanmax(grid_y) - np.nanmin(grid_y)))
    normalized_x = (grid_x - np.nanmin(grid_x)) / width * 2.0 - 1.0
    normalized_y = (grid_y - np.nanmin(grid_y)) / height * 2.0 - 1.0
    selected_matrix = _polynomial_features(
        normalized_x[supported],
        normalized_y[supported],
        degree,
    )
    full_matrix = _polynomial_features(
        normalized_x.ravel(),
        normalized_y.ravel(),
        degree,
    )
    coefficient_x = _robust_polynomial_fit(
        selected_matrix,
        (source_x - fallback_x)[supported],
        ridge=ridge,
    )
    coefficient_y = _robust_polynomial_fit(
        selected_matrix,
        (source_y - fallback_y)[supported],
        ridge=ridge,
    )
    correction_x = (full_matrix @ coefficient_x).reshape(grid_x.shape)
    correction_y = (full_matrix @ coefficient_y).reshape(grid_y.shape)
    return fallback_x + correction_x, fallback_y + correction_y


def _polynomial_features(
    x: np.ndarray,
    y: np.ndarray,
    degree: int,
) -> np.ndarray:
    columns: list[np.ndarray] = []
    for total_degree in range(max(0, int(degree)) + 1):
        for x_power in range(total_degree + 1):
            y_power = total_degree - x_power
            columns.append(np.power(x, x_power) * np.power(y, y_power))
    return np.column_stack(columns)


def _robust_polynomial_fit(
    matrix: np.ndarray,
    values: np.ndarray,
    *,
    ridge: float,
) -> np.ndarray:
    weights = np.ones(len(values), dtype=np.float64)
    coefficients = np.zeros(matrix.shape[1], dtype=np.float64)
    penalty = np.eye(matrix.shape[1], dtype=np.float64) * max(1e-6, ridge)
    penalty[0, 0] *= 0.01
    for _iteration in range(8):
        root = np.sqrt(weights)
        selected_matrix = matrix * root[:, None]
        selected_values = values * root
        coefficients = np.linalg.solve(
            selected_matrix.T @ selected_matrix + penalty,
            selected_matrix.T @ selected_values,
        )
        error = values - matrix @ coefficients
        scale = max(3.0, float(np.median(np.abs(error))) * 1.4826)
        normalized = np.abs(error) / (2.3 * scale)
        weights = np.where(normalized <= 1.0, 1.0, 1.0 / normalized)
    return coefficients


def _thin_plate_residuals(
    control_sources: np.ndarray,
    control_residuals: np.ndarray,
    queries: np.ndarray,
) -> np.ndarray:
    """Fit a regularized thin-plate spline over structural residuals.

    Thin-plate splines are the standard smooth analogue of a control-point
    warp: they reproduce broad lens/parallax curvature without corners or
    triangle seams. A small regularizer prevents one noisy landmark from
    pulling a sharp bump into the live view.
    """

    query_points = np.asarray(queries, dtype=np.float64).reshape(-1, 2)
    if len(control_sources) < 3:
        return np.zeros((len(query_points), 2), dtype=np.float64)
    sources = np.asarray(control_sources, dtype=np.float64).reshape(-1, 2)
    residuals = np.asarray(control_residuals, dtype=np.float64).reshape(-1, 2)
    pairwise = sources[:, None, :] - sources[None, :, :]
    kernel = _thin_plate_kernel(np.linalg.norm(pairwise, axis=2))
    affine = np.column_stack((np.ones(len(sources)), sources))
    system = np.block(
        [
            [kernel + np.eye(len(sources)) * 0.002, affine],
            [affine.T, np.zeros((3, 3), dtype=np.float64)],
        ]
    )
    values = np.vstack((residuals, np.zeros((3, 2), dtype=np.float64)))
    coefficients, *_unused = np.linalg.lstsq(system, values, rcond=1e-9)
    query_distances = np.linalg.norm(
        query_points[:, None, :] - sources[None, :, :],
        axis=2,
    )
    query_kernel = _thin_plate_kernel(query_distances)
    query_affine = np.column_stack((np.ones(len(query_points)), query_points))
    selected = (
        query_kernel @ coefficients[: len(sources)]
        + query_affine @ coefficients[len(sources) :]
    )

    nearest = np.min(query_distances, axis=1)
    locality = np.clip((0.48 - nearest) / 0.18, 0.0, 1.0)
    locality = locality * locality * (3.0 - 2.0 * locality)
    # The lower pews already align well in this installation. Blend the curved
    # correction smoothly to zero below them instead of bending the aisle.
    vertical = np.clip((0.80 - query_points[:, 1]) / 0.20, 0.0, 1.0)
    vertical = vertical * vertical * (3.0 - 2.0 * vertical)
    selected *= (locality * vertical)[:, None]
    magnitude = np.linalg.norm(selected, axis=1)
    excessive = magnitude > 180.0
    if np.any(excessive):
        selected[excessive] *= (180.0 / magnitude[excessive])[:, None]
    return selected


def _thin_plate_kernel(distance: np.ndarray) -> np.ndarray:
    squared = np.asarray(distance, dtype=np.float64) ** 2
    return squared * np.log(np.maximum(squared, 1e-12))


def _delaunay_triangles(
    source_points: Sequence[tuple[float, float]],
    target_points: Sequence[tuple[float, float]],
    source_size: tuple[int, int],
) -> tuple[tuple[int, int, int], ...]:
    subdivision = cv2.Subdiv2D((0, 0, source_size[0], source_size[1]))
    for point in source_points:
        subdivision.insert((float(point[0]), float(point[1])))
    source = np.asarray(source_points, dtype=np.float64)
    target = np.asarray(target_points, dtype=np.float64)
    selected: set[tuple[int, int, int]] = set()
    for raw in subdivision.getTriangleList():
        vertices = np.asarray(raw, dtype=np.float64).reshape(3, 2)
        if np.any(vertices[:, 0] < -0.5) or np.any(vertices[:, 0] > source_size[0] - 0.5):
            continue
        if np.any(vertices[:, 1] < -0.5) or np.any(vertices[:, 1] > source_size[1] - 0.5):
            continue
        indices = tuple(
            int(np.argmin(np.linalg.norm(source - vertex, axis=1)))
            for vertex in vertices
        )
        if len(set(indices)) != 3:
            continue
        source_triangle = source[list(indices)]
        target_triangle = target[list(indices)]
        source_cross = _triangle_cross(source_triangle)
        target_cross = _triangle_cross(target_triangle)
        if abs(source_cross) < 0.25 or abs(target_cross) < 0.25:
            continue
        if source_cross * target_cross <= 0.0:
            continue
        selected.add(tuple(sorted(indices)))
    return tuple(sorted(selected))


def _triangle_cross(points: np.ndarray) -> float:
    first = points[1] - points[0]
    second = points[2] - points[0]
    return float(first[0] * second[1] - first[1] * second[0])


def _reference_to_pose(pose_payload: dict[str, Any]) -> np.ndarray:
    direct = pose_payload.get("reference_ptz_to_pose")
    if direct is not None:
        return _matrix(direct)
    link = pose_payload.get("ptz_link") or {}
    # build_camera_sync_map estimates this link between the reference PTZ image
    # and each moved PTZ image. Older maps retained the alignment library's
    # generic field name; the explicit direct field is preferred when present.
    linked = link.get("audience_to_ptz")
    if linked is not None:
        return _matrix(linked)
    if int(pose_payload.get("index", 0)) == 1:
        return np.eye(3, dtype=np.float64)
    raise KeyError("PTZ pose has no reference-image transform")


def _feather_weight(width: int, height: int) -> np.ndarray:
    x = np.minimum(np.arange(width) + 1, np.arange(width, 0, -1))
    y = np.minimum(np.arange(height) + 1, np.arange(height, 0, -1))
    distance = np.minimum(y[:, None], x[None, :]).astype(np.float32)
    feather = max(24.0, min(width, height) * 0.08)
    return np.clip(distance / feather, 0.03, 1.0)


def _cache_paths(review: CalibrationReviewData) -> tuple[Path, Path]:
    return (
        review.map_path.with_name("click-to-frame-panorama.jpg"),
        review.map_path.with_name("click-to-frame-panorama.json"),
    )


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Calibration map must contain an object.")
    return payload


def _matrix(value: Any) -> np.ndarray:
    matrix = np.asarray(value, dtype=np.float64)
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        raise ValueError("Calibration pose transform must be a finite 3x3 matrix.")
    return matrix


def _point(value: Any) -> tuple[float, float]:
    x, y = value
    selected = float(x), float(y)
    if not all(math.isfinite(item) for item in selected):
        raise ValueError("Calibration mesh point must be finite.")
    return selected


def _size(value: Any) -> tuple[int, int]:
    width, height = value
    selected = round(float(width)), round(float(height))
    if min(selected) <= 0 or not all(math.isfinite(item) for item in selected):
        raise ValueError("Calibration pose image size must be positive.")
    return selected


def _optional_grid_shape(value: Any) -> tuple[int, int]:
    width, height = value
    selected = round(float(width)), round(float(height))
    if min(selected) < 0 or not all(math.isfinite(item) for item in selected):
        raise ValueError("Calibration grid dimensions cannot be negative.")
    return selected


def _project(matrix: Any, x: float, y: float) -> tuple[float, float]:
    selected = np.asarray(matrix, dtype=np.float64)
    denominator = selected[2, 0] * x + selected[2, 1] * y + selected[2, 2]
    if abs(float(denominator)) < 1e-10:
        raise ValueError("Calibration projection is singular.")
    return (
        float((selected[0, 0] * x + selected[0, 1] * y + selected[0, 2]) / denominator),
        float((selected[1, 0] * x + selected[1, 1] * y + selected[1, 2]) / denominator),
    )


def _project_arrays(
    matrix: Any,
    x: np.ndarray,
    y: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    selected = np.asarray(matrix, dtype=np.float64)
    denominator = selected[2, 0] * x + selected[2, 1] * y + selected[2, 2]
    safe = np.abs(denominator) > 1e-10
    inverse = np.divide(
        1.0,
        denominator,
        out=np.full_like(denominator, np.nan, dtype=np.float64),
        where=safe,
    )
    return (
        (selected[0, 0] * x + selected[0, 1] * y + selected[0, 2])
        * inverse,
        (selected[1, 0] * x + selected[1, 1] * y + selected[1, 2])
        * inverse,
    )


def _project_points(
    matrix: Any,
    points: Sequence[tuple[float, float]],
) -> list[tuple[float, float]]:
    return [_project(matrix, point[0], point[1]) for point in points]


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
