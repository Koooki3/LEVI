"""Performance baseline: a read-only trace view over the records LEVI already
keeps, and a CPU micro-benchmark. Documented in docs/PERFORMANCE.md.

Nothing here stores anything of its own. ``trace`` merges ``live/stats.jsonl``,
``live/gate.jsonl``, the agent usage samples and the harness cost records;
``bench`` times the CPU-side steps (hashing, presentation-timestamp scan, pixel
statistics) on synthetic video in a scratch directory it removes.

``budget.DiskBudget`` is a cache folder with a hard size limit (LRU, TTL, version
invalidation, atomic writes, crash recovery). It is a library only: nothing in LEVI
uses it yet.
"""
