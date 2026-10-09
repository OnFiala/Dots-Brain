"""A narrowly scoped CORTEX MCP connector for the appliance.

The connector deliberately has no generic ``call_tool`` or arbitrary fetch API.
Every call is authorized against the local policy and an immutable, explicit
Dots-project to CORTEX-project mapping before it crosses the MCP boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .errors import CapabilityError, ConflictError, InputError, NotFoundError

# This is intentionally separate from Store.SCHEMA.  The schema worker owns when
# it is installed or migrated; connector calls never create tables implicitly.
SCHEMA_SQL = """
CREATE TABLE cortex_operations (
    operation_id TEXT PRIMARY KEY,
    request_digest TEXT NOT NULL,
    principal TEXT NOT NULL,
    local_project TEXT NOT NULL,
    cortex_project TEXT NOT NULL,
    operation_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('planned', 'sending', 'acknowledged', 'uncertain')),
    receipt_json TEXT,
    upstream_object_id TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX cortex_operations_project_state ON cortex_operations(local_project, state);
"""

_READ_SCOPE = "cortex:read"
_WRITE_SCOPE = "cortex:write"
_MAX_QUERY_CHARS = 2_000
_MAX_CONTEXT_CHARS = 24_000
_MIN_CONTEXT_CHARS = 256
_MAX_RESULTS = 20
_WRITE_KINDS = frozenset({"note", "decision", "outcome"})


class CortexConnectorError(CapabilityError):
    """A safe error category for a connector wrapper to expose."""

    code = "cortex_connector_unavailable"


class CortexWriteUncertain(CortexConnectorError):
    code = "cortex_write_uncertain"

    def __init__(self, operation_id: str):
        super().__init__(
            "CORTEX write outcome is uncertain. Inspect or reconcile the stored receipt "
            "before retrying."
        )
        self.operation_id = operation_id


class CortexTransport(Protocol):
    async def call_tool(
        self, name: str, arguments: Mapping[str, object]
    ) -> Mapping[str, object]: ...


class CortexLedger(Protocol):
    def get(self, operation_id: str) -> Mapping[str, object] | None: ...

    def create_planned(self, record: Mapping[str, object]) -> None: ...

    def claim_sending(self, operation_id: str, request_digest: str) -> bool: ...

    def mark_acknowledged(
        self, operation_id: str, *, receipt: Mapping[str, object], upstream_object_id: str | None
    ) -> None: ...

    def mark_uncertain(self, operation_id: str, *, error_code: str) -> None: ...


def setup_cortex_operations(db: sqlite3.Connection) -> None:
    """Install the connector ledger as part of a caller-owned schema transaction."""
    for statement in SCHEMA_SQL.split(";"):
        if statement.strip():
            db.execute(statement)


class SqliteCortexLedger:
    """Ledger adapter for a schema-v2 Store.  It never performs schema setup."""

    def __init__(self, store) -> None:
        self.store = store

    def get(self, operation_id: str) -> Mapping[str, object] | None:
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM cortex_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
        return None if row is None else dict(row)

    def create_planned(self, record: Mapping[str, object]) -> None:
        with self.store.connection(write=True) as db:
            self.store.ensure_writable()
            db.execute(
                """
                INSERT OR IGNORE INTO cortex_operations (
                    operation_id,request_digest,principal,local_project,cortex_project,
                    operation_kind,source_ref,state,receipt_json,upstream_object_id,error_code,
                    created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record["operation_id"],
                    record["request_digest"],
                    record["principal"],
                    record["local_project"],
                    record["cortex_project"],
                    record["operation_kind"],
                    record["source_ref"],
                    "planned",
                    None,
                    None,
                    None,
                    record["created_at"],
                    record["created_at"],
                ),
            )

    def claim_sending(self, operation_id: str, request_digest: str) -> bool:
        from .store import now

        with self.store.connection(write=True) as db:
            self.store.ensure_writable()
            return (
                db.execute(
                    "UPDATE cortex_operations SET state='sending',error_code=NULL,updated_at=? "
                    "WHERE operation_id=? AND request_digest=? AND state='planned'",
                    (now(), operation_id, request_digest),
                ).rowcount
                == 1
            )

    def mark_acknowledged(
        self, operation_id: str, *, receipt: Mapping[str, object], upstream_object_id: str | None
    ) -> None:
        self._update(
            operation_id,
            state="acknowledged",
            receipt_json=json.dumps(receipt, sort_keys=True, separators=(",", ":")),
            upstream_object_id=upstream_object_id,
            error_code=None,
        )

    def mark_uncertain(self, operation_id: str, *, error_code: str) -> None:
        from .store import now

        with self.store.connection(write=True) as db:
            self.store.ensure_writable()
            db.execute(
                "UPDATE cortex_operations SET state='uncertain',error_code=?,updated_at=? "
                "WHERE operation_id=? AND state='sending'",
                (error_code, now(), operation_id),
            )

    def _update(self, operation_id: str, **changes: object) -> None:
        from .store import now

        columns = list(changes)
        values = [changes[column] for column in columns]
        columns.append("updated_at")
        values.append(now())
        assignments = ",".join(f"{column}=?" for column in columns)
        with self.store.connection(write=True) as db:
            self.store.ensure_writable()
            cursor = db.execute(
                f"UPDATE cortex_operations SET {assignments} WHERE operation_id=?",
                (*values, operation_id),
            )
            if cursor.rowcount != 1:
                raise InputError("CORTEX operation receipt is unavailable.")


