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
  native/ndi_receiver/ narrow bridge to the installed official NDI runtime
  state/               runtime state repository
```

## Video Pipeline

```text
Audience NDI worker ─┐
                     ├─> one-frame broker ─> throttled Qt previews
Qt PTZ capture ──────┘          │
                                └─> bounded Qt recording/replay pipeline
```

The native bridge isolates the NDI C ABI from Python and is built into the app
bundle. The official NDI runtime remains a system dependency. NDI discovery,
receive, pixel copying, local capture conversion, encoding, and decoding run in
native or worker contexts rather than the Qt UI thread. The UI only submits
prepared frames to Qt Multimedia. Consumers poll immutable frame snapshots;
producers replace the one pending frame instead of accumulating latency.

Video configuration is backward-compatible because older profiles receive the
typed `VideoConfig` defaults when the field is absent. Automated tracking and
camera-motion authority are intentionally outside the Phase 1 boundary.

## First Version Boundaries

This milestone creates a maintainable foundation and the compatibility backend
for existing browser pages. It intentionally keeps profile switching, keychain
storage, advanced QR generation, and rich per-integration editors as next-layer
work, while the data model is prepared for them.
