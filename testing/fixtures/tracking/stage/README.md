# ICAS stage tracking acceptance suite

This corpus covers **March 20–September 20, 2026**, the six-month window at the
original request. It is frozen for reproducible comparisons, not a rolling feed.
Seven short excerpts (165 seconds) were selected after surveying the channel's
stream and video catalogs and contact sheets. This is a representative sample,
not a claim that every service was watched. Anonymous identities are local to a
clip; no face identification is performed.

## What is actually tested

| Lane | Input | Implementation | Meaning of a pass |
|---|---|---|---|
| `pixels` | 41 checked-in JPEG frames | Apple Vision person detector | Required people have acceptable boxes at labelled instants |
| `video` | Seven checksum-pinned clips, replayed at 1×/2×/4× | Apple Vision + production `SubjectAssociator` | Sampled detection and identity checks pass; this is **not** camera pursuit |
| `motion` | 246 deterministic stage-inspired and footage-guided scenarios | Real `PtzAutomationService`, framing, target lock and velocity controller; simulated cameras | Meets framing/retention/settling requirements under the explicitly stated model |

`--backend installed --counts 1` loads `production_hub` bytecode directly from
the installed application using PyInstaller. It refuses to fall back to Dev
source, records the executable and loaded-module hashes, and does **not** launch
the app, change profiles, or contact a camera. The installed version is tested
only on single-person cases, as requested. `--backend source` runs this
worktree's source with its Dev experiment flags enabled. It does not imply that
another running Dev checkout has the same code.

The motion matrix covers one, two and three people, both horizontal directions,
0.5/1.5/3/5 body-widths per second, stop, reversal, duck, and group separation /
regrouping. Six profiles isolate ideal feedback, video delay, motor delay,
combined delay, 500 ms detection loss in both views, and a transient oversized
PTZ box. Solo synthetic walks start at zoom 3450, the telephoto range implicated in the
previous live failure; group tests start framed for the group. Six extra cases
per subject count project sparse, manually labelled head paths from the August
two-person shot and May skit into the simulator at 1×, 4× and 16× playback,
with ideal and combined-delay profiles. The solo path follows just the moving
lectern speaker from the August shot. These source-guided paths provide a
realistic move, stop, and kneel shape, while the high speeds are explicit stress
variants. Background stabilization is approximate and the reference footprint
remains an assumption; these paths are not physical measurements.

The motor model assumes full-scale pan/tilt/zoom rates of 2600/1200/900 encoder
units per second, a 150 ms command delay and 120 ms response constant in delayed
profiles, and 250 ms observation latency. These are sensitivity assumptions,
**not measured properties of the installed camera**. They must be calibrated
against a real recording before simulated failures can be attributed to that
camera. No test changes these assumptions to make the controller pass.

The original instantaneous simulator remains unchanged by default. Its new
optional camera/observation hooks allow these effects without changing existing
golden recordings. Each delayed image retains its capture timestamp. Camera
movement changes the next simulated image; prerecorded broadcast boxes are
never misrepresented as closed-loop feedback.

## Sources and independent labels

