"""Regression coverage for bounded, failure-tolerant semantic retrieval."""

import threading
from types import SimpleNamespace

import pytest

from dots_brain.auth import Policy
from dots_brain.errors import StoreDisabledError
from dots_brain.semantic import MAX_CHUNKS_PER_MEMORY, MODEL_ID, SemanticIndex
from dots_brain.service import MemoryService
from dots_brain.store import Store


@pytest.fixture
def index(tmp_path):
    np = pytest.importorskip("numpy")
    store = Store(tmp_path / "memory")
    store.initialize()
    result = SemanticIndex.__new__(SemanticIndex)
    result.store, result.np = store, np
    result.vector_dimension = 2
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


def test_corrupt_vector_population_emits_one_summary_per_search(index, caplog):
    save(index, "lexical fact", "many-corrupt-chunks")
    index._chunks = lambda text: [(0, 1)] * MAX_CHUNKS_PER_MEMORY
    index.index()
    with index.store.connection(write=True) as db:
        db.execute("UPDATE semantic_chunks SET vector=?", (b"bad",))

    assert index.search("query") == []
    warnings = [record for record in caplog.records if record.name == "dots_brain.semantic"]
    assert len(warnings) == 1
    assert warnings[0].getMessage() == (
        f"Skipped {MAX_CHUNKS_PER_MEMORY} invalid semantic vectors in this search"
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


def test_long_task_keeps_semantic_query_and_reports_fulltext_term_limit(index):
    memory = save(index, "A deployment decision about the production gateway.", "long-query")
    index.index()
    query = " ".join(f"word{number}" for number in range(60)) + " deployment gateway"
    caller = Policy(frozenset({"memory:read"}), ("alpha",))
    service = MemoryService(index.store, index)
    result = service.context(query, policy=caller)
    assert result["fulltext_query_limited"] is True
    assert memory["id"] in result["context"]
    assert result["memories"] == 1
    assert (
        service.search("Gateway GATEWAY gateway", policy=caller)["fulltext_query_limited"] is False
    )


def test_hybrid_keeps_the_semantic_body_passage_in_a_long_note(index):
    background = "Deployment gateway background without the answer. " * 30
    answer = "Endpoint value: 8443."
    memory = save(index, background + answer, "deep-answer")
    index._chunks = lambda text: (
        [(0, len(background)), (len(background), len(text))]
        if len(text) > len(background)
        else [(0, len(text))]
    )
    index._embed = lambda text: index.np.array(
        [0.0, 1.0] if "8443" in text or "Which port" in text else [1.0, 0.0], dtype="<f4"
    )
    index.index()
    service = MemoryService(index.store, index)
    caller = Policy(frozenset({"memory:read"}), ("alpha",))
    query = "Which port is used for the deployment gateway?"
    lexical = index.store.search(query)[0]
    assert "8443" not in lexical["excerpt"]
    hybrid = service.search(query, policy=caller)["results"][0]
    assert hybrid["id"] == memory["id"]
    assert hybrid["passage"]["start_char"] == len(background)
    assert "8443" in hybrid["excerpt"]
    assert hybrid["semantic_score"] == 1.0
    assert "8443" in service.context(query, policy=caller)["context"]


def test_context_reports_omitted_and_clipped_results(index):
    for number in range(3):
        save(index, "deployment " * 100, f"budget-{number}")
    index.index()
    result = MemoryService(index.store, index).context(
        "deployment", policy=Policy(frozenset({"memory:read"}), ("alpha",)), max_chars=256
    )
    assert result["characters"] == len(result["context"]) <= 256
    assert result["memories"] == 1
    assert result["omitted_memories"] == 2
    assert result["truncated_excerpts"] == 1


def test_failed_embedding_retries_only_after_its_bounded_deadline(index):
    memory = save(index, "transient embedding failure", "retry")
    original = index._embed
    attempts = 0

    def failing_embed(text):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("synthetic transient failure")

    index._embed = failing_embed
    assert index.index()["failed"] == 1
    assert attempts == 1

    index._embed = original
    assert index.index()["examined"] == 0
    assert attempts == 1
    index._failed_retry_deadlines[(memory["id"], memory["revision"])] = 0
    assert index.index()["indexed_now"] == 1
    assert index.status()["failed"] == 0


@pytest.mark.parametrize("repair", ["missing", "corrupt", "wrong_dimension", "nan"])
def test_missing_or_corrupt_vectors_are_pending_until_reindexed(index, repair):
    memory = save(index, "repairable derived vector", f"repair-{repair}")
    assert index.index()["indexed_now"] == 1
    with index.store.connection(write=True) as db:
        if repair == "missing":
            db.execute("DELETE FROM semantic_chunks WHERE memory_id=?", (memory["id"],))
        elif repair == "corrupt":
            db.execute(
                "UPDATE semantic_chunks SET vector=? WHERE memory_id=?", (b"bad", memory["id"])
            )
        elif repair == "wrong_dimension":
            db.execute(
                "UPDATE semantic_chunks SET vector=?,dimension=? WHERE memory_id=?",
                (index.np.array([1.0, 0.0, 0.0], dtype="<f4").tobytes(), 3, memory["id"]),
            )
        else:
            db.execute(
                "UPDATE semantic_chunks SET vector=? WHERE memory_id=?",
                (index.np.array([index.np.nan, 0.0], dtype="<f4").tobytes(), memory["id"]),
            )

    degraded = index.status()
    assert degraded["state"] == "pending_index"
    assert degraded["indexed"] == 0
    assert degraded["repair_pending"] == degraded["pending"] == 1
    assert index.index()["indexed_now"] == 1
    repaired = index.status()
    assert repaired["state"] == "ready"
    assert repaired["indexed"] == 1
    assert repaired["repair_pending"] == 0


def test_disabled_store_prevents_semantic_index_and_retry_writes(index):
    memory = save(index, "pending while disabled", "disabled")
    before = index.store.path.read_bytes()
    (index.store.directory / "disabled.json").write_text('{"disabled":true}', encoding="utf-8")

    with pytest.raises(StoreDisabledError):
        index.index()
    with pytest.raises(StoreDisabledError):
        index.retry_failed()

    assert index.store.path.read_bytes() == before
    with index.store.connection() as db:
        chunks = db.execute(
            "SELECT COUNT(*) FROM semantic_chunks WHERE memory_id=?", (memory["id"],)
        ).fetchone()[0]
        assert chunks == 0


def test_disabled_store_recheck_after_embedding_prevents_derived_writes(index):
    memory = save(index, "disable after embedding", "disable-after-embedding")
    original = index._embed

    def disable_then_embed(text):
        (index.store.directory / "disabled.json").write_text('{"disabled":true}', encoding="utf-8")
        return original(text)

    index._embed = disable_then_embed
    with pytest.raises(StoreDisabledError):
        index.index()
    with index.store.connection() as db:
        chunks = db.execute(
            "SELECT COUNT(*) FROM semantic_chunks WHERE memory_id=?", (memory["id"],)
        ).fetchone()[0]
        state = db.execute(
            "SELECT COUNT(*) FROM semantic_state WHERE memory_id=?", (memory["id"],)
        ).fetchone()[0]
        assert chunks == state == 0


def test_title_score_ranks_memory_while_best_body_passage_is_returned(index):
    body = "The deep body answer is 8443."
    memory = save(index, body, "title-and-body", title="Production gateway")
    index._embed = lambda text: index.np.array(
        [1.0, 0.0]
        if text == "Production gateway" or text.startswith("Which port")
        else [0.9, (1 - 0.9**2) ** 0.5],
        dtype="<f4",
    )
    index.index()
    result = index.search("Which port serves the production gateway?", limit=1)[0]
    assert result["id"] == memory["id"]
    assert result["semantic_score"] == 1.0
    assert result["passage"]["field"] == "content"
    assert result["excerpt"] == body
    context = MemoryService(index.store, index).context(
        "Which port serves the production gateway?",
        policy=Policy(frozenset({"memory:read"}), ("alpha",)),
    )
    assert body in context["context"]


def test_title_only_semantic_hit_keeps_score_with_fulltext_body_excerpt(index):
    background = "Unrelated cooking notes without the answer. " * 40
    memory = save(
        index, background + "Gateway endpoint port: 8443.", "title-only-fulltext", title="gateway"
    )
    index._embed = lambda text: index.np.array(
        [1.0, 0.0] if text == "gateway" else [0.0, 1.0], dtype="<f4"
    )
    index.index()
    semantic = index.search("gateway", limit=1)[0]
    assert semantic["passage"]["field"] == "title"
    service = MemoryService(index.store, index)
    caller = Policy(frozenset({"memory:read"}), ("alpha",))

    result = service.search("gateway", policy=caller, limit=1)["results"][0]
    assert result["id"] == memory["id"]
    assert result["semantic_score"] == 1.0
    assert result["passage"] == {"field": "content", "source": "fulltext"}
    assert "8443" in result["excerpt"]
    assert "8443" in service.context("gateway", policy=caller)["context"]


def test_hybrid_does_not_attach_new_semantic_metadata_to_an_old_fulltext_revision(
    index, monkeypatch
):
    memory = save(index, "First body revision.", "hybrid-revision-race", title="gateway")
    stale_fulltext = index.store.search("gateway")
    assert stale_fulltext[0]["revision"] == 1
    save(
        index,
        "Second body revision.",
        "hybrid-revision-race",
        title="gateway",
        expected_revision=1,
    )
    index._embed = lambda text: index.np.array(
        [1.0, 0.0] if text == "gateway" else [0.0, 1.0], dtype="<f4"
    )
    index.index()
    monkeypatch.setattr(index.store, "search", lambda *args, **kwargs: stale_fulltext)

    result = MemoryService(index.store, index).search(
        "gateway", policy=Policy(frozenset({"memory:read"}), ("alpha",))
    )["results"][0]
    assert result["id"] == memory["id"]
    assert result["revision"] == 2
    assert result["semantic_score"] == 1.0
    assert result["passage"]["field"] == "title"
    assert result["excerpt"] == "gateway"


def test_duplicate_fulltext_rows_without_semantic_hit_do_not_require_semantic_metadata(index):
    memory = save(index, "Gateway endpoint details.", "duplicate-fulltext")
    index._embed = lambda text: index.np.array(
        [1.0, 0.0] if text == "gateway" else [0.0, 1.0], dtype="<f4"
    )
    index.index()
    with index.store.connection(write=True) as db:
        db.execute(
            "INSERT INTO memory_fts(memory_id,title,content) VALUES (?,?,?)",
            (memory["id"], "", "Gateway endpoint details."),
        )
    assert len(index.store.search("gateway")) == 2
    assert index.search("gateway") == []

    results = MemoryService(index.store, index).search(
        "gateway", policy=Policy(frozenset({"memory:read"}), ("alpha",))
    )["results"]
    assert len(results) == 1
    assert results[0]["id"] == memory["id"]
    assert results[0]["revision"] == 1
    assert "semantic_score" not in results[0]
    assert "passage" not in results[0]


def test_background_index_summary_avoids_explicit_status_scan(index, monkeypatch):
    monkeypatch.setattr(index, "status", lambda: pytest.fail("background index called status"))
    assert index.index(summary=False) == {"indexed_now": 0, "examined": 0}


def test_background_index_detects_same_cardinality_revision_one_replacement(index, monkeypatch):
    first = save(index, "first source record", "same-cardinality-first")
    assert index.index(summary=False) == {"indexed_now": 1, "examined": 1}
    index.store.forget(first["id"], expected_revision=1)
    replacement = save(index, "replacement source record", "same-cardinality-replacement")
    monkeypatch.setattr(index, "status", lambda: pytest.fail("background index called status"))
    assert index.index(summary=False) == {"indexed_now": 1, "examined": 1}
    assert index.search("replacement", limit=1)[0]["id"] == replacement["id"]


@pytest.mark.parametrize("change", ["revision", "deletion"])
def test_stale_failed_retry_deadlines_are_removed_for_revised_or_deleted_memories(index, change):
    memory = save(index, "will fail then change", f"deadline-{change}")
    index._embed = lambda text: (_ for _ in ()).throw(RuntimeError("synthetic failure"))
    assert index.index()["failed"] == 1
    key = (memory["id"], 1)
    assert key in index._failed_retry_deadlines
    if change == "revision":
        save(index, "revised after failure", f"deadline-{change}", expected_revision=1)
    else:
        index.store.forget(memory["id"], expected_revision=1)
    index._embed = lambda text: index.np.array([1.0, 0.0], dtype="<f4")
    index.index(summary=False)
    assert key not in index._failed_retry_deadlines


def test_due_failed_record_beyond_discovery_limit_does_not_starve(index):
    records = [save(index, f"failed record {number}", f"failed-{number}") for number in range(5)]
    pending = save(index, "new source has priority", "new-pending")
    with index.store.connection(write=True) as db:
        for record in records:
            db.execute(
                "INSERT INTO semantic_state(memory_id,model,revision,state,truncated) "
                "VALUES (?,?,?,?,0)",
                (record["id"], MODEL_ID, 1, "failed"),
            )
    index._failed_retry_deadlines = {
        (record["id"], 1): (0 if record is records[-1] else float("inf")) for record in records
    }
    assert index.index(batch_size=1, summary=False) == {"indexed_now": 1, "examined": 1}
    assert index.search("new source", limit=1)[0]["id"] == pending["id"]
    # A bounded rotating pass must eventually reach a due row beyond its first
    # discovery page, even when earlier rows are deferred indefinitely.
    retried = sum(
        index.index(batch_size=1, summary=False)["indexed_now"] for _ in range(len(records))
    )
    assert retried == 1
    with index.store.connection() as db:
        assert (
            db.execute(
                "SELECT state FROM semantic_state WHERE memory_id=? AND model=?",
                (records[-1]["id"], MODEL_ID),
            ).fetchone()[0]
            == "indexed"
        )


def test_failed_deadline_cleanup_has_a_bounded_query_count(index):
    deadlines = {(f"deleted-{number}", 1): 0 for number in range(2_000)}
    statements = []
    with index.store.connection() as db:
        db.set_trace_callback(statements.append)
        assert index._failed_candidates(db, deadlines, now=1, limit=4) == []
    selects = [statement for statement in statements if statement.lstrip().startswith("SELECT")]
    assert len(selects) <= 6
    assert len(deadlines) == 1_996


@pytest.mark.parametrize("vector", [[1.0, 0.0, 0.0], [[1.0], [0.0]]])
def test_wrong_model_dimension_is_failed_before_vector_publication(index, vector):
    memory = save(index, "A fact awaiting an embedding", "wrong-model-dimension")
    del index._embed  # Exercise the production embedding method.
    index.lock = threading.RLock()
    index._encode_for_chunks = lambda *args, **kwargs: SimpleNamespace(ids=[1])
    index.model = SimpleNamespace(embed=lambda texts: [index.np.array(vector, dtype="<f4")])
    result = index.index()
    assert result["indexed_now"] == result["repair_pending"] == 0
    assert result["failed"] == 1
    assert index.index(summary=False) == {"indexed_now": 0, "examined": 0}
    with index.store.connection() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM semantic_chunks WHERE memory_id=?", (memory["id"],)
            ).fetchone()[0]
            == 0
        )
