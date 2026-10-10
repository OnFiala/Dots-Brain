import os
import subprocess
import sys

import pytest

from dots_brain.errors import InputError
from dots_brain.privacy import guard_content, sanitize


@pytest.mark.parametrize(
    "text",
    [
        "Basic configuration is documented in the deployment guide.",
        "The password is stored by the operating system keychain.",
        "session = requests.Session()",
        "docker run -u 1000:1000 example.test/worker",
        "Token budget is 40 points for this sprint.",
        'password = getpass.getpass("Password: ")',
        'token = os.environ["DOTS_TOKEN"]',
        'curl -u "$API_USER:$API_PASS" "$BASE_URL/health"',
        "self.token = token\nself.password = password",
    ],
)
def test_guard_keeps_benign_auth_and_configuration_prose(text):
    assert guard_content({"content": text}) == {"content": text}


@pytest.mark.parametrize(
    "text",
    [
        "DB_PASSWORD=correct-horse-battery-staple",
        '"clientSecret": "synthetic-secret-value"',
        "curl --user operator:synthetic-password https://example.test",
        "https://example.test/callback?code=synthetic-oauth-code",
    ],
)
def test_guard_rejects_sensitive_assignment_and_url_forms(text):
    with pytest.raises(InputError, match="appears to contain a secret"):
        guard_content({"content": text})


def test_sanitize_preserves_diagnostic_fields_but_not_secret_shaped_values():
    diagnostics = {
        "status_code": 503,
        "error_code": "connection_timeout",
        "exit_code": 1,
        "code": "E_UPSTREAM_TIMEOUT",
        "session": "capture-42",
    }
    assert sanitize(diagnostics).value == diagnostics
    assert sanitize({"error_code": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"}).value == {
        "error_code": "[REDACTED_TOKEN]"
    }


@pytest.mark.parametrize(
    "text",
    [
        "password=CorrectHorseBatteryStaple",
        "token=AlphabeticCredentialValue",
        'self.password = "SyntheticLiteralPassword"',
    ],
)
def test_explicit_alphabetic_assignments_are_not_treated_as_code_references(text):
    with pytest.raises(InputError, match="appears to contain a secret"):
        guard_content({"content": text})


def test_sensitive_field_marker_is_idempotent_without_new_redactions():
    first = sanitize({"details": {"password": "synthetic-secret"}})
    second = sanitize(first.value)
    assert second.value == first.value
    assert second.redactions == 0
    assert second.categories == {}


@pytest.mark.parametrize("field", ["title", "source_uri"])
def test_guard_names_only_known_field_and_category_on_rejection(field):
    value = "https://example.test/callback?token=PRIVATE_CANARY"
    with pytest.raises(InputError) as rejected:
        guard_content({field: value})
    message = str(rejected.value)
    assert f"Fields: {field}; categories:" in message
    assert "PRIVATE_CANARY" not in message
    with pytest.raises(InputError) as rejected:
        guard_content({"PRIVATE_FIELD_CANARY": value})
    assert "PRIVATE_FIELD_CANARY" not in str(rejected.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/?token=[REDACTED]",
        "https://example.test/?code=synthetic-oauth-code&token=[REDACTED]",
        "https://example.test/?state=ok&access_token=synthetic-token-value",
    ],
)
def test_url_redaction_is_idempotent(url):
    first = sanitize(url).value
    assert "synthetic" not in first
    assert sanitize(first).value == first


def test_sanitization_summary_only_allows_numeric_known_categories():
    value = {
        "sanitization": {
            "categories": {"token": 1, "secret": "must-not-survive"},
            "truncated": {"text_chars": 2},
            "unexpected": {"password": "must-not-survive"},
        }
    }
    result = sanitize(value).value["sanitization"]
    assert result["categories"] == {"token": 1, "secret": "[REDACTED]"}
    assert result["truncated"] == {"text_chars": 2}
    assert result["unexpected"]["password"] == "[REDACTED]"
    assert sanitize({"sanitization": result}).value["sanitization"] == result


def test_maximum_length_adversarial_input_finishes_in_a_child_process():
    environment = os.environ | {"PYTHONPATH": "src"}
    program = """
from dots_brain.privacy import guard_content
for length in (2_000, 4_096, 24_000, 32_000):
    assert guard_content({"content": ("a_" * length)[:length]})["content"]
"""
    subprocess.run(
        [sys.executable, "-c", program],
        cwd=os.fspath(__file__).rsplit("/tests/", 1)[0],
        env=environment,
        check=True,
        timeout=3,
    )


def test_private_key_and_czech_prefix_scans_have_a_linear_watchdog():
    environment = os.environ | {"PYTHONPATH": "src"}
    program = """
import time
from dots_brain.privacy import sanitize
for shape in ("BEGIN ", "heslo k "):
    started = time.perf_counter()
    sanitize((shape * 32_000)[:32_000], max_text=32_000)
    assert time.perf_counter() - started < 0.5
"""
    subprocess.run(
        [sys.executable, "-c", program],
        cwd=os.fspath(__file__).rsplit("/tests/", 1)[0],
        env=environment,
        check=True,
        timeout=3,
    )
