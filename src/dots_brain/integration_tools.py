"""Explicitly scoped audit MCP tools.

The memory server installs these capabilities; a scope never appears implicitly
in an existing client's grant.
"""

from __future__ import annotations

from typing import Any

from mcp.types import ToolAnnotations


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
    return scopes
