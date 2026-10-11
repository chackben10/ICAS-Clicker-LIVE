from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from production_hub.integrations.panasonic_awp import speed_model
from production_hub.tracking.models import NormalizedRect
from production_hub.tracking.ptz_geometry import PtzMotorPose

# Responsive (Production Hub Dev) pursuit works in picture speed -- frame
# widths or heights per second -- and converts to AW speed through the
# measured AW-HE120 model. The camera already slows pan/tilt as it zooms in,
# so a fixed speed cap left it unable to keep up with a walker at sermon zoom
# (speed 26 is ~0.23 frames/s there); picture speed is the same at any zoom.
_PURSUIT_GAIN = 1.6
_PURSUIT_LOOKAHEAD = 0.30
# Seconds between sending a speed and the camera moving at it.
_COMMAND_DELAY = 0.10
_PURSUIT_MAX_PAN = 1.2
_PURSUIT_MAX_TILT = 0.8
_PURSUIT_ACCELERATION = 2.4
_PURSUIT_BRAKING = 6.0
_WALK_THRESHOLD = 0.06
_CENTER_TOLERANCE_X = 0.05
_RECOVERY_GAIN = 1.4
_RECOVERY_MAX_SPEED = 0.6
_VELOCITY_TIME_CONSTANT = 0.35
_IMAGE_EDGE = 0.015
_ESCAPE_ZOOM_UNITS = 320
_ESCAPE_RESTORE_SPEED = 14.0
_TIGHTEN_EDGE_MARGIN = 0.08
_HEADROOM_TRIGGER = 0.04
_HEADROOM_TARGET = 0.07
_OSCILLATION_SPEED = 0.10
_MINIMUM_GAIN = 0.25
_REDETECTION_JUMP = 0.25
_MAXIMUM_SUBJECT_SPEED = 1.0


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
        # Responsive pursuit: commanded aim speed in picture units (+x right,
        # +y down), the subject's own motion as a still camera would see it,
        # and recent aim speeds to line up with late-arriving detections.
        self._aim_x = 0.0
        self._aim_y = 0.0
        self._world_vx = 0.0
        self._world_vy = 0.0
        self._aim_history: list[tuple[float, float, float]] = []
        self._escape_until = 0.0
        self._escape_floor: float | None = None
        self._escaped_at = -1e9
        self._last_live_edges: tuple[float, float] | None = None
        self._last_observed_at: float | None = None
        # Whether the last update saw anything worth fast analysis.
        self.motion_active = False
        # Per-axis loop gain, cut when commands flip back and forth: the
        # speed model can be off by a large factor (calibration footprints
        # disagree 1.6x at telephoto), and over-correction must die out.
        self._gain_x = 1.0
        self._gain_y = 1.0
        self._last_target_x = 0.0
        self._last_target_y = 0.0
        self._pose_history: list[tuple[float, float, float]] = []
        # Consecutive same-direction motion readings per axis.
        self._streak_x = 0
        self._streak_y = 0

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
        self._aim_x = 0.0
        self._aim_y = 0.0
        self._world_vx = 0.0
        self._world_vy = 0.0
        self._aim_history = []
        self._escape_until = 0.0
        self._escape_floor = None
        self._escaped_at = -1e9
        self._last_live_edges = None
        self._last_observed_at = None
        self._gain_x = 1.0
        self._gain_y = 1.0
        self._last_target_x = 0.0
        self._last_target_y = 0.0
        self._pose_history = []
        self._streak_x = 0
        self._streak_y = 0

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
        frame_spans: tuple[float, float] | None = None,
        observed_at: float | None = None,
    ) -> PtzVelocityCommand:
        elapsed = (
            1.0 / max(1.0, float(config.decision_fps))
            if self._last_update is None
            else max(0.04, min(0.50, now - self._last_update))
        )
        self._last_update = now
        if responsive_tracking:
            return self._pursue(
                actual=actual,
                desired=desired,
                config=config,
                now=now,
                elapsed=elapsed,
                live_bounds=live_bounds,
                group_tracking=group_tracking,
                frame_spans=frame_spans,
                observed_at=observed_at,
            )
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

    def _pursue(
        self,
        *,
        actual: PtzMotorPose,
        desired: PtzMotorPose,
        config: Any,
        now: float,
        elapsed: float,
        live_bounds: NormalizedRect | None,
        group_tracking: bool,
        frame_spans: tuple[float, float] | None,
        observed_at: float | None = None,
    ) -> PtzVelocityCommand:
        """Follow in picture speed: match the subject's motion, then centre it.

        Detections are ~0.3 s old, so the camera aim that produced them is
        looked up from that long ago; adding it back to the image velocity
        gives the subject's own motion, which the camera matches outright
        instead of discovering it as a growing error. A still subject inside
        the centre band holds the camera completely still.
        """

        zoom = actual.zoom
        escaping = False
        self._aim_history.append((now, self._aim_x, self._aim_y))
        self._aim_history = [item for item in self._aim_history if item[0] >= now - 1.0]
        self._pose_history.append((now, float(actual.pan), float(actual.tilt)))
        self._pose_history = [item for item in self._pose_history if item[0] >= now - 2.0]
        pan_span, tilt_span = frame_spans or (
            speed_model.frame_units("pan", zoom),
            speed_model.frame_units("tilt", zoom),
        )
        # When the analysed frame was captured; older pipelines without a
        # stamp fall back to the typical live delay.
        observed = (
            observed_at
            if observed_at is not None and 0.0 <= now - observed_at < 2.0
            else now - _PURSUIT_LOOKAHEAD
        )
        lookahead = max(0.0, now - observed) + _COMMAND_DELAY
        if live_bounds is not None:
            center_x, center_y = live_bounds.center
            x_error = center_x - 0.50
            y_error = center_y - 0.48
            # A box cut off by the frame edge is incomplete: its centre sits
            # inside the frame while the person is further out than it shows.
            # A group wider than the frame touches both edges; its centre is
            # the honest measurement, so only a one-sided clip is pushed out.
            clipped_left = live_bounds.x <= _IMAGE_EDGE
            clipped_right = live_bounds.x + live_bounds.width >= 1.0 - _IMAGE_EDGE
            # For a group, a clipped member means the shot is too tight, not
            # that the group moved: forcing a hard pan toward whichever side
            # clipped just clips the other side next -- a smaller whip pan.
            # Groups rely on escape widening instead.
            if not group_tracking:
                if clipped_left and not clipped_right and x_error < 0:
                    x_error = min(x_error, -0.45)
                if clipped_right and not clipped_left and x_error > 0:
                    x_error = max(x_error, 0.45)
            if (
                live_bounds.y > 0.45
                and live_bounds.y + live_bounds.height >= 1.0 - _IMAGE_EDGE
            ):
                # Head in the lower half and body off the bottom: the person
                # stepped down or crouched, so tilt must go with them.
                y_error = max(y_error, 0.40)
            previous_at = self._last_observed_at
            if self._last_live_center is None or previous_at is None:
                self._last_live_center = (center_x, center_y)
                self._last_observed_at = observed
            elif (
                abs(center_x - self._last_live_center[0]) > _REDETECTION_JUMP
                or abs(center_y - self._last_live_center[1]) > _REDETECTION_JUMP
            ):
                # A box that teleports a quarter frame between two analyses
                # is a re-detection or identity glitch, not a walk; using it
                # as speed would fling the camera. Re-baseline instead.
                self._last_live_center = (center_x, center_y)
                self._last_observed_at = observed
                self._world_vx *= 0.5
                self._world_vy *= 0.5
            elif observed - previous_at > 0.02:
                # Velocity between two real detections, with the camera's own
                # motion over that same window added back: the subject's
                # motion as a still camera would have seen it.
                span = observed - previous_at
                image_vx = (center_x - self._last_live_center[0]) / span
                image_vy = (center_y - self._last_live_center[1]) / span
                # How far the camera really moved between the two frames,
                # from its reported position -- not from the speed model,
                # whose error would otherwise be mistaken for the subject
                # moving and fed straight back as a limit cycle.
                camera_x, camera_y = self._camera_motion(
                    previous_at, observed, pan_span, tilt_span,
                )
                world_vx = max(-_MAXIMUM_SUBJECT_SPEED, min(_MAXIMUM_SUBJECT_SPEED, image_vx + camera_x))
                world_vy = max(-_MAXIMUM_SUBJECT_SPEED, min(_MAXIMUM_SUBJECT_SPEED, image_vy + camera_y))
                self._streak_x = _motion_streak(self._streak_x, world_vx)
                self._streak_y = _motion_streak(self._streak_y, world_vy)
                blend = 1.0 - math.exp(-span / _VELOCITY_TIME_CONSTANT)
                self._world_vx += (world_vx - self._world_vx) * blend
                self._world_vy += (world_vy - self._world_vy) * blend
                self._last_live_center = (center_x, center_y)
                self._last_observed_at = observed
            # Widen rather than lose someone: a person still heading out past
            # the comfortable band is moving faster than a tight shot can
            # follow, so open the frame until they are safely inside it.
            # Only motion the camera is not already matching moves them in
            # the picture.
            drift_x = self._world_vx - self._aim_x
            ahead_x = x_error + drift_x * lookahead
            # Edges, not just the centre: a pair splitting symmetrically keeps
            # a still centre while both sides race for the frame edges.
            left, right = live_bounds.x, live_bounds.x + live_bounds.width
            edge_escape = False
            if self._last_live_edges is not None:
                left_ahead = left + (left - self._last_live_edges[0]) / elapsed * lookahead
                right_ahead = right + (right - self._last_live_edges[1]) / elapsed * lookahead
                # Zooming in also pushes edges outward; that is not escape.
                outward = 0.05 * elapsed
                edge_escape = self._zoom_delta <= 0 and (
                    (left_ahead < 0.04 and left < self._last_live_edges[0] - outward)
                    or (right_ahead > 0.96 and right > self._last_live_edges[1] + outward)
                )
            self._last_live_edges = (left, right)
            escaping = edge_escape or (
                abs(ahead_x) > 0.30 and x_error * drift_x > 0.0
            ) or abs(x_error) >= 0.45 or y_error >= 0.40 or (
                group_tracking and live_bounds.width > 0.92
            )
            # One reading cannot tell a re-frame from the start of a walk;
            # match the subject's speed once two agree.
            trusted_vx = self._world_vx * min(1.0, abs(self._streak_x) / 2.0)
            trusted_vy = self._world_vy * min(1.0, abs(self._streak_y) / 2.0)
            target_x = _pursuit_axis(
                x_error, trusted_vx, self._aim_x,
                hold=_CENTER_TOLERANCE_X, lookahead=lookahead,
            )
            target_y = _pursuit_axis(
                y_error, trusted_vy, self._aim_y,
                hold=float(config.subject_center_deadband_y), lookahead=lookahead,
            )
            # Headroom is not a centring preference: a head at the top edge
            # (someone standing back up, a tall subject) is cut off however
            # small the centre error, so it bypasses the band -- and acts on
            # where a rising head will be by the time the camera responds.
            head_top = live_bounds.y + min(0.0, self._world_vy - self._aim_y) * lookahead
            if head_top < _HEADROOM_TRIGGER:
                target_y = min(
                    target_y,
                    min(0.0, self._world_vy) - _PURSUIT_GAIN * (_HEADROOM_TARGET - head_top),
                )
        else:
            # Recovery from the wide camera has only a motor-space target.
            # Panasonic pan rises when the aim moves left and tilt rises when
            # it moves down, so convert the motor error into picture terms.
            self._last_live_center = None
            self._last_live_edges = None
            self._last_observed_at = None
            self._world_vx = 0.0
            self._world_vy = 0.0
            pan_error = desired.pan - actual.pan
            tilt_error = desired.tilt - actual.tilt
            target_x = (
                0.0 if abs(pan_error) <= int(config.pan_deadband_units)
                else -_RECOVERY_GAIN * pan_error / max(1.0, pan_span)
            )
            target_y = (
                0.0 if abs(tilt_error) <= int(config.tilt_deadband_units)
                else _RECOVERY_GAIN * tilt_error / max(1.0, tilt_span)
            )
            # The wide camera's estimate is coarse; full speed on it is what
            # whipped the live camera past people at telephoto.
            target_x = max(-_RECOVERY_MAX_SPEED, min(_RECOVERY_MAX_SPEED, target_x))
            target_y = max(-_RECOVERY_MAX_SPEED, min(_RECOVERY_MAX_SPEED, target_y))
        self._gain_x, self._last_target_x = _adapt_gain(self._gain_x, self._last_target_x, target_x, elapsed)
        self._gain_y, self._last_target_y = _adapt_gain(self._gain_y, self._last_target_y, target_y, elapsed)
        target_x *= self._gain_x
        target_y *= self._gain_y
        target_x = max(-_PURSUIT_MAX_PAN, min(_PURSUIT_MAX_PAN, target_x))
        target_y = max(-_PURSUIT_MAX_TILT, min(_PURSUIT_MAX_TILT, target_y))
        self._aim_x = _ramp(self._aim_x, target_x, elapsed)
        self._aim_y = _ramp(self._aim_y, target_y, elapsed)
        # Aim right is a positive AW pan offset; aim down is a negative tilt one.
        self._pan_delta = speed_model.offset_for_picture_speed("pan", self._aim_x, zoom)
        self._tilt_delta = speed_model.offset_for_picture_speed("tilt", -self._aim_y, zoom)
        # Report what the camera will actually do after integer rounding, so
        # the next lookup of the aim matches the picture that results.
        self._aim_x = speed_model.picture_speed("pan", round(self._pan_delta), zoom)
        self._aim_y = -speed_model.picture_speed("tilt", round(self._tilt_delta), zoom)

        urgent_widen = (group_tracking or live_bounds is None) and desired.zoom < actual.zoom
        target_zoom = self._zoom_target(actual, desired, config, now, urgent_widen=urgent_widen)
        if live_bounds is None and target_zoom > 0:
            target_zoom = 0.0
        if escaping:
            if self._escape_floor is None or now - self._escaped_at > 3.0:
                # Bound an escape episode to about 1.5x the field of view it
                # started from, however many times it re-triggers.
                self._escape_floor = actual.zoom - _ESCAPE_ZOOM_UNITS
            self._escape_until = now + 0.4
            self._escaped_at = now
        walking = max(abs(self._world_vx), abs(self._world_vy)) > 2.5 * _WALK_THRESHOLD
        if now < self._escape_until and (
            self._escape_floor is None or actual.zoom > self._escape_floor
        ):
            urgent_widen = True
            target_zoom = -float(config.group_zoom_out_max_speed)
            # Someone is about to leave the shot: start widening at once.
            self._zoom_delta = min(self._zoom_delta, target_zoom)
        elif target_zoom > 0 and live_bounds is not None and (
            live_bounds.x < _TIGHTEN_EDGE_MARGIN
            or live_bounds.x + live_bounds.width > 1.0 - _TIGHTEN_EDGE_MARGIN
        ):
            # Tightening into the side edges is the zoom hunt seen live with a
            # pair: fill to the edge, touch it, widen, tighten again.
            target_zoom = 0.0
        elif walking and target_zoom > 0:
            # Tightening on someone mid-stride is what walks them out of
            # shot; wait until they settle.
            target_zoom = 0.0
        elif target_zoom > 0:
            # Return from a widened shot (escape or handoff) at a steady pace
            # rather than creeping for many seconds: scale with the distance
            # still to go, gentle for small trims.
            remaining = desired.zoom - actual.zoom - int(config.zoom_deadband_units)
            target_zoom = max(
                target_zoom,
                min(_ESCAPE_RESTORE_SPEED, 2.0 + remaining / 250.0 * (_ESCAPE_RESTORE_SPEED - 2.0)),
            )
        maximum_change = float(config.continuous_speed_slew_per_second) * elapsed
        widening = urgent_widen and (target_zoom < 0 or self._zoom_delta < 0)
        if widening and target_zoom == 0:
            self._zoom_delta = 0.0
        else:
            self._zoom_delta = _slew(
                self._zoom_delta,
                target_zoom,
                max(1.0, maximum_change * (1.5 if widening else 0.55)),
            )
        self.motion_active = bool(
            live_bounds is None
            or escaping
            or now < self._escape_until
            or max(abs(self._aim_x), abs(self._aim_y)) > 0.02
            or abs(self._zoom_delta) >= 1.0
            or max(abs(self._world_vx), abs(self._world_vy)) > _WALK_THRESHOLD
            or abs(live_bounds.center[0] - 0.5) > 0.25
        )
        return PtzVelocityCommand(
            pan=_aw_speed(self._pan_delta),
            tilt=_aw_speed(self._tilt_delta),
            zoom=_aw_speed(self._zoom_delta),
        )

    def _camera_motion(
        self, start: float, end: float, pan_span: float, tilt_span: float,
    ) -> tuple[float, float]:
        """Measured aim speed (picture units/s, +x right, +y down) over a window."""

        def pose_at(moment: float) -> tuple[float, float]:
            history = self._pose_history
            if moment <= history[0][0]:
                return history[0][1], history[0][2]
            for (t0, p0, q0), (t1, p1, q1) in zip(history, history[1:]):
                if t0 <= moment <= t1:
                    fraction = (moment - t0) / max(1e-6, t1 - t0)
                    return p0 + (p1 - p0) * fraction, q0 + (q1 - q0) * fraction
            return history[-1][1], history[-1][2]

        span = max(1e-3, end - start)
        pan0, tilt0 = pose_at(start)
        pan1, tilt1 = pose_at(end)
        # Aiming right lowers Panasonic pan; aiming down raises tilt.
        return (
            -(pan1 - pan0) / span / max(1.0, pan_span),
            (tilt1 - tilt0) / span / max(1.0, tilt_span),
        )

    def _mean_aim(self, start: float, end: float) -> tuple[float, float]:
        samples = [self._aim_at(start + (end - start) * k / 4.0) for k in range(5)]
        return (
            sum(item[0] for item in samples) / len(samples),
            sum(item[1] for item in samples) / len(samples),
        )

    def _aim_at(self, moment: float) -> tuple[float, float]:
        for stamp, aim_x, aim_y in reversed(self._aim_history):
            if stamp <= moment:
                return aim_x, aim_y
        if self._aim_history:
            return self._aim_history[0][1], self._aim_history[0][2]
        return 0.0, 0.0

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


