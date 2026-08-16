# Production Hub

Production Hub is the macOS-first backend control plane for ICAS production remotes.
It hosts the API used by the existing browser pages, persists configuration/state,
and provides a desktop admin interface for setup, diagnostics, and health checks.

Camera Control includes stage-gated person detection, calibrated shadow framing,
and explicitly armed, bounded Panasonic PTZ tracking. See
[`docs/PTZ_AUTOMATION_PHASE45.md`](docs/PTZ_AUTOMATION_PHASE45.md).

## Install

```bash
cd Production_Hub_App
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

The official NDI Runtime 6 must also be installed on the Production Hub Mac.
Production Hub loads that runtime dynamically; the repository and application
bundle do not redistribute it.

## Run

Desktop app plus embedded API:

```bash
cd Production_Hub_App
source .venv/bin/activate
python3 main.py
```

API/background service only:

```bash
cd Production_Hub_App
source .venv/bin/activate
python3 main.py --api-only
```

Development data directory:

```bash
python3 main.py --api-only --data-dir ./dev-data
```

Default API binding is local-only:

```text
http://127.0.0.1:1337
```

## Build And Install The macOS App

Build `Production Hub.app` and update `/Applications/Production Hub.app`:

```bash
python3 Production_Hub_App/scripts/build_macos_app.py --install-deps --install
```

After the first dependency install, you can usually rebuild with:

```bash
python3 Production_Hub_App/scripts/build_macos_app.py --install
```

The generated app is written to:

```text
Production_Hub_App/dist/Production Hub.app
```

The build compiles and packages Production Hub's small in-process NDI receiver
bridge automatically. No NDI helper executable or video subprocess is launched.

When `--install` is used, the script replaces the existing app at:

```text
/Applications/Production Hub.app
```

If macOS denies write access to `/Applications`, rerun the same command with
`sudo` or pass a writable destination:

```bash
python3 Production_Hub_App/scripts/build_macos_app.py --install --install-destination ~/Applications
```

## Existing Remote Page URLs

Production Hub serves the current browser pages at:

```text
http://127.0.0.1:1337/remote/index.html
http://127.0.0.1:1337/remote/control.html
http://127.0.0.1:1337/remote/score.html
```

`score.html` now prefers the current Production Hub origin when served from
`/remote/score.html`. It also supports `?api=http://host:port` and the
`icas-score-api-base` localStorage key for manual overrides.

The other current HTML files still contain ICAS hosted defaults. For local
testing, change their API base constants to `http://127.0.0.1:1337` or add the
same base-URL selector pattern.

## Desktop Pages

The desktop UI includes:

- Overview
- Endpoints, with editable endpoint definition JSON
- Automations, with editable automation definition JSON and pause/resume
- Integrations
- Camera Control, including independent NDI/local inputs, bounded video
  diagnostics, Apple Vision person-detection shadow mode, recording/replay,
  Panasonic PTZ/lens controls, and presets
- Scoreboard, with native row editing, per-row score controls, queue controls,
  local action history, and undo
- Remote Pages
- Data & Storage
- Diagnostics
- Extensions
- Settings

## App Icon

For a Production Hub icon, keep the Apple Icon Composer source at
`Production_Hub_App/Production_hub.icon`. That source keeps the Liquid Glass
layers, translucency, dark appearance, and platform metadata intact.

Qt cannot load that `.icon` folder directly for the live window/menu-bar icon.
The custom runtime and bundle icon now use native `.icns` files only:

```text
Production_Hub_App/assets/ProductionHub.icns
Production_Hub_App/assets/production_hub_icon.icns
```

Recommended workflow:

```text
Production_hub.icon     Apple Icon Composer source
ProductionHub.icns      exported macOS app icon file
```

The macOS build script first looks for `Production_Hub_App/assets/ProductionHub.icns`.
If that file does not exist, it tries to render one from
`Production_Hub_App/Production_hub.icon` using Apple Icon Composer's bundled
`ictool` and macOS `iconutil`.

