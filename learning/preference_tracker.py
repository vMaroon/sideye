"""Preference tracker — captures feedback, tracks submissions, builds reviewer directives."""

import json
import logging
from app import database as db
from app.config import Config

logger = logging.getLogger(__name__)


# ── Explicit Feedback (thumbs up/down, calibration) ─────────────

def record_feedback(review_id: str, feedback: dict) -> str:
    """
    Store user feedback on a review.

    feedback dict expected shape:
    {
        "verdict_correct": true/false,
        "correct_verdict": "approve" | "request_changes" | "comment" | null,
        "severity_assessment": "too_strict" | "too_lenient" | "about_right",
        "tone_assessment": "too_harsh" | "too_soft" | "appropriate",
        "missed_issues": "freetext description of what was missed" | null,
        "false_positives": "freetext description of overblown issues" | null,
        "notes": "any additional reviewer notes" | null,
    }
    """
    review = db.get_review(review_id)
    if not review:
        raise ValueError(f"Review {review_id} not found")

    repo_id = review["repo_id"]

    enriched = {
        **feedback,
        "review_id": review_id,
        "pr_number": review["pr_number"],
        "pr_title": review["pr_title"],
    }

    pref_id = db.save_preference(
        repo_id=repo_id,
        category="review_feedback",
        feedback_data=enriched,
    )

    logger.info("Recorded feedback for review %s (pref %s)", review_id, pref_id)

    # Trigger pattern extraction every 10 feedbacks
    all_feedback = db.get_preferences(repo_id, category="review_feedback")
    if len(all_feedback) % 10 == 0 and len(all_feedback) > 0:
        logger.info("Triggering pattern extraction for %s (%d feedbacks)",
                     repo_id, len(all_feedback))
        _extract_patterns(repo_id, all_feedback)

    return pref_id


def _extract_patterns(repo_id: str, feedbacks: list[dict]) -> None:
    """Analyze accumulated feedback to extract preference patterns."""
    from agents.base import call_claude, parse_json_response

    recent = feedbacks[:20]
    feedback_summary = []
    for fb in recent:
        fd = fb.get("feedback_data", {})
        feedback_summary.append({
            "pr_title": fd.get("pr_title", "?"),
            "verdict_correct": fd.get("verdict_correct"),
            "correct_verdict": fd.get("correct_verdict"),
            "severity": fd.get("severity_assessment"),
            "tone": fd.get("tone_assessment"),
            "missed": fd.get("missed_issues"),
            "false_positives": fd.get("false_positives"),
        })

    prompt = f"""Analyze this reviewer's feedback on {len(feedback_summary)} recent PR reviews.
Extract patterns about their preferences.

Feedback history:
{json.dumps(feedback_summary, indent=2)}

Output JSON:
{{
  "strictness_trend": "strict|moderate|lenient",
  "tone_preference": "direct|balanced|gentle",
  "common_false_positives": ["types of issues that get flagged but reviewer considers fine"],
  "common_misses": ["types of issues reviewer wishes were caught"],
  "adjustments": ["specific, actionable instructions for future reviews"],
  "confidence": 0.0-1.0
}}"""

    system = "You analyze reviewer feedback patterns to improve future reviews. Be precise and actionable."
    cr = call_claude(system, prompt, max_tokens=1024,
                     agent_type="preference_extraction",
                     model=Config.CLAUDE_HAIKU)
    patterns = parse_json_response(cr.text)

    db.save_preference(
        repo_id=repo_id,
        category="learned_patterns",
        feedback_data=patterns,
    )

    logger.info("Extracted patterns for %s: %s", repo_id,
                patterns.get("adjustments", []))


# ── Implicit Feedback (submission tracking) ──────────────────────

