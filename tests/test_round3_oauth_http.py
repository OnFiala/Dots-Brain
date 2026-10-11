"""Socket-level listener/limiter regressions; all stores and credentials are disposable."""

import asyncio
import contextlib
import json
import os
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.testclient import TestClient

from dots_brain.auth import issue_client
from dots_brain.cli import parser, run
from dots_brain.errors import StateError
from dots_brain.oauth import OAuthStore, configure
from dots_brain.operator_cli import doctor
from dots_brain.server import create_http_app, create_server
from dots_brain.service import MemoryService
from dots_brain.store import Store


@contextlib.contextmanager
def http_host(tmp_path, *, dual=False, public_issuer=False):
    root = Path(__file__).parents[1]
    store = Store(tmp_path / "memory")
    store.initialize()
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        reserved.listen()
        port = reserved.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        with socket.socket() as second:
            second.bind(("127.0.0.1", 0))
            public_port = second.getsockname()[1]
        issuer = "https://memory.example.test" if public_issuer else url
        configure(store, issuer)
        state = OAuthStore(store)
        state.set_onboarding(open_for_seconds=600)
        command = [
            sys.executable,
            "-m",
            "dots_brain.cli",
            "--data-dir",
            str(store.directory),
            "serve",
            "--transport",
            "http",
            "--port",
            str(port),
        ]
        if dual:
            command += ["--public-port", str(public_port)]
        log_path = tmp_path / "server.log"
        # The inherited-fd managed path is Linux-only. Exercise the portable
        # supervised command here; the chosen port stays reserved until launch.
        reserved.close()
        with log_path.open("w+") as log:
            process = subprocess.Popen(
                command,
                cwd=root,
                env={**os.environ, "FORWARDED_ALLOW_IPS": "*"},
                stdout=subprocess.DEVNULL,
                stderr=log,
            )
            try:
                deadline = time.monotonic() + 20
                while True:
                    if process.poll() is not None:
                        log.seek(0)
                        pytest.fail("Synthetic server startup failed: " + log.read())
                    try:
                        ready = httpx.get(
                            url + "/.well-known/oauth-authorization-server",
                            timeout=0.5,
                            trust_env=False,
                        )
                        if ready.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline, "Synthetic HTTP startup timed out"
                    time.sleep(0.02)
                yield store, state, url, f"http://127.0.0.1:{public_port}", log_path
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def oauth_token(state):
    callback = "https://callback.example.test/done"
    client = OAuthClientInformationFull(
        client_id="socket-client",
        redirect_uris=[AnyUrl(callback)],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="memory:read",
    )
    state.register_client(client)
    location = state.authorize(
        client,
        AuthorizationParams(
            redirect_uri=AnyUrl(callback),
            redirect_uri_provided_explicitly=True,
            scopes=["memory:read"],
            state="synthetic-state",
            code_challenge="x" * 43,
            resource=state.resource,
        ),
    )
    request_id = location.rsplit("/", 1)[-1]
    state.decide(request_id, redirect_host="callback.example.test", projects=["synthetic"])
    claimed = state.claim(request_id)
    code = parse_qs(urlsplit(claimed["url"]).query)["code"][0]
    return state.exchange_code(client, state.load_code(client, code)).access_token


async def memory_call(url, token, name, arguments):
    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer " + token}, trust_env=False
    ) as http:
        async with streamable_http_client(url + "/mcp", http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
                assert not result.isError, result
                return result.structuredContent


def test_socket_forwarded_headers_cannot_reset_onboarding_limiter(tmp_path):
    with http_host(tmp_path) as (store, _, url, _, _):
        with httpx.Client(base_url=url, trust_env=False) as http:
            statuses = []
            for number in range(9):
                statuses.append(
                    http.post(
                        "/register",
                        headers={
                            "X-Forwarded-For": f"192.0.2.{number + 1}",
                            "CF-Connecting-IP": f"198.51.100.{number + 1}",
                            "Forwarded": f"for=203.0.113.{number + 1};proto=https",
                        },
                        json={
                            "redirect_uris": ["https://callback.example.test/done"],
                            "token_endpoint_auth_method": "none",
                        },
                    ).status_code
                )
        assert statuses[:6] == [201] * 6
        assert statuses[6:] == [429] * 3
        with store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0] == 6


