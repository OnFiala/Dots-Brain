"""Regression checks for repository-level release and documentation contracts."""

from __future__ import annotations

import importlib.util
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_validator():
    spec = importlib.util.spec_from_file_location(
        "validate_project", ROOT / "scripts/validate_project.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_validator_rejects_a_version_mismatch_with_optimized_python(tmp_path: Path):
    candidate = tmp_path / "candidate"
    (candidate / "src/dots_brain").mkdir(parents=True)
    (candidate / ".github/workflows").mkdir(parents=True)
    (candidate / "skills/setup").mkdir(parents=True)
    (candidate / "pyproject.toml").write_text(
        '[project]\nname = "dots-brain"\nversion = "0.4.0a2"\n'
    )
    plugin = {
        "name": "dots-brain",
        "version": "0.4.0-alpha.1",
        "extensions": {
            "com.openai": {
                "onboardingSkill": "skills/setup/SKILL.md",
                "interface": {"shortDescription": "short"},
            }
        },
    }
    (candidate / "plugin.json").write_text(json.dumps(plugin))
    (candidate / "src/dots_brain/__init__.py").write_text('__version__ = "0.4.0a2"\n')
    (candidate / "skills/setup/SKILL.md").write_text("# Setup\n")
    (candidate / "LICENSE").write_text("MIT License\n")
    (candidate / ".github/workflows/ci.yml").write_text("name: CI\n")
    (candidate / "scripts").mkdir()
    validator_path = candidate / "scripts/validate_project.py"
    shutil.copy(ROOT / "scripts/validate_project.py", validator_path)

    completed = subprocess.run(
        [sys.executable, "-O", str(validator_path)],
        cwd=candidate,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "Plugin version mismatch" in completed.stdout


def test_project_validator_accepts_current_tree():
    validator = load_validator()
    validator.validate()


@pytest.mark.parametrize(
    "document",
    [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md")), ROOT / "skills/setup/SKILL.md"],
)
def test_documented_dots_brain_commands_parse(document: Path):
    """Every fenced command beginning with dots-brain stays aligned with the CLI parser."""
    from dots_brain.cli import parser

    fenced = False
    for line in document.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            continue
        command = line.strip()
        if command.startswith("uv run dots-brain "):
            command = command.removeprefix("uv run ")
        elif not command.startswith("dots-brain "):
            continue
        command = command.replace("/absolute/private/memory", "/tmp/memory")
        command = command.replace("/absolute/path/to/personal-memory", "/tmp/memory")
        command = command.replace("/private/current", "/tmp/current")
        command = command.replace("/private/restored", "/tmp/restored")
        command = command.replace("/private/backups/snapshot.sqlite3", "/tmp/backup.sqlite3")
        command = command.replace("<", "example-").replace(">", "")
        arguments = shlex.split(command)[1:]
        try:
            parser().parse_args(arguments)
        except SystemExit as error:
            pytest.fail(f"{document.relative_to(ROOT)} has an invalid command {line!r}: {error}")
