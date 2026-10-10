"""A local stdio client for the same authenticated remote memory service."""

import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from . import __version__
from .auth import authenticate, read_connection
from .errors import BrainError, BusyError, InputError
from .local import read_json, write_json
from .protocol import INSTRUCTIONS, error_result, protect_tool_dispatch, safe_error

PROBE_SOURCE = "dots-brain-probe"
PROBE_ACCOUNT = "connection-verifier"


def _probe_receipt_path(path: Path) -> Path:
    """Keep a recoverable probe receipt beside its private credential."""
    return path.with_name(path.name + ".probe-recovery.json")


def _probe_identity(path: Path) -> tuple[str, Path]:
    """Reuse an unfinished probe; a completed verification gets a fresh identity."""
    receipt = _probe_receipt_path(path)
    if receipt.exists():
        event_id = read_json(receipt).get("event_id", "")
        if not isinstance(event_id, str) or not re.fullmatch(
            r"connection-probe-[a-f0-9]{32,64}", event_id
        ):
            raise InputError("The unfinished probe receipt is invalid; preserve it for review.")
        return event_id, receipt
    return "connection-probe-" + uuid4().hex, receipt


def _entry_credential_path(entry: dict) -> Path | None:
    args = entry.get("args")
    if not isinstance(args, list):
        return None
    try:
        index = args.index("--credential-file")
        value = args[index + 1]
    except (ValueError, IndexError):
        return None
    return Path(value) if isinstance(value, str) else None


def _write_probe_receipt(path: Path | None, receipt: dict) -> None:
    if path is not None:
        write_json(path, receipt)


def _clear_probe_receipt(path: Path | None) -> None:
    if path is not None:
        path.unlink(missing_ok=True)


def _pending_probe(path: Path | None, *, event_id: str, project: str) -> None:
    if path is None or not path.exists():
        return
    receipt = read_json(path)
    if (
        receipt.get("version") != 1
        or receipt.get("event_id") != event_id
        or receipt.get("project") != project
        or receipt.get("source") != PROBE_SOURCE
        or receipt.get("account") != PROBE_ACCOUNT
    ):
        raise InputError("A different unresolved connection probe exists; it was preserved.")


def resume_local_connection(path: Path, directory: Path) -> None:
    """Resume only an existing service matched by its local credential and endpoint."""
    from .local import read_json
    from .runtime import ensure_enabled, state_path, up
    from .store import Store

    store = Store(directory)
    ensure_enabled(store)
    store.status()  # Validate the existing store without initializing a new one.
    connection = read_connection(path)
    if authenticate(store, connection["token"]) is None:
        raise InputError("The connection credential is not valid for this local store.")
    if not state_path(store).is_file():
        raise InputError("No managed service state exists; configure the local service explicitly.")
    state = read_json(state_path(store))
    port = state.get("port")
    if (
        type(port) is not int
        or not 1 <= port <= 65535
        or state.get("url") != f"http://127.0.0.1:{port}/mcp"
        or connection["url"] != state["url"]
    ):
        raise InputError("The connection endpoint does not match the existing local service.")
    up(store)


