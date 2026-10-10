"""Behavioral regressions from the external review; all data is disposable."""

import os
import sqlite3
import threading
import unicodedata
from contextlib import closing
from multiprocessing import get_context

import pytest

from dots_brain.errors import BrainError, ConflictError, InputError
from dots_brain.operations import activate_restore, backup_store, restore_store
from dots_brain.store import Store


def _initialize_in_child(directory: str, crash_before_publish: bool = False) -> None:
    """Exercise the initialization publication boundary in a fresh interpreter."""
    if crash_before_publish:
        from dots_brain import store as store_module

        store_module.sync_file_and_parent = lambda _path: os._exit(86)
    Store(directory).initialize()


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "source")
    result.initialize()
    return result


def put(store, content="Alpha", **kwargs):
    return store.remember(
        content=content, source="fixture", account="owner", event_id="one", **kwargs
    )


def test_replay_matches_the_revision_this_request_could_have_created(store):
    first = put(store)
    put(store, "Beta", expected_revision=1)
    put(store, "Alpha", expected_revision=2)
    assert put(store, "Alpha", expected_revision=3)["changed"] is False
    assert store.get(first["id"])["revision"] == 3
    assert put(store, "Beta", expected_revision=1)["replayed_revision"] == 2
    for previous in (0, 2, 99):
        with pytest.raises((ConflictError, InputError)):
            put(store, "Beta", expected_revision=previous)
    # A replay of the original create remains valid after later updates.
    assert put(store)["replayed_revision"] == 1


def test_rejected_cutover_does_not_disable_the_current_store(store, tmp_path):
    put(store)
    snapshot = tmp_path / "backup.sqlite3"
    backup_store(store, snapshot)
    target = Store(tmp_path / "restored")
    restore_store(snapshot, target, latest_deletions=store)
    put(store, "New canonical fact", expected_revision=1)
    with pytest.raises(BrainError, match="canonical state"):
        activate_restore(store, target, writers_stopped=True)
    assert not (store.directory / "disabled.json").exists()
    assert put(store, "Still writable", expected_revision=2)["revision"] == 3
    assert (target.directory / "disabled.json").exists()


def test_forgotten_terms_are_removed_from_new_snapshots_and_restores(store, tmp_path):
    for number in range(30):
        store.remember(
            content=f"Synthetic gardening fixture {number}",
            source="fixture",
            account="owner",
            event_id=str(number),
        )
    sentinel = "uniqueforgettensynthetictoken"
    saved = put(store, sentinel)
    store.forget(saved["id"], expected_revision=1)
    with store.connection() as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert store.search(sentinel) == []
    assert sentinel.encode() not in store.path.read_bytes()
    wal = store.path.with_name(store.path.name + "-wal")
    assert not wal.exists() or sentinel.encode() not in wal.read_bytes()
    snapshot = tmp_path / "after-forget.sqlite3"
    backup_store(store, snapshot)
    assert sentinel.encode() not in snapshot.read_bytes()
    target = Store(tmp_path / "restored")
    restore_store(snapshot, target, latest_deletions=store)
    assert sentinel.encode() not in target.path.read_bytes()
    assert not list(tmp_path.glob("after-forget.sqlite3-*"))


def test_setup_derives_fts_row_mapping_for_existing_v2_store(store):
    record = put(store)
    with store.connection(write=True) as db:
        db.execute("DROP TABLE memory_fts_rows")
        db.execute(
            "INSERT INTO memory_fts(memory_id,title,content) "
            "VALUES ('orphaned-derived-row','','old')"
        )
    store.initialize()
    with store.connection() as db:
        mapped = db.execute(
            "SELECT fts_rowid FROM memory_fts_rows WHERE memory_id=?", (record["id"],)
        ).fetchone()[0]
        actual = db.execute(
            "SELECT rowid FROM memory_fts WHERE memory_id=?", (record["id"],)
        ).fetchone()[0]
        assert (
            db.execute(
                "SELECT COUNT(*) FROM memory_fts WHERE memory_id='orphaned-derived-row'"
            ).fetchone()[0]
            == 0
        )
    assert mapped == actual
    put(store, "Updated full-text content", expected_revision=1)
    assert store.forget(record["id"], expected_revision=2)["deleted"]
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_fts_rows").fetchone()[0] == 0


def test_backup_is_standalone_and_restore_does_not_modify_it(store, tmp_path):
    put(store)
    snapshot = tmp_path / "backup.sqlite3"
    backup_store(store, snapshot)
    before = snapshot.read_bytes()
    assert before[18:20] == b"\x01\x01"  # Rollback journal header, no WAL sidecars.
    snapshot.chmod(0o400)
    restore_store(snapshot, Store(tmp_path / "restore"), latest_deletions=store)
    assert snapshot.read_bytes() == before
    assert not list(tmp_path.glob("backup.sqlite3-*"))


