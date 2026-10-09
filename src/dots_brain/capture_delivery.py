"""Deliver minimized file records through an existing, scoped MCP connection."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .bridge import connect
from .capture import JSONLCollector
from .errors import InputError
from .local import locked, read_json, write_json


async def collect_remote(
    *, paths: list[Path], cursor: Path, credential: Path, project: str, account: str, kind: str
) -> dict:
    async with connect(credential) as session:

        async def call(name, arguments):
            result = await session.call_tool(name, arguments)
            if result.isError or not isinstance(result.structuredContent, dict):
                raise InputError(
                    "Capture delivery was not acknowledged; retry using the same cursor."
                )
            return result.structuredContent

        async def deliver(event):
            details = {
                "source": event["source"],
                "source_kind": "provider_snapshot",
                "source_timestamp": event.get("timestamp"),
                "sanitization": event.get("sanitization", {}),
            }
            action = event.get("action")
            if event["kind"] == "conversation":
                saved = await call(
                    "memory_remember",
                    {
                        "content": event["details"]["content"],
                        "source": event["source"],
                        "account": account,
                        "event_id": event["record_id"],
                        "project": project,
                        "title": "Imported user message (source snapshot)",
                    },
                )
                details["memory"] = {key: saved[key] for key in ("id", "revision")}
                action = {"type": "conversation_import"}
            else:
                details["event"] = event.get("details", {})
            await call(
                "audit_record",
                {
                    "project": project,
                    "kind": "gap" if event["kind"] == "gap" else "action",
                    "client_event_id": "capture:" + event["record_id"],
                    "action": action,
                    "target": event.get("target"),
                    "details": details,
                },
            )
            return True

        loop = asyncio.get_running_loop()

        def sink(event):
            return asyncio.run_coroutine_threadsafe(deliver(event), loop).result(timeout=45)

        def collect_pass():
            # Serialize collection and its receipt together. A lost receipt ACK
            # retries the same event ID before another pass can advance the cursor.
            pending = cursor.with_suffix(".coverage.json")
            with locked(cursor.with_suffix(".delivery.lock")):

                def acknowledge_pending():
                    receipt = read_json(pending)
                    if receipt:
                        asyncio.run_coroutine_threadsafe(
                            call("audit_record", receipt), loop
                        ).result(timeout=45)
                        write_json(pending, {})

                try:
                    if pending.exists():
                        acknowledge_pending()
                except Exception:
                    return {
                        "state": "capture_partial",
                        "forwarded": 0,
                        "coverage_receipt": "pending_retry",
                        "coverage": "partial",
                        "live_provider_log": "unverified",
                    }
                source_times = []
                for path in paths:
                    try:
                        source_times.append(
                            datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
                        )
                    except FileNotFoundError:
                        source_times.append(None)
                receipt = {
                    "project": project,
                    "kind": "coverage",
                    "client_event_id": "capture-run:" + str(uuid.uuid4()),
                    "action": {"type": "collector_pass"},
                    "details": {
                        "forwarded": None,
                        "pass_completed": False,
                        "source_last_modified": source_times,
                        "coverage": "partial",
                        "live_provider_log": "unverified",
                        "assistant_text": "excluded_visibility_unknown",
                    },
                }
                # Persist before the first checkpoint. If the process dies while
                # collecting/finalizing, the next run reports this unfinished pass
                # with an unknown count instead of inventing complete coverage.
                write_json(pending, receipt)
                collector = JSONLCollector(paths, cursor, sink, kind=kind)
                count = collector.collect()
                receipt["details"].update(forwarded=count, pass_completed=True)
                write_json(pending, receipt)
                try:
                    acknowledge_pending()
                except Exception:
                    return {
                        "state": "capture_partial",
                        "forwarded": count,
                        "cursor_checkpoint": "acknowledged_records_saved",
                        "coverage_receipt": "pending_retry",
                        "coverage": "partial",
                        "live_provider_log": "unverified",
                    }
                return {
                    "state": "capture_pass_complete",
                    "forwarded": count,
                    "coverage_receipt": "acknowledged",
                    "coverage": "partial",
                    "live_provider_log": "unverified",
                }

        return await asyncio.to_thread(collect_pass)