@dataclass(frozen=True)
class CortexConnectionConfig:
    """Private appliance connection references; the token value is never stored here."""

    endpoint: str
    token_file: Path
    project_mapping: tuple[tuple[str, str], ...]
    token_header: str = "Authorization"
    token_prefix: str = "Bearer "
    timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        _validate_endpoint(self.endpoint)
        if not self.token_header or any(char in self.token_header for char in "\r\n"):
            raise InputError("CORTEX connection configuration is invalid.")
        if any(char in self.token_prefix for char in "\r\n\x00"):
            raise InputError("CORTEX connection configuration is invalid.")
        if not 1 <= self.timeout_seconds <= 60:
            raise InputError("CORTEX connection configuration is invalid.")
        seen: set[str] = set()
        for local_project, cortex_project in self.project_mapping:
            _validate_project(local_project)
            _validate_project(cortex_project)
            if local_project in seen:
                raise InputError("CORTEX connection configuration is invalid.")
            seen.add(local_project)

    def cortex_project_for(self, local_project: str) -> str:
        for local, cortex in self.project_mapping:
            if local == local_project:
                return cortex
        raise NotFoundError("Project is unavailable through the CORTEX connector.")


class StreamableHttpCortexTransport:
    """The real MCP transport, constrained to the configured origin and token file."""

    def __init__(self, config: CortexConnectionConfig) -> None:
        self._config = config

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> Mapping[str, object]:
        token = _read_token(self._config.token_file)
        header_value = (
            token
            if self._config.token_header.lower() != "authorization"
            else (self._config.token_prefix + token)
        )
        try:
            async with (
                asyncio.timeout(self._config.timeout_seconds),
                streamable_http_client(
                    self._config.endpoint,
                    headers={self._config.token_header: header_value},
                    timeout=self._config.timeout_seconds,
                ) as (read, write, _),
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(name, dict(arguments))
        except Exception as exc:
            # Never include the endpoint, headers, token, or upstream body in an error.
            raise CortexConnectorError("CORTEX connector is unavailable.") from exc
        if result.isError:
            raise CortexConnectorError("CORTEX rejected the requested operation.")
        if isinstance(result.structuredContent, dict):
            return result.structuredContent
        for item in result.content:
            text = getattr(item, "text", None)
            if isinstance(text, str):
                try:
                    decoded = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(decoded, dict):
                    return decoded
        raise CortexConnectorError("CORTEX returned an invalid operation receipt.")


ContentGuard = Callable[[Mapping[str, object]], None]


class CortexConnector:
    """Authorization, project boundary, bounded reads, and durable write receipts."""

    def __init__(
        self,
        config: CortexConnectionConfig,
        *,
        transport: CortexTransport | None = None,
        ledger: CortexLedger,
        content_guard: ContentGuard | None = None,
    ) -> None:
        self._config = config
        self._transport = transport or StreamableHttpCortexTransport(config)
        self._ledger = ledger
        self._content_guard = content_guard or _missing_content_guard

    async def read_context(
        self,
        *,
        policy: object,
        project: str,
        query: str,
        limit: int = 10,
        max_chars: int = 6_000,
    ) -> dict[str, object]:
        cortex_project, _ = self._authorize(policy, _READ_SCOPE, project)
        if not isinstance(query, str) or not query.strip() or len(query) > _MAX_QUERY_CHARS:
            raise InputError("CORTEX query is invalid.")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_RESULTS:
            raise InputError("CORTEX result limit is invalid.")
        if (
            not isinstance(max_chars, int)
            or isinstance(max_chars, bool)
            or not (_MIN_CONTEXT_CHARS <= max_chars <= _MAX_CONTEXT_CHARS)
        ):
            raise InputError("CORTEX context limit is invalid.")
        arguments = {"query": query, "project_id": cortex_project}
        self._content_guard(arguments)
        brief = await self._transport.call_tool("cortex_brief", arguments)
        search = await self._transport.call_tool("cortex_search", {**arguments, "limit": limit})
        raw = {"project_id": cortex_project, "brief": brief, "search": search}
        from .privacy import sanitize

        minimized = sanitize(raw, max_text=max_chars)
        context = _bounded_json(minimized.value, max_chars)
        return {
            "project": project,
            "cortex_project": cortex_project,
            "context": context,
            "characters": len(context),
            "limit_unit": "characters_not_tokens",
            "source": "cortex",
            "sanitization": minimized.summary(),
            "partial": bool(minimized.redactions or minimized.truncated),
        }

    async def write_note(
        self,
        *,
        policy: object,
        project: str,
        source_ref: str,
        title: str,
        summary: str = "",
        content: str = "",
        topic: str | None = None,
        operation_id: str | None = None,
    ) -> dict[str, object]:
        return await self._write(
            "note",
            policy=policy,
            project=project,
            source_ref=source_ref,
            payload={"title": title, "summary": summary, "content": content, "topic": topic},
            operation_id=operation_id,
        )

    async def write_decision(
        self,
        *,
        policy: object,
        project: str,
        source_ref: str,
        title: str,
        summary: str = "",
        doctrine_statement: str | None = None,
        doctrine_level: str = "soft",
        operation_id: str | None = None,
    ) -> dict[str, object]:
        return await self._write(
            "decision",
            policy=policy,
            project=project,
            source_ref=source_ref,
            payload={
                "title": title,
                "summary": summary,
                "doctrine_statement": doctrine_statement,
                "doctrine_level": doctrine_level,
            },
            operation_id=operation_id,
        )

    async def write_outcome(
        self,
        *,
        policy: object,
        project: str,
        source_ref: str,
        title: str,
        summary: str,
        status: str,
        topic: str | None = None,
        operation_id: str | None = None,
    ) -> dict[str, object]:
        return await self._write(
            "outcome",
            policy=policy,
            project=project,
            source_ref=source_ref,
            payload={"title": title, "summary": summary, "status": status, "topic": topic},
            operation_id=operation_id,
        )

    async def _write(
        self,
        kind: str,
        *,
        policy: object,
        project: str,
        source_ref: str,
        payload: Mapping[str, object],
        operation_id: str | None,
    ) -> dict[str, object]:
        if kind not in _WRITE_KINDS:
            raise InputError("CORTEX write type is invalid.")
        cortex_project, principal = self._authorize(policy, _WRITE_SCOPE, project)
        _validate_source_ref(source_ref)
        _validate_write_payload(kind, payload)
        # Privacy policy is centralized by the parent.  It must reject any secret
        # before this content is persisted in the receipt ledger or sent upstream.
        self._content_guard(
            {
                "kind": kind,
                "project": project,
                "source_ref": source_ref,
                "principal": principal,
                "payload": dict(payload),
            }
        )
        actual_id = operation_id or _operation_id(principal, project, source_ref, kind)
        _validate_operation_id(actual_id)
        request = self._upstream_request(
            kind, cortex_project, principal, source_ref, payload, actual_id
        )
        digest = _digest(request)
        existing = self._ledger.get(actual_id)
        if existing is not None:
            if existing.get("request_digest") != digest:
                raise ConflictError(
                    "CORTEX operation identity is already bound to different content."
                )
            state = existing.get("state")
            if state == "acknowledged":
                return _receipt_from_row(existing, replayed=True)
            if state in {"sending", "uncertain"}:
                raise CortexWriteUncertain(actual_id)
            if state != "planned":
                raise CortexConnectorError("CORTEX operation receipt is invalid.")
        else:
            from .store import now

            self._ledger.create_planned(
                {
                    "operation_id": actual_id,
                    "request_digest": digest,
                    "principal": principal,
                    "local_project": project,
                    "cortex_project": cortex_project,
                    "operation_kind": kind,
                    "source_ref": source_ref,
                    "created_at": now(),
                }
            )
        if not self._ledger.claim_sending(actual_id, digest):
            existing = self._ledger.get(actual_id)
            if existing is None or existing.get("request_digest") != digest:
                raise ConflictError("CORTEX operation identity is bound to different content.")
            if existing.get("state") == "acknowledged":
                return _receipt_from_row(existing, replayed=True)
            raise CortexWriteUncertain(actual_id)
        try:
            upstream = await self._transport.call_tool(f"cortex_record_{kind}", request)
            upstream = _safe_receipt(upstream)
        except Exception as exc:
            self._ledger.mark_uncertain(actual_id, error_code="transport_or_upstream_failure")
            raise CortexWriteUncertain(actual_id) from exc
        object_id = _object_id(upstream)
        self._ledger.mark_acknowledged(actual_id, receipt=upstream, upstream_object_id=object_id)
        row = self._ledger.get(actual_id)
        if row is None:
            raise CortexConnectorError("CORTEX operation receipt is unavailable.")
        return _receipt_from_row(row, replayed=False)

    def operation_status(self, *, policy, project: str, operation_id: str) -> dict:
        """Read the caller's receipt without expanding project or actor permissions."""
        return _receipt_from_row(
            self._owned_operation(policy, project, operation_id, _READ_SCOPE), replayed=True
        )

    def _owned_operation(self, policy, project, operation_id, scope):
        _, principal = self._authorize(policy, scope, project)
        _validate_operation_id(operation_id)
        row = self._ledger.get(operation_id)
        if row is None or row["principal"] != principal or row["local_project"] != project:
            raise NotFoundError("CORTEX operation is unavailable.")
        return row

    async def reconcile(self, *, policy, project: str, operation_id: str) -> dict:
        """Confirm a projected upstream object. Search absence never authorizes replay."""
        cortex_project, _ = self._authorize(policy, _WRITE_SCOPE, project)
        operation = self._owned_operation(policy, project, operation_id, _WRITE_SCOPE)
        result = _receipt_from_row(operation, replayed=True)
        if result["retrievable_source_ref"]:
            return result
        found = await self._transport.call_tool(
            "cortex_search", {"query": operation_id, "project_id": cortex_project, "limit": 20}
        )
        for candidate in found.get("results", [])[:20]:
            object_id = candidate.get("object_id") if isinstance(candidate, dict) else None
            if not _safe_id(object_id):
                continue
            item = await self._transport.call_tool("cortex_fetch", {"object_id": object_id})
            stored = item.get("stored_object", item)
            if not isinstance(stored, dict) or stored.get("project_id") != cortex_project:
                continue
            field = "content_text" if operation["operation_kind"] == "note" else "summary_text"
            evidence = stored.get(field)
            expected_origin = _origin(operation["principal"], operation["source_ref"], operation_id)
            if not isinstance(evidence, str) or expected_origin not in evidence.splitlines():
                continue
            self._ledger.mark_acknowledged(
                operation_id,
                receipt={"object_id": object_id, "status": "reconciled"},
                upstream_object_id=object_id,
            )
            return _receipt_from_row(
                self._owned_operation(policy, project, operation_id, _WRITE_SCOPE), replayed=True
            )
        return {**result, "reconciliation": "indeterminate", "retry_allowed": False}

    def _authorize(self, policy: object, scope: str, project: str) -> tuple[str, str]:
        _validate_project(project)
        require = getattr(policy, "require", None)
        if not callable(require):
            raise CortexConnectorError("Verified caller policy is required.")
        require(scope)
        projects = getattr(policy, "projects", None)
        if projects is not None and project not in projects:
            raise NotFoundError("Project is unavailable to this client.")
        principal = getattr(policy, "principal", None)
        if not isinstance(principal, str) or not principal.strip() or len(principal) > 500:
            raise CortexConnectorError("Verified caller principal is required.")
        return self._config.cortex_project_for(project), principal

    @staticmethod
    def _upstream_request(
        kind: str,
        cortex_project: str,
        principal: str,
        source_ref: str,
        payload: Mapping[str, object],
        operation_id: str,
    ) -> dict[str, object]:
        origin = _origin(principal, source_ref, operation_id)
        request = {"project_id": cortex_project, **dict(payload)}
        if kind == "note":
            request["content"] = _join_origin(request.get("content"), origin)
            # Notes are explicitly confirmed to support CORTEX idempotency.
            request["idempotency_key"] = operation_id
        else:
            request["summary"] = _join_origin(request.get("summary"), origin)
        return request


def _validate_endpoint(endpoint: str) -> None:
    if not isinstance(endpoint, str):
        raise InputError("CORTEX connection configuration is invalid.")
    parts = urlsplit(endpoint)
    local = parts.hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path != "/mcp"
        or (parts.scheme != "https" and not (parts.scheme == "http" and local))
    ):
        raise InputError("CORTEX connection configuration is invalid.")