def record_submission(review_id: str, repo_id: str, pr_number: int,
                      suggested_verdict: str, chosen_verdict: str,
                      all_suggested_comments: list[dict],
                      selected_comments: list[dict]) -> str:
    """Record what the user actually posted vs what the bot suggested.

    all_suggested_comments: every inline comment the bot generated
        [{file, line_hint, comment, severity}]
    selected_comments: user's chosen subset, possibly edited
        [{file, line_hint, comment, original_comment}]
    """
    # Match selected back to suggested by (file, line_hint)
    selected_lookup = {}
    for sc in selected_comments:
        key = (sc.get("file", ""), sc.get("line_hint", ""))
        selected_lookup[key] = sc

    comments_data = []
    edited_count = 0
    for suggested in all_suggested_comments:
        key = (suggested.get("file", ""), suggested.get("line_hint", ""))
        sel = selected_lookup.get(key)
        was_selected = sel is not None
        was_edited = False
        posted_text = None

        if was_selected:
            posted_text = sel.get("comment", "")
            original_text = sel.get("original_comment", suggested.get("comment", ""))
            was_edited = posted_text.strip() != original_text.strip()
            if was_edited:
                edited_count += 1

        comments_data.append({
            "file": suggested.get("file", ""),
            "line_hint": suggested.get("line_hint", ""),
            "severity": suggested.get("severity", "minor"),
            "original": suggested.get("comment", ""),
            "posted": posted_text,
            "was_edited": was_edited,
            "was_selected": was_selected,
        })

    sid = db.save_submission(
        review_id=review_id,
        repo_id=repo_id,
        pr_number=pr_number,
        suggested_verdict=suggested_verdict,
        chosen_verdict=chosen_verdict,
        total_suggested=len(all_suggested_comments),
        total_selected=len(selected_comments),
        total_edited=edited_count,
        comments_data=comments_data,
    )

    logger.info("Recorded submission %s: %d/%d selected, %d edited, verdict %s→%s",
                sid, len(selected_comments), len(all_suggested_comments),
                edited_count, suggested_verdict, chosen_verdict)

    # Refresh directive every 5 submissions
    submission_count = db.count_submissions(repo_id)
    if submission_count > 0 and submission_count % 5 == 0:
        logger.info("Triggering directive refresh for %s (%d submissions)",
                     repo_id, submission_count)
        try:
            _refresh_directive(repo_id)
        except Exception as e:
            logger.error("Directive refresh failed: %s", e, exc_info=True)

    return sid


# ── Submission Stats ─────────────────────────────────────────────

def _compute_submission_stats(submissions: list[dict]) -> dict:
    """Compute aggregate stats from submission records."""
    total_suggested = 0
    total_selected = 0
    total_edited = 0
    verdict_overrides = 0
    verdict_counts = {}
    severity_suggested = {}
    severity_accepted = {}

    for s in submissions:
        total_suggested += s.get("total_suggested", 0)
        total_selected += s.get("total_selected", 0)
        total_edited += s.get("total_edited", 0)

        sv = (s.get("suggested_verdict") or "").lower()
        cv = (s.get("chosen_verdict") or "").lower()
        if sv and cv and sv != cv:
            verdict_overrides += 1
        if cv:
            verdict_counts[cv] = verdict_counts.get(cv, 0) + 1

        for c in (s.get("comments_data") or []):
            sev = c.get("severity", "minor")
            severity_suggested[sev] = severity_suggested.get(sev, 0) + 1
            if c.get("was_selected"):
                severity_accepted[sev] = severity_accepted.get(sev, 0) + 1

    sev_rates = {}
    for sev in severity_suggested:
        sev_rates[sev] = severity_accepted.get(sev, 0) / severity_suggested[sev]

    return {
        "acceptance_rate": total_selected / total_suggested if total_suggested > 0 else None,
        "edit_rate": total_edited / total_selected if total_selected > 0 else None,
        "verdict_override_rate": verdict_overrides / len(submissions) if submissions else None,
        "preferred_verdict": max(verdict_counts, key=verdict_counts.get) if verdict_counts else None,
        "severity_acceptance": sev_rates,
        "total_submissions": len(submissions),
    }


# ── Reviewer Directive ───────────────────────────────────────────

def build_reviewer_directive(repo_id: str) -> str:
    """Return a prompt-ready preference string for the review agents.

    Checks for a cached Claude-synthesized directive first, then falls back
    to raw assembly from mined profile + learned patterns + submission stats.
    """
    cached = db.get_reviewer_directive(repo_id)
    if cached and cached.get("directive_text"):
        return cached["directive_text"]

    # Fall back to global directive
    global_cached = db.get_reviewer_directive("__global__")
    if global_cached and global_cached.get("directive_text"):
        return global_cached["directive_text"]

    # No cached directive — assemble from raw sources
    return _assemble_directive_from_sources(repo_id)


