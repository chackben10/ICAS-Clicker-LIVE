#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
import traceback
import urllib.request
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VENV_ROOT = REPOSITORY_ROOT / "testing" / ".venv"
VENV_PYTHON = VENV_ROOT / "bin" / "python"
if (
    VENV_PYTHON.is_file()
    and Path(sys.prefix).resolve() != VENV_ROOT.resolve()
    and os.environ.get("PRODUCTION_HUB_TEST_REEXEC") != "1"
):
    reexec_environment = os.environ.copy()
    reexec_environment["PRODUCTION_HUB_TEST_REEXEC"] = "1"
    os.execve(
        str(VENV_PYTHON),
        [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
        reexec_environment,
    )
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
APP_SOURCE = REPOSITORY_ROOT / "Production_Hub_App"
if str(APP_SOURCE) not in sys.path:
    sys.path.insert(0, str(APP_SOURCE))

from testing.harness.benchmarks import accept_baseline  # noqa: E402
from testing.harness.live_system import (  # noqa: E402
    ApplicationLifecycle,
    RecoverySnapshot,
    capture_external_state,
    live_preflight,
    restore_external_state,
    sanitized_checks,
)
from production_hub.app.dev_launcher import DEV_APP_NAME  # noqa: E402
from testing.harness.paths import OUTPUT_ROOT, VIDEO_TO_NDI_ROOT  # noqa: E402
from testing.harness.performance import run_deterministic_benchmarks  # noqa: E402
from testing.harness.reporting import PhaseResult, RunReport, redact  # noqa: E402


def command_phase(
    report: RunReport,
    name: str,
    command: list[str],
    *,
    cwd: Path = REPOSITORY_ROOT,
    environment: dict[str, str] | None = None,
) -> bool:
    started = time.perf_counter()
    env = os.environ.copy()
    env.update(environment or {})
    log_path = report.output_dir / f"{name}.log"
    print(f"\n[{name}] {' '.join(command)}", flush=True)
    try:
        result = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, check=False)
        combined = "".join((result.stdout, result.stderr))
        safe_output = str(redact(combined))
        log_path.write_text(safe_output, encoding="utf-8")
        if safe_output:
            print(safe_output[-12000:], end="" if safe_output.endswith("\n") else "\n", flush=True)
        ok = result.returncode == 0
        report.add(
            PhaseResult(
                name,
                ok,
                result.returncode,
                time.perf_counter() - started,
                {"command": command, "log": log_path.name},
            )
        )
        return ok
    except Exception as exc:
        log_path.write_text(str(redact(traceback.format_exc())), encoding="utf-8")
        report.add(
            PhaseResult(
                name,
                False,
                1,
                time.perf_counter() - started,
                {"command": command, "error": str(exc), "log": log_path.name},
            )
        )
        return False


def pytest_command(report: RunReport, name: str, paths: list[str], extra: list[str] | None = None) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        *paths,
        "-ra",
        "--strict-markers",
        f"--junitxml={report.output_dir / f'{name}.junit.xml'}",
        f"--html={report.output_dir / f'{name}.html'}",
        "--self-contained-html",
        "--json-report",
        f"--json-report-file={report.output_dir / f'{name}.json'}",
        *(extra or []),
    ]


def dependency_preflight(report: RunReport, *, browser: bool) -> bool:
    modules = ["pytest", "pytest_html", "pytest_jsonreport", "PySide6", "fastapi"]
    if browser:
        modules.extend(["playwright", "pytest_playwright"])
    missing: list[str] = []
    for module in modules:
        try:
            __import__(module)
        except Exception as exc:
            missing.append(f"{module}: {exc}")
    ok = not missing
    report.add(
        PhaseResult(
            "dependency-preflight",
            ok,
            0 if ok else 2,
            details={
                "python": sys.executable,
                "missing": missing,
                "install": "testing/.venv/bin/python -m pip install -r testing/requirements.txt",
            },
        )
    )
    if missing:
        print("Missing test dependencies:\n  " + "\n  ".join(missing), file=sys.stderr)
    return ok


