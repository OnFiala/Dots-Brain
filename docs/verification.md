# Verification evidence

Environment: managed Linux workspace, Python 3.12.14, SQLite through the Python
standard library. On 2026-10-05 the user confirmed that Work and their Dot share
this VM. Earlier tests used disposable instances; an actual retained installation
has now been exercised separately. See [deployment evidence](vm-deployment.md).
Dependencies are recorded in `uv.lock`.

## Unreleased shared-client safety checks — 2026-10-09

On macOS with Python 3.11.14, the locally executable suite passed 75 tests. Eight
Linux-managed-process tests were explicitly excluded; two synthetic vector tests
were skipped because NumPy was absent, and the opt-in real-model test was skipped.
The new checks cover stale/invalid deletion revisions through SQLite and real
MCP SDK HTTP messages, two authenticated clients sharing revisions, and rejection
of missing/mismatched local resume targets before startup. An existing live
loopback HTTP process plus subprocess stdio bridge test also passed. All data and
credentials were disposable fixtures.

Lint, formatting, documentation validation and offline wheel/sdist builds passed.
This is local source evidence: Linux restart/reconnect, the retained VM, actual
Botter/Grok clients, and real embedding inference were not revalidated here.
The existing Linux suite must pass on the candidate before a release is approved.

Linux CI then passed all 85 default tests on Python 3.11 and 3.12 for commit
`1bc3604`, with only the real-model test skipped. That run includes Linux managed
restart/reconnect, synthetic vector deletion, lint, formatting, documentation and
package builds. See [the exact CI run](https://github.com/OnFiala/Dots-Brain/actions/runs/37954727661).
The bounded stress harness now also has a regression test to keep its winning
update's revision for synthetic cleanup. These checks do not establish the state
of the retained VM or either actual bot.

## OAuth validation follow-up: 0.3.0-alpha.2

The follow-up adds two issuer-validation cases, bringing the suite to 66 tests
and 19 OAuth cases. Python 3.11.16 and 3.12.14 each passed all 66 tests, with the
unchanged opt-in model test skipped. Unsupported IPv6 HTTP issuer configuration and an empty
userinfo delimiter now fail before filesystem changes. The alpha.1 candidate
was superseded before publication; its immutable tag and earlier evidence remain.

## OAuth alpha: 0.3.0-alpha.1

Python 3.12.14 and 3.11.16 each passed 64 tests, with the unchanged opt-in real-model
test skipped. The 17 OAuth cases cover the full SDK protocol flow, authorization
and token resource binding, PKCE/callback checks, scope narrowing, project isolation,
single-use concurrent code exchange, rotating refresh tokens, expiry, owner/client
revocation, bounded registration, malformed inputs, and retained-data uninstall.

The official MCP `OAuthClientProvider` connected to a live background HTTP service:
discovery, registration, browser redirect/callback simulation, exact-request CLI
approval, token exchange, MCP initialization, and a synthetic memory write all
passed. Triggering client-side expiry completed automatic refresh without another
approval, invalidated the previous access token, and retained one grant. The test
also disabled OAuth and verified that ordinary local service operation remained.

Other tests use the real SDK HTTP handlers through ASGI and synthetic requests.
Confidential `client_secret_post` and public clients are exercised. No actual
ChatGPT/Claude web account, public TLS route, or retained user installation was connected. All
OAuth test data and credentials are synthetic and remain outside the repository.

Lint, formatting, documentation links, and wheel/source builds passed. The managed
development VM still reports no VPN or configured TCP destinations; public ingress
and zero-touch web-account onboarding remain unverified. No additional runtime
dependency, database service, identity service, or paid API was introduced.

## Lifecycle alpha: 0.2.0-alpha.2

Python 3.12.14 and 3.11.16 each passed 47 tests with the real-model test skipped.
This release changes lifecycle handling, not embedding inference. The previous
model and stress measurements below are historical evidence, not new runs.

The lifecycle checks cover preview without filesystem writes, repeated removal,
unchanged-entry removal in all four adapters, preservation of newer settings and
TOML comments, partial results for modified/malformed/symlink configurations,
revocation, independent credentials for profiles, legacy custom paths, remote
credential retention, no extra client database, retained memories and deletion
suppression, and rejected automatic restart until explicit reinstall. A running
owner stdio server also rejects memory calls after disabling.

A fresh disposable source copy completed bootstrap and real Claude Code 2.1.287
reported `Connected` in an isolated profile. The synthetic lifecycle then removed
the integration, verified `disabled`, ran bootstrap again against the same store,
verified read/write and the retained memory, and uninstalled twice. Package removal
with `uv pip uninstall` and deletion of that disposable checkout left the separate
test database intact. No user installation or personal memory was removed.

Lint, formatting, documentation links, and wheel/source builds passed. Repository
CI repeats the supported Python matrix. This does not certify external supervisors,
old unmanaged binaries, arbitrary unrecorded legacy configurations, or Botter's VM.

## Local automation alpha: 0.2.0-alpha.1

The 0.2.0 alpha passed 34 tests on Python 3.12.14, including the explicitly
prepared real model. The tests cover generated bridge startup and write/read
verification, preserved client settings, concurrent startup, crash recovery,
safe stopping, and responsive reads while a writer holds the database lock.
Python 3.11.16 passed 33 tests with the opt-in real-model test skipped.

A fresh writable source copy completed the single bootstrap command with local
embeddings and the Claude Code adapter enabled. It verified writes and reads
through the generated bridge and passed Claude Code's own connection health check.

A real Claude Code 2.1.287 installation reported `Connected` from an isolated
test profile after automated setup. This is not a claim that the user's laptop
or an existing Claude account was reconfigured. Cursor and Codex configuration
formats and SDK bridge calls are tested; their interactive UIs remain unverified.

Bounded storage/HTTP and real-model stress tests ran successfully. See the
[workloads, failures fixed, measurements, and raw results](stress-tests.md).

## Earlier releases

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

Public ingress and public OAuth deployment, Dot VM lifecycle, host billing/quotas,
actual web-provider clients,
automatic conversation capture, native ChatGPT views, and MCP Events. Successful
local MCP calls do not establish these capabilities.
