from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path


DEV_CHILD_ARGUMENT = "--production-hub-dev-child"
DEV_BUNDLE_ID = "org.icas.productionhub.dev"
DEV_APP_NAME = "Production Hub Dev"


class DevelopmentLauncherError(RuntimeError):
    pass


def should_use_development_app(arguments: list[str]) -> bool:
    """Use an app-bundled interpreter for source GUI runs that may access cameras."""

    if sys.platform != "darwin" or getattr(sys, "frozen", False):
        return False
    if os.environ.get("PRODUCTION_HUB_DIRECT_PYTHON") == "1":
        return False
    if DEV_CHILD_ARGUMENT in arguments:
        return False
    return not any(item in arguments for item in ("--api-only", "--help", "-h"))


def without_dev_child_argument(arguments: list[str]) -> list[str]:
    return [item for item in arguments if item != DEV_CHILD_ARGUMENT]


def run_in_development_app(arguments: list[str], app_root: Path) -> int:
    return run_python_entry_in_development_app(
        arguments,
        app_root,
        app_root / "main.py",
    )


def run_python_entry_in_development_app(
    arguments: list[str],
    app_root: Path,
    entry_path: Path,
) -> int:
    """Run a source entry point with Production Hub Dev's stable privacy identity."""

    app_path = prepare_development_app(app_root)
    executable = app_path / "Contents" / "MacOS" / DEV_APP_NAME
    command = [
        str(executable),
        str(entry_path.resolve()),
        DEV_CHILD_ARGUMENT,
        *arguments,
    ]
    try:
        return subprocess.run(command, cwd=app_root, check=False).returncode
    except OSError as exc:
        raise DevelopmentLauncherError(f"Could not launch {app_path}: {exc}") from exc


def prepare_development_app(app_root: Path) -> Path:
    """Create a stable, source-loading app identity once and reuse it across edits."""

    app_path = app_root / ".build" / "dev" / f"{DEV_APP_NAME}.app"
    executable = app_path / "Contents" / "MacOS" / DEV_APP_NAME
    info_path = app_path / "Contents" / "Info.plist"
    if executable.is_file() and _valid_info_plist(info_path):
        return app_path

    python_app = _python_framework_app()
    if not python_app.is_dir():
        raise DevelopmentLauncherError(
            "The framework Python.app needed for camera-aware source runs was not found. "
            "Use scripts/build_macos_app.py --no-install as a fallback."
        )

    app_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = app_path.with_name(f".{DEV_APP_NAME}.building.app")
    if temporary.exists():
        shutil.rmtree(temporary)
    try:
        shutil.copytree(python_app, temporary, symlinks=True)
        original_executable = temporary / "Contents" / "MacOS" / "Python"
        renamed_executable = temporary / "Contents" / "MacOS" / DEV_APP_NAME
        original_executable.rename(renamed_executable)
        _write_info_plist(temporary / "Contents" / "Info.plist")
        icon = app_root / "assets" / "ProductionHub.icns"
        if icon.is_file():
            resources = temporary / "Contents" / "Resources"
            resources.mkdir(parents=True, exist_ok=True)
            shutil.copy2(icon, resources / "ProductionHub.icns")
        _run_codesign(temporary)
        if app_path.exists():
            shutil.rmtree(app_path)
        temporary.rename(app_path)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return app_path


def _python_framework_app() -> Path:
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    candidates = [
        Path(sys.base_prefix) / "Resources" / "Python.app",
        Path("/Library/Frameworks/Python.framework/Versions")
        / version
        / "Resources"
        / "Python.app",
    ]
    return next((item for item in candidates if item.is_dir()), candidates[0])


def _write_info_plist(path: Path) -> None:
    with path.open("rb") as handle:
        info = plistlib.load(handle)
    info.update(
        {
            "CFBundleDisplayName": DEV_APP_NAME,
            "CFBundleExecutable": DEV_APP_NAME,
            "CFBundleIdentifier": DEV_BUNDLE_ID,
            "CFBundleName": DEV_APP_NAME,
            "CFBundleIconFile": "ProductionHub",
            "CFBundleShortVersionString": "1.0",
            "CFBundleVersion": "1",
            "NSCameraUsageDescription": (
                "Production Hub Dev accesses connected camera inputs while running source code."
            ),
            "NSLocalNetworkUsageDescription": (
                "Production Hub Dev discovers and receives NDI video on the production network."
            ),
            "NSBonjourServices": ["_ndi._tcp", "_ndi._udp"],
            "NSCameraUseContinuityCameraDeviceType": True,
            "NSHighResolutionCapable": True,
        }
    )
    with path.open("wb") as handle:
        plistlib.dump(info, handle)


def _valid_info_plist(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return False
    return (
        info.get("CFBundleIdentifier") == DEV_BUNDLE_ID
        and info.get("CFBundleExecutable") == DEV_APP_NAME
        and bool(info.get("NSCameraUsageDescription"))
    )


def _run_codesign(app_path: Path) -> None:
    result = subprocess.run(
        ["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(app_path)],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise DevelopmentLauncherError(f"Could not sign the development launcher: {detail}")
