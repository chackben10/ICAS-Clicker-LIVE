from __future__ import annotations

from pathlib import Path

from production_hub.core.config.models import RemotePageConfig

CONTROL_HINTS = ("control", "score", "picker", "index", "debug")
DISPLAY_HINTS = ("display", "countdown", "clock", "audio", "wheel", "fullscreen", "large", "dynamic", "top-right", "bottom-right")


def page_kind(path: str) -> str:
    lowered = path.lower()
    if lowered.startswith("displays/") or lowered.startswith("scoreboard/") or any(hint in lowered for hint in DISPLAY_HINTS):
        return "Display"
    if any(hint in lowered for hint in CONTROL_HINTS):
        return "Control"
    return "HTML"


def display_name(path: str) -> str:
    stem = Path(path).stem
    if path == "index.html":
        return "Presentation Clicker / Viewer"
    return stem.replace("-", " ").replace("_", " ").title()


def required_integrations_for(path: str) -> list[str]:
    lowered = path.lower()
    if "score" in lowered:
        return ["Scoreboard Service"]
    if "audio" in lowered or "pads" in lowered:
        return ["ProPresenter"]
    if "control" in lowered or "index" in lowered or "picker" in lowered:
        return ["ProPresenter", "OBS"]
    return []


def discover_remote_pages(workspace_root: Path, configured_pages: list[RemotePageConfig]) -> list[dict[str, object]]:
    configured_by_path = {page.path: page for page in configured_pages}
    discovered: list[dict[str, object]] = []
    for file_path in sorted(workspace_root.glob("**/*.html")):
        relative = file_path.relative_to(workspace_root).as_posix()
        if relative.startswith("Production_Hub_App/"):
            continue
        configured = configured_by_path.get(relative)
        discovered.append(
            {
                "name": configured.name if configured else display_name(relative),
                "path": relative,
                "enabled": configured.enabled if configured else True,
                "required_integrations": configured.required_integrations if configured else required_integrations_for(relative),
                "access_protected": configured.access_protected if configured else False,
                "kind": page_kind(relative),
                "source": "Configured" if configured else "Discovered",
            }
        )
    for page in configured_pages:
        if not any(item["path"] == page.path for item in discovered):
            discovered.append(
                {
                    **page.to_dict(),
                    "kind": page_kind(page.path),
                    "source": "Configured Missing File",
                }
            )
    return sorted(discovered, key=lambda item: (str(item["kind"]), str(item["path"])))
