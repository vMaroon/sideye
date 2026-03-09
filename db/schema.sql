-- Sideye Schema

CREATE TABLE IF NOT EXISTS repositories (
    repo_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    local_path TEXT,
    language TEXT,
    last_coherence_run TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS context_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    file_tree TEXT,
    coding_standards TEXT,
    design_docs TEXT,
    recent_prs TEXT,
    readme_excerpt TEXT,
    built_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (repo_id) REFERENCES repositories(repo_id)
);

CREATE TABLE IF NOT EXISTS pr_reviews (
    review_id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    pr_title TEXT NOT NULL,
    pr_author TEXT NOT NULL,
    pr_url TEXT NOT NULL,
    branch_name TEXT,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (repo_id) REFERENCES repositories(repo_id),
    UNIQUE(repo_id, pr_number)
);

CREATE TABLE IF NOT EXISTS review_results (
    result_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    status TEXT,
    verdict TEXT,
    summary TEXT,
    details TEXT,
    confidence REAL,
    execution_time_ms INTEGER,
    prompt_sent TEXT,
    ran_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (review_id) REFERENCES pr_reviews(review_id)
);

CREATE TABLE IF NOT EXISTS review_preferences (
    pref_id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    category TEXT NOT NULL,
    feedback_data TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (repo_id) REFERENCES repositories(repo_id)
);

CREATE TABLE IF NOT EXISTS action_tickets (
    ticket_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL,
    repo_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    payload TEXT,
    diff_hash TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    used_at TIMESTAMP,
    FOREIGN KEY (repo_id) REFERENCES repositories(repo_id),
    FOREIGN KEY (review_id) REFERENCES pr_reviews(review_id)
);

CREATE TABLE IF NOT EXISTS pr_data_cache (
    cache_key TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    diff_content TEXT,
    pr_description TEXT,
    pr_title TEXT,
    pr_author TEXT,
    linked_issues TEXT,
    files_changed TEXT,
    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (repo_id) REFERENCES repositories(repo_id)
);

CREATE TABLE IF NOT EXISTS review_submissions (
    submission_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL,
    repo_id TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    suggested_verdict TEXT NOT NULL,
    chosen_verdict TEXT NOT NULL,
    total_suggested INTEGER NOT NULL,
    total_selected INTEGER NOT NULL,
    total_edited INTEGER NOT NULL,
    comments_data TEXT,
    submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (review_id) REFERENCES pr_reviews(review_id),
    FOREIGN KEY (repo_id) REFERENCES repositories(repo_id)
);

CREATE TABLE IF NOT EXISTS claude_usage (
    usage_id TEXT PRIMARY KEY,
    review_id TEXT,
    agent_type TEXT,
    model TEXT NOT NULL,
    backend TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    tokens_estimated BOOLEAN DEFAULT FALSE,
    elapsed_ms INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_review_results_review ON review_results(review_id);
CREATE INDEX IF NOT EXISTS idx_context_snapshots_repo ON context_snapshots(repo_id, built_at DESC);
CREATE INDEX IF NOT EXISTS idx_pr_reviews_repo ON pr_reviews(repo_id, pr_number);
CREATE INDEX IF NOT EXISTS idx_review_prefs_repo ON review_preferences(repo_id, category);
CREATE INDEX IF NOT EXISTS idx_review_submissions_repo ON review_submissions(repo_id, submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_claude_usage_date ON claude_usage(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_claude_usage_review ON claude_usage(review_id);
