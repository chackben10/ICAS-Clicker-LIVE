# Production Hub agent requirements

These instructions apply to every change in this repository.

## Tests are part of every change

- Every feature, behavior change, or bug fix must add or update automated tests.
- Update `testing/feature_matrix.json` whenever an endpoint, automation, action type,
  integration, hosted page, or actionable UI control is added or changed.
- Run `python3 testing/run_end_to_end.py --mode quick` before handing work back.
- Run the relevant live scenarios when a change affects ProPresenter, OBS, MIDI,
  VISCA, Panasonic PTZ, NDI/video, tracking, or macOS application lifecycle.
- A full live run must not skip an enabled capability. Missing hardware or
  permissions are a failed preflight, not a passing test.
- Do not replace, weaken, or accept a benchmark baseline merely to hide a
  regression. Baseline changes require an explicit `--accept-baseline` run and
  a written explanation in the change summary.

## Safety and artifacts

- Read `testing/README.md` before changing test infrastructure or running live
  tests.
- Quick tests must never contact or mutate production services.
- Live tests must capture recoverable state before mutation and restore it even
  when a test fails.
- Reports and logs must redact credentials, tokens, authorization headers, and
  private recovery data.
- Generated reports belong under `testing/benchmarks/outputs/` and must not be
  committed.

