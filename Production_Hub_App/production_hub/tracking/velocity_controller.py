from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from production_hub.integrations.panasonic_awp import speed_model
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
        self._walk = _WalkEvidence()
        self._walk_tilt = _WalkEvidence()
        self._exit_error = (0.0, 0.0)
        self._exit_top = 0.5
        self._rising_frames = 0
        self._rising_since: float | None = None
        self._climbing = False
        self._settled_since: float | None = None
        self._has_moved = False
        self._escape_episode = False
        self._framed_x = False
        self._framed_y = False
        self._headroom_ref: tuple[float, float, float] | None = None  # (head top, zoom, tilt) when a breath out began

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
        self._walk = _WalkEvidence()
        self._walk_tilt = _WalkEvidence()
        self._exit_error = (0.0, 0.0)
        self._exit_top = 0.5
        self._rising_frames = 0
        self._rising_since: float | None = None
        self._climbing = False
        self._settled_since: float | None = None
        self._has_moved = False
        self._escape_episode = False
        self._framed_x = False
        self._framed_y = False
        self._headroom_ref: tuple[float, float, float] | None = None  # (head top, zoom, tilt) when a breath out began

    def halt(self) -> PtzVelocityCommand:
        self.reset()
        return PtzVelocityCommand()

    def coast_through_loss(self, now: float) -> PtzVelocityCommand:
        """Ease pan/tilt down through a brief detection gap.

        Cutting a walking-pace pan to zero in one step is the most visible
        lurch a tracked shot makes, and it throws away the pace the camera
        needs when the subject reappears a moment later. Zoom stops at once.
        """

        elapsed = 0.0 if self._last_update is None else max(0.0, min(0.5, now - self._last_update))
        self._last_update = now
        decay = math.exp(-elapsed / LOSS_COAST_TAU)
        self._pan_delta = 0.0 if abs(self._pan_delta * decay) < 1.0 else self._pan_delta * decay
        self._tilt_delta = 0.0 if abs(self._tilt_delta * decay) < 1.0 else self._tilt_delta * decay
        self._zoom_delta = 0.0
        return PtzVelocityCommand(pan=_aw_speed(self._pan_delta), tilt=_aw_speed(self._tilt_delta))

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
        safe_zone_control: bool = False,
        feedforward_pan_units: float | None = None,
        feedforward_tilt_units: float | None = None,
        confirmed_wide_target: bool = False,
        front_of_stage: bool = False,
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
            # Where the subject was when last seen: if they leave the shot,
            # this says through which edge.
            self._exit_error = (x_error, y_error)
            self._exit_top = live_bounds.y
            # The smoothing settings are per 0.25 s decision; keep their time
            # constants at other decision rates.
            smoothing = _per_period(float(config.continuous_error_smoothing), elapsed)
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
                velocity_smoothing = _per_period(float(config.continuous_velocity_smoothing), elapsed)
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
            pan_urgency = _catch_up_urgency((desired.pan - actual.pan) / 1400.0 * .32)
            tilt_urgency = _catch_up_urgency((desired.tilt - actual.tilt) / 900.0 * .32)
            telephoto = _smoothstep((actual.zoom - 2900.0) / 300.0)
            if responsive_tracking and not group_tracking and confirmed_wide_target and telephoto > 0.0:
                # A confirmed wide-camera sighting of the locked person: close
                # the gap at whatever speed this zoom needs instead of a flat
                # cap that at sermon zoom moves slower than a walker.
                pan_error = desired.pan - actual.pan
                if abs(pan_error) > int(config.pan_deadband_units):
                    needed = speed_model.offset_for_rate(
                        "pan", abs(pan_error) / WIDE_FOLLOW_SECONDS, actual.zoom, maximum=PURSUIT_PAN_MAX,
                    ) * telephoto
                    target_pan = -math.copysign(max(abs(target_pan), needed), pan_error)
                tilt_error = desired.tilt - actual.tilt
                if abs(tilt_error) > int(config.tilt_deadband_units):
                    needed = speed_model.offset_for_rate(
                        "tilt", abs(tilt_error) / WIDE_FOLLOW_SECONDS, actual.zoom, maximum=PURSUIT_TILT_MAX,
                    ) * telephoto
                    target_tilt = -math.copysign(max(abs(target_tilt), needed), tilt_error)
            if (
                responsive_tracking
                and not group_tracking
                and feedforward_pan_units is not None
                and telephoto > 0.0
            ):
                # The walker has just left the PTZ frame: keep going at their
                # last measured pace (it fades within about a second) so the
                # camera brings them back into shot, rather than trusting the
                # wide camera's coarse position at pursuit speed -- but only
                # along the axis they actually left by.
                pace = abs(feedforward_pan_units)
                left_by_side = self._exit_error[0] * _walk_image_direction("pan", feedforward_pan_units) > WALKER_EXIT_EDGE
                weight = float(left_by_side) * telephoto * _smoothstep(
                    (pace - WALKER_FEEDFORWARD_START) / (WALKER_FEEDFORWARD_FULL - WALKER_FEEDFORWARD_START)
                )
                coast = -math.copysign(
                    speed_model.offset_for_rate("pan", pace, actual.zoom, maximum=PURSUIT_PAN_MAX) * weight,
                    feedforward_pan_units,
                )
                if coast * target_pan >= 0.0 and abs(coast) > abs(target_pan):
                    target_pan = coast
            if (
                responsive_tracking
                and not group_tracking
                and feedforward_tilt_units is not None
                and telephoto > 0.0
            ):
                pace = abs(feedforward_tilt_units)
                left_by_edge = self._exit_error[1] * _walk_image_direction("tilt", feedforward_tilt_units) > WALKER_EXIT_EDGE
                weight = float(left_by_edge) * telephoto * _smoothstep(
                    (pace - WALKER_FEEDFORWARD_START) / (WALKER_FEEDFORWARD_FULL - WALKER_FEEDFORWARD_START)
                )
                coast = -math.copysign(
                    speed_model.offset_for_rate("tilt", pace, actual.zoom, maximum=PURSUIT_TILT_MAX) * weight,
                    feedforward_tilt_units,
                )
                if coast * target_tilt >= 0.0 and abs(coast) > abs(target_tilt):
                    target_tilt = coast
            if (
                responsive_tracking
                and safe_zone_control
                and not group_tracking
                and abs(desired.tilt - actual.tilt) < OFF_SHOT_TILT_FRAMES * speed_model.frame_units("tilt", actual.zoom)
            ):
                # Off the PTZ shot, the wide camera's vertical estimate is
                # rough, and someone leaving by the side has not changed
                # height: a difference this small would still be in shot.
                target_tilt = 0.0
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
        safe_image_control = safe_zone_control and live_bounds is not None
        if safe_image_control:
            # Latest observed position is authoritative. Image velocity also
            # includes camera motion and must not kick the camera out of the
            # safe zone or inflate an edge correction.
            wide_weight = 1.0 - _smoothstep((actual.zoom - 2900.0) / 300.0)
            pan_max = float(config.continuous_pan_max_speed)
            tilt_max = float(config.continuous_tilt_max_speed)
            if responsive_tracking:
                pan_max += max(0.0, 44-pan_max)*wide_weight
                tilt_max += max(0.0, 32-tilt_max)*wide_weight
            pan_edge = max(.30, .44-live_bounds.width*.25) if group_tracking else .44
            target_pan = _safe_zone_target(x_error, float(config.subject_center_deadband_x), pan_max, edge_error=pan_edge)
            target_tilt = -_safe_zone_target(y_error, float(config.subject_center_deadband_y), tilt_max)
            if responsive_tracking and not group_tracking and y_error < 0.0:
                # The box centre can sit comfortably inside the dead zone while
                # the head is already on the top edge (someone stepping up, or
                # standing tall in a close shot). The head is what must stay in.
                # Lead a rising head by where it will be when the camera responds.
                head_top = live_bounds.y + min(0.0, self._subject_velocity_y) * HEADROOM_LEAD_SECONDS
                head_out = _smoothstep((HEADROOM_EDGE - head_top) / HEADROOM_EDGE)
                target_tilt = max(target_tilt, head_out * max(tilt_max, HEADROOM_TILT_SPEED))
            if responsive_tracking and not group_tracking:
                # The wide-shot boost above already covers wide zooms; this is
                # its telephoto counterpart. It stays fully on down to the
                # medium shot an escape breathes out to (2900) -- otherwise a
                # walker there gets only the soft position correction -- and
                # fades out below it, toward the wide shots where it hunted.
                telephoto = _smoothstep((actual.zoom - (ESCAPE_WIDE_ZOOM - 200.0)) / 200.0)
                walking = 0.0
                # Framed: stay put. A sway or a small step reads as the camera
                # creeping if it follows; move only once the subject drifts
                # clearly off centre, however they got there.
                self._framed_x = _framed(self._framed_x, x_error, abs(self._pan_delta))
                self._framed_y = _framed(self._framed_y, y_error, abs(self._tilt_delta))
                if feedforward_pan_units is not None:
                    sustained = self._walk.observe(
                        feedforward_pan_units, now,
                        image_agrees=_image_agrees("pan", feedforward_pan_units, self._subject_velocity_x),
                    )
                    pace = abs(feedforward_pan_units) / speed_model.frame_units("pan", actual.zoom)
                    if self._framed_x and sustained > 0.0 and pace >= FRAMED_WALK_RELEASE:
                        # A real walk, not a sway: let go of the framed hold now
                        # rather than waiting for them to reach the drift line.
                        self._framed_x = False
                    if self._framed_x:
                        sustained = 0.0
                    target_pan = _with_walker_feedforward(
                        "pan", target_pan, feedforward_pan_units, actual.zoom, sustained * telephoto,
                        image_velocity=self._subject_velocity_x,
                    )
                    walking = sustained * _smoothstep(
                        (abs(feedforward_pan_units) - WALKER_FEEDFORWARD_START)
                        / (WALKER_FEEDFORWARD_FULL - WALKER_FEEDFORWARD_START)
                    )
                if self._framed_x:
                    target_pan = 0.0
                if self._framed_y and live_bounds.y > HEADROOM_EDGE * 2 and self._headroom_ref is None:
                    target_tilt = 0.0
                if walking <= 0.0:
                    # Standing: re-centre calmly. At a medium shot the wide
                    # boost allows speed 44, which whips the picture at about
                    # a frame per second to recover someone who is not moving.
                    # Someone half out of the shot still gets the full speed.
                    calm = speed_model.offset_for_picture_speed("pan", RECENTRE_FRAMES_PER_SECOND, actual.zoom)
                    calm += (49.0 - calm) * _smoothstep((abs(x_error) - 0.30) / 0.10)
                    target_pan = max(-calm, min(calm, target_pan))
                if feedforward_tilt_units is not None and walking > 0.0:
                    # Vertical pace only while walking: stepping off the stage
                    # is a walk; bending, sitting, or a gesture is not.
                    sustained = self._walk_tilt.observe(
                        feedforward_tilt_units, now,
                        image_agrees=_image_agrees("tilt", feedforward_tilt_units, self._subject_velocity_y),
                    )
                    target_tilt = _with_walker_feedforward(
                        "tilt", target_tilt, feedforward_tilt_units, actual.zoom, sustained * telephoto * walking,
                        image_velocity=self._subject_velocity_y,
                    )
            if (
                responsive_tracking
                and not group_tracking
                and abs(self._pan_delta) > float(config.continuous_pan_max_speed)
                and abs(target_pan) < abs(self._pan_delta)
            ):
                # Coming down from walking pace: at telephoto every 0.1 s at
                # speed 38 is a large slice of the frame, so shed it quickly
                # once the walker stops or turns instead of easing past them.
                self._pan_delta = _brake(self._pan_delta, target_pan, elapsed)
            if (
                responsive_tracking
                and not group_tracking
                and abs(self._tilt_delta) > float(config.continuous_tilt_max_speed)
                and abs(target_tilt) < abs(self._tilt_delta)
            ):
                self._tilt_delta = _brake(self._tilt_delta, target_tilt, elapsed)
            target_pan = _safe_zone_ease(self._pan_delta, target_pan, elapsed)
            target_tilt = _safe_zone_ease(self._tilt_delta, target_tilt, elapsed)
            pan_change = tilt_change = max(maximum_change, 60.0*elapsed)
        elif responsive_tracking:
            # The live camera overshot repeatedly at zoom 3452-3663 with the
            # new wide-shot boost. Taper that extra gain out before telephoto;
            # retain the established motion envelope for tight podium shots.
            # Apply the same weight to acceleration and speed, including
            # Audience recovery, so losing a detection cannot restore the boost.
            boost_weight = 1.0 - _smoothstep((actual.zoom - 2900.0) / 300.0)
            pan_urgency *= boost_weight
            tilt_urgency *= boost_weight
            # Keep the existing gentle gain near centre; reserve additional
            # speed and acceleration for pursuit near an edge or reacquisition.
            target_pan = _catch_up_target(target_pan, pan_urgency, config.continuous_pan_max_speed, 44)
            target_tilt = _catch_up_target(target_tilt, tilt_urgency, config.continuous_tilt_max_speed, 32)
            extra_slew = max(0.0, 60.0 - float(config.continuous_speed_slew_per_second)) * elapsed * boost_weight
            pan_change += extra_slew * max(pan_urgency, _catch_up_urgency(self._pan_delta / 44.0 * .32))
            tilt_change += extra_slew * max(tilt_urgency, _catch_up_urgency(self._tilt_delta / 32.0 * .32))
        if ease_motion and not safe_image_control:
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
        if responsive_tracking and not group_tracking and self._headroom_ref is not None:
            target_tilt = self._keep_headroom(target_tilt, actual, config)
        if responsive_tracking and safe_zone_control and not group_tracking:
            # Nobody on this stage needs the picture to move faster than this
            # (a 3 m/s sprint is ~0.67 frames/s at a medium shot). Beyond it,
            # the wide-shot speed allowance only whips past someone who has
            # already stopped while the detection catches up.
            fastest = speed_model.offset_for_picture_speed("pan", MAX_PICTURE_SPEED, actual.zoom)
            if live_bounds is not None:
                # Someone half out of the shot still gets everything.
                fastest += (49.0 - fastest) * _smoothstep((abs(live_bounds.center[0] - 0.50) - 0.35) / 0.10)
            target_pan = max(-fastest, min(fastest, target_pan))
        self._pan_delta = _slew(self._pan_delta, target_pan, pan_change)
        self._tilt_delta = _slew(self._tilt_delta, target_tilt, tilt_change)
        escape = 0.0
        releasing = False
        if responsive_tracking and not group_tracking:
            escape = self._escape_widening(actual, live_bounds, confirmed_wide_target, desired)
            escape = self._escape_through(escape, actual, live_bounds)
            if escape > 0.0:
                target_zoom = min(target_zoom, -ESCAPE_ZOOM_SPEED * escape)
            moving = escape > 0.0 or live_bounds is None or self._walk.streak >= 1
            if moving:
                self._settled_since = None
                self._has_moved = True
            elif self._settled_since is None:
                self._settled_since = now
            if (
                target_zoom > 0.0
                and self._has_moved
                and (self._settled_since is None or now - self._settled_since < ZOOM_IN_SETTLE_SECONDS)
            ):
                # Hold the wider shot while they move, and until they have
                # stood still a moment: pushing in at every pause is breathing.
                target_zoom = 0.0
            if front_of_stage and live_bounds is not None:
                # Down in front of the stage they will step back up, and the
                # rise takes the head out of a tight shot faster than any
                # reaction. Keep a medium shot there.
                if actual.zoom > FRONT_OF_STAGE_ZOOM + float(config.zoom_deadband_units):
                    target_zoom = min(target_zoom, -float(config.continuous_zoom_max_speed))
                elif target_zoom > 0.0:
                    target_zoom = 0.0
            # Coming out of a fast escape zoom-out: stop it now, rather than
            # letting it drift on wider for seconds after the need has passed.
            releasing = (
                self._zoom_delta < -float(config.continuous_zoom_max_speed)
                and target_zoom > self._zoom_delta
            )
        group_widening = group_tracking and (target_zoom < 0 or self._zoom_delta < 0)
        if group_widening and target_zoom == 0:
            # Stop promptly at the requested width rather than coasting wider
            # at the faster group speed.
            self._zoom_delta = 0.0
        else:
            self._zoom_delta = _slew(
                self._zoom_delta,
                target_zoom,
                max(1.0, maximum_change * (1.5 if group_widening else 0.55), 60.0 * elapsed if escape or releasing else 0.0),
            )

        return PtzVelocityCommand(
            pan=_aw_speed(self._pan_delta),
            tilt=_aw_speed(self._tilt_delta),
            zoom=_aw_speed(self._zoom_delta),
        )

    def _keep_headroom(self, target_tilt: float, actual: PtzMotorPose, config: Any) -> float:
        """Hold the operator's headroom while the shot is breathed out.

        Zooming out happens about the frame centre, so the subject sinks down
        the frame (head from 13% to 30% at medium width). Tilt by exactly the
        amount the zoom change explains -- geometry, not the detection -- so
        this neither chases a bend nor follows a step down.
        """

        reference_top, reference_zoom, reference_tilt = self._headroom_ref
        if not self._escape_episode and actual.zoom >= reference_zoom - HEADROOM_ZOOM_MARGIN:
            self._headroom_ref = None
            return target_tilt
        grown = speed_model.frame_units("tilt", actual.zoom) - speed_model.frame_units("tilt", reference_zoom)
        # Tilt units rising = view moving down.
        goal = reference_tilt + (0.5 - reference_top) * max(0.0, grown)
        if target_tilt > 0.0:
            return target_tilt  # the head is nearing the top edge: that comes first
        # This replaces the box-centre rule rather than adding to it: both
        # react to the same sinking, and together they overshoot.
        error = goal - actual.tilt
        if abs(error) <= float(config.tilt_deadband_units):
            return 0.0
        offset = speed_model.offset_for_rate(
            "tilt", abs(error) / HEADROOM_CATCH_SECONDS, actual.zoom, maximum=float(config.continuous_tilt_max_speed),
        )
        # Panasonic: tilt speed below 50 moves down (tilt units rising).
        return -math.copysign(offset, error)

    def _escape_through(
        self,
        escape: float,
        actual: PtzMotorPose,
        live_bounds: NormalizedRect | None,
    ) -> float:
        """Once breathing out for an escape, finish it: to a planned medium shot.

        Stopping the moment the subject is back inside leaves a shot only a
        little wider, which they outrun again a step later.
        """

        if escape > 0.0:
            # Not for a climb: there the head really does move in the frame.
            if not self._escape_episode and self._headroom_ref is None and not self._climbing and self._exit_top < 0.5:
                self._headroom_ref = (max(0.06, min(0.30, self._exit_top)), float(actual.zoom), float(actual.tilt))
            self._escape_episode = True
        if not self._escape_episode:
            return escape
        settled = (
            live_bounds is not None
            and self._walk.streak == 0
            and abs(live_bounds.center[0] - 0.50) < 0.15
            and live_bounds.y > ESCAPE_HEAD_TOP * 2
        )
        if actual.zoom <= ESCAPE_WIDE_ZOOM or settled:
            self._escape_episode = False
            return escape
        return max(escape, ESCAPE_THROUGH_SPEED)

    def _escape_widening(
        self,
        actual: PtzMotorPose,
        live_bounds: NormalizedRect | None,
        confirmed_wide_target: bool,
        desired: PtzMotorPose | None = None,
    ) -> float:
        """How urgently to breathe out when a subject outruns a telephoto shot.

        A good operator widens when someone dashes or steps up out of a tight
        shot, then pushes back in once they settle. A wider view both gives the
        subject more room and, with the camera's speed-with-zoom feature, lets
        pan and tilt move several times faster. The framing's own zoom-in
        brings the shot back afterwards.
        """

        if actual.zoom <= ESCAPE_ZOOM_FLOOR:
            return 0.0
        telephoto = _smoothstep((actual.zoom - 2900.0) / 300.0)
        if telephoto <= 0.0:
            return 0.0
        if live_bounds is None:
            # Out of the PTZ shot: widen only if they actually ran out of it
            # (last seen at an edge, or lost mid-walk), not for a detector
            # dropout mid-frame while they stand, bend or turn.
            escaped = (
                abs(self._exit_error[0]) > ESCAPE_EDGE_X - 0.05
                or self._exit_error[1] < -0.25
                # Stepped up out of the top: the head went, the box centre did not.
                or (self._exit_top <= ESCAPE_HEAD_TOP and self._rising_frames >= 2)
                # A dash blurs out of detection before it reaches the edge.
                or self._walk.streak >= 2
                # The wide camera puts them well outside this shot.
                or (desired is not None and abs(desired.pan - actual.pan) > ESCAPE_WIDE_PAN_UNITS)
            )
            return telephoto if confirmed_wide_target and escaped else 0.0
        x_error = live_bounds.center[0] - 0.50
        # Only an established walk (or climb) counts: a one-frame jump in the
        # detection is not somebody outrunning the shot.
        pulling_away = (
            self._walk.streak >= 2
            and self._subject_velocity_x * x_error > 0.0
            and abs(self._subject_velocity_x) > ESCAPE_IMAGE_SPEED
        )
        side = _smoothstep((abs(x_error) - ESCAPE_EDGE_X) / 0.10) if pulling_away else 0.0
        if self._subject_velocity_y < -ESCAPE_IMAGE_SPEED:
            if self._rising_since is None:
                self._rising_since = self._last_update or 0.0
            self._rising_frames = 1.0 + ((self._last_update or 0.0) - self._rising_since) / DECISION_PERIOD
        else:
            self._rising_since = None
            self._rising_frames = 0
        # Stepping up onto the stage happens while walking; standing up from
        # a bend or a gesture does not, and must not make the shot breathe.
        # Once widening for a climb, keep going until the head is back in:
        # the walk that carried them up the steps ends before the shot catches up.
        self._climbing = (
            self._rising_frames >= 2 and self._walk.streak >= 1
            or self._climbing and live_bounds.y <= ESCAPE_HEAD_TOP
        )
        climbing = self._climbing
        top = _smoothstep((ESCAPE_HEAD_TOP - live_bounds.y) / 0.05) if climbing else 0.0
        return max(side, top) * telephoto

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


