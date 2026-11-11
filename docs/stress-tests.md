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

The script always creates a disposable store. It accepts no existing database
and refuses to overwrite an existing report. Use only the reviewed fictional
fixtures with its `--corpus` option; never supply private memory exports. It verifies
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
synthetic baseline/development/challenge JSON and JUnit as `semantic-retrieval-baseline`, retained for
14 days; model weights and stores are not uploaded.

## Development and held-out evaluation

The same runner supports two larger, topic-separated fixtures. The frozen v1
corpus remains unchanged as regression evidence. V2 source IDs are deterministic
for each of the fixed UUID seeds `11`, `29`, and `47`; every seed is reported
separately. Product ID generation and ranking remain unchanged.

- [Development](../tests/fixtures/retrieval/cz-en-dev-v2.json): 28 records,
  100 positive questions across 25 fact topics, 50 cross-language positives and
  25 no-answer questions. It includes two updated records, one deleted record and
  two excluded records. Most negative questions concern distant fictional topics;
  this is a limited development distribution, not general abstention proof.
- [Held-out acceptance](../tests/fixtures/retrieval/cz-en-holdout-v2.json):
  200 positive questions across 50 separate fact topics, at least 50 cross-language
  positives and 50 no-answer questions. An independent author and oracle reviewer
  prepare its labels before any candidate evaluation. Topic separation concerns
  the actual facts, not merely different names or IDs.

Each `relevant` list contains all annotated acceptable current visible sources;
returning any of these counts as a Hit@3. Recall@3 measures the fraction of that
list retrieved, while MRR@3 uses the first acceptable result. Positive averages
exclude no-answer queries. A no-answer false positive is a query with no relevant
visible source that nevertheless returns a record. A related source does not
become irrelevant merely because it lacks every requested answer detail.

```sh
EVALUATION_DIR=$(mktemp -d)
uv run --locked --no-sync python scripts/retrieval_baseline.py --model-dir /absolute/private/prepared-model --corpus tests/fixtures/retrieval/cz-en-dev-v2.json --output "$EVALUATION_DIR/development.json"
```

The runner first indexes the initial revisions, applies real updates/deletions,
checks provenance and project/revision/deletion boundaries while indexing is
pending, then drains indexing and measures the current corpus. Pending checks
are not quality measurements. Each indexing phase has a 120-second budget and
rejects no-progress results. Reports separate loading/indexing and pending-probe
time, report Hit@3, MRR@3, no-answer rates, categories, cross-language results and
query p95, and retain the exact corpus, scoring, runtime, lock and model hashes.
Changing measured source or fixture bytes during execution fails the run.

Only development fixtures run automatically in CI. Held-out execution needs
explicit `--allow-holdout` on an immutable candidate after ranking/abstention
selection. The flag is a guard, not proof of independent custody. Keep an evaluator
receipt identifying the candidate, fixture, labels, seeds and any prior exposure.
After results are inspected, that slice becomes regression evidence; do not tune
against it and continue describing it as an unseen acceptance set.

### Development field diagnostics

The additional [challenge](../tests/fixtures/retrieval/cz-en-challenge-v1.json)
contains 25 positive and 25 no-answer questions over 28 fictional records, with
26 current visible records, one deleted record and one excluded record. Every
negative shares a visible name, identifier or nearby vocabulary with an unrelated
source. Its independent oracle review precedes measurement; once inspected, its
results are development evidence, never unseen release acceptance. The original
v1 and v2 fixture bytes remain unchanged.

Use `--field-diagnostics` with either development fixture to collect the actual
unchanged hybrid lists at limits 1, 3, 20 and 50 and actual context output with the
default 6,000-character budget. Context is backed by search at limit 20 but may
include fewer sources; the report keeps both lists. Per-result evidence separates
title, body and overall similarity, the current model/revision, and exact source
offsets. Missing current indexing is explicit, not inferred from a bounded list.
Pending diagnostics remain separate from ready quality evidence.

```sh
uv run --locked --no-sync python scripts/retrieval_baseline.py --model-dir /absolute/private/prepared-model --corpus tests/fixtures/retrieval/cz-en-challenge-v1.json --field-diagnostics --output "$EVALUATION_DIR/challenge.json"
```

