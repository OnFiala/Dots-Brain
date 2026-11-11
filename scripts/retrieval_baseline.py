"""Measure a frozen synthetic corpus in a disposable store; never accepts user data."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import platform
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
from dots_brain.semantic import FILES, MODEL_ID, SemanticIndex, model_directory
from dots_brain.service import MemoryService
from dots_brain.store import Store

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests/fixtures/retrieval/cz-en-v1.json"
TOP_K = 3


def load_corpus(path: Path = CORPUS) -> dict:
    corpus = json.loads(path.read_text())
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
            key not in records or records[key]["project"] != "garden" for key in query["relevant"]
        ):
            raise ValueError("Relevance judgments must reference visible corpus records.")
    return corpus


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


def seed_store(store: Store, corpus: dict) -> None:
    # Product tie-breaking uses memory UUIDs. Freeze only fixture creation so
    # unchanged retrieval cannot get a different rank from new random IDs.
    for record in corpus["records"]:
        identity = json.dumps([corpus["version"], record["project"], record["event_id"]])
        stable_id = uuid.uuid5(uuid.NAMESPACE_URL, "dots-brain-retrieval:" + identity)
        with patch("dots_brain.store.uuid.uuid4", return_value=stable_id):
            store.remember(source="retrieval-baseline", account="synthetic", **record)


def summarize(rows: list[dict]) -> dict:
    positives = [row for row in rows if row["recall_at_3"] is not None]
    negatives = [row for row in rows if row["recall_at_3"] is None]
    return {
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


def measure(service: MemoryService, corpus: dict, mode: str) -> dict:
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
            if source is None or source["project"] != "garden" or hit["project"] != "garden":
                raise RuntimeError("Retrieval crossed the corpus project boundary.")
            exact = service.store.get(hit["id"], revision=hit["revision"], projects=caller.projects)
            if (
                hit["revision"] != 1
                or hit["source"] != "retrieval-baseline"
                or hit["account"] != "synthetic"
                or exact["content"] != source["content"]
            ):
                raise RuntimeError("Retrieved source or revision does not match the corpus.")
        rows.append(
            {
                "id": query["id"],
                "category": query["category"],
                "language": query["language"],
                "returned": keys,
                "relevant": query["relevant"],
                "elapsed_ms": round(elapsed, 3),
                **quality(keys, query["relevant"]),
            }
        )
    return {
        "summary": summarize(rows),
        "by_category": {
            category: summarize([row for row in rows if row["category"] == category])
            for category in sorted({row["category"] for row in rows})
        },
        "queries": rows,
    }


def run(model: Path) -> dict:
    corpus = load_corpus()
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
    with tempfile.TemporaryDirectory(prefix="dots-brain-retrieval-") as temporary:
        store = Store(Path(temporary) / "memory")
        store.initialize()
        seed_store(store, corpus)
        destination = model_directory(store)
        destination.mkdir(parents=True)
        for name in FILES:
            shutil.copyfile(model / name, destination / name)
        started = time.perf_counter()
        semantic = SemanticIndex(store)  # Verifies every prepared model artifact.
        semantic.index()
        status = semantic.status()
        if status["state"] != "ready" or status["indexed"] != len(corpus["records"]):
            raise RuntimeError("The complete synthetic corpus must be indexed before measurement.")
        loading_index_ms = (time.perf_counter() - started) * 1000
        modes = {
            "fulltext": measure(MemoryService(store), corpus, "fulltext"),
            "hybrid": measure(MemoryService(store, semantic), corpus, "hybrid"),
        }
    return {
        "schema_version": 1,
        "state": "measured",
        "corpus": corpus["version"],
        "corpus_sha256": hashlib.sha256(CORPUS.read_bytes()).hexdigest(),
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
        "source_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "src/dots_brain/store.py",
                "src/dots_brain/semantic.py",
                "src/dots_brain/service.py",
                "scripts/retrieval_baseline.py",
                "uv.lock",
            )
        },
        "model_loading_and_index_ms": round(loading_index_ms, 3),
        "modes": modes,
        "limits": (
            "Frozen fictional corpus; judgments are declared, not a general quality guarantee. "
            "No relevance threshold or abstention is added. Negative-query results are reported. "
            "Query timings exclude warmup/model loading/indexing and are not throughput evidence."
        ),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = run(args.model_dir)
    # Refuse to overwrite a previous measurement.
    with args.output.open("x") as output:
        output.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({mode: data["summary"] for mode, data in report["modes"].items()}))
