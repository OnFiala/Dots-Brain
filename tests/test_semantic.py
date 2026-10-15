import contextlib
import hashlib
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from dots_brain.auth import Policy
from dots_brain.semantic import SemanticIndex, verify_file
from dots_brain.service import MemoryService
from dots_brain.store import Store


def test_artifact_integrity_detects_modified_bytes(tmp_path):
    path = tmp_path / "artifact"
    path.write_bytes(b"a pinned artifact")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert verify_file(path, "sha256", digest)
    path.write_bytes(b"a different artifact")
    assert not verify_file(path, "sha256", digest)


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


def save(index, **overrides):
    return index.store.remember(
        **(
            dict(
                content="A useful fact", source="test", account="a", event_id="one", project="alpha"
            )
            | overrides
        )
    )


def test_semantic_excludes_stale_revisions_and_unauthorized_projects(index):
    memory = save(index)
    assert index.index()["indexed"] == 1
    assert index.search("fact", projects=("beta",)) == []
    assert index.search("fact", projects=("alpha",))[0]["id"] == memory["id"]
    save(index, content="An updated fact", expected_revision=1)
    assert index.status()["pending"] == 1
    assert index.search("fact") == []
    assert index.index()["indexed"] == 1
    assert index.search("fact")[0]["revision"] == 2
    index.store.forget(memory["id"], expected_revision=2)
    assert index.search("fact") == []


def test_forget_during_embedding_does_not_resurrect_memory(index):
    memory = save(index)
    original = index._embed

    def embed(text):
        index.store.forget(memory["id"], expected_revision=memory["revision"])
        return original(text)

    index._embed = embed
    assert index.index()["indexed"] == 0
    assert index.store.status()["memories"] == 0


def test_search_returns_the_matching_tail_passage(index):
    index._chunks = lambda text: (
        [(0, 700), (700, len(text))] if len(text) > 700 else [(0, len(text))]
    )
    index._embed = lambda text: index.np.array(
        [0.0, 1.0] if "peanuts" in text else [1.0, 0.0], dtype="<f4"
    )
    memory = save(index, content="Ordinary discussion. " * 60 + "Avoid peanuts.")
    index.index()
    hit = index.search("peanuts")[0]
    assert hit["id"] == memory["id"] and "peanuts" in hit["excerpt"]
    assert hit["passage"]["start_char"] == 700


@pytest.mark.parametrize("reverse_scan", [False, True])
def test_title_winner_keeps_best_body_passage_after_pruning(index, monkeypatch, reverse_scan):
    memories = sorted(
        (save(index, event_id=str(number)) for number in range(3)),
        key=lambda memory: memory["id"],
    )
    tail = "Best matching body."
    target_body = "P" * 700 + tail
    scores = {
        "Target title": 1.0,
        "P" * 700: 0.1,
        tail: 0.2,
        "First distractor": 0.8,
        "Second distractor": 0.7,
    }
    for memory, title, content in zip(
        memories,
        ("Target title", "First distractor", "Second distractor"),
        (target_body, "First distractor", "Second distractor"),
        strict=True,
    ):
        save(
            index,
            event_id=index.store.get(memory["id"])["event_id"],
            title=title,
            content=content,
            expected_revision=1,
        )
    index._chunks = lambda text: (
        [(0, 700), (700, len(text))] if text == target_body else [(0, len(text))]
    )

    def embed(text):
        score = 1.0 if text == "query" else scores[text]
        return index.np.array([score, math.sqrt(1 - score * score)], dtype="<f4")

    index._embed = embed
    assert index.index()["indexed_now"] == 3
    connection = index.store.connection

    @contextlib.contextmanager
    def configured_connection(*, write=False):
        with connection(write=write) as db:
            db.execute(f"PRAGMA reverse_unordered_selects={int(reverse_scan)}")
            yield db

    monkeypatch.setattr(index.store, "connection", configured_connection)
    hit = index.search("query", limit=1)[0]
    assert hit["id"] == memories[0]["id"]
    assert hit["semantic_score"] == 1.0 and hit["match_field"] == "title"
    assert hit["passage"] == {
        "field": "content",
        "start_char": 700,
        "end_char": len(target_body),
    }
    assert hit["excerpt"] == tail


