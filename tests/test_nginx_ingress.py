"""Exercise request buffering through the real Linux gateway with synthetic data."""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

NGINX = shutil.which("nginx")
pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or NGINX is None or os.geteuid() == 0,
    reason="Requires Linux nginx and an unprivileged test user",
)


@pytest.mark.parametrize("body_writable", [True, False])
def test_gateway_buffers_large_json_as_service_user(tmp_path, free_ports, body_writable):
    received = []

    class Backend(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"mock_backend_unauthorized"}')

        def log_message(self, *_args):
            pass

    backend = HTTPServer(("127.0.0.1", 0), Backend)
    worker = threading.Thread(target=backend.serve_forever, daemon=True)
    worker.start()
    runtime = tmp_path / "nginx"
    runtime.mkdir()
    body_directory = runtime / "body"
    body_directory.mkdir(mode=0o700 if body_writable else 0o500)
    config = tmp_path / "ingress.conf"
    errors = tmp_path / "nginx.log"
    port = free_ports[0]
    config.write_text(
        (Path(__file__).resolve().parents[1] / "deploy/nginx/dots-brain.conf")
        .read_text()
        .replace("/run/dots-brain-ingress", str(runtime))
        .replace("error_log /dev/null;", f"error_log {errors} warn;")
        .replace("http {", "http {\n    client_body_buffer_size 8k;")
        .replace("192.0.2.1:8788", f"127.0.0.1:{port}")
        .replace("allow 192.0.2.2;", "allow 127.0.0.1;")
        .replace("127.0.0.1:8787", f"127.0.0.1:{backend.server_port}")
    )
    process = None
    try:
        # Validation and runtime inherit this same non-root UID; no sudo/chown.
        subprocess.run(
            [NGINX, "-t", "-q", "-e", "stderr", "-c", str(config)],
            check=True,
            timeout=10,
        )
        process = subprocess.Popen(
            [NGINX, "-e", "stderr", "-c", str(config), "-g", "daemon off;"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        with httpx.Client(
            trust_env=False, timeout=3, headers={"Host": "memory.example.com"}
        ) as client:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    client.get(f"http://127.0.0.1:{port}/")
                    break
                except httpx.TransportError:
                    time.sleep(0.05)
            else:
                pytest.fail("Disposable nginx failed to become ready")
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "padding": "x" * (32 * 1024)}).encode()
            response = client.post(
                f"http://127.0.0.1:{port}/mcp",
                content=body,
                headers={"Content-Type": "application/json"},
            )
        if body_writable:
            assert response.status_code == 401
            assert response.json() == {"error": "mock_backend_unauthorized"}
            assert received == [body]
            assert "a client request body is buffered to a temporary file" in errors.read_text()
        else:
            assert response.status_code == 500
            assert received == []
            assert "Permission denied" in errors.read_text()
    finally:
        if process is not None:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
        backend.shutdown()
        backend.server_close()
        worker.join(timeout=5)
        body_directory.chmod(0o700)
