"""Durable disable markers shared by removal and recovery.

Only an ordinary uninstall is resumable. Recovery transitions retain their
identity and require an explicit cutover or cancellation.
"""

from datetime import UTC, datetime

from .errors import StateError
from .local import read_json, sync_directory, write_json


def marker_path(store):
    return store.directory / "disabled.json"


def read_marker(store) -> dict:
    path = marker_path(store)
    return read_json(path) if path.exists() else {}


def write_marker(store, *, reason: str, **fields) -> None:
    write_json(
        marker_path(store),
        {
            "version": 1,
            "reason": reason,
            "created_at": datetime.now(UTC).isoformat(),
            **fields,
        },
    )


def mark_uninstalled(store) -> None:
    previous = read_marker(store)
    if previous:
        # Do not erase a recovery barrier or unknown future state during removal.
        write_json(
            marker_path(store), {**previous, "uninstalled_at": datetime.now(UTC).isoformat()}
        )
    else:
        write_marker(store, reason="uninstalled")


def resume_uninstalled(store) -> None:
    state = read_marker(store)
    if not state:
        return
    if state.get("version") != 1 or state.get("reason") != "uninstalled":
        raise StateError("Recovery requires activate-restore; up --resume cannot bypass it.")
    marker_path(store).unlink()
    sync_directory(store.directory)