def test_backup_is_consistent_while_a_wal_writer_holds_an_uncommitted_change(store, tmp_path):
    with store.connection(write=True) as db:
        db.execute("CREATE TABLE writer_probe(value TEXT NOT NULL)")
    writer_ready = threading.Event()
    release_writer = threading.Event()

    def writer():
        with store.connection(write=True) as db:
            db.execute("INSERT INTO writer_probe VALUES ('uncommitted')")
            writer_ready.set()
            assert release_writer.wait(timeout=5)

    active = threading.Thread(target=writer)
    active.start()
    assert writer_ready.wait(timeout=5)
    snapshot = tmp_path / "live-wal.sqlite3"
    assert backup_store(store, snapshot)["state"] == "verified_backup"
    with closing(sqlite3.connect(snapshot)) as db:
        assert db.execute("SELECT COUNT(*) FROM writer_probe").fetchone()[0] == 0
    release_writer.set()
    active.join(timeout=5)
    assert not active.is_alive()
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM writer_probe").fetchone()[0] == 1


@pytest.mark.parametrize("name", ["my#backup.sqlite3", "what?backup.sqlite3", "percent%25.sqlite3"])
def test_backup_names_are_paths_not_sqlite_uri_fragments(store, tmp_path, name):
    put(store)
    snapshot = tmp_path / name
    backup_store(store, snapshot)
    target = Store(tmp_path / "restored")
    restore_store(snapshot, target, latest_deletions=store)
    assert target.status()["memories"] == 1
    assert not (tmp_path / "my").exists()
    assert not (tmp_path / "what").exists()


@pytest.mark.parametrize("revision", [True, 1.5, "1", 0, -1, 2**70])
def test_get_rejects_non_sqlite_revisions(store, revision):
    record = put(store)
    with pytest.raises(InputError):
        store.get(record["id"], revision=revision)


@pytest.mark.parametrize("limit", [True, 1.5, "1", 0, -1, 51])
def test_search_rejects_non_integer_limits(store, limit):
    put(store)
    with pytest.raises(InputError):
        store.search("Alpha", limit=limit)


def test_invalid_text_and_project_containers_fail_before_sqlite(store):
    for text in ("broken\ud800", "\x00"):
        with pytest.raises(InputError):
            put(store, text)
    record = put(store)
    with pytest.raises(InputError):
        store.get(record["id"], projects="default")
    assert store.status()["memories"] == 1


def test_nfd_query_and_revision_timestamp(store):
    record = put(store, "Kočka má paměť")
    assert store.search(unicodedata.normalize("NFD", "kočka"))[0]["id"] == record["id"]
    first = store.get(record["id"])
    put(store, "A new fact", expected_revision=1)
    assert (
        store.get(record["id"], revision=1)["revision_created_at"] == first["revision_created_at"]
    )
    assert list(store.export())[0]["revision_created_at"] == first["revision_created_at"]


def test_existing_foreign_or_incomplete_store_is_not_adopted(tmp_path):
    foreign = Store(tmp_path / "foreign")
    foreign.directory.mkdir()
    with closing(sqlite3.connect(foreign.path)) as db:
        db.execute("CREATE TABLE unrelated(value TEXT)")
        db.commit()
    before = foreign.path.read_bytes()
    with pytest.raises(BrainError):
        foreign.initialize()
    assert foreign.path.read_bytes() == before
    with pytest.raises(BrainError):
        foreign.status()


def test_multiprocess_setup_and_prepublication_crash_leave_one_complete_database(tmp_path):
    directory = tmp_path / "concurrent-initialize"
    context = get_context("spawn")
    crashed = context.Process(target=_initialize_in_child, args=(str(directory), True))
    crashed.start()
    crashed.join(timeout=10)
    assert crashed.exitcode == 86
    assert not (directory / "brain.sqlite3").exists()

    workers = [
        context.Process(target=_initialize_in_child, args=(str(directory),)) for _ in range(6)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0

    store = Store(directory)
    assert store.status()["memories"] == 0
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("PRAGMA application_id").fetchone()[0] != 0
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_legacy_schema_v2_zero_application_id_remains_explicitly_compatible(tmp_path):
    store = Store(tmp_path / "legacy-v2")
    store.initialize()
    with store.connection() as db:
        db.execute("PRAGMA application_id=0")
    store.initialize()
    assert store.status()["memories"] == 0
