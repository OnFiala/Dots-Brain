# Stress testing

Stress tests must run against a disposable store with synthetic data. Record the
source revision, environment, workload, limits, and measurements separately from
production claims. Do not access a canonical store, credential, or external client
to improve a benchmark result.

SQLite has one writer at a time; measure contention and failure behavior rather
than inferring capacity from a single successful run. Semantic relevance and large
corpus capacity need their own representative datasets.

## Audit workload, 2026-10-10

The candidate reviewed on macOS was `8fd71d0`. An independent AI reviewer ran
these disposable workloads; they are measurements from one development host.

| Workload | Observed result |
| --- | --- |
| 50,000 valid chunks, 384 dimensions each | Search 0.130–0.134 s; status 0.040–0.041 s; idle index pass 0.068–0.072 s. Database 129.8 MiB. |
| 50,000 invalid chunks | Search 0.033 s, no result, one summary warning. |
| 31,800-character input with the pinned model | 133 candidate chunks; 128 stored in 1.764 s, truncation reported. |
| Pinned model memory use | About 1.01–1.02 GiB RSS for one instance. |
| 1,000 supported JSONL records | About 0.244 s; a further record remained pending for the next bounded pass. |
| Bridge shutdown with 100 callers | Every caller settled; no abandoned request remained in the queue. |
| 2,101 audit events | Reviewed in bounded chunks with matching checkpoint anchors. |

Search remains linear in stored chunks. Cancellation during embedding occurs
between records. These results do not establish appliance capacity, concurrent
workload headroom, provider-log freshness, or a long-running service limit.
