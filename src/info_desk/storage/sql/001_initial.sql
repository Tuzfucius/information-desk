CREATE TABLE items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'general',
    triage_status TEXT NOT NULL DEFAULT 'inbox'
        CHECK (triage_status IN ('inbox', 'saved', 'ignored')),
    is_watched INTEGER NOT NULL DEFAULT 0 CHECK (is_watched IN (0, 1)),
    code_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK (code_status IN ('unknown', 'not_detected', 'detected')),
    notes TEXT NOT NULL DEFAULT '',
    title_locked INTEGER NOT NULL DEFAULT 0 CHECK (title_locked IN (0, 1)),
    summary_locked INTEGER NOT NULL DEFAULT 0 CHECK (summary_locked IN (0, 1)),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
    source_type TEXT NOT NULL
        CHECK (source_type IN ('arxiv', 'hf_paper', 'hf_model', 'github_repo')),
    source_uid TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    source_updated_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_checked_at TEXT,
    content_hash TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (source_type, source_uid),
    UNIQUE (source_type, external_id)
);

CREATE TABLE source_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    target_source_type TEXT NOT NULL
        CHECK (target_source_type IN ('arxiv', 'hf_paper', 'hf_model', 'github_repo')),
    target_external_id TEXT NOT NULL,
    target_url TEXT NOT NULL DEFAULT '',
    relation_type TEXT NOT NULL
        CHECK (relation_type IN
            ('same_paper', 'official_code', 'official_model', 'references', 'uses', 'related')),
    evidence_level TEXT NOT NULL CHECK (evidence_level IN ('strong', 'supporting', 'weak')),
    evidence_origin TEXT NOT NULL,
    evidence_excerpt TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL,
    UNIQUE (from_source_id, target_source_type, target_external_id, relation_type, evidence_origin)
);

CREATE TABLE sync_state (
    job_key TEXT PRIMARY KEY,
    feed TEXT NOT NULL CHECK (feed IN ('arxiv', 'hf_papers', 'hf_models', 'github')),
    query_json TEXT NOT NULL,
    query_version INTEGER NOT NULL DEFAULT 1,
    coverage_from TEXT,
    completed_until TEXT,
    last_success_at TEXT,
    last_attempt_at TEXT,
    last_status TEXT NOT NULL DEFAULT 'never'
        CHECK (last_status IN ('never', 'success', 'partial', 'failed')),
    last_error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_run_id INTEGER REFERENCES sync_runs(id) ON DELETE SET NULL,
    job_key TEXT NOT NULL DEFAULT '',
    feed TEXT CHECK (feed IN ('arxiv', 'hf_papers', 'hf_models', 'github')),
    window_start TEXT,
    window_end TEXT,
    completed_until TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failed')),
    fetched_count INTEGER NOT NULL DEFAULT 0,
    new_sources INTEGER NOT NULL DEFAULT 0,
    new_items INTEGER NOT NULL DEFAULT 0,
    new_events INTEGER NOT NULL DEFAULT 0,
    conflict_count INTEGER NOT NULL DEFAULT 0,
    warning_json TEXT NOT NULL DEFAULT '[]',
    error_code TEXT
);

CREATE TABLE change_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    run_id INTEGER REFERENCES sync_runs(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL
        CHECK (event_type IN ('discovered', 'source_attached', 'content_changed', 'code_detected')),
    event_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE merge_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    left_item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
    right_item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'resolved', 'dismissed')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    CHECK (left_item_id < right_item_id)
);

CREATE UNIQUE INDEX uq_open_merge_pair
    ON merge_conflicts(left_item_id, right_item_id) WHERE status = 'open';
CREATE INDEX idx_sources_item ON sources(item_id);
CREATE INDEX idx_sources_published ON sources(source_type, published_at DESC);
CREATE INDEX idx_sources_recent ON sources(last_seen_at DESC);
CREATE INDEX idx_items_triage ON items(triage_status, updated_at DESC, id DESC);
CREATE INDEX idx_items_watch ON items(is_watched, updated_at DESC, id DESC);
CREATE INDEX idx_items_seen ON items(first_seen_at DESC, id DESC);
CREATE INDEX idx_events_item ON change_events(item_id, id DESC);
CREATE INDEX idx_events_type ON change_events(event_type, id DESC);
CREATE INDEX idx_events_run ON change_events(run_id, id ASC);
CREATE INDEX idx_runs_job ON sync_runs(job_key, id DESC);
CREATE INDEX idx_runs_root ON sync_runs(root_run_id, id ASC);
