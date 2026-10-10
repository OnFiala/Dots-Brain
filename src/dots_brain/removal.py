"""Remove recorded integrations without replacing unrelated application settings."""

from __future__ import annotations

import json
from pathlib import Path

import tomlkit

from .auth import read_connection, revoke_client
from .clients import (
    PROVIDERS,
    bridge_entry,
    integration_key,
    local_connection_path,
    registrations,
    target_path,
)
from .errors import InputError
from .installation_state import mark_uninstalled
from .local import atomic_write, locked, read_json, write_json
from .runtime import state_path, stop_process
from .store import Store


def inspect_config(path: Path, provider: str) -> tuple[str, dict, str]:
    if path.is_symlink():
        raise InputError("Symbolic-link configuration was preserved.")
    original = path.read_text() if path.exists() else ""
    document = tomlkit.parse(original) if provider == "codex" else json.loads(original or "{}")
    section = "mcp_servers" if provider == "codex" else "mcpServers"
    if not isinstance(document, dict) or not isinstance(document.get(section, {}), dict):
        raise InputError("Unrecognized configuration was preserved.")
    return original, document, section


def remove_entry(record: dict, *, dry_run: bool) -> dict:
    path, provider = Path(record["config_file"]), record["provider"]
    result = {"provider": provider, "config_file": str(path)}

    def change():
        original, document, section = inspect_config(path, provider)
        servers = document.get(section, {})
        if "dots-brain" not in servers:
            return {**result, "state": "already_absent"}
        if servers["dots-brain"] != record["entry"]:
            return {**result, "state": "preserved_modified"}
        if dry_run:
            return {**result, "state": "would_remove"}
        del servers["dots-brain"]
        content = (
            tomlkit.dumps(document)
            if provider == "codex"
            else json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        )
        if path.is_symlink() or path.read_text() != original:
            return {**result, "state": "preserved_concurrent_change"}
        atomic_write(path, content, preserve=True)
        return {**result, "state": "removed"}

    try:
        # A preview must not create directories or lock files.
        if dry_run or not path.exists():
            return change()
        with locked(path.with_name(path.name + ".dots-brain.lock")):
            return change()
    except (OSError, ValueError, InputError):
        return {**result, "state": "preserved_unreadable"}


def inventory(store: Store, extra_configs: list[str]) -> tuple[dict, list[dict]]:
    issues = []
    try:
        raw = registrations(store.directory)
        valid_items, invalid_items = {}, {}
        for key, item in raw["items"].items():
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("provider"), str)
                or item.get("provider") not in PROVIDERS
                or not all(
                    isinstance(item.get(k), str)
                    for k in ("config_file", "connection_file", "client_id")
                )
                or not isinstance(item.get("entry"), dict)
                or not isinstance(item.get("local"), bool)
                or not Path(item["config_file"]).is_absolute()
                or key != integration_key(item["provider"], Path(item["config_file"]))
            ):
                invalid_items[key] = item
                issues.append({"state": "registry_invalid_entry", "key": str(key)})
                continue
            valid_items[key] = item
        registry = {"version": 1, "items": valid_items, "_invalid_items": invalid_items}
    except (OSError, ValueError, InputError):
        registry = {"version": 1, "items": {}, "_invalid_items": None}
        issues.append(
            {"state": "registry_unreadable", "action": "Preserve and repair the registry."}
        )

    candidates = [(p, target_path(p, None), False) for p in PROVIDERS if p != "mcp-json"]
    for option in extra_configs:
        provider, separator, path = option.partition("=")
        if not separator or not path or provider not in PROVIDERS:
            raise InputError("Expected --config PROVIDER=PATH for a supported provider.")
        candidates.append((provider, target_path(provider, Path(path)), True))
    for provider, path, explicit in candidates:
        key = integration_key(provider, path)
        if key in registry["items"]:
            continue
        connection = store.directory / "connections" / f"{provider}.json"
        expected = bridge_entry(connection, store.directory, provider)
        try:
            _, document, section = inspect_config(path, provider)
            entry = document.get(section, {}).get("dots-brain")
            if entry == expected:
                client_id = read_connection(connection)["client_id"] if connection.exists() else ""
                registry["items"][key] = {
                    "provider": provider,
                    "config_file": str(path),
                    "entry": expected,
                    "connection_file": str(connection),
                    "client_id": client_id,
                    "local": True,
                }
            elif explicit and entry is not None:
                issues.append({"config_file": str(path), "state": "preserved_unowned"})
        except (OSError, ValueError, InputError):
            if explicit:
                issues.append({"config_file": str(path), "state": "preserved_unreadable"})
    return registry, issues


def persist_registry(store: Store, registry: dict, issues: list[dict]) -> None:
    """Keep malformed rows visible while updating independent valid registrations."""
    if any(issue["state"] == "registry_unreadable" for issue in issues):
        return
    invalid = registry.pop("_invalid_items", {})
    try:
        write_json(
            store.directory / "integrations.json",
            {"version": 1, "items": {**invalid, **registry["items"]}},
        )
    finally:
        registry["_invalid_items"] = invalid


