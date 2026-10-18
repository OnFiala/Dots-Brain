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
    ("status_update", "search_update", "error"),
    [
        ({}, {}, None),
        ({"state": "degraded"}, {}, "Semantic index"),
        ({"indexed": 0}, {}, "Semantic index"),
        ({}, {"mode": "fulltext"}, "hybrid"),
        ({}, {"semantic": "degraded"}, "hybrid"),
        ({}, {"mode": "fulltext", "semantic": "degraded"}, "hybrid"),
    ],
    ids=["ready", "degraded-index", "empty-index", "fulltext", "degraded-search", "fallback"],
)
def test_semantic_stress_rejects_fallback_with_correct_hits(
    tmp_path, monkeypatch, status_update, search_update, error
):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    run = runpy.run_path(str(scripts / "stress_semantic.py"))["run"]
    namespace = run.__globals__
    rows, stopped, calls = [], [], []

    def start(store, **kwargs):
        with store.connection() as db:
            rows.extend({"id": row[0]} for row in db.execute("SELECT id FROM memories"))
        return {"pid": 999999, "url": "http://127.0.0.1:1/mcp"}

    class Session:
        async def call_tool(self, name, arguments):
            if name == "memory_status":
                payload = {
                    "semantic": {"state": "ready", "indexed": 3, "pending": 0, **status_update}
                }
            else:
                calls.append(name)
                payload = {"mode": "hybrid", "semantic": "enabled", "results": rows}
                if len(calls) == 2:
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
    if error:
        with pytest.raises(AssertionError, match=error):
            run(tmp_path, records=3, workers=2, queries=6)
    else:
        report = run(tmp_path, records=3, workers=2, queries=6)
        assert report["state"] == "passed"
        assert report["queries"]["count"] == 6
        assert report["synthetic_recall_at_3"] == 1
    assert len(stopped) == 1
    assert not stopped[0].exists()
