"""Choose which build of `production_hub` the stage-realism suite exercises.

`installed` (the default) is the live build, /Applications/Production Hub.app:
its PyInstaller archive is unpacked into a cache as sourceless `.pyc` files and
put first on `sys.path`, so the tests exercise the exact bytecode that ran on
Sunday. `dev` (alias `worktree`) runs `Production_Hub_App/` from this checkout, which is
what /Applications/Production Hub Dev.app launches, uncommitted work included.

Selection must happen before anything imports `production_hub`; `activate()`
refuses to switch once a different build is already loaded.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import marshal
import os
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKTREE_SOURCE = REPO_ROOT / "Production_Hub_App"
INSTALLED_APP = Path("/Applications/Production Hub.app")
CACHE_ROOT = Path.home() / "Library/Caches/ProductionHubStageRealism"


@dataclass(frozen=True, slots=True)
class Target:
    name: str
    path: Path
    description: str


_active: Target | None = None


def _executable(app: Path) -> Path:
    return app / "Contents/MacOS/Production Hub"


def _extract_installed(app: Path) -> Path:
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

    executable = _executable(app)
    if not executable.exists():
        raise FileNotFoundError(f"{executable} is missing; is Production Hub installed?")
    stat = executable.stat()
    key = hashlib.sha256(f"{executable}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()[:16]
    root = CACHE_ROOT / key
    marker = root / "manifest.json"
    if marker.exists():
        return root

    archive = CArchiveReader(str(executable))
    pyz_name = next(name for name in archive.toc if name.endswith(".pyz"))
    staging = root.with_suffix(".tmp")
    staging.mkdir(parents=True, exist_ok=True)
    pyz_path = staging / "PYZ.pyz"
    pyz_path.write_bytes(archive.extract(pyz_name))
    pyz = ZlibArchiveReader(str(pyz_path))
    names = sorted(name for name in pyz.toc if name == "production_hub" or name.startswith("production_hub."))
    packages = {name for name in names if any(other.startswith(name + ".") for other in names)}
    header = importlib.util.MAGIC_NUMBER + b"\x00" * 12
    for name in names:
        code = pyz.extract(name)
        if code is None:
            continue
        parts = name.split(".")
        if name in packages:
            target = staging.joinpath(*parts, "__init__.pyc")
        else:
            target = staging.joinpath(*parts[:-1], parts[-1] + ".pyc")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(header + marshal.dumps(code))
    pyz_path.unlink()
    info = {
        "app": str(app),
        "executable_mtime": stat.st_mtime,
        "executable_size": stat.st_size,
        "modules": len(names),
        "python": sys.version.split()[0],
    }
    (staging / "manifest.json").write_text(json.dumps(info, indent=1))
    staging.rename(root)
    return root


def activate(name: str | None = None) -> Target:
    """Put the chosen build first on `sys.path`; idempotent per process."""

    global _active
    name = (name or os.environ.get("STAGE_REALISM_TARGET") or "installed").lower()
    if name == "worktree":
        name = "dev"
    if _active is not None:
        if _active.name != name:
            raise RuntimeError(
                f"production_hub is already loaded from the {_active.name!r} build; "
                "run each target in its own process."
            )
        return _active
    if "production_hub" in sys.modules:
        raise RuntimeError("production_hub was imported before the stage-realism target was chosen.")

    if name == "installed":
        path = _extract_installed(INSTALLED_APP)
        manifest = json.loads((path / "manifest.json").read_text())
        from datetime import datetime

        built = datetime.fromtimestamp(manifest["executable_mtime"]).strftime("%Y-%m-%d %H:%M")
        description = f"Installed Production Hub.app (built {built}, {manifest['modules']} modules)"
    elif name == "dev":
        path = WORKTREE_SOURCE
        description = f"Dev build (this checkout, as the Dev app runs it: {WORKTREE_SOURCE})"
    else:
        raise ValueError("STAGE_REALISM_TARGET must be 'installed' or 'dev'.")

    # The worktree copy is usually already on sys.path through the test
    # configuration; the chosen build must win.
    sys.path[:] = [item for item in sys.path if Path(item or ".").resolve() != WORKTREE_SOURCE]
    sys.path.insert(0, str(path))
    _active = Target(name, path, description)
    return _active


# How the app configures tracking at launch (production_hub/app/main.py
# `_run`). Builds from 2026-09-27 on follow the saved "Improved subject
# tracking" setting (on by default) in release and Dev alike. Earlier builds
# switched these flags on only in the Dev app.
DEV_APP_AUTOMATION_FLAGS = (
    "motion_easing_enabled",
    "tracking_refinements_enabled",
    "safe_zone_tracking_enabled",
)
DEV_APP_TRACKING_FLAGS = ("validated_boxes_enabled",)


def apply_app_settings(tracking, automation) -> None:
    """Configure services the way the chosen app configures them at launch."""

    if hasattr(automation, "apply_tracking_setting"):
        automation.follow_tracking_setting = True
        automation.apply_tracking_setting()
        return
    if _active is None or _active.name != "dev":
        return
    for name in DEV_APP_AUTOMATION_FLAGS:
        if hasattr(automation, name):
            setattr(automation, name, True)
    for name in DEV_APP_TRACKING_FLAGS:
        if hasattr(tracking, name):
            setattr(tracking, name, True)
