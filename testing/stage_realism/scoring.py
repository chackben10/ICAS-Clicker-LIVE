"""Judge a run the way the operator judges the program feed.

Every rule is measured against where the people really are and what the PTZ
really shows (true optics), never against what the tracker believes.

Rules and thresholds (chosen by the operator on 2026-09-22):

head_in_frame   No part of a tracked head may leave the PTZ frame. A 0.1 s
                tolerance absorbs sampling only.
keeps_up        A moving subject may not sit in the outer 20% of the frame
                (or outside it) for more than 1 s at a time.
settles         After the subject stops, the camera may not hunt (two or more
                visible pan reversals), overshoot past them by more than 12% of
                the frame, or still be visibly panning 3 s later. While a
                subject stands in place, the pan may not reverse more than 8
                times a minute. "Visible" means at least 2 speed steps from
                neutral (3 for "still panning"): one step on the AW-HE120 is
                ~0.07 deg/s, which does not show on the stream.
right_person    The camera never frames someone else instead of the locked
                subject, and the lock never moves onto another person.
stays_armed     Tracking never switches itself off or raises cycle errors.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .scenarios import Scenario
from .world import RunResult

GRACE_SECONDS = 1.0
HEAD_TOLERANCE_SECONDS = 0.1
LAG_EDGE = 0.20
LAG_LIMIT_SECONDS = 1.0
SETTLE_WINDOW = 3.0
SETTLE_MIN_STILL = 2.5
OVERSHOOT_LIMIT = 0.12
REVERSALS_PER_MINUTE_LIMIT = 8.0
WRONG_PERSON_LIMIT_SECONDS = 0.5
VISIBLE_PAN_STEPS = 3

RULES = ("head_in_frame", "keeps_up", "settles", "right_person", "stays_armed")


@dataclass(slots=True)
class RuleResult:
    rule: str
    passed: bool
    detail: str
    value: float = 0.0


@dataclass(slots=True)
class Score:
    scenario: Scenario
    variant: str
    run: RunResult
    rules: list[RuleResult] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    first_failure_t: float | None = None

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.rules)

    def rule(self, name: str) -> RuleResult:
        return next(item for item in self.rules if item.rule == name)


def _inside(rect, view, margin: float = 0.0) -> bool:
    x, y, w, h = rect
    vx, vy, vw, vh = view
    return (
        x >= vx + margin * vw
        and x + w <= vx + (1 - margin) * vw
        and y >= vy
        and y + h <= vy + vh
    )


def _normalized_centre_x(rect, view) -> float:
    x, _y, w, _h = rect
    return (x + w / 2.0 - view[0]) / view[2]


def _stops(run: RunResult, targets) -> list[float]:
    """Times at which every target has just stopped and stays still >= 2.5 s."""

    points = []
    for person in targets:
        segments = person.segments
        for index, segment in enumerate(segments):
            moved = segment.x0 != segment.x1 or segment.y0 != segment.y1
            if not moved:
                continue
            still = 0.0
            for following in segments[index + 1 :]:
                if following.x0 != following.x1 or following.y0 != following.y1:
                    break
                still += following.end - following.start
            if still >= SETTLE_MIN_STILL or index == len(segments) - 1:
                points.append(segment.end)
    return sorted(points)


def score(scenario: Scenario, variant: str, run: RunResult) -> Score:
    result = Score(scenario, variant, run)
    targets = [person for person in run.people if person.name in scenario.targets]
    others = [person for person in run.people if person.name not in scenario.targets]
    samples = [sample for sample in run.samples if sample.t >= GRACE_SECONDS]
    step = 0.05
    failures: list[float] = []

    if not run.armed:
        for rule in RULES:
            result.rules.append(RuleResult(rule, rule != "stays_armed", "not armed"))
        result.rule("stays_armed").detail = f"arming refused: {run.arm_message}"
        result.first_failure_t = 0.0
        return result

    # head_in_frame ------------------------------------------------------------
    head_out = 0.0
    first_head_out = None
    for sample in samples:
        if any(
            person.present(sample.t) and not _inside(person.head_rect(sample.t), sample.view)
            for person in targets
        ):
            head_out += step
            first_head_out = sample.t if first_head_out is None else first_head_out
    passed = head_out <= HEAD_TOLERANCE_SECONDS
    if not passed:
        failures.append(first_head_out)
    result.rules.append(
        RuleResult(
            "head_in_frame",
            passed,
            f"a tracked head was cut off for {head_out:.1f} s"
            + (f", first at {first_head_out:.1f} s" if first_head_out is not None else ""),
            head_out,
        )
    )

    # keeps_up -------------------------------------------------------------------
    longest = current = 0.0
    longest_at = None
    for sample in samples:
        lagging = False
        for person in targets:
            if not person.present(sample.t):
                continue
            rect = person.body_rect(sample.t)
            if len(targets) == 1:
                centre = _normalized_centre_x(rect, sample.view)
                lagging |= centre < LAG_EDGE or centre > 1 - LAG_EDGE
            else:
                lagging |= not _inside(person.head_rect(sample.t), sample.view, margin=0.05)
        current = current + step if lagging else 0.0
        if current > longest:
            longest = current
            longest_at = sample.t - current
    passed = longest <= LAG_LIMIT_SECONDS
    if not passed:
        failures.append(longest_at + LAG_LIMIT_SECONDS)
    result.rules.append(
        RuleResult(
            "keeps_up",
            passed,
            f"longest stretch at the frame edge or outside it: {longest:.1f} s"
            + (f" from {longest_at:.1f} s" if longest_at is not None and longest > 0 else ""),
            longest,
        )
    )

    # settles ----------------------------------------------------------------------
    problems = []
    for stop in _stops(run, targets):
        window = [s for s in samples if stop + 0.3 <= s.t <= stop + SETTLE_WINDOW]
        if len(window) < 10:
            continue
        signs = [(s.speeds[0] >= 52) - (s.speeds[0] <= 48) for s in window]
        moving = [sign for sign in signs if sign]
        reversals = sum(1 for a, b in zip(moving, moving[1:]) if a != b)
        centre_x = [
            sum(_normalized_centre_x(p.body_rect(s.t), s.view) for p in targets) / len(targets)
            for s in window
        ]
        approach = 1 if centre_x[0] > 0.5 else -1
        overshoot = max(0.0, max((0.5 - c) * approach for c in centre_x))
        still_moving = abs(window[-1].speeds[0] - 50) >= VISIBLE_PAN_STEPS and abs(centre_x[-1] - 0.5) < 0.15
        if reversals >= 2:
            problems.append((stop, f"hunted ({reversals} pan reversals) after the stop at {stop:.1f} s"))
        if overshoot > OVERSHOOT_LIMIT:
            problems.append((stop, f"overshot by {overshoot:.0%} of the frame after {stop:.1f} s"))
        if still_moving:
            problems.append((stop, f"still panning 3 s after the stop at {stop:.1f} s"))
    for start, end in scenario.stationary_windows:
        window = [s for s in samples if start <= s.t <= end]
        if not window:
            continue
        moving = [(s.speeds[0] >= 52) - (s.speeds[0] <= 48) for s in window]
        moving = [sign for sign in moving if sign]
        reversals = sum(1 for a, b in zip(moving, moving[1:]) if a != b)
        minutes = max(1e-6, (window[-1].t - window[0].t) / 60.0)
        rate = reversals / minutes
        result.metrics["stationary_reversals_per_min"] = round(rate, 1)
        if rate > REVERSALS_PER_MINUTE_LIMIT:
            problems.append((window[0].t, f"restless: {rate:.0f} pan reversals/min while the subject stood in place"))
    if problems:
        failures.append(min(item[0] for item in problems))
    result.rules.append(
        RuleResult(
            "settles",
            not problems,
            "; ".join(item[1] for item in problems) or "settled cleanly after every stop",
            len(problems),
        )
    )

    # right_person -----------------------------------------------------------------
    wrong_frame = 0.0
    wrong_lock = 0.0
    first_wrong = None
    for sample in samples:
        target_heads = [p for p in targets if p.present(sample.t) and _inside(p.head_rect(sample.t), sample.view)]
        other_heads = [p for p in others if p.present(sample.t) and _inside(p.head_rect(sample.t), sample.view)]
        if other_heads and not target_heads:
            wrong_frame += step
            first_wrong = sample.t if first_wrong is None else first_wrong
        if sample.target_bounds is not None and others:
            bx, by, bw, bh = sample.target_bounds
            centre = (bx + bw / 2.0, by + bh / 2.0)

            def distance(person) -> float:
                x, y, w, h = person.body_rect(sample.t)
                return abs(x + w / 2.0 - centre[0]) + abs(y + h / 2.0 - centre[1]) * 0.5

            present_targets = [p for p in targets if p.present(sample.t)]
            present_others = [p for p in others if p.present(sample.t)]
            if present_targets and present_others:
                if min(map(distance, present_others)) + 0.01 < min(map(distance, present_targets)):
                    wrong_lock += step
                    first_wrong = sample.t if first_wrong is None else first_wrong
    passed = wrong_frame <= WRONG_PERSON_LIMIT_SECONDS and wrong_lock <= WRONG_PERSON_LIMIT_SECONDS
    if not passed:
        failures.append(first_wrong)
    detail = (
        "never framed or locked onto anyone else"
        if not others
        else f"framed someone else for {wrong_frame:.1f} s; lock nearer someone else for {wrong_lock:.1f} s"
    )
    result.rules.append(RuleResult("right_person", passed, detail, max(wrong_frame, wrong_lock)))

    # stays_armed -------------------------------------------------------------------
    problems = []
    if run.disarm_reason:
        problems.append(f"disarmed itself: {run.disarm_reason}")
    if run.errors:
        problems.append(f"{len(run.errors)} cycle error(s), first: {run.errors[0]}")
    if problems:
        last = run.samples[-1].t if run.samples else 0.0
        failures.append(last)
    result.rules.append(
        RuleResult("stays_armed", not problems, "; ".join(problems) or "armed throughout", len(problems))
    )

    # metrics --------------------------------------------------------------------------
    total = max(1, len(samples))
    in_frame = sum(
        1
        for sample in samples
        if all(
            (not p.present(sample.t)) or _inside(p.head_rect(sample.t), sample.view) for p in targets
        )
    )
    result.metrics["heads_in_frame_pct"] = round(100.0 * in_frame / total, 1)
    result.metrics["max_edge_stretch_s"] = round(longest, 2)
    result.metrics["peak_pan_speed"] = max((abs(s.speeds[0] - 50) for s in samples), default=0)
    sources = {}
    for sample in samples:
        sources[sample.source or "-"] = sources.get(sample.source or "-", 0) + 1
    result.metrics["time_by_source_s"] = {k: round(v * step, 1) for k, v in sorted(sources.items())}
    result.metrics["watchability"] = watchability(run, targets)
    result.first_failure_t = min((t for t in failures if t is not None), default=None)
    return result


def watchability(run: RunResult, targets) -> dict[str, float]:
    """What a viewer notices beyond "is the person in shot" (informational).

    jolts_per_min         speed-command changes of 10+ steps within 0.25 s. A coarse proxy:
                          AW speed steps are very uneven (at sermon zoom 30->44 moves
                          the picture ~7x more than 0->14), so see lurches_per_min
    lurches_per_min       the picture itself speeding up or slowing down by 0.2+ frame
                          widths/s within 0.25 s (from the camera's actual view, so it
                          includes the motor's response)
    reversals_per_min     visible pan direction reversals (hunting)
    idle_motion_pct       time the camera moves while everyone tracked stands still
    idle_pan_tilt_pct     the same, pan/tilt only (a slow deliberate push-in is not creep)
    zoom_changes_per_min  zoom direction changes (breathing)
    headroom_mean/sd      top of the head in the frame (0 = top edge) while standing
    walk_headroom_sd      the same spread while they walk (tilt bouncing along a walk)
    offcentre_mean        |x - 0.5| of the tracked people while standing
    """

    samples = [s for s in run.samples if s.t >= GRACE_SECONDS]
    if len(samples) < 10:
        return {}
    minutes = max(1e-6, (samples[-1].t - samples[0].t) / 60.0)
    lag = max(1, round(0.25 / 0.05))
    jolts = 0
    for earlier, later in zip(samples, samples[lag:]):
        if max(abs(later.speeds[0] - earlier.speeds[0]), abs(later.speeds[1] - earlier.speeds[1])) >= 10:
            jolts += 1
    # Count each lurch once, not once per overlapping window.
    jolts = jolts / lag
    # Picture velocity in frame widths/s, centred difference over 0.1 s.
    picture = []
    for before, here, after in zip(samples, samples[1:], samples[2:]):
        dt = max(1e-6, after.t - before.t)
        width = max(1e-6, here.view[2])
        vx = ((after.view[0] + after.view[2] / 2) - (before.view[0] + before.view[2] / 2)) / dt / width
        vy = ((after.view[1] + after.view[3] / 2) - (before.view[1] + before.view[3] / 2)) / dt / width
        picture.append((vx, vy))
    lurch_windows = sum(
        1 for a, b in zip(picture, picture[lag:])
        if max(abs(b[0] - a[0]), abs(b[1] - a[1])) >= 0.2
    )
    lurches = lurch_windows / lag
    signs = [(s.speeds[0] >= 52) - (s.speeds[0] <= 48) for s in samples]
    moving = [sign for sign in signs if sign]
    reversals = sum(1 for a, b in zip(moving, moving[1:]) if a != b)
    zoom_signs = [(s.speeds[2] > 50) - (s.speeds[2] < 50) for s in samples]
    zoom_moving = [sign for sign in zoom_signs if sign]
    zoom_changes = sum(1 for a, b in zip(zoom_moving, zoom_moving[1:]) if a != b) + (1 if zoom_moving else 0)
    still = [
        s for s in samples
        if all((not p.present(s.t)) or p.state(s.t)[3] < 0.1 for p in targets)
    ]
    idle = sum(
        1 for s in still
        if abs(s.speeds[0] - 50) >= 2 or abs(s.speeds[1] - 50) >= 2 or s.speeds[2] != 50
    )
    idle_pan_tilt = sum(1 for s in still if abs(s.speeds[0] - 50) >= 2 or abs(s.speeds[1] - 50) >= 2)
    walking_heads = [
        (p.head_rect(s.t)[1] - s.view[1]) / s.view[3]
        for s in samples if s not in still
        for p in targets if p.present(s.t) and p.state(s.t)[3] >= 0.1
    ]
    walk_mean = sum(walking_heads) / len(walking_heads) if walking_heads else 0.0
    walk_sd = (sum((v - walk_mean) ** 2 for v in walking_heads) / len(walking_heads)) ** 0.5 if walking_heads else 0.0
    heads, offsets = [], []
    for s in still:
        for p in targets:
            if not p.present(s.t):
                continue
            x, y, w, h = p.head_rect(s.t)
            heads.append((y - s.view[1]) / s.view[3])
            offsets.append(abs(_normalized_centre_x(p.body_rect(s.t), s.view) - 0.5))
    mean = sum(heads) / len(heads) if heads else 0.0
    sd = (sum((v - mean) ** 2 for v in heads) / len(heads)) ** 0.5 if heads else 0.0
    return {
        "jolts_per_min": round(jolts / minutes, 1),
        "lurches_per_min": round(lurches / minutes, 1),
        "reversals_per_min": round(reversals / minutes, 1),
        "idle_motion_pct": round(100.0 * idle / max(1, len(still)), 1),
        "idle_pan_tilt_pct": round(100.0 * idle_pan_tilt / max(1, len(still)), 1),
        "zoom_changes_per_min": round(zoom_changes / minutes, 1),
        "headroom_mean": round(mean, 3),
        "headroom_sd": round(sd, 3),
        "walk_headroom_sd": round(walk_sd, 3),
        "offcentre_mean": round(sum(offsets) / len(offsets), 3) if offsets else 0.0,
    }
