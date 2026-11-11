"""Independent metric examples and functional guards; model execution belongs to CI."""

import copy
import importlib.util
import json
import uuid
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


@pytest.fixture
def v2_corpus():
    records = [
        {
            "event_id": "current",
            "project": "garden",
            "title": "Current valve",
            "content": "Valve calibrates cedar pressure today.",
            "topic": "valve",
            "language": "en",
            "previous": [{"title": "Old valve", "content": "Obsolete copper setting yesterday."}],
        },
        {
            "event_id": "alternative",
            "project": "garden",
            "title": "Current duplicate authority",
            "content": "Cedar pressure uses the calibrated valve.",
            "topic": "valve",
            "language": "en",
        },
        {
            "event_id": "deleted",
            "project": "garden",
            "title": "Removed valve",
            "content": "Deleted cedar pressure valve.",
            "topic": "retired",
            "language": "en",
            "deleted": True,
        },
        {
            "event_id": "private",
            "project": "workshop",
            "title": "Private valve",
            "content": "Secret cedar pressure valve.",
            "topic": "private",
            "language": "en",
        },
    ]
    queries = [
        {
            "id": f"positive-{i}",
            "topic": "valve",
            "category": "paraphrase",
            "language": "cs",
            "cross_language": True,
            "text": "cedar valve",
            "relevant": ["current", "alternative"],
        }
        for i in range(100)
    ] + [
        {
            "id": f"negative-{i}",
            "topic": "absent",
            "category": "no_answer",
            "language": "en",
            "cross_language": False,
            "text": "unobtainium",
            "relevant": [],
        }
        for i in range(25)
    ]
    return {
        "version": "test-v2",
        "split": "development",
        "fixture_id_seeds": [11, 29, 47],
        "scope": ["garden"],
        "description": "Fictional test fixture",
        "records": records,
        "queries": queries,
    }


def test_v2_denominators_keep_alternatives_negatives_and_cross_language_separate(
    v2_corpus, tmp_path
):
    store = Store(tmp_path / "memory")
    store.initialize()
    baseline.seed_store(store, v2_corpus, 11)
    result = baseline.measure(MemoryService(store), v2_corpus, "fulltext", seed=11)
    summary = result["summary"]
    assert summary["positive_queries"] == 100 and summary["negative_queries"] == 25
    assert summary["hit_at_3"] == summary["mean_recall_at_3"] == 1
    assert summary["no_answer_false_positive_rate"] == 0
    assert summary["by_topic_counts"] == {"valve": 100, "absent": 25}
    assert result["cross_language"]["queries"] == 100
    assert result["cross_language"]["negative_queries"] == 0
    assert result["by_category"]["no_answer"]["hit_at_3"] is None
    assert summary["max_query_ms"] >= summary["p95_query_ms"] >= summary["median_query_ms"]
    rows = [
        {
            "topic": "one",
            "returned": ["alternative"],
            "elapsed_ms": 1,
            **baseline.quality(["alternative"], ["current", "alternative"]),
        },
        {
            "topic": "absent",
            "returned": ["wrong"],
            "elapsed_ms": 2,
            **baseline.quality(["wrong"], []),
        },
    ]
    summary = baseline.summarize(rows, v2=True)
    assert summary["hit_at_3"] == 1 and summary["mean_recall_at_3"] == 0.5
    assert summary["no_answer_false_positive_rate"] == 1


def test_v2_three_seeds_repeat_exact_ids_and_apply_real_history_and_forget(v2_corpus, tmp_path):
    identities = []
    for seed in (11, 29, 47):
        repeated = []
        for repeat in range(2):
            store = Store(tmp_path / f"{seed}-{repeat}")
            store.initialize()
            baseline.seed_store(store, v2_corpus, seed, initial=True)
            key = str(baseline.fixture_id(v2_corpus, v2_corpus["records"][0], seed))
            assert store.get(key)["revision"] == 1
            baseline.apply_changes(store, v2_corpus, seed)
            baseline.verify_fixture(store, v2_corpus, seed)
            assert store.get(key)["revision"] == 2
            assert store.get(key, revision=1)["content"].startswith("Obsolete")
            repeated.append([(h["id"], h["revision"]) for h in store.search("cedar")])
        assert repeated[0] == repeated[1]
        identities.append({key for key, _ in repeated[0]})
    assert all(not a & b for i, a in enumerate(identities) for b in identities[i + 1 :])


