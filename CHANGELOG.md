# Changelog

## Unreleased

- Added an optional SAM3 object annotation integration: revisioned lossless RLE mask/bbox/track sidecars, CPU deterministic demo, human review edits, visual overlays, and source-safe export. The real worker is pinned and isolated in a separate uv environment; core CI never imports Torch or probes CUDA.
- Action Insights now scopes to the full dataset, an episode-index range or a single task, with a sample cap that can be set to "All" for full coverage; the panel reports how many of the in-scope episodes were analysed.
- Added a task filter to the episode sidebar for multi-task datasets, backed by a shared dataset-wide task ↔ episode index.
- Replaced the fixed 120-episode ceiling with a configurable budget, concurrency-limited parquet reads and load progress.
- Autocorrelation now sizes its lag horizon from the 25th-percentile episode length instead of the shortest episode, so one truncated episode no longer blanks the chart on a full-dataset pass.

## 0.2.0 — First public release

- Bundled the complete capture pipeline with strict alignment, source-safe snapshots, configurable maps/thresholds, H.264 encoding and measured statistics.
- Added CLI stages, source-change checks, structured reports and interrupted-job recovery.
- Made workspace configuration portable and removed external project dependencies.
- Consolidated docs and added explicit cache cleanup.

## 0.1.0 — Initial development

- Created LEVI from the Apache-2.0 LeRobot Dataset Visualizer baseline, retaining upstream functionality and attribution.
- Added original visual design, Chinese-default/English interface and the requested strawberry demonstration cards.
- Added independent uv environment, local Bun bootstrap, dual-service launcher and production packaging.
- Added local dataset registration, Range serving, review manifests, version-aware diagnostics and conversion job integration.
- Extended annotations to v2 metadata; added sidecar persistence, non-overwriting exports and stale-response protection.
- Added backend tests, real-browser validation, conversion integration check, documentation and publishing CI.