To generate only the `.icns` file:

```bash
python3 Production_Hub_App/scripts/build_macos_app.py --icon-only
```

If `ictool` cannot open the `.icon` document, the build stops instead of using
the flat PNG fallback. Open the `.icon` in Icon Composer, re-save it, and rerun
the script.

## Application Preferences

Settings now includes preferences for:

- Keeping Production Hub running when the main window is closed
- Showing a macOS menu-bar status icon
- Saving a launch-at-login preference

The close-window behavior and menu-bar icon are active in the desktop app.
Launch-at-login is saved as a preference for now; installing the LaunchAgent
should be done during packaging, when the final app bundle identifier and
executable path are known.

## Compatibility Routes

Implemented routes include:

- `GET /health`
- `GET /active-presentation`
- `GET /presentation/{uuid}`
- `POST /presentation/{uuid}/{index}/trigger`
- `GET /slide-index`
- `GET /thumbnail?uuid=...&index=...`
- `GET|POST /focus?index=...`
- `GET|POST /next`
- `GET|POST /previous`
- `GET|POST /prev`
- `GET /current-base`
- `GET /service_logos`
- `GET /macros`
- `POST /macro`
- `POST /preset`
- `GET /audio/playlists`
- `GET /audio/tracks?playlist=...`
- `POST /audio/trigger`
- `POST /audio/clear`
- `GET /audio/active`
- `GET /auto-show`
- `POST /auto-show`
- `GET /score`
- `POST /score`
- OBS bridge compatibility routes such as `/scene/current`, `/scene/set`,
  `/scene/items`, `/scene/items/apply`, `/obs/look/refresh`, and `/debug`

## Storage

By default, Production Hub stores data in:

```text
~/Library/Application Support/Production Hub/
```

Set `PRODUCTION_HUB_HOME` or pass `--data-dir` to override this. The app creates:

- `config/default_profile.json`
- `config/endpoints.json`
- `config/automations.json`
- `state/runtime_state.json`
- `state/scoreboard.json`
- `logs/production-hub-YYYY-MM-DD.log`
- `recordings/YYYYMMDD-HHMMSS/audience.mp4`
- `recordings/YYYYMMDD-HHMMSS/ptz.mp4`
- `backups/automatic/`
- `backups/manual/`

Writes are atomic and existing files are backed up before replacement.

## Phase 1 Video Inputs

The Camera Control page has independent Audience and PTZ video slots. Either
slot can use a discovered NDI source or any local camera/capture device exposed
by Qt Multimedia. Audience defaults to `Production Hub - Audience Cam` over
NDI; PTZ defaults to explicit local-camera selection so Production Hub does not
accidentally grab an unrelated webcam. Once a PTZ capture device is selected
and saved, that input auto-connects at application startup.

Local cameras request macOS Camera permission when Production Hub first tries
to connect (at startup for a saved PTZ input, or after Connect for a newly
selected input). A denial is shown directly in source health instead of leaving
the source in a permanent Starting state, and Camera Control links to the
correct System Settings privacy page.

Performance safeguards are intentional:

- NDI receive, local frame conversion, recording, and replay run outside Qt's UI thread.
- Each source has a one-frame broker slot; stale work is replaced, never queued.
- The full NDI stream is continuously drained while preview publication is capped.
- Pixel copying and preview rendering suspend when Camera Control is hidden and
  no diagnostic recording or explicitly active vision workflow needs frames;
  source health and reconnection continue.
- Person inference and Audience relocalization sleep on runtime activity events
  when neither tracking nor calibration/review is actually in use. Persisted
  enablement alone does not consume inference CPU.
- Preview delivery is capped at 12 fps. Diagnostic recording defaults to 10 fps
  and at most 1280 pixels wide through Qt Multimedia's bounded in-process
  MPEG-4 encoder.
