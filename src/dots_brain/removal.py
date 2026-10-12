"""Remove recorded integrations without replacing unrelated application settings."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import tomlkit
from tomlkit import items as toml_items

from .auth import revoke_client
from .clients import (
    PROVIDERS,
    config_lock_path,
    file_hash,
    integration_key,
    legacy_notice,
    registrations,
    target_path,
)
from .errors import BrainError, InputError
from .local import atomic_write, locked, read_json, write_json
from .runtime import bound_state, state_path, stop_process
from .store import Store, now


def inspect_config(path: Path, provider: str) -> tuple[str, dict, str]:
    if path.is_symlink():
        raise InputError("Symbolic-link configuration was preserved.")
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    document = tomlkit.parse(original) if provider == "codex" else json.loads(original or "{}")
    section = "mcp_servers" if provider == "codex" else "mcpServers"
    if not isinstance(document, dict) or not isinstance(document.get(section, {}), dict):
        raise InputError("Unrecognized configuration was preserved.")
    return original, document, section


def remove_owned_file(path, expected_hash):
    result = {"path": str(path)}
    try:
        if not path.exists() and not path.is_symlink():
            return {**result, "state": "already_absent"}
        if path.is_symlink() or not expected_hash or file_hash(path) != expected_hash:
            return {**result, "state": "preserved_modified_or_unowned"}
        path.unlink()
        return {**result, "state": "removed"}
    except OSError:
        return {**result, "state": "preserved_unreadable"}


def cleanup_directories(store, record):
    allowed = set(Path(record["config_file"]).parents) | {store.directory / "connections"}
    results = []
    for name in record.get("ownership", {}).get("directories", []):
        path = Path(name)
        if path not in allowed or path.is_symlink():
            continue
        try:
            path.rmdir()  # Empty owned directories only; never remove persistent lock files.
            state = "removed"
        except FileNotFoundError:
            state = "already_absent"
        except OSError:
            state = "preserved_nonempty_or_unavailable"
        results.append({"path": str(path), "state": state})
    return results


def retain_toml_comments(document, item):
    """Keep comments attached by TOML's parser to a table being removed."""
    if isinstance(item, (toml_items.Whitespace, toml_items.Null)):
        return
    if item.trivia.comment:
        document.append(None, toml_items.Comment(toml_items.Trivia(comment=item.trivia.comment)))
    if isinstance(item, (toml_items.Table, toml_items.InlineTable)):
        for _, child in item.value.body:
            retain_toml_comments(document, child)
    elif isinstance(item, toml_items.Array):
        # Array iteration exposes values only; its token iterator also includes comments.
        for child in item._iter_items():
            retain_toml_comments(document, child)


def remove_entry(record: dict, *, directory: Path, dry_run: bool) -> dict:
    path, provider = Path(record["config_file"]), record["provider"]
    result = {"provider": provider, "config_file": str(path)}

    def change():
        original, document, section = inspect_config(path, provider)
        servers = document.get(section, {})
        if "dots-brain" not in servers:
            return {**result, "state": "already_absent"}
        ownership = record.get("ownership", {})
        if not ownership.get("entry_created"):
            return {**result, "state": "preserved_unowned"}
        if servers["dots-brain"] != record["entry"]:
            return {**result, "state": "preserved_modified"}
        if dry_run:
            return {**result, "state": "would_remove"}
        unchanged = file_hash(path) == ownership.get("config_hash")
        removed = servers["dots-brain"]
        del servers["dots-brain"]
        if provider == "codex":
            retain_toml_comments(document, removed)
        content = (
            tomlkit.dumps(document)
            if provider == "codex"
            else json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        )
        if path.is_symlink() or path.read_text(encoding="utf-8") != original:
            return {
                **result,
                "state": "preserved_concurrent_change",
                "action": "Close the client/editor before retrying configuration changes.",
            }
        if ownership.get("config_created") and unchanged and not any(document.values()):
            path.unlink()
            config_state = "removed_empty_owned_file"
        else:
            atomic_write(path, content, preserve=True, expected=(True, original.encode("utf-8")))
            config_state = "retained"
        backup = path.with_name(path.name + ".before-dots-brain")
        return {
            **result,
            "state": "removed",
            "file": config_state,
            "backup": remove_owned_file(
                backup, ownership.get("backup_hash") if unchanged else None
            ),
        }

    try:
        # A preview must not create directories or lock files.
        if dry_run or not path.exists():
            return change()
        with locked(config_lock_path(directory, path)):
            return change()
    except InputError as error:
        return {
            **result,
            "state": "preserved_concurrent_change"
            if "concurrently" in str(error)
            else "preserved_unreadable",
            "action": str(error),
        }
    except (OSError, ValueError):
        return {**result, "state": "preserved_unreadable"}


def inventory(store: Store, extra_configs: list[str], *, defaults=True) -> tuple[dict, list[dict]]:
    issues = []
    try:
        registry = registrations(store.directory)
        for key, item in registry["items"].items():
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
                raise InputError("Invalid integration registration.")
    except (OSError, ValueError, InputError):
        registry = {"version": 1, "items": {}}
        issues.append(
            {"state": "registry_unreadable", "action": "Preserve and repair the registry."}
        )

    candidates = (
        [(p, target_path(p, None), False) for p in PROVIDERS if p != "mcp-json"] if defaults else []
    )
    for option in extra_configs:
        provider, separator, path = option.partition("=")
        if not separator or not path or provider not in PROVIDERS:
            raise InputError("Expected --config PROVIDER=PATH for a supported provider.")
        candidates.append((provider, target_path(provider, Path(path)), True))
    for provider, path, explicit in candidates:
        key = integration_key(provider, path)
        if key in registry["items"]:
            continue
        try:
            _, document, section = inspect_config(path, provider)
            entry = document.get(section, {}).get("dots-brain")
            notice = legacy_notice(provider, path, entry)
            if notice:
                issues.append(
                    {
                        "config_file": str(path),
                        "state": "unsupported_legacy_client",
                        "action": notice,
                    }
                )
            elif explicit and entry is not None:
                issues.append({"config_file": str(path), "state": "preserved_unowned"})
        except (OSError, ValueError, InputError):
            if explicit:
                issues.append({"config_file": str(path), "state": "preserved_unreadable"})
    return registry, issues


