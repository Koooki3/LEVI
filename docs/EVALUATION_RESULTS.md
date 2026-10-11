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

This first interface indexes existing campaign reports. Automatic campaign registration remains a follow-up; live-session exports and background ended-session registration are available. the catalog alone does not enable those producers or demonstrate GPU/robot performance.

## Live-session exports

Use `levi results live-export --root lab/evaluation-results --live-dir <live-workspace>/live --rollout-root /path/online_rollout_data/models --session-file /path/online_rollout_data/models/.eval_sessions/<session>.json`. Only an explicitly identified finished session is exported. Matching canonical dataset sources and episode run_ids are required; unknown or blinded/campaign-bound sessions are refused. Existing live statistics generate data/analysis.json, data/records.json, bilingual summaries and a success SVG with human and automatic labels kept separate. Only committed labelled episodes are summarized; missing historical coverage is explicitly unknown. Versions and sources are retained, no rollout copying. The background quiet-analysis hook indexes terminal sessions at <live-workspace>/outputs/LEVI/evaluation-results, safely skipping unknown or active sources. Manual CLI roots may point to lab/evaluation-results. Each session keeps distinct reports/<label-basis>/references.

AERI sessions without explicit public-analysis provenance are not automatically exported. Use the campaign report with its state and blinding gate. A session identity already bound to another rollout root is rejected rather than overwritten.


## Read-only live result API

The results page and exact session page use `GET /api/levi/live/results` and `GET /api/levi/live/results/session?root=<configured-root>&group=<model>&task=<task>&session=<run-or-session-id>`. The session request also accepts `session_id` and `run_id` as consistency checks. Roots must exactly match server configuration; identity components cannot select paths. The list accepts `dataset` (a canonical dataset or a verified viewer alias) and `run_id` (an actual temporal/review annotation run, not an evaluation run). Repeated names across roots and distinct runs within one task remain separate.

The JSON schema is `levi.live.result-index.v1`. Each item includes `identity`, `status`, `result_visibility`, `statistics_visibility`, `analysis`, `records`, `runs`, `viewers`, `reports`, `policy`, `source` and `comparison`. `analysis` reuses the existing live summary, with human and automatic labels separate. `result_visibility` is `visible`, `withheld` or `unknown`; the new session analysis requires `visible` and a non-null `analysis`. Active, blinded, unknown, rebound or changed sources have no analysis, records, annotation-run links or report files. Both independent capture markers (`metadata.aeri` and `metadata.eval.aeri`), including episode roles, are checked even when older session/state files omit AERI metadata. A readable legacy header without AERI markers may remain eligible; missing or unreadable headers cannot authorize analysis.

`statistics_visibility` independently controls the existing live statistics, exports and recent episodes. A trusted ordinary non-AERI, non-campaign session can remain `visible` while running, with its exact canonical datasets retained; its new terminal-session analysis remains withheld. Campaign statistics stay withheld and use the original campaign report gate. The real `/stats`, `/stats/export` and `/analysis` handlers verify canonical source/session/header identities using the same request snapshot and original aggregation functions. Mixed blind or unknown scopes return HTTP 409, and a verified filtered legacy scope remains available. Dataset-level incomplete/discarded aggregates without a run/header binding cannot authorize old analysis. Filtered statistics `datasets` now lists the authorized scope, rather than global choices. Successful statistics JSON (including JSON exports) includes `source_identities`, each with canonical `root`, `group`, `task`, `run_id`, `session_id` and `datasets`, authenticated from the actual consumed sources. The page compares these with its selected result identity; a dataset/run reused under another root between requests cannot be displayed as the old root.

Statuses include `active`, `waiting_annotation`, `partial`, `reported`, `stale`, `missing_source`, `archived`, `rebound`, `unknown` and `blocked`. These describe availability and provenance, not model quality. Source and label replacement during a request invalidates the response. Downloads at `/api/levi/live/results/session/download` accept only the same identity, `format=json|markdown` and `lang=en|zh`. They generate a current read-only snapshot with an explicit digest; they never publish or automatically export it. Catalog reports retain their own basis/version and indicate whether their recorded source hashes match the current snapshot. Stored external report paths are not followed or made into download URLs. Historical catalog identities remain discoverable as withheld references with unverified versions when canonical session visibility is unavailable.

Viewer links use verified native/product catalog mappings and the existing `episode_<index>` route. Annotation links additionally require the actual run to be bound to the native dataset in the read-only run store and the correct live workspace UI to be available. A product viewer alias does not imply that its product run store contains the live annotation run. Missing associations remain plain references.

Campaigns reuse their existing terminal-state and blinding report gate. Their analysis is never presented as a live session summary. Comparison requires an explicit campaign, exact child run/arm mapping, and matching frozen policy and settings hashes. Reset, label basis, early-stop rules, coverage, run commits and environment versions come from the existing report; differences or unverified conditions prevent a comparable result. Model names and times do not establish comparability. Missing historical incomplete/discarded session coverage remains unknown.

Each request shares source reads and parsed indexes, then rechecks the pinned snapshot without a persistent cache. Scans are bounded to 1,000 sessions per configured root, 1,000 dataset states, 1,000 catalog references and 16 MiB per file. A request retains at most 32 MiB of source bytes and admits at most 250,000 parsed JSON/TOML values and 50,000 JSONL record lines; these are engineering budgets. File sizes are checked against the remaining byte budget before allocation; TOML configuration has an additional 64 KiB input limit checked before reading and parsing (`config_bytes` in `limits`/`truncated`); the default CLI configuration loader is unchanged. Configuration, campaign plans/journals/reports, annotation-run JSON and linked workspace catalogs share the same budgets. JSON value starts are counted before decoding, and final generation checks reject changed files before reading a larger replacement. Loader dependencies stay internal and are never HTTP query parameters. The list returns `complete`, `truncated` and `limits`; truncation withholds derived results instead of claiming a complete scope. These routes do not start jobs, write capture data, register exports or mutate workspace state.
