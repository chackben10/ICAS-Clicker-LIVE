# Phase 3 Camera Calibration, Live Relocalization, and Scene Planes

## Purpose and safety boundary

`scripts/calibrate_camera_views.py` is a standalone, read-only calibration
tool. It opens the configured Audience and PTZ sources, captures several fresh
frame pairs, discovers natural image features automatically, estimates an
Audience-to-PTZ image transform, and saves everything needed for review. It
does not send Panasonic commands and does not modify Production Hub
configuration.

The implementation uses OpenCV's SIFT detector, reciprocal nearest-neighbor
ratio filtering, and a USAC MAGSAC robust homography. This replaces a handful
of manually clicked landmarks with hundreds or thousands of computer-detected
candidate points. The robust fit rejects correspondences that disagree with
the dominant scene geometry.

## Run against the configured live feeds

Install the updated requirements once, then run:

```bash
python3 -m pip install -r requirements.txt
python3 scripts/calibrate_camera_views.py
```

Live runs automatically use the stable `Production Hub Dev.app` identity so
the PTZ camera uses the same macOS Camera permission as source-mode Production
Hub. The default output is:

```text
~/Library/Application Support/Production Hub/calibration/YYYYMMDD-HHMMSS/
```

Use `--output /some/directory` to choose another location. Use `--samples 10`
for a larger multi-frame trial. The tool evaluates every captured pair and
selects the strongest defensible result rather than averaging a bad frame into
a good result.

## Guarded PTZ movement sweep

For a multi-pose synchronization map, leave the PTZ on an operator-approved
center-stage view and run:

```bash
python3 scripts/calibrate_camera_sweep.py --confirm-movement --pose-samples 6
```

The explicit confirmation flag is mandatory. The routine queries and records
the exact starting pan, tilt, and zoom values, moves through nine conservative
wide/medium/tight and left/right/tilt poses, verifies arrival at every pose,
and restores the starting position in a `finally` path. Each pose retains its
raw images, direct matches, diagnostics, motor coordinates, and result in a
timestamped `calibration-sweeps` directory.

Direct Audience-to-PTZ matching can become sparse at moved poses because the
cameras have different exposure, height, and field of view. Build the final
map by anchoring one accepted direct alignment and composing robust
same-camera PTZ-to-PTZ links:

```bash
python3 scripts/build_camera_sync_map.py \
  --reference-calibration /path/to/reference/calibration.json \
  --sweep /path/to/calibration-sweeps/TIMESTAMP/sweep.json
```

The resulting `full_sync.json` contains both directions of the composed
homography for every accepted motor pose. The builder never moves the camera.
It only accepts PTZ links that pass robust geometric quality gates and writes
a separate match/overlay review for every pose.

## Production Hub review and calibration action

Camera Control's **Tracking Marker Review** loads the newest accepted map from
Application Support and remains usable from saved images when the room is dark
or cameras are offline. Unique physical points are labeled identically in the
Audience and selected PTZ pose. The operator can select a point in either image,
switch among every calibrated motor pose, temporarily use live images, and
reveal the complete diagnostic directory.

The review is non-destructive. **Exclude Selected** writes marker decisions to
a sidecar review file; it never rewrites the generated map. Restoring a marker
is equally reversible. Every edit changes the review state to `pending_review`,
which makes runtime mapping fail closed until the operator chooses **Approve
and Activate**. The active-map registry preserves earlier approved paths for
**Roll Back Active**.

**Calibrate PTZ Camera to Audience Camera** is available in both Camera Control
and the application **Tools** menu. Before presenting the movement confirmation,
Production Hub requires fresh frames with sufficient mean luminance, contrast,
and visible-pixel coverage from both cameras. The full workflow runs in a
separate process so the desktop UI remains responsive. It refuses to move
without explicit operator confirmation and attempts restoration on every sweep
failure path.

## Live Audience-camera relocalization

Production Hub loads only an approved calibration for runtime use. A dedicated
latest-frame worker detects SIFT features near enabled reference landmarks and
fits reference-to-live geometry with reciprocal descriptor matching and USAC
MAGSAC. The worker is capped at one pass per second by default, operates on a
maximum 1280-pixel-wide copy, and never queues more than the broker's newest
frame.

Runtime health is `Locked`, `Degraded`, `Lost`, or `Error`. Only `Locked` with
an approved map reports `motionSafe=true`; all other states are explicitly
fail-closed. The current matrix, quality metrics, and relocated marker points
are exposed read-only at `GET /api/camera/calibration`.

