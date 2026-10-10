import hashlib
import json
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
    result.vector_dimension = 2
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
    assert index.search("allergy", projects=caller.projects)[0]["passage"]["field"] == "title"
    assert "Avoid peanuts." in service.search("allergy", policy=caller)["results"][0]["excerpt"]
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
    truncation = dict(model.tokenizer.truncation)
    padding = dict(model.tokenizer.padding)
    model._chunks("token " * 300)
    assert model.tokenizer.truncation == truncation
    assert model.tokenizer.padding == padding
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


@pytest.mark.skipif(
    not os.environ.get("BRAIN_TEST_MODEL_DIR"),
    reason="Requires an explicitly prepared local model; never downloads in tests.",
)
def test_real_local_model_calibration_corpus_has_measured_recall_and_ood_bound(tmp_path):
    """Pinned 24-fact Czech/English regression corpus; 11 forms × 24 facts."""
    from dots_brain.semantic import MODEL_REVISION

    corpus = json.loads(
        (Path(__file__).parent / "fixtures" / "semantic_calibration.json").read_text(
            encoding="utf-8"
        )
    )
    store = Store(tmp_path / "memory")
    store.initialize()
    target = store.directory / "models" / MODEL_REVISION
    target.parent.mkdir()
    target.symlink_to(Path(os.environ["BRAIN_TEST_MODEL_DIR"]))
    filler = corpus["filler"]
    projects = {
        "en": lambda fact: fact[0],
        "cz": lambda fact: fact[1],
        "en_long": lambda fact: filler["en"] + fact[0] + " " + filler["en2"],
        "cz_long": lambda fact: filler["cz"] + fact[1] + " " + filler["cz2"],
    }
    expected = {}
    for project, content_for in projects.items():
        for number, fact in enumerate(corpus["facts"]):
            record = store.remember(
                content=content_for(fact),
                source="calibration",
                account="test",
                event_id=f"{project}-{number}",
                project=project,
            )
            expected[project, number] = record["id"]
    model = SemanticIndex(store)
    while model.index(batch_size=128, summary=False)["examined"]:
        pass

    def long_en(question):
        return (
            "We are in the middle of a longer working session and I need to recall something "
            "before I continue with the task. " + question + " Please give me everything you "
            "remember about this so that I can decide the next step."
        )

    def long_cz(question):
        return (
            "Jsme uprostřed delší pracovní relace a potřebuji si něco připomenout, než budu "
            "pokračovat v úkolu. " + question + " Dej mi prosím vše, co si o tom pamatuješ, "
            "abych se mohl rozhodnout, jak dál."
        )

    query_forms = (
        ("en", lambda fact: fact[3]),
        ("cz", lambda fact: fact[2]),
        ("en", lambda fact: fact[5]),
        ("cz", lambda fact: fact[4]),
        ("en", lambda fact: fact[7]),
        ("cz", lambda fact: fact[6]),
        ("en", lambda fact: long_cz(fact[3])),
        ("cz", lambda fact: long_en(fact[2])),
        ("en_long", lambda fact: fact[3]),
        ("cz_long", lambda fact: fact[2]),
        ("en_long", lambda fact: fact[7]),
    )
    relevant = 0
    for project, query_for in query_forms:
        for number, fact in enumerate(corpus["facts"]):
            ids = {
                record["id"] for record in model.search(query_for(fact), project=project, limit=10)
            }
            relevant += expected[project, number] in ids
    ood_candidates = sum(bool(model.search(query, limit=1)) for query in corpus["ood"])
    assert relevant >= 231, f"recall@10={relevant}/264"
    assert ood_candidates <= 14, f"OOD candidates={ood_candidates}/24"
