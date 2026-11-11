# Installation and operation

Version 1.0.2 uses schema v2. Linux with Python **3.11 or 3.12** is the
supported deployment target. Python must provide SQLite **3.42 or newer with
FTS5**. Data, backups and exports need a local POSIX filesystem with SQLite and advisory lock support.
macOS supports local development and stdio checks; managed `up` requires Linux
process identity and pidfd support. Windows is unsupported.

One host owns the canonical store. Before changing it, record the exact artifact,
interpreter, data directory, supervisor, active clients and rollback source in a
private operator record. Use that installation's executable for shell commands;
a checkout or a second device's client configuration does not establish access
to the canonical data. Keep host paths and credentials out of public reports.

## Install an identified release

Use a fresh installation directory, outside the private data directory. The
commands require [uv](https://docs.astral.sh/uv/), `curl`, `unzip` and GNU
`sha256sum`. They target the
[1.0.2 release](https://github.com/OnFiala/Dots-Brain/releases/tag/v1.0.2) and require
its published, verified assets. Source version metadata and passing CI do not
establish download availability; check [published releases](https://github.com/OnFiala/Dots-Brain/releases).
Download the attached source/plugin ZIP, not a floating `main` checkout:

```sh
set -eu
RELEASE_URL=https://github.com/OnFiala/Dots-Brain/releases/download/v1.0.2
curl --fail --location --output SHA256SUMS "$RELEASE_URL/SHA256SUMS"
curl --fail --location --output dots-brain-1.0.2-plugin.zip "$RELEASE_URL/dots-brain-1.0.2-plugin.zip"
sha256sum --check --ignore-missing SHA256SUMS
unzip dots-brain-1.0.2-plugin.zip
cd dots-brain
```

Stop if downloading or checksum verification fails. `--ignore-missing` skips
other release assets you have not downloaded; it must still report the ZIP as
`OK`. Checksums identify the assets served by the release; they are not an
independent publisher signature. Retain the archive and its checksum manifest
with the installation record. The ZIP contains `pyproject.toml`, `uv.lock`,
operator documentation and verification scripts from the same release.

### Locked source installation

From the extracted `dots-brain` directory:

```sh
uv sync --locked --no-dev --python 3.12
BRAIN_EXEC="$PWD/.venv/bin/dots-brain"
"$BRAIN_EXEC" --version
```

Use `--python 3.11` throughout instead if needed. `--locked` refuses to rewrite
the release lock. Network access may be needed to obtain the selected Python
interpreter and locked dependencies. Do not run a bare `pip install` or replace
the lock with one from another release.

### Verified release wheel alternative

Use this instead of the source installation. The wheel environment is a sibling
of the extracted source directory so the verifier can reject source-tree imports.
Keep the extracted source ZIP for its matching lock and installed-wheel verifier. From the extracted directory,
download the wheel next to that directory and verify it with the same manifest:

```sh
curl --fail --location --output ../dots_brain-1.0.2-py3-none-any.whl "$RELEASE_URL/dots_brain-1.0.2-py3-none-any.whl"
(cd .. && sha256sum --check --ignore-missing SHA256SUMS)
uv export --locked --no-dev --no-emit-project --format requirements.txt --output-file core-requirements.txt
uv venv --python 3.12 ../dots-brain-venv
uv pip sync --python ../dots-brain-venv/bin/python --require-hashes core-requirements.txt
uv pip install --python ../dots-brain-venv/bin/python --no-deps ../dots_brain-1.0.2-py3-none-any.whl
env -u PYTHONPATH ../dots-brain-venv/bin/python scripts/verify_wheel.py
BRAIN_EXEC="$PWD/../dots-brain-venv/bin/dots-brain"
"$BRAIN_EXEC" --version
```

The checksum check must report both downloaded assets as `OK`. Dependencies
come from `uv.lock` with required hashes; `--no-deps` prevents the wheel install
from resolving a different dependency set. The verifier uses disposable stores
and real local stdio/HTTP MCP calls. It must report `state: "passed"` and version
`1.0.2`; it does not use your canonical memory directory. This core-only verifier
expects no semantic extra. See [verification](verification.md#installed-wheel).

## Initialize and connect

Use the selected installation's absolute `BRAIN_EXEC` path. Choose a private
canonical data directory outside the extracted source and environment:

```sh
"$BRAIN_EXEC" --data-dir /absolute/private/memory setup
"$BRAIN_EXEC" --data-dir /absolute/private/memory doctor
```

`--version` must print `1.0.2`; inspect `doctor` for SQLite, storage and capability
failures before starting a service. Python's `sqlite3` library is what matters,
not a separately installed `sqlite3` shell.

An existing v1 store needs the explicit [offline upgrade](upgrading.md). A new
store uses schema v2. Schema v3/v4 is refused; it is never relabeled or downgraded.

One client can use `serve` for owner stdio MCP. For a managed Linux HTTP service:

```sh
"$BRAIN_EXEC" --data-dir /absolute/private/memory up
"$BRAIN_EXEC" --data-dir /absolute/private/memory connect codex --project work
```

`up` checks a real read before publishing service metadata. Its probe credential
has read scope only. `connect` verifies a read through the generated bridge and
returns `write: "not_tested"`; it creates no synthetic memory. Reload the actual
client, then verify intended read/write calls separately. Client activation and
conversation behavior are not established by configuration.

From the extracted release source,
`python3 scripts/bootstrap.py --data-dir /absolute/private/memory` runs the same
locked source setup on Linux (it does not use the wheel environment). Optional
`--semantic` prepares the model; optional
`--connect PROVIDER --config PATH` configures a selected adapter. `mcp-json`
requires its explicit path. Failures return one sanitized JSON result naming
the failed phase (`prerequisites`, `arguments`, `dependencies`, `setup`, `model`,
`service` or `client`). Completed earlier phases are not rolled back; inspect the
named phase before retrying. Child output is not copied into that result.

Bootstrap invokes `up --resume`. Before running it on an existing store, establish
why the store is disabled and confirm that resuming is authorized. Resume accepts
only ordinary uninstall state, including its recognized legacy form; it never
bypasses restore or unknown recovery barriers. Follow [recovery](upgrading.md)
for those barriers. Bootstrap is not a generic recovery command.

## An existing supervisor

Run exactly one HTTP process for a store. Use the chosen executable by its
absolute path in a supervisor unit. The equivalent shell command is:

```sh
"$BRAIN_EXEC" --data-dir /absolute/private/memory serve --transport http --port 8765 --public-port 8787
```

The private port accepts scoped static credentials and OAuth. The second port
accepts OAuth only; route public HTTPS solely to that port. See [OAuth](oauth.md)
for offline configuration and the explicit owner approval flow.

Use an existing private credential for read-only diagnosis. A new local client
can obtain its own scoped credential through `connect` with the private URL:

```sh
"$BRAIN_EXEC" --data-dir /absolute/private/memory doctor --mcp-url http://127.0.0.1:8765/mcp --credential-file /absolute/private/credential.json
"$BRAIN_EXEC" --data-dir /absolute/private/memory connect codex --project work --mcp-url http://127.0.0.1:8765/mcp
```

This bridge has no automatic `up`. Unreachable or mismatched endpoints fail
before successful client configuration. A failed connect restores its owned
changes where safe and preserves concurrent edits. If rollback cannot safely
remove a published configuration, it reports incomplete cleanup and retains
its recorded credential, integration and any newly started service. Inspect
that configuration before reconnecting or disconnecting; the error does not
mean that all earlier changes were reversed. `doctor` reports database,
OAuth and MCP checks separately; a remote issuer without a verified public
listener produces a warning. It does not scan or signal supervisor processes.
`up` refuses an already held HTTP lease; `down` leaves external services running.

## Optional local embeddings

For the locked source installation, from the same extracted release:

```sh
uv sync --locked --no-dev --python 3.12 --extra semantic
```

For the wheel installation, use its matching release lock and verified wheel:

```sh
uv export --locked --no-dev --no-emit-project --extra semantic --format requirements.txt --output-file semantic-requirements.txt
uv pip sync --python ../dots-brain-venv/bin/python --require-hashes semantic-requirements.txt
uv pip install --python ../dots-brain-venv/bin/python --no-deps ../dots_brain-1.0.2-py3-none-any.whl
```

Keep `BRAIN_EXEC` pointed at the chosen environment. Do not run the core-only
wheel verifier in a semantic environment. With either installation:

```sh
"$BRAIN_EXEC" --data-dir /absolute/private/memory model prepare
"$BRAIN_EXEC" --data-dir /absolute/private/memory index
```

Preparation explicitly downloads the pinned local CPU model and verifies its
files. Enable semantic service operation with `--semantic` through the chosen
supervisor, or a separately authorized `down`/`up --semantic` cycle. `index`
reports `indexed_now` separately from the total. FTS remains available during
indexing or a query embedding failure. Relevance, especially short words and
negative queries, needs a corpus check and owner judgment; see [using memory](using-memory.md).

## Installation boundaries

The plugin manifest describes metadata and the setup skill. It does not install
a service or grant memory access. Capture and CORTEX are disabled and excluded
from the core distribution; their old configuration does not activate them.
See [activity](activity.md) for preserved state and activation boundaries. Boot startup, public
TLS, real-assistant behavior and recovery of real data need separate acceptance.
The deployment templates are examples, not an observed installation.
