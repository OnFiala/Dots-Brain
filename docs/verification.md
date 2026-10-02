# Verification evidence

Environment: managed Linux development workspace, Python 3.12.14, SQLite through
the Python standard library. This workspace has not been identified as the user's
Dot VM. Dependencies are recorded in `uv.lock`.

Alpha 2 passed 23 tests on each of Python 3.12.14 and 3.11.16; the opt-in model
test was intentionally skipped for this SQLite initialization fix. Thirty
synchronized rounds with eight simultaneous setup calls each also passed.
The regression checks cover recovery after a temporary journal-mode lock and
recovery after the bounded wait expires, preserving existing database content.

The initial alpha passed 22 tests on Python 3.12.14, including the explicitly prepared
model test. The default suite also passed on Python 3.11.16: 21 tests passed and
the model test was intentionally skipped. A fresh writable source copy completed
the packaged bootstrap successfully. The plugin manifest passed the published
Agent Plugins 1.0 JSON schema. Lint, formatting, local documentation links, and
wheel/source-distribution builds were checked locally. The supported-version
matrix also runs in [GitHub CI](https://github.com/OnFiala/Dots-Brain/actions/workflows/ci.yml).

The opt-in model test currently emits one upstream Starlette test-client
deprecation warning; the assertions pass. It does not affect the server transport.

## Implemented checks

- Transactional persistence, revisions, concurrent retry deduplication, and restart.
- Project filtering before ranking and on reads, status, export, and deletion.
- Forgetting removes revisions and indexes and suppresses reimport.
- Credential expiry, revocation, private file creation, and redacted CLI output.
- Official MCP SDK initialization and tool calls over HTTP and subprocess stdio.
- A live loopback HTTP process with a subprocess stdio bridge, shared storage,
  read verification, and credential revocation.
- Rejection of unauthorized writes and access to another project.
- Bounded context, model integrity, stale vector exclusion, and deletion during indexing.

The model test is opt-in. It never downloads artifacts during the test suite:

```sh
BRAIN_TEST_MODEL_DIR=/absolute/path/to/prepared/model uv run pytest tests/test_semantic.py
```

## Initial model smoke measurement

On 2026-10-01, a fresh process loaded the pinned multilingual model, indexed three
short synthetic English memories, and answered a Czech retrieval query. The food
allergy record ranked first for `Kterému jídlu se mám vyhnout?` ("Which food should
I avoid?"). The process took 2.51 seconds and reached 669,360 KiB peak RSS, about
654 MiB. This includes Python, libraries, model loading, indexing, and the query.

This is a smoke test, not a latency benchmark, a capacity guarantee, or an evaluation
against another memory system. A larger bilingual evaluation and an actual Dot VM
resource test are still required. Model artifacts are excluded from the repository.

## Not yet verified

Public ingress, Dot VM lifecycle, host billing/quotas, OAuth, named provider clients,
automatic conversation capture, native ChatGPT views, and MCP Events. Successful
local MCP calls do not establish these capabilities.