@pytest.mark.parametrize(
    "failure",
    [
        "stale",
        "deleted",
        "unknown",
        "leak",
        "duplicate",
        "excessive",
        "title",
        "source",
        "account",
        "uuid",
        "body",
        "offset",
        "excerpt",
        "snippet",
    ],
)
def test_v2_oracle_rejects_bad_results_without_mutating_database(
    v2_corpus, tmp_path, monkeypatch, failure
):
    store = Store(tmp_path / "memory")
    store.initialize()
    baseline.seed_store(store, v2_corpus, 11)
    service = MemoryService(store)
    current = next(h for h in store.search("cedar") if h["event_id"] == "current")
    hit = copy.deepcopy(current)
    results = [hit]
    if failure == "stale":
        hit.update(revision=1, title="Old valve", excerpt="Obsolete copper setting yesterday.")
    elif failure == "deleted":
        hit["event_id"] = "deleted"
        hit["id"] = str(baseline.fixture_id(v2_corpus, v2_corpus["records"][2], 11))
    elif failure == "unknown":
        hit["event_id"] = "unknown"
    elif failure == "leak":
        hit = next(h for h in store.search("cedar") if h["project"] == "workshop")
        results = [hit]
    elif failure == "duplicate":
        results = [hit, hit]
    elif failure == "excessive":
        results = [dict(hit, event_id=str(i)) for i in range(4)]
    elif failure in ("title", "source", "account"):
        hit[failure] = "corrupted"
    elif failure == "uuid":
        hit["id"] = str(uuid.uuid4())
    elif failure == "body":
        hit["content"] = "Corrupted body"
    elif failure in ("offset", "excerpt"):
        hit["passage"] = {"field": "content", "start_char": 0, "end_char": 5}
        hit["excerpt"] = "Valve" if failure == "offset" else "WRONG"
        if failure == "offset":
            hit["passage"]["end_char"] = 999
    elif failure == "snippet":
        hit["excerpt"] = "fabricated text"
    monkeypatch.setattr(
        service,
        "search",
        lambda *a, **kw: {"mode": "fulltext", "semantic": "disabled", "results": results},
    )
    with pytest.raises(RuntimeError):
        baseline.measure(service, v2_corpus, "fulltext", seed=11)


def test_pending_probe_reports_invariants_without_quality_for_true_absence(
    v2_corpus, tmp_path, monkeypatch
):
    store = Store(tmp_path / "memory")
    store.initialize()
    baseline.seed_store(store, v2_corpus, 11)
    service = MemoryService(store)
    monkeypatch.setattr(
        service, "search", lambda *a, **kw: {"mode": "hybrid", "semantic": "enabled", "results": []}
    )
    result = baseline.measure(service, v2_corpus, "hybrid", score=False, seed=11)
    assert result == {"state": "invariants_only", "queries_checked": 125, "quality_measured": False}
    ready = baseline.measure(service, v2_corpus, "hybrid", seed=11)
    assert ready["summary"]["hit_at_3"] == 0


@pytest.mark.parametrize(
    "failure",
    [
        "seeds",
        "positive_count",
        "negative_count",
        "duplicate_source",
        "duplicate_query",
        "deleted_label",
        "unknown_label",
        "topic",
        "cross_language",
        "identical_revision",
    ],
)
def test_v2_fixture_integrity_failures(v2_corpus, tmp_path, failure):
    if failure == "seeds":
        v2_corpus["fixture_id_seeds"] = [11, 29, 48]
    elif failure == "positive_count":
        del v2_corpus["queries"][0]
    elif failure == "negative_count":
        v2_corpus["queries"].pop()
    elif failure == "duplicate_source":
        v2_corpus["records"].append(copy.deepcopy(v2_corpus["records"][0]))
    elif failure == "duplicate_query":
        v2_corpus["queries"].append(copy.deepcopy(v2_corpus["queries"][0]))
    elif failure in ("deleted_label", "unknown_label"):
        v2_corpus["queries"][0]["relevant"] = [failure.removesuffix("_label")]
    elif failure == "topic":
        v2_corpus["queries"][0]["topic"] = "wrong"
    elif failure == "cross_language":
        v2_corpus["queries"][0]["language"] = "en"
    elif failure == "identical_revision":
        row = v2_corpus["records"][0]
        row["previous"] = [{key: row[key] for key in ("title", "content")}]
    fixture = tmp_path / "invalid.json"
    fixture.write_text(json.dumps(v2_corpus))
    with pytest.raises(ValueError):
        baseline.load_corpus(fixture)


