# Stress testing

Stress tests must run against a disposable store with synthetic data. Record the
source revision, environment, workload, limits, and measurements separately from
production claims. Do not access a canonical store, credential, or external client
to improve a benchmark result.

SQLite has one writer at a time; measure contention and failure behavior rather
than inferring capacity from a single successful run. Semantic relevance and large
corpus capacity need their own representative datasets.
