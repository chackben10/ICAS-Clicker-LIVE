# PTZ stage-realism suite

Tests for Subject Tracking built around the ICAS Seattle stage, our camera
(Panasonic AW-HE120), and moments from our own services, instead of an
idealized sanctuary. The suite is **not** in CI: `pytest.ini` only collects
`testing/tests`.

## Builds and statuses

* `installed`: the live app, `/Applications/Production Hub.app`. The suite
  unpacks and runs the exact bytecode that ran on Sunday.
* `dev`: this checkout, which is what `/Applications/Production Hub Dev.app`
  launches, uncommitted work included. The suite switches on the same
  experiments the Dev app enables at launch (motion easing, tracking
  refinements, safe-zone tracking, validated boxes; see `app/main.py`), so
  results reflect what the Dev app actually runs.

`fixtures/live_baseline.json` records how the live app scores. Every test is
then one of:

| Status | Meaning |
|---|---|
| **challenge** | The live app fails it. This is what the Dev build is working toward. |
| **guard** | The live app passes it. No build may regress it. |
| **FIXED** / **REGRESSION** | How a Dev run compares against the live baseline. |

```sh
# score the Dev build against the live baseline (scorecard + MP4s of the rendered runs)
testing/.venv/bin/python -m testing.stage_realism.run --target dev --video
# re-record the live baseline after installing a new live build
testing/.venv/bin/python -m testing.stage_realism.run --target installed --write-baseline
# pytest: guards first, then the challenges
STAGE_REALISM_TARGET=dev testing/.venv/bin/python -m pytest testing/stage_realism -m guard
STAGE_REALISM_TARGET=dev testing/.venv/bin/python -m pytest testing/stage_realism -m "challenge and single"
```

Useful filters: `--mode model|rendered|replay`, `--group single|double|triple`,
`-k <name>`, `--variant measured_motor`. To try other rates than the ICAS
profile's 4 per second: `--analysis-fps 8` (the app's *Analysis rate* setting,
per camera) and `--decision-fps 8` (PTZ tracking decisions; the Dev build
makes one decision per analysis, so `--analysis-fps` alone sets both). Output goes to
`testing/benchmarks/outputs/stage-realism/` (git-ignored).

## Four kinds of test

1. **Stream moments** (`replay.py`, `--mode replay`): real frames from our
   streams, replayed through the build's own detector, association, and
   identity lock at the live analysis rate. They cover the exact moments the
   operator had to take the camera back on 2026-09-13 and 2026-09-20. A
   recording cannot respond to the camera, so this mode judges what the tracker
   sees and decides: does it stay on the person it was armed on, does it keep
   them, and does it ask to pan toward them.
2. **Rendered** (`render.py`, `--mode rendered`): closed loop on real pixels.
   The background is our stage, built from the calibration sweep's own PTZ
   photos. The subjects are real people cut out of our streams: the Sep 13
   preacher, youth readers, a duet, look-alike pairs. Each frame the simulated
   PTZ sees goes through the production Apple Vision detector; the tracker
   moves the simulated camera; the next frame is rendered from the new pose.
   Subjects can move far faster than on the recordings (dashes at 2.5 m/s,
   sprints at 3.5 m/s) and stop dead. The lectern, communion table, and floor
   monitors hide anyone standing behind them.
3. **Model** (`--mode model`): the same closed loop with a statistical detector
   tuned to real detector output. About 100x faster; good for sweeping speeds.
4. **Detector checks** (`checks.py`): the PTZ pans across the *empty* stage
   at sermon zoom (39 views of our calibration photos). Any "person" found is
   stage hardware the tracker could lock onto. The live app sees the
   electronic kick-drum tower as a person in 9 of 39 views.

Every closed-loop run also drives an **oracle camera**: exact positions, no
sensing delay, same motors and speed caps. The scorecard's *Feasible* column
says whether a perfect camera succeeds at the live speed caps and at full motor
speed. `no / yes` means the live caps alone make the scenario impossible.
Every scenario is feasible at full speed. Each scenario also runs with a 15%
faster motor (the measurement's uncertainty between and beyond the measured
zooms), so a failure in both variants is not an artifact of the motor model.

## Rules (set by the operator)

