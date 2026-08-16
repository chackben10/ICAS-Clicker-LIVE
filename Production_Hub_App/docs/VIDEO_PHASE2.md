# Phase 2 Scene Regions, Person Detection, and Subject Association

## Implemented boundary

Phase 2 provides an empty-room scene model plus an observation-only perception
layer. An operator can draw named Stage, Front of Stage, Altar, and Custom
polygons directly on the fixed Audience feed. These regions use normalized
coordinates, persist in validated configuration, and do not require a person
to be present. The same phase detects people on the Audience and/or PTZ broker
slots, associates detections with short-horizon subject IDs, draws operator
overlays, and stores subject selections.

The regions are image-space polygons, not yet a reconstructed physical plane.
Phase 2 does not estimate camera geometry, correct mount drift, map Audience
coordinates into the moving PTZ view, calculate a target frame, or send
Panasonic commands. Those calibration and control responsibilities remain
Phase 3 work.

The deliberate order is safety-critical: service footage and operator feedback
can expose missed detections, false detections, and identity swaps before any
computer-vision output is allowed to influence physical camera motion.

## Runtime design

- Apple's Vision framework supplies the pretrained human-rectangle detector and
  chooses the appropriate Apple compute device. PyObjC provides the maintained
  Python-to-Objective-C binding.
- One worker services both logical sources. It reads immutable snapshots from
  the Phase 1 one-frame broker and never owns an unbounded input queue.
- Superseded frames are skipped. Default analysis is capped at 4 fps per source
  and 960 pixels wide.
- A deterministic short-gap associator combines overlap and center distance to
  preserve subject IDs during ordinary stage movement. Tracks expire after a
  bounded number of missed analyses.
- Subject selection exists only in tracking state. The service has no reference
  to `PanasonicAwpService`, so it cannot issue movement commands.
- Tracking is disabled by default and may be enabled independently for Audience
  and PTZ from Camera Control.
- Scene-region editing is independent of person detection. Drawing works from
  any connected Audience frame even when tracking is disabled or zero people
  are detected.
- `Select All Detected · Audience/PTZ` acts only on visible detector results,
  reports the current count, and is disabled when that count is zero.
- Scene regions are exposed read-only at `GET /api/camera/regions`; tracking
  remains available at `GET /api/camera/tracking` and its per-source routes.

## Empty-room setup workflow

1. Connect the Audience feed in Camera Control.
2. Under **Phase 2 · Scene Drawings**, choose **Open Scene Drawing Review**.
3. Review the ten supplied stage, altar, podium, step, and left/right zone
   candidates. The popup shows only the selected drawing by default; turn that
   option off when comparing overlaps.
4. Use each row checkbox to hide a drawing, or select multiple rows and choose
   **Delete Selected**. Deleted suggestions can be restored explicitly.
5. To add a custom boundary, choose **Draw New Region**, select its semantic
   type and name, then click at least three points clockwise or counter-clockwise
   around the area and choose **Finish Drawing**.
6. Select a saved drawing to redraw or rename it. Changes persist immediately,
   and destructive edits create a configuration backup.

Draw Stage as the normal performance area, Front of Stage as the forward strip
or steps that may need different framing behavior, and Altar as its own region.
Overlapping polygons are supported intentionally.

## Live Audience NDI baseline

Measured on the Production Hub Mac against the live 1920×1080/30 Audience NDI
feed with analysis at 4 fps and a 960-pixel Vision input:

| Metric | Result |
| --- | ---: |
| Analysis frames | 60 |
| Elapsed time | 17.38 seconds |
| Effective analysis rate | 3.65 fps |
| Latest Vision inference | 8.21 ms |
| NDI frames received | 502 |
| Receiver drops | 0 |
| Process user + system CPU time | 6.80 seconds |
| Peak memory footprint reported by macOS | 248 MB |

No people were visible in this particular sample, so the correct subject count
was zero. A staffed-stage validation and a complete service-length soak remain
acceptance gates; a short empty-stage measurement cannot establish detection
recall, identity stability, or ProPresenter behavior over several hours.

## Development camera identity

Raw framework Python lacks Production Hub's macOS privacy usage description and
bundle identity. GUI source runs from `python3 main.py` therefore relaunch through
a small `.build/dev/Production Hub Dev.app` wrapper. The wrapper contains only a
stable framework-Python executable, metadata, and the Production Hub icon; it
continues loading code and dependencies from the source tree. Because source
edits do not modify the wrapper, macOS can retain its Camera authorization.
