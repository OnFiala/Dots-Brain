"""Append-only, scoped audit metadata stored beside the memory database."""

from __future__ import annotations

import hashlib
import json
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


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


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
    ) -> dict:
        policy.require("audit:write")
        self._policy_project(policy, project)
        if kind not in _KINDS:
            raise InputError("Unsupported audit event kind.")
        validate_text(client_event_id, "client_event_id", 500)
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
        payload_hash = hashlib.sha256(_canonical(payload).encode()).hexdigest()
        with self.store.connection(write=True) as db:
            self.store.ensure_writable()
            existing = db.execute(
                "SELECT * FROM audit_events WHERE principal=? AND project=? AND client_event_id=?",
                (principal, project, client_event_id),
            ).fetchone()
            if existing:
                if occurred_at is None:
                    payload["occurred_at"] = existing["occurred_at"]
                    payload_hash = hashlib.sha256(_canonical(payload).encode()).hexdigest()
                if existing["payload_hash"] != payload_hash:
                    raise ConflictError(
                        "An audit event with this client_event_id has different content."
                    )
                return {
                    "id": existing["id"],
                    "replayed": True,
                    "event_hash": existing["event_hash"],
                }
            if (
                intent_event_id is not None
                and not db.execute(
                    "SELECT 1 FROM audit_events WHERE project=? AND principal=? "
                    "AND client_event_id=? AND kind='intent'",
                    (project, principal, intent_event_id),
                ).fetchone()
            ):
                raise InputError(
                    "A receipt must reference an existing intent for this actor and project."
                )
            previous = db.execute(
                "SELECT event_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            prev_hash = "" if previous is None else previous["event_hash"]
            event_hash = hashlib.sha256((prev_hash + payload_hash).encode()).hexdigest()
            recorded_at = datetime.now(UTC).isoformat()
            cursor = db.execute(
                """INSERT INTO audit_events(
                project,principal,client_event_id,kind,evidence,occurred_at,recorded_at,
                action,target,details,intent_event_id,payload_hash,prev_hash,event_hash)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
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
    ) -> list[dict]:
        policy.require("audit:read")
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            raise InputError("limit must be between 1 and 500.")
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
        if since:
            clauses.append("occurred_at>=?")
            values.append(_time(since))
        if until:
            clauses.append("occurred_at<=?")
            values.append(_time(until))
        if after_id is not None:
            clauses.append("id>?")
            values.append(after_id)
        with self.store.connection() as db:
            rows = db.execute(
                f"SELECT * FROM audit_events WHERE {' AND '.join(clauses)} ORDER BY id LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def report(
        self,
        policy,
        *,
        project: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
    ) -> dict:
        rows = self.events(policy, project=project, since=since, until=until, limit=limit)
        intents = [row for row in rows if row["kind"] == "intent"]
        receipts = {
            (row["project"], row["principal"], row["intent_event_id"])
            for row in rows
            if row["kind"] == "receipt"
        }
        missing = sum(
            (item["project"], item["principal"], item["client_event_id"]) not in receipts
            for item in intents
        )
        gaps = sum(item["kind"] == "gap" for item in rows)
        has_more = bool(
            rows
            and self.events(
                policy, project=project, since=since, until=until, after_id=rows[-1]["id"], limit=1
            )
        )
        severity = "warning" if missing or gaps or has_more else "info"
        return {
            "status": "partial" if missing or gaps or has_more else "observed",
            "severity": severity,
            "window": {
                "since": since,
                "until": until,
                "returned": len(rows),
                "limit": limit,
                "has_more": has_more,
                "complete": not has_more,
            },
            "high_watermark": rows[-1]["id"] if rows else None,
            "coverage": {
                "intents": len(intents),
                "receipts": len(receipts),
                "missing_receipts": missing,
                "gaps": gaps,
            },
            "note": "Coverage describes recorded events only; it does not prove complete "
            "provider logs.",
        }
