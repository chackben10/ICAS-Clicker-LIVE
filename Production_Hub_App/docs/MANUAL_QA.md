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

