"""Performance baseline: a read-only trace view over the records LEVI already
keeps, and a CPU micro-benchmark. Documented in docs/PERFORMANCE.md.

Nothing here stores anything of its own. ``trace`` merges ``live/stats.jsonl``,
``live/gate.jsonl``, the agent usage samples and the harness cost records;
``bench`` times the CPU-side steps (hashing, presentation-timestamp scan, pixel
statistics) on synthetic video in a scratch directory it removes.
"""
