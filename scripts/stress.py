"""Bounded synthetic stress test. Never accepts an existing database or remote target."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import resource
import sqlite3
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from dots_brain.auth import issue_client, read_connection, revoke_client
from dots_brain.bridge import connect
from dots_brain.errors import ConflictError, SuppressedError
from dots_brain.runtime import down, up
from dots_brain.store import Store


def summary(samples):
    ordered = sorted(samples)
    return {
        "count": len(ordered),
        **{
            f"p{p}_ms": round(
                ordered[min(len(ordered) - 1, math.ceil(len(ordered) * p / 100) - 1)] * 1000, 2
            )
            for p in (50, 95, 99)
        },
        "max_ms": round(max(ordered) * 1000, 2),
    }


def run(records: int, workers: int, http_calls: int) -> dict:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="dots-brain-stress-") as temporary:
        store = Store(Path(temporary) / "memory")
        store.initialize()
        payloads = [
            dict(
                content=f"Synthetic memory {i}: project decision about SQLite and shared context.",
                source="stress",
                account="synthetic",
                event_id=str(i),
                project=f"project-{i % 4}",
            )
            for i in range(records)
        ]

        def write(payload):
            tick = time.monotonic()
            record = store.remember(**payload)
            return record, time.monotonic() - tick

        with ThreadPoolExecutor(max_workers=workers) as pool:
            writes = list(pool.map(write, payloads))
            retries = list(pool.map(write, payloads))
        assert all(not item[0]["changed"] for item in retries)
        assert store.status()["memories"] == records

        conflict_source = payloads[0]

        def conflict(number):
            try:
                store.remember(
                    **{**conflict_source, "content": f"Contending update {number}"},
                    expected_revision=1,
                )
                return "updated"
            except ConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=workers) as pool:
            conflicts = list(pool.map(conflict, range(workers)))
        assert conflicts.count("updated") == 1

        searches = []
        for project in range(4):
            tick = time.monotonic()
            found = store.search("SQLite shared", projects=(f"project-{project}",), limit=50)
            searches.append(time.monotonic() - tick)
            assert found and all(row["project"] == f"project-{project}" for row in found)

        try:
            service = up(store, port=0)
            path = Path(temporary) / "client.json"
            client = issue_client(
                store,
                name="stress-client",
                scopes=["memory:read", "memory:write"],
                projects=["http-test"],
                days=1,
                output=path,
                url=service["url"],
            )

            async def http_load():
                measurements = []

                async def worker(number):
                    async with connect(path) as session:
                        for i in range(number, http_calls, workers):
                            tick = time.monotonic()
                            saved = await session.call_tool(
                                "memory_remember",
                                dict(
                                    content=f"HTTP synthetic record {i}",
                                    source="stress-http",
                                    account="synthetic",
                                    event_id=str(i),
                                    project="http-test",
                                ),
                            )
                            assert not saved.isError
                            read = await session.call_tool(
                                "memory_get", {"memory_id": saved.structuredContent["id"]}
                            )
                            assert not read.isError and read.structuredContent["event_id"] == str(i)
                            measurements.append(time.monotonic() - tick)
                        forbidden = await session.call_tool(
                            "memory_get", {"memory_id": writes[-1][0]["id"]}
                        )
                        assert forbidden.isError

                await asyncio.gather(*(worker(i) for i in range(workers)))
                return measurements

            http_samples = asyncio.run(asyncio.wait_for(http_load(), timeout=180))
            with open(f"/proc/{service['pid']}/status") as stream:
                process_status = stream.read()
            rss = next(
                int(line.split()[1])
                for line in process_status.splitlines()
                if line.startswith("VmHWM:")
            )
            revoke_client(store, client["client_id"])

            async def revoked():
                connection = read_connection(path)
                async with httpx.AsyncClient(timeout=5, trust_env=False) as http:
                    response = await http.post(
                        connection["url"],
                        headers={"Authorization": "Bearer " + connection["token"]},
                        json={},
                    )
                return response.status_code == 401

            assert asyncio.run(revoked())
            down(store)
            assert up(store)["read"]
            assert Store(store.directory).status()["memories"] == records + http_calls
        finally:
            down(store)

        for record, _ in writes[: min(records, 100)]:
            store.forget(record["id"])
        for payload in payloads[: min(records, 100)]:
            try:
                store.remember(**payload)
                raise AssertionError("Forgotten source was reimported.")
            except SuppressedError:
                pass
        with store.connection() as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
        return {
            "state": "passed",
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "cpu_count": os.cpu_count(),
            "records": records,
            "workers": workers,
            "writes": summary([v for _, v in writes]),
            "idempotent_retries": summary([v for _, v in retries]),
            "project_search": summary(searches),
            "http_write_read_pairs": summary(http_samples),
            "conflict_winners": conflicts.count("updated"),
            "service_peak_rss_kib": rss,
            "harness_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "semantic": "not_exercised",
            "checks": [
                "retry_deduplication",
                "conflict_atomicity",
                "project_isolation",
                "http_read_write",
                "revocation",
                "restart_persistence",
                "deletion_suppression",
                "sqlite_integrity",
                "foreign_keys",
            ],
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=5000)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--http-calls", type=int, default=500)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if (
        not 100 <= args.records <= 20000
        or not 1 <= args.workers <= 32
        or not 1 <= args.http_calls <= 2000
    ):
        parser.error("Bounds: 100–20000 records, 1–32 workers, 1–2000 HTTP pairs.")
    report = run(args.records, args.workers, args.http_calls)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
