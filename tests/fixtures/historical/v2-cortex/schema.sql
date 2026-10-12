CREATE TABLE cortex_operations (
    operation_id TEXT PRIMARY KEY,
    request_digest TEXT NOT NULL,
    principal TEXT NOT NULL,
    local_project TEXT NOT NULL,
    cortex_project TEXT NOT NULL,
    operation_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('planned', 'sending', 'acknowledged', 'uncertain')),
    receipt_json TEXT,
    upstream_object_id TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX cortex_operations_project_state ON cortex_operations(local_project, state);
