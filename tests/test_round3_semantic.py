"""Synthetic regression evidence for bounded semantic status, repair and retry."""

import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from dots_brain import semantic
from dots_brain.semantic import MODEL_ID, SemanticIndex
from dots_brain.store import Store


@pytest.fixture
def make_index(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    monkeypatch.setattr(semantic, "verify_model_artifacts", lambda store: tmp_path)
    monkeypatch.setitem(
        sys.modules, "onnxruntime", SimpleNamespace(disable_telemetry_events=lambda: None)
    )
    monkeypatch.setitem(
        sys.modules,
        "fastembed",
        SimpleNamespace(
            TextEmbedding=lambda **kwargs: SimpleNamespace(
                model=SimpleNamespace(tokenizer=object())
            )
        ),
    )
    store = Store(tmp_path / "memory")
    store.initialize()

    def build():
        result = SemanticIndex(store)
        result.vector_dimension = 2
        result._chunks = lambda text: [(0, len(text))]
        result._embed = lambda text: np.array([1.0, 0.0], dtype="<f4")
        return result

    return build


def remember(index, number, *, project="alpha"):
    return index.store.remember(
        content=f"Synthetic neutral fact {number}",
        source="fixture",
        account="owner",
        event_id=f"event-{number}",
        project=project,
    )


def seed_failed(index, count):
    records = [remember(index, number) for number in range(count)]
    with index.store.connection(write=True) as db:
        db.executemany(
            "INSERT INTO semantic_state(memory_id,model,revision,state,truncated) "
            "VALUES (?,?,1,'failed',0)",
            [(record["id"], MODEL_ID) for record in records],
        )
    return records


def test_status_aggregates_structure_without_reading_numeric_vectors(make_index, monkeypatch):
    index = make_index()
    records = [
        remember(index, number, project="beta" if number == 4 else "alpha") for number in range(5)
    ]
    index.index()
    with index.store.connection(write=True) as db:
        db.execute("DELETE FROM semantic_chunks WHERE memory_id=?", (records[0]["id"],))
        db.execute(
            "UPDATE semantic_chunks SET vector=? WHERE memory_id=?", (b"bad", records[1]["id"])
        )
        db.execute("UPDATE semantic_chunks SET dimension=3 WHERE memory_id=?", (records[2]["id"],))
    monkeypatch.setattr(index, "_valid_vector", lambda *args: pytest.fail("status read vectors"))
    connection = index.store.connection

    @contextmanager
    def reject_vector_materialization(*, write=False):
        with connection(write=write) as db:
            factory = db.row_factory

            def guard(cursor, row):
                assert not any(isinstance(value, bytes) for value in row), (
                    "status materialized a BLOB"
                )
                return factory(cursor, row)

            db.row_factory = guard
            yield db

    monkeypatch.setattr(index.store, "connection", reject_vector_materialization)
    status = index.status(projects=("alpha",))
    assert (status["indexed"], status["pending"], status["repair_pending"]) == (1, 3, 3)
    assert status["state"] == "pending_index"
    assert index.status(projects=("beta",))["indexed"] == 1
    assert index.status(projects=())["indexed"] == 0


@pytest.mark.parametrize("damage", ["missing", "malformed", "nan"])
def test_repair_drain_reaches_damage_after_valid_first_slices(make_index, damage, monkeypatch):
    index = make_index()
    records = sorted([remember(index, number) for number in range(13)], key=lambda row: row["id"])
    index.index()
    target = records[-1]
    with index.store.connection(write=True) as db:
        if damage == "missing":
            db.execute("DELETE FROM semantic_chunks WHERE memory_id=?", (target["id"],))
        else:
            value = (
                b"bad"
                if damage == "malformed"
                else index.np.array([index.np.nan, 0.0], dtype="<f4").tobytes()
            )
            db.execute(
                "UPDATE semantic_chunks SET vector=? WHERE memory_id=?", (value, target["id"])
            )
    # A fresh operator process must not stop after validating only its first slice.
    fresh = make_index()
    repaired = 0
    checks = []
    original_valid = fresh._valid_vector

    def valid(vector, dimension):
        checks.append(1)
        return original_valid(vector, dimension)

    monkeypatch.setattr(fresh, "_valid_vector", valid)
    for _ in range(12):
        checks.clear()
        result = fresh.index(summary=False)
        assert len(checks) <= semantic.REPAIR_SCAN_BATCH_SIZE
        repaired += result["indexed_now"]
        if result["examined"] == 0 and result.get("repair_pass_complete", True):
            break
    else:
        pytest.fail("quiescent repair drain did not terminate")
    assert repaired == 1
    with fresh.store.connection() as db:
        chunks = db.execute(
            "SELECT vector,dimension FROM semantic_chunks WHERE memory_id=?", (target["id"],)
        ).fetchall()
    assert chunks and all(fresh._valid_vector(row["vector"], row["dimension"]) for row in chunks)


def test_cli_index_finishes_repair_pass_beyond_first_valid_slice(make_index, monkeypatch):
    from dots_brain.cli import parser, run

    index = make_index()
    records = sorted([remember(index, number) for number in range(13)], key=lambda row: row["id"])
    index.index()
    with index.store.connection(write=True) as db:
        db.execute("DELETE FROM semantic_chunks WHERE memory_id=?", (records[-1]["id"],))
    fresh = make_index()
    monkeypatch.setattr(semantic, "SemanticIndex", lambda store: fresh)
    result = run(parser().parse_args(["--data-dir", str(fresh.store.directory), "index"]))
    assert result["indexed_this_run"] == 1
    assert result["indexed"] == 13
    assert result["repair_pending"] == 0


def test_failed_backlog_has_global_retry_rate_and_new_source_priority(make_index, monkeypatch):
    index = make_index()
    seed_failed(index, 300)
    clock = [0.0]
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])
    attempts = []

    def fail(text):
        attempts.append(clock[0])
        raise RuntimeError("synthetic failure")

    index._embed = fail
    for second in range(30):
        clock[0] = float(second)
        index.index(summary=False)
    assert attempts == [0, 5, 10, 15, 20, 25]
    assert index.status()["failed"] == 300
    fresh = remember(index, "new")
    original = index._embed
    index._embed = lambda text: index.np.array([1.0, 0.0], dtype="<f4")
    clock[0] = 30
    assert index.index(batch_size=1, summary=False)["indexed_now"] == 1
    assert index.search("new")[0]["id"] == fresh["id"]
    index._embed = original


