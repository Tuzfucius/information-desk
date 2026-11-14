ALTER TABLE change_events ADD COLUMN source_snapshot_json TEXT;
ALTER TABLE merge_conflicts ADD COLUMN resolved_item_id INTEGER REFERENCES items(id) ON DELETE RESTRICT;

CREATE TABLE pending_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL
        CHECK (source_type IN ('arxiv', 'hf_paper', 'hf_model', 'github_repo')),
    source_uid TEXT NOT NULL,
    candidate_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    matched_item_ids_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'resolved')),
    resolved_item_id INTEGER REFERENCES items(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX uq_pending_source_open_identity
    ON pending_sources(source_type, source_uid) WHERE status = 'open';
CREATE INDEX idx_pending_sources_open ON pending_sources(status, created_at);
