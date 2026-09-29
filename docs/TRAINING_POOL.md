# Training pool / 训练池

The training pool indexes every dataset under a set of read-only folders, one row per episode, and composes new training datasets from it: pick categories, sources and an ordered list of tasks, preview the counts, and export a merged LeRobot v2.1 dataset (openpi / π0.5), a RECAP value dataset or a copy of the raw captures. Code: `levi/pool/`.

训练池把若干只读目录下的所有数据集逐片段登记，再按类别、来源和有序任务列表组合成新的训练数据集：可以先预览数量，再导出合并后的 LeRobot v2.1 数据集（openpi / π0.5）、RECAP 价值数据集或原始采集目录副本。代码在 `levi/pool/`。

## Guarantees / 保证

- **Sources are read-only.** The pool only reads `LEVI_POOL_ROOTS`. Its own files live in `<workspace>/pool/`; an export goes to a new folder, written as `.<name>.partial` and renamed when complete. An export may not lie inside (or contain) any indexed source dataset, must lie inside `LEVI_EXPORT_ROOTS`, and its folder must not exist yet.
- **Held-out episodes are never exported.** The lists in `LEVI_POOL_HELDOUT` (for example `levi-hub/gold/frozen/plates-frozen-v1.json`) are matched by path and by video sha256; every copy, filtered variant or conversion of a held-out episode is held out too. A recipe cannot include them, and the export checks again — by path, by sha256 and against the index — and refuses the whole export if one slipped into a plan. This is a refusal, not a filter default.
- **One recorded episode counts once.** Copies are grouped (see [Grouping](#grouping-copies-variants-conversions--副本与版本归并)); an export takes the canonical member unless the recipe names only other sources.
- **Traceable.** Every export writes `pool_export.json`: the recipe, the task order, each episode's source path, group and fingerprint, every exclusion with its reason, the LEVI commit and the format parameters.

- **源数据只读**：只读取 `LEVI_POOL_ROOTS`；训练池自己的文件在 `<workspace>/pool/`；导出写到新目录，先写 `.<名称>.partial`，完成后改名。导出目录不能在任何已登记的源数据集内部（也不能包含源数据集），必须在 `LEVI_EXPORT_ROOTS` 之内，且事先不存在。
- **留出（冻结测试）片段永不导出**：`LEVI_POOL_HELDOUT` 中的清单按路径和视频 sha256 匹配，留出片段的副本、过滤版本和转换结果同样视为留出。选择无法包含它们；导出时再按路径、sha256 和索引各查一遍，只要有一条混入就拒绝整个导出。
- **同一次录制只算一次**：副本归为一组，导出默认只用规范来源。
- **可追溯**：每次导出写 `pool_export.json`（选择条件、任务顺序、每个片段的来源路径与指纹、排除清单及原因、LEVI commit、格式参数）。

## Settings / 设置

| Setting | Meaning | Default |
| --- | --- | --- |
| `LEVI_POOL_ROOTS` | Comma-separated folders the pool reads, never writes | unset: the training pool is idle |
| `LEVI_EXPORT_ROOTS` | Comma-separated folders an export may be written under | the workspace |
| `LEVI_POOL_HELDOUT` | Comma-separated held-out lists (JSON with `episodes: [{path, sha256: {video: hex}, frame_count, frozen_id}]`; `path` relative to a pool root or absolute; other keys such as `dev_pool` are ignored) | none |

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
- categories, first match wins: `_archive/` paths → `archive`; DROID raw → `external`; a dataset with a LEVI marker (`meta/levi_conversion.json`, `meta/levi_recap.json`, `pool_export.json`, …) or inside a LEVI workspace (a folder holding `outputs/LEVI/workbench/datasets.json`) → `levi`; `data_source: policy_rollout` → `rollout`; `data_source` of a human collector or a teleoperation `control_mode` (`pygame`, `spacemouse`, `gello`, …) → `human`; a raw capture without `data_source` → `human`; a LeRobot dataset converted from indexed raw episodes → the category of those episodes; any other LeRobot dataset → `human` (`lerobot_default_category`).
- tasks are normalised for grouping: underscores to spaces, lower case, single spaces (`Pour_water_into_brown_cup` and `pour water into brown cup` are one task); the original spellings stay listed.

分类规则全部是数据：`_archive/` → 归档；DROID → 外部；有 LEVI 标记或在 LEVI 工作区内 → LEVI 处理后；`data_source: policy_rollout` → rollout；遥操作 `control_mode` 或无 `data_source` 的原始采集 → 人工。任务文本规范化（下划线变空格、小写），原始写法保留。

### Index / 片段索引

Columns of `pool/index.parquet` include `key`, `source`, `format`, `episode`, `task` (normalised) and `task_raw`, `frames`, `fps`, `cameras`, `state_dim`/`action_dim`, `category` and `category_reason`, `data_source`, `control_mode`, `policy` (a rollout's checkpoint name), `date`, `robot_flag` (a rollout's own success flag), `human_label` (from any LEVI workspace under the roots, read-only), `outcome` and `outcome_source` (human label first, then the robot's flag), `nonstandard`, `exportable`, `fingerprint`, `content_hash`, `recording`, `filtered`, `group`, `canonical`, `copies`, `heldout`, `heldout_set`, `heldout_id`.

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
| `categories`, `sources`, `formats`, `policies`, `date_from`, `date_to` | filters (`sources` also orders episodes within a task) |
| `tasks` | ordered list of normalised tasks; the export follows this order |
| `outcome` | `all`, `robot_flag_success` (the robot's flag) or `verified_success` (a human label first, then the robot's flag) |
| `per_task_cap`, `seed` | at most this many episodes per task, drawn reproducibly |
| `include_nonstandard`, `exclude` | non-standard folders in; episode keys out |
| `task_text` | normalised task → the text written into the export |

The selection runs in this order: held-out out (always), `exclude`, non-standard and unsupported formats out, one episode per group, the outcome filter, the export format's own need (a raw copy takes raw captures; a RECAP value export needs an outcome), then `per_task_cap`. The preview lists episodes and frames per task and every exclusion by reason (`heldout`, `duplicate`, `nonstandard`, `unsupported`, `outcome_filter`, `no_outcome`, `per_task_cap`, …).

```bash
uv run levi pool recipe save pi05-mix --category human --task "pick fork into green plate" \
    --task "pick apple on green plate" [--per-task-cap 50 --seed 1] [--outcome verified_success] \
    [--source <id>] [--policy <checkpoint>] [--date-from 2026-09-01] [--exclude <episode key>] \
    [--task-text "pick fork into green plate=Pick the fork into the green plate"] [--file recipe.json]
uv run levi pool recipe show pi05-mix [--format recap_value]     # the recipe and its preview
uv run levi pool recipe list | delete <name>
```

## Exports / 导出

```bash
uv run levi pool export pi05-mix --format lerobot_v21 --name pi05-mix-v1 \
    [--output-dir /data/exports] [--fps 10] [--camera-map observation.images.wrist=observation.images.hand] \
    [--hardlink] [--dry-run]
```

| Format | What it writes |
| --- | --- |
| `lerobot_v21` | One LeRobot v2.1 dataset for openpi / π0.5. Raw captures go through LEVI's conversion pipeline (the same checks and options as `levi convert`: resample to `fps`, static-frame filter); LeRobot v2.x episodes are copied with new indices. Episodes follow the recipe's task order, then source order, then episode order; one merged task table. Camera keys become `observation.images.hand` / `observation.images.view1` (`cameras` maps raw capture cameras, `camera_map` LeRobot keys). Every source must share fps, state and action dimensions and video resolution — otherwise nothing is written and the refusal lists the differences. Raw captures failing the capture checks are left out and listed (`conversion_preflight`). |
| `recap_value` | The same plus RECAP rewards, returns and labels ([RECAP](RECAP.md)); raw captures are retimed without filtering (one row per executed step). The outcome is the human label, else the robot's flag; episodes with neither are left out (`no_outcome`). |
| `raw_capture` | Raw capture folders copied (or with `--hardlink`, hard-linked: never edit such an export) into `<task>/demo_NNNN`, renumbered per task, with `task_description.txt`. |

Each episode row in `meta/episodes.jsonl` of a LeRobot export carries `pool_key`, `pool_source` and `pool_fingerprint`; `meta/levi_provenance.jsonl` maps new to source episode indices. Not in this version: LeRobot v3 output (needs lerobot's v2.1→v3 converter; convert the v2.1 export with it), exporting a recipe as a training manifest without copying data, and remote transfer.

导出格式：LeRobot v2.1（多来源合并，按任务顺序→来源→片段排序，统一相机键和 fps，状态/动作维度或分辨率不一致时拒绝并列出差异）、RECAP 价值数据集（结局按“人工标签 > 机器人标志”，无结局的片段排除并列出）、原始采集目录（按任务重编号，可硬链接）。暂不支持：LeRobot v3 输出、导出为训练清单、远程传输。

## API

All routes are behind the service's UI token and same-origin check.

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/api/levi/pool/status` | Settings, the last scan's summary, recent jobs |
| GET | `/api/levi/pool/sources?category=&show_archive=` | Sources with format, category, episodes, copies, held-out, exportable |
| GET | `/api/levi/pool/tasks?category=&source=&search=&format=&show_heldout=&show_copies=&show_archive=` | Per task: episodes, frames, categories, success rate, spellings |
| GET | `/api/levi/pool/episodes?…&task=&outcome=&policy=&limit=&offset=` | The index, paged |
| POST | `/api/levi/pool/scan?rehash=` | Start a scan job |
| GET | `/api/levi/pool/jobs`, `/api/levi/pool/jobs/{id}` | Scan and export jobs with progress and result |
| GET / PUT / DELETE | `/api/levi/pool/recipes`, `/api/levi/pool/recipes/{name}` | Recipe CRUD (the body's `name` must match the URL) |
| POST | `/api/levi/pool/preview` | `{ "recipe": {…}, "format": "recap_value" }` → counts and exclusions |
| POST | `/api/levi/pool/export` | `{ "recipe_name": "…" or "recipe": {…}, "options": {"format", "name", "output_dir", "fps", "cameras", "camera_map", "hardlink", …}, "dry_run": false }`; 403 for a path outside `LEVI_EXPORT_ROOTS` or inside a source, 400 for an existing target or an empty selection |
