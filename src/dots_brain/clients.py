"""Provider adapters configure a verified bridge without exposing its credential."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import tomlkit

from .auth import read_connection
from .bridge import verify_command
from .errors import InputError
from .local import atomic_write, locked
from .runtime import credential, up
from .store import Store

PROVIDERS = {
    "claude-code": {"format": "json", "path": ".claude.json", "scope": "user"},
    "cursor": {"format": "json", "path": ".cursor/mcp.json", "scope": "user"},
    "codex": {"format": "toml", "path": ".codex/config.toml", "scope": "user"},
    "mcp-json": {"format": "json", "path": None, "scope": "explicit_file"},
}


def providers() -> dict:
    return {
        "adapters": PROVIDERS,
        "verification": "SDK bridge read/write; application activation is separate",
        "web": {"state": "blocked", "reason": "Web OAuth and public ingress are not implemented."},
        "capture": "not_implemented",
    }


def target_path(provider: str, config: Path | None) -> Path:
    if provider not in PROVIDERS:
        raise InputError("No tested adapter for this provider. Inspect dots-brain providers.")
    if config is not None:
        return config.expanduser().absolute()
    default = PROVIDERS[provider]["path"]
    if default is None:
        raise InputError("A generic MCP client requires its actual configuration file path.")
    if provider == "claude-code" and os.environ.get("CLAUDE_CONFIG_DIR"):
        return Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser().absolute() / ".claude.json"
    return Path.home() / default


def configure(path: Path, *, provider: str, entry: dict) -> bool:
    with locked(path.with_name(path.name + ".dots-brain.lock")):
        if path.is_symlink():
            raise InputError("Client configuration must not be a symbolic link.")
        original = path.read_text() if path.exists() else ""
        document = (
            tomlkit.parse(original)
            if PROVIDERS[provider]["format"] == "toml"
            else json.loads(original)
            if original
            else {}
        )
        if not isinstance(document, dict):
            # TOMLDocument implements dict in supported tomlkit versions.
            raise InputError("Client configuration must contain an object.")
        key = "mcp_servers" if PROVIDERS[provider]["format"] == "toml" else "mcpServers"
        servers = document.setdefault(key, {})
        if not isinstance(servers, dict):
            raise InputError("The client's MCP server section is not an object.")
        existing = servers.get("dots-brain")
        if existing == entry:
            return False
        if existing is not None:
            raise InputError("A different dots-brain connection already exists; it was preserved.")
        servers["dots-brain"] = entry
        content = (
            tomlkit.dumps(document)
            if PROVIDERS[provider]["format"] == "toml"
            else json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        )
        # Detect an application write that occurred while this installer held its own lock.
        if (path.read_text() if path.exists() else "") != original:
            raise InputError("The application changed its configuration; retry the connection.")
        if original:
            backup = path.with_name(path.name + ".before-dots-brain")
            if not backup.exists():
                atomic_write(backup, original)
        atomic_write(path, content)
        return True


def connect_client(
    store: Store,
    *,
    provider: str,
    config: Path | None = None,
    connection: Path | None = None,
    projects: list[str] | None = None,
) -> dict:
    if provider not in PROVIDERS:
        return {
            "state": "blocked",
            "provider": provider,
            "reason": "No verified configuration adapter is available for this provider.",
            "required": "For web clients: supported public HTTPS ingress and OAuth registration.",
        }
    target = target_path(provider, config)
    local = connection is None
    if local:
        runtime = up(store)
        private = store.directory / "connections"
        private.mkdir(mode=0o700, exist_ok=True)
        connection = private / f"{provider}.json"
        with locked(private / f"{provider}.lock"):
            client_id = credential(
                store, name=provider, path=connection, url=runtime["url"], projects=projects
            )
    else:
        connection = connection.expanduser().absolute()
        client_id = read_connection(connection)["client_id"]
    args = ["-m", "dots_brain.cli", "bridge", "--credential-file", str(connection)]
    if local:
        args += ["--local-data-dir", str(store.directory)]
    entry = {"command": sys.executable, "args": args}
    if provider == "claude-code":
        entry["type"] = "stdio"
    check = asyncio.run(
        asyncio.wait_for(
            verify_command(
                entry,
                write=local,
                project=(projects or ["default"])[0],
                cleanup_store=store if local else None,
            ),
            timeout=30,
        )
    )
    if check["state"] == "verification_failed":
        raise InputError(
            "The generated bridge failed verification; client configuration was preserved."
        )
    changed = configure(target, provider=provider, entry=entry)
    return {
        "state": "configured_verified_bridge",
        "provider": provider,
        "config_file": str(target),
        "client_id": client_id,
        "configuration_changed": changed,
        "read": check["read"],
        "write": check["write"],
        "capture": "not_implemented",
        "application_activation": "not_verified",
        "secret_isolation": False,
        "memory_host": "this_machine" if local else "existing_remote_connection",
        "next_action": "The client may need to reload MCP configuration or approve its tools.",
    }
