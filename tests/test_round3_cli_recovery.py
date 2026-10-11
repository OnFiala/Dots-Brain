"""A blocked CLI probe has a concrete, local recovery path with honest status."""

import json
import shlex
import subprocess
import sys


def command(arguments):
    return subprocess.run(
        [sys.executable, "-m", "dots_brain.cli", *arguments],
        text=True,
        capture_output=True,
        timeout=10,
    )


def test_probe_cli_refusal_has_recover_then_explicit_archive_path(tmp_path):
    credential = tmp_path / "connection with space.json"
    # Invalid connection data makes an accidental connection attempt observable.
    credential.write_text('{"version":999}\n')
    receipt = credential.with_name(credential.name + ".probe-recovery.json")
    original = json.dumps(
        {
            "version": 1,
            "state": "sending",
            "project": "test",
            "source": "dots-brain-probe",
            "account": "connection-verifier",
            "event_id": "connection-probe-" + "d" * 32,
        }
    ).encode()
    receipt.write_bytes(original)
    blocked = command(["verify", "--credential-file", str(credential), "--write"])
    assert blocked.returncode == 1
    error = json.loads(blocked.stderr)
    recovery = shlex.split(error["message"].split("Next command: ", 1)[1])
    assert recovery[0] == "dots-brain"
    inspected = command(recovery[1:])
    assert inspected.returncode == 1
    result = json.loads(inspected.stdout)
    assert result["state"] == "probe_recovery_needed"
    assert receipt.read_bytes() == original
    archived = command(shlex.split(result["archive_command"])[1:])
    assert archived.returncode == 0, archived.stderr
    result = json.loads(archived.stdout)
    assert result["state"] == "probe_receipt_archived"
    assert result["probe_removed"] == result["remaining_probe"] == "unknown"
    assert not receipt.exists()
    archive = tmp_path / result["archive"]
    assert archive.read_bytes() == original
    assert archive.stat().st_mode & 0o777 == 0o600


def test_partial_cli_probe_target_is_rejected_with_next_command(tmp_path):
    result = command(
        [
            "verify",
            "--credential-file",
            str(tmp_path / "absent.json"),
            "--recover-probe",
            "--probe-memory-id",
            "unpaired-target",
        ]
    )
    assert result.returncode == 1
    assert "Next command: dots-brain verify" in json.loads(result.stderr)["message"]
