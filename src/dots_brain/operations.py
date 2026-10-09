"""Explicit operator commands for schema, backup, and connector configuration."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import uuid
from itertools import zip_longest
from pathlib import Path

from .database_checks import validate_snapshot
from .errors import InputError
from .local import locked, read_json, sync_file_and_parent, write_json
from .store import SCHEMA_VERSION, Store


def migrate(store: Store, *, apply: bool, writers_stopped: bool, backup: Path | None) -> dict:
    from .migrations import migrate_v1_to_v2
    from .runtime import active, state_path

    if not apply:
        return migrate_v1_to_v2(store, apply=False)

    def stopped():
        if not writers_stopped:
            raise InputError("Stop managed, supervised, and stdio writers; pass --writers-stopped.")
        state = read_json(state_path(store)) if state_path(store).exists() else {}
        if active(state):
            raise InputError("The managed service is still running. Stop it before migration.")

    with locked(store.directory / "installation.lock"), locked(store.directory / "service.lock"):
        return migrate_v1_to_v2(store, apply=True, backup_path=backup, stop_guard=stopped)


def backup_store(store: Store, output: Path) -> dict:
    """Take a consistent SQLite snapshot, including WAL, into a new private file."""
    output = output.expanduser().absolute()
    if not output.parent.is_dir():
        raise InputError("Create a private backup directory first.")
    descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(descriptor)
    try:
        with store.connection() as source, sqlite3.connect(output) as target:
            source.backup(target)
            validate_snapshot(target, SCHEMA_VERSION)
        sync_file_and_parent(output)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return {
        "state": "verified_backup",
        "output": str(output),
        "schema_version": SCHEMA_VERSION,
        "off_host": "not_verified",
    }


def restore_store(backup: Path, target: Store, *, latest_deletions: Store) -> dict:
    """Restore into a new directory, retaining the surviving store's deletion barriers.

    Credentials are revoked and startup is disabled. This does not overwrite the
    current instance or invent a recovery point after total host/storage loss.
    """
    if target.directory.exists():
        raise InputError("Restore requires a new empty target directory.")
    if backup.is_symlink() or not backup.is_file():
        raise InputError("Backup must be an existing regular file.")
    # A separate surviving store is mandatory to prevent an old backup undoing deletion.
    with latest_deletions.connection() as current:
        legacy = [tuple(row) for row in current.execute("SELECT * FROM suppressions")]
        scoped = [tuple(row) for row in current.execute("SELECT * FROM scoped_suppressions")]
    with sqlite3.connect(f"file:{backup.absolute()}?mode=ro", uri=True) as source:
        validate_snapshot(source, SCHEMA_VERSION)
        try:
            target.directory.mkdir(parents=True, mode=0o700)
        except FileExistsError:
            raise InputError("Another restore already created the target directory.") from None
        descriptor = os.open(target.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(descriptor)
        # Mark disabled before copying so an interrupted restore cannot be started.
        write_json(
            target.directory / "disabled.json",
            {
                "reason": "restored_requires_review",
                "deletion_source": str(latest_deletions.directory),
                "recovery_id": str(uuid.uuid4()),
            },
        )
        with sqlite3.connect(target.path) as destination:
            source.backup(destination)
    with target.connection(write=True) as db:
        forgotten = _apply_deletions(db, legacy, scoped)
        _revoke_credentials(db)
        db.execute("UPDATE cortex_operations SET state='uncertain' WHERE state='sending'")
        validate_snapshot(db, SCHEMA_VERSION)
    sync_file_and_parent(target.path)
    return {
        "state": "restored_disabled",
        "target": str(target.directory),
        "credentials": "revoked",
        "deletion_barriers": "snapshot_only_until_activation",
        "removed_since_backup": len(forgotten),
    }


def _apply_deletions(db, legacy, scoped) -> list[str]:
    db.executemany("INSERT OR IGNORE INTO suppressions VALUES (?,?)", legacy)
    db.executemany("INSERT OR REPLACE INTO scoped_suppressions VALUES (?,?,?,?,?)", scoped)
    forgotten = [
        row[0]
        for row in db.execute(
            "SELECT m.id FROM memories m WHERE EXISTS "
            "(SELECT 1 FROM suppressions s WHERE s.source_key=m.identity_key) OR EXISTS "
            "(SELECT 1 FROM scoped_suppressions s WHERE s.project=m.project "
            "AND s.identity_key=m.identity_key)"
        )
    ]
    db.executemany("DELETE FROM memory_fts WHERE memory_id=?", [(key,) for key in forgotten])
    db.executemany("DELETE FROM memories WHERE id=?", [(key,) for key in forgotten])
    return forgotten


def _revoke_credentials(db) -> None:
    from .oauth import revoke_all

    db.execute("UPDATE clients SET revoked=1")
    revoke_all(db)


def _validate_cutover_state(source, target) -> None:
    # Backups may lag new facts, audit events, or outbound receipts. Deletion
    # reconciliation alone does not make that rollback lossless. Compare canonical
    # rows without exposing their contents; derived indexes and revoked credentials
    # are intentionally excluded. Full history merging is not an operator shortcut.
    queries = {
        "memories": "SELECT * FROM memories ORDER BY id",
        "revisions": "SELECT * FROM revisions ORDER BY memory_id,revision",
        "audit_events": "SELECT * FROM audit_events ORDER BY id",
        "cortex_operations": "SELECT operation_id,request_digest,principal,local_project,"
        "cortex_project,operation_kind,source_ref,"
        "CASE state WHEN 'sending' THEN 'uncertain' ELSE state END,"
        "receipt_json,upstream_object_id,error_code,created_at,updated_at "
        "FROM cortex_operations ORDER BY operation_id",
    }
    divergent = []
    for name, query in queries.items():
        count = sum(
            tuple(left or ()) != tuple(right or ())
            for left, right in zip_longest(source.execute(query), target.execute(query))
        )
        if count:
            divergent.append(f"{name}={count}")
    if divergent:
        raise InputError(
            "Recovery would discard or alter canonical state ("
            + ", ".join(divergent)
            + "). Both stores remain disabled; reconcile or stage a current backup."
        )


def _merge_audit_suffix(source, target) -> None:
    """Retain the verified source audit suffix, preserving IDs and the hash chain."""
    last = target.execute("SELECT COALESCE(MAX(id),0) FROM audit_events").fetchone()[0]
    prefix = source.execute("SELECT * FROM audit_events WHERE id<=? ORDER BY id", (last,))
    restored = target.execute("SELECT * FROM audit_events ORDER BY id")
    if any(
        tuple(left or ()) != tuple(right or ()) for left, right in zip_longest(prefix, restored)
    ):
        raise InputError("Restored audit is not an exact prefix of the surviving audit.")
    suffix = source.execute("SELECT * FROM audit_events WHERE id>? ORDER BY id", (last,))
    for row in suffix:
        placeholders = ",".join("?" for _ in row)
        target.execute(f"INSERT INTO audit_events VALUES ({placeholders})", tuple(row))


def activate_restore(source: Store, target: Store, *, writers_stopped: bool) -> dict:
    """Offline cutover; keep both stores disabled if any recovery check fails.

    The source writer lock orders the final deletion snapshot after in-flight
    writes. Its durable disabled marker prevents queued/new memory writes. Only
    then can the target become available, with all old credentials revoked.
    """
    from .runtime import active, state_path

    if not writers_stopped:
        raise InputError("Stop all managed, supervised, and stdio writers; pass --writers-stopped.")
    if source.directory.resolve() == target.directory.resolve():
        raise InputError("Recovery source and target must be different installations.")
    with contextlib.ExitStack() as locks:
        for directory in sorted([source.directory, target.directory]):
            if not directory.is_dir():
                raise InputError("Both recovery installations must exist.")
            locks.enter_context(locked(directory / "installation.lock"))
            locks.enter_context(locked(directory / "service.lock"))
        marker = target.directory / "disabled.json"
        state = read_json(marker) if marker.exists() else {}
        if (
            state.get("reason") != "restored_requires_review"
            or Path(state.get("deletion_source", "")).resolve() != source.directory.resolve()
        ):
            raise InputError("The restored target does not name this surviving deletion source.")
        try:
            recovery_id = str(uuid.UUID(state["recovery_id"]))
        except (KeyError, ValueError, TypeError, AttributeError):
            raise InputError(
                "The target has no valid recovery identity; stage a new copy."
            ) from None
        for store in (source, target):
            runtime = read_json(state_path(store)) if state_path(store).exists() else {}
            if active(runtime):
                raise InputError("A managed recovery service is still running; stop it first.")
        with target.connection() as db:
            validate_snapshot(db, SCHEMA_VERSION)
        with source.connection(write=True) as current:
            validate_snapshot(current, SCHEMA_VERSION)
            source_marker = source.directory / "disabled.json"
            previous = read_json(source_marker) if source_marker.exists() else {}
            if previous.get("cutover") == "committed" and (
                previous.get("recovery_id") != recovery_id
                or Path(previous.get("replacement", "")).resolve() != target.directory.resolve()
            ):
                raise InputError(
                    "This source was already replaced; recover from its active successor."
                )
            if previous.get("cutover") != "committed":
                write_json(
                    source_marker,
                    {
                        "reason": "restore_cutover",
                        "replacement": str(target.directory),
                        "cutover": "pending",
                        "recovery_id": recovery_id,
                    },
                )
            legacy = [tuple(row) for row in current.execute("SELECT * FROM suppressions")]
            scoped = [tuple(row) for row in current.execute("SELECT * FROM scoped_suppressions")]
            with target.connection(write=True) as restored:
                forgotten = _apply_deletions(restored, legacy, scoped)
                _revoke_credentials(restored)
                _merge_audit_suffix(current, restored)
                validate_snapshot(restored, SCHEMA_VERSION)
                _validate_cutover_state(current, restored)
            sync_file_and_parent(target.path)
            write_json(
                source_marker,
                {
                    "reason": "restore_cutover",
                    "replacement": str(target.directory),
                    "cutover": "committed",
                    "recovery_id": recovery_id,
                },
            )
        marker.unlink()
        sync_file_and_parent(target.path)  # Persist removal of the disabled marker too.
    return {
        "state": "restored_ready",
        "target": str(target.directory),
        "old_store": "disabled",
        "credentials": "revoked",
        "removed_at_cutover": len(forgotten),
        "service": "not_started",
    }


def configure_cortex(store: Store, *, endpoint: str, token_file: Path, projects: list[str]) -> dict:
    from .cortex_connector import CortexConnectionConfig

    mapping = {}
    for item in projects:
        local, separator, remote = item.partition("=")
        if not separator or local in mapping:
            raise InputError("Use one unique LOCAL_PROJECT=CORTEX_PROJECT mapping per option.")
        mapping[local] = remote
    config = CortexConnectionConfig(
        endpoint, token_file.expanduser().absolute(), tuple(mapping.items())
    )
    store.status()
    write_json(
        store.directory / "cortex.json",
        {
            "endpoint": config.endpoint,
            "token_file": str(config.token_file),
            "project_mapping": mapping,
        },
    )
    return {
        "state": "configured_pending_verification",
        "projects": list(mapping),
        "restart_required": True,
    }
