"""Deliver minimized file records through an existing, scoped MCP connection."""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any

from .bridge import connect
from .capture import JSONLCollector
from .errors import InputError
from .local import locked, read_json, write_json


def _result_error(result: Any) -> str:
    return " ".join(
        block.text
        for block in getattr(result, "content", [])
        if isinstance(getattr(block, "text", None), str)
    ).lower()


def _result_code(result: Any) -> str | None:
    structured = getattr(result, "structuredContent", None)
    error = structured.get("error") if isinstance(structured, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def _terminal_delivery_reason(error: Exception) -> str | None:
    code = getattr(error, "delivery_error_code", None)
    if code in {"source_suppressed", "source_forgotten"}:
        return "delivery_suppressed_source"
    if code == "invalid_input":
        return "delivery_invalid_input"
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


@dataclass(frozen=True)
class _RemoteDelivery:
    """Connection and scope used by both record delivery and receipt ACKs."""

    session: Any
    loop: asyncio.AbstractEventLoop
    project: str
    account: str

    async def call(self, name: str, arguments: dict) -> dict:
        result = await self.session.call_tool(name, arguments)
        if result.isError or not isinstance(result.structuredContent, dict):
            message = _result_error(result)
            error = InputError(
                "Capture delivery was not acknowledged; retry using the same cursor."
                if not message
                else f"Capture delivery rejected: {message[:240]}"
            )
            error.delivery_error_code = _result_code(result)
            raise error
        return result.structuredContent

    async def deliver(self, event: dict) -> bool:
        details = {
            "source": event["source"],
            "source_kind": "provider_snapshot",
            "source_timestamp": event.get("timestamp"),
            "source_actor": event.get("actor"),
            "sanitization": event.get("sanitization", {}),
        }
        action = event.get("action")
        if event["kind"] == "conversation":
            saved = await self.call(
                "memory_remember",
                {
                    "content": event["details"]["content"],
                    "source": event["source"],
                    "account": self.account,
                    "event_id": event["record_id"],
                    "project": self.project,
                    "title": "Imported user message (source snapshot)",
                },
            )
            details["memory"] = {key: saved[key] for key in ("id", "revision")}
            action = {"type": "conversation_import"}
        else:
            details["event"] = event.get("details", {})
        await self.call(
            "audit_record",
            {
                "project": self.project,
                "kind": "gap" if event["kind"] == "gap" else "action",
                "client_event_id": "capture:" + event["record_id"],
                "action": action,
                "target": event.get("target"),
                "details": details,
            },
        )
        return True

    async def deliver_or_record_terminal_gap(self, event: dict) -> bool:
        try:
            return await self.deliver(event)
        except InputError as exc:
            reason = _terminal_delivery_reason(exc)
            if reason is None or event.get("kind") != "conversation":
                raise
            # Advancing the source requires an acknowledged omission gap.
            await self.call(
                "audit_record",
                {
                    "project": self.project,
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

    def sink(self, event: dict) -> bool:
        return asyncio.run_coroutine_threadsafe(
            self.deliver_or_record_terminal_gap(event),
            self.loop,
        ).result(timeout=45)

    def acknowledge_receipt(self, receipt: dict) -> None:
        asyncio.run_coroutine_threadsafe(
            self.call("audit_record", receipt),
            self.loop,
        ).result(timeout=45)


class _ResumeDisposition(Enum):
    READY = auto()
    RETRY = auto()
    RECOVERY_REQUIRED = auto()


@dataclass(frozen=True)
class _ReceiptResume:
    disposition: _ResumeDisposition
    recovered: bool = False


@dataclass(frozen=True)
class _ReceiptJournal:
    """Own pending receipt validation, recovery and durable acknowledgement."""

    cursor: Path
    delivery: _RemoteDelivery
    recover_pending: bool

    @property
    def pending(self) -> Path:
        return self.cursor.with_name(self.cursor.name + ".coverage.json")

    def is_current(self, receipt: Any) -> bool:
        return (
            isinstance(receipt, dict)
            and receipt.get("project") == self.delivery.project
            and receipt.get("kind") == "coverage"
            and isinstance(receipt.get("client_event_id"), str)
            and receipt["client_event_id"].startswith("capture-run:")
        )

    def archive(self, path: Path) -> None:
        os.replace(path, path.with_name(path.name + ".recovered-" + uuid.uuid4().hex))

    def migrate_legacy(self) -> bool:
        # Full-name sidecars prevent collisions, especially with the cursor itself.
        legacy = self.cursor.with_suffix(".coverage.json")
        if self.pending.exists() or legacy == self.cursor or not legacy.exists():
            return True
        try:
            receipt = read_json(legacy)
            if self.is_current(receipt):
                os.replace(legacy, self.pending)
        except InputError:
            if not self.recover_pending:
                return False
            self.archive(legacy)
        return True

    def acknowledge(self) -> None:
        receipt = read_json(self.pending)
        if receipt and self.is_current(receipt):
            self.delivery.acknowledge_receipt(receipt)
            write_json(self.pending, {})

    def resume(self) -> _ReceiptResume:
        if not self.migrate_legacy():
            return _ReceiptResume(_ResumeDisposition.RECOVERY_REQUIRED)
        recovered = False
        if self.pending.exists():
            try:
                receipt = read_json(self.pending)
            except InputError:
                if not self.recover_pending:
                    return _ReceiptResume(_ResumeDisposition.RECOVERY_REQUIRED)
                self.archive(self.pending)
                recovered = True
            else:
                if receipt and not self.is_current(receipt):
                    if not self.recover_pending:
                        return _ReceiptResume(_ResumeDisposition.RECOVERY_REQUIRED)
                    self.archive(self.pending)
                    recovered = True
        try:
            if self.pending.exists():
                self.acknowledge()
        except InputError as exc:
            if _terminal_delivery_reason(exc) is None:
                return _ReceiptResume(_ResumeDisposition.RETRY)
            if not self.recover_pending:
                return _ReceiptResume(_ResumeDisposition.RECOVERY_REQUIRED)
            self.archive(self.pending)
            recovered = True
        except Exception:
            return _ReceiptResume(_ResumeDisposition.RETRY)
        return _ReceiptResume(_ResumeDisposition.READY, recovered)


def _pass_result(
    state: str,
    forwarded: int,
    coverage_receipt: str,
    *,
    cursor_checkpoint: str | None = None,
    **metadata: Any,
) -> dict:
    result = {"state": state, "forwarded": forwarded}
    if cursor_checkpoint is not None:
        result["cursor_checkpoint"] = cursor_checkpoint
    result.update(
        coverage_receipt=coverage_receipt,
        coverage="partial",
        live_provider_log="unverified",
        **metadata,
    )
    return result


def _new_receipt(paths: list[Path], project: str) -> dict:
    source_times = []
    for path in paths:
        try:
            source_times.append(datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat())
        except FileNotFoundError:
            source_times.append(None)
    return {
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


def _collect_pass(paths: list[Path], kind: str, journal: _ReceiptJournal) -> dict:
    # A lost receipt ACK retries the same event ID before advancing the cursor.
    with locked(journal.cursor.with_name(journal.cursor.name + ".delivery.lock")):
        resume = journal.resume()
        if resume.disposition is _ResumeDisposition.RECOVERY_REQUIRED:
            return _pass_result("capture_recovery_required", 0, "invalid_pending_receipt")
        if resume.disposition is _ResumeDisposition.RETRY:
            return _pass_result("capture_partial", 0, "pending_retry")
        receipt = _new_receipt(paths, journal.delivery.project)
        # Persist before the first checkpoint, so a crash cannot invent coverage.
        write_json(journal.pending, receipt)
        collector = JSONLCollector(paths, journal.cursor, journal.delivery.sink, kind=kind)
        count = collector.collect()
        receipt["details"].update(
            forwarded=count,
            pass_completed=collector.completed,
            batch_limit_reached=not collector.completed,
        )
        write_json(journal.pending, receipt)
        try:
            journal.acknowledge()
        except Exception:
            return _pass_result(
                "capture_partial",
                count,
                "pending_retry",
                cursor_checkpoint="acknowledged_records_saved",
            )
        return _pass_result(
            "capture_pass_complete" if collector.completed else "capture_partial",
            count,
            "acknowledged",
            **({"recovered_pending_receipt": True} if resume.recovered else {}),
            **({"batch_limit_reached": True} if not collector.completed else {}),
        )


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
        delivery = _RemoteDelivery(session, asyncio.get_running_loop(), project, account)
        journal = _ReceiptJournal(cursor, delivery, recover_pending)
        return await asyncio.to_thread(_collect_pass, paths, kind, journal)
