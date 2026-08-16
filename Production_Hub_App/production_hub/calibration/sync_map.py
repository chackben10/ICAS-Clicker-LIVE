from __future__ import annotations

from typing import Sequence

import numpy as np


def compose_audience_to_pose(
    audience_to_reference_ptz: Sequence[Sequence[float]],
    reference_ptz_to_pose: Sequence[Sequence[float]],
) -> tuple[tuple[float, float, float], ...]:
    """Compose Audience→reference and reference→pose homographies."""

    audience_to_reference = _homography(audience_to_reference_ptz)
    reference_to_pose = _homography(reference_ptz_to_pose)
    composed = reference_to_pose @ audience_to_reference
    composed /= composed[2, 2]
    return tuple(tuple(float(value) for value in row) for row in composed)


def invert_homography(
    matrix: Sequence[Sequence[float]],
) -> tuple[tuple[float, float, float], ...]:
    selected = _homography(matrix)
    inverse = np.linalg.inv(selected)
    inverse /= inverse[2, 2]
    return tuple(tuple(float(value) for value in row) for row in inverse)


def _homography(matrix: Sequence[Sequence[float]]) -> np.ndarray:
    selected = np.asarray(matrix, dtype=np.float64)
    if selected.shape != (3, 3):
        raise ValueError("A homography must be a 3x3 matrix.")
    if not np.isfinite(selected).all() or abs(float(np.linalg.det(selected))) < 1e-12:
        raise ValueError("A homography must be finite and invertible.")
    if abs(float(selected[2, 2])) < 1e-12:
        raise ValueError("A homography cannot be normalized because H[2,2] is zero.")
    return selected / selected[2, 2]
