# Production Hub agent requirements

These instructions apply to every change in this repository.

## Tests are part of every change

- Every feature, behavior change, or bug fix must add or update automated tests.
- Update `testing/feature_matrix.json` whenever an endpoint, automation, action type,
  integration, hosted page, or actionable UI control is added or changed.
- Run `python3 testing/run_end_to_end.py --mode fast` before handing work back. It is
  the Python unit, contract, and integration suites and finishes in under a minute.
- Run `python3 testing/run_end_to_end.py --mode quick` once before a release, and
  whenever a change touches a hosted page, a `data-testid`, or the Swift NDI app.
  Quick adds two 213-scenario browser passes and a Swift build; it costs about five
  minutes and must not be the gate on every handback.
- Run the relevant live scenarios when a change affects ProPresenter, OBS, MIDI,
  VISCA, Panasonic PTZ, NDI/video, tracking, or macOS application lifecycle.
- A full live run must not skip an enabled capability. Missing hardware or
  permissions are a failed preflight, not a passing test.
- Do not replace, weaken, or accept a benchmark baseline merely to hide a
  regression. Baseline changes require an explicit `--accept-baseline` run and
  a written explanation in the change summary.

## Staging before production

- `Production Hub Dev` is the staging application. Run it with
  `python3 Production_Hub_App/main.py`; it loads the live source tree under its
  own bundle identity (`org.icas.productionhub.dev`) and its own privacy grants.
- Exercise every change there, against the real cameras, ProPresenter, and NDI
  sources, before it goes anywhere near the installed release.
- The installed `/Applications/Production Hub.app` is production. Do not
  rebuild or reinstall it to try something out; that is what staging is for.
  Promote only after the quick suite passes and the change is ready for a
  service, and never mid-service.
- Do not remove or bypass the development launcher
  (`production_hub/app/dev_launcher.py`). Losing it means testing against the
  release bundle, which costs a full build per edit and puts the release's
  permissions at risk.

## Performance work is measured, not asserted

- The number that matters for cue responsiveness is press-to-cue: the CGEventTap
  timestamp of a physical clicker press to the ProPresenter status-stream push that
  reports the resulting slide. Hub-to-ProPresenter HTTP latency is not that number
  and must not be reported as it.
- Never add a poller or an Accessibility observer that reads ProPresenter's UI.
  Those are synchronous cross-process calls served by the same main thread that
  renders cues, so they add load to the thread they are meant to help.
- A performance fix is accepted only against a press-to-cue distribution captured
  under realistic load, alongside ProPresenter's resident size. A green test suite
  is not evidence that a service feels better.

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

