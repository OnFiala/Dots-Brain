"""Independent upgrade and rejection regressions using only synthetic stores."""

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from dots_brain.activity import AuditLog
from dots_brain.auth import authenticate
from dots_brain.errors import BrainError, InputError, SuppressedError
from dots_brain.migrations import migrate_store
from dots_brain.oauth import OAuthStore
from dots_brain.store import APPLICATION_ID, Store

FIXTURES = Path(__file__).parent / "fixtures" / "historical"


def _copy_fixture(tmp_path, name):
    fixture = FIXTURES / name
    manifest = json.loads((fixture / "manifest.json").read_text())
    for filename, digest in manifest["files"].items():
        assert hashlib.sha256((fixture / filename).read_bytes()).hexdigest() == digest
    destination = tmp_path / name
    shutil.copytree(fixture, destination)
    return Store(destination), manifest


def _canonical_rows(path):
    """Compare actual persisted values, not merely record counts."""
    with closing(sqlite3.connect(path)) as db:
        tables = [
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if not row[0].startswith(("sqlite_", "memory_fts")) and row[0] != "semantic_state"
        ]
        return {
            table: sorted(db.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr)
            for table in tables
        }


def _file_bytes(directory):
    return {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}


@pytest.mark.parametrize("origin_version", [1, 2])
def test_actual_7085841_touch_can_upgrade_without_changing_canonical_rows(tmp_path, origin_version):
    store, manifest = _copy_fixture(tmp_path, f"touched-7085841-v{origin_version}")
    assert manifest["touched_by_commit"] == "708584169b50903ed83145676a506f7cbe017744"
    before = _canonical_rows(store.path)
    assert len(before["audit_events"]) == 1
    assert len(before["revisions"]) == 2
    assert len(before["clients"]) == 1
    assert len(before["oauth_grants"]) == len(before["oauth_tokens"]) == 2
    deletion_table = "suppressions" if origin_version == 1 else "scoped_suppressions"
    assert len(before[deletion_table]) == 1
    with closing(sqlite3.connect(store.path)) as db:
        assert (
            db.execute("SELECT sql FROM sqlite_master WHERE name='memory_fts_rows'").fetchone()[0]
            == manifest["memory_fts_rows_sql"]
        )
    original = _file_bytes(store.directory)
    plan = migrate_store(store, apply=False)
    assert plan == {
        "state": "migration_required",
        "from_version": 2,
        "to_version": 3,
        "writes": False,
    }
    assert _file_bytes(store.directory) == original
    backup = tmp_path / "before-current.sqlite3"
    result = migrate_store(store, apply=True, backup_path=backup, stop_guard=lambda: None)
    assert result["state"] == "migrated"
    after = _canonical_rows(store.path)
    assert {table: after[table] for table in before} == before
    assert _canonical_rows(backup) == before
    assert AuditLog(store).verify() == {"events": 1}
    assert authenticate(store, "public-synthetic-upgrade-probe").principal.endswith(
        manifest["probe_id"]
    )
    assert OAuthStore(store).policy("public-synthetic-oauth-token").principal == (
        "oauth-grant:fixture-grant"
    )
    assert OAuthStore(store).policy("public-synthetic-revoked-token") is None
    assert store.get(manifest["memory_id"])["revision"] == 2
    assert store.search("second")[0]["id"] == manifest["memory_id"]
    with pytest.raises(SuppressedError):
        store.remember(
            content="Synthetic retry",
            source="fixture",
            account="test",
            event_id="deleted",
            project="test",
        )


@pytest.mark.parametrize(
    "corruption",
    ["missing-current", "revision-gap", "fractional-revision", "foreign-key", "audit-chain"],
)
def test_plan_and_apply_both_reject_canonical_corruption_without_outputs(tmp_path, corruption):
    store, _ = _copy_fixture(tmp_path, "v2-c963700")
    with closing(sqlite3.connect(store.path)) as db, db:
        if corruption == "missing-current":
            db.execute("UPDATE memories SET current_revision=99")
        elif corruption == "revision-gap":
            db.execute("DELETE FROM revisions WHERE revision=1")
        elif corruption == "fractional-revision":
            db.execute("UPDATE revisions SET revision=1.5 WHERE revision=2")
            db.execute(
                "INSERT INTO revisions SELECT memory_id,3,content,digest,source_uri,title,"
                "writer_principal,created_at FROM revisions WHERE revision=1"
            )
            db.execute("UPDATE memories SET current_revision=3")
        elif corruption == "foreign-key":
            db.execute("UPDATE oauth_tokens SET grant_id='missing-fixture-grant'")
        else:
            with closing(sqlite3.connect(FIXTURES / "touched-7085841-v2/brain.sqlite3")) as source:
                audit = source.execute("SELECT * FROM audit_events").fetchone()
            db.execute(
                "INSERT INTO audit_events VALUES (" + ",".join("?" * len(audit)) + ")", audit
            )
            trigger = db.execute(
                "SELECT sql FROM sqlite_master WHERE name='audit_events_no_update'"
            ).fetchone()[0]
            db.execute("DROP TRIGGER audit_events_no_update")
            db.execute("UPDATE audit_events SET event_hash='synthetic-corrupt-hash'")
            db.execute(trigger)
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    before = _file_bytes(store.directory)
    for apply in (False, True):
        with pytest.raises(BrainError) as rejected:
            migrate_store(store, apply=apply, stop_guard=lambda: None)
        assert "--data-dir /absolute/private/recovery migrate" in str(rejected.value)
        after = _file_bytes(store.directory)
        assert {name: after[name] for name in before} == before
        # SQLite read-only WAL access may create empty coordination files, but
        # must neither publish a backup nor checkpoint/change committed data.
        assert set(after) - set(before) <= {"brain.sqlite3-wal", "brain.sqlite3-shm"}
        if "brain.sqlite3-wal" not in before and "brain.sqlite3-wal" in after:
            assert after["brain.sqlite3-wal"] == b""


