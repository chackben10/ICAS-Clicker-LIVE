#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def default_output_dir() -> Path:
    return PROJECT_ROOT / "debug_logs" / "propresenter"


def timestamp() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def file_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Record every read from ProPresenter's "
            "/v1/presentation/active?chunked=true stream."
        )
    )
    parser.add_argument(
        "--url",
        default="",
        help="Full stream URL. Defaults to the configured ProPresenter host/port.",
    )
    parser.add_argument("--host", default="", help="Override ProPresenter host from app config.")
    parser.add_argument("--port", type=int, default=0, help="Override ProPresenter port from app config.")
    parser.add_argument(
        "--endpoint",
        default="/presentation/active?chunked=true",
        help=(
            "ProPresenter v1 endpoint path when --url is not supplied. "
            "Defaults to /presentation/active?chunked=true."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_output_dir(),
        help="Directory for JSONL and raw capture files.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0,
        help="Seconds to record. Defaults to run until Ctrl-C.",
    )
    parser.add_argument(
        "--read-size",
        type=int,
        default=1,
        help="Bytes per read from the stream. Use 1 for maximum boundary detail.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30,
        help="Connection/read timeout in seconds.",
    )
    return parser.parse_args(argv)


def configured_base_url() -> str:
    try:
        from production_hub.core.config.models import AppPaths
        from production_hub.core.config.repository import ConfigRepository, default_app_root
    except Exception as exc:
        raise SystemExit(f"Could not import Production Hub config loader: {exc}") from exc

    repo = ConfigRepository(AppPaths(default_app_root()))
    config = repo.load_app_config().integrations.propresenter
    return config.base_url


def build_url(args: argparse.Namespace) -> str:
    if args.url.strip():
        return args.url.strip()

    base_url = configured_base_url()
    parsed = urllib.parse.urlparse(base_url)
    host = args.host.strip() or parsed.hostname or "localhost"
    port = args.port or parsed.port or 49232
    scheme = parsed.scheme or "http"
    endpoint = str(args.endpoint or "").strip() or "/presentation/active?chunked=true"
    endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
    return f"{scheme}://{host}:{port}/v1{endpoint}"


def decode_line(line: bytes) -> tuple[str, Any | None]:
    text = line.decode("utf-8", errors="replace").strip()
    if not text:
        return text, None
    candidate = text
    if candidate.startswith("data:"):
        candidate = candidate.removeprefix("data:").strip()
    try:
        return text, json.loads(candidate)
    except json.JSONDecodeError:
        return text, None


def write_jsonl(handle, payload: dict[str, Any]) -> None:
    handle.write(json.dumps(payload, sort_keys=True) + "\n")
    handle.flush()


def summarize_json(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    presentation_index = value.get("presentation_index")
    if not isinstance(presentation_index, dict):
        return ""
    presentation_id = presentation_index.get("presentation_id")
    uuid = ""
    if isinstance(presentation_id, dict):
        uuid = str(presentation_id.get("uuid") or "")
    index = presentation_index.get("index")
    return f" index={index} uuid={uuid}" if uuid or index is not None else ""


def record_stream(args: argparse.Namespace) -> int:
    url = build_url(args)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = file_stamp()
    safe_endpoint = str(args.endpoint or "url").strip("/").replace("/", "-").replace("?", "-").replace("&", "-")
    jsonl_path = args.output_dir / f"{safe_endpoint}-stream-{stamp}.jsonl"
    raw_path = args.output_dir / f"{safe_endpoint}-stream-{stamp}.raw"

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json, text/event-stream, */*",
            "Cache-Control": "no-cache",
        },
    )

    print(f"Opening {url}")
    print(f"Writing events: {jsonl_path}")
    print(f"Writing raw bytes: {raw_path}")
    print("Press/retrigger slides now. Stop with Ctrl-C.")

    start = time.monotonic()
    read_size = max(1, int(args.read_size))
    line_buffer = b""
    chunk_number = 0

    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            headers = dict(response.headers.items())
            with jsonl_path.open("w", encoding="utf-8") as jsonl, raw_path.open("wb") as raw:
                write_jsonl(
                    jsonl,
                    {
                        "type": "connection_open",
                        "timestamp": timestamp(),
                        "url": url,
                        "status": response.status,
                        "headers": headers,
                    },
                )
                print(f"Connected: HTTP {response.status} {headers.get('content-type', '')}")

                while True:
                    if args.duration and time.monotonic() - start >= args.duration:
                        break

                    chunk = response.read(read_size)
                    if not chunk:
                        write_jsonl(jsonl, {"type": "connection_closed", "timestamp": timestamp()})
                        print("Connection closed by server.")
                        break

                    chunk_number += 1
                    raw.write(chunk)
                    raw.flush()
                    payload = {
                        "type": "chunk",
                        "timestamp": timestamp(),
                        "elapsed_seconds": round(time.monotonic() - start, 3),
                        "chunk_number": chunk_number,
                        "byte_count": len(chunk),
                        "hex": chunk.hex(),
                        "text": chunk.decode("utf-8", errors="replace"),
                    }
                    write_jsonl(jsonl, payload)

                    line_buffer += chunk
                    while b"\n" in line_buffer:
                        line, line_buffer = line_buffer.split(b"\n", 1)
                        text, parsed = decode_line(line)
                        line_payload = {
                            "type": "line",
                            "timestamp": timestamp(),
                            "elapsed_seconds": round(time.monotonic() - start, 3),
                            "text": text,
                            "json": parsed,
                        }
                        write_jsonl(jsonl, line_payload)
                        if text or parsed is not None:
                            print(f"{line_payload['timestamp']} line={text!r}{summarize_json(parsed)}")

                if line_buffer:
                    text, parsed = decode_line(line_buffer)
                    write_jsonl(
                        jsonl,
                        {
                            "type": "partial_line",
                            "timestamp": timestamp(),
                            "elapsed_seconds": round(time.monotonic() - start, 3),
                            "text": text,
                            "json": parsed,
                        },
                    )
    except KeyboardInterrupt:
        print("\nStopped.")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"Stream failed: {exc}", file=sys.stderr)
        return 2

    print(f"Saved: {jsonl_path}")
    print(f"Saved: {raw_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return record_stream(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
