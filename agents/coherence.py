"""Coherence Agent — maintains ambient repo knowledge.

Runs on schedule (daily) or on demand. Does NOT review PRs;
it builds and caches context snapshots that other agents consume.
"""

import logging
from datetime import datetime, timezone, timedelta

from agents.base import BaseAgent, AgentResult
from repo_context.builder import RepoContextBuilder
from app import database as db

logger = logging.getLogger(__name__)


class CoherenceAgent(BaseAgent):
    agent_type = "coherence"

    def __init__(self):
        self.builder = RepoContextBuilder()

    def run(self, *, repo_id: str, local_path: str, force: bool = False) -> AgentResult:
        return self._timed_run(
            self._build_context,
            repo_id=repo_id,
            local_path=local_path,
            force=force,
        )

    def _build_context(self, *, repo_id: str, local_path: str,
                       force: bool = False) -> AgentResult:
        # Check if we already have a fresh snapshot
        if not force:
            existing = db.get_latest_snapshot(repo_id)
            if existing:
                built_at = existing.get("built_at", "")
                if built_at:
                    try:
                        ts = datetime.fromisoformat(built_at)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        age = datetime.now(timezone.utc) - ts
                        if age < timedelta(hours=24):
                            logger.info("Snapshot for %s is fresh (%s old), skipping",
                                        repo_id, age)
                            return AgentResult(
                                agent_type=self.agent_type,
                                status="success",
                                summary=f"Context snapshot is fresh ({age.seconds // 3600}h old)",
                                details={"action": "skipped", "age_hours": age.seconds / 3600},
                            )
                    except (ValueError, TypeError):
                        pass

        logger.info("Building context snapshot for %s", repo_id)
        snapshot = self.builder.build_snapshot(local_path)

        db.save_context_snapshot(
            repo_id=repo_id,
            file_tree=snapshot["file_tree"],
            coding_standards=snapshot["coding_standards"],
            design_docs=snapshot["design_docs"],
            recent_prs=snapshot["recent_prs"],
            readme=snapshot["readme_excerpt"],
        )

        file_count = snapshot["file_tree"].get("total_files", 0)
        doc_count = len(snapshot["design_docs"])
        pr_count = len(snapshot["recent_prs"])

        return AgentResult(
            agent_type=self.agent_type,
            status="success",
            summary=(
                f"Built context: {file_count} files, {doc_count} design docs, "
                f"{pr_count} recent commits"
            ),
            details=snapshot,
            confidence=1.0,
        )


def run_coherence_cycle():
    """Run coherence update for all registered repos. Called by scheduler."""
    agent = CoherenceAgent()
    repos = db.list_repos()
    results = []
    for repo in repos:
        if not repo.get("local_path"):
            logger.warning("Repo %s has no local_path, skipping", repo["repo_id"])
            continue
        try:
            result = agent.run(repo_id=repo["repo_id"], local_path=repo["local_path"])
            results.append((repo["repo_id"], result.status))
            logger.info("Coherence for %s: %s", repo["repo_id"], result.summary)
        except Exception as e:
            logger.exception("Coherence failed for %s", repo["repo_id"])
            results.append((repo["repo_id"], f"error: {e}"))
    return results
