"""Utilities for parsing unified diffs and resolving line hints to positions.

The GitHub PR review API requires comments to specify `path` (file) and `line`
(the line number on the diff's "right side" — i.e., the new/head version).

Our bot's inline comments use `line_hint` (a text snippet) instead of exact
line numbers, because Claude produces hints from context, not line numbers.

This module bridges the gap: given a unified diff and a set of comments with
line_hint + file, it resolves each to the GitHub API's (path, line) format.
"""

import re
import logging

logger = logging.getLogger(__name__)


def resolve_line_positions(diff_text: str, comments: list[dict]) -> list[dict]:
    """Resolve line_hint → (path, line) for each comment.

    Args:
        diff_text: Full unified diff text (from GitHub's PR diff endpoint).
        comments: List of dicts with at least {file, line_hint, comment}.

    Returns:
        List of dicts with {path, line, body, side} — ready for GitHub API.
        Comments that can't be resolved are omitted.
    """
    file_hunks = _parse_diff(diff_text)
    resolved = []

    for c in comments:
        file_key = _normalize_path(c.get("file", ""))
        hint = c.get("line_hint", "")
        body = c.get("comment", "")

        if not file_key or not body:
            continue

        # Find matching file in diff
        lines = _find_file_lines(file_key, file_hunks)
        if not lines:
            logger.debug("No diff lines found for file: %s", file_key)
            continue

        # Match line_hint to a specific line
        match = _match_hint(hint, lines)
        if not match:
            logger.debug("Could not resolve line_hint '%s' in %s", hint[:60], file_key)
            continue

        resolved.append({
            "path": match["path"],
            "line": match["line"],
            "side": match.get("side", "RIGHT"),
            "body": body,
        })

    return resolved


# ── Diff Parsing ─────────────────────────────────────────────────

_DIFF_FILE_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _parse_diff(diff_text: str) -> dict[str, list[dict]]:
    """Parse unified diff into {normalized_path: [line_info, ...]}."""
    files = {}
    current_file = None
    old_line = 0
    new_line = 0

    for raw_line in diff_text.splitlines():
        # New file header
        m = _DIFF_FILE_RE.match(raw_line)
        if m:
            current_file = m.group(2)  # b/ side = new path
            if current_file not in files:
                files[current_file] = []
            continue

        if not current_file:
            continue

        # Hunk header
        m = _HUNK_RE.match(raw_line)
        if m:
            old_line = int(m.group(1))
            new_line = int(m.group(2))
            continue

        # Skip non-diff content
        if raw_line.startswith("+++") or raw_line.startswith("---"):
            continue
        if raw_line.startswith("\\"):  # "\ No newline at end of file"
            continue

        # Diff lines
        if raw_line.startswith("+"):
            files[current_file].append({
                "content": raw_line[1:],
                "new_line": new_line,
                "old_line": None,
                "type": "add",
                "path": current_file,
            })
            new_line += 1
        elif raw_line.startswith("-"):
            files[current_file].append({
                "content": raw_line[1:],
                "new_line": None,
                "old_line": old_line,
                "type": "del",
                "path": current_file,
            })
            old_line += 1
        else:
            # Context line (starts with space or is empty)
            content = raw_line[1:] if raw_line.startswith(" ") else raw_line
            files[current_file].append({
                "content": content,
                "new_line": new_line,
                "old_line": old_line,
                "type": "ctx",
                "path": current_file,
            })
            old_line += 1
            new_line += 1

    return files


def _normalize_path(p: str) -> str:
    """Strip a/ b/ prefixes and whitespace."""
    return re.sub(r"^[ab]/", "", (p or "").strip())


def _find_file_lines(file_key: str, file_hunks: dict) -> list[dict]:
    """Find diff lines for a file, with fuzzy matching on path suffix."""
    # Exact match
    if file_key in file_hunks:
        return file_hunks[file_key]

    # Suffix match (e.g., "foo/bar.go" matches "pkg/foo/bar.go")
    for path, lines in file_hunks.items():
        if path.endswith(file_key) or file_key.endswith(path):
            return lines

    # Basename match
    basename = file_key.rsplit("/", 1)[-1]
    for path, lines in file_hunks.items():
        if path.rsplit("/", 1)[-1] == basename:
            return lines

    return []


def _match_hint(hint: str, lines: list[dict]) -> dict | None:
    """Find the best matching line for a line_hint.

    Uses the same token-matching approach as the extension's matchLineHint().
    Returns the line dict with path + line number, or None.
    """
    if not hint:
        return None

    h = hint.lower().strip()
    tokens = re.findall(r"[a-zA-Z_]\w+(?:\.\w+)*", h)
    if not tokens:
        return None

    best = None
    best_score = 0

    for entry in lines:
        content = (entry.get("content") or "").lower()
        score = sum(1 for tok in tokens if tok.lower() in content)

        # Prefer added/deleted lines
        if entry["type"] in ("add", "del"):
            score += 0.5

        if score > best_score:
            best_score = score
            best = entry

    if best_score < 1:
        return None

    # GitHub API uses the "line" on the new (RIGHT) side for added/context,
    # and old (LEFT) side for deleted lines.
    if best["type"] == "del":
        return {
            "path": best["path"],
            "line": best["old_line"],
            "side": "LEFT",
        }
    else:
        return {
            "path": best["path"],
            "line": best["new_line"],
            "side": "RIGHT",
        }
