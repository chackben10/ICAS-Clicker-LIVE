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
  state/               runtime state repository
```

## First Version Boundaries

This milestone creates a maintainable foundation and the compatibility backend
for existing browser pages. It intentionally keeps profile switching, keychain
storage, advanced QR generation, and rich per-integration editors as next-layer
work, while the data model is prepared for them.

