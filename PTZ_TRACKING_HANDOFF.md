# PTZ tracking: state of the work (handoff for Claude and Codex)

Last updated 2026-10-11 (evening) by Claude. Read this before touching PTZ tracking.

## Repository layout (decided by the user)

- Exactly two worktrees:
  - `main` at `/Users/icas/Github/ICAS-Clicker-LIVE`
  - `dev` at `/Users/icas/Github/ICAS-Clicker-LIVE-dev` (branch `dev`)
- All in-progress PTZ work goes on `dev` (or a branch off it) until the user
  merges it into main. Don't create more worktrees.
- `dev` = the former Codex worktree (`~/.codex/worktrees/41e7`), committed
  as `1b66138` on top of `06044ad`. main's `f76b88f` ("improved PTZ subject
  tracking") is a **separate, parallel** tracking line. A merge preview
  (`git merge-tree main dev`) shows 10 conflicts: tracking modules, README,
  feature matrix, `testing/harness/ptz_tracking.py`,
  `test_single_subject_easing.py`. Resolving that merge is the user's call.
- The new controller work is on branch **`dev-pursuit-wip`**, not on
  `dev`. It passes every test, but it hasn't been tried on the real camera
  yet. See "Work in progress" below.

## Running Production Hub Dev from `dev`

Launch it from Terminal, with the framework Python:

    cd /Users/icas/Github/ICAS-Clicker-LIVE-dev
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 Production_Hub_App/main.py

Or double-click `Launch Production Hub Dev.command`; it now prefers the
framework Python.

- Don't launch it from an agent's shell. macOS then charges the privacy
  permissions to the agent's app (Claude/Codex). The camera permission comes
  back "undetermined" and the PTZ local camera fails, hitting a separate
  crash: `local_camera_source.py:194` passes a lambda to
  `QCoreApplication.requestPermission`, which PySide6 rejects. That bug is
  also on main and not yet fixed.
- A bare `python3` lacks `httpx` and the other requirements.
- Quitting: the AppleScript `quit` is often ignored (the app hides to the
  tray). Agents used SIGTERM on the `--production-hub-dev-child` process,
  only after confirming `/api/camera/automation` showed `armed: false`.
- Audience Cam arrives over NDI as
  `ICASS-MAC-MINI-2.LOCAL (Production Hub - Audience Cam)`, published from
  the OBS Mac by the Video-to-NDI app. The receiver bridge is
  `Production_Hub_App/.build/native/libProductionHubNDI.dylib` (git-ignored;
  present in the dev worktree).
- The Dev process shares
  `~/Library/Application Support/Production Hub/config/default_profile.json`
  with the production app. Editing the profile changes production too.

## Committed on `dev` (in `1b66138`)

- **Diagnostic recording button never enabled.** Video services start
  lazily, after the diagnostics group is built, and nothing re-enabled the
  buttons. Fixed with `CameraControlPage._refresh_recording_controls`. The
  "Qt Multimedia could not initialize" notice now appears only after a
  real failure. Main has the same bug.
- **Diagnostic recorder hidden.** The section was built but never added to
  the page. It now shows on the camera page in Dev only (gated on
  `ptz_automation.tracking_refinements_enabled`).
- **Launcher Python.** `Launch Production Hub Dev.command` prefers the
  framework Python.
- Tests: three new ones in `testing/tests/unit/test_video_phase1.py`.

The recorder delivers about 10 frames/s even when `recording_fps` says 30.
The MP4 is stamped 30 fps, so it plays about 3x fast. Align it by frame
count over the manifest's `started_at`/`ended_at`, or by camera motion (see
tools below). The profile's `recording_fps` was put back to 10.

## What the live recordings showed

Two Dev sessions were recorded on 2026-10-11, with telemetry polled at 10 Hz
from `/api/camera/tracking` and `/api/camera/automation`. Annotated video:
`testing/benchmarks/outputs/ptz-debug-20261011-140345.mp4`, plus
`ptz-telemetry-*.jsonl` in the same folder. These outputs are git-ignored
and exist only on this Mac.

Note: `decision.targetBounds` is in **reference (Audience map)
coordinates**, not PTZ image coordinates; `framing.py` converts it with
`ptz_rect_to_reference`. Use the PTZ `subjects[].bounds` for on-screen
analysis.

