#!/usr/bin/env bash
#
# Link the shared extension files into the Safari Xcode project's Resources folder.
#
# Run this once after cloning the repo, or whenever you add new extension files.
# This creates symlinks so you edit files in one place (extension/) and both
# Chrome and Safari see the changes.
#
# Usage:
#   cd sideye/extension/safari
#   ./setup.sh
#
# Then open the Xcode project:
#   open "Sideye.xcodeproj"
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RESOURCES="$SCRIPT_DIR/Sideye Extension/Resources"

echo "Linking extension files into Safari project..."

# List of files to symlink from the parent extension/ directory
FILES=(
    manifest.json
    browser-polyfill.js
    background.js
    content.js
    overlay.css
    popup.html
    popup.js
    icon16.png
    icon48.png
    icon128.png
)

mkdir -p "$RESOURCES"

for f in "${FILES[@]}"; do
    src="$EXT_DIR/$f"
    dst="$RESOURCES/$f"
    if [ ! -e "$src" ]; then
        echo "  SKIP $f (not found)"
        continue
    fi
    # Remove existing (symlink or file)
    rm -f "$dst"
    ln -s "$src" "$dst"
    echo "  ✓ $f"
done

echo ""
echo "Done! Now open the Xcode project:"
echo "  open \"$SCRIPT_DIR/Sideye.xcodeproj\""
echo ""
echo "Steps:"
echo "  1. Select your development team in Signing & Capabilities"
echo "  2. Build and run (Cmd+R)"
echo "  3. Open Safari → Settings → Extensions → enable 'Sideye'"
echo "  4. Grant permission for github.com"
echo "  5. The extension will connect to your bot at localhost:8000"
