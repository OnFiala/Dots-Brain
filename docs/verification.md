# Verification boundaries

Source checks, package builds, installed runtime and assistant behavior are
different evidence. Tests use disposable synthetic stores. The retained CI
configuration and the tests actually executed for a checkpoint are the authority
for coverage; inherited documentation is not evidence of a job having run.

The storage checkpoint runs:

```sh
uv run pytest -q tests/test_store.py tests/test_operations.py tests/test_migrations.py tests/test_storage_v2.py
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run python scripts/validate_project.py
uv build
```

These checks cover synthetic v1 migration, revisions, deletion suppression,
standalone backups, recovery barriers and writer leases, including an actual
stdio MCP server. macOS checks do not verify the Linux managed daemon or systemd.
Subsequent batch steps must exercise OAuth callbacks, both HTTP listeners, MCP
mutations, retrieval, installed-wheel behavior and runtime diagnostics.

For deployment acceptance, bind the exact source and artifact digest to the
installed executable, supervisor, data directory and tested backup. Obtain the
owner's permission before using real data or changing production. Keep evidence
private and never publish credentials, addresses, client IDs or memory contents.
Both intended assistants need actual tool calls and new-session behavior checks;
a successful fixture or one assistant cannot stand in for the other.

Optional model tests require preprepared model artifacts in a disposable
directory. Ordinary tests must not download them implicitly. Live capture, CORTEX
publication, public route behavior, reboot recovery and owner relevance judgments
remain separate acceptance steps.