def _leave_committed_wal_after_abnormal_close(path):
    script = """
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute('PRAGMA journal_mode=WAL')
db.execute('PRAGMA wal_autocheckpoint=0')
if sys.argv[2] == 'foreign':
    db.execute("INSERT INTO foreign_data VALUES ('synthetic WAL record')")
else:
    db.execute("UPDATE memories SET title='Synthetic WAL title'")
db.commit()
os._exit(0)
"""
    with closing(sqlite3.connect(path)) as db:
        foreign = bool(
            db.execute("SELECT 1 FROM sqlite_master WHERE name='foreign_data'").fetchone()
        )
    subprocess.run(
        [sys.executable, "-c", script, str(path), "foreign" if foreign else "historical"],
        check=True,
        timeout=10,
    )
    wal = Path(str(path) + "-wal")
    assert wal.stat().st_size > 32
    return wal


@pytest.mark.parametrize("layout", ["v1-263c386", "v2-c963700", "foreign-id", "foreign-shape"])
@pytest.mark.parametrize("entrypoint", ["initialize", "connection"])
def test_rejected_store_preserves_main_and_wal_after_abnormal_writer_close(
    tmp_path, layout, entrypoint
):
    if layout.startswith("v"):
        store, _ = _copy_fixture(tmp_path, layout)
    else:
        directory = tmp_path / layout
        directory.mkdir()
        store = Store(directory)
        with closing(sqlite3.connect(store.path)) as db:
            db.executescript("CREATE TABLE foreign_data(value TEXT); PRAGMA user_version=3;")
            app_id = APPLICATION_ID if layout == "foreign-shape" else 123
            db.execute(f"PRAGMA application_id={app_id}")
    wal = _leave_committed_wal_after_abnormal_close(store.path)
    before = store.path.read_bytes(), wal.read_bytes()
    with pytest.raises(BrainError):
        if entrypoint == "initialize":
            store.initialize()
        else:
            with store.connection():
                pytest.fail("An unsupported store connection was exposed to its caller")
    assert store.path.read_bytes() == before[0]
    assert wal.exists(), "Rejecting a store must not checkpoint and remove its committed WAL"
    assert wal.read_bytes() == before[1]


def test_failed_migration_removes_only_its_new_backup_and_partial_outputs(tmp_path, monkeypatch):
    import dots_brain.migrations as migrations

    transform = migrations._migrate

    def fail_after_real_transform(db):
        transform(db)
        raise sqlite3.OperationalError("deterministic failure after migration DDL")

    monkeypatch.setattr(migrations, "_migrate", fail_after_real_transform)
    for backup_kind in ("default", "explicit-new", "existing"):
        case = tmp_path / backup_kind
        case.mkdir()
        store, _ = _copy_fixture(case, "v1-263c386")
        saved_backup = store.directory / "brain.sqlite3.v1-backup-existing"
        saved_backup.write_bytes(b"synthetic pre-existing backup")
        backup = None if backup_kind == "default" else store.directory / "requested.sqlite3"
        if backup_kind == "existing":
            backup.write_bytes(b"synthetic user-supplied backup")
        before = _file_bytes(store.directory)
        expected_error = (
            InputError if backup_kind == "existing" else (BrainError, sqlite3.OperationalError)
        )
        with pytest.raises(expected_error):
            migrate_store(store, apply=True, backup_path=backup, stop_guard=lambda: None)
        after = _file_bytes(store.directory)
        if backup_kind == "explicit-new":
            # An explicitly requested recovery artifact belongs to the operator.
            assert _canonical_rows(backup) == _canonical_rows(store.path)
            del after[backup.name]
        assert after == before
        assert not list(store.directory.glob("*.partial*"))
        assert not list(store.directory.glob(".*.partial*"))


def test_changed_literal_whitespace_is_not_schema_equivalence(tmp_path):
    store, _ = _copy_fixture(tmp_path, "v2-c963700")
    with closing(sqlite3.connect(store.path)) as db, db:
        sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='audit_events_no_update'"
        ).fetchone()[0]
        db.execute("DROP TRIGGER audit_events_no_update")
        db.execute(sql.replace("audit events are append-only", "audit  events are append-only"))
    before = store.path.read_bytes()
    with pytest.raises(BrainError, match="Unrecognized audit_events_no_update"):
        migrate_store(store, apply=False)
    assert store.path.read_bytes() == before