def _pursuit_axis(
    error: float, world_speed: float, aim: float, *, hold: float, lookahead: float,
) -> float:
    """Aim speed for one axis: the subject's own speed plus a centring term."""

    walking = _smoothstep((abs(world_speed) - _WALK_THRESHOLD) / _WALK_THRESHOLD)
    # Where the subject will be once this command takes effect.
    predicted = error + (world_speed - aim) * lookahead * walking
    # Nothing inside the hold band (a still, centred subject never creeps);
    # full authority by twice the band.
    centring = _PURSUIT_GAIN * predicted * _smoothstep((abs(predicted) - hold) / hold)
    return world_speed * walking + centring


def _motion_streak(streak: int, speed: float) -> int:
    """Signed count of consecutive readings moving the same way."""

    if abs(speed) <= _WALK_THRESHOLD:
        return 0
    direction = 1 if speed > 0 else -1
    if streak * direction > 0:
        return streak + direction
    return direction


def _adapt_gain(gain: float, previous: float, target: float, elapsed: float) -> tuple[float, float]:
    """Halve-ish the gain on a meaningful command reversal; recover over ~6 s."""

    if previous * target < 0.0 and min(abs(previous), abs(target)) > _OSCILLATION_SPEED:
        gain = max(_MINIMUM_GAIN, gain * 0.6)
    else:
        gain = min(1.0, gain + elapsed / 6.0)
    return gain, target


def _ramp(current: float, target: float, elapsed: float) -> float:
    """Accelerate gently, but brake and reverse promptly."""

    slowing = current * target < 0 or abs(target) < abs(current)
    step = (_PURSUIT_BRAKING if slowing else _PURSUIT_ACCELERATION) * elapsed
    if abs(target - current) <= step:
        return target
    return current + math.copysign(step, target - current)


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