def test_holdout_guard_refuses_before_setup_without_claiming_independence(
    v2_corpus, monkeypatch, tmp_path
):
    # Synthetic guard-only metadata; no actual heldout corpus is opened or evaluated.
    v2_corpus["split"] = "holdout"
    monkeypatch.setattr(baseline, "load_corpus", lambda path, **kw: v2_corpus)
    synthetic_path = tmp_path / "guard-only.json"
    synthetic_path.write_text("{}")
    monkeypatch.setattr(baseline, "run_seed", lambda *a: pytest.fail("Holdout setup must not run"))
    with pytest.raises(ValueError, match="explicit --allow-holdout"):
        baseline.run(Path("missing"), synthetic_path)


def test_topic_split_rejects_underlying_fact_overlap_without_opening_files():
    development = {"split": "development", "records": [{"topic": "same-fact"}], "queries": []}
    heldout_metadata = {"split": "holdout", "records": [], "queries": [{"topic": "same-fact"}]}
    with pytest.raises(ValueError, match="topics overlap"):
        baseline.check_split_topics(development, heldout_metadata)
    heldout_metadata["queries"][0]["topic"] = "independent-fact"
    baseline.check_split_topics(development, heldout_metadata)


def test_run_preserves_each_seed_and_manifest_without_averaging_failures(
    v2_corpus, tmp_path, monkeypatch
):
    fixture = tmp_path / "development.json"
    fixture.write_text(json.dumps(v2_corpus))
    calls = []

    def fake_run(model, corpus, seed):
        calls.append(seed)
        return {
            "fixture_id_seed": seed,
            "model_loading_and_index_ms": 1,
            "modes": {
                "fulltext": {"summary": {"hit_at_3": seed / 100}},
                "hybrid": {"summary": {"hit_at_3": seed / 100}},
            },
        }

    monkeypatch.setattr(baseline, "run_seed", fake_run)
    report = baseline.run(Path("not-used"), fixture)
    assert calls == [11, 29, 47]
    assert [run["fixture_id_seed"] for run in report["runs"]] == calls
    assert "modes" not in report
    assert (
        report["runs"][0]["modes"]["hybrid"]["summary"]
        != report["runs"][2]["modes"]["hybrid"]["summary"]
    )
    assert len(report["scoring_sha256"]) == len(report["environment_sha256"]) == 64
    assert "uv.lock" in report["source_sha256"]

    def fail_seed(model, corpus, seed):
        if seed == 29:
            raise RuntimeError("one seed failed")
        return fake_run(model, corpus, seed)

    monkeypatch.setattr(baseline, "run_seed", fail_seed)
    with pytest.raises(RuntimeError, match="one seed failed"):
        baseline.run(Path("not-used"), fixture)


def test_absent_model_never_reports_measurement(v2_corpus, tmp_path):
    with pytest.raises(FileNotFoundError):
        baseline.run_seed(tmp_path / "missing-model", v2_corpus, 11)


def test_current_get_cannot_be_replaced_by_a_valid_but_historical_revision(
    v2_corpus, tmp_path, monkeypatch
):
    store = Store(tmp_path / "memory")
    store.initialize()
    baseline.seed_store(store, v2_corpus, 11)
    service = MemoryService(store)
    original = store.get
    current = next(hit for hit in store.search("cedar") if hit["event_id"] == "current")

    def bad_current(memory_id, *, revision=None, projects=None):
        return original(memory_id, revision=revision or 1, projects=projects)

    monkeypatch.setattr(store, "get", bad_current)
    monkeypatch.setattr(
        service,
        "search",
        lambda *a, **kw: {"mode": "fulltext", "semantic": "disabled", "results": [current]},
    )
    with pytest.raises(RuntimeError, match="current corpus"):
        baseline.measure(service, v2_corpus, "fulltext", seed=11)


