"""Check the shipped gateway syntax without starting a listener or touching /etc."""

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nginx", default=shutil.which("nginx"))
    args = parser.parse_args()
    if not args.nginx:
        parser.error("nginx is required for the deployment syntax check")
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="dots-brain-nginx-") as name:
        directory = Path(name)
        (directory / "runtime").mkdir()
        config = (root / "deploy/nginx/dots-brain.conf").read_text(encoding="utf-8")
        config = config.replace("/run/dots-brain-ingress", str(directory / "runtime"))
        config = config.replace("/run/dots-brain-onboarding", str(directory / "onboarding"))
        path = directory / "nginx.conf"
        path.write_text(config, encoding="utf-8")
        subprocess.run([args.nginx, "-t", "-q", "-p", name, "-c", str(path)], check=True)
    print("Gateway syntax verified; no service was started.")


if __name__ == "__main__":
    main()
