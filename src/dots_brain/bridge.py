"""A local stdio client for the same authenticated remote memory service."""

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .auth import read_connection


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


async def verify_connection(path: Path) -> dict:
    async with connect(path) as session:
        tools = await session.list_tools()
        result = await session.call_tool("memory_status", {})
        return {
            "state": "verified_read" if not result.isError else "verification_failed",
            "tools": [tool.name for tool in tools.tools],
            "read": not result.isError,
            "write": "not_tested",
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
