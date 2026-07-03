# Migration Guide

This guide moves the current production-control stack into Production Hub.

## 1. Install and First Launch

1. Install dependencies from `Production_Hub_App/requirements.txt`.
2. Start Production Hub with `python3 Production_Hub_App/main.py`.
3. Confirm the app creates its config/state/log directories.
4. Open Overview and Diagnostics.
5. Leave browser remotes pointed at the current production URLs until local tests pass.

## 2. Replace Hammerspoon HTTP Server

Production Hub implements the compatibility routes used by `index.html` and
`control.html`, including slide navigation, focus, thumbnails, macros, presets,
timers, audio, Auto Show, OBS look refresh, and health.

Migration steps:

1. Start Production Hub on `127.0.0.1:1337`.
2. Update a local copy of `index.html` and `control.html` to use
   `http://127.0.0.1:1337` as the API base.
3. Test `/health`, `/active-presentation`, `/slide-index`, `/next`, `/previous`,
   `/macro`, `/preset`, and audio routes.
4. Disable the Hammerspoon HTTP server after browser compatibility is confirmed.

## 3. Replace Node OBS Bridge

Production Hub connects to OBS WebSocket directly through Python. The migrated
configuration seeds:

- Host `192.168.1.156`
- Port `4455`
- Main layout scene `ProPresenter Input`
- Transition policy from the Hammerspoon file
- All current look-to-source visibility rules
- Source IDs 72, 77, 81, 73, 78, 79, 75, 74, 76, and 82 with readable labels

Migration steps:

1. Confirm OBS WebSocket is enabled in OBS.
2. Confirm password/auth behavior matches the seeded blank password.
3. Use Diagnostics to test connection and scene discovery.
4. Call `/scene/current`, `/scene/items?scene=ProPresenter%20Input`, and
   `/obs/look/refresh`.
5. Stop the old Node `server.mjs` process.

## 4. Replace VISCA / Panasonic Bridge

Production Hub migrates Panasonic CGI and VISCA translation into separate
modules:

- `integrations/panasonic_awp/client.py`
- `integrations/panasonic_awp/service.py`
- `integrations/visca/parser.py`
- `integrations/visca/response_builder.py`
- `integrations/visca/command_mapper.py`
- `integrations/visca/udp_listener.py`

The duplicated parser flow from the old Tkinter script is replaced by one
packet parser and one command mapper.

Migration steps:

1. Confirm camera IP, username, and password in Settings or config.
2. Test Panasonic CGI with a non-destructive command.
3. Start VISCA listener on UDP `52383`.
4. Send test packets for pan/tilt, zoom, focus, preset recall, and Tenveo menu.
5. Stop the old Tkinter bridge after verification.

Production Hub never silently kills a blocking process. If the VISCA port is
in use, choose another port, enable shared-port mode, or inspect the blocking
process manually.

## 5. Restore Scoreboard Backend

Production Hub provides:

- `GET /score`
- `POST /score`
- Persistent `state/scoreboard.json`
- Revision numbers and conflict responses for newer clients
- Compatibility with the current `score.html` payload shape

Migration steps:

1. Start Production Hub.
2. Point a local score page at `http://127.0.0.1:1337`.
3. Add rows, change scores, refresh the page, and restart Production Hub.
4. Confirm score state recovers after restart.

## 6. LAN Access

Default binding is `127.0.0.1`. Enable LAN only after:

1. Selecting a LAN bind address.
2. Creating an access token for privileged endpoints.
3. Reviewing CORS allow-list settings.
4. Confirming Diagnostics logs remote caller IPs.

