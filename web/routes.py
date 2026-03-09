"""FastAPI routes for the Sideye web UI."""

import json
import logging
import asyncio
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app import database as db
from app.config import Config
from agents.orchestrator import ReviewPipeline, parse_pr_url
from agents.coherence import CoherenceAgent
from learning.preference_tracker import record_feedback, record_submission
from tickets.one_time_actions import (
    validate_and_use_ticket, TicketError, TicketAlreadyUsedError, TicketDiffChangedError,
)

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="web/templates")

_pool = ThreadPoolExecutor(max_workers=2)

# ── Pages ────────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    repos = db.list_repos()
    reviews = db.list_reviews(limit=20)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "repos": repos,
        "reviews": reviews,
    })


@router.get("/review/{review_id}", response_class=HTMLResponse)
async def review_detail(request: Request, review_id: str):
    review = db.get_review(review_id)
    if not review:
        raise HTTPException(404, "Review not found")
    results = db.get_review_results(review_id)
    # Group results by agent type
    results_by_agent = {r["agent_type"]: r for r in results}

    # Fetch cached PR data for context (description, linked issues, etc.)
    pr_data = db.get_cached_pr(review["repo_id"], review["pr_number"], max_age_hours=999999)

    return templates.TemplateResponse("review.html", {
        "request": request,
        "review": review,
        "results": results_by_agent,
        "results_list": results,
        "pr_data": pr_data,
    })


@router.get("/review/{review_id}/agent/{agent_type}", response_class=HTMLResponse)
async def agent_detail(request: Request, review_id: str, agent_type: str):
    review = db.get_review(review_id)
    if not review:
        raise HTTPException(404, "Review not found")
    results = db.get_review_results(review_id)
    result = None
    for r in results:
        if r["agent_type"] == agent_type:
            result = r
            break
    if not result:
        raise HTTPException(404, f"No result for agent '{agent_type}' in this review")

    # Get context snapshot if available (for contextual agent)
    local_repo_id = review["repo_id"]
    # Try to find the fork's repo record for context
    repo_name = local_repo_id.split("/")[-1] if "/" in local_repo_id else local_repo_id
    repo_record = db.get_repo(local_repo_id) or db.find_repo_by_name(repo_name)
    context_repo_id = repo_record["repo_id"] if repo_record else local_repo_id
    context = db.get_latest_snapshot(context_repo_id)

    return templates.TemplateResponse("agent_detail.html", {
        "request": request,
        "review": review,
        "result": result,
        "agent_type": agent_type,
        "context": context,
        "results_by_agent": {r["agent_type"]: r for r in results},
    })


@router.get("/repos", response_class=HTMLResponse)
async def repos_page(request: Request):
    repos = db.list_repos()

    # Merge upstream + fork entries that share the same repo name.
    # The fork (with local_path) is the primary entry; the upstream is noted as a linked upstream.
    by_name: dict[str, list[dict]] = {}
    for r in repos:
        if r["repo_id"] == "__global__":
            continue
        name = r["name"]
        if name not in by_name:
            by_name[name] = []
        by_name[name].append(r)

    merged_repos = []
    seen_ids = set()
    for name, group in by_name.items():
        if len(group) == 1:
            merged_repos.append(group[0])
            seen_ids.add(group[0]["repo_id"])
        else:
            # Multiple entries with same name — pick the one with local_path as primary
            primary = None
            upstreams = []
            for r in group:
                if r.get("local_path"):
                    primary = r
                else:
                    upstreams.append(r)
            if not primary:
                primary = group[0]
                upstreams = group[1:]
            # Annotate primary with upstream info
            primary = dict(primary)  # copy
            primary["_upstreams"] = [u["repo_id"] for u in upstreams]
            merged_repos.append(primary)
            seen_ids.add(primary["repo_id"])
            for u in upstreams:
                seen_ids.add(u["repo_id"])

    return templates.TemplateResponse("repos.html", {
        "request": request,
        "repos": merged_repos,
    })


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    repos = db.list_repos()
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "repos": repos,
    })


# ── API: Review ──────────────────────────────────────────────────


