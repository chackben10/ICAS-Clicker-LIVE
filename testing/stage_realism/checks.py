"""Detector checks on our real stage imagery, outside the closed loop."""

from __future__ import annotations

from dataclasses import dataclass, field

SERMON_ZOOM = 3544
PANS = range(41600, 46800, 400)
TILTS = (33100, 33317, 33600)


@dataclass(slots=True)
class CheckResult:
    name: str
    passed: bool
    detail: str
    hits: list[tuple[int, int, float]] = field(default_factory=list)


def empty_stage_sweep() -> CheckResult:
    """Pan the PTZ across the empty stage at sermon zoom; nothing is a person.

    The stage is the calibration sweep's own photos (piano, drum kit, keyboard,
    guitars, lectern, monitors). Any detection is stage hardware the tracker
    could lock onto when the real subject leaves the frame.
    """

    from production_hub.tracking import service as service_module

    from . import render
    from .world import StageWorld

    world = StageWorld([], [], initial_zoom=SERMON_ZOOM, duration=0.0)
    geometry, config, _ = world._build()
    detector = service_module._default_detector_factory(config)
    from .target import _active

    if _active is not None and _active.name == "dev" and hasattr(detector, "conservative_pose_merging"):
        detector.conservative_pose_merging = True  # the Dev app's validated boxes
    hits = []
    total = 0
    for pan in PANS:
        for tilt in TILTS:
            total += 1
            frame = render.render_ptz(world._view(geometry, (pan, tilt, SERMON_ZOOM)), [], 0.0)
            for candidate in detector.detect(render.to_qimage(frame)):
                hits.append((pan, tilt, round(candidate.confidence, 2)))
    views = len({(pan, tilt) for pan, tilt, _ in hits})
    return CheckResult(
        "empty_stage_sweep",
        not hits,
        f"a 'person' was detected on the empty stage in {views} of {total} sermon-zoom views"
        if hits
        else f"no person detected in any of {total} empty-stage sermon-zoom views",
        hits,
    )
