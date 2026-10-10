"""SQLite schema owned by setup and explicit offline migrations.

Feature modules only read and write data. Adding a table or changing its contract
requires a new SCHEMA_VERSION and a migration test from a historical fixture.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from functools import lru_cache

from .errors import IntegrityError

SCHEMA_VERSION = 3
APPLICATION_ID = 0x444F5453

MEMORIES = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY, identity_key TEXT NOT NULL,
    source TEXT NOT NULL, account TEXT NOT NULL, event_id TEXT NOT NULL,
    project TEXT NOT NULL, title TEXT NOT NULL, current_revision INTEGER NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(project, identity_key)
)
"""

REVISIONS = """
CREATE TABLE IF NOT EXISTS revisions (
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL, content TEXT NOT NULL, digest TEXT NOT NULL,
    source_uri TEXT, title TEXT NOT NULL, writer_principal TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(memory_id, revision)
)
"""

SCOPED_SUPPRESSIONS = """
CREATE TABLE IF NOT EXISTS scoped_suppressions (
    project TEXT NOT NULL, identity_key TEXT NOT NULL,
    deleted_revision INTEGER NOT NULL, deleted_by TEXT NOT NULL, deleted_at TEXT NOT NULL,
    PRIMARY KEY(project, identity_key)
)
"""

VECTORS = """
CREATE TABLE IF NOT EXISTS vectors (
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL, model TEXT NOT NULL,
    dimension INTEGER NOT NULL, vector BLOB NOT NULL,
    PRIMARY KEY(memory_id, model)
)
"""