def _framed(framed: bool, error: float, speed: float) -> bool:
    """Hysteresis: settle near centre, then hold until a clear drift off it."""

    if framed:
        return abs(error) <= FRAMED_DRIFT
    return abs(error) <= FRAMED_SETTLE and speed < 1.0


def _per_period(alpha: float, elapsed: float) -> float:
    """An exponential-smoothing factor set per decision period, for `elapsed` seconds."""

    alpha = max(0.0, min(1.0, alpha))
    return 1.0 - (1.0 - alpha) ** (elapsed / DECISION_PERIOD)


def _smoothstep(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3.0 - 2.0 * value)


def _safe_zone_target(error: float, deadband: float, maximum: float, *, edge_error: float = .44) -> float:
    # Continuous zero-speed boundary; full correction before the centre of
    # the subject reaches an image edge. The maximum remains zoom-aware.
    fraction = (abs(error)-deadband) / max(.05, edge_error-deadband)
    gain = _smoothstep(fraction)
    return math.copysign(min(49.0, maximum)*(.15*gain + .85*gain*gain), error)


# Keeping pace with a walker (single subject, Dev experiments only).
#
# At sermon zoom the camera's speed-with-zoom feature makes the configured cap
# (26) only ~390 pan units/s, while a person walking at 1 m/s needs ~620 and a
# brisk 1.3 m/s ~800 (measured on the ICAS AW-HE120, see speed_model.py). And
# a controller that waits for the subject to near the frame edge starts far
# too late. So once the locked person is actually walking, command the pan
# speed that matches their measured pace, plus the usual position correction;
# when they stop, the pace term fades with their speed estimate.
DECISION_PERIOD = 0.25  # the 4 Hz decision rate the tuning below was made at
PURSUIT_PAN_MAX = 44.0
PURSUIT_TILT_MAX = 36.0
WIDE_FOLLOW_SECONDS = 0.7
HEADROOM_EDGE = 0.06  # head top this close to the top of the frame tilts up regardless of the box centre
HEADROOM_TILT_SPEED = 24.0
HEADROOM_LEAD_SECONDS = 0.3
HEADROOM_CATCH_SECONDS = 0.6
HEADROOM_ZOOM_MARGIN = 100  # back within this of the earlier zoom: normal framing resumes
FRAMED_SETTLE = 0.05  # a standing subject this near centre counts as framed
FRAMED_DRIFT = 0.14  # ...and the camera holds until they drift this far off it
FRAMED_WALK_RELEASE = 0.20  # or until they walk faster than this (frames/s); a sway is slower
MAX_PICTURE_SPEED = 0.75  # frames/s, single-subject pan
RECENTRE_FRAMES_PER_SECOND = 0.3  # fastest the picture moves to re-centre someone standing still
ZOOM_IN_SETTLE_SECONDS = 2.5  # after moving, stand still this long before the shot pushes back in
OFF_SHOT_TILT_FRAMES = 0.35  # off-shot tilt differences under this share of the frame height are ignored
LOSS_COAST_SECONDS = 0.6  # a detection gap this short eases the camera down instead of stopping it
LOSS_COAST_TAU = 0.25
ESCAPE_ZOOM_SPEED = 44.0  # zoom-out speed when a subject outruns a tight shot
# Breathe out no wider than this: still a tight single shot on this stage
# (~30% wider than zoom 3500, and pan ~1.6x faster there), and inside the
# range where pace matching stays fully active.
ESCAPE_ZOOM_FLOOR = 3200
FRONT_OF_STAGE_ZOOM = 3000  # no tighter than this while the subject is below the deck
ESCAPE_WIDE_ZOOM = 2900  # an escape breathes out to about this medium shot (~1.7x wider than 3500)
ESCAPE_THROUGH_SPEED = 0.6  # share of the escape zoom speed used to finish the breath out
ESCAPE_WIDE_PAN_UNITS = 600  # about half the frame width at sermon zoom
ESCAPE_EDGE_X = 0.30  # subject centre beyond 80% of the frame toward the edge
ESCAPE_HEAD_TOP = 0.04  # head within 4% of the top of the frame
ESCAPE_IMAGE_SPEED = 0.05  # image widths/s: still moving out, not just standing there
WALKER_CATCH_UP = 0.5  # share of the position correction added on top of the pace
WALKER_FEEDFORWARD_START = 120.0  # pan units/s: below this it is a sway, not a walk
WALKER_FEEDFORWARD_FULL = 320.0
WALKER_SUSTAINED_PACE = 0.8  # a pace falling below 80% of the last is not a walk
WALKER_OVERTAKE_START = 0.04  # image widths/s the subject may drift back before the camera lets go
WALKER_OVERTAKE_SPAN = 0.10
WALKER_REVERSAL_SECONDS = 2.5
WALKER_EXIT_EDGE = 0.22  # last seen this far off centre toward the walk = left by that edge
WALKER_IMAGE_AGREEMENT = 0.10  # image widths/s the subject must also move in the image


