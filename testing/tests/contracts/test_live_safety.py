from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from testing.harness.live_system import ApplicationLifecycle, RecoverySnapshot
from testing.harness.ndi_fixture_sender import SINGLE_PERSON, TWO_PERSON, scenario_frames
from testing.harness.reporting import RunReport
from testing.run_end_to_end import run_soak_phase


def test_recovery_snapshot_restores_files_and_is_removed_only_after_success(tmp_path: Path) -> None:
    data_root = tmp_path / "Production Hub"
    (data_root / "config").mkdir(parents=True)
    (data_root / "state").mkdir()
    config = data_root / "config" / "default_profile.json"
    state = data_root / "state" / "runtime_state.json"
    config.write_text('{"value": "before"}\n', encoding="utf-8")
    state.write_text('{"score": 1}\n', encoding="utf-8")

    snapshot = RecoverySnapshot(data_root)
    snapshot.capture()
    assert snapshot.path.stat().st_mode & 0o077 == 0
    with pytest.raises(RuntimeError):
        snapshot.discard()

    config.write_text('{"value": "after"}\n', encoding="utf-8")
    state.write_text('{"score": 99}\n', encoding="utf-8")
    snapshot.restore()
    assert '"before"' in config.read_text(encoding="utf-8")
    assert '"score": 1' in state.read_text(encoding="utf-8")
    recovery_path = snapshot.path
    snapshot.discard()
    assert not recovery_path.exists()


def test_privacy_safe_tracking_fixture_scenarios_are_deterministic() -> None:
    assert SINGLE_PERSON.is_file()
    assert TWO_PERSON.is_file()
    for scenario in ("single-person", "multi-person", "crossing", "dropout", "rapid-motion"):
        frames = list(scenario_frames(scenario, 9, width=320, height=180))
        assert len(frames) == 9
        if scenario == "dropout":
            assert sum(frame is None for frame in frames) == 3
        else:
            assert all(frame is not None and frame.width() == 320 and frame.height() == 180 for frame in frames)


def test_graceful_quit_tolerates_transient_apple_event_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    running = iter((True, True, False))
    commands: list[tuple[str, bool]] = []

    monkeypatch.setattr(ApplicationLifecycle, "is_running", classmethod(lambda cls, name: next(running)))

    def fake_osascript(cls: type[ApplicationLifecycle], script: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        commands.append((script, check))
        return subprocess.CompletedProcess(["osascript"], 1, "", "connection invalid")

    monkeypatch.setattr(ApplicationLifecycle, "_osascript", classmethod(fake_osascript))

    ApplicationLifecycle.quit_gracefully("Production Hub", 1, retry_interval=0)

    assert len(commands) == 1
    assert 'keystroke "q" using command down' in commands[0][0]
    assert commands[0][1] is False


@pytest.mark.parametrize("mode", ["full", "soak"])
@pytest.mark.parametrize("skip_flag", ["--skip-browser", "--skip-swift"])
def test_live_runner_cannot_bypass_required_quick_phases(mode: str, skip_flag: str) -> None:
    result = subprocess.run(
        [sys.executable, "testing/run_end_to_end.py", "--mode", mode, skip_flag, "--yes"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "cannot skip browser or Swift validation" in result.stderr


def test_zero_duration_soak_still_disarms_tracking_and_clears_audio(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_http(url: str, token: str = "", *, method: str = "GET", payload=None):
        calls.append((url, method))
        return {"ok": True}

    monkeypatch.setattr("testing.run_end_to_end._http_json", fake_http)
    config = SimpleNamespace(
        api=SimpleNamespace(base_url="http://127.0.0.1:4000", access_token="test"),
        remote_pages=[],
    )
    report = RunReport(mode="soak", output_dir=tmp_path)

    assert run_soak_phase(report, config, 0)
    assert ("http://127.0.0.1:4000/api/camera/automation/configure", "POST") in calls
    assert ("http://127.0.0.1:4000/api/camera/automation/disarm", "POST") in calls
    assert ("http://127.0.0.1:4000/audio/clear", "POST") in calls
    assert (tmp_path / "soak.json").is_file()
