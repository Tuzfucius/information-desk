-- Expand adapter keys without changing stable row IDs or dropping source/event history.
ALTER TABLE items ADD COLUMN canonical_source_id INTEGER;
ALTER TABLE sources ADD COLUMN next_check_at TEXT;
ALTER TABLE sources ADD COLUMN last_check_success_at TEXT;
ALTER TABLE sources ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sources ADD COLUMN last_error_code TEXT;
ALTER TABLE sources ADD COLUMN refresh_interval_hours INTEGER;
ALTER TABLE sources ADD COLUMN feed TEXT;
UPDATE sources SET feed = CASE source_type
    WHEN 'arxiv' THEN 'arxiv'
    WHEN 'hf_paper' THEN 'hf_papers'
    WHEN 'hf_model' THEN 'hf_models'
    WHEN 'github_repo' THEN 'github'
END;
UPDATE sources SET next_check_at = COALESCE(last_checked_at, last_seen_at);

CREATE TABLE sources_v3 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
    source_type TEXT NOT NULL CHECK (length(source_type) BETWEEN 1 AND 64
        AND source_type GLOB '[a-z]*' AND source_type NOT GLOB '*[^a-z0-9_]*'),
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
    next_check_at TEXT,
    last_check_success_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    last_error_code TEXT,
    refresh_interval_hours INTEGER CHECK (refresh_interval_hours IS NULL OR refresh_interval_hours > 0),
    feed TEXT NOT NULL CHECK (length(feed) BETWEEN 1 AND 64 AND feed GLOB '[a-z]*'
        AND feed NOT GLOB '*[^a-z0-9_]*'),
    UNIQUE (source_type, source_uid),
    UNIQUE (source_type, external_id)
);
INSERT INTO sources_v3 SELECT * FROM sources;
ALTER TABLE sources RENAME TO sources_v2;
ALTER TABLE sources_v3 RENAME TO sources;

CREATE TABLE source_links_v3 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    target_source_type TEXT NOT NULL CHECK (length(target_source_type) BETWEEN 1 AND 64
        AND target_source_type GLOB '[a-z]*'
        AND target_source_type NOT GLOB '*[^a-z0-9_]*'),
    target_external_id TEXT NOT NULL,
    target_url TEXT NOT NULL DEFAULT '',
    relation_type TEXT NOT NULL CHECK (relation_type IN
        ('same_paper', 'official_code', 'official_model', 'references', 'uses', 'related')),
    evidence_level TEXT NOT NULL CHECK (evidence_level IN ('strong', 'supporting', 'weak')),
    evidence_origin TEXT NOT NULL,
    evidence_excerpt TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL,
    UNIQUE (from_source_id, target_source_type, target_external_id, relation_type, evidence_origin)
);
INSERT INTO source_links_v3 SELECT * FROM source_links;
ALTER TABLE source_links RENAME TO source_links_v2;
ALTER TABLE source_links_v3 RENAME TO source_links;

CREATE TABLE sync_state_v3 (
    job_key TEXT PRIMARY KEY,
    feed TEXT NOT NULL CHECK (length(feed) BETWEEN 1 AND 64 AND feed GLOB '[a-z]*'
        AND feed NOT GLOB '*[^a-z0-9_]*'),
    query_json TEXT NOT NULL,
    query_version INTEGER NOT NULL DEFAULT 1,
    coverage_from TEXT,
    completed_until TEXT,
    last_success_at TEXT,
    last_attempt_at TEXT,
    last_status TEXT NOT NULL DEFAULT 'never'
        CHECK (last_status IN ('never', 'success', 'partial', 'failed', 'skipped')),
    last_error TEXT NOT NULL DEFAULT '',
    resume_token TEXT,
    resume_window_start TEXT,
    resume_window_end TEXT,
    provider_cursor TEXT
);
INSERT INTO sync_state_v3 SELECT job_key, feed, query_json, query_version, coverage_from,
    completed_until, last_success_at, last_attempt_at, last_status, last_error,
    NULL, NULL, NULL, NULL
    FROM sync_state;
