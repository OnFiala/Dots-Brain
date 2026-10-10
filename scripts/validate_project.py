"""Validate release identity, immutable Actions, and repository links without network access."""

from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACTION_PIN = re.compile(r"[\w./-]+@[0-9a-f]{40}$")
VERSION = re.compile(r"\d+\.\d+\.\d+(?:a\d+|b\d+|rc\d+)?$")


class ValidationError(Exception):
    """A project contract is not satisfied."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def runtime_version() -> str:
    module = ast.parse((ROOT / "src/dots_brain/__init__.py").read_text(encoding="utf-8"))
    for statement in module.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in statement.targets
        ):
            continue
        value = ast.literal_eval(statement.value)
        require(isinstance(value, str), "Runtime version must be a string")
        return value
    raise ValidationError("Runtime version is missing")


def validate() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    plugin = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    version = project["version"]
    require(
        isinstance(version, str) and VERSION.fullmatch(version) is not None,
        "Invalid project version",
    )
    plugin_version = version.replace("a", "-alpha.").replace("b", "-beta.").replace("rc", "-rc.")
    require(plugin.get("version") == plugin_version, "Plugin version mismatch")
    require(runtime_version() == version, "Runtime version mismatch")
    require(plugin.get("name") == project.get("name") == "dots-brain", "Project name mismatch")
    interface = plugin.get("extensions", {}).get("com.openai", {}).get("interface", {})
    require(
        len(interface.get("shortDescription", "")) <= 30,
        "Plugin short description exceeds 30 characters",
    )
    skill = plugin.get("extensions", {}).get("com.openai", {}).get("onboardingSkill")
    require(isinstance(skill, str) and (ROOT / skill).is_file(), "Missing onboarding skill")
    require(
        (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License\n"),
        "Invalid license",
    )

    workflows = ROOT / ".github/workflows"
    for workflow in (*workflows.glob("*.yml"), *workflows.glob("*.yaml")):
        for action in re.findall(r"\buses:\s+(\S+)", workflow.read_text(encoding="utf-8")):
            require(
                ACTION_PIN.fullmatch(action) is not None,
                f"Action must use a full commit hash in {workflow.name}: {action}",
            )

    documents = [*ROOT.glob("*.md"), *ROOT.glob("docs/**/*.md"), *ROOT.glob("skills/**/*.md")]
    for document in documents:
        for target in re.findall(r"\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
            if "://" in target or target.startswith("#"):
                continue
            target = target.split("#", 1)[0]
            resolved = (document.parent / target).resolve()
            require(resolved.is_relative_to(ROOT), f"Link leaves repository: {document.name}")
            require(resolved.exists(), f"Broken link in {document.name}: {target}")


def main() -> int:
    try:
        validate()
    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        tomllib.TOMLDecodeError,
        ValidationError,
    ) as error:
        print(f"project validation failed: {error}")
        return 1
    print("Release identity, Action pins, plugin metadata, license, and links are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
