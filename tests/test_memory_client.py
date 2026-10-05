import json
import subprocess
import sys
from pathlib import Path

from dots_brain.auth import issue_client
from dots_brain.runtime import down, up
from dots_brain.store import Store

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "memory_client.py"


def invoke(credential, *args, payload="{}"):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--credential-file", str(credential), *args],
        input=payload,
        text=True,
        capture_output=True,
        timeout=60,
    )


def test_shell_client_uses_live_mcp_and_preserves_client_scope(tmp_path):
    store = Store(tmp_path / "memory")
    store.initialize()
    credential = tmp_path / "client.json"
    try:
        service = up(store, port=0)
        issue_client(
            store,
            name="shell-test",
            scopes=["memory:read", "memory:write"],
            projects=["allowed"],
            days=1,
            output=credential,
            url=service["url"],
        )
        discovered = invoke(credential)
        assert discovered.returncode == 0, discovered.stderr
        names = {tool["name"] for tool in json.loads(discovered.stdout)["tools"]}
        assert "memory_remember" in names and "memory_forget" not in names
        event = dict(content="A shell probe.", source="test", account="test", event_id="one")
        saved = invoke(
            credential, "memory_remember", payload=json.dumps(event | {"project": "allowed"})
        )
        assert saved.returncode == 0, saved.stderr
        record = json.loads(saved.stdout)["structuredContent"]
        down(store)
        fetched = invoke(
            credential,
            "--local-data-dir",
            str(store.directory),
            "memory_get",
            payload=json.dumps({"memory_id": record["id"]}),
        )
        assert fetched.returncode == 0, fetched.stderr
        assert json.loads(fetched.stdout)["structuredContent"]["event_id"] == "one"
        denied = invoke(
            credential, "memory_remember", payload=json.dumps(event | {"project": "denied"})
        )
        assert denied.returncode == 1
        assert json.loads(denied.stdout)["isError"] is True
        assert store.status()["memories"] == 1
    finally:
        down(store)


def test_invalid_shell_arguments_do_not_initialize_memory_or_print_credentials(tmp_path):
    credential = tmp_path / "client.json"
    secret = "never-return-this-secret"
    credential.write_text(json.dumps({"token": secret}))
    target = tmp_path / "must-not-create"
    for payload in ("[]", "{invalid", "x" * 131073):
        result = invoke(
            credential, "--local-data-dir", str(target), "memory_status", payload=payload
        )
        assert result.returncode == 1
        assert secret not in result.stdout + result.stderr
        assert not target.exists()
