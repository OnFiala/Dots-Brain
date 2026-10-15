# Verification

Source checks, an installed wheel, live protocol calls and assistant behavior
are separate results. Record the exact commit, artifact hashes, Python/SQLite
versions, commands, failures and skips for each candidate. Keep private runtime
and recovery evidence outside the repository; old test counts do not verify a
new artifact.

## Source checks

From a clean checkout of the reviewed commit:

```sh
uv sync --locked --all-extras
uv run ruff check src tests scripts modules
uv run ruff format --check src tests scripts modules
uv run pytest -q
uv run python scripts/validate_project.py
uv build
```

Tests use disposable synthetic stores and isolate client configuration from the
operator's home. Repository tests include retained module characterizations;
core distribution tests do not require those modules. Ordinary tests do not
download a model. Set `BRAIN_TEST_MODEL_DIR` to explicitly prepared, verified
model files to include the real CPU embedding smoke test.

[CI](../.github/workflows/ci.yml) runs Python 3.11 and 3.12 on Linux. Its service
and nginx checks validate temporary template copies, not running units or TLS.
Inspect results for the exact commit. A local macOS run can skip Linux lifecycle
cases; a skipped or simulated check is not evidence of that behavior.

## Installed wheel

Build from that same checkout. Select one exact wheel and install only the locked
core dependencies into a fresh environment; do not use the editable source
installation. These commands may download dependencies and a Python interpreter.
Use `--offline` only when the required artifacts are already cached.

```sh
CHECK_ROOT=$(mktemp -d)
WHEEL=/absolute/path/to/the/exact/dots_brain-VERSION-py3-none-any.whl
uv export --locked --no-dev --no-emit-project --format requirements.txt --output-file "$CHECK_ROOT/core-requirements.txt"
uv venv --python 3.11 "$CHECK_ROOT/venv"
uv pip sync --python "$CHECK_ROOT/venv/bin/python" --require-hashes "$CHECK_ROOT/core-requirements.txt"
uv pip install --python "$CHECK_ROOT/venv/bin/python" --no-deps "$WHEEL"
env -u PYTHONPATH "$CHECK_ROOT/venv/bin/python" scripts/verify_wheel.py
```

Repeat for each supported Python version being accepted. Preserve the wheel hash
and the helper's result, then remove the disposable environment when no longer
needed. [The helper](../scripts/verify_wheel.py) refuses an import from the source
checkout. It checks the hashed released-v1 fixture, migration, revisions, static
grants, OAuth history, deletion suppression, staged restore and real installed
stdio MCP read/write. It does not verify managed Linux startup, public OAuth or
an assistant's conversation integration. Core artifacts exclude capture and
CORTEX module packages; those modules must remain unavailable after installation.

## Runtime acceptance

- On the target Linux host, verify the exact installed artifact, SQLite support,
  service owner and canonical directory. Check managed startup, shutdown,
  restart persistence and process identity with disposable data before touching
  an authorized real store. See [deployment](../deploy/README.md).
- With an explicitly prepared model, verify indexing and real CPU queries.
  Compare source, revision, body passage and negative queries on a predeclared
  neutral corpus; assess usefulness separately. See [performance checks](stress-tests.md).
- For remote OAuth, verify private and public listener authentication, the exact
  approval/callback flow, refresh, revocation and project scopes through the
  intended route. Reload each real assistant and test actual read/write and
  cross-reading within its grants. A configured bridge is not client activation.
- For recovery, use an owner-authorized copy and a target-bound rollback plan.
  Verify revisions, deletion barriers, access invalidation and the exact staged
  activation/retry procedure in [recovery](upgrading.md). The current restore
  requires a surviving store; total host-loss recovery is unsupported.

Controlled expiry tests do not establish months of live operation. A code rollback
cannot restore erased data, invalidated grants or an incompatible FTS format.
