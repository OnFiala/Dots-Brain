import hashlib
import os
import time
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


def test_title_passage_revision_and_forget_remove_derived_chunks(index):
    index._embed = lambda text: index.np.array(
        [0.0, 1.0] if "allergy" in text else [1.0, 0.0], dtype="<f4"
    )
    memory = save(index, title="Food allergy", content="A related detail.")
    index.index()
    hit = index.search("allergy")[0]
    assert hit["passage"] == {"field": "title", "start_char": 0, "end_char": 12}
    assert hit["excerpt"] == "Food allergy" and "content" not in hit
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
    assert service.search("allergy", policy=caller)["results"][0]["passage"]["field"] == "title"
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
    assert model.index()["indexed"] == 3
    assert model.search("Kterému jídlu se mám vyhnout?", limit=1)[0]["event_id"] == "food"

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
