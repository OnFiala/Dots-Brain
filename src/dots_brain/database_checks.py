"""Recovery checks for both SQLite structure and the application's current records."""

import sqlite3

from .errors import InputError


def validate_snapshot(db: sqlite3.Connection, version: int) -> dict[str, int]:
    if db.execute("PRAGMA user_version").fetchone()[0] != version:
        raise InputError("Database schema does not match the requested recovery operation.")
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise InputError("SQLite integrity validation failed.")
    if db.execute("PRAGMA foreign_key_check").fetchone():
        raise InputError("Database foreign-key validation failed.")
    column = "revision" if version == 1 else "current_revision"
    if db.execute(
        f"SELECT 1 FROM memories m LEFT JOIN revisions r "
        f"ON r.memory_id=m.id AND r.revision=m.{column} "
        "WHERE r.memory_id IS NULL LIMIT 1"
    ).fetchone():
        raise InputError("A memory has no current revision; recovery was not applied.")
    if db.execute(
        f"SELECT 1 FROM memories m WHERE m.{column} != "
        "(SELECT COUNT(*) FROM revisions r WHERE r.memory_id=m.id) LIMIT 1"
    ).fetchone():
        raise InputError("Memory revision counts are inconsistent.")
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    tracked = {"memories", "revisions", "vectors", "memory_fts", "suppressions", "clients"}
    tracked.update(table for table in tables if table.startswith("oauth_"))
    return {
        table: db.execute('SELECT COUNT(*) FROM "' + table.replace('"', '""') + '"').fetchone()[0]
        for table in tracked
    }
