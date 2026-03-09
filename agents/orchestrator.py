"""Pipeline orchestrator — coordinates the multi-agent review flow.

Sequence:
1. Injection scan (blocking gate)
2. Context check/refresh
3. Contextual + Unbiased reviews (parallel)
4. Synthesis (after both complete)
"""

import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from agents.injection_scanner import InjectionScannerAgent
from agents.coherence import CoherenceAgent
from agents.contextual_review import ContextualReviewAgent
from agents.unbiased_review import UnbiasedReviewAgent
from agents.synthesis import SynthesisAgent
from agents.base import AgentResult
from github.client import GitHubClient
from github.pr_fetcher import PRFetcher, diff_hash
from github.models import PRInfo
from app import database as db
from app.config import Config

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=4)


def _resolve_models(review_mode: str, agent_config: dict) -> dict[str, str]:
    """Resolve which model each agent should use.

    Priority: review_mode presets > agent_config.models > Config.CLAUDE_MODEL
    Returns dict mapping agent_type -> model string.
    """
    # Start with global default
    agent_types = ["injection_scanner", "contextual_review", "unbiased_review", "synthesis"]
    models = {a: "" for a in agent_types}  # empty string = use call_claude default

    # Layer 1: review mode preset
    mode_config = Config.REVIEW_MODES.get(review_mode, {})
    if mode_config:
        default_model = mode_config.get("default", "")
        for a in agent_types:
            models[a] = mode_config.get(a, default_model)

    # Layer 2: per-agent-type config (overrides mode for specific agents)
    config_models = agent_config.get("models", {})
    for a in agent_types:
        if a in config_models:
            models[a] = config_models[a]

    return models


def parse_pr_url(url: str) -> tuple[str, str, int]:
    """Extract (owner, repo, pr_number) from a GitHub PR URL."""
    m = re.match(
        r"https?://github\.com/([\w\-\.]+)/([\w\-\.]+)/pull/(\d+)",
        url.strip(),
    )
    if not m:
        raise ValueError(f"Invalid GitHub PR URL: {url}")
    return m.group(1), m.group(2), int(m.group(3))