def managed_credential(store: Store, record: dict) -> Path | None:
    path = local_connection_path(store, record["connection_file"])
    private = store.directory / "connections"
    provider = record["provider"]
    key = integration_key(provider, Path(record["config_file"]))
    if (
        record["local"]
        and path.parent == private
        and not private.is_symlink()
        and path.name in {f"{provider}.json", f"{provider}-{key}.json"}
    ):
        return path
    return None


def disconnect_client(
    store: Store, *, provider: str, config: Path | None = None, dry_run: bool = False
) -> dict:
    target = target_path(provider, config)
    key = integration_key(provider, target)

    def disconnect():
        registry, issues = inventory(store, [f"{provider}={target}"])
        record = registry["items"].get(key)
        if record is None:
            return {"state": "partial" if issues else "already_absent", "issues": issues}
        result = remove_entry(record, dry_run=dry_run)
        if dry_run:
            return {"state": "preview", "client": result, "changes_applied": False}
        credential = managed_credential(store, record)
        unique = credential is not None and credential.name == f"{provider}-{key}.json"
        shared = any(
            other != key and item["client_id"] == record["client_id"]
            for other, item in registry["items"].items()
        )
        if unique and not shared and store.path.is_file():
            revoke_client(store, record["client_id"])
            credential.unlink(missing_ok=True)
            access = "revoked"
        else:
            access = (
                "issuer_revocation_required"
                if not record["local"]
                else "shared_credential_retained"
            )
        complete = result["state"] in {"removed", "already_absent"} and access == "revoked"
        remote_complete = result["state"] in {"removed", "already_absent"} and not record["local"]
        if complete or remote_complete:
            del registry["items"][key]
        persist_registry(store, registry, issues)
        return {
            "state": (
                "disconnected"
                if complete and not issues
                else "disconnected_remote"
                if remote_complete and not issues
                else "partial"
            ),
            "client": result,
            "access": access,
            "data_preserved": True,
            "issues": issues,
        }

    if dry_run or not store.directory.exists():
        return disconnect()
    with locked(store.directory / "installation.lock", create_parent=True):
        return disconnect()


def uninstall(
    store: Store, *, dry_run: bool = False, extra_configs: list[str] | None = None
) -> dict:
    def remove():
        registry, issues = inventory(store, extra_configs or [])
        results = [remove_entry(record, dry_run=True) for record in registry["items"].values()]
        common = {
            "data_dir": str(store.directory),
            "data_preserved": True,
            "software_removed": False,
            "preserved": [
                "memories",
                "revisions",
                "deletion_suppressions",
                "models",
                "exports",
                "backups",
                "source_checkout",
                "python_environment",
            ],
            "discovery": "registered paths, default paths, and explicitly supplied legacy paths",
        }
        if dry_run:
            return {
                **common,
                "state": "preview",
                "changes_applied": False,
                "clients": results,
                "issues": issues,
            }
        if not store.directory.exists() and not registry["items"]:
            return {
                **common,
                "state": "partial" if issues else "not_installed",
                "clients": [],
                "issues": issues,
            }
        with locked(store.directory / "service.lock", create_parent=True):
            # Disable first: stale bridges cannot restart, and running MCP tools reject calls.
            mark_uninstalled(store)
            stopped = False
            try:
                if state_path(store).exists():
                    stop_process(store, read_json(state_path(store)))
                stopped = True
            except (OSError, ValueError, InputError):
                issues.append({"state": "managed_process_stop_failed"})
            credentials_revoked = False
            if store.path.is_file():
                try:
                    with store.connection(write=True) as db:
                        db.execute("UPDATE clients SET revoked=1")
                        from .oauth import revoke_all

                        revoke_all(db)
                    credentials_revoked = True
                except (OSError, ValueError, InputError):
                    issues.append({"state": "credential_revocation_failed"})
            results = []
            remaining = {}
            credentials = [store.directory / "probe.connection.json"]
            for key, record in registry["items"].items():
                result = remove_entry(record, dry_run=False)
                results.append(result)
                if result["state"] not in {"removed", "already_absent"} or not record["local"]:
                    remaining[key] = record
                if not record["local"]:
                    issues.append(
                        {
                            "provider": record["provider"],
                            "client_id": record["client_id"],
                            "state": "issuer_revocation_required",
                        }
                    )
                path = managed_credential(store, record)
                if path is not None:
                    credentials.append(path)
            for path in credentials:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    issues.append({"state": "credential_file_retained", "path": str(path)})
            registry["items"] = remaining
            persist_registry(store, registry, issues)
        return {
            **common,
            "state": "partial" if remaining or issues else "uninstalled",
            "service_disabled": True,
            "managed_process_stopped": stopped,
            "local_credentials_revoked": credentials_revoked,
            "clients": results,
            "issues": issues,
        }

    if dry_run or not store.directory.exists():
        return remove()
    with locked(store.directory / "installation.lock", create_parent=True):
        return remove()
