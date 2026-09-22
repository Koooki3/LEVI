# Data quality and curation

LEVI checks a dataset at two levels. **Structural checks** run without a model and read every episode: timestamps, numeric features, metadata, video integrity. **Content review** — is this demo the task it claims to be, did it succeed, when did each attempt happen — needs someone to look, and LEVI gives that look to an agent under human review. Neither level changes the source data.

## Structural checks (Doctor)

Open a dataset and choose **Doctor**, or call it from an agent or script:

```bash
# agent / MCP
quality.inspect {"repo_id": "local/my_dataset", "decode_video": true}
# REST
POST /api/levi/diagnostics {"repo_id": "local/my_dataset", "max_episodes": 0, "decode_video": true}
```

Both run the same checks and keep the report with the dataset, one per hour: `outputs/LEVI/datasets/<name>/reports/quality-<YYYYmmddTHH>.json`. A re-run in the same hour replaces that hour's report. **Export report** in the browser downloads `<name>-quality-<YYYYmmddTHH>.json`. For a local dataset the Doctor checks every episode and decodes video samples by default; Hub datasets default to a sample, because analysing them downloads data. The result states its scope (`all` or `sample`).

| Group | What is checked |
| --- | --- |
| Metadata & format | Accepted version, task metadata, frame totals, portable file templates |
| Temporal consistency | Missing or non-finite timestamps, duplicates, ordering, spacing against the declared FPS, non-consecutive frame indices |
| Action quality | Missing fields, inconsistent shapes, NaN/Inf, action jumps beyond mean + 8σ |
| Video integrity | Optional first/middle/last-frame decode, FPS agreement, near-identical sampled frames |
| Distribution, episode health, features | Extreme values beyond 10σ, episode lengths, feature consistency across episodes |
| Anomalies | Constant actions or actuators (often intentional, e.g. a binary gripper) |
| Training readiness, portability | Stored normalization statistics, relative paths |

Counts are per individual result, not per heading. Thresholds are heuristics: a flagged episode is a review candidate, not a verdict. The native Doctor is version-aware and does not treat v2.1 metadata layout as corruption, so its counts differ from the upstream external Doctor (still linked for Hub datasets). **Flag findings for review** adds the flagged episodes to the dataset's review list.

## What structural checks cannot see

Real example from a policy-rollout capture of 214 demos: the structural check passed with warnings, yet 7 of the first 20 demos showed a different task entirely (plates, not screws), and the metadata labelled all of them as the screw task. Other problems of the same kind:

- a camera that froze mid-recording and kept writing the same frame;
- success/failure labels that disagree with what the video shows;
- an event (a lift, a release, a hand entering) that happens between the frames a coarse pass sampled.

These need content review.

## Content review

Content review is an agent task (see [Agents](AGENTS.md)), planned and approved like any other:

| Task type | Produces | Typical use |
| --- | --- | --- |
| Dataset review | Per-episode verdicts: `outcome` (success / failure / unknown) and `issue` flags with a written reason | Find mixed-in tasks, check outcome labels, flag broken episodes |
| Video subtasks and events | Time segments per attempt with subtask, outcome and uncertainty | Subtask labels for training, failure analysis |
| Visible object masks | Masks and tracks per object | Object-centric training data |

Choose cameras for what must be judged: a wide camera shows which task and scene; a wrist camera shows small parts (whether a screw is seated). What the reviewer accepts is published as a revision with provenance; nothing is inferred into a label without that review.

## Labels, flags and the review list

- **Outcome labels.** Click an episode's dot in the sidebar to label it success or failure; human labels override metadata and are written as `levi_outcome` in exports. They feed the Failures filter and the RECAP export ([RECAP](RECAP.md)).
- **Review flags.** Mark episodes to exclude or revisit; the review list is saved per dataset (`outputs/LEVI/workbench/reviews/<name>.json`) and exported as a `levi.review.v1` manifest, `<name>-review-<YYYYmmddTHH>.json`. Flags never delete source data.
- **Carry-over.** Annotations and labels made on a raw capture move with it into every conversion, matched by source demo ([Conversion](CONVERSION.md)).

## What the workspace remembers

Committed review work becomes the dataset's local memory (`outputs/LEVI/workbench/memory/<name>.json`): the verdicts per episode, how many episodes belonged to another task, the outcome counts, and the rules a reviewer wrote down. The next task on that dataset starts with them. See [Agents → The harness](AGENTS.md#the-harness).
