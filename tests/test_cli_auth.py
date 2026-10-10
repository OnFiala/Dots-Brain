import json
import subprocess
import sys
from pathlib import Path

import pytest

from dots_brain.auth import Policy, authenticate, issue_client, validate_endpoint
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
        directory,
        "client",
        "create",
        "--name",
        "test",
        "--credential-file",
        str(credential),
        "--url",
        "http://127.0.0.1:8765/mcp",
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
            directory,
            "client",
            "create",
            "--name",
            "again",
            "--credential-file",
            str(credential),
            "--url",
            "http://127.0.0.1:8765/mcp",
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
