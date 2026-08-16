# PTZ Automation · Phases 4 and 5

Production Hub separates automatic camera work into two authority levels:

- **Phase 4 / Shadow:** detect Stage subjects, select a composition, and display a recommended absolute pan/tilt/zoom pose. No camera command is permitted.
- **Phase 5 / Armed:** send rate-limited, bounded Panasonic absolute-position commands while every safety prerequisite remains healthy.

The application always starts disarmed. Arming is runtime-only and requires explicit operator confirmation. It is blocked unless Panasonic AWP is enabled, an approved Audience-to-PTZ map is active, live Audience relocalization is locked, Audience video and person analysis are fresh, and at least one operational Stage/Altar/Podium drawing is enabled.

## Framing modes

- **Selected Subject(s):** follows only orange-selected subjects. Multiple selected subjects are framed together.
- **Everyone on Stage:** frames stable subjects whose reference-coordinate floor point is in a Stage, Front Stage, or Podium drawing.
- **Stage + Altar:** adds Altar drawings to the group area.
- **Click to Frame:** maps an operator click in the drifting live Audience image back to the approved calibration reference and frames that point.

Audience detections outside enabled Stage, Front Stage, Altar, and Podium drawings are suppressed before identity association. Apple Vision body-pose joints enrich the human rectangle when available, so a raised hand or visible feet enlarge the requested composition without a second ML runtime. The UI and `/api/camera/tracking` report both raw and suppressed candidate counts.

The podium/stand option uses a closer composition for one subject inside a Podium drawing. Away from a podium it expands an upper-body observation toward a near-full-body stage composition. Group movement expands the common envelope and zooms out.

## Motion supervisor

The armed controller:

- queries actual pan, tilt, and zoom before issuing movement;
- uses only absolute Panasonic commands;
- applies pan, tilt, and zoom deadbands;
- limits every command to a small configured motor step;
- enforces a minimum command interval and target dwell;
- smooths changing recommendations;
- holds on brief target loss and disarms on sustained loss;
- disarms if calibration lock, video freshness, tracking freshness, or Panasonic health is lost;
- disarms when UI controls, presets, endpoint actions, VISCA, calibration, the global STOP action, or another external movement takes over.

The Panasonic CGI client serializes requests from calibration, manual controls, and automation so the camera is never flooded by overlapping requests.

## API

Read-only state:

- `GET /api/camera/tracking`
- `GET /api/camera/calibration`
- `GET /api/camera/regions`
- `GET /api/camera/automation`

Privileged controls:

- `POST /api/camera/automation/configure`
- `POST /api/camera/automation/click-target`
- `POST /api/camera/automation/arm`
- `POST /api/camera/automation/disarm`

Privileged routes use the existing bearer token or `X-Production-Hub-Token` policy. A LAN-enabled server fails authorization closed when no valid token is supplied.

## First live test

1. Open Camera Control and wait for Audience calibration to say **Locked · mapping safe**.
2. Open Scene Drawing Review. Keep only accurate operational Stage/Altar/Podium areas enabled.
3. Enable person detection and verify the Audience status reports congregants as **outside-stage suppressed**.
4. Choose a mode and watch the shadow recommendation. Do not arm yet.
5. Test Click to Frame at several stage locations and confirm the red target rectangle is sensible.
6. With the PTZ output off-air, choose one stationary target, arm, and keep the global **STOP / Disarm Automatic PTZ** action ready (`Ctrl+Shift+X`).
7. Confirm each movement is small and convergent before trying walking subjects, groups, or podium framing.

Do not approve a new calibration merely to make arming succeed. Review and approve it independently first.
