"""Local, resumable JSONL converters for supplied Grok activity exports.

The collector has no provider hooks and does not claim complete provider coverage.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import InputError
from .local import locked, write_json
from .privacy import sanitize

_AUDIT_TYPES = frozenset(
    {"shell_command", "mcp_tool_call", "browser_navigation", "computer_use_session"}
)
MAX_LINE_BYTES = 256 * 1024
MAX_BATCH_BYTES = 16 * 1024 * 1024
MAX_BATCH_RECORDS = 1000


def _identity(source: str, offset: int, raw: bytes) -> str:
    return hashlib.sha256(f"{source}:{offset}:".encode() + hashlib.sha256(raw).digest()).hexdigest()


def _gap(
    source: str, record_id: str, reason: str, *, timestamp: str | None = None
) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "source": source,
        "kind": "gap",
        "timestamp": timestamp,
        "actor": None,
        "action": {"reason": reason},
        "target": {},
        "details": {"coverage": "partial"},
    }


def normalize_audit_record(
    record: Any, *, source: str = "grok-audit", record_id: str = "unknown"
) -> dict[str, Any]:
    """Reduce a documented audit JSONL row to safe action metadata."""
    if not isinstance(record, dict) or record.get("type") not in _AUDIT_TYPES:
        return _gap(source, record_id, "unknown_audit_shape")
    event_type = record["type"]
    action: dict[str, Any] = {"type": event_type}
    target: dict[str, Any] = {}
    details: dict[str, Any] = {}
    if event_type == "shell_command":
        action["command"] = record.get("command")
        action["shell_kind"] = record.get("shellKind")
    elif event_type == "mcp_tool_call":
        action.update(
            {
                "server_identifier": record.get("serverIdentifier"),
                "tool_name": record.get("toolName"),
                "tool_call_id": record.get("toolCallId"),
                "transport": record.get("transport"),
            }
        )
    elif event_type == "browser_navigation":
        target.update({"url": record.get("url"), "page_title": record.get("pageTitle")})
    else:
        details["action_count"] = record.get("actionCount")
    details.update(
        {
            "status": record.get("status"),
            "duration_ms": record.get("durationMs"),
            "event_id": record.get("eventId"),
            "turn_id": record.get("turnId"),
        }
    )
    clean = sanitize({"action": action, "target": target, "details": details})
    value = clean.value
    return {
        "record_id": record_id,
        "source": source,
        "kind": "action",
        "timestamp": record.get("ts"),
        "actor": record.get("agentId"),
        "action": value["action"],
        "target": value["target"],
        "details": value["details"],
        "sanitization": clean.summary(),
    }


def _visible_text(message: Any) -> str | None:
    if isinstance(message, str):
        return message
    if not isinstance(message, dict):
        return None
    if message.get("channel") == "analysis" or message.get("type") in {"reasoning", "analysis"}:
        return None
    text = message.get("text")
    if isinstance(text, str):
        return text
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if (
                not isinstance(block, dict)
                or block.get("type") not in {"text", "output_text", None}
                or not isinstance(block.get("text"), str)
            ):
                return None
            parts.append(block["text"])
        return "\n".join(parts)
    return None


def normalize_transcript_record(
    record: Any, *, source: str = "grok-transcript", record_id: str = "unknown"
) -> dict[str, Any]:
    """Keep visible user/assistant text and tool metadata; exclude system/reasoning payloads."""
    if not isinstance(record, dict) or not isinstance(record.get("role"), str):
        return _gap(source, record_id, "unknown_transcript_shape")
    role = record["role"]
    if role in {"system", "developer"} or record.get("channel") == "analysis":
        return _gap(source, record_id, "private_instruction_skipped")
    if role in {"user", "assistant"}:
        if role == "assistant":
            # Grok's snapshot has no channel/visibility flag for assistant text.
            # Only tool names are attributable; never infer visibility of free text.
            return _normalize_tool_blocks(record, source, record_id, "tool_use")
        text = _visible_text(record.get("message", record.get("content")))
        if text is None:
            return _gap(source, record_id, "unknown_visible_message_shape")
        clean = sanitize(text, max_text=32000)
        return {
            "record_id": record_id,
            "source": source,
            "kind": "conversation",
            "timestamp": record.get("ts"),
            "actor": role,
            "action": {"role": role},
            "target": {},
            "details": {"content": clean.value},
            "sanitization": clean.summary(),
        }
    if role == "tool":
        return _normalize_tool_blocks(record, source, record_id, "tool_result")
    return _gap(source, record_id, "unknown_transcript_role")


def _normalize_tool_blocks(record, source, record_id, expected_type):
    message = record.get("message")
    blocks = message.get("content") if isinstance(message, dict) else None
    if not isinstance(blocks, list):
        return _gap(source, record_id, "unknown_tool_message_shape")
    actions = []
    omitted = 0
    for block in blocks[:100]:
        if not isinstance(block, dict) or block.get("type") != expected_type:
            omitted += 1
            continue
        name = block.get("name")
        if not isinstance(name, str) or not name.strip():
            omitted += 1
            continue
        item = {"tool_name": name, "type": expected_type}
        if expected_type == "tool_result" and isinstance(block.get("result"), dict):
            item["success"] = (
                block["result"].get("success")
                if type(block["result"].get("success")) is bool
                else None
            )
        actions.append(item)
    if not actions:
        return _gap(
            source,
            record_id,
            "assistant_visibility_unknown"
            if record["role"] == "assistant"
            else "unknown_tool_message_shape",
        )
    return {
        "record_id": record_id,
        "source": source,
        "kind": "action",
        "timestamp": None,
        "actor": record["role"],
        "action": sanitize(actions).value,
        "target": {},
        "details": {
            "coverage": "partial",
            "omitted_blocks": omitted + max(0, len(blocks) - 100),
            "tool_pairing": "unverified_no_call_ids",
            "source_kind": "snapshot",
        },
    }


class JSONLCollector:
    """A sink-injected collector that checkpoints only completed, acknowledged lines."""

    def __init__(
        self,
        paths: list[str | Path],
        cursor_path: str | Path,
        sink: Callable[[dict], Any],
        *,
        kind: str = "audit",
    ):
        if kind not in {"audit", "transcript"}:
            raise InputError("kind must be audit or transcript.")
        self.paths = [Path(path).expanduser().resolve() for path in paths]
        self.cursor_path = Path(cursor_path).expanduser().resolve()
        self.sink, self.kind = sink, kind

    def _load_cursor(self) -> dict[str, Any]:
        try:
            data = json.loads(self.cursor_path.read_text())
            if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
                raise InputError("Capture cursor is invalid; refusing to restart from zero.")
            for value in data["files"].values():
                if not isinstance(value, dict) or any(
                    type(value.get(key)) is not int or value[key] < 0
                    for key in ("device", "inode", "offset")
                ):
                    raise InputError("Capture cursor is invalid; refusing to restart from zero.")
            return data
        except FileNotFoundError:
            return {"files": {}}
        except (OSError, ValueError) as exc:
            raise InputError("Capture cursor is invalid.") from exc

    def _save_cursor(self, data: dict[str, Any]) -> None:
        write_json(self.cursor_path, data)

    def collect(self) -> int:
        with locked(self.cursor_path.with_suffix(".lock")):
            return self._collect()

    def _collect(self) -> int:
        cursor, forwarded, read_bytes = self._load_cursor(), 0, 0
        for path in self.paths:
            # Bind offsets to the descriptor we read, even if the pathname is rotated.
            key, previous = str(path), cursor["files"].get(str(path), {})
            offset = int(previous.get("offset", 0))
            try:
                stat = path.stat()
            except FileNotFoundError:
                if not previous.get("unavailable"):
                    gap = _gap(
                        "collector", _identity(key, offset, b"missing"), "source_unavailable"
                    )
                    if self.sink(gap) is False:
                        raise InputError("Capture sink did not acknowledge record.") from None
                    cursor["files"][key] = {
                        "device": previous.get("device", 0),
                        "inode": previous.get("inode", 0),
                        "offset": offset,
                        "unavailable": True,
                    }
                    self._save_cursor(cursor)
                    forwarded += 1
                continue
            rotated = previous and (
                previous.get("device") != stat.st_dev
                or previous.get("inode") != stat.st_ino
                or stat.st_size < offset
            )
            if rotated:
                gap = _gap(
                    "collector", _identity(key, offset, b"rotation"), "file_rotated_or_truncated"
                )
                if self.sink(gap) is False:
                    raise InputError("Capture sink did not acknowledge record.")
                offset = 0
                cursor["files"][key] = {
                    "device": stat.st_dev,
                    "inode": stat.st_ino,
                    "offset": 0,
                }
                self._save_cursor(cursor)
                forwarded += 1
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (stat.st_dev, stat.st_ino):
                    raise InputError("Capture source rotated during open; retry collection.")
                stream.seek(offset)
                discard = not rotated and previous.get("discard_until_newline", False)
                while True:
                    if read_bytes >= MAX_BATCH_BYTES or forwarded >= MAX_BATCH_RECORDS:
                        return forwarded
                    start, raw = stream.tell(), stream.readline(MAX_LINE_BYTES + 1)
                    read_bytes += len(raw)
                    if not raw:
                        break
                    oversized = len(raw) > MAX_LINE_BYTES
                    if not discard and not oversized and not raw.endswith(b"\n"):
                        break
                    next_offset = stream.tell()
                    record_id = _identity(key, start, raw)
                    if discard:
                        event = None  # The previously acknowledged gap covers these bytes.
                    elif oversized:
                        event = _gap("collector", record_id, "oversized_line")
                    else:
                        event = self._decode(raw, record_id)
                    discard = (discard or oversized) and not raw.endswith(b"\n")
                    if event is not None:
                        event = sanitize(event, max_text=32000).value
                        if self.sink(event) is False:
                            raise InputError("Capture sink did not acknowledge record.")
                        forwarded += 1
                    cursor["files"][key] = {
                        "device": stat.st_dev,
                        "inode": stat.st_ino,
                        "offset": next_offset,
                        "discard_until_newline": discard,
                    }
                    self._save_cursor(cursor)
        return forwarded

    def _decode(self, raw: bytes, record_id: str) -> dict:
        try:
            decoded = json.loads(raw)
            return (
                normalize_audit_record(decoded, record_id=record_id)
                if self.kind == "audit"
                else normalize_transcript_record(decoded, record_id=record_id)
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return _gap("collector", record_id, "invalid_json_line")
