import asyncio
import sys
import threading
import time

import pytest

from dots_brain.auth import issue_client
from dots_brain.bridge import connect
from dots_brain.runtime import down, up
from dots_brain.store import Store


@pytest.mark.skipif(sys.platform != "linux", reason="Requires actual Linux managed process control")
def test_waiting_writer_does_not_block_mcp_reads(tmp_path):
    store = Store(tmp_path / "memory")
    lock_acquired = threading.Event()
    release_lock = threading.Event()

    def hold_write_lock():
        with store.connection(write=True):
            lock_acquired.set()
            release_lock.wait(timeout=3)

    async def exercise():
        async with connect(store.directory / "writer.connection.json") as writer:
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
                    assert not (await writing).isError

    try:
        running = up(store, port=0)
        issue_client(
            store,
            name="synthetic-writer",
            scopes=["memory:read", "memory:write"],
            projects=["__dots_brain_probe__"],
            days=1,
            output=store.directory / "writer.connection.json",
            url=running["url"],
        )
        asyncio.run(asyncio.wait_for(exercise(), 10))
    finally:
        release_lock.set()
        down(store)
