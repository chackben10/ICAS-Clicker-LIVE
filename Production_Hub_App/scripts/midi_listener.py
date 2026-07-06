#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Print and log all incoming MIDI messages.")
    parser.add_argument("--list", action="store_true", help="List visible MIDI input ports and exit.")
    parser.add_argument("--input", default="", help="Input port name or partial name. Defaults to first visible input.")
    parser.add_argument("--log", type=Path, default=Path("midi_debug.log"), help="Output log path.")
    parser.add_argument("--json", action="store_true", help="Write one JSON object per MIDI message.")
    parser.add_argument("--no-map", action="store_true", help="Do not include Production Hub default mapping hints.")
    return parser.parse_args(argv)


def import_mido():
    try:
        import mido
    except Exception as exc:
        print(f"mido/python-rtmidi is not available: {exc}", file=sys.stderr)
        print("Install with: python3 -m pip install mido python-rtmidi", file=sys.stderr)
        raise SystemExit(2)
    return mido


def select_input(names: list[str], wanted: str) -> str:
    wanted = wanted.strip()
    if wanted and wanted in names:
        return wanted
    if wanted:
        partial = next((name for name in names if wanted.lower() in name.lower()), "")
        if partial:
            return partial
        print(f"No MIDI input matched {wanted!r}. Visible inputs:", file=sys.stderr)
        for name in names:
            print(f"  - {name}", file=sys.stderr)
        raise SystemExit(1)
    if names:
        return names[0]
    print("No MIDI input ports are visible.", file=sys.stderr)
    raise SystemExit(1)


def message_payload(input_name: str, message: Any) -> dict[str, Any]:
    payload = {
        "timestamp": datetime.now().isoformat(timespec="milliseconds"),
        "input": input_name,
        "type": getattr(message, "type", ""),
        "raw": str(message),
    }
    for attr in ("channel", "note", "velocity", "control", "value", "program", "pitch", "time"):
        if hasattr(message, attr):
            payload[attr] = getattr(message, attr)
    return payload


def add_mapping_hint(payload: dict[str, Any]) -> None:
    if payload.get("type") != "note_on" or "note" not in payload:
        return
    try:
        from production_hub.core.config.defaults import build_default_midi_mappings
        from production_hub.integrations.midi.mapping_service import MidiMappingService
    except Exception:
        return
    service = MidiMappingService(build_default_midi_mappings())
    action = service.action_for("note_on", int(payload.get("channel", -1)), int(payload["note"]))
    if not action:
        payload["mapped"] = False
        return
    payload["mapped"] = True
    payload["mapped_action"] = action.action_type
    payload["mapped_playlist"] = action.params.get("playlist", "")
    payload["mapped_track"] = action.params.get("track", "")


def format_line(payload: dict[str, Any], json_lines: bool) -> str:
    if json_lines:
        return json.dumps(payload, sort_keys=True)
    extras = []
    for key in ("channel", "note", "velocity", "control", "value", "program", "pitch"):
        if key in payload:
            extras.append(f"{key}={payload[key]}")
    if payload.get("mapped"):
        extras.append(f"mapped={payload.get('mapped_playlist')} / {payload.get('mapped_track')}")
    elif payload.get("mapped") is False:
        extras.append("mapped=no")
    suffix = f" {' '.join(extras)}" if extras else ""
    return f"{payload['timestamp']} input={payload['input']} type={payload['type']}{suffix} raw={payload['raw']}"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    mido = import_mido()

    try:
        names = list(mido.get_input_names())
    except Exception as exc:
        print(f"Could not read MIDI input ports: {exc}", file=sys.stderr)
        return 2

    if args.list:
        print("Visible MIDI inputs:")
        for name in names:
            print(f"  - {name}")
        return 0

    input_name = select_input(names, args.input)
    stop = False

    def request_stop(_signum, _frame) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    args.log.parent.mkdir(parents=True, exist_ok=True)
    print(f"Listening on: {input_name}")
    print(f"Writing log: {args.log.resolve()}")
    print("Press Ctrl+C to stop.")

    with args.log.open("a", encoding="utf-8") as log_file, mido.open_input(input_name) as port:
        log_file.write(f"\n--- MIDI listener started {datetime.now().isoformat(timespec='seconds')} input={input_name} ---\n")
        log_file.flush()
        while not stop:
            for message in port.iter_pending():
                payload = message_payload(input_name, message)
                if not args.no_map:
                    add_mapping_hint(payload)
                line = format_line(payload, args.json)
                print(line, flush=True)
                log_file.write(line + "\n")
                log_file.flush()
            time.sleep(0.01)
    print("Stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