@router.post("/api/review")
async def start_review(request: Request):
    """Start a PR review. Accepts JSON: {"pr_url": "https://github.com/..."}"""
    body = await request.json()
    pr_url = body.get("pr_url", "").strip()
    review_mode = body.get("review_mode", "standard")
    if not pr_url:
        raise HTTPException(400, "pr_url is required")
    if review_mode not in ("quick", "standard", "thorough"):
        raise HTTPException(400, f"Invalid review_mode: {review_mode}")

    try:
        parse_pr_url(pr_url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # Run the pipeline in a thread to not block
    loop = asyncio.get_event_loop()
    pipeline = ReviewPipeline()

    # Store progress events for SSE
    progress_events = []

    def on_progress(stage, msg):
        progress_events.append({"stage": stage, "message": msg})

    result = await loop.run_in_executor(
        _pool,
        lambda: pipeline.review_pr_sync(pr_url, progress_callback=on_progress,
                                         review_mode=review_mode),
    )

    return result


@router.get("/api/review/{review_id}")
async def get_review_api(review_id: str):
    review = db.get_review(review_id)
    if not review:
        raise HTTPException(404, "Review not found")
    results = db.get_review_results(review_id)
    return {
        "review": review,
        "results": results,
    }


@router.delete("/api/review/{review_id}")
async def delete_review(review_id: str):
    """Delete a review and all its child records."""
    review = db.get_review(review_id)
    if not review:
        raise HTTPException(404, "Review not found")
    db.delete_review(review_id)
    return {"status": "ok"}


@router.post("/api/review/{review_id}/rerun")
async def rerun_review(review_id: str):
    """Delete the old review and re-run the pipeline on the same PR."""
    review = db.get_review(review_id)
    if not review:
        raise HTTPException(404, "Review not found")

    pr_url = review.get("pr_url", "")
    if not pr_url:
        raise HTTPException(400, "Original PR URL not found on this review")

    # Delete old review first
    db.delete_review(review_id)

    # Also clear the PR data cache so we get a fresh diff
    db.clear_pr_cache(review["repo_id"], review["pr_number"])

    # Re-run pipeline
    loop = asyncio.get_event_loop()
    pipeline = ReviewPipeline()
    result = await loop.run_in_executor(
        _pool,
        lambda: pipeline.review_pr_sync(pr_url),
    )
    return result


@router.get("/api/review/{review_id}/diff")
async def get_review_diff(review_id: str):
    """Return parsed diff + agent comments anchored to files for the diff viewer."""
    review = db.get_review(review_id)
    if not review:
        raise HTTPException(404, "Review not found")

    # Get cached diff — use a very long max_age since the diff won't change
    cached = db.get_cached_pr(review["repo_id"], review["pr_number"], max_age_hours=999999)
    diff_text = cached.get("diff_content", "") if cached else ""

    # Collect all agent comments anchored to files
    results = db.get_review_results(review_id)
    comments = []
    for r in results:
        agent = r["agent_type"]
        details = r.get("details", {})
        if isinstance(details, str):
            import json as _json
            try:
                details = _json.loads(details)
            except Exception:
                details = {}
        # Both agents use "detailed_comments" and/or "bugs"
        for c in details.get("detailed_comments", []):
            comments.append({
                "agent": agent,
                "file": c.get("file", ""),
                "line_hint": c.get("line_hint", ""),
                "comment": c.get("comment", ""),
                "severity": c.get("severity", "info"),
                "suggestion": c.get("suggestion", ""),
                "type": c.get("type", "review_comment"),
            })
        for b in details.get("bugs", []):
            comments.append({
                "agent": agent,
                "file": b.get("file", ""),
                "line_hint": b.get("line_hint", ""),
                "comment": b.get("description", ""),
                "severity": b.get("severity", "major"),
                "suggestion": b.get("suggestion", ""),
                "type": "review_comment",
            })

    return {"diff": diff_text, "comments": comments}


@router.get("/api/review/{review_id}/stream")
async def review_stream(review_id: str):
    """SSE endpoint to stream review progress (for future use)."""
    # Placeholder — for now, reviews are fetched after completion
    async def event_generator():
        yield f"data: {json.dumps({'status': 'check', 'review_id': review_id})}\n\n"
        # Poll for completion
        for _ in range(60):
            review = db.get_review(review_id)
            if review and review["status"] in ("complete", "flagged"):
                yield f"data: {json.dumps({'status': review['status']})}\n\n"
                return
            await asyncio.sleep(1)
            yield f"data: {json.dumps({'status': 'pending'})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ── API: Repos ───────────────────────────────────────────────────


@router.post("/api/repos")
async def add_repo(request: Request):
    body = await request.json()
    owner = body.get("owner", "").strip()
    name = body.get("name", "").strip()
    local_path = body.get("local_path", "").strip()
    language = body.get("language", "").strip()

    if not owner or not name:
        raise HTTPException(400, "owner and name are required")

    repo_id = db.upsert_repo(
        owner=owner,
        name=name,
        url=f"https://github.com/{owner}/{name}",
        local_path=local_path,
        language=language,
    )
    return {"repo_id": repo_id, "status": "ok"}


@router.post("/api/repos/auto")
async def add_repo_auto(request: Request):
    """Smart repo registration. Takes github_user + upstream, auto-discovers local clone.

    Input: {"github_user": "maroonay", "upstream": "llm-d/llm-d-inference-scheduler"}
    """
    body = await request.json()
    github_user = body.get("github_user", "").strip()
    upstream = body.get("upstream", "").strip().strip("/")

    if not github_user or not upstream:
        raise HTTPException(400, "github_user and upstream are required")

    # Parse upstream: "llm-d/llm-d-inference-scheduler" or just "llm-d-inference-scheduler"
    if "/" in upstream:
        upstream_owner, repo_name = upstream.split("/", 1)
    else:
        upstream_owner = ""
        repo_name = upstream

    if not repo_name:
        raise HTTPException(400, "Could not parse repo name from upstream")

    # Try to find local clone under WORKSPACE_ROOT
    workspace = Path(Config.WORKSPACE_ROOT)
    local_path = ""
    language = ""

    # Check common locations: workspace/repo_name, workspace/github_user/repo_name
    candidates = [
        workspace / repo_name,
        workspace / github_user / repo_name,
    ]
    for candidate in candidates:
        if candidate.is_dir() and (candidate / ".git").exists():
            local_path = str(candidate)
            break

    # Auto-detect language from local clone
    if local_path:
        lp = Path(local_path)
        if (lp / "go.mod").exists():
            language = "go"
        elif (lp / "pyproject.toml").exists() or (lp / "setup.py").exists():
            language = "python"
        elif (lp / "package.json").exists():
            language = "javascript"

    # Register the user's fork (owner = github_user)
    fork_repo_id = db.upsert_repo(
        owner=github_user,
        name=repo_name,
        url=f"https://github.com/{github_user}/{repo_name}",
        local_path=local_path,
        language=language,
    )

    result = {
        "repo_id": fork_repo_id,
        "status": "ok",
        "local_path": local_path or None,
        "language": language or None,
    }

    # Also register upstream if different (needed for FK references when reviewing upstream PRs)
    if upstream_owner and upstream_owner != github_user:
        db.upsert_repo(
            owner=upstream_owner,
            name=repo_name,
            url=f"https://github.com/{upstream_owner}/{repo_name}",
        )
        result["upstream_registered"] = f"{upstream_owner}/{repo_name}"

    return result


@router.delete("/api/repos/{repo_id:path}")
async def remove_repo(repo_id: str):
    # Soft: just remove from DB
    with db.get_db() as conn:
        conn.execute("DELETE FROM repositories WHERE repo_id=?", (repo_id,))
    return {"status": "ok"}


# ── API: Coherence ───────────────────────────────────────────────


@router.post("/api/coherence/refresh/{repo_id:path}")
async def refresh_coherence(repo_id: str):
    repo = db.get_repo(repo_id)
    if not repo:
        raise HTTPException(404, f"Repo {repo_id} not found")
    if not repo.get("local_path"):
        raise HTTPException(400, f"Repo {repo_id} has no local_path configured")

    agent = CoherenceAgent()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        _pool,
        lambda: agent.run(repo_id=repo_id, local_path=repo["local_path"], force=True),
    )
    return {
        "status": result.status,
        "summary": result.summary,
        "execution_time_ms": result.execution_time_ms,
    }


