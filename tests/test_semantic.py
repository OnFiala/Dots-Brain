import hashlib
import os
import time
from pathlib import Path

import pytest

from dots_brain.semantic import SemanticIndex, verify_file
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
    index.store.forget(memory["id"])
    assert index.search("fact") == []


def test_forget_during_embedding_does_not_resurrect_memory(index):
    memory = save(index)
    original = index._embed

    def embed(text):
        index.store.forget(memory["id"])
        return original(text)

    index._embed = embed
    assert index.index()["indexed"] == 0
    assert index.store.status()["memories"] == 0


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
