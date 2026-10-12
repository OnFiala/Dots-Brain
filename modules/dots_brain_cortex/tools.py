"""Preserved CORTEX tool registration; the core never loads this module."""

from typing import Any

from mcp.types import ToolAnnotations

from dots_brain.errors import InputError


def register_tools(server, service, policy, read_call, write_call):
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)
    scopes = {}
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
