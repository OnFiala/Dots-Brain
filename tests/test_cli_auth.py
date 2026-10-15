import contextlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from dots_brain.auth import Policy, authenticate, issue_client, validate_endpoint
from dots_brain.cli import main, parser, run
from dots_brain.errors import InputError
from dots_brain.service import MemoryService
from dots_brain.store import Store


def cli(directory, *arguments):
    return subprocess.run(
        [sys.executable, "-m", "dots_brain.cli", "--data-dir", str(directory), *arguments],
        capture_output=True,
        text=True,
    )


def test_setup_is_repeatable_and_cli_never_prints_issued_token(tmp_path):
    directory = tmp_path / "memory"
    assert json.loads(cli(directory, "setup").stdout)["state"] == "local_ready"
    assert cli(directory, "setup").returncode == 0
    credential = tmp_path / "client.json"
    result = cli(
        directory, "client", "create", "--name", "test", "--credential-file", str(credential)
    )
    assert result.returncode == 0, result.stderr
    token = json.loads(credential.read_text())["token"]
    assert token not in result.stdout + result.stderr
    assert credential.stat().st_mode & 0o777 == 0o600
    assert json.loads(result.stdout)["secret_isolation"] is False
    assert "not_verified" in cli(directory, "doctor").stdout
    existing = credential.read_bytes()
    assert (
        cli(
            directory, "client", "create", "--name", "again", "--credential-file", str(credential)
        ).returncode
        == 1
    )
    assert credential.read_bytes() == existing


def test_expired_credential_is_rejected_and_only_hash_is_stored(tmp_path):
    store = Store(tmp_path / "memory")
    store.initialize()
    output = tmp_path / "client.json"
    client = issue_client(
        store,
        name="reader",
        scopes=["memory:read"],
        projects=None,
        days=1,
        output=output,
        url="http://localhost:8765/mcp",
    )
    token = json.loads(output.read_text())["token"]
    assert authenticate(store, token) is not None
    assert token.encode() not in store.path.read_bytes()
    with store.connection(write=True) as db:
        db.execute("UPDATE clients SET expires_at=0 WHERE id=?", (client["client_id"],))
    assert authenticate(store, token) is None


@pytest.mark.parametrize("output_state", ["owned", "replacement", "cleanup-denied", "removed"])
def test_failed_issuance_cleans_only_owned_output(tmp_path, monkeypatch, output_state):
    store = Store(tmp_path / "memory")
    store.initialize()
    with store.connection(write=True) as db:
        db.execute(
            "CREATE TRIGGER fail_issue BEFORE INSERT ON clients "
            "BEGIN SELECT RAISE(ABORT, 'synthetic client insert failure'); END"
        )
    output = tmp_path / "client.json"
    replacement = b"Unrelated replacement must survive.\n"
    actual_connection = store.connection
    published = {}
    failures = []

    @contextlib.contextmanager
    def fail_after_publication(*, write=False):
        with actual_connection(write=write) as db:
            if write:
                published.update(json.loads(output.read_text()))
                assert output.stat().st_mode & 0o777 == 0o600
                if output_state == "replacement":
                    staging = tmp_path / "replacement"
                    staging.write_bytes(replacement)
                    os.replace(staging, output)
                elif output_state == "removed":
                    output.unlink()
            try:
                yield db
            except sqlite3.IntegrityError as exc:
                failures.append(exc)
                raise

    monkeypatch.setattr(store, "connection", fail_after_publication)
    if output_state == "cleanup-denied":
        actual_unlink = Path.unlink

        def deny_cleanup(path, *args, **kwargs):
            if path == output:
                raise PermissionError("synthetic cleanup failure")
            return actual_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", deny_cleanup)
    with pytest.raises(sqlite3.IntegrityError, match="synthetic client insert failure") as caught:
        issue_client(
            store,
            name="reader",
            scopes=["memory:read"],
            projects=None,
            days=1,
            output=output,
            url="http://localhost:8765/mcp",
        )
    assert caught.value is failures[0]
    notes = getattr(caught.value, "__notes__", [])
    if output_state == "cleanup-denied":
        assert len(notes) == 1
        assert "Output cleanup incomplete" in notes[0]
        assert str(output) in notes[0]
        assert "PermissionError" in notes[0]
        assert published["token"] not in notes[0]
    else:
        assert notes == []
    if output_state == "replacement":
        assert output.read_bytes() == replacement
    elif output_state in {"owned", "removed"}:
        assert not output.exists()
    else:
        assert json.loads(output.read_text()) == published
    with actual_connection() as db:
        assert db.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 0
    assert authenticate(store, published["token"]) is None


