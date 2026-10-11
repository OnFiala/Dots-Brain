"""Real v3 format upgrade and exact-ID deletion with neutral synthetic data."""

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from dots_brain.activity import AuditLog
from dots_brain.errors import BrainError, IntegrityError, MigrationRequiredError
from dots_brain.migrations import migrate_store
from dots_brain.store import Store

FIXTURE = Path(__file__).parent / "fixtures/historical/v3-f34b90e"


def canonical(path):
    with closing(sqlite3.connect(path)) as db:
        names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {
            name: sorted(db.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr)
            for name in names
            if not name.startswith("memory_fts")
        }


@pytest.fixture
def historical(tmp_path):
    manifest = json.loads((FIXTURE / "manifest.json").read_text())
    assert manifest["schema_version"] == 3
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest() == digest
    target = tmp_path / "historical"
    shutil.copytree(FIXTURE, target)
    return Store(target)


def test_v3_requires_explicit_upgrade_and_preserves_all_canonical_values(historical, tmp_path):
    store = historical
    before_bytes = store.path.read_bytes()
    before = canonical(store.path)
    assert len(before["audit_events"]) == 2
    with pytest.raises(MigrationRequiredError):
        store.status()
    assert migrate_store(store, apply=False) == {
        "state": "migration_required",
        "from_version": 3,
        "to_version": 4,
        "writes": False,
    }
    assert store.path.read_bytes() == before_bytes
    backup = tmp_path / "before.sqlite3"
    assert (
        migrate_store(store, apply=True, stop_guard=lambda: None, backup_path=backup)["state"]
        == "migrated"
    )
    assert canonical(store.path) == before
    assert canonical(backup) == before
    assert AuditLog(store).verify() == {"events": 2}
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        memory_id = db.execute("SELECT id FROM memories").fetchone()[0]
        assert (
            db.execute(
                "SELECT memory_id FROM memory_fts WHERE memory_fts MATCH ?",
                ('memory_id : "' + memory_id + '"',),
            ).fetchone()[0]
            == memory_id
        )
    assert store.search("second")[0]["id"] == memory_id
    assert store.search(memory_id) == []


def test_v3_upgrade_failure_after_old_fts_drop_rolls_back_bytes(historical, monkeypatch):
    import dots_brain.migrations as migrations

    original = historical.path.read_bytes()
    before = canonical(historical.path)
    create = migrations.create_schema

    def fail(db):
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_fts'").fetchone() is None
        raise sqlite3.OperationalError("synthetic post-drop interruption")

    monkeypatch.setattr(migrations, "create_schema", fail)
    with pytest.raises(BrainError, match="rolled back and validated.*run dots-brain"):
        migrate_store(historical, apply=True, stop_guard=lambda: None)
    assert historical.path.read_bytes() == original
    assert canonical(historical.path) == before
    assert not list(historical.directory.glob("*.v3-backup-*"))
    monkeypatch.setattr(migrations, "create_schema", create)
    assert migrate_store(historical, apply=True, stop_guard=lambda: None)["state"] == "migrated"


@pytest.mark.parametrize("damage", ["application_id", "missing_table"])
def test_frozen_v3_still_requires_application_id_and_complete_schema(historical, damage):
    with closing(sqlite3.connect(historical.path)) as db, db:
        if damage == "application_id":
            db.execute("PRAGMA application_id=0")
        else:
            db.execute("DROP TABLE oauth_request_approvals")
    original = historical.path.read_bytes()
    for apply in (False, True):
        with pytest.raises(IntegrityError):
            migrate_store(historical, apply=apply, stop_guard=lambda: None)
        assert historical.path.read_bytes() == original
    assert not list(historical.directory.glob("*-backup-*"))


@pytest.mark.parametrize(
    "target,other",
    [
        ("---", "--"),
        ('a"b', "ab"),
        ("Řada", "rada"),
        ("ＡＢＣ", "ABC"),
        (b"binary-id", "binary-id"),
        ("ABCDEF01-ABCD-ABCD-ABCD-ABCDEF012345", "abcdef01-abcd-abcd-abcd-abcdef012345"),
    ],
)
def test_fulltext_deletion_uses_exact_identity_for_fallbacks_and_token_collisions(
    tmp_path, target, other
):
    from dots_brain.store import delete_fulltext

    store = Store(tmp_path / "memory")
    store.initialize()
    with store.connection(write=True) as db:
        db.executemany(
            "INSERT INTO memory_fts(memory_id,title,content) VALUES (?,'',?)",
            [(target, "discard one"), (target, "discard two"), (other, "preserve")],
        )
        delete_fulltext(db, target)
        assert [tuple(row) for row in db.execute("SELECT memory_id,content FROM memory_fts")] == [
            (other, "preserve")
        ]
