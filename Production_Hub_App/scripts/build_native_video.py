#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = APP_ROOT / "native" / "ndi_receiver"
DEFAULT_OUTPUT = APP_ROOT / ".build" / "native" / "libProductionHubNDI.dylib"


def build(output: Path = DEFAULT_OUTPUT) -> Path:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "/usr/bin/clang",
        "-std=c11",
        "-O2",
        "-fvisibility=hidden",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-dynamiclib",
        "-mmacosx-version-min=13.0",
        "-DPRODUCTION_HUB_NDI_EXPORTS",
        "-I",
        str(SOURCE_ROOT),
        str(SOURCE_ROOT / "ProductionHubNDI.c"),
        "-install_name",
        "@rpath/libProductionHubNDI.dylib",
        "-o",
        str(output),
    ]
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        details = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
        raise RuntimeError(f"Could not build the Production Hub NDI bridge:\n{details}")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Production Hub's in-process NDI receiver bridge.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        output = build(args.output)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
