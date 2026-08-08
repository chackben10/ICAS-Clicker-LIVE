# Production Hub - Video to NDI

A standalone, single-process macOS helper that opens local cameras and capture interfaces, then publishes each configured input as an independent NDI source. It is deliberately separate from the Production Hub application.

The intended setup is:

- This helper publishes **Audience Cam** as `Production Hub - Audience Cam` from the OBS Mac.
- Production Hub receives Audience Cam over NDI.
- The PTZ feed can be opened directly by both OBS and Production Hub, so it does not need to pass through this helper.

## Requirements

- macOS 13 Ventura or later, on Apple silicon or Intel.
- Xcode 15.3 or later with its matching macOS SDK selected. Command Line Tools alone also work when their Swift compiler and SDK versions match.
- The official NDI Runtime 6, normally installed with [NDI Tools](https://ndi.video/tools/).
- An NDI receiver in OBS, normally the [DistroAV](https://github.com/DistroAV/DistroAV) plugin.
- Gigabit Ethernet is strongly recommended for the full-bandwidth Audience Cam feed.

The NDI runtime is loaded dynamically and is not bundled or redistributed by this repository.

## Build the app

From this directory:

```bash
./scripts/build_app.sh
open "/Applications/Production Hub - Video to NDI.app"
```

The script builds and signs the app, then installs it into `/Applications`. If `/Applications` requires administrator access, the script asks for your macOS password through `sudo`. For a build that stays only in `dist/`, use `./scripts/build_app.sh --no-install`.

If `swift build` reports that the SDK was built by a different Swift compiler, select a complete matching Xcode installation:

```bash
sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer
```

The build script also contains a direct-compiler fallback when SwiftPM itself is unavailable. A matching Swift compiler and macOS SDK are still required.

## First-time setup

1. Connect the Audience Cam capture interface.
2. Open **Production Hub - Video to NDI** and allow Camera and Local Network access.
3. Edit **Audience Cam**, choose its physical device and desired format, then save.
4. Start Audience Cam. Its card should show a live preview and green **Running** state.
5. Receive `Production Hub - Audience Cam` in Production Hub. NDI receiver menus normally prefix it with the source Mac's computer name.
6. Enable automatic start for Audience Cam and optionally enable launch at login.

You can add more routes for additional capture devices. A test-pattern input is included so NDI and OBS can be checked without camera hardware.

## Important behavior

- Each physical device is opened only once inside this app.
- Every route can rotate its video by 0°, 90°, 180°, or 270° and flip it horizontally and/or vertically before preview and NDI output.
- Multiple enabled routes may share a device; the first route started chooses its capture format.
- Every route has a one-frame queue. Under load, stale frames are dropped to preserve live latency.
- Closing the window does not stop the feed. It suspends preview conversion to save GPU time; the NDI feed continues uninterrupted.
- The menu-bar camera icon shows receiver and frame-rate health and provides per-route start/stop controls without opening the main window.
- UI statistics are published twice per second rather than once per video frame. Every captured frame still reaches the bounded NDI send path.
- This release publishes video only; it does not capture audio.

OBS and this helper may open the same capture interface when its driver supports shared access. If a particular interface is exclusive, its route will show **Busy**; close the other direct source or receive the helper's NDI output instead.

## Configuration and diagnostics

Routes are saved at:

```text
~/Library/Application Support/Production Hub - Video to NDI/routes.json
```

The app keeps the preceding version as `routes.backup.json`. Open **Diagnostics** to see the NDI version, discovered devices, negotiated formats, sender frame counts, receiver counts, and dropped frames.

## Development checks

```bash
./scripts/test.sh
```

That runs Swift unit tests, compiles the C bridge with warnings treated as errors, and creates a short live NDI sender. If SwiftPM is unavailable because of a partial Command Line Tools installation, it performs a complete direct app build as the fallback compile check. Use `./scripts/test.sh --skip-live-ndi` when network access is unavailable.

See [Architecture](docs/ARCHITECTURE.md) for implementation details and [Manual QA](docs/MANUAL_QA.md) for the hardware acceptance checklist.