STATEMENTS = (
    MEMORIES,
    REVISIONS,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
        memory_id UNINDEXED, title, content, tokenize='unicode61 remove_diacritics 2'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_fts_rows (
        memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
        fts_rowid INTEGER NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS suppressions (source_key TEXT PRIMARY KEY, deleted_at TEXT NOT NULL)
    """,
    SCOPED_SUPPRESSIONS,
    VECTORS,
    """
    CREATE TABLE IF NOT EXISTS clients (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
        scopes TEXT NOT NULL, projects TEXT, expires_at REAL NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, principal TEXT NOT NULL,
            client_event_id TEXT NOT NULL, kind TEXT NOT NULL, evidence TEXT NOT NULL,
            occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL, action TEXT NOT NULL,
            target TEXT NOT NULL, details TEXT NOT NULL, intent_event_id TEXT,
            payload_hash TEXT NOT NULL, prev_hash TEXT NOT NULL, event_hash TEXT NOT NULL,
            UNIQUE(principal, project, client_event_id)
        )
    """,
    """
    CREATE INDEX IF NOT EXISTS audit_events_project_id ON audit_events(project, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS audit_events_intent
        ON audit_events(project, principal, intent_event_id)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS audit_events_no_update BEFORE UPDATE ON audit_events
           BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS audit_events_no_delete BEFORE DELETE ON audit_events
           BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS audit_events_chain BEFORE INSERT ON audit_events
           BEGIN SELECT CASE WHEN NEW.prev_hash != COALESCE(
           (SELECT event_hash FROM audit_events ORDER BY id DESC LIMIT 1), '')
           THEN RAISE(ABORT, 'audit hash chain mismatch') END; END
    """,
    """
    CREATE TABLE IF NOT EXISTS cortex_operations (
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
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS cortex_operations_project_state
        ON cortex_operations(local_project, state)
    """,
    """
    CREATE TABLE IF NOT EXISTS semantic_chunks (
        memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL, model TEXT NOT NULL, chunk_index INTEGER NOT NULL,
        field TEXT NOT NULL CHECK(field IN ('title','content')),
        start_char INTEGER NOT NULL, end_char INTEGER NOT NULL,
        dimension INTEGER NOT NULL, vector BLOB NOT NULL,
        PRIMARY KEY(memory_id, model, chunk_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS semantic_state (
        memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
        model TEXT NOT NULL, revision INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('indexed','empty','failed')),
        truncated INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(memory_id, model)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_clients (
        id TEXT PRIMARY KEY, metadata TEXT NOT NULL, created REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_requests (
        id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES oauth_clients(id) ON DELETE CASCADE,
        params TEXT NOT NULL, status TEXT NOT NULL, projects TEXT, expires REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_codes (
        hash TEXT PRIMARY KEY,
        client_id TEXT NOT NULL REFERENCES oauth_clients(id) ON DELETE CASCADE,
        params TEXT NOT NULL, projects TEXT, expires REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_grants (
        id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES oauth_clients(id) ON DELETE CASCADE,
        scopes TEXT NOT NULL, projects TEXT, resource TEXT NOT NULL, expires REAL NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_tokens (
        hash TEXT PRIMARY KEY, kind TEXT NOT NULL,
        grant_id TEXT NOT NULL REFERENCES oauth_grants(id) ON DELETE CASCADE,
        scopes TEXT NOT NULL, expires REAL NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS oauth_tokens_grant ON oauth_tokens(grant_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_request_provenance (
        request_id TEXT PRIMARY KEY REFERENCES oauth_requests(id) ON DELETE CASCADE,
        request_created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_code_provenance (
        code_hash TEXT PRIMARY KEY REFERENCES oauth_codes(hash) ON DELETE CASCADE,
        request_id TEXT NOT NULL, request_created_at REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_grant_provenance (
        grant_id TEXT PRIMARY KEY REFERENCES oauth_grants(id) ON DELETE CASCADE,
        request_id TEXT, request_created_at REAL, created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_request_approvals (
        request_id TEXT PRIMARY KEY REFERENCES oauth_requests(id) ON DELETE CASCADE,
        scopes TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_client_revocations (
        client_id TEXT PRIMARY KEY REFERENCES oauth_clients(id) ON DELETE CASCADE,
        revoked_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_config_commits (id TEXT PRIMARY KEY)
    """,
)


def create_schema(db: sqlite3.Connection) -> None:
    """Install the current schema inside the caller's transaction.

    Migration validates the historical store before calling this. IF NOT EXISTS
    preserves existing tables, rows, grants, and audit history during v2 upgrades.
    """
    for statement in STATEMENTS:
        db.execute(statement)
    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    db.execute(f"PRAGMA application_id={APPLICATION_ID}")


def _objects(db: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """Compare declared schema objects; SQLite-owned FTS shadow tables are derived."""
    return {
        name: (kind, re.sub(r"\s+", " ", sql.strip()))
        for name, kind, sql in db.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
        )
        if not name.startswith("sqlite_")
        and name
        not in {
            "memory_fts_data",
            "memory_fts_idx",
            "memory_fts_content",
            "memory_fts_docsize",
            "memory_fts_config",
        }
    }


@lru_cache(maxsize=2)
def _reference_objects(legacy_v1: bool) -> dict[str, tuple[str, str]]:
    with closing(sqlite3.connect(":memory:")) as reference:
        for statement in STATEMENTS:
            if legacy_v1 and statement == MEMORIES:
                statement = statement.replace(
                    "identity_key TEXT NOT NULL", "source_key TEXT UNIQUE NOT NULL"
                )
                statement = statement.replace("current_revision", "revision")
                statement = statement.replace(",\n    UNIQUE(project, identity_key)", "")
            elif legacy_v1 and statement == REVISIONS:
                statement = statement.replace(" writer_principal TEXT NOT NULL,", "")
            reference.execute(statement)
        return _objects(reference)


def validate_schema(db: sqlite3.Connection, version: int) -> None:
    """Accept only known historical layouts, before mutating a real database."""
    actual_version = db.execute("PRAGMA user_version").fetchone()[0]
    application = db.execute("PRAGMA application_id").fetchone()[0]
    allowed_ids = {APPLICATION_ID} if version == SCHEMA_VERSION else {0, APPLICATION_ID}
    if (
        version not in (1, 2, SCHEMA_VERSION)
        or actual_version != version
        or application not in allowed_ids
    ):
        raise IntegrityError("Not a supported Dots Brain database; no migration was applied.")
    actual, reference = _objects(db), _reference_objects(version == 1)
    core = {"memories", "revisions", "memory_fts", "suppressions", "vectors", "clients"}
    oauth_base = {
        "oauth_clients",
        "oauth_requests",
        "oauth_codes",
        "oauth_grants",
        "oauth_tokens",
        "oauth_tokens_grant",
    }
    required = (
        core
        if version == 1
        else (
            core
            | {
                "scoped_suppressions",
                "audit_events",
                "audit_events_project_id",
                "audit_events_intent",
                "audit_events_no_update",
                "audit_events_no_delete",
                "audit_events_chain",
                "cortex_operations",
                "cortex_operations_project_state",
                "semantic_chunks",
            }
        )
    )
    if oauth_base.intersection(actual):
        required |= oauth_base
    if version == SCHEMA_VERSION:
        required = set(reference)
    for name in sorted(required | actual.keys()):
        if name not in actual or actual[name] != reference.get(name):
            raise IntegrityError(f"Unrecognized {name} schema; no migration was applied.")