def managed_credential(store: Store, record: dict) -> Path | None:
    path = Path(record["connection_file"])
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
        registry, issues = inventory(store, [f"{provider}={target}"], defaults=False)
        record = registry["items"].get(key)
        if record is None:
            return {"state": "partial" if issues else "already_absent", "issues": issues}
        result = remove_entry(record, directory=store.directory, dry_run=dry_run)
        if dry_run:
            return {"state": "preview", "client": result, "changes_applied": False}
        if result["state"] not in {"removed", "already_absent"}:
            return {
                "state": "partial",
                "client": result,
                "access": "preserved",
                "data_preserved": True,
                "issues": issues,
            }
        credential = managed_credential(store, record)
        owned = credential is not None and record.get("ownership", {}).get("credential_hash")
        shared = any(
            other != key and item["client_id"] == record["client_id"]
            for other, item in registry["items"].items()
        )
        cleanup = {"state": "preserved"}
        if owned and not shared and store.path.is_file():
            try:
                revoke_client(store, record["client_id"])
                access = "revoked"
                cleanup = remove_owned_file(credential, owned)
            except (OSError, BrainError, sqlite3.Error):
                access = "revocation_failed"
        else:
            access = (
                "issuer_revocation_required"
                if not record["local"]
                else "shared_or_unowned_credential_retained"
            )
        complete = access == "revoked" and cleanup["state"] in {"removed", "already_absent"}
        directories = cleanup_directories(store, record)
        if complete:
            del registry["items"][key]
        if not any(issue["state"] == "registry_unreadable" for issue in issues):
            write_json(store.directory / "integrations.json", registry)
        return {
            "state": "disconnected" if complete and not issues else "partial",
            "client": result,
            "access": access,
            "credential": cleanup,
            "directories": directories,
            "data_preserved": True,
            "issues": issues,
        }

    if dry_run or not store.directory.exists():
        return disconnect()
    with locked(store.directory / "installation.lock"):
        return disconnect()


def uninstall(
    store: Store, *, dry_run: bool = False, extra_configs: list[str] | None = None
) -> dict:
    def remove():
        registry, issues = inventory(store, extra_configs or [])
        results = [
            remove_entry(record, directory=store.directory, dry_run=True)
            for record in registry["items"].values()
        ]
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
            "discovery": "registered paths, default paths, and explicitly supplied paths",
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
        with locked(store.directory / "service.lock"):
            # Disable first: stale bridges cannot restart, and running MCP tools reject calls.
            marker = store.directory / "disabled.json"
            previous = read_json(marker) if marker.exists() else {"version": 1, "disabled": True}
            write_json(marker, {**previous, "uninstalled_at": now()})
            access, stop = "no_local_store", "already_stopped"
            try:
                if store.path.is_file():
                    with store.connection(write=True) as db:
                        db.execute("UPDATE clients SET revoked=1")
                        from .oauth import revoke_all

                        revoke_all(store, db, reason="uninstalled")
                    access = "revoked"
            except (OSError, BrainError, sqlite3.Error):
                access = "revocation_failed"
                issues.append({"state": access})
            try:
                if state_path(store).exists():
                    state = read_json(state_path(store))
                    if bound_state(store, state):
                        stop_process(state)
                        stop = "stopped"
                    else:
                        stop = "preserved_unbound"
                        issues.append({"state": "managed_process_unbound"})
            except (OSError, ValueError, InputError):
                stop = "stop_failed"
                issues.append({"state": "managed_process_stop_failed"})
            results = []
            remaining = {}
            for key, record in registry["items"].items():
                result = remove_entry(record, directory=store.directory, dry_run=False)
                results.append(result)
                if not record["local"]:
                    issues.append(
                        {
                            "provider": record["provider"],
                            "client_id": record["client_id"],
                            "state": "issuer_revocation_required",
                        }
                    )
                path = managed_credential(store, record)
                cleanup = {"state": "preserved"}
                if path is not None and access == "revoked":
                    cleanup = remove_owned_file(
                        path, record.get("ownership", {}).get("credential_hash")
                    )
                result["credential"] = cleanup
                result["directories"] = cleanup_directories(store, record)
                if result["state"] not in {"removed", "already_absent"} or cleanup["state"] not in {
                    "removed",
                    "already_absent",
                }:
                    remaining[key] = record
            if not any(issue["state"] == "registry_unreadable" for issue in issues):
                write_json(
                    store.directory / "integrations.json", {"version": 1, "items": remaining}
                )
        return {
            **common,
            "state": "partial" if remaining or issues else "uninstalled",
            "service_disabled": True,
            "local_credentials_revoked": access == "revoked",
            "access": access,
            "stop": stop,
            "probe_credential": {
                "path": str(store.directory / "probe.connection.json"),
                "state": "preserved_unowned"
                if (store.directory / "probe.connection.json").exists()
                or (store.directory / "probe.connection.json").is_symlink()
                else "already_absent",
            },
            "clients": results,
            "issues": issues,
        }

    if dry_run or not store.directory.exists():
        return remove()
    from .local import writer_lock

    with (
        locked(store.directory / "installation.lock"),
        writer_lock(store.directory, exclusive=True),
    ):
        return remove()