@pytest.mark.parametrize("output_state", ["owned", "replacement", "cleanup-denied", "removed"])
def test_failed_export_cleans_only_owned_output(tmp_path, monkeypatch, output_state):
    store = Store(tmp_path / "memory")
    store.initialize()
    output = tmp_path / "export.jsonl"
    replacement = b"Unrelated replacement must survive.\n"
    failure = RuntimeError("synthetic export failure")

    def interrupted_export(self):
        yield {"content": "synthetic partial export"}
        if output_state == "replacement":
            staging = tmp_path / "replacement"
            staging.write_bytes(replacement)
            os.replace(staging, output)
        elif output_state == "removed":
            output.unlink()
        raise failure

    monkeypatch.setattr(Store, "export", interrupted_export)
    if output_state == "cleanup-denied":
        actual_unlink = Path.unlink

        def deny_cleanup(path, *args, **kwargs):
            if path == output:
                raise PermissionError("synthetic cleanup failure")
            return actual_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", deny_cleanup)
    args = parser().parse_args(
        ["--data-dir", str(store.directory), "export", "--output", str(output)]
    )
    with pytest.raises(RuntimeError) as caught:
        run(args)
    assert caught.value is failure
    notes = getattr(failure, "__notes__", [])
    if output_state == "cleanup-denied":
        assert len(notes) == 1
        assert "Output cleanup incomplete" in notes[0]
        assert str(output) in notes[0]
        assert "PermissionError" in notes[0]
        assert "synthetic partial export" not in notes[0]
    else:
        assert notes == []
    if output_state == "replacement":
        assert output.read_bytes() == replacement
    elif output_state in {"owned", "removed"}:
        assert not output.exists()
    else:
        assert output.exists()


@pytest.mark.parametrize("error_type", [InputError, OSError])
def test_cli_reports_incomplete_export_cleanup(tmp_path, monkeypatch, capsys, error_type):
    store = Store(tmp_path / "memory")
    store.initialize()
    output = tmp_path / "export.jsonl"
    failure = error_type("synthetic export failure")
    failure.add_note("private arbitrary diagnostic")
    private_content = "private partial export content"

    def interrupted_export(self):
        yield {"content": private_content}
        raise failure

    actual_unlink = Path.unlink

    def deny_cleanup(path, *args, **kwargs):
        if path == output:
            raise PermissionError("private cleanup details")
        return actual_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Store, "export", interrupted_export)
    monkeypatch.setattr(Path, "unlink", deny_cleanup)
    monkeypatch.setattr(
        sys,
        "argv",
        ["dots-brain", "--data-dir", str(store.directory), "export", "--output", str(output)],
    )
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 1
    captured = capsys.readouterr()
    report = json.loads(captured.err)
    assert report["state"] == "error"
    assert report["code"] == ("invalid_input" if error_type is InputError else "operation_failed")
    assert "Output cleanup incomplete" in report["warning"]
    assert "may remain" in report["warning"]
    assert output.exists()
    assert "private cleanup details" not in captured.err
    assert "private arbitrary diagnostic" not in captured.err
    assert private_content not in captured.err
    assert str(output) not in captured.err


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com/mcp",
        "https://user:secret@example.com/mcp",
        "https://example.com/mcp?token=secret",
        "file:///mcp",
    ],
)
def test_credentials_are_not_allowed_in_endpoints(endpoint):
    with pytest.raises(InputError):
        validate_endpoint(endpoint)


def test_bounded_context_and_private_export(tmp_path):
    store = Store(tmp_path / "memory")
    store.initialize()
    store.remember(content="decision " * 1000, source="test", account="a", event_id="one")
    context = MemoryService(store).context("decision", policy=Policy(), max_chars=256)
    assert context["characters"] == len(context["context"]) <= 256
    output = tmp_path / "export.jsonl"
    result = cli(store.directory, "export", "--output", str(output))
    assert result.returncode == 0
    assert Path(output).stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text())["source"] == "test"


def test_bridge_with_wrong_local_directory_cannot_initialize_it(tmp_path):
    missing = tmp_path / "must-not-create"
    credential = tmp_path / "client.json"
    credential.write_text(
        json.dumps(
            {
                "version": 1,
                "token": "private-test-value",
                "url": "http://127.0.0.1:8765/mcp",
            }
        )
    )
    result = cli(
        missing,
        "bridge",
        "--credential-file",
        str(credential),
        "--local-data-dir",
        str(missing),
    )
    assert result.returncode == 1
    assert "not initialized" in result.stderr
    assert not missing.exists()
    assert "private-test-value" not in result.stdout + result.stderr
