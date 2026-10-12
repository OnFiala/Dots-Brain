"""Install the locked alpha on a writable, authorized memory host."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(
            2,
            json.dumps(
                {
                    "state": "blocked",
                    "phase": "arguments",
                    "reason": "Invalid bootstrap arguments.",
                    "hint": "Run bootstrap.py --help for valid options.",
                }
            )
            + "\n",
        )


def step(phase, command, *, states=(), **kwargs):
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, **kwargs)
        if states:
            document = json.loads(result.stdout)
            if (
                not isinstance(document, dict)
                or not isinstance(document.get("state"), str)
                or document["state"] not in states
            ):
                raise ValueError("Unexpected command result.")
            return document
        return None
    except (OSError, subprocess.SubprocessError, ValueError):
        print(
            json.dumps({"state": "blocked", "phase": phase, "reason": "Installation step failed."})
        )
        raise SystemExit(1) from None


def main():
    parser = Parser(prog="bootstrap.py", description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--semantic", action="store_true")
    parser.add_argument("--connect", choices=["claude-code", "cursor", "codex", "mcp-json"])
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    if (args.config and not args.connect) or (args.connect == "mcp-json" and not args.config):
        parser.error("--config requires --connect; mcp-json requires --config.")
    root = Path(__file__).resolve().parents[1]
    uv = shutil.which("uv")
    if sys.platform != "linux" or sys.version_info < (3, 11) or uv is None:
        print(
            json.dumps(
                {
                    "state": "blocked",
                    "phase": "prerequisites",
                    "reason": "Bootstrap requires Linux, Python 3.11+ and uv.",
                }
            )
        )
        return 1
    environment = dict(os.environ)
    environment.setdefault("UV_CACHE_DIR", str(root / ".cache" / "uv"))
    sync = [uv, "sync", "--locked", "--no-dev"]
    if args.semantic:
        sync += ["--extra", "semantic"]
    step("dependencies", sync, cwd=root, env=environment)
    executable = root / ".venv" / "bin/python"
    base = [str(executable), "-m", "dots_brain.cli", "--data-dir", str(args.data_dir)]
    setup = step("setup", base + ["setup"], states={"local_ready", "disabled"})
    if args.semantic:
        step("model", base + ["model", "prepare"])
    start = base + ["up", "--resume"] + (["--semantic"] if args.semantic else [])
    service = step("service", start, states={"verified_local_service"})
    connected = None
    if args.connect:
        command = base + ["connect", args.connect]
        if args.config:
            command += ["--config", str(args.config)]
        connected = step("client", command, states={"configured_verified_bridge"})
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
