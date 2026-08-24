from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from production_hub.tracking.models import NormalizedRect
from production_hub.tracking.ptz_geometry import PtzMotorPose


@dataclass(frozen=True, slots=True)
class PtzVelocityCommand:
    """Panasonic AWP speed values; 50 is neutral on every axis."""

    pan: int = 50
    tilt: int = 50
    zoom: int = 50

    @property
    def moving(self) -> bool:
        return (self.pan, self.tilt, self.zoom) != (50, 50, 50)


class SmoothPtzVelocityController:
    """Closed-loop, acceleration-limited tracking motion.

    Absolute position steps make a camera move, settle, and move again. This
    controller instead keeps a low continuous speed while error exists and
    ramps that speed up/down as the subject crosses a soft image dead zone.
    """

    def __init__(self) -> None:
        self._last_update: float | None = None
        self._pan_delta = 0.0
        self._tilt_delta = 0.0
        self._zoom_delta = 0.0
        self._filtered_x_error = 0.0
        self._filtered_y_error = 0.0
        self._last_live_center: tuple[float, float] | None = None
        self._subject_velocity_x = 0.0
        self._subject_velocity_y = 0.0
        self._zoom_direction = 0
        self._zoom_direction_since = 0.0

    def reset(self) -> None:
        self._last_update = None
        self._pan_delta = 0.0
        self._tilt_delta = 0.0
        self._zoom_delta = 0.0
        self._filtered_x_error = 0.0
        self._filtered_y_error = 0.0
        self._last_live_center = None
        self._subject_velocity_x = 0.0
        self._subject_velocity_y = 0.0
        self._zoom_direction = 0
        self._zoom_direction_since = 0.0

    def halt(self) -> PtzVelocityCommand:
        self.reset()
        return PtzVelocityCommand()

    def update(
        self,
        *,
        actual: PtzMotorPose,
        desired: PtzMotorPose,
        config: Any,
        now: float,
        live_bounds: NormalizedRect | None,
    ) -> PtzVelocityCommand:
        elapsed = (
            1.0 / max(1.0, float(config.decision_fps))
            if self._last_update is None
            else max(0.04, min(0.50, now - self._last_update))
        )
        self._last_update = now

        if live_bounds is not None:
            center_x, center_y = live_bounds.center
            x_error = center_x - 0.50
            y_error = center_y - 0.48
            smoothing = float(config.continuous_error_smoothing)
            self._filtered_x_error += (x_error - self._filtered_x_error) * smoothing
            self._filtered_y_error += (y_error - self._filtered_y_error) * smoothing
            if self._last_live_center is not None:
                measured_x = max(
                    -0.65,
                    min(0.65, (center_x - self._last_live_center[0]) / elapsed),
                )
                measured_y = max(
                    -0.65,
                    min(0.65, (center_y - self._last_live_center[1]) / elapsed),
                )
                velocity_smoothing = float(config.continuous_velocity_smoothing)
                self._subject_velocity_x += (
                    measured_x - self._subject_velocity_x
                ) * velocity_smoothing
                self._subject_velocity_y += (
                    measured_y - self._subject_velocity_y
                ) * velocity_smoothing
            self._last_live_center = (center_x, center_y)
            prediction = float(config.continuous_prediction_seconds)
            predicted_x_error = _predicted_error(
                self._filtered_x_error,
                self._subject_velocity_x,
                prediction,
            )
            predicted_y_error = _predicted_error(
                self._filtered_y_error,
                self._subject_velocity_y,
                prediction * 0.55,
            )
            target_pan = _image_axis_target(
                predicted_x_error,
                float(config.subject_center_deadband_x),
                float(config.continuous_pan_max_speed),
            )
            # Panasonic tilt speed is above 50 for up and below 50 for down.
            target_tilt = -_image_axis_target(
                predicted_y_error,
                float(config.subject_center_deadband_y),
                float(config.continuous_tilt_max_speed),
            )
        else:
            # Audience-camera reacquisition has no PTZ image-space rectangle.
            # Use the calibrated absolute target only as an error measurement;
            # the camera is still driven by a continuous bounded speed.
            # Panasonic's continuous pan command and absolute pan-position
            # coordinate have opposite signs: values below 50 increase the
            # reported motor position, while values above 50 decrease it.
            # PTZ-image tracking does not expose that distinction because it
            # works directly in screen space; calibrated Audience recovery
            # must invert the absolute motor error here.
            target_pan = -_motor_axis_target(
                desired.pan - actual.pan,
                int(config.pan_deadband_units),
                1400.0,
                float(config.continuous_pan_max_speed),
            )
            target_tilt = -_motor_axis_target(
                desired.tilt - actual.tilt,
                int(config.tilt_deadband_units),
                900.0,
                float(config.continuous_tilt_max_speed),
            )
            self._filtered_x_error = 0.0
            self._filtered_y_error = 0.0
            self._last_live_center = None
            self._subject_velocity_x = 0.0
            self._subject_velocity_y = 0.0

        target_zoom = self._zoom_target(actual, desired, config, now)
        if live_bounds is None and target_zoom > 0:
            # Keep the field of view open during wide-camera reacquisition.
            # Zooming in before the locked person is visible in the PTZ image
            # makes reacquisition harder and can immediately lose them again.
            target_zoom = 0.0
        maximum_change = float(config.continuous_speed_slew_per_second) * elapsed
        self._pan_delta = _slew(self._pan_delta, target_pan, maximum_change)
        self._tilt_delta = _slew(self._tilt_delta, target_tilt, maximum_change)
        self._zoom_delta = _slew(
            self._zoom_delta,
            target_zoom,
            max(1.0, maximum_change * 0.55),
        )

        return PtzVelocityCommand(
            pan=_aw_speed(self._pan_delta),
            tilt=_aw_speed(self._tilt_delta),
            zoom=_aw_speed(self._zoom_delta),
        )

    def _zoom_target(
        self,
        actual: PtzMotorPose,
        desired: PtzMotorPose,
        config: Any,
        now: float,
    ) -> float:
        error = desired.zoom - actual.zoom
        deadband = int(config.zoom_deadband_units)
        direction = 0 if abs(error) <= deadband else (1 if error > 0 else -1)
        if direction != self._zoom_direction:
            self._zoom_direction = direction
            self._zoom_direction_since = now
            return 0.0
        if direction == 0:
            return 0.0
        if now - self._zoom_direction_since < float(config.continuous_zoom_dwell_seconds):
            return 0.0
        ratio = min(1.0, max(0.0, (abs(error) - deadband) / 500.0))
        magnitude = 2.0 + ratio * (float(config.continuous_zoom_max_speed) - 2.0)
        return math.copysign(magnitude, direction)