def _assemble_directive_from_sources(repo_id: str) -> str:
    """Build a basic directive from raw preference data (no Claude call)."""
    sections = []

    # 1. Mined profile (global)
    global_profile = db.get_preferences("__global__", category="mined_profile")
    if global_profile:
        p = global_profile[0].get("feedback_data", {})
        summary = p.get("summary", "")
        if summary:
            sections.append(f"## Reviewer Background\n{summary}")

        style = p.get("review_style", {})
        parts = []
        if style.get("strictness"):
            parts.append(f"Strictness: {style['strictness']}")
        if style.get("tone"):
            parts.append(f"Tone: {style['tone']}")
        if style.get("detail_level"):
            parts.append(f"Detail: {style['detail_level']}")
        focus = style.get("focus_areas", [])
        if focus:
            parts.append(f"Focus: {', '.join(focus)}")
        lets_slide = style.get("lets_slide", [])
        if lets_slide:
            parts.append(f"Lenient on: {', '.join(lets_slide)}")
        if parts:
            sections.append("## Review Style\n" + ". ".join(parts) + ".")

        approval = p.get("approval_criteria", {})
        blocking = approval.get("blocking_issues", [])
        if blocking:
            sections.append(
                "## Approval Criteria\nBlocking issues: " + ", ".join(blocking)
            )

    # 2. Learned patterns (repo-specific, fall back to global)
    patterns = db.get_preferences(repo_id, category="learned_patterns")
    if not patterns:
        patterns = db.get_preferences("__global__", category="learned_patterns")
    if patterns:
        lp = patterns[0].get("feedback_data", {})
        adjustments = lp.get("adjustments", [])
        false_pos = lp.get("common_false_positives", [])
        misses = lp.get("common_misses", [])

        if adjustments:
            sections.append(
                "## Learned Adjustments\n" + "\n".join(f"- {a}" for a in adjustments)
            )
        if false_pos:
            sections.append(
                "## Skip These (reviewer considers fine)\n"
                + "\n".join(f"- {fp}" for fp in false_pos)
            )
        if misses:
            sections.append(
                "## Catch These (reviewer wants flagged)\n"
                + "\n".join(f"- {m}" for m in misses)
            )

    # 3. Submission stats
    submissions = db.get_submissions(repo_id, limit=50)
    if submissions:
        stats = _compute_submission_stats(submissions)
        lines = []
        if stats["acceptance_rate"] is not None:
            lines.append(f"- Comment acceptance rate: {stats['acceptance_rate']:.0%}")
        if stats["edit_rate"] is not None:
            lines.append(f"- Edit rate (of accepted): {stats['edit_rate']:.0%}")
        if stats["verdict_override_rate"] is not None:
            lines.append(f"- Verdict override rate: {stats['verdict_override_rate']:.0%}")
        if stats["preferred_verdict"]:
            lines.append(f"- Preferred verdict: {stats['preferred_verdict']}")
        sev = stats.get("severity_acceptance", {})
        for s in ("critical", "major", "minor", "nit"):
            if s in sev:
                lines.append(f"- {s} acceptance: {sev[s]:.0%}")
        if lines:
            sections.append("## Submission Behavior\n" + "\n".join(lines))

    if not sections:
        return ""
    return "\n\n".join(sections)


def _refresh_directive(repo_id: str) -> None:
    """Re-synthesize the reviewer directive from all data sources via Claude."""
    from agents.base import call_claude

    raw_directive = _assemble_directive_from_sources(repo_id)

    submissions = db.get_submissions(repo_id, limit=50)
    stats = _compute_submission_stats(submissions) if submissions else {}

    # Recent explicit feedback
    feedback = db.get_preferences(repo_id, category="review_feedback")
    if not feedback:
        feedback = db.get_preferences("__global__", category="review_feedback")
    feedback_summary = []
    for fb in (feedback or [])[:10]:
        fd = fb.get("feedback_data", {})
        feedback_summary.append({
            "verdict_correct": fd.get("verdict_correct"),
            "severity": fd.get("severity_assessment"),
            "missed": fd.get("missed_issues"),
            "false_positives": fd.get("false_positives"),
        })

    sev_json = json.dumps(stats.get("severity_acceptance", {}))
    fb_json = json.dumps(feedback_summary, indent=2) if feedback_summary else "(none)"

    prompt = f"""Synthesize a reviewer preference directive from these data sources.
The directive will be injected into a code review agent's prompt to calibrate its behavior.

## Current Profile
{raw_directive if raw_directive else "(no prior profile)"}

## Submission Stats ({stats.get('total_submissions', 0)} reviews posted)
- Acceptance rate: {stats.get('acceptance_rate', 'N/A')}
- Edit rate: {stats.get('edit_rate', 'N/A')}
- Verdict override rate: {stats.get('verdict_override_rate', 'N/A')}
- Preferred verdict: {stats.get('preferred_verdict', 'N/A')}
- Severity acceptance: {sev_json}

## Recent Explicit Feedback ({len(feedback_summary)} entries)
{fb_json}

Write a concise directive (max 400 words) with these sections:
## Reviewer Preferences
- Strict on: [areas where thorough checking is wanted]
- Lenient on: [areas considered unimportant]
- Severity calibration: [which severity levels get accepted/rejected, with rates]
- Verdict pattern: [how reviewer chooses verdicts vs bot suggestions]
- Communication: [preferred tone and comment style]
- Wants caught: [issues reviewer wishes were flagged more]

Be specific and data-driven. Use actual percentages. If data is insufficient for a field, say so."""

    system = (
        "You synthesize reviewer preference data into concise, actionable directives "
        "for a code review AI. Be precise. Use data, not speculation."
    )

    cr = call_claude(system, prompt, max_tokens=1024,
                     agent_type="directive_refresh",
                     model=Config.CLAUDE_HAIKU)
    directive_text = cr.text.strip()

    db.save_reviewer_directive(
        repo_id=repo_id,
        directive_data={
            "directive_text": directive_text,
            "stats": stats,
            "submission_count": stats.get("total_submissions", 0),
        },
    )

    logger.info("Refreshed reviewer directive for %s", repo_id)
