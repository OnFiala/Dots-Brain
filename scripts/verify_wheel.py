"""Verify an installed wheel with the released synthetic v1 fixture and real stdio MCP."""

import asyncio
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import dots_brain
from dots_brain.auth import authenticate
from dots_brain.errors import NotFoundError, SuppressedError
from dots_brain.oauth import OAuthStore
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


def main():
    root = Path(__file__).resolve().parents[1]
    require(
        not Path(dots_brain.__file__).resolve().is_relative_to(root), "Imported source, not wheel"
    )
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
    print(
        json.dumps(
            {
                "state": "passed",
                "version": dots_brain.__version__,
                "package": dots_brain.__file__,
                "checks": ["v1_upgrade", "revision", "grants", "deletion", "restore", "stdio_mcp"],
            }
        )
    )


if __name__ == "__main__":
    main()
