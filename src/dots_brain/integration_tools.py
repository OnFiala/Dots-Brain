"""Explicitly scoped audit and CORTEX MCP tools.

The memory server installs these capabilities; a scope never appears implicitly
in an existing client's grant. CORTEX writes resolve a real source revision first.
"""

from __future__ import annotations

from typing import Any

from mcp.types import ToolAnnotations

from .errors import CapabilityError, InputError


def register_tools(
    server, service, policy, read_call, write_call
) -> dict[str, str | tuple[str, ...]]:
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)

    @server.tool(annotations=write)
    async def audit_record(
        project: str,
        kind: str,
        client_event_id: str,
        action: Any = None,
        target: Any = None,
        details: Any = None,
        occurred_at: str | None = None,
        intent_event_id: str | None = None,
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
            occurred_at=occurred_at,
            intent_event_id=intent_event_id,
        )

    @server.tool(annotations=read)
    async def audit_events(
        project: str | None = None,
        since: str | None = None,
        until: str | None = None,
        after_id: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Read sanitized events in ID order; paginate until has_more is false."""
        current = policy("audit:read")
        rows = await read_call(
            service.audit.events,
            current,
            project=project,
            since=since,
            until=until,
            after_id=after_id,
            limit=limit,
        )
        more = bool(
            rows
            and await read_call(
                service.audit.events,
                current,
                project=project,
                since=since,
                until=until,
                after_id=rows[-1]["id"],
                limit=1,
            )
        )
        return {
            "events": rows,
            "has_more": more,
            "next_after_id": rows[-1]["id"] if rows else after_id,
        }

    @server.tool(annotations=read)
    async def audit_report(
        project: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Summarize recorded coverage; a truncated window remains partial."""
        return await read_call(
            service.audit.report,
            policy("audit:read"),
            project=project,
            since=since,
            until=until,
            limit=limit,
        )

    scopes = {
        "audit_record": "audit:write",
        "audit_events": "audit:read",
        "audit_report": "audit:read",
    }
    if service.cortex is None:
        return scopes

    @server.tool(annotations=read)
    async def cortex_context(project: str, query: str, max_chars: int = 6000) -> dict[str, Any]:
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
        memory_id: str,
        revision: int,
        kind: str = "note",
        status: str | None = None,
    ) -> dict[str, Any]:
        """Publish this exact accessible revision as a selected note, decision, or outcome.

        This is an explicit cross-system write. Local deletion does not delete the
        CORTEX copy. Unknown outcomes must be reconciled, never blindly retried.
        """
        current = policy("cortex:write")
        current.require("memory:read")
        if type(revision) is not int or revision < 1:
            raise InputError("revision must be a positive integer.")
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
        if kind == "note":
            return await service.cortex.write_note(**arguments, content=record["content"])
        if kind == "decision":
            return await service.cortex.write_decision(**arguments, summary=record["content"])
        if kind == "outcome" and status in {"success", "failure", "partial"}:
            return await service.cortex.write_outcome(
                **arguments, summary=record["content"], status=status
            )
        raise InputError("Use note, decision, or outcome with success/failure/partial status.")

    @server.tool(annotations=read)
    async def cortex_operation(project: str, operation_id: str) -> dict[str, Any]:
        """Inspect the authenticated caller's durable outbound receipt."""
        return await read_call(
            service.cortex.operation_status,
            policy=policy("cortex:read"),
            project=project,
            operation_id=operation_id,
        )

    @server.tool(annotations=write)
    async def cortex_reconcile(project: str, operation_id: str) -> dict[str, Any]:
        """Confirm an upstream object; an empty search never permits duplicate writes."""
        return await service.cortex.reconcile(
            policy=policy("cortex:write"), project=project, operation_id=operation_id
        )

    scopes.update(
        {
            "cortex_context": "cortex:read",
            "cortex_publish": ("cortex:write", "memory:read"),
            "cortex_operation": "cortex:read",
            "cortex_reconcile": "cortex:write",
        }
    )
    return scopes


def load_cortex(store):
    """Load private connection references; this does not contact CORTEX or read its token."""
    from pathlib import Path

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