The diagnostic collector uses extra query embeddings and a complete current
vector scan, and checks scores against canonical semantic search over at most
50 visible sources. It is a bounded synthetic diagnostic, not a product search
implementation or latency benchmark. It fails when scope coverage or current
source/passages disagree. The opt-in flag does not change product behavior or
ordinary baseline outputs. CI collects diagnostics for both development slices;
it neither chooses a relevance threshold nor executes the heldout.

The bounded query-gate experiment compares only overall versus body evidence.
Each rule either preserves the complete ranked list or returns an empty list;
it retains the declared whole-input identifier/acronym exception. Use one cutoff
per rule across both slices, all seeds and limits. Require no baseline positive
hit or MRR loss and at most two false positives among each slice's 25 negatives,
separately for every seed and search limit and for actual context output. Context
gate evidence comes from its full search-20 list, while its metrics use only the
emitted sources. The original 100 development hits at limit 3 remain required.
Freeze the formula and all labels before measurement; retain rejected thresholds
and independent recomputation. A development pass only permits candidate review
and the separate release acceptance below. If neither rule qualifies, keep the
existing product behavior rather than weakening these requirements.

Release acceptance is not implemented as a hidden runner threshold. The approved
candidate protocol requires, on every seed, positive Hit@3 at least 95%,
cross-language Hit@3 at least 90%, no more than 5 of 50 unsupported no-answer
results, and MRR@3 no worse than the matched baseline. Exact identifiers/acronyms
and revision/deletion cases must not regress; source, project and passage
invariants must have zero violations. Candidate and baseline use identical
fixtures, model, dependencies and host limits. Failed criteria block release
preparation until the scope or candidate is explicitly resolved.

## Bounded capacity at the current scale

The optional `capacity` input of the existing CI workflow runs only when manually
dispatched. It runs storage/HTTP scenarios at 100 records / 1 worker, 1,000 / 8,
and 5,000 / 16, followed by 450 synthetic records with 29,952 measured embedding
chunks. The large bodies are repetitive capacity fixtures; their six repeated
questions do not measure representative relevance.

The semantic harness uses four concurrent workers and three batches of 100
measured calls, retaining all three p95 values plus their median and worst tail.
It declares 900 seconds for indexing and 900 seconds per query batch. One
disposable systemd unit bounds the harness and every descendant process to
2 GiB without swap and 3,900 seconds total. The receipt reads actual cgroup limits
and peak use, and rejects OOM events. Timeout, wrong chunk counts, operation errors,
incomplete indexing or fulltext fallback fail the run. The unit kills all remaining processes in its
cgroup on exit and is collected; these are test-host units, not deployment services.

Run the equivalent semantic measurement on an authorized isolated Linux host:

```sh
uv run --locked --no-sync python scripts/stress_semantic.py --model-dir /absolute/private/prepared-model --records 450 --chunks-per-record 67 --workers 4 --queries 100 --runs 3 --index-timeout 900 --query-timeout 900 --output "$EVALUATION_DIR/capacity.json"
```

That direct command does not install a memory limit; use the reviewed CI unit or
an equivalent enforced whole-cgroup limit for the release comparison.
Retain the resource receipt and all raw synthetic reports outside Git. Candidate
hybrid p95 must not exceed its matched baseline by more than 20%; never infer
semantic performance from a fallback run. Artifact receipts bind actual results;
the presence of this harness alone is not a capacity or release pass.

## Interpret the reports

Keep host, Python/SQLite versions, pinned model, corpus and expected answers
constant when comparing versions. Separate model preparation, startup/indexing
and steady-state latency. Reports include latency percentiles and service peak
RSS; their elapsed time and hit counts apply only to that bounded run.

Use a predeclared neutral corpus for relevance acceptance, checking exact source,
revision and body passage as well as rank. Include negative and short-word
queries and the owner's usefulness assessment. No general speed, capacity or
relevance guarantee follows from the synthetic harness. See [verification](verification.md).
