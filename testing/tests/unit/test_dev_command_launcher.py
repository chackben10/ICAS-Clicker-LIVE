"""Finder launcher resolves its source tree and preserves arguments/errors."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("exit_code", [0, 17])
def test_double_click_launcher_runs_source_from_any_directory(tmp_path, exit_code):
    source = Path(__file__).resolve().parents[3] / "Launch Production Hub Dev.command"
    assert os.access(source, os.X_OK)
    project = tmp_path / "Project with spaces"
    app = project / "Production_Hub_App"
    app.mkdir(parents=True)
    launcher = project / source.name
    shutil.copy2(source, launcher)
    (app / "main.py").write_text(
        "import json, os, sys\n"
        "print(json.dumps({'cwd': os.getcwd(), 'args': sys.argv[1:]}))\n"
        f"raise SystemExit({exit_code})\n"
    )
    result = subprocess.run(
        [str(launcher), "--data-dir", "a folder with spaces", "--no-api"],
        cwd=tmp_path, text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == exit_code, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload == {
        "cwd": str(project),
        "args": ["--data-dir", "a folder with spaces", "--no-api"],
    }
