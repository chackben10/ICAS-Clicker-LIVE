"""The Applications launcher for Production Hub Dev builds, points at this checkout, and guards the live app."""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "Production_Hub_App" / "scripts"))
import install_dev_app  # noqa: E402


@pytest.mark.skipif(sys.platform != "darwin", reason="builds a macOS app bundle")
def test_launcher_bundle_runs_this_checkout_and_guards_the_live_app(tmp_path):
    python = Path(sys.executable)
    app = install_dev_app.build(tmp_path, python)
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleExecutable"] == "Production Hub Dev"
    # Its own identity: the Dev runtime's privacy grants stay with .build/dev.
    assert info["CFBundleIdentifier"] == install_dev_app.LAUNCHER_BUNDLE_ID != "org.icas.productionhub.dev"
    launcher = app / "Contents" / "MacOS" / "Production Hub Dev"
    command = app / "Contents" / "Resources" / "Open Production Hub Dev.command"
    for script in (launcher, command):
        assert script.stat().st_mode & 0o111
        subprocess.run(["/bin/zsh", "-n", str(script)], check=True)
    text = command.read_text()
    assert str(install_dev_app.REPOSITORY) in text and str(python) in text
    assert "Production_Hub_App/main.py" in text
    guard = launcher.read_text()
    assert 'tell application id "org.icas.productionhub" to quit' in guard
    # Detecting a running Dev must not match this launcher, which shares the name.
    assert ".build/dev/Production Hub Dev.app" in guard
    subprocess.run(["/usr/bin/codesign", "-v", str(app)], check=True)
