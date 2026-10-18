# Performance and retrieval checks

Run the bounded synthetic harnesses only on an authorized Linux test host. They
create disposable stores, start managed loopback services and inspect Linux
`/proc`; they are not macOS runtime checks or production probes. Reports are
written to the supplied paths, so choose fresh private filenames.

## Storage and HTTP

From the reviewed source checkout with locked dependencies:

```sh
uv sync --locked --all-extras
REPORT_DIR=$(mktemp -d)
uv run python scripts/stress.py --records 100 --workers 4 --http-calls 20 --output "$REPORT_DIR/storage.json"
```

[The storage harness](../scripts/stress.py) permits 100–20,000 records, 1–32
workers and 1–2,000 HTTP write/read pairs. It checks retry deduplication, one
revision-conflict winner, project isolation, HTTP access, revocation, restart
persistence, deletion suppression and SQLite integrity. It does not exercise
semantic search.

## Local CPU embeddings

Prepare the pinned model explicitly with `model prepare` in a separate disposable
store; that step downloads and verifies files:

```sh
MODEL_STORE=$(mktemp -d)
uv run dots-brain --data-dir "$MODEL_STORE/memory" setup
uv run dots-brain --data-dir "$MODEL_STORE/memory" model prepare
```

Supply the returned `directory` to [the semantic harness](../scripts/stress_semantic.py):

```sh
SEMANTIC_REPORT_DIR=$(mktemp -d)
uv run python scripts/stress_semantic.py --model-dir /absolute/private/prepared-model --records 100 --workers 4 --queries 24 --output "$SEMANTIC_REPORT_DIR/semantic.json"
```

The harness copies or hard-links model files into a fresh store. It permits
3–300 records, 1–8 workers and 6–200 queries. It requires a ready index containing
every synthetic record, no pending indexing and enabled hybrid search for every
query. Fulltext fallback fails the run even if it returns the expected records.
Six repeated synthetic Czech/English prompts check recall at three; this is not
a representative relevance benchmark.

## Interpret the reports

Keep host, Python/SQLite versions, pinned model, corpus and expected answers
constant when comparing versions. Separate model preparation, startup/indexing
and steady-state latency. Reports include latency percentiles and service peak
RSS; their elapsed time and hit counts apply only to that bounded run.

Use a predeclared neutral corpus for relevance acceptance, checking exact source,
revision and body passage as well as rank. Include negative and short-word
queries and the owner's usefulness assessment. No general speed, capacity or
relevance guarantee follows from the synthetic harness. See [verification](verification.md).
