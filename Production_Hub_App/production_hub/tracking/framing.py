from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable, Sequence

from production_hub.calibration.relocalization import RelocalizationSnapshot
from production_hub.core.config.models import (
    CameraSceneRegion,
    PtzAutomationConfig,
)
from production_hub.tracking.models import NormalizedRect, TrackedSubject, TrackingSnapshot
from production_hub.tracking.ptz_geometry import (
    PtzGeometryModel,
    PtzMotorPose,
    transform_live_rect_to_reference,
)


class FramingState(StrEnum):
    OFF = "off"
    WAITING = "waiting"
    READY = "ready"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class FramingDecision:
    state: FramingState
    mode: str
    reason: str
    target_bounds: NormalizedRect | None = None
    target_ids: tuple[str, ...] = ()
    target_confidence: float = 0.0
    desired_pose: PtzMotorPose | None = None
    podium_framing: bool = False


class FramingEngine:
    """Pure target selection and framing policy; never talks to camera hardware."""

    def decide(
        self,
        config: PtzAutomationConfig,
        audience: TrackingSnapshot,
        ptz: TrackingSnapshot,
        relocalization: RelocalizationSnapshot,
        regions: Sequence[CameraSceneRegion],
        geometry: PtzGeometryModel,
        *,
        current_pose: PtzMotorPose | None = None,
        click_target: tuple[float, float] | None = None,
    ) -> FramingDecision:
        mode = config.mode
        if mode == "off":
            return FramingDecision(FramingState.OFF, mode, "Choose a framing mode.")
        if not relocalization.motion_safe:
            return FramingDecision(
                FramingState.BLOCKED,
                mode,
                "Live Audience calibration is not locked to an approved map.",
            )

        usable_regions = tuple(
            region
            for region in regions
            if region.enabled and region.source == "audience"
        )
        if mode in {"stage", "stage_altar"} and not usable_regions:
            return FramingDecision(
                FramingState.BLOCKED,
                mode,
                "No enabled stage/altar/podium regions are available.",
            )

        subjects = self._reference_subjects(
            audience,
            relocalization,
            config.minimum_subject_age_frames,
        )
        selected: list[tuple[str, NormalizedRect, float]] = []
        ptz_selected: list[tuple[str, NormalizedRect, float]] = []
        if mode == "subject":
            # Subject Tracking follows whoever is already visible in the PTZ
            # frame. This makes the operator workflow a simple move-then-toggle
            # action and avoids a second subject-selection UI.
            if current_pose is not None:
                ptz_selected = self._stable_ptz_subjects(
                    ptz,
                    config.minimum_subject_age_frames,
                )
                selected = self._project_ptz_subjects(
                    ptz_selected,
                    geometry,
                    current_pose,
                )
            # The fixed Audience view is backup/reacquisition only. Restrict it
            # to operational stage areas so congregants in the pews cannot pull
            # the PTZ away from the speaker.
            if not selected:
                allowed = {"stage", "front_stage", "altar", "podium"}
                selected = [
                    item
                    for item in subjects
                    if any(
                        region.kind in allowed
                        and _region_contains(region, *_subject_floor(item[1]))
                        for region in usable_regions
                    )
                ]
            if not selected:
                return FramingDecision(
                    FramingState.WAITING,
                    mode,
                    "No stable subject is visible in the PTZ frame or stage areas.",
                )
        elif mode in {"stage", "stage_altar"}:
            allowed = {"stage", "front_stage", "podium"}
            if mode == "stage_altar":
                allowed.add("altar")
            selected = [
                item
                for item in subjects
                if any(
                    region.kind in allowed
                    and _region_contains(region, *_subject_floor(item[1]))
                    for region in usable_regions
                )
            ]
            # The fixed Audience view is intentionally very wide, so a lone
            # speaker can be below Vision's useful detection size even while
            # they are clear in the PTZ image.  Once a subject is visible in
            # the PTZ feed, project that observation through the calibrated
            # motor pose and keep following it.  Audience detections retain
            # priority because they can see people outside the current PTZ
            # frame and can therefore drive group framing/reacquisition.
            if not selected and current_pose is not None:
                selected = [
                    item
                    for item in self._ptz_reference_subjects(
                        ptz,
                        geometry,
                        current_pose,
                        config.minimum_subject_age_frames,
                    )
                    if any(
                        region.kind in allowed
                        and _region_contains(region, *_subject_floor(item[1]))
                        for region in usable_regions
                    )
                ]
            if not selected:
                return FramingDecision(
                    FramingState.WAITING,
                    mode,
                    "No stable subjects are visible in the selected stage areas.",
                )
        elif mode == "click":
            if click_target is None:
                return FramingDecision(
                    FramingState.WAITING,
                    mode,
                    "Click a location in the Audience preview.",
                )
            width = config.click_target_width
            height = config.click_target_height
            target = NormalizedRect(
                click_target[0] - width / 2.0,
                click_target[1] - height / 2.0,
                width,
                height,
            ).clamped()
            return self._pose_decision(
                config,
                geometry,
                target,
                ("operator-click",),
                1.0,
                podium=False,
            )

        podium = bool(
            config.podium_zoom_enabled
            and len(selected) == 1
            and any(
                region.kind == "podium"
                and _region_contains(region, *_subject_floor(selected[0][1]))
                for region in usable_regions
            )
        )
        if (
            mode == "subject"
            and config.adaptive_subject_framing_enabled
            and ptz_selected
            and current_pose is not None
        ):
            target, desired_height = _adaptive_ptz_composition(
                config,
                [item[1] for item in ptz_selected],
                podium=podium,
            )
            try:
                reference_target = geometry.ptz_rect_to_reference(
                    target,
                    current_pose,
                )
            except ValueError as exc:
                return FramingDecision(
                    FramingState.BLOCKED,
                    mode,
                    str(exc),
                    target_ids=tuple(item[0] for item in ptz_selected),
                    target_confidence=min(item[2] for item in ptz_selected),
                    podium_framing=podium,
                )
            return self._pose_decision(
                config,
                geometry,
                reference_target,
                tuple(item[0] for item in ptz_selected),
                min(item[2] for item in ptz_selected),
                podium=podium,
                desired_frame_height=desired_height,
            )
        prepared = [
            bounds if podium else _expand_upper_body_to_stage_frame(bounds)
            for _identifier, bounds, _confidence in selected
        ]
        target = _union(prepared)
        target = _pad(target, config.target_padding_x, config.target_padding_y)
        desired_height = (
            config.podium_subject_frame_height
            if podium
            else config.single_subject_frame_height
            if len(selected) == 1
            else config.group_frame_height
        )
        return self._pose_decision(
            config,
            geometry,
            target,
            tuple(item[0].replace(":selected", "") for item in selected),
            min(item[2] for item in selected),
            podium=podium,
            desired_frame_height=desired_height,
        )

    @staticmethod
    def _reference_subjects(
        snapshot: TrackingSnapshot,
        relocalization: RelocalizationSnapshot,
        minimum_age_frames: int,
    ) -> list[tuple[str, NormalizedRect, float]]:
        selected: list[tuple[str, NormalizedRect, float]] = []
        for subject in snapshot.subjects:
            try:
                bounds = transform_live_rect_to_reference(
                    subject.bounds,
                    relocalization.live_to_reference,
                    relocalization.live_size,
                    relocalization.reference_size,
                )
            except ValueError:
                continue
            if subject.age_frames < minimum_age_frames:
                continue
            suffix = ":selected" if subject.selected else ""
            selected.append(
                (f"audience:{subject.track_id}{suffix}", bounds, subject.confidence)
            )
        return selected

    @staticmethod
    def _ptz_reference_subjects(
        snapshot: TrackingSnapshot,
        geometry: PtzGeometryModel,
        current_pose: PtzMotorPose,
        minimum_age_frames: int,
    ) -> list[tuple[str, NormalizedRect, float]]:
        return FramingEngine._project_ptz_subjects(
            FramingEngine._stable_ptz_subjects(snapshot, minimum_age_frames),
            geometry,
            current_pose,
        )

    @staticmethod
    def _stable_ptz_subjects(
        snapshot: TrackingSnapshot,
        minimum_age_frames: int,
    ) -> list[tuple[str, NormalizedRect, float]]:
        return [
            (f"ptz:{subject.track_id}", subject.bounds, subject.confidence)
            for subject in snapshot.subjects
            if subject.age_frames >= minimum_age_frames
        ]

    @staticmethod
    def _project_ptz_subjects(
        subjects: Sequence[tuple[str, NormalizedRect, float]],
        geometry: PtzGeometryModel,
        current_pose: PtzMotorPose,
    ) -> list[tuple[str, NormalizedRect, float]]:
        selected: list[tuple[str, NormalizedRect, float]] = []
        for identifier, bounds, confidence in subjects:
            try:
                projected = geometry.ptz_rect_to_reference(bounds, current_pose)
            except ValueError:
                continue
            selected.append((identifier, projected, confidence))
        return selected

    @staticmethod
    def _pose_decision(
        config: PtzAutomationConfig,
        geometry: PtzGeometryModel,
        target: NormalizedRect,
        target_ids: tuple[str, ...],
        confidence: float,
        *,
        podium: bool,
        desired_frame_height: float | None = None,
    ) -> FramingDecision:
        try:
            pose = geometry.pose_for_target(
                target,
                desired_frame_height=(
                    desired_frame_height
                    if desired_frame_height is not None
                    else config.single_subject_frame_height
                ),
            )
        except ValueError as exc:
            return FramingDecision(
                FramingState.BLOCKED,
                config.mode,
                str(exc),
                target_bounds=target,
                target_ids=target_ids,
                target_confidence=confidence,
                podium_framing=podium,
            )
        return FramingDecision(
            FramingState.READY,
            config.mode,
            "Framing target is ready.",
            target_bounds=target,
            target_ids=target_ids,
            target_confidence=confidence,
            desired_pose=pose,
            podium_framing=podium,
        )


