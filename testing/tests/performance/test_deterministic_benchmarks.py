from __future__ import annotations

import pytest

from testing.harness.benchmarks import compare
from testing.harness.performance import collect_deterministic_metrics


@pytest.mark.performance
def test_deterministic_metrics_are_finite_and_named() -> None:
    metrics = collect_deterministic_metrics()
    assert metrics
    assert len({item["name"] for item in metrics}) == len(metrics)
    for metric in metrics:
        assert metric["iterations"] > 0
        assert 0 <= metric["minimum"] <= metric["median"] <= metric["p95"] <= metric["maximum"]


def test_benchmark_comparison_respects_lower_and_higher_is_better_metrics() -> None:
    baseline = {
        "metrics": [
            {"name": "latency", "p95": 100.0},
            {"name": "fps", "p95": 20.0, "direction": "higher_is_better"},
        ]
    }
    comparisons = compare(
        [
            {"name": "latency", "p95": 121.0},
            {"name": "fps", "p95": 10.0, "direction": "higher_is_better"},
        ],
        baseline,
    )

    assert comparisons[0]["status"] == "failed"
    assert comparisons[0]["delta_percent"] == 21.0
    # Deterministic comparisons retain the shared absolute threshold, so use a
    # large FPS drop here to prove directionality as well as percentage logic.
    assert comparisons[1]["status"] == "failed"
    assert comparisons[1]["delta_percent"] == 50.0


def test_live_regressions_warn_instead_of_failing() -> None:
    comparisons = compare(
        [{"name": "hardware_round_trip", "p95": 350.0}],
        {"metrics": [{"name": "hardware_round_trip", "p95": 100.0}]},
        live=True,
    )

    assert comparisons == [
        {
            "name": "hardware_round_trip",
            "status": "warning",
            "baseline_p95": 100.0,
            "current_p95": 350.0,
            "delta_ms": 250.0,
            "delta_percent": 250.0,
            "direction": "lower_is_better",
        }
    ]
