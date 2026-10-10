"""Recovery checks for both SQLite structure and the application's current records."""

import sqlite3

from .errors import IntegrityError
from .schema import SCHEMA_VERSION, validate_schema


def validate_snapshot(db: sqlite3.Connection, version: int) -> dict[str, int]:
    if version == SCHEMA_VERSION:
        validate_schema(db, version)
    if db.execute("PRAGMA user_version").fetchone()[0] != version:
        raise IntegrityError("Database schema does not match the requested recovery operation.")
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise IntegrityError("SQLite integrity validation failed.")
    if db.execute("PRAGMA foreign_key_check").fetchone():
        raise IntegrityError("Database foreign-key validation failed.")
    if version >= 2:
        from .activity import AuditLog

        AuditLog.verify_connection(db)
    column = "revision" if version == 1 else "current_revision"
    if db.execute(
        f"SELECT 1 FROM memories m LEFT JOIN revisions r "
        f"ON r.memory_id=m.id AND r.revision=m.{column} "
        "WHERE r.memory_id IS NULL LIMIT 1"
    ).fetchone():
        raise IntegrityError("A memory has no current revision; recovery was not applied.")
    if db.execute(
        f"SELECT 1 FROM memories m WHERE m.{column} != "
        "(SELECT COUNT(*) FROM revisions r WHERE r.memory_id=m.id) LIMIT 1"
    ).fetchone():
        raise IntegrityError("Memory revision counts are inconsistent.")
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    # Derived indexes can be rebuilt and must not block migration of canonical records.
    tracked = tables & {
        "memories",
        "revisions",
        "vectors",
        "suppressions",
        "scoped_suppressions",
        "clients",
        "audit_events",
        "cortex_operations",
        "semantic_chunks",
    }
    tracked.update(table for table in tables if table.startswith("oauth_"))
    return {
        table: db.execute('SELECT COUNT(*) FROM "' + table.replace('"', '""') + '"').fetchone()[0]
        for table in tracked
    }
