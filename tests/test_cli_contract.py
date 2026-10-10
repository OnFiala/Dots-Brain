"""CLI exit codes used by shell clients and scheduled collectors."""

import json

import pytest

from dots_brain import cli


@pytest.mark.parametrize(
    "state",
    ["verification_failed", "blocked", "partial", "capture_partial", "capture_recovery_required"],
)
def test_incomplete_cli_results_exit_nonzero(monkeypatch, capsys, state):
    monkeypatch.setattr("sys.argv", ["dots-brain", "doctor"])
    monkeypatch.setattr(cli, "run", lambda args: {"state": state})
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 1
    assert json.loads(capsys.readouterr().out)["state"] == state


def test_successful_cli_result_exits_normally(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["dots-brain", "doctor"])
    monkeypatch.setattr(cli, "run", lambda args: {"state": "verified_read"})
    cli.main()
    assert json.loads(capsys.readouterr().out)["state"] == "verified_read"
