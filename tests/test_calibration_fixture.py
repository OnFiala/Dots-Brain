"""Keep the reviewed public fixture separate from private evaluation material."""

import hashlib
import json
from pathlib import Path


def test_calibration_fixture_is_the_reviewed_neutral_synthetic_dataset():
    path = Path(__file__).parent / "fixtures" / "semantic_calibration.json"
    raw = path.read_bytes()
    # Check bytes before parsing: an accidental old/private fixture must not be
    # printed in assertion introspection. New datasets need a content review.
    assert hashlib.sha256(raw).hexdigest() == (
        "6e08b8d263a5fa4eac520078728a04a4f2b38a74f71c6ffc89409115f104d667"
    ), "The fixture differs from the reviewed neutral dataset."
    corpus = json.loads(raw)
    provenance = corpus["provenance"]
    assert provenance["kind"] == "repository-authored-synthetic"
    assert provenance["dataset_id"] == "neutral-objects-v1"
    assert provenance["version"] == 1
    assert len(corpus["facts"]) == 24
    assert all(len(row) == 8 for row in corpus["facts"])
    assert len(corpus["ood"]) == 24
    assert set(corpus["filler"]) == {"en", "en2", "cz", "cz2"}
