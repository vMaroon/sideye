"""One-time action tickets — burn-after-use mechanism for GitHub actions."""

import logging

from github.client import GitHubClient
from github.pr_fetcher import PRFetcher, diff_hash
from app import database as db

logger = logging.getLogger(__name__)


class TicketError(Exception):
    pass


class TicketAlreadyUsedError(TicketError):
    pass


class TicketDiffChangedError(TicketError):
    pass


def validate_and_use_ticket(ticket_id: str) -> dict:
    """
    Validate a ticket, check the PR hasn't changed significantly,
    execute the action, and burn the ticket.

    Uses claim → execute → burn pattern: the ticket is only permanently
    consumed after the GitHub action succeeds. On failure the ticket is
    released back so it can be retried.

    Returns the action result.
    Raises TicketError on any failure.
    """
    # Claim the ticket (sets claimed_at but NOT used_at)
    ticket = db.claim_ticket(ticket_id)
    if not ticket:
        existing = db.get_ticket(ticket_id)
        if existing and existing.get("used_at"):
            raise TicketAlreadyUsedError(
                f"Ticket {ticket_id} was already used at {existing['used_at']}"
            )
        if existing and existing.get("claimed_at"):
            raise TicketError(
                f"Ticket {ticket_id} is currently being processed"
            )
        raise TicketError(f"Ticket {ticket_id} not found")

    owner_repo = ticket["repo_id"]
    owner, repo = owner_repo.split("/", 1)
    pr_number = ticket["pr_number"]
    stored_hash = ticket.get("diff_hash", "")

    # Verify PR hasn't changed since review
    if stored_hash:
        try:
            fetcher = PRFetcher()
            pr = fetcher.fetch(owner, repo, pr_number, cache_hours=0)
            current_hash = diff_hash(pr.diff)
            if current_hash != stored_hash:
                db.release_ticket(ticket_id)
                raise TicketDiffChangedError(
                    f"PR #{pr_number} has changed since review. "
                    f"Diff hash was {stored_hash}, now {current_hash}. "
                    f"Please re-review before acting."
                )
        except TicketDiffChangedError:
            raise
        except Exception as e:
            logger.warning("Could not verify PR diff: %s (proceeding anyway)", e)

    # Execute the action — release ticket on failure so it can be retried
    action_type = ticket["action_type"]
    payload = ticket.get("payload", {})
    comment = payload.get("comment", "")

    try:
        gh = GitHubClient()
        result = _execute_action(gh, owner, repo, pr_number, action_type, comment)
    except Exception as e:
        db.release_ticket(ticket_id)
        logger.error("GitHub action failed for ticket %s, released: %s", ticket_id, e)
        raise TicketError(f"GitHub action failed: {e}") from e

    # Success — permanently burn the ticket
    db.burn_ticket(ticket_id)
    logger.info("Executed %s on PR %s#%d via ticket %s",
                action_type, owner_repo, pr_number, ticket_id)

    return {
        "ticket_id": ticket_id,
        "action": action_type,
        "pr": f"{owner_repo}#{pr_number}",
        "github_response_url": result.get("html_url", ""),
        "status": "executed",
    }


def _execute_action(gh: GitHubClient, owner: str, repo: str,
                    pr_number: int, action_type: str, comment: str) -> dict:
    """Execute a single GitHub action. Raises on failure."""
    if action_type == "APPROVE":
        return gh.post_pr_review(
            owner, repo, pr_number,
            event="APPROVE",
            body=comment or "Looks good to me.",
        )
    elif action_type == "REQUEST_CHANGES":
        return gh.post_pr_review(
            owner, repo, pr_number,
            event="REQUEST_CHANGES",
            body=comment or "Changes requested — see detailed comments.",
        )
    elif action_type == "COMMENT":
        return gh.post_pr_comment(
            owner, repo, pr_number,
            body=comment or "Review comments posted.",
        )
    else:
        raise TicketError(f"Unknown action type: {action_type}")
