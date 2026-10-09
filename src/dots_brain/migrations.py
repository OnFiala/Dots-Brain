"""Explicit, offline SQLite migrations.

Callers must stop every server and stdio client before applying a migration.  The
CLI supplies that runtime-specific check through ``stop_guard``; this module does
not guess whether a process using the database is safe to interrupt.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .database_checks import validate_snapshot
from .errors import InputError
from .local import sync_file_and_parent
from .store import Store, extension_statements

# Used by failure injection tests after the real schema transformation.
SCHEMA_EXTENSION_SQL: tuple[str, ...] = ()

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
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        return db.execute("PRAGMA user_version").fetchone()[0]


def _default_backup(path: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return path.with_name(f"{path.name}.v1-backup-{stamp}")


def _backup(path: Path, destination: Path) -> Path:
    destination = destination.expanduser().absolute()
    if destination.parent != path.parent:
        raise InputError("The migration backup must be a private file beside the database.")
    if destination.is_symlink():
        raise InputError("The migration backup must not be a symbolic link.")
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(descriptor)
    destination.chmod(0o600)
    try:
        with sqlite3.connect(path) as source, sqlite3.connect(destination) as copied:
            validate_snapshot(source, V1)
            source.backup(copied)
            validate_snapshot(copied, V1)
        if destination.stat().st_mode & 0o777 != 0o600:
            raise InputError("The migration backup is not private.")
        sync_file_and_parent(destination)
    except BaseException:
        # Keep a failed backup for inspection rather than silently deleting evidence.
        raise
    return destination


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
    for statement in (*extension_statements(), *SCHEMA_EXTENSION_SQL):
        db.execute(statement)
    db.execute(f"PRAGMA user_version={V2}")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise InputError("Foreign-key validation failed; migration was rolled back.")
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
