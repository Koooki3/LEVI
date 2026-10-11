# Evaluation results catalog

Use one catalog to find the existing report by the model and task directory names of your rollouts. The catalog does not compute statistics, move captures or copy videos. A cross-model comparison remains one campaign report; each model/task/session entry references it.

```text
evaluation-results/
  INDEX.md
  index.json
  models/<model>/<task>/sessions/<session>/reference.json
  models/<model>/<task>/sessions/<session>/versions/<digest>.json
  campaigns/<campaign>/<label-basis>/reference.json
```

Register an existing published campaign report with the exact frozen model/task mapping:

```sh
levi results register --root lab/evaluation-results \
  --report <workspace>/campaigns/<campaign>/report/operator_label \
  --rollout-root /path/online_rollout_data/models \
  --rollout-dir /path/online_rollout_data/models/<model>/<task> \
  --model <model> --task <task> --session <session>
levi results list --root lab/evaluation-results
```

The rollout folder must exactly match the root/model/task arguments. Checkpoint filenames are not used to guess a model name. The explicit mapping is recorded as caller supplied; it must come from the frozen campaign configuration. Sources stay in place. The catalog verifies every source artifact hash, retains immutable reference versions, and publishes current pointers atomically under one catalog lock. INDEX.md links to the report directory; the report contains its existing data, tables and charts.

Every entry records the label basis, campaign state, source report manifest hash, artifact hashes and rollout location. Human, automatic and adjudicated label bases remain distinct. In-progress or blinded reports receive only a campaign progress reference; model/task/session mappings, arm metadata and analysis paths are not indexed until analysis is permitted. A missing, changed, malformed or symlinked source is refused; retry after publication instead of indexing a partial report.

This first interface indexes existing campaign reports. Automatic registration from campaign/live services and standalone live-session analysis exports need integration; the catalog alone does not enable those producers or demonstrate GPU/robot performance.
