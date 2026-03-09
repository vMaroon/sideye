"""Unbiased Review Agent — minimal context, code-focused.

Reviews a PR with NO repo context. Focuses purely on code correctness,
readability, and obvious issues. This provides an independent second
opinion that isn't biased by familiarity with the codebase.
"""

import logging

from agents.base import BaseAgent, AgentResult, call_claude, parse_json_response
from github.models import PRInfo

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a code reviewer with NO context about the repository. You have ONLY the PR
title, its description, and its diff. You cannot and should not make assumptions
about the repo's specific conventions or history.

Focus exclusively on:
1. Code correctness — logic errors, edge cases, off-by-one, null handling, race conditions
2. Readability — naming, structure, comments where needed, dead code
3. Obvious test gaps — is the change tested? Are obvious cases missing?
4. Security concerns — injection, auth bypass, hardcoded secrets, unsafe operations
5. Error handling — are errors handled appropriately or silently swallowed?
6. Intent match — does the code change match what the PR description claims?

Do NOT comment on:
- Repo-specific style conventions (you don't know them)
- Whether the change aligns with project goals (you don't know them)
- Test framework choices or coverage percentages

Your tone: direct, constructive, focused on the code itself.

Output strict JSON:
{
  "verdict": "approve" | "request_changes" | "comment",
  "confidence": 0.0-1.0,
  "summary": "2-3 sentence assessment focusing on code quality",
  "bugs": [
    {
      "file": "filename",
      "line_hint": "surrounding context or line range",
      "description": "what's wrong",
      "severity": "critical|major|minor",
      "suggestion": "how to fix (if obvious)"
    }
  ],
  "readability_issues": [
    {
      "file": "filename",
      "description": "what could be clearer",
      "severity": "minor|nit"
    }
  ],
  "security_concerns": [
    {
      "file": "filename",
      "description": "the concern",
      "severity": "critical|major|minor"
    }
  ],
  "test_assessment": {
    "has_tests": true/false,
    "gaps": ["list of untested scenarios"] or [],
    "notes": "..." or null
  },
  "detailed_comments": [
    {
      "file": "filename",
      "line_hint": "context",
      "comment": "specific feedback",
      "severity": "critical|major|minor|nit",
      "type": "review_comment|note"
    }
  ]
}

IMPORTANT: Each comment must have a "type" field:
- "review_comment" — actionable feedback suitable for posting on the PR (bugs, suggestions, questions about the code, things that need fixing or clarification)
- "note" — internal observation for the reviewer only (positive feedback, impressions, things that are fine as-is)

Most comments pointing out issues or suggesting changes should be "review_comment".
Observations like "good error handling here" should be "note"."""


def _apply_unbiased_config(system: str, config: dict) -> str:
    """Append relevant agent configuration to the unbiased agent's system prompt.

    The unbiased agent intentionally gets LESS config than the contextual agent —
    no repo-specific standards or focus areas. But it does respect: guidelines,
    ignore patterns, tone, and severity threshold.
    """
    additions = []

    guidelines = config.get("review_guidelines", "")
    if guidelines:
        additions.append(f"## Custom Review Guidelines (set by the reviewer)\n{guidelines}")

    focus_areas = config.get("unbiased_focus", [])
    if focus_areas:
        additions.append(
            f"## Priority Focus Areas\nPay special attention to: {', '.join(focus_areas)}"
        )

    ignore = config.get("ignore_patterns", [])
    if ignore:
        additions.append(
            f"## Ignore Patterns\nDo not comment on files matching: {', '.join(ignore)}"
        )

    tone = config.get("tone", "")
    if tone and tone != "direct":
        tone_map = {
            "gentle": "Be gentle and supportive. Frame issues as suggestions rather than demands.",
            "strict": "Be strict and thorough. Flag all issues including minor ones.",
        }
        if tone in tone_map:
            additions.append(f"## Tone Override\n{tone_map[tone]}")

    threshold = config.get("severity_threshold", "")
    if threshold and threshold != "nit":
        additions.append(
            f"## Severity Threshold\nOnly report issues at '{threshold}' severity or above."
        )

    if additions:
        return system + "\n\n" + "\n\n".join(additions)
    return system


class UnbiasedReviewAgent(BaseAgent):
    agent_type = "unbiased_review"

    def run(self, *, pr: PRInfo, agent_config: dict | None = None,
            model: str = "") -> AgentResult:
        return self._timed_run(self._review, pr=pr, agent_config=agent_config or {},
                               model=model)

    def _review(self, *, pr: PRInfo, agent_config: dict,
                model: str = "") -> AgentResult:
        diff = pr.diff
        if len(diff) > 40000:
            diff = diff[:40000] + "\n\n[... diff truncated at 40k chars ...]"

        # File summary for orientation
        files_summary = ""
        if pr.files:
            files_summary = "\n".join(
                f"- {f.filename} ({f.status}: +{f.additions}/-{f.deletions})"
                for f in pr.files[:50]
            )

        # Include PR description so the agent can check intent match
        desc = pr.description[:3000] if pr.description else "(no description)"

        prompt = f"""Review this code change. You have NO context about the repository.

## PR Title
{pr.title}

## PR Description (author's stated intent)
{desc}

## Files Changed
{files_summary if files_summary else "(file list not available)"}

## Diff
{diff}"""

        # Apply custom config to system prompt if provided
        system = _SYSTEM_PROMPT
        if agent_config:
            system = _apply_unbiased_config(system, agent_config)

        cr = call_claude(system, prompt, model=model or None,
                         max_tokens=4096, agent_type="unbiased_review")
        parsed = parse_json_response(cr.text)

        verdict = parsed.get("verdict", "comment")
        confidence = parsed.get("confidence", 0.5)

        return AgentResult(
            agent_type=self.agent_type,
            status="success",
            verdict=verdict,
            summary=parsed.get("summary", ""),
            details=parsed,
            confidence=confidence,
            prompt_sent=f"SYSTEM:\n{system}\n\nUSER:\n{prompt}",
            model_used=cr.model,
            input_tokens=cr.input_tokens,
            output_tokens=cr.output_tokens,
            tokens_estimated=cr.tokens_estimated,
        )
