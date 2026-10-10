import json
import sqlite3
import subprocess
import sys
from contextlib import closing

import pytest

from dots_brain.errors import IntegrityError, MigrationRequiredError
from dots_brain.migrations import HISTORICAL_WRITER, migrate_v1_to_v2
from dots_brain.store import APPLICATION_ID, Store, source_key

V1_SCHEMA = """
CREATE TABLE memories (
    id TEXT PRIMARY KEY, source_key TEXT UNIQUE NOT NULL,
    source TEXT NOT NULL, account TEXT NOT NULL, event_id TEXT NOT NULL,
    project TEXT NOT NULL, title TEXT NOT NULL, revision INTEGER NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE revisions (
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL, content TEXT NOT NULL, digest TEXT NOT NULL,
    source_uri TEXT, title TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(memory_id, revision)
);
CREATE VIRTUAL TABLE memory_fts USING fts5(
    memory_id UNINDEXED, title, content, tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE suppressions (source_key TEXT PRIMARY KEY, deleted_at TEXT NOT NULL);
CREATE TABLE vectors (
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL, model TEXT NOT NULL,
    dimension INTEGER NOT NULL, vector BLOB NOT NULL,
    PRIMARY KEY(memory_id, model)
);
CREATE TABLE clients (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
    scopes TEXT NOT NULL, projects TEXT, expires_at REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE oauth_grants (
    id TEXT PRIMARY KEY, client_id TEXT NOT NULL, scopes TEXT NOT NULL, projects TEXT,
    resource TEXT NOT NULL, expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
);
PRAGMA user_version=1;
"""


def legacy_store(tmp_path):
    store = Store(tmp_path / "legacy")
    store.directory.mkdir(mode=0o700)
    key = source_key("source", "account", "event")
    with closing(sqlite3.connect(store.path)) as db, db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        db.executescript(V1_SCHEMA)
        db.execute(
            "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("memory-1", key, "source", "account", "event", "alpha", "Title", 2, "old", "new"),
        )
        db.executemany(
            "INSERT INTO revisions VALUES (?,?,?,?,?,?,?)",
            [
                ("memory-1", 1, "first content", "one", None, "Title", "old"),
                ("memory-1", 2, "second content", "two", None, "Title", "new"),
            ],
        )
        db.execute("INSERT INTO memory_fts VALUES (?,?,?)", ("memory-1", "Title", "second content"))
        db.execute("INSERT INTO suppressions VALUES (?,?)", ("legacy-key", "old"))
        db.execute("INSERT INTO vectors VALUES (?,?,?,?,?)", ("memory-1", 2, "model", 1, b"x"))
        db.execute(
            "INSERT INTO clients VALUES (?,?,?,?,?,?,?)",
            ("client", "name", "hash", "[]", None, 9999999999, 0),
        )
        db.execute(
            "INSERT INTO oauth_grants VALUES (?,?,?,?,?,?,?)",
            ("grant", "client", "[]", None, "resource", 9999999999, 0),
        )
    return store


def test_documented_cli_migration_accepts_backup_outside_data_directory(tmp_path):
    store = legacy_store(tmp_path)
    backup = tmp_path / "outside-legacy.sqlite3"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "dots_brain.cli",
            "--data-dir",
            str(store.directory),
            "migrate",
            "--apply",
            "--writers-stopped",
            "--backup",
            str(backup),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["state"] == "migrated"
    assert store.get("memory-1")["revision"] == 2
    with closing(sqlite3.connect(backup.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1


def test_old_store_requires_explicit_migration_and_dry_run_has_no_writes(tmp_path):
    store = legacy_store(tmp_path)
    before = store.path.read_bytes()
    with pytest.raises(MigrationRequiredError, match="explicit offline migration"):
        store.initialize()
    assert migrate_v1_to_v2(store, apply=False) == {
        "state": "migration_required",
        "from_version": 1,
        "to_version": 2,
        "writes": False,
    }
    assert store.path.read_bytes() == before
    assert not list(store.directory.glob("*.v1-backup-*"))


def test_migration_preserves_data_and_rejects_old_sql_contract(tmp_path):
    store = legacy_store(tmp_path)
    backup = store.directory / "pre-v2.sqlite3"
    result = migrate_v1_to_v2(store, apply=True, backup_path=backup, stop_guard=lambda: None)
    assert result["state"] == "migrated" and result["backup"] == str(backup)
    assert backup.stat().st_mode & 0o777 == 0o600
    store.initialize()
    assert store.get("memory-1", revision=1)["content"] == "first content"
    assert store.search("second")[0]["revision"] == 2
    with store.connection() as db:
        assert db.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        revision = db.execute("SELECT writer_principal FROM revisions WHERE revision=1").fetchone()[
            0
        ]
        assert revision == HISTORICAL_WRITER
        assert db.execute("SELECT token_hash FROM clients").fetchone()[0] == "hash"
        assert db.execute("SELECT id FROM oauth_grants").fetchone()[0] == "grant"
        assert db.execute("SELECT source_key FROM suppressions").fetchone()[0] == "legacy-key"
        columns = {row[1] for row in db.execute("PRAGMA table_info(memories)")}
    assert {"identity_key", "current_revision"} <= columns
    assert "source_key" not in columns and "revision" not in columns


def test_migration_rollback_leaves_v1_untouched_and_repeat_is_a_noop(tmp_path, monkeypatch):
    store = legacy_store(tmp_path)
    import dots_brain.migrations as migrations

    transform = migrations._migrate

    def fail_after_transform(db):
        transform(db)
        db.execute("INVALID SQL AFTER REAL DDL")

    monkeypatch.setattr(migrations, "_migrate", fail_after_transform)
    with pytest.raises(sqlite3.OperationalError):
        migrate_v1_to_v2(store, apply=True, stop_guard=lambda: None)
    with closing(sqlite3.connect(store.path)) as db, db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT revision FROM memories").fetchone()[0] == 2
        assert db.execute("SELECT content FROM memory_fts").fetchone()[0] == "second content"
        assert db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 2
        assert db.execute("SELECT id FROM oauth_grants").fetchone()[0] == "grant"
    backup = next(store.directory.glob("*.v1-backup-*"))
    with closing(sqlite3.connect(backup.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 2
        assert db.execute("SELECT source_key FROM suppressions").fetchone()[0] == "legacy-key"
    monkeypatch.undo()
    assert migrate_v1_to_v2(store, apply=True, stop_guard=lambda: None)["state"] == "migrated"
    backups = list(store.directory.glob("*.v1-backup-*"))
    repeated = migrate_v1_to_v2(store, apply=True, stop_guard=lambda: None)
    assert repeated["state"] == "already_current"
    assert list(store.directory.glob("*.v1-backup-*")) == backups


def test_migration_rejects_missing_current_revision(tmp_path):
    store = legacy_store(tmp_path)
    with closing(sqlite3.connect(store.path)) as db, db:
        db.execute("UPDATE memories SET revision=3")
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with pytest.raises(IntegrityError, match="no current revision"):
        migrate_v1_to_v2(store, apply=True, stop_guard=lambda: None)
    with closing(sqlite3.connect(store.path)) as db, db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
