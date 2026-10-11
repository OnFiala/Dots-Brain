# Stress testing

Stress tests must run against a disposable store with synthetic data. Record the
source revision, environment, workload, limits, and measurements separately from
production claims. Do not access a canonical store, credential, or external client
to improve a benchmark result.

SQLite has one writer at a time; measure contention and failure behavior rather
than inferring capacity from a single successful run. Semantic relevance and large
corpus capacity need their own representative datasets.

## Round 3 W4 comparison, 2026-10-11

Baseline `555f615` and the schema-v4 `0.4.0a4` implementation were measured on
the same MacBook Pro, macOS 26.6.2 arm64, Python 3.11.14, SQLite 3.50.4.
The final comparison ran sequentially without concurrent model inference. It
uses deterministic hash-based UUIDs, short neutral text and one fixed normalized
384-dimensional vector per record. It excludes model loading/inference, HTTP
and external clients. Status has five samples; update and forget each have 20.

| Workload | `555f615` | W4 candidate |
| --- | ---: | ---: |
| 50,000 records: status median CPU | 223.27 ms | 166.55 ms |
| 100,000 records: status median CPU | 494.20 ms | 395.26 ms |
| 50,000 records: status process peak RSS | 143.80 MiB | 40.39 MiB |
| 100,000 records: status process peak RSS | 250.20 MiB | 40.77 MiB |
| 50,000 records: update median CPU | 7.41 ms | 2.05 ms |
| 100,000 records: update median CPU | 13.20 ms | 2.26 ms |
| 50,000 records: forget median CPU | 5.45 ms | 1.78 ms |
| 100,000 records: forget median CPU | 9.92 ms | 2.09 ms |

The fresh processes began near 35 MiB RSS. Status still scans SQL rows and does
not have constant-time latency; the change avoids Python vector materialization.
These timings do not establish concurrent production throughput or a service RSS
limit. A deterministic SQLite instruction-budget regression separately verifies
that UUID deletion does not walk unrelated FTS rows. Unusual historical IDs keep
the slower exact-equality fallback.

Clean seeded database sizes grew from 176,234,496 to 178,749,440 bytes at 50,000
records and from 352,903,168 to 357,515,264 bytes at 100,000. The vector payload
dominates this workload; text-only or short-record stores can have a larger
percentage increase. Explicit v3-to-v4 migration after the measured writes took
4.44 s and 8.20 s wall time respectively, one sample each, including validation,
standalone backup and FTS rebuild. Each store then contained 20 fewer memories
plus their deletion barriers. These are not disk-space upper bounds.

After those writes, the FTS index pages alone grew from 3,768,320 to 6,262,784
bytes at 50,000 records and from 7,540,736 to 12,173,312 bytes at 100,000
(about 66% and 61%). Whole-database growth therefore understates the cost of
indexing IDs when vectors account for most of the database.

Reproduce with [benchmark_storage.py](../scripts/benchmark_storage.py), the same
interpreter and two clean source exports. Each directory
must be new; the helper creates only synthetic data and refuses an existing seed
directory. It emits JSON with environment, timings, sample ranges and RSS.

```sh
PYTHONPATH=/path/to/baseline/src python scripts/benchmark_storage.py seed /private/new-baseline 100000
PYTHONPATH=/path/to/baseline/src python scripts/benchmark_storage.py measure /private/new-baseline
PYTHONPATH=/path/to/candidate/src python scripts/benchmark_storage.py seed /private/new-candidate 100000
PYTHONPATH=/path/to/candidate/src python scripts/benchmark_storage.py measure /private/new-candidate
PYTHONPATH=/path/to/candidate/src python scripts/benchmark_storage.py migrate /private/new-baseline
```

The separate frozen public neutral corpus measured hybrid recall at ten as
**258/264 on `555f615` and 253/264 on W4**; semantic-only recall remained
**232/264 on both**. Both modes returned a candidate for all 24 unrelated queries
on both sources. The full-text policy and ID indexing therefore carry a measured
hybrid recall regression on this dataset; W4 does not claim a relevance or noise
improvement. The corpus is a public regression fixture, not held-out validation.

## Semantic index limits

The shipped storage helper measures status, updates, forget and migration; it
does not reproduce search or index-pass latency. Historical 50,000/100,000 chunk
timings without a shipped harness and comparable baseline have been removed.
Use [stress_semantic.py](../scripts/stress_semantic.py) for a bounded real-model
workload; record its environment and source revision, and compare the same
workload on the same machine before claiming an improvement.

Search remains linear in stored chunks. Discovering pending records still queries
the source table; repair and failed-retry work proceeds in bounded slices. CLI
`index` drains a complete repair sweep before returning. Cancellation during
embedding occurs between records. Deterministic regressions cover invalid vectors,
replacement records and deferred failures in
[semantic regressions](../tests/test_semantic_regressions.py) and
[round 3 semantic checks](../tests/test_round3_semantic.py).

None of these checks establishes production capacity, model relevance, concurrent
headroom, provider-log freshness or long-running service limits. CI separately
exercises the pinned real model and an actual Chromium OAuth callback.