def _subject_floor(bounds: NormalizedRect) -> tuple[float, float]:
    return bounds.x + bounds.width / 2.0, min(1.0, bounds.y + bounds.height)


def _expand_upper_body_to_stage_frame(bounds: NormalizedRect) -> NormalizedRect:
    """Give an off-stand speaker enough room for a near full-body composition."""

    # Body-pose enrichment already reaches the subject's legs in a clear wide
    # shot. Extending that envelope again was the source of severe over-wide
    # framing, so expansion is reserved for genuine upper-body detections.
    if bounds.height >= 0.68:
        return bounds

    center_x, _center_y = bounds.center
    width = min(1.0, bounds.width * 1.35)
    top = max(0.0, bounds.y - bounds.height * 0.12)
    bottom = min(1.0, bounds.y + bounds.height * 1.85)
    return NormalizedRect(center_x - width / 2.0, top, width, bottom - top).clamped()


def _adaptive_ptz_composition(
    config: PtzAutomationConfig,
    subjects: Sequence[NormalizedRect],
    *,
    podium: bool,
) -> tuple[NormalizedRect, float]:
    """Compose PTZ observations while preserving an already-good manual zoom.

    The operator establishes the shot before enabling tracking. Within a
    reference-derived occupancy band, the zoom is held. Crossing an edge or
    becoming materially too small is what changes it. Image-space deadbands
    similarly prevent detector noise from producing constant pan/tilt nudges.
    """

    if not subjects:
        raise ValueError("At least one PTZ subject is required for framing.")
    prepared = list(subjects)
    if len(prepared) == 1 and not podium:
        prepared[0] = _expand_upper_body_to_stage_frame(prepared[0])
    content = _union(prepared)
    raised_gesture_at_top = content.y <= 0.015
    target = _pad(
        content,
        config.subject_safety_padding_x,
        config.subject_safety_padding_y,
    )
    if raised_gesture_at_top:
        # Bias the aim upward while leaving the lower lectern/body near the
        # bottom. The smaller occupancy cap creates the requested zoom-out.
        target = NormalizedRect(target.x, 0.0, target.width, 0.94).clamped()
    target = _apply_center_deadband(
        target,
        max(config.subject_center_deadband_x, 0.09)
        if raised_gesture_at_top
        else config.subject_center_deadband_x,
        config.subject_center_deadband_y,
        preserve_vertical=raised_gesture_at_top,
    )
    effective_occupancy = max(
        target.height,
        target.width / (16.0 / 9.0),
    )
    if len(subjects) > 1:
        minimum = config.group_minimum_occupancy
        maximum = config.group_maximum_occupancy
    else:
        minimum = config.subject_minimum_occupancy
        maximum = config.subject_maximum_occupancy
    if raised_gesture_at_top:
        maximum = min(maximum, config.raised_gesture_maximum_occupancy)
    desired_height = max(minimum, min(maximum, effective_occupancy))
    return target, desired_height


