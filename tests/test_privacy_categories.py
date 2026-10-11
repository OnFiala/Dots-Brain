"""Original category fixtures; no external audit corpus is embedded here."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dots_brain import privacy
from dots_brain.errors import InputError


@pytest.mark.parametrize(
    ("text", "fragments"),
    [
        ('service_password="cobalt[canvas](84)"', ("cobalt", "canvas")),
        ('passphrase: "paper canvas orbit lantern"', ("paper", "lantern")),
        ("<password>cobalt+paper+84</password>", ("cobalt", "paper")),
        ("<privateKey>ORIGINAL_SYNTHETIC_MATERIAL_84</privateKey>", ("ORIGINAL",)),
        ('sshpass -p "cobalt; canvas \\"paper\\" 84" command', ("cobalt", "canvas", "paper")),
        ("tool --secret-token='canvas orbit 84'", ("canvas", "orbit")),
        ("opaque+tcp://unit:cobalt%2B84@demo.test/resource", ("cobalt",)),
        ('password="credentialexamplematerial84"', ("credentialexamplematerial",)),
        ('password: change after review; service_password="copper87"', ("copper87",)),
        ('sshpass -p "copper"CANARY_87 command', ("copper", "CANARY_87")),
        ('sshpass -p "copper\nCANARY_87" command', ("copper", "CANARY_87")),
        ('service_password="copper\nCANARY_87"', ("copper", "CANARY_87")),
        ("curl --user=reader:copper87 https://demo.test", ("copper87",)),
        ("curl -ureader:copper87 https://demo.test", ("copper87",)),
        ("Cookie: session=copper87; Secure", ("copper87",)),
        ("curl -H 'X-Api-Key: copper87' https://demo.test", ("copper87",)),
    ],
)
def test_literal_category_values_are_rejected_and_completely_redacted(text, fragments):
    with pytest.raises(InputError, match="appears to contain a secret"):
        privacy.guard_content({"content": text})
    minimized = privacy.sanitize(text)
    assert minimized.redactions
    assert all(fragment not in minimized.value for fragment in fragments)
    repeated = privacy.sanitize(minimized.value)
    assert repeated.value == minimized.value
    assert repeated.redactions == 0


@pytest.mark.parametrize(
    "text",
    [
        "CACHE_PASSWORD=${CONFIG_PASSWORD}",
        "STORAGE_API_KEY=<replace-api-credential>",
        "tool --password=$CONFIG_PASSWORD",
        "https://demo.test/?token=%24%7BCONFIG_TOKEN%7D",
        'password = input("Password: ")',
        'password = secret_source["credential"]',
        "docker run -p 7070:70 demo.test/image",
        "key = mapping[index]",
        "SERVICE_PASSWORD=REPLACE_WITH_YOUR_PASSWORD",
    ],
)
def test_complete_placeholders_references_and_noncredential_flags_are_retained(text):
    assert privacy.guard_content({"content": text}) == {"content": text}
    assert privacy.sanitize(text).value == text


@pytest.mark.parametrize(
    "text",
    [
        "password: replace after maintenance",
        "password=unclassified",
        "https://demo.test/?session=staging",
        "Bearer documentationwordsequence",
        "token=AlphabeticCredentialValue",
        'signature="draft-87"',
        "auth=stage-87",
        "https://demo.test/?key=sample87",
        "opaque+tcp://sample87@demo.test/",
    ],
)
def test_uncertain_forms_are_retained_with_fixed_warning_metadata(text):
    mapping = {"content": text}
    assert privacy.guard_content(mapping) == mapping
    warnings = privacy.check_content(mapping)
    assert warnings
    assert all(set(item) == {"code", "category"} for item in warnings)
    assert all(item["code"] == "possible_secret" for item in warnings)
    assert all(item["category"] in privacy.CONTENT_WARNING_CATEGORIES for item in warnings)
    assert text not in json.dumps(warnings)
    assert privacy.sanitize(text).value != text


def test_quoted_literals_do_not_inherit_code_reference_exemptions():
    for value in ("source[CANARY_84]", "source(CANARY_84)"):
        with pytest.raises(InputError):
            privacy.guard_content({"content": f'password="{value}"'})
        assert "CANARY" not in privacy.sanitize(f'password="{value}"').value


@pytest.mark.parametrize("value", ["next_item", 84])
def test_low_information_structured_field_is_warned_not_rejected(value):
    record = {"token": value}
    assert privacy.guard_content(record) == record
    assert privacy.check_content(record) == [
        {"code": "possible_secret", "category": "sensitive_field"}
    ]
    assert privacy.sanitize(record).value == {"token": "[REDACTED]"}


@pytest.mark.parametrize(
    "text",
    [
        "archive.test:5544:box:reader:CANARY_MATERIAL_84",
        "machine archive.test\nlogin reader\npassword CANARY_MATERIAL_84",
    ],
)
def test_declarative_credential_file_fields_are_rejected_and_redacted(text):
    with pytest.raises(InputError):
        privacy.guard_content({"content": text})
    assert "CANARY_MATERIAL" not in privacy.sanitize(text).value


@pytest.mark.parametrize("tail", ["\\", '"\\', "[", "(", "'", '"'])
def test_malformed_reference_suffix_is_bounded_and_repeatable(tail):
    text = "password=source(" + tail
    result = privacy.sanitize(text)
    assert privacy.sanitize(result.value).value == result.value


def test_adjacent_and_mixed_representations_are_idempotent():
    parts = [
        'password="cobalt84"',
        "ghp_" + "Q7" * 14,
        "https://demo.test/?token=cobalt84",
        'tool --password="canvas84"',
        'sshpass -p "paper84" run',
        "PuTTY-User-Key-File-3: synthetic\nPrivate-Lines: 1\nSYNTHETIC84",
        "-----BEGIN PRIVATE KEY-----\nSYNTHETIC84\n-----END PRIVATE KEY-----",
    ]
    for delimiter in ("", " ", ";", "\n", ","):
        for left in parts:
            for right in parts:
                first = privacy.sanitize(left + delimiter + right)
                second = privacy.sanitize(first.value)
                assert second.value == first.value
                assert second.redactions == 0


@pytest.mark.parametrize(
    ("category", "prefix", "unit"),
    [
        ("assignment", "service_password=", 'item="'),
        ("xml", "<password>", "<password>"),
        ("pem", "-----BEGIN ", "-----BEGIN "),
        ("putty", "PuTTY-User-Key-File-", "PuTTY-User-Key-File-"),
        ("url", "opaque+tcp://", "field=%3C&"),
        ("known_token", "sk-proj-", "sk-proj-"),
        ("short_token_head", "sk-", "sk-"),
        ("segmented_token", "MTE", "MTE-"),
        ("bearer", "Bearer ", "Bearer "),
        ("bot_token", "987654:", "987654:"),
        ("prose", "heslo pro", "heslo pro "),
        ("command", "sshpass -p ", "sshpass -p "),
        ("flag", "--password=", "--password="),
        ("reference", "password=$", "password=source("),
        ("placeholder", "password=", "example"),
        ("credential_file", "archive.test:5544:box:reader:", "password "),
    ]
    + [
        ("token_format", prefix, prefix)
        for prefix in (
            "ghp_",
            "github_pat_",
            "glpat-",
            "sk_live_",
            "sk_test_",
            "sk_proj_",
            "sk-ant-",
            "rk_live_",
            "xai-",
            "hf_",
            "npm_",
            "GOCSPX-",
            "pypi-",
            "dop_v1_",
            "shpat_",
            "lin_api_",
            "ntn_",
            "SG.",
            "AKIA",
            "AIza",
            "xoxb-",
            "eyJ",
        )
    ],
    ids=lambda value: value,
)
def test_category_scans_bound_prefix_runs_and_repeated_units(tmp_path, category, prefix, unit):
    # Every locator/recognizer gets prefix + spaces, prefix + characters and a
    # repeated syntax unit, both with and without a failing suffix. Each family
    # has its own subprocess deadline, so one failure cannot hide the others.
    program = r"""
