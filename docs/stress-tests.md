# Performance and retrieval checks

The v2 implementation branch carries no production performance claim. Run
[storage/HTTP stress](../scripts/stress.py) and
[semantic stress](../scripts/stress_semantic.py) only against disposable data.
Use the same pinned model, corpus, expected source/revision answers and host when
comparing the baseline and candidate. Keep cold preparation and steady-state
latency separate. Do not turn a count of hits or one elapsed time into a general
quality or speed guarantee.

The acceptance target is no worse than the v2 baseline on a predeclared neutral
corpus, plus the owner's relevance assessment. Known short-word weakness is
recorded; improving it is outside this batch. Correct revision and body retrieval
are independent requirements. See [verification](verification.md).
