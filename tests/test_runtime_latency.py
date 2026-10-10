"""Compare the two Linux launch paths over the same persistent MCP connection."""

import asyncio
import json
import statistics
import subprocess
import sys
import time

import pytest

from dots_brain.bridge import connect, verify_connection
from dots_brain.runtime import down, up
from dots_brain.store import Store


async def measure(credential):
    async with connect(credential) as session:
        await session.list_tools()
        samples = []
        for number in range(55):
            started = time.perf_counter()
            result = await session.call_tool("memory_status", {})
            assert not result.isError
            if number >= 5:
                samples.append(time.perf_counter() - started)
    return statistics.median(samples)


@pytest.mark.skipif(sys.platform != "linux", reason="managed up uses Linux process identity")
def test_managed_socket_has_no_delayed_ack_penalty(tmp_path, capsys):
    store = Store(tmp_path / "brain")
    started = up(store, port=0, semantic=False)
    credential = store.directory / "probe.connection.json"
    try:
        managed = asyncio.run(measure(credential))
        port = json.loads((store.directory / "service.json").read_text())["port"]
    finally:
        down(store)
    command = [
        sys.executable,
        "-m",
        "dots_brain.cli",
        "--data-dir",
        str(store.directory),
        "serve",
        "--transport",
        "http",
        "--port",
        str(port),
    ]
    with (tmp_path / "direct.log").open("wb") as log:
        process = subprocess.Popen(command, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    result = asyncio.run(verify_connection(credential))
                    assert result["read"] is True
                    break
                except Exception:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        pytest.fail("Disposable direct server did not become ready")
                    time.sleep(0.05)
            direct = asyncio.run(measure(credential))
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    measurements = {"managed_ms": managed * 1000, "direct_ms": direct * 1000, "samples": 50}
    with capsys.disabled():
        print("\nMCP socket comparison " + json.dumps(measurements))
    assert started["read"] is True
    # The reported regression adds about 40ms to every managed request. Allow
    # ordinary shared-runner noise while detecting that transport-level delay.
    assert managed - direct < 0.025, measurements
