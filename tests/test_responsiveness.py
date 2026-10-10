import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest

from dots_brain.bridge import connect
from dots_brain.cortex_connector import CortexConnectionConfig, CortexConnector
from dots_brain.runtime import down, up
from dots_brain.store import Store

requires_linux_lifecycle = pytest.mark.skipif(
    sys.platform != "linux", reason="managed subprocess lifecycle requires Linux /proc and pidfd"
)


@requires_linux_lifecycle
def test_waiting_writer_does_not_block_mcp_reads(tmp_path):
    store = Store(tmp_path / "memory")
    lock_acquired = threading.Event()
    release_lock = threading.Event()

    def hold_write_lock():
        with store.connection(write=True):
            lock_acquired.set()
            release_lock.wait(timeout=3)

    async def exercise():
        async with connect(store.directory / "probe.connection.json") as writer:
            async with connect(store.directory / "probe.connection.json") as reader:
                thread = threading.Thread(target=hold_write_lock)
                thread.start()
                await asyncio.to_thread(lock_acquired.wait, 3)
                writing = asyncio.create_task(
                    writer.call_tool(
                        "memory_remember",
                        dict(
                            content="Waiting synthetic write",
                            source="test",
                            account="a",
                            event_id="blocked",
                            project="__dots_brain_probe__",
                        ),
                    )
                )
                try:
                    await asyncio.sleep(0.2)
                    started = time.monotonic()
                    result = await reader.call_tool("memory_status", {})
                    elapsed = time.monotonic() - started
                    assert not result.isError
                    assert elapsed < 1.5, f"Read blocked behind SQLite writer for {elapsed:.2f}s"
                finally:
                    release_lock.set()
                    await asyncio.to_thread(thread.join, 5)
                    await writing

    try:
        up(store, port=0)
        asyncio.run(asyncio.wait_for(exercise(), 10))
    finally:
        release_lock.set()
        down(store)


def test_cortex_ledger_does_not_block_event_loop():
    """The CORTEX write path sends its synchronous ledger work to a thread."""

    class Policy:
        principal = "oauth-grant:synthetic"
        projects = ("alpha",)

        def require(self, scope):
            assert scope == "cortex:write"

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class SlowLedger:
        def __init__(self):
            self.row = None

        def get(self, operation_id):
            entered.set()
            release.wait(timeout=5)
            finished.set()
            return self.row

        def create_planned(self, record):
            self.row = {**record, "state": "planned", "receipt_json": None}

        def claim_sending(self, operation_id, request_digest):
            self.row["state"] = "sending"
            return True

        def mark_acknowledged(self, operation_id, *, receipt, upstream_object_id):
            self.row.update(
                state="acknowledged",
                receipt_json='{"event_id":"synthetic"}',
                upstream_object_id=upstream_object_id,
            )

        def mark_uncertain(self, operation_id, *, error_code):
            self.row.update(state="uncertain", error_code=error_code)

        def release_planned(self, operation_id, *, error_code):
            self.row.update(state="planned", error_code=error_code)

    class Transport:
        async def call_tool(self, name, arguments):
            return {"event_id": "synthetic"}

    connector = CortexConnector(
        CortexConnectionConfig(
            endpoint="http://127.0.0.1:9/mcp",
            token_file=Path(__file__),
            project_mapping=(("alpha", "cortex-alpha"),),
        ),
        transport=Transport(),
        ledger=SlowLedger(),
        content_guard=lambda value: None,
    )

    async def exercise():
        write = asyncio.create_task(
            connector.write_note(
                policy=Policy(),
                project="alpha",
                source_ref="dots://memory/synthetic@1",
                title="Synthetic",
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert not finished.is_set(), "The synchronous ledger blocked the event loop"
        finally:
            release.set()
            await write

    asyncio.run(exercise())
