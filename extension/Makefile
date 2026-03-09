# Sideye — Browser Extension Build Targets
#
# Usage:
#   make chrome    — package for Chrome (zip)
#   make safari    — build Safari Xcode project (macOS only)
#   make clean     — remove build artifacts

EXTENSION_FILES = manifest.json browser-polyfill.js background.js content.js overlay.css \
                  popup.html popup.js icon16.png icon48.png icon128.png

.PHONY: chrome safari clean

chrome: dist/sideye-chrome.zip

dist/sideye-chrome.zip: $(EXTENSION_FILES)
	@mkdir -p dist
	@echo "Packaging Chrome extension..."
	@zip -j dist/sideye-chrome.zip $(EXTENSION_FILES)
	@echo "→ dist/sideye-chrome.zip"
	@echo "Load in Chrome: chrome://extensions → Developer mode → Load unpacked → select extension/"
	@echo "Or upload the .zip to the Chrome Web Store."

safari:
	@echo "Building Safari Web Extension..."
	@./build-safari.sh

clean:
	rm -rf dist/
	rm -f safari/Sideye\ Extension/Resources/*.js
	rm -f safari/Sideye\ Extension/Resources/*.css
	rm -f safari/Sideye\ Extension/Resources/*.html
	rm -f safari/Sideye\ Extension/Resources/*.json
	rm -f safari/Sideye\ Extension/Resources/*.png
