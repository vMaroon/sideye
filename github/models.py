"""Data models for GitHub entities."""

from dataclasses import dataclass, field


@dataclass
class Issue:
    number: int
    title: str
    body: str
    state: str
    labels: list[str] = field(default_factory=list)
    url: str = ""


@dataclass
class PRFile:
    filename: str
    status: str  # added, modified, removed, renamed
    additions: int = 0
    deletions: int = 0
    patch: str = ""


@dataclass
class PRInfo:
    number: int
    title: str
    author: str
    description: str
    state: str
    base_branch: str
    head_branch: str
    url: str
    diff: str = ""
    files: list[PRFile] = field(default_factory=list)
    linked_issues: list[Issue] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    commits_count: int = 0

    @property
    def files_changed(self) -> list[str]:
        return [f.filename for f in self.files]

    @property
    def total_additions(self) -> int:
        return sum(f.additions for f in self.files)

    @property
    def total_deletions(self) -> int:
        return sum(f.deletions for f in self.files)

    def to_cache_dict(self) -> dict:
        return {
            "title": self.title,
            "author": self.author,
            "description": self.description,
            "state": self.state,
            "base_branch": self.base_branch,
            "head_branch": self.head_branch,
            "url": self.url,
            "diff": self.diff,
            "labels": self.labels,
            "commits_count": self.commits_count,
            "files": [
                {
                    "filename": f.filename,
                    "status": f.status,
                    "additions": f.additions,
                    "deletions": f.deletions,
                    "patch": f.patch,
                }
                for f in self.files
            ],
            "linked_issues": [
                {
                    "number": i.number,
                    "title": i.title,
                    "body": i.body,
                    "state": i.state,
                    "labels": i.labels,
                    "url": i.url,
                }
                for i in self.linked_issues
            ],
            # Backward compat: keep flat list so old readers don't crash
            "files_changed": self.files_changed,
        }
