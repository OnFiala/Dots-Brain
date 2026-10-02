"""A local stdio client for the same authenticated remote memory service."""

from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .auth import read_connection
from .errors import InputError


@asynccontextmanager
async def connect(path: Path):
    connection = read_connection(path)
    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer " + connection["token"]},
        timeout=30,
        follow_redirects=False,
    ) as http:
        async with streamable_http_client(connection["url"], http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def verify_connection(
    path: Path, *, write: bool = False, project: str = "default", cleanup_store=None
) -> dict:
    async with connect(path) as session:
        return await verify_session(
            session, write=write, project=project, cleanup_store=cleanup_store
        )


async def verify_command(entry: dict, **checks) -> dict:
    async with stdio_client(
        StdioServerParameters(command=entry["command"], args=entry["args"])
    ) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await verify_session(session, **checks)


async def verify_session(session, *, write=False, project="default", cleanup_store=None) -> dict:
    tools = await session.list_tools()
    names = [tool.name for tool in tools.tools]
    result = await session.call_tool("memory_status", {})
    verified_write = False
    if write and not result.isError:
        if "memory_forget" not in names and cleanup_store is None:
            raise InputError("Write verification needs a way to remove its synthetic probe.")
        event_id = "connection-probe-" + str(uuid4())
        saved = await session.call_tool(
            "memory_remember",
            {
                "content": "Synthetic Dots Brain connection probe.",
                "source": "dots-brain-probe",
                "account": "installation",
                "event_id": event_id,
                "project": project,
            },
        )
        if saved.isError or not saved.structuredContent:
            raise InputError("The client could not write its connection probe.")
        memory_id = saved.structuredContent["id"]
        try:
            check = await session.call_tool("memory_get", {"memory_id": memory_id})
            verified_write = bool(
                not check.isError
                and check.structuredContent
                and check.structuredContent.get("event_id") == event_id
            )
        finally:
            if cleanup_store is not None:
                record = cleanup_store.get(memory_id)
                if record["event_id"] != event_id or record["source"] != "dots-brain-probe":
                    raise InputError("Probe identity mismatch; no memory was removed.")
                cleanup_store.forget(memory_id)
            else:
                removed = await session.call_tool("memory_forget", {"memory_id": memory_id})
                if removed.isError:
                    raise InputError("The synthetic connection probe could not be removed.")
    return {
        "state": "verification_failed"
        if result.isError or (write and not verified_write)
        else "verified_read_write"
        if write
        else "verified_read",
        "tools": names,
        "read": not result.isError,
        "write": verified_write if write else "not_tested",
        "probe_removed": True if write and verified_write else "not_applicable",
        "capture": "not_implemented",
    }


async def run_bridge(path: Path) -> None:
    server = Server("Dots Brain bridge")

    @server.list_tools()
    async def list_tools():
        async with connect(path) as session:
            return (await session.list_tools()).tools

    @server.call_tool()
    async def call_tool(name, arguments):
        async with connect(path) as session:
            return await session.call_tool(name, arguments)

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
