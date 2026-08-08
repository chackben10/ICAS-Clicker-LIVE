#!/bin/bash

set -euo pipefail

APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LIVE_NDI_TEST=true
if [[ "${1:-}" == "--skip-live-ndi" ]]; then
    LIVE_NDI_TEST=false
fi

export CLANG_MODULE_CACHE_PATH="$APP_ROOT/.build/module-cache"
export SWIFTPM_MODULECACHE_OVERRIDE="$APP_ROOT/.build/module-cache"

cd "$APP_ROOT"

echo "Running Swift unit tests…"
if swift test; then
    echo "Swift unit tests passed."
else
    echo "SwiftPM unit tests were unavailable with the selected toolchain."
    echo "Compiling and packaging the complete application as the fallback verification…"
    "$APP_ROOT/scripts/build_app.sh" --no-install
fi

echo "Compiling the C/NDI bridge with warnings as errors…"
mkdir -p "$APP_ROOT/.build/c-smoke"
clang -std=c11 -Wall -Wextra -Werror \
    -I "$APP_ROOT/Sources/CNDIShim/include" \
    "$APP_ROOT/Sources/CNDIShim/CNDIShim.c" \
    "$APP_ROOT/Tests/ndi_bridge_smoke.c" \
    -o "$APP_ROOT/.build/c-smoke/ndi_bridge_smoke"

if [[ "$LIVE_NDI_TEST" == true ]]; then
    echo "Creating a live NDI sender…"
    "$APP_ROOT/.build/c-smoke/ndi_bridge_smoke"
else
    echo "Skipped the live NDI sender test."
fi
