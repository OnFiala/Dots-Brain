"""Selected storage contracts; every database and credential is disposable."""

import asyncio
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
import unicodedata
from contextlib import closing
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from dots_brain import cli, operations, runtime
from dots_brain.errors import CapabilityError, ConflictError, InputError, SuppressedError
from dots_brain.local import locked, write_json, writer_lock
from dots_brain.migrations import migrate_v1_to_v2
from dots_brain.operations import activate_restore, backup_store, restore_store
from dots_brain.store import Store


@pytest.fixture
def store(tmp_path, monkeypatch):
    private_home = tmp_path / "home"
    private_home.mkdir()
    monkeypatch.setenv("HOME", str(private_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(private_home / ".local/share"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    value = Store(tmp_path / "memory")
    value.initialize()
    return value


def put(store, content="Alpha", **kwargs):
    fields = dict(content=content, source="fixture", account="owner", event_id="one")
    return store.remember(**(fields | kwargs))


def test_replay_matches_the_revision_this_request_could_have_created(store):
    first = put(store)
    put(store, "Beta", expected_revision=1)
    put(store, "Alpha", expected_revision=2)
    assert put(store, "Alpha", expected_revision=3)["changed"] is False
    assert store.get(first["id"])["revision"] == 3
    assert put(store, "Beta", expected_revision=1)["replayed_revision"] == 2
    for previous in (0, 2, 99, 2**65):
        with pytest.raises((ConflictError, InputError)):
            put(store, "Beta", expected_revision=previous)
    assert put(store)["replayed_revision"] == 1


def test_nfd_query_full_query_and_revision_timestamp(store):
    record = put(store, "Kočka má paměť zahradnictvi")
    assert store.search(unicodedata.normalize("NFD", "kočka"))[0]["id"] == record["id"]
    assert store.search(" ".join(["absent"] * 40 + ["zahradnictvi"]))[0]["id"] == record["id"]
    first = store.get(record["id"])
    put(store, "A new fact", expected_revision=1)
    assert (
        store.get(record["id"], revision=1)["revision_created_at"] == first["revision_created_at"]
    )
    assert list(store.export())[0]["revision_created_at"] == first["revision_created_at"]


@pytest.mark.parametrize(
    "field", ["content", "title", "source", "account", "event_id", "project", "source_uri"]
)
def test_invalid_utf8_is_rejected_before_storage(store, field):
    with pytest.raises(InputError, match="UTF-8") as error:
        put(store, **{field: "synthetic\ud800"})
    assert "synthetic" not in str(error.value)
    assert store.status()["memories"] == 0


@pytest.mark.parametrize("entrypoint", ["initialize", "connection"])
@pytest.mark.parametrize("version", [1, 99])
def test_rejected_version_preserves_main_and_committed_wal(tmp_path, entrypoint, version):
    store = Store(tmp_path / "unsupported")
    store.directory.mkdir()
    script = """
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute('CREATE TABLE fixture(value TEXT)')
db.execute('PRAGMA journal_mode=WAL')
db.execute('PRAGMA wal_autocheckpoint=0')
db.execute('PRAGMA user_version=' + sys.argv[2])
db.execute("INSERT INTO fixture VALUES ('synthetic committed record')")
db.commit()
os._exit(0)
"""
    subprocess.run(
        [sys.executable, "-c", script, str(store.path), str(version)], check=True, timeout=10
    )
    wal = Path(str(store.path) + "-wal")
    before = store.path.read_bytes(), wal.read_bytes()
    assert len(before[1]) > 32
    with pytest.raises(InputError):
        if entrypoint == "initialize":
            store.initialize()
        else:
            with store.connection():
                pytest.fail("unsupported database was opened")
    assert (store.path.read_bytes(), wal.read_bytes()) == before


def test_old_sqlite_cannot_write_or_initialize(store, tmp_path, monkeypatch):
    before = store.path.read_bytes()
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 41, 2))
    with pytest.raises(CapabilityError, match="3.42"):
        put(store)
    fresh = Store(tmp_path / "fresh")
    with pytest.raises(CapabilityError):
        fresh.initialize()
    assert not fresh.path.exists()
    assert store.path.read_bytes() == before


def test_secure_fts_compacts_old_deleted_terms_once(store):
    sentinel = "syntheticdeletedftsword"
    put(store, sentinel)
    with store.connection() as db:
        db.execute("INSERT INTO memory_fts(memory_fts,rank) VALUES ('secure-delete',0)")
        db.execute("DELETE FROM memory_fts")
        db.execute("DELETE FROM memories")
    assert sentinel.encode() in store.path.read_bytes()
    store.initialize()
    assert sentinel.encode() not in store.path.read_bytes()
    with store.connection() as db:
        assert (
            db.execute("SELECT v FROM memory_fts_config WHERE k='secure-delete'").fetchone()[0] == 1
        )
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2


def test_forget_and_restore_suppression_remove_terms_from_new_snapshots(store, tmp_path):
    sentinel = "uniqueforgettensynthetictoken"
    record = put(store, sentinel)
    old = tmp_path / "old.sqlite3"
    backup_store(store, old)
    before = old.read_bytes()
    store.forget(record["id"], expected_revision=1)
    assert store.search(sentinel) == []
    new = tmp_path / "new.sqlite3"
    backup_store(store, new)
    assert sentinel.encode() not in new.read_bytes()
    restored = Store(tmp_path / "restored")
    restore_store(old, restored, latest_deletions=store)
    assert sentinel.encode() not in restored.path.read_bytes()
    assert old.read_bytes() == before
    assert restored.status()["memories"] == 0


@pytest.mark.parametrize("name", ["backup.sqlite3", "question?.sqlite3", "hash#percent%.sqlite3"])
def test_snapshot_is_closed_standalone_and_restore_preserves_input(store, tmp_path, name):
    put(store)
    snapshot = tmp_path / name
    backup_store(store, snapshot)
    before = snapshot.read_bytes()
    assert before[18:20] == b"\x01\x01"
    snapshot.chmod(0o400)
    restore_store(snapshot, Store(tmp_path / "restored"), latest_deletions=store)
    assert snapshot.read_bytes() == before
    assert not list(tmp_path.glob(name + "-*"))


def test_snapshot_includes_committed_state_during_wal_write(store, tmp_path):
    record = put(store)
    ready, release = threading.Event(), threading.Event()
    errors = []

    def writer():
        try:
            with store.connection(write=True) as db:
                db.execute(
                    "UPDATE revisions SET content='uncommitted' WHERE memory_id=?", (record["id"],)
                )
                ready.set()
                assert release.wait(10)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert ready.wait(5)
        output = tmp_path / "wal-backup.sqlite3"
        backup_store(store, output)
        with closing(sqlite3.connect(output)) as db:
            assert db.execute("SELECT content FROM revisions").fetchone()[0] == "Alpha"
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and not errors


def external_lease(directory, exclusive):
    script = """
import sys
from pathlib import Path
from dots_brain.errors import InputError
from dots_brain.local import writer_lock
try:
    with writer_lock(Path(sys.argv[1]), exclusive=sys.argv[2] == 'True'):
        pass
except InputError:
    sys.exit(3)
"""
    return subprocess.run(
        [sys.executable, "-c", script, str(directory), str(exclusive)], timeout=10
    ).returncode


def test_writer_and_maintenance_leases_are_reentrant_and_exclude_other_writers(store):
    with writer_lock(store.directory):
        assert external_lease(store.directory, False) == 0
        assert external_lease(store.directory, True) == 3
        with pytest.raises(InputError, match="active writer"):
            with writer_lock(store.directory, exclusive=True):
                pytest.fail("lease upgrade is unsafe")
    with writer_lock(store.directory, exclusive=True):
        put(store)
        assert external_lease(store.directory, False) == 3
        assert external_lease(store.directory, True) == 3
    assert external_lease(store.directory, True) == 0


def test_stdio_server_holds_writer_lease_for_its_lifetime(store):
    async def exercise():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "dots_brain.cli", "--data-dir", str(store.directory), "serve"],
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert (await session.list_tools()).tools
                with pytest.raises(InputError, match="lock is busy"):
                    with writer_lock(store.directory, exclusive=True):
                        pytest.fail("server lost its lifetime lease")
                with pytest.raises(InputError, match="lock is busy"):
                    migrate_v1_to_v2(store, apply=True, stop_guard=lambda: None)

    asyncio.run(exercise())
    with writer_lock(store.directory, exclusive=True):
        pass
    assert migrate_v1_to_v2(store, apply=True)["state"] == "already_current"