| # | Symptom (live) | Root cause found |
|---|---|---|
| 1 | Two people split to opposite sides; the camera whip-pans 3,000 units to one side wall, then the other (run E, 21:10:57) | `ptz_automation.py`, partial-group branch of `_send_continuous_command`: with `widen_before_recentering` the speed command steers on `_subject_union(ptz_snapshot)`, i.e. **only the visible person**, against the correct composed `desired_pose` (the midpoint). **Fixed on WIP:** Dev steers on `decision.live_subject_bounds` (whole group); a clipped group member no longer forces a hard pan (groups widen instead). |
| 2 | Walker reaches the frame edge every 10-15 s; camera crawls 6-60 units/0.1 s | At sermon zoom the AW-HE120 already slows pan (measured: speed 26 ≈ 390 units/s at zoom 3500 vs 2,800 at 2238). Dev's `continuous_pan_max_speed=26` plus `image_motion_scale` slowed it a **second** time, to ≈0.23 frame/s or less. A walker needs ~0.5-1 frame/s. |
| 3 | Camera keeps panning the wrong way ~1 s after the subject turns | Speed slew limit (18/s) also limits **braking**; `_predicted_error` zeroes the target rather than reversing. |
| 4 | Lost speaker for 10-12 s after walking off the edge (run D, 21:05:48) | The handoff followed the right person, but: (a) speed as in #2; (b) never widened (zoom held ~3516); (c) hit `audience_handoff_max_seconds=8` and held; (d) the guard rejects any Audience-reported reversal. |
| 5 | Speaker stepped down off the platform; only the top of the head visible at the bottom for 10 s (run D, 21:06:54) | Tilt barely moves (speed ~3 for 150-unit errors). The detector can't see a person from a head sliver. Widening on loss would have recovered it. |
| 6 | Two-person zoom hunt: 7 reversals / 1,470 units in 40 s; one person clipped at the edge (first recording, 20:49:34) | Portrait group composition fills `group_maximum_width_occupancy=0.92` with only a 0.015 edge margin. Box jitter flips between "zoom in" and "touches edge → widen". **WIP:** Dev never tightens while anyone is within 8% of a side edge. |
| 7 | "PTZ analysis 0.9/s" right after arming | Display only: the rate estimate warms up. The real rate was 4/s. **Fixed on WIP:** the readout averages only the last 3 s. |

## Rate (user decisions)

- The user allows more than 4 analyses/s if it makes tracking fluid.
  **ProPresenter on the same Mac has priority.**
- **Variable rate, implemented on WIP (Dev only, while armed).**
  - PTZ analysis runs at 12/s while anything moves, or the subject is more
    than 25% off centre, or an escape or a reacquisition is running. It
    returns to the configured `analysis_fps` (4) after 1.5 s of calm.
  - The decision loop follows the same rate. Audience stays at 4/s.
  - Code: `PtzAutomationService._update_analysis_rate`,
    `PersonTrackingService.set_analysis_rate` / `analysis_rate`.
- **Measured cost** (`testing/tools/ptz_debug/vision_cost.py` on the
  recorded PTZ video): 6.3 ms CPU (11 ms wall) per analysis. That's 3% of
  one core at 4/s and 8% at 12/s, on a 10-core Mac. ProPresenter was at
  ~110% CPU / 495 MB at the time. Not yet measured: Vision's GPU/ANE use
  next to ProPresenter's rendering, live.
- Simulator, fixed rates vs adaptive: 4/s = 60, 8/s = 100, 12/s = 100,
  adaptive = 100 (fast 65% of the time in these motion-heavy cases).
- Earlier finding (memory): at 8/s, look-alikes crossing swapped IDs in
  main's twin scenarios. dev has no twin scenarios. Add one before shipping
  a higher rate.

## Work in progress: branch `dev-pursuit-wip` (not finished)

1. **Measured AW-HE120 speed model** copied unchanged from main:
   `Production_Hub_App/production_hub/integrations/panasonic_awp/speed_model.py`.
2. **Realistic stage simulation.**
   - `SimulatedPanasonic(measured_rates=True)` moves at the measured
     *picture speed* (frames/s per speed step and zoom), in the harness
     geometry's units. The harness sweep's frame is ~4x narrower in motor
     units than the real camera's at telephoto (432 vs measured 1,734
     pan units at zoom 3450; the live calibration says 2,798, i.e. inflated
     as noted in memory). So the old linear 2,600 units/s motor made
     telephoto pursuit look far easier than reality.
   - Only `stage_simulation.DelayedPanasonic` opts in. The golden recording
     and other harness tests keep the linear motor and still pass.
3. **New Dev pursuit** (`SmoothPtzVelocityController._pursue`, used only
   when `responsive_tracking`, i.e. Dev):
   - Works in picture speed (frames/s) and converts through `speed_model`.
   - Feed-forward on the subject's own motion: image velocity between two
     real detections (timed by `frame_captured_monotonic`, passed in as
     `observed_at`) plus the camera's motion over the same window, measured
     from its reported pose. Using the speed model for that caused limit
     cycles when the model was off.
   - The subject's speed counts only after two agreeing readings. A jump of
     more than 0.25 frame is a re-detection; speed is capped at 1 frame/s.
   - Centring gain 1.6/s with nothing inside the hold band. Adaptive per-axis
     gain halves on oscillation and recovers over ~6 s.
   - Accelerates at 2.4 frames/s², brakes and reverses at 6 frames/s².
   - Single subject: an edge-clipped box counts as further out. Head low with
     body off the bottom → tilt down. Headroom rule: a head within 4% of the
     top (predicted for a rising head) tilts up to 7% clearance.
   - Escape widening:
     - Triggered by a centre or an edge heading out of frame, starting at
       full widening speed.
     - At most 320 zoom units per episode.
     - No zoom-in mid-stride or while anyone is within 8% of a side edge.
     - Zoom-in restore speed scales with the distance left to go.
   - Audience recovery: motor error → picture speed via geometry
     `frame_spans`, capped at 0.6 frames/s.
