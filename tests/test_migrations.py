import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from dots_brain.errors import IntegrityError, MigrationRequiredError
from dots_brain.migrations import HISTORICAL_WRITER, migrate_store
from dots_brain.store import APPLICATION_ID, Store

FIXTURE = Path(__file__).parent / "fixtures/historical/v1-263c386"
LEGACY_ID = json.loads((FIXTURE / "manifest.json").read_text())["memory_id"]


def legacy_store(tmp_path):
    destination = tmp_path / "legacy"
    shutil.copytree(FIXTURE, destination)
    store = Store(destination)
    with closing(sqlite3.connect(store.path)) as db, db:
        db.execute("INSERT INTO suppressions VALUES (?,?)", ("legacy-key", "old"))
        db.execute("INSERT INTO vectors VALUES (?,?,?,?,?)", (LEGACY_ID, 2, "model", 1, b"x"))
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
    assert store.get(LEGACY_ID)["revision"] == 2
    with closing(sqlite3.connect(backup.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1


def test_old_store_requires_explicit_migration_and_dry_run_has_no_writes(tmp_path):
    store = legacy_store(tmp_path)
    before = store.path.read_bytes()
    with pytest.raises(MigrationRequiredError, match="explicit offline migration"):
        store.initialize()
    assert migrate_store(store, apply=False) == {
        "state": "migration_required",
        "from_version": 1,
        "to_version": 3,
        "writes": False,
    }
    assert store.path.read_bytes() == before
    assert not list(store.directory.glob("*.v1-backup-*"))


def test_migration_preserves_data_and_rejects_old_sql_contract(tmp_path):
    store = legacy_store(tmp_path)
    backup = store.directory / "pre-v2.sqlite3"
    result = migrate_store(store, apply=True, backup_path=backup, stop_guard=lambda: None)
    assert result["state"] == "migrated" and result["backup"] == str(backup)
    assert backup.stat().st_mode & 0o777 == 0o600
    store.initialize()
    assert store.get(LEGACY_ID, revision=1)["content"] == "Synthetic first revision"
    assert store.search("second")[0]["revision"] == 2
    with store.connection() as db:
        assert db.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        revision = db.execute("SELECT writer_principal FROM revisions WHERE revision=1").fetchone()[
            0
        ]
        assert revision == HISTORICAL_WRITER
        assert (
            db.execute("SELECT token_hash FROM clients").fetchone()[0]
            == hashlib.sha256(b"public-synthetic-upgrade-probe").hexdigest()
        )
        assert db.execute("SELECT id FROM oauth_grants").fetchone()[0] == "fixture-grant"
        assert (
            db.execute(
                "SELECT source_key FROM suppressions WHERE source_key='legacy-key'"
            ).fetchone()[0]
            == "legacy-key"
        )
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
        migrate_store(store, apply=True, stop_guard=lambda: None)
    with closing(sqlite3.connect(store.path)) as db, db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT revision FROM memories").fetchone()[0] == 2
        assert (
            db.execute("SELECT content FROM memory_fts").fetchone()[0]
            == "Synthetic second revision"
        )
        assert db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 2
        assert db.execute("SELECT id FROM oauth_grants").fetchone()[0] == "fixture-grant"
    backup = next(store.directory.glob("*.v1-backup-*"))
    with closing(sqlite3.connect(backup.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 2
        assert (
            db.execute(
                "SELECT source_key FROM suppressions WHERE source_key='legacy-key'"
            ).fetchone()[0]
            == "legacy-key"
        )
    monkeypatch.undo()
    assert migrate_store(store, apply=True, stop_guard=lambda: None)["state"] == "migrated"
    backups = list(store.directory.glob("*.v1-backup-*"))
    repeated = migrate_store(store, apply=True, stop_guard=lambda: None)
    assert repeated["state"] == "already_current"
    assert list(store.directory.glob("*.v1-backup-*")) == backups


def test_migration_rejects_missing_current_revision(tmp_path):
    store = legacy_store(tmp_path)
    with closing(sqlite3.connect(store.path)) as db, db:
        db.execute("UPDATE memories SET revision=3")
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with pytest.raises(IntegrityError, match="no current revision"):
        migrate_store(store, apply=True, stop_guard=lambda: None)
    with closing(sqlite3.connect(store.path)) as db, db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1


def test_checkpoint_failure_does_not_hide_a_committed_migration(tmp_path, monkeypatch):
    store = legacy_store(tmp_path)
    connect = sqlite3.connect

    class DeferredCheckpoint(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == "PRAGMA wal_checkpoint(TRUNCATE)":
                raise sqlite3.OperationalError("synthetic checkpoint failure")
            return super().execute(sql, *args, **kwargs)

    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *args, **kwargs: connect(*args, **kwargs, factory=DeferredCheckpoint),
    )
    result = migrate_store(store, apply=True, stop_guard=lambda: None)
    assert result["state"] == "migrated"
    assert result["wal_checkpoint"] == "deferred"
    assert store.get(LEGACY_ID)["revision"] == 2
    assert migrate_store(store, apply=False)["state"] == "already_current"
