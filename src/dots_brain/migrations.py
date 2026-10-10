"""Explicit, offline SQLite migrations.

Callers must stop every server and stdio client before applying a migration.  The
CLI supplies that runtime-specific check through ``stop_guard``; this module does
not guess whether a process using the database is safe to interrupt.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from .database_checks import validate_snapshot
from .database_io import open_readonly, snapshot
from .errors import BusyError, InputError
from .store import Store, ensure_fts_row_mapping, extension_statements, secure_fts

V1 = 1
V2 = 2
HISTORICAL_WRITER = "unknown:v1-migration"

MEMORIES_V2 = """
CREATE TABLE memories (
    id TEXT PRIMARY KEY, identity_key TEXT NOT NULL,
    source TEXT NOT NULL, account TEXT NOT NULL, event_id TEXT NOT NULL,
    project TEXT NOT NULL, title TEXT NOT NULL, current_revision INTEGER NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(project, identity_key)
)
"""
REVISIONS_V2 = """
CREATE TABLE revisions (
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL, content TEXT NOT NULL, digest TEXT NOT NULL,
    source_uri TEXT, title TEXT NOT NULL, writer_principal TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(memory_id, revision)
)
"""
VECTORS_V2 = """
CREATE TABLE vectors (
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL, model TEXT NOT NULL,
    dimension INTEGER NOT NULL, vector BLOB NOT NULL,
    PRIMARY KEY(memory_id, model)
)
"""
SCOPED_SUPPRESSIONS_V2 = """
CREATE TABLE scoped_suppressions (
    project TEXT NOT NULL, identity_key TEXT NOT NULL,
    deleted_revision INTEGER NOT NULL, deleted_by TEXT NOT NULL, deleted_at TEXT NOT NULL,
    PRIMARY KEY(project, identity_key)
)
"""


def _version(path: Path) -> int:
    if not path.exists():
        raise InputError("Memory is not initialized. Run dots-brain setup first.")
    if path.is_symlink():
        raise InputError("The database must not be a symbolic link.")
    wal = path.with_name(path.name + "-wal")
    if wal.exists() and wal.stat().st_size > 0:
        raise BusyError("Stop every writer and checkpoint its WAL before planning migration.")
    with closing(open_readonly(path, immutable=True)) as db:
        return db.execute("PRAGMA user_version").fetchone()[0]


def _default_backup(path: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return path.with_name(f"{path.name}.v1-backup-{stamp}")


def _backup(path: Path, destination: Path) -> Path:
    with closing(open_readonly(path)) as source:
        validate_snapshot(source, V1)
        return snapshot(source, destination, V1)


def _migrate(db: sqlite3.Connection) -> None:
    """Transform the open v1 connection inside one transaction.

    Kept separate so failure-path tests can inject an exception before commit.
    """
    before = validate_snapshot(db, V1)
    db.execute("ALTER TABLE memories RENAME TO memories_v1")
    db.execute("ALTER TABLE revisions RENAME TO revisions_v1")
    db.execute("ALTER TABLE vectors RENAME TO vectors_v1")
    db.execute(MEMORIES_V2)
    db.execute(REVISIONS_V2)
    db.execute(VECTORS_V2)
    db.execute(SCOPED_SUPPRESSIONS_V2)
    db.execute(
        "INSERT INTO memories "
        "SELECT id,source_key,source,account,event_id,project,title,revision,created_at,updated_at "
        "FROM memories_v1"
    )
    db.execute(
        "INSERT INTO revisions "
        "SELECT memory_id,revision,content,digest,source_uri,title,?,created_at FROM revisions_v1",
        (HISTORICAL_WRITER,),
    )
    db.execute(
        "INSERT INTO vectors SELECT memory_id,revision,model,dimension,vector FROM vectors_v1"
    )
    db.execute("DELETE FROM memory_fts")
    db.execute(
        "INSERT INTO memory_fts(memory_id,title,content) "
        "SELECT m.id,r.title,r.content FROM memories m JOIN revisions r "
        "ON r.memory_id=m.id AND r.revision=m.current_revision"
    )
    db.execute("DROP TABLE vectors_v1")
    db.execute("DROP TABLE revisions_v1")
    db.execute("DROP TABLE memories_v1")
    for statement in extension_statements():
        db.execute(statement)
    db.execute(f"PRAGMA user_version={V2}")
    secure_fts(db)
    ensure_fts_row_mapping(db)
    if validate_snapshot(db, V2) != before:
        raise InputError("Migration changed record counts; migration was rolled back.")


def migrate_v1_to_v2(
    store: Store,
    *,
    apply: bool,
    backup_path: Path | None = None,
    stop_guard: Callable[[], None] | None = None,
) -> dict:
    """Plan or apply the schema v1 to v2 migration.

    ``apply=False`` only reads the version.  Applying requires a caller-provided
    stopped-runtime guard, creates and verifies a private SQLite backup, then
    performs this migration atomically.  Repeating after success is a no-op.
    """
    version = _version(store.path)
    if version == V2:
        return {"state": "already_current", "from_version": V2, "to_version": V2, "writes": False}
    if version != V1:
        raise InputError("Only a schema v1 store can be migrated to schema v2.")
    if not apply:
        return {
            "state": "migration_required",
            "from_version": V1,
            "to_version": V2,
            "writes": False,
        }
    if stop_guard is None:
        raise InputError("Migration requires a stopped-runtime guard from the caller.")
    stop_guard()
    backup = _backup(store.path, backup_path or _default_backup(store.path))
    db = sqlite3.connect(store.path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("PRAGMA secure_delete=ON")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        _migrate(db)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
    return {
        "state": "migrated",
        "from_version": V1,
        "to_version": V2,
        "writes": True,
        "backup": str(backup),
    }