FAST_PATHS = (
    "testing/tests/unit",
    "testing/tests/contracts",
    "testing/tests/integration",
)


def run_fast(report: RunReport) -> bool:
    """Iteration lane: the Python suites that actually gate correctness.

    The full quick matrix adds two 213-scenario browser passes and a Swift
    build.  Those belong once before a release, not between two edits of the
    same function -- a multi-minute gate on every handback is why single-day
    work stretched across days.
    """

    if not dependency_preflight(report, browser=False):
        return False
    return command_phase(
        report,
        "python-fast",
        pytest_command(report, "python-fast", list(FAST_PATHS)),
        environment={"QT_QPA_PLATFORM": "offscreen", "PRODUCTION_HUB_LIVE": "0"},
    )


def run_quick(report: RunReport, *, skip_browser: bool = False, skip_swift: bool = False) -> bool:
    if not dependency_preflight(report, browser=not skip_browser):
        return False
    core_paths = [
        "testing/tests/unit",
        "testing/tests/contracts",
        "testing/tests/integration",
        "testing/tests/desktop",
        "testing/tests/performance",
    ]
    core_ok = command_phase(
        report,
        "python-quick",
        pytest_command(report, "python-quick", core_paths),
        environment={"QT_QPA_PLATFORM": "offscreen", "PRODUCTION_HUB_LIVE": "0"},
    )
    browser_ok = True
    if not skip_browser:
        for engine in ("chromium", "webkit"):
            name = f"browser-{engine}"
            browser_ok = command_phase(
                report,
                name,
                pytest_command(
                    report,
                    name,
                    ["testing/tests/browser"],
                    ["--browser", engine, "--tracing", "retain-on-failure"],
                ),
                environment={
                    "PRODUCTION_HUB_LIVE": "0",
                    "PRODUCTION_HUB_DEPLOYED_SMOKE": "1",
                    "PRODUCTION_HUB_BROWSER_OUTPUT": str(report.output_dir / "screenshots" / engine),
                    "PRODUCTION_HUB_BROWSER_METRICS": str(report.output_dir / f"browser-{engine}-metrics.json"),
                },
            ) and browser_ok
    swift_ok = True
    if not skip_swift:
        swift_ok = command_phase(
            report,
            "swift",
            [str(VIDEO_TO_NDI_ROOT / "scripts" / "test.sh"), "--skip-live-ndi"],
            cwd=VIDEO_TO_NDI_ROOT,
        )
    benchmark_started = time.perf_counter()
    benchmark_path = report.output_dir / "deterministic.json"
    try:
        browser_metrics: list[dict[str, Any]] = []
        for engine in ("chromium", "webkit"):
            path = report.output_dir / f"browser-{engine}-metrics.json"
            if path.is_file():
                browser_metrics.extend(json.loads(path.read_text(encoding="utf-8")).get("metrics", []))
        payload, benchmark_ok = run_deterministic_benchmarks(
            benchmark_path,
            extra_metrics=browser_metrics,
        )
        report.add(
            PhaseResult(
                "deterministic-benchmarks",
                benchmark_ok,
                0 if benchmark_ok else 1,
                time.perf_counter() - benchmark_started,
                {"output": benchmark_path.name, "comparisons": payload["comparisons"]},
            )
        )
    except Exception as exc:
        benchmark_ok = False
        report.add(
            PhaseResult(
                "deterministic-benchmarks",
                False,
                1,
                time.perf_counter() - benchmark_started,
                {"error": str(exc)},
            )
        )
    return core_ok and browser_ok and swift_ok and benchmark_ok


def _http_json(url: str, token: str = "", *, method: str = "GET", payload: dict | None = None) -> Any:
    headers = {"X-Production-Hub-Token": token} if token else {}
    data = json.dumps(payload).encode() if payload is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, headers=headers, method=method, data=data)
    with urllib.request.urlopen(request, timeout=5) as response:
        content_type = response.headers.get("content-type", "")
        payload = response.read()
        return json.loads(payload) if "json" in content_type else payload.decode(errors="replace")


