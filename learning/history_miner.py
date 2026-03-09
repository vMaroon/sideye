"""GitHub review history miner.

Fetches your past PR reviews from GitHub and analyzes them to build
a preference profile. Runs on-demand or on schedule.
"""

import json
import logging
import time
from dataclasses import dataclass, field

from github.client import GitHubClient, RateLimitError
from app import database as db

logger = logging.getLogger(__name__)


@dataclass
class MinedReview:
    """A single review extracted from GitHub history."""
    repo: str  # owner/name
    pr_number: int
    pr_title: str
    state: str  # APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED
    review_body: str
    inline_comments: list[dict] = field(default_factory=list)
    # Each inline comment: {path, body, position}


def mine_review_history(username: str | None = None,
                        max_prs: int = 50,
                        repos: list[str] | None = None) -> list[MinedReview]:
    """
    Fetch the authenticated user's review history from GitHub.

    Args:
        username: GitHub username (auto-detected if None)
        max_prs: Max PRs to fetch reviews from
        repos: Limit to specific repos (e.g., ["llm-d/llm-d-kv-cache"])
               If None, fetches from all repos the user has reviewed in.

    Returns list of MinedReview objects.
    """
    gh = GitHubClient()

    if not username:
        user = gh.get("/user")
        username = user["login"]
        logger.info("Mining review history for %s", username)

    # Search for PRs reviewed by this user
    query = f"type:pr reviewed-by:{username}"
    if repos:
        repo_filter = " ".join(f"repo:{r}" for r in repos)
        query += f" {repo_filter}"

    search_results = gh.get("/search/issues", params={
        "q": query,
        "per_page": min(max_prs, 100),
        "sort": "updated",
        "order": "desc",
    })

    items = search_results.get("items", [])
    logger.info("Found %d PRs reviewed by %s (fetching up to %d)",
                search_results.get("total_count", 0), username, max_prs)

    mined = []
    for item in items[:max_prs]:
        parts = item["repository_url"].split("/")
        owner, repo = parts[-2], parts[-1]
        pr_num = item["number"]

        try:
            # Fetch reviews on this PR by the user
            reviews = gh.get(f"/repos/{owner}/{repo}/pulls/{pr_num}/reviews")
            my_reviews = [r for r in reviews if r["user"]["login"] == username]

            if not my_reviews:
                continue

            # Get the most meaningful review (prefer CHANGES_REQUESTED > APPROVED > COMMENTED)
            priority = {"CHANGES_REQUESTED": 0, "APPROVED": 1, "COMMENTED": 2, "DISMISSED": 3}
            my_reviews.sort(key=lambda r: priority.get(r.get("state", ""), 9))
            primary = my_reviews[0]

            # Fetch inline comments
            all_comments = gh.get(f"/repos/{owner}/{repo}/pulls/{pr_num}/comments")
            my_comments = [
                {
                    "path": c.get("path", ""),
                    "body": c.get("body", ""),
                    "position": c.get("original_position") or c.get("position"),
                }
                for c in all_comments
                if c["user"]["login"] == username
            ]

            mined.append(MinedReview(
                repo=f"{owner}/{repo}",
                pr_number=pr_num,
                pr_title=item.get("title", ""),
                state=primary.get("state", "COMMENTED"),
                review_body=(primary.get("body") or ""),
                inline_comments=my_comments,
            ))

            logger.info("  Mined %s#%d: %s (%d inline comments)",
                        f"{owner}/{repo}", pr_num, primary.get("state"),
                        len(my_comments))

        except RateLimitError as e:
            logger.warning("Rate limited — stopping mine at %d PRs. Resets at %d",
                           len(mined), e.reset_at)
            break
        except Exception as e:
            logger.warning("Failed to mine %s#%d: %s", f"{owner}/{repo}", pr_num, e)
            continue

        # Be polite to the API
        time.sleep(0.3)

    logger.info("Mined %d reviews total", len(mined))
    return mined


