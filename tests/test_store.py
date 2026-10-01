from concurrent.futures import ThreadPoolExecutor

import pytest

from dots_brain.errors import ConflictError, NotFoundError, SuppressedError
from dots_brain.store import Store


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
    assert store.forget(private["id"], projects=("beta",))["deleted"] is False
    assert store.status(projects=("beta",))["memories"] == 1
    assert len(list(store.export(projects=("beta",)))) == 1


def test_forget_removes_revisions_search_and_prevents_reimport(store):
    record = remember(store)
    remember(store, content="Changed private content", expected_revision=1)
    assert store.forget(record["id"])["deleted"] is True
    assert store.forget(record["id"])["deleted"] is False
    with pytest.raises(NotFoundError):
        store.get(record["id"], revision=1)
    assert store.search("SQLite private") == []
    assert list(store.export()) == []
    with pytest.raises(SuppressedError):
        remember(store)
    assert store.status()["memories"] == 0


def test_concurrent_retries_create_one_record(store):
    with ThreadPoolExecutor(max_workers=8) as executor:
        records = list(executor.map(lambda _: remember(store), range(16)))
    assert len({r["id"] for r in records}) == 1
    assert sum(r["changed"] for r in records) == 1
    assert store.status()["revisions"] == 1


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
