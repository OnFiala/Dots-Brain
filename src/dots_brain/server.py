"""The official MCP SDK owns protocol parsing and transport behavior."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from functools import partial
from typing import Annotated, Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .auth import Policy
from .errors import ForbiddenError, StoreDisabledError
from .protocol import (
    BrainMCP,
    ContextLimit,
    Identifier,
    Label,
    Project,
    Query,
    Revision,
    SearchLimit,
)
from .service import MemoryService


@contextlib.asynccontextmanager
async def indexing_lifespan(service: MemoryService):
    async def indexing():
        while True:
            if (service.store.directory / "disabled.json").exists():
                await asyncio.sleep(2)
                continue
            try:
                result = await asyncio.to_thread(service.semantic.index, batch_size=4)
            except Exception:
                logging.getLogger("dots_brain").warning("Local indexing failed; retrying.")
                await asyncio.sleep(2)
            else:
                # Drain a backlog in bounded batches; only idle polling needs a long sleep.
                await asyncio.sleep(0.05 if result["examined"] else 2)

    worker = asyncio.create_task(indexing()) if service.semantic is not None else None
    try:
        yield
    finally:
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker


def create_http_app(server: FastMCP, service: MemoryService, *, public_gateway: bool = False):
    from .auth import BearerAuth
    from .oauth import OAuthStore, configuration
    from .oauth_http import routes_app

    oauth = OAuthStore(service.store) if configuration(service.store) else None
    protected = BearerAuth(
        server.streamable_http_app(), service.store, oauth=oauth, public_gateway=public_gateway
    )
    authorization = routes_app(oauth) if oauth else None

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            async with indexing_lifespan(service):
                await protected(scope, receive, send)
        elif authorization is not None and scope.get("path") != "/mcp":
            await authorization(scope, receive, send)
        else:
            await protected(scope, receive, send)

    return app


def create_server(service: MemoryService, *, http: bool = False, port: int = 8765) -> FastMCP:
    from urllib.parse import urlsplit

    from mcp.server.transport_security import TransportSecuritySettings

    from .oauth import configuration

    read_limiter = anyio.CapacityLimiter(8)
    search_limiter = anyio.CapacityLimiter(2)
    write_limiter = anyio.CapacityLimiter(1)

    async def read_call(function, *args, **kwargs):
        return await anyio.to_thread.run_sync(
            partial(function, *args, **kwargs), limiter=read_limiter
        )

    async def write_call(function, *args, **kwargs):
        # One in-process SQLite writer; blocked writers never consume read workers.
        return await anyio.to_thread.run_sync(
            partial(function, *args, **kwargs), limiter=write_limiter
        )

    async def search_call(function, *args, **kwargs):
        return await anyio.to_thread.run_sync(
            partial(function, *args, **kwargs), limiter=search_limiter
        )

    @contextlib.asynccontextmanager
    async def lifespan(_server):
        if http:
            # Stateless HTTP creates an MCP server lifecycle for each request.
            # Its indexer belongs to the ASGI application's lifecycle instead.
            yield {}
        else:
            async with indexing_lifespan(service):
                yield {}

    config = configuration(service.store) if http else None
    transport_security = None
    if config:
        transport_security = TransportSecuritySettings(
            allowed_hosts=[
                "127.0.0.1:*",
                "localhost:*",
                "[::1]:*",
                urlsplit(config["issuer"]).netloc,
            ],
            allowed_origins=[
                "http://127.0.0.1:*",
                "http://localhost:*",
                "http://[::1]:*",
                config["issuer"],
            ],
        )
    server = BrainMCP(
        "Dots Brain",
        host="127.0.0.1",
        port=port,
        stateless_http=True,
        json_response=True,
        log_level="WARNING",
        max_request_body_size=1048576,
        transport_security=transport_security,
        lifespan=lifespan,
    )

    def policy(scope: str | None = None) -> Policy:
        if (service.store.directory / "disabled.json").exists():
            raise StoreDisabledError("This memory installation has been disabled.")
        if http:
            request = server.get_context().request_context.request
            current = None if request is None else request.scope.get("brain_policy")
            if not isinstance(current, Policy):
                raise ForbiddenError("Authenticated request context is required.")
        else:
            current = Policy()
        if scope is not None:
            current.require(scope)
        return current

    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    @server.tool(annotations=read)
    async def memory_context(
        task: Query, project: Project = "", max_chars: ContextLimit = 6000
    ) -> dict[str, Any]:
        """Get bounded relevant source context; the limit is characters, not tokens."""
        return await search_call(
            service.context,
            task,
            policy=policy("memory:read"),
            project=project or None,
            max_chars=max_chars,
        )

    @server.tool(annotations=read)
    async def memory_search(
        query: Query, project: Project = "", limit: SearchLimit = 10
    ) -> dict[str, Any]:
        """Search accessible memories and return source references and revisions."""
        return await search_call(
            service.search,
            query,
            policy=policy("memory:read"),
            project=project or None,
            limit=limit,
        )

    @server.tool(annotations=read)
    async def memory_get(memory_id: Identifier, revision: Revision | None = None) -> dict[str, Any]:
        """Read a current or historical memory revision under the client's project scope."""
        return await read_call(
            service.store.get, memory_id, revision=revision, projects=policy("memory:read").projects
        )

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        )
    )
    async def memory_remember(
        content: Annotated[str, Field(strict=True, min_length=1, max_length=32000)],
        source: Label,
        account: Label,
        event_id: Identifier,
        project: Project = "",
        title: Annotated[str, Field(strict=True, max_length=300)] = "",
        source_uri: Annotated[str, Field(strict=True, max_length=2000)] = "",
        expected_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Store a source record. Reuse event_id on retries; updates need expected_revision."""
        current = policy("memory:write")
        if not project:
            if current.projects is not None and len(current.projects) != 1:
                from .errors import InputError

                raise InputError("Select one project for this write.")
            project = current.projects[0] if current.projects else "default"
        return await write_call(
            service.mutate,
            service.store.remember,
            policy=current,
            audit_project=project,
            action="memory_remember",
            content=content,
            source=source,
            account=account,
            event_id=event_id,
            project=project,
            title=title,
            source_uri=source_uri or None,
            expected_revision=expected_revision,
            projects=current.projects,
            writer_principal=current.principal,
        )

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
        )
    )
    async def memory_forget(memory_id: Identifier, expected_revision: Revision) -> dict[str, Any]:
        """Delete only the observed revision; suppress reimport. Requires explicit user intent.

        Read with memory_get first. On conflict, review the new content and user intent;
        never retry deletion automatically with a newer revision.
        """
        current = policy("memory:forget")
        from .errors import NotFoundError

        try:
            record = await read_call(service.store.get, memory_id, projects=current.projects)
        except NotFoundError:
            return {"deleted": False}
        return await write_call(
            service.mutate,
            service.store.forget,
            policy=current,
            audit_project=record["project"],
            action="memory_forget",
            memory_id=memory_id,
            expected_revision=expected_revision,
            projects=current.projects,
            writer_principal=current.principal,
        )

    @server.tool(annotations=read)
    async def memory_status() -> dict[str, Any]:
        """Report accessible source counts and implemented capabilities without secrets."""
        return await read_call(service.status, policy=policy("memory:read"))

    from .integration_tools import register_tools

    required = register_tools(server, service, policy, read_call, write_call)
    required.update(
        {
            "memory_context": "memory:read",
            "memory_search": "memory:read",
            "memory_get": "memory:read",
            "memory_status": "memory:read",
            "memory_remember": "memory:write",
            "memory_forget": "memory:forget",
        }
    )
    server.required_scopes = {
        name: (scopes,) if isinstance(scopes, str) else scopes for name, scopes in required.items()
    }
    server.current_policy = policy

    return server
