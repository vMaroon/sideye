"""Build comprehensive repo context snapshots for the coherence agent."""

import os
import subprocess
import logging
from pathlib import Path
from collections import Counter

from repo_context.detector import detect_standards

logger = logging.getLogger(__name__)


class RepoContextBuilder:
    """Builds a context snapshot for a single repository."""

    def build_snapshot(self, repo_path: str) -> dict:
        """Return a complete context snapshot dict."""
        p = Path(repo_path)
        if not p.exists():
            raise FileNotFoundError(f"Repo path does not exist: {repo_path}")

        return {
            "file_tree": self._scan_file_tree(p),
            "coding_standards": detect_standards(repo_path),
            "design_docs": self._index_design_docs(p),
            "recent_prs": self._extract_recent_prs(p),
            "readme_excerpt": self._read_readme(p),
        }

    def _scan_file_tree(self, p: Path) -> dict:
        """Scan git-tracked files, return structure summary."""
        try:
            result = subprocess.run(
                ["git", "ls-files"],
                cwd=str(p), capture_output=True, text=True, timeout=30,
            )
            files = [f for f in result.stdout.strip().split("\n") if f]
        except Exception as e:
            logger.warning("git ls-files failed: %s", e)
            files = []

        # Count by extension
        ext_counts = Counter()
        dir_counts = Counter()
        for f in files:
            ext = Path(f).suffix.lower()
            if ext:
                ext_counts[ext] += 1
            top_dir = f.split("/")[0] if "/" in f else "."
            dir_counts[top_dir] += 1

        # Map extensions to languages
        ext_to_lang = {
            ".py": "python", ".go": "go", ".js": "javascript", ".ts": "typescript",
            ".yaml": "yaml", ".yml": "yaml", ".md": "markdown", ".sh": "shell",
            ".dockerfile": "docker", ".proto": "protobuf", ".sql": "sql",
        }
        lang_counts = Counter()
        for ext, count in ext_counts.items():
            lang = ext_to_lang.get(ext, ext)
            lang_counts[lang] += count

        return {
            "total_files": len(files),
            "top_directories": dict(dir_counts.most_common(15)),
            "languages": dict(lang_counts.most_common(10)),
            "extensions": dict(ext_counts.most_common(15)),
        }

    def _index_design_docs(self, p: Path) -> list[dict]:
        """Find and summarize design docs, proposals, KEPs, etc."""
        docs = []
        search_dirs = ["docs", "proposals", "design", "keps", "doc", "specifications"]
        search_patterns = ["*.md", "*.rst", "*.txt"]

        for dirname in search_dirs:
            doc_dir = p / dirname
            if not doc_dir.is_dir():
                continue
            for pattern in search_patterns:
                for f in sorted(doc_dir.rglob(pattern))[:20]:
                    try:
                        text = f.read_text(errors="replace")[:500]
                        # Extract title: first heading or first non-empty line
                        title = ""
                        for line in text.split("\n"):
                            line = line.strip()
                            if line.startswith("#"):
                                title = line.lstrip("#").strip()
                                break
                            elif line and not title:
                                title = line[:100]
                                break

                        docs.append({
                            "path": str(f.relative_to(p)),
                            "title": title,
                            "excerpt": text[:200],
                        })
                    except Exception:
                        pass

        return docs[:30]

    def _extract_recent_prs(self, p: Path) -> list[dict]:
        """Extract recent merge/PR info from git log."""
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "--all", "-50", "--format=%h|%an|%ai|%s"],
                cwd=str(p), capture_output=True, text=True, timeout=30,
            )
            lines = result.stdout.strip().split("\n")
        except Exception as e:
            logger.warning("git log failed: %s", e)
            return []

        prs = []
        for line in lines:
            if not line:
                continue
            parts = line.split("|", 3)
            if len(parts) < 4:
                continue
            sha, author, date, subject = parts
            prs.append({
                "sha": sha.strip(),
                "author": author.strip(),
                "date": date.strip()[:10],
                "subject": subject.strip(),
            })

        return prs

    def _read_readme(self, p: Path) -> str:
        """Read the README excerpt."""
        for name in ("README.md", "readme.md", "README.rst", "README"):
            f = p / name
            if f.exists():
                try:
                    return f.read_text(errors="replace")[:800]
                except Exception:
                    pass
        return ""