- Diagnostic files are independent MP4 streams plus a small JSON manifest.
- Video capture is stopped cleanly when Production Hub quits.

## Phase 2 Scene Regions and Person Detection

The scene-plane editor supports empty-room setup by letting an operator draw, name, enable,
redraw, rename, and delete polygonal regions on the fixed Audience view. A
large Scene Drawing Review window includes ten church-stage candidates and
shows one selected drawing at a time by default; operators can compare all,
hide individual drawings, or bulk-delete unwanted candidates. The built-in
semantic types are Stage, Front of Stage, Altar, Podium, Audience, and Custom.
Phase 3C stores polygons in approved calibration-reference coordinates and
projects them into a drifted live Audience view. Both reference `points` and
locked `livePoints` are published read-only at `GET /api/camera/regions`.
The same popup can derive unlabeled structural-plane candidates from every
simultaneous Audience/PTZ image in an approved sweep. Candidates must recur
across PTZ poses and retain their view count, feature support, and confidence
for operator review; semantic names are always assigned by the operator.

Phase 2 also adds observation-only person detection through Apple's native
Vision framework. A single worker analyzes only the newest frame for each
enabled source, defaults to 4 fps per source and 960 pixels wide, and never
accumulates an inference queue. Camera Control draws stable short-horizon
subject IDs and lets an operator select or deselect people that are currently
detected. A zero-person feed therefore leaves the explicitly named
`Select All Detected` buttons disabled; region setup remains fully usable.
These selections do not send Panasonic commands. See `docs/VIDEO_PHASE2.md`
for the measured baseline and safety boundary. Read-only tracking snapshots
are available at `GET /api/camera/tracking` and
`GET /api/camera/tracking/{audience|ptz}` for future dashboards and automation
observability.

For source development on macOS, `python3 main.py` automatically relaunches the
GUI through `.build/dev/Production Hub Dev.app`. That small, stable app wrapper
loads the live source tree while supplying the bundle identity and privacy
description macOS requires for Camera access. Grant Camera permission once to
`Production Hub Dev`; source edits do not rebuild or change that identity.

## Phase 3 Calibration, Curation, Relocalization, and Scene Planes

Camera Control now includes a large Tracking Marker Review popup. It loads the
latest accepted calibration without requiring live cameras, shows each unique
Audience point beside its mapped PTZ point across eleven structural motor poses,
and lets an operator select the same marker in either image. Markers can be
reversibly excluded/restored; edited maps require explicit approval and prior
approved maps remain available for rollback. The same review is
available through **Tools → Calibrate PTZ Camera to Audience Camera**.

The calibration button first checks illumination and visible detail in both
live feeds. If either feed is missing, dark, or featureless, movement is blocked.
After operator confirmation, the packaged workflow captures a direct reference,
runs the guarded eleven-pose PTZ sweep, restores the recorded starting pose, and
builds the composed synchronization map. The standalone read-only
`scripts/calibrate_camera_views.py` remains available for diagnostics. See
`docs/CAMERA_CALIBRATION_PHASE3.md` for commands, confidence gates, and the
single-plane/parallax limitation.

An approved map also drives an activity-gated latest-frame relocalization worker. It
uses curated landmarks to compensate live Audience-camera drift and reports
Locked, Degraded, Lost, or Error health at `GET /api/camera/calibration`. Only
Locked plus approved reports `motionSafe=true`; no Phase 3 service imports the
Panasonic client or grants camera-motion authority.

## Tests

These tests use the Python standard library `unittest` runner:

```bash
cd Production_Hub_App
python3 -m unittest discover production_hub/tests
```

They cover configuration seeding/backups, endpoint sequencing, automation
cooldown/debounce behavior, audio normalization, OBS look rule mapping,
Panasonic CGI URL construction, VISCA parsing/mapping/responses, and scoreboard
revision conflict handling. Phase 2 tests also cover frame supersession,
subject association, selection persistence, empty-room scene-region setup, and
safe configuration defaults.
