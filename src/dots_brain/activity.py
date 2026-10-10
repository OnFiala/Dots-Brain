"""Append-only, scoped audit metadata stored beside the memory database."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from .errors import ConflictError, InputError, NotFoundError
from .privacy import sanitize
from .store import validate_text

SCHEMA_SQL = (
    """CREATE TABLE audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, principal TEXT NOT NULL,
        client_event_id TEXT NOT NULL, kind TEXT NOT NULL, evidence TEXT NOT NULL,
        occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL, action TEXT NOT NULL,
        target TEXT NOT NULL, details TEXT NOT NULL, intent_event_id TEXT,
        payload_hash TEXT NOT NULL, prev_hash TEXT NOT NULL, event_hash TEXT NOT NULL,
        UNIQUE(principal, project, client_event_id)
    )""",
    "CREATE INDEX audit_events_project_id ON audit_events(project, id)",
    "CREATE INDEX audit_events_intent ON audit_events(project, principal, intent_event_id)",
    """CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events
       BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END""",
    """CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events
       BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END""",
    """CREATE TRIGGER audit_events_chain BEFORE INSERT ON audit_events
       BEGIN SELECT CASE WHEN NEW.prev_hash != COALESCE(
       (SELECT event_hash FROM audit_events ORDER BY id DESC LIMIT 1), '')
       THEN RAISE(ABORT, 'audit hash chain mismatch') END; END""",
)
_KINDS = frozenset({"intent", "receipt", "error", "gap", "correction", "coverage", "action"})
_CHAIN_V3 = "v3:"
_CHAIN_DOMAIN = "dots-brain.audit-chain.v3"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(*parts: str) -> str:
    """Hash a versioned sequence without relying on ambiguous string joins."""
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def _time(value: str | None) -> str:
    if value is None:
        return datetime.now(UTC).isoformat()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(UTC).isoformat()
    except (AttributeError, TypeError, ValueError) as exc:
        raise InputError("occurred_at must be an ISO-8601 timestamp with a timezone.") from exc


class AuditLog:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _policy_project(policy, project: str) -> None:
        validate_text(project, "project", 200)
        if getattr(policy, "projects", None) is not None and project not in policy.projects:
            raise NotFoundError("Project is not available to this client.")

    @staticmethod
    def _metadata(value: Any) -> tuple[str, dict[str, Any]]:
        result = sanitize({} if value is None else value, max_items=40, max_text=2000)
        return _canonical(result.value), result.summary()

    @staticmethod
    def _payload(row: dict[str, Any]) -> dict[str, Any]:
        """Return the canonical payload from a database row or normalized API event."""

        def decoded(name: str) -> Any:
            value = row[name]
            return json.loads(value) if isinstance(value, str) else value

        return {
            "project": row["project"],
            "principal": row["principal"],
            "client_event_id": row["client_event_id"],
            "kind": row["kind"],
            "evidence": row["evidence"],
            "occurred_at": row["occurred_at"],
            "action": decoded("action"),
            "target": decoded("target"),
            "details": decoded("details"),
            "intent_event_id": row["intent_event_id"],
        }

    @classmethod
    def _hashes(cls, row: dict[str, Any], previous_hash: str) -> tuple[dict[str, Any], str, str]:
        """Verify-compatible v2/v3 hashes; v3 binds durable event identity and stamp."""
        payload = cls._payload(row)
        stored_payload_hash = row.get("payload_hash", "")
        if stored_payload_hash.startswith(_CHAIN_V3):
            envelope = {
                "encoding": _CHAIN_DOMAIN,
                "id": row["id"],
                "recorded_at": row["recorded_at"],
                "payload": payload,
            }
            payload_hash = _CHAIN_V3 + _digest(_CHAIN_DOMAIN, "payload", _canonical(envelope))
            event_hash = _CHAIN_V3 + _digest(_CHAIN_DOMAIN, "event", previous_hash, payload_hash)
        else:
            payload_hash = hashlib.sha256(_canonical(payload).encode()).hexdigest()
            event_hash = hashlib.sha256((previous_hash + payload_hash).encode()).hexdigest()
        return payload, payload_hash, event_hash

    @classmethod
    def verify_row(cls, row: dict[str, Any], previous_hash: str) -> dict[str, Any]:
        """Verify one public audit row and return its normalized payload.

        This is shared by the local review tool so its anchor verification uses
        exactly the same legacy and v3 encoding as database recovery.
        """
        payload, payload_hash, event_hash = cls._hashes(row, previous_hash)
        if (
            row["prev_hash"] != previous_hash
            or row["payload_hash"] != payload_hash
            or row["event_hash"] != event_hash
        ):
            raise ConflictError("Audit hash chain verification failed.")
        return payload

    def record(
        self,
        policy,
        *,
        project: str,
        kind: str,
        client_event_id: str,
        action: Any = None,
        target: Any = None,
        details: Any = None,
        occurred_at: str | None = None,
        intent_event_id: str | None = None,
    ) -> dict:
        return self._append(
            policy,
            project=project,
            kind=kind,
            client_event_id=client_event_id,
            action=action,
            target=target,
            details=details,
            occurred_at=occurred_at,
            intent_event_id=intent_event_id,
            evidence="client_report",
        )

    def observed(self, policy, **kwargs) -> dict:
        """Internal integration path for server-observed MCP handling only."""
        return self._append(policy, evidence="server_observed", **kwargs)

    def observed_in_connection(self, db, policy, **kwargs) -> dict:
        """Append server-observed metadata in the caller's SQLite transaction.

        Callers use this for one auth or publication state transition and its
        durable audit receipt. The caller controls commit/rollback; credentials
        and provider payloads must never be passed as event metadata.
        """
        return self._append(policy, evidence="server_observed", db=db, **kwargs)

    @contextmanager
    def _write_connection(self, connection):
        if connection is None:
            with self.store.connection(write=True) as db:
                yield db
        else:
            yield connection

    def _append(
        self,
        policy,
        *,
        project: str,
        kind: str,
        client_event_id: str,
        action: Any = None,
        target: Any = None,
        details: Any = None,
        occurred_at: str | None = None,
        intent_event_id: str | None = None,
        evidence: str,
        db=None,
    ) -> dict:
        policy.require("audit:write")
        self._policy_project(policy, project)
        if kind not in _KINDS:
            raise InputError("Unsupported audit event kind.")
        validate_text(client_event_id, "client_event_id", 500)
        if kind == "receipt" and intent_event_id is None:
            raise InputError(
                "A receipt must reference an existing intent for this actor and project."
            )
        if intent_event_id is not None:
            validate_text(intent_event_id, "intent_event_id", 500)
            if kind != "receipt":
                raise InputError("Only a receipt can reference an intent.")
        principal = validate_text(getattr(policy, "principal", ""), "principal", 500)
        action_json, action_summary = self._metadata(action)
        target_json, target_summary = self._metadata(target)
        details_json, details_summary = self._metadata(details)
        details_json = _canonical(
            {
                "value": json.loads(details_json),
                "sanitization": {
                    "action": action_summary,
                    "target": target_summary,
                    "details": details_summary,
                },
            }
        )
        timestamp = _time(occurred_at)
        payload = {
            "project": project,
            "principal": principal,
            "client_event_id": client_event_id,
            "kind": kind,
            "evidence": evidence,
            "occurred_at": timestamp,
            "action": json.loads(action_json),
            "target": json.loads(target_json),
            "details": json.loads(details_json),
            "intent_event_id": intent_event_id,
        }
        with self._write_connection(db) as db:
            self.store.ensure_writable()
            existing = db.execute(
                "SELECT * FROM audit_events WHERE principal=? AND project=? AND client_event_id=?",
                (principal, project, client_event_id),
            ).fetchone()
            if existing:
                if occurred_at is None:
                    payload["occurred_at"] = existing["occurred_at"]
                # The requested payload is compared to the persisted row's encoding.
                existing_payload = self._payload(dict(existing))
                if payload != existing_payload:
                    raise ConflictError(
                        "An audit event with this client_event_id has different content."
                    )
                return {
                    "id": existing["id"],
                    "replayed": True,
                    "event_hash": existing["event_hash"],
                }
            if intent_event_id is not None and not (
                intent := db.execute(
                    "SELECT evidence FROM audit_events WHERE project=? AND principal=? "
                    "AND client_event_id=? AND kind='intent'",
                    (project, principal, intent_event_id),
                ).fetchone()
            ):
                raise InputError(
                    "A receipt must reference an existing intent for this actor and project."
                )
            if intent_event_id is not None and intent["evidence"] != evidence:
                raise InputError("A receipt must have the same evidence class as its intent.")
            if (
                intent_event_id is not None
                and db.execute(
                    "SELECT 1 FROM audit_events WHERE project=? AND principal=? "
                    "AND intent_event_id=? AND kind='receipt'",
                    (project, principal, intent_event_id),
                ).fetchone()
            ):
                raise ConflictError("An intent already has a receipt for this actor and project.")
            previous = db.execute(
                "SELECT event_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            prev_hash = "" if previous is None else previous["event_hash"]
            recorded_at = datetime.now(UTC).isoformat()
            # The write transaction serializes SQLite writers, so this explicit ID is
            # the ID committed by the INSERT and can be bound into the v3 envelope.
            event_id = db.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM audit_events").fetchone()[0]
            hash_row = {
                "id": event_id,
                "recorded_at": recorded_at,
                "payload_hash": _CHAIN_V3,
                "project": project,
                "principal": principal,
                "client_event_id": client_event_id,
                "kind": kind,
                "evidence": evidence,
                "occurred_at": timestamp,
                "action": action_json,
                "target": target_json,
                "details": details_json,
                "intent_event_id": intent_event_id,
            }
            _, payload_hash, event_hash = self._hashes(hash_row, prev_hash)
            cursor = db.execute(
                """INSERT INTO audit_events(
                id,project,principal,client_event_id,kind,evidence,occurred_at,recorded_at,
                action,target,details,intent_event_id,payload_hash,prev_hash,event_hash)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id,
                    project,
                    principal,
                    client_event_id,
                    kind,
                    evidence,
                    timestamp,
                    recorded_at,
                    action_json,
                    target_json,
                    details_json,
                    intent_event_id,
                    payload_hash,
                    prev_hash,
                    event_hash,
                ),
            )
        return {
            "id": cursor.lastrowid,
            "replayed": False,
            "event_hash": event_hash,
            "sanitization": {
                "action": action_summary,
                "target": target_summary,
                "details": details_summary,
            },
        }

    def events(
        self,
        policy,
        *,
        project: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
        after_id: int | None = None,
        recorded: bool = False,
    ) -> list[dict]:
        policy.require("audit:read")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise InputError("limit must be between 1 and 500.")
        if after_id is not None and (type(after_id) is not int or after_id < 0):
            raise InputError("after_id must be a non-negative integer.")
        clauses, values = ["1=1"], []
        if project is not None:
            self._policy_project(policy, project)
            clauses.append("project=?")
            values.append(project)
        elif getattr(policy, "projects", None) is not None:
            if not policy.projects:
                return []
            clauses.append(f"project IN ({','.join('?' for _ in policy.projects)})")
            values.extend(policy.projects)
        timestamp_column = "recorded_at" if recorded else "occurred_at"
        if since:
            clauses.append(f"{timestamp_column}>=?")
            values.append(_time(since))
        if until:
            clauses.append(f"{timestamp_column}<=?")
            values.append(_time(until))
        if after_id is not None:
            clauses.append("id>?")
            values.append(after_id)
        with self.store.connection() as db:
            rows = db.execute(
                f"SELECT * FROM audit_events WHERE {' AND '.join(clauses)} ORDER BY id LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [
            {
                **dict(row),
                **{key: json.loads(row[key]) for key in ("action", "target", "details")},
            }
            for row in rows
        ]

    def page(self, policy, **kwargs) -> dict[str, Any]:
        """One normalized page with one canonical continuation calculation."""
        after_id = kwargs.get("after_id") or 0
        rows = self.events(policy, **kwargs)
        more = bool(
            rows and self.events(policy, **(kwargs | {"after_id": rows[-1]["id"], "limit": 1}))
        )
        return {
            "events": rows,
            "next_after_id": rows[-1]["id"] if rows else after_id,
            "has_more": more,
        }

    def report(
        self,
        policy,
        *,
        project: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
        after_id: int | None = None,
    ) -> dict:
        rows = self.events(
            policy,
            project=project,
            since=since,
            until=until,
            limit=limit,
            after_id=after_id,
            recorded=True,
        )
        intents = [row for row in rows if row["kind"] == "intent"]
        receipts = sum(row["kind"] == "receipt" for row in rows)
        # An audit report is a recorded-at window, but an intent may be closed by
        # a later receipt outside that window. Check canonical history per intent.
        with self.store.connection() as db:
            missing = sum(
                not db.execute(
                    "SELECT 1 FROM audit_events WHERE project=? AND principal=? "
                    "AND intent_event_id=? AND kind='receipt' AND evidence=? LIMIT 1",
                    (
                        item["project"],
                        item["principal"],
                        item["client_event_id"],
                        item["evidence"],
                    ),
                ).fetchone()
                for item in intents
            )
        gaps = sum(item["kind"] == "gap" for item in rows)
        has_more = bool(
            rows
            and self.events(
                policy,
                project=project,
                since=since,
                until=until,
                after_id=rows[-1]["id"],
                limit=1,
                recorded=True,
            )
        )
        status = "empty" if not rows else "partial" if missing or gaps or has_more else "observed"
        severity = "warning" if missing or gaps or has_more else "info"
        return {
            "status": status,
            "severity": severity,
            "window": {
                "since": since,
                "until": until,
                "returned": len(rows),
                "limit": limit,
                "after_id": after_id,
                "has_more": has_more,
                "complete": not has_more,
            },
            "high_watermark": rows[-1]["id"] if rows else None,
            "coverage": {
                "intents": len(intents),
                "receipts": receipts,
                "missing_receipts": missing,
                "gaps": gaps,
            },
            "note": "Coverage describes recorded events only; it does not prove complete "
            "provider logs.",
        }

    def verify(self) -> dict[str, int]:
        """Verify v2/v3 continuity without rewriting history.

        These are unkeyed integrity hashes, so a valid chain does not authenticate
        the original actor or prove provider-wide capture.
        """
        with self.store.connection() as db:
            return self.verify_connection(db)

    @staticmethod
    def verify_connection(db) -> dict[str, int]:
        """Verify a caller-owned source or restored SQLite connection in place."""
        previous = ""
        checked = 0
        rows = db.execute("SELECT * FROM audit_events ORDER BY id")
        columns = [column[0] for column in rows.description]
        for values in rows:
            row = dict(zip(columns, values, strict=True))
            AuditLog.verify_row(row, previous)
            previous, checked = row["event_hash"], checked + 1
        return {"events": checked}