def _read_token(path: Path) -> str:
    try:
        path = path.expanduser()
        if not path.is_absolute():
            raise ValueError
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077
                or metadata.st_size > 16384
            ):
                raise ValueError
            token = stream.read(16385).decode("utf-8").removesuffix("\n")
    except (OSError, UnicodeError, ValueError) as exc:
        raise CortexConnectorError("CORTEX connector credentials are unavailable.") from exc
    if not token or len(token) > 16_384 or any(char in token for char in "\r\n\x00"):
        raise CortexConnectorError("CORTEX connector credentials are unavailable.")
    return token


def _validate_project(value: object) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or "\x00" in value:
        raise InputError("CORTEX project is invalid.")


def _validate_source_ref(value: object) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 2_000 or "\x00" in value:
        raise InputError("CORTEX source reference is invalid.")


def _validate_operation_id(value: object) -> None:
    if not isinstance(value, str) or not value.startswith("cxo_") or len(value) > 200:
        raise InputError("CORTEX operation identity is invalid.")


def _validate_write_payload(kind: str, payload: Mapping[str, object]) -> None:
    required = {
        "note": ("title", "summary", "content", "topic"),
        "decision": ("title", "summary", "doctrine_statement", "doctrine_level"),
        "outcome": ("title", "summary", "status", "topic"),
    }[kind]
    if set(payload) != set(required):
        raise InputError("CORTEX write content is invalid.")
    for _name, value in payload.items():
        if value is not None and (
            not isinstance(value, str) or len(value) > 32_000 or "\x00" in value
        ):
            raise InputError("CORTEX write content is invalid.")
    if not isinstance(payload.get("title"), str) or not payload["title"].strip():
        raise InputError("CORTEX write content is invalid.")
    if kind == "outcome" and payload.get("status") not in {"success", "failure", "partial"}:
        raise InputError("CORTEX write content is invalid.")
    if kind == "decision" and payload.get("doctrine_level") not in {"soft", "hard"}:
        raise InputError("CORTEX write content is invalid.")


