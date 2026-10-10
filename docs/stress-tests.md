# Stress testing

Stress tests must run against a disposable store with synthetic data. Record the
source revision, environment, workload, limits, and measurements separately from
production claims. Do not access a canonical store, credential, or external client
to improve a benchmark result.

SQLite has one writer at a time; measure contention and failure behavior rather
than inferring capacity from a single successful run. Semantic relevance and large
corpus capacity need their own representative datasets.

## Audit workload, 2026-10-10

An independent AI reviewer tested commit `0a48497` (source tree
`2b9dab2072ba5495852587c3c2795974c5d9b20d`) on macOS 26.6.2 arm64,
Python 3.11.14 and SQLite 3.50.4. All stores were disposable and all records
synthetic. The vector workloads used fixed normalized 384-dimensional vectors,
so their timings exclude model inference, HTTP and external clients.

| Workload | Observed result |
| --- | --- |
| 50,000 valid chunks | Search median 147 ms (3 samples); idle index pass median 30 ms (5 samples). |
| 100,000 valid chunks | Search median 388 ms (3 samples, range 302–503 ms); idle index pass median 62 ms (5 samples). |
| 50,000 / 100,000 invalid chunks | Search 81 / 137 ms, no result and one summary warning per call. One repair pass repaired exactly 4 records in 34 / 68 ms. Each is one sample. |
| 100,000 stale retry entries, limit 16 | 18 SELECT statements, exactly 16 stale entries removed in one pass (0.144 ms, one sample). |
| 256 deferred failures | A new healthy record was indexed first; the last due failure was reached within 16 bounded passes. |
| Forget followed by a new revision-1 record, unchanged total count | The replacement was indexed and returned by search. |

The review caught a lossy count/revision shortcut that missed replacements, an
unbounded retry-deadline loop, and invalid embedding shapes that could enter a
repair loop. The final source removes the shortcut, rotates a bounded set of
retry entries, and validates vector shape before publication. Deterministic
regressions live in `tests/test_semantic_regressions.py`.

Search remains linear in stored chunks, and an idle indexing pass still scans
the source table for pending records. Cancellation during embedding occurs
between records. These warm, bounded measurements do not establish production
capacity, model relevance, concurrent workload headroom, provider-log freshness,
or long-running service limits. CI separately exercises the pinned real model
and an actual Chromium OAuth callback.
