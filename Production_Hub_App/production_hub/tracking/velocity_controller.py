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
        group_tracking: bool = False,
        ease_motion: bool = False,
        responsive_tracking: bool = False,
        image_motion_scale: tuple[float, float] | None = None,
    ) -> PtzVelocityCommand:
        elapsed = (
            1.0 / max(1.0, float(config.decision_fps))
            if self._last_update is None
            else max(0.04, min(0.50, now - self._last_update))
        )
        self._last_update = now
        easing = ease_motion and not group_tracking and live_bounds is not None

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
            # A one-step correction should not get the same velocity lead as
            # a person walking toward the edge of the shot.
            pan_prediction = prediction * (_smoothstep(abs(x_error) / 0.25) if easing else 1.0)
            tilt_prediction = prediction * (_smoothstep(abs(y_error) / 0.25) if easing else 1.0)
            predicted_x_error = _predicted_error(
                self._filtered_x_error,
                self._subject_velocity_x,
                pan_prediction,
            )
            predicted_y_error = _predicted_error(
                self._filtered_y_error,
                self._subject_velocity_y,
                tilt_prediction * 0.55,
            )
            image_target = _eased_image_axis_target if easing else _image_axis_target
            target_pan = image_target(
                predicted_x_error,
                float(config.subject_center_deadband_x),
                float(config.continuous_pan_max_speed),
            )
            # Panasonic tilt speed is above 50 for up and below 50 for down.
            target_tilt = -image_target(
                predicted_y_error,
                float(config.subject_center_deadband_y),
                float(config.continuous_tilt_max_speed),
            )
            pan_urgency = _catch_up_urgency(predicted_x_error)
            tilt_urgency = _catch_up_urgency(predicted_y_error)
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
                max(10, round(config.pan_deadband_units * image_motion_scale[0])) if image_motion_scale else int(config.pan_deadband_units),
                1400.0,
                float(config.continuous_pan_max_speed),
            )
            target_tilt = -_motor_axis_target(
                desired.tilt - actual.tilt,
                max(10, round(config.tilt_deadband_units * image_motion_scale[1])) if image_motion_scale else int(config.tilt_deadband_units),
                900.0,
                float(config.continuous_tilt_max_speed),
            )
            pan_urgency = _catch_up_urgency((desired.pan - actual.pan) / 1400.0 * .32)
            tilt_urgency = _catch_up_urgency((desired.tilt - actual.tilt) / 900.0 * .32)
            self._filtered_x_error = 0.0
            self._filtered_y_error = 0.0
            self._last_live_center = None
            self._subject_velocity_x = 0.0
            self._subject_velocity_y = 0.0

        urgent_widen = group_tracking and desired.zoom < actual.zoom
        target_zoom = self._zoom_target(
            actual, desired, config, now, urgent_widen=urgent_widen,
        )
        if live_bounds is None and target_zoom > 0:
            # Keep the field of view open during wide-camera reacquisition.
            # Zooming in before the locked person is visible in the PTZ image
            # makes reacquisition harder and can immediately lose them again.
            target_zoom = 0.0
        maximum_change = float(config.continuous_speed_slew_per_second) * elapsed
        pan_change = tilt_change = maximum_change
        if responsive_tracking:
            # The live camera overshot repeatedly at zoom 3452-3663 with the
            # new wide-shot boost. Taper that extra gain out before telephoto;
            # retain the established motion envelope for tight podium shots.
            # Apply the same weight to acceleration and speed, including
            # Audience recovery, so losing a detection cannot restore the boost.
            boost_weight = 1.0 - _smoothstep((actual.zoom - 2900.0) / 300.0)
            # A tight shot still needs escape authority at the frame edge.
            # Keep the telephoto suppression for ordinary framing corrections.
            pan_edge = _smoothstep((abs(x_error) - .38) / .10) if live_bounds is not None and x_error * self._subject_velocity_x > .02 else 0.0
            tilt_edge = _smoothstep((abs(y_error) - .38) / .10) if live_bounds is not None and y_error * self._subject_velocity_y > .02 else 0.0
            pan_urgency *= max(boost_weight, pan_edge)
            tilt_urgency *= max(boost_weight, tilt_edge)
            # Keep the existing gentle gain near centre; reserve additional
            # speed and acceleration for pursuit near an edge or reacquisition.
            target_pan = _catch_up_target(target_pan, pan_urgency, config.continuous_pan_max_speed, 44)
            target_tilt = _catch_up_target(target_tilt, tilt_urgency, config.continuous_tilt_max_speed, 32)
            extra_slew = max(0.0, 60.0 - float(config.continuous_speed_slew_per_second)) * elapsed
            pan_change += extra_slew * max(boost_weight, pan_edge) * max(pan_urgency, _catch_up_urgency(self._pan_delta / 44.0 * .32))
            tilt_change += extra_slew * max(boost_weight, tilt_edge) * max(tilt_urgency, _catch_up_urgency(self._tilt_delta / 32.0 * .32))
        if image_motion_scale is not None and live_bounds is not None:
            # Keep fine corrections gentle, but restore pursuit authority as
            # a subject approaches the frame edge. A fixed telephoto cap could
            # otherwise make a fast-moving pair impossible to catch.
            pan_scale = image_motion_scale[0]
            tilt_scale = image_motion_scale[1]
            pan_scale += (1-pan_scale)*_smoothstep((abs(x_error)-.20)/.30)
            tilt_scale += (1-tilt_scale)*_smoothstep((abs(y_error)-.20)/.30)
            target_pan *= pan_scale
            target_tilt *= tilt_scale
        if ease_motion:
            # Every routine pan/tilt slowdown gets the same taper, including
            # group tracking and calibrated recovery. Only single-person
            # small corrections also soften acceleration near the centre.
            target_pan = _ease_axis(
                self._pan_delta, target_pan, elapsed,
                small_correction_error=x_error if easing else None,
            )
            target_tilt = _ease_axis(
                self._tilt_delta, target_tilt, elapsed,
                small_correction_error=y_error if easing else None,
            )
        self._pan_delta = _slew(self._pan_delta, target_pan, pan_change)
        self._tilt_delta = _slew(self._tilt_delta, target_tilt, tilt_change)
        group_widening = group_tracking and (target_zoom < 0 or self._zoom_delta < 0)
        if group_widening and target_zoom == 0:
            # Stop promptly at the requested width rather than coasting wider
            # at the faster group speed.
            self._zoom_delta = 0.0
        else:
            self._zoom_delta = _slew(
                self._zoom_delta,
                target_zoom,
                max(1.0, maximum_change * (1.5 if group_widening else 0.55)),
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
        *,
        urgent_widen: bool = False,
    ) -> float:
        error = desired.zoom - actual.zoom
        deadband = int(config.zoom_deadband_units)
        direction = 0 if abs(error) <= deadband else (1 if error > 0 else -1)
        if direction != self._zoom_direction:
            self._zoom_direction = direction
            self._zoom_direction_since = now
            if not (urgent_widen and direction < 0):
                return 0.0
        if direction == 0:
            return 0.0
        if (
            not urgent_widen
            and now - self._zoom_direction_since < float(config.continuous_zoom_dwell_seconds)
        ):
            return 0.0
        if urgent_widen:
            ratio = min(1.0, max(0.0, (abs(error) - deadband) / 300.0))
            maximum = float(config.group_zoom_out_max_speed)
            minimum = min(6.0, maximum)
            return -(minimum + ratio * (maximum - minimum))
        ratio = min(1.0, max(0.0, (abs(error) - deadband) / 500.0))
        magnitude = 2.0 + ratio * (float(config.continuous_zoom_max_speed) - 2.0)
        return math.copysign(magnitude, direction)


