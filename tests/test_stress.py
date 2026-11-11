import runpy
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.skipif(sys.platform != "linux", reason="Exercises the Linux process lifecycle.")
def test_bounded_stress_cleans_up_its_winning_revision():
    script = Path(__file__).resolve().parents[1] / "scripts" / "stress.py"
    report = runpy.run_path(str(script))["run"](records=100, workers=2, http_calls=4)
    assert report["state"] == "passed"
    assert report["conflict_winners"] == 1
    assert report["http_write_read_pairs"]["count"] == 4
    assert "deletion_suppression" in report["checks"]


@pytest.mark.parametrize(
    ("status_update", "search_update", "error", "late_ready"),
    [
        ({}, {}, None, False),
        ({"state": "degraded"}, {}, "Semantic index", False),
        ({"indexed": 0}, {}, "Semantic index", False),
        ({}, {"mode": "fulltext"}, "hybrid", False),
        ({}, {"semantic": "degraded"}, "hybrid", False),
        ({}, {"mode": "fulltext", "semantic": "degraded"}, "hybrid", False),
        ({}, {}, "declared budget", True),
    ],
    ids=[
        "ready",
        "degraded-index",
        "empty-index",
        "fulltext",
        "degraded-search",
        "fallback",
        "late-ready",
    ],
)
@pytest.mark.parametrize("runs", [1, 3])
def test_semantic_stress_rejects_fallback_with_correct_hits(
    tmp_path, monkeypatch, status_update, search_update, error, runs, late_ready
):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    run = runpy.run_path(str(scripts / "stress_semantic.py"))["run"]
    namespace = run.__globals__
    rows, stopped, calls = [], [], []
    clock = [0.0]
    monkeypatch.setitem(namespace, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def start(store, **kwargs):
        with store.connection() as db:
            rows.extend({"id": row[0]} for row in db.execute("SELECT id FROM memories"))
        return {"pid": 999999, "url": "http://127.0.0.1:1/mcp"}

    class Session:
        async def call_tool(self, name, arguments):
            if name == "memory_status":
                clock[0] = 901.0 if late_ready else 1.0
                payload = {
                    "semantic": {"state": "ready", "indexed": 3, "pending": 0, **status_update}
                }
            else:
                calls.append(name)
                payload = {"mode": "hybrid", "semantic": "enabled", "results": rows}
                if len(calls) == (2 if runs == 1 else 8):
                    payload.update(search_update)
            return SimpleNamespace(isError=False, structuredContent=payload)

    @asynccontextmanager
    async def connect(credential):
        yield Session()

    # Model, process and MCP boundaries are doubles; the disposable store is real.
    monkeypatch.setitem(namespace, "FILES", {})
    monkeypatch.setitem(namespace, "up", start)
    monkeypatch.setitem(namespace, "down", lambda store: stopped.append(store.directory))
    monkeypatch.setitem(namespace, "issue_client", lambda *args, **kwargs: None)
    monkeypatch.setitem(namespace, "connect", connect)
    read_text = Path.read_text

    def read_status(path, *args, **kwargs):
        if path == Path("/proc/999999/status"):
            return "VmHWM: 1234 kB\n"
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_status)
    if error or late_ready:
        with pytest.raises(AssertionError, match="declared budget" if late_ready else error):
            run(tmp_path, records=3, workers=2, queries=6, runs=runs, index_timeout=900)
    else:
        report = run(tmp_path, records=3, workers=2, queries=6, runs=runs, index_timeout=900)
        assert report["state"] == "passed"
        assert report["queries"]["count"] == 6
        assert report["synthetic_recall_at_3"] == 1
        assert len(report["runs"]) == runs
        assert all(row["queries"]["count"] == 6 for row in report["runs"])
        assert len(calls) == 6 * runs
    assert len(stopped) == 1
    assert not stopped[0].exists()