4. **Handoff** (Dev only, `ptz_automation.py`):
   - Widens to ~1.6x the field of view at loss (`_HANDOFF_WIDENING`).
   - `_guard_audience_handoff(audience_fresh=...)`: while the Audience
     camera has a fresh (non-coast) match of the lock, it follows reversals
     and the time and travel limits restart. Stale or coasting estimates
     still face every limit.
   - Bug fix: the handoff anchor was the raw motor pose at the last PTZ
     sighting, usually with the person at the frame edge, so recovery fell
     short by up to half a frame. `_centred_motor_pose` corrects it.
   - Partial group steering fix (#1 above).
5. **Variable analysis rate** (see Rate above).

Stage motion matrix, 228 cases, realistic camera, `--counts 1,2,3`:

| Build | 4/s | 8/s | adaptive 4↔12 |
|---|---|---|---|
| `dev` as committed | 26 | 26 | – |
| WIP now | 60 | 100 | 100 |

- Remaining failures:
  - Fast walks (3-5 body widths/s) leaving the harness's calibrated sweep
    (x 0.30-0.70). A harness limit: pose_for_target raises outside coverage.
  - Separate/regroup (0/12): the sim splits a pair to both edges within
    0.25 s.
  - Some reversal-with-delay cases.
- Tests:
  - All 213 PTZ unit tests pass, plus 8 new ones in
    `testing/tests/unit/test_dev_pursuit_and_variable_rate.py`.
  - `run_end_to_end.py --mode fast`: 522 passed.
  - Golden (release path) unchanged.
- Rewritten tests (old-controller tuning → intent; review these):
  - `test_edge_pursuit_reaches_higher_speeds_and_only_widens`: was
    "...without_changing_zoom", which pinned exact speeds 44/32.
  - `test_small_corrections_stay_gentle_and_stop`: was "Dev must equal
    release".
  - `test_recorded_telephoto_range_stays_within_a_followable_picture_speed`:
    was "AW offset ≤ 26 and Dev == release". That cap is exactly what
    couldn't follow a walker.
  - `test_stage_tracking_suite.py` motor tests now compare against the
    measured-rate camera, plus a new picture-speed test.

Next steps:

1. **Live staging test** in Production Hub Dev on this branch (quit the
   production app first; they share ports and the camera). Record with the
   diagnostic recorder plus `ptz_telemetry_logger.py`, then use
   `align.py` + `annotate.py`.
2. Check CPU/GPU next to ProPresenter during a moving test. Look for
   press-to-cue impact as AGENTS.md requires.
3. Add a look-alike crossing scenario before trusting 12/s identity.
4. Widen the harness calibrated sweep (or make it configurable) so fast
   walks are scored fairly.
5. Merge `dev-pursuit-wip` into `dev` once live testing is good.

## Debugging tools (on `dev-pursuit-wip`, `testing/tools/ptz_debug/`)

- `ptz_telemetry_logger.py OUT.jsonl`: 10 Hz poll of the Dev API.
  Read-only; never touches ProPresenter.
- `motion.py`, `align.py`, `annotate.py`: align a diagnostic recording to
  telemetry by camera motion, and render the annotated debug video.
- `stage_rate.py DT COUNTS OUT.json`: run the stage motion matrix at any
  decision interval (0.25 = 4/s, 0.125 = 8/s, 0.083 = 12/s).
- `trace.py COUNT KIND SPEED DIR PROFILE DT [EVERY]`: step-by-step trace of
  one stage case (truth vs detection vs pose vs speed).
- `stage_rate.py adaptive COUNTS OUT.json`: matrix at 12/s ticks with PTZ
  detections refreshed at the automation's requested rate.
- `vision_cost.py PTZ_VIDEO [FRAMES]`: CPU cost per analysis.

Run tests with main's venv: `/Users/icas/Github/ICAS-Clicker-LIVE/testing/.venv/bin/python -m pytest`
(dev has no venv). The stage tools use the framework Python with
`PYTHONPATH=Production_Hub_App:.` and run from the dev root.

## Other open items

- Camera-permission lambda crash (above), offered as a separate task.
- The record-button fix is not on main yet.
- The fast suite (`testing/run_end_to_end.py --mode fast`) has not been run
  on dev since the move.
