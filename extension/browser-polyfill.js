/**
 * Minimal browser API polyfill for cross-browser compatibility.
 *
 * Safari uses the `browser.*` namespace (WebExtension standard),
 * Chrome uses `chrome.*`. This shim normalizes to a single `browserAPI`
 * global that works in both.
 */

/* global chrome, browser */

const browserAPI = (() => {
  // Safari / Firefox provide `browser` with Promise-based APIs
  if (typeof browser !== "undefined" && browser.storage) {
    return browser;
  }
  // Chrome provides `chrome` — wrap callback-based APIs in Promises
  if (typeof chrome !== "undefined" && chrome.storage) {
    return {
      storage: {
        local: {
          get(keys) {
            return new Promise((resolve) => {
              chrome.storage.local.get(keys, resolve);
            });
          },
          set(items) {
            return new Promise((resolve) => {
              chrome.storage.local.set(items, resolve);
            });
          },
        },
        onChanged: chrome.storage.onChanged,
      },
      runtime: chrome.runtime,
    };
  }
  // Fallback: no extension APIs (running outside extension context)
  console.warn("[Sideye] No extension API found — settings will use defaults");
  return {
    storage: {
      local: {
        async get() { return {}; },
        async set() {},
      },
      onChanged: { addListener() {} },
    },
    runtime: {},
  };
})();
