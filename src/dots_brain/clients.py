"""Provider adapters configure a verified bridge without exposing its credential."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import sys
from contextlib import ExitStack
from pathlib import Path

import tomlkit

from .auth import read_connection, revoke_client
from .bridge import verify_command
from .errors import InputError
from .local import atomic_write, locked, read_json, write_json
from .runtime import credential, private_endpoint, state_path, stop_process, up
from .store import Store

PROVIDERS = {
    "claude-code": {"format": "json", "path": ".claude.json", "scope": "user"},
    "cursor": {"format": "json", "path": ".cursor/mcp.json", "scope": "user"},
    "codex": {"format": "toml", "path": ".codex/config.toml", "scope": "user"},
    "mcp-json": {"format": "json", "path": None, "scope": "explicit_file"},
}


def integration_key(provider: str, path: Path) -> str:
    return hashlib.sha256(f"{provider}\0{path}".encode()).hexdigest()[:24]


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
        "verification": "SDK bridge read only; application activation is separate",
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
        return config.expanduser().absolute()
    default = PROVIDERS[provider]["path"]
    if default is None:
        raise InputError("A generic MCP client requires its actual configuration file path.")
    if provider == "claude-code" and os.environ.get("CLAUDE_CONFIG_DIR"):
        return Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser().absolute() / ".claude.json"
    return Path.home() / default


def legacy_notice(provider, path, entry):
    args = entry.get("args", []) if isinstance(entry, dict) else []
    if (
        isinstance(args, list)
        and len(args) == 7
        and all(isinstance(arg, str) for arg in args)
        and args[:4] == ["-m", "dots_brain.cli", "bridge", "--credential-file"]
        and args[5] == "--local-data-dir"
        and Path(args[4]).name == f"{provider}.json"
    ):
        section = "mcp_servers" if provider == "codex" else "mcpServers"
        command = shlex.join(
            ["dots-brain", "--data-dir", args[6], "connect", provider, "--config", str(path)]
        )
        return (
            f"Unsupported 0.2 client: manually remove only {section}.dots-brain from {path}, "
            f"preserving other settings and credentials; then run {command}."
        )
    return None


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def configure(
    path: Path,
    *,
    provider: str,
    entry: dict,
    dry_run=False,
    rollback=None,
    ownership=None,
    publish=None,
) -> bool:
    with locked(path.with_name(path.name + ".dots-brain.lock")):
        if path.is_symlink():
            raise InputError("Client configuration must not be a symbolic link.")
        existed = path.exists()
        original = path.read_text(encoding="utf-8") if existed else ""
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
            if not dry_run and publish is not None:
                publish()
            return False
        if existing is not None:
            raise InputError(
                legacy_notice(provider, path, existing)
                or "A different dots-brain connection already exists; it was preserved."
            )
        servers["dots-brain"] = entry
        content = (
            tomlkit.dumps(document)
            if PROVIDERS[provider]["format"] == "toml"
            else json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        )
        if dry_run:
            return True
        # Detect an application write that occurred while this installer held its own lock.
        if (path.read_text(encoding="utf-8") if path.exists() else "") != original:
            raise InputError("The application changed its configuration; retry the connection.")
        backup = path.with_name(path.name + ".before-dots-brain")
        new_backup = bool(original) and not (backup.exists() or backup.is_symlink())
        if ownership is not None:
            ownership.update(
                entry_created=True,
                config_created=not existed,
                config_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                backup_hash=hashlib.sha256(original.encode("utf-8")).hexdigest()
                if new_backup
                else None,
            )
        if publish is not None:
            publish()  # Ownership intent precedes the configuration and backup writes.
        if new_backup:
            atomic_write(backup, original, expected=(False, b""))
            if rollback is not None:
                rollback.callback(
                    _undo_configuration, path, original, content, existed, backup, rollback
                )
        if rollback is not None:
            rollback.callback(_undo_configuration, path, original, content, existed, None, rollback)
        atomic_write(path, content, preserve=True, expected=(existed, original.encode("utf-8")))
        return True


def _undo_configuration(path, original, written, existed, new_backup, rollback):
    try:
        with locked(path.with_name(path.name + ".dots-brain.lock")):
            if path.is_symlink():
                raise InputError("Concurrent configuration change.")
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current not in (original, written):
                raise InputError("Concurrent configuration change.")
            if current == written:
                if existed:
                    atomic_write(
                        path, original, preserve=True, expected=(True, current.encode("utf-8"))
                    )
                else:
                    path.unlink(missing_ok=True)
            if (
                new_backup is not None
                and not new_backup.is_symlink()
                and new_backup.is_file()
                and new_backup.read_text(encoding="utf-8") == original
            ):
                new_backup.unlink()
    except (OSError, ValueError, InputError):
        # Retained configuration may still reference the credential, registry and service.
        # Discard their dependent compensations rather than creating a dangling connection.
        rollback.pop_all()
        raise InputError(
            "Connection cleanup is incomplete. Configuration and recorded access were "
            "preserved, along with any service started for them. Close the client/editor "
            "and inspect the recorded integration before retrying."
        ) from None


def _undo_credential(store, path, previous, client_id, written):
    if previous is None or json.loads(previous)["client_id"] != client_id:
        revoke_client(store, client_id)
    if path.is_symlink() or not path.exists() or path.read_bytes() != written:
        return  # Keep a credential replaced or edited after our write.
    if previous is None:
        path.unlink()
    else:
        atomic_write(path, previous, expected=(True, written))


def _undo_existing_credential(path, previous, url):
    if path.is_symlink():
        return
    expected = json.loads(previous) | {"url": url}
    if not path.exists() or read_connection(path) == expected:
        atomic_write(path, previous)


def _undo_registry(path, previous):
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write(path, previous)


async def _read_probe(entry, **checks):
    try:
        async with asyncio.timeout(20):
            return await verify_command(entry, **checks)
    except Exception:
        raise InputError(
            "The private MCP endpoint or credential failed read verification; "
            "configuration was preserved."
        ) from None


def connect_client(
    store: Store,
    *,
    provider: str,
    config: Path | None = None,
    connection: Path | None = None,
    projects: list[str] | None = None,
    mcp_url: str | None = None,
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
    if mcp_url is not None:
        private_endpoint(mcp_url)
        if connection is not None:
            raise InputError("Choose an existing connection or a local private MCP URL.")
    with locked(store.directory / "installation.lock"), ExitStack() as rollback:
        result = _connect_client(
            store,
            provider=provider,
            config=config,
            connection=connection,
            projects=projects,
            mcp_url=mcp_url,
            rollback=rollback,
        )
        rollback.pop_all()
        return result


def _connect_client(store, *, provider, config, connection, projects, mcp_url, rollback):
    target = target_path(provider, config)
    registry = registrations(store.directory)
    key = integration_key(provider, target)
    ownership = dict(registry["items"].get(key, {}).get("ownership", {}))
    created_dirs = [str(parent) for parent in target.parents if not parent.exists()]
    local = connection is None
    managed = local and mcp_url is None
    if local:
        private = store.directory / "connections"
        if private.is_symlink():
            raise InputError("Managed connection storage must not be a symbolic link.")
        if not private.exists():
            created_dirs.append(str(private))
        private.mkdir(mode=0o700, exist_ok=True)
        connection = private / f"{provider}-{key}.json"
        if key in registry["items"]:
            previous = registry["items"][key]
            if previous["local"]:
                previous_path = Path(previous["connection_file"])
                if previous_path not in (connection, private / f"{provider}.json"):
                    raise InputError(
                        "Unexpected managed credential path; existing state was preserved."
                    )
                connection = previous_path
        with locked(private / f"{provider}.lock"):
            configure(
                target,
                provider=provider,
                entry=bridge_entry(connection, store.directory if managed else None, provider),
                dry_run=True,
            )
            if connection.exists() and (
                connection.is_symlink() or ownership.get("credential_hash") != file_hash(connection)
            ):
                raise InputError(
                    "Unowned or changed credential was preserved; disconnect manually."
                )
            previous_service = read_json(state_path(store)) if state_path(store).exists() else {}
            runtime = {"url": mcp_url} if mcp_url else up(store)
            if managed and runtime["pid"] != previous_service.get("pid"):
                rollback.callback(stop_process, read_json(state_path(store)))
            if connection.is_symlink():
                raise InputError("Managed credentials must not be symbolic links.")
            previous = connection.read_text() if connection.exists() else None
            if previous is not None:
                read_connection(connection)
                rollback.callback(_undo_existing_credential, connection, previous, runtime["url"])
            client_id = credential(
                store, name=provider, path=connection, url=runtime["url"], projects=projects
            )
            written = connection.read_bytes()
            rollback.callback(_undo_credential, store, connection, previous, client_id, written)
            ownership["credential_hash"] = hashlib.sha256(written).hexdigest()
    else:
        connection = connection.expanduser().absolute()
        client_id = read_connection(connection)["client_id"]
    entry = bridge_entry(connection, store.directory if managed else None, provider)
    configure(target, provider=provider, entry=entry, dry_run=True)
    check = asyncio.run(
        asyncio.wait_for(
            _read_probe(
                entry,
                write=False,
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
    registry["items"][key] = {
        "provider": provider,
        "config_file": str(target),
        "entry": entry,
        "connection_file": str(connection),
        "client_id": client_id,
        "local": local,
        "ownership": ownership,
    }
    ownership["directories"] = list(dict.fromkeys(ownership.get("directories", []) + created_dirs))
    # Record intent first so an interruption after configuration can be cleaned up.
    registry_path = store.directory / "integrations.json"
    rollback.callback(
        _undo_registry, registry_path, registry_path.read_text() if registry_path.exists() else None
    )
    changed = configure(
        target,
        provider=provider,
        entry=entry,
        rollback=rollback,
        ownership=ownership,
        publish=lambda: write_json(registry_path, registry),
    )
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
