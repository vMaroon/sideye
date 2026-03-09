/**
 * Sideye — Background Service Worker
 *
 * Handles API requests to the local bot on behalf of content scripts.
 * Safari blocks direct fetch() from content scripts to localhost,
 * so we proxy through the background script using message passing.
 *
 * Chrome content scripts can fetch localhost directly (with host_permissions),
 * but using the background proxy is more reliable across all browsers.
 */

/* global chrome, browser */

const api = typeof browser !== "undefined" ? browser : chrome;
const DEFAULT_BOT_URL = "http://localhost:8111";

// Handle messages from content scripts and popup
api.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "bot-fetch") {
    handleBotFetch(message).then(sendResponse);
    return true; // async response
  }
});

async function handleBotFetch({ endpoint, botUrl, method, body }) {
  const base = botUrl || DEFAULT_BOT_URL;
  try {
    const opts = {
      method: method || "GET",
      headers: { Accept: "application/json" },
    };
    if (body) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(`${base}${endpoint}`, opts);
    if (!resp.ok) {
      const errBody = await resp.text().catch(() => "");
      return { ok: false, status: resp.status, error: errBody || `HTTP ${resp.status}` };
    }
    const data = await resp.json();
    return { ok: true, data };
  } catch (err) {
    return { ok: false, error: err.message };
  }
}
