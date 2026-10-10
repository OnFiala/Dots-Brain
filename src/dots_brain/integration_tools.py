"""Explicitly scoped audit and CORTEX MCP tools.

The memory server installs these capabilities; a scope never appears implicitly
in an existing client's grant. CORTEX writes resolve a real source revision first.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from .errors import CapabilityError, InputError
from .protocol import ContextLimit, Identifier, Project, Query, Revision

AuditLimit = Annotated[int, Field(strict=True, ge=1, le=500)]


def register_tools(
    server, service, policy, read_call, write_call
) -> dict[str, str | tuple[str, ...]]:
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    write = ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
    external_read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)

    @server.tool(annotations=write)
    async def audit_record(
        project: Identifier,
        kind: Literal["intent", "receipt", "error", "gap", "correction", "coverage", "action"],
        client_event_id: Identifier,
        action: Any = None,
        target: Any = None,
        details: Any = None,
        occurred_at: Annotated[str, Field(strict=True, max_length=64)] = "",
        intent_event_id: Project = "",
    ) -> dict[str, Any]:
        """Append a sanitized client report. It is not evidence of provider-wide capture."""
        return await write_call(
            service.audit.record,
            policy("audit:write"),
            project=project,
            kind=kind,
            client_event_id=client_event_id,
            action=action,
            target=target,
            details=details,
            occurred_at=occurred_at or None,
            intent_event_id=intent_event_id or None,
        )

    @server.tool(annotations=read)
    async def audit_events(
        project: Project = "",
        since: Annotated[str, Field(strict=True, max_length=64)] = "",
        until: Annotated[str, Field(strict=True, max_length=64)] = "",
        after_id: Revision | None = None,
        limit: AuditLimit = 100,
    ) -> dict[str, Any]:
        """Read sanitized events in ID order; paginate until has_more is false."""
        return await read_call(
            service.audit.page,
            policy("audit:read"),
            project=project or None,
            since=since or None,
            until=until or None,
            after_id=after_id,
            limit=limit,
        )

    @server.tool(annotations=read)
    async def audit_report(
        project: Project = "",
        since: Annotated[str, Field(strict=True, max_length=64)] = "",
        until: Annotated[str, Field(strict=True, max_length=64)] = "",
        limit: AuditLimit = 100,
        after_id: Revision | None = None,
    ) -> dict[str, Any]:
        """Summarize recorded coverage; a truncated window remains partial."""
        return await read_call(
            service.audit.report,
            policy("audit:read"),
            project=project or None,
            since=since or None,
            until=until or None,
            limit=limit,
            after_id=after_id,
        )

    scopes = {
        "audit_record": "audit:write",
        "audit_events": "audit:read",
        "audit_report": "audit:read",
    }
    if service.cortex is None:
        return scopes

    async def record_reconcile_observation(observer, project, operation_id, event_id, details):
        await write_call(
            service.audit.observed,
            policy=observer,
            project=project,
            kind="action",
            client_event_id=event_id,
            action={"tool": "cortex_reconcile"},
            target={"operation_id": operation_id},
            details=details,
        )

    @server.tool(annotations=external_read)
    async def cortex_context(
        project: Identifier, query: Query, max_chars: ContextLimit = 6000
    ) -> dict[str, Any]:
        """Read mapped CORTEX context without importing it into local memories."""
        return await service.cortex.read_context(
            policy=policy("cortex:read"),
            project=project,
            query=query,
            max_chars=max_chars,
        )

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        )
    )
    async def cortex_publish(
        memory_id: Identifier,
        revision: Revision,
        kind: Literal["note", "decision", "outcome"] = "note",
        status: Literal["success", "failure", "partial"] | None = None,
    ) -> dict[str, Any]:
        """Publish this exact accessible revision as a selected note, decision, or outcome.

        This is an explicit cross-system write. Local deletion does not delete the
        CORTEX copy. Unknown outcomes must be reconciled, never blindly retried.
        """
        current = policy("cortex:write")
        current.require("memory:read")
        record = await read_call(
            service.store.get, memory_id, revision=revision, projects=current.projects
        )
        source_ref = f"dots://memory/{record['id']}@{record['revision']}"
        arguments = dict(
            policy=current,
            project=record["project"],
            source_ref=source_ref,
            title=record["title"] or "Selected bot memory",
        )
        if kind == "outcome" and status is None:
            raise InputError("An outcome requires success, failure or partial status.")
        if kind != "outcome" and status is not None:
            raise InputError("Status is valid only for an outcome.")
        observer = replace(current, scopes=current.scopes | {"audit:write"})
        event_id = uuid.uuid4().hex
        audit = dict(policy=observer, project=record["project"])
        await write_call(
            service.audit.observed,
            **audit,
            kind="intent",
            client_event_id=event_id,
            action={"tool": "cortex_publish", "kind": kind},
            target={"source_ref": source_ref},
        )
        try:
            if kind == "note":
                result = await service.cortex.write_note(**arguments, content=record["content"])
            elif kind == "decision":
                result = await service.cortex.write_decision(**arguments, summary=record["content"])
            else:
                result = await service.cortex.write_outcome(
                    **arguments, summary=record["content"], status=status
                )
        except Exception as exc:
            try:
                await write_call(
                    service.audit.observed,
                    **audit,
                    kind="receipt",
                    client_event_id=event_id + ":receipt",
                    intent_event_id=event_id,
                    details={
                        "status": "failed",
                        "error_code": getattr(exc, "code", "operation_failed"),
                        "operation_id": getattr(exc, "operation_id", None),
                    },
                )
            except Exception:
                logging.getLogger("dots_brain").error(
                    "CORTEX publish failed and its audit receipt is pending"
                )
            raise
        try:
            await write_call(
                service.audit.observed,
                **audit,
                kind="receipt",
                client_event_id=event_id + ":receipt",
                intent_event_id=event_id,
                details={
                    "status": "completed",
                    "operation_id": result.get("operation_id"),
                    "state": result.get("state"),
                },
            )
        except Exception:
            logging.getLogger("dots_brain").error(
                "CORTEX publish completed but its audit receipt is pending"
            )
            return {**result, "audit_receipt": "pending"}
        return result

    @server.tool(annotations=read)
    async def cortex_operation(project: Identifier, operation_id: Identifier) -> dict[str, Any]:
        """Inspect the authenticated caller's durable outbound receipt."""
        return await read_call(
            service.cortex.operation_status,
            policy=policy("cortex:read"),
            project=project,
            operation_id=operation_id,
        )

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
        )
    )
    async def cortex_reconcile(project: Identifier, operation_id: Identifier) -> dict[str, Any]:
        """Confirm an upstream object; an empty search never permits duplicate writes."""
        current = policy("cortex:write")
        observer = replace(current, scopes=current.scopes | {"audit:write"})
        event_id = uuid.uuid4().hex
        try:
            result = await service.cortex.reconcile(
                policy=current, project=project, operation_id=operation_id
            )
            details = {
                "status": "completed",
                "state": result.get("state"),
                "reconciliation": result.get("reconciliation"),
                "retry_allowed": bool(result.get("retry_allowed")),
            }
        except Exception as exc:
            details = {
                "status": "failed",
                "error_code": getattr(exc, "code", "operation_failed"),
            }
            try:
                await record_reconcile_observation(
                    observer, project, operation_id, event_id, details
                )
            except Exception:
                logging.getLogger("dots_brain").error(
                    "CORTEX reconciliation failed and its audit event is pending"
                )
            raise
        try:
            await record_reconcile_observation(observer, project, operation_id, event_id, details)
        except Exception:
            logging.getLogger("dots_brain").error(
                "CORTEX reconciliation completed but its audit event is pending"
            )
            return {**result, "audit_receipt": "pending"}
        return result

    @server.tool(annotations=read)
    async def cortex_operations(project: Identifier) -> dict[str, Any]:
        """List the caller's recorded outbound operations for one accessible project."""
        operations = await read_call(
            service.cortex.operations, policy=policy("cortex:read"), project=project
        )
        return {"operations": operations}

    scopes.update(
        {
            "cortex_context": "cortex:read",
            "cortex_publish": ("cortex:write", "memory:read"),
            "cortex_operation": "cortex:read",
            "cortex_reconcile": "cortex:write",
            "cortex_operations": "cortex:read",
        }
    )
    return scopes


def load_cortex(store):
    """Load private connection references; this does not contact CORTEX or read its token."""

    from .cortex_connector import CortexConnectionConfig, CortexConnector, SqliteCortexLedger
    from .local import read_json
    from .privacy import guard_content

    path = store.directory / "cortex.json"
    if not path.exists():
        return None
    try:
        data = read_json(path)
        mapping = tuple(data["project_mapping"].items())
        config = CortexConnectionConfig(
            endpoint=data["endpoint"], token_file=Path(data["token_file"]), project_mapping=mapping
        )
    except (OSError, KeyError, AttributeError, TypeError, ValueError) as exc:
        raise CapabilityError("CORTEX connection configuration is invalid.") from exc
    return CortexConnector(config, ledger=SqliteCortexLedger(store), content_guard=guard_content)
