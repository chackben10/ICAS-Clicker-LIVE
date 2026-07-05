#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import importlib.metadata
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


APP_NAME = "Production Hub"
BUNDLE_ID = "org.icas.productionhub"
REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = Path(__file__).resolve().parents[1]
DIST_ROOT = APP_ROOT / "dist"
BUILD_ROOT = APP_ROOT / "build" / "macos"
ICON_SOURCE = APP_ROOT / "Production_hub.icon"
ICON_OUTPUT = APP_ROOT / "assets" / "ProductionHub.icns"
IC_TOOL = Path("/Applications/Xcode.app/Contents/Applications/Icon Composer.app/Contents/Executables/ictool")


class BuildError(RuntimeError):
    pass


def log(message: str) -> None:
    print(message, flush=True)


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    stream_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    if stream_output:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        output: list[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            output.append(line)
            print(line, end="", flush=True)
        returncode = process.wait()
        stdout = "".join(output)
        if returncode != 0:
            raise BuildError(f"Command failed: {' '.join(command)}\n{stdout.strip()}")
        return subprocess.CompletedProcess(command, returncode, stdout, "")

    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    if result.returncode != 0:
        details = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
        raise BuildError(f"Command failed: {' '.join(command)}\n{details}")
    return result


def install_dependencies() -> None:
    log("Installing Python dependencies...")
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-r",
            str(APP_ROOT / "requirements.txt"),
            "-r",
            str(APP_ROOT / "requirements-build.txt"),
        ],
        stream_output=True,
    )


def require_pyinstaller() -> None:
    if importlib.util.find_spec("PyInstaller") is None:
        raise BuildError(
            "PyInstaller is not installed. Run:\n"
            "  python3 Production_Hub_App/scripts/build_macos_app.py --install-deps --install\n"
            "or install build dependencies with:\n"
            "  python3 -m pip install -r Production_Hub_App/requirements.txt -r Production_Hub_App/requirements-build.txt"
        )


def reject_obsolete_pathlib_backport() -> None:
    try:
        distribution = importlib.metadata.distribution("pathlib")
    except importlib.metadata.PackageNotFoundError:
        return
    location = distribution.locate_file("")
    raise BuildError(
        "The active Python environment has the obsolete 'pathlib' backport installed. "
        "PyInstaller cannot build with this package present because pathlib is already "
        "part of Python 3.\n\n"
        f"Python executable: {sys.executable}\n"
        f"pathlib package location: {location}\n\n"
        "Remove it from this Python environment, then rerun the build:\n"
        f"  {sys.executable} -m pip uninstall pathlib\n\n"
        "Alternatively, build from a clean virtual environment so PyInstaller does not see "
        "global site-packages."
    )


def ensure_icon() -> Path:
    if ICON_OUTPUT.exists():
        log(f"Using existing icon: {ICON_OUTPUT}")
        return ICON_OUTPUT
    if not ICON_SOURCE.exists():
        raise BuildError(f"Missing Icon Composer source: {ICON_SOURCE}")
    if not IC_TOOL.exists():
        raise BuildError(
            "Apple Icon Composer's ictool was not found. Install Xcode with Icon Composer, "
            f"or place an exported icon at {ICON_OUTPUT}."
        )
    ICON_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    try:
        log("Rendering app icon...")
        render_icns_from_icon(ICON_SOURCE, ICON_OUTPUT)
    except BuildError as exc:
        raise BuildError(
            "Could not render Production_hub.icon with Apple's ictool, so the build stopped "
            "instead of falling back to the flat PNG.\n\n"
            "Open Production_Hub_App/Production_hub.icon in Icon Composer, re-save it, then rerun this script. "
            f"If your Icon Composer version can export a macOS .icns file, save it as {ICON_OUTPUT}.\n\n"
            f"ictool/iconutil details:\n{exc}"
        ) from exc
    return ICON_OUTPUT


def render_icns_from_icon(source: Path, output: Path) -> None:
    variants = [
        ("icon_16x16.png", 16, 1),
        ("icon_16x16@2x.png", 16, 2),
        ("icon_32x32.png", 32, 1),
        ("icon_32x32@2x.png", 32, 2),
        ("icon_128x128.png", 128, 1),
        ("icon_128x128@2x.png", 128, 2),
        ("icon_256x256.png", 256, 1),
        ("icon_256x256@2x.png", 256, 2),
        ("icon_512x512.png", 512, 1),
        ("icon_512x512@2x.png", 512, 2),
    ]
    with tempfile.TemporaryDirectory(prefix="production-hub-icon-") as temp_dir:
        iconset = Path(temp_dir) / "ProductionHub.iconset"
        iconset.mkdir()
        for filename, width, scale in variants:
            run(
                [
                    str(IC_TOOL),
                    str(source),
                    "--export-image",
                    "--output-file",
                    str(iconset / filename),
                    "--platform",
                    "macOS",
                    "--rendition",
                    "Default",
                    "--width",
                    str(width),
                    "--height",
                    str(width),
                    "--scale",
                    str(scale),
                ]
            )
        run(["iconutil", "--convert", "icns", "--output", str(output), str(iconset)])


