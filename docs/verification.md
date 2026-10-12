# Verification boundaries

Source, installed artifact, live protocol and assistant behavior are separate
results. This branch is an unreleased schema-v2 candidate. Tests use disposable
synthetic stores and isolate client configuration from the operator's home.

## Local batch-2 evidence

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
have not run on this macOS host. The supervisor must repeat the crash-recovery
case, full suite and disconnect matrix on the exact batch-2 checkpoint. Actual
systemd operation, public ingress and production restoration remain separate.

## Earlier batch-1 evidence

The integrated Python 3.11/macOS run has **323 passed, 0 failed, 9 skipped** from
332 static cases. All nine skips require actual Linux managed process control;
mocked lifecycle cases are not Linux evidence. The optional real CPU model test
ran against previously prepared, verified model files. Ruff, formatting,
project/links validation and an offline source/wheel build also passed.

```sh
uv sync --locked --all-extras
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
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
- A Linux supervisor separately reported batch-1 coexistence on its exact commit.
  Batch-2 changes require a new Linux run; macOS skips remain unverified here.
  Actual systemd-unit acceptance belongs to a later approved production step.
- Optional module separation belongs to batch 3 and has not started.

The independent AI reviewer read the lifecycle patch and its repairs; its
read-only role was procedural, not enforced OS isolation. Runtime checks above
were run by the main operator. Keep the checkpoint's exact commit, artifact
hashes, commands and reports together outside the public repository. A code
revert cannot restore erased data, invalidated access or old FTS compatibility.