@router.get("/api/coherence/{repo_id:path}")
async def get_coherence(repo_id: str):
    snapshot = db.get_latest_snapshot(repo_id)
    if not snapshot:
        raise HTTPException(404, "No context snapshot found")
    return snapshot


# ── API: Feedback ────────────────────────────────────────────────


@router.post("/api/review/{review_id}/feedback")
async def submit_feedback(review_id: str, request: Request):
    body = await request.json()
    try:
        pref_id = record_feedback(review_id, body)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"pref_id": pref_id, "status": "ok"}


# ── API: History Mining ──────────────────────────────────────────


@router.post("/api/learning/mine")
async def mine_history(request: Request):
    """Mine GitHub review history and extract preferences."""
    body = await request.json() if await request.body() else {}
    max_prs = body.get("max_prs", 50)
    repos_filter = body.get("repos")  # optional: ["llm-d/llm-d-kv-cache", ...]

    from learning.history_miner import run_history_mine

    loop = asyncio.get_event_loop()
    profile = await loop.run_in_executor(
        _pool,
        lambda: run_history_mine(max_prs=max_prs, repos=repos_filter),
    )

    return {"status": "ok", "profile": profile}


@router.get("/api/learning/profile")
async def get_profile():
    """Get the current mined reviewer profile."""
    global_prefs = db.get_preferences("__global__", category="mined_profile")
    if not global_prefs:
        return {"status": "not_mined", "profile": None}
    return {
        "status": "ok",
        "profile": global_prefs[0].get("feedback_data", {}),
        "mined_at": global_prefs[0].get("created_at"),
    }


