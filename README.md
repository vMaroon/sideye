<div align="center">

# Sideye

**Your code reviews, your machine, your style.**

A local AI review assistant that learns how you review and gets better at it over time.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Claude](https://img.shields.io/badge/Claude-Anthropic-D4A574?logo=anthropic&logoColor=white)](https://anthropic.com)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Safari](https://img.shields.io/badge/Safari-Extension-006CFF?logo=safari&logoColor=white)](#browser-extension)
[![Chrome](https://img.shields.io/badge/Chrome-Extension-4285F4?logo=googlechrome&logoColor=white)](#browser-extension)

</div>

---

Sideye runs entirely on your machine. It reads your local repo clones, learns from your feedback, and produces reviews that sound like you — not like a generic AI. It doesn't add a bot to your GitHub repo, doesn't require org-level permissions, and doesn't send your code to third-party services beyond Claude. Register the repos you care about, review a few PRs, give it feedback, and it starts calibrating to your standards: what you're strict about, what you let slide, how you phrase things.

Two ways to use it: a **web UI** with a diff viewer and inline comments, or a **browser extension** that overlays reviews directly on GitHub PR pages so you never leave your workflow.

## Highlights

- **Runs locally** — your code stays on your machine. No GitHub App install, no org permissions, no third-party data sharing.
- **Dual-perspective reviews** — a repo-aware reviewer and a context-free reviewer run in parallel, then get reconciled into a single voice. The information asymmetry catches things that either alone would miss.
- **Learns your style** — tracks your feedback and which comments you actually post. Builds a per-repo reviewer directive that calibrates strictness, tone, and focus areas over time.
- **Injection-safe** — every PR is scanned for prompt injection before the review pipeline runs. Flagged PRs are blocked, not reviewed.
- **Works where you work** — diff-centric web UI for deep review, browser extension for inline overlay directly on GitHub.
- **Burn-after-use actions** — one-time tickets for GitHub actions (approve/request changes/comment) with diff-hash tamper detection.
- **No vendor lock-in** — works with a Claude Pro subscription (via CLI) or Anthropic API credits. No build step, no external services beyond GitHub and Claude.

## Setup

### Server

```bash
git clone https://github.com/vMaroon/sideye.git
cd sideye

cp .env.example .env
# Fill in GITHUB_TOKEN (the only required secret)

pip install -r requirements.txt
python run.py
```

Open `http://127.0.0.1:8111`, paste a GitHub PR URL, and hit Review.

**Claude backend.** By default, Sideye uses the `claude` CLI (Claude Code) as its backend — works with a **Claude Pro subscription**, no API credits needed. To use the Anthropic API instead, set `CLAUDE_BACKEND=api` and `ANTHROPIC_API_KEY` in `.env`.

### Chrome Extension

```bash
cd extension
make chrome
```

Then in Chrome: `chrome://extensions` → enable Developer Mode → Load Unpacked → select the `extension/` directory (or upload `dist/sideye-chrome.zip`).

### Safari Extension

Requires macOS with Xcode 14+.

```bash
cd extension
make safari
```

This opens the Xcode project. Select your development team in Signing & Capabilities, build (Cmd+R), then enable the extension in Safari → Settings → Extensions.

### Extension Setup

Once installed, click the extension icon to open the popup. Set the **Bot URL** to your running Sideye server (default `http://localhost:8111`) and toggle the overlay on. Navigate to any GitHub PR page — the extension auto-detects PR URLs and shows a badge with review status or a "Run Review" button.

## How It Works

Every review runs a 5-stage pipeline:

```
1. Injection Scanner    → blocks review if PR contains hidden instructions
2. Coherence Agent      → loads cached repo context (standards, docs, history)
3. Contextual Review  ─┐
                        ├─ run in parallel, then reconciled
4. Unbiased Review    ─┘
5. Synthesis            → single-voice verdict + ready-to-post GitHub comment
```

**Injection Scanner** is a blocking gate. It combines regex heuristics (detecting hidden directives, encoded payloads, reviewer manipulation patterns, role reassignment attempts) with a Claude deep scan for subtle tricks like whitespace encoding, HTML comment hiding, and meta-commentary designed to influence review outcomes. If anything is flagged, the review halts — no code review happens on a potentially adversarial PR.

**Coherence Agent** builds and caches a repo context snapshot without calling Claude. It scans the local clone for file tree structure, coding standards (detected from `.pre-commit-config.yaml`, `pyproject.toml`, `go.mod`, Makefiles, `CONTRIBUTING.md`), design docs, recent commit history, and README excerpts. Snapshots are cached for 24 hours and refreshed daily on a configurable cron schedule.

**Contextual Review** has full repo awareness: the context snapshot, linked issues, and your learned review preferences. It checks scope alignment with linked issues, codebase coherence, test coverage, and performance claims.

**Unbiased Review** deliberately gets no repo context. It sees only the diff and PR title. It focuses on code correctness, bugs, readability, security, and error handling — catching things that familiarity with the codebase might cause you to overlook.

The deliberate information asymmetry between the two reviewers produces genuine signal diversity, not redundant LLM calls.

**Synthesis** reconciles both verdicts into a single review that reads as one reviewer's opinion. It never references "both analyses" or "multiple agents." When the two reviews agree, it states the conclusion directly. When they disagree, it picks the stronger position with reasoning. The output is a suggested GitHub review comment you can post as-is or edit.

## Review Modes

Three modes control the cost/quality tradeoff. Each assigns models per pipeline stage:

| Agent | Quick | Standard | Thorough |
|---|---|---|---|
| Injection Scanner | Haiku 4.5 | Haiku 4.5 | Haiku 4.5 |
| Contextual Review | Haiku 4.5 | Sonnet 4.6 | Opus 4.6 |
| Unbiased Review | Haiku 4.5 | Sonnet 4.6 | Opus 4.6 |
| Synthesis | Haiku 4.5 | Opus 4.6 | Opus 4.6 |

The injection scanner uses Haiku in all modes — it's pattern detection on the critical path, not deep reasoning. Standard balances cost and quality: Sonnet for the two review agents, Opus for synthesis where verdict reconciliation matters most. Thorough runs the entire pipeline on Opus except the scanner.

Per-repo model overrides are supported via the agent config API if you want to pin a specific repo to a different model.

## Web UI

The web app is diff-centric. The main review page shows:

- **Verdict bar** with confidence percentage, PR metadata, and rerun/delete controls
- **Synthesis summary** and collapsible PR context panel (description, linked issues, branch, file stats)
- **Diff viewer** with a file tree sidebar (showing additions/deletions/modifications per file) and inline agent comments anchored to specific lines in the diff
- **Tabs** for the suggested review comment (with copy button), key findings with severity, per-agent detail drilldowns, and a feedback form

Other pages: dashboard (recent reviews + registered repos), repo management (register/remove repos, trigger context refresh), and settings (config status, reviewer profile, feedback summary).

No build step. Vanilla HTML/CSS/JS served by FastAPI with Jinja2 templates.

## Browser Extension

A Safari/Chrome extension (Manifest v3) that injects an overlay directly on GitHub PR pages (`github.com/*/pull/*`). It works without leaving GitHub:

- **Badge** next to the PR title shows the review verdict and confidence, or a "Run Review" button with mode selection if no review exists yet
- **Side panel** with the PR brief, verdict, synthesis summary, key findings, and the suggested review comment
- **Inline diff comments** overlaid on GitHub's diff view, positioned at the relevant lines using token-matching heuristics
- **Comment selection** — each inline comment has a checkbox and edit button. Critical and major severity comments are auto-selected. A sticky submit bar lets you post selected comments as a real GitHub review (COMMENT, APPROVE, or REQUEST_CHANGES)
- **Quick feedback** — thumbs up/down on the verdict and severity calibration buttons, all without leaving the PR page

The extension communicates with the local bot server (default `http://localhost:8111`). The popup lets you configure the bot URL and toggle the overlay on/off.

## Repo Management

You register your fork clones. When you paste an upstream PR URL, the bot matches by repo name to find your local code for context.

**Auto-registration** (recommended):

```bash
curl -X POST http://localhost:8111/api/repos/auto \
  -H 'Content-Type: application/json' \
  -d '{"github_user": "youruser", "upstream": "org/repo-name"}'
```

This discovers your local clone under `WORKSPACE_ROOT`, registers both your fork and the upstream, and links them by name.

**Manual registration**:

```bash
curl -X POST http://localhost:8111/api/repos \
  -H 'Content-Type: application/json' \
  -d '{"owner": "youruser", "name": "repo-name", "local_path": "/path/to/clone", "language": "go"}'
```

When you review `https://github.com/org/repo-name/pull/42`, the bot matches by repo name, uses your local clone for context, and calls the GitHub API against the upstream where the PR lives.

## Learning

The bot adapts to your review style through two feedback channels:

**Explicit feedback.** After each review, rate whether the verdict was correct, whether severity was too strict/lenient, and flag missed issues or false positives. Every 10 feedbacks, a pattern extractor analyzes the history and produces strictness trends, tone preferences, common false positives, and recurring misses.

**Implicit feedback (submission tracking).** When you post a review through the extension, the bot records which suggested comments you selected, which you edited, and whether you overrode the verdict. Every 5 submissions, it synthesizes a reviewer directive — a ~400-word profile injected into future contextual review prompts — calibrated with actual acceptance rates per severity level.

Both channels feed into a per-repo reviewer directive that steers future reviews toward your preferences.

## One-Time Action Tickets

When a review completes, the bot creates a burn-after-use ticket. To have the bot act on GitHub (approve, request changes, or comment), use the ticket once via the API or the extension's submit flow. Before executing, the ticket system verifies the PR diff hasn't changed since the review (hash check). After use, the ticket is permanently burnt — no replays.

## Configuration

All config lives in `.env`:

| Variable | Description |
|---|---|
| `GITHUB_TOKEN` | GitHub personal access token (repo read + PR read/write scope) |
| `CLAUDE_BACKEND` | `cli` (default, uses Pro subscription) or `api` (needs API key) |
| `ANTHROPIC_API_KEY` | Only needed if `CLAUDE_BACKEND=api` |
| `APP_PORT` | Server port (default: `8111`) |
| `DB_PATH` | SQLite database path (default: `./data/reviews.db`) |
| `COHERENCE_CRON` | Context refresh schedule (default: `0 9 * * *`) |
| `WORKSPACE_ROOT` | Parent directory of your local repo clones |

Models are assigned per agent per review mode (see [Review Modes](#review-modes)). You don't need to set a model in `.env` — the defaults are already configured. Per-repo overrides are available via the agent config API.

### Per-Repo Agent Config

Customize review behavior per repo via `PUT /api/config/agents/{repo_id}`:

```json
{
  "review_guidelines": "Always check for DCO sign-off",
  "custom_standards": "Follow Kubernetes naming conventions",
  "contextual_focus": ["api-compatibility", "backward-compat"],
  "ignore_patterns": ["generated/*", "vendor/*"],
  "tone": "direct",
  "severity_threshold": "minor",
  "models": {"synthesis": "claude-opus-4-6-20250514"}
}
```

## API

### Reviews

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/review` | Start a review (`{pr_url, review_mode?}`) |
| `GET` | `/api/review/{id}` | Get review results |
| `DELETE` | `/api/review/{id}` | Delete a review |
| `POST` | `/api/review/{id}/rerun` | Re-run a review (clears cache) |
| `GET` | `/api/review/{id}/diff` | Diff text + inline agent comments |
| `POST` | `/api/review/{id}/feedback` | Submit review feedback |

### Repos & Context

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/repos` | Register a repo manually |
| `POST` | `/api/repos/auto` | Auto-discover and register |
| `DELETE` | `/api/repos/{repo_id}` | Remove a repo |
| `POST` | `/api/coherence/refresh/{repo_id}` | Force context refresh |
| `GET` | `/api/coherence/{repo_id}` | View context snapshot |

### Extension

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/ext/review-by-url?pr_url=` | Look up review by PR URL |
| `POST` | `/api/ext/trigger-review` | Start review from extension |
| `GET` | `/api/ext/review-status?review_id=` | Poll in-progress review |
| `POST` | `/api/ext/submit-review` | Post review to GitHub |
| `POST` | `/api/ext/quick-feedback` | Lightweight feedback from extension |
| `GET` | `/api/ext/ping` | Health check |

### Config & Learning

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/config/status` | Config status check |
| `GET/PUT` | `/api/config/agents/{repo_id}` | Get/set per-repo agent config |
| `POST` | `/api/learning/mine` | Mine your GitHub review history |
| `GET` | `/api/learning/profile` | View mined reviewer profile |
| `GET` | `/api/usage/summary?days=30` | Token usage stats |
| `POST` | `/api/tickets/{id}/use` | Burn a one-time action ticket |

## Project Structure

```
sideye/
├── agents/         # Pipeline agents + orchestrator
│   ├── orchestrator.py
│   ├── base.py           # Claude calling (CLI + API backends)
│   ├── injection_scanner.py
│   ├── coherence.py
│   ├── contextual_review.py
│   ├── unbiased_review.py
│   └── synthesis.py
├── app/            # FastAPI app, config, database
├── github/         # GitHub REST client, PR fetcher, data models
├── repo_context/   # Context builder + coding standards detector
├── learning/       # Preference tracker, history miner, pattern extraction
├── tickets/        # One-time action ticket system
├── web/            # Routes, Jinja2 templates, static assets
├── extension/      # Browser extension (Manifest v3)
├── db/             # SQLite schema (8 tables)
└── run.py          # Entry point
```

## Stack

Python 3.10+, FastAPI, SQLite (WAL mode), Anthropic SDK, vanilla HTML/CSS/JS. No build step, no external services beyond GitHub and Claude.

## Contributing

Issues and PRs welcome. This is a personal tool that grew into something publishable — rough edges exist.

## License

[Apache 2.0](LICENSE)
