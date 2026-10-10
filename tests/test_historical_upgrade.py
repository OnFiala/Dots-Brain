"""Upgrade real historical database bytes, using only synthetic fixture data."""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from dots_brain.auth import authenticate
from dots_brain.errors import IntegrityError, MigrationRequiredError, SuppressedError
from dots_brain.oauth import OAuthStore
from dots_brain.operations import migrate
from dots_brain.operator_cli import doctor
from dots_brain.removal import uninstall
from dots_brain.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "historical"


@pytest.fixture(params=["v1-263c386", "v2-c963700"])
def historical(request, tmp_path):
    fixture = FIXTURES / request.param
    manifest = json.loads((fixture / "manifest.json").read_text())
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((fixture / name).read_bytes()).hexdigest() == digest
    destination = tmp_path / "historical"
    shutil.copytree(fixture, destination)
    return Store(destination), manifest


def test_historical_upgrade_preserves_data_grants_and_deletions(historical, tmp_path):
    store, manifest = historical
    original = store.path.read_bytes()
    assert doctor(store)["healthy"] is False
    for action in (store.initialize, lambda: OAuthStore(store)):
        with pytest.raises(MigrationRequiredError):
            action()
    plan = migrate(store, apply=False, writers_stopped=False, backup=None)
    assert plan == {
        "state": "migration_required",
        "from_version": manifest["schema_version"],
        "to_version": 3,
        "writes": False,
    }
    assert store.path.read_bytes() == original
    backup = tmp_path / "before.sqlite3"
    result = migrate(store, apply=True, writers_stopped=True, backup=backup)
    assert result["state"] == "migrated"
    assert doctor(store)["healthy"] is True
    assert store.get(manifest["memory_id"], revision=1)["content"] == "Synthetic first revision"
    assert store.get(manifest["memory_id"])["revision"] == 2
    assert authenticate(store, "public-synthetic-upgrade-probe").principal.endswith(
        manifest["probe_id"]
    )
    oauth = OAuthStore(store)
    assert oauth.policy("public-synthetic-oauth-token").principal == "oauth-grant:fixture-grant"
    assert oauth.grants()["grants"][0]["request_id"] is None
    with pytest.raises(SuppressedError):
        store.remember(
            content="Synthetic retry",
            source="fixture",
            account="test",
            event_id="deleted",
            project="test",
        )
    saved = store.remember(
        content="Synthetic upgraded write",
        source="fixture",
        account="test",
        event_id="new",
        project="test",
    )
    assert store.search("upgraded")[0]["id"] == saved["id"]
    assert store.forget(saved["id"], expected_revision=1)["deleted"]
    assert not store.search("upgraded")
    assert (
        migrate(store, apply=True, writers_stopped=True, backup=None)["state"] == "already_current"
    )
    # The rollback artifact is still the historical format and includes its grants.
    with closing(sqlite3.connect(backup)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == manifest["schema_version"]
        assert db.execute("SELECT id FROM oauth_grants").fetchone()[0] == "fixture-grant"


def test_committed_historical_wal_is_included_in_plan_backup_and_upgrade(historical, tmp_path):
    store, manifest = historical
    script = """
import hashlib, os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute('PRAGMA journal_mode=WAL')
db.execute('PRAGMA wal_autocheckpoint=0')
content = 'Synthetic committed WAL revision'
db.execute('UPDATE revisions SET content=?,digest=? WHERE revision=2',
           (content, hashlib.sha256(content.encode()).hexdigest()))
db.commit()
os._exit(0)  # Simulate a stopped historical process without a close-time checkpoint.
"""
    subprocess.run([sys.executable, "-c", script, str(store.path)], check=True, timeout=10)
    wal = Path(str(store.path) + "-wal")
    assert wal.stat().st_size > 0
    before = store.path.read_bytes(), wal.read_bytes()
    assert migrate(store, apply=False, writers_stopped=False, backup=None)["writes"] is False
    assert (store.path.read_bytes(), wal.read_bytes()) == before
    backup = tmp_path / "wal-backup.sqlite3"
    migrate(store, apply=True, writers_stopped=True, backup=backup)
    assert store.get(manifest["memory_id"])["content"] == "Synthetic committed WAL revision"
    assert store.search("committed")[0]["id"] == manifest["memory_id"]
    with closing(sqlite3.connect(backup)) as db:
        assert db.execute("SELECT content FROM revisions WHERE revision=2").fetchone()[0] == (
            "Synthetic committed WAL revision"
        )


def test_foreign_v2_is_untouched_even_with_zero_application_id(tmp_path):
    store = Store(tmp_path)
    with closing(sqlite3.connect(store.path)) as db:
        db.executescript("CREATE TABLE foreign_data (value TEXT); PRAGMA user_version=2;")
    before = store.path.read_bytes()
    with pytest.raises(IntegrityError, match="Unrecognized"):
        migrate(store, apply=True, writers_stopped=True, backup=None)
    assert store.path.read_bytes() == before
    assert not list(tmp_path.glob("*-backup-*"))


def test_uninstall_disables_an_unmigrated_store_and_reports_revocation_gap(historical):
    store, _ = historical
    before = store.path.read_bytes()
    result = uninstall(store)
    assert result["state"] == "partial"
    assert result["service_disabled"] is True
    assert result["local_credentials_revoked"] is False
    assert {"state": "credential_revocation_failed"} in result["issues"]
    assert (store.directory / "disabled.json").exists()
    assert store.path.read_bytes() == before


def test_historical_setup_refuses_migrated_store(historical, tmp_path):
    store, manifest = historical
    migrate(store, apply=True, writers_stopped=True, backup=None)
    old_source = os.environ.get(f"BRAIN_TEST_OLD_V{manifest['schema_version']}_SRC")
    if not old_source:
        pytest.skip("Historical interpreter source is supplied by the Linux upgrade CI job")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from dots_brain.store import Store; import sys; Store(sys.argv[1]).initialize()",
            str(store.directory),
        ],
        env={**os.environ, "PYTHONPATH": old_source},
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert "Unsupported database version" in result.stderr