@router.get("/api/learning/feedback-summary")
async def feedback_summary():
    """Summary of all feedback the user has given on reviews."""
    with db.get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) as c FROM review_preferences WHERE category='feedback'"
        ).fetchone()["c"]
        reviews = conn.execute(
            "SELECT COUNT(DISTINCT repo_id) as c FROM review_preferences WHERE category='feedback'"
        ).fetchone()["c"]
    return {
        "total": total,
        "review_count": reviews,
    }


# ── API: Config ──────────────────────────────────────────────────


@router.get("/api/config/status")
async def config_status():
    """Return current config status (no secrets exposed)."""
    return {
        "github_token": bool(Config.GITHUB_TOKEN),
        "api_key": bool(Config.ANTHROPIC_API_KEY),
        "backend": Config.CLAUDE_BACKEND,
        "model": Config.CLAUDE_MODEL,
    }


# ── API: Agent Config ────────────────────────────────────────────


@router.get("/api/config/agents/{repo_id:path}")
async def get_agent_config(repo_id: str):
    """Get agent configuration for a repo."""
    config = db.get_agent_config(repo_id)
    if not config:
        # Return defaults
        return {
            "repo_id": repo_id,
            "configured": False,
            "config": {
                "review_guidelines": "",
                "custom_standards": "",
                "contextual_focus": [],
                "unbiased_focus": [],
                "ignore_patterns": [],
                "tone": "direct",
                "severity_threshold": "nit",
            },
        }
    return {"repo_id": repo_id, "configured": True, "config": config}


@router.put("/api/config/agents/{repo_id:path}")
async def set_agent_config(repo_id: str, request: Request):
    """Set or update agent configuration for a repo.

    Accepts JSON with any subset of config fields:
    {
        "review_guidelines": "Focus on security and test coverage. Ignore minor style issues.",
        "custom_standards": "All functions must have docstrings. Use type hints everywhere.",
        "contextual_focus": ["scope_alignment", "coherence", "test_coverage"],
        "unbiased_focus": ["security", "error_handling", "race_conditions"],
        "ignore_patterns": ["*.generated.go", "vendor/", "testdata/"],
        "tone": "direct",
        "severity_threshold": "minor"
    }
    """
    body = await request.json()

    # Merge with existing config (or defaults)
    existing = db.get_agent_config(repo_id) or {}
    merged = {
        "review_guidelines": body.get("review_guidelines", existing.get("review_guidelines", "")),
        "custom_standards": body.get("custom_standards", existing.get("custom_standards", "")),
        "contextual_focus": body.get("contextual_focus", existing.get("contextual_focus", [])),
        "unbiased_focus": body.get("unbiased_focus", existing.get("unbiased_focus", [])),
        "ignore_patterns": body.get("ignore_patterns", existing.get("ignore_patterns", [])),
        "tone": body.get("tone", existing.get("tone", "direct")),
        "severity_threshold": body.get("severity_threshold", existing.get("severity_threshold", "nit")),
    }

    pid = db.save_agent_config(repo_id, merged)
    return {"status": "ok", "pref_id": pid, "config": merged}