def test_title_passage_revision_and_forget_remove_derived_chunks(index):
    index._embed = lambda text: index.np.array(
        [0.0, 1.0] if "allergy" in text else [1.0, 0.0], dtype="<f4"
    )
    memory = save(index, title="Food allergy", content="A related detail.")
    index.index()
    hit = index.search("allergy")[0]
    assert hit["passage"] == {"field": "content", "start_char": 0, "end_char": 17}
    assert hit["match_field"] == "title" and hit["semantic_score"] == 1
    assert hit["excerpt"] == "A related detail." and "content" not in hit
    save(index, title="Changed", content="A related detail.", expected_revision=1)
    assert index.search("allergy") == []
    index.index()
    with index.store.connection() as db:
        assert {row[0] for row in db.execute("SELECT revision FROM semantic_chunks")} == {2}
    index.store.forget(memory["id"], expected_revision=2)
    with index.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM semantic_chunks").fetchone()[0] == 0


@pytest.mark.parametrize("title", ["Food allergy", "Food allergy " + "x" * 240])
def test_context_uses_scoped_body_when_semantic_title_wins(index, title):
    index._embed = lambda text: index.np.array(
        [0.0, 1.0] if "allergy" in text else [1.0, 0.0], dtype="<f4"
    )
    memory = save(index, title=title, content="Avoid peanuts. " + "Detail. " * 200)
    save(index, event_id="private", project="beta", title="Food allergy", content="PRIVATE_CANARY")
    index.index()
    service = MemoryService(index.store, index)
    caller = Policy(frozenset({"memory:read"}), ("alpha",))
    assert service.search("allergy", policy=caller)["results"][0]["passage"]["field"] == "content"
    for budget in (256, 6000):
        result = service.context("allergy", policy=caller, max_chars=budget)
        assert "Avoid peanuts." in result["context"] and memory["id"] in result["context"]
        assert "Food allergy" in result["context"]
        assert "PRIVATE_CANARY" not in result["context"]
        assert result["characters"] == len(result["context"]) <= budget
        assert result["memories"] == 1
    assert len(result["context"]) < 1100  # The fallback remains an excerpt, not a full export.


@pytest.mark.parametrize("change", ["update", "delete"])
def test_title_context_never_mixes_revisions_or_restores_deleted_content(index, change):
    index._embed = lambda text: index.np.array(
        [0.0, 1.0] if "allergy" in text else [1.0, 0.0], dtype="<f4"
    )
    memory = save(index, title="Food allergy", content="Original detail.")
    index.index()
    search = index.search

    def search_then_change(*args, **kwargs):
        results = search(*args, **kwargs)
        if change == "update":
            save(index, content="NEW_REVISION_CANARY", expected_revision=1)
        else:
            index.store.forget(memory["id"], expected_revision=1)
        return results

    index.search = search_then_change
    result = MemoryService(index.store, index).context(
        "allergy", policy=Policy(frozenset({"memory:read"}), ("alpha",))
    )
    assert "NEW_REVISION_CANARY" not in result["context"]
    if change == "update":
        assert "Original detail." in result["context"] and '"revision": 1' in result["context"]
    else:
        assert result["memories"] == 0 and "Original detail." not in result["context"]


@pytest.mark.skipif(
    not os.environ.get("BRAIN_TEST_MODEL_DIR"),
    reason="Requires an explicitly prepared local model; never downloads in tests.",
)
def test_real_local_model_retrieves_english_memory_from_czech_question(tmp_path):
    from dots_brain.semantic import MODEL_REVISION

    store = Store(tmp_path / "memory")
    store.initialize()
    target = store.directory / "models" / MODEL_REVISION
    target.parent.mkdir()
    # Link the model directory only; individual artifacts are still verified regular files.
    target.symlink_to(Path(os.environ["BRAIN_TEST_MODEL_DIR"]))
    for event_id, content in (
        ("food", "The user avoids peanuts because of an allergy."),
        ("storage", "Store project memory in SQLite."),
        ("travel", "The trip to Prague starts next Monday."),
    ):
        store.remember(content=content, source="synthetic", account="test", event_id=event_id)
    model = SemanticIndex(store)
    assert model.tokenizer is model.model.model.tokenizer
    settings = (model.tokenizer.truncation, model.tokenizer.padding)
    assert model.index()["indexed"] == 3
    assert model.search("Kterému jídlu se mám vyhnout?", limit=1)[0]["event_id"] == "food"
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(model._chunks, "long text " * 300),
            pool.submit(model._embed, "A short fact."),
        ]
        chunks, vector = [future.result(timeout=10) for future in futures]
    assert len(chunks) > 1 and len(vector) > 0
    assert (model.tokenizer.truncation, model.tokenizer.padding) == settings

    class FailingTokenizer:
        def __getattr__(self, name):
            return getattr(model.model.model.tokenizer, name)

        def encode(self, *args, **kwargs):
            raise ValueError("Synthetic tokenizer failure")

    model.tokenizer = FailingTokenizer()
    with pytest.raises(ValueError):
        model._chunks("A failing passage")
    assert (model.tokenizer.truncation, model.tokenizer.padding) == settings
    model.tokenizer = model.model.model.tokenizer

    from starlette.testclient import TestClient

    from dots_brain.server import create_http_app, create_server
    from dots_brain.service import MemoryService

    store.remember(
        content="A new decision to index in the background.",
        source="synthetic",
        account="test",
        event_id="background",
    )
    service = MemoryService(store, model)
    server = create_server(service, http=True)
    with TestClient(create_http_app(server, service)):
        deadline = time.monotonic() + 10
        while model.status()["pending"]:
            assert time.monotonic() < deadline, "Background indexing did not complete."
            time.sleep(0.05)
    assert model.status()["indexed"] == 4


