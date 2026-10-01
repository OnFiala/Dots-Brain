"""Install the locked alpha on a writable, authorized memory host."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--semantic", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    uv = shutil.which("uv")
    if sys.version_info < (3, 11) or uv is None:
        print(json.dumps({"state": "blocked", "reason": "Python 3.11+ and uv are required."}))
        return 1
    environment = dict(os.environ)
    environment.setdefault("UV_CACHE_DIR", str(root / ".cache" / "uv"))
    sync = [uv, "sync", "--frozen", "--no-dev"]
    if args.semantic:
        sync += ["--extra", "semantic"]
    subprocess.run(sync, cwd=root, env=environment, check=True, stdout=sys.stderr)
    executable = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    base = [str(executable), "-m", "dots_brain.cli", "--data-dir", str(args.data_dir)]
    setup = subprocess.run(base + ["setup"], check=True, capture_output=True, text=True)
    if args.semantic:
        subprocess.run(base + ["model", "prepare"], check=True, stdout=sys.stderr)
    print(
        json.dumps(
            {
                "state": "local_ready",
                "setup": json.loads(setup.stdout),
                "python": str(executable),
                "semantic_model_prepared": args.semantic,
                "service_running": False,
                "remote_connection": "not_verified",
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError):
        print(json.dumps({"state": "blocked", "reason": "Installation command failed."}))
        raise SystemExit(1) from None