def test_v1_report_shape_and_uuid_formula_remain_compatible(monkeypatch):
    corpus = baseline.load_corpus()
    record = corpus["records"][0]
    expected = uuid.uuid5(
        uuid.NAMESPACE_URL,
        "dots-brain-retrieval:"
        + json.dumps([corpus["version"], record["project"], record["event_id"]]),
    )
    assert baseline.fixture_id(corpus, record) == expected
    calls = []

    def fake_run(model, data, seed):
        calls.append(seed)
        return {
            "modes": {"fulltext": {"summary": {}}, "hybrid": {"summary": {}}},
            "model_loading_and_index_ms": 7,
        }

    monkeypatch.setattr(baseline, "run_seed", fake_run)
    report = baseline.run(Path("unused"), baseline.CORPUS)
    assert calls == [None]
    assert report["schema_version"] == 1 and report["model_loading_and_index_ms"] == 7
    assert set(report) == {
        "schema_version",
        "state",
        "corpus",
        "corpus_sha256",
        "records",
        "scope",
        "model",
        "model_manifest_sha256",
        "environment",
        "source_sha256",
        "model_loading_and_index_ms",
        "modes",
        "limits",
    }


class BatchIndex:
    def __init__(self, pending):
        self.pending = pending
        self.total = pending
        self.calls = []

    def status(self):
        return {
            "state": "pending_index" if self.pending else "ready",
            "pending": self.pending,
            "indexed": self.total - self.pending,
        }

    def index(self, *, batch_size):
        self.calls.append(batch_size)
        self.pending = max(0, self.pending - batch_size)
        return self.status()


def test_index_drain_handles_multiple_default_batches():
    index = BatchIndex(53)
    result = baseline.drain_index(index)
    assert result == {"state": "ready", "pending": 0, "indexed": 53}
    assert index.calls == [16, 16, 16, 16]


def test_index_drain_rejects_no_progress_and_timeout(monkeypatch):
    index = BatchIndex(28)
    monkeypatch.setattr(index, "index", lambda **kw: index.status())
    with pytest.raises(RuntimeError, match="no progress"):
        baseline.drain_index(index)
    ticks = iter([0, 0, 2])
    monkeypatch.setattr(baseline.time, "monotonic", lambda: next(ticks))
    with pytest.raises(RuntimeError, match="declared time budget"):
        baseline.drain_index(BatchIndex(28), timeout_seconds=1)


def test_source_uri_is_preserved_and_forged_uri_rejected(v2_corpus, tmp_path):
    corpus = baseline.load_corpus()
    corpus["records"][0]["source_uri"] = "https://fictional.invalid/storage"
    store = Store(tmp_path / "memory")
    store.initialize()
    baseline.seed_store(store, corpus)
    source = corpus["records"][0]
    hit = next(h for h in store.search("SQLite") if h["event_id"] == source["event_id"])
    service = MemoryService(store)
    records = {row["event_id"]: row for row in corpus["records"]}
    baseline.verify_hit(service, hit, records, ("garden",))
    assert hit["source_uri"] == source["source_uri"]
    with pytest.raises(RuntimeError, match="current corpus"):
        baseline.verify_hit(
            service, dict(hit, source_uri="https://forged.invalid"), records, ("garden",)
        )
    # Fixed v2 contract has no URI: even an extra URI on an otherwise valid hit fails.
    second = Store(tmp_path / "v2")
    second.initialize()
    baseline.seed_store(second, v2_corpus, 11)
    hit = next(h for h in second.search("cedar") if h["event_id"] == "current")
    with pytest.raises(RuntimeError, match="current corpus"):
        baseline.verify_hit(
            MemoryService(second),
            dict(hit, source_uri="https://forged.invalid"),
            {row["event_id"]: row for row in v2_corpus["records"]},
            ("garden",),
        )


