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
from .errors import InputError
from .protocol import INSTRUCTIONS, protect_calls, safe_http_transport
from .service import MemoryService


@contextlib.asynccontextmanager
async def indexing_lifespan(service: MemoryService):
    async def indexing():
        while True:
            if (service.store.directory / "disabled.json").exists():
                return
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


def http_configuration(store):
    from .oauth import configuration

    try:
        return configuration(store)
    except InputError:
        logging.getLogger("dots_brain").warning(
            "OAuth configuration is invalid; inspect oauth status. OAuth access is closed."
        )
        return None


def create_http_app(
    server: FastMCP, service: MemoryService, *, public_gateway=False, listener_policies=None
):
    from .auth import BearerAuth
    from .oauth import OAuthStore
    from .oauth_http import routes_app

    oauth = OAuthStore(service.store) if http_configuration(service.store) else None
    transport = safe_http_transport(server.streamable_http_app())
    protected = BearerAuth(transport, service.store, oauth=oauth, public_gateway=public_gateway)
    public = BearerAuth(transport, service.store, oauth=oauth, public_gateway=True)
    authorization = routes_app(oauth) if oauth else None

    async def app(scope, receive, send):
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        if scope["type"] == "lifespan":
            async with indexing_lifespan(service):
                await protected(scope, receive, send)
        elif authorization is not None and scope.get("path") != "/mcp":
            await authorization(scope, receive, send)
        else:
            mode = public_gateway
            if listener_policies is not None:
                address = scope.get("server")
                mode = listener_policies.get(address[1]) if address else None
                if mode is None:
                    from starlette.responses import JSONResponse

                    return await JSONResponse({"error": "unknown_listener"}, status_code=503)(
                        scope, receive, send
                    )
            await (public if mode else protected)(scope, receive, send)

    return app


def run_http(server, service, *, port, public_port=None, public_gateway=False):
    """One ASGI lifecycle and indexer; policies come from bound sockets, never Host."""
    import socket

    import uvicorn

    ports = [port] + ([public_port] if public_port is not None else [])
    if any(type(value) is not int or not 1 <= value <= 65535 for value in ports):
        raise InputError("HTTP ports must be between 1 and 65535.")
    if public_port is not None and (public_gateway or public_port == port):
        raise InputError(
            "Use distinct private/public ports; --public-gateway is single-listener only."
        )
    policies = {port: public_gateway}
    if public_port is not None:
        policies[public_port] = True
    service.http_listeners = [
        {"port": number, "policy": "oauth_only" if public else "private"}
        for number, public in policies.items()
    ]
    with contextlib.ExitStack() as stack:
        sockets = [
            stack.enter_context(socket.create_server(("127.0.0.1", value))) for value in ports
        ]
        for listener in sockets:
            listener.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        app = create_http_app(server, service, listener_policies=policies)
        uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning")).run(
            sockets=sockets
        )


def create_server(service: MemoryService, *, http: bool = False, port: int = 8765) -> FastMCP:
    from urllib.parse import urlsplit

    from mcp.server.transport_security import TransportSecuritySettings

    read_limiter = anyio.CapacityLimiter(8)
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

    @contextlib.asynccontextmanager
    async def lifespan(_server):
        if http:
            # Stateless HTTP creates an MCP server lifecycle for each request.
            # Its indexer belongs to the ASGI application's lifecycle instead.
            yield {}
        else:
            async with indexing_lifespan(service):
                yield {}

    config = http_configuration(service.store) if http else None
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
    server = FastMCP(
        "Dots Brain",
        host="127.0.0.1",
        port=port,
        stateless_http=True,
        json_response=True,
        log_level="WARNING",
        max_request_body_size=131072,
        transport_security=transport_security,
        lifespan=lifespan,
        instructions=INSTRUCTIONS,
    )
    protect_calls(server)

    def policy(scope: str | None = None) -> Policy:
        if (service.store.directory / "disabled.json").exists():
            raise InputError("This memory installation has been disabled.")
        if http:
            request = server.get_context().request_context.request
            current = None if request is None else request.scope.get("brain_policy")
            if not isinstance(current, Policy):
                raise InputError("Authenticated request context is required.")
        else:
            current = Policy()
        if scope is not None:
            current.require(scope)
        return current

    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    @server.tool(annotations=read)
    async def memory_context(
        task: Annotated[str, Field(min_length=1, max_length=2000)],
        project: str | None = None,
        max_chars: Annotated[int, Field(ge=256, le=24000)] = 6000,
    ) -> dict[str, Any]:
        """Get bounded relevant source context; the limit is characters, not tokens."""
        return await read_call(
            service.context,
            task,
            policy=policy("memory:read"),
            project=project,
            max_chars=max_chars,
        )

    @server.tool(annotations=read)
    async def memory_search(
        query: Annotated[str, Field(min_length=1, max_length=2000)],
        project: str | None = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> dict[str, Any]:
        """Search accessible memories and return source references and revisions."""
        return await read_call(
            service.search, query, policy=policy("memory:read"), project=project, limit=limit
        )

    @server.tool(annotations=read)
    async def memory_get(memory_id: str, revision: int | None = None) -> dict[str, Any]:
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
        content: Annotated[str, Field(min_length=1, max_length=32000)],
        source: Annotated[str, Field(min_length=1, max_length=200)],
        account: Annotated[str, Field(min_length=1, max_length=200)],
        event_id: Annotated[str, Field(min_length=1, max_length=500)],
        project: Annotated[str, Field(min_length=1, max_length=200)] | None = None,
        title: Annotated[str, Field(max_length=300)] = "",
        source_uri: Annotated[str, Field(min_length=1, max_length=2000)] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Store a source record. Reuse event_id on retries; updates need expected_revision."""
        current = policy("memory:write")
        if project is None:
            if current.projects is not None and len(current.projects) != 1:
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
            source_uri=source_uri,
            expected_revision=expected_revision,
            projects=current.projects,
            writer_principal=current.principal,
        )

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
        )
    )
    async def memory_forget(
        memory_id: str, expected_revision: Annotated[int, Field(strict=True, ge=1)]
    ) -> dict[str, Any]:
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
    required.update({"memory_remember": "memory:write", "memory_forget": "memory:forget"})

    @server._mcp_server.list_tools()
    async def visible_tools():
        scopes = policy().scopes

        def allowed(name):
            needed = required.get(name, "memory:read")
            return all(
                scope in scopes for scope in ((needed,) if isinstance(needed, str) else needed)
            )

        return [tool for tool in await server.list_tools() if allowed(tool.name)]

    return server