def extract_preferences_from_history(mined: list[MinedReview],
                                     repo_id: str | None = None) -> dict:
    """
    Analyze mined review history with Claude to extract a preference profile.

    Returns a structured preference dict that gets stored in the DB.
    """
    from agents.base import call_claude, parse_json_response

    if not mined:
        return {"error": "No reviews to analyze"}

    # Build a condensed summary of reviews for Claude
    review_summaries = []
    for m in mined[:30]:  # Cap to avoid context overflow
        entry = {
            "repo": m.repo,
            "pr_title": m.pr_title,
            "verdict": m.state,
            "review_body": m.review_body[:500] if m.review_body else "",
            "inline_comment_count": len(m.inline_comments),
            "sample_comments": [
                {"file": c["path"], "comment": c["body"][:200]}
                for c in m.inline_comments[:5]
            ],
        }
        review_summaries.append(entry)

    # Compute basic stats
    total = len(mined)
    approved = sum(1 for m in mined if m.state == "APPROVED")
    changes_req = sum(1 for m in mined if m.state == "CHANGES_REQUESTED")
    commented = sum(1 for m in mined if m.state == "COMMENTED")
    avg_inline = sum(len(m.inline_comments) for m in mined) / max(total, 1)
    has_body = sum(1 for m in mined if m.review_body.strip())

    stats = {
        "total_reviews": total,
        "approved": approved,
        "changes_requested": changes_req,
        "commented_only": commented,
        "approval_rate": f"{approved/total:.0%}" if total else "N/A",
        "avg_inline_comments": f"{avg_inline:.1f}",
        "reviews_with_body": has_body,
    }

    system = """\
You are analyzing a code reviewer's past GitHub review history to build a preference profile.
Your goal is to understand their review style, what they care about, what they let slide,
and how they communicate.

Be specific and actionable — this profile will be injected into future AI-generated reviews
to match their style. Look for patterns, not individual instances.

Output strict JSON:
{
  "review_style": {
    "strictness": "strict|moderate|lenient",
    "detail_level": "very_detailed|detailed|concise|minimal",
    "tone": "direct|balanced|encouraging|formal",
    "focus_areas": ["list of things they consistently flag"],
    "lets_slide": ["list of things they don't seem to care about"]
  },
  "technical_preferences": {
    "cares_about_architecture": true/false,
    "cares_about_naming": true/false,
    "cares_about_tests": true/false,
    "cares_about_docs": true/false,
    "cares_about_performance": true/false,
    "flags_duplication": true/false,
    "prefers_minimal_interfaces": true/false,
    "common_suggestions": ["recurring types of suggestions"]
  },
  "communication_patterns": {
    "uses_questions_vs_directives": "questions|directives|mix",
    "provides_alternatives": true/false,
    "explains_reasoning": true/false,
    "references_broader_context": true/false
  },
  "approval_criteria": {
    "approval_threshold": "description of what makes them approve",
    "changes_requested_triggers": ["what triggers a request_changes"],
    "blocking_issues": ["what they consider blocking"]
  },
  "summary": "2-3 sentence natural language description of this reviewer's style"
}"""

    prompt = f"""Analyze this reviewer's GitHub review history and extract their preference profile.

## Statistics
{json.dumps(stats, indent=2)}

## Review Samples ({len(review_summaries)} of {total} total)
{json.dumps(review_summaries, indent=2)}

Extract a detailed preference profile from these reviews."""

    raw = call_claude(system, prompt, max_tokens=2048)
    profile = parse_json_response(raw)

    # Attach stats
    profile["_stats"] = stats
    profile["_mined_count"] = total
    profile["_repos_covered"] = list(set(m.repo for m in mined))

    return profile


def run_history_mine(max_prs: int = 50,
                     repos: list[str] | None = None,
                     save: bool = True) -> dict:
    """
    Full pipeline: mine GitHub history → extract preferences → store in DB.

    Builds a single global reviewer profile from all reviews across all repos.
    Your review style is yours — not per-repo.

    Returns the preference profile.
    """
    logger.info("Starting review history mine (max_prs=%d)", max_prs)

    # Mine across all repos
    mined = mine_review_history(max_prs=max_prs, repos=repos)
    if not mined:
        logger.warning("No reviews found to mine")
        return {"error": "No reviews found"}

    # Extract a single profile from everything
    logger.info("Extracting preferences from %d mined reviews...", len(mined))
    profile = extract_preferences_from_history(mined)

    if save:
        db.save_preference(
            repo_id="__global__",
            category="mined_profile",
            feedback_data=profile,
        )
        logger.info("Saved global mined profile (%d reviews across %d repos)",
                     len(mined), len(set(m.repo for m in mined)))

    return profile
