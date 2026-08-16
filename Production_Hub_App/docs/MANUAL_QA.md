# Manual QA Checklist

## Startup

- Production Hub creates config, state, logs, and backup directories.
- App opens to Overview.
- API starts on `127.0.0.1:1337`.
- Missing ProPresenter, OBS, or Panasonic services show as offline without crashing.
- Diagnostics shows visible errors for unavailable integrations.

## ProPresenter

- Test `/health`.
- Fetch `/active-presentation`.
- Fetch `/slide-index`.
- Trigger `/next`.
- Trigger `/previous`.
- Trigger `/focus?index=0`.
- Confirm active/focused presentation fallback behavior.
- Fetch a valid `/thumbnail`.
- Confirm blank preview UUID is avoided where relevant.
- With a focused service playlist, fetch `/playlist/focused` and `/playlist/{uuid}`.
- Confirm the clicker shows Previous, Next, and the playlist menu only for a non-null focused playlist item.
- Confirm Previous/Next skip headers, placeholders, hidden presentations, and announcement presentations.
- Confirm playlist headers and placeholders remain visible but disabled in the menu.
- Preview a playlist presentation and confirm playlist controls disappear until Return to Active Presentation is used.
- With Clicker Can Present enabled, select a preview slide and verify the switch confirmation can be cancelled.
- Confirming a playlist preview must retain playlist focus and trigger the chosen slide.
- Search for a song in the focused playlist and confirm it also retains playlist focus when triggered.
- If a searched song occurs more than once, confirm the next occurrence is chosen, falling back to the closest earlier occurrence.
- Confirm a disabled first slide runs before the requested slide when switching presentations.
- With Clicker Can Present disabled, previews remain readable and neither UUID nor playlist triggers are allowed.

## OBS

- Connect to OBS WebSocket.
- Fetch `/scene/current`.
- Fetch `/scene/items?scene=ProPresenter%20Input`.
- Trigger scene changes with transition policy.
- Test special scene transition fallback.
- Run `/obs/look/refresh` for each seeded look rule.
- Confirm source visibility matches the seeded show/hide IDs.

## Automations

- Bible Look Enforcement triggers only after cooldown.
- OBS Look Sync avoids duplicate applications.
- Slide Label Audio Sync waits 0.5 seconds and prevents duplicates.
- Auto Show clears announcements and sets OBS to ProPresenter Input.
- OBS Watchdog does not create overlapping reconnect attempts.

## Audio

- Fetch `/audio/playlists`.
- Fetch `/audio/tracks?playlist=Major%20Pads`.
- Trigger a valid track.
- Clear audio.
- Fetch `/audio/active`.
- Confirm labels such as `D(Major).wav` match `D(Major)`.

## Panasonic AWP

- Test camera connection.
- Pan/tilt movement starts and stops.
- Zoom in/out starts and stops.
- Focus auto/manual/near/far works.
- Menu on/off works.
- Camera feed/color bars toggles.
- Power on/standby works.
- Auto white balance triggers.
- Recall preset 00 Home.
- Save, recall, and rename a non-zero preset.

## Phase 1 Video

- Install NDI Runtime 6 and start `Production Hub - Audience Cam` on the OBS Mac.
- Open Camera Control and grant Camera and Local Network permissions.
- Switch both Audience and PTZ between NDI and local-camera modes; confirm each source list is populated independently.
- Deny Camera permission once; confirm the source reports Permission Denied and Camera Privacy opens the correct System Settings page.
- Confirm Audience Cam resolves, displays 1920×1080 video, and stays fresh.
- Confirm NDI received-frame count rises and dropped-frame count remains zero during a 10-minute test.
- Select the PTZ capture interface and connect it while ProPresenter is already using that same device.
- Confirm ProPresenter remains responsive and its PTZ feed does not freeze or renegotiate.
- Confirm Production Hub shows both previews without increasing latency over time.
- Disconnect and reconnect Audience NDI; confirm Production Hub recovers automatically.
- Unplug and reconnect the PTZ interface; confirm a stale/error state is visible and no app crash occurs.
- Start a diagnostic recording, exercise both feeds, stop it, and confirm two MP4 files and `manifest.json` exist.
- Replay both saved files from Camera Control and confirm they render without accessing live devices.
- Close the Production Hub window and confirm configured video services remain active with the app.
- Quit Production Hub and confirm both video inputs are released immediately.
- Run a complete service-length soak while watching CPU, memory, ProPresenter responsiveness, and NDI drops.