| Clip | Source timestamp | Required targets / challenge |
|---|---|---|
| `march-single` | [March 22, 07:20](https://www.youtube.com/watch?v=Imk30InrO14&t=440) | One speaker; lectern and small movement |
| `march-triple` | [March 22, 13:20](https://www.youtube.com/watch?v=Imk30InrO14&t=800) | Three singers; stands, overlap, background drummer |
| `june-dual` | [June 7 upload, 17:45](https://www.youtube.com/watch?v=ZnWoQFmCkVc&t=1065) | Two speakers; lectern and gestures |
| `august-dual-walk` | [August 31 upload, 23:25](https://www.youtube.com/watch?v=ORtkVnGtEyg&t=1405) | Two speakers; side-step and raised arm |
| `september-single` | [September 13, 1:37:10](https://www.youtube.com/watch?v=bvuQFg7ESPM&t=5830) | One speaker; shift, graphics and broadcast cut |
| `july-overlap` | [July 26, 05:45](https://www.youtube.com/watch?v=SmaUPOqVrVw&t=345) | Three overlapping people; partial heads and lectern |
| `may-triple` | [May 17, 07:05](https://www.youtube.com/watch?v=_kv4L9wop4Y&t=425) | Three actors; entry, kneeling, costumes, low light |

`manifest.json` records dates, exact excerpt boundaries, media/image hashes,
head anchors, approximate visible-body envelopes, clip-local identities and
selection notes. Labels were made by visual inspection independently of detector
outputs. They are deliberately sparse and approximate, not segmentation masks.
Only labelled instants have scored ground truth. Identity switches between
labelled instants can be missed. Background people are distractors; these tests
do not penalize valid detections of them or yet verify foreground selection.

The first May frame is not labelled as three people because the entering actor
is not yet visible. The September broadcast edit changes `shot`, so it does not
count as an identity switch. Operator pan/zoom, lower thirds and picture-in-picture
are present in these published videos; raw NDI quality and physical world speed
cannot be inferred from them. Accelerated playback increases temporal sampling
difficulty, but does not create the motion blur of a genuinely faster walker.
Fast sustained pursuit is therefore tested separately in the motion lane.

## Running

Use Python 3.12, the application's test environment, and macOS for Apple Vision.
The frozen installed app must use the same Python bytecode version. Install the
usual application/test requirements plus `testing/stage_requirements.txt`.
Node.js is also needed by yt-dlp for retrieving media. Apple Vision needs normal
macOS hardware access; a sandbox denial is a preflight failure, never a skip.

```sh
# Images are already checked in. Long video and generated reports stay ignored.
testing/.venv/bin/python testing/fetch_stage_corpus.py --verify-only
# If clips are absent, retrieve just the pinned excerpts:
testing/.venv/bin/python testing/fetch_stage_corpus.py

# Current production code, solo only; video + motion gates:
QT_QPA_PLATFORM=offscreen testing/.venv/bin/python testing/run_stage_tracking.py \
  --backend installed --counts 1 --lane all \
  --report testing/benchmarks/outputs/stage-corpus/installed.json

# Worktree Dev code, all subject counts:
QT_QPA_PLATFORM=offscreen testing/.venv/bin/python testing/run_stage_tracking.py \
  --backend source --counts 1,2,3 --lane all \
  --report testing/benchmarks/outputs/stage-corpus/source.json

# Fast harness validation, without Apple Vision or network access:
python3 testing/run_end_to_end.py --mode fast
```

Exit **0** means every requested case passed; **1** means behavioral failures;
**2** means preflight/execution failed. Missing clips, changed hashes, missing
results, incompatible installed modules, or unavailable Vision are errors.
There is no automatic baseline acceptance. Changed downloads remain `.candidate`
files for visual review and never silently replace expected media. Reproduction
uses the pinned yt-dlp/imageio-ffmpeg versions; a YouTube transcode or different
encoder output may require explicit corpus review rather than a checksum bypass.

Video gates require ≥95% recall, no merged/oversized boxes and no observed ID
switches within a shot. Matching is one-to-one, anchored to the labelled head,
with IoU ≥0.12, a 0.025-frame head tolerance and width ≤2× the visible-body label.
These tolerances accommodate upper-body/full-body variation and approximate
labels; they are not tuned against a passing controller output.

Motion gates require ≥98% subject retention, no loss longer than 500 ms,
settled pan/tilt after an eight-second hold, no continuing zoom hunting, and
adequate final subject scale. Visibility comes from independent world truth,
not claimed detector rectangles. A camera cannot pass by reporting perfect boxes
or remaining permanently wide. The fast suite includes oracle tests for those
counterexamples, delay timing, stale timestamps, malformed fixtures and positive
stationary controls.

## How to use failures

Run `all` before declaring tracking ready; passing the standard fast suite alone
only validates the test machinery and existing contracts. Keep the JSON reports,
exact implementation hashes and scenario names with the tracking change. Fix
tracking in its own workstream, then rerun this unchanged suite. Do not rewrite
annotations or relax thresholds to conceal failures.

The remaining hardware validation is to record a staged solo fast walk, stop,
reversal and duck, capturing source video, detection timestamps, PTZ commands and
encoder feedback together. Fit the motor/latency model from that recording and
repeat separation/regrouping with two and three people when available. Follow
`testing/README.md` recovery requirements. This offline suite does not claim that
those physical checks have happened or authorize a production release.
