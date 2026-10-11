"""Keep client discovery and configuration tests away from the operator's home."""

import contextlib
import socket
import subprocess
import sys
import time

import httpx
import pytest


@pytest.fixture(autouse=True)
def private_owner_environment(tmp_path, monkeypatch):
    directory = tmp_path / "private-home"
    directory.mkdir()
    monkeypatch.setenv("HOME", str(directory))
    monkeypatch.setenv("XDG_DATA_HOME", str(directory / ".local/share"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(directory / ".config"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


@pytest.fixture
def free_ports():
    with contextlib.ExitStack() as stack:
        sockets = [stack.enter_context(socket.socket()) for _ in range(2)]
        for item in sockets:
            item.bind(("127.0.0.1", 0))
        return [item.getsockname()[1] for item in sockets]


@pytest.fixture
def http_process(tmp_path):
    @contextlib.contextmanager
    def start(store, port, public_port=None):
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
        if public_port is not None:
            command.extend(["--public-port", str(public_port)])
        with (tmp_path / "http-stderr.log").open("wb") as errors:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=errors)
            try:
                deadline = time.monotonic() + 10
                with httpx.Client(trust_env=False, timeout=0.3) as client:
                    while time.monotonic() < deadline and process.poll() is None:
                        try:
                            client.get(f"http://127.0.0.1:{port}/mcp")
                            break
                        except httpx.TransportError:
                            time.sleep(0.05)
                    else:
                        pytest.fail("Disposable HTTP process failed to become ready")
                yield process
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(5)

    return start