## Phase 2 Scene Regions and Person Detection

- With an empty room, connect Audience Cam and open **Scene Drawing Review**.
- Confirm ten suggested drawings are listed and the large preview initially shows only the selected drawing.
- Select each row and visually review its boundary; turn off **Show only selected drawing** and confirm all enabled polygons can be compared.
- Uncheck a drawing to hide it, select multiple rows, and confirm **Delete Selected** removes all selected drawings and creates a configuration backup.
- Draw a custom region and confirm it appears in the popup, remains after restarting the app, and retains its name and enabled state.
- Select a drawing and verify Redraw replaces its boundary without changing its identity; verify Rename and Delete.
- Confirm `GET /api/camera/regions` returns normalized points for all saved regions.
- Confirm both `Select All Detected` buttons show `(0)` and remain disabled while no people are visible.
- Enable Person Detection in Camera Control and confirm the page states that it is in shadow mode.
- Analyze Audience only; confirm PTZ conversion remains suspended when its preview is hidden and it is not being recorded.
- Walk through the Audience frame and confirm a subject box follows with a stable `S#` label under ordinary motion.
- Click a subject box and confirm it turns orange; click it again and confirm it is deselected.
- Select two visible people, then move apart and cross paths; record any identity swaps for later tracker tuning.
- Confirm the subject count drops after a person leaves the frame and recovers when the person returns.
- Repeat with PTZ analysis and with both feeds enabled.
- Confirm inference latency and analysis FPS remain visible and no inference backlog grows.
- Confirm no Panasonic command appears in logs or camera activity while detection is enabled.
- Run `python3 main.py`, grant Camera access to `Production Hub Dev` once, and confirm subsequent source runs retain access.
- Run `python3 scripts/verify_phase2_tracking.py --frames 60` and confirm tracking is running with zero receiver drops.
- Complete a service-length soak with ProPresenter and compare CPU, memory, NDI drops, and presentation responsiveness to the Phase 1 baseline.

## Phase 3 Automatic View-Alignment Prototype