def _image_axis_target(error: float, deadband: float, maximum: float) -> float:
    magnitude = abs(error)
    if magnitude <= deadband:
        return 0.0
    normalized = min(1.0, (magnitude - deadband) / max(0.05, 0.32 - deadband))
    speed = 2.0 + normalized * max(0.0, maximum - 2.0)
    return math.copysign(speed, error)


def _predicted_error(error: float, velocity: float, lookahead: float) -> float:
    predicted = error + velocity * lookahead
    # Prediction should accelerate pursuit, not reverse the motor while the
    # subject remains on the same side of center. A genuine direction change
    # is acted on as soon as the observed/filtered error crosses center.
    if abs(error) > 1e-6 and predicted * error < 0:
        return 0.0
    return max(-0.50, min(0.50, predicted))


def _motor_axis_target(
    error: int,
    deadband: int,
    full_speed_error: float,
    maximum: float,
) -> float:
    magnitude = abs(int(error))
    if magnitude <= deadband:
        return 0.0
    normalized = min(1.0, (magnitude - deadband) / max(1.0, full_speed_error))
    speed = 2.0 + normalized * max(0.0, maximum - 2.0)
    return math.copysign(speed, error)


def _slew(current: float, target: float, maximum_change: float) -> float:
    if target > current:
        return min(target, current + maximum_change)
    return max(target, current - maximum_change)


def _aw_speed(delta: float) -> int:
    selected = int(round(delta))
    if selected == 0:
        return 50
    return max(1, min(99, 50 + selected))
