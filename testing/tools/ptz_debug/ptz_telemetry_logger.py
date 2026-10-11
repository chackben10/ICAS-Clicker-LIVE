"""Sidecar logger: samples Production Hub Dev's tracking + PTZ automation state.

Writes one JSON object per line with wall-clock UTC and monotonic timestamps so
it can be aligned with a Diagnostic Recording (manifest.json "started_at").
Reads only the Hub's local API; never touches ProPresenter.
"""

from __future__ import annotations

import json
import signal
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

BASE = "http://127.0.0.1:5000"
ENDPOINTS = {"tracking": "/api/camera/tracking", "automation": "/api/camera/automation"}
INTERVAL = 0.1

running = True


def _stop(*_args) -> None:
    global running
    running = False


def fetch(path: str) -> object:
    with urllib.request.urlopen(BASE + path, timeout=1.0) as response:
        return json.load(response)


def main() -> None:
    out = Path(sys.argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    samples = errors = 0
    next_tick = time.monotonic()
    with out.open("a", buffering=1) as handle:
        while running:
            record: dict[str, object] = {
                "wall_utc": datetime.now(UTC).isoformat(),
                "monotonic": time.monotonic(),
            }
            for key, path in ENDPOINTS.items():
                try:
                    record[key] = fetch(path)
                except Exception as exc:  # keep logging through app hiccups
                    record[f"{key}_error"] = str(exc)
                    errors += 1
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
            samples += 1
            next_tick += INTERVAL
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()
    print(f"stopped: {samples} samples, {errors} fetch errors -> {out}")


if __name__ == "__main__":
    main()
