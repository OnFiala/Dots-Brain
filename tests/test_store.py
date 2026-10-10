import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from dots_brain.errors import ConflictError, InputError, NotFoundError, SuppressedError
from dots_brain.store import Store, source_key


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "memory")
    result.initialize()
    return result


def remember(store, **overrides):
    fields = dict(
        content="Use SQLite for the project memory.",
        source="test",
        account="owner",
        event_id="decision-1",
        title="Storage decision",
        project="alpha",
    )
    return store.remember(**(fields | overrides))


def test_retry_revision_and_restart_preserve_provenance(store):
    first = remember(store)
    assert remember(store)["changed"] is False
    with pytest.raises(ConflictError):
        remember(store, content="Use a local database.")
    second = remember(store, content="Use a local database.", expected_revision=1)
    assert second["revision"] == 2
    assert remember(store)["replayed_revision"] == 1
    reopened = Store(store.directory)
    reopened.initialize()
    assert reopened.get(first["id"])["content"] == "Use a local database."
    assert reopened.get(first["id"], revision=1)["content"].startswith("Use SQLite")
    assert reopened.get(first["id"])["event_id"] == "decision-1"
    assert len(list(reopened.export())) == 2


def test_project_boundaries_apply_before_search_and_to_all_reads(store):
    private = remember(store, content="Secret alpha design")
    remember(store, event_id="public", project="beta", content="Public beta design")
    results = store.search("design", projects=("beta",), limit=1)
    assert len(results) == 1 and results[0]["project"] == "beta"
    assert store.search("design", project="alpha", projects=("beta",)) == []
    assert store.search("design", projects=()) == []
    with pytest.raises(NotFoundError):
        store.get(private["id"], projects=("beta",))
    assert (
        store.forget(private["id"], expected_revision=999, projects=("beta",))["deleted"] is False
    )
    assert store.status(projects=("beta",))["memories"] == 1
    assert len(list(store.export(projects=("beta",)))) == 1


def test_explicit_revision_can_restore_earlier_content(store):
    initial = remember(store)
    remember(store, content="A different storage decision", expected_revision=1)
    restored = remember(store, expected_revision=2)
    assert restored["revision"] == 3 and restored["changed"]
    assert store.get(initial["id"])["content"].startswith("Use SQLite")


def test_forget_removes_revisions_search_and_prevents_reimport(store):
    record = remember(store)
    remember(store, content="Changed private content", expected_revision=1)
    assert store.forget(record["id"], expected_revision=2)["deleted"] is True
    assert store.forget(record["id"], expected_revision=2)["deleted"] is False
    with pytest.raises(NotFoundError):
        store.get(record["id"], revision=1)
    assert store.search("SQLite private") == []
    assert list(store.export()) == []
    with pytest.raises(SuppressedError):
        remember(store)
    assert store.status()["memories"] == 0


def test_stale_forget_preserves_the_new_revision_and_allows_current_delete(store):
    first = remember(store)
    remember(store, content="A newer decision from another client", expected_revision=1)
    with pytest.raises(ConflictError, match="current expected_revision"):
        store.forget(first["id"], expected_revision=first["revision"])
    assert store.get(first["id"])["revision"] == 2
    assert store.get(first["id"], revision=1)["content"].startswith("Use SQLite")
    assert store.search("newer")[0]["id"] == first["id"]
    assert store.forget(first["id"], expected_revision=2)["deleted"] is True
    assert store.forget(first["id"], expected_revision=2)["deleted"] is False
    with pytest.raises(SuppressedError):
        remember(store)


@pytest.mark.parametrize("revision", [None, True, False, 0, -1, 1.5, "1"])
def test_forget_rejects_invalid_revision_without_removing_memory(store, revision):
    record = remember(store)
    with pytest.raises(InputError, match="positive integer"):
        store.forget(record["id"], expected_revision=revision)
    assert store.get(record["id"])["revision"] == 1


def test_concurrent_retries_create_one_record(store):
    with ThreadPoolExecutor(max_workers=8) as executor:
        records = list(executor.map(lambda _: remember(store), range(16)))
    assert len({r["id"] for r in records}) == 1
    assert sum(r["changed"] for r in records) == 1
    assert store.status()["revisions"] == 1


def test_concurrent_setup_converges_on_one_schema(tmp_path):
    store = Store(tmp_path / "new-memory")
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: Store(store.directory).initialize(), range(8)))
    assert store.status()["memories"] == 0
    remember(store)
    assert store.status()["memories"] == 1


