import sqlite3
from contextlib import closing

import pytest

from dots_brain.auth import authenticate, issue_client
from dots_brain.errors import (
    InputError,
    IntegrityError,
    StateError,
    StoreDisabledError,
    SuppressedError,
)
from dots_brain.installation_state import resume_uninstalled
from dots_brain.operations import activate_restore, backup_store, restore_store
from dots_brain.store import Store


def test_restore_merges_new_deletions_revokes_credentials_and_stays_disabled(tmp_path):
    store = Store(tmp_path / "live")
    store.initialize()
    record = dict(content="Synthetic old fact", source="fixture", account="bot", event_id="one")
    saved = store.remember(**record)
    credentials = tmp_path / "client.json"
    issue_client(
        store,
        name="test",
        scopes=["memory:read"],
        projects=None,
        days=1,
        output=credentials,
        url="http://127.0.0.1:8765/mcp",
    )
    backup = tmp_path / "backup.sqlite3"
    assert backup_store(store, backup)["state"] == "verified_backup"
    assert backup.stat().st_mode & 0o777 == 0o600
    store.forget(saved["id"], expected_revision=1)
    target = Store(tmp_path / "restored")
    result = restore_store(backup, target, latest_deletions=store)
    assert result["state"] == "restored_disabled" and result["removed_since_backup"] == 1
    assert target.status()["memories"] == 0
    assert (target.directory / "disabled.json").exists()
    with pytest.raises(StoreDisabledError, match="disabled"):
        target.remember(**record)
    with target.connection() as db:
        assert db.execute("SELECT revoked FROM clients").fetchone()[0] == 1
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    import json

    assert authenticate(target, json.loads(credentials.read_text())["token"]) is None
    with pytest.raises(InputError):
        restore_store(backup, target, latest_deletions=store)
    with closing(sqlite3.connect(backup.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


def test_restore_activation_rechecks_deletions_and_freezes_old_writers(tmp_path):
    source = Store(tmp_path / "live")
    source.initialize()
    record = dict(content="Synthetic fact", source="test", account="bot", event_id="late")
    saved = source.remember(**record)
    backup = tmp_path / "backup.sqlite3"
    backup_store(source, backup)
    target = Store(tmp_path / "restored")
    restore_store(backup, target, latest_deletions=source)
    from dots_brain.auth import Policy
    from dots_brain.service import MemoryService

    MemoryService(source).mutate(
        source.forget,
        policy=Policy(),
        audit_project="default",
        action="memory_forget",
        memory_id=saved["id"],
        expected_revision=1,
    )  # Public audited mutation after staging, before cutover.
    with pytest.raises((StateError, InputError), match="activate-restore"):
        with resume_uninstalled(target, requested=True):
            pytest.fail("recovery target cannot resume")
    with pytest.raises(InputError, match="writers-stopped"):
        activate_restore(source, target, writers_stopped=False)
    assert activate_restore(source, target, writers_stopped=True)["removed_at_cutover"] == 1
    assert target.status()["memories"] == 0
    with source.connection() as live, target.connection() as recovered:
        expected = [tuple(row) for row in live.execute("SELECT * FROM audit_events ORDER BY id")]
        actual = [tuple(row) for row in recovered.execute("SELECT * FROM audit_events ORDER BY id")]
        assert len(expected) == 2 and actual == expected  # Exact ID, payload and hash chain.
    with pytest.raises(SuppressedError):
        target.remember(**record)
    with pytest.raises(StoreDisabledError, match="disabled"):
        source.remember(**{**record, "event_id": "new"})
    with pytest.raises((StateError, InputError), match="activate-restore"):
        with resume_uninstalled(source, requested=True):
            pytest.fail("recovery source cannot resume")


@pytest.mark.parametrize("damage", ["version", "revision"])
def test_backup_rejects_unsupported_or_logically_corrupt_database(tmp_path, damage):
    source = Store(tmp_path / "live")
    source.initialize()
    source.remember(content="Fact", source="test", account="bot", event_id="a")
    with closing(sqlite3.connect(source.path)) as db, db:
        if damage == "version":
            db.execute("PRAGMA user_version=99")
        else:
            db.execute("UPDATE memories SET current_revision=2")
    output = tmp_path / "bad.sqlite3"
    with pytest.raises(IntegrityError):
        backup_store(source, output)
    assert not output.exists()


def test_restore_revokes_pending_oauth_and_marks_inflight_connector_uncertain(tmp_path):
    from dots_brain.oauth import configure

    source = Store(tmp_path / "live")
    source.initialize()
    configure(source, "http://127.0.0.1:8765")
    with source.connection(write=True) as db:
        db.execute("INSERT INTO oauth_clients VALUES ('client','{}',1)")
        db.execute(
            "INSERT INTO oauth_requests VALUES ('request','client','{}','pending',NULL,9999999999)"
        )
        db.execute("INSERT INTO oauth_codes VALUES ('code','client','{}',NULL,9999999999)")
        db.execute(
            "INSERT INTO oauth_grants VALUES ('grant','client','[]',NULL,'resource',9999999999,0)"
        )
        db.execute("INSERT INTO oauth_tokens VALUES ('token','access','grant','[]',9999999999)")
        for state in ("sending", "acknowledged"):
            db.execute(
                "INSERT INTO cortex_operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    state,
                    "digest",
                    "owner",
                    "work",
                    "work",
                    "note",
                    "source",
                    state,
                    '{"event_id":"receipt"}',
                    None,
                    None,
                    "now",
                    "now",
                ),
            )
    backup = tmp_path / "backup.sqlite3"
    backup_store(source, backup)
    target = Store(tmp_path / "restored")
    restore_store(backup, target, latest_deletions=source)
    with target.connection() as db:
        for table in (
            "oauth_requests",
            "oauth_codes",
            "oauth_tokens",
        ):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert db.execute("SELECT revoked FROM oauth_grants WHERE id='grant'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0] == 1
        rows = db.execute(
            "SELECT operation_id,state,receipt_json FROM cortex_operations ORDER BY operation_id"
        ).fetchall()
        assert [(row[0], row[1]) for row in rows] == [
            ("acknowledged", "acknowledged"),
            ("sending", "uncertain"),
        ]
        assert "receipt" in rows[0][2]


def test_cutover_refuses_to_lose_new_revisions_or_audit_history(tmp_path):
    from dots_brain.activity import AuditLog
    from dots_brain.auth import Policy

    source = Store(tmp_path / "live")
    source.initialize()
    record = dict(content="First", source="test", account="bot", event_id="a")
    source.remember(**record)
    backup = tmp_path / "backup.sqlite3"
    backup_store(source, backup)
    target = Store(tmp_path / "restored")
    restore_store(backup, target, latest_deletions=source)
    source.remember(**{**record, "content": "Second"}, expected_revision=1)
    source.remember(**{**record, "event_id": "b"})
    AuditLog(source).record(
        Policy(), project="default", kind="action", client_event_id="new-action"
    )
    with pytest.raises(IntegrityError, match="canonical state") as error:
        activate_restore(source, target, writers_stopped=True)
    assert "revisions=" in str(error.value)
    assert source.status()["memories"] == 2
    assert not (source.directory / "disabled.json").exists()
    assert (target.directory / "disabled.json").exists()
    assert target.status()["memories"] == 1


def test_cutover_rejects_recreated_target_at_same_path(tmp_path):
    import shutil

    source = Store(tmp_path / "live")
    source.initialize()
    backup = tmp_path / "backup.sqlite3"
    backup_store(source, backup)
    target = Store(tmp_path / "restored")
    restore_store(backup, target, latest_deletions=source)
    activate_restore(source, target, writers_stopped=True)
    shutil.rmtree(target.directory)  # Disposable test only: emulate path reuse.
    restore_store(backup, target, latest_deletions=source)
    with pytest.raises(InputError, match="already replaced"):
        activate_restore(source, target, writers_stopped=True)
    assert (target.directory / "disabled.json").exists()


def test_cutover_retries_same_recovery_after_committed_marker_crash(tmp_path, monkeypatch):
    from pathlib import Path

    source = Store(tmp_path / "live")
    source.initialize()
    backup = tmp_path / "backup.sqlite3"
    backup_store(source, backup)
    target = Store(tmp_path / "restored")
    restore_store(backup, target, latest_deletions=source)
    real_unlink = Path.unlink
    failed = False

    def fail_once(path, *args, **kwargs):
        nonlocal failed
        if path == target.directory / "disabled.json" and not failed:
            failed = True
            raise OSError("Synthetic crash before enabling target")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_once)
    with pytest.raises(OSError):
        activate_restore(source, target, writers_stopped=True)
    assert all((store.directory / "disabled.json").exists() for store in (source, target))
    assert activate_restore(source, target, writers_stopped=True)["state"] == "restored_ready"


def test_committed_recovery_choice_survives_failed_resume(tmp_path, monkeypatch):
    import json
    from pathlib import Path

    from dots_brain import operations

    source = Store(tmp_path / "live")
    source.initialize()
    backup = tmp_path / "backup.sqlite3"
    backup_store(source, backup)
    target = Store(tmp_path / "first")
    restore_store(backup, target, latest_deletions=source)
    real_unlink = Path.unlink

    def crash_before_enable(path, *args, **kwargs):
        if path == target.directory / "disabled.json":
            raise OSError("First interruption")
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", crash_before_enable)
        with pytest.raises(OSError):
            activate_restore(source, target, writers_stopped=True)
    marker = source.directory / "disabled.json"
    first = json.loads(marker.read_text())
    assert first["cutover"] == "committed"

    def crash_during_resume(*args):
        raise OSError("Second interruption")

    with monkeypatch.context() as patch:
        patch.setattr(operations, "_apply_deletions", crash_during_resume)
        with pytest.raises(OSError):
            activate_restore(source, target, writers_stopped=True)
    assert json.loads(marker.read_text()) == first
    another = Store(tmp_path / "another")
    restore_store(backup, another, latest_deletions=source)
    with pytest.raises(InputError, match="already replaced"):
        activate_restore(source, another, writers_stopped=True)
    assert activate_restore(source, target, writers_stopped=True)["state"] == "restored_ready"


@pytest.mark.parametrize("missing_target", [False, True])
@pytest.mark.parametrize("uninstalled", [False, True])
def test_abort_pending_restore_retains_uninstall_and_handles_missing_target(
    tmp_path, monkeypatch, missing_target, uninstalled
):
    import shutil

    from dots_brain import operations
    from dots_brain.installation_state import mark_uninstalled, read_marker

    source, target = Store(tmp_path / "source"), Store(tmp_path / "target")
    source.initialize()
    backup = tmp_path / "before.sqlite3"
    backup_store(source, backup)
    restore_store(backup, target, latest_deletions=source)
    sync = operations.sync_file_and_parent

    def fail_after_pending(path):
        if read_marker(source).get("cutover") == "pending":
            raise OSError("Synthetic interruption after pending marker")
        sync(path)

    with monkeypatch.context() as patch:
        patch.setattr(operations, "sync_file_and_parent", fail_after_pending)
        with pytest.raises(OSError, match="Synthetic interruption"):
            activate_restore(source, target, writers_stopped=True)
    assert read_marker(source)["cutover"] == "pending"
    if uninstalled:
        mark_uninstalled(source)
    if missing_target:
        shutil.rmtree(target.directory)  # Only this disposable recovery fixture.
    result = operations.abort_restore(source, target, writers_stopped=True)
    assert result["target"] == ("missing" if missing_target else "disabled")
    assert result["source"] == ("disabled" if uninstalled else "enabled")
    if uninstalled:
        assert read_marker(source)["reason"] == "uninstalled"
    else:
        assert not read_marker(source)
