"""Application configuration loaded from environment / .env file."""

import os
import json
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
_project_root = Path(__file__).parent.parent
load_dotenv(_project_root / ".env")


class Config:
    # GitHub
    GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")
    GITHUB_API_URL: str = os.getenv("GITHUB_API_URL", "https://api.github.com")

    # Claude backend: "cli" (default, uses Pro subscription) or "api" (needs ANTHROPIC_API_KEY)
    CLAUDE_BACKEND: str = os.getenv("CLAUDE_BACKEND", "cli")
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6-20250514")
    CLAUDE_HAIKU: str = "claude-haiku-4-5-20251001"
    CLAUDE_SONNET: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6-20250514")
    CLAUDE_OPUS: str = "claude-opus-4-6-20250514"

    REVIEW_MODES: dict = {
        "quick": {
            "default": "claude-haiku-4-5-20251001",
        },
        "standard": {
            "default": os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6-20250514"),
            "injection_scanner": "claude-haiku-4-5-20251001",
            "synthesis": "claude-opus-4-6-20250514",
        },
        "thorough": {
            "default": "claude-opus-4-6-20250514",
            "injection_scanner": "claude-haiku-4-5-20251001",
        },
    }

    # App
    APP_PORT: int = int(os.getenv("APP_PORT", "8111"))
    APP_HOST: str = os.getenv("APP_HOST", "127.0.0.1")

    # Database
    DB_PATH: str = os.getenv("DB_PATH", str(_project_root / "data" / "reviews.db"))

    # Coherence
    COHERENCE_CRON: str = os.getenv("COHERENCE_CRON", "0 9 * * *")

    # Workspace root (parent of all repos)
    WORKSPACE_ROOT: str = os.getenv("WORKSPACE_ROOT", str(_project_root.parent))

    @classmethod
    def validate(cls) -> list[str]:
        """Return list of missing critical config keys."""
        issues = []
        if not cls.GITHUB_TOKEN:
            issues.append("GITHUB_TOKEN not set")
        if cls.CLAUDE_BACKEND == "api" and not cls.ANTHROPIC_API_KEY:
            issues.append("CLAUDE_BACKEND=api but ANTHROPIC_API_KEY not set")
        if cls.CLAUDE_BACKEND == "cli":
            import shutil
            if not shutil.which("claude"):
                issues.append("CLAUDE_BACKEND=cli but claude CLI not found on PATH")
        return issues

    @classmethod
    def db_dir(cls) -> Path:
        return Path(cls.DB_PATH).parent
