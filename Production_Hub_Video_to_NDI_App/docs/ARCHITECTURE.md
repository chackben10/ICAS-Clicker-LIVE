# Architecture

`Production Hub - Video to NDI` is intentionally separate from Production Hub. Its only job is to own local video-capture devices and expose their frames as stable NDI sources.

```text
Audience capture device ─ AVFoundation capture hub ─ route queue ─ NDI sender ─ Production Hub

PTZ capture device ─ direct local capture in OBS and Production Hub (outside this helper)
```

## Main components

- `CaptureDeviceDiscovery` enumerates macOS video devices and their supported formats.
- `CaptureHub` opens each physical device once. Multiple routes for the same device share its frames, avoiding exclusive-access conflicts inside this app.
- `LatestFramePump` gives every route a one-frame queue. If transmission falls behind, it drops the stale frame instead of accumulating latency.
- `VideoFrameTransformer` applies that route's rotation and horizontal/vertical flips with Core Image before the frame reaches its preview or NDI sender. Quarter-turn rotations automatically swap output dimensions.
- `OfficialNDISender` sends progressive BGRA frames through the installed official NDI Runtime.
- `CNDIShim` dynamically loads `libndi.dylib`. No NDI binaries or proprietary SDK files are committed to this repository.
- `RouteController` owns saved routes and exposes simple start/stop state to the SwiftUI app and menu-bar control.

## Device ownership rule

The first active route for a physical device selects its capture format. Any other active route using that device receives the same format. This is deliberate: AVFoundation opens the hardware once, then the app fans out frames in memory.

OBS should receive the helper's NDI source instead of opening the same physical capture device. Production Hub can receive that NDI source at the same time; NDI senders support multiple receivers.

## Runtime and safety behavior

- NDI sending is synchronous on a dedicated serial queue per route.
- Each route retains only its newest pending frame.
- Route shutdown removes its capture subscription, drains its send queue, and only then destroys its NDI sender.
- The app continues running after its main window closes and exposes controls from the macOS menu bar.
- Closing the main window disables preview rendering while leaving capture and NDI transmission active.
- Route statistics reach SwiftUI at 2 Hz instead of video frame rate, avoiding needless main-thread rendering work.
- Route settings are written atomically, with the previous file retained as `routes.backup.json`.

## Current scope

Version 0.1 publishes video only. Camera audio is intentionally not captured. The app does not perform PTZ tracking or Panasonic control; Production Hub consumes the PTZ feed directly and owns that automation.