def test_one_service_two_sockets_keep_static_private_and_share_oauth_store(tmp_path):
    with http_host(tmp_path, dual=True, public_issuer=True) as (store, state, private, public, _):
        credential = tmp_path / "credential.json"
        issue_client(
            store,
            name="synthetic local client",
            scopes=["memory:read", "memory:write"],
            projects=["synthetic"],
            days=1,
            output=credential,
            url=private + "/mcp",
        )
        static = json.loads(credential.read_text())["token"]
        granted = oauth_token(state)
        saved = asyncio.run(
            memory_call(
                private,
                static,
                "memory_remember",
                {
                    "content": "A neutral glass prism has a flat base.",
                    "source": "synthetic",
                    "account": "socket",
                    "event_id": "one",
                    "project": "synthetic",
                },
            )
        )
        assert saved["revision"] == 1
        for url in (private, public):
            found = asyncio.run(memory_call(url, granted, "memory_search", {"query": "prism"}))
            assert any(row["id"] == saved["id"] for row in found["results"])
        for headers in (
            {},
            {"Host": urlsplit(private).netloc},
            {
                "X-Forwarded-For": "127.0.0.1",
                "X-Forwarded-Proto": "http",
                "X-Forwarded-Host": urlsplit(private).netloc,
            },
        ):
            response = httpx.post(
                public + "/mcp",
                json={},
                trust_env=False,
                headers={"Authorization": "Bearer " + static, **headers},
            )
            assert response.status_code == 401
        status = doctor(store)["checks"]["http_listeners"]
        assert status["mode"] == "dual" and status["state"] == "recorded"
        assert status["private_port"] == urlsplit(private).port
        assert status["public_port"] == urlsplit(public).port


def test_public_issuer_without_public_listener_warns_and_doctor_shows_private(tmp_path):
    with http_host(tmp_path, public_issuer=True) as (store, _, _, _, log):
        assert "public_listener_missing" in log.read_text()
        status = doctor(store)["checks"]["http_listeners"]
        assert status["mode"] == "private"
        assert status["warnings"][0]["code"] == "public_listener_missing"
        assert "--public-port" in status["warnings"][0]["next_command"]


def test_absent_listener_metadata_upgrades_on_first_serve_without_schema_change(tmp_path):
    with http_host(tmp_path) as (store, _, _, _, _):
        data = json.loads((store.directory / "http-listeners.json").read_text())
        assert data["version"] == 1
        assert data["mode"] == "private"
        assert (store.directory / "http-listeners.json").stat().st_mode & 0o777 == 0o600
        with store.connection() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 4


def test_doctor_compares_current_issuer_with_running_private_listener(tmp_path):
    with http_host(tmp_path) as (store, _, _, _, _):
        assert doctor(store)["checks"]["http_listeners"]["warnings"] == []
        configure(store, "https://memory.example.test", replace_issuer=True)
        status = doctor(store)["checks"]["http_listeners"]
        assert status["mode"] == "private"
        assert status["warnings"][0]["code"] == "public_listener_missing"


def test_corrupt_config_diagnosis_and_cli_recovery_preserve_state(tmp_path):
    store = Store(tmp_path / "memory")
    store.initialize()
    path = store.directory / "oauth.json"
    path.write_text('{"version":')
    args = ["--data-dir", str(store.directory), "oauth"]
    result = run(parser().parse_args(args + ["status"]))
    assert result["state"] == "invalid" and "oauth configure" in result["next_command"]
    assert doctor(store)["healthy"] is False
    assert path.read_text() == '{"version":'
    run(
        parser().parse_args(
            args
            + [
                "configure",
                "--issuer",
                "https://memory.example.test",
                "--discard-journal",
                "--replace-issuer",
            ]
        )
    )
    recovered = run(parser().parse_args(args + ["status"]))
    assert recovered["state"] == "configured"
    assert recovered["issuer"] == "https://memory.example.test"


def test_public_listener_requires_configuration_and_recovery_command_is_usable(
    tmp_path, monkeypatch
):
    from dots_brain import http_runtime

    store = Store(tmp_path / "synthetic memory")
    store.initialize()
    command = [
        "--data-dir",
        str(store.directory),
        "serve",
        "--transport",
        "http",
        "--public-gateway",
    ]
    started = []
    monkeypatch.setattr("uvicorn.Server.run", lambda self, **kwargs: started.append(True))
    monkeypatch.setattr(http_runtime, "listener_sockets", lambda *args: contextlib.nullcontext([]))
    with pytest.raises(StateError, match="requires OAuth") as error:
        run(parser().parse_args(command))
    assert not started
    recovery = str(error.value).split("; run ", 1)[1].split(" before starting serve.", 1)[0]
    assert run(parser().parse_args(shlex.split(recovery)[1:]))["state"] == "configured"
    run(parser().parse_args(command))
    assert started == [True]


@pytest.mark.parametrize(
    "callback",
    ["https://bad\nhost.example/cb", "https://bad host.example/cb", "https://bad;host.example/cb"],
)
def test_registration_checks_raw_redirect_before_sdk_url_normalization(tmp_path, callback):
    store = Store(tmp_path / "memory")
    store.initialize()
    configure(store, "http://127.0.0.1:8765")
    OAuthStore(store).set_onboarding(open_for_seconds=600)
    service = MemoryService(store)
    with TestClient(
        create_http_app(create_server(service, http=True), service),
        base_url="http://127.0.0.1:8765",
    ) as http:
        assert (
            http.post(
                "/register",
                json={"redirect_uris": [callback], "token_endpoint_auth_method": "none"},
            ).status_code
            == 400
        )
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0] == 0
