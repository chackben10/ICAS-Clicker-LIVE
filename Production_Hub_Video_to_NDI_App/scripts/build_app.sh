#!/bin/bash

set -euo pipefail

INSTALL_AFTER_BUILD=true
case "${1:-}" in
    "") ;;
    --no-install) INSTALL_AFTER_BUILD=false ;;
    -h|--help)
        echo "Usage: $0 [--no-install]"
        echo "Builds and signs the app, then installs it in /Applications by default."
        exit 0
        ;;
    *)
        echo "Unknown option: $1" >&2
        echo "Usage: $0 [--no-install]" >&2
        exit 2
        ;;
esac

APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPOSITORY_ROOT="$(cd "$APP_ROOT/.." && pwd)"
PRODUCT_NAME="Production Hub - Video to NDI"
OUTPUT_BUNDLE="$APP_ROOT/dist/$PRODUCT_NAME.app"
INSTALL_BUNDLE="/Applications/$PRODUCT_NAME.app"
STAGING_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/production-hub-video-to-ndi.XXXXXX")"
STAGING_BUNDLE="$STAGING_ROOT/$PRODUCT_NAME.app"

cleanup() {
    rm -rf "$STAGING_ROOT"
}
trap cleanup EXIT

export CLANG_MODULE_CACHE_PATH="$APP_ROOT/.build/module-cache"
export SWIFTPM_MODULECACHE_OVERRIDE="$APP_ROOT/.build/module-cache"

cd "$APP_ROOT"

MACHINE_ARCHITECTURE="$(uname -m)"
SDK_PATH="$(xcrun --show-sdk-path)"
COMPILER_BUILD="$(swiftc --version 2>&1 | sed -n 's/.*swiftlang-\([^ ]*\).*/\1/p' | head -1)"
USE_SWIFTPM=true
DIRECT_SWIFT_ARGUMENTS=(-sdk "$SDK_PATH")
if [[ "$MACHINE_ARCHITECTURE" == "arm64" ]]; then
    SDK_INTERFACE_ARCHITECTURE="arm64e"
else
    SDK_INTERFACE_ARCHITECTURE="$MACHINE_ARCHITECTURE"
fi
FOUNDATION_INTERFACE="$SDK_PATH/System/Library/Frameworks/Foundation.framework/Modules/Foundation.swiftmodule/$SDK_INTERFACE_ARCHITECTURE-apple-macos.swiftinterface"
if [[ -f "$FOUNDATION_INTERFACE" ]]; then
    SDK_BUILD="$(sed -n 's/.*swiftlang-\([^ ]*\).*/\1/p' "$FOUNDATION_INTERFACE" | head -1)"
    if [[ -n "$COMPILER_BUILD" && -n "$SDK_BUILD" && "$COMPILER_BUILD" != "$SDK_BUILD" ]]; then
        # A known partial Command Line Tools update can leave two conflicting
        # Swift bridging module maps beside an otherwise usable compiler. Give
        # the compiler an isolated resource path containing the current runtime
        # and one canonical map, then use the direct build below.
        USE_SWIFTPM=false
        TOOLCHAIN_USR="$(cd "$(dirname "$(xcrun --find swiftc)")/.." && pwd)"
        ISOLATED_USR="$APP_ROOT/.build/isolated-swift-resources/usr"
        mkdir -p "$ISOLATED_USR/lib" "$ISOLATED_USR/include/swift"
        ln -sfn "$TOOLCHAIN_USR/lib/swift" "$ISOLATED_USR/lib/swift"
        cp "$TOOLCHAIN_USR/include/swift/bridging" "$ISOLATED_USR/include/swift/bridging"
        cp "$TOOLCHAIN_USR/include/swift/bridging.modulemap" "$ISOLATED_USR/include/swift/module.modulemap"
        DIRECT_SWIFT_ARGUMENTS=(
            -resource-dir "$ISOLATED_USR/lib/swift"
            -sdk "$SDK_PATH"
        )
        echo "The selected Command Line Tools are partially mismatched; using the isolated direct build path."
    fi
fi

echo "Building Production Hub - Video to NDI…"
if [[ "$USE_SWIFTPM" == true ]] && swift build -c release --product VideoToNDIApp; then
    BIN_DIRECTORY="$(swift build -c release --show-bin-path)"
