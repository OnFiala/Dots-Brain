"""Install the locked alpha on a writable, authorized Linux memory host."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


class StepFailed(Exception):
    def __init__(self, step, result):
        self.step, self.result = step, result


def run_step(step, command, *, cwd, environment, structured=True):
    """Keep the failing phase and the CLI's safe JSON diagnosis visible."""
    try:
        result = subprocess.run(
            command, cwd=cwd, env=environment, capture_output=True, text=True, check=False
        )
    except OSError:
        raise StepFailed(step, {"code": "command_unavailable"}) from None
    payload = None
    if structured:
        for output in (result.stdout, result.stderr):
            try:
                payload = json.loads(output)
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict):
                break
            payload = None
    if result.returncode:
        raise StepFailed(step, payload or {"code": "command_failed"})
    if structured and payload is None:
        raise StepFailed(step, {"code": "invalid_command_response"})
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--semantic", action="store_true")
    parser.add_argument("--port", type=int, help="Keep the saved port unless explicitly set.")
    parser.add_argument("--connect", choices=("claude-code", "cursor", "codex", "mcp-json"))
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    if args.config and not args.connect:
        parser.error("--config requires --connect")
    if args.connect == "mcp-json" and not args.config:
        parser.error("--connect mcp-json requires --config")
    if args.port is not None and not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    root = Path(__file__).resolve().parents[1]
    uv = shutil.which("uv")
    if sys.platform != "linux" or sys.version_info < (3, 11) or uv is None:
        print(
            json.dumps({"state": "blocked", "reason": "Linux, Python 3.11+ and uv are required."})
        )
        return 1
    environment = dict(os.environ)
    environment.setdefault("UV_CACHE_DIR", str(root / ".cache" / "uv"))

    def step(name, command, *, structured=True):
        return run_step(name, command, cwd=root, environment=environment, structured=structured)

    try:
        sync = [uv, "sync", "--locked", "--no-dev"]
        if args.semantic:
            sync += ["--extra", "semantic"]
        step("dependencies", sync, structured=False)
        executable = root / ".venv/bin/python"
        base = [str(executable), "-m", "dots_brain.cli", "--data-dir", str(args.data_dir.resolve())]
        setup = step(
            "setup", base + ["doctor" if (args.data_dir / "brain.sqlite3").exists() else "setup"]
        )
        if args.semantic:
            step("model", base + ["model", "prepare"])
        start = base + ["up", "--resume"]
        if args.port is not None:
            start += ["--port", str(args.port)]
        if args.semantic:
            start.append("--semantic")
        service = step("service", start)
        connected = None
        if args.connect:
            command = base + ["connect", args.connect]
            if args.config:
                command += ["--config", str(args.config)]
            connected = step("client", command)
    except StepFailed as exc:
        print(json.dumps({"state": "blocked", "step": exc.step, "result": exc.result}))
        return 1
    print(
        json.dumps(
            {
                "state": "configured_verified_bridge" if connected else "verified_local_service",
                "setup": setup,
                "python": str(executable),
                "semantic_model_prepared": args.semantic,
                "service": service,
                "connection": connected,
                "remote_connection": "not_verified",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