def _smoothstep(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3.0 - 2.0 * value)


def _catch_up_urgency(error: float) -> float:
    return _smoothstep((abs(error) - .16) / .16)


def _catch_up_target(target: float, urgency: float, configured: float, ceiling: float) -> float:
    if target == 0:
        return 0.0
    return math.copysign(min(49.0, abs(target) + urgency * max(0.0, ceiling - configured)), target)


def _ease_axis(
    current: float, target: float, elapsed: float,
    *, small_correction_error: float | None,
) -> float:
    blend = 1.0 - math.exp(-elapsed / 0.4)
    if current * target >= 0.0 and abs(target) < abs(current):
        return current + (target - current) * blend
    # A reversal must brake promptly instead of prolonging motion away from
    # the subject. Emergency/manual stops use halt(), bypassing easing entirely.
    if current * target < 0.0 or small_correction_error is None:
        return target
    blend += (1.0 - blend) * _smoothstep((abs(small_correction_error) - 0.14) / 0.12)
    return current + (target - current) * blend


def _eased_image_axis_target(error: float, deadband: float, maximum: float) -> float:
    # Remove the old jump from stopped to speed 2 at the deadband boundary.
    return _image_axis_target(error, deadband, maximum) * _smoothstep(
        (abs(error) - deadband) / 0.08
    )


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
