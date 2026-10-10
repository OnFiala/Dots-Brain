"""Run installed-artifact acceptance against disposable historical databases.

Invoke with the wheel environment's Python. This script refuses source imports.
It uses only the checked-in synthetic fixtures; no host configuration is read.
"""

import json
import shutil
import tempfile
from pathlib import Path

import dots_brain
from dots_brain.auth import authenticate
from dots_brain.errors import SuppressedError
from dots_brain.oauth import OAuthStore
from dots_brain.operations import backup_store, migrate, restore_store
from dots_brain.store import Store


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    root = Path(__file__).resolve().parents[1]
    require(
        not Path(dots_brain.__file__).resolve().is_relative_to(root), "Imported source, not wheel"
    )
    fixtures = sorted((root / "tests/fixtures/historical").glob("v*-*"))
    require(
        len(fixtures) == 2
        and {
            json.loads((path / "manifest.json").read_text())["schema_version"] for path in fixtures
        }
        == {1, 2},
        "Both historical schema fixtures are required",
    )
    with tempfile.TemporaryDirectory(prefix="dots-wheel-") as name:
        directory = Path(name)
        for fixture in fixtures:
            target = directory / fixture.name
            shutil.copytree(fixture, target)
            manifest = json.loads((target / "manifest.json").read_text())
            store = Store(target)
            result = migrate(
                store,
                apply=True,
                writers_stopped=True,
                backup=directory / (fixture.name + ".sqlite3"),
            )
            require(result["state"] == "migrated", "Historical migration did not finish")
            record = store.get(manifest["memory_id"])
            require(record["revision"] == 2, "Historical revisions were lost")
            require(
                authenticate(store, "public-synthetic-upgrade-probe") is not None,
                "Static grant lost",
            )
            require(
                OAuthStore(store).grants()["grants"][0]["id"] == "fixture-grant", "OAuth grant lost"
            )
            arguments = dict(
                content="Synthetic wheel acceptance", source="wheel", account="test", event_id="one"
            )
            saved = store.remember(**arguments)
            updated = store.remember(
                **(arguments | {"content": "Synthetic wheel revision"}), expected_revision=1
            )
            require(updated["revision"] == 2, "Wheel update failed")
            require(
                store.forget(saved["id"], expected_revision=2)["deleted"], "Wheel deletion failed"
            )
            try:
                store.remember(**arguments)
            except SuppressedError:
                pass
            else:
                raise RuntimeError("Forgotten source was reimported")
            snapshot = directory / (fixture.name + "-current.sqlite3")
            backup_store(store, snapshot)
            restored = Store(directory / (fixture.name + "-restored"))
            require(
                restore_store(snapshot, restored, latest_deletions=store)["state"]
                == "restored_disabled",
                "Restore failed",
            )
            require(restored.get(record["id"])["revision"] == 2, "Restore lost original record")
    print(
        "Installed wheel: v1/v2 migration, retained grants, CRUD, suppression and restore passed."
    )


if __name__ == "__main__":
    main()
