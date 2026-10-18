"""Verify a core-only installed wheel with synthetic stores and real stdio/HTTP MCP."""

import asyncio
import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import dots_brain
from dots_brain.auth import authenticate, issue_client
from dots_brain.bridge import verify_connection
from dots_brain.errors import NotFoundError, SuppressedError
from dots_brain.oauth import OAuthStore
from dots_brain.oauth import configure as configure_oauth
from dots_brain.operations import backup_store, migrate, restore_store
from dots_brain.store import Store


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


async def verify_mcp(store):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "dots_brain.cli", "--data-dir", str(store.directory), "serve"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            require(initialized.serverInfo.version == dots_brain.__version__, "MCP version differs")
            result = await session.call_tool("memory_status", {})
            require(not result.isError, "Installed MCP read failed")
            result = await session.call_tool(
                "memory_remember",
                {
                    "content": "Synthetic wheel MCP",
                    "source": "wheel",
                    "account": "test",
                    "event_id": "mcp",
                },
            )
            require(not result.isError, "Installed MCP write failed")


def verify_service_command(root, directory):
    """Run the template command with only installation paths and ports substituted."""
    store = Store(directory / "http-store")
    store.initialize()
    configure_oauth(store, "https://wheel.example.invalid")
    lines = (root / "deploy/systemd/dots-brain.service").read_text().splitlines()
    command = shlex.split(
        next(line.removeprefix("ExecStart=") for line in lines if line.startswith("ExecStart="))
    )
    command[0] = str(Path(sys.executable).with_name("dots-brain"))
    with ExitStack() as stack:
        listeners = [stack.enter_context(socket.socket()) for _ in range(2)]
        for listener in listeners:
            listener.bind(("127.0.0.1", 0))
        ports = [listener.getsockname()[1] for listener in listeners]
    for option, value in (
        ("--data-dir", store.directory),
        ("--port", ports[0]),
        ("--public-port", ports[1]),
    ):
        command[command.index(option) + 1] = str(value)
    environment = os.environ.copy()
    for line in lines:
        if line.startswith("Environment="):
            for assignment in shlex.split(line.removeprefix("Environment=")):
                key, value = assignment.split("=", 1)
                environment[key] = value
    credential = directory / "http-credential.json"
    endpoint = f"http://127.0.0.1:{ports[0]}/mcp"
    issue_client(
        store,
        name="wheel",
        scopes=["memory:read", "memory:write", "memory:forget"],
        projects=["wheel"],
        days=1,
        output=credential,
        url=endpoint,
    )
    with (directory / "http-stderr.log").open("w+") as errors:
        process = subprocess.Popen(
            command, cwd=store.directory, env=environment, stdout=subprocess.DEVNULL, stderr=errors
        )
        try:
            deadline = time.monotonic() + 10
            with httpx.Client(trust_env=False, timeout=0.3) as client:
                while time.monotonic() < deadline and process.poll() is None:
                    try:
                        require(
                            client.get(endpoint).status_code == 401,
                            "Private listener accepted an anonymous request",
                        )
                        break
                    except httpx.TransportError:
                        time.sleep(0.05)
                else:
                    errors.seek(0)
                    raise RuntimeError("Template command failed to start: " + errors.read())
                token = json.loads(credential.read_text())["token"]
                public = client.get(
                    f"http://127.0.0.1:{ports[1]}/mcp", headers={"Authorization": f"Bearer {token}"}
                )
                require(public.status_code == 401, "Public listener accepted a static token")
            result = asyncio.run(
                asyncio.wait_for(
                    verify_connection(credential, write=True, project="wheel"), timeout=20
                )
            )
            require(result["state"] == "verified_read_write", "Installed HTTP MCP failed")
            require(process.poll() is None, "Template process exited during verification")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def main():
    root = Path(__file__).resolve().parents[1]
    require(
        not Path(dots_brain.__file__).resolve().is_relative_to(root), "Imported source, not wheel"
    )
    require(importlib.util.find_spec("fastembed") is None, "Expected core-only dependencies")
    fixture = root / "tests/fixtures/historical/v1-263c386"
    manifest = json.loads((fixture / "manifest.json").read_text())
    for name, digest in manifest["files"].items():
        require(
            hashlib.sha256((fixture / name).read_bytes()).hexdigest() == digest, "Fixture changed"
        )
    with tempfile.TemporaryDirectory(prefix="dots-wheel-") as name:
        directory = Path(name)
        shutil.copytree(fixture, directory / "store")
        store = Store(directory / "store")
        result = migrate(store, apply=True, writers_stopped=True, backup=None)
        require(result["state"] == "migrated", "Released v1 migration failed")
        require(store.get(manifest["memory_id"])["revision"] == 2, "Historical revisions lost")
        require(
            authenticate(store, "public-synthetic-upgrade-probe") is not None, "Static grant lost"
        )
        require(
            OAuthStore(store).grants()["grants"][0]["id"] == manifest["oauth_grant_id"],
            "OAuth history lost",
        )
        fields = dict(
            content="Synthetic wheel revision one", source="wheel", account="test", event_id="one"
        )
        saved = store.remember(**fields)
        updated = store.remember(
            **(fields | {"content": "Synthetic revision two"}), expected_revision=1
        )
        require(updated["revision"] == 2, "Wheel update failed")
        snapshot = directory / "before-deletion.sqlite3"
        backup_store(store, snapshot)
        require(store.forget(saved["id"], expected_revision=2)["deleted"], "Wheel deletion failed")
        try:
            store.remember(**fields)
        except SuppressedError:
            pass
        else:
            raise RuntimeError("Forgotten source was reimported")
        restored = Store(directory / "restored")
        result = restore_store(snapshot, restored, latest_deletions=store)
        require(result["state"] == "restored_disabled", "Restore failed")
        require(restored.get(manifest["memory_id"])["revision"] == 2, "Restore lost prior revision")
        try:
            restored.get(saved["id"])
        except NotFoundError:
            pass
        else:
            raise RuntimeError("Restore resurrected deleted memory")
        with restored.connection() as db:
            require(
                db.execute("SELECT id FROM oauth_grants").fetchone()[0]
                == manifest["oauth_grant_id"],
                "Restored OAuth history lost",
            )
        asyncio.run(asyncio.wait_for(verify_mcp(store), timeout=20))
        verify_service_command(root, directory)
    print(
        json.dumps(
            {
                "state": "passed",
                "version": dots_brain.__version__,
                "package": dots_brain.__file__,
                "checks": [
                    "v1_upgrade",
                    "revision",
                    "grants",
                    "deletion",
                    "restore",
                    "stdio_mcp",
                    "core_service_command",
                    "http_mcp",
                    "public_static_rejection",
                ],
            }
        )
    )


if __name__ == "__main__":
    main()