def stage_remote_pages() -> Path:
    log("Packaging remote HTML pages...")
    staging = BUILD_ROOT / "remote_pages"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    copied = 0
    for html_file in sorted(REPO_ROOT.rglob("*.html")):
        relative = html_file.relative_to(REPO_ROOT)
        if relative.parts and relative.parts[0] == "Production_Hub_App":
            continue
        destination = staging / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(html_file, destination)
        copied += 1
    if copied == 0:
        raise BuildError("No remote HTML pages were found to package.")
    log(f"Packaged {copied} remote HTML page(s).")
    return staging


def build_app(icon_path: Path, remote_pages: Path) -> Path:
    log("Checking build environment...")
    require_pyinstaller()
    reject_obsolete_pathlib_backport()
    pyinstaller_work = BUILD_ROOT / "pyinstaller"
    spec_path = BUILD_ROOT / "spec"
    for directory in (pyinstaller_work, spec_path):
        directory.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        APP_NAME,
        "--osx-bundle-identifier",
        BUNDLE_ID,
        "--icon",
        str(icon_path),
        "--paths",
        str(APP_ROOT),
        "--distpath",
        str(DIST_ROOT),
        "--workpath",
        str(pyinstaller_work),
        "--specpath",
        str(spec_path),
        "--add-data",
        f"{remote_pages}:remote_pages",
        "--add-data",
        f"{icon_path}:assets",
        "--collect-all",
        "PySide6",
        "--hidden-import",
        "uvicorn.logging",
        "--hidden-import",
        "uvicorn.loops.auto",
        "--hidden-import",
        "uvicorn.protocols.http.auto",
        "--hidden-import",
        "uvicorn.protocols.websockets.auto",
        "--hidden-import",
        "uvicorn.lifespan.on",
        str(APP_ROOT / "main.py"),
    ]
    log("Building Production Hub.app with PyInstaller...")
    run(command, cwd=APP_ROOT, stream_output=True)
    app_path = DIST_ROOT / f"{APP_NAME}.app"
    if not app_path.exists():
        raise BuildError(f"PyInstaller finished, but {app_path} was not created.")
    log("Finalizing app bundle...")
    finalize_bundle_icon(app_path, icon_path)
    return app_path


def finalize_bundle_icon(app_path: Path, icon_path: Path) -> None:
    resources = app_path / "Contents" / "Resources"
    resources.mkdir(parents=True, exist_ok=True)
    shutil.copy2(icon_path, resources / "ProductionHub.icns")
    assets = resources / "assets"
    assets.mkdir(exist_ok=True)
    shutil.copy2(icon_path, assets / "ProductionHub.icns")

    info_plist = app_path / "Contents" / "Info.plist"
    with info_plist.open("rb") as handle:
        info = plistlib.load(handle)
    info["CFBundleDisplayName"] = APP_NAME
    info["CFBundleName"] = APP_NAME
    info["CFBundleIdentifier"] = BUNDLE_ID
    info["CFBundleIconFile"] = "ProductionHub"
    info["CFBundleIconName"] = "ProductionHub"
    info["NSHighResolutionCapable"] = True
    with info_plist.open("wb") as handle:
        plistlib.dump(info, handle)
    run(["/usr/bin/touch", str(app_path)])


def install_app(app_path: Path, destination_root: Path) -> Path:
    log(f"Installing app to {destination_root}...")
    target = destination_root / app_path.name
    destination_root.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(app_path, target, symlinks=True)
    run(["/usr/bin/touch", str(target)])
    return target


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and optionally install Production Hub.app for macOS.")
    parser.add_argument("--install", action="store_true", help="Copy/update Production Hub.app in /Applications after building.")
    parser.add_argument("--install-destination", type=Path, default=Path("/Applications"), help="Destination folder for --install.")
    parser.add_argument("--install-deps", action="store_true", help="Install runtime and build dependencies before building.")
    parser.add_argument("--icon-only", action="store_true", help="Only generate assets/ProductionHub.icns from Production_hub.icon.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        icon_path = ensure_icon()
        if args.icon_only:
            print(f"Icon ready: {icon_path}")
            return 0
        if args.install_deps:
            install_dependencies()
        remote_pages = stage_remote_pages()
        app_path = build_app(icon_path, remote_pages)
        print(f"Built: {app_path}")
        if args.install:
            installed = install_app(app_path, args.install_destination.expanduser())
            print(f"Installed: {installed}")
        return 0
    except BuildError as exc:
        print(f"Build failed:\n{exc}", file=sys.stderr)
        return 1
    except PermissionError as exc:
        print(
            "Build failed because macOS denied file access. If this happened while installing to /Applications, "
            "rerun the same command with sudo or choose a user-writable --install-destination.",
            file=sys.stderr,
        )
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
