from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from production_hub.core.config.defaults import build_default_config, build_default_endpoints
from production_hub.core.endpoints.registry import EndpointRegistry
from production_hub.integrations.scoreboard.models import ScoreRow
from production_hub.integrations.scoreboard.repository import ScoreboardRepository
from production_hub.integrations.scoreboard.service import ScoreboardService
from testing.harness.benchmarks import compare, load_baseline, measure, write_json_atomic
from testing.harness.inventory import html_pages
from testing.harness.paths import REPOSITORY_ROOT
from testing.harness.reporting import git_revision


def collect_deterministic_metrics() -> list[dict[str, Any]]:
    endpoints = build_default_endpoints()
    registry = EndpointRegistry(endpoints)
    config = build_default_config()
    page_paths = sorted(html_pages())

    metrics = [
        measure(
            "endpoint_registry_match",
            lambda: registry.matching_endpoint("/presentation/example/3/trigger", "POST", {}),
            iterations=500,
        ),
        measure(
            "config_serialize",
            config.to_dict,
            iterations=150,
        ),
        measure(
            "remote_page_read",
            lambda: ((REPOSITORY_ROOT / page_paths[0]).read_bytes() if page_paths else b""),
            iterations=500,
        ),
    ]
    with tempfile.TemporaryDirectory(prefix="production-hub-benchmark-") as temp_dir:
        root = Path(temp_dir)
        repository = ScoreboardRepository(root / "state", root / "backups")
        service = ScoreboardService(repository, config.integrations.scoreboard)
        service.update_state(
            {"rows": [ScoreRow("row-1", "Test", 1).to_dict()]},
            writer={"source": "benchmark"},
        )
        metrics.append(measure("scoreboard_state_load", service.get_state, iterations=200))
    return metrics


def run_deterministic_benchmarks(
    output_path: Path,
    *,
    extra_metrics: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], bool]:
    metrics = [*collect_deterministic_metrics(), *(extra_metrics or [])]
    baseline = load_baseline("deterministic")
    comparisons = compare(metrics, baseline, live=False)
    payload = {
        "suite": "deterministic",
        "created_at": datetime.now(UTC).isoformat(),
        "revision": git_revision(),
        "metrics": metrics,
        "comparisons": comparisons,
    }
    write_json_atomic(output_path, payload)
    return payload, not any(item.get("status") == "failed" for item in comparisons)
