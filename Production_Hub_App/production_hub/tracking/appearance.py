"""A small clothing-colour signature for telling two people apart.

Positions alone cannot separate a speaker who walks off from the next speaker
who walks on where they left, and at sermon zoom the calibrated geometry is too
approximate to try. Clothing colour can: on 2026-09-20 a reader in a pink sari
left and a reader in a black jacket arrived; on 2026-09-13 a preacher in a blue
shirt left and a man in a black vest took the lectern.

The signature is a normalized hue/saturation histogram of the torso: the middle
of the box horizontally and from the shoulders to the waist vertically, which
skips hair, headscarves, faces, and the background beside the arms.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from production_hub.tracking.models import NormalizedRect

HUE_BINS = 12
SATURATION_BINS = 4
Signature = tuple[float, ...]


def image_array(image) -> np.ndarray | None:
    """An RGB numpy view of a QImage, or None if it cannot be read."""

    from PySide6.QtGui import QImage

    if image is None or image.isNull():
        return None
    converted = image.convertToFormat(QImage.Format.Format_RGB888)
    width, height = converted.width(), converted.height()
    buffer = np.frombuffer(converted.constBits(), dtype=np.uint8, count=converted.sizeInBytes())
    return buffer.reshape(height, converted.bytesPerLine())[:, : width * 3].reshape(height, width, 3)


def signature(rgb: np.ndarray, bounds: NormalizedRect) -> Signature:
    """Torso colour histogram for a detection, or () when too small to trust."""

    height, width = rgb.shape[:2]
    left = int((bounds.x + bounds.width * 0.25) * width)
    right = int((bounds.x + bounds.width * 0.75) * width)
    top = int((bounds.y + bounds.height * 0.22) * height)
    bottom = int((bounds.y + bounds.height * 0.62) * height)
    left, right = max(0, left), min(width, right)
    top, bottom = max(0, top), min(height, bottom)
    if right - left < 6 or bottom - top < 8:
        return ()
    patch = rgb[top:bottom, left:right].reshape(-1, 3).astype(np.float32) / 255.0
    r, g, b = patch[:, 0], patch[:, 1], patch[:, 2]
    maximum = patch.max(axis=1)
    minimum = patch.min(axis=1)
    chroma = maximum - minimum
    saturation = np.where(maximum > 1e-6, chroma / np.maximum(maximum, 1e-6), 0.0)
    hue = np.zeros_like(maximum)
    nonzero = chroma > 1e-6
    rc = np.where(nonzero, (maximum - r) / np.maximum(chroma, 1e-6), 0.0)
    gc = np.where(nonzero, (maximum - g) / np.maximum(chroma, 1e-6), 0.0)
    bc = np.where(nonzero, (maximum - b) / np.maximum(chroma, 1e-6), 0.0)
    hue = np.where(r == maximum, bc - gc, np.where(g == maximum, 2.0 + rc - bc, 4.0 + gc - rc))
    hue = (hue / 6.0) % 1.0
    # Near-grey pixels have no reliable hue: give them their own bins by
    # brightness (black jacket, white dress, grey shirt) instead.
    grey = saturation < 0.18
    hue_bin = np.minimum((hue * HUE_BINS).astype(int), HUE_BINS - 1)
    saturation_bin = np.minimum((saturation * SATURATION_BINS).astype(int), SATURATION_BINS - 1)
    colour_index = hue_bin * SATURATION_BINS + saturation_bin
    grey_index = HUE_BINS * SATURATION_BINS + np.minimum((maximum * 4).astype(int), 3)
    index = np.where(grey, grey_index, colour_index)
    histogram = np.bincount(index, minlength=HUE_BINS * SATURATION_BINS + 4).astype(np.float64)
    total = histogram.sum()
    if total <= 0:
        return ()
    return tuple(float(value) for value in histogram / total)


def distance(left: Sequence[float], right: Sequence[float]) -> float | None:
    """Hellinger distance in [0, 1]; None when either signature is missing."""

    if not left or not right or len(left) != len(right):
        return None
    overlap = sum((a * b) ** 0.5 for a, b in zip(left, right))
    return max(0.0, 1.0 - overlap) ** 0.5


def blend(current: Sequence[float], observed: Sequence[float], weight: float) -> Signature:
    if not current:
        return tuple(observed)
    if not observed:
        return tuple(current)
    return tuple(a + (b - a) * weight for a, b in zip(current, observed))