def _apply_center_deadband(
    bounds: NormalizedRect,
    horizontal: float,
    vertical: float,
    *,
    preserve_vertical: bool,
) -> NormalizedRect:
    center_x, center_y = bounds.center
    if abs(center_x - 0.5) <= horizontal:
        center_x = 0.5
    if not preserve_vertical and abs(center_y - 0.5) <= vertical:
        center_y = 0.5
    width = min(1.0, bounds.width)
    height = min(1.0, bounds.height)
    left = max(0.0, min(1.0 - width, center_x - width / 2.0))
    top = max(0.0, min(1.0 - height, center_y - height / 2.0))
    return NormalizedRect(left, top, width, height)


def _union(rectangles: Iterable[NormalizedRect]) -> NormalizedRect:
    values = tuple(rectangles)
    if not values:
        return NormalizedRect(0, 0, 0, 0)
    left = min(item.x for item in values)
    top = min(item.y for item in values)
    right = max(item.x + item.width for item in values)
    bottom = max(item.y + item.height for item in values)
    return NormalizedRect(left, top, right - left, bottom - top).clamped()


def _pad(bounds: NormalizedRect, padding_x: float, padding_y: float) -> NormalizedRect:
    return NormalizedRect(
        bounds.x - padding_x / 2.0,
        bounds.y - padding_y / 2.0,
        bounds.width + padding_x,
        bounds.height + padding_y,
    ).clamped()


def _region_contains(region: CameraSceneRegion, x: float, y: float) -> bool:
    points = region.points
    inside = False
    previous = points[-1]
    for current in points:
        if (
            (current.y > y) != (previous.y > y)
            and x
            < (previous.x - current.x)
            * (y - current.y)
            / (previous.y - current.y)
            + current.x
        ):
            inside = not inside
        previous = current
    return inside
