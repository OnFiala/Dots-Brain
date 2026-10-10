"""Deletion follows canonical IDs even when the derived index was damaged."""

import pytest

from dots_brain.errors import SuppressedError
from dots_brain.store import Store


@pytest.mark.parametrize("operation", ["forget", "update"])
@pytest.mark.parametrize("damage", ["missing", "wrong_row", "duplicate"])
def test_corrupt_fts_mapping_cannot_delete_unrelated_text_or_leave_old_text(
    tmp_path, operation, damage
):
    store = Store(tmp_path)
    store.initialize()
    original = dict(source="synthetic", account="test", event_id="delete", project="test")
    target = store.remember(content="UniqueDeleteCanary", **original)
    other = store.remember(
        content="UniquePreserveCanary",
        source="synthetic",
        account="test",
        event_id="keep",
        project="test",
    )
    with store.connection(write=True) as db:
        db.execute("DELETE FROM memory_fts_rows WHERE memory_id=?", (target["id"],))
        if damage == "wrong_row":
            row = db.execute(
                "SELECT rowid FROM memory_fts WHERE memory_id=?", (other["id"],)
            ).fetchone()[0]
            db.execute("DELETE FROM memory_fts_rows WHERE memory_id=?", (other["id"],))
            db.execute("INSERT INTO memory_fts_rows VALUES (?,?)", (target["id"], row))
        elif damage == "duplicate":
            db.execute(
                "INSERT INTO memory_fts(memory_id,title,content) VALUES (?,'',?)",
                (target["id"], "DuplicateDeleteCanary"),
            )
    if operation == "forget":
        assert store.forget(target["id"], expected_revision=1)["deleted"]
        with pytest.raises(SuppressedError):
            store.remember(content="Replay", **original)
    else:
        assert (
            store.remember(content="NewRevision", expected_revision=1, **original)["revision"] == 2
        )
    assert store.search("UniquePreserveCanary")[0]["id"] == other["id"]
    with store.connection() as db:
        assert not db.execute(
            "SELECT 1 FROM memory_fts WHERE content LIKE '%DeleteCanary%'"
        ).fetchone()
        if operation == "forget":
            assert not db.execute(
                "SELECT 1 FROM memory_fts WHERE memory_id=?", (target["id"],)
            ).fetchone()
