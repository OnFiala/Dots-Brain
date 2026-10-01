"""Validate release identity and local documentation links without network access."""

import json
import re
import tomllib
from pathlib import Path

from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]


def main():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    plugin = json.loads((ROOT / "plugin.json").read_text())
    assert Version(project["version"]) == Version(plugin["version"]), "Version mismatch"
    assert plugin["name"] == project["name"] == "dots-brain"
    interface = plugin["extensions"]["com.openai"]["interface"]
    assert len(interface["shortDescription"]) <= 30
    skill = plugin["extensions"]["com.openai"]["onboardingSkill"]
    assert (ROOT / skill).is_file(), "Missing onboarding skill"
    assert (ROOT / "LICENSE").read_text().startswith("MIT License\n")
    for document in [ROOT / "README.md", *ROOT.glob("docs/*.md")]:
        for target in re.findall(r"\]\(([^)]+)\)", document.read_text()):
            if "://" in target or target.startswith("#"):
                continue
            target = target.split("#", 1)[0]
            assert (document.parent / target).exists(), f"Broken link in {document.name}: {target}"
    print("Release identity, plugin metadata, license, and documentation links are valid.")


if __name__ == "__main__":
    main()