def run_soak_phase(report: RunReport, config, minutes: float) -> bool:
    import psutil

    from testing.harness.ndi_fixture_sender import NDISyntheticSender, stream_scenario
    from production_hub.integrations.propresenter.audio_service import normalize_pad_key
    import mido

    base_url = config.api.base_url
    token = config.api.access_token
    started = time.perf_counter()
    deadline = time.monotonic() + minutes * 60
    samples = 0
    errors: list[str] = []
    latencies: list[float] = []
    cpu_samples: list[float] = []
    rss_samples: list[float] = []
    frame_ages: list[float] = []
    inference_samples: list[float] = []
    analysis_fps_samples: list[float] = []
    midi_latencies: list[float] = []
    paths = [
        "/health",
        "/active-presentation",
        "/slide-index",
        "/score",
        "/api/camera/tracking",
        "/admin/automations",
        *[f"/remote/{page.path}" for page in config.remote_pages],
    ]
    path_latencies: dict[str, list[float]] = {path: [] for path in paths}
    try:
        processes = [
            process
            for process in psutil.process_iter(["name", "cmdline", "create_time"])
            if "production hub"
            in " ".join([process.info.get("name") or "", *(process.info.get("cmdline") or [])]).casefold()
        ]
    except (psutil.Error, PermissionError):
        processes = []
    hub_process = max(processes, key=lambda process: process.info.get("create_time") or 0) if processes else None
    if hub_process:
        hub_process.cpu_percent(None)
    cycle = 0
    next_ndi_at = time.monotonic()
    next_midi_at = time.monotonic()
    print(f"Starting {minutes:g}-minute live soak", flush=True)
    try:
        _http_json(
            f"{base_url.rstrip('/')}/api/camera/automation/configure",
            token,
            method="POST",
            payload={"mode": "subject"},
        )
        while time.monotonic() < deadline:
            for path in paths:
                path_latencies.setdefault(path, [])
                tick = time.perf_counter()
                try:
                    payload = _http_json(f"{base_url.rstrip('/')}{path}", token)
                    elapsed_ms = (time.perf_counter() - tick) * 1000
                    latencies.append(elapsed_ms)
                    path_latencies[path].append(elapsed_ms)
                    if path == "/api/camera/tracking" and isinstance(payload, dict):
                        for snapshot in payload.values():
                            if not isinstance(snapshot, dict):
                                continue
                            if snapshot.get("analysisAgeSeconds") is not None:
                                frame_ages.append(float(snapshot["analysisAgeSeconds"]) * 1000)
                            if snapshot.get("inferenceMs") is not None:
                                inference_samples.append(float(snapshot["inferenceMs"]))
                            if snapshot.get("analysisFps") is not None:
                                analysis_fps_samples.append(float(snapshot["analysisFps"]))
                except Exception as exc:
                    errors.append(f"{path}: {exc}")
                samples += 1
            if cycle % 30 == 0:
                try:
                    _http_json(f"{base_url.rstrip('/')}/obs/look/refresh", token)
                    _http_json(f"{base_url.rstrip('/')}/audio/clear", token, method="POST", payload={})
                    _http_json(
                        f"{base_url.rstrip('/')}/admin/automations/resume",
                        token,
                        method="POST",
                        payload={},
                    )
                except Exception as exc:
                    errors.append(f"representative action cycle: {exc}")
            if time.monotonic() >= next_midi_at:
                try:
                    mapping = next(
                        item
                        for item in config.integrations.midi.mappings
                        if item.get("event_type") == "note_on" and item.get("actions")
                    )
                    action = mapping["actions"][0]
                    output_name = next(
                        name
                        for name in mido.get_output_names()
                        if config.integrations.midi.input_name.casefold() in name.casefold()
                        or name.casefold() in config.integrations.midi.input_name.casefold()
                    )
                    message = mido.Message(
                        "note_on",
                        channel=max(0, int(mapping["channel"])),
                        note=int(mapping["number"]),
                        velocity=100,
                    )
                    midi_started = time.perf_counter()
                    with mido.open_output(output_name) as port:
                        port.send(message)
                    expected = str(action.get("params", {}).get("track", ""))
                    midi_deadline = time.monotonic() + 8
                    while time.monotonic() < midi_deadline:
                        observed = str(_http_json(f"{base_url.rstrip('/')}/audio/active", token))
                        if normalize_pad_key(expected) == normalize_pad_key(observed.removeprefix("item:").strip()):
                            midi_latencies.append((time.perf_counter() - midi_started) * 1000)
                            break
                        time.sleep(0.1)
                    else:
                        raise RuntimeError(f"mapped audio effect was not observed for {expected}")
                    _http_json(f"{base_url.rstrip('/')}/audio/clear", token, method="POST", payload={})
                    next_midi_at = time.monotonic() + 300
                except Exception as exc:
                    errors.append(f"MIDI soak cycle: {exc}")
                    next_midi_at = time.monotonic() + 60
            if time.monotonic() >= next_ndi_at:
                try:
                    with NDISyntheticSender(output_dir=report.output_dir / ".ndi-soak") as sender:
                        stream_scenario(sender, "rapid-motion", 1.5, 20)
                        stream_scenario(sender, "dropout", 1.5, 20)
                    next_ndi_at = time.monotonic() + 300
                except Exception as exc:
                    errors.append(f"NDI soak fixture: {exc}")
                    next_ndi_at = time.monotonic() + 60
            if hub_process:
                try:
                    cpu_samples.append(hub_process.cpu_percent(None))
                    rss_samples.append(hub_process.memory_info().rss / (1024 * 1024))
                except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
                    errors.append(f"resource sampling: {exc}")
                    hub_process = None
            cycle += 1
            if cycle % 60 == 0:
                print(f"Soak progress: {(time.perf_counter() - started) / 60:.1f}/{minutes:g} minutes", flush=True)
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    finally:
        try:
            _http_json(
                f"{base_url.rstrip('/')}/api/camera/automation/disarm",
                token,
                method="POST",
                payload={},
            )
            _http_json(f"{base_url.rstrip('/')}/audio/clear", token, method="POST", payload={})
        except Exception as exc:
            errors.append(f"soak neutralization: {exc}")
    elapsed_minutes = max((time.perf_counter() - started) / 60, 1e-6)
    memory_slope = ((rss_samples[-1] - rss_samples[0]) / elapsed_minutes) if len(rss_samples) >= 2 else None
    details = {
        "minutes": minutes,
        "samples": samples,
        "errors": errors[-50:],
        "median_ms": sorted(latencies)[len(latencies) // 2] if latencies else None,
        "p95_ms": sorted(latencies)[max(0, round(len(latencies) * 0.95) - 1)] if latencies else None,
        "p99_ms": sorted(latencies)[max(0, round(len(latencies) * 0.99) - 1)] if latencies else None,
        "endpoint_p95_ms": {
            path: sorted(values)[max(0, round(len(values) * 0.95) - 1)]
            for path, values in path_latencies.items()
            if values
        },
        "cpu_median_percent": statistics.median(cpu_samples) if cpu_samples else None,
        "cpu_peak_percent": max(cpu_samples) if cpu_samples else None,
        "peak_memory_mb": max(rss_samples) if rss_samples else None,
        "memory_slope_mb_per_minute": memory_slope,
        "frame_age_p95_ms": sorted(frame_ages)[max(0, round(len(frame_ages) * 0.95) - 1)] if frame_ages else None,
        "tracking_inference_p95_ms": sorted(inference_samples)[max(0, round(len(inference_samples) * 0.95) - 1)] if inference_samples else None,
        "tracking_fps_median": statistics.median(analysis_fps_samples) if analysis_fps_samples else None,
        "midi_end_to_effect_p95_ms": sorted(midi_latencies)[max(0, round(len(midi_latencies) * 0.95) - 1)] if midi_latencies else None,
        "error_count": len(errors),
    }
    ok = not errors and (memory_slope is None or memory_slope <= 5.0)
    (report.output_dir / "soak.json").write_text(json.dumps(redact(details), indent=2) + "\n", encoding="utf-8")
    report.add(PhaseResult("soak", ok, 0 if ok else 1, time.perf_counter() - started, details))
    return ok


def run_full(report: RunReport, *, soak_minutes: float | None = None, assume_yes: bool = False) -> bool:
    if not assume_yes:
        if not sys.stdin.isatty():
            report.add(PhaseResult("live-confirmation", False, 2, details={"error": "Use --yes for an unattended live run"}))
            return False
        answer = input(
            "FULL LIVE TEST: Production Hub and ProPresenter will close, the PTZ camera will move, and configured devices will be exercised. Type RUN LIVE TESTS to continue: "
        )
        if answer.strip() != "RUN LIVE TESTS":
            report.add(PhaseResult("live-confirmation", False, 2, details={"error": "confirmation declined"}))
            return False
    report.add(PhaseResult("live-confirmation", True))

    config, checks = live_preflight()
    preflight_ok = all(item.ok for item in checks)
    report.add(PhaseResult("live-preflight", preflight_ok, 0 if preflight_ok else 2, details={"checks": sanitized_checks(checks)}))
    if not preflight_ok:
        return False

    lifecycle = ApplicationLifecycle()
    recovery = RecoverySnapshot()
    report.recovery = {"path": str(recovery.path), "captured": False, "restored": False, "discarded": False}
    report.write()
    external_state: dict[str, Any] = {}
    live_ok = False
    restoration_errors: list[str] = []
    test_hub_running = False
    try:
        recovery.capture()
        report.recovery["captured"] = True
        external_state = asyncio.run(capture_external_state(config))
        (recovery.path / "external-state.json").write_text(json.dumps(redact(external_state), indent=2) + "\n", encoding="utf-8")
        os.chmod(recovery.path / "external-state.json", 0o600)
        report.write()

        lifecycle.quit_gracefully("Production Hub", 30)
        lifecycle.quit_gracefully(DEV_APP_NAME, 30)
        lifecycle.quit_gracefully("ProPresenter", 45)
        lifecycle.open_application("ProPresenter")
        propresenter_ready = lifecycle.wait_http(f"{config.integrations.propresenter.base_url}/presentation/slide_index", 90)
        report.add(PhaseResult("propresenter-fresh-start", True, duration_seconds=propresenter_ready))

        launch_started = time.perf_counter()
        bundle = lifecycle.open_source_development_hub()
        report.add(
            PhaseResult(
                "production-hub-stable-development-launch",
                True,
                duration_seconds=time.perf_counter() - launch_started,
                details={"bundle": str(bundle), "loads_live_source": True},
            )
        )
        test_hub_running = True
        hub_ready = lifecycle.wait_http(f"{config.api.base_url}/health", 90)
        report.add(PhaseResult("production-hub-fresh-start", True, duration_seconds=hub_ready))

        live_ok = command_phase(
            report,
            "live",
            pytest_command(report, "live", ["testing/tests/live"]),
            environment={
                "PRODUCTION_HUB_LIVE": "1",
                "PRODUCTION_HUB_LIVE_OUTPUT": str(report.output_dir),
                "QT_QPA_PLATFORM": "cocoa",
                "PRODUCTION_HUB_PROPRESENTER_STARTUP_MS": str(propresenter_ready * 1000),
                "PRODUCTION_HUB_APP_STARTUP_MS": str(hub_ready * 1000),
            },
        )
        if soak_minutes is not None:
            live_ok = run_soak_phase(report, config, soak_minutes) and live_ok
    except Exception as exc:
        report.add(PhaseResult("live-lifecycle", False, 1, details={"error": str(exc), "traceback": traceback.format_exc()}))
    finally:
        if test_hub_running and ApplicationLifecycle.is_running(DEV_APP_NAME):
            try:
                restoration_errors.extend(asyncio.run(restore_external_state(config, external_state)))
            except Exception as exc:
                restoration_errors.append(f"external restoration: {exc}")
            try:
                lifecycle.quit_gracefully(DEV_APP_NAME, 30)
            except Exception as exc:
                restoration_errors.append(f"quit test Production Hub: {exc}")
        try:
            recovery.restore()
            report.recovery["restored"] = True
        except Exception as exc:
            restoration_errors.append(f"file restoration: {exc}")
        if recovery.restored and not restoration_errors:
            recovery.discard()
            report.recovery["discarded"] = True
        if lifecycle.initially_running[DEV_APP_NAME]:
            try:
                lifecycle.open_source_development_hub()
                lifecycle.wait_http(f"{config.api.base_url}/health", 90)
            except Exception as exc:
                restoration_errors.append(f"reopen source Production Hub: {exc}")
        elif lifecycle.initially_running["Production Hub"]:
            try:
                lifecycle.open_application("/Applications/Production Hub.app")
                lifecycle.wait_http(f"{config.api.base_url}/health", 90)
            except Exception as exc:
                restoration_errors.append(f"reopen installed Production Hub: {exc}")
        if not lifecycle.initially_running["ProPresenter"] and ApplicationLifecycle.is_running("ProPresenter"):
            try:
                lifecycle.quit_gracefully("ProPresenter", 45)
            except Exception as exc:
                restoration_errors.append(f"restore initial ProPresenter lifecycle: {exc}")
        elif lifecycle.initially_running["ProPresenter"] and not ApplicationLifecycle.is_running("ProPresenter"):
            try:
                lifecycle.open_application("ProPresenter")
                lifecycle.wait_http(f"{config.integrations.propresenter.base_url}/presentation/slide_index", 90)
            except Exception as exc:
                restoration_errors.append(f"reopen ProPresenter: {exc}")
        report.recovery["errors"] = restoration_errors
        report.write()
        report.add(
            PhaseResult(
                "restoration",
                not restoration_errors and recovery.restored,
                0 if not restoration_errors and recovery.restored else 1,
                details=report.recovery,
            )
        )
    return live_ok and not restoration_errors and recovery.restored


def latest_suite_result(suite: str) -> Path:
    candidates = sorted(OUTPUT_ROOT.glob(f"*/{suite}.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No {suite!r} result exists beneath {OUTPUT_ROOT}")
    return candidates[0]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Production Hub regression, live integration, and benchmark runner")
    parser.add_argument("--mode", choices=("fast", "quick", "full", "soak"))
    parser.add_argument("--accept-baseline", choices=("deterministic", "production-mac"))
    parser.add_argument("--result", type=Path, help="Result JSON to promote; defaults to the newest suite result")
    parser.add_argument("--soak-minutes", type=float, default=60.0)
    parser.add_argument("--yes", action="store_true", help="Confirm disruptive live testing without a prompt")
    parser.add_argument("--skip-browser", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--skip-swift", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if bool(args.mode) == bool(args.accept_baseline):
        parser.error("choose exactly one of --mode or --accept-baseline")
    if args.soak_minutes <= 0:
        parser.error("--soak-minutes must be positive")
    if args.mode in {"full", "soak"} and (args.skip_browser or args.skip_swift):
        parser.error("full and soak modes cannot skip browser or Swift validation")
    if args.mode == "fast" and (args.skip_browser or args.skip_swift):
        parser.error("fast mode already excludes browser and Swift validation")
    return args


def main() -> int:
    args = parse_args()
    if args.accept_baseline:
        source = args.result or latest_suite_result(args.accept_baseline)
        target = accept_baseline(args.accept_baseline, source)
        print(f"Accepted reviewed {args.accept_baseline} baseline from {source} into {target}")
        return 0

    report = RunReport.create(args.mode)
    print(f"Reports: {report.output_dir}", flush=True)
    if args.mode == "fast":
        overall_ok = run_fast(report)
        report.write()
        print(f"{'PASS' if overall_ok else 'FAIL'}: {report.output_dir}")
        return 0 if overall_ok else 1
    quick_ok = run_quick(report, skip_browser=args.skip_browser, skip_swift=args.skip_swift)
    overall_ok = quick_ok
    if quick_ok and args.mode in {"full", "soak"}:
        overall_ok = run_full(
            report,
            soak_minutes=args.soak_minutes if args.mode == "soak" else None,
            assume_yes=args.yes,
        )
    report.write()
    print(f"{'PASS' if overall_ok else 'FAIL'}: {report.output_dir}")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
