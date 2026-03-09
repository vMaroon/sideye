/**
 * Sideye — Extension Popup
 * Uses browserAPI from browser-polyfill.js for cross-browser support.
 */

const DEFAULT_URL = "http://localhost:8111";

const urlInput = document.getElementById("bot-url");
const toggleInput = document.getElementById("overlay-toggle");
const testBtn = document.getElementById("test-btn");
const statusDot = document.getElementById("status-dot");
const statusMsg = document.getElementById("status-msg");

// Load saved settings
browserAPI.storage.local.get(["botUrl", "overlayEnabled"]).then((data) => {
  urlInput.value = data.botUrl || DEFAULT_URL;
  toggleInput.checked = data.overlayEnabled !== false;
  testConnection();
});

// Save on change
urlInput.addEventListener("change", () => {
  const val = urlInput.value.trim().replace(/\/+$/, "") || DEFAULT_URL;
  urlInput.value = val;
  browserAPI.storage.local.set({ botUrl: val });
  testConnection();
});

toggleInput.addEventListener("change", () => {
  browserAPI.storage.local.set({ overlayEnabled: toggleInput.checked });
});

testBtn.addEventListener("click", testConnection);

async function testConnection() {
  const url = urlInput.value.trim() || DEFAULT_URL;
  statusDot.className = "status";
  statusMsg.className = "status-text";
  statusMsg.textContent = "Connecting...";

  try {
    const resp = await fetch(`${url}/api/ext/ping`, {
      signal: AbortSignal.timeout(3000),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    statusDot.className = "status connected";
    statusMsg.className = "status-text ok";
    statusMsg.textContent = `Connected — bot v${data.version}`;
  } catch (err) {
    statusDot.className = "status error";
    statusMsg.className = "status-text err";
    if (err.name === "TimeoutError") {
      statusMsg.textContent = "Timeout — is the bot running?";
    } else {
      statusMsg.textContent = `Not reachable — ${err.message}`;
    }
  }
}
