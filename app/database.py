"""SQLite database initialization and helpers."""

import sqlite3
import json
import uuid
import logging
from pathlib import Path
from datetime import datetime, timezone
from contextlib import contextmanager

from app.config import Config

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).parent.parent / "db" / "schema.sql"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def init_database() -> None:
    """Create database file and run schema DDL."""
    db_path = Path(Config.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    with open(_SCHEMA_PATH) as f:
        conn.executescript(f.read())

    # Migrations: add columns that may be missing in older databases
    for migration in [
        "ALTER TABLE review_results ADD COLUMN prompt_sent TEXT",
        "ALTER TABLE pr_reviews ADD COLUMN error_message TEXT",
        "ALTER TABLE action_tickets ADD COLUMN claimed_at TIMESTAMP",
        "ALTER TABLE review_results ADD COLUMN model_used TEXT",
        "ALTER TABLE review_results ADD COLUMN input_tokens INTEGER",
        "ALTER TABLE review_results ADD COLUMN output_tokens INTEGER",
        "ALTER TABLE review_results ADD COLUMN tokens_estimated BOOLEAN DEFAULT FALSE",
    ]:
        try:
            conn.execute(migration)
        except sqlite3.OperationalError:
            pass  # Column already exists

    # Ensure __global__ pseudo-repo exists for cross-repo preferences
    conn.execute(
        """INSERT INTO repositories (repo_id, owner, name, url)
           VALUES ('__global__', '__system__', '__global__', '')
           ON CONFLICT(repo_id) DO NOTHING"""
    )
    conn.commit()
    conn.close()
    logger.info("Database initialized at %s", db_path)


@contextmanager
def get_db():
    """Context manager yielding a sqlite3 connection with row_factory."""
    conn = sqlite3.connect(Config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Repository CRUD ──────────────────────────────────────────────

def upsert_repo(owner: str, name: str, url: str, local_path: str = "",
                language: str = "") -> str:
    repo_id = f"{owner}/{name}"
    with get_db() as db:
        db.execute(
            """INSERT INTO repositories (repo_id, owner, name, url, local_path, language)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(repo_id) DO UPDATE SET
                 url=excluded.url, local_path=excluded.local_path,
                 language=excluded.language""",
            (repo_id, owner, name, url, local_path, language),
        )
    return repo_id


def get_repo(repo_id: str) -> dict | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM repositories WHERE repo_id=?", (repo_id,)).fetchone()
        return dict(row) if row else None


def find_repo_by_name(name: str) -> dict | None:
    """Find a registered repo by just its name (ignoring owner).

    This is the key lookup for matching an upstream PR URL to a local fork clone.
    E.g., a PR on llm-d/llm-d-inference-scheduler matches a registered repo
    maroonay/llm-d-inference-scheduler because the repo *name* is the same.

    Prioritizes repos that have a local_path (fork with clone) over upstream-only entries.
    """
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM repositories WHERE name=? ORDER BY created_at",
            (name,),
        ).fetchall()
        if not rows:
            return None
        # Prefer the one with local_path set (the fork clone)
        for r in rows:
            if r["local_path"]:
                return dict(r)
        return dict(rows[0])


def list_repos() -> list[dict]:
    with get_db() as db:
        rows = db.execute("SELECT * FROM repositories ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


# ── Context Snapshots ────────────────────────────────────────────

def save_context_snapshot(repo_id: str, file_tree: dict, coding_standards: dict,
                          design_docs: list, recent_prs: list, readme: str) -> str:
    sid = new_id()
    with get_db() as db:
        db.execute(
            """INSERT INTO context_snapshots
               (snapshot_id, repo_id, file_tree, coding_standards, design_docs, recent_prs, readme_excerpt)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (sid, repo_id, json.dumps(file_tree), json.dumps(coding_standards),
             json.dumps(design_docs), json.dumps(recent_prs), readme),
        )
        db.execute(
            "UPDATE repositories SET last_coherence_run=? WHERE repo_id=?",
            (_now_iso(), repo_id),
        )
    return sid


def get_latest_snapshot(repo_id: str) -> dict | None:
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM context_snapshots WHERE repo_id=? ORDER BY built_at DESC LIMIT 1",
            (repo_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        for col in ("file_tree", "coding_standards", "design_docs", "recent_prs"):
            if d.get(col):
                d[col] = json.loads(d[col])
        return d


# ── PR Reviews ───────────────────────────────────────────────────

def create_review(repo_id: str, pr_number: int, pr_title: str,
                  pr_author: str, pr_url: str, branch: str = "") -> str:
    rid = new_id()
    with get_db() as db:
        db.execute(
            """INSERT INTO pr_reviews
               (review_id, repo_id, pr_number, pr_title, pr_author, pr_url, branch_name, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'in_progress')
               ON CONFLICT(repo_id, pr_number) DO UPDATE SET
                 pr_title=excluded.pr_title, pr_author=excluded.pr_author,
                 status='in_progress', review_id=excluded.review_id""",
            (rid, repo_id, pr_number, pr_title, pr_author, pr_url, branch),
        )
    return rid


def update_review_status(review_id: str, status: str, error_message: str = "") -> None:
    with get_db() as db:
        if error_message:
            db.execute(
                "UPDATE pr_reviews SET status=?, error_message=? WHERE review_id=?",
                (status, error_message[:1000], review_id),
            )
        else:
            db.execute("UPDATE pr_reviews SET status=? WHERE review_id=?", (status, review_id))


def get_review(review_id: str) -> dict | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM pr_reviews WHERE review_id=?", (review_id,)).fetchone()
        return dict(row) if row else None


def list_reviews(repo_id: str | None = None, limit: int = 50) -> list[dict]:
    with get_db() as db:
        if repo_id:
            rows = db.execute(
                "SELECT * FROM pr_reviews WHERE repo_id=? ORDER BY created_at DESC LIMIT ?",
                (repo_id, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM pr_reviews ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


# ── Review Results ───────────────────────────────────────────────

def save_review_result(review_id: str, agent_type: str, status: str,
                       verdict: str, summary: str, details: dict,
                       confidence: float, execution_time_ms: int,
                       prompt_sent: str = "",
                       model_used: str = "", input_tokens: int = 0,
                       output_tokens: int = 0,
                       tokens_estimated: bool = False) -> str:
    rid = new_id()
    with get_db() as db:
        db.execute(
            """INSERT INTO review_results
               (result_id, review_id, agent_type, status, verdict, summary, details,
                confidence, execution_time_ms, prompt_sent,
                model_used, input_tokens, output_tokens, tokens_estimated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (rid, review_id, agent_type, status, verdict, summary,
             json.dumps(details), confidence, execution_time_ms, prompt_sent,
             model_used, input_tokens, output_tokens, tokens_estimated),
        )
    return rid


def get_review_results(review_id: str) -> list[dict]:
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM review_results WHERE review_id=? ORDER BY ran_at",
            (review_id,),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            if d.get("details"):
                d["details"] = json.loads(d["details"])
            results.append(d)
        return results


# ── Preferences ──────────────────────────────────────────────────

def save_preference(repo_id: str, category: str, feedback_data: dict) -> str:
    pid = new_id()
    with get_db() as db:
        db.execute(
            """INSERT INTO review_preferences (pref_id, repo_id, category, feedback_data)
               VALUES (?, ?, ?, ?)""",
            (pid, repo_id, category, json.dumps(feedback_data)),
        )
    return pid


def save_agent_config(repo_id: str, config: dict) -> str:
    """Save or update agent configuration for a repo.

    Config shape:
    {
        "review_guidelines": "Free-text guidelines...",
        "custom_standards": "Override or supplement auto-detected standards...",
        "contextual_focus": ["scope_alignment", "coherence", ...],
        "unbiased_focus": ["security", "error_handling", ...],
        "ignore_patterns": ["*.generated.go", "vendor/", ...],
        "tone": "direct|gentle|strict",
        "severity_threshold": "nit|minor|major|critical",
    }
    """
    # Use upsert — one agent_config per repo
    with get_db() as db:
        existing = db.execute(
            "SELECT pref_id FROM review_preferences WHERE repo_id=? AND category='agent_config'",
            (repo_id,),
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE review_preferences SET feedback_data=? WHERE pref_id=?",
                (json.dumps(config), existing["pref_id"]),
            )
            return existing["pref_id"]
        else:
            pid = new_id()
            db.execute(
                "INSERT INTO review_preferences (pref_id, repo_id, category, feedback_data) VALUES (?, ?, 'agent_config', ?)",
                (pid, repo_id, json.dumps(config)),
            )
            return pid


def get_agent_config(repo_id: str) -> dict | None:
    """Get agent configuration for a repo. Returns None if not configured."""
    with get_db() as db:
        row = db.execute(
            "SELECT feedback_data FROM review_preferences WHERE repo_id=? AND category='agent_config' ORDER BY created_at DESC LIMIT 1",
            (repo_id,),
        ).fetchone()
        if not row:
            return None
        return json.loads(row["feedback_data"])


def get_preferences(repo_id: str, category: str | None = None) -> list[dict]:
    with get_db() as db:
        if category:
            rows = db.execute(
                "SELECT * FROM review_preferences WHERE repo_id=? AND category=? ORDER BY created_at DESC",
                (repo_id, category),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM review_preferences WHERE repo_id=? ORDER BY created_at DESC",
                (repo_id,),
            ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            if d.get("feedback_data"):
                d["feedback_data"] = json.loads(d["feedback_data"])
            results.append(d)
        return results


# ── Review Submissions (implicit feedback tracking) ──────────────

def save_submission(review_id: str, repo_id: str, pr_number: int,
                    suggested_verdict: str, chosen_verdict: str,
                    total_suggested: int, total_selected: int,
                    total_edited: int, comments_data: list[dict]) -> str:
    sid = new_id()
    with get_db() as db:
        db.execute(
            """INSERT INTO review_submissions
               (submission_id, review_id, repo_id, pr_number,
                suggested_verdict, chosen_verdict,
                total_suggested, total_selected, total_edited, comments_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sid, review_id, repo_id, pr_number,
             suggested_verdict, chosen_verdict,
             total_suggested, total_selected, total_edited,
             json.dumps(comments_data)),
        )
    return sid


def get_submissions(repo_id: str, limit: int = 50) -> list[dict]:
    with get_db() as db:
        rows = db.execute(
            """SELECT * FROM review_submissions
               WHERE repo_id=? ORDER BY submitted_at DESC LIMIT ?""",
            (repo_id, limit),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            if d.get("comments_data"):
                d["comments_data"] = json.loads(d["comments_data"])
            results.append(d)
        return results


def count_submissions(repo_id: str) -> int:
    with get_db() as db:
        row = db.execute(
            "SELECT COUNT(*) as cnt FROM review_submissions WHERE repo_id=?",
            (repo_id,),
        ).fetchone()
        return row["cnt"] if row else 0


# ── Reviewer Directive ───────────────────────────────────────────

def save_reviewer_directive(repo_id: str, directive_data: dict) -> str:
    """Save or update the synthesized reviewer directive (one per repo)."""
    with get_db() as db:
        existing = db.execute(
            "SELECT pref_id FROM review_preferences WHERE repo_id=? AND category='reviewer_directive'",
            (repo_id,),
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE review_preferences SET feedback_data=?, created_at=? WHERE pref_id=?",
                (json.dumps(directive_data), _now_iso(), existing["pref_id"]),
            )
            return existing["pref_id"]
        else:
            pid = new_id()
            db.execute(
                "INSERT INTO review_preferences (pref_id, repo_id, category, feedback_data) VALUES (?, ?, 'reviewer_directive', ?)",
                (pid, repo_id, json.dumps(directive_data)),
            )
            return pid


def get_reviewer_directive(repo_id: str) -> dict | None:
    with get_db() as db:
        row = db.execute(
            "SELECT feedback_data FROM review_preferences WHERE repo_id=? AND category='reviewer_directive' ORDER BY created_at DESC LIMIT 1",
            (repo_id,),
        ).fetchone()
        if not row:
            return None
        return json.loads(row["feedback_data"])


# ── Claude Usage Tracking ────────────────────────────────────────

def save_usage(review_id: str | None, agent_type: str | None,
               model: str, backend: str,
               input_tokens: int, output_tokens: int,
               tokens_estimated: bool = False,
               elapsed_ms: int = 0) -> str:
    uid = new_id()
    with get_db() as db:
        db.execute(
            """INSERT INTO claude_usage
               (usage_id, review_id, agent_type, model, backend,
                input_tokens, output_tokens, tokens_estimated, elapsed_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (uid, review_id, agent_type, model, backend,
             input_tokens, output_tokens, tokens_estimated, elapsed_ms),
        )
    return uid


def get_usage_summary(days: int = 30) -> dict:
    """Aggregate usage stats over the last N days."""
    with get_db() as db:
        # Totals
        row = db.execute(
            """SELECT
                 COUNT(*) as total_calls,
                 COALESCE(SUM(input_tokens), 0) as total_input,
                 COALESCE(SUM(output_tokens), 0) as total_output,
                 COALESCE(SUM(input_tokens + output_tokens), 0) as total_tokens,
                 COALESCE(AVG(elapsed_ms), 0) as avg_elapsed_ms
               FROM claude_usage
               WHERE created_at > datetime('now', ? || ' days')""",
            (f"-{days}",),
        ).fetchone()
        totals = dict(row) if row else {}

        # By model
        rows = db.execute(
            """SELECT model,
                 COUNT(*) as calls,
                 SUM(input_tokens + output_tokens) as tokens
               FROM claude_usage
               WHERE created_at > datetime('now', ? || ' days')
               GROUP BY model ORDER BY tokens DESC""",
            (f"-{days}",),
        ).fetchall()
        by_model = [dict(r) for r in rows]

        # By agent type
        rows = db.execute(
            """SELECT agent_type,
                 COUNT(*) as calls,
                 SUM(input_tokens + output_tokens) as tokens,
                 AVG(elapsed_ms) as avg_ms
               FROM claude_usage
               WHERE created_at > datetime('now', ? || ' days')
                 AND agent_type IS NOT NULL
               GROUP BY agent_type ORDER BY tokens DESC""",
            (f"-{days}",),
        ).fetchall()
        by_agent = [dict(r) for r in rows]

        # By day (last N days)
        rows = db.execute(
            """SELECT DATE(created_at) as day,
                 COUNT(*) as calls,
                 SUM(input_tokens + output_tokens) as tokens
               FROM claude_usage
               WHERE created_at > datetime('now', ? || ' days')
               GROUP BY DATE(created_at) ORDER BY day DESC""",
            (f"-{days}",),
        ).fetchall()
        by_day = [dict(r) for r in rows]

    return {
        **totals,
        "days": days,
        "by_model": by_model,
        "by_agent_type": by_agent,
        "by_day": by_day,
    }


def get_review_usage(review_id: str) -> list[dict]:
    """Get all usage rows for a single review."""
    with get_db() as db:
        rows = db.execute(
            """SELECT * FROM claude_usage
               WHERE review_id=? ORDER BY created_at""",
            (review_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Action Tickets ───────────────────────────────────────────────

def create_ticket(review_id: str, repo_id: str, action_type: str,
                  pr_number: int, payload: dict, diff_hash: str) -> str:
    tid = new_id()
    with get_db() as db:
        db.execute(
            """INSERT INTO action_tickets
               (ticket_id, review_id, repo_id, action_type, pr_number, payload, diff_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (tid, review_id, repo_id, action_type, pr_number, json.dumps(payload), diff_hash),
        )
    return tid


def use_ticket(ticket_id: str) -> dict | None:
    """Legacy: atomically mark ticket as used. Returns ticket data if valid, None if already used.

    Prefer claim_ticket() + burn_ticket() for safe execute-then-burn pattern.
    """
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM action_tickets WHERE ticket_id=? AND used_at IS NULL",
            (ticket_id,),
        ).fetchone()
        if not row:
            return None
        db.execute(
            "UPDATE action_tickets SET used_at=? WHERE ticket_id=?",
            (_now_iso(), ticket_id),
        )
        d = dict(row)
        if d.get("payload"):
            d["payload"] = json.loads(d["payload"])
        return d


def claim_ticket(ticket_id: str) -> dict | None:
    """Claim a ticket for execution. Returns ticket data if available, None otherwise.

    Sets claimed_at but NOT used_at. Call burn_ticket() after successful execution,
    or release_ticket() if execution fails.
    """
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM action_tickets WHERE ticket_id=? AND used_at IS NULL AND claimed_at IS NULL",
            (ticket_id,),
        ).fetchone()
        if not row:
            return None
        db.execute(
            "UPDATE action_tickets SET claimed_at=? WHERE ticket_id=?",
            (_now_iso(), ticket_id),
        )
        d = dict(row)
        if d.get("payload"):
            d["payload"] = json.loads(d["payload"])
        return d


def burn_ticket(ticket_id: str) -> None:
    """Mark a claimed ticket as permanently used after successful execution."""
    with get_db() as db:
        db.execute(
            "UPDATE action_tickets SET used_at=? WHERE ticket_id=?",
            (_now_iso(), ticket_id),
        )


def release_ticket(ticket_id: str) -> None:
    """Release a claimed ticket back to available state (execution failed)."""
    with get_db() as db:
        db.execute(
            "UPDATE action_tickets SET claimed_at=NULL WHERE ticket_id=? AND used_at IS NULL",
            (ticket_id,),
        )


def get_ticket(ticket_id: str) -> dict | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM action_tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("payload"):
            d["payload"] = json.loads(d["payload"])
        return d


# ── Review Management ─────────────────────────────────────────────

def delete_review(review_id: str) -> None:
    """Delete a review and all its child records (results, tickets, preferences)."""
    with get_db() as db:
        db.execute("DELETE FROM review_results WHERE review_id=?", (review_id,))
        db.execute("DELETE FROM action_tickets WHERE review_id=?", (review_id,))
        db.execute("DELETE FROM pr_reviews WHERE review_id=?", (review_id,))


# ── PR Cache ─────────────────────────────────────────────────────

def cache_pr_data(repo_id: str, pr_number: int, data: dict) -> None:
    key = f"{repo_id}#{pr_number}"
    # Store enriched file dicts in files_changed column if available,
    # falling back to flat filename list for backward compat
    files_data = data.get("files") or data.get("files_changed", [])
    with get_db() as db:
        db.execute(
            """INSERT INTO pr_data_cache
               (cache_key, repo_id, pr_number, diff_content, pr_description, pr_title,
                pr_author, linked_issues, files_changed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(cache_key) DO UPDATE SET
                 diff_content=excluded.diff_content, pr_description=excluded.pr_description,
                 pr_title=excluded.pr_title, pr_author=excluded.pr_author,
                 linked_issues=excluded.linked_issues, files_changed=excluded.files_changed,
                 cached_at=CURRENT_TIMESTAMP""",
            (key, repo_id, pr_number, data.get("diff", ""), data.get("description", ""),
             data.get("title", ""), data.get("author", ""),
             json.dumps(data.get("linked_issues", [])),
             json.dumps(files_data)),
        )


def clear_pr_cache(repo_id: str, pr_number: int) -> None:
    """Remove cached PR data so a fresh fetch is triggered on next review."""
    key = f"{repo_id}#{pr_number}"
    with get_db() as db:
        db.execute("DELETE FROM pr_data_cache WHERE cache_key=?", (key,))


def get_cached_pr(repo_id: str, pr_number: int, max_age_hours: int = 1) -> dict | None:
    key = f"{repo_id}#{pr_number}"
    with get_db() as db:
        row = db.execute(
            """SELECT * FROM pr_data_cache
               WHERE cache_key=?
                 AND cached_at > datetime('now', ? || ' hours')""",
            (key, f"-{max_age_hours}"),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        for col in ("linked_issues", "files_changed"):
            if d.get(col):
                d[col] = json.loads(d[col])
        # Expose files_changed as "files" too so _cached_to_prinfo can
        # reconstruct PRFile objects from the enriched data
        if "files_changed" in d:
            d["files"] = d["files_changed"]
        return d