@router.get("/api/config/agents")
async def list_agent_configs():
    """List all repos that have agent config."""
    repos = db.list_repos()
    configs = []
    for r in repos:
        if r["repo_id"] == "__global__":
            continue
        config = db.get_agent_config(r["repo_id"])
        if config:
            configs.append({"repo_id": r["repo_id"], "config": config})
    return {"configs": configs}


# ── API: Extension ───────────────────────────────────────────────


@router.get("/api/ext/review-by-url")
async def ext_review_by_url(pr_url: str):
    """Look up a review by PR URL. Used by the browser extension.

    Returns the full review with synthesis + inline comments for overlay.
    """
    if not pr_url:
        raise HTTPException(400, "pr_url is required")

    # Find the review
    try:
        owner, repo, pr_number = parse_pr_url(pr_url)
    except ValueError:
        raise HTTPException(400, "Invalid PR URL")

    repo_id = f"{owner}/{repo}"

    # Check if we have a review for this PR
    with db.get_db() as conn:
        row = conn.execute(
            """SELECT * FROM pr_reviews
               WHERE repo_id=? AND pr_number=?
               ORDER BY created_at DESC LIMIT 1""",
            (repo_id, pr_number),
        ).fetchone()

    if not row:
        return {"found": False}

    review = dict(row)
    review_id = review["review_id"]

    results = db.get_review_results(review_id)
    results_by_agent = {r["agent_type"]: r for r in results}

    # Collect inline comments from all agents
    comments = []
    for r in results:
        details = r.get("details", {})
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except Exception:
                details = {}
        for c in details.get("detailed_comments", []):
            comments.append({
                "agent": r["agent_type"],
                "file": c.get("file", ""),
                "line_hint": c.get("line_hint", ""),
                "comment": c.get("comment", ""),
                "severity": c.get("severity", "info"),
                "type": c.get("type", "review_comment"),
            })
        for b in details.get("bugs", []):
            # Bugs are always review_comments — they're actionable
            comments.append({
                "agent": r["agent_type"],
                "file": b.get("file", ""),
                "line_hint": b.get("line_hint", ""),
                "comment": b.get("description", ""),
                "severity": b.get("severity", "major"),
                "suggestion": b.get("suggestion", ""),
                "type": "review_comment",
            })

    synthesis = results_by_agent.get("synthesis", {})
    syn_details = synthesis.get("details", {}) if synthesis else {}

    # Token usage summary from review_results
    total_tokens = 0
    model_used = ""
    tokens_estimated = False
    for r in results:
        total_tokens += (r.get("input_tokens") or 0) + (r.get("output_tokens") or 0)
        if r.get("model_used") and not model_used:
            model_used = r["model_used"]
        if r.get("tokens_estimated"):
            tokens_estimated = True

    return {
        "found": True,
        "review_id": review_id,
        "status": review["status"],
        "verdict": synthesis.get("verdict", "?"),
        "confidence": synthesis.get("confidence", 0),
        "summary": synthesis.get("summary", ""),
        "pr_brief": syn_details.get("pr_brief"),
        "suggested_comment": syn_details.get("suggested_review_comment", ""),
        "key_findings": syn_details.get("key_findings", []),
        "inline_comments": comments,
        "review_url": f"/review/{review_id}",
        "usage": {
            "total_tokens": total_tokens,
            "model": model_used,
            "estimated": tokens_estimated,
        },
    }