def test_http_entrypoint_has_one_exclusive_owner(store, monkeypatch):
    args = cli.parser().parse_args(
        ["--data-dir", str(store.directory), "serve", "--transport", "http"]
    )
    called = []
    monkeypatch.setattr(cli, "_run", lambda _: called.append(True))
    with locked(store.directory / "http.lock"):
        with pytest.raises(InputError, match="lock is busy"):
            cli.run(args)
    assert called == []
    cli.run(args)
    assert called == [True]


def test_cutover_validation_finishes_before_disabling_source(store, tmp_path):
    put(store)
    output = tmp_path / "backup.sqlite3"
    backup_store(store, output)
    target = Store(tmp_path / "restored")
    restore_store(output, target, latest_deletions=store)
    put(store, "New canonical fact", expected_revision=1)
    with pytest.raises(InputError, match="canonical state"):
        activate_restore(store, target, writers_stopped=True)
    assert not (store.directory / "disabled.json").exists()
    assert put(store, "Still writable", expected_revision=2)["changed"]
    assert (target.directory / "disabled.json").exists()


@pytest.mark.parametrize("barrier", ["empty", "unknown", "dangling-symlink"])
def test_cutover_preserves_unrelated_source_barrier_and_both_stores(store, tmp_path, barrier):
    put(store)
    output = tmp_path / "backup.sqlite3"
    backup_store(store, output)
    target = Store(tmp_path / "restored")
    restore_store(output, target, latest_deletions=store)
    marker = store.directory / "disabled.json"
    if barrier == "dangling-symlink":
        marker.symlink_to(tmp_path / "missing-marker")
    else:
        write_json(marker, {} if barrier == "empty" else {"reason": "unknown"})
    marker_before = marker.readlink() if marker.is_symlink() else marker.read_bytes()
    target_marker = target.directory / "disabled.json"
    target_before = target_marker.read_bytes()

    def snapshots():
        results = []
        for installation in (store, target):
            with installation.connection() as db:
                results.append(tuple(db.iterdump()))
        return results

    before = snapshots()
    with pytest.raises(InputError, match="disabled"):
        store.ensure_writable()
    message = "symbolic link" if barrier == "dangling-symlink" else "unrelated disable barrier"
    with pytest.raises(InputError, match=message):
        activate_restore(store, target, writers_stopped=True)
    after_marker = marker.readlink() if marker.is_symlink() else marker.read_bytes()
    assert after_marker == marker_before
    assert target_marker.read_bytes() == target_before
    assert snapshots() == before
    for installation in (store, target):
        with pytest.raises(InputError, match="disabled"):
            installation.ensure_writable()