class _WalkEvidence:
    """Walking is sustained motion; a single jump in position is not.

    A detector re-centring or a person shifting their weight produces one
    large velocity sample that then decays. A walker keeps the same direction
    at an undiminished pace. The pace term engages over the second and third
    decision periods (0.25 s each) of that, and drops the moment the pace
    falls away. Measured in time, so a faster decision rate reacts no sooner.
    """

    def __init__(self) -> None:
        self.started_at: float | None = None
        self.last_at = 0.0
        self.sign = 0
        self.walking_sign = 0
        self.pace = 0.0
        self.reversed_at = -math.inf

    @property
    def streak(self) -> float:
        """How long the walk has lasted, in 0.25 s decision periods (1 on the first sighting)."""

        if self.started_at is None:
            return 0.0
        return 1.0 + (self.last_at - self.started_at) / DECISION_PERIOD

    def observe(self, pan_units_per_second: float, now: float, *, image_agrees: bool = False) -> float:
        pace = abs(pan_units_per_second)
        sign = (pan_units_per_second > 0) - (pan_units_per_second < 0)
        if pace < WALKER_FEEDFORWARD_START:
            self.started_at = None
        elif not (sign == self.sign and pace >= self.pace * WALKER_SUSTAINED_PACE and self.started_at is not None):
            self.started_at = now
        self.last_at = now
        if self.streak >= 1 and sign != self.walking_sign:
            if self.walking_sign != 0:
                self.reversed_at = now
            self.walking_sign = sign
        self.sign, self.pace = sign, pace
        # Delayed feedback can make a standing subject appear to walk back
        # and forth as the camera moves. A walker rarely reverses twice in a
        # few seconds, so after a reversal ask for more evidence.
        if now - self.reversed_at < WALKER_REVERSAL_SECONDS:
            return min(1.0, max(0, self.streak - 3) / 3.0)
        # Two independent signals (world-space speed from the lock, image
        # motion here) agreeing is strong evidence: engage faster.
        steps = 1.5 if image_agrees else 3.0
        return min(1.0, self.streak / steps)