else
    # Some partial Command Line Tools installations ship a SwiftPM manifest
    # library that does not match their compiler. The compiler itself still
    # works, so keep a dependency-free manual build path for those Macs.
    if [[ "$USE_SWIFTPM" == true ]]; then
        echo "SwiftPM was unavailable; retrying with the direct Swift compiler…"
    else
        echo "Building with the direct Swift compiler…"
    fi
    case "$MACHINE_ARCHITECTURE" in
        arm64|x86_64) ;;
        *)
            echo "Unsupported Mac architecture: $MACHINE_ARCHITECTURE" >&2
            exit 1
            ;;
    esac
    TARGET="$MACHINE_ARCHITECTURE-apple-macosx13.0"
    BIN_DIRECTORY="$APP_ROOT/.build/manual-release"
    MANUAL_MODULE_CACHE="$BIN_DIRECTORY/module-cache-isolated"
    mkdir -p "$BIN_DIRECTORY" "$MANUAL_MODULE_CACHE"

    clang -std=c11 -Wall -Wextra -Werror -O2 \
        -arch "$MACHINE_ARCHITECTURE" \
        -mmacosx-version-min=13.0 \
        -I "$APP_ROOT/Sources/CNDIShim/include" \
        -c "$APP_ROOT/Sources/CNDIShim/CNDIShim.c" \
        -o "$BIN_DIRECTORY/CNDIShim.o"

    swiftc -O -parse-as-library -whole-module-optimization \
        -emit-object -emit-module \
        "${DIRECT_SWIFT_ARGUMENTS[@]}" \
        -target "$TARGET" \
        -module-name VideoToNDICore \
        -I "$APP_ROOT/Sources/CNDIShim/include" \
        -module-cache-path "$MANUAL_MODULE_CACHE" \
        "$APP_ROOT"/Sources/VideoToNDICore/*.swift \
        -o "$BIN_DIRECTORY/VideoToNDICore.o" \
        -emit-module-path "$BIN_DIRECTORY/VideoToNDICore.swiftmodule"

    swiftc -O -parse-as-library \
        "${DIRECT_SWIFT_ARGUMENTS[@]}" \
        -target "$TARGET" \
        -I "$BIN_DIRECTORY" \
        -I "$APP_ROOT/Sources/CNDIShim/include" \
        -module-cache-path "$MANUAL_MODULE_CACHE" \
        "$APP_ROOT"/Sources/VideoToNDIApp/*.swift \
        "$BIN_DIRECTORY/VideoToNDICore.o" \
        "$BIN_DIRECTORY/CNDIShim.o" \
        -o "$BIN_DIRECTORY/VideoToNDIApp"
fi

mkdir -p "$STAGING_BUNDLE/Contents/MacOS" "$STAGING_BUNDLE/Contents/Resources"
cp "$BIN_DIRECTORY/VideoToNDIApp" "$STAGING_BUNDLE/Contents/MacOS/VideoToNDIApp"
cp "$APP_ROOT/Resources/Info.plist" "$STAGING_BUNDLE/Contents/Info.plist"

ICON_SOURCE="$REPOSITORY_ROOT/Production_Hub_App/assets/ProductionHub.icns"
if [[ ! -f "$ICON_SOURCE" ]]; then
    echo "Required Production Hub icon not found: $ICON_SOURCE" >&2
    exit 1
fi
cp "$ICON_SOURCE" "$STAGING_BUNDLE/Contents/Resources/ProductionHub.icns"

codesign --force --deep --options runtime \
    --entitlements "$APP_ROOT/Resources/VideoToNDI.entitlements" \
    --sign - "$STAGING_BUNDLE"

mkdir -p "$APP_ROOT/dist"
if [[ -e "$OUTPUT_BUNDLE" ]]; then
    PREVIOUS_BUNDLE="$APP_ROOT/dist/$PRODUCT_NAME.previous-$(date +%Y%m%d-%H%M%S).app"
    mv "$OUTPUT_BUNDLE" "$PREVIOUS_BUNDLE"
    echo "Previous build retained at: $PREVIOUS_BUNDLE"
fi
mv "$STAGING_BUNDLE" "$OUTPUT_BUNDLE"

echo "Built: $OUTPUT_BUNDLE"

if [[ "$INSTALL_AFTER_BUILD" == true ]]; then
    echo "Installing into /Applications…"
    if [[ -w /Applications ]]; then
        /usr/bin/ditto "$OUTPUT_BUNDLE" "$INSTALL_BUNDLE"
    else
        echo "Administrator permission is required to install the application."
        /usr/bin/sudo /usr/bin/ditto "$OUTPUT_BUNDLE" "$INSTALL_BUNDLE"
    fi
    /usr/bin/codesign --verify --deep --strict "$INSTALL_BUNDLE"
    echo "Installed: $INSTALL_BUNDLE"
    echo "Open it with: open \"$INSTALL_BUNDLE\""
else
    echo "Installation skipped."
    echo "Open the build with: open \"$OUTPUT_BUNDLE\""
fi
