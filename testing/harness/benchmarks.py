from __future__ import annotations

import json
import math
import os
import statistics
import time
from pathlib import Path
from typing import Any, Callable

from testing.harness.paths import BASELINE_ROOT


DETERMINISTIC_PERCENT_LIMIT = 15.0
DETERMINISTIC_ABSOLUTE_MS = 5.0
LIVE_PERCENT_LIMIT = 25.0
LIVE_ABSOLUTE_MS = 100.0


def percentile(samples: list[float], quantile: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1))
    return ordered[index]


def measure(name: str, operation: Callable[[], Any], *, iterations: int = 100, warmups: int = 10) -> dict[str, Any]:
    for _ in range(warmups):
        operation()
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        operation()
        samples.append((time.perf_counter() - started) * 1000.0)
    return {
        "name": name,
        "unit": "ms",
        "iterations": iterations,
        "median": round(statistics.median(samples), 4),
        "p95": round(percentile(samples, 0.95), 4),
        "p99": round(percentile(samples, 0.99), 4),
        "minimum": round(min(samples), 4),
        "maximum": round(max(samples), 4),
    }


def compare(metrics: list[dict[str, Any]], baseline: dict[str, Any], *, live: bool = False) -> list[dict[str, Any]]:
    baseline_metrics = {item["name"]: item for item in baseline.get("metrics", [])}
    percent_limit = LIVE_PERCENT_LIMIT if live else DETERMINISTIC_PERCENT_LIMIT
    absolute_limit = LIVE_ABSOLUTE_MS if live else DETERMINISTIC_ABSOLUTE_MS
    comparisons: list[dict[str, Any]] = []
    for metric in metrics:
        previous = baseline_metrics.get(metric["name"])
        if not previous or float(previous.get("p95", 0)) <= 0:
            comparisons.append({"name": metric["name"], "status": "new"})
            continue
        old = float(previous["p95"])
        new = float(metric["p95"])
        direction = str(metric.get("direction") or previous.get("direction") or "lower_is_better")
        delta = new - old
        regression_delta = old - new if direction == "higher_is_better" else delta
        percent = regression_delta / old * 100.0
        regressed = regression_delta > absolute_limit and percent > percent_limit
        comparisons.append(
            {
                "name": metric["name"],
                "status": "warning" if live and regressed else "failed" if regressed else "passed",
                "baseline_p95": old,
                "current_p95": new,
                "delta_ms": round(delta, 4),
                "delta_percent": round(percent, 2),
                "direction": direction,
            }
        )
    return comparisons


def load_baseline(suite: str) -> dict[str, Any]:
    path = BASELINE_ROOT / f"{suite}.json"
    if not path.exists():
        return {"suite": suite, "metrics": []}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def accept_baseline(suite: str, result_path: Path) -> Path:
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    if payload.get("suite") != suite:
        raise ValueError(f"Expected suite {suite!r}, found {payload.get('suite')!r}")
    target = BASELINE_ROOT / f"{suite}.json"
    write_json_atomic(target, payload)
    return target
