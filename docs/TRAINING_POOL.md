# Training pool / 训练池

The training pool indexes every dataset under a set of read-only folders, one row per episode, and composes new training datasets from it: pick categories, sources and an ordered list of tasks, preview the counts, and export a merged LeRobot v2.1 dataset (openpi / π0.5), a RECAP value dataset or a copy of the raw captures. Code: `levi/pool/`.

训练池把若干只读目录下的所有数据集逐片段登记，再按类别、来源和有序任务列表组合成新的训练数据集：可以先预览数量，再导出合并后的 LeRobot v2.1 数据集（openpi / π0.5）、RECAP 价值数据集或原始采集目录副本。代码在 `levi/pool/`。

## Guarantees / 保证

- **Sources are read-only.** The pool only reads `LEVI_POOL_ROOTS`. Its own files live in `<workspace>/pool/`; an export goes to a new folder, written as `.<name>.partial` and renamed when complete. An export may not lie inside (or contain) any indexed source dataset, must lie inside `LEVI_EXPORT_ROOTS`, and its folder must not exist yet.
- **Held-out episodes are never exported.** The lists in `LEVI_POOL_HELDOUT` (for example `levi-hub/gold/frozen/plates-frozen-v1.json`) are matched by path and by video sha256; every copy, filtered variant or conversion of a held-out episode is held out too. A recipe cannot include them, and the export checks again — by path, by sha256 and against the index — and refuses the whole export if one slipped into a plan. This is a refusal, not a filter default.
- **One recorded episode counts once.** Copies are grouped (see [Grouping](#grouping-copies-variants-conversions--副本与版本归并)); an export takes the canonical member unless the recipe names only other sources.
- **Traceable.** Every export writes `pool_export.json`: the recipe, the task order, each episode's source path, group, fingerprint, outcome and its source, policy fields (`policy_model`, `policy_checkpoint`, `policy_method`, `policy_phase`, `policy_label`) and why it was picked (`selection_stratum`, `quality_score`, `selection_reason`), per task what was asked for and what was picked (`selection`), every exclusion with its reason, the LEVI commit and the format parameters.

- **源数据只读**：只读取 `LEVI_POOL_ROOTS`；训练池自己的文件在 `<workspace>/pool/`；导出写到新目录，先写 `.<名称>.partial`，完成后改名。导出目录不能在任何已登记的源数据集内部（也不能包含源数据集），必须在 `LEVI_EXPORT_ROOTS` 之内，且事先不存在。
- **留出（冻结测试）片段永不导出**：`LEVI_POOL_HELDOUT` 中的清单按路径和视频 sha256 匹配，留出片段的副本、过滤版本和转换结果同样视为留出。选择无法包含它们；导出时再按路径、sha256 和索引各查一遍，只要有一条混入就拒绝整个导出。
- **同一次录制只算一次**：副本归为一组，导出默认只用规范来源。
- **可追溯**：每次导出写 `pool_export.json`（选择条件、任务顺序、每个片段的来源路径与指纹、排除清单及原因、LEVI commit、格式参数）。

## Settings / 设置

| Setting | Meaning | Default |
| --- | --- | --- |
| `LEVI_POOL_ROOTS` | Comma-separated folders the pool reads, never writes | unset: the training pool is idle |
| `LEVI_EXPORT_ROOTS` | Comma-separated folders an export may be written under | the workspace |
| `LEVI_POOL_SSH` | The SSH client of remote transfers (default `ssh`; tests point it at a stand-in) | `ssh` |
| `LEVI_POOL_HELDOUT` | Comma-separated held-out lists (JSON with `episodes: [{path, sha256: {video: hex}, frame_count, frozen_id}]`; `path` relative to a pool root or absolute; other keys such as `dev_pool` are ignored), or `none` for a pool without a held-out set | unset: **exports are refused** |

An export also needs the held-out setting: unset, planning and running an export are refused (a missing setting must not let frozen test episodes through). The lists in force must be the ones the last scan used — otherwise the index does not mark them — so a change asks for a new scan; held-out entries that match no indexed episode are listed as a warning (`heldout_unmatched`).

没配置 `LEVI_POOL_HELDOUT`（也没写 `none`）时拒绝导出；导出时的清单必须和上次扫描用的一致，否则要求重新扫描；匹配不到片段的清单条目会给出警告。

A relative export name lands in `<workspace>/exports/pool/<name>`.

## Scanning / 扫描

```bash
LEVI_POOL_ROOTS=/data LEVI_POOL_HELDOUT=/data/frozen/plates-frozen-v1.json uv run levi pool scan [--rehash]
uv run levi pool status
```

The scan walks the roots and writes `pool/index.parquet` (one row per episode), `pool/sources.json` and `pool/scan.json` (the summary). A re-scan reuses every episode whose files did not change (size and modification time); `--rehash` recomputes everything. From the service, `POST /api/levi/pool/scan` runs the same scan as a worker process with progress (tracked like every other LEVI worker, stopped with the service).

扫描遍历根目录，写出 `pool/index.parquet`（每片段一行）、`pool/sources.json` 和 `pool/scan.json`。再次扫描时文件未变（大小与修改时间）的片段直接复用；`--rehash` 全部重算。服务里 `POST /api/levi/pool/scan` 以后台作业运行，有进度。

### Formats / 格式

| Format | Detected by | Exportable |
| --- | --- | --- |
| Raw capture (`robot_capture`) | a folder with `end_effector_pose.csv`; standard name `demo_NNNN` | yes |
| LeRobot v2.x (`lerobot`) | `meta/info.json` | yes; v3 is listed without episodes and not exported |
| DROID raw (`droid_raw`) | `trajectory.h5` + `metadata_*.json` | listed only (no action mapping to the pool's schema) |
| Pickle / npz / npy folders | data files of at least 1 MB | listed as sources, not exported |

Raw episodes with a non-standard folder name (`demo_0022 copy`, `demo_0038_failure`) or a task folder nested inside another task folder are listed with `nonstandard: true` and left out of exports unless a recipe sets `include_nonstandard`.

非标准目录（`… copy`、`_failure`、误嵌套的任务目录）登记为 `nonstandard`，默认不导出。

### Rules / 规则

Skip, category and format rules are data, not code (`levi/pool/rules.py`); `<workspace>/pool/rules.json` may override any top-level key. Defaults:

- never descended: `.git`, `.venv`, `venv`, `node_modules`, `.cache`, `__pycache__`, `wandb`, `checkpoints`, `site-packages`, a root's top-level `models`, a LEVI workspace's `outputs/LEVI`, `agent/datasets`, `.state/tmp`, `.state/logs`, `*.partial`, symlinked folders;
- categories, first match wins: `_archive/` paths → `archive`; DROID raw → `external`; a dataset with a LEVI marker (`meta/levi_conversion.json`, `meta/levi_recap.json`, `pool_export.json`, …), anything below a folder holding `pool_export.json` or `.levi-export.json` (a raw-capture export lying in a pool root), or inside a LEVI workspace (a folder holding `outputs/LEVI/workbench/datasets.json`) → `levi`; `data_source: policy_rollout` → `rollout`; `data_source` of a human collector or a teleoperation `control_mode` (`pygame`, `spacemouse`, `gello`, …) → `human`; a raw capture without `data_source` → `human`; a LeRobot dataset converted from indexed raw episodes → the category of those episodes; any other LeRobot dataset → `human` (`lerobot_default_category`).
- tasks are normalised for grouping: underscores to spaces, lower case, single spaces (`Pour_water_into_brown_cup` and `pour water into brown cup` are one task); the original spellings stay listed.

分类规则全部是数据：`_archive/` → 归档；DROID → 外部；有 LEVI 标记或在 LEVI 工作区内 → LEVI 处理后；`data_source: policy_rollout` → rollout；遥操作 `control_mode` 或无 `data_source` 的原始采集 → 人工。任务文本规范化（下划线变空格、小写），原始写法保留。

### Index / 片段索引

Columns of `pool/index.parquet` include `key`, `source`, `format`, `episode`, `task` (normalised) and `task_raw`, `frames`, `fps`, `cameras`, `state_dim`/`action_dim`, `category` and `category_reason`, `data_source`, `control_mode`, the policy columns below, `date`, `robot_flag` (a rollout's own success flag), `human_label` (from any LEVI workspace under the roots, read-only), `outcome` and `outcome_source` (human label first, then the robot's flag), `nonstandard`, `exportable`, `fingerprint`, `content_hash`, `recording`, `filtered`, `group`, `canonical`, `copies`, `heldout`, `heldout_set`, `heldout_id`.

### Which policy produced a rollout / 是哪个策略产生的 rollout

A rollout's `policy` used to be one value (the checkpoint name), so a direct deployment, an online-RL run and a student-policy run of one checkpoint looked alike. The index now keeps them apart (schema `levi.pool.index.v2`; an older index asks for a rescan). Code: `levi/pool/policy.py`; nothing in it is task specific.

| Column | Meaning |
| --- | --- |
| `policy_model` | the model config, e.g. `pi05_fr3_all_state` (`policy.config`; then `policy.server_metadata.config`, a string `policy`, the `models/<policy>/` or `online_rl/<method>/<policy>/` folder) |
| `policy_checkpoint` | the checkpoint folder name, e.g. `pi05_fr3_all_step49999` (`policy.checkpoint_dir`); two models may share one checkpoint name |
| `policy_method` | how it was run: `direct` (direct deployment), `dsrl`, `rlt`, `sfe`, `student`, `other` (a method the pool does not know) or `unknown` (a rollout with no policy information at all) |
| `policy_phase` | `policy.phase` (`eval_ckpt`, `prior`, `sac`, `student`, …) |
| `policy_label` | the four above in one line: `pi05_fr3_all_state · step49999 · DSRL online RL` |
| `policy` | the old column, kept for saved recipes: the checkpoint, else the model |

`policy_method` is read in this order: `policy.method`, the top-level `method`, `control_mode` (`policy_rollout` = direct; `dsrl_online_rl`, `rlt_online_rl`, `sfe_online_rl`, `student_policy_rollout`), then the folder layout (`models/…` = direct, `online_rl/<method>/…`). A value the pool does not know never hides a known one further down. A teleoperation mode is not a policy method, so only rollouts get a method (or `unknown`); a human capture has all of these empty. A LeRobot rollout takes the fields of its linked raw capture (the existing conversion links, `rollout_source_demo` first), and for what that leaves empty its own `rollout_source_method` (`models` = direct), the folder in `rollout_source_demo` and `rollout_policy`.

旧的 `policy` 只有一个值（检查点名），直接部署、在线 RL 和学生策略的同一检查点看起来一样。索引现在把它们分开：`policy_model`（模型配置）、`policy_checkpoint`（检查点）、`policy_method`（运行方式：`direct` 直接部署、`dsrl`、`rlt`、`sfe`、`student` 学生策略、`other`、`unknown` 未知）、`policy_phase` 和一行可读的 `policy_label`。运行方式按 `policy.method`、顶层 `method`、`control_mode`、文件夹路径的顺序判断；没有任何策略信息的 rollout 记为 `unknown`；LeRobot rollout 继承所链接原始采集的字段，缺的部分再用 `rollout_source_method` 和 `rollout_source_demo` 的路径补。旧列 `policy` 保留（检查点，否则模型），旧配方照常可用。索引版本升为 v2，旧索引会要求重新扫描。

### Grouping: copies, variants, conversions / 副本与版本归并

Rows are grouped when they share any of:

- the **fingerprint** of a raw episode: md5 of `frames.csv` (else `end_effector_pose.csv`) plus every video's name and size — byte copies;
- the **recording** identity: the normalised task folder and the collector's `created_at`/`stopped_at` — a filtered variant (`filtered_from_frame_count` in its metadata) belongs to its original;
- a **conversion link**: a LeRobot episode's `rollout_source_demo`, `meta/processed_demos.json` or LEVI's `source_demo`;
- the **content hash** of a LeRobot episode (md5 of its state and action arrays) — LeRobot copies.

The canonical member is the original: not archived, not in a LEVI workspace, a raw capture before a LeRobot conversion, unfiltered before filtered, the shallowest path. Human labels and held-out marks apply to the whole group. A LeRobot dataset with no provenance whose episodes differ from every other dataset (for example an older conversion of a different filtering) cannot be linked to its raw captures and appears as a separate human source; pick sources explicitly when that matters.

分组依据：原始片段指纹（字节副本）、录制身份（任务目录 + 开始/结束时间，过滤版本归入原件）、转换链接（`rollout_source_demo`、`processed_demos.json`、`source_demo`）、LeRobot 内容哈希。规范成员是原始采集或 rollout 目录。没有来源记录的 LeRobot 数据集无法关联到原始采集，会作为单独来源出现。

## Recipes / 选择

A recipe is a named, saved selection (`pool/recipes/<name>.json`):

| Field | Meaning |
| --- | --- |
| `categories`, `sources`, `formats`, `policies`, `date_from`, `date_to` | filters (`sources` also orders episodes within a task); `policies` is the old checkpoint filter |
| `policy_models`, `policy_checkpoints`, `policy_methods` | policy filters (see above; `policy_methods` values: `direct`, `dsrl`, `rlt`, `sfe`, `student`, `other`, `unknown`); combined with AND, values within one list with OR |
| `tasks` | ordered list of task entries `{task, count, success_ratio, strategy}` (see [Choosing episodes](#choosing-how-many-episodes-a-task-contributes--每个任务取多少片段)); the export follows this order. A bare task text (the old form) still loads: it means `count` unset, `strategy: random` |
| `outcome` | `all`, `robot_flag_success` (the robot's flag), `verified_success` (a human label first, then the robot's flag; the preview counts `outcome_sources` and warns how many rest on the operator's key press only) or `human_verified_success` (a human label only). An episode whose human labels disagree is left out of both verified outcomes and of RECAP exports (`label_conflict`) |
| `per_task_cap`, `seed` | the count of a task entry that has none of its own (at most this many episodes per task); `seed` makes every draw reproducible |
| `include_nonstandard`, `exclude` | non-standard folders in; episode keys out |
| `allow_unlinked_sources` | see below |
| `task_text` | normalised task → the text written into the export |

The selection runs in this order: held-out out (always), `exclude`, non-standard and unsupported formats out, one episode per group, the outcome filter, the export format's own need (a raw copy takes raw captures; a RECAP value export needs an outcome), then, per task in the task order, the choice of how many and which episodes. A task taken from raw captures **and** from a LeRobot dataset that no scan link ties to them (no provenance, different content hash) may be one recording twice. The preview warns (`possible_unlinked_conversion`) and planning an export refuses, unless the recipe names its `sources` or sets `allow_unlinked_sources`. Preview `warnings` also carry the held-out problems above; a `blocking` one stops an export.

同一任务同时取自原始采集和无法关联的 LeRobot 数据集时，可能是同一批录制的重复：预览警告，导出默认拒绝，指定 `sources` 或设置 `allow_unlinked_sources` 后放行。

The preview lists episodes and frames per task and every exclusion by reason (`heldout`, `duplicate`, `nonstandard`, `unsupported`, `outcome_filter`, `no_outcome`, `not_selected` (over the task's count), `per_task_cap` (over the global cap), `already_in_composition`, …).

```bash
uv run levi pool recipe save pi05-mix --category human --task "pick fork into green plate:count=60,success=50%" \
    --task "pick apple on green plate" [--per-task-cap 50 --seed 1] [--outcome verified_success] \
    [--source <id>] [--policy-model <config>] [--policy-checkpoint <name>] \
    [--policy-method direct|dsrl|rlt|sfe|student|other|unknown] [--policy <checkpoint>] [--date-from 2026-09-01] [--exclude <episode key>] \
    [--task-text "pick fork into green plate=Pick the fork into the green plate"] [--file recipe.json]
uv run levi pool recipe show pi05-mix [--format recap_value]     # the recipe and its preview
uv run levi pool recipe episodes pi05-mix --task "<task>"          # the episodes picked for one task, with score and reason
uv run levi pool recipe suggest pi05-mix --task "<task>"           # availability and a balanced default count for adding a task
uv run levi pool recipe list | delete <name>
```

### Choosing how many episodes a task contributes / 每个任务取多少片段

Some tasks have hundreds of episodes. Each task entry says how many it contributes and which ones:

| Field | Meaning |
| --- | --- |
| `count` | how many episodes; unset = the recipe's `per_task_cap`, else every episode left after the filters |
| `success_ratio` | target share of successes among the picked episodes, 0..1; unset = keep the task's own share |
| `strategy` | `quality` (default in the page and for an entry with options), `random` (a seeded draw), `first` (the first episodes of the index: source order, then episode number) |

On the command line a task is `text` (the old form) or `text:count=50,success=0.6,strategy=quality`: `count` is a number or `all`; `success` is 0..1, `60%` or `natural`; `strategy` is `quality`, `random` or `first`. The options follow the last colon and count only when every part is a known `key=value`, so a task text may contain colons and commas.

**How the pick works.** It runs per task, in the task order, on the episodes left after every filter above (held-out, duplicates, non-standard, unsupported formats, the outcome filter, the category, source, policy and date filters).

1. *Split by outcome.* Successes and failures follow the same outcome the export writes: a human label first, then the robot's flag. Episodes with no outcome, and (for `quality`) episodes whose human labels disagree, are a last resort: they are picked only when the count cannot be met otherwise, and reported as `selected_unknown`. A task with no recorded outcome at all (human demonstrations) is taken as it is.
2. *Quota.* Without `success_ratio`, `quality` keeps the natural share of the task's successes and failures (rounded); `random` and `first` draw over the whole task, which keeps it in expectation. With `success_ratio`, `round(count × ratio)` successes and the rest failures, for every strategy. If one side has too few, the other side fills the gap and the shortfall is reported (`shortfall_successes`, `shortfall_failures`, note `success_short` / `failure_short`). If fewer episodes exist than `count`, all are taken and `shortfall` says how many are missing.
3. *Quality score* (strategy `quality`), 0..1: `0.40 × trust + 0.30 × completeness + 0.30 × fit`.
   - *trust*: a human label 1.0, a demonstration 0.8, the robot's flag 0.6, no outcome 0.3, conflicting human labels 0.
   - *completeness*: 1 for a frame count between half and twice the task's median; it falls to 0 at 0.15 × the median (an aborted recording) and at 4 × (a run that never ended).
   - *fit* for a success (efficiency): the episode's frame count is ranked among the task's successes; the lower middle (15th to 55th percentile) scores 1, the shortest a little less (often truncated), the longest, slow and hesitant, down to 0. *Fit* for a failure: whether it is long enough to hold an attempt, `frames / (half the median success length)`, at most 1, so near-zero frames rank last.
   - A LEVI workspace's RECAP advantage labels (`recap_values`, found by the scan) only break ties between episodes whose scores are within 0.02; they never outrank the score. Without them nothing changes.
4. *Diversity.* Within each side, episodes are grouped into strata (`policy_method | policy_checkpoint | source | day`). The pick takes the best episode of the stratum that has given the fewest so far, round after round, so a task's picks do not all come from one run. An episode scoring under 0.4 waits until no stratum has a better one.
5. *The composition as a whole.* A newly added task defaults to the median count of the tasks already added (100 when none; never more than what is available), so the composition stays balanced. An episode (or a copy of it) picked for an earlier task is not picked again (`already_in_composition`). If the composition leans to one category or method (80% or more), the preview shows it in `mix.lean`; nothing is reweighted.

The choice is deterministic for a recipe, its `seed` and the index, and one function serves the preview, the export plan and `POST /selection`, so they cannot differ. `pool_export.json` records per episode `task`, `selection_stratum`, `quality_score`, `selection_reason`, `outcome` and `outcome_source`, and per task (`selection`) `requested`, `available`, `successes`, `failures`, `selected`, the selected successes and failures, `shortfall`, `shortfall_successes`, `shortfall_failures`, `notes` and `exported`.

部分任务有数百个片段，任务条目因此可以指定取多少、取哪些：`count`（片段数，缺省用 `per_task_cap`，再缺省取全部）、`success_ratio`（成功占比 0..1，缺省保持该任务原有比例）、`strategy`（`quality` 智能选取、`random` 按种子随机、`first` 按索引顺序）。命令行写法 `文本:count=50,success=0.6,strategy=quality`（`count` 为数字或 `all`，`success` 为 0..1、`60%` 或 `natural`）；不带选项的文本仍是旧写法（按种子随机）。旧配方（任务文本列表加全局 `per_task_cap`）载入后含义不变。

选取规则：在所有过滤之后，按任务顺序逐个任务进行。（1）按结果分成功/失败，口径与导出一致（人工标签优先，其次机器人标志）；没有结果的片段、以及人工标签互相矛盾的片段（智能选取时）只在凑不够数量时才用，并单独报告；完全没有结果的任务（人工示范）原样取。（2）配额：不指定成功占比时智能选取保持自然比例，指定后取 `round(片段数 × 占比)` 个成功，其余为失败；某一类不够时由另一类补足并报告缺口；可用片段少于要求时全部取走并报告缺少的数量。（3）质量分 = 0.40 × 标注可信度 + 0.30 × 完整度 + 0.30 × 适配度：人工标签 1.0、示范 0.8、机器人标志 0.6、无结果 0.3、标签矛盾 0；片段长度在任务中位数的 0.5–2 倍内为满分，短于 0.15 倍（中途中止）或长于 4 倍（一直没结束）降为 0；成功片段偏好“较高效”的（帧数排在该任务成功片段的 15%–55% 分位），失败片段需要足够长以包含一次尝试（帧数至少约为成功片段中位数的一半）；RECAP 优势标签只在分数相差不到 0.02 时用来打破平局。（4）多样性：按 策略方法 | 检查点 | 来源 | 日期（按天）分层，轮流从已选最少的层里取最好的，避免全部来自同一次运行；低于 0.4 分的片段最后才用。（5）整体：新添加任务的默认片段数为已添加任务片段数的中位数（没有时为 100，不超过可用数）；前面任务已选的片段不会重复选取；组合明显偏向某一类别或方法（≥ 80%）时预览会显示，不会自动重新加权。预览、导出计划和 `POST /selection` 使用同一个函数，结果一致；`pool_export.json` 逐片段记录所属任务、分层、质量分、入选原因、结果及其来源，并按任务记录要求数、可用数、实际数和缺口。

## Exports / 导出

```bash
uv run levi pool export pi05-mix --format lerobot_v21 --name pi05-mix-v1 \
    [--output-dir /data/exports] [--fps 10] [--camera-map observation.images.wrist=observation.images.hand] \
    [--hardlink] [--human-as-success] [--timing resample|retime] [--filter-static|--no-filter-static] [--dry-run]
```

| Format | What it writes |
| --- | --- |
| `lerobot_v21` | One LeRobot v2.1 dataset for openpi / π0.5. Raw captures go through LEVI's conversion pipeline (the same checks and options as `levi convert`: resample to `fps`, static-frame filter); LeRobot v2.x episodes are copied with new indices. Episodes follow the recipe's task order, then source order, then episode order; one merged task table. Camera keys become `observation.images.hand` / `observation.images.view1` (`cameras` maps raw capture cameras, `camera_map` LeRobot keys). Every source must share fps, state and action dimensions and video resolution — otherwise nothing is written and the refusal lists the differences. Raw captures failing the capture checks are left out and listed (`conversion_preflight`). |
| `recap_value` | The same plus RECAP rewards, returns and labels ([RECAP](RECAP.md)); raw captures are retimed without filtering (one row per executed step). The outcome is the human label, else the robot's flag; episodes with neither are left out (`no_outcome`), and an episode with conflicting human labels is left out (`label_conflict`) instead of falling back to the operator's key. `--human-as-success` counts human-category episodes without an outcome (demonstrations, RLinf's `sft`) as successes, recorded as `outcome_source: sft_demonstration`. |
| `raw_capture` | Raw capture folders copied into `<task>/demo_NNNN`, renumbered per task, with `task_description.txt`. With `--hardlink` only the videos are hard-linked (they are most of the bytes; never edit them in place); metadata and CSV files are always copied. A task text that is empty, `.` or `..` becomes the folder `task`, and every path written is checked to lie inside the staging folder. |

`pool_export.json` also records the effective conversion parameters (`conversion`: fps, timing, static-frame filter, orientation, cameras, …), each episode's `group`, the held-out lists (or `heldout_disabled`) and the warnings that applied. Two tasks that would be written under one text are refused at planning; `camera_map` and `cameras` must map to distinct output keys; LeRobot sources whose `observation.state` names differ from the output's are refused with the differences. A dry run runs the same held-out group check as the export and leaves no plan behind.

Each episode row in `meta/episodes.jsonl` of a LeRobot export carries `pool_key`, `pool_source` and `pool_fingerprint`; `meta/levi_provenance.jsonl` maps new to source episode indices. Not in this version: LeRobot v3 output (needs lerobot's v2.1→v3 converter; convert the v2.1 export with it) and exporting a recipe as a training manifest without copying data. To move an export to another machine see [Remote transfer](#remote-transfer--远程传输).

导出格式：LeRobot v2.1（多来源合并，按任务顺序→来源→片段排序，统一相机键和 fps，状态/动作维度或分辨率不一致时拒绝并列出差异）、RECAP 价值数据集（结局按“人工标签 > 机器人标志”，无结局的片段排除并列出）、原始采集目录（按任务重编号，可硬链接）。暂不支持：LeRobot v3 输出、导出为训练清单。传到其他机器见 [远程传输](#remote-transfer--远程传输)。


## The page / 训练池页面

`/pool` (top navigation **Training pool / 训练池**, and a card on the Workbench). It is the same backend as `levi pool …`.

- **Pool folders** (top): the roots, the last scan time and counts, **Scan now** with a progress bar and Cancel, and the recent jobs (scans, exports, pushes) with their status.
- **Filters** (left): category (原始人工采集 / 原始 rollout / LEVI 处理后 / 外部 / 归档), source (searchable, counts), a task search, outcome (all / robot flag success / verified success), three policy facets (策略模型 / Policy model, 检查点 / Checkpoint, 运行方式 / How it was run: 直接部署, DSRL, RLT, SFE, 学生策略, 未知; each with counts and shown only when the selection holds rollouts) and date. *Show hidden* switches on held-out episodes (留出测试集), copies and the archive; each shows how many it hides.
- **Tasks and episodes** (centre): the task table (episodes per category, frames, success rate, how its rollouts were run, **+ Add** puts the task at the end of the composition; a task with more than 100 available episodes, or more than the composition's balanced count, opens a chooser first: number of episodes (a number field and a slider, default the balanced count), share of successes (natural share / all successes / all failures / custom %) and how to pick (smart pick / random / in order), with a hint of the resulting split and a warning when the share cannot be met; clicking a task narrows the episode table to it) and the paged episode table with source, category, task, frames, outcome, the policy label (rollouts) and badges for held-out, copy, non-standard and not exportable. An episode of a source registered in LEVI links to the viewer; otherwise its path is shown. A held-out row has no checkbox and cannot be part of an export: the server excludes it whatever the page sends.
- **Composition** (right): the ordered task list (drag a task by its grip, or use its up / down buttons; the order is the export order); each task shows *Picked N of M · successes a · failures b* with **Edit** (the same chooser), a warning when the requested mix or count cannot be met, and **Show picked episodes** (the episodes with quality score and why); the per-task cap (for tasks with no count of their own), seed, non-standard folders, the constraints taken from the filters, and a live preview (episodes, frames, held-out excluded, the overall success / failure mix with the category and method shares, and every other exclusion with its reason). Recipes are saved, loaded and deleted by name.
- **Export**: format, dataset name, output directory (a folder outside `LEVI_EXPORT_ROOTS` is flagged in the field and refused by the server), fps, camera mapping, hard links (raw capture copy only), **Dry run** and **Start export** with progress. A finished export links to its `pool_export.json` and offers **Send to remote / 传到远程**.

Page routes beyond the table above: `GET /api/levi/pool/facets` (facet counts and what the toggles hide), `outcome=robot_flag_success|verified_success`, `date_from`, `date_to` on `tasks` and `episodes`, and each episode row carries `viewer` (its LEVI viewer path or `null`).

页面在 `/pool`（顶部导航“训练池”，工作台也有入口），与 `levi pool …` 共用后端：顶部是池目录、上次扫描和“立即扫描”；左侧分面（类别、来源、任务搜索、结局、策略模型、检查点、运行方式、日期，可显示留出测试集、副本、归档并显示被隐藏的数量）；中间是任务表和片段表（已登记的数据集可跳到片段查看器，否则显示路径）；右侧是组合（有序任务列表，可拖动或用上下按钮排序；每个任务显示“选 N / 共 M · 成功 a · 失败 b”，可编辑片段数、成功占比和选取方式，可查看所选片段及质量分；片段数超过 100 的任务添加时先弹出选择面板；每任务上限、种子、实时预览（含成功/失败构成）及各类排除原因、命名保存/载入/删除）和导出面板（格式、名称、输出目录、fps、相机映射、硬链接、试运行、进度、`pool_export.json` 链接）。留出片段在页面上不可选，服务端也会排除。

## Remote transfer / 远程传输

```bash
uv run levi pool remote add gpu1 wk@gpu1.lab:/data/datasets     # or an ~/.ssh/config alias: gpu1:/data/datasets
uv run levi pool remote list | delete <name>
uv run levi pool push <export-dir> --target gpu1 [--dry-run]
```

- **Targets** are named `[user@]host:/path` (host may be an `~/.ssh/config` alias; `--port`), kept in `<workspace>/pool/remotes.json`. Host, user and path are checked against strict patterns: nothing may start with `-`, the path is absolute (or `~/`), and spaces, quotes, `;`, `$`, backticks and `..` are refused. There is no password field anywhere; a spec such as `user:secret@host:/x` is refused.
- **Transfer** is `rsync -a --partial --info=progress2 --protect-args` over `ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o PasswordAuthentication=no`, built as an argument list (no shell). Key or agent login only; the host key must already be in `known_hosts` (LEVI never relaxes host-key checking). The target path must exist on the remote; the export lands in `<path>/<export name>/`. An interrupted push keeps what arrived (`--partial`) and pushing again resumes.
- **What can be pushed**: a finished export made by the pool (a folder holding `pool_export.json`, not a `.partial` one, not a symbolic link) inside `LEVI_EXPORT_ROOTS` or the workspace's export folder. Anything else is refused.
- **Jobs**: `POST /api/levi/pool/push` runs the transfer as a pool job (worker process group tracked in `children.py`, progress parsed from rsync into the job, `POST /api/levi/pool/jobs/{id}/cancel` stops it). `--dry-run` uses `rsync -n`. The SSH client is `ssh`, or `LEVI_POOL_SSH` (used by tests).

远程目标是命名的 `[user@]host:/路径`（host 可以是 `~/.ssh/config` 里的别名），存在工作区 `pool/remotes.json`；各部分严格校验，不允许以 `-` 开头，不接受也不保存密码。传输用 rsync over SSH（`BatchMode=yes`，主机密钥必须已在 `known_hosts`，不关闭 `StrictHostKeyChecking`），参数以列表传递，不经 shell；支持 `--partial` 续传、进度、取消。只有训练池生成的、位于 `LEVI_EXPORT_ROOTS` 内的完整导出目录可以传输。

## API

All routes are behind the service's UI token and same-origin check.

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/api/levi/pool/status` | Settings, the last scan's summary, recent jobs |
| GET | `/api/levi/pool/sources?category=&show_archive=` | Sources with format, category, episodes, copies, held-out, exportable |
| GET | `/api/levi/pool/tasks?category=&source=&search=&format=&show_heldout=&show_copies=&show_archive=` | Per task: episodes, frames, categories, success rate, spellings, `policy_methods`; takes the same policy filters |
| GET | `/api/levi/pool/episodes?…&task=&outcome=&policy=&policy_model=&policy_checkpoint=&policy_method=&limit=&offset=` | The index, paged |
| POST | `/api/levi/pool/scan?rehash=` | Start a scan job |
| GET | `/api/levi/pool/jobs`, `/api/levi/pool/jobs/{id}` | Scan and export jobs with progress and result |
| GET / PUT / DELETE | `/api/levi/pool/recipes`, `/api/levi/pool/recipes/{name}` | Recipe CRUD (the body's `name` must match the URL) |
| POST | `/api/levi/pool/preview` | `{ "recipe": {…}, "format": "recap_value" }` → counts and exclusions; per task `available`, `successes`, `failures`, `selected`, `selected_successes`, `selected_failures`, `shortfall`, `notes`; the overall `mix` |
| POST | `/api/levi/pool/selection` | `{ "recipe": {…}, "task": "…" }` → the episodes picked for that task (`quality_score`, `sel_stratum`, `selection_reason`, `viewer`) and the task's report; 404 for a task the recipe lacks |
| POST | `/api/levi/pool/suggest` | `{ "recipe": {…}, "task": "…" }` → `available`, `successes`, `failures`, `suggested_count` (the balanced default) for adding that task |
| POST | `/api/levi/pool/export` | `{ "recipe_name": "…" or "recipe": {…}, "options": {"format", "name", "output_dir", "fps", "cameras", "camera_map", "hardlink", …}, "dry_run": false }`; 403 for a path outside `LEVI_EXPORT_ROOTS` or inside a source, 400 for an existing target or an empty selection |
| GET | `/api/levi/pool/facets?category=&show_heldout=&show_copies=&show_archive=` | Facet counts for the page (including `policy_models`, `policy_checkpoints`, `policy_methods`) and what the toggles hide |
| POST | `/api/levi/pool/jobs/{id}/cancel` | Stop a running scan, export or push |
| GET | `/api/levi/pool/jobs/{id}/summary` | `pool_export.json` of a finished export job |
| GET | `/api/levi/pool/remotes` | Registered remote targets |
| PUT / DELETE | `/api/levi/pool/remotes/{name}` | Register (`{"spec": "[user@]host:/path", "port"?}`) or forget a target; unknown fields such as `password` are refused |
| POST | `/api/levi/pool/push` | `{ "target": "…", "export_job": "…" or "source": "<export dir>", "dry_run": false }`: rsync over SSH as a cancellable job |
