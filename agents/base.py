"""Base agent interface and shared Claude calling helper.

Supports two backends:
  1. Claude CLI (`claude` command) — uses your Pro subscription, no API key needed.
  2. Anthropic API — if ANTHROPIC_API_KEY is set in .env.

The CLI backend is the default. Set CLAUDE_BACKEND=api in .env to use the API instead.
"""

import json
import random
import subprocess
import time
import logging
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.config import Config

logger = logging.getLogger(__name__)


# ── Call result ─────────────────────────────────────────────────

@dataclass
class CallResult:
    """Result of a single Claude call, including usage metadata."""
    text: str
    model: str = ""
    backend: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    tokens_estimated: bool = False
    elapsed_ms: int = 0


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English/code."""
    return max(1, len(text) // 4)


# ── Backend: Claude CLI ──────────────────────────────────────────

def _find_claude_cli() -> str | None:
    return shutil.which("claude")


def _call_claude_cli(system: str, user_prompt: str,
                     model: str = "", max_tokens: int = 4096) -> CallResult:
    """Call Claude via the `claude` CLI (uses Pro subscription)."""
    cli = _find_claude_cli()
    if not cli:
        raise RuntimeError(
            "claude CLI not found on PATH. Install Claude Code or set "
            "CLAUDE_BACKEND=api with ANTHROPIC_API_KEY in .env"
        )

    # Build the combined prompt with system context
    full_prompt = f"{system}\n\n---\n\n{user_prompt}"

    # Build a clean env: unset CLAUDECODE to allow running from within
    # a Cowork/Claude Code session (otherwise it blocks nested sessions).
    import os
    clean_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    clean_env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"

    cmd = [cli, "-p", full_prompt, "--output-format", "text",
           "--max-turns", "1", "--tools", ""]
    if model:
        cmd.extend(["--model", model])

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
        env=clean_env,
    )

    if result.returncode != 0:
        stderr = result.stderr.strip()[:500]
        logger.error("Claude CLI failed (rc=%d): %s", result.returncode, stderr)
        raise RuntimeError(f"Claude CLI error: {stderr}")

    text = result.stdout.strip()
    logger.info("Claude CLI call returned %d chars", len(text))
    return CallResult(
        text=text,
        model=model or Config.CLAUDE_MODEL,
        backend="cli",
        input_tokens=_estimate_tokens(full_prompt),
        output_tokens=_estimate_tokens(text),
        tokens_estimated=True,
    )


# ── Backend: Anthropic API ───────────────────────────────────────

_api_client = None


def _call_claude_api(system: str, user_prompt: str, model: str | None = None,
                     max_tokens: int = 4096) -> CallResult:
    """Call Claude via the Anthropic API (requires ANTHROPIC_API_KEY)."""
    global _api_client
    if _api_client is None:
        import anthropic
        _api_client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)

    used_model = model or Config.CLAUDE_MODEL
    response = _api_client.messages.create(
        model=used_model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = response.content[0].text if response.content else ""
    in_tok = response.usage.input_tokens
    out_tok = response.usage.output_tokens
    logger.info("API call (%d tokens in, %d tokens out)", in_tok, out_tok)
    return CallResult(
        text=text,
        model=used_model,
        backend="api",
        input_tokens=in_tok,
        output_tokens=out_tok,
        tokens_estimated=False,
    )


# ── Retry helper ─────────────────────────────────────────────────

# Exceptions worth retrying — transient failures, not logic errors
_CLI_RETRYABLE = (RuntimeError, subprocess.TimeoutExpired, OSError)

def _is_api_retryable(exc: Exception) -> bool:
    """Check if an Anthropic API exception is retryable."""
    cls_name = type(exc).__name__
    return cls_name in ("RateLimitError", "APIConnectionError",
                        "APITimeoutError", "InternalServerError",
                        "OverloadedError")


def _retry(fn, *, max_attempts: int = 3, backoff_base: float = 2.0):
    """Call fn() with exponential backoff + jitter on retryable errors."""
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as e:
            retryable = isinstance(e, _CLI_RETRYABLE) or _is_api_retryable(e)
            if not retryable or attempt == max_attempts:
                raise
            last_exc = e
            delay = backoff_base ** (attempt - 1) + random.uniform(0, 1)
            logger.warning("Claude call attempt %d/%d failed (%s), retrying in %.1fs",
                           attempt, max_attempts, e, delay)
            time.sleep(delay)
    raise last_exc  # unreachable, but satisfies type checker


# ── Unified interface ────────────────────────────────────────────

def call_claude(system: str, user_prompt: str, model: str | None = None,
                max_tokens: int = 4096,
                review_id: str = "", agent_type: str = "") -> CallResult:
    """Send a single-turn message to Claude. Auto-selects backend. Retries on transient failures.

    Returns CallResult with text + usage metadata.
    Optional review_id/agent_type are stored in the claude_usage table for tracking.
    """
    t0 = time.monotonic()
    backend = Config.CLAUDE_BACKEND

    if backend == "api" and Config.ANTHROPIC_API_KEY:
        cr = _retry(lambda: _call_claude_api(system, user_prompt, model, max_tokens))
    elif backend == "api" and not Config.ANTHROPIC_API_KEY:
        logger.warning("CLAUDE_BACKEND=api but no ANTHROPIC_API_KEY — falling back to CLI")
        cr = _retry(lambda: _call_claude_cli(system, user_prompt, model or "", max_tokens))
    else:
        cr = _retry(lambda: _call_claude_cli(system, user_prompt, model or "", max_tokens))

    cr.elapsed_ms = int((time.monotonic() - t0) * 1000)
    logger.info("Claude call (%s backend, model=%s) took %dms, ~%d+%d tokens",
                cr.backend, cr.model, cr.elapsed_ms, cr.input_tokens, cr.output_tokens)

    # Persist usage to DB (best-effort, never fail the call)
    try:
        from app import database as _db
        _db.save_usage(
            review_id=review_id or None,
            agent_type=agent_type or None,
            model=cr.model,
            backend=cr.backend,
            input_tokens=cr.input_tokens,
            output_tokens=cr.output_tokens,
            tokens_estimated=cr.tokens_estimated,
            elapsed_ms=cr.elapsed_ms,
        )
    except Exception as e:
        logger.debug("Failed to save usage (non-fatal): %s", e)

    return cr


def parse_json_response(text: str) -> dict:
    """Best-effort extraction of JSON from Claude's response."""
    text = text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find { ... } block
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    logger.warning("Could not parse JSON from response, returning raw text")
    return {"raw_response": text}


@dataclass
class AgentResult:
    agent_type: str
    status: str = "success"  # success, error, flagged
    verdict: str = ""  # approve, request_changes, comment
    summary: str = ""
    details: dict = field(default_factory=dict)
    confidence: float = 0.0
    execution_time_ms: int = 0
    flags: list[str] = field(default_factory=list)
    prompt_sent: str = ""  # system + user prompt for debugging
    model_used: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    tokens_estimated: bool = False


class BaseAgent(ABC):
    """Abstract base for all review pipeline agents."""

    agent_type: str = "base"

    @abstractmethod
    def run(self, **kwargs) -> AgentResult:
        ...

    def _timed_run(self, fn, **kwargs) -> AgentResult:
        t0 = time.monotonic()
        try:
            result = fn(**kwargs)
            result.execution_time_ms = int((time.monotonic() - t0) * 1000)
            return result
        except Exception as e:
            logger.exception("Agent %s failed", self.agent_type)
            return AgentResult(
                agent_type=self.agent_type,
                status="error",
                summary=f"Agent error: {e}",
                execution_time_ms=int((time.monotonic() - t0) * 1000),
            )