def test_pending_cutover_can_only_retry_its_exact_target(store, tmp_path, monkeypatch):
    output = tmp_path / "backup.sqlite3"
    backup_store(store, output)
    target = Store(tmp_path / "restored")
    restore_store(output, target, latest_deletions=store)
    with monkeypatch.context() as patch:
        patch.setattr(
            operations, "sync_file_and_parent", lambda _: (_ for _ in ()).throw(OSError("fixture"))
        )
        with pytest.raises(OSError):
            activate_restore(store, target, writers_stopped=True)
    assert json.loads((store.directory / "disabled.json").read_text())["cutover"] == "pending"
    other = Store(tmp_path / "other")
    restore_store(output, other, latest_deletions=store)
    with pytest.raises(InputError, match="already replaced"):
        activate_restore(store, other, writers_stopped=True)
    assert activate_restore(store, target, writers_stopped=True)["state"] == "restored_ready"


@pytest.mark.parametrize(
    "state", [{"version": 1, "disabled": True}, {"version": 1, "reason": "uninstalled"}]
)
def test_failed_resume_preserves_original_marker(store, monkeypatch, state):
    marker = store.directory / "disabled.json"
    write_json(marker, state)
    before = marker.read_bytes()
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(store, "initialize", lambda: (_ for _ in ()).throw(OSError("fixture")))
    with pytest.raises(OSError):
        runtime.up(store, resume=True)
    assert marker.read_bytes() == before