def _operation_id(principal: str, project: str, source_ref: str, kind: str) -> str:
    raw = json.dumps(
        [principal, project, source_ref, kind], ensure_ascii=False, separators=(",", ":")
    )
    return "cxo_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _digest(request: Mapping[str, object]) -> str:
    raw = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _join_origin(value: object, origin: str) -> str:
    content = "" if value is None else str(value)
    return origin if not content else f"{content}\n\n{origin}"


def _origin(principal: str, source_ref: str, operation_id: str) -> str:
    return (
        f"[Dots Brain origin] principal={principal}; source_ref={source_ref}; "
        f"operation_id={operation_id}"
    )


def _object_id(receipt: Mapping[str, object]) -> str | None:
    value = receipt.get("object_id")
    return value if isinstance(value, str) and value else None


def _safe_id(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[\w:./@-]{1,300}", value))


def _safe_receipt(receipt: Mapping[str, object]) -> dict:
    """Persist identifiers, never upstream payloads or echoed content."""
    clean = {key: receipt[key] for key in ("event_id", "object_id") if _safe_id(receipt.get(key))}
    if not clean:
        raise CortexConnectorError("CORTEX returned no valid acknowledgement identifier.")
    from .privacy import guard_content

    guard_content(clean)
    return clean


def _receipt_from_row(row: Mapping[str, object], *, replayed: bool) -> dict[str, object]:
    receipt_json = row.get("receipt_json")
    receipt = json.loads(receipt_json) if isinstance(receipt_json, str) else None
    object_id = row.get("upstream_object_id")
    return {
        "operation_id": row["operation_id"],
        "request_digest": row["request_digest"],
        "state": row["state"],
        "source_ref": row["source_ref"],
        "receipt": receipt,
        "retrievable_source_ref": f"cortex://object/{object_id}" if object_id else None,
        "acknowledgement_only": object_id is None,
        "replayed": replayed,
    }


def _bounded_json(value: Mapping[str, object], maximum: int) -> str:
    """Return valid JSON and never exceed the caller's character bound."""
    compact = _compact(value, text_limit=max(16, maximum // 8))
    encoded = json.dumps(compact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded) <= maximum:
        return encoded
    # Keep explicit project evidence while reducing variable upstream content.
    fallback = {
        "project_id": str(value.get("project_id", ""))[:80],
        "notice": "CORTEX context exceeded the requested bound.",
    }
    return json.dumps(fallback, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _compact(value: object, *, text_limit: int, depth: int = 0) -> object:
    if depth > 6:
        return "[TRUNCATED_DEPTH]"
    if isinstance(value, str):
        return value if len(value) <= text_limit else value[: max(1, text_limit - 1)] + "…"
    if isinstance(value, list):
        return [
            _compact(item, text_limit=text_limit, depth=depth + 1) for item in value[:_MAX_RESULTS]
        ]
    if isinstance(value, dict):
        return {
            str(key)[:200]: _compact(item, text_limit=text_limit, depth=depth + 1)
            for key, item in islice(value.items(), _MAX_RESULTS)
        }
    return value


def _missing_content_guard(_value: Mapping[str, object]) -> None:
    """Writes fail closed until the appliance injects its canonical privacy guard."""
    raise CortexConnectorError("CORTEX write privacy validation is not configured.")
