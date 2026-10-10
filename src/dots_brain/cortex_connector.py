"""A narrowly scoped CORTEX MCP connector for the appliance.

The connector deliberately has no generic ``call_tool`` or arbitrary fetch API.
Every call is authorized against the local policy and an immutable, explicit
Dots-project to CORTEX-project mapping before it crosses the MCP boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import stat
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .auth import validate_endpoint
from .errors import CapabilityError, ConflictError, InputError, NotFoundError

logger = logging.getLogger(__name__)

_READ_SCOPE = "cortex:read"
_WRITE_SCOPE = "cortex:write"
_MAX_QUERY_CHARS = 2_000
_MAX_CONTEXT_CHARS = 24_000
_MIN_CONTEXT_CHARS = 256
_MAX_RESULTS = 20
_WRITE_KINDS = frozenset({"note", "decision", "outcome"})
_PROVEN_UNSENT_ERRORS = frozenset({"cortex_pre_send_failed", "cortex_credentials_unavailable"})


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


class CortexWriteRejected(CortexConnectorError):
    """An upstream error response; it does not prove that no write committed."""

    code = "cortex_write_rejected"


class CortexPreSendError(CortexConnectorError):
    """A locally known failure before the connector could send a write."""

    code = "cortex_pre_send_failed"


class CortexCredentialError(CortexPreSendError):
    code = "cortex_credentials_unavailable"


class CortexTransport(Protocol):
    async def call_tool(
        self, name: str, arguments: Mapping[str, object]
    ) -> Mapping[str, object]: ...

    async def prepare_write(self) -> None: ...


class CortexLedger(Protocol):
    def get(self, operation_id: str) -> Mapping[str, object] | None: ...

    def list_owned(self, principal: str, project: str) -> list[Mapping[str, object]]: ...

    def create_planned(self, record: Mapping[str, object]) -> None: ...

    def claim_sending(self, operation_id: str, request_digest: str) -> bool: ...

    def mark_acknowledged(
        self, operation_id: str, *, receipt: Mapping[str, object], upstream_object_id: str | None
    ) -> None: ...

    def mark_uncertain(self, operation_id: str, *, error_code: str) -> None: ...

    def release_planned(self, operation_id: str, *, error_code: str) -> None: ...

    def return_pre_send(
        self, operation_id: str, request_digest: str, *, error_code: str
    ) -> bool: ...

    def retry_uncertain(self, operation_id: str, *, error_code: str) -> bool: ...

    def recover_sending(self, operation_id: str, *, error_code: str) -> bool: ...

    def supersede_pre_send_outcomes(
        self,
        *,
        principal: str,
        project: str,
        source_ref: str,
        replacement: Mapping[str, object],
    ) -> bool: ...


class SqliteCortexLedger:
    """Ledger adapter for an initialized Store. It never creates tables."""

    def __init__(self, store) -> None:
        self.store = store

    def get(self, operation_id: str) -> Mapping[str, object] | None:
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM cortex_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
        return None if row is None else dict(row)

    def list_owned(self, principal: str, project: str) -> list[Mapping[str, object]]:
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT * FROM cortex_operations WHERE principal=? AND local_project=? "
                "ORDER BY created_at",
                (principal, project),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_project(self, project: str) -> list[Mapping[str, object]]:
        with self.store.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM cortex_operations WHERE local_project=? "
                    "ORDER BY created_at,operation_id",
                    (project,),
                )
            ]

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

    def release_planned(self, operation_id: str, *, error_code: str) -> None:
        """Annotate an operation which has not yet been claimed for sending."""
        from .store import now

        with self.store.connection(write=True) as db:
            self.store.ensure_writable()
            db.execute(
                "UPDATE cortex_operations SET state='planned',error_code=?,updated_at=? "
                "WHERE operation_id=? AND state='planned'",
                (error_code, now(), operation_id),
            )

    def return_pre_send(self, operation_id: str, request_digest: str, *, error_code: str) -> bool:
        """Release this sender's proven-unsent claim without changing another sender."""
        from .store import now

        with self.store.connection(write=True) as db:
            self.store.ensure_writable()
            return (
                db.execute(
                    "UPDATE cortex_operations SET state='planned',error_code=?,updated_at=? "
                    "WHERE operation_id=? AND request_digest=? AND state='sending'",
                    (error_code, now(), operation_id, request_digest),
                ).rowcount
                == 1
            )

    def retry_uncertain(self, operation_id: str, *, error_code: str) -> bool:
        """Record an explicit owner retry decision without changing active sends."""
        from .store import now

        with self.store.connection(write=True) as db:
            self.store.ensure_writable()
            return (
                db.execute(
                    "UPDATE cortex_operations SET state='planned',error_code=?,updated_at=? "
                    "WHERE operation_id=? AND state='uncertain' "
                    "AND COALESCE(error_code,'') NOT GLOB 'superseded_*'",
                    (error_code, now(), operation_id),
                ).rowcount
                == 1
            )

    def recover_sending(self, operation_id: str, *, error_code: str) -> bool:
        """Mark an orphaned send uncertain while every cooperating writer is stopped."""
        from .local import locked
        from .store import now

        with locked(self.store.directory / "writers.lock", timeout=0):
            with self.store.connection(write=True) as db:
                self.store.ensure_writable()
                return (
                    db.execute(
                        "UPDATE cortex_operations SET state='uncertain',error_code=?,updated_at=? "
                        "WHERE operation_id=? AND state='sending'",
                        (error_code, now(), operation_id),
                    ).rowcount
                    == 1
                )

    def supersede_pre_send_outcomes(
        self,
        *,
        principal: str,
        project: str,
        source_ref: str,
        replacement: Mapping[str, object],
    ) -> bool:
        """Atomically retire proven-unsent outcomes before recording a corrected outcome."""
        from .store import now

        with self.store.connection(write=True) as db:
            self.store.ensure_writable()
            prior = db.execute(
                "SELECT operation_id,error_code,state FROM cortex_operations "
                "WHERE principal=? AND local_project=? AND source_ref=? "
                "AND operation_kind='outcome' "
                "AND COALESCE(error_code,'') NOT GLOB 'superseded_*' "
                "AND operation_id<>?",
                (principal, project, source_ref, replacement["operation_id"]),
            ).fetchall()
            if not prior or any(
                row["state"] != "planned" or row["error_code"] not in _PROVEN_UNSENT_ERRORS
                for row in prior
            ):
                return False
            timestamp = now()
            for row in prior:
                db.execute(
                    "UPDATE cortex_operations SET state='uncertain',error_code=?,updated_at=? "
                    "WHERE operation_id=? AND state='planned'",
                    (f"superseded_{row['error_code']}", timestamp, row["operation_id"]),
                )
            inserted = db.execute(
                """
                INSERT OR IGNORE INTO cortex_operations (
                    operation_id,request_digest,principal,local_project,cortex_project,
                    operation_kind,source_ref,state,receipt_json,upstream_object_id,error_code,
                    created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    replacement["operation_id"],
                    replacement["request_digest"],
                    replacement["principal"],
                    replacement["local_project"],
                    replacement["cortex_project"],
                    replacement["operation_kind"],
                    replacement["source_ref"],
                    "planned",
                    None,
                    None,
                    None,
                    replacement["created_at"],
                    replacement["created_at"],
                ),
            )
            if inserted.rowcount != 1:
                raise ConflictError(
                    "CORTEX operation identity is already bound to different content."
                )
            return True

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
        if not self.project_mapping:
            raise InputError("CORTEX project mapping must not be empty.")
        seen: set[str] = set()
        targets: set[str] = set()
        for local_project, cortex_project in self.project_mapping:
            _validate_project(local_project)
            _validate_project(cortex_project)
            if local_project in seen or cortex_project in targets:
                raise InputError("CORTEX project mapping must be one-to-one.")
            seen.add(local_project)
            targets.add(cortex_project)

    def cortex_project_for(self, local_project: str) -> str:
        for local, cortex in self.project_mapping:
            if local == local_project:
                return cortex
        raise NotFoundError("Project is unavailable through the CORTEX connector.")


class StreamableHttpCortexTransport:
    """The real MCP transport, constrained to the configured origin and token file."""

    def __init__(self, config: CortexConnectionConfig) -> None:
        self._config = config

    async def prepare_write(self) -> None:
        """Reject a missing or unsafe credential before a ledger claim is made."""
        _read_token(self._config.token_file)

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> Mapping[str, object]:
        async with self.operation() as transport:
            return await transport.call_tool(name, arguments)

    @asynccontextmanager
    async def operation(self):
        """Reuse one authenticated MCP session for calls in one connector operation."""
        token = _read_token(self._config.token_file)
        header_value = (
            token
            if self._config.token_header.lower() != "authorization"
            else (self._config.token_prefix + token)
        )
        try:
            async with (
                httpx.AsyncClient(
                    headers={self._config.token_header: header_value},
                    timeout=self._config.timeout_seconds,
                    trust_env=False,
                    follow_redirects=False,
                ) as client,
                asyncio.timeout(self._config.timeout_seconds),
                streamable_http_client(self._config.endpoint, http_client=client) as (
                    read,
                    write,
                    _,
                ),
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield _McpSessionTransport(session)
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError | KeyboardInterrupt | SystemExit):
                raise
            classified = _classify_transport_failure(exc)
            if classified is not None:
                raise classified from exc
            # Never include the endpoint, headers, token, or upstream body in an error.
            logger.warning("CORTEX transport failed: %s", type(exc).__name__)
            raise CortexConnectorError("CORTEX connector is unavailable.") from exc


class _McpSessionTransport:
    """Parse one initialized SDK session without exposing it to connector callers."""

    def __init__(self, session: ClientSession) -> None:
        self._session = session

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> Mapping[str, object]:
        result = await self._session.call_tool(name, dict(arguments))
        if result.isError:
            raise CortexWriteRejected("CORTEX rejected the requested operation.")
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


def _classify_transport_failure(exc: BaseException) -> CortexConnectorError | None:
    """Classify only leaves whose transport position proves no write was sent."""
    leaves = _exception_leaves(exc)
    if len(leaves) == 1 and isinstance(leaves[0], CortexConnectorError):
        return leaves[0]
    if leaves and all(isinstance(leaf, httpx.ConnectError) for leaf in leaves):
        logger.warning("CORTEX connection failed before request: %s", type(leaves[0]).__name__)
        return CortexPreSendError("CORTEX connector is unavailable before sending.")
    return None


def _exception_leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for nested in exc.exceptions for leaf in _exception_leaves(nested)]
    return [exc]


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
        self._content_guard({"content": query})
        brief = await self._transport.call_tool("cortex_brief", arguments)
        search = await self._transport.call_tool("cortex_search", {**arguments, "limit": limit})
        raw = {"project_id": cortex_project, "brief": brief, "search": search}
        minimized, context, bounded = await anyio.to_thread.run_sync(
            _minimize_context, raw, max_chars
        )
        return {
            "project": project,
            "cortex_project": cortex_project,
            "context": context,
            "characters": len(context),
            "limit_unit": "characters_not_tokens",
            "source": "cortex",
            "sanitization": minimized.summary(),
            "partial": bool(minimized.redactions or minimized.truncated or bounded),
            "untrusted_data": True,
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
        actual_id = operation_id or _operation_id(
            principal, project, source_ref, kind, outcome_status=payload.get("status")
        )
        _validate_operation_id(actual_id)
        request = self._upstream_request(
            kind, cortex_project, principal, source_ref, payload, actual_id
        )
        digest = _digest(request)
        from .store import now

        planned = {
            "operation_id": actual_id,
            "request_digest": digest,
            "principal": principal,
            "local_project": project,
            "cortex_project": cortex_project,
            "operation_kind": kind,
            "source_ref": source_ref,
            "created_at": now(),
        }
        # SQLite ledger calls are synchronous and can block on its writer lock;
        # never perform them in the MCP event loop.
        existing = await asyncio.to_thread(self._ledger.get, actual_id)
        superseded = False
        if kind == "outcome" and existing is None:
            supersede = getattr(self._ledger, "supersede_pre_send_outcomes", None)
            if callable(supersede):
                superseded = await asyncio.to_thread(
                    supersede,
                    principal=principal,
                    project=project,
                    source_ref=source_ref,
                    replacement=planned,
                )
            if not superseded:
                await asyncio.to_thread(
                    self._reject_prior_outcome,
                    principal,
                    project,
                    source_ref,
                    actual_id,
                )
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
        elif not superseded:
            await asyncio.to_thread(
                self._ledger.create_planned,
                planned,
            )
        try:
            await _prepare_write(self._transport)
        except CortexConnectorError as exc:
            # Token/config validation happens before claim_sending.  Preserve a
            # durable, retryable reason without representing this as delivery risk.
            await asyncio.to_thread(
                self._ledger.release_planned, actual_id, error_code=_error_code(exc)
            )
            raise
        if not await asyncio.to_thread(self._ledger.claim_sending, actual_id, digest):
            existing = await asyncio.to_thread(self._ledger.get, actual_id)
            if existing is None or existing.get("request_digest") != digest:
                raise ConflictError("CORTEX operation identity is bound to different content.")
            if existing.get("state") == "acknowledged":
                return _receipt_from_row(existing, replayed=True)
            raise CortexWriteUncertain(actual_id)
        try:
            upstream = await self._transport.call_tool(f"cortex_record_{kind}", request)
            upstream = _safe_receipt(upstream)
            await asyncio.to_thread(
                self._ledger.mark_acknowledged,
                actual_id,
                receipt=upstream,
                upstream_object_id=_object_id(upstream),
            )
            row = await asyncio.to_thread(self._ledger.get, actual_id)
            if row is None:
                raise CortexConnectorError("CORTEX operation receipt is unavailable.")
            return _receipt_from_row(row, replayed=False)
        except CortexWriteRejected as exc:
            # MCP isError is not proof of rollback: an upstream implementation
            # may commit and then fail while producing its response.
            try:
                await asyncio.to_thread(
                    self._ledger.mark_uncertain, actual_id, error_code=_error_code(exc)
                )
            except Exception:
                pass
            raise CortexWriteUncertain(actual_id) from exc
        except CortexPreSendError as exc:
            try:
                released = await asyncio.to_thread(
                    self._ledger.return_pre_send,
                    actual_id,
                    digest,
                    error_code=_error_code(exc),
                )
            except Exception:
                released = False
            if not released:
                raise CortexWriteUncertain(actual_id) from exc
            raise
        except asyncio.CancelledError:
            # Cancellation after a claimed send never leaves an immortal
            # ``sending`` record. Its upstream outcome remains unknown.
            try:
                await asyncio.shield(
                    asyncio.to_thread(
                        self._ledger.mark_uncertain, actual_id, error_code="cancelled_after_send"
                    )
                )
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                await asyncio.shield(
                    asyncio.to_thread(
                        self._ledger.mark_uncertain,
                        actual_id,
                        error_code="write_or_receipt_failure",
                    )
                )
            except Exception:
                # A committed sending state still blocks replay if persistence failed.
                pass
            raise CortexWriteUncertain(actual_id) from exc

    def operation_status(self, *, policy, project: str, operation_id: str) -> dict:
        """Read the caller's receipt without expanding project or actor permissions."""
        return _receipt_from_row(
            self._owned_operation(policy, project, operation_id, _READ_SCOPE), replayed=False
        )

    def operations(self, *, policy, project: str) -> list[dict[str, object]]:
        """Return the caller's own operation receipts without expanding scope."""
        _, principal = self._authorize(policy, _READ_SCOPE, project)
        if principal == "local-owner:stdio" and callable(
            getattr(self._ledger, "list_project", None)
        ):
            return [
                _receipt_from_row(row, replayed=False) for row in self._ledger.list_project(project)
            ]
        rows = getattr(self._ledger, "list_owned", None)
        if not callable(rows):
            return []
        return [_receipt_from_row(row, replayed=False) for row in rows(principal, project)]

    def resolve_operation(
        self,
        *,
        policy,
        project: str,
        operation_id: str,
        resolution: str,
        writers_stopped: bool = False,
    ) -> dict[str, object]:
        """Record an explicit owner retry decision after uncertainty review.

        This records no asserted upstream success.  Only the local operator may
        invoke it. The upstream outcome remains unproven and any later send is
        a deliberate owner action, never an automatic retry.
        """
        if getattr(policy, "principal", None) != "local-owner:stdio":
            raise NotFoundError("CORTEX operation is unavailable.")
        self._authorize(policy, _WRITE_SCOPE, project)
        _validate_operation_id(operation_id)
        operation = self._ledger.get(operation_id)
        if operation is None or operation["local_project"] != project:
            raise NotFoundError("CORTEX operation is unavailable.")
        if resolution == "recover-sending":
            if not writers_stopped:
                raise InputError(
                    "Stop every writer and pass --writers-stopped before recovering send."
                )
            recover = getattr(self._ledger, "recover_sending", None)
            if not callable(recover) or not recover(
                operation_id, error_code="recovered_after_sender_exit"
            ):
                raise InputError("CORTEX operation resolution is invalid.")
            receipt = _receipt_from_row(self._ledger.get(operation_id), replayed=False)
            return {
                **receipt,
                "resolution": "sending_recovered",
                "retry_allowed": False,
                "next_action": "reconcile_or_explicit_retry",
            }
        if (
            resolution != "retry"
            or operation["state"] != "uncertain"
            or str(operation.get("error_code") or "").startswith("superseded_")
        ):
            raise InputError("CORTEX operation resolution is invalid.")
        if not self._ledger.retry_uncertain(operation_id, error_code="operator_authorized_retry"):
            raise InputError("CORTEX operation resolution is invalid.")
        receipt = _receipt_from_row(self._ledger.get(operation_id), replayed=False)
        return {**receipt, "resolution": "retry_allowed"}

    def _owned_operation(self, policy, project, operation_id, scope):
        _, principal = self._authorize(policy, scope, project)
        _validate_operation_id(operation_id)
        row = self._ledger.get(operation_id)
        if row is None or row["principal"] != principal or row["local_project"] != project:
            raise NotFoundError("CORTEX operation is unavailable.")
        return row

    def _reject_prior_outcome(
        self, principal: str, project: str, source_ref: str, operation_id: str
    ) -> None:
        """Do not silently turn a prior outcome for one revision into another."""
        list_owned = getattr(self._ledger, "list_owned", None)
        if not callable(list_owned):
            return
        for row in list_owned(principal, project):
            if (
                row.get("operation_id") != operation_id
                and row.get("operation_kind") == "outcome"
                and row.get("source_ref") == source_ref
            ):
                raise ConflictError(
                    "CORTEX outcome identity is already bound to a different status."
                )

    async def reconcile(self, *, policy, project: str, operation_id: str) -> dict:
        """Confirm a projected upstream object. Search absence never authorizes replay."""
        self._authorize(policy, _WRITE_SCOPE, project)
        operation = await asyncio.to_thread(
            self._owned_operation, policy, project, operation_id, _WRITE_SCOPE
        )
        result = _receipt_from_row(operation, replayed=False)
        if result["retrievable_source_ref"]:
            return result
        cortex_project = operation.get("cortex_project")
        if not isinstance(cortex_project, str):
            raise CortexConnectorError("CORTEX operation receipt is invalid.")
        async with (
            asyncio.timeout(self._config.timeout_seconds),
            _transport_operation(self._transport) as transport,
        ):
            found = await transport.call_tool(
                "cortex_search", {"query": operation_id, "project_id": cortex_project, "limit": 20}
            )
            results = found.get("results") if isinstance(found, Mapping) else None
            if not isinstance(results, list):
                raise CortexConnectorError("CORTEX returned invalid reconciliation results.")
            for candidate in results[:20]:
                object_id = (
                    _safe_id(candidate.get("object_id")) if isinstance(candidate, Mapping) else None
                )
                if object_id is None:
                    continue
                item = await transport.call_tool("cortex_fetch", {"object_id": object_id})
                stored = item.get("stored_object", item) if isinstance(item, Mapping) else None
                if not isinstance(stored, Mapping) or stored.get("project_id") != cortex_project:
                    continue
                field = "content_text" if operation["operation_kind"] == "note" else "summary_text"
                evidence = stored.get(field)
                expected_origin = _origin(
                    operation["principal"], operation["source_ref"], operation_id
                )
                if not isinstance(evidence, str) or not evidence.endswith(expected_origin):
                    continue
                # Text returned by search/fetch is untrusted source data.  A matching
                # origin line is useful to the operator but cannot prove that this
                # connector made the upstream mutation, so it never acknowledges an
                # uncertain write or authorizes an automatic replay.
                return {
                    **result,
                    "reconciliation": "candidate_unverified",
                    "candidate_source_ref": f"cortex://object/{object_id}",
                    "retry_allowed": False,
                }
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
    try:
        validate_endpoint(endpoint)
        port = urlsplit(endpoint).port
    except (InputError, ValueError) as exc:
        raise InputError("CORTEX connection configuration is invalid.") from exc
    if port is not None and not 1 <= port <= 65535:
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
        raise CortexCredentialError("CORTEX connector credentials are unavailable.") from exc
    if not token or len(token) > 16_384 or any(char in token for char in "\r\n\x00"):
        raise CortexCredentialError("CORTEX connector credentials are unavailable.")
    return token


def _validate_project(value: object) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or "\x00" in value:
        raise InputError("CORTEX project is invalid.")


def _validate_source_ref(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 2_000
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
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
        if isinstance(value, str) and "[Dots Brain origin]" in value:
            raise InputError("CORTEX write content must not include connector origin metadata.")
    if not isinstance(payload.get("title"), str) or not payload["title"].strip():
        raise InputError("CORTEX write content is invalid.")
    if kind == "outcome" and payload.get("status") not in {"success", "failure", "partial"}:
        raise InputError("CORTEX write content is invalid.")
    if kind == "decision" and payload.get("doctrine_level") not in {"soft", "hard"}:
        raise InputError("CORTEX write content is invalid.")


def _operation_id(
    principal: str,
    project: str,
    source_ref: str,
    kind: str,
    *,
    outcome_status: object = None,
) -> str:
    identity: list[object] = [principal, project, source_ref, kind]
    if kind == "outcome":
        identity.append(outcome_status)
    raw = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
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
    return _safe_id(receipt.get("object_id"))


def _safe_id(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    normalized = str(value)
    if not re.fullmatch(r"[\w:./@-]{1,300}", normalized):
        return None
    return normalized


def _safe_receipt(receipt: Mapping[str, object]) -> dict:
    """Persist identifiers, never upstream payloads or echoed content."""
    clean = {
        key: value
        for key in ("event_id", "object_id")
        if (value := _safe_id(receipt.get(key))) is not None
    }
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


def _minimize_context(raw: Mapping[str, object], maximum: int):
    """Perform potentially expensive upstream-data minimization off the event loop."""
    from .privacy import sanitize

    minimized = sanitize(raw, max_text=maximum)
    context, bounded = _bounded_json(minimized.value, maximum)
    return minimized, context, bounded


def _bounded_json(value: Mapping[str, object], maximum: int) -> tuple[str, bool]:
    """Return useful valid JSON within the caller bound and report every omission."""
    # Reduce cardinality before reducing per-item text. Object identifiers remain
    # available even in the smallest representation, so a caller can fetch source.
    identifiers = _context_identifiers(value)
    best: tuple[str, bool] | None = None
    for item_limit in (20, 15, 12, 10, 8, 6, 5, 4, 3, 2, 1):
        for text_limit in {max(16, maximum // divisor) for divisor in range(1, 33)}:
            compact, partial = _compact(value, text_limit=text_limit, item_limit=item_limit)
            if isinstance(compact, dict):
                compact = {
                    "untrusted_data": True,
                    "content": compact,
                }
            encoded = json.dumps(compact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if len(encoded) <= maximum and (
                not identifiers or any(identifier in encoded for identifier in identifiers)
            ):
                if best is None or len(encoded) > len(best[0]):
                    best = encoded, partial
    if best is not None:
        return best
    fallback = {
        "untrusted_data": True,
        "project_id": str(value.get("project_id", ""))[:80],
        "object_ids": identifiers,
        "partial": True,
    }
    while identifiers:
        encoded = json.dumps(fallback, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded) <= maximum:
            return encoded, True
        identifiers.pop()
    encoded = json.dumps(
        {"untrusted_data": True, "partial": True},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return encoded, True


def _compact(
    value: object, *, text_limit: int, item_limit: int, depth: int = 0
) -> tuple[object, bool]:
    if depth > 6:
        return "[TRUNCATED_DEPTH]", True
    if isinstance(value, str):
        return (
            (value, False)
            if len(value) <= text_limit
            else (value[: max(1, text_limit - 1)] + "…", True)
        )
    if isinstance(value, list):
        items: list[object] = []
        partial = len(value) > item_limit
        for item in value[:item_limit]:
            compact, item_partial = _compact(
                item, text_limit=text_limit, item_limit=item_limit, depth=depth + 1
            )
            items.append(compact)
            partial = partial or item_partial
        return items, partial
    if isinstance(value, Mapping):
        items: dict[str, object] = {}
        partial = len(value) > item_limit
        for key, item in islice(value.items(), item_limit):
            compact, item_partial = _compact(
                item, text_limit=text_limit, item_limit=item_limit, depth=depth + 1
            )
            items[str(key)[:200]] = compact
            partial = partial or item_partial
        return items, partial
    return value, False


def _context_identifiers(value: object, *, remaining: int = 12) -> list[str]:
    found: list[str] = []

    def visit(current: object) -> None:
        if len(found) >= remaining:
            return
        if isinstance(current, Mapping):
            for key, item in current.items():
                if str(key) in {"id", "object_id", "event_id", "memory_id"}:
                    identifier = _safe_id(item)
                    if identifier is not None and identifier not in found:
                        found.append(identifier)
                visit(item)
                if len(found) >= remaining:
                    return
        elif isinstance(current, list):
            for item in current:
                visit(item)
                if len(found) >= remaining:
                    return

    visit(value)
    return found


async def _prepare_write(transport: CortexTransport) -> None:
    prepare = getattr(transport, "prepare_write", None)
    if not callable(prepare):
        return
    result = prepare()
    if inspect.isawaitable(result):
        await result


@asynccontextmanager
async def _transport_operation(transport: CortexTransport):
    operation = getattr(transport, "operation", None)
    if not callable(operation):
        yield transport
        return
    async with operation() as active:
        yield active


def _error_code(exc: CortexConnectorError) -> str:
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code else "cortex_connector_unavailable"


def _missing_content_guard(_value: Mapping[str, object]) -> None:
    """Reject writes when the caller omitted the required privacy guard."""
    raise CortexConnectorError("CORTEX write privacy validation is not configured.")
