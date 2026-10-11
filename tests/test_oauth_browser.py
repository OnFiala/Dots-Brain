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


def _port(host="127.0.0.1") -> int:
    listener = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
    try:
        listener.bind((host, 0))
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


@pytest.mark.parametrize(
    ("callback_host", "slow_double_submit"),
    [("127.0.0.1", False), ("::1", False), ("127.0.0.1", True)],
    ids=["ipv4", "ipv6", "slow-double-click"],
)
def test_chromium_pairing_form_reaches_cross_origin_loopback_callback(
    tmp_path, callback_host, slow_double_submit
):
    """RV-01/RV-37: actual CSP, IPv6 delivery and delayed duplicate submission."""
    _Callback.received = None
    server_port = _port()
    try:
        callback_port = _port(callback_host)
    except OSError:
        if callback_host == "::1":
            pytest.skip("IPv6 loopback is unavailable on this host")
        raise
    issuer = f"http://127.0.0.1:{server_port}"
    callback_netloc = f"[{callback_host}]" if ":" in callback_host else callback_host
    callback = f"http://{callback_netloc}:{callback_port}/callback"
    root = Path(__file__).parents[1]
    environment = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "NO_PROXY": "127.0.0.1,localhost,::1",
    }
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
        for _ in range(400):
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

        class CallbackServer(http.server.ThreadingHTTPServer):
            address_family = socket.AF_INET6 if ":" in callback_host else socket.AF_INET

        callback_server = CallbackServer((callback_host, callback_port), _Callback)
        callback_thread = threading.Thread(target=callback_server.serve_forever, daemon=True)
        callback_thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=["--no-proxy-server"])
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
                    callback_host,
                    "--project",
                    "synthetic",
                    "--scope",
                    "memory:read",
                )
                response = page.reload(wait_until="networkidle")
                assert response is not None
                csp = response.header_value("content-security-policy")
                assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
                posts = []

                def delay_claim(route):
                    if route.request.method != "POST":
                        route.continue_()
                        return
                    posts.append(route.request.url)
                    claimed = route.fetch(max_redirects=0)
                    # Hold the first response while the browser makes the
                    # second click. The disabled button must prevent a POST.
                    time.sleep(0.4)
                    route.fulfill(response=claimed)

                if slow_double_submit:
                    page.route("**/oauth/pair/*", delay_claim)
                    page.evaluate("""() => {
                        const button = document.querySelector('button');
                        setTimeout(() => button.click(), 100);
                        button.click();
                    }""")
                else:
                    page.locator("button").click()
                page.wait_for_url(callback + "?**", timeout=10000)
                assert _Callback.received and _Callback.received.get("code")
                assert _Callback.received.get("state") == ["synthetic-state"]
                if slow_double_submit:
                    assert posts == [issuer + "/oauth/pair/" + request_id]
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
