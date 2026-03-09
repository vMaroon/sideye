"""GitHub REST API v3 client."""

import logging
import random
import time
import requests

from app.config import Config

logger = logging.getLogger(__name__)

_TIMEOUT = 30  # seconds per request
_MAX_RETRIES = 3
_BACKOFF_BASE = 2.0
_RATE_LIMIT_MAX_WAIT = 60  # seconds — if reset is further, raise instead


class GitHubAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"GitHub API {status_code}: {message}")


class RateLimitError(GitHubAPIError):
    def __init__(self, reset_at: int):
        self.reset_at = reset_at
        super().__init__(403, f"Rate limited, resets at {reset_at}")


class GitHubClient:
    """Thin wrapper around GitHub REST API v3."""

    def __init__(self, token: str | None = None):
        self.token = token or Config.GITHUB_TOKEN
        self.base_url = Config.GITHUB_API_URL
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Send a request with timeout, retry on 5xx/network errors, and rate-limit handling."""
        url = f"{self.base_url}{path}"
        kwargs.setdefault("timeout", _TIMEOUT)
        last_exc = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = self.session.request(method, url, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                if attempt == _MAX_RETRIES:
                    raise GitHubAPIError(0, f"Network error after {_MAX_RETRIES} attempts: {e}")
                delay = _BACKOFF_BASE ** (attempt - 1) + random.uniform(0, 1)
                logger.warning("GitHub request attempt %d/%d failed (%s), retrying in %.1fs",
                               attempt, _MAX_RETRIES, e, delay)
                time.sleep(delay)
                continue

            # Rate limit — wait if reset is soon, otherwise raise
            if resp.status_code == 403:
                remaining = resp.headers.get("X-RateLimit-Remaining", "?")
                reset = int(resp.headers.get("X-RateLimit-Reset", 0))
                if remaining == "0":
                    wait = reset - int(time.time())
                    if 0 < wait <= _RATE_LIMIT_MAX_WAIT and attempt < _MAX_RETRIES:
                        logger.warning("Rate limited, waiting %ds for reset", wait)
                        time.sleep(wait + 1)
                        continue
                    raise RateLimitError(reset)

            # Retry on 5xx
            if resp.status_code >= 500 and attempt < _MAX_RETRIES:
                delay = _BACKOFF_BASE ** (attempt - 1) + random.uniform(0, 1)
                logger.warning("GitHub 5xx (%d) on attempt %d/%d, retrying in %.1fs",
                               resp.status_code, attempt, _MAX_RETRIES, delay)
                time.sleep(delay)
                continue

            if resp.status_code >= 400:
                raise GitHubAPIError(resp.status_code, resp.text[:500])

            return resp

        # Shouldn't reach here, but safety net
        raise GitHubAPIError(0, f"Request failed after {_MAX_RETRIES} attempts")

    def get(self, path: str, **kwargs) -> dict | list:
        return self._request("GET", path, **kwargs).json()

    def post(self, path: str, json_data: dict = None, **kwargs) -> dict:
        return self._request("POST", path, json=json_data, **kwargs).json()

    # ── PR endpoints ─────────────────────────────────────────────

    def get_pr(self, owner: str, repo: str, pr_number: int) -> dict:
        return self.get(f"/repos/{owner}/{repo}/pulls/{pr_number}")

    def get_pr_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Fetch the unified diff for a PR."""
        resp = self._request(
            "GET", f"/repos/{owner}/{repo}/pulls/{pr_number}",
            headers={"Accept": "application/vnd.github.v3.diff"},
        )
        return resp.text

    def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list[dict]:
        """Fetch list of files changed in PR (paginated)."""
        files = []
        page = 1
        while True:
            batch = self.get(
                f"/repos/{owner}/{repo}/pulls/{pr_number}/files",
                params={"per_page": 100, "page": page},
            )
            files.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return files

    # ── Issue endpoints ──────────────────────────────────────────

    def get_issue(self, owner: str, repo: str, issue_number: int) -> dict:
        return self.get(f"/repos/{owner}/{repo}/issues/{issue_number}")

    def list_issues(self, owner: str, repo: str, state: str = "open",
                    limit: int = 30) -> list[dict]:
        return self.get(
            f"/repos/{owner}/{repo}/issues",
            params={"state": state, "per_page": min(limit, 100), "sort": "updated"},
        )

    # ── PR review actions (for one-time tickets) ─────────────────

    def post_pr_review(self, owner: str, repo: str, pr_number: int,
                       event: str, body: str,
                       comments: list[dict] | None = None) -> dict:
        """Submit a PR review, optionally with inline comments.

        event: APPROVE, REQUEST_CHANGES, COMMENT
        comments: optional list of {"path": str, "line": int, "body": str}
                  where line is the diff-side line number on the PR head.
        """
        payload = {"event": event, "body": body}
        if comments:
            payload["comments"] = comments
        return self.post(
            f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            json_data=payload,
        )

    def post_pr_comment(self, owner: str, repo: str, pr_number: int,
                        body: str) -> dict:
        """Post a general comment on a PR."""
        return self.post(
            f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
            json_data={"body": body},
        )

    # ── Repo endpoints ───────────────────────────────────────────

    def get_repo_info(self, owner: str, repo: str) -> dict:
        return self.get(f"/repos/{owner}/{repo}")

    def list_recent_prs(self, owner: str, repo: str, state: str = "all",
                        limit: int = 20) -> list[dict]:
        return self.get(
            f"/repos/{owner}/{repo}/pulls",
            params={"state": state, "per_page": min(limit, 100), "sort": "updated"},
        )
