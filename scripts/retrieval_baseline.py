"""Measure a frozen synthetic corpus in a disposable store; never accepts user data."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import platform
import re
import shutil
import sqlite3
import statistics
import tempfile
import time
import uuid
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

from dots_brain.auth import Policy
from dots_brain.errors import NotFoundError
from dots_brain.semantic import FILES, MODEL_ID, SemanticIndex, model_directory
from dots_brain.service import MemoryService
from dots_brain.store import Store

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests/fixtures/retrieval/cz-en-v1.json"
TOP_K = 3
INDEX_TIMEOUT_SECONDS = 120
INDEX_BATCH_SIZE = 16


def load_corpus(path: Path = CORPUS, *, frozen_bytes: bytes | None = None) -> dict:
    corpus = json.loads(path.read_bytes() if frozen_bytes is None else frozen_bytes)
    records = {row["event_id"]: row for row in corpus["records"]}
    if len(records) != len(corpus["records"]):
        raise ValueError("Corpus source identities must be unique.")
    queries = corpus["queries"]
    if len({q["id"] for q in queries}) != len(queries):
        raise ValueError("Corpus query identities must be unique.")
    for query in queries:
        if len(set(query["relevant"])) != len(query["relevant"]):
            raise ValueError("Relevance judgments must be unique.")
        if any(
            key not in records
            or records[key]["project"] != "garden"
            or records[key].get("deleted", False)
            for key in query["relevant"]
        ):
            raise ValueError("Relevance judgments must reference visible corpus records.")
    if "split" in corpus:
        validate_v2(corpus)
    return corpus


def validate_v2(corpus: dict) -> None:
    """Validate source judgments; optional record URI is constant across revisions."""
    if (
        corpus["split"] not in ("development", "holdout")
        or corpus.get("fixture_id_seeds") != [11, 29, 47]
        or any(type(seed) is not int for seed in corpus["fixture_id_seeds"])
        or corpus.get("scope") != ["garden"]
        or not isinstance(corpus.get("version"), str)
        or not isinstance(corpus.get("description"), str)
    ):
        raise ValueError("Invalid v2 split, fixture seeds, scope or metadata.")
    records = {row["event_id"]: row for row in corpus["records"]}
    for row in corpus["records"]:
        for field in ("event_id", "project", "title", "content", "topic"):
            if not isinstance(row.get(field), str) or not row[field]:
                raise ValueError("V2 source fields must be nonempty strings.")
        if row.get("language") not in ("en", "cs") or type(row.get("deleted", False)) is not bool:
            raise ValueError("Invalid v2 source language or deletion flag.")
        previous = row.get("previous", [])
        if not isinstance(previous, list):
            raise ValueError("Previous revisions must be an ordered list.")
        if any(not isinstance(old, dict) or set(old) != {"title", "content"} for old in previous):
            raise ValueError(
                "Previous revisions support title/content only; URI is constant per record."
            )
        revisions = [*previous, row]
        for i, revision in enumerate(revisions):
            if any(
                not isinstance(revision.get(k), str) or not revision[k]
                for k in ("title", "content")
            ):
                raise ValueError("Invalid previous revision.")
            if i and all(revision[k] == revisions[i - 1][k] for k in ("title", "content")):
                raise ValueError("Adjacent revisions must change source content or title.")
    for query in corpus["queries"]:
        if (
            any(
                not isinstance(query.get(k), str) or not query[k]
                for k in ("id", "topic", "text", "category")
            )
            or not re.fullmatch(r"[a-z][a-z0-9_]*", query["category"])
            or query.get("language") not in ("en", "cs")
            or type(query.get("cross_language")) is not bool
            or not isinstance(query["relevant"], list)
        ):
            raise ValueError("Invalid v2 query metadata.")
        if any(records[key]["topic"] != query["topic"] for key in query["relevant"]):
            raise ValueError("Query relevance must stay within its declared topic.")
        if query["cross_language"] and (
            not query["relevant"]
            or any(records[key]["language"] == query["language"] for key in query["relevant"])
        ):
            raise ValueError("Cross-language positives require opposite-language sources.")
    positive = sum(bool(q["relevant"]) for q in corpus["queries"])
    negative = len(corpus["queries"]) - positive
    minimum = (100, 25) if corpus["split"] == "development" else (200, 50)
    if positive < minimum[0] or negative < minimum[1]:
        raise ValueError("V2 split has insufficient positive or no-answer queries.")
    if corpus["split"] == "holdout" and sum(q["cross_language"] for q in corpus["queries"]) < 50:
        raise ValueError("Holdout requires at least 50 cross-language positives.")


def check_split_topics(development: dict, holdout: dict) -> None:
    """Caller supplies both fixtures; this helper never discovers or opens holdouts."""
    if development.get("split") != "development" or holdout.get("split") != "holdout":
        raise ValueError("Expected development and holdout splits.")

    def topics(corpus):
        return {row["topic"] for kind in ("records", "queries") for row in corpus[kind]}

    if topics(development) & topics(holdout):
        raise ValueError("Development and holdout topics overlap.")


def quality(returned: list[str], relevant: list[str]) -> dict:
    expected = set(relevant)
    if not expected:
        return {
            "recall_at_3": None,
            "reciprocal_rank_at_3": None,
            "unexpected_results": len(returned),
        }
    hits = expected.intersection(returned[:TOP_K])
    rank = next((i for i, key in enumerate(returned[:TOP_K], 1) if key in expected), None)
    return {
        "recall_at_3": len(hits) / len(expected),
        "reciprocal_rank_at_3": 0.0 if rank is None else 1 / rank,
        "unexpected_results": len(set(returned[:TOP_K]) - expected),
    }


def fixture_id(corpus: dict, record: dict, seed: int | None = None) -> uuid.UUID:
    identity = [corpus["version"], record["project"], record["event_id"]]
    if "split" in corpus:
        if seed not in corpus["fixture_id_seeds"]:
            raise ValueError("A declared fixture-ID seed is required.")
        identity.append(seed)
    return uuid.uuid5(uuid.NAMESPACE_URL, "dots-brain-retrieval:" + json.dumps(identity))


def seed_store(store: Store, corpus: dict, seed: int | None = None, *, initial=False) -> None:
    # Freeze fixture UUIDs only; preserve product ranking and v1 UUID ordering.
    for record in corpus["records"]:
        first = record.get("previous", [record])[0] if record.get("previous") else record
        payload = {key: record[key] for key in ("event_id", "project")}
        with patch("dots_brain.store.uuid.uuid4", return_value=fixture_id(corpus, record, seed)):
            store.remember(
                source="retrieval-baseline",
                account="synthetic",
                **payload,
                source_uri=record.get("source_uri"),
                **{key: first[key] for key in ("title", "content")},
            )
    if not initial:
        apply_changes(store, corpus, seed)


def apply_changes(store: Store, corpus: dict, seed: int | None) -> None:
    for record in corpus["records"]:
        for revision, content in enumerate([*record.get("previous", [])[1:], record], 1):
            if not record.get("previous"):
                break
            store.remember(
                source="retrieval-baseline",
                account="synthetic",
                event_id=record["event_id"],
                project=record["project"],
                title=content["title"],
                content=content["content"],
                expected_revision=revision,
                source_uri=record.get("source_uri"),
            )
        if record.get("deleted"):
            store.forget(
                str(fixture_id(corpus, record, seed)),
                expected_revision=1 + len(record.get("previous", [])),
            )


def verify_hit(
    service: MemoryService,
    hit: dict,
    records: dict,
    projects: tuple[str, ...],
    *,
    expected_id: str | None = None,
) -> None:
    source = records.get(hit["event_id"])
    if source is None or source["project"] not in projects or hit["project"] not in projects:
        raise RuntimeError("Retrieval crossed the corpus project boundary.")
    if expected_id is not None and hit["id"] != expected_id:
        raise RuntimeError("Retrieved source UUID does not match the fixture seed.")
    if source.get("deleted"):
        raise RuntimeError("Retrieved deleted source.")
    current = service.store.get(hit["id"], projects=projects)
    historical = service.store.get(hit["id"], revision=hit["revision"], projects=projects)
    expected = {key: source[key] for key in ("event_id", "project", "title", "content")}
    expected.update(
        source="retrieval-baseline",
        account="synthetic",
        revision=1 + len(source.get("previous", [])),
        source_uri=source.get("source_uri"),
    )
    if (
        any(
            current.get(key) != value or historical.get(key) != value
            for key, value in expected.items()
        )
        or any(hit.get(key) != value for key, value in expected.items() if key != "content")
        or ("content" in hit and hit["content"] != source["content"])
        or current["id"] != hit["id"]
        or historical["id"] != hit["id"]
    ):
        raise RuntimeError("Retrieved source or revision does not match the current corpus.")
    passage = hit.get("passage", {})
    excerpt = hit.get("excerpt")
    if not isinstance(excerpt, str):
        raise RuntimeError("Retrieved excerpt is missing.")
    if "start_char" in passage or "end_char" in passage:
        field, start, end = (passage.get(key) for key in ("field", "start_char", "end_char"))
        if (
            field not in ("title", "content")
            or type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(current[field])
            or excerpt != current[field][start:end][:800]
        ):
            raise RuntimeError("Retrieved passage offsets or excerpt do not match the source.")
    elif any(piece not in current["content"] for piece in excerpt.split(" … ") if piece):
        raise RuntimeError("Retrieved snippet does not match the source body.")


def verify_fixture(store: Store, corpus: dict, seed: int) -> None:
    """Prove actual write/update/forget setup rather than trust declared metadata."""
    records = {row["event_id"]: row for row in corpus["records"]}
    for row in corpus["records"]:
        key = str(fixture_id(corpus, row, seed))
        if row.get("deleted"):
            try:
                store.get(key)
            except NotFoundError:
                continue
            raise RuntimeError("Fixture deletion was not applied.")
        current = store.get(key)
        verify_hit(
            MemoryService(store),
            dict(current, excerpt=current["content"]),
            records,
            (row["project"],),
            expected_id=key,
        )
        for revision, previous in enumerate(row.get("previous", []), 1):
            historical = store.get(key, revision=revision)
            if any(historical[field] != previous[field] for field in ("title", "content")):
                raise RuntimeError("Fixture history does not match the oracle.")


def summarize(rows: list[dict], *, v2=False) -> dict:
    positives = [row for row in rows if row["recall_at_3"] is not None]
    negatives = [row for row in rows if row["recall_at_3"] is None]
    result = {
        "queries": len(rows),
        "positive_queries": len(positives),
        "mean_recall_at_3": statistics.mean(r["recall_at_3"] for r in positives)
        if positives
        else None,
        "mean_reciprocal_rank_at_3": statistics.mean(r["reciprocal_rank_at_3"] for r in positives)
        if positives
        else None,
        "negative_queries": len(negatives),
        "negative_queries_with_results": sum(bool(row["returned"]) for row in negatives),
        "median_query_ms": statistics.median(row["elapsed_ms"] for row in rows),
        "max_query_ms": max(row["elapsed_ms"] for row in rows),
    }

    if v2:
        result.update(
            hit_at_3=statistics.mean(bool(r["recall_at_3"]) for r in positives)
            if positives
            else None,
            no_answer_false_positive_rate=(
                sum(bool(r["returned"]) for r in negatives) / len(negatives) if negatives else None
            ),
            p95_query_ms=sorted(r["elapsed_ms"] for r in rows)[math.ceil(0.95 * len(rows)) - 1],
            topics=len({r["topic"] for r in rows}),
            by_topic_counts={
                topic: sum(r["topic"] == topic for r in rows)
                for topic in sorted({r["topic"] for r in rows})
            },
        )
    return result


def measure(service: MemoryService, corpus: dict, mode: str, *, score=True, seed=None) -> dict:
    records = {r["event_id"]: r for r in corpus["records"]}
    caller = Policy(frozenset({"memory:read"}), ("garden",))
    # Warmup is excluded from query timings. Index/model loading are measured separately.
    service.search(corpus["queries"][0]["text"], policy=caller, limit=TOP_K)
    rows = []
    for query in corpus["queries"]:
        started = time.perf_counter()
        response = service.search(query["text"], policy=caller, limit=TOP_K)
        elapsed = (time.perf_counter() - started) * 1000
        if response["mode"] != mode or response["semantic"] != (
            "enabled" if mode == "hybrid" else "disabled"
        ):
            raise RuntimeError("Retrieval mode degraded; do not report a semantic baseline.")
        returned = response["results"]
        keys = [hit["event_id"] for hit in returned]
        if len(keys) != len(set(keys)) or len(keys) > TOP_K:
            raise RuntimeError("Duplicate or excessive retrieval results.")
        for hit in returned:
            source = records.get(hit["event_id"])
            expected_id = str(fixture_id(corpus, source, seed)) if source else None
            verify_hit(service, hit, records, caller.projects, expected_id=expected_id)
        if not score:
            continue
        rows.append(
            {
                "id": query["id"],
                "category": query["category"],
                "language": query["language"],
                "returned": keys,
                "relevant": query["relevant"],
                "elapsed_ms": round(elapsed, 3),
                **(
                    {"topic": query["topic"], "cross_language": query["cross_language"]}
                    if "split" in corpus
                    else {}
                ),
                **quality(keys, query["relevant"]),
            }
        )
    if not score:
        return {
            "state": "invariants_only",
            "queries_checked": len(corpus["queries"]),
            "quality_measured": False,
        }
    v2 = "split" in corpus
    result = {
        "summary": summarize(rows, v2=v2),
        "by_category": {
            category: summarize([row for row in rows if row["category"] == category], v2=v2)
            for category in sorted({row["category"] for row in rows})
        },
        "queries": rows,
    }

    if v2:
        cross = [row for row in rows if row["cross_language"]]
        result["cross_language"] = summarize(cross, v2=True) if cross else {"queries": 0}
    return result


def drain_index(semantic: SemanticIndex, *, timeout_seconds=INDEX_TIMEOUT_SECONDS) -> dict:
    """Drain bounded batches; reject timeout, inconsistent state or stalled progress."""
    deadline = time.monotonic() + timeout_seconds
    status = semantic.status()
    while status["state"] != "ready":
        pending = status["pending"]
        if status["state"] != "pending_index" or type(pending) is not int or pending <= 0:
            raise RuntimeError("Invalid pending index state.")
        if time.monotonic() >= deadline:
            raise RuntimeError("Indexing exceeded its declared time budget.")
        semantic.index(batch_size=INDEX_BATCH_SIZE)
        status = semantic.status()
        if time.monotonic() >= deadline:
            raise RuntimeError("Indexing exceeded its declared time budget.")
        if status["pending"] >= pending:
            raise RuntimeError("Indexing made no progress.")
    if status["pending"] != 0:
        raise RuntimeError("Ready index still has pending sources.")
    return status


def run_seed(model: Path, corpus: dict, seed: int | None) -> dict:
    with tempfile.TemporaryDirectory(prefix="dots-brain-retrieval-") as temporary:
        store = Store(Path(temporary) / "memory")
        store.initialize()
        seed_store(store, corpus, seed, initial=True)
        destination = model_directory(store)
        destination.mkdir(parents=True)
        for name in FILES:
            shutil.copyfile(model / name, destination / name)
        started = time.perf_counter()
        semantic = SemanticIndex(store)  # Verifies every prepared model artifact.
        status = drain_index(semantic)
        initial_index_ms = (time.perf_counter() - started) * 1000
        if status["state"] != "ready" or status["indexed"] != len(corpus["records"]):
            raise RuntimeError("The complete synthetic corpus must be indexed before measurement.")
        pending = None
        reindex_ms = 0.0
        if "split" in corpus:
            apply_changes(store, corpus, seed)
            verify_fixture(store, corpus, seed)
            probe_started = time.perf_counter()
            pending = {
                "index": semantic.status(),
                "modes": {
                    "fulltext": measure(
                        MemoryService(store), corpus, "fulltext", score=False, seed=seed
                    ),
                    "hybrid": measure(
                        MemoryService(store, semantic), corpus, "hybrid", score=False, seed=seed
                    ),
                },
            }
            pending["elapsed_ms"] = round((time.perf_counter() - probe_started) * 1000, 3)
            reindex_started = time.perf_counter()
            status = drain_index(semantic)
            reindex_ms = (time.perf_counter() - reindex_started) * 1000
            if status["state"] != "ready" or status["indexed"] != sum(
                not row.get("deleted", False) for row in corpus["records"]
            ):
                raise RuntimeError(
                    "Current visible sources must be indexed before quality measurement."
                )
        loading_index_ms = initial_index_ms + reindex_ms
        modes = {
            "fulltext": measure(MemoryService(store), corpus, "fulltext", seed=seed),
            "hybrid": measure(MemoryService(store, semantic), corpus, "hybrid", seed=seed),
        }
    return {
        "fixture_id_seed": seed,
        "modes": modes,
        "model_loading_and_index_ms": round(loading_index_ms, 3),
        **(
            {
                "pending_probe": pending,
                "initial_model_loading_and_index_ms": round(initial_index_ms, 3),
                "reindex_ms": round(reindex_ms, 3),
            }
            if pending is not None
            else {}
        ),
    }


def run(model: Path, corpus_path: Path = CORPUS, *, allow_holdout=False) -> dict:
    corpus_bytes = corpus_path.read_bytes()
    corpus_hash = hashlib.sha256(corpus_bytes).hexdigest()
    corpus = load_corpus(corpus_path, frozen_bytes=corpus_bytes)
    if corpus.get("split") == "holdout" and not allow_holdout:
        raise ValueError(
            "Holdout execution requires explicit --allow-holdout; it does not prove independence."
        )
    runtime = {
        "src/dots_brain/store.py": Store,
        "src/dots_brain/semantic.py": SemanticIndex,
        "src/dots_brain/service.py": MemoryService,
    }
    for name, component in runtime.items():
        if Path(inspect.getfile(component)).read_bytes() != (ROOT / name).read_bytes():
            raise RuntimeError(
                "Loaded runtime differs from this checkout; use locked dependencies."
            )
    source_paths = {name: ROOT / name for name in runtime}
    source_paths.update(
        {"scripts/retrieval_baseline.py": Path(__file__), "uv.lock": ROOT / "uv.lock"}
    )
    source_bytes = {name: path.read_bytes() for name, path in source_paths.items()}
    source_hashes = {name: hashlib.sha256(data).hexdigest() for name, data in source_bytes.items()}
    seeds = corpus.get("fixture_id_seeds", [None])
    runs = [run_seed(model, corpus, seed) for seed in seeds]
    if corpus_path.read_bytes() != corpus_bytes:
        raise RuntimeError("Corpus bytes changed during measurement; reject this report.")
    if any(path.read_bytes() != source_bytes[name] for name, path in source_paths.items()):
        raise RuntimeError(
            "Runner, runtime source or lock changed during measurement; reject report."
        )
    modes = runs[0]["modes"]
    loading_index_ms = runs[0]["model_loading_and_index_ms"]
    report = {
        "schema_version": 1,
        "state": "measured",
        "corpus": corpus["version"],
        "corpus_sha256": corpus_hash,
        "records": len(corpus["records"]),
        "scope": ["garden"],
        "model": MODEL_ID,
        "model_manifest_sha256": hashlib.sha256(
            json.dumps({"model": MODEL_ID, "files": FILES}, sort_keys=True).encode()
        ).hexdigest(),
        "environment": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
            "packages": {name: version(name) for name in ("fastembed", "numpy", "onnxruntime")},
        },
        "source_sha256": source_hashes,
        "model_loading_and_index_ms": round(loading_index_ms, 3),
        "modes": modes,
        "limits": (
            "Frozen fictional corpus; judgments are declared, not a general quality guarantee. "
            "No relevance threshold or abstention is added. Negative-query results are reported. "
            "Query timings exclude warmup/model loading/indexing and are not throughput evidence."
        ),
    }

    if "split" in corpus:
        report.update(
            schema_version=2,
            split=corpus["split"],
            runs=runs,
            fixture_id_seeds=seeds,
            holdout_independence="Requires evaluator custody evidence.",
            indexing_budget={
                "seconds_per_phase": INDEX_TIMEOUT_SECONDS,
                "batch_size": INDEX_BATCH_SIZE,
                "no_progress": "fail",
            },
        )
        del report["modes"]
        del report["model_loading_and_index_ms"]
        report["scoring_sha256"] = hashlib.sha256(
            "".join(
                inspect.getsource(fn) for fn in (quality, summarize, verify_hit, measure)
            ).encode()
        ).hexdigest()
        report["environment_sha256"] = hashlib.sha256(
            json.dumps(report["environment"], sort_keys=True).encode()
        ).hexdigest()
    return report


def report_summary(report: dict) -> dict | list:
    if report["schema_version"] == 1:
        return {mode: data["summary"] for mode, data in report["modes"].items()}
    return [
        {
            "fixture_id_seed": run["fixture_id_seed"],
            "modes": {mode: data["summary"] for mode, data in run["modes"].items()},
        }
        for run in report["runs"]
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--allow-holdout", action="store_true")
    args = parser.parse_args()
    report = run(args.model_dir, args.corpus, allow_holdout=args.allow_holdout)
    # Refuse to overwrite a previous measurement.
    with args.output.open("x") as output:
        output.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report_summary(report)))
