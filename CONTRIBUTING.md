# Contributing

Use English for documentation, code comments, product text, issues, and commits.
Small, focused changes are easier to review and maintain.

Use Conventional Commits, such as `feat(memory): preserve source revisions` or
`fix(auth): reject expired credentials`. Commit each coherent milestone and
include its validation. Do not squash or rewrite already published history
without an explicit repository-maintainer decision.

Tests must exercise behavior: persistence, retries, source conflicts, scope
boundaries, deletion, and real protocol calls. Avoid tests that only duplicate
the implementation. Keep the capability matrix accurate.

Never include personal data, credentials, model downloads, or local databases
in commits or issue reports. Use synthetic examples. Contributions are made
under the repository's MIT license; do not submit code you cannot license.
