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
from .errors import InputError, IntegrityError
from .schema import MEMORIES, REVISIONS, SCHEMA_VERSION, VECTORS, create_schema, validate_schema
from .store import Store, rebuild_fts, require_sqlite

HISTORICAL_WRITER = "unknown:v1-migration"


def _version(path: Path) -> int:
    if not path.exists():
        raise InputError("Memory is not initialized. Run dots-brain setup first.")
    if path.is_symlink():
        raise InputError("The database must not be a symbolic link.")
    with closing(open_readonly(path)) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        validate_schema(db, version)
        return version


def _default_backup(path: Path, version: int) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return path.with_name(f"{path.name}.v{version}-backup-{stamp}")


def _backup(path: Path, destination: Path, version: int) -> Path:
    with closing(open_readonly(path)) as source:
        validate_snapshot(source, version)
        return snapshot(source, destination, version)


def _migrate_v1(db: sqlite3.Connection) -> None:
    for table in ("memories", "revisions", "vectors"):
        db.execute(f"ALTER TABLE {table} RENAME TO {table}_v1")
    for statement in (MEMORIES, REVISIONS, VECTORS):
        db.execute(statement)
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
    db.execute("INSERT INTO vectors SELECT * FROM vectors_v1")
    for table in ("vectors", "revisions", "memories"):
        db.execute(f"DROP TABLE {table}_v1")


def _migrate(db: sqlite3.Connection) -> None:
    """Upgrade canonical records and rebuild derived FTS in one transaction."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    validate_schema(db, version)
    before = validate_snapshot(db, version)
    if version == 1:
        _migrate_v1(db)
    create_schema(db)
    rebuild_fts(db)
    db.execute(
        "INSERT OR IGNORE INTO semantic_state(memory_id,model,revision,state,truncated) "
        "SELECT c.memory_id,c.model,c.revision,'indexed',0 FROM semantic_chunks c "
        "JOIN memories m ON m.id=c.memory_id AND m.current_revision=c.revision "
        "GROUP BY c.memory_id,c.model,c.revision"
    )
    validate_schema(db, SCHEMA_VERSION)
    after = validate_snapshot(db, SCHEMA_VERSION)
    if any(after.get(table) != count for table, count in before.items()):
        raise IntegrityError("Migration changed canonical record counts; it was rolled back.")


def migrate_store(
    store: Store,
    *,
    apply: bool,
    backup_path: Path | None = None,
    stop_guard: Callable[[], None] | None = None,
) -> dict:
    """Plan without database writes, or back up and atomically upgrade a stopped store."""
    require_sqlite()
    version = _version(store.path)
    result = {"from_version": version, "to_version": SCHEMA_VERSION, "writes": False}
    if version == SCHEMA_VERSION:
        return {"state": "already_current", **result}
    if not apply:
        return {"state": "migration_required", **result}
    if stop_guard is None:
        raise InputError("Migration requires a stopped-runtime guard from the caller.")
    stop_guard()
    # Reserve SQLite's writer lock before the snapshot. Historical writers do
    # not know our lease files, but none can commit between this backup and DDL.
    with closing(sqlite3.connect(store.path, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("PRAGMA secure_delete=ON")
        db.execute("PRAGMA synchronous=FULL")
        with db:
            db.execute("BEGIN IMMEDIATE")
            validate_schema(db, version)
            backup = _backup(
                store.path, backup_path or _default_backup(store.path, version), version
            )
            _migrate(db)
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return {"state": "migrated", **result, "writes": True, "backup": str(backup)}