def test_semantic_only_title_match_returns_current_body_and_source(index):
    index._embed = lambda text: index.np.array(
        [0.0, 1.0] if text in {"Hypersensitivity", "allergic reaction"} else [1.0, 0.0], dtype="<f4"
    )
    saved = save(
        index, title="Hypersensitivity", content="Avoid hazelnuts.", source_uri="fixture://allergy"
    )
    assert index.store.search("allergic reaction") == []
    index.index()
    result = MemoryService(index.store, index).search(
        "allergic reaction", policy=Policy(frozenset({"memory:read"}))
    )
    record = result["results"][0]
    assert record["id"] == saved["id"] and record["revision"] == 1
    assert record["excerpt"] == "Avoid hazelnuts." and record["source_uri"] == "fixture://allergy"


def test_hybrid_title_winner_preserves_fulltext_tail_body(index):
    index._embed = lambda text: index.np.array(
        [0.0, 1.0] if text == "Target" else [1.0, 0.0], dtype="<f4"
    )
    saved = save(
        index, title="Target", content="Ordinary filler. " * 100 + "Target fact is at the tail."
    )
    index.index()
    result = MemoryService(index.store, index).search(
        "Target", policy=Policy(frozenset({"memory:read"}))
    )
    record = result["results"][0]
    assert record["id"] == saved["id"] and record["match_field"] == "title"
    assert "Target fact is at the tail." in record["excerpt"]
    assert record["passage"] == {"field": "content"}  # FTS snippets do not claim character offsets.


def test_index_reports_this_call_and_removes_other_models(index):
    first = save(index)
    index.index()
    second = save(index, event_id="two")
    with index.store.connection(write=True) as db:
        db.execute(
            "INSERT INTO semantic_chunks SELECT ?,1,'old-model',chunk_index,field,"
            "start_char,end_char,dimension,vector FROM semantic_chunks WHERE memory_id=?",
            (second["id"], first["id"]),
        )
    result = index.index(batch_size=1)
    assert result["indexed_now"] == 1 and result["indexed"] == 2 and result["examined"] == 1
    assert index.index()["indexed_now"] == 0
    with index.store.connection() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM semantic_chunks WHERE model='old-model'").fetchone()[0]
            == 0
        )


def test_index_command_reports_only_newly_indexed_memories(index, monkeypatch):
    from dots_brain.cli import parser, run

    save(index)
    monkeypatch.setattr("dots_brain.semantic.SemanticIndex", lambda store: index)
    args = parser().parse_args(["--data-dir", str(index.store.directory), "index"])
    assert run(args)["indexed_this_run"] == 1
    assert run(args)["indexed_this_run"] == 0
    save(index, event_id="second")
    assert run(args)["indexed_this_run"] == 1


def test_disable_during_embedding_refuses_derived_write(index):
    from dots_brain.errors import StoreDisabledError

    save(index)
    embed = index._embed

    def disable(text):
        (index.store.directory / "disabled.json").write_text("{}")
        return embed(text)

    index._embed = disable
    with pytest.raises(StoreDisabledError):
        index.index()
    with index.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM semantic_chunks").fetchone()[0] == 0


def test_semantic_failure_falls_back_without_echoing_error(index, caplog):
    save(index, content="Remember the porcelain tray.")

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic-secret-canary")

    index.search = fail
    result = MemoryService(index.store, index).search(
        "porcelain", policy=Policy(frozenset({"memory:read"}))
    )
    assert result["mode"] == "fulltext" and result["semantic"] == "degraded"
    assert result["results"][0]["excerpt"] == "Remember the porcelain tray."
    assert "synthetic-secret-canary" not in caplog.text
