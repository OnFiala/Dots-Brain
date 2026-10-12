"""Explicit operator commands for schema, backup, and connector configuration."""

from __future__ import annotations

import contextlib
import hashlib
import json
import uuid
from itertools import zip_longest
from pathlib import Path

from .database_checks import validate_snapshot
from .database_io import open_readonly, snapshot
from .errors import InputError
from .local import locked, read_json, sync_file_and_parent, write_json, writer_lock
from .store import SCHEMA_VERSION, Store, secure_fts


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
    with store.connection() as source:
        output = snapshot(source, output, SCHEMA_VERSION)
    return {
        "state": "verified_backup",
        "output": str(output),
        "schema_version": SCHEMA_VERSION,
        "off_host": "not_verified",
    }


def restore_store(backup: Path, target: Store, *, latest_deletions: Store) -> dict:
    """Restore into a new directory, retaining the surviving store's deletion barriers.

    Startup is disabled; OAuth reset is audited at activation. This does not overwrite the
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
    with (
        contextlib.closing(open_readonly(backup, immutable=True)) as source,
        contextlib.ExitStack() as leases,
    ):
        if source.execute("PRAGMA user_version").fetchone()[0] == 1:
            raise InputError(
                "Schema v1 backup requires migration of a separate working copy before restore."
            )
        validate_snapshot(source, SCHEMA_VERSION)
        try:
            target.directory.mkdir(parents=True, mode=0o700)
        except FileExistsError:
            raise InputError("Another restore already created the target directory.") from None
        leases.enter_context(writer_lock(target.directory, exclusive=True))
        # Mark disabled before copying so an interrupted restore cannot be started.
        write_json(
            target.directory / "disabled.json",
            {
                "reason": "restored_requires_review",
                "deletion_source": str(latest_deletions.directory),
                "recovery_id": str(uuid.uuid4()),
            },
        )
        snapshot(source, target.path, SCHEMA_VERSION)
        with target.connection(write=True) as db:
            forgotten = _apply_deletions(db, legacy, scoped)
            db.execute("UPDATE clients SET revoked=1")
            if _has_cortex_ledger(db):
                db.execute("UPDATE cortex_operations SET state='uncertain' WHERE state='sending'")
            validate_snapshot(db, SCHEMA_VERSION)
        sync_file_and_parent(target.path)
    return {
        "state": "restored_disabled",
        "target": str(target.directory),
        "credentials": "disabled_pending_activation",
        "deletion_barriers": "snapshot_only_until_activation",
        "removed_since_backup": len(forgotten),
    }


def _apply_deletions(db, legacy, scoped) -> list[str]:
    secure_fts(db)
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


def _revoke_credentials(store, db, recovery_id) -> None:
    from .oauth import revoke_all

    db.execute("UPDATE clients SET revoked=1")
    revoke_all(store, db, reason="restore_reset", event_id="restore_reset:" + recovery_id)


def _has_cortex_ledger(db) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cortex_operations'"
        ).fetchone()
        is not None
    )


def _validate_cutover_state(source, target) -> None:
    # Backups may lag new facts, audit events, or outbound receipts. Deletion
    # reconciliation alone does not make that rollback lossless. Compare canonical
    # rows without exposing their contents; derived indexes and revoked credentials
    # are intentionally excluded. Full history merging is not an operator shortcut.
    queries = {
        "memories": "SELECT * FROM memories ORDER BY id",
        "revisions": "SELECT * FROM revisions ORDER BY memory_id,revision",
        "cortex_operations": "SELECT operation_id,request_digest,principal,local_project,"
        "cortex_project,operation_kind,source_ref,"
        "CASE state WHEN 'sending' THEN 'uncertain' ELSE state END,"
        "receipt_json,upstream_object_id,error_code,created_at,updated_at "
        "FROM cortex_operations ORDER BY operation_id",
    }
    divergent = []
    if _oauth_history(source) != _oauth_history(target):
        divergent.append("oauth_history")
    for name, query in queries.items():
        if name == "cortex_operations":
            present = [_has_cortex_ledger(db) for db in (source, target)]
            if not all(present):
                if any(present):
                    divergent.append("cortex_operations=table_presence")
                continue
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
            + "). Recovery was not activated; reconcile or stage a current backup."
        )


def _oauth_history(db):
    from .oauth import RETIRED

    result = {}
    for table in ("oauth_clients", "oauth_grants", "oauth_grant_provenance"):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            continue
        rows = [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")]
        for row in rows:
            row.pop("revoked", None)
            if "metadata" in row:
                row["metadata"] = {
                    key: value
                    for key, value in json.loads(row["metadata"]).items()
                    if key not in {"client_secret", RETIRED}
                }
        result[table] = rows
    return result


def _merge_audit_suffix(source, target, *, recovery_id, retry) -> bool:
    """Retain the verified source audit suffix, preserving IDs and the hash chain."""
    tail = target.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
    if tail and tail["client_event_id"] == "restore_reset:" + recovery_id:
        # Only the one committed reset for this recovery may extend the source chain.
        prefix = target.execute("SELECT * FROM audit_events WHERE id<? ORDER BY id", (tail["id"],))
        if not retry or any(
            tuple(a or ()) != tuple(b or ())
            for a, b in zip_longest(
                source.execute("SELECT * FROM audit_events ORDER BY id"), prefix
            )
        ):
            raise InputError(
                "Recovery audit differs from the surviving source; stage a current backup."
            )
        from .activity import _canonical

        payload = {
            key: tail[key]
            for key in (
                "project",
                "principal",
                "client_event_id",
                "kind",
                "evidence",
                "occurred_at",
                "intent_event_id",
            )
        }
        payload.update({key: json.loads(tail[key]) for key in ("action", "target", "details")})
        previous = source.execute(
            "SELECT event_hash FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        valid = (
            tail["payload_hash"] == hashlib.sha256(_canonical(payload).encode()).hexdigest()
            and payload["principal"] == "server:oauth"
            and payload["project"] == "system:auth"
            and payload["kind"] == "action"
            and payload["evidence"] == "server_observed"
            and payload["action"] == "oauth_access_invalidated"
            and payload["details"]["value"].get("reason") == "restore_reset"
            and tail["prev_hash"] == (previous[0] if previous else "")
            and tail["event_hash"]
            == hashlib.sha256((tail["prev_hash"] + tail["payload_hash"]).encode()).hexdigest()
        )
        if not valid:
            raise InputError(
                "Recovery audit receipt is invalid; preserve both stores for inspection."
            )
        return True
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
    return False


def activate_restore(source: Store, target: Store, *, writers_stopped: bool) -> dict:
    """Offline cutover; validate before disabling the surviving source.

    The source writer lock orders the final deletion snapshot after in-flight
    writes. Its durable disabled marker prevents queued/new memory writes. Only
    then can the target become available, with all old credentials revoked.
    """
    from .oauth import RETIRED
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
            locks.enter_context(writer_lock(directory, exclusive=True))
        marker = target.directory / "disabled.json"
        state = read_json(marker) if marker.exists() else {}
        if (
            state.get("reason") != "restored_requires_review"
            or state.get("uninstalled_at")
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
            if previous and (
                previous.get("reason") != "restore_cutover"
                or previous.get("cutover") not in {"pending", "committed"}
                or previous.get("uninstalled_at")
            ):
                raise InputError("The surviving store has an unrelated disable barrier.")
            if previous and (
                previous.get("recovery_id") != recovery_id
                or Path(previous.get("replacement", "")).resolve() != target.directory.resolve()
            ):
                raise InputError(
                    "This source was already replaced; recover from its active successor."
                )
            legacy = [tuple(row) for row in current.execute("SELECT * FROM suppressions")]
            scoped = [tuple(row) for row in current.execute("SELECT * FROM scoped_suppressions")]
            with target.connection(write=True) as restored:
                forgotten = _apply_deletions(restored, legacy, scoped)
                reset_done = _merge_audit_suffix(
                    current, restored, recovery_id=recovery_id, retry=bool(previous)
                )
                validate_snapshot(restored, SCHEMA_VERSION)
                _validate_cutover_state(current, restored)
                if not reset_done:
                    _revoke_credentials(target, restored, recovery_id)
                elif any(
                    restored.execute(query).fetchone()
                    for query in (
                        "SELECT 1 FROM clients WHERE revoked=0 LIMIT 1",
                        "SELECT 1 FROM oauth_grants WHERE revoked=0 LIMIT 1",
                        "SELECT 1 FROM oauth_clients "
                        f"WHERE json_type(metadata,'$.{RETIRED}') IS NULL LIMIT 1",
                        "SELECT 1 FROM oauth_tokens UNION ALL SELECT 1 FROM oauth_codes "
                        "UNION ALL SELECT 1 FROM oauth_requests LIMIT 1",
                    )
                ):
                    raise InputError(
                        "Recovery credentials changed after reset; "
                        "preserve both stores for inspection."
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
