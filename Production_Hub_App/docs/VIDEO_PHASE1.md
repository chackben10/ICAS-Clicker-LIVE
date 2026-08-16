# Phase 1 Video Foundation

## Implemented boundary

Phase 1 provides independent Audience and PTZ video slots. Each slot can use a
discovered NDI source or a user-selected local capture device through Qt
Multimedia. It displays bounded previews, reports source health, and
records/replays short diagnostic MP4 files. It does not perform person
detection, calibration, framing decisions, or automated Panasonic movement.

## Runtime ownership

- Each `NDIVideoSource` owns one receive thread and continuously releases every
  NDI frame. It copies pixels only for an active preview or recording consumer.
- Each `LocalCameraVideoSource` owns a Qt native capture session. Its signal handler
  performs no pixel conversion; one replaceable `QVideoFrame` is handed to a
  dedicated converter thread.
- `LatestFrameBroker` retains exactly one immutable `QImage` per source.
- `DiagnosticRecorder` prepares at most one pending frame per source on a
  worker, then feeds Qt Multimedia's in-process MPEG-4 recorder. Qt's encoder
  backpressure and the replaceable frame slots prevent a recording backlog.
- `ReplayVideoSource` uses Qt Multimedia for decoding and hands at most one
  replaceable frame to a converter worker before publishing it to the replay
  slot.

## Measured Audience NDI baseline

Measured on the Production Hub Mac against the live
`Production Hub - Audience Cam` source at 1920×1080/30 fps:

| State | CPU use |
| --- | ---: |
| Receiving with preview suspended | 28.4% of one CPU core |
| Receiving with bounded preview | 30.7% of one CPU core |
| Receiving and 1280×720/10 fps diagnostic recording | 37.0% of one CPU core |

The latest 20-second recording run received 600 NDI frames with zero receiver
drops and wrote a 1.0 MB MPEG-4 diagnostic file. These figures are percentages
of one core, not percentages of the whole Mac. Long service-length soak testing
with ProPresenter remains a hardware acceptance gate because a short synthetic
benchmark cannot prove another application's behavior over several hours.

## Safe defaults

- Audience NDI auto-connects using full bandwidth.
- PTZ local capture auto-connects at startup after a device has been selected and saved.
- Both slots can be changed between NDI and local camera without restarting the app.
- Local capture checks and requests macOS Camera permission before opening a device.
- Preview delivery is capped at 12 fps.
- Recording is capped at 10 fps and 1280 pixels wide.
- Hidden previews do not render pixels; frame copying also suspends unless a
  recording or later-phase analysis consumer is active.
- Stale sources are visible and NDI reconnects without operator intervention.
- Quitting Production Hub stops capture and flushes recordings.
