"""Contextual Review Agent — full repo context aware.

Reviews a PR with deep awareness of the repository's coding standards,
design docs, recent PR history, and learned user preferences.
"""

import json
import logging

from agents.base import BaseAgent, AgentResult, call_claude, parse_json_response
from github.models import PRInfo

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a senior code reviewer with deep context about the repository you're reviewing.
You review PRs with awareness of the codebase's standards, conventions, design direction,
and recent activity.

Your review philosophy:
- Content should be clear, bug-free, and tested
- If performance claims are made without benchmarks, flag them
- PRs should address an open issue, design doc intention, or be a reasonable self-discovered fix
- Major changes without prior community discussion should be flagged
- Code should maintain codebase-level coherence — don't let one file break the style of the whole repo
- Direction and scope should be inferrable from the PR without reading every line
- No hard opinions on things that can go by — focus on what matters
- Tone: encouraging but not cheesy or pointless. Be direct and helpful.

Output strict JSON:
{
  "verdict": "approve" | "request_changes" | "comment",
  "confidence": 0.0-1.0,
  "summary": "2-3 sentence high-level assessment",
  "scope_alignment": {
    "has_linked_issue": true/false,
    "scope_clear": true/false,
    "concerns": "..." or null
  },
  "coherence": {
    "follows_standards": true/false,
    "style_issues": ["list of specific style deviations"] or [],
    "notes": "..." or null
  },
  "code_quality": {
    "bugs_found": [{"file": "...", "line_hint": "...", "description": "...", "severity": "critical|major|minor"}],
    "test_coverage": "adequate|insufficient|missing|not_applicable",
    "readability": "good|acceptable|poor",
    "notes": "..." or null
  },
  "performance": {
    "claims_made": true/false,
    "benchmarks_provided": true/false,
    "concerns": "..." or null
  },
  "detailed_comments": [
    {
      "file": "filename",
      "line_hint": "context or line range",
      "comment": "specific feedback",
      "severity": "critical|major|minor|nit",
      "type": "review_comment|note"
    }
  ]
}

IMPORTANT: Each comment must have a "type" field:
- "review_comment" — actionable feedback the user could post on the PR as a review comment (bugs, suggestions, questions about the code, things that need fixing or clarification)
- "note" — internal observation for the reviewer only, NOT suitable for posting on the PR (positive feedback, context about why code is structured a way, architectural observations, general impressions, things that are fine as-is)

Most comments pointing out issues, asking questions, or suggesting changes should be "review_comment".
Observations like "this follows the repo pattern well" or "good test coverage here" should be "note"."""


def _apply_config_to_system(system: str, config: dict) -> str:
    """Append custom agent configuration to the system prompt."""
    additions = []

    guidelines = config.get("review_guidelines", "")
    if guidelines:
        additions.append(f"## Custom Review Guidelines (set by the reviewer)\n{guidelines}")

    custom_standards = config.get("custom_standards", "")
    if custom_standards:
        additions.append(f"## Custom Coding Standards\n{custom_standards}")

    focus_areas = config.get("contextual_focus", [])
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
            f"## Severity Threshold\nOnly report issues at '{threshold}' severity or above. "
            f"Skip anything less severe."
        )

    if additions:
        return system + "\n\n" + "\n\n".join(additions)
    return system


def _build_prompt(pr: PRInfo, context: dict, repo_id: str = "",
                  agent_config: dict | None = None) -> str:
    """Assemble the full prompt with PR data + repo context + reviewer directive."""

    # Repo context (trimmed: no file tree stats, max 5 recent commits)
    ctx_parts = []
    if context:
        cs = context.get("coding_standards", {})
        dd = context.get("design_docs", [])
        rp = context.get("recent_prs", [])
        readme = context.get("readme_excerpt", "")

        if readme:
            ctx_parts.append(f"## Repository Overview\n{readme[:400]}")

        if cs:
            ctx_parts.append(
                f"## Coding Standards\n"
                f"Language: {cs.get('language', '?')}\n"
                f"Linters: {', '.join(cs.get('linters', []))}\n"
                f"Formatters: {', '.join(cs.get('formatters', []))}\n"
                f"Test framework: {cs.get('test_framework', '?')}\n"
                f"Naming: {json.dumps(cs.get('naming_conventions', {}))}\n"
                f"Notes: {'; '.join(cs.get('style_notes', []))}"
            )

        if dd:
            docs_text = "\n".join(
                f"- {d['path']}: {d['title']}" for d in dd[:10]
            )
            ctx_parts.append(f"## Design Documents\n{docs_text}")

        if rp:
            prs_text = "\n".join(
                f"- [{r['date']}] {r['subject']} ({r['author']})" for r in rp[:5]
            )
            ctx_parts.append(f"## Recent Commits\n{prs_text}")

    # Linked issues
    issues_text = ""
    if pr.linked_issues:
        issues_text = "\n".join(
            f"- #{i.number}: {i.title} [{i.state}]\n  {i.body[:200]}"
            for i in pr.linked_issues
        )

    # Reviewer directive — pre-formatted English from preference tracker
    pref_text = ""
    if repo_id:
        from learning.preference_tracker import build_reviewer_directive
        directive = build_reviewer_directive(repo_id)
        if directive:
            pref_text = f"\n## Reviewer Preferences\n{directive}"

    # Truncate diff if very large
    diff = pr.diff
    if len(diff) > 30000:
        diff = diff[:30000] + "\n\n[... diff truncated at 30k chars ...]"

    # File summary
    files_summary = ""
    if pr.files:
        files_summary = "\n".join(
            f"- {f.filename} ({f.status}: +{f.additions}/-{f.deletions})"
            for f in pr.files[:50]
        )

    return f"""Review this pull request with full repository context.

## Repository Context
{chr(10).join(ctx_parts) if ctx_parts else "No context available."}

## PR #{pr.number}: {pr.title}
Author: {pr.author}
Branch: {pr.head_branch} → {pr.base_branch}
Labels: {', '.join(pr.labels) if pr.labels else 'none'}
Commits: {pr.commits_count}

## PR Description
{pr.description[:3000] if pr.description else "(no description)"}

## Linked Issues
{issues_text if issues_text else "None referenced"}

## Files Changed ({len(pr.files)} files, +{pr.total_additions}/-{pr.total_deletions})
{files_summary}

## Diff
{diff}
{pref_text}"""


class ContextualReviewAgent(BaseAgent):
    agent_type = "contextual_review"

    def run(self, *, pr: PRInfo, context: dict,
            repo_id: str = "",
            agent_config: dict | None = None,
            model: str = "") -> AgentResult:
        return self._timed_run(
            self._review,
            pr=pr,
            context=context,
            repo_id=repo_id,
            agent_config=agent_config or {},
            model=model,
        )

    def _review(self, *, pr: PRInfo, context: dict,
                repo_id: str,
                agent_config: dict,
                model: str = "") -> AgentResult:
        prompt = _build_prompt(pr, context, repo_id, agent_config)

        # Apply custom config to system prompt if provided
        system = _SYSTEM_PROMPT
        if agent_config:
            system = _apply_config_to_system(system, agent_config)

        cr = call_claude(system, prompt, model=model or None,
                         max_tokens=4096, agent_type="contextual_review")
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
