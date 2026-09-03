# Production Hub testing

This directory is the single entry point for Production Hub regression,
integration, UI, hardware, and performance testing.

## Install test dependencies

From the repository root:

```bash
python3 -m venv testing/.venv
testing/.venv/bin/python -m pip install -r Production_Hub_App/requirements.txt
testing/.venv/bin/python -m pip install -r testing/requirements.txt
testing/.venv/bin/python -m playwright install chromium webkit
```

The official NDI Runtime 6 and a compatible Xcode toolchain remain system
dependencies. The runner reports a missing or mismatched Swift toolchain as a
failure; a successful fallback build is not counted as a test pass.

## Run modes

```bash
python3 testing/run_end_to_end.py --mode quick
python3 testing/run_end_to_end.py --mode full
python3 testing/run_end_to_end.py --mode soak
```

- `quick` is production-safe. It runs Python unit and contract tests, simulated
  integrations, offscreen Qt tests, safe browser tests, Swift tests, and stable
  local benchmarks.
- `full` includes `quick`, then performs live preflight, restarts ProPresenter
  and launches the current Production Hub source through the stable `Production
  Hub Dev.app` identity, exercises configured integrations, and restores
  captured state. It moves the PTZ camera. Grant Accessibility, Input
  Monitoring, Camera, and local-network access to that development identity
  once; source edits do not rebuild it or invalidate those grants.
- `soak` includes `full` and then samples the live system for 60 minutes by
  default. It repeatedly polls every hosted page and representative API,
  samples CPU/memory/video health, sends MIDI and deterministic NDI scenarios,
  exercises reconnect paths, and leaves tracking disarmed and audio clear.
  Override the duration only for diagnosis with `--soak-minutes`.

Do not start `full` or `soak` while ProPresenter or the production system is in
active use. The runner asks for an explicit confirmation unless `--yes` is
provided. It never force-quits ProPresenter.

## Baselines and outputs

Tracked baselines live in `testing/benchmarks/baselines/`. A normal run never
changes them. Promote a reviewed result explicitly:

```bash
python3 testing/run_end_to_end.py --accept-baseline deterministic
python3 testing/run_end_to_end.py --accept-baseline production-mac
```

Deterministic p95 regressions fail when they exceed both 15 percent and 5 ms.
Live p95 regressions warn when they exceed both 25 percent and 100 ms; live
functional failures still fail. Reports, screenshots, traces, redacted logs,
and recovery status are written beneath `testing/benchmarks/outputs/`.

## Coverage contract

`testing/feature_matrix.json` maps public capabilities to pytest node IDs. The
inventory test compares it with the runtime endpoint, automation, action, page,
and integration catalogs. Adding a capability without a test mapping fails the
quick suite.

Web controls use `data-testid`. Qt controls use `objectName` and accessible
names. New actionable controls must be identifiable and mapped to a scenario.

## Live test safety

Live tests use the current Production Hub profile. Before any mutation the
runner creates a mode-0700 recovery directory outside the repository and copies
configuration and durable state into it. Production Hub is stopped before file
restoration. A successful restore removes the recovery directory; a failed
restore leaves it in place and reports its path without copying secrets into
the normal artifacts.

The live camera suite may move, zoom, focus, recall Home, and arm/disarm
tracking. Power, standby, preset save, and preset rename are simulated only.
Preflight captures a current Audience NDI frame and verifies usable light,
visible detail, and a lock against the approved calibration reference before
any application is stopped or production state is captured. A dark or changed
room fails safely before mutation rather than skipping camera coverage.
