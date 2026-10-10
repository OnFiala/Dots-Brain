"""Opt-in Chromium acceptance coverage for the OAuth pairing callback."""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright


def _port() -> int:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    try:
        return listener.getsockname()[1]
    finally:
        listener.close()


def _command(environment: dict[str, str], data: Path, *args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "dots_brain.cli", "--data-dir", str(data), *args],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )


def _register(issuer: str, callback: str) -> dict:
    body = json.dumps(
        {
            "client_name": "Chromium synthetic client",
            "redirect_uris": [callback],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "memory:read",
        }
    ).encode()
    request = urllib.request.Request(
        issuer + "/register", body, {"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


class _Callback(http.server.BaseHTTPRequestHandler):
    received: dict[str, list[str]] | None = None

    def do_GET(self):
        type(self).received = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass


def test_chromium_pairing_form_reaches_cross_origin_loopback_callback(tmp_path):
    """RV-01: browser-enforced CSP permits only the approved callback origin."""
    _Callback.received = None
    server_port, callback_port = _port(), _port()
    issuer = f"http://127.0.0.1:{server_port}"
    callback = f"http://127.0.0.1:{callback_port}/callback"
    root = Path(__file__).parents[1]
    environment = {**os.environ, "HOME": str(tmp_path / "home"), "NO_PROXY": "127.0.0.1,localhost"}
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        environment.pop(name, None)
    data = tmp_path / "data"
    _command(environment, data, "setup")
    _command(environment, data, "oauth", "configure", "--issuer", issuer)
    _command(environment, data, "oauth", "onboarding", "open", "--minutes", "10")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "dots_brain.cli",
            "--data-dir",
            str(data),
            "serve",
            "--transport",
            "http",
            "--port",
            str(server_port),
        ],
        cwd=root,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError(process.stderr.read())
            try:
                with socket.create_connection(("127.0.0.1", server_port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("OAuth loopback server did not start")
        client = _register(issuer, callback)
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        authorize = (
            issuer
            + "/authorize?"
            + urllib.parse.urlencode(
                {
                    "client_id": client["client_id"],
                    "redirect_uri": callback,
                    "response_type": "code",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "scope": "memory:read",
                    "resource": issuer + "/mcp",
                    "state": "synthetic-state",
                }
            )
        )
        callback_server = http.server.ThreadingHTTPServer(("127.0.0.1", callback_port), _Callback)
        callback_thread = threading.Thread(target=callback_server.serve_forever, daemon=True)
        callback_thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(authorize, wait_until="networkidle")
                request_id = page.url.rsplit("/", 1)[-1]
                assert request_id.startswith("req_")
                _command(
                    environment,
                    data,
                    "oauth",
                    "approve",
                    request_id,
                    "--redirect-host",
                    "127.0.0.1",
                    "--project",
                    "synthetic",
                    "--scope",
                    "memory:read",
                )
                response = page.reload(wait_until="networkidle")
                assert response is not None
                assert response.header_value("content-security-policy") == (
                    "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
                    f"form-action 'self' http://127.0.0.1:{callback_port}"
                )
                page.locator("button").click()
                page.wait_for_url("http://127.0.0.1:*/*", timeout=5000)
                assert _Callback.received and _Callback.received.get("code")
                assert _Callback.received.get("state") == ["synthetic-state"]
                browser.close()
        finally:
            callback_server.shutdown()
            callback_server.server_close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        assert process.stderr is not None
        process.stderr.close()