def test_offline_rollback_copy_opens_with_original_binary(historical, tmp_path):
    store, manifest = historical
    old_source = os.environ.get(f"BRAIN_TEST_OLD_V{manifest['schema_version']}_SRC")
    if not old_source:
        pytest.skip("Historical interpreter source is supplied by the Linux upgrade CI job")
    backup = tmp_path / "before.sqlite3"
    migrate(store, apply=True, writers_stopped=True, backup=backup)
    rollback = tmp_path / "rollback"
    rollback.mkdir()
    shutil.copyfile(backup, rollback / "brain.sqlite3")
    shutil.copyfile(store.directory / "oauth.json", rollback / "oauth.json")
    script = """
import sys
from dots_brain.store import Store
from dots_brain.auth import authenticate
from dots_brain.oauth import OAuthStore
from dots_brain.errors import SuppressedError
store = Store(sys.argv[1])
store.initialize()
assert store.get(sys.argv[2])["revision"] == 2
assert authenticate(store, "public-synthetic-upgrade-probe") is not None
assert OAuthStore(store).policy("public-synthetic-oauth-token") is not None
try:
    store.remember(content="Synthetic retry", source="fixture", account="test",
                   event_id="deleted", project="test")
except SuppressedError:
    pass
else:
    raise AssertionError("Rollback lost its deletion barrier")
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(rollback),
            manifest["memory_id"],
        ],
        env={**os.environ, "PYTHONPATH": old_source},
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_backup_holds_sqlite_writer_lock_until_migration_commits(historical, tmp_path, monkeypatch):
    import dots_brain.migrations as migrations

    store, manifest = historical
    original_backup = migrations._backup
    attempts = []

    def competing_writer(path, destination, version):
        with closing(sqlite3.connect(path, timeout=0)) as other:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("UPDATE memories SET title='Concurrent write'")
            attempts.append(True)
        return original_backup(path, destination, version)

    monkeypatch.setattr(migrations, "_backup", competing_writer)
    backup = tmp_path / "locked-backup.sqlite3"
    migrate(store, apply=True, writers_stopped=True, backup=backup)
    assert attempts == [True]
    with closing(sqlite3.connect(backup)) as db:
        title = db.execute(
            "SELECT title FROM memories WHERE id=?", (manifest["memory_id"],)
        ).fetchone()[0]
    assert store.get(manifest["memory_id"])["title"] == title


@pytest.mark.parametrize("wal", [False, True])
@pytest.mark.parametrize(
    "mutation",
    [
        "ALTER TABLE oauth_clients RENAME COLUMN metadata TO bogus",
        "ALTER TABLE oauth_tokens RENAME COLUMN grant_id TO bogus",
        "DROP INDEX oauth_tokens_grant",
    ],
)
def test_unknown_historical_schema_is_rejected_without_touching_files(
    historical, tmp_path, wal, mutation
):
    store, _ = historical
    script = """
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
if sys.argv[3] == "wal":
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA wal_autocheckpoint=0")
db.execute(sys.argv[2])
db.commit()
if sys.argv[3] == "wal":
    os._exit(0)
db.close()
"""
    subprocess.run(
        [sys.executable, "-c", script, str(store.path), mutation, "wal" if wal else "main"],
        check=True,
        timeout=10,
    )
    paths = [store.path] + ([Path(str(store.path) + "-wal")] if wal else [])
    before = [path.read_bytes() for path in paths]
    backup = tmp_path / "should-not-exist.sqlite3"
    with pytest.raises(IntegrityError, match="Unrecognized"):
        migrate(store, apply=True, writers_stopped=True, backup=backup)
    assert [path.read_bytes() for path in paths] == before
    assert not backup.exists()


def test_current_schema_requires_application_id(tmp_path):
    store = Store(tmp_path / "current")
    store.initialize()
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("PRAGMA application_id=0")
    before = store.path.read_bytes()
    with pytest.raises(IntegrityError, match="Not a supported"):
        migrate(store, apply=True, writers_stopped=True, backup=None)
    assert store.path.read_bytes() == before
