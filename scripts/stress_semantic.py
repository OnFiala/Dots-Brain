"""Exercise prepared CPU embeddings in an isolated synthetic memory service."""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import shutil
import statistics
import tempfile
import time
import uuid
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

from stress import summary

from dots_brain.auth import issue_client
from dots_brain.bridge import connect
from dots_brain.runtime import down, up
from dots_brain.semantic import FILES, MODEL_ID, model_directory
from dots_brain.store import Store


def capacity_content(number, chunks, tokenizer):
    """Fixed synthetic shape; actual indexed chunk count is checked separately."""
    target = 80 * (chunks - 1) + 48
    sentence = f"Note {number}: the local team plans calm work and keeps clear task notes. "
    text = sentence * (target // 10 + 1)
    offsets = tokenizer.encode(text, add_special_tokens=False).offsets
    if len(offsets) < target:
        raise AssertionError("Synthetic capacity text has too few tokens.")
    text = text[: offsets[target - 1][1]]
    if len(text) > 32000:
        raise AssertionError("Synthetic capacity text exceeds the product input bound.")
    return text


def run(
    model: Path,
    records: int,
    workers: int,
    queries: int,
    *,
    chunks_per_record=1,
    runs=1,
    index_timeout=180,
    query_timeout=240,
) -> dict:
    root = Path(__file__).resolve().parents[1]
    source_paths = (
        "src/dots_brain/service.py",
        "src/dots_brain/semantic.py",
        "src/dots_brain/store.py",
        "scripts/stress_semantic.py",
        "uv.lock",
    )
    source_hashes = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in source_paths
    }
    fixture_digest = hashlib.sha256()
    with tempfile.TemporaryDirectory(prefix="dots-brain-semantic-stress-") as temporary:
        store = Store(Path(temporary) / "memory")
        store.initialize()
        directory = model_directory(store)
        directory.mkdir(parents=True)
        for name in FILES:
            source = model / name
            try:
                os.link(source, directory / name)
            except OSError:
                shutil.copyfile(source, directory / name)
        tokenizer = None
        if chunks_per_record > 1:
            from tokenizers import Tokenizer

            tokenizer = Tokenizer.from_file(str(model / "tokenizer.json"))
            tokenizer.no_truncation()
            tokenizer.no_padding()

        def remember(key, content):
            identity = uuid.uuid5(uuid.NAMESPACE_URL, "dots-brain-capacity-v1:" + key)
            fixture_digest.update(json.dumps([str(identity), key, content]).encode())
            with patch("dots_brain.store.uuid.uuid4", return_value=identity):
                return store.remember(
                    content=content, source="synthetic", account="stress", event_id=key
                )

        for i in range(records - 3):
            remember(
                str(i),
                capacity_content(i, chunks_per_record, tokenizer)
                if tokenizer is not None
                else f"Synthetic planning note {i}: review the team task list on Monday.",
            )
        facts = {
            "allergy": (
                "I have a severe peanut allergy. Avoid peanuts and peanut butter in my food."
            ),
            "storage": (
                "The application stores its database locally in SQLite, without a cloud database."
            ),
            "city": "My upcoming trip is to Paris, France. I want to visit the Eiffel Tower.",
        }
        expected = {}
        for key, content in facts.items():
            expected[key] = remember(key, content)["id"]
        started = time.monotonic()
        try:
            service = up(store, port=0, semantic=True)
            credential = Path(temporary) / "reader.json"
            issue_client(
                store,
                name="semantic-stress",
                scopes=["memory:read"],
                projects=None,
                days=1,
                output=credential,
                url=service["url"],
            )

            async def exercise():
                deadline = started + index_timeout

                async def wait_for_index():
                    async with connect(credential) as session:
                        while True:
                            status = await session.call_tool("memory_status", {})
                            if time.monotonic() >= deadline:
                                raise AssertionError(
                                    "Background indexing exceeded the declared budget."
                                )
                            assert not status.isError
                            semantic = status.structuredContent["semantic"]
                            if semantic["pending"] == 0:
                                assert (
                                    semantic["state"] == "ready" and semantic["indexed"] == records
                                ), "Semantic index is not ready with all synthetic records."
                                break
                            await asyncio.sleep(0.5)

                await asyncio.wait_for(
                    wait_for_index(), timeout=max(0, deadline - time.monotonic())
                )
                indexing_seconds = time.monotonic() - started
                with store.connection() as db:
                    chunk_count = db.execute("SELECT count(*) FROM semantic_chunks").fetchone()[0]
                if chunks_per_record > 1:
                    assert chunk_count == (records - 3) * chunks_per_record + 3, (
                        "Actual indexed chunk count differs from the declared capacity shape."
                    )
                prompts = [
                    ("Kterému jídlu se mám kvůli alergii vyhnout?", "allergy"),
                    ("Kde je uložená databáze aplikace?", "storage"),
                    ("Do kterého města pojedu na výlet?", "city"),
                    ("Which food must I avoid because of my allergy?", "allergy"),
                    ("What database does the application use?", "storage"),
                    ("Where am I going on my upcoming trip?", "city"),
                ]

                async def worker(number, samples, hits):
                    async with connect(credential) as session:
                        for i in range(number, queries, workers):
                            query, key = prompts[i % len(prompts)]
                            tick = time.monotonic()
                            result = await session.call_tool(
                                "memory_search", {"query": query, "limit": 3}
                            )
                            assert not result.isError
                            payload = result.structuredContent
                            assert (
                                payload["semantic"] == "enabled" and payload["mode"] == "hybrid"
                            ), "Semantic stress requires enabled hybrid search."
                            rows = payload["results"]
                            hits.append(any(row["id"] == expected[key] for row in rows))
                            samples.append(time.monotonic() - tick)

                measurements = []
                for _ in range(runs):
                    samples, hits = [], []
                    await asyncio.wait_for(
                        asyncio.gather(*(worker(i, samples, hits) for i in range(workers))),
                        timeout=query_timeout,
                    )
                    assert len(samples) == queries and len(hits) == queries
                    assert all(hits), (
                        f"Synthetic bilingual retrieval missed {len(hits) - sum(hits)} queries."
                    )
                    measurements.append(
                        {
                            "queries": summary(samples),
                            "synthetic_recall_at_3": sum(hits) / len(hits),
                        }
                    )
                return indexing_seconds, chunk_count, measurements

            indexing_seconds, chunk_count, measurements = asyncio.run(
                asyncio.wait_for(exercise(), timeout=index_timeout + runs * query_timeout + 30)
            )
            status = Path(f"/proc/{service['pid']}/status").read_text()
            rss = next(
                int(line.split()[1]) for line in status.splitlines() if line.startswith("VmHWM:")
            )
            if any(
                hashlib.sha256((root / name).read_bytes()).hexdigest() != digest
                for name, digest in source_hashes.items()
            ):
                raise RuntimeError(
                    "Benchmark source or dependency lock changed during measurement."
                )
            return {
                "state": "passed",
                "model": MODEL_ID,
                "records": records,
                "workers": workers,
                "chunks": chunk_count,
                **measurements[0],
                "runs": measurements,
                "median_run_p95_ms": statistics.median(
                    row["queries"]["p95_ms"] for row in measurements
                ),
                "worst_run_p95_ms": max(row["queries"]["p95_ms"] for row in measurements),
                "fixture_sha256": fixture_digest.hexdigest(),
                "budget": {"index_seconds": index_timeout, "query_seconds_per_run": query_timeout},
                "indexing_including_startup_seconds": round(indexing_seconds, 2),
                "service_peak_rss_kib": rss,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "environment": {
                    "python": platform.python_version(),
                    "platform": platform.platform(),
                    "packages": {
                        name: version(name) for name in ("fastembed", "numpy", "onnxruntime")
                    },
                },
                "source_sha256": source_hashes,
                "model_manifest_sha256": hashlib.sha256(
                    json.dumps({"model": MODEL_ID, "files": FILES}, sort_keys=True).encode()
                ).hexdigest(),
                "limits": (
                    "Six synthetic bilingual queries; not a representative relevance benchmark."
                ),
            }
        finally:
            down(store)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--records", type=int, default=200)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--queries", type=int, default=96)
    parser.add_argument("--chunks-per-record", type=int, default=1)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--index-timeout", type=int, default=180)
    parser.add_argument("--query-timeout", type=int, default=240)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if (
        not 3 <= args.records <= 450
        or not 1 <= args.workers <= 8
        or not 6 <= args.queries <= 200
        or not 1 <= args.chunks_per_record <= 67
        or not 1 <= args.runs <= 3
        or not 30 <= args.index_timeout <= 900
        or not 30 <= args.query_timeout <= 900
    ):
        parser.error(
            "Bounds: 3–450 records, 1–8 workers, 6–200 queries, "
            "1–67 chunks, 1–3 runs, 30–900 seconds per phase."
        )
    report = run(
        args.model_dir,
        args.records,
        args.workers,
        args.queries,
        chunks_per_record=args.chunks_per_record,
        runs=args.runs,
        index_timeout=args.index_timeout,
        query_timeout=args.query_timeout,
    )
    with args.output.open("x") as output:
        output.write(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
