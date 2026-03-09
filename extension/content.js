/**
 * Sideye — GitHub Overlay Content Script
 *
 * Detects when you're on a GitHub PR page, queries your local bot,
 * and injects review results as an overlay: verdict badge, sidebar panel,
 * and inline comments on the diff.
 *
 * If no review exists, shows a "Run Review" button so you can trigger
 * one directly from GitHub.
 */

(function () {
  "use strict";

  const DEFAULT_BOT_URL = "http://localhost:8111";
  const POLL_INTERVAL = 3000; // Check for page changes (GitHub uses SPA navigation)
  const REVIEW_POLL_INTERVAL = 4000; // Poll review status during a run
  const OVERLAY_ID = "prbot-overlay";
  const BADGE_ID = "prbot-badge";
  const PANEL_ID = "prbot-panel";

  let lastUrl = "";
  let botUrl = DEFAULT_BOT_URL;
  let overlayEnabled = true;
  let currentReview = null;
  let reviewPollTimer = null;
  let autoShowPanel = false; // Open panel automatically after triggered review
  let inlineInjected = false; // Track if inline comments have been injected

  // Track selected/edited comments for submission
  const commentEdits = new Map(); // id → {file, line_hint, comment, original, selected}
  const SUBMIT_BAR_ID = "prbot-submit-bar";
  let commentIdCounter = 0;
  let selectedReviewMode = "standard"; // quick | standard | thorough

  // ── Init ────────────────────────────────────────────────────────

  async function init() {
    // Load settings (browserAPI from browser-polyfill.js handles Chrome/Safari/Firefox)
    const stored = await browserAPI.storage.local.get(["botUrl", "overlayEnabled"]);
    botUrl = stored.botUrl || DEFAULT_BOT_URL;
    overlayEnabled = stored.overlayEnabled !== false;

    if (!overlayEnabled) return;

    // Initial check
    checkPage();

    // Watch for SPA navigation (GitHub uses turbo/pjax)
    const observer = new MutationObserver(() => {
      if (window.location.href !== lastUrl) {
        checkPage();
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });

    // Also poll as a fallback
    setInterval(checkPage, POLL_INTERVAL);
  }

  // ── Page Detection ──────────────────────────────────────────────

  function checkPage() {
    const url = window.location.href;
    if (url === lastUrl) return;
    lastUrl = url;

    // Clean up old overlay
    removeOverlay();

    // Check if we're on a PR page
    const match = url.match(
      /github\.com\/([\w\-\.]+)\/([\w\-\.]+)\/pull\/(\d+)/
    );
    if (!match) return;

    // We're on a PR page — query the bot
    queryBot(url);
  }

  // ── Bot Communication ─────────────────────────────────────────

  /**
   * Fetch from the bot API via background service worker.
   * Safari blocks direct fetch() from content scripts to localhost,
   * so we proxy through the background script via message passing.
   * Falls back to direct fetch on Chrome if messaging fails.
   */
  async function botFetch(endpoint, method, body) {
    // Try message passing first (works in Safari + Chrome)
    try {
      const api = typeof browser !== "undefined" ? browser : chrome;
      const msg = { type: "bot-fetch", endpoint, botUrl };
      if (method) msg.method = method;
      if (body) msg.body = body;
      const result = await api.runtime.sendMessage(msg);
      if (result && result.ok) return result.data;
      if (result && !result.ok) {
        console.warn("[Sideye] API error:", result.status, result.error);
        return { _error: true, status: result.status, message: result.error };
      }
    } catch (e) {
      console.warn("[Sideye] Message passing failed:", e.message);
      // Message passing unavailable — fall back to direct fetch
    }

    // Direct fetch fallback (Chrome with host_permissions)
    const opts = { method: method || "GET", headers: { Accept: "application/json" } };
    if (body) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    try {
      const resp = await fetch(`${botUrl}${endpoint}`, opts);
      if (!resp.ok) {
        const errText = await resp.text().catch(() => "");
        console.warn("[Sideye] HTTP error:", resp.status, errText);
        return { _error: true, status: resp.status, message: errText || `HTTP ${resp.status}` };
      }
      return resp.json();
    } catch (e) {
      console.warn("[Sideye] Fetch failed:", e.message);
      return { _error: true, status: 0, message: e.message };
    }
  }

  // ── Bot Query ───────────────────────────────────────────────────

  async function queryBot(prUrl) {
    try {
      const cleanUrl = prUrl.split("?")[0].split("#")[0]; // Strip query/hash
      const data = await botFetch(
        `/api/ext/review-by-url?pr_url=${encodeURIComponent(cleanUrl)}`
      );

      if (!data || data._error || !data.found) {
        // No review — show "Run Review" button
        injectBadge(null, cleanUrl);
        return;
      }

      // Check if the review is still running
      if (data.status === "in_progress" || data.status === "pending") {
        injectBadge("running", cleanUrl);
        startPolling(data.review_id || null, cleanUrl);
        return;
      }

      currentReview = data;
      injectBadge(data, cleanUrl);
      injectPanel(data);

      // Auto-open panel if this review was just triggered from the extension
      if (autoShowPanel) {
        autoShowPanel = false;
        const panel = document.getElementById(PANEL_ID);
        if (panel) panel.classList.remove("prbot-panel-hidden");
      }

      // Inject inline comments on the diff
      inlineInjected = false;
      tryInjectInline(data);
    } catch (err) {
      // Bot not running or unreachable — silently fail
      console.debug("[Sideye] Bot unreachable:", err.message);
    }
  }

  function isFilesTab() {
    return (
      window.location.href.includes("/files") ||
      getDiffContainers().length > 0
    );
  }

  /**
   * Find diff file containers on the page — GitHub uses several layouts.
   */
  function getDiffContainers() {
    // Try multiple selectors for different GitHub layouts
    const selectors = [
      '[id^="diff-"]',                        // Classic diff view
      '.js-diff-progressive-container > div',  // Progressive loading
      '[data-tagsearch-path]',                 // Modern file containers
      '.file[data-file-type]',                 // Another variant
      '.js-file-content',                      // Fallback
      'copilot-diff-entry',                    // Copilot-era diff entries
    ];
    for (const sel of selectors) {
      const els = document.querySelectorAll(sel);
      if (els.length > 0) return els;
    }
    return [];
  }

  // Track overlay placements for repositioning
  let overlayPlacements = []; // {overlay, targetEl}
  let overlayPositionRAF = null;

  /**
   * Try to inject inline comments on the diff.
   * Uses overlay divs positioned outside React's managed DOM tree,
   * since React re-renders wipe injected <tr> elements.
   */
  function tryInjectInline(data) {
    if (inlineInjected) return;
    if (!data || !data.inline_comments || !data.inline_comments.length) return;

    function attemptInject() {
      const containers = getDiffContainers();
      if (!containers.length) return 0;
      return injectInlineComments(data.inline_comments, containers);
    }

    const injected = attemptInject();
    if (injected > 0) {
      console.log("[Sideye] Injected %d inline comments", injected);
      inlineInjected = true;
      return;
    }

    // Diff not fully loaded yet — watch for changes
    console.debug("[Sideye] Setting up observer for diff containers (%d comments pending)", data.inline_comments.length);
    let retries = 0;
    const maxRetries = 60;
    let debounceTimer = null;

    const inlineObserver = new MutationObserver(() => {
      if (inlineInjected) { inlineObserver.disconnect(); return; }
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => {
        retries++;
        const count = attemptInject();
        if (count > 0) {
          console.log("[Sideye] Observer: injected %d inline comments (attempt %d)", count, retries);
          inlineInjected = true;
          inlineObserver.disconnect();
        }
        if (retries >= maxRetries) {
          console.debug("[Sideye] Observer: giving up after %d retries", retries);
          inlineObserver.disconnect();
        }
      }, 500);
    });
    inlineObserver.observe(document.body, { childList: true, subtree: true });
    setTimeout(() => inlineObserver.disconnect(), 300000);
  }

  // ── Review Trigger ────────────────────────────────────────────

  async function triggerReview(prUrl) {
    // Switch badge to running state
    autoShowPanel = true; // Auto-open panel when review completes
    removeBadge();
    injectBadge("running", prUrl);

    try {
      const result = await botFetch("/api/ext/trigger-review", "POST", {
        pr_url: prUrl,
        review_mode: selectedReviewMode,
      });

      if (!result || result._error) {
        const errMsg = result?._error ? result.message : "Trigger request failed";
        console.error("[Sideye] Trigger failed:", errMsg);
        removeBadge();
        injectBadge({ _errorDetail: errMsg }, prUrl);
        return;
      }

      // review_id may be null if the pipeline is still starting up —
      // startPolling handles both cases (polls by review_id or by URL)
      console.log("[Sideye] Trigger response:", result);
      startPolling(result.review_id || null, prUrl);
    } catch (err) {
      console.error("[Sideye] Failed to trigger review:", err);
      removeBadge();
      injectBadge({ _errorDetail: err.message }, prUrl);
    }
  }

  // ── Polling ───────────────────────────────────────────────────

  function startPolling(reviewId, prUrl) {
    stopPolling();

    let elapsed = 0;
    reviewPollTimer = setInterval(async () => {
      elapsed += REVIEW_POLL_INTERVAL;

      // Update progress text on badge
      const badge = document.getElementById(BADGE_ID);
      if (badge) {
        const secs = Math.round(elapsed / 1000);
        const spinnerEl = badge.querySelector(".prbot-spinner-text");
        if (spinnerEl) spinnerEl.textContent = `Running review… ${secs}s`;
      }

      try {
        let status = null;

        if (reviewId) {
          const statusData = await botFetch(
            `/api/ext/review-status?review_id=${encodeURIComponent(reviewId)}`
          );
          status = statusData?.status;
        } else {
          // No review_id yet — re-query by URL to find one
          const cleanUrl = prUrl.split("?")[0].split("#")[0];
          const data = await botFetch(
            `/api/ext/review-by-url?pr_url=${encodeURIComponent(cleanUrl)}`
          );
          if (data?.found) {
            if (data.status === "in_progress" || data.status === "pending") {
              reviewId = data.review_id;
              return; // Keep polling
            }
            status = data.status || "complete";
          }
        }

        if (status === "error") {
          // Review failed on the server
          stopPolling();
          removeBadge();
          injectBadge({ _errorDetail: "Review failed on server — check bot logs" }, prUrl);
        } else if (status && status !== "in_progress" && status !== "pending" && status !== "starting") {
          // Review done — reload results
          stopPolling();
          removeOverlay();
          queryBot(prUrl);
        }
      } catch (_) {
        // Bot might be busy — keep polling
      }

      // Timeout after 10 minutes
      if (elapsed > 600000) {
        stopPolling();
        removeBadge();
        injectBadge("timeout", prUrl);
      }
    }, REVIEW_POLL_INTERVAL);
  }

  function stopPolling() {
    if (reviewPollTimer) {
      clearInterval(reviewPollTimer);
      reviewPollTimer = null;
    }
  }

  // ── Badge (verdict indicator in PR header) ──────────────────────

  function removeBadge() {
    const el = document.getElementById(BADGE_ID);
    if (el) el.remove();
  }

  function injectBadge(data, prUrl) {
    if (document.getElementById(BADGE_ID)) return;

    const badge = document.createElement("span");
    badge.id = BADGE_ID;

    if (data === null) {
      // No review — show Run Review button with mode selector
      badge.className = "prbot-badge prbot-badge-run";
      badge.innerHTML = `
        <span class="prbot-run-icon">▶</span>
        <span>Run Review</span>
        <select class="prbot-mode-select" title="Review depth">
          <option value="quick">Quick</option>
          <option value="standard" selected>Standard</option>
          <option value="thorough">Thorough</option>
        </select>
      `;
      badge.title = "Trigger a PR review with your local bot";
      badge.style.cursor = "pointer";
      const modeSelect = badge.querySelector(".prbot-mode-select");
      modeSelect.addEventListener("click", (e) => e.stopPropagation());
      modeSelect.addEventListener("change", (e) => {
        selectedReviewMode = e.target.value;
        e.stopPropagation();
      });
      badge.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (e.target === modeSelect) return;
        triggerReview(prUrl);
      });
    } else if (data === "running") {
      // Review in progress — show spinner
      badge.className = "prbot-badge prbot-badge-running";
      badge.innerHTML = `
        <span class="prbot-spinner"></span>
        <span class="prbot-spinner-text">Running review…</span>
      `;
      badge.title = "Review is running — this usually takes 1-3 minutes";
    } else if (data && data._errorDetail) {
      // Trigger failed — show detail
      const short = (data._errorDetail || "").substring(0, 80);
      badge.className = "prbot-badge prbot-badge-error";
      badge.innerHTML = `<span>Bot error — retry?</span>`;
      badge.title = `Error: ${short}\nClick to retry.`;
      badge.style.cursor = "pointer";
      console.error("[Sideye] Badge error detail:", data._errorDetail);
      badge.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        removeBadge();
        triggerReview(prUrl);
      });
    } else if (data === "error") {
      // Generic error fallback
      badge.className = "prbot-badge prbot-badge-error";
      badge.innerHTML = `<span>Bot error — retry?</span>`;
      badge.title = "Failed to start review. Is the bot running?";
      badge.style.cursor = "pointer";
      badge.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        removeBadge();
        triggerReview(prUrl);
      });
    } else if (data === "timeout") {
      // Polling timeout
      badge.className = "prbot-badge prbot-badge-error";
      badge.innerHTML = `<span>Review timed out</span>`;
      badge.title = "Review took too long. Check the bot dashboard.";
    } else {
      // Normal review result
      const verdictClass = {
        approve: "prbot-badge-approve",
        request_changes: "prbot-badge-changes",
        comment: "prbot-badge-comment",
      }[data.verdict] || "prbot-badge-comment";

      badge.className = `prbot-badge ${verdictClass}`;
      badge.textContent = `Bot: ${data.verdict.replace("_", " ").toUpperCase()} (${Math.round(data.confidence * 100)}%)`;
      badge.title = data.summary;
      badge.style.cursor = "pointer";
      badge.addEventListener("click", () => togglePanel());
    }

    // Insert after the PR title
    const titleEl =
      document.querySelector(".gh-header-title") ||
      document.querySelector("[data-testid='issue-title']") ||
      document.querySelector(".js-issue-title");

    if (titleEl) {
      titleEl.parentElement.insertBefore(badge, titleEl.nextSibling);
    } else {
      // Fallback: insert at top of page
      const main = document.querySelector("main") || document.body;
      main.insertBefore(badge, main.firstChild);
    }
  }

  // ── Side Panel ──────────────────────────────────────────────────

  function injectPanel(data) {
    if (document.getElementById(PANEL_ID)) return;

    const panel = document.createElement("div");
    panel.id = PANEL_ID;
    panel.className = "prbot-panel prbot-panel-hidden";

    const verdictColor = {
      approve: "#3fb950",
      request_changes: "#f85149",
      comment: "#d29922",
    }[data.verdict] || "#d29922";

    // PR Brief section — first thing the reviewer sees
    const brief = data.pr_brief || {};
    const briefHtml = brief.purpose ? `
      <div class="prbot-panel-section prbot-brief">
        <h4>PR Brief</h4>
        <p class="prbot-brief-purpose">${escHtml(brief.purpose)}</p>
        ${brief.key_changes && brief.key_changes.length ? `
          <ul class="prbot-brief-changes">
            ${brief.key_changes.map(c => `<li>${escHtml(c)}</li>`).join("")}
          </ul>` : ""}
        ${brief.scope ? `<p class="prbot-brief-scope">${escHtml(brief.scope)}</p>` : ""}
        ${brief.alignment ? `<p class="prbot-brief-alignment">${escHtml(brief.alignment)}</p>` : ""}
      </div>` : "";

    // Key findings with per-finding reasoning
    const findingsHtml = data.key_findings && data.key_findings.length ? `
      <div class="prbot-panel-section">
        <h4>Key Findings</h4>
        ${data.key_findings.map(f => `
          <div class="prbot-finding prbot-finding-${f.severity || 'minor'}">
            <span class="prbot-finding-sev">${f.severity || 'info'}</span>
            ${escHtml(f.finding)}
            ${f.reason ? `<div class="prbot-finding-reason">${escHtml(f.reason)}</div>` : ""}
          </div>
        `).join("")}
      </div>` : "";

    panel.innerHTML = `
      <div class="prbot-panel-header">
        <span class="prbot-panel-title">Sideye</span>
        <button class="prbot-panel-close" onclick="document.getElementById('${PANEL_ID}').classList.add('prbot-panel-hidden')">&times;</button>
      </div>

      ${briefHtml}

      <div class="prbot-panel-verdict" style="border-left: 3px solid ${verdictColor}">
        <strong>${data.verdict.replace("_", " ").toUpperCase()}</strong>
        <span class="prbot-confidence">${Math.round(data.confidence * 100)}% confidence</span>
      </div>

      <div class="prbot-panel-summary">${escHtml(data.summary)}</div>

      ${findingsHtml}

      ${data.suggested_comment ? `
      <div class="prbot-panel-section">
        <h4>Suggested Comment</h4>
        <pre class="prbot-comment">${escHtml(data.suggested_comment)}</pre>
        <button class="prbot-copy-btn" onclick="navigator.clipboard.writeText(this.previousElementSibling.textContent); this.textContent='Copied!'; setTimeout(() => this.textContent='Copy', 1500)">Copy</button>
      </div>` : ""}

      <div class="prbot-panel-section prbot-feedback-section">
        <div class="prbot-quick-feedback" data-review-id="${data.review_id}" data-submitted="false">
          <span class="prbot-feedback-label">Verdict correct?</span>
          <button class="prbot-fb-btn prbot-fb-yes" data-value="true" title="Yes">&#x1F44D;</button>
          <button class="prbot-fb-btn prbot-fb-no" data-value="false" title="No">&#x1F44E;</button>
          <span class="prbot-feedback-sep">|</span>
          <button class="prbot-fb-cal" data-value="too_strict">Too strict</button>
          <button class="prbot-fb-cal" data-value="about_right">About right</button>
          <button class="prbot-fb-cal" data-value="too_lenient">Too lenient</button>
          <span class="prbot-feedback-status"></span>
        </div>
      </div>

      <div class="prbot-panel-footer">
        <a href="${botUrl}${data.review_url}" target="_blank" class="prbot-link">Open in Bot UI &rarr;</a>
        <span class="prbot-comment-count">${data.inline_comments ? data.inline_comments.length : 0} inline comments</span>
        ${data.usage && data.usage.total_tokens ? `<span class="prbot-token-count" title="${data.usage.estimated ? 'estimated' : 'exact'} tokens">${formatTokens(data.usage.total_tokens)} tokens${data.usage.model ? ` (${shortModel(data.usage.model)})` : ''}</span>` : ''}
      </div>
    `;

    // Wire up quick feedback buttons
    const fbContainer = panel.querySelector(".prbot-quick-feedback");
    const fbState = { verdict_correct: null, severity_assessment: null };

    fbContainer.querySelectorAll(".prbot-fb-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        fbState.verdict_correct = btn.dataset.value === "true";
        fbContainer.querySelectorAll(".prbot-fb-btn").forEach(b => b.classList.remove("prbot-fb-active"));
        btn.classList.add("prbot-fb-active");
        submitQuickFeedback(fbContainer, fbState);
      });
    });

    fbContainer.querySelectorAll(".prbot-fb-cal").forEach(btn => {
      btn.addEventListener("click", () => {
        fbState.severity_assessment = btn.dataset.value;
        fbContainer.querySelectorAll(".prbot-fb-cal").forEach(b => b.classList.remove("prbot-fb-active"));
        btn.classList.add("prbot-fb-active");
        submitQuickFeedback(fbContainer, fbState);
      });
    });

    document.body.appendChild(panel);
  }

  // ── Quick Feedback ──────────────────────────────────────────────

  async function submitQuickFeedback(container, state) {
    if (state.verdict_correct === null) return;

    const reviewId = container.dataset.reviewId;
    const statusEl = container.querySelector(".prbot-feedback-status");

    const payload = { review_id: reviewId, verdict_correct: state.verdict_correct };
    if (state.severity_assessment) payload.severity_assessment = state.severity_assessment;

    try {
      const result = await botFetch("/api/ext/quick-feedback", "POST", payload);
      if (result && !result._error) {
        statusEl.textContent = "Saved";
        statusEl.className = "prbot-feedback-status prbot-fb-ok";
      } else {
        statusEl.textContent = "Failed";
        statusEl.className = "prbot-feedback-status prbot-fb-err";
      }
    } catch (e) {
      statusEl.textContent = "Error";
      statusEl.className = "prbot-feedback-status prbot-fb-err";
    }
  }

  function togglePanel() {
    const panel = document.getElementById(PANEL_ID);
    if (panel) {
      panel.classList.toggle("prbot-panel-hidden");
    }
  }

  // ── Inline Comments on Diff ─────────────────────────────────────

  /**
   * Extract diff line data from a container.
   * Supports both table-based and div-based GitHub diff layouts.
   */
  function getDiffLineData(container) {
    const lineData = [];

    // Strategy 1: table rows (GitHub still uses tables in many layouts)
    const table = container.querySelector("table");
    if (table) {
      const rows = table.querySelectorAll("tr");
      rows.forEach((row, idx) => {
        const cells = row.querySelectorAll("td");
        if (cells.length < 2) return;
        const codeCell = cells[cells.length - 1];
        const content = codeCell?.textContent || "";

        // Detect add/del: check classes AND data-code-marker attribute
        const rowHtml = row.outerHTML.substring(0, 500);
        const rowCls = row.className + " " + (codeCell?.className || "");
        const isAdd = rowCls.includes("addition") || rowCls.includes("blob-code-addition")
          || row.querySelector('[data-code-marker="+"]') !== null;
        const isDel = rowCls.includes("deletion") || rowCls.includes("blob-code-deletion")
          || row.querySelector('[data-code-marker="-"]') !== null;

        lineData.push({ idx, element: row, content, type: isAdd ? "add" : isDel ? "del" : "ctx" });
      });
    }

    // Strategy 2: div-based lines with data-line-number (fallback)
    if (!lineData.length) {
      const codeEls = container.querySelectorAll('[data-line-number]');
      const seen = new Set();
      codeEls.forEach((el, idx) => {
        // Find the closest row-like parent
        const rowEl = el.closest('tr') || el.closest('[role="row"]') || el.parentElement;
        if (!rowEl || seen.has(rowEl)) return;
        seen.add(rowEl);
        const content = rowEl.textContent || "";
        const marker = rowEl.querySelector('[data-code-marker]');
        const markerVal = marker ? marker.getAttribute('data-code-marker') : '';
        const isAdd = markerVal === '+';
        const isDel = markerVal === '-';
        lineData.push({ idx, element: rowEl, content, type: isAdd ? "add" : isDel ? "del" : "ctx" });
      });
    }

    return lineData;
  }

  /**
   * Inject inline comments onto diff containers using overlay divs.
   * Overlays are positioned outside React's managed DOM tree so they
   * survive React re-renders.
   * Returns the number of comments successfully injected.
   */
  function injectInlineComments(comments, containers) {
    if (!comments || !comments.length) return 0;

    // Group comments by file
    const byFile = {};
    comments.forEach((c) => {
      const f = normalizeFile(c.file);
      if (!byFile[f]) byFile[f] = [];
      byFile[f].push(c);
    });

    console.debug("[Sideye] Inline comments grouped:", Object.keys(byFile));

    const diffContainers = containers || getDiffContainers();
    if (!diffContainers.length) {
      console.debug("[Sideye] No diff containers found on page");
      return 0;
    }

    console.debug("[Sideye] Found %d diff containers", diffContainers.length);

    // Create or reuse overlay root (lives outside React's tree)
    let overlayRoot = document.getElementById("prbot-inline-overlays");
    if (!overlayRoot) {
      overlayRoot = document.createElement("div");
      overlayRoot.id = "prbot-inline-overlays";
      document.body.appendChild(overlayRoot);
    }

    let totalInjected = 0;
    overlayPlacements = [];

    diffContainers.forEach((container) => {
      const filePath = extractFilePath(container);
      if (!filePath) return;

      const fileComments = resolveFileComments(normalizeFile(filePath), byFile);
      if (!fileComments.length) return;

      console.debug("[Sideye] File '%s' has %d comments to place", filePath, fileComments.length);

      const lineData = getDiffLineData(container);
      if (!lineData.length) {
        console.debug("[Sideye] No diff lines found in container for '%s'", filePath);
        return;
      }

      fileComments.forEach((c) => {
        const matchIdx = matchLineHint(c.line_hint, lineData);
        if (matchIdx < 0) {
          console.debug("[Sideye] No line match for hint: '%s'", (c.line_hint || "").substring(0, 60));
          return;
        }

        const targetEl = lineData[matchIdx].element;
        const isContextLine = lineData[matchIdx].type === "ctx";
        const commentType = c.type || "review_comment";
        const isNote = commentType === "note";
        const cid = `prbot-c-${commentIdCounter++}`;

        const autoSelect = !isNote && (c.severity === "critical" || c.severity === "major");
        if (!isNote) {
          commentEdits.set(cid, {
            file: c.file,
            line_hint: c.line_hint,
            comment: c.comment,
            original: c.comment,
            severity: c.severity || "minor",
            selected: autoSelect,
          });
        }

        const severityColors = {
          critical: "#f85149", major: "#db6d28", minor: "#d29922", nit: "#8b949e",
        };
        const contextCls = isContextLine ? " prbot-inline-on-context" : "";
        const noteCls = isNote ? " prbot-inline-note" : "";
        const contextLabel = isContextLine ? `<span class="prbot-inline-ctx-label">existing code</span>` : "";
        const typeLabel = isNote
          ? `<span class="prbot-inline-type-note">NOTE</span>`
          : `<span class="prbot-inline-type-review">REVIEW</span>`;
        const toolbar = isNote
          ? ""
          : `<span class="prbot-inline-toolbar">
              <button class="prbot-btn-edit" title="Edit comment">✎</button>
              <label class="prbot-btn-select" title="Select for submission"><input type="checkbox" class="prbot-select-cb"> Post</label>
            </span>`;

        // Build overlay div (not a <tr>)
        const overlay = document.createElement("div");
        overlay.className = "prbot-inline-row" + (isContextLine ? " prbot-inline-context" : "");
        overlay.innerHTML = `
          <div class="prbot-inline prbot-inline-${c.severity || "info"}${contextCls}${noteCls}" data-cid="${cid}">
            <div class="prbot-inline-header">
              <span class="prbot-inline-sev" style="color: ${severityColors[c.severity] || "#58a6ff"}">${(c.severity || "info").toUpperCase()}</span>
              ${typeLabel}
              ${contextLabel}
              ${toolbar}
            </div>
            <div class="prbot-inline-body">${escHtml(c.comment)}</div>
            ${c.suggestion ? `<div class="prbot-inline-suggestion">→ ${escHtml(c.suggestion)}</div>` : ""}
          </div>
        `;

        if (!isNote) {
          const editBtn = overlay.querySelector(".prbot-btn-edit");
          editBtn.addEventListener("click", () => startEdit(cid, overlay));

          const selectCb = overlay.querySelector(".prbot-select-cb");
          if (autoSelect) {
            selectCb.checked = true;
            const wrapper = overlay.querySelector(`[data-cid="${cid}"]`);
            if (wrapper) wrapper.classList.add("prbot-inline-selected");
          }
          selectCb.addEventListener("change", () => {
            const entry = commentEdits.get(cid);
            if (entry) entry.selected = selectCb.checked;
            const wrapper = overlay.querySelector(`[data-cid="${cid}"]`);
            if (wrapper) wrapper.classList.toggle("prbot-inline-selected", selectCb.checked);
            updateSubmitBar();
          });
        }

        overlayRoot.appendChild(overlay);
        overlayPlacements.push({ overlay, targetEl });
        totalInjected++;
      });
    });

    console.debug("[Sideye] Total inline comments injected: %d", totalInjected);

    // Position overlays and start tracking
    if (totalInjected > 0) {
      positionAllOverlays();
      startOverlayPositioning();
      updateSubmitBar();
    }
    return totalInjected;
  }

  /**
   * Position all overlay comments relative to their target diff lines.
   * Handles stacking: if multiple overlays target the same or adjacent lines,
   * they stack downward without overlapping each other.
   */
  function positionAllOverlays() {
    // Track the bottom edge of each placed overlay so subsequent ones
    // don't overlap. Keyed by approximate X band (to handle split diffs).
    const bottomEdges = []; // { left, right, bottom }

    for (const { overlay, targetEl } of overlayPlacements) {
      if (!document.body.contains(targetEl)) {
        overlay.style.display = "none";
        continue;
      }
      overlay.style.display = "";
      const rect = targetEl.getBoundingClientRect();
      const scrollY = window.scrollY || document.documentElement.scrollTop;
      const scrollX = window.scrollX || document.documentElement.scrollLeft;

      let desiredTop = rect.bottom + scrollY + 2; // 2px gap below target line
      const left = rect.left + scrollX;
      const width = rect.width;

      // Check if this overlaps with any previously placed overlay
      for (const edge of bottomEdges) {
        // Same horizontal band (overlapping X ranges)
        if (left < edge.right && (left + width) > edge.left) {
          if (desiredTop < edge.bottom + 2) {
            desiredTop = edge.bottom + 2;
          }
        }
      }

      overlay.style.position = "absolute";
      overlay.style.top = desiredTop + "px";
      overlay.style.left = left + "px";
      overlay.style.width = width + "px";
      overlay.style.zIndex = "99";

      // Record this overlay's bottom edge for stacking
      const overlayHeight = overlay.offsetHeight || 60; // estimate if not yet rendered
      bottomEdges.push({
        left: left,
        right: left + width,
        bottom: desiredTop + overlayHeight,
      });
    }
  }

  /**
   * Keep overlays positioned as the user scrolls or React re-renders.
   * Uses requestAnimationFrame with a throttle.
   */
  function startOverlayPositioning() {
    if (overlayPositionRAF) return;
    let lastRun = 0;
    function tick() {
      const now = performance.now();
      if (now - lastRun > 200) { // Throttle to ~5fps
        positionAllOverlays();
        lastRun = now;
      }
      overlayPositionRAF = requestAnimationFrame(tick);
    }
    overlayPositionRAF = requestAnimationFrame(tick);
  }

  function stopOverlayPositioning() {
    if (overlayPositionRAF) {
      cancelAnimationFrame(overlayPositionRAF);
      overlayPositionRAF = null;
    }
  }

  /**
   * Extract the file path from a diff container element.
   * Tries multiple GitHub layout strategies.
   */
  function extractFilePath(container) {
    // data-tagsearch-path is the most reliable modern attribute
    if (container.getAttribute("data-tagsearch-path")) {
      return container.getAttribute("data-tagsearch-path");
    }

    // Try known file header selectors
    const selectors = [
      "[data-path]",
      ".file-header [title]",
      ".file-info a",
      ".file-header .file-info .Truncate a",
      ".file-header a[title]",
      'a[href*="#diff-"]',
      ".filename",
    ];

    for (const sel of selectors) {
      const el = container.querySelector(sel);
      if (el) {
        const path =
          el.getAttribute("data-path") ||
          el.getAttribute("title") ||
          el.textContent.trim();
        if (path && path.length < 500) return path;
      }
    }

    // Last resort: look for any element with a path-like text in the header area
    const header = container.querySelector(".file-header, .js-file-header");
    if (header) {
      const text = header.textContent.trim();
      const pathMatch = text.match(/[\w][\w\-./]*\.\w+/);
      if (pathMatch) return pathMatch[0];
    }

    console.debug("[Sideye] Could not extract file path from container:", container.id || container.className);
    return null;
  }

  // ── Inline Edit ────────────────────────────────────────────────

  function startEdit(cid, container) {
    const wrapper = container.querySelector(`[data-cid="${cid}"]`);
    if (!wrapper || wrapper.classList.contains("prbot-inline-editing")) return;

    const bodyEl = wrapper.querySelector(".prbot-inline-body");
    if (!bodyEl) return;

    const entry = commentEdits.get(cid);
    if (!entry) return;

    wrapper.classList.add("prbot-inline-editing");

    const currentText = entry.comment;
    bodyEl.innerHTML = `
      <textarea class="prbot-edit-textarea">${escHtml(currentText)}</textarea>
      <div class="prbot-edit-actions">
        <button class="prbot-edit-save">Save</button>
        <button class="prbot-edit-cancel">Cancel</button>
      </div>
    `;

    const textarea = bodyEl.querySelector(".prbot-edit-textarea");
    textarea.focus();
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);

    bodyEl.querySelector(".prbot-edit-save").addEventListener("click", () => {
      const newText = textarea.value.trim();
      if (newText) {
        entry.comment = newText;
        bodyEl.textContent = newText;
      } else {
        bodyEl.textContent = entry.comment;
      }
      wrapper.classList.remove("prbot-inline-editing");
    });

    bodyEl.querySelector(".prbot-edit-cancel").addEventListener("click", () => {
      bodyEl.textContent = entry.comment;
      wrapper.classList.remove("prbot-inline-editing");
    });
  }

  // ── Submit Review Bar ─────────────────────────────────────────

  function updateSubmitBar() {
    const total = commentEdits.size;
    const selected = [...commentEdits.values()].filter(e => e.selected);
    let bar = document.getElementById(SUBMIT_BAR_ID);

    if (!bar && total === 0) return;

    if (!bar) {
      bar = document.createElement("div");
      bar.id = SUBMIT_BAR_ID;
      bar.className = "prbot-submit-bar";
      bar.innerHTML = `
        <button class="prbot-select-all">Select All</button>
        <button class="prbot-deselect-all">Deselect All</button>
        <span class="prbot-submit-count"></span>
        <select class="prbot-submit-verdict">
          <option value="COMMENT">Comment</option>
          <option value="APPROVE">Approve</option>
          <option value="REQUEST_CHANGES">Request Changes</option>
        </select>
        <button class="prbot-submit-btn">Submit Review</button>
        <span class="prbot-submit-status"></span>
      `;
      bar.querySelector(".prbot-submit-btn").addEventListener("click", submitReview);
      bar.querySelector(".prbot-select-all").addEventListener("click", () => toggleAllComments(true));
      bar.querySelector(".prbot-deselect-all").addEventListener("click", () => toggleAllComments(false));
      document.body.appendChild(bar);
    }

    if (selected.length === 0) {
      bar.classList.add("prbot-submit-hidden");
      return;
    }

    bar.classList.remove("prbot-submit-hidden");
    bar.querySelector(".prbot-submit-count").textContent =
      `${selected.length}/${total} selected`;
    bar.querySelector(".prbot-submit-status").textContent = "";
  }

  function toggleAllComments(select) {
    commentEdits.forEach((entry, cid) => {
      entry.selected = select;
      const wrapper = document.querySelector(`[data-cid="${cid}"]`);
      if (wrapper) {
        wrapper.classList.toggle("prbot-inline-selected", select);
        const cb = wrapper.querySelector(".prbot-select-cb");
        if (cb) cb.checked = select;
      }
    });
    updateSubmitBar();
  }

  async function submitReview() {
    const bar = document.getElementById(SUBMIT_BAR_ID);
    if (!bar) return;

    const selected = [...commentEdits.entries()]
      .filter(([, e]) => e.selected)
      .map(([cid, e]) => ({
        file: e.file,
        line_hint: e.line_hint,
        comment: e.comment,
        original_comment: e.original,
      }));

    if (!selected.length) return;

    const event = bar.querySelector(".prbot-submit-verdict").value;
    const btn = bar.querySelector(".prbot-submit-btn");
    const statusEl = bar.querySelector(".prbot-submit-status");

    // Get PR URL
    const prUrl = window.location.href.split("?")[0].split("#")[0];

    btn.disabled = true;
    btn.textContent = "Submitting…";
    statusEl.textContent = "";

    // Build all_suggested_comments from the full commentEdits map (including unselected)
    const allSuggested = [...commentEdits.entries()].map(([cid, e]) => ({
      file: e.file,
      line_hint: e.line_hint,
      comment: e.original,
      severity: e.severity || "minor",
    }));

    const payload = {
      pr_url: prUrl,
      event,
      body: "",
      comments: selected,
    };

    // Include submission tracking data if we have a current review
    if (currentReview && currentReview.review_id) {
      payload.review_id = currentReview.review_id;
      payload.suggested_verdict = currentReview.verdict;
      payload.all_suggested_comments = allSuggested;
    }

    try {
      const result = await botFetch("/api/ext/submit-review", "POST", payload);

      if (!result || result._error) {
        statusEl.textContent = `Error: ${result?.message || "Request failed"}`;
        statusEl.className = "prbot-submit-status prbot-submit-err";
        btn.disabled = false;
        btn.textContent = "Submit Review";
        return;
      }

      // Mark submitted comments as posted
      const postedCount = result.posted_count || 0;
      const skipped = result.skipped_count || 0;

      commentEdits.forEach((entry, cid) => {
        if (!entry.selected) return;
        entry.selected = false;
        const wrapper = document.querySelector(`[data-cid="${cid}"]`);
        if (wrapper) {
          wrapper.classList.remove("prbot-inline-selected");
          wrapper.classList.add("prbot-inline-posted");
          // Replace toolbar with "Posted" badge
          const toolbar = wrapper.querySelector(".prbot-inline-toolbar");
          if (toolbar) toolbar.innerHTML = `<span class="prbot-posted-badge">Posted ✓</span>`;
        }
      });

      statusEl.className = "prbot-submit-status prbot-submit-ok";
      if (skipped > 0) {
        statusEl.textContent = `${postedCount} posted, ${skipped} couldn't be matched`;
      } else {
        statusEl.textContent = `${postedCount} comment${postedCount > 1 ? "s" : ""} posted`;
      }

      // Hide bar after a moment
      setTimeout(() => {
        bar.classList.add("prbot-submit-hidden");
        btn.disabled = false;
        btn.textContent = "Submit Review";
      }, 3000);

    } catch (err) {
      statusEl.textContent = `Error: ${err.message}`;
      statusEl.className = "prbot-submit-status prbot-submit-err";
      btn.disabled = false;
      btn.textContent = "Submit Review";
    }
  }

  // ── Helpers ─────────────────────────────────────────────────────

  function normalizeFile(f) {
    return (f || "").replace(/^[ab]\//, "").trim();
  }

  function resolveFileComments(fname, byFile) {
    if (byFile[fname]) return byFile[fname];
    const basename = fname.split("/").pop();
    for (const k of Object.keys(byFile)) {
      if (k === basename || fname.endsWith(k) || k.endsWith(basename)) {
        return byFile[k];
      }
    }
    return [];
  }

  function matchLineHint(hint, lines) {
    if (!hint) return -1;
    const h = hint.toLowerCase().replace(/\s+/g, " ").trim();
    const tokens = h.match(/[a-zA-Z_]\w+(?:\.\w+)*/g) || [];
    if (!tokens.length) return -1;

    let bestIdx = -1,
      bestScore = 0;
    for (let i = 0; i < lines.length; i++) {
      const lc = (lines[i].content || "").toLowerCase();
      let score = 0;
      for (const tok of tokens) {
        if (lc.includes(tok)) score++;
      }
      if (lines[i].type === "add" || lines[i].type === "del") score += 0.5;
      if (score > bestScore) {
        bestScore = score;
        bestIdx = i;
      }
    }
    return bestScore >= 1 ? bestIdx : -1;
  }

  function escHtml(s) {
    const d = document.createElement("div");
    d.textContent = s || "";
    return d.innerHTML;
  }

  function formatTokens(n) {
    if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
    if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
    return String(n);
  }

  function shortModel(model) {
    if (!model) return "";
    if (model.includes("haiku")) return "haiku";
    if (model.includes("opus")) return "opus";
    if (model.includes("sonnet")) return "sonnet";
    return model.split("-").slice(0, 2).join("-");
  }

  function removeOverlay() {
    stopPolling();
    stopOverlayPositioning();
    [BADGE_ID, PANEL_ID, SUBMIT_BAR_ID, "prbot-inline-overlays"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.remove();
    });
    document
      .querySelectorAll(".prbot-inline-row")
      .forEach((el) => el.remove());
    overlayPlacements = [];
    currentReview = null;
    inlineInjected = false;
    commentEdits.clear();
    commentIdCounter = 0;
  }

  // Listen for settings changes
  browserAPI.storage.onChanged.addListener((changes) => {
    if (changes.botUrl) botUrl = changes.botUrl.newValue || DEFAULT_BOT_URL;
    if (changes.overlayEnabled !== undefined) {
      overlayEnabled = changes.overlayEnabled.newValue !== false;
      if (!overlayEnabled) {
        removeOverlay();
      } else {
        lastUrl = ""; // Force re-check
        checkPage();
      }
    }
  });

  // Go!
  init();
})();