def test_repeated_failure_backs_off_then_recovers_without_manual_reset(make_index, monkeypatch):
    index = make_index()
    record = remember(index, "transient")
    clock = [0.0]
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])
    good_embed = index._embed
    index._embed = lambda text: (_ for _ in ()).throw(RuntimeError("synthetic failure"))
    for moment, due in [
        (0, True),
        (29, False),
        (30, True),
        (60, False),
        (89, False),
        (90, True),
        (209, False),
        (210, True),
    ]:
        clock[0] = moment
        assert bool(index.index(summary=False)["examined"]) is due
    key = (record["id"], 1)
    assert index._failed_retry_deadlines[key] == 450
    index._embed = good_embed
    clock[0] = 450
    assert index.index()["failed"] == 0
    assert key not in index._failed_retry_deadlines
    assert key not in index._failed_retry_attempts


def test_backoff_caps_and_explicit_retry_clears_process_limits(make_index, monkeypatch):
    index = make_index()
    record = remember(index, "cap")
    clock = [0.0]
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])
    good_embed = index._embed
    index._embed = lambda text: (_ for _ in ()).throw(RuntimeError("synthetic failure"))
    key = (record["id"], 1)
    for _ in range(12):
        assert index.index(summary=False)["examined"] == 1
        deadline = index._failed_retry_deadlines[key]
        delay = deadline - clock[0]
        assert delay <= 3600
        clock[0] = deadline
    assert delay == 3600
    assert index.retry_failed() == 1
    index._embed = good_embed
    assert index.index()["indexed"] == 1


def test_failed_discovery_advances_even_with_tracked_due_rows(make_index, monkeypatch):
    index = make_index()
    records = sorted(seed_failed(index, 80), key=lambda row: row["id"])
    clock = [0.0]
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])
    # A full tracked due page must not prevent discovery of the rest of the store.
    index._failed_retry_deadlines = {(row["id"], 1): 0 for row in records[:16]}
    index._embed = lambda text: (_ for _ in ()).throw(RuntimeError("synthetic failure"))
    for second in range(0, 25, 5):
        clock[0] = second
        index.index(summary=False)
    assert (records[-1]["id"], 1) in index._failed_retry_deadlines
