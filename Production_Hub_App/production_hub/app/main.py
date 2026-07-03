from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from pathlib import Path

from production_hub.app.bootstrap import build_context
from production_hub.app.lifecycle import start_api_server, startup_checks


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="production-hub")
    parser.add_argument("--api-only", action="store_true", help="Run the API/background service without the desktop UI.")
    parser.add_argument("--no-api", action="store_true", help="Open the desktop UI without starting the embedded API server.")
    parser.add_argument("--data-dir", type=Path, default=None, help="Override Production Hub's config/state/log directory.")
    parser.add_argument("--host", default=None, help="Override API bind host for this run.")
    parser.add_argument("--port", type=int, default=None, help="Override API port for this run.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    context = build_context(args.data_dir)
    if args.host:
        context.config.api.bind_host = args.host
    if args.port:
        context.config.api.port = args.port

    try:
        asyncio.run(startup_checks(context))
    except Exception as exc:
        context.logger.warning("startup_checks_failed", "Startup checks did not complete", error=str(exc))

    api_handle = None
    if not args.no_api:
        try:
            api_handle = start_api_server(context)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            if "required to run" in str(exc):
                print("Install dependencies with: python3 -m pip install -r Production_Hub_App/requirements.txt", file=sys.stderr)
            return 2

    if args.api_only:
        print(f"Production Hub API running at {context.config.api.base_url}")
        stop = False

        def _stop(_signum, _frame) -> None:
            nonlocal stop
            stop = True

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
        while not stop:
            time.sleep(0.25)
        if api_handle:
            api_handle.stop()
        return 0

    try:
        from production_hub.ui.main_window import run_desktop_app
    except Exception as exc:
        print("PySide6 is required for the Production Hub desktop UI.", file=sys.stderr)
        print(f"Import error: {exc}", file=sys.stderr)
        print("Install dependencies with: python3 -m pip install -r Production_Hub_App/requirements.txt", file=sys.stderr)
        if api_handle:
            api_handle.stop()
        return 2

    code = run_desktop_app(context, api_handle)
    if api_handle:
        api_handle.stop()
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