@asynccontextmanager
async def connect(path: Path):
    connection = read_connection(path)
    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer " + connection["token"]},
        timeout=30,
        follow_redirects=False,
        trust_env=False,
    ) as http:
        async with streamable_http_client(connection["url"], http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def verify_connection(
    path: Path, *, write: bool = False, project: str = "default", cleanup_store=None
) -> dict:
    event_id, receipt_path = _probe_identity(path)
    async with connect(path) as session:
        return await verify_session(
            session,
            write=write,
            project=project,
            cleanup_store=cleanup_store,
            probe_event_id=event_id,
            probe_receipt_path=receipt_path,
            fresh_session=lambda: connect(path),
        )


@asynccontextmanager
async def _stdio_session(entry: dict):
    async with stdio_client(
        StdioServerParameters(command=entry["command"], args=entry["args"])
    ) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def verify_command(entry: dict, **checks) -> dict:
    credential_path = _entry_credential_path(entry)
    if credential_path is not None:
        event_id, receipt_path = _probe_identity(credential_path)
        checks.setdefault("probe_event_id", event_id)
        checks.setdefault("probe_receipt_path", receipt_path)
    checks.setdefault("fresh_session", lambda: _stdio_session(entry))
    async with _stdio_session(entry) as session:
        return await verify_session(session, **checks)


def _matches_probe(record, *, memory_id, revision, event_id, project) -> bool:
    expected = {
        "id": memory_id,
        "revision": revision,
        "event_id": event_id,
        "project": project,
        "source": PROBE_SOURCE,
        "account": PROBE_ACCOUNT,
    }
    return isinstance(record, dict) and all(
        record.get(key) == value for key, value in expected.items()
    )


async def _remove_probe(session, *, memory_id, revision, event_id, project, cleanup_store) -> bool:
    if cleanup_store is not None:
        record = cleanup_store.get(memory_id)
    else:
        checked = await session.call_tool("memory_get", {"memory_id": memory_id})
        record = None if checked.isError else checked.structuredContent
    if not _matches_probe(
        record, memory_id=memory_id, revision=revision, event_id=event_id, project=project
    ):
        raise InputError("Probe identity mismatch; no memory was removed.")
    if cleanup_store is not None:
        cleanup_store.forget(memory_id, expected_revision=revision)
        return True
    removed = await session.call_tool(
        "memory_forget", {"memory_id": memory_id, "expected_revision": revision}
    )
    return bool(
        not removed.isError
        and removed.structuredContent
        and removed.structuredContent.get("deleted") is True
    )


async def _remove_probe_on_fresh_session(
    fresh_session, *, memory_id, revision, event_id, project, cleanup_store
) -> bool:
    try:
        if cleanup_store is not None:
            return await _remove_probe(
                None,
                memory_id=memory_id,
                revision=revision,
                event_id=event_id,
                project=project,
                cleanup_store=cleanup_store,
            )
        if fresh_session is None:
            return False
        async with fresh_session() as session:
            return await _remove_probe(
                session,
                memory_id=memory_id,
                revision=revision,
                event_id=event_id,
                project=project,
                cleanup_store=None,
            )
    except Exception:
        return False


def _verification_result(*, state, names, read, write, probe_removed):
    return {
        "state": state,
        "tools": names,
        "read": read,
        "write": write,
        "probe_removed": probe_removed,
        "capture": "not_verified_by_connection_check",
    }


async def verify_session(
    session,
    *,
    write=False,
    project="default",
    cleanup_store=None,
    probe_event_id=None,
    probe_receipt_path=None,
    fresh_session=None,
) -> dict:
    tools = await session.list_tools()
    names = [tool.name for tool in tools.tools]
    check = "memory_status" if "memory_status" in names else "audit_report"
    if check not in names:
        if write or not names:
            raise InputError("This client has no supported read tool for verification.")
        return _verification_result(
            state="verified_connection",
            names=names,
            read="not_available",
            write="not_tested",
            probe_removed="not_applicable",
        )
    result = await session.call_tool(check, {})
    if result.isError:
        return _verification_result(
            state="verification_failed",
            names=names,
            read=False,
            write="not_tested" if not write else False,
            probe_removed="not_applicable" if not write else False,
        )
    if not write:
        return _verification_result(
            state="verified_read",
            names=names,
            read=True,
            write="not_tested",
            probe_removed="not_applicable",
        )
    if "memory_forget" not in names and cleanup_store is None:
        raise InputError("Write verification needs a way to remove its synthetic probe.")
    if "memory_remember" not in names or "memory_get" not in names:
        raise InputError("This client cannot write and read a connection probe.")
    event_id = probe_event_id or "connection-probe-" + str(uuid4())
    _pending_probe(probe_receipt_path, event_id=event_id, project=project)
    receipt = {
        "version": 1,
        "event_id": event_id,
        "project": project,
        "source": PROBE_SOURCE,
        "account": PROBE_ACCOUNT,
        "state": "sending",
    }
    _write_probe_receipt(probe_receipt_path, receipt)
    try:
        saved = await session.call_tool(
            "memory_remember",
            {
                "content": "Synthetic Dots Brain connection probe.",
                "source": PROBE_SOURCE,
                "account": PROBE_ACCOUNT,
                "event_id": event_id,
                "project": project,
            },
        )
    except Exception:
        # The write may have reached the service. Keep the receipt and let an
        # explicit later --write rerun use the same idempotency identity.
        return _verification_result(
            state="verification_failed",
            names=names,
            read=True,
            write="unknown",
            probe_removed="unknown",
        )
    if saved.isError or not saved.structuredContent:
        # A server error can occur after a commit; retain the idempotency receipt.
        return _verification_result(
            state="verification_failed",
            names=names,
            read=True,
            write="unknown",
            probe_removed="unknown",
        )
    try:
        memory_id = saved.structuredContent["id"]
        revision = saved.structuredContent["revision"]
    except (KeyError, TypeError):
        return _verification_result(
            state="verification_failed",
            names=names,
            read=True,
            write="unknown",
            probe_removed="unknown",
        )
    receipt.update({"state": "written", "memory_id": memory_id, "revision": revision})
    _write_probe_receipt(probe_receipt_path, receipt)
    try:
        checked = await session.call_tool("memory_get", {"memory_id": memory_id})
        verified_write = bool(
            not checked.isError
            and _matches_probe(
                checked.structuredContent,
                memory_id=memory_id,
                revision=revision,
                event_id=event_id,
                project=project,
            )
        )
    except Exception:
        verified_write = False
    if verified_write:
        try:
            removed = await _remove_probe(
                session,
                memory_id=memory_id,
                revision=revision,
                event_id=event_id,
                project=project,
                cleanup_store=cleanup_store,
            )
        except Exception:
            removed = False
    else:
        # Never trust a broken session to clean up a write it may have accepted.
        removed = await _remove_probe_on_fresh_session(
            fresh_session,
            memory_id=memory_id,
            revision=revision,
            event_id=event_id,
            project=project,
            cleanup_store=cleanup_store,
        )
    if removed:
        _clear_probe_receipt(probe_receipt_path)
    return _verification_result(
        state="verified_read_write" if verified_write and removed else "verification_failed",
        names=names,
        read=True,
        write=verified_write,
        probe_removed=removed if removed else "pending_recovery",
    )


class BridgeFailure(BrainError):
    def __init__(self, error):
        super().__init__(error["message"])
        self.code = error["code"]


class BridgeSession:
    """One owner for the HTTP context, with bounded concurrent tool calls.

    Every request has one deadline including queue time. A failed transport ends
    the session and fails its outstanding requests; no operation is replayed.
    """

    MAX_PENDING = 128
    MAX_CONCURRENT = 8
    REQUEST_TIMEOUT = 30

    def __init__(self, path):
        self.path = path
        self.queue = asyncio.Queue(maxsize=self.MAX_PENDING)
        self.pending = set()
        self.closed = False
        self.owner = None

    async def request(self, name, arguments=None):
        if self.closed:
            raise BridgeFailure(
                {"code": "service_unavailable", "message": "The local bridge is closed."}
            )
        if len(self.pending) >= self.MAX_PENDING:
            raise BusyError("The local bridge capacity is full; retry later.")
        result = asyncio.get_running_loop().create_future()
        self.pending.add(result)
        try:
            self.queue.put_nowait((name, arguments, result))
        except asyncio.QueueFull:
            self.pending.discard(result)
            result.cancel()
            raise BusyError("The local bridge capacity is full; retry later.") from None
        try:
            return await asyncio.wait_for(result, timeout=self.REQUEST_TIMEOUT)
        except TimeoutError as exc:
            raise BridgeFailure(safe_error(exc)) from None
        finally:
            self.pending.discard(result)

    def _settle_pending(self, error=None):
        while not self.queue.empty():
            self.queue.get_nowait()
        for result in tuple(self.pending):
            if not result.done():
                if error is None:
                    result.cancel()
                else:
                    result.set_exception(BridgeFailure(error))

    async def close(self):
        self.closed = True
        self._settle_pending()
        if self.owner is not None and not self.owner.done():
            self.owner.cancel()

    async def _call(self, session, slots, request):
        name, arguments, result = request
        task = asyncio.current_task()

        def cancel_abandoned(future):
            if future.cancelled():
                task.cancel()

        result.add_done_callback(cancel_abandoned)
        try:
            async with slots:
                if result.done():
                    return
                value = (
                    await session.list_tools()
                    if name is None
                    else await session.call_tool(name, arguments)
                )
                if not result.done():
                    result.set_result(value)
        finally:
            result.remove_done_callback(cancel_abandoned)

    async def run(self):
        self.owner = asyncio.current_task()
        slots = asyncio.Semaphore(self.MAX_CONCURRENT)
        try:
            while not self.closed:
                request = await self.queue.get()
                if request[2].done():
                    continue
                try:
                    async with connect(self.path) as session, asyncio.TaskGroup() as calls:
                        while not self.closed:
                            if not request[2].done():
                                calls.create_task(self._call(session, slots, request))
                            request = await self.queue.get()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._settle_pending(safe_error(exc))
        except asyncio.CancelledError:
            if not self.closed:
                raise
        finally:
            self.closed = True
            self._settle_pending()


async def run_bridge(path: Path) -> None:
    server = Server("Dots Brain bridge", version=__version__, instructions=INSTRUCTIONS)
    remote = BridgeSession(path)

    @server.list_tools()
    async def list_tools():
        return (await remote.request(None)).tools

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        try:
            return await remote.request(name, arguments)
        except Exception as exc:
            return error_result(exc)

    protect_tool_dispatch(server)
    worker = asyncio.create_task(remote.run())
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await remote.close()
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
