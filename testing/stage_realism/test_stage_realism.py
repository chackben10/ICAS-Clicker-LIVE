"""Every stage scenario and stream moment, judged by the operator's rules.

These are not in `testpaths`, so CI never collects them. Markers:

    challenge   the live app (Production Hub.app) fails it; the Dev build should
                learn to pass it
    guard       the live app passes it; the Dev build must not regress
    single / double / triple, rendered, moment

    STAGE_REALISM_TARGET=dev testing/.venv/bin/python -m pytest testing/stage_realism -m guard
    STAGE_REALISM_TARGET=dev testing/.venv/bin/python -m pytest testing/stage_realism -m "challenge and single"

A failure message names the broken rule, when it first broke, whether a
perfect camera could have done it at the live speed caps, and the recording
the scenario reproduces.
"""

from __future__ import annotations

import pytest

from testing.stage_realism import render
from testing.stage_realism.run import VARIANTS, load_baseline, run_scenario
from testing.stage_realism.scenarios import ALL

BASELINE = load_baseline()


def _status(key: str) -> str:
    entry = BASELINE.get(key)
    if entry is None:
        return "new"
    return "guard" if entry["passed"] else "challenge"


def _scenario_params():
    for scenario in ALL:
        for variant in VARIANTS:
            marks = [getattr(pytest.mark, scenario.group), getattr(pytest.mark, _status(f"{scenario.name}|{variant}"))]
            if scenario.rendered:
                marks.append(pytest.mark.rendered)
                marks.append(pytest.mark.skipif(not render.assets_ready(), reason="rendered assets not built"))
            yield pytest.param(scenario, variant, id=f"{scenario.name}-{variant}", marks=marks)


@pytest.mark.parametrize(("scenario", "variant"), list(_scenario_params()))
def test_stage_scenario(scenario, variant) -> None:
    result = run_scenario(scenario, variant)
    if result.passed:
        return
    feasible = result.metrics["feasible"]
    status = _status(f"{scenario.name}|{variant}")
    lines = [f"{scenario.title} [{variant}]" + (" — REGRESSION: the live app passes this" if status == "guard" else "")]
    lines += [f"  {rule.rule}: {rule.detail}" for rule in result.rules if not rule.passed]
    if result.first_failure_t is not None:
        lines.append(f"  first failure at {result.first_failure_t:.1f} s after arming")
    if not feasible["live_caps"]:
        lines.append(
            "  note: even a perfect camera fails this at the live speed caps"
            + (" (possible at full motor speed)" if feasible["full_speed"] else "")
        )
    lines.append(f"  source: {scenario.source}")
    pytest.fail("\n".join(lines), pytrace=False)


def _moment_params():
    from testing.stage_realism.replay import ARMED_POSE

    for name in sorted(ARMED_POSE):
        marks = [pytest.mark.single, pytest.mark.moment, getattr(pytest.mark, _status(f"moment:{name}"))]
        yield pytest.param(name, id=name, marks=marks)


@pytest.mark.parametrize("name", list(_moment_params()))
def test_stream_moment(name) -> None:
    from testing.stage_realism.replay import available, replay

    if name not in available():
        pytest.skip("stream excerpt not downloaded (tools/annotate_stream_moment.py)")
    result = replay(name)
    if result.passed:
        return
    lines = [f"{result.note}"]
    lines += [f"  {rule}: {detail}" for rule, (ok, detail) in result.rules.items() if not ok]
    lines.append(f"  log: {result.log}")
    lines.append(f"  source: {result.source}")
    pytest.fail("\n".join(lines), pytrace=False)


@pytest.mark.rendered
@pytest.mark.single
@pytest.mark.skipif(not render.assets_ready(), reason="rendered assets not built")
def test_empty_stage_has_no_people() -> None:
    from testing.stage_realism.checks import empty_stage_sweep

    result = empty_stage_sweep()
    assert result.passed, f"{result.detail}; (pan, tilt, confidence): {result.hits[:6]}"
