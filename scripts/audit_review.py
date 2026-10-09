"""Read appliance audit; commit a local cursor only after an operator's review.

This operator tool never changes the appliance. `scan` prints untrusted, sanitized
audit data and stages metadata. `complete` explicitly acknowledges analysis of
that exact scan. A checkpoint is not proof of complete provider capture.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

ORIGIN = "openclaw-appliance-tailnet:/var/lib/dots-brain"
DEFAULT_STATE = Path.home() / ".local/state/dots-brain-audit"
PAGE_SIZE = 100
MAX_PAGES = 20
FINDING_CATEGORIES = {
    "coverage_unverified",
    "capture_gap",
    "source_stale",
    "missing_receipt",
    "unexpected_activity",
    "operation_failure",
    "continuity_failure",
    "availability_failure",
    "missed_review",
    "sanitization",
    "expected_probe",
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def utc_now():
    return datetime.now(UTC).isoformat()


def scheduled_slot():
    now = datetime.now(ZoneInfo("Europe/Prague"))
    if now.hour < 9:
        return f"{(now - timedelta(days=1)).date()}:21"
    return f"{now.date()}:{21 if now.hour >= 21 else 9:02}"


def fetch_page(after_id, limit):
    # Only validated integers enter this fixed remote command, never audit text.
    if type(after_id) is not int or after_id < 0 or type(limit) is not int:
        raise ValueError("Invalid audit cursor")
    command = (
        "sudo -n -u dots-brain -H /opt/dots-brain/current/.venv/bin/dots-brain "
        "--data-dir /var/lib/dots-brain audit events "
        f"--after-id {after_id} --limit {limit}"
    )
    result = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "openclaw-appliance-tailnet",
            command,
        ],
        capture_output=True,
        timeout=30,
        check=True,
    )
    if len(result.stdout) > 8 * 1024 * 1024:
        raise ValueError("Audit page exceeds review budget")
    return json.loads(result.stdout)


def validate_page(page, after_id, limit):
    rows = page["events"]
    if not isinstance(rows, list) or len(rows) > limit or type(page["has_more"]) is not bool:
        raise ValueError("Invalid audit page")
    previous = after_id
    for row in rows:
        if type(row["id"]) is not int or row["id"] <= previous:
            raise ValueError("Audit IDs are not strictly increasing")
        previous = row["id"]
    if page["next_after_id"] != previous or (page["has_more"] and not rows):
        raise ValueError("Invalid audit continuation")
    return rows


def validate_row(row, previous_hash):
    payload = {
        key: row[key]
        for key in (
            "project",
            "principal",
            "client_event_id",
            "kind",
            "evidence",
            "occurred_at",
            "intent_event_id",
        )
    }
    payload.update({key: json.loads(row[key]) for key in ("action", "target", "details")})
    payload_hash = digest(payload)
    event_hash = hashlib.sha256((previous_hash + payload_hash).encode()).hexdigest()
    if (
        row["prev_hash"] != previous_hash
        or row["payload_hash"] != payload_hash
        or row["event_hash"] != event_hash
    ):
        raise ValueError("Audit continuity broken")
    return payload


def collect(checkpoint, fetch=fetch_page, max_pages=MAX_PAGES):
    """Bounded scan; failures return no candidate and never change a checkpoint."""
    if checkpoint and checkpoint["origin"] != ORIGIN:
        raise ValueError("Audit origin changed")
    after = checkpoint.get("last_id", 0)
    previous_hash = checkpoint.get("event_hash", "")
    if after:
        anchor = validate_page(fetch(after - 1, 1), after - 1, 1)
        if not anchor or anchor[0]["id"] != after or anchor[0]["event_hash"] != previous_hash:
            raise ValueError("Audit anchor changed or disappeared; do not reset cursor")
        validate_row(anchor[0], anchor[0]["prev_hash"])

    unresolved = {item["key"]: dict(item) for item in checkpoint.get("unresolved", [])}
    events = []
    for _ in range(max_pages):
        page = fetch(after, PAGE_SIZE)
        rows = validate_page(page, after, PAGE_SIZE)
        for row in rows:
            payload = validate_row(row, previous_hash)
            previous_hash = row["event_hash"]
            key = canonical([row["project"], row["principal"], row["client_event_id"]])
            if row["kind"] == "intent":
                unresolved[key] = {"key": key, "id": row["id"], "recorded_at": row["recorded_at"]}
            elif row["kind"] == "receipt":
                reference = canonical(
                    [
                        row["project"],
                        row["principal"],
                        row["intent_event_id"],
                    ]
                )
                if reference not in unresolved:
                    raise ValueError("Receipt does not match an unresolved actor/project intent")
                del unresolved[reference]
            events.append({"id": row["id"], "recorded_at": row["recorded_at"], **payload})
            after = row["id"]
        if not page["has_more"]:
            break
    else:
        raise ValueError("Audit page budget exhausted; incomplete review")
    return events, {
        "origin": ORIGIN,
        "last_id": after,
        "event_hash": previous_hash,
        "unresolved": list(unresolved.values()),
    }


def read_json(path):
    if not path.exists():
        return {}
    info = path.stat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("Review state must be owner-only regular files")
    return json.loads(path.read_text())


def atomic_json(path, value):
    fd, name = tempfile.mkstemp(prefix=".review-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(canonical(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def stage(root, run_key, fetch=fetch_page):
    checkpoint = read_json(root / "checkpoint.json")
    if checkpoint.get("run_key") == run_key:
        return {"state": "already_completed", "run_key": run_key}
    events, candidate = collect(checkpoint, fetch)
    previous = read_json(root / "pending.json")
    token = str(uuid4())
    pending = {
        "review_id": token,
        "run_key": run_key,
        "scanned_at": utc_now(),
        "base_digest": digest(checkpoint),
        "events_digest": digest(events),
        "count": len(events),
        "event_ids": [event["id"] for event in events],
        "candidate": candidate,
        "prior_findings": checkpoint.get("findings", []),
        "supersedes_review_id": previous.get("review_id")
        if previous.get("review_id") != checkpoint.get("review_id")
        else None,
        "supersedes_run_key": previous.get("run_key")
        if previous.get("review_id") != checkpoint.get("review_id")
        else None,
    }
    atomic_json(root / "pending.json", pending)
    return {
        "state": "needs_analysis",
        **pending,
        "events": events,
        "coverage": "recorded audit only; provider-wide capture unverified",
    }


def validate_findings(findings):
    if not isinstance(findings, list) or len(canonical(findings)) > 32000:
        raise ValueError("Findings must be a bounded metadata-only list")
    for item in findings:
        if not isinstance(item, dict) or set(item) != {
            "category",
            "severity",
            "status",
            "event_ids",
            "confidence",
        }:
            raise ValueError("Unsupported finding fields")
        if (
            item["category"] not in FINDING_CATEGORIES
            or item["severity"] not in {"info", "warning", "critical"}
            or item["status"] not in {"known", "active", "resolved"}
            or item["confidence"] not in {"low", "medium", "high"}
            or not isinstance(item["event_ids"], list)
            or any(type(value) is not int or value < 1 for value in item["event_ids"])
        ):
            raise ValueError("Invalid finding metadata")


def complete(root, review):
    if not isinstance(review, dict) or set(review) != {
        "review_id",
        "events_digest",
        "model",
        "findings",
    }:
        raise ValueError("Completion requires a review-bound findings envelope")
    review_id, findings, model = review["review_id"], review["findings"], review["model"]
    validate_findings(findings)
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,100}", model):
        raise ValueError("Model must be an identifier, not free text")
    checkpoint = read_json(root / "checkpoint.json")
    # A lost local acknowledgement may retry without advancing twice.
    if checkpoint.get("review_id") == review_id:
        if checkpoint.get("review_digest") != digest(review):
            raise ValueError("Completed review content digest mismatch")
        return {"state": "already_completed", "last_id": checkpoint["last_id"]}
    pending = read_json(root / "pending.json")
    if pending.get("review_id") != review_id or pending["base_digest"] != digest(checkpoint):
        raise ValueError("Stale or missing review; scan and analyze again")
    if pending["events_digest"] != review["events_digest"]:
        raise ValueError("Review content digest mismatch")
    previous_findings = checkpoint.get("findings", [])
    validate_findings(previous_findings)
    allowed_ids = set(pending["event_ids"])
    allowed_ids.update(item["id"] for item in pending["candidate"]["unresolved"])
    for item in previous_findings:
        allowed_ids.update(item["event_ids"])
    if any(set(item["event_ids"]) - allowed_ids for item in findings):
        raise ValueError("Finding refers to an event outside the reviewed evidence")
    merged = {}
    for item in previous_findings + findings:
        normalized = item | {"event_ids": sorted(set(item["event_ids"]))}
        key = canonical([normalized["category"], normalized["event_ids"]])
        if normalized["status"] == "resolved":
            merged.pop(key, None)
        else:
            merged[key] = normalized
    validate_findings(list(merged.values()))
    record = {
        **pending["candidate"],
        "review_id": review_id,
        "run_key": pending["run_key"],
        "scanned_at": pending["scanned_at"],
        "completed_at": utc_now(),
        "events_digest": pending["events_digest"],
        "review_digest": digest(review),
        "reviewed_events": pending["count"],
        "model_reported_by_reviewer": model,
        "findings": list(merged.values()),
        "coverage": "recorded audit only; provider-wide capture unverified",
    }
    # Report metadata and cursor have one atomic commit point.
    atomic_json(root / "checkpoint.json", record)
    return {"state": "completed", "last_id": record["last_id"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan")
    scan.add_argument("--run-key", default=None)
    done = commands.add_parser("complete")
    done.add_argument("--review-file", required=True, type=Path)
    args = parser.parse_args()
    root = args.state_dir
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ValueError("Review directory must be private and owner-controlled")
    lock_fd = os.open(root / "review.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == "scan":
            result = stage(root, args.run_key or scheduled_slot())
        else:
            result = complete(root, read_json(args.review_file))
        print(canonical(result))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        # Remote stderr and audit payloads are deliberately not reproduced.
        print(canonical({"state": "incomplete", "error_type": type(error).__name__}))
        raise SystemExit(1) from None
