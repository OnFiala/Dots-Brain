"""Check operational documentation contracts against executable local behavior."""

from __future__ import annotations

import errno
import re
import shlex
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from dots_brain import oauth
from dots_brain.auth import issue_client
from dots_brain.cli import parser
from dots_brain.cortex_connector import CortexConnector
from dots_brain.errors import IntegrityError, StateError, StoreDisabledError
from dots_brain.protocol import safe_error
from dots_brain.runtime import credential
from dots_brain.store import Store

ROOT = Path(__file__).resolve().parents[1]


def document(path):
    return (ROOT / path).read_text(encoding="utf-8")


def table_row(path, label):
    return next(
        (line for line in document(path).splitlines() if line.startswith(f"| {label} |")), ""
    )


@pytest.mark.parametrize(
    "error",
    [
        StoreDisabledError("disabled"),
        StateError("invalid"),
        IntegrityError("invalid database"),
        sqlite3.DatabaseError("failure"),
        PermissionError(errno.EACCES, "denied"),
    ],
)
def test_typed_errors_have_reference_and_actionable_recovery(error):
    code = safe_error(error)["code"]
    for path in ("docs/reference/cli.md", "docs/reference/mcp-tools.md"):
        assert f"`{code}`" in document(path), (path, code)
    row = table_row("docs/troubleshooting.md", f"`{code}`")
    assert "doctor" in row, (code, "missing diagnostic command")


@pytest.mark.parametrize(
    "required",
    [
        ("oauth", "status"),
        ("oauth", "clients"),
        ("oauth", "purge"),
        ("oauth", "deny"),
        ("oauth", "onboarding", "status"),
        ("oauth", "approve", "--allow-forget"),
        ("audit", "events", "--since", "--until"),
        ("bridge", "--local-data-dir"),
    ],
)
def test_operator_commands_have_parseable_examples(required):
    examples = []
    for block in re.findall(r"```sh\n(.*?)```", document("docs/reference/cli.md"), re.S):
        for line in block.splitlines():
            if line.startswith("dots-brain "):
                arguments = shlex.split(line)[1:]
                parser().parse_args(arguments)
                examples.append(arguments)
    assert any(set(required) <= set(arguments) for arguments in examples), required


def documented_lifetime(path, item):
    row = table_row(path, item)
    value = re.search(r"([\d,]+) (seconds|days)", row)
    assert value is not None, (item, "missing quantitative lifetime")
    return int(value[1].replace(",", "")) * (86400 if value[2] == "days" else 1)


def test_static_credential_lifetimes_match_issued_credentials(tmp_path):
    store = Store(tmp_path / "memory")
    store.initialize()
    start = time.time()
    args = parser().parse_args(
        ["client", "create", "--name", "synthetic", "--credential-file", "unused"]
    )
    explicit = issue_client(
        store,
        name="synthetic",
        scopes=["memory:read"],
        projects=["synthetic"],
        days=args.days,
        output=tmp_path / "explicit.json",
        url="http://127.0.0.1:8765/mcp",
    )
    managed_id = credential(
        store,
        name="synthetic-managed",
        path=tmp_path / "managed.json",
        url="http://127.0.0.1:8765/mcp",
        projects=["synthetic"],
    )
    with store.connection() as db:
        managed_expiry = db.execute(
            "SELECT expires_at FROM clients WHERE id=?", (managed_id,)
        ).fetchone()[0]
    observed = (("`client create`", explicit["expires_at"]), ("`connect`", managed_expiry))
    for item, expiry in observed:
        seconds = documented_lifetime("docs/reference/cli.md", item)
        assert start + seconds <= expiry <= time.time() + seconds


