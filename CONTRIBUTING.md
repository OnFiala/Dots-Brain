# Contributing

Use English for documentation, code comments, product text, issues, and commits.
Small, focused changes are easier to review and maintain.

Install development dependencies with `uv sync --frozen --all-extras`. Before
committing, run `uv run ruff check src tests scripts`,
`uv run ruff format --check src tests scripts`, `uv run pytest -q`,
`uv run python scripts/validate_project.py`, and `uv build`. Model downloads are
not required for the default test suite. Enable the real model smoke test only
with an explicitly prepared artifact directory as described in the verification docs.

Use Conventional Commits, such as `feat(memory): preserve source revisions` or
`fix(auth): reject expired credentials`. Commit each coherent milestone and
include its validation. Do not squash or rewrite already published history
without an explicit repository-maintainer decision.

For tagged builds and release assets, follow the [release procedure](docs/releases.md).

Tests must exercise behavior: persistence, retries, source conflicts, scope
boundaries, deletion, and real protocol calls. Avoid tests that only duplicate
the implementation. Keep the capability matrix accurate.

Never include personal data, credentials, model downloads, or local databases
in commits or issue reports. Use synthetic examples. Contributions are made
under the repository's MIT license; do not submit code you cannot license.
