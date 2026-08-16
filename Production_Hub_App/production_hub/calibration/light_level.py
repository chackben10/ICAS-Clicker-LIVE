from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage


@dataclass(frozen=True, slots=True)
class CameraLightAssessment:
    acceptable: bool
    mean_luma: float
    contrast: float
    visible_fraction: float
    message: str


def assess_camera_light(image: QImage | None) -> CameraLightAssessment:
    if image is None or image.isNull():
        return CameraLightAssessment(False, 0.0, 0.0, 0.0, "No camera frame is available.")
    grayscale = image.scaled(
        192,
        108,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    ).convertToFormat(QImage.Format.Format_Grayscale8)
    width = grayscale.width()
    height = grayscale.height()
    stride = grayscale.bytesPerLine()
    values = np.frombuffer(
        grayscale.bits(),
        dtype=np.uint8,
        count=stride * height,
    ).reshape(height, stride)[:, :width]
    mean_luma = float(np.mean(values))
    contrast = float(np.std(values))
    visible_fraction = float(np.mean(values >= 18))
    acceptable = mean_luma >= 18.0 and contrast >= 10.0 and visible_fraction >= 0.12
    message = (
        f"Light sufficient · mean {mean_luma:.1f}/255 · contrast {contrast:.1f} · "
        f"visible detail {visible_fraction:.0%}"
        if acceptable
        else f"Light too low or featureless · mean {mean_luma:.1f}/255 · "
        f"contrast {contrast:.1f} · visible detail {visible_fraction:.0%}"
    )
    return CameraLightAssessment(
        acceptable,
        mean_luma,
        contrast,
        visible_fraction,
        message,
    )
