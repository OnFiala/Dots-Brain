"""Deliver minimized file records through an existing, scoped MCP connection."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .bridge import connect
from .capture import JSONLCollector
from .errors import InputError
from .local import locked, read_json, write_json


async def collect_remote(
    *,
    paths: list[Path],
    cursor: Path,
    credential: Path,
    project: str,
    account: str,
    kind: str,
    recover_pending: bool = False,
) -> dict:
    async with connect(credential) as session:

        def result_error(result) -> str:
            return " ".join(
                block.text
                for block in getattr(result, "content", [])
                if isinstance(getattr(block, "text", None), str)
            ).lower()

        async def call(name, arguments):
            result = await session.call_tool(name, arguments)
            if result.isError or not isinstance(result.structuredContent, dict):
                message = result_error(result)
                raise InputError(
                    "Capture delivery was not acknowledged; retry using the same cursor."
                    if not message
                    else f"Capture delivery rejected: {message[:240]}"
                )
            return result.structuredContent

        def terminal_delivery_reason(error: Exception) -> str | None:
            message = str(error).lower()
            if "suppressed" in message or "forgotten" in message:
                return "delivery_suppressed_source"
            if any(
                token in message
                for token in (
                    "invalid_input",
                    "invalid input",
                    "content rejected",
                    "appears to contain a secret",
                )
            ):
                return "delivery_invalid_input"
            return None

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

        async def deliver_or_record_terminal_gap(event):
            try:
                return await deliver(event)
            except InputError as exc:
                reason = terminal_delivery_reason(exc)
                if reason is None or event.get("kind") != "conversation":
                    raise
                # The source record cannot enter memory, but its omission is a
                # durable, acknowledged coverage gap.  The collector advances
                # only after this audit write succeeds.
                await call(
                    "audit_record",
                    {
                        "project": project,
                        "kind": "gap",
                        "client_event_id": "capture:" + event["record_id"],
                        "action": {"reason": reason},
                        "target": {},
                        "details": {
                            "coverage": "partial",
                            "source_kind": "provider_snapshot",
                            "delivery": "terminal_source_omitted",
                        },
                    },
                )
                return True

        loop = asyncio.get_running_loop()

        def sink(event):
            return asyncio.run_coroutine_threadsafe(
                deliver_or_record_terminal_gap(event), loop
            ).result(timeout=45)

        def collect_pass():
            # Serialize collection and its receipt together. A lost receipt ACK
            # retries the same event ID before another pass can advance the cursor.
            pending = cursor.with_name(cursor.name + ".coverage.json")
            legacy_pending = cursor.with_suffix(".coverage.json")
            lock_path = cursor.with_name(cursor.name + ".delivery.lock")
            with locked(lock_path):

                def pending_is_current(receipt):
                    return (
                        isinstance(receipt, dict)
                        and receipt.get("project") == project
                        and receipt.get("kind") == "coverage"
                        and isinstance(receipt.get("client_event_id"), str)
                        and receipt["client_event_id"].startswith("capture-run:")
                    )

                def archive_pending(path: Path) -> None:
                    os.replace(
                        path,
                        path.with_name(path.name + ".recovered-" + uuid.uuid4().hex),
                    )

                def recovery_required() -> dict:
                    return {
                        "state": "capture_recovery_required",
                        "forwarded": 0,
                        "coverage_receipt": "invalid_pending_receipt",
                        "coverage": "partial",
                        "live_provider_log": "unverified",
                    }

                # Read a legacy sidecar exactly once only when it cannot be the
                # cursor itself; full-name sidecars prevent future collisions.
                if not pending.exists() and legacy_pending != cursor and legacy_pending.exists():
                    try:
                        legacy = read_json(legacy_pending)
                        if pending_is_current(legacy):
                            os.replace(legacy_pending, pending)
                    except InputError:
                        if not recover_pending:
                            return recovery_required()
                        archive_pending(legacy_pending)

                def acknowledge_pending():
                    receipt = read_json(pending)
                    if receipt and pending_is_current(receipt):
                        asyncio.run_coroutine_threadsafe(
                            call("audit_record", receipt), loop
                        ).result(timeout=45)
                        write_json(pending, {})

                recovery = False
                if pending.exists():
                    try:
                        pending_receipt = read_json(pending)
                    except InputError:
                        if not recover_pending:
                            return recovery_required()
                        archive_pending(pending)
                        recovery = True
                    else:
                        if pending_receipt and not pending_is_current(pending_receipt):
                            if not recover_pending:
                                return recovery_required()
                            archive_pending(pending)
                            recovery = True

                try:
                    if pending.exists():
                        acknowledge_pending()
                except InputError as exc:
                    if terminal_delivery_reason(exc) is not None:
                        if not recover_pending:
                            return recovery_required()
                        archive_pending(pending)
                        recovery = True
                    else:
                        return {
                            "state": "capture_partial",
                            "forwarded": 0,
                            "coverage_receipt": "pending_retry",
                            "coverage": "partial",
                            "live_provider_log": "unverified",
                        }
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
                receipt["details"].update(
                    forwarded=count,
                    pass_completed=collector.completed,
                    batch_limit_reached=not collector.completed,
                )
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
                    "state": "capture_pass_complete" if collector.completed else "capture_partial",
                    "forwarded": count,
                    "coverage_receipt": "acknowledged",
                    "coverage": "partial",
                    "live_provider_log": "unverified",
                    **({"recovered_pending_receipt": True} if recovery else {}),
                    **({"batch_limit_reached": True} if not collector.completed else {}),
                }

        return await asyncio.to_thread(collect_pass)
