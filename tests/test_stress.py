import runpy
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "linux", reason="Exercises the Linux process lifecycle.")
def test_bounded_stress_cleans_up_its_winning_revision():
    script = Path(__file__).resolve().parents[1] / "scripts" / "stress.py"
    report = runpy.run_path(str(script))["run"](records=100, workers=2, http_calls=4)
    assert report["state"] == "passed"
    assert report["conflict_winners"] == 1
    assert report["http_write_read_pairs"]["count"] == 4
    assert report["remaining_memories"] == 4
    assert report["suppressed_sources"] == 100
