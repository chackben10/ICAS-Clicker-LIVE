from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from production_hub.core.config.models import AppConfig
from testing.harness.benchmarks import compare, load_baseline, write_json_atomic
from testing.harness.paths import production_data_root
from testing.harness.reporting import git_revision


def pytest_collection_modifyitems(config, items) -> None:
    if os.environ.get("PRODUCTION_HUB_LIVE") == "1":
        return
    marker = pytest.mark.skip(reason="live tests require the full runner and its protected recovery snapshot")
    for item in items:
        item.add_marker(marker)


@pytest.fixture(scope="session")
def live_config() -> AppConfig:
    path = production_data_root() / "config" / "default_profile.json"
    return AppConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))


@pytest.fixture(scope="session")
def live_metrics():
    metrics: list[dict] = []
    for environment_name, metric_name in (
        ("PRODUCTION_HUB_PROPRESENTER_STARTUP_MS", "propresenter_startup_to_ready"),
        ("PRODUCTION_HUB_APP_STARTUP_MS", "production_hub_startup_to_ready"),
    ):
        raw = os.environ.get(environment_name)
        if raw:
            value = round(float(raw), 4)
            metrics.append(
                {
                    "name": metric_name,
                    "unit": "ms",
                    "iterations": 1,
                    "median": value,
                    "p95": value,
                    "p99": value,
                    "minimum": value,
                    "maximum": value,
                }
            )
    yield metrics
    output_dir = Path(os.environ["PRODUCTION_HUB_LIVE_OUTPUT"])
    comparisons = compare(metrics, load_baseline("production-mac"), live=True)
    payload = {
        "suite": "production-mac",
        "created_at": datetime.now(UTC).isoformat(),
        "revision": git_revision(),
        "metrics": metrics,
        "comparisons": comparisons,
    }
    write_json_atomic(output_dir / "production-mac.json", payload)
