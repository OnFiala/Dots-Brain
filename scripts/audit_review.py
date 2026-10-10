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
import shlex
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

# The documented operator entry point runs from a source checkout, without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dots_brain.activity import AuditLog  # noqa: E402
from dots_brain.errors import ConflictError  # noqa: E402

DEFAULT_STATE = Path.home() / ".local/state/dots-brain-audit"
PAGE_SIZE = 100
MAX_PAGES = 20
MAX_PAGE_BYTES = 1024 * 1024
MAX_UNRESOLVED = 1000
MAX_ANOMALY_EVENT_IDS = 16
FINDING_CATEGORIES = {
    "coverage_unverified",
    "capture_gap",
    "source_stale",
    "missing_receipt",
    "unexpected_activity",
    "operation_failure",
    "continuity_failure",
    "orphan_or_duplicate_receipt",
    "receipt_evidence_mismatch",
    "unresolved_budget_exhausted",
    "availability_failure",
    "missed_review",
    "sanitization",
    "expected_probe",
}


class ReviewError(ValueError):
    code = "invalid_review_data"


class AuditPageError(ReviewError):
    code = "invalid_audit_page"


class AuditPageTooLarge(AuditPageError):
    """A valid response exceeded the local review budget."""


class AuditContinuityError(ReviewError):
    code = "audit_continuity_failure"


class AuditTransportError(ReviewError):
    code = "audit_transport_failure"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def utc_now():
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class AuditTarget:
    """Allowlisted private transport configuration; never accepts a shell command."""

    origin: str
    host: str
    user: str
    executable: str
    data_dir: str
    timezone: str = "UTC"

    @classmethod
    def load(cls, path):
        data = read_json(Path(path))
        if not isinstance(data, dict) or set(data) - {
            "origin",
            "host",
            "user",
            "executable",
            "data_dir",
            "timezone",
        }:
            raise ValueError("Invalid audit target configuration")
        target = cls(**data)
        for value in (target.origin, target.host, target.user, target.executable, target.data_dir):
            if not isinstance(value, str) or not value or any(char in value for char in "\r\n\x00"):
                raise ValueError("Invalid audit target configuration")
        if not target.executable.startswith("/") or not target.data_dir.startswith("/"):
            raise ValueError("Invalid audit target configuration")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,253}", target.host) or not re.fullmatch(
            r"[a-z_][a-z0-9_-]{0,31}", target.user
        ):
            raise ValueError("Invalid audit target configuration")
        ZoneInfo(target.timezone)
        return target


def scheduled_slot(timezone="UTC"):
    now = datetime.now(ZoneInfo(timezone))
    if now.hour < 9:
        return f"{(now - timedelta(days=1)).date()}:21"
    return f"{now.date()}:{21 if now.hour >= 21 else 9:02}"


def fetch_page(after_id, limit, target):
    # Only validated integers enter this fixed remote command, never audit text.
    if type(after_id) is not int or after_id < 0 or type(limit) is not int:
        raise ValueError("Invalid audit cursor")
    command = [
        "sudo",
        "-n",
        "-u",
        target.user,
        "-H",
        target.executable,
        "--data-dir",
        target.data_dir,
        "audit",
        "events",
        "--after-id",
        str(after_id),
        "--limit",
        str(limit),
    ]
    result = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            target.host,
            shlex.join(command),
        ],
        capture_output=True,
        timeout=30,
        check=True,
    )
    if len(result.stdout) > MAX_PAGE_BYTES:
        raise AuditPageTooLarge("Audit page exceeds review budget")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AuditPageError("Audit page is not valid JSON") from exc


def validate_page(page, after_id, limit):
    if not isinstance(page, dict) or set(page) != {"events", "next_after_id", "has_more"}:
        raise AuditPageError("Invalid audit page")
    if len(canonical(page).encode("utf-8")) > MAX_PAGE_BYTES:
        raise AuditPageTooLarge("Audit page exceeds review budget")
    rows = page["events"]
    if not isinstance(rows, list) or len(rows) > limit or type(page["has_more"]) is not bool:
        raise AuditPageError("Invalid audit page")
    previous = after_id
    for row in rows:
        if type(row["id"]) is not int or row["id"] <= previous:
            raise AuditPageError("Audit IDs are not strictly increasing")
        previous = row["id"]
    if page["next_after_id"] != previous or (page["has_more"] and not rows):
        raise AuditPageError("Invalid audit continuation")
    return rows