class ReviewPipeline:
    """Orchestrates the full multi-agent review flow."""

    def __init__(self):
        self.injection_scanner = InjectionScannerAgent()
        self.coherence_agent = CoherenceAgent()
        self.contextual_agent = ContextualReviewAgent()
        self.unbiased_agent = UnbiasedReviewAgent()
        self.synthesis_agent = SynthesisAgent()
        self.pr_fetcher = PRFetcher()

    def review_pr_sync(self, pr_url: str,
                       progress_callback=None,
                       review_mode: str = "standard") -> dict:
        """
        Run the full review pipeline synchronously.
        Returns a dict with all results.

        review_mode: "quick" (haiku), "standard" (sonnet), "thorough" (opus synthesis)
        progress_callback(stage: str, message: str) is called at each step.
        """
        def progress(stage, msg):
            logger.info("[%s] %s", stage, msg)
            if progress_callback:
                progress_callback(stage, msg)

        # ── Parse URL & fetch PR ─────────────────────────────────
        progress("fetch", "Parsing PR URL...")
        owner, repo, pr_number = parse_pr_url(pr_url)
        repo_id = f"{owner}/{repo}"  # upstream identity (used for GitHub API)

        # Resolve local clone: the PR URL points to upstream (e.g., llm-d/repo)
        # but the user's local clone is their fork (e.g., maroonay/repo).
        # We match by repo name to find the local path + context.
        repo_record = db.get_repo(repo_id)
        if not repo_record:
            repo_record = db.find_repo_by_name(repo)
            if repo_record:
                progress("fetch",
                         f"Matched upstream {repo_id} → local clone "
                         f"{repo_record['repo_id']} ({repo_record.get('local_path', '')})")

        if not repo_record:
            # No fork registered — register upstream directly (context will be skipped)
            db.upsert_repo(
                owner=owner, name=repo,
                url=f"https://github.com/{owner}/{repo}",
            )

        # Always ensure the upstream repo_id exists in DB (needed for PR cache FK).
        # This is a no-op if it's already registered (e.g., user reviews upstream directly).
        if repo_record and repo_record["repo_id"] != repo_id:
            db.upsert_repo(
                owner=owner, name=repo,
                url=f"https://github.com/{owner}/{repo}",
            )

        # local_repo_id tracks which registered repo provides context
        local_repo_id = repo_record["repo_id"] if repo_record else repo_id

        progress("fetch", f"Fetching PR #{pr_number} from {repo_id}...")
        pr = self.pr_fetcher.fetch(owner, repo, pr_number)

        # Create review record
        review_id = db.create_review(
            repo_id=repo_id,
            pr_number=pr_number,
            pr_title=pr.title,
            pr_author=pr.author,
            pr_url=pr.url,
            branch=pr.head_branch,
        )

        # ── Step 1+2: Injection Scan + Context Build (parallel) ──
        # These are independent — run concurrently to save 5-30s.
        progress("injection_scan", "Scanning for injection + checking context...")
        local_path = (repo_record or {}).get("local_path", "")

        # Resolve injection model early (agent_config not loaded yet)
        inj_model = _resolve_models(review_mode, {}).get("injection_scanner", "")

        def _run_injection_scan():
            return self.injection_scanner.run(
                pr_title=pr.title,
                pr_description=pr.description,
                diff=pr.diff,
                model=inj_model,
            )

        def _build_context():
            ctx = db.get_latest_snapshot(local_repo_id)
            if not ctx and local_path:
                coh_result = self.coherence_agent.run(
                    repo_id=local_repo_id, local_path=local_path, force=True,
                )
                ctx = db.get_latest_snapshot(local_repo_id)
                return ctx, coh_result.summary
            return ctx, None

        with ThreadPoolExecutor(max_workers=2) as scan_pool:
            inj_future = scan_pool.submit(_run_injection_scan)
            ctx_future = scan_pool.submit(_build_context)

            injection_result = inj_future.result(timeout=120)
            context, ctx_msg = ctx_future.result(timeout=120)

        _save_agent_result(review_id, injection_result)

        if injection_result.status == "flagged":
            db.update_review_status(review_id, "flagged")
            progress("injection_scan", "INJECTION DETECTED — review halted")
            return {
                "review_id": review_id,
                "status": "flagged",
                "pr": _pr_summary(pr),
                "injection_scan": _result_to_dict(injection_result),
                "contextual_review": None,
                "unbiased_review": None,
                "synthesis": None,
            }

        progress("injection_scan", "Clear — no injection detected")

        if ctx_msg:
            progress("context", ctx_msg)
        elif context:
            progress("context", f"Using cached context from {local_repo_id}")
        else:
            progress("context", "No local clone registered — running without repo context")

        # Load agent config (custom review guidelines, focus areas, etc.)
        agent_config = db.get_agent_config(local_repo_id) or db.get_agent_config(repo_id) or {}
        if agent_config:
            progress("config", f"Loaded custom agent config for {local_repo_id}")

        # Resolve models per agent type
        models = _resolve_models(review_mode, agent_config)
        if review_mode != "standard":
            progress("config", f"Review mode: {review_mode} → models: {models}")

        # ── Step 3: Parallel Reviews ─────────────────────────────
        progress("review", "Running contextual + unbiased reviews in parallel...")

        def run_contextual():
            return self.contextual_agent.run(
                pr=pr, context=context or {}, repo_id=local_repo_id,
                agent_config=agent_config,
                model=models.get("contextual_review", ""),
            )

        def run_unbiased():
            return self.unbiased_agent.run(
                pr=pr, agent_config=agent_config,
                model=models.get("unbiased_review", ""),
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            ctx_future = pool.submit(run_contextual)
            unb_future = pool.submit(run_unbiased)

            # Timeout recovery: if one agent fails, continue with the other
            try:
                contextual_result = ctx_future.result(timeout=180)
            except Exception as e:
                logger.error("Contextual agent failed: %s", e, exc_info=True)
                contextual_result = AgentResult(
                    agent_type="contextual_review",
                    status="error",
                    summary=f"Agent timed out or failed: {e}",
                )

            try:
                unbiased_result = unb_future.result(timeout=180)
            except Exception as e:
                logger.error("Unbiased agent failed: %s", e, exc_info=True)
                unbiased_result = AgentResult(
                    agent_type="unbiased_review",
                    status="error",
                    summary=f"Agent timed out or failed: {e}",
                )

        # Save both results
        for result in (contextual_result, unbiased_result):
            _save_agent_result(review_id, result)

        progress("review",
                 f"Contextual: {contextual_result.verdict} ({contextual_result.confidence:.0%}) | "
                 f"Unbiased: {unbiased_result.verdict} ({unbiased_result.confidence:.0%})")

        # ── Step 4: Synthesis ────────────────────────────────────
        progress("synthesis", "Synthesizing final verdict...")
        synthesis_result = self.synthesis_agent.run(
            contextual_result=contextual_result,
            unbiased_result=unbiased_result,
            pr=pr,
            model=models.get("synthesis", ""),
        )

        _save_agent_result(review_id, synthesis_result)

        db.update_review_status(review_id, "complete")

        # Create an action ticket (for potential one-time use)
        ticket_id = db.create_ticket(
            review_id=review_id,
            repo_id=repo_id,
            action_type=_verdict_to_action(synthesis_result.verdict),
            pr_number=pr_number,
            payload={
                "comment": synthesis_result.details.get("suggested_review_comment", ""),
                "verdict": synthesis_result.verdict,
            },
            diff_hash=diff_hash(pr.diff),
        )

        progress("complete",
                 f"Review complete: {synthesis_result.verdict} "
                 f"({synthesis_result.confidence:.0%} confidence)")

        return {
            "review_id": review_id,
            "ticket_id": ticket_id,
            "status": "complete",
            "review_mode": review_mode,
            "pr": _pr_summary(pr),
            "injection_scan": _result_to_dict(injection_result),
            "contextual_review": _result_to_dict(contextual_result),
            "unbiased_review": _result_to_dict(unbiased_result),
            "synthesis": _result_to_dict(synthesis_result),
        }


def _save_agent_result(review_id: str, result: AgentResult) -> str:
    """Save an agent result with usage metadata."""
    return db.save_review_result(
        review_id=review_id,
        agent_type=result.agent_type,
        status=result.status,
        verdict=result.verdict,
        summary=result.summary,
        details=result.details,
        confidence=result.confidence,
        execution_time_ms=result.execution_time_ms,
        prompt_sent=result.prompt_sent,
        model_used=result.model_used,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        tokens_estimated=result.tokens_estimated,
    )


def _pr_summary(pr: PRInfo) -> dict:
    return {
        "number": pr.number,
        "title": pr.title,
        "author": pr.author,
        "url": pr.url,
        "files_changed": len(pr.files),
        "additions": pr.total_additions,
        "deletions": pr.total_deletions,
        "labels": pr.labels,
        "linked_issues": [i.number for i in pr.linked_issues],
    }


def _result_to_dict(result: AgentResult) -> dict:
    return {
        "agent_type": result.agent_type,
        "status": result.status,
        "verdict": result.verdict,
        "summary": result.summary,
        "details": result.details,
        "confidence": result.confidence,
        "execution_time_ms": result.execution_time_ms,
        "flags": result.flags,
        "model_used": result.model_used,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "tokens_estimated": result.tokens_estimated,
    }


def _verdict_to_action(verdict: str) -> str:
    mapping = {
        "approve": "APPROVE",
        "request_changes": "REQUEST_CHANGES",
        "comment": "COMMENT",
    }
    return mapping.get(verdict, "COMMENT")