@pytest.mark.parametrize(
    "state",
    [
        {"future": True},
        {"reason": "restored_requires_review"},
        {"reason": "restore_cutover", "cutover": "committed"},
    ],
)
def test_uninstall_preserves_unknown_and_recovery_barriers(store, state):
    from dots_brain.removal import uninstall

    marker = store.directory / "disabled.json"
    write_json(marker, state)
    uninstall(store)
    saved = json.loads(marker.read_text())
    assert all(saved[k] == value for k, value in state.items())
    assert saved["uninstalled_at"]
    with pytest.raises(InputError, match="activate-restore"):
        runtime.resume_installation(store)


def test_released_v1_fixture_migrates_with_data_access_and_deletion_history(tmp_path):
    fixture = Path(__file__).parent / "fixtures/historical/v1-263c386"
    manifest = json.loads((fixture / "manifest.json").read_text())
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((fixture / name).read_bytes()).hexdigest() == digest
    destination = tmp_path / "historical"
    shutil.copytree(fixture, destination)
    store = Store(destination)
    result = migrate_v1_to_v2(store, apply=True, stop_guard=lambda: None)
    assert result["state"] == "migrated"
    assert store.get(manifest["memory_id"], revision=1)["content"] == "Synthetic first revision"
    assert store.get(manifest["memory_id"])["revision"] == 2
    with pytest.raises(SuppressedError):
        store.remember(
            content="Synthetic retry",
            source="fixture",
            account="test",
            event_id="deleted",
            project="test",
        )
    from dots_brain.auth import authenticate
    from dots_brain.oauth import OAuthStore

    assert authenticate(store, "public-synthetic-upgrade-probe") is not None
    assert OAuthStore(store).policy("public-synthetic-oauth-token") is not None
    with closing(sqlite3.connect(result["backup"])) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1


def test_forget_after_v1_migration_erases_dropped_table_pages_from_new_backup(tmp_path):
    fixture = Path(__file__).parent / "fixtures/historical/v1-263c386"
    destination = tmp_path / "historical"
    shutil.copytree(fixture, destination)
    manifest = json.loads((fixture / "manifest.json").read_text())
    store = Store(destination)
    result = migrate_v1_to_v2(store, apply=True, stop_guard=lambda: None)
    old_backup = Path(result["backup"])
    old_bytes = old_backup.read_bytes()
    store.forget(manifest["memory_id"], expected_revision=2)
    new_backup = tmp_path / "after-forget.sqlite3"
    backup_store(store, new_backup)
    for sentinel in (b"Synthetic first revision", b"Synthetic second revision"):
        assert sentinel in old_bytes
        assert sentinel not in store.path.read_bytes()
        assert sentinel not in new_backup.read_bytes()
    assert old_backup.read_bytes() == old_bytes
