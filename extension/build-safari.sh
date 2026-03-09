#!/usr/bin/env bash
#
# Build the Safari Web Extension.
#
# This script sets up the pre-built Xcode project by linking the shared
# extension files into it, then opens it in Xcode for you to build & install.
#
# Prerequisites:
#   - macOS with Xcode 14+ installed
#
# Usage:
#   cd sideye/extension
#   ./build-safari.sh
#
# After Xcode opens:
#   1. Select your development team in Signing & Capabilities
#   2. Build and run (Cmd+R)
#   3. Enable the extension in Safari → Settings → Extensions
#   4. Grant permission for github.com
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SAFARI_DIR="$SCRIPT_DIR/safari"

echo "==> Setting up Safari Web Extension..."
echo ""

# Run the setup script to link extension files into the Xcode project
cd "$SAFARI_DIR"
./setup.sh

echo ""

# Offer to open
read -rp "Open Xcode project now? [Y/n] " answer
if [[ "${answer:-y}" =~ ^[Yy]?$ ]]; then
    open "$SAFARI_DIR/Sideye.xcodeproj"
fi