def test_corpus_drift_during_measurement_rejects_report(v2_corpus, tmp_path, monkeypatch):
    fixture = tmp_path / "development.json"
    fixture.write_text(json.dumps(v2_corpus))

    def mutate_corpus(*args):
        fixture.write_bytes(fixture.read_bytes() + b" ")
        return {"modes": {}, "model_loading_and_index_ms": 0}

    monkeypatch.setattr(baseline, "run_seed", mutate_corpus)
    with pytest.raises(RuntimeError, match="Corpus bytes changed"):
        baseline.run(Path("unused"), fixture)


def test_lock_drift_during_measurement_rejects_report(v2_corpus, tmp_path, monkeypatch):
    fixture = tmp_path / "development.json"
    fixture.write_text(json.dumps(v2_corpus))
    root = tmp_path / "checkout"
    for name in (
        "src/dots_brain/store.py",
        "src/dots_brain/semantic.py",
        "src/dots_brain/service.py",
        "uv.lock",
    ):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((baseline.ROOT / name).read_bytes())
    monkeypatch.setattr(baseline, "ROOT", root)

    def mutate_lock(*args):
        lock = root / "uv.lock"
        lock.write_bytes(lock.read_bytes() + b" ")
        return {"modes": {}, "model_loading_and_index_ms": 0}

    monkeypatch.setattr(baseline, "run_seed", mutate_lock)
    with pytest.raises(RuntimeError, match="runtime source or lock changed"):
        baseline.run(Path("unused"), fixture)


def test_cli_v2_summary_contains_only_per_seed_mode_summaries():
    report = {
        "schema_version": 2,
        "runs": [
            {
                "fixture_id_seed": seed,
                "modes": {
                    "hybrid": {"summary": {"hit_at_3": 0.5}, "queries": ["private detailed rows"]}
                },
            }
            for seed in (11, 29, 47)
        ],
    }
    assert baseline.report_summary(report) == [
        {"fixture_id_seed": seed, "modes": {"hybrid": {"hit_at_3": 0.5}}} for seed in (11, 29, 47)
    ]


def test_previous_revision_uri_is_explicitly_unsupported(v2_corpus, tmp_path):
    v2_corpus["records"][0]["previous"][0]["source_uri"] = "https://fictional.invalid/old"
    fixture = tmp_path / "unsupported.json"
    fixture.write_text(json.dumps(v2_corpus))
    with pytest.raises(ValueError, match="URI is constant per record"):
        baseline.load_corpus(fixture)


def test_index_timing_excludes_pending_query_probe(v2_corpus, tmp_path, monkeypatch):
    clock = [0.0]

    class FixtureIndex:
        def __init__(self, store):
            self.store = store
            self.indexed = set()
            clock[0] += 0.01

        def current(self):
            ids = {row["id"] for row in self.store.export()}
            return {(key, self.store.get(key)["revision"]) for key in ids}

        def status(self):
            current = self.current()
            count = len(current & self.indexed)
            return {
                "state": "ready" if count == len(current) else "pending_index",
                "pending": len(current) - count,
                "indexed": count,
            }

        def index(self, *, batch_size):
            clock[0] += 0.02
            self.indexed.update(sorted(self.current() - self.indexed)[:batch_size])
            return self.status()

    def measured(*args, **kwargs):
        clock[0] += 0.1
        return {"summary": {}} if kwargs.get("score", True) else {"quality_measured": False}

    monkeypatch.setattr(baseline, "FILES", {})
    monkeypatch.setattr(baseline, "SemanticIndex", FixtureIndex)
    monkeypatch.setattr(baseline, "measure", measured)
    monkeypatch.setattr(baseline.time, "perf_counter", lambda: clock[0])
    report = baseline.run_seed(tmp_path, v2_corpus, 11)
    assert report["pending_probe"]["index"]["pending"] == 1
    assert report["pending_probe"]["elapsed_ms"] == 200
    assert report["initial_model_loading_and_index_ms"] == 30
    assert report["reindex_ms"] == 20
    assert report["model_loading_and_index_ms"] == 50