* **Head in frame**: no part of a tracked head leaves the PTZ frame.
* **Keeps up**: a subject never sits in the outer 20% of the frame, or outside it, for more than 1 s.
* **Settles**: no hunting or overshoot after a stop, and no creeping 3 s later.
* **Right person**: the armed person is followed the whole time, including walking off stage. The camera never moves to anyone else on its own; the operator re-arms to change subjects.
* **Stays armed**: tracking never turns itself off.

## Watchability (informational)

Passing the rules keeps the person in shot; these say how it looks. They are
reported for every run (details section of the scorecard, `watchability` in
`results.json`) but never fail a scenario.

| Metric | Meaning |
|---|---|
| `lurches_per_min` | The picture speeding up or slowing down by 0.2+ frame widths/s within 0.25 s, from the camera's actual view (so it includes the motor's response). The one to watch for jerky motion |
| `jolts_per_min` | Speed-command changes of 10+ steps within 0.25 s. A coarse proxy: AW speed steps are very uneven (at sermon zoom 30->44 moves the picture ~7x more than 0->14) |
| `zoom_changes_per_min` | Zoom direction changes: breathing |
| `idle_pan_tilt_pct` | Share of the time everyone tracked stands still that the camera pans or tilts anyway: creep (`idle_motion_pct` also counts zoom) |
| `walk_headroom_sd` | Spread of the head's height in the frame while walking; `headroom_sd` while standing |
| `reversals_per_min`, `offcentre_mean` | Visible pan direction changes; how far off centre a standing subject sits |

The live app scores near zero on lurches and breathing mostly because it does
not follow: its heads-in-frame on the walks is 37-48%. Compare builds that both
keep people in shot.

## Where the numbers come from

| Piece | Source |
|---|---|
| Stage geometry, calibration, live settings | Approved sweep and `default_profile.json` (`fixtures/icas_stage.json`, rebuilt by `tools/build_stage_fixture.py`) |
| Motor speed | **Measured** on the AW-HE120 (`fixtures/icas_motor_measured.json`, `tools/measure_motor_speed.py`, needs no light). Speed-with-zoom is on: speed 26 is ~2800 pan units/s at zoom 2238 but ~390 at sermon zoom 3500; full speed at 3500 is ~1860 |
| Optics at sermon zoom | Measured from the 2026-09-20 recording: the view is ~0.13 of the room width at zoom 3642. The calibration model says 0.23, because it never measured beyond zoom 2238 |
| Walking speeds | 1.0 m/s measured from a real walk-off. 1.3, 1.7, 2.2, 2.5, and 3.5 m/s are stress levels |
| Latency | Camera command **measured** at 0.15 s (motion starts 0.14-0.16 s after the command). Video (0.10 s) still assumed. Apple Vision takes ~11 ms per frame on the Apple M4 this app runs on (PTZ at 960 px and the full audience frame alike), so the 0.06 s inference figure is conservative |

`fixtures/icas_footage_measurements.json` records the source and uncertainty of
every hand-measured number. Change a value only with a new measurement.

## Local assets (git-ignored)

Rendered tests and stream moments need `testing/stage_realism/assets/`. It
holds real people from our streams, some of them minors, so it stays on the
production Mac. Rebuild it from scratch with:

```sh
testing/.venv/bin/python testing/stage_realism/tools/build_stage_plate.py
testing/.venv/bin/python testing/stage_realism/tools/extract_person_sprites.py --clip <excerpt.mp4> --video <id> --start <s>
PYTHONPATH=Production_Hub_App testing/.venv/bin/python testing/stage_realism/tools/qa_sprites.py
testing/.venv/bin/python testing/stage_realism/tools/annotate_stream_moment.py --video <id> --start <s> --end <s> --name <name> ...
```

The committed fixtures (`fixtures/moments/*.json`, `fixtures/tracks/*.json`)
hold only normalized boxes, camera motion, and provenance. Every moment's
annotation was checked by eye on its overlay sheet: a colour histogram cannot
reliably tell two people in white headscarves apart. See `--target-until` and
`--also-target`.

## Limitations

* Everything here is a model of the room. A pass is necessary but not sufficient; confirm on the real camera before a service.
* Stream moments are open loop and have no wide-camera footage, so the wide-camera backup sees an empty frame.
* The rendered wide camera shows the empty church with the subjects; no congregation cut-outs yet.
* Cut-outs from the stream stop where the PTZ framing cut them (often mid-thigh). They are scaled from body pose and faded at the cut edge.
