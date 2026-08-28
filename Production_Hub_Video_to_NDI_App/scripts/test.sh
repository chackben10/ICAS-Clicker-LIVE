#!/bin/bash

set -euo pipefail

APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LIVE_NDI_TEST=true
if [[ "${1:-}" == "--skip-live-ndi" ]]; then
    LIVE_NDI_TEST=false
fi

# Prefer a full Xcode toolchain when it is installed but the machine is still
# pointed at Command Line Tools. CI images and developer Macs commonly have
# this layout, and selecting it per-process avoids mutating xcode-select.
if [[ -z "${DEVELOPER_DIR:-}" && -d /Applications/Xcode.app/Contents/Developer ]]; then
    export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
fi

# Clang/Swift module caches are not portable across toolchain upgrades. Keeping
# them versioned prevents an older cached PackageDescription module from making
# a healthy updated toolchain look broken.
SWIFT_VERSION_TEXT="$(swift --version 2>&1)"
SWIFT_CACHE_KEY="$(printf '%s' "$SWIFT_VERSION_TEXT" | tr -cs 'A-Za-z0-9._-' '_')"
export CLANG_MODULE_CACHE_PATH="$APP_ROOT/.build/module-cache/$SWIFT_CACHE_KEY"
export SWIFTPM_MODULECACHE_OVERRIDE="$APP_ROOT/.build/module-cache/$SWIFT_CACHE_KEY"

cd "$APP_ROOT"

echo "Validating the selected SwiftPM toolchain…"
MANIFEST_LOG="$APP_ROOT/.build/swift-manifest-preflight.log"
DEVELOPER_ROOT="${DEVELOPER_DIR:-$(xcode-select -p 2>/dev/null || true)}"
mkdir -p "$APP_ROOT/.build"
if ! swift package dump-package > /dev/null 2> "$MANIFEST_LOG"; then
    # Some Command Line Tools updates have been observed leaving older private
    # PackageDescription interfaces beside the newly installed public ABI. Use
    # SwiftPM's supported custom-libraries hook with a repo-local copy that
    # contains only the public interfaces from that same Apple toolchain.
    MANIFEST_SOURCE="$DEVELOPER_ROOT/usr/lib/swift/pm/ManifestAPI"
    CUSTOM_LIBS_ROOT="$APP_ROOT/.build/swiftpm-libs/$SWIFT_CACHE_KEY"
    if [[ -d "$MANIFEST_SOURCE" ]] && find "$MANIFEST_SOURCE" -name '*.private.swiftinterface' -print -quit | grep -q .; then
        mkdir -p "$CUSTOM_LIBS_ROOT"
        cp -R "$MANIFEST_SOURCE" "$CUSTOM_LIBS_ROOT/"
        while IFS= read -r PRIVATE_INTERFACE; do
            mv "$PRIVATE_INTERFACE" "$PRIVATE_INTERFACE.disabled"
        done < <(find "$CUSTOM_LIBS_ROOT/ManifestAPI" -name '*.private.swiftinterface' -type f)
        export SWIFTPM_CUSTOM_LIBS_DIR="$CUSTOM_LIBS_ROOT"
    fi
    if ! swift package dump-package > /dev/null 2> "$MANIFEST_LOG"; then
        echo "SWIFT ENVIRONMENT FAILURE: the selected toolchain cannot load Package.swift." >&2
        echo "Selected developer directory: ${DEVELOPER_ROOT:-<unavailable>}" >&2
        echo "Install/select a compatible full Xcode, or repair Command Line Tools." >&2
        tail -n 40 "$MANIFEST_LOG" >&2
        exit 3
    fi
fi

SWIFT_INCLUDE_ROOT="$DEVELOPER_ROOT/usr/include/swift"
if [[ -f "$SWIFT_INCLUDE_ROOT/module.modulemap" && -f "$SWIFT_INCLUDE_ROOT/bridging.modulemap" ]] \
    && grep -q 'module SwiftBridging' "$SWIFT_INCLUDE_ROOT/module.modulemap" \
    && grep -q 'module SwiftBridging' "$SWIFT_INCLUDE_ROOT/bridging.modulemap"; then
    echo "SWIFT ENVIRONMENT FAILURE: duplicate SwiftBridging module maps were found." >&2
    echo "The selected Apple toolchain contains stale files from an older Command Line Tools release." >&2
    echo "Install/select a full compatible Xcode or repair Command Line Tools before running Swift tests." >&2
    exit 3
fi

echo "Running Swift unit tests…"
if ! swift test --jobs "${SWIFT_TEST_JOBS:-1}"; then
    echo "Swift unit tests failed. A successful app build is not a test pass." >&2
    exit 2
fi
echo "Swift unit tests passed."

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
