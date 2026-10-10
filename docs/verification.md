# Verification boundaries

Source tests, built artifacts, installed runtime, and client behavior are distinct
claims. CI validates the source tree on Python 3.11, 3.12, and 3.13 with disposable
stores. It does not establish a service on a particular host.

CI also tests the OAuth callback in Chromium, checks nginx and systemd syntax,
and runs migration, CRUD, suppression and restore through an installed wheel
outside the checkout. The semantic job explicitly downloads and verifies the
pinned model before its tests. Ordinary tests never download model files.

To run the real-model tests locally, first prepare the model in a disposable
directory, then point the test suite at that model directory:

```sh
uv sync --locked --extra semantic
uv run dots-brain --data-dir /absolute/private/test-memory model prepare
BRAIN_TEST_MODEL_DIR=/absolute/private/test-memory/models/REVISION uv run pytest -q tests/test_semantic.py tests/test_semantic_regressions.py
```

Replace `REVISION` with the directory returned by `model prepare`. For browser
acceptance, install the separate test group and Chromium:

```sh
uv sync --locked --group browser
uv run playwright install chromium
uv run pytest -q tests/test_oauth_browser.py
```

For a deployment, record the exact artifact, data directory, supervisor state,
backup validation, and direct read/write behavior from each intended client. Keep
that evidence private. Do not publish credentials, addresses, client IDs, or memory
contents.

The following conditions need direct acceptance evidence and remain outside a
source-only pass: public OAuth route, provider UI activation, continuous capture,
CORTEX upstream connection, host reboot, off-host recovery, relevance on personal data,
and sustained capacity.
