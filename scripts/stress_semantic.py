"""Exercise prepared CPU embeddings in an isolated synthetic memory service."""

import argparse
import asyncio
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from stress import summary

from dots_brain.auth import issue_client
from dots_brain.bridge import connect
from dots_brain.runtime import down, up
from dots_brain.semantic import FILES, MODEL_ID, model_directory
from dots_brain.store import Store


def run(model: Path, records: int, workers: int, queries: int) -> dict:
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
        for i in range(records - 3):
            store.remember(
                content=f"Synthetic planning note {i}: review the team task list on Monday.",
                source="synthetic",
                account="stress",
                event_id=str(i),
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
            expected[key] = store.remember(
                content=content, source="synthetic", account="stress", event_id=key
            )["id"]
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
                async with connect(credential) as session:
                    deadline = time.monotonic() + 180
                    while True:
                        status = await session.call_tool("memory_status", {})
                        assert not status.isError
                        if status.structuredContent["semantic"]["pending"] == 0:
                            break
                        if time.monotonic() >= deadline:
                            raise AssertionError("Background indexing exceeded 180 seconds.")
                        await asyncio.sleep(0.5)
                indexing_seconds = time.monotonic() - started
                prompts = [
                    ("Kterému jídlu se mám kvůli alergii vyhnout?", "allergy"),
                    ("Kde je uložená databáze aplikace?", "storage"),
                    ("Do kterého města pojedu na výlet?", "city"),
                    ("Which food must I avoid because of my allergy?", "allergy"),
                    ("What database does the application use?", "storage"),
                    ("Where am I going on my upcoming trip?", "city"),
                ]
                samples, hits = [], []

                async def worker(number):
                    async with connect(credential) as session:
                        for i in range(number, queries, workers):
                            query, key = prompts[i % len(prompts)]
                            tick = time.monotonic()
                            result = await session.call_tool(
                                "memory_search", {"query": query, "limit": 3}
                            )
                            assert not result.isError
                            rows = result.structuredContent["results"]
                            hits.append(any(row["id"] == expected[key] for row in rows))
                            samples.append(time.monotonic() - tick)

                await asyncio.gather(*(worker(i) for i in range(workers)))
                return indexing_seconds, samples, hits

            indexing_seconds, samples, hits = asyncio.run(asyncio.wait_for(exercise(), timeout=240))
            status = Path(f"/proc/{service['pid']}/status").read_text()
            rss = next(
                int(line.split()[1]) for line in status.splitlines() if line.startswith("VmHWM:")
            )
            assert all(hits), (
                f"Synthetic bilingual retrieval missed {len(hits) - sum(hits)} queries."
            )
            return {
                "state": "passed",
                "model": MODEL_ID,
                "records": records,
                "workers": workers,
                "queries": summary(samples),
                "synthetic_recall_at_3": sum(hits) / len(hits),
                "indexing_including_startup_seconds": round(indexing_seconds, 2),
                "service_peak_rss_kib": rss,
                "elapsed_seconds": round(time.monotonic() - started, 2),
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
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not 3 <= args.records <= 300 or not 1 <= args.workers <= 8 or not 6 <= args.queries <= 200:
        parser.error("Bounds: 3–300 records, 1–8 workers, 6–200 queries.")
    report = run(args.model_dir, args.records, args.workers, args.queries)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
