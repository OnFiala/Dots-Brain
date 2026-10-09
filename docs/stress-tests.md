# Stress-test evidence

## Appliance acceptance — 2026-10-09

These isolated synthetic runs used the installed `7417e7b` source on
`openclaw-appliance`, Python 3.12.3 and SQLite 3.45.1. Each transient job was
limited to one CPU, 1.5 GiB RAM and 300 seconds. It used its own temporary store
and credential. The live service stayed active with the same PID; the harnesses
did not open its database.

| Workload | Result |
| --- | --- |
| 2,000 writes, eight workers; write p95 / p99 | 134.53 / 736.71 ms |
| 2,000 identical retries; duplicate records | Zero |
| 200 HTTP write/read pairs; p95 / p99 | 640.95 / 662.07 ms |
| Storage run elapsed; service peak RSS | 32.43 s; 66,592 KiB |
| 200 records, four query workers, 48 bilingual queries | 48/48 expected facts in top three |
| Semantic startup and indexing | 14.13 s |
| Semantic query p95 / maximum | 991.65 / 1,009.10 ms |
| Semantic service peak RSS | 986,996 KiB (about 964 MiB) |

Retry deduplication, one-winner conflicting writes, project isolation, revocation,
restart persistence, deletion suppression, integrity and foreign-key checks passed.
Raw results: [storage/HTTP](benchmarks/2026-10-09-appliance/storage-http.json) and
[semantics](benchmarks/2026-10-09-appliance/semantic.json).

The six repeated bilingual prompts are a retrieval smoke test. These measurements
do not establish live traffic latency, representative recall or large-corpus
capacity. SQLite still serializes writers; the longest synthetic write took
1.64 seconds. CPU quotas and concurrent host work affect these results.

## Earlier development measurements — 2026-10-02

These tests ran on 2026-10-02 in a managed Linux development workspace. The host
exposed five CPU IDs with a cgroup quota of four CPU cores and a 16 GiB memory
limit. It has not been verified as the user's Dot VM. Measurements are individual
synthetic runs on shared development infrastructure, not a capacity guarantee.

## Storage and authenticated HTTP

The final run used Python 3.12.14, SQLite 3.53.1, 32 workers, 10,000 source records,
10,000 identical retries, and 1,000 HTTP write/read pairs. It completed in 31.04 s.

| Measurement | Result |
| --- | --- |
| Direct store writes, p95 / p99 | 107.53 / 637.08 ms |
| HTTP write/read pairs, p95 / p99 | 691.37 / 778.95 ms |
| HTTP service peak RSS, embeddings disabled | 62,120 KiB (about 61 MiB) |
| Harness peak RSS, separate from the service | 173,944 KiB |
| Concurrent conflicting updates accepted | Exactly one |
| Duplicate records from retries | Zero |
| SQLite integrity and foreign-key checks | Passed |

The run also checked project isolation, explicit HTTP 401 after credential
revocation, restart persistence, deletion, and suppression of forgotten sources.
Four project-search samples are recorded in the raw report; this sample is too
small to characterize search latency.

Raw output: [storage and HTTP](benchmarks/2026-10-02/storage-http.json).

## Local multilingual embeddings

A second run used the pinned CPU model with 200 synthetic memories, eight
concurrent clients, and 96 queries cycling through six Czech/English prompts.

| Measurement | Result |
| --- | --- |
| Startup and background indexing | 11.20 s |
| Query latency, p95 / p99 | 301.04 / 383.62 ms |
| Service peak RSS, embeddings enabled | 700,212 KiB (about 684 MiB) |
| Expected synthetic fact in the top three | 96 / 96 queries |

This checks a narrow bilingual scenario. It does not establish representative
retrieval quality, large-vector-index performance, or parity with Cortex.
The model used two ONNX CPU threads and performed no paid API inference.

Raw output: [local embeddings](benchmarks/2026-10-02/semantic.json).

## Failures found and fixed

- A waiting SQLite writer blocked unrelated MCP reads for 2.92 s. Synchronous
  tool work now runs outside the network event loop, with separate bounded read
  and write workers. A regression test holds the write lock for up to three
  seconds and requires reads to complete before that lock is released.
- The first 10,000-record / 32-worker storage run hit SQLite's lock timeout.
  New records unnecessarily scanned the full-text table to delete an entry that
  did not exist. Skipping that scan for inserts removed the observed failure.
- The first embedding run took 105.78 s to index the same 200 records, mainly
  because the worker slept between every four-record batch. It now uses short
  bounded batches while a backlog exists, and retains idle/error backoff.
- Crash recovery occasionally tried to reuse the canonical port before all
  killed worker threads had released their sockets. Startup now waits briefly
  for that port without changing the endpoint or stopping an unrelated process.

## Reproduce without touching personal memory

The harnesses create and clean up their own temporary stores and loopback
services. They do not accept an existing database or remote stress target.

```sh
uv sync --frozen --all-extras
uv run python scripts/stress.py --records 10000 --workers 32 --http-calls 1000 \
  --output /tmp/brain-storage-report.json

uv run dots-brain --data-dir /tmp/brain-model-cache model prepare
uv run python scripts/stress_semantic.py \
  --model-dir /tmp/brain-model-cache/models/faf4aa4225822f3bc6376869cb1164e8e3feedd0 \
  --records 200 --workers 8 --queries 96 --output /tmp/brain-semantic-report.json
```

The embedding harness never downloads a model implicitly. Both scripts cap their
requested workload. Keep resource measurements separate from correctness checks:
network proxies, filesystem caches, CPU quotas, and concurrent work affect timing.
