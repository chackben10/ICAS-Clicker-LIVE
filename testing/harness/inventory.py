from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from production_hub.api.server import create_app
from production_hub.app.bootstrap import build_context
from production_hub.core.config.defaults import build_default_automations, build_default_config, build_default_endpoints
from production_hub.core.endpoints.catalog import ACTION_SPECS
from testing.harness.paths import FEATURE_MATRIX, REPOSITORY_ROOT


INTEGRATIONS = {
    "propresenter",
    "obs",
    "panasonic",
    "visca",
    "midi",
    "scoreboard",
    "video",
    "camera_tracking",
}


def runtime_catalogs() -> dict[str, set[str]]:
    return {
        "endpoints": {item.key for item in build_default_endpoints()},
        "automations": {item.key for item in build_default_automations()},
        "actions": {item.action_type for item in ACTION_SPECS},
        "integrations": set(INTEGRATIONS),
        "pages": html_pages(),
        "api_routes": explicit_api_routes(),
    }


def explicit_api_routes() -> set[str]:
    with TemporaryDirectory(prefix="production-hub-route-inventory-") as temp_dir:
        context = build_context(Path(temp_dir))
        try:
            schema = create_app(context).openapi()
            return {
                f"{method.upper()} {path}"
                for path, definition in schema["paths"].items()
                for method in definition
            }
        finally:
            context.video.shutdown()


def load_feature_matrix(path: Path = FEATURE_MATRIX) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def matrix_catalogs(matrix: dict[str, Any] | None = None) -> dict[str, set[str]]:
    source = matrix or load_feature_matrix()
    return {
        name: set(source["catalogs"][name]["features"])
        for name in ("endpoints", "automations", "actions", "integrations", "pages", "api_routes")
    }


def html_pages() -> set[str]:
    ignored_roots = {"Production_Hub_App", "Production_Hub_Video_to_NDI_App", "testing", ".git"}
    pages: set[str] = set()
    for path in REPOSITORY_ROOT.rglob("*.html"):
        relative = path.relative_to(REPOSITORY_ROOT)
        if relative.parts and relative.parts[0] in ignored_roots:
            continue
        pages.add(relative.as_posix())
    return pages


def coverage_errors() -> list[str]:
    actual = runtime_catalogs()
    declared = matrix_catalogs()
    errors: list[str] = []
    for name in actual:
        missing = sorted(actual[name] - declared[name])
        stale = sorted(declared[name] - actual[name])
        if missing:
            errors.append(f"{name}: missing matrix entries: {', '.join(missing)}")
        if stale:
            errors.append(f"{name}: stale matrix entries: {', '.join(stale)}")
    discovered_pages = html_pages()
    missing_pages = sorted(discovered_pages - declared["pages"])
    if missing_pages:
        errors.append(f"pages: repository HTML missing matrix entries: {', '.join(missing_pages)}")
    configured_pages = {item.path for item in build_default_config().remote_pages}
    if not configured_pages <= declared["pages"]:
        errors.append(
            "pages: configured remote pages missing matrix entries: "
            + ", ".join(sorted(configured_pages - declared["pages"]))
        )
    for name, definition in load_feature_matrix()["catalogs"].items():
        if not definition.get("tests"):
            errors.append(f"{name}: no test node IDs declared")
    return errors
