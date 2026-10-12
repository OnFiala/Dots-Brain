# Verification boundaries

Source, installed artifact, live protocol and assistant behavior are separate
results. This version uses schema v2. Tests use disposable
synthetic stores and isolate client configuration from the operator's home.

## Local batch-3 evidence

The complete repository run on Python 3.11/macOS has **481 passed, 0 failed,
9 skipped** from 490 cases, including the prepared real CPU model and preserved
source-only module characterizations. The skips require Linux process control.
Twelve new disabled-module cases fail against the preceding source and pass with
the separation. They trap optional imports, file/resource access and network
attempts, verify real in-process MCP, and check both fresh and historical state.
The transport in their `serve` tests is stubbed; this is labeled separately from
the installed wheel's real stdio MCP smoke below.

The core wheel and sdist exclude both module packages and their tests. A fresh
offline environment installs the wheel with only locked core dependencies;
released-v1 migration, revisions, grants, deletion, restore and real stdio MCP
pass there. Module SQL and implementation bodies are preserved outside core;
old module state is not migrated or deleted. Two recovery cases also reject
cutover when an optional ledger exists on only one side.
The installed core also exposes exactly nine core/audit MCP tools, rejects both
disabled CLI commands before creating state, preserves historical module files,
and runs without either module package or the optional embedding dependency.

The preceding lock-location fix has **86 passed / 6 Linux skips** in the client
matrix, including all four adapters with and without old adjacent lock files.
The new config lock is inside the Dots Brain data directory; an untouched created
client configuration leaves no new home-directory residue after disconnect.

## Batch-2 evidence

The final Python 3.11/macOS integration run has **459 passed, 0 failed, 9 skipped**
from 468 cases. The real prepared CPU model test ran. All skips require Linux
managed process control. The cleanup matrix covers 29 cases, including foreign
backup creation, replaced credentials and a failed rollback whose retained
configuration still references access. Independent review reproduced those three
data-loss paths and verified their repairs. Existing data-preservation assertions
remain in place after updating two expected error outcomes.

Operator tests simulate bootstrap children; they do not prove an installation.
CI now uses locked dependencies, a 20-minute timeout and actual nginx/systemd
syntax tools on temporary template copies. Those Linux commands and remote CI
have not run on this macOS host. A separate Linux supervisor reported **468
passed / 0 skipped twice**, **30/30** crash-recovery runs and **84 passed** in the
client matrix on `68be851`, with Python 3.12.3 and SQLite 3.45.1. It found the CI
placeholder bind defect and verified nginx/systemd syntax after changing only
the temporary copy to loopback, as CI now does. That evidence identifies batch 2;
subsequent lock and module changes need their own Linux check. Actual systemd
operation, public ingress and production restoration remain separate.

## Earlier batch-1 evidence

The integrated Python 3.11/macOS run has **323 passed, 0 failed, 9 skipped** from
332 static cases. All nine skips require actual Linux managed process control;
mocked lifecycle cases are not Linux evidence. The optional real CPU model test
ran against previously prepared, verified model files. Ruff, formatting,
project/links validation and an offline source/wheel build also passed.

```sh
uv sync --locked --all-extras
uv run ruff check src tests scripts modules
uv run ruff format --check src tests scripts modules
uv run pytest -q
uv run python scripts/validate_project.py
uv build --offline
```

Set `BRAIN_TEST_MODEL_DIR` to prepared model files to run that optional case;
ordinary tests do not silently download them. The retained CI matrix targets
Linux Python 3.11/3.12. Local execution is not a claim that remote CI ran.

Install the built wheel into a fresh environment and invoke this check with its
Python; the helper refuses an import from the source checkout:

```sh
/path/to/wheel-environment/bin/python scripts/verify_wheel.py
```

It verifies the hashed released v1 synthetic fixture, revision retention, static
grant and OAuth history, CRUD, suppression after restoring a pre-deletion backup,
and real installed stdio MCP. Separate live tests exercise both HTTP listeners,
PKCE/refresh/revocation, project scopes, concurrent bridge calls and a lost reply
after a committed mutation. An actual local browser flow also displayed the
pairing request, followed the automatic callback and completed S256 exchange
plus OAuth MCP read/write against the installed candidate.

A frozen private Czech corpus compared the same model/environment with baseline
`c963700`: expected revision/source appeared in the top three for 15/15 positive
queries in both; correct body/revision/source improved from 3/15 to 15/15. All
three negative queries still returned results in both. The candidate was slower
in that run. No relevance floor, short-word fix or speedup is claimed. Owner
judgment remains required; the corpus and helper are not repository content.

## Remaining acceptance

- Both intended assistants need real new-session calls and cross-reading.
- The 30-day grant boundary uses controlled fixture time, not 30 observed days.
- Real-store copies, their backup/restore and the installed production artifact
  need explicit owner authorization and a target-bound recovery plan.
- A Linux supervisor separately reported batch-1 and batch-2 coexistence on their exact commits.
  Subsequent lock and batch-3 changes require a new Linux run; macOS skips remain unverified here.
  Actual systemd-unit acceptance belongs to a later approved production step.
- Optional modules remain disabled; their internal repairs and live activation
  require a separate target-specific plan.

Independent AI review covered the batch-2 lifecycle repairs and the frozen
batch-3 patch. Batch 3 had no supported code finding and 95 independently passing
targeted cases. Read-only roles were procedural, not enforced OS isolation; the
main operator owns integrated acceptance and final artifact verification. Keep
the checkpoint's exact commit, artifact hashes, commands and reports together
outside the public repository. A code revert cannot restore erased data,
invalidated access or old FTS compatibility.