def validate_row(row, previous_hash):
    if not isinstance(row, dict):
        raise AuditPageError("Invalid audit event")
    try:
        return AuditLog.verify_row(row, previous_hash)
    except (ConflictError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AuditContinuityError("Audit continuity broken") from exc


def unresolved_key(project, principal, client_event_id):
    """A one-way local pairing key; review state never retains client identifiers."""
    return digest(["dots-brain.audit-review.unresolved.v2", project, principal, client_event_id])


def checkpoint_unresolved(items):
    """Read legacy local state into a non-secret, hash-keyed candidate without rewriting it."""
    if not isinstance(items, list):
        raise ReviewError("Invalid unresolved checkpoint")
    result = {}
    for item in items:
        if not isinstance(item, dict) or type(item.get("id")) is not int:
            raise ReviewError("Invalid unresolved checkpoint")
        if "key_hash" in item:
            key = item["key_hash"]
            if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
                raise ReviewError("Invalid unresolved checkpoint")
        else:
            # v1 persisted the clear actor/project/client tuple. Convert only in
            # memory; the committed candidate replaces it after reviewed completion.
            try:
                project, principal, client_event_id = json.loads(item["key"])
            except (KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
                raise ReviewError("Invalid unresolved checkpoint") from exc
            key = unresolved_key(project, principal, client_event_id)
        result[key] = {
            "key_hash": key,
            "id": item["id"],
            "recorded_at": item.get("recorded_at"),
            "event_hash": item.get("event_hash"),
            "evidence": item.get("evidence"),
        }
    return result


def anomaly(code, event_ids):
    return {"code": code, "event_ids": sorted(set(event_ids))}


def add_anomaly(anomalies, code, event_ids):
    """Aggregate repeated bounded anomalies without hiding their scale."""
    event_ids = sorted(set(event_ids))
    for item in anomalies:
        if item["code"] != code:
            continue
        item["count"] = item.get("count", 1) + 1
        samples = sorted(set(item["event_ids"] + event_ids))
        if len(samples) > MAX_ANOMALY_EVENT_IDS:
            samples = samples[: MAX_ANOMALY_EVENT_IDS - 1] + [samples[-1]]
        item["event_ids"] = samples
        return
    anomalies.append(anomaly(code, event_ids))


def fetch_valid_page(fetch, after, limit):
    """Retry only an oversized page with fewer rows; never truncate a row."""
    while True:
        try:
            page = fetch(after, limit)
            return page, validate_page(page, after, limit), limit
        except AuditPageError as error:
            if (
                not isinstance(error, AuditPageTooLarge)
                and str(error) != "Audit page exceeds review budget"
            ):
                raise
            if limit == 1:
                raise
            limit = max(1, limit // 2)


def collect(checkpoint, fetch, max_pages=None, origin="synthetic"):
    """Bounded scan; failures return no candidate and never change a checkpoint."""
    if max_pages is None:
        max_pages = MAX_PAGES
    if checkpoint and checkpoint["origin"] != origin:
        raise ReviewError("Audit origin changed")
    after = checkpoint.get("last_id", 0)
    previous_hash = checkpoint.get("event_hash", "")
    if after:
        anchor = validate_page(fetch(after - 1, 1), after - 1, 1)
        if not anchor or anchor[0]["id"] != after or anchor[0]["event_hash"] != previous_hash:
            raise AuditContinuityError("Audit anchor changed or disappeared; do not reset cursor")
        validate_row(anchor[0], anchor[0]["prev_hash"])

    unresolved = checkpoint_unresolved(checkpoint.get("unresolved", []))
    events = []
    anomalies = []
    has_more = False
    page_limit = PAGE_SIZE
    for _ in range(max_pages):
        page, rows, page_limit = fetch_valid_page(fetch, after, page_limit)
        for row in rows:
            payload = validate_row(row, previous_hash)
            previous_hash = row["event_hash"]
            key = unresolved_key(row["project"], row["principal"], row["client_event_id"])
            if row["kind"] == "intent":
                if len(unresolved) >= MAX_UNRESOLVED:
                    add_anomaly(anomalies, "unresolved_budget_exhausted", [row["id"]])
                else:
                    unresolved[key] = {
                        "key_hash": key,
                        "id": row["id"],
                        "recorded_at": row["recorded_at"],
                        "event_hash": row["event_hash"],
                        "evidence": row["evidence"],
                    }
            elif row["kind"] == "receipt":
                reference = unresolved_key(row["project"], row["principal"], row["intent_event_id"])
                if reference not in unresolved:
                    add_anomaly(anomalies, "orphan_or_duplicate_receipt", [row["id"]])
                elif unresolved[reference]["evidence"] not in (None, row["evidence"]):
                    add_anomaly(
                        anomalies,
                        "receipt_evidence_mismatch",
                        [unresolved[reference]["id"], row["id"]],
                    )
                else:
                    del unresolved[reference]
            events.append({"id": row["id"], "recorded_at": row["recorded_at"], **payload})
            after = row["id"]
        has_more = page["has_more"]
        if not has_more:
            break
    return events, {
        "origin": origin,
        "last_id": after,
        "event_hash": previous_hash,
        "unresolved": list(unresolved.values()),
        "anomalies": anomalies,
        "has_more": has_more,
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


def stage(root, run_key, fetch, origin="synthetic"):
    checkpoint = read_json(root / "checkpoint.json")
    if checkpoint.get("run_key") == run_key and not checkpoint.get("has_more"):
        return {"state": "already_completed", "run_key": run_key}
    events, candidate = collect(checkpoint, fetch, origin=origin)
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
        "scan_findings": [
            {
                "category": item["code"],
                "severity": "warning",
                "status": "active",
                "event_ids": item["event_ids"],
                "confidence": "high",
            }
            for item in candidate["anomalies"]
        ],
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


def _sample_event_ids(values):
    values = sorted(set(values))
    if len(values) > MAX_ANOMALY_EVENT_IDS:
        return values[: MAX_ANOMALY_EVENT_IDS - 1] + [values[-1]]
    return values


def checkpoint_anomaly_counts(checkpoint):
    """Read bounded cumulative counts without changing the findings envelope."""
    counts = checkpoint.get("anomaly_counts", {})
    if not isinstance(counts, dict) or any(
        category not in FINDING_CATEGORIES or type(count) is not int or count < 1
        for category, count in counts.items()
    ):
        raise ReviewError("Invalid anomaly counts checkpoint")
    return counts


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
        return {
            "state": "completed_chunk" if checkpoint.get("has_more") else "already_completed",
            "last_id": checkpoint["last_id"],
            "has_more": checkpoint.get("has_more", False),
        }
    pending = read_json(root / "pending.json")
    if pending.get("review_id") != review_id or pending["base_digest"] != digest(checkpoint):
        raise ValueError("Stale or missing review; scan and analyze again")
    if pending["events_digest"] != review["events_digest"]:
        raise ValueError("Review content digest mismatch")
    previous_findings = checkpoint.get("findings", [])
    validate_findings(previous_findings)
    anomaly_counts = checkpoint_anomaly_counts(checkpoint)
    allowed_ids = set(pending["event_ids"])
    allowed_ids.update(item["id"] for item in pending["candidate"]["unresolved"])
    for item in previous_findings:
        allowed_ids.update(item["event_ids"])
    if any(set(item["event_ids"]) - allowed_ids for item in findings):
        raise ValueError("Finding refers to an event outside the reviewed evidence")
    auto_categories = {item["category"] for item in pending.get("scan_findings", [])}
    merged = {}
    for item in previous_findings + pending.get("scan_findings", []) + findings:
        normalized = item | {"event_ids": sorted(set(item["event_ids"]))}
        key = canonical(
            [
                normalized["category"],
                None if normalized["category"] in auto_categories else normalized["event_ids"],
            ]
        )
        if normalized["status"] == "resolved":
            merged.pop(key, None)
        else:
            if existing := merged.get(key):
                normalized = normalized | {
                    "event_ids": _sample_event_ids(existing["event_ids"] + normalized["event_ids"])
                }
            merged[key] = normalized
    validate_findings(list(merged.values()))
    anomaly_counts = dict(anomaly_counts)
    for anomaly in pending["candidate"]["anomalies"]:
        count = anomaly.get("count", 1)
        if type(count) is not int or count < 1:
            raise ReviewError("Invalid anomaly count")
        category = anomaly["code"]
        anomaly_counts[category] = anomaly_counts.get(category, 0) + count
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
        "anomaly_counts": anomaly_counts,
        "coverage": "recorded audit only; provider-wide capture unverified",
    }
    # Report metadata and cursor have one atomic commit point.
    atomic_json(root / "checkpoint.json", record)
    return {
        "state": "completed_chunk" if record["has_more"] else "completed",
        "last_id": record["last_id"],
        "has_more": record["has_more"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan")
    scan.add_argument("--run-key", default=None)
    scan.add_argument(
        "--target-config",
        type=Path,
        help="Private target JSON; defaults to target.json in the review state directory.",
    )
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
            target = AuditTarget.load(args.target_config or root / "target.json")
            result = stage(
                root,
                args.run_key or scheduled_slot(target.timezone),
                lambda after, limit: fetch_page(after, limit, target),
                origin=target.origin,
            )
        else:
            result = complete(root, read_json(args.review_file))
        print(canonical(result))


if __name__ == "__main__":
    try:
        main()
    except AuditTransportError as error:
        print(canonical({"state": "incomplete", "error_code": error.code}))
        raise SystemExit(1) from None
    except (OSError, subprocess.SubprocessError):
        print(canonical({"state": "incomplete", "error_code": "audit_transport_failure"}))
        raise SystemExit(1) from None
    except ReviewError as error:
        # Codes are fixed; remote stderr and sanitized audit payloads stay out of output.
        print(canonical({"state": "incomplete", "error_code": error.code}))
        raise SystemExit(1) from None
    except (ValueError, KeyError, TypeError):
        # Remote stderr and audit payloads are deliberately not reproduced.
        print(canonical({"state": "incomplete", "error_code": "invalid_review_data"}))
        raise SystemExit(1) from None
