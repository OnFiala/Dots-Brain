"""Provider adapters configure a verified bridge without exposing its credential."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import os
import sys
from pathlib import Path

import tomlkit

from .auth import read_connection
from .bridge import verify_command
from .errors import InputError
from .local import atomic_write, locked, read_json, write_json
from .runtime import credential, up
from .store import Store

PROVIDERS = {
    "claude-code": {"format": "json", "path": ".claude.json", "scope": "user"},
    "cursor": {"format": "json", "path": ".cursor/mcp.json", "scope": "user"},
    "codex": {"format": "toml", "path": ".codex/config.toml", "scope": "user"},
    "mcp-json": {"format": "json", "path": None, "scope": "explicit_file"},
}


def normalized_path(path: Path) -> Path:
    """Normalize spelling without following a configuration symlink."""
    return Path(os.path.abspath(os.path.normpath(str(path.expanduser()))))


def integration_key(provider: str, path: Path) -> str:
    canonical = normalized_path(path)
    return hashlib.sha256(f"{provider}\0{canonical}".encode()).hexdigest()[:24]


def registrations(directory: Path) -> dict:
    path = directory / "integrations.json"
    if not path.exists():
        return {"version": 1, "items": {}}
    document = read_json(path)
    if document.get("version") != 1 or not isinstance(document.get("items"), dict):
        raise InputError("Unsupported client registration file; existing state was preserved.")
    return document


def bridge_entry(connection: Path, directory: Path | None, provider: str) -> dict:
    args = ["-m", "dots_brain.cli", "bridge", "--credential-file", str(connection)]
    if directory is not None:
        args += ["--local-data-dir", str(directory)]
    entry = {"command": sys.executable, "args": args}
    if provider == "claude-code":
        entry["type"] = "stdio"
    return entry


def providers() -> dict:
    return {
        "adapters": PROVIDERS,
        "verification": "SDK bridge read/write; application activation is separate",
        "web": {
            "state": "blocked",
            "reason": "OAuth is available; public ingress and web-app setup are not automated.",
        },
        "capture": "not_implemented",
    }


def target_path(provider: str, config: Path | None) -> Path:
    if provider not in PROVIDERS:
        raise InputError("No tested adapter for this provider. Inspect dots-brain providers.")
    if config is not None:
        return normalized_path(config)
    default = PROVIDERS[provider]["path"]
    if default is None:
        raise InputError("A generic MCP client requires its actual configuration file path.")
    if provider == "claude-code" and os.environ.get("CLAUDE_CONFIG_DIR"):
        return normalized_path(Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json")
    return normalized_path(Path.home() / default)


def configure(path: Path, *, provider: str, entry: dict, replace_entry: dict | None = None) -> bool:
    with locked(path.with_name(path.name + ".dots-brain.lock"), create_parent=True):
        if path.is_symlink():
            raise InputError("Client configuration must not be a symbolic link.")
        original = path.read_text(encoding="utf-8") if path.exists() else ""
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
        if existing is not None and existing != replace_entry:
            raise InputError("A different dots-brain connection already exists; it was preserved.")
        servers["dots-brain"] = entry
        content = (
            tomlkit.dumps(document)
            if PROVIDERS[provider]["format"] == "toml"
            else json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        )
        # Detect an application write that occurred while this installer held its own lock.
        if (path.read_text(encoding="utf-8") if path.exists() else "") != original:
            raise InputError("The application changed its configuration; retry the connection.")
        if original:
            backup = path.with_name(path.name + ".before-dots-brain")
            if not backup.exists():
                atomic_write(backup, original, preserve=True)
        atomic_write(path, content, preserve=True)
        return True


def preflight_configure(
    path: Path, *, provider: str, entry: dict, replace_entry: dict | None = None
) -> None:
    """Reject an unsafe or conflicting client file before minting a credential."""
    if path.is_symlink():
        raise InputError("Client configuration must not be a symbolic link.")
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    document = (
        tomlkit.parse(original)
        if PROVIDERS[provider]["format"] == "toml"
        else json.loads(original)
        if original
        else {}
    )
    if not isinstance(document, dict):
        raise InputError("Client configuration must contain an object.")
    key = "mcp_servers" if PROVIDERS[provider]["format"] == "toml" else "mcpServers"
    servers = document.get(key, {})
    if not isinstance(servers, dict):
        raise InputError("The client's MCP server section is not an object.")
    existing = servers.get("dots-brain")
    if existing is not None and existing != entry and existing != replace_entry:
        raise InputError("A different dots-brain connection already exists; it was preserved.")


def local_connection_path(store: Store, value: str) -> Path:
    """Resolve current and legacy local registry paths after a data-dir move."""
    path = Path(value)
    if not path.is_absolute():
        return (store.directory / path).resolve(strict=False)
    if path.parent.name == "connections":
        return (store.directory / "connections" / path.name).resolve(strict=False)
    return path.resolve(strict=False)


def local_connection_record(store: Store, path: Path) -> str:
    return str(path.resolve(strict=False).relative_to(store.directory.resolve()))


def valid_local_registration(record: object, *, provider: str, target: Path) -> bool:
    return (
        isinstance(record, dict)
        and record.get("local") is True
        and record.get("provider") == provider
        and isinstance(record.get("connection_file"), str)
        and isinstance(record.get("client_id"), str)
        and isinstance(record.get("entry"), dict)
        and record.get("config_file") == str(target)
    )


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
            "required": "Verified HTTPS ingress, configured OAuth, and supported web-client setup.",
        }
    if connection is None and not store.path.is_file():
        raise InputError(
            "Initialize the chosen memory host with up, or provide an existing connection."
        )
    # A remote client needs a local registration but must not initialize a local database.
    with locked(store.directory / "installation.lock", create_parent=connection is not None):
        return _connect_client(
            store, provider=provider, config=config, connection=connection, projects=projects
        )


def _connect_client(store, *, provider, config, connection, projects):
    target = target_path(provider, config)
    registry = registrations(store.directory)
    key = integration_key(provider, target)
    local = connection is None
    previous = registry["items"].get(key)
    replace_entry = None
    created_credential = False
    if local:
        private = store.directory / "connections"
        if private.is_symlink():
            raise InputError("Managed connection storage must not be a symbolic link.")
        connection = private / f"{provider}-{key}.json"
        if previous is not None:
            if not valid_local_registration(previous, provider=provider, target=target):
                raise InputError("Invalid integration registration; existing state was preserved.")
            if previous["local"]:
                previous_path = local_connection_path(store, previous["connection_file"])
                if previous_path not in (connection, private / f"{provider}.json"):
                    raise InputError(
                        "Unexpected managed credential path; existing state was preserved."
                    )
                connection = previous_path
                replace_entry = previous["entry"]
        elif (private / f"{provider}.json").is_file() and target.is_file():
            # Preserve alpha.1 connections when their generated entry is unchanged.
            legacy = private / f"{provider}.json"
            text = target.read_text(encoding="utf-8")
            document = tomlkit.parse(text) if provider == "codex" else json.loads(text)
            section = "mcp_servers" if provider == "codex" else "mcpServers"
            if document.get(section, {}).get("dots-brain") == bridge_entry(
                legacy, store.directory, provider
            ):
                connection = legacy
        entry = bridge_entry(connection, store.directory, provider)
        preflight_configure(target, provider=provider, entry=entry, replace_entry=replace_entry)
        runtime = up(store)
        private.mkdir(mode=0o700, exist_ok=True)
        with locked(private / f"{provider}.lock"):
            prior_client_id = None
            if connection.exists():
                prior_client_id = read_connection(connection)["client_id"]
            client_id = credential(
                store,
                name=provider,
                path=connection,
                url=runtime["url"],
                projects=projects,
                replace_invalid=previous is not None,
            )
            created_credential = client_id != prior_client_id
    else:
        connection = connection.expanduser().resolve(strict=False)
        client_id = read_connection(connection)["client_id"]
        entry = bridge_entry(connection, None, provider)
        preflight_configure(target, provider=provider, entry=entry)
    entry = bridge_entry(connection, store.directory if local else None, provider)
    try:
        check = asyncio.run(
            asyncio.wait_for(
                verify_command(
                    entry,
                    # A prior successful local connection already proved writes for this
                    # credential. Repeating its deterministic probe would target a
                    # deliberately suppressed deleted probe record.
                    write=local and created_credential,
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
    except BaseException:
        if local and created_credential:
            with contextlib.suppress(InputError):
                from .auth import revoke_client

                revoke_client(store, client_id)
            connection.unlink(missing_ok=True)
        raise
    record = {
        "provider": provider,
        "config_file": str(target),
        "entry": entry,
        "connection_file": local_connection_record(store, connection) if local else str(connection),
        "client_id": client_id,
        "local": local,
    }
    previous_registry = copy.deepcopy(registry)
    registry["items"][key] = record
    # Record intent first so an interruption after configuration can be cleaned up.
    write_json(store.directory / "integrations.json", registry)
    try:
        changed = configure(target, provider=provider, entry=entry, replace_entry=replace_entry)
    except BaseException:
        write_json(store.directory / "integrations.json", previous_registry)
        if local and created_credential:
            with contextlib.suppress(InputError):
                from .auth import revoke_client

                revoke_client(store, client_id)
            connection.unlink(missing_ok=True)
        raise
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