ALTER TABLE sync_state RENAME TO sync_state_v2;
ALTER TABLE sync_state_v3 RENAME TO sync_state;

CREATE TABLE sync_runs_v3 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_run_id INTEGER REFERENCES sync_runs_v3(id) ON DELETE SET NULL,
    job_key TEXT NOT NULL DEFAULT '',
    feed TEXT CHECK (feed IS NULL OR (length(feed) BETWEEN 1 AND 64
        AND feed GLOB '[a-z]*' AND feed NOT GLOB '*[^a-z0-9_]*')),
    window_start TEXT,
    window_end TEXT,
    completed_until TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failed', 'skipped')),
    fetched_count INTEGER NOT NULL DEFAULT 0,
    new_sources INTEGER NOT NULL DEFAULT 0,
    new_items INTEGER NOT NULL DEFAULT 0,
    new_events INTEGER NOT NULL DEFAULT 0,
    conflict_count INTEGER NOT NULL DEFAULT 0,
    warning_json TEXT NOT NULL DEFAULT '[]',
    error_code TEXT
);
INSERT INTO sync_runs_v3 SELECT * FROM sync_runs;
ALTER TABLE sync_runs RENAME TO sync_runs_v2;
ALTER TABLE sync_runs_v3 RENAME TO sync_runs;

CREATE TABLE change_events_v3 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    run_id INTEGER REFERENCES sync_runs(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL CHECK (event_type IN
        ('discovered', 'source_attached', 'content_changed', 'code_detected')),
    event_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    source_snapshot_json TEXT
);
INSERT INTO change_events_v3 SELECT * FROM change_events;
ALTER TABLE change_events RENAME TO change_events_v2;
ALTER TABLE change_events_v3 RENAME TO change_events;

CREATE TABLE pending_sources_v3 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL CHECK (length(source_type) BETWEEN 1 AND 64
        AND source_type GLOB '[a-z]*' AND source_type NOT GLOB '*[^a-z0-9_]*'),
    source_uid TEXT NOT NULL,
    candidate_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    matched_item_ids_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    resolved_item_id INTEGER REFERENCES items(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO pending_sources_v3 SELECT * FROM pending_sources;
ALTER TABLE pending_sources RENAME TO pending_sources_v2;
ALTER TABLE pending_sources_v3 RENAME TO pending_sources;

DROP TABLE change_events_v2;
DROP TABLE source_links_v2;
DROP TABLE sync_runs_v2;
DROP TABLE sources_v2;
DROP TABLE sync_state_v2;
DROP TABLE pending_sources_v2;

CREATE INDEX idx_sources_item ON sources(item_id);
CREATE INDEX idx_sources_published ON sources(source_type, published_at DESC);
CREATE INDEX idx_sources_recent ON sources(last_seen_at DESC);
CREATE INDEX idx_sources_watch_due ON sources(next_check_at, consecutive_failures, id);
CREATE INDEX idx_events_item ON change_events(item_id, id DESC);
CREATE INDEX idx_events_type ON change_events(event_type, id DESC);
CREATE INDEX idx_events_run ON change_events(run_id, id ASC);
CREATE INDEX idx_runs_job ON sync_runs(job_key, id DESC);
CREATE INDEX idx_runs_root ON sync_runs(root_run_id, id ASC);
CREATE UNIQUE INDEX uq_pending_source_open_identity
    ON pending_sources(source_type, source_uid) WHERE status = 'open';
CREATE INDEX idx_pending_sources_open ON pending_sources(status, created_at);

CREATE TABLE sync_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    page_token TEXT,
    split_depth INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    last_attempt_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'complete')),
    completed_at TEXT,
    UNIQUE (job_key, window_start, window_end)
);
CREATE INDEX idx_sync_segments_queue ON sync_segments(job_key, window_start, window_end, id);
