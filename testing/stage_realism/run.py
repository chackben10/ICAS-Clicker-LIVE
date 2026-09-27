"""Run the stage-realism suite and write a scorecard.

    testing/.venv/bin/python -m testing.stage_realism.run                 # installed app
    testing/.venv/bin/python -m testing.stage_realism.run --target dev
    testing/.venv/bin/python -m testing.stage_realism.run --group single -k walk_off

Each scenario runs twice: with the AW-HE120 motor as measured, and with it 15%
faster (the uncertainty of the measurement between and beyond the measured
zooms).

Output: testing/benchmarks/outputs/stage-realism/<stamp>-<target>/
  scorecard.md   human-readable results
  results.json   every rule, metric, and a 10 Hz camera/subject timeline
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from . import target as target_module

# The motor is measured on the real AW-HE120 (fixtures/icas_motor_measured.json).
# `fast_motor` covers the remaining uncertainty (zooms between and beyond the
# two measured ones) on the side that makes pace matching overshoot. It was
# +40% while the motor was only estimated from Sunday logs.
VARIANTS = {"measured_motor": 1.0, "fast_motor": 1.15}
OUTPUT_ROOT = target_module.REPO_ROOT / "testing/benchmarks/outputs/stage-realism"


# Settings to try that differ from the ICAS profile (e.g. a faster analysis rate).
OVERRIDES: dict[str, dict] = {"tracking": {}, "automation": {}}


def _world(scenario, variant: str, seed: int, record: Path | None = None):
    from .world import StageWorld

    return StageWorld(
        scenario.build(),
        scenario.targets,
        initial_zoom=scenario.zoom,
        duration=scenario.duration,
        speed_scale=VARIANTS[variant],
        seed=seed,
        rendered=scenario.rendered,
        record=record,
        tracking_overrides=dict(OVERRIDES["tracking"]),
        automation_overrides=dict(OVERRIDES["automation"]),
    )


def feasibility(scenario, variant: str, seed: int = 1) -> dict[str, bool]:
    """Can a perfect camera keep every head in frame at the live caps / at full speed?"""

    from .scoring import score
    from .world import STAGE, oracle_run

    automation = STAGE["automation_config"]
    caps = {
        "live_caps": (
            automation["continuous_pan_max_speed"],
            automation["continuous_tilt_max_speed"],
            automation["continuous_zoom_max_speed"],
        ),
        "full_speed": (49, 49, 49),
    }
    verdict = {}
    for label, (pan_limit, tilt_limit, zoom_limit) in caps.items():
        run = oracle_run(
            _world(scenario, variant, seed),
            pan_limit=pan_limit,
            tilt_limit=tilt_limit,
            zoom_limit=zoom_limit,
        )
        judged = score(scenario, variant, run)
        verdict[label] = judged.rule("head_in_frame").passed and judged.rule("keeps_up").passed
    return verdict


def run_scenario(scenario, variant: str, seed: int = 1, record: Path | None = None):
    from .scoring import score

    result = score(scenario, variant, _world(scenario, variant, seed, record).run())
    result.metrics["feasible"] = feasibility(scenario, variant, seed)
    return result


def _timeline(result) -> list[dict]:
    rows = []
    targets = [p for p in result.run.people if p.name in result.scenario.targets]
    for sample in result.run.samples[::2]:
        vx, vy, vw, vh = sample.view
        people = {}
        for person in result.run.people:
            if not person.present(sample.t):
                continue
            x, y, w, h = person.head_rect(sample.t)
            people[person.name] = [round((x + w / 2 - vx) / vw, 3), round((y - vy) / vh, 3)]
        rows.append(
            {
                "t": sample.t,
                "pose": [sample.pan, sample.tilt, sample.zoom],
                "speeds": list(sample.speeds),
                "state": sample.state,
                "source": sample.source,
                "headsInView": people,
                "targetsInFrame": all(
                    name in people and 0 <= people[name][0] <= 1 for name in (p.name for p in targets if p.present(sample.t))
                ),
            }
        )
    return rows


BASELINE = target_module.REPO_ROOT / "testing/stage_realism/fixtures/live_baseline.json"


def load_baseline() -> dict:
    return json.loads(BASELINE.read_text())["results"] if BASELINE.exists() else {}


def classify(key: str, passed: bool, baseline: dict) -> str:
    """challenge: the live app fails it; guard: the live app passes it and must keep passing."""

    if key not in baseline:
        return "new"
    if baseline[key]["passed"]:
        return "guard" if passed else "REGRESSION"
    return "FIXED" if passed else "challenge"


def _markdown(target, results, moments, started: str, baseline: dict, checks=()) -> str:
    lines = [
        "# PTZ stage-realism scorecard",
        "",
        f"- Build: **{target.description}**",
        f"- Run: {started}",
        "- Motor variants: `measured_motor` (measured AW-HE120), `fast_motor` (+15%, measurement uncertainty)",
        "- Status: **challenge** = the live app fails it too; **guard** = the live app passes it and so must this build;",
        "  **FIXED** = live fails, this build passes; **REGRESSION** = live passes, this build fails.",
        "",
        "Feasible = a perfect camera (exact positions, no sensing delay, same motors) keeps every head",
        "in frame at the live speed caps / at full motor speed. `no / yes` means the live speed caps alone",
        "make the scenario impossible; `yes / yes` means the tracking logic is what fails.",
        "",
    ]
    if checks:
        lines += ["## Detector checks on our stage", "", "| Check | Result | Status | Detail |", "|---|---|---|---|"]
        for check in checks:
            lines.append(f"| {check.name} | {'PASS' if check.passed else '**FAIL**'} | "
                         f"{classify(f'check:{check.name}', check.passed, baseline)} | {check.detail} |")
        lines.append("")
    if moments:
        lines += [
            "## Real stream moments (replayed frame by frame)",
            "",
            "| Moment | Result | Status | Never switches | Keeps lock | Reacts |",
            "|---|---|---|---|---|---|",
        ]
        for item in moments:
            key = f"moment:{item.name}"
            cells = ["✅" if item.rules.get(rule, (False, ""))[0] else "❌" for rule in ("never_switches", "keeps_lock", "reacts")]
            lines.append(f"| {item.name} | {'PASS' if item.passed else '**FAIL**'} | {classify(key, item.passed, baseline)} | " + " | ".join(cells) + " |")
        lines.append("")
    lines += [
        "## Scenarios",
        "",
        "| Scenario | Group | Mode | Motor | Result | Status | Head in frame | Keeps up | Settles | Right person | Stays armed | Heads in frame | Feasible (live caps / full) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for item in results:
        cells = []
        for rule in ("head_in_frame", "keeps_up", "settles", "right_person", "stays_armed"):
            cells.append("✅" if item.rule(rule).passed else "❌")
        key = f"{item.scenario.name}|{item.variant}"
        lines.append(
            f"| {item.scenario.name} | {item.scenario.group} | {'rendered' if item.scenario.rendered else 'model'} | {item.variant} | "
            f"{'PASS' if item.passed else '**FAIL**'} | {classify(key, item.passed, baseline)} | " + " | ".join(cells)
            + f" | {item.metrics['heads_in_frame_pct']}% | "
            + " / ".join("yes" if item.metrics["feasible"][k] else "no" for k in ("live_caps", "full_speed")) + " |"
        )
    lines += ["", "## Details", ""]
    for item in moments:
        lines.append(f"### moment {item.name} — {'PASS' if item.passed else 'FAIL'}")
        lines.append(f"{item.note}  ")
        lines.append(f"Source: {item.source}  ")
        lines.append(f"Log: {item.log}")
        lines.append("")
        for rule, (ok, detail) in item.rules.items():
            lines.append(f"- {'✅' if ok else '❌'} **{rule}**: {detail}")
        lines.append("")
    for item in results:
        lines.append(f"### {item.scenario.name} — {item.variant} — {'PASS' if item.passed else 'FAIL'}")
        lines.append(f"{item.scenario.title}  ")
        lines.append(f"Source: {item.scenario.source}")
        lines.append("")
        for rule in item.rules:
            lines.append(f"- {'✅' if rule.passed else '❌'} **{rule.rule}**: {rule.detail}")
        lines.append(f"- time by tracking source: {item.metrics.get('time_by_source_s')}")
        lines.append(f"- peak pan speed offset: {item.metrics.get('peak_pan_speed')}")
        lines.append(f"- watchability: {item.metrics.get('watchability')}")
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", default="installed", choices=("installed", "dev", "worktree"))
    parser.add_argument("--group", choices=("single", "double", "triple", "all"), default="all")
    parser.add_argument("-k", dest="keyword", default="", help="only scenarios whose name contains this")
    parser.add_argument("--variant", choices=(*VARIANTS, "both"), default="both")
    parser.add_argument("--mode", choices=("model", "rendered", "replay", "all"), default="all",
                        help="model: fast simulated detector; rendered: real stage imagery + real detector; "
                        "replay: real stream moments")
    parser.add_argument("--video", action="store_true", help="save an MP4 of each rendered run's PTZ feed")
    parser.add_argument("--write-baseline", action="store_true",
                        help="record these results as the live app's baseline (installed target only)")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--analysis-fps", type=float,
                        help="person-detection rate per camera (the app's Analysis rate setting)")
    parser.add_argument("--decision-fps", type=float, help="PTZ tracking decisions per second")
    args = parser.parse_args(argv)
    if args.analysis_fps:
        OVERRIDES["tracking"]["analysis_fps"] = args.analysis_fps
    if args.decision_fps:
        OVERRIDES["automation"]["decision_fps"] = args.decision_fps

    target = target_module.activate(args.target)
    if args.write_baseline and target.name != "installed":
        parser.error("--write-baseline records the live app; use --target installed")
    from .scenarios import ALL

    scenarios = [
        item
        for item in ALL
        if (args.group == "all" or item.group == args.group)
        and args.keyword in item.name
        and (args.mode == "all" or (args.mode == "rendered") == item.rendered)
        and args.mode != "replay"
    ]
    variants = list(VARIANTS) if args.variant == "both" else [args.variant]
    started = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    output = args.output or OUTPUT_ROOT / f"{stamp}-{target.name}"
    baseline = {} if args.write_baseline else load_baseline()
    print(f"Target: {target.description}")

    checks = []
    if args.mode in ("rendered", "all") and not args.keyword:
        from . import render
        from .checks import empty_stage_sweep

        if render.assets_ready():
            check = empty_stage_sweep()
            checks.append(check)
            print(f"{'PASS' if check.passed else 'FAIL'}  check {check.name:42s} "
                  f"{classify(f'check:{check.name}', check.passed, baseline):10s} {check.detail}")

    moments = []
    if args.mode in ("replay", "all") and args.group in ("single", "all"):
        from . import render
        from .replay import available, replay

        if render.assets_ready():
            for name in available():
                if args.keyword not in name:
                    continue
                item = replay(name)
                moments.append(item)
                failed = [rule for rule, (ok, _) in item.rules.items() if not ok]
                status = classify(f"moment:{name}", item.passed, baseline)
                print(f"{'PASS' if item.passed else 'FAIL'}  moment {name:41s} {status:10s} "
                      + (f"failed: {', '.join(failed)}" if failed else ""))

    results = []
    for scenario in scenarios:
        for variant in variants:
            video = output / "videos" / f"{scenario.name}-{variant}.mp4" if args.video and scenario.rendered else None
            result = run_scenario(scenario, variant, record=video)
            results.append(result)
            failed = [rule.rule for rule in result.rules if not rule.passed]
            status = classify(f"{scenario.name}|{variant}", result.passed, baseline)
            print(
                f"{'PASS' if result.passed else 'FAIL'}  {scenario.name:48s} {variant:15s} {status:10s} "
                f"heads in frame {result.metrics['heads_in_frame_pct']:5.1f}%  "
                f"feasible {'Y' if result.metrics['feasible']['live_caps'] else 'n'}/{'Y' if result.metrics['feasible']['full_speed'] else 'n'}  "
                + (f"failed: {', '.join(failed)}" if failed else "")
            )

    output.mkdir(parents=True, exist_ok=True)
    (output / "scorecard.md").write_text(_markdown(target, results, moments, started, baseline, checks))
    (output / "results.json").write_text(
        json.dumps(
            {
                "target": target.description,
                "targetName": target.name,
                "started": started,
                "moments": [
                    {
                        "moment": item.name,
                        "source": item.source,
                        "passed": item.passed,
                        "rules": {rule: {"passed": ok, "detail": detail} for rule, (ok, detail) in item.rules.items()},
                        "timeline": item.timeline,
                    }
                    for item in moments
                ],
                "results": [
                    {
                        "scenario": item.scenario.name,
                        "group": item.scenario.group,
                        "rendered": item.scenario.rendered,
                        "title": item.scenario.title,
                        "source": item.scenario.source,
                        "variant": item.variant,
                        "passed": item.passed,
                        "firstFailureT": item.first_failure_t,
                        "rules": [
                            {"rule": r.rule, "passed": r.passed, "detail": r.detail, "value": r.value}
                            for r in item.rules
                        ],
                        "metrics": item.metrics,
                        "timeline": _timeline(item),
                    }
                    for item in results
                ],
            },
            indent=1,
        )
    )
    if args.write_baseline:
        record = load_baseline()
        for check in checks:
            record[f"check:{check.name}"] = {"passed": check.passed, "failed": [] if check.passed else [check.name]}
        for item in moments:
            record[f"moment:{item.name}"] = {
                "passed": item.passed,
                "failed": [rule for rule, (ok, _) in item.rules.items() if not ok],
            }
        for item in results:
            record[f"{item.scenario.name}|{item.variant}"] = {
                "passed": item.passed,
                "failed": [rule.rule for rule in item.rules if not rule.passed],
            }
        BASELINE.write_text(
            json.dumps({"build": target.description, "recorded": started, "results": dict(sorted(record.items()))}, indent=1)
            + "\n"
        )
        print(f"Live baseline written: {BASELINE}")
    total = len(results) + len(moments) + len(checks)
    passed = sum(item.passed for item in results) + sum(item.passed for item in moments) + sum(c.passed for c in checks)
    regressions = [
        key
        for key, ok in [(f"{i.scenario.name}|{i.variant}", i.passed) for i in results]
        + [(f"moment:{i.name}", i.passed) for i in moments]
        + [(f"check:{c.name}", c.passed) for c in checks]
        if classify(key, ok, baseline) == "REGRESSION"
    ]
    fixed = [
        key
        for key, ok in [(f"{i.scenario.name}|{i.variant}", i.passed) for i in results]
        + [(f"moment:{i.name}", i.passed) for i in moments]
        + [(f"check:{c.name}", c.passed) for c in checks]
        if classify(key, ok, baseline) == "FIXED"
    ]
    print(f"\n{passed}/{total} passed. Fixed vs live: {len(fixed)}. Regressions vs live: {len(regressions)}.")
    for key in regressions:
        print(f"  REGRESSION {key}")
    print(f"Scorecard: {output / 'scorecard.md'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
