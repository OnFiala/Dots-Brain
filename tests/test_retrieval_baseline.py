"""Independent metric examples and functional guards; model execution belongs to CI."""

import importlib.util
import json
from pathlib import Path

import pytest

from dots_brain.service import MemoryService
from dots_brain.store import Store

spec = importlib.util.spec_from_file_location(
    "retrieval_baseline", Path(__file__).resolve().parents[1] / "scripts/retrieval_baseline.py"
)
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)


@pytest.mark.parametrize(
    "returned,relevant,recall,rr,unexpected",
    [
        (["other", "a", "b"], ["a", "b"], 1.0, 0.5, 1),
        (["a", "b"], ["a", "b", "c"], 2 / 3, 1.0, 0),
        ([], ["a"], 0.0, 0.0, 0),
        (["x", "y", "z", "a"], ["a"], 0.0, 0.0, 3),
        (["x"], [], None, None, 1),
        ([], [], None, None, 0),
    ],
)
def test_declared_ranking_metric_examples(returned, relevant, recall, rr, unexpected):
    assert baseline.quality(returned, relevant) == {
        "recall_at_3": recall,
        "reciprocal_rank_at_3": rr,
        "unexpected_results": unexpected,
    }


@pytest.fixture
def corpus_store(tmp_path):
    corpus = baseline.load_corpus()
    store = Store(tmp_path / "memory")
    store.initialize()
    baseline.seed_store(store, corpus)
    return corpus, store


def test_fulltext_measurement_reports_misses_and_negative_results(corpus_store):
    corpus, store = corpus_store
    result = baseline.measure(MemoryService(store), corpus, "fulltext")
    rows = {row["id"]: row for row in result["queries"]}
    assert rows["en-storage"]["returned"][0] == "storage"
    assert rows["cz-architecture-inflection"]["recall_at_3"] == 0
    assert rows["excluded-project"]["returned"] == []
    assert result["summary"]["positive_queries"] == 13
    assert result["summary"]["negative_queries"] == 3
    assert all(not key.startswith("workshop-") for row in rows.values() for key in row["returned"])


def test_semantic_fallback_cannot_produce_a_hybrid_baseline(corpus_store):
    corpus, store = corpus_store
    with pytest.raises(RuntimeError, match="mode degraded"):
        baseline.measure(MemoryService(store), corpus, "hybrid")


def test_measurement_fails_on_project_leakage(corpus_store, monkeypatch):
    corpus, store = corpus_store
    service = MemoryService(store)
    excluded = store.search("azimuth")[0]
    monkeypatch.setattr(
        service,
        "search",
        lambda *args, **kwargs: {"mode": "fulltext", "semantic": "disabled", "results": [excluded]},
    )
    with pytest.raises(RuntimeError, match="project boundary"):
        baseline.measure(service, corpus, "fulltext")


def test_negative_queries_are_not_counted_as_successful_positive_retrieval():
    rows = [
        {"returned": ["wrong"], "elapsed_ms": 2, **baseline.quality(["wrong"], [])},
        {"returned": ["a"], "elapsed_ms": 4, **baseline.quality(["a"], ["a"])},
    ]
    result = baseline.summarize(rows)
    assert result["mean_recall_at_3"] == 1 and result["positive_queries"] == 1
    assert result["negative_queries"] == result["negative_queries_with_results"] == 1
    assert result["median_query_ms"] == 3 and result["max_query_ms"] == 4


def test_corpus_judgments_cannot_reference_an_excluded_project(tmp_path):
    corpus = baseline.load_corpus()
    corpus["queries"][0]["relevant"] = ["workshop-storage"]
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(corpus))
    with pytest.raises(ValueError, match="visible corpus records"):
        baseline.load_corpus(invalid)


def test_fresh_fixture_stores_have_identical_ids_and_tied_rankings(tmp_path):
    corpus = {
        "version": "tie-control",
        "records": [
            {"event_id": key, "project": "garden", "title": "Same", "content": "Identical wording"}
            for key in ("first", "second", "third")
        ],
    }
    results = []
    for name in ("one", "two"):
        store = Store(tmp_path / name)
        store.initialize()
        baseline.seed_store(store, corpus)
        results.append([(hit["event_id"], hit["id"]) for hit in store.search("Identical")])
    assert len(results[0]) == 3 and results[0] == results[1]