def test_historical_schema_does_not_depend_on_current_ddl(tmp_path, monkeypatch):
    import dots_brain.schema as schema

    store, _ = _copy_fixture(tmp_path, "v1-263c386")
    # A future current-schema change cannot rewrite which old layouts are known.
    monkeypatch.setattr(
        schema,
        "STATEMENTS",
        tuple(
            sql.replace("title TEXT NOT NULL", "title BLOB NOT NULL") for sql in schema.STATEMENTS
        ),
    )
    schema._reference_objects.cache_clear()
    try:
        assert migrate_store(store, apply=False)["state"] == "migration_required"
    finally:
        schema._reference_objects.cache_clear()


def test_corrupt_plan_names_recovery_command_and_valid_backup_copy_can_upgrade(tmp_path):
    store, _ = _copy_fixture(tmp_path, "v2-c963700")
    good_backup = tmp_path / "known-good.sqlite3"
    shutil.copy2(store.path, good_backup)
    with closing(sqlite3.connect(store.path)) as db, db:
        db.execute("UPDATE memories SET current_revision=99")
    command = [
        sys.executable,
        "-m",
        "dots_brain.cli",
        "--data-dir",
        str(store.directory),
        "migrate",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert result.returncode == 1
    assert "--data-dir /absolute/private/recovery migrate" in json.loads(result.stderr)["message"]
    recovery = tmp_path / "recovery"
    recovery.mkdir(mode=0o700)
    shutil.copy2(good_backup, recovery / "brain.sqlite3")
    command[4] = str(recovery)
    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["state"] == "migration_required"
    result = subprocess.run(
        command + ["--apply", "--writers-stopped"], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["state"] == "migrated"


def test_backup_fsync_failure_removes_only_own_published_output(tmp_path, monkeypatch):
    import dots_brain.database_io as io

    store, _ = _copy_fixture(tmp_path, "v2-c963700")
    output = tmp_path / "failed.sqlite3"
    original_sync = io.sync_file_and_parent

    def fail_after_publication(path):
        if path == output:
            raise OSError("synthetic post-publication failure")
        original_sync(path)

    monkeypatch.setattr(io, "sync_file_and_parent", fail_after_publication)
    with closing(io.open_readonly(store.path)) as db, pytest.raises(OSError):
        io.snapshot(db, output, 2)
    assert not output.exists()
    assert not list(tmp_path.glob(".*.partial*"))


def test_backup_failure_preserves_concurrently_replaced_output(tmp_path, monkeypatch):
    import dots_brain.database_io as io

    store, _ = _copy_fixture(tmp_path, "v2-c963700")
    output = tmp_path / "replaced.sqlite3"
    replacement = tmp_path / "other-output"
    replacement.write_bytes(b"synthetic unrelated file")
    original_sync = io.sync_file_and_parent

    def replace_after_publication(path):
        if path == output:
            replacement.replace(output)
            raise OSError("synthetic post-publication replacement")
        original_sync(path)

    monkeypatch.setattr(io, "sync_file_and_parent", replace_after_publication)
    with closing(io.open_readonly(store.path)) as db, pytest.raises(OSError):
        io.snapshot(db, output, 2)
    assert output.read_bytes() == b"synthetic unrelated file"


def test_migration_failure_keeps_backup_if_old_state_cannot_be_proved(tmp_path, monkeypatch):
    import dots_brain.migrations as migrations

    store, _ = _copy_fixture(tmp_path, "v2-c963700")
    transform = migrations._migrate

    def fail_after_unexpected_commit(db):
        transform(db)
        db.commit()
        raise sqlite3.OperationalError("synthetic failure after commit")

    monkeypatch.setattr(migrations, "_migrate", fail_after_unexpected_commit)
    with pytest.raises((BrainError, sqlite3.OperationalError)):
        migrate_store(store, apply=True, stop_guard=lambda: None)
    backup = next(store.directory.glob("*.v2-backup-*"))
    with closing(sqlite3.connect(backup)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert migrate_store(store, apply=False)["state"] == "already_current"


def test_failed_migration_does_not_adopt_a_replaced_backup(tmp_path, monkeypatch):
    import dots_brain.migrations as migrations

    store, _ = _copy_fixture(tmp_path, "v2-c963700")
    real_backup = migrations._backup
    replaced = []

    def replaced_backup(*args):
        created = real_backup(*args)
        path = created.path if hasattr(created, "path") else created
        replacement = tmp_path / "unrelated-output"
        replacement.write_bytes(b"synthetic replacement owned elsewhere")
        replacement.replace(path)
        replaced.append(path)
        return created

    def failure(db):
        raise sqlite3.OperationalError("synthetic migration failure")

    monkeypatch.setattr(migrations, "_backup", replaced_backup)
    monkeypatch.setattr(migrations, "_migrate", failure)
    with pytest.raises((BrainError, sqlite3.OperationalError)):
        migrate_store(store, apply=True, stop_guard=lambda: None)
    assert len(replaced) == 1
    assert replaced[0].read_bytes() == b"synthetic replacement owned elsewhere"