## Calibrated scene planes

Stage, Front of Stage, Altar, Podium, Audience, and custom polygons are stored
in normalized coordinates of the approved Audience reference image. When the
live camera moves, the relocalization matrix projects those polygons into the
current frame. Drawing on a locked live frame applies the inverse transform
before saving, so camera drift never becomes part of the stored boundary.

Every polygon records its calibration reference identifier. Polygons belonging
to an older calibration are labeled **Needs Redraw** and are withheld from the
live stabilized output. `GET /api/camera/regions` returns both durable
`points` and, while locked, current `livePoints`.

### Cross-camera structural-plane generation

The scene-plane review popup can generate unlabeled structural candidates from
the approved multi-pose sweep. The generator reuses every simultaneous
Audience/PTZ image and the recorded Audience drift transform; it does not move
either camera. For each pose it uses SIFT reciprocal matches and iteratively
fits several USAC MAGSAC homographies instead of forcing the room into one
global plane. Spatial support clusters are transformed back into the approved
Audience reference, and a surface is retained only when it recurs in multiple
PTZ poses.

The resulting polygons are deliberately named `Structural Plane 01`, and so
on. Production Hub does not infer Stage, Altar, or Podium semantics from image
geometry. The popup shows the number of confirming PTZ views, supporting
features, and confidence so an operator can rename, redraw, disable, or bulk
delete questionable candidates. Generation writes `structural-planes/planes.json`
and `planes-overlay.jpg` alongside the immutable synchronization map, then
installs only the polygons into normal validated configuration.

The equivalent offline command is:

```bash
python3 scripts/build_structural_planes.py \
  --data-dir "$HOME/Library/Application Support/Production Hub"
```

### Idle workload policy

Person inference and live Audience relocalization require an explicit runtime
activity grant. When neither active person tracking nor calibration/review is
in use, both workers sleep on events, analyze zero frames, and do not keep NDI
or local-camera pixel conversion active. Opening a calibration or plane-review
window wakes relocalization; enabling person detection while Camera Control is
visible wakes the configured tracking sources. Closing or leaving those
workflows immediately returns the services to `Idle`. This activity gate
grants computation only and never grants PTZ motion authority.

## Offline reproduction

The same estimator can be reproduced without camera access:

```bash
python3 scripts/calibrate_camera_views.py \
  --audience-image audience.jpg \
  --ptz-image ptz.jpg \
  --output calibration-review
```

## Artifacts and acceptance

Each run writes:

- `calibration.json`: transforms, every accepted correspondence, capture
  metadata, thresholds, per-sample results, cross-sample transform stability,
  and explicit rejection reasons.
- `audience.jpg` and `ptz.jpg`: exact selected source frames.
- `inlier_matches.jpg`: up to 250 spatial correspondences drawn across the two
  views.
- `alignment_overlay.jpg`: the Audience image warped into PTZ space and blended
  over the PTZ frame for fast visual inspection.

Exit code `0` means all conservative gates passed. Exit code `2` means a model
was found and diagnostics were saved, but one or more confidence gates failed.
Exit code `1` means capture or estimation failed.

Acceptance requires enough reciprocal matches and robust inliers, adequate
feature coverage in both images, a healthy inlier ratio, and low symmetric
reprojection error. A numerically valid homography that covers only the cross
or one television corner is deliberately marked low-confidence.

For three or more mutually stable sample models, the consensus fit may use half
the single-frame coverage floor. This reflects independent temporal evidence
without relaxing the inlier-count, inlier-ratio, reprojection-error, or
cross-sample agreement gates. It is especially important here because the PTZ
stage view occupies only a small part of the very-wide Audience image.

## Geometry limitation

A homography represents one plane or a distant scene approximation.
The back wall, stage floor, steps, altar, and foreground audience are at
different depths. The cameras' roughly two-foot vertical separation therefore
creates some parallax even at a distance of more than thirty feet. This first
prototype measures whether the dominant stage geometry is accurate enough; it
does not pretend a single matrix is a complete 3D calibration.

The structural-plane generator now provides a piecewise image-space model for
operator review, but it is not a metric 3D reconstruction. Calibrated camera
intrinsics remain the next step if depth-dependent errors are too large.
Automatic movement is isolated in the explicit, operator-approved guarded
sweep above.
