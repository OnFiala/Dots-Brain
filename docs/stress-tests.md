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

## Frozen CZ/EN retrieval baseline

[The versioned corpus](../tests/fixtures/retrieval/cz-en-v1.json) contains ten
fictional notes and sixteen declared queries. Eight notes are visible to the
reader; two belong to an excluded project. Thirteen queries have declared
relevant sources. Three have no relevant visible source, including one targeting
only the excluded project. Cases cover exact terms, Czech diacritics and
inflections, cross-language questions, paraphrases and short queries.

From a checkout with locked semantic dependencies and the explicitly prepared
model directory above, run:

```sh
BASELINE_DIR=$(mktemp -d)
uv run --locked --no-sync python scripts/retrieval_baseline.py --model-dir /absolute/private/prepared-model --output "$BASELINE_DIR/retrieval.json"
```

The script always creates a disposable synthetic store. It accepts no existing
database or user corpus and refuses to overwrite an existing report. It verifies
the model artifacts, checks that loaded retrieval code matches the checkout,
indexes all ten notes, then measures full-text and enabled hybrid search at three
results per query. Wrong mode, degraded semantic search, project leakage or a
mismatched source/revision fails the run. Relevance misses are recorded rather
than turned into a new acceptance threshold.

Fixture creation uses stable UUIDs derived from corpus version, project and
source event ID. The override is limited to synchronous note creation in the
disposable store and ends before indexing. This preserves existing UUID-based
tie-breaking while avoiding random rank changes between otherwise identical
measurements; product identity generation and ranking are unchanged.

The report includes each query's stable source event IDs and judgments, recall
at three and reciprocal rank within three, category summaries, and counts of
negative queries that still return results. Negative queries are excluded from
positive-query averages. Hybrid search has no abstention threshold, so returning
results for an unrelated question is observable behavior, not proof that the
question was answered correctly. Timings exclude one warmup per mode and model
loading/indexing; they are not a throughput benchmark.

Keep the corpus version/hash, model manifest hash, source/lock hashes and
environment fields with each comparison. Freeze judgments before comparing
changes; a deliberate corpus change requires a new version. A small fictional
corpus does not establish usefulness for an owner's real memories.

The dedicated `semantic-baseline` CI job prepares and verifies the pinned model
even on a cache hit, then performs inference offline. It runs the existing real
model test and checks its JUnit receipt: a missing, failed or skipped test fails
the job. Regular matrix jobs retain the opt-in skip. CI publishes only the
synthetic baseline JSON and JUnit as `semantic-retrieval-baseline`, retained for
14 days; model weights and stores are not uploaded.

## Interpret the reports

Keep host, Python/SQLite versions, pinned model, corpus and expected answers
constant when comparing versions. Separate model preparation, startup/indexing
and steady-state latency. Reports include latency percentiles and service peak
RSS; their elapsed time and hit counts apply only to that bounded run.

Use a predeclared neutral corpus for relevance acceptance, checking exact source,
revision and body passage as well as rank. Include negative and short-word
queries and the owner's usefulness assessment. No general speed, capacity or
relevance guarantee follows from the synthetic harness. See [verification](verification.md).