@router.post("/api/ext/trigger-review")
async def ext_trigger_review(request: Request):
    """Trigger a review from the browser extension.

    Auto-registers the repo if needed, kicks off the pipeline in the background,
    returns a review_id that can be polled for status.
    Accepts JSON: {"pr_url": "https://github.com/owner/repo/pull/123"}
    """
    body = await request.json()
    pr_url = body.get("pr_url", "").strip()
    review_mode = body.get("review_mode", "standard")
    if not pr_url:
        raise HTTPException(400, "pr_url is required")
    if review_mode not in ("quick", "standard", "thorough"):
        review_mode = "standard"

    try:
        owner, repo_name, pr_number = parse_pr_url(pr_url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    repo_id = f"{owner}/{repo_name}"

    # Auto-register the repo if we don't know about it
    existing = db.get_repo(repo_id)
    if not existing:
        db.upsert_repo(owner=owner, name=repo_name,
                        url=f"https://github.com/{owner}/{repo_name}")
        logger.info("Auto-registered repo %s from extension trigger", repo_id)

    # Clean up any stale in_progress reviews (from previously crashed runs).
    # If there's an in_progress review older than 15 minutes, mark it failed.
    with db.get_db() as conn:
        conn.execute(
            """UPDATE pr_reviews SET status='error'
               WHERE repo_id=? AND pr_number=? AND status='in_progress'
               AND created_at < datetime('now', '-15 minutes')""",
            (repo_id, pr_number),
        )

    # Check for a genuinely running review (created recently)
    with db.get_db() as conn:
        row = conn.execute(
            """SELECT * FROM pr_reviews
               WHERE repo_id=? AND pr_number=? AND status='in_progress'
               ORDER BY created_at DESC LIMIT 1""",
            (repo_id, pr_number),
        ).fetchone()
    if row:
        return {"review_id": row["review_id"], "status": "in_progress",
                "message": "Review already running"}

    # Run the pipeline in a background thread.
    # Use run_in_executor directly — the thread pool handles concurrency.
    logger.info("Extension trigger: starting review for %s PR #%s", repo_id, pr_number)

    def _run_pipeline():
        try:
            pipeline = ReviewPipeline()
            pipeline.review_pr_sync(pr_url, review_mode=review_mode)
            logger.info("Extension trigger: review complete for %s PR #%s", repo_id, pr_number)
        except Exception as e:
            logger.error("Extension-triggered review failed for %s PR #%s: %s",
                         repo_id, pr_number, e, exc_info=True)
            # Mark any in_progress review as error so polling stops
            try:
                with db.get_db() as conn:
                    row = conn.execute(
                        """SELECT review_id FROM pr_reviews
                           WHERE repo_id=? AND pr_number=? AND status='in_progress'
                           ORDER BY created_at DESC LIMIT 1""",
                        (repo_id, pr_number),
                    ).fetchone()
                if row:
                    db.update_review_status(
                        row["review_id"], "error",
                        error_message=str(e)[:1000],
                    )
            except Exception:
                pass

    loop = asyncio.get_event_loop()
    loop.run_in_executor(_pool, _run_pipeline)

    # Wait for the review record to be created.
    # review_pr_sync creates it after fetching the PR from GitHub, which can
    # take a few seconds.  Poll the DB briefly rather than a fixed sleep.
    review_id = None
    for _ in range(12):  # Up to ~6 seconds
        await asyncio.sleep(0.5)
        with db.get_db() as conn:
            row = conn.execute(
                """SELECT review_id FROM pr_reviews
                   WHERE repo_id=? AND pr_number=? AND status='in_progress'
                   ORDER BY created_at DESC LIMIT 1""",
                (repo_id, pr_number),
            ).fetchone()
        if row:
            review_id = row["review_id"]
            break

    if not review_id:
        # Pipeline hasn't created a review record yet — that's OK, return
        # a sentinel so the extension knows to poll by URL instead
        return {"review_id": None, "status": "starting",
                "message": "Review is starting — poll by URL"}

    return {"review_id": review_id, "status": "in_progress"}


@router.get("/api/ext/review-status")
async def ext_review_status(review_id: str):
    """Poll review status. Used by extension after triggering a review."""
    review = db.get_review(review_id)
    if not review:
        raise HTTPException(404, "Review not found")
    resp = {"review_id": review_id, "status": review["status"] or "in_progress"}
    if review.get("error_message"):
        resp["error_message"] = review["error_message"]
    return resp


@router.post("/api/ext/submit-review")
async def ext_submit_review(request: Request):
    """Submit selected bot comments as a real GitHub PR review.

    Accepts JSON:
    {
      "pr_url": "https://github.com/owner/repo/pull/123",
      "event": "COMMENT",   // or "APPROVE" or "REQUEST_CHANGES"
      "body": "",            // optional top-level review body
      "comments": [
        {"file": "pkg/foo.go", "line_hint": "func New(", "comment": "edited text"}
      ]
    }
    """
    from github.client import GitHubClient
    from github.diff_utils import resolve_line_positions

    body = await request.json()
    pr_url = body.get("pr_url", "").strip()
    event = body.get("event", "COMMENT").upper()
    review_body = body.get("body", "")
    comments = body.get("comments", [])

    if not pr_url:
        raise HTTPException(400, "pr_url is required")
    if not comments:
        raise HTTPException(400, "At least one comment is required")
    if event not in ("COMMENT", "APPROVE", "REQUEST_CHANGES"):
        raise HTTPException(400, f"Invalid event: {event}")

    try:
        owner, repo_name, pr_number = parse_pr_url(pr_url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # Get the diff to resolve line_hint → line numbers
    gh = GitHubClient()
    try:
        diff_text = gh.get_pr_diff(owner, repo_name, pr_number)
    except Exception as e:
        logger.error("Failed to fetch diff for %s/%s#%s: %s", owner, repo_name, pr_number, e)
        raise HTTPException(502, f"Failed to fetch PR diff: {e}")

    # Resolve line_hint → (path, line) for each comment
    resolved = resolve_line_positions(diff_text, comments)

    if not resolved:
        raise HTTPException(
            422,
            "Could not resolve any comments to diff line positions. "
            "The line hints may not match the current diff."
        )

    logger.info(
        "Submitting review for %s/%s#%s: event=%s, %d/%d comments resolved",
        owner, repo_name, pr_number, event,
        len(resolved), len(comments),
    )

    # Format for GitHub API: {path, line, body} (side is optional, defaults to RIGHT)
    gh_comments = []
    for r in resolved:
        gc = {"path": r["path"], "line": r["line"], "body": r["body"]}
        if r.get("side") == "LEFT":
            gc["side"] = "LEFT"
        gh_comments.append(gc)

    try:
        result = gh.post_pr_review(
            owner=owner,
            repo=repo_name,
            pr_number=pr_number,
            event=event,
            body=review_body,
            comments=gh_comments,
        )
        review_url = result.get("html_url", "")
        logger.info("Review posted: %s", review_url)

        # Record submission for adaptive learning (never fails the actual submission)
        submission_review_id = body.get("review_id")
        all_suggested = body.get("all_suggested_comments")
        suggested_verdict = body.get("suggested_verdict")
        if submission_review_id and all_suggested and suggested_verdict:
            try:
                repo_id = f"{owner}/{repo_name}"
                record_submission(
                    review_id=submission_review_id,
                    repo_id=repo_id,
                    pr_number=pr_number,
                    suggested_verdict=suggested_verdict,
                    chosen_verdict=event.lower(),
                    all_suggested_comments=all_suggested,
                    selected_comments=comments,
                )
                logger.info("Recorded submission for review %s", submission_review_id)
            except Exception as sub_err:
                logger.warning("Failed to record submission (non-fatal): %s", sub_err)

        return {
            "status": "ok",
            "review_url": review_url,
            "posted_count": len(gh_comments),
            "skipped_count": len(comments) - len(resolved),
        }
    except Exception as e:
        logger.error("Failed to post review: %s", e)
        raise HTTPException(502, f"GitHub API error: {e}")


@router.get("/api/ext/ping")
async def ext_ping():
    """Health check for the extension to verify bot is running."""
    return {"status": "ok", "version": "0.1.0"}


@router.get("/api/ext/review-context/{review_id}")
async def ext_review_context(review_id: str):
    """Return display-friendly summary of what context went into a review.

    Used by the extension panel's "Context" expandable section.
    """
    review = db.get_review(review_id)
    if not review:
        raise HTTPException(404, "Review not found")

    repo_id = review["repo_id"]
    # Find the local fork's repo record for context lookups
    repo_name = repo_id.split("/")[-1] if "/" in repo_id else repo_id
    repo_record = db.get_repo(repo_id) or db.find_repo_by_name(repo_name)
    context_repo_id = repo_record["repo_id"] if repo_record else repo_id

    result = {}

    # Repo standards from context snapshot
    snapshot = db.get_latest_snapshot(context_repo_id)
    if snapshot:
        standards = snapshot.get("coding_standards", {})
        if isinstance(standards, str):
            try:
                standards = json.loads(standards)
            except Exception:
                standards = {}
        parts = []
        if standards.get("linters"):
            parts.append(f"Linters: {', '.join(standards['linters'])}")
        if standards.get("formatters"):
            parts.append(f"Formatters: {', '.join(standards['formatters'])}")
        if standards.get("test_frameworks"):
            parts.append(f"Tests: {', '.join(standards['test_frameworks'])}")
        if standards.get("language"):
            parts.insert(0, f"Language: {standards['language']}")
        result["standards"] = "; ".join(parts) if parts else None

        # Snapshot age
        built_at = snapshot.get("built_at", "")
        if built_at:
            result["snapshot_age"] = built_at[:16].replace("T", " ") + " UTC"
    else:
        result["standards"] = None

    # Mined reviewer profile
    global_prefs = db.get_preferences("__global__", category="mined_profile")
    if global_prefs:
        profile_data = global_prefs[0].get("feedback_data", {})
        style = profile_data.get("review_style", {})
        parts = []
        if style.get("strictness"):
            parts.append(f"Strictness: {style['strictness']}")
        if style.get("tone"):
            parts.append(f"Tone: {style['tone']}")
        if style.get("focus_areas"):
            parts.append(f"Focus: {', '.join(style['focus_areas'][:5])}")
        if style.get("lets_slide"):
            parts.append(f"Lenient on: {', '.join(style['lets_slide'][:3])}")
        result["profile"] = "; ".join(parts) if parts else None
    else:
        result["profile"] = None

    # Learned patterns from feedback
    learned = db.get_preferences(context_repo_id, category="learned_patterns")
    if not learned:
        learned = db.get_preferences("__global__", category="learned_patterns")
    if learned:
        patterns = learned[0].get("feedback_data", {})
        adjustments = patterns.get("adjustments", [])
        result["learned"] = "; ".join(adjustments[:5]) if adjustments else None
    else:
        result["learned"] = None

    # Agent config
    agent_config = db.get_agent_config(context_repo_id) or db.get_agent_config(repo_id)
    if agent_config:
        parts = []
        if agent_config.get("tone"):
            parts.append(f"Tone: {agent_config['tone']}")
        if agent_config.get("severity_threshold"):
            parts.append(f"Min severity: {agent_config['severity_threshold']}")
        if agent_config.get("review_guidelines"):
            parts.append(agent_config["review_guidelines"][:100])
        result["agent_config"] = "; ".join(parts) if parts else None
    else:
        result["agent_config"] = None

    return result


@router.post("/api/ext/quick-feedback")
async def ext_quick_feedback(request: Request):
    """Lightweight feedback from the extension. Accepts verdict_correct + severity_assessment.

    Wraps the existing record_feedback() with minimal fields.
    """
    body = await request.json()
    review_id = body.get("review_id", "").strip()
    if not review_id:
        raise HTTPException(400, "review_id is required")

    feedback = {}
    if "verdict_correct" in body:
        feedback["verdict_correct"] = body["verdict_correct"]
    if "severity_assessment" in body:
        feedback["severity_assessment"] = body["severity_assessment"]

    if not feedback:
        raise HTTPException(400, "At least verdict_correct is required")

    # Default tone_assessment to avoid pattern extraction issues
    feedback.setdefault("tone_assessment", "appropriate")
    feedback["source"] = "extension_quick"

    try:
        pref_id = record_feedback(review_id, feedback)
    except ValueError as e:
        raise HTTPException(404, str(e))

    return {"pref_id": pref_id, "status": "ok"}


# ── API: Usage Tracking ──────────────────────────────────────────


@router.get("/api/usage/summary")
async def usage_summary(days: int = 30):
    """Aggregate usage stats over the last N days."""
    if days < 1 or days > 365:
        raise HTTPException(400, "days must be between 1 and 365")
    return db.get_usage_summary(days)


@router.get("/api/usage/review/{review_id}")
async def review_usage(review_id: str):
    """Per-agent usage for a single review."""
    review = db.get_review(review_id)
    if not review:
        raise HTTPException(404, "Review not found")
    return {"review_id": review_id, "usage": db.get_review_usage(review_id)}


# ── API: Tickets ─────────────────────────────────────────────────


@router.post("/api/tickets/{ticket_id}/use")
async def use_ticket(ticket_id: str):
    try:
        result = validate_and_use_ticket(ticket_id)
        return result
    except TicketAlreadyUsedError as e:
        raise HTTPException(409, str(e))
    except TicketDiffChangedError as e:
        raise HTTPException(409, str(e))
    except TicketError as e:
        raise HTTPException(400, str(e))
