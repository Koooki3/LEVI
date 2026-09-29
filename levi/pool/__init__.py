"""Training pool: every dataset under read-only pool roots, indexed per
episode, composed into new training datasets (docs/TRAINING_POOL.md).

- ``settings``: pool roots, export roots, held-out lists and the path guards;
- ``rules``: data-driven skip, format and category rules;
- ``scanner``: walk, detect, fingerprint, classify, deduplicate, match
  held-out episodes and join human outcome labels into ``pool/index.parquet``;
- ``index``: queries over the index (sources, tasks, episodes);
- ``recipe``: saved, named selections and their preview;
- ``export``: LeRobot v2.1, RECAP value and raw-capture exports with
  ``pool_export.json``;
- ``jobs``: scan and export as tracked worker processes with progress.

Pool sources are never written: every write goes to the workspace's ``pool/``
folder or to a new export directory (``.partial`` first, renamed when done).
"""