def test_journal_mode_contention_has_a_deadline_and_can_resume(tmp_path, monkeypatch):
    from dots_brain import store as store_module

    memory = Store(tmp_path / "contended")
    memory.initialize()
    with closing(sqlite3.connect(memory.path)) as reader, reader:
        reader.execute("PRAGMA journal_mode=DELETE")
        reader.execute("CREATE TABLE existing_data (value TEXT)")
        reader.execute("INSERT INTO existing_data VALUES ('preserve me')")
        reader.commit()
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM existing_data").fetchall()
        monkeypatch.setattr(store_module, "WAL_LOCK_TIMEOUT", 0.05)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            memory.initialize()
        reader.rollback()
    memory.initialize()
    assert memory.status()["memories"] == 0
    with memory.connection() as db:
        assert db.execute("SELECT value FROM existing_data").fetchone()[0] == "preserve me"
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_journal_mode_retries_a_temporary_reader_lock(tmp_path):
    from concurrent.futures import TimeoutError

    memory = Store(tmp_path / "temporary-lock")
    memory.initialize()
    with (
        closing(sqlite3.connect(memory.path)) as reader,
        reader,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        reader.execute("PRAGMA journal_mode=DELETE")
        reader.execute("CREATE TABLE existing_data (value TEXT)")
        reader.execute("INSERT INTO existing_data VALUES ('preserve me')")
        reader.commit()
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM existing_data").fetchall()
        future = executor.submit(memory.initialize)
        try:
            with pytest.raises(TimeoutError):
                future.result(timeout=0.1)
        finally:
            reader.rollback()
        future.result(timeout=5)
    assert memory.status()["memories"] == 0


def test_search_handles_czech_and_query_syntax_without_sql_execution(store):
    remember(store, content="Paměť používá lokální úložiště a češtinu.")
    assert store.search('"paměť" OR (lokální):*')[0]["project"] == "alpha"
    assert store.search("!!!") == []
    assert store.status()["memories"] == 1


def test_schema_and_database_permissions(store):
    assert store.path.stat().st_mode & 0o777 == 0o600
    with store.connection(write=True) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(Exception, match="Unsupported database version"):
        store.initialize()


def test_source_identity_and_deletion_suppression_are_project_scoped(store):
    alpha = remember(store, project="alpha", writer_principal="local-client:alpha")
    beta = remember(store, project="beta", writer_principal="local-client:beta")
    assert alpha["id"] != beta["id"]
    assert store.forget(alpha["id"], expected_revision=1, writer_principal="local-client:alpha")[
        "deleted"
    ]
    assert store.get(beta["id"])["project"] == "beta"
    with pytest.raises(SuppressedError):
        remember(store, project="alpha")


def test_legacy_global_tombstones_still_block_all_projects(store):
    key = source_key("test", "owner", "decision-1")
    with store.connection(write=True) as db:
        db.execute("INSERT INTO suppressions VALUES (?,?)", (key, "2026-01-01T00:00:00+00:00"))
    with pytest.raises(SuppressedError):
        remember(store, project="alpha")
    with pytest.raises(SuppressedError):
        remember(store, project="beta")


def test_writer_attribution_and_replay_are_idempotent(store):
    initial = remember(store, writer_principal="local-client:writer-a")
    updated = remember(
        store,
        content="Updated by another writer.",
        expected_revision=1,
        writer_principal="oauth-grant:writer-b",
    )
    replay = remember(store, writer_principal="local-client:writer-c")
    assert replay == {
        "id": initial["id"],
        "revision": updated["revision"],
        "replayed_revision": 1,
        "changed": False,
    }
    with store.connection() as db:
        writers = db.execute(
            "SELECT revision,writer_principal FROM revisions WHERE memory_id=? ORDER BY revision",
            (initial["id"],),
        ).fetchall()
    assert [tuple(row) for row in writers] == [
        (1, "local-client:writer-a"),
        (2, "oauth-grant:writer-b"),
    ]
    assert store.forget(
        initial["id"], expected_revision=2, writer_principal="oauth-grant:writer-b"
    )["deleted"]
    with store.connection() as db:
        deleted = db.execute(
            "SELECT deleted_revision,deleted_by FROM scoped_suppressions"
        ).fetchone()
    assert tuple(deleted) == (2, "oauth-grant:writer-b")
