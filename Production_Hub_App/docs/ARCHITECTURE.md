# Architecture

Production Hub is organized around four shared primitives:

- Validated configuration and durable runtime state
- Integration services with health reporting
- Endpoint definitions and action execution
- Automation definitions and safety gates

Browser pages, automations, and future device modules should call endpoints or
the endpoint action engine rather than reaching into integrations directly.

## Package Map

```text
production_hub/
  app/                 bootstrap, lifecycle, CLI entry
  api/                 embedded FastAPI server and compatibility routes
  ui/                  PySide6 desktop admin interface
  core/config/         typed config, defaults, atomic repositories
  core/endpoints/      endpoint registry and sequential executor
  core/automation/     definitions, cooldowns, debounce, engine
  core/health/         health status models and monitor
  core/logging/        structured JSON logs
  integrations/        ProPresenter, OBS, Panasonic, VISCA, scoreboard, MIDI
  video/               bounded NDI/local capture, frame broker, recording/replay
  tracking/            Apple Vision, stage gating, framing geometry, guarded PTZ control
  calibration/         alignment, curation registry, live relocalization, confidence gates
  native/ndi_receiver/ narrow bridge to the installed official NDI runtime
  state/               runtime state repository
```

## Video Pipeline

```text
Audience: NDI or local ─┐
                        ├─> one-frame broker ─> throttled Qt previews
PTZ: NDI or local ──────┘          │
                                   ├─> bounded Qt recording/replay pipeline
                                   └─> latest-only Apple Vision worker
                                                 │
                                                 └─> overlays + subject selection

Audience preview ─> normalized semantic polygons (Stage / Front / Altar / Custom)
                         └─> validated config + read-only regions API

Standalone calibration CLI ─> multi-frame SIFT correspondence models
                             └─> consensus cluster + MAGSAC homography
                                  └─> immutable JSON and visual review artifacts

Approved marker atlas ─> latest-only low-rate relocalization worker
                       ├─> Locked / Degraded / Lost fail-closed health
                       ├─> live marker positions + read-only API
                       └─> calibration-reference scene planes projected into live video

Live Stage subjects + semantic regions ─> pure framing engine
                                      └─> shadow recommendation
                                           └─> runtime-only safety supervisor
                                                └─> bounded absolute Panasonic commands
```

The native bridge isolates the NDI C ABI from Python and is built into the app
bundle. The official NDI runtime remains a system dependency. NDI discovery,
receive, pixel copying, local capture conversion, encoding, and decoding run in
native or worker contexts rather than the Qt UI thread. The UI only submits
prepared frames to Qt Multimedia. Consumers poll immutable frame snapshots;
producers replace the one pending frame instead of accumulating latency.

Video and tracking configuration are backward-compatible because older profiles receive the
typed `VideoConfig` defaults when the field is absent. Each logical slot stores
its source type plus independent NDI and local identifiers, so switching source
types does not discard the previous selection. `CameraTrackingConfig` is
disabled by default and separately controls each source, analysis rate,
confidence floor, image-size ceiling, and short-gap association limits. The
same typed configuration stores named scene polygons in normalized source
coordinates so empty-room setup survives restarts and resolution changes.

The tracking, relocalization, and framing engine have no Panasonic dependency and therefore cannot move a
camera. Tracking consumes immutable broker snapshots, runs one native Apple Vision
request at a time, discards superseded frames, and publishes immutable tracking
snapshots. Phase 2 scene polygons describe semantic areas only; they do not
claim calibrated physical geometry. Phase 3 adds low-rate Audience drift
compensation and calibrated semantic regions. Phase 4 adds pure shadow framing
decisions. Phase 5 grants runtime-only motion authority to a separate safety
supervisor after an explicit arm action.

The guarded Phase 3 calibration capture remains a separate process. Generated
maps are immutable; reversible review sidecars hold exclusions and approval,
and an active-map manifest provides rollback. Runtime relocalization consumes
only an approved map, uses the shared one-frame broker, and publishes immutable
health snapshots. Any stale frame, weak fit, or reference mismatch blocks
future motion consumers by setting `motionSafe=false`.

Both analysis workers have a separate multi-owner runtime activity gate. Configuration may
remain enabled across restarts, but enabled is not the same as active: person
inference and relocalization sleep without polling frames unless Camera Control
is actively using tracking or a calibration/review workflow is open. The video
service also excludes idle analysis from its output-activity calculation, so
NDI/local capture can suspend pixel conversion while receivers remain healthy.

Audience person candidates are filtered against drift-stabilized operational
Stage, Front Stage, Altar, and Podium polygons before association. The automatic
motion supervisor fails closed on stale video, tracking, calibration, target loss,
or manual takeover; it starts disarmed and never persists arming across restarts.
See `docs/PTZ_AUTOMATION_PHASE45.md`.

## First Version Boundaries

This milestone creates a maintainable foundation and the compatibility backend
for existing browser pages. It intentionally keeps profile switching, keychain
storage, advanced QR generation, and rich per-integration editors as next-layer
work, while the data model is prepared for them.