def test_oauth_lifetimes_match_an_actual_authorization(tmp_path, monkeypatch):
    store = Store(tmp_path / "memory")
    store.initialize()
    oauth.configure(store, "http://127.0.0.1:8765")
    state = oauth.OAuthStore(store)
    now = 2_000_000_000
    monkeypatch.setattr(oauth, "time", SimpleNamespace(time=lambda: now))
    state.set_onboarding(open_for_seconds=600)
    callback = "http://127.0.0.1:9999/callback"
    client = OAuthClientInformationFull(
        client_id="synthetic-client",
        redirect_uris=[AnyUrl(callback)],
        token_endpoint_auth_method="none",
        scope="memory:read",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )
    state.register_client(client)
    pairing = state.authorize(
        client,
        AuthorizationParams(
            redirect_uri=AnyUrl(callback),
            state="synthetic",
            redirect_uri_provided_explicitly=True,
            scopes=["memory:read"],
            code_challenge="x" * 43,
            resource=state.resource,
        ),
    )
    request_id = pairing.rsplit("/", 1)[-1]
    request_expiry = state.pending()["requests"][0]["expires_at"]
    state.decide(request_id, redirect_host="127.0.0.1", projects=["synthetic"])
    code_text = parse_qs(urlsplit(state.claim(request_id)["url"]).query)["code"][0]
    code = state.load_code(client, code_text)
    tokens = state.exchange_code(client, code)
    grant_expiry = state.grants()["grants"][0]["expires"]
    for item, lifetime in (
        ("Authorization request", request_expiry - now),
        ("Authorization code", code.expires_at - now),
        ("OAuth grant", grant_expiry - now),
        ("OAuth access token", tokens.expires_in),
    ):
        assert documented_lifetime("docs/oauth.md", item) == lifetime


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("note", {"title": "Synthetic", "summary": "", "content": "Tile", "topic": None}),
        (
            "decision",
            {
                "title": "Synthetic",
                "summary": "Tile",
                "doctrine_statement": None,
                "doctrine_level": "soft",
            },
        ),
        ("outcome", {"title": "Synthetic", "summary": "Tile", "status": "success", "topic": None}),
    ],
)
def test_cortex_write_contract_documents_actual_upstream_fields(kind, payload):
    request = CortexConnector._upstream_request(
        kind, "synthetic", "synthetic-principal", "memory://synthetic/1", payload, "cxo_synthetic"
    )
    row = table_row("docs/reference/mcp-tools.md", f"`cortex_record_{kind}`")
    fields = set(re.findall(r"`([^`]+)`", row)) - {f"cortex_record_{kind}"}
    assert fields == set(request)
    retry_paragraph = next(
        part for part in document("skills/setup/SKILL.md").split("\n\n") if "retrying" in part
    )
    assert re.search(rf"\b{kind}\b", retry_paragraph), kind


@pytest.mark.parametrize(
    "contract",
    [
        "Unknown tool arguments",
        "1 MiB",
        "capture_partial",
        "capture_recovery_required",
        "--no-start",
        "--redirect-host",
        "--replace-issuer",
        "POST",
        "0.20",
        "128 chunks",
        "credential filter",
        "--network-policy",
        "`uninstall` returns `partial`",
    ],
)
def test_unreleased_breaking_contracts_are_in_breaking_section(contract):
    unreleased = document("CHANGELOG.md").split("\n## ", 2)[1]
    breaking = unreleased.split("\n### Breaking\n", 1)[1]
    assert contract in breaking
    assert "30 days" not in breaking  # This default predates the candidate.


def test_historical_deployment_points_to_canonical_runbook():
    historical = document("docs/appliance-deployment.md")
    assert "[Linux deployment template](../deploy/README.md)" in historical
    assert "serve --public-gateway" not in historical


def test_installation_describes_tested_wheel_without_claiming_publication():
    for path in ("README.md", "docs/installation.md"):
        text = document(path)
        assert "no supported `pip install` path" not in text
        assert "wheel" in text and "CI" in text


def test_tls_example_keeps_the_dedicated_gateway_boundary():
    text = document("docs/oauth.md")
    blocks = re.findall(r"```nginx\n(.*?)```", text, re.S)
    assert len(blocks) == 1
    assert "proxy_pass http://127.0.0.1:8788;" in blocks[0]
    assert "ssl_certificate_key " in blocks[0] and "listen 443 ssl;" in blocks[0]
    assert "[Linux deployment template](../deploy/README.md)" in text


def test_oauth_configure_and_code_replay_contract_is_explicit():
    text = " ".join(document("docs/oauth.md").split())
    assert "Configure the issuer while writers are stopped" not in text
    assert "can run alongside an existing service" in text
    assert "HTTP 400 `invalid_grant`" in text
    assert "original access token and grant remain valid" in text


def test_performance_measurements_have_shipped_reproduction():
    text = document("docs/stress-tests.md")
    assert "[benchmark_storage.py](../scripts/benchmark_storage.py)" in text
    assert (ROOT / "scripts/benchmark_storage.py").is_file()
    for mode in ("seed", "measure", "migrate"):
        assert f"scripts/benchmark_storage.py {mode} " in text
    # Search/index timing rows need their own comparable harness before publication.
    assert not re.search(r"^\|[^\n]*chunks[^\n]*\bms\b", text, re.M)


def test_upgrade_guide_does_not_disqualify_its_own_candidate_procedure():
    text = " ".join(document("docs/upgrading.md").split())
    assert "candidate is not a normal upgrade path" not in text
