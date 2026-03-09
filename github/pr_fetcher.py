"""Fetch and structure PR data from GitHub, with local caching."""

import re
import hashlib
import logging

from github.client import GitHubClient
from github.models import PRInfo, PRFile, Issue
from app import database as db

logger = logging.getLogger(__name__)

# Patterns for extracting linked issue references from PR descriptions
_ISSUE_REF_PATTERNS = [
    r"(?:fixes|resolves|closes|fix|resolve|close)\s+#(\d+)",
    r"(?:fixes|resolves|closes|fix|resolve|close)\s+https?://github\.com/[\w\-]+/[\w\-]+/issues/(\d+)",
    r"(?:^|\s)#(\d+)(?:\s|$|[,.])",
]


def extract_issue_refs(text: str) -> list[int]:
    """Extract issue numbers referenced in PR description."""
    refs = set()
    for pattern in _ISSUE_REF_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE):
            refs.add(int(m.group(1)))
    return sorted(refs)


def diff_hash(diff: str) -> str:
    """Compute a short hash of the diff for change detection."""
    return hashlib.sha256(diff.encode()).hexdigest()[:16]


class PRFetcher:
    def __init__(self, github: GitHubClient | None = None):
        self.gh = github or GitHubClient()

    def fetch(self, owner: str, repo: str, pr_number: int,
              cache_hours: int = 1) -> PRInfo:
        """
        Fetch full PR data. Uses local cache if fresh enough.
        """
        repo_id = f"{owner}/{repo}"

        # Check cache
        cached = db.get_cached_pr(repo_id, pr_number, max_age_hours=cache_hours)
        if cached:
            logger.info("Using cached PR data for %s#%d", repo_id, pr_number)
            return self._cached_to_prinfo(cached, owner, repo, pr_number)

        # Fetch from GitHub
        logger.info("Fetching PR %s#%d from GitHub", repo_id, pr_number)
        pr_data = self.gh.get_pr(owner, repo, pr_number)
        pr_diff = self.gh.get_pr_diff(owner, repo, pr_number)
        pr_files_raw = self.gh.get_pr_files(owner, repo, pr_number)

        pr_files = [
            PRFile(
                filename=f["filename"],
                status=f["status"],
                additions=f.get("additions", 0),
                deletions=f.get("deletions", 0),
                patch=f.get("patch", ""),
            )
            for f in pr_files_raw
        ]

        description = pr_data.get("body", "") or ""
        issue_nums = extract_issue_refs(description)

        # Fetch linked issues
        linked_issues = []
        for inum in issue_nums[:10]:  # cap to avoid API spam
            try:
                iss = self.gh.get_issue(owner, repo, inum)
                linked_issues.append(Issue(
                    number=inum,
                    title=iss.get("title", ""),
                    body=iss.get("body", "") or "",
                    state=iss.get("state", ""),
                    labels=[l["name"] for l in iss.get("labels", [])],
                    url=iss.get("html_url", ""),
                ))
            except Exception as e:
                logger.warning("Failed to fetch issue #%d: %s", inum, e)

        pr_info = PRInfo(
            number=pr_number,
            title=pr_data.get("title", ""),
            author=pr_data.get("user", {}).get("login", ""),
            description=description,
            state=pr_data.get("state", ""),
            base_branch=pr_data.get("base", {}).get("ref", ""),
            head_branch=pr_data.get("head", {}).get("ref", ""),
            url=pr_data.get("html_url", ""),
            diff=pr_diff,
            files=pr_files,
            linked_issues=linked_issues,
            labels=[l["name"] for l in pr_data.get("labels", [])],
            commits_count=pr_data.get("commits", 0),
        )

        # Cache it
        db.cache_pr_data(repo_id, pr_number, pr_info.to_cache_dict())

        return pr_info

    def _cached_to_prinfo(self, cached: dict, owner: str, repo: str,
                          pr_number: int) -> PRInfo:
        """Reconstruct PRInfo from cached data.

        Handles both old cache format (flat strings/ints) and new format
        (full dicts for files and issues).
        """
        # Reconstruct files — new format stores dicts, old format stores
        # only filenames as strings under files_changed
        raw_files = cached.get("files", [])
        files = []
        for f in raw_files:
            if isinstance(f, dict):
                files.append(PRFile(
                    filename=f.get("filename", ""),
                    status=f.get("status", "modified"),
                    additions=f.get("additions", 0),
                    deletions=f.get("deletions", 0),
                    patch=f.get("patch", ""),
                ))
            elif isinstance(f, str):
                files.append(PRFile(filename=f, status="modified"))

        # Fallback: old cache only had files_changed (list of strings)
        if not files:
            for name in cached.get("files_changed", []):
                if isinstance(name, str):
                    files.append(PRFile(filename=name, status="modified"))

        # Reconstruct linked issues — new format stores dicts, old stored ints
        raw_issues = cached.get("linked_issues", [])
        issues = []
        for i in raw_issues:
            if isinstance(i, dict):
                issues.append(Issue(
                    number=i.get("number", 0),
                    title=i.get("title", ""),
                    body=i.get("body", ""),
                    state=i.get("state", ""),
                    labels=i.get("labels", []),
                    url=i.get("url", ""),
                ))
            elif isinstance(i, int):
                issues.append(Issue(number=i, title="", body="", state=""))

        return PRInfo(
            number=pr_number,
            title=cached.get("pr_title", ""),
            author=cached.get("pr_author", ""),
            description=cached.get("pr_description", ""),
            state=cached.get("state", "open"),
            base_branch=cached.get("base_branch", ""),
            head_branch=cached.get("head_branch", ""),
            url=cached.get("url", f"https://github.com/{owner}/{repo}/pull/{pr_number}"),
            diff=cached.get("diff_content", ""),
            files=files,
            linked_issues=issues,
            labels=cached.get("labels", []),
            commits_count=cached.get("commits_count", 0),
        )