def _image_agrees(axis: str, motor_units_per_second: float, image_velocity: float) -> bool:
    return image_velocity * _walk_image_direction(axis, motor_units_per_second) > WALKER_IMAGE_AGREEMENT


def _walk_image_direction(axis: str, motor_units_per_second: float) -> float:
    """Which way the walker moves in the image for this motor direction.

    A rising pan coordinate moves the view left (the walker is heading to -x);
    a rising tilt coordinate moves the view down (the walker is heading to +y).
    """

    rising = motor_units_per_second > 0
    if axis == "pan":
        return -1.0 if rising else 1.0
    return 1.0 if rising else -1.0


def _with_walker_feedforward(
    axis: str,
    correction: float,
    motor_units_per_second: float,
    zoom: float,
    sustained: float = 1.0,
    *,
    image_velocity: float = 0.0,
) -> float:
    pace = abs(motor_units_per_second)
    weight = sustained * _smoothstep(
        (pace - WALKER_FEEDFORWARD_START) / (WALKER_FEEDFORWARD_FULL - WALKER_FEEDFORWARD_START)
    )
    overtaking = -image_velocity * _walk_image_direction(axis, motor_units_per_second)
    # The walker's speed estimate needs ~0.5 s to decay after they stop; the
    # image shows it at once, as the subject slides back toward the trailing
    # side. Let go of the pace term as soon as the camera is overtaking.
    weight *= 1.0 - _smoothstep((overtaking - WALKER_OVERTAKE_START) / WALKER_OVERTAKE_SPAN)
    if weight <= 0.0:
        return correction
    maximum = PURSUIT_PAN_MAX if axis == "pan" else PURSUIT_TILT_MAX
    offset = speed_model.offset_for_rate(axis, pace, zoom, maximum=maximum) * weight
    # Panasonic: an AW speed above 50 decreases the pan and tilt coordinates.
    feedforward = -math.copysign(offset, motor_units_per_second)
    if feedforward * correction >= 0.0:
        # Same direction: the walker is behind centre. Matching their pace
        # alone holds that gap for the whole walk, so add part of the
        # position correction to close it; the overtaking release above stops
        # it running past them when they stop.
        combined = feedforward + WALKER_CATCH_UP * correction
    else:
        # The camera is ahead of them: let the correction brake it.
        combined = feedforward + correction
    return max(-maximum, min(maximum, combined))


def _brake(current: float, target: float, elapsed: float) -> float:
    return target + (current - target) * math.exp(-elapsed / 0.08)


def _safe_zone_ease(current: float, target: float, elapsed: float) -> float:
    if current*target < 0:
        # Brake before reversing rather than continuing a prediction-driven
        # swing across the frame. Snap only sub-command residuals to neutral.
        if abs(current) >= .5:
            return current*math.exp(-elapsed/.15)
        current = 0.0
    return current+(target-current)*(1.0-math.exp(-elapsed/.22))


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