- Leave the PTZ at the desired reference pose and ensure both configured feeds are live.
- Run `python3 scripts/calibrate_camera_views.py --samples 12` and confirm the summary says `accepted` and `stable`.
- Confirm the tool sends no Panasonic commands and does not change Production Hub configuration.
- Review `inlier_matches.jpg`; matches should span several distinct stage objects rather than one repeated corner.
- Review `alignment_overlay.jpg`; confirm the dominant stage geometry overlaps while allowing expected parallax between the floor, back wall, podium, and foreground pews.
- Confirm `calibration.json` contains multiple consistent samples, both transform directions, every consolidated inlier, source metadata, and zero motion authority.
- Obscure a camera or use a featureless test image and confirm the tool fails closed or returns `low_confidence` instead of publishing an accepted model.
- With the room clear and the PTZ on a safe center-stage view, run `python3 scripts/calibrate_camera_sweep.py --confirm-movement --pose-samples 6`.
- Confirm all commanded positions remain on the stage, every reached pose records exact pan/tilt/zoom values, and the camera returns within the documented motor tolerance even if a pose calibration fails.
- Build `full_sync.json` with `scripts/build_camera_sync_map.py`; confirm every accepted pose has a robust same-camera PTZ link, composed Audience→PTZ and PTZ→Audience matrices, and reviewable link diagnostics.
- Open **Tracking Marker Review** with both cameras offline; confirm saved Audience and PTZ images, unique marker labels, all calibrated poses, and motor coordinates remain reviewable.
- Select the same marker from the Audience image, PTZ image, and marker list; confirm it is highlighted in orange in both views.
- Confirm **Calibrate PTZ Camera to Audience Camera** appears in Camera Control and the application Tools menu.
- With missing, dark, or featureless frames, trigger calibration and confirm the light gate blocks all movement before the confirmation dialog.
- Under adequate light, confirm the action requires an explicit movement confirmation, streams progress without freezing the UI, and reloads the new map only after a successful restored sweep.
- Exclude one marker in **Tracking Marker Review** and confirm it disappears from every PTZ pose, appears under **Restore Excluded**, and changes the map to Pending Review.
- Confirm live relocalization stops using an edited pending map until **Approve and Activate** is selected.
- Restore the marker, re-approve, and confirm the review decision survives an application restart without changing `full_sync.json`.
- Approve a newer map and use **Roll Back Active** to restore the preceding approved map.
- With Audience Cam live, confirm **Live Audience lock** reports Locked with inlier/error metrics and `GET /api/camera/calibration` reports `motionSafe: true`.
- Slightly move Audience Cam and confirm the same marker dots remain attached to fixed architecture while the reference-to-live matrix changes.
- Obscure or disconnect Audience Cam and confirm health becomes Degraded/Lost, marker projection stops, and `motionSafe` becomes false.
- Open **Calibrated Scene Plane Review** while locked, redraw Stage, Front of Stage, Altar, and Podium, then confirm their saved coordinates remain in `calibration_reference` space.
- Move Audience Cam again and confirm the live scene-plane overlays follow the room geometry; verify `/api/camera/regions` returns both reference `points` and stabilized `livePoints`.
- Confirm a scene plane tied to an older calibration is marked **Needs Redraw** and is not published as a stabilized live plane.
- In **Calibrated Scene Plane Review**, choose **Generate Structural Planes from Calibration** and confirm the PTZ does not move while every saved sweep pose is analyzed in a child process.
- Confirm the generated rows are unlabeled `Structural Plane` candidates and show confirming view count, feature support, and confidence; compare them with `structural-planes/planes-overlay.jpg`.
- Select several inconsistent candidates and confirm **Delete Selected** removes them together; rename or redraw a retained plane and confirm it persists after restart.
- Leave Camera Control and close both calibration popups. Confirm tracking and calibration APIs report `idle`, analyzed-frame counts stop increasing, camera preview conversion is suspended, and no background inference CPU remains.
- Reopen the plane popup and confirm Audience relocalization wakes and locks; close it and confirm it returns to `idle`. Enable person detection while Camera Control is visible and confirm only the selected analysis sources wake.

## VISCA

- Listener starts on configured UDP port.
- Raw VISCA packets receive ACK and completion.
- VISCA-over-IP packets preserve sequence in ACK and completion.
- Pan/tilt translates to `#PTS`.
- Zoom translates to `#Z`.
- Focus translates to `#F`.
- Preset recall/save translate to `#R` and `#M`.
- Tenveo menu, home, autofocus, manual focus, and AWB commands translate correctly.
- Port conflicts are surfaced without terminating another process.

## Browser Remotes

- `index.html` works against Production Hub with local API base.
- `control.html` works against Production Hub with local API base.
- `score.html` works against Production Hub with local API base.
- Remote page URLs appear in the desktop Remote Pages section.
- Request history appears in Diagnostics.

## Scoreboard

- `GET /score` returns rows and history.
- `POST /score` updates rows.
- Revision increments on each update.
- Conflicting revision-aware updates return 409.
- Score state persists after app restart.
- Undo history is preserved.

## Networking and Security

- Local-only binding is the default.
- LAN binding requires explicit configuration.
- Privileged token settings are visible.
- Passwords and tokens are not written to logs.
- Caller IP and route are recorded for remote API requests.