import json
import sys
import time
from dots_brain.privacy import sanitize
category, prefix, unit = sys.argv[1:]
maximum = 32_000
samples = []
for fill in (" ", "z", unit):
    for suffix in ("", "7"):
        text = prefix + (fill * maximum)[:maximum - len(prefix) - len(suffix)] + suffix
        started = time.process_time()
        sanitize(text, max_text=maximum)
        elapsed = time.process_time() - started
        assert elapsed < 0.5, (category, elapsed)
        samples.append(elapsed)
# Time ratios supplement the absolute deadline: a fast machine must not hide
# quadratic retries over overlapping token heads. Repeat within one process
# to amortize timer noise, and compare the same shape at 4k and 32k characters.
if category in {"known_token", "short_token_head", "segmented_token", "token_format"}:
    timings = []
    for size in (4_000, maximum):
        text = (unit * maximum)[:size]
        started = time.process_time()
        for _ in range(4):
            sanitize(text, max_text=maximum)
        timings.append(time.process_time() - started)
    assert timings[1] <= timings[0] * 20 + 0.01, (category, timings)
print(json.dumps(samples))
"""
    source = str(Path(privacy.__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-B", "-c", program, category, prefix, unit],
        cwd=tmp_path,
        env=os.environ | {"PYTHONPATH": source},
        capture_output=True,
        text=True,
        timeout=4,
    )
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)) == 6
