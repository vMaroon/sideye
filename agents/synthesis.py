"""Synthesis Agent — reconciles dual review verdicts.

Takes the contextual (full-context) and unbiased (code-only) review results
and produces a final, unified verdict that accounts for both perspectives.
Now also receives the PR itself for grounding (description, diff, linked issues).
"""

import json
import logging

from agents.base import BaseAgent, AgentResult, call_claude, parse_json_response
from github.models import PRInfo

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are the final reviewer in a PR review pipeline. You have received analysis
from two internal passes — one with full repo context and one without — as well
as the actual PR (description, diff, linked issues). Your job is to reconcile
the analyses into a **single, coherent review** written in your own voice,
grounded in the real code.

Do NOT reference "agents", "passes", "contextual review", or "unbiased review"
in any user-facing text. The reviewer should read your output as one reviewer's
considered opinion. Internal attribution stays in the `_internal` field only.

Your job:
1. Write a **PR Brief** — a concise explanation of what this PR does, for a
   reviewer who hasn't read the code yet. Include alignment: does this PR match
   known project direction (linked issues, roadmap, repo conventions)? Flag
   misalignment or scope creep clearly.
2. Produce a single **verdict** with confidence.
3. List **key findings** ordered by severity. Each finding must have a reason
   that grounds it in repo standards, conventions, or general quality. No
   attribution to which pass found it.
4. Write a ready-to-post GitHub review comment in first person as the reviewer.

CRITICAL: Your output must read as ONE person's review. Never say "both analyses",
"both passes", "both agents", "independently flagged", "agents agree", or anything
that implies multiple reviewers or multiple sources. Synthesize into your own voice.
If two analyses agree, just state the conclusion. If they disagree, pick the stronger
position and state it as your own.

Tone: direct, fair, specific. No fluff. No flattery. No "great job", "nice work",
"thanks for this PR", "solid implementation", or any other complimentary preamble.
Lead with substance. The reviewer is a peer, not a manager giving positive feedback.

The suggested_review_comment MUST be SHORT — aim for 3-8 sentences max. State the
verdict, list the important issues (if any), and stop. Do not summarize the PR back
to the author (they wrote it, they know what it does). Do not repeat findings that
are already covered by inline comments. The comment should add signal, not length.

Output strict JSON:
{
  "pr_brief": {
    "purpose": "One sentence: what this PR accomplishes",
    "scope": "What areas of the codebase it touches and why",
    "key_changes": ["3-5 bullet points of the most important changes, in plain language"],
    "alignment": "How this relates to linked issues, design docs, or project direction. Flag misalignment or scope creep. null if no context available."
  },
  "final_verdict": "approve" | "request_changes" | "comment",
  "confidence": 0.0-1.0,
  "executive_summary": "3-5 sentence review summary for the reviewer. Written as a single coherent assessment — no references to internal passes or agents.",
  "key_findings": [
    {
      "finding": "description",
      "severity": "critical|major|minor|nit",
      "file": "filename or null",
      "actionable": true/false,
      "reason": "Why this matters — grounded in repo standards, conventions, or code quality principles"
    }
  ],
  "suggested_review_comment": "SHORT ready-to-post review comment (3-8 sentences). First person. State verdict + key issues only. No summary of what the PR does. No flattery. No references to bots or automated review.",
  "_internal": {
    "agreement": true/false,
    "contextual_verdict": "...",
    "unbiased_verdict": "...",
    "reconciliation_notes": "How disagreements were resolved (internal only)"
  }
}"""


class SynthesisAgent(BaseAgent):
    agent_type = "synthesis"

    def run(self, *, contextual_result: AgentResult,
            unbiased_result: AgentResult,
            pr: PRInfo | None = None,
            pr_title: str = "", pr_url: str = "",
            model: str = "") -> AgentResult:
        return self._timed_run(
            self._synthesize,
            contextual_result=contextual_result,
            unbiased_result=unbiased_result,
            pr=pr,
            pr_title=pr_title,
            pr_url=pr_url,
            model=model,
        )

    def _synthesize(self, *, contextual_result: AgentResult,
                    unbiased_result: AgentResult,
                    pr: PRInfo | None,
                    pr_title: str, pr_url: str,
                    model: str = "") -> AgentResult:

        # Use PRInfo title if available, fall back to explicit param
        title = (pr.title if pr else None) or pr_title

        # Build the PR section — the synthesis agent now sees the actual PR
        pr_section = ""
        if pr:
            desc = pr.description[:2000] if pr.description else "(no description)"

            issues_text = ""
            if pr.linked_issues:
                issues_text = "\n".join(
                    f"- #{i.number}: {i.title} [{i.state}]"
                    for i in pr.linked_issues
                )

            diff = pr.diff
            if len(diff) > 15000:
                diff = diff[:15000] + "\n\n[... diff truncated at 15k chars ...]"

            pr_section = f"""
## The PR Itself
**PR #{pr.number}: {pr.title}**
Author: {pr.author} | Branch: {pr.head_branch} → {pr.base_branch}
Labels: {', '.join(pr.labels) if pr.labels else 'none'}

### Description
{desc}

### Linked Issues
{issues_text if issues_text else "None referenced"}

### Diff
{diff}
"""

        prompt = f"""Review PR: "{title}"
{pr_section}
Below are internal review notes from two angles. Use them as input to form YOUR
review. Do not reference these notes or their structure in your output.

## Notes: Repo-Aware Assessment
Verdict: {contextual_result.verdict}
Confidence: {contextual_result.confidence}
Summary: {contextual_result.summary}

Details:
{json.dumps(contextual_result.details, indent=2)[:6000]}

## Notes: Code-Focused Assessment
Verdict: {unbiased_result.verdict}
Confidence: {unbiased_result.confidence}
Summary: {unbiased_result.summary}

Details:
{json.dumps(unbiased_result.details, indent=2)[:6000]}

Write your review as a single coherent assessment. Everything above is raw input —
your output must stand alone as one reviewer's opinion."""

        cr = call_claude(_SYSTEM_PROMPT, prompt, model=model or None,
                         max_tokens=4096, agent_type="synthesis")
        parsed = parse_json_response(cr.text)

        verdict = parsed.get("final_verdict", "comment")
        confidence = parsed.get("confidence", 0.5)

        return AgentResult(
            agent_type=self.agent_type,
            status="success",
            verdict=verdict,
            summary=parsed.get("executive_summary", ""),
            details=parsed,
            confidence=confidence,
            prompt_sent=f"SYSTEM:\n{_SYSTEM_PROMPT}\n\nUSER:\n{prompt}",
            model_used=cr.model,
            input_tokens=cr.input_tokens,
            output_tokens=cr.output_tokens,
            tokens_estimated=cr.tokens_estimated,
        )
