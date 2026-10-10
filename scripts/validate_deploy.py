"""Check the shipped gateway and service syntax without starting a service."""

import argparse
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nginx", default=shutil.which("nginx"))
    parser.add_argument("--systemd-analyze", default=shutil.which("systemd-analyze"))
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
        # Some nginx builds bind listeners even during -t. A unique Unix socket
        # validates the listener syntax without requiring the example host IP.
        config = re.sub(r"listen [^;]+;", f"listen unix:{directory}/check.sock;", config)
        path = directory / "nginx.conf"
        path.write_text(config, encoding="utf-8")
        subprocess.run([args.nginx, "-t", "-q", "-p", name, "-c", str(path)], check=True)
        if args.systemd_analyze:
            units = directory / "units"
            units.mkdir()
            paths = []
            for template in sorted((root / "deploy/systemd").glob("*.service")):
                content = template.read_text(encoding="utf-8")
                # Syntax validation does not install the application executable.
                content = content.replace(
                    "/opt/dots-brain/current/.venv/bin/dots-brain", "/bin/true"
                )
                output = units / template.name
                output.write_text(content, encoding="utf-8")
                paths.append(str(output))
            (units / "docker.service").write_text("[Service]\nExecStart=/bin/true\n")
            subprocess.run(
                [args.systemd_analyze, "verify", "--man=no", "--generators=no", *paths],
                env={**os.environ, "SYSTEMD_UNIT_PATH": str(units) + ":"},
                check=True,
            )
    print("Deployment syntax verified; no service was started.")


if __name__ == "__main__":
    main()
