# Production Hub

Production Hub is the macOS-first backend control plane for ICAS production remotes.
It hosts the API used by the existing browser pages, persists configuration/state,
and provides a desktop admin interface for setup, diagnostics, and health checks.

## Install

```bash
cd Production_Hub_App
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

The current development environment did not have FastAPI, Uvicorn, PySide6, or
obsws-python installed, so the core modules and unit tests are dependency-light,
while the API/UI activate after installing `requirements.txt`.

## Run

Desktop app plus embedded API:

```bash
cd Production_Hub_App
source .venv/bin/activate
python3 main.py
```

API/background service only:

```bash
cd Production_Hub_App
source .venv/bin/activate
python3 main.py --api-only
```

Development data directory:

```bash
python3 main.py --api-only --data-dir ./dev-data
```

Default API binding is local-only:

```text
http://127.0.0.1:1337
```

## Build And Install The macOS App

Build `Production Hub.app` and update `/Applications/Production Hub.app`:

```bash
python3 Production_Hub_App/scripts/build_macos_app.py --install-deps --install
```

After the first dependency install, you can usually rebuild with:

```bash
python3 Production_Hub_App/scripts/build_macos_app.py --install
```

The generated app is written to:

```text
Production_Hub_App/dist/Production Hub.app
```

When `--install` is used, the script replaces the existing app at:

```text
/Applications/Production Hub.app
```

If macOS denies write access to `/Applications`, rerun the same command with
`sudo` or pass a writable destination:

```bash
python3 Production_Hub_App/scripts/build_macos_app.py --install --install-destination ~/Applications
```

## Existing Remote Page URLs

Production Hub serves the current browser pages at:

```text
http://127.0.0.1:1337/remote/index.html
http://127.0.0.1:1337/remote/control.html
http://127.0.0.1:1337/remote/score.html
```

`score.html` now prefers the current Production Hub origin when served from
`/remote/score.html`. It also supports `?api=http://host:port` and the
`icas-score-api-base` localStorage key for manual overrides.

The other current HTML files still contain ICAS hosted defaults. For local
testing, change their API base constants to `http://127.0.0.1:1337` or add the
same base-URL selector pattern.

## Desktop Pages

The desktop UI includes:

- Overview
- Endpoints, with editable endpoint definition JSON
- Automations, with editable automation definition JSON and pause/resume
- Integrations
- Camera Control, including Panasonic system controls, PTZ/lens controls,
  VISCA settings, and preset recall/save/rename
- Scoreboard, with native row editing, per-row score controls, queue controls,
  local action history, and undo
- Remote Pages
- Data & Storage
- Diagnostics
- Extensions
- Settings

## App Icon

For a Production Hub icon, keep the Apple Icon Composer source at
`Production_Hub_App/Production_hub.icon`. That source keeps the Liquid Glass
layers, translucency, dark appearance, and platform metadata intact.

Qt cannot load that `.icon` folder directly for the live window/menu-bar icon.
The custom runtime and bundle icon now use native `.icns` files only:

```text
Production_Hub_App/assets/ProductionHub.icns
Production_Hub_App/assets/production_hub_icon.icns
```

Recommended workflow:

```text
Production_hub.icon     Apple Icon Composer source
ProductionHub.icns      exported macOS app icon file
```

The macOS build script first looks for `Production_Hub_App/assets/ProductionHub.icns`.
If that file does not exist, it tries to render one from
`Production_Hub_App/Production_hub.icon` using Apple Icon Composer's bundled
`ictool` and macOS `iconutil`.

To generate only the `.icns` file:

```bash
python3 Production_Hub_App/scripts/build_macos_app.py --icon-only
```

If `ictool` cannot open the `.icon` document, the build stops instead of using
the flat PNG fallback. Open the `.icon` in Icon Composer, re-save it, and rerun
the script.

## Application Preferences

Settings now includes preferences for:

- Keeping Production Hub running when the main window is closed
- Showing a macOS menu-bar status icon
- Saving a launch-at-login preference

The close-window behavior and menu-bar icon are active in the desktop app.
Launch-at-login is saved as a preference for now; installing the LaunchAgent
should be done during packaging, when the final app bundle identifier and
executable path are known.

## Compatibility Routes

Implemented routes include:

- `GET /health`
- `GET /active-presentation`
- `GET /slide-index`
- `GET /thumbnail?uuid=...&index=...`
- `GET|POST /focus?index=...`
- `GET|POST /next`
- `GET|POST /previous`
- `GET|POST /prev`
- `GET /current-base`
- `GET /service_logos`
- `GET /macros`
- `POST /macro`
- `POST /preset`
- `GET /audio/playlists`
- `GET /audio/tracks?playlist=...`
- `POST /audio/trigger`
- `POST /audio/clear`
- `GET /audio/active`
- `GET /auto-show`
- `POST /auto-show`
- `GET /score`
- `POST /score`
- OBS bridge compatibility routes such as `/scene/current`, `/scene/set`,
  `/scene/items`, `/scene/items/apply`, `/obs/look/refresh`, and `/debug`

## Storage

By default, Production Hub stores data in:

```text
~/Library/Application Support/Production Hub/
```

Set `PRODUCTION_HUB_HOME` or pass `--data-dir` to override this. The app creates:

- `config/default_profile.json`
- `config/endpoints.json`
- `config/automations.json`
- `state/runtime_state.json`
- `state/scoreboard.json`
- `logs/production-hub-YYYY-MM-DD.log`
- `backups/automatic/`
- `backups/manual/`

Writes are atomic and existing files are backed up before replacement.

## Tests

These tests use the Python standard library `unittest` runner:

```bash
cd Production_Hub_App
python3 -m unittest discover production_hub/tests
```

They cover configuration seeding/backups, endpoint sequencing, automation
cooldown/debounce behavior, audio normalization, OBS look rule mapping,
Panasonic CGI URL construction, VISCA parsing/mapping/responses, and scoreboard
revision conflict handling.
