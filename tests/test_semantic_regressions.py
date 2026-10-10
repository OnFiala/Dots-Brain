"""Regression coverage for bounded, failure-tolerant semantic retrieval."""

import pytest

from dots_brain.auth import Policy
from dots_brain.semantic import MAX_CHUNKS_PER_MEMORY, SemanticIndex
from dots_brain.service import MemoryService
from dots_brain.store import Store


@pytest.fixture
def index(tmp_path):
    np = pytest.importorskip("numpy")
    store = Store(tmp_path / "memory")
    store.initialize()
    result = SemanticIndex.__new__(SemanticIndex)
    result.store, result.np = store, np
    result._chunks = lambda text: [(0, len(text))]
    result._embed = lambda text: np.array([1.0, 0.0], dtype="<f4")
    return result


def save(index, content, event_id, **extra):
    return index.store.remember(
        content=content,
        source="fixture",
        account="owner",
        event_id=event_id,
        project="alpha",
        **extra,
    )


def test_poisoned_and_empty_records_do_not_starve_later_healthy_records(index):
    poisoned = save(index, "poisoned embedding input", "poison")
    blank = save(index, "will be made blank", "blank")
    healthy = save(index, "healthy indexed fact", "healthy")
    with index.store.connection(write=True) as db:
        db.execute(
            "UPDATE revisions SET content=' \n\t ' WHERE memory_id=? AND revision=1",
            (blank["id"],),
        )

    original = index._embed

    def embed(text):
        if "poisoned" in text:
            raise RuntimeError("synthetic embedding failure")
        return original(text)

    index._embed = embed
    result = index.index(batch_size=16)
    assert result["examined"] == 3 and result["indexed_now"] == 1
    status = index.status()
    assert status["indexed"] == 1 and status["failed"] == 1 and status["empty"] == 1
    assert status["pending"] == 0
    assert index.search("healthy")[0]["id"] == healthy["id"]

    assert index.retry_failed() == 1
    index._embed = original
    assert index.index(batch_size=16)["indexed_now"] == 1
    assert index.status()["failed"] == 0
    assert {record["id"] for record in index.search("healthy", limit=5)} == {
        poisoned["id"],
        healthy["id"],
    }
    assert poisoned["id"] != healthy["id"]


def test_truncation_is_bounded_and_reported(index):
    memory = save(index, "x", "truncated")
    index._chunks = lambda text: [(0, 1)] * (MAX_CHUNKS_PER_MEMORY + 3)

    result = index.index()
    assert result["indexed_now"] == 1
    status = index.status()
    assert status["truncated"] == 1 and status["state"] == "degraded"
    with index.store.connection() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM semantic_chunks WHERE memory_id=?", (memory["id"],)
            ).fetchone()[0]
            == MAX_CHUNKS_PER_MEMORY
        )


def test_corrupt_derived_vector_is_skipped_without_losing_fulltext(index):
    memory = save(index, "stable lexical fact", "corrupt")
    index.index()
    with index.store.connection(write=True) as db:
        db.execute(
            "UPDATE semantic_chunks SET vector=?,dimension=? WHERE memory_id=?",
            (b"bad", 2, memory["id"]),
        )

    assert index.search("unrelated semantic query") == []
    result = MemoryService(index.store, index).search(
        "lexical", policy=Policy(frozenset({"memory:read"}), ("alpha",))
    )
    assert result["semantic"] == "enabled"
    assert [record["id"] for record in result["results"]] == [memory["id"]]


def test_strong_query_excludes_semantic_noise(index):
    save(index, "gardening and tomatoes", "noise")
    index._embed = lambda text: index.np.array(
        [1.0, 0.0] if "quantum" in text else [0.0, 1.0], dtype="<f4"
    )
    index.index()

    results = MemoryService(index.store, index).search(
        "quantum lattice gauge theory",
        policy=Policy(frozenset({"memory:read"}), ("alpha",)),
    )
    assert results["results"] == []


def test_hybrid_result_prefix_and_tiny_context_are_consistent(index):
    first = save(index, "retrieval signal alpha", "alpha")
    save(index, "retrieval signal beta", "beta")
    index.index()
    service = MemoryService(index.store, index)
    policy = Policy(frozenset({"memory:read"}), ("alpha",))

    many = service.search("retrieval signal", policy=policy, limit=10)["results"]
    one = service.search("retrieval signal", policy=policy, limit=1)["results"]
    assert one == many[:1]
    assert len(many) == 2 and first["id"] in {record["id"] for record in many}

    context = service.context("retrieval signal", policy=policy, max_chars=256)
    assert context["characters"] == len(context["context"]) <= 256
    assert context["memories"] >= 1
