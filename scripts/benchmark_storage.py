"""Standalone synthetic measurement; no downloads, model inference or real stores.

Run with the chosen source on PYTHONPATH in the same Python environment:
  python benchmark_storage.py seed /new/private/path 50000
  python benchmark_storage.py measure /new/private/path
  python benchmark_storage.py migrate /seeded/v3/path
A marker prevents opening a non-benchmark directory. Seed refuses an existing path.
"""

import hashlib
import json
import platform
import resource
import sqlite3
import statistics
import sys
import time
import uuid
from pathlib import Path

import numpy as np

from dots_brain.semantic import MODEL_ID, SemanticIndex
from dots_brain.store import SCHEMA_VERSION, Store, rebuild_fts, source_key

mode, path = sys.argv[1], Path(sys.argv[2])
marker = path / "SYNTHETIC-W4-BENCHMARK.json"
if mode == "seed":
    count = int(sys.argv[3])
    assert 100 <= count <= 100000
    path.mkdir(mode=0o700)
    marker.write_text(json.dumps({"rows": count, "kind": "synthetic-w4-v1"}))
else:
    meta = json.loads(marker.read_text())
    assert meta["kind"] == "synthetic-w4-v1"
    count = meta["rows"]
store = Store(path)
environment = {
    "machine": platform.node(),
    "platform": platform.platform(),
    "python": platform.python_version(),
    "sqlite": sqlite3.sqlite_version,
    "source": str(Path(sys.modules["dots_brain"].__file__).resolve()),
    "schema": SCHEMA_VERSION,
    "rows": count,
}


def content(i):
    return f"Synthetic neutral tile {i} has a round edge and a dotted reverse."


def memory_id(i):
    return str(uuid.uuid5(uuid.NAMESPACE_OID, f"neutral-fixture-{i}"))


def times(action, repeats):
    cpu, wall = [], []
    for i in range(repeats):
        c, w = time.process_time(), time.perf_counter()
        action(i)
        cpu.append((time.process_time() - c) * 1000)
        wall.append((time.perf_counter() - w) * 1000)
    return {
        "samples": repeats,
        "cpu_ms_median": statistics.median(cpu),
        "cpu_ms_range": [min(cpu), max(cpu)],
        "wall_ms_median": statistics.median(wall),
        "wall_ms_range": [min(wall), max(wall)],
    }


def peak_mib():
    scale = 1024 * 1024 if sys.platform == "darwin" else 1024
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / scale


if mode == "seed":
    store.initialize()
    vector = np.zeros(384, dtype="<f4")
    vector[0] = 1.0
    vector = vector.tobytes()
    stamp = "2026-01-01T00:00:00+00:00"
    with store.connection(write=True) as db:
        db.executemany(
            "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                (
                    memory_id(i),
                    source_key("fixture", "benchmark", str(i)),
                    "fixture",
                    "benchmark",
                    str(i),
                    "synthetic",
                    "",
                    1,
                    stamp,
                    stamp,
                )
                for i in range(count)
            ),
        )
        db.executemany(
            "INSERT INTO revisions VALUES (?,?,?,?,?,?,?,?)",
            (
                (
                    memory_id(i),
                    1,
                    content(i),
                    hashlib.sha256(content(i).encode()).hexdigest(),
                    None,
                    "",
                    "local-owner:stdio",
                    stamp,
                )
                for i in range(count)
            ),
        )
        db.executemany(
            "INSERT INTO semantic_state VALUES (?,?,?,?,?)",
            ((memory_id(i), MODEL_ID, 1, "indexed", 0) for i in range(count)),
        )
        db.executemany(
            "INSERT INTO semantic_chunks VALUES (?,?,?,?,?,?,?,?,?)",
            (
                (memory_id(i), 1, MODEL_ID, 0, "content", 0, len(content(i)), 384, vector)
                for i in range(count)
            ),
        )
        rebuild_fts(db)
    with store.connection() as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    result = {"database_bytes": store.path.stat().st_size}
elif mode == "measure":
    model = SemanticIndex.__new__(SemanticIndex)
    model.store, model.np, model.vector_dimension = store, np, 384
    if hasattr(model, "_initialize_runtime_state"):
        model._initialize_runtime_state()
    rss_before = peak_mib()

    def status(i):
        result = model.status()
        assert result["indexed"] == count and result["repair_pending"] == 0

    status_result = times(status, 5)
    rss_after = peak_mib()

    def update(i):
        result = store.remember(
            content=content(i) + " Updated.",
            source="fixture",
            account="benchmark",
            event_id=str(i),
            project="synthetic",
            expected_revision=1,
        )
        assert result["revision"] == 2

    update_result = times(update, 20)

    def forget(i):
        assert store.forget(memory_id(i), expected_revision=2)["deleted"]

    forget_result = times(forget, 20)
    result = {
        "status": status_result,
        "status_peak_rss_before_mib": rss_before,
        "status_peak_rss_after_mib": rss_after,
        "update": update_result,
        "forget": forget_result,
        "database_bytes_before": store.path.stat().st_size,
    }
elif mode == "migrate":
    from dots_brain.migrations import migrate_store

    before = store.path.stat().st_size
    cpu, wall = time.process_time(), time.perf_counter()
    migrated = migrate_store(store, apply=True, stop_guard=lambda: None)
    result = {
        "migration_result": migrated["state"],
        "from_version": migrated["from_version"],
        "to_version": migrated["to_version"],
        "cpu_seconds": time.process_time() - cpu,
        "wall_seconds": time.perf_counter() - wall,
        "before_bytes": before,
        "after_bytes": store.path.stat().st_size,
        "backup_bytes": Path(migrated["backup"]).stat().st_size,
    }
else:
    raise ValueError(mode)
print(json.dumps({"environment": environment, "mode": mode, **result}, sort_keys=True))
