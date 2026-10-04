# Training pool / 训练池

The training pool indexes every dataset under a set of read-only folders, one row per episode, and composes new training datasets from it: pick categories, sources and an ordered list of tasks, preview the counts, and export a merged LeRobot v2.1 dataset (openpi / π0.5), a RECAP value dataset or a copy of the raw captures. Code: `levi/pool/`.

训练池把若干只读目录下的所有数据集逐片段登记，再按类别、来源和有序任务列表组合成新的训练数据集：可以先预览数量，再导出合并后的 LeRobot v2.1 数据集（openpi / π0.5）、RECAP 价值数据集或原始采集目录副本。代码在 `levi/pool/`。

## Guarantees / 保证

- **Sources are read-only.** The pool only reads `LEVI_POOL_ROOTS`. Its own files live in `<workspace>/pool/`; an export goes to a new folder, written as `.<name>.partial` and renamed when complete. An export may not lie inside (or contain) any indexed source dataset, must lie inside `LEVI_EXPORT_ROOTS`, and its folder must not exist yet.
- **Held-out episodes are never exported.** The lists in `LEVI_POOL_HELDOUT` (for example `/data/frozen/heldout.json`) are matched by path and by video sha256; every copy, filtered variant or conversion of a held-out episode is held out too. A recipe cannot include them, and the export checks again — by path, by sha256 and against the index — and refuses the whole export if one slipped into a plan. This is a refusal, not a filter default.
- **One export holds one gripper.** When the selected episodes carry more than one known gripper, or one known gripper next to episodes whose gripper is not recorded, the whole export is refused, unless the recipe allows it (`allow_mixed_gripper`) or names `unknown` on purpose. See [Robot and gripper](#robot-gripper-action-mode-and-end-effector-frame--机器人夹爪动作模式与末端坐标系).
- **An episode a person removed on the live page stays out.** A live workspace (`levi live`, [LIVE.md](LIVE.md#removing-an-episode-restorable)) lets a person remove an episode (restorable; no file is deleted). When that workspace lies under a pool root, its mirror is indexed beside the rollout folder it was linked from, and the removal applies to the whole group of copies: the pool reads the live workspace's dataset states when it is asked (no new scan), also names the rollout folder the mirror was linked from (the state file's `source`), so the original is kept out even when the scan ran before the demo was mirrored or the source was replaced afterwards; it does not list or count the recording (`facets.removed_in_live` counts recordings, and the page shows it as "Episodes removed on the live page"), leaves it out of every recipe (`excluded_in_live` among the exclusions) and refuses it when an export is planned, run or resumed ("removed on the live page"). Only live workspaces the last scan found under the roots (and the pool's own workspace) are read.
- **One recorded episode counts once.** Copies are grouped (see [Grouping](#grouping-copies-variants-conversions--副本与版本归并)); an export takes the canonical member unless the recipe names only other sources.
- **A corrected task text is a reviewed, separate record.** A [task text correction](#task-text-corrections--任务文本订正) never changes a source; an export applies one only when its recipe names the version and a person approved it, and `pool_export.json` lists every correction it applied.
- **Traceable.** Every export writes `pool_export.json`: the recipe, the task order, each episode's source path, group, fingerprint, outcome and its source, policy fields (`policy_model`, `policy_checkpoint`, `policy_method`, `policy_phase`, `policy_label`), robot, gripper, action mode and end-effector frame (`robot`, `gripper`, `action_mode`, `ee_frame`) and why it was picked (`selection_stratum`, `quality_score`, `selection_reason`), per task what was asked for and what was picked (`selection`), every exclusion with its reason, the LEVI commit and the format parameters.

- **源数据只读**：只读取 `LEVI_POOL_ROOTS`；训练池自己的文件在 `<workspace>/pool/`；导出写到新目录，先写 `.<名称>.partial`，完成后改名。导出目录不能在任何已登记的源数据集内部（也不能包含源数据集），必须在 `LEVI_EXPORT_ROOTS` 之内，且事先不存在。
- **留出（冻结测试）片段永不导出**：`LEVI_POOL_HELDOUT` 中的清单按路径和视频 sha256 匹配，留出片段的副本、过滤版本和转换结果同样视为留出。选择无法包含它们；导出时再按路径、sha256 和索引各查一遍，只要有一条混入就拒绝整个导出。
- **人在实时页面排除的片段不会进池**：实时工作区（`levi live`，见 [LIVE.zh-CN.md](LIVE.zh-CN.md#排除片段可恢复)）允许人排除一个片段（可恢复，不删除任何文件）。该工作区在池根目录之下时，它的镜像会和所链接的 rollout 目录一起登记，排除对整组副本生效：训练池在被查询时读实时工作区的数据集状态（不需要重新扫描），也按状态文件里的 `source` 认出镜像所链接的 rollout 目录（扫描先于镜像、或源在镜像后被替换时原件同样被排除），不列出也不计入这个录制（`facets.removed_in_live` 按录制计数，页面显示为“在实时页面被删除的片段数”），任何配方都会跳过它（排除原因里是 `excluded_in_live`），导出在规划、运行和续跑时都会拒绝它（“removed on the live page”）。只读取上次扫描在池根下找到的实时工作区（以及训练池自己的工作区）。
- **同一次录制只算一次**：副本归为一组，导出默认只用规范来源。
- **任务文本订正是经人审核的独立记录**：[任务文本订正](#task-text-corrections--任务文本订正)不改任何源数据；只有配方点名了该版本、且人已经批准的订正才会在导出时应用，`pool_export.json` 列出应用了哪些。
- **一次导出只取一种夹爪**：所选片段含有一种以上已知夹爪（或已知夹爪加上未记录夹爪的片段）时，整次导出被拒绝，除非配方明确允许（`allow_mixed_gripper`）或有意选了“未知”。见[机器人与夹爪](#robot-gripper-action-mode-and-end-effector-frame--机器人夹爪动作模式与末端坐标系)。
- **可追溯**：每次导出写 `pool_export.json`（选择条件、任务顺序、每个片段的来源路径与指纹、排除清单及原因、LEVI commit、格式参数）。

## Settings / 设置

| Setting | Meaning | Default |
| --- | --- | --- |
| `LEVI_POOL_ROOTS` | Comma-separated folders the pool reads, never writes | unset: the training pool is idle |
| `LEVI_EXPORT_ROOTS` | Comma-separated folders an export may be written under | the workspace |
| `LEVI_POOL_SSH` | The SSH client of remote transfers (default `ssh`; tests point it at a stand-in) | `ssh` |
| `LEVI_POOL_STALL_SECONDS` | A running job whose worker has not moved for this long is reported `stalled` | `300` |
| `LEVI_POOL_PARTIAL_TTL` | How long an interrupted or failed export's `.partial` folder is kept for a resume before the sweeper removes it (`3d`, `12h`, `90m`, `3600s`; a bare number is days) | `3d` |
| `LEVI_POOL_JOB_TTL` | How long a finished job's log and files are kept; the record then shrinks to a summary (failed, cancelled and interrupted records go entirely). Same units | `30d` |
| `LEVI_POOL_AUTO_RESUME` | `1`: when the service starts, resume the exports it finds interrupted | off |
| `LEVI_POOL_FREE_MARGIN_GIB` | Free space an export leaves on its volume; planning and starting refuse when the estimate plus this does not fit | `1` |
| `LEVI_POOL_BATCH_EPISODES` | Raw episodes converted per journaled part (a smaller part loses less to an interruption, costs a little more start-up) | automatic, at least 8 |
| `LEVI_POOL_HELDOUT` | Comma-separated held-out lists (JSON with `episodes: [{path, sha256: {video: hex}, frame_count, frozen_id}]`; `path` relative to a pool root or absolute; other keys such as `dev_pool` are ignored), or `none` for a pool without a held-out set | unset: **exports are refused** |

An export also needs the held-out setting: unset, planning and running an export are refused (a missing setting must not let frozen test episodes through). The lists in force must be the ones the last scan used — otherwise the index does not mark them — so a change asks for a new scan; held-out entries that match no indexed episode are listed as a warning (`heldout_unmatched`).

没配置 `LEVI_POOL_HELDOUT`（也没写 `none`）时拒绝导出；导出时的清单必须和上次扫描用的一致，否则要求重新扫描；匹配不到片段的清单条目会给出警告。

A relative export name lands in `<workspace>/exports/pool/<name>`.

## Scanning / 扫描

```bash
LEVI_POOL_ROOTS=/data LEVI_POOL_HELDOUT=/data/frozen/heldout.json uv run levi pool scan [--rehash]
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

Columns of `pool/index.parquet` include `key`, `source`, `format`, `episode`, `task` (normalised) and `task_raw`, `frames`, `fps` (the nominal rate), `measured_fps` (the rate the collector measured, `collection_freq_hz`; raw captures only), `cameras`, `state_dim`/`action_dim`, `category` and `category_reason`, `data_source`, `control_mode`, the policy columns below, the robot and gripper columns (`robot`, `gripper`, `action_mode`, `ee_frame`, `embodiment_evidence`; see [Robot and gripper](#robot-gripper-action-mode-and-end-effector-frame--机器人夹爪动作模式与末端坐标系)), `date`, `robot_flag` (a rollout's own success flag), `human_label` (from any LEVI workspace under the roots, read-only), `outcome` and `outcome_source` (human label first, then the robot's flag), `nonstandard`, `exportable`, `fingerprint`, `content_hash`, `recording`, `filtered`, `group`, `canonical`, `copies`, `heldout`, `heldout_set`, `heldout_id`.

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

### Robot, gripper, action mode and end-effector frame / 机器人、夹爪、动作模式与末端坐标系

Training on the episodes of two different grippers teaches one policy two meanings of "closed", and a state of seven numbers looks the same for both. So the index records four fields per episode, and an export refuses to mix grippers. Code: `levi/pool/embodiment.py`; nothing in it names a task.

| Column | Values | Read from |
| --- | --- | --- |
| `robot` | `franka_fr3`, `franka_panda`, `unknown` | `robot_joint_names` in a raw capture's `metadata.json` (`fr3_joint1…`, `panda_joint1…`) |
| `gripper` | `robotiq_2f85`, `franka_hand`, `unknown` | `gripper_joint_names` (`robotiq_85_left_knuckle_joint`; `fr3_finger_joint1/2`), `gripper.joint_name` (spacemouse captures), `gripper_state_topic` or `gripper_control.move_action_topic` under `/franka_gripper/` |
| `action_mode` | `ee_pose_abs_next`, `ee_pose_abs_current`, `unknown` | a LEVI conversion's `meta/levi_conversion.json` (`action_semantics`: `next_state` or `state`); for a raw capture, which has no action column, the format itself: the pool's export derives the action as the next recorded pose (marked `derived:` in the evidence, not read from the capture's metadata) |
| `ee_frame` | the name a policy server declares (`franka_hand_tcp`), else `unknown` | `policy.server_metadata.ee_frame` of a rollout |
| `embodiment_evidence` | JSON | per field, what decided it: `metadata:<key>` / `info:<key>` / `conversion:<key>` (read from the episode), `derived:format:<format>` (a fixed fact of the format), `linked capture: …`, `declared: <source>` (a person's declaration, shown as a badge on the page), or `conflict: …` when evidence disagrees (shown as the tooltip of the gripper cell) |

**`unknown` means the episode's own metadata does not say**, and nothing is guessed: not from the folder name, the source name or the task text. Two rules that give different values for one field (a Robotiq joint name next to a Franka gripper topic) make that field `unknown` and the evidence says so. A LeRobot episode converted from a raw capture the pool indexed (its `rollout_source_demo`, `processed_demos.json` or LEVI's `source_demo`) takes `robot`, `gripper` and `ee_frame` from the linked capture(s) when its own files say nothing (several linked captures that disagree on a field leave it `unknown`, with the conflict in the evidence); its `action_mode` is its own (a LeRobot `info.json` does not record whether an action is the next pose, so it is `unknown` unless a conversion record says). A LeRobot dataset with no link and no record, for example an older conversion with no `processed_demos.json`, is `unknown` throughout.

The evidence is a table of rules in `levi/pool/rules.py` (`embodiment`: a `version` and `rules`), data not code. Each rule is `{field, value | copy, in, key, regex}`: look at `key` (a dotted path) in `metadata` (a raw capture's `metadata.json`), `info` (`meta/info.json`), `conversion` (`meta/levi_conversion.json`) or the episode's `format`; when the text matches `regex` the field gets `value` (or, with `copy`, that text cleaned to `[a-z0-9_.-]`). A workspace replaces the whole table in `<workspace>/pool/rules.json` (`{"embodiment": {"version": 2, "rules": [...]}}`) to teach the pool a new gripper. Rule values must look like a recipe's names (`[a-z0-9][a-z0-9_.-]{0,39}`), or the recipe could not select them. A raw capture's scan signature contains a hash of the table and the declarations below, so changing either (or upgrading LEVI to a new table) reads every raw capture again at the next scan.

**Declaring a source.** A source whose metadata records no gripper (the older LeRobot conversions: their `info.json` has only `robot_type: Industrial_Arm` and the dimension names) stays `unknown` until a person says what it is. Add it to the workspace's `<workspace>/pool/rules.json`, where nothing is declared by default and no data folder is named in the code:

```json
{"embodiment_declared": [
  {"field": "gripper", "value": "franka_hand", "source": "data_collection/lerobot_fr3_filtered",
   "evidence": "declared", "note": "collector, 2026-10-03"}
]}
```

`source` is a path relative to a pool root; it matches that folder and everything below it (whole path components: `a/b` does not match `a/bc`). A declaration only fills a field whose metadata has no evidence at all; when it contradicts the metadata the field becomes `unknown` and the evidence names both; metadata that contradicts itself stays `unknown`; two declarations that disagree give `unknown`. The evidence reads `declared: <source> (<note>)` and the page marks the gripper cell as declared. A malformed declaration is refused when the pool reads its rules. The page marks the gripper cell as declared, also when the value comes through a declared linked capture, and the composition panel says how many of the selected episodes have their gripper from a declaration. With several pool roots, a `source` matches that relative path under every root. The declarations are part of the scan signature, so unchanged captures come from the scan cache (a changed declaration reads the captures it may touch again).

**First scan after upgrading.** An index written before these columns exists asks for a rescan (`levi pool scan`, or Scan now on the page) and the page shows that message until then. The scan reads every raw capture again once, because its signature changed: its `metadata.json`, the whole of `frames.csv` (hashed for the fingerprint), and, for the captures that match a held-out list, the videos' sha256 (the cached `video_sha256` values are dropped, a few hundred files). It does not decode videos, and LeRobot datasets keep their cached content hashes. Measured on a scratch workspace over this machine's pool (21,055 episodes, both held-out lists, files in the page cache): about 14 s for such a full read against about 7 s for an unchanged rescan (the product's own incremental scans take about 5 s). A cold disk, or a pool that is not on a local SSD, takes longer; that was not measured. The page shows the "older LEVI" message (in both languages) until this scan has run.

**The mix rule** (`embodiment.gripper_mix`, used by the preview, the plan and the run). Count the selected episodes per gripper class (`unknown` is its own class):

| Selected | Result |
| --- | --- |
| one known gripper | allowed |
| only `unknown`, all from one source | allowed, with a non-blocking note (`gripper_unknown`); the export is recorded as `gripper: unknown` |
| only `unknown`, from two or more sources | refused (`problem: unknown_multi_source`) unless `allow_mixed_gripper` or the recipe lists `unknown` in `grippers`: sources with no gripper record may hold different grippers (a Franka and a Robotiq conversion look alike) |
| more than one known gripper | refused (`mixed_gripper`, `problem: mixed_known`) unless `allow_mixed_gripper` |
| one known gripper and some `unknown` | refused (`problem: known_and_unknown`) unless `allow_mixed_gripper` or the recipe lists `unknown` in `grippers` |

The way out for sources with no record is to declare their gripper (above). The refusal lists the counts per class and the sources holding each (for several unknown sources, the episodes per source). It is a blocking warning in the preview, the plan refuses it (nothing is written), and the run checks again from the frozen plan, so an edited plan or a resumed export cannot slip a mix through. The run also compares each planned episode's `source` and `gripper` with the current index and refuses on any difference, so a plan edited by hand cannot hide a mix. This applies to every plan made by this version: one that carries the `embodiment_check` marker or whose episodes carry a `gripper` key (deleting the marker alone does not skip it); a plan made before this version has neither and resumes as before. The held-out group check reads only `key`, `group` and `heldout` of the index, so it still runs on an index from an older LEVI. `pool_export.json` gets an `embodiment` block (`gripper`: the single class or `mixed`, `grippers`, `robots`, `action_modes`, `ee_frames` as counts, `mixed`, `sources` (how many), `allow_mixed_gripper`, `rules_version`, `declared`) and every episode row carries its four fields and `embodiment_evidence`. A value that came from a declaration is therefore visible downstream: `declared` is `{episodes, by_field: {gripper: {<source>: n}}, declarations: [{field, value, source, evidence, note}]}` (copies of the declarations the export used, with their notes; empty when every value was read from metadata), and the plan carries the same copies as `embodiment_declared`. An export of declared grippers is a person's judgement, as `allow_mixed_gripper` is, and its record says so.

Why: the user's decision of 2026-10-03: when training a policy for the Robotiq gripper, human demonstrations recorded with the original Franka gripper are left out; with the field in the index the pool selects by it (`--gripper robotiq_2f85`, or Gripper in the filters) instead of by source folder.

训练池为每个片段记录四个字段：`robot`（机器人）、`gripper`（夹爪：`robotiq_2f85`、`franka_hand`、`unknown`）、`action_mode`（动作模式）、`ee_frame`（末端坐标系），并拒绝一次导出混用夹爪。字段只来自片段自己的元数据（原始采集的 `metadata.json`、LEVI 转换记录 `meta/levi_conversion.json`、策略服务器声明的 `ee_frame`；规则表支持读 `meta/info.json`，但默认规则没有用它），**读不出就是 `unknown`，不从目录名、来源名或任务文本推断**；两条证据互相矛盾时也记 `unknown`，并在 `embodiment_evidence` 里写明。由原始采集转换来的 LeRobot 片段在自己的文件没有说明时，从关联的原始采集取机器人、夹爪和末端坐标系（几个关联采集取值不一致时记 `unknown`；动作模式不继承）。原始采集的 `action_mode` 来自格式本身（池导出把动作定义为下一帧位姿），证据里标 `derived:`，不是从元数据读出的。证据规则是 `levi/pool/rules.py` 里的一张数据表（带版本号），工作区可用 `pool/rules.json` 整体替换，规则值必须符合配方能选的名字格式；规则表和声明的哈希进入原始采集的扫描签名，规则变了或升级到新表，下次扫描会重新读取。旧索引会要求重新扫描（页面中英文都会提示“点击立即扫描”）：这次扫描要把全部原始采集重读一遍（`metadata.json`、完整读取并哈希 `frames.csv`，并重新哈希和留出清单匹配的视频，几百个文件），不解码视频；在本机的临时工作区实测（21,055 个片段、两份留出清单、文件在页面缓存中）完整重读约 14 秒，不变的重扫约 7 秒；磁盘冷或不在本地 SSD 上会更慢，没有实测。

**声明来源。** 元数据没有夹爪记录的来源（例如较早的 LeRobot 转换，`info.json` 只有 `robot_type: Industrial_Arm` 和维度名）保持 `unknown`，直到有人声明：在工作区 `pool/rules.json` 加 `embodiment_declared`（默认为空，代码里不写死任何数据目录），每条 `{field, value, source, evidence: "declared", note}`，`source` 是相对池根的路径前缀（按整段路径匹配）。声明只填元数据完全没有证据的字段；与元数据矛盾时记 `unknown` 并写明双方；证据显示为 `declared: <来源>`，页面上夹爪单元格带“已声明”标记。

混合规则：所选片段含一种以上已知夹爪，或一种已知夹爪加上 `unknown`（且配方没有在 `grippers` 里明确列出 `unknown`），整次导出被拒绝，并列出各类数量和涉及的来源；`allow_mixed_gripper: true` 明确允许后放行。全是 `unknown` 且来自**多个来源**时同样拒绝（`unknown_multi_source`：没有夹爪记录的来源可能是任一种夹爪，Franka 和 Robotiq 的转换数据看起来一样），解除办法同上，或声明各来源的夹爪；全是 `unknown` 且只有一个来源仍可导出，只给非阻断提示，记录为 `gripper: unknown`。预览里是阻止导出的提示，计划阶段拒绝，执行时再查一遍，并按索引核对计划里每个片段的来源和夹爪，被手改过的计划绕不过（凡是本版本生成的计划都会检查：带 `embodiment_check` 标记，或片段带 `gripper` 键；只删掉标记不能跳过；旧版本的计划两者都没有，照旧续做）。导出记录里：每个片段带 `embodiment_evidence`，`embodiment.declared` 给出声明来的片段数（按字段和来源）以及所用声明的副本（含 note），计划里是 `embodiment_declared`；预览和构成面板显示其中多少个片段的夹爪来自声明；声明过的原始采集不再每次扫描重读（声明算进扫描签名，签名不变就用缓存，声明变了才重读）。留出副本检查只读索引的 `key`、`group`、`heldout` 三列，旧索引下照常执行。`pool_export.json` 记录 `embodiment` 块和每个片段的四个字段。原因（用户 2026-10-03 的决定）：给 Robotiq 夹爪的策略训练时，先排除 Franka 原装夹爪的人工数据；有了字段就按字段筛选，不再按来源目录。

### Grouping: copies, variants, conversions / 副本与版本归并

Rows are grouped when they share any of:

- the **fingerprint** of a raw episode: md5 of `frames.csv` (else `end_effector_pose.csv`) plus every video's name and size — byte copies;
- the **recording** identity: the normalised task folder and the collector's `created_at`/`stopped_at` — a filtered variant (`filtered_from_frame_count` in its metadata) belongs to its original;
- a **conversion link**: a LeRobot episode's `rollout_source_demo`, `meta/processed_demos.json` or LEVI's `source_demo`;
- the **content hash** of a LeRobot episode (md5 of its state and action arrays) — LeRobot copies.

The canonical member is the original: not archived, not in a LEVI workspace, a raw capture before a LeRobot conversion, unfiltered before filtered, a standard folder name before a non-standard one (`demo_0001` is kept over `demo_0001 copy`), the shallowest path. When the members of a group carry different task texts, the export keeps the canonical member's text and warns (`copy_task_conflict`); `levi pool corrections copies` lists those groups for a person, see [Copies whose texts differ](#copies-whose-texts-differ--文本不同的副本). Human labels and held-out marks apply to the whole group. A LeRobot dataset with no provenance whose episodes differ from every other dataset (for example an older conversion of a different filtering) cannot be linked to its raw captures and appears as a separate human source; pick sources explicitly when that matters.

分组依据：原始片段指纹（字节副本）、录制身份（任务目录 + 开始/结束时间，过滤版本归入原件）、转换链接（`rollout_source_demo`、`processed_demos.json`、`source_demo`）、LeRobot 内容哈希。规范成员是原始采集或 rollout 目录；标准目录名优先于非标准目录名（保留 `demo_0001`，`demo_0001 copy` 视为副本）。同组成员的任务文本不同时，导出用规范成员的文本并给出提示（`copy_task_conflict`），`levi pool corrections copies` 把这些组列出来交人决定，见[文本不同的副本](#copies-whose-texts-differ--文本不同的副本)。没有来源记录的 LeRobot 数据集无法关联到原始采集，会作为单独来源出现。

## Recipes / 选择

A recipe is a named, saved selection (`pool/recipes/<name>.json`):

| Field | Meaning |
| --- | --- |
| `categories`, `sources`, `formats`, `policies`, `date_from`, `date_to` | filters (`sources` also orders episodes within a task); `policies` is the old checkpoint filter |
| `policy_models`, `policy_checkpoints`, `policy_methods` | policy filters (see above; `policy_methods` values: `direct`, `dsrl`, `rlt`, `sfe`, `student`, `other`, `unknown`); combined with AND, values within one list with OR |
| `robots`, `grippers` | filters on the recorded robot and gripper (`unknown` selects the episodes whose metadata says nothing); one gripper per export unless `allow_mixed_gripper` |
| `allow_mixed_gripper` | `false` by default: an export of more than one known gripper (or one known next to unknown, unless `grippers` lists `unknown`) is refused; `true` lets it through and `pool_export.json` records the mix |
| `tasks` | ordered list of task entries `{task, count, success_ratio, strategy}` (see [Choosing episodes](#choosing-how-many-episodes-a-task-contributes--每个任务取多少片段)); the export follows this order. A bare task text (the old form) still loads: it means `count` unset, `strategy: random` |
| `outcome` | `all`, `robot_flag_success` (the robot's flag), `verified_success` (a human label first, then the robot's flag; the preview counts `outcome_sources` and warns how many rest on the operator's key press only) or `human_verified_success` (a human label only). An episode whose human labels disagree is left out of both verified outcomes and of RECAP exports (`label_conflict`) |
| `per_task_cap`, `seed` | the count of a task entry that has none of its own (at most this many episodes per task); `seed` makes every draw reproducible |
| `include_nonstandard`, `exclude` | non-standard folders in; episode keys out |
| `allow_unlinked_sources` | see below |
| `task_text` | normalised task → the text written into the export |
| `task_corrections` | versions of the pool's [task text corrections](#task-text-corrections--任务文本订正) to apply, in order; only approved ones are applied, before every filter, so a corrected episode counts under its corrected task |

The selection runs in this order: held-out out (always), `exclude`, non-standard and unsupported formats out, one episode per group, the outcome filter, the export format's own need (a raw copy takes raw captures; a RECAP value export needs an outcome), then, per task in the task order, the choice of how many and which episodes. A task taken from raw captures **and** from a LeRobot dataset that no scan link ties to them (no provenance, different content hash) may be one recording twice. The preview warns (`possible_unlinked_conversion`) and planning an export refuses, unless the recipe names its `sources` or sets `allow_unlinked_sources`. Preview `warnings` also carry the held-out problems above; a `blocking` one stops an export.

同一任务同时取自原始采集和无法关联的 LeRobot 数据集时，可能是同一批录制的重复：预览警告，导出默认拒绝，指定 `sources` 或设置 `allow_unlinked_sources` 后放行。

The preview lists episodes and frames per task and every exclusion by reason (`heldout`, `duplicate`, `nonstandard`, `unsupported`, `outcome_filter`, `no_outcome`, `not_selected` (over the task's count), `per_task_cap` (over the global cap), `already_in_composition`, …).

```bash
uv run levi pool recipe save pi05-mix --category human --task "pick fork into green plate:count=60,success=50%" \
    --task "pick apple on green plate" [--per-task-cap 50 --seed 1] [--outcome verified_success] \
    [--source <id>] [--policy-model <config>] [--policy-checkpoint <name>] \
    [--policy-method direct|dsrl|rlt|sfe|student|other|unknown] [--policy <checkpoint>] \
    [--gripper robotiq_2f85|franka_hand|unknown] [--robot franka_fr3] [--allow-mixed-gripper] [--date-from 2026-09-01] [--exclude <episode key>] \
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

## Task text corrections / 任务文本订正

Some episodes record another task than their text says: in a paired collection (put into / take out of, fold / flatten) an operator who falls out of step with the collector's task pointer records the opposite direction under the text of the other half. A correction table fixes this without touching the data. Code: `levi/pool/corrections.py`; nothing in it names a task.

| | |
| --- | --- |
| What | per episode: the corrected text (`task_to`), the text it carries now (`task_from`, a guard), where the proposal comes from (`source`, `evidence`, `evidence_files`, `confidence`, `proposed_by`, `proposed_at`), an optional `review_batch`, and the review: `status` (`proposed`, `approved`, `rejected`), `reviewed_by`, `reviewed_at`, `review_note` |
| Where | `<workspace>/pool/task_corrections/<version>.jsonl` (the proposals, written once), `<version>.json` (when, from what file, the sha256) and `<version>.reviews.jsonl` (decisions, appended; the latest one counts) |
| Who | anyone may **propose** (`import`); only a **person** approves or rejects. The review route (`POST /api/levi/pool/corrections/{version}/review`) refuses an agent's credential and needs the UI token; the CLI's `approve` and `reject` send the decision to the running service as the person |
| Applied | only when a recipe names the version (`task_corrections`), only approved ones, to every copy of the recording (the group), before every filter; the source files never change |

```bash
uv run levi pool corrections import proposals.jsonl --version cast-direction-v1   # proposals only
uv run levi pool corrections list | show cast-direction-v1 [--status proposed] [--batch 1]   # both print the sha256
uv run levi pool corrections approve cast-direction-v1 --sha256 <sha256> --batch 1 --except 0007 --reviewer "Ann"   # a person
uv run levi pool corrections reject cast-direction-v1 --sha256 <sha256> --id 0007 --reviewer "Ann" --note "unclear"   # a person
uv run levi pool corrections copies                                                  # copies whose texts differ
uv run levi pool recipe save towels --task "flatten the towel" --file recipe.json     # recipe.json: {"task_corrections": ["cast-direction-v1"]}
```

A proposal file is JSONL (or a JSON list), one object per episode: `id` (default: its line number), `episode` (a path relative to a pool root, or absolute), optionally `fingerprint` (the pool's, used when the path matches nothing), `task_from`, `task_to`, `source`, `proposed_by` and the optional fields above. An import that carries a decision (`status` other than `proposed`, `reviewed_by`, `reviewed_at`) is refused, as is one that changes nothing. A version is written once: when any file of it exists (proposals, manifest or reviews) the import is refused; a change is a new version, so a decision always refers to the proposals the person saw.

**A decision binds to content.** `show` and `list` print the version's `sha256` (of its proposals file, recorded by the import); `approve` and `reject` must name it (`--sha256`, `sha256` in the request), and a different value is refused. Each decision is stored with that sha256 and the proposal's `task_to`, and counts only while both still match: a decision made on other content is ignored. Before anything is applied (preview, plan, run) and before a decision is recorded, the proposals file is hashed again; when it no longer matches the manifest the version is refused as a whole (`changed after its import`): import the change as a new version.

A review is appended in one write and synced to disk. A last line cut short (a write that did not finish) is skipped and reported as `reviews_truncated` by `show` and `list`, and the next review removes it; a damaged line before the last one is refused (restore the file), since reading past it could change a decision.

Matching: `show`, `list` and the preview resolve each proposal against the index: `ok`; `stale` (the episode's text is no longer `task_from`: the source changed or the proposal is wrong; not applied); `unmatched` (no such episode). The preview warns about approved corrections that are stale or unmatched (`task_corrections_not_applied`), and refuses (blocking `task_correction_conflict`) when two approved corrections of one recording in the named versions disagree.

The export: each corrected episode carries `task_original` and `task_correction` (`version:id`) in `pool_export.json`, and the record has `task_corrections`: the versions and their sha256, each applied correction with its episodes, `task_from`, `task_to`, `source`, `confidence`, `reviewed_by` and `reviewed_at`. A raw-capture export writes the corrected `task_description` into its copy of `metadata.json` (the original text and the correction under `levi_task_correction`); the source's file is unchanged. At run time the export checks again that every planned correction is still approved as planned: a decision reversed after planning stops it (plan again).

有些片段录下的任务和文本不符：成对采集（放入/取出、折叠/摊平）时操作者与采集程序的任务指针错位，录下的是反方向，文本却是另一半的。订正表不改数据就能修正。代码在 `levi/pool/corrections.py`，不含任何任务专用逻辑。

- **内容**：每个片段一条：订正后的文本（`task_to`）、当前文本（`task_from`，用来核对）、提议来源（`source`、`evidence`、`evidence_files`、`confidence`、`proposed_by`、`proposed_at`）、可选的 `review_batch`（审核批次），以及审核状态 `status`（`proposed` 待审、`approved` 批准、`rejected` 驳回）、`reviewed_by`、`reviewed_at`、`review_note`。
- **存放**：`<工作区>/pool/task_corrections/<版本>.jsonl`（提议，只写一次）、`<版本>.json`（导入时间、来源文件、sha256）、`<版本>.reviews.jsonl`（审核决定，只追加，以最新一条为准）。同一版本只写一次：该版本的任何文件（提议、清单或审核记录）已存在时拒绝导入，要改就导入新版本。
- **批准绑定内容**：`show`、`list` 打印该版本提议文件的 `sha256`（导入时记在清单里）；`approve`、`reject` 必须带上它（`--sha256`），对不上就拒绝。每条审核记录连同这个 sha256 和该条的 `task_to` 一起保存，只有两者都和当前内容一致时才算数。预览、计划、运行和记录审核之前都会重新计算提议文件的 sha256，和清单不一致时整个版本被拒绝，改动要作为新版本导入。
- **审核记录的写入**：一次审核拼成一次写入并同步到磁盘。最后一行没写完时跳过，`show`、`list` 标出 `reviews_truncated`，下一次审核把这段残行去掉；中间的行损坏时报错（请恢复文件），不猜。
- **谁能做**：任何人（包括 agent）都可以**提议**（`import`）；**批准和驳回只能由人做**。审核接口拒绝 agent 凭据，需要界面令牌；命令行的 `approve`、`reject` 以人的身份发给正在运行的服务。
- **何时应用**：只有配方在 `task_corrections` 里点名了该版本，且订正已被批准；对这次录制的所有副本（同组）生效，在所有过滤之前应用；源文件永远不变。
- **匹配**：`ok`；`stale`（片段的文本已不是 `task_from`，不应用）；`unmatched`（找不到该片段）。预览对已批准但无法应用的订正给出提示；同一录制在所选版本里有两条互相矛盾的已批准订正时拒绝导出。
- **导出记录**：被订正的片段在 `pool_export.json` 里带 `task_original` 和 `task_correction`（`版本:编号`）；记录顶层的 `task_corrections` 列出版本及其 sha256、每条已应用的订正（片段、原文本、新文本、来源、置信度、审核人、审核时间）。原始采集格式的导出在复制出的 `metadata.json` 里写入订正后的 `task_description`（原文本和订正编号放在 `levi_task_correction`），源文件不变。导出运行时再核对一次：计划之后被驳回的订正会让导出停止（重新计划）。

### Copies whose texts differ / 文本不同的副本

`levi pool corrections copies` (or `GET /api/levi/pool/corrections/copies`) lists what a person must decide; nothing here is resolved silently:

- `group_text_differs`: copies of one recording whose task texts differ. The export keeps the canonical member (an original over a folder named like a copy) and its text, and the preview warns (`copy_task_conflict`) until an approved correction of that recording settles the text.
- `nonstandard_text_differs`: a raw capture in a non-standard folder (`demo_0022 copy`, `demo_0038_failure`) with no original in the pool, whose text is not its task folder's name. Which one is right is unknown; such folders stay out of exports unless a recipe sets `include_nonstandard`, and a correction can settle the text.

`levi pool corrections copies` 列出需要人决定的副本，不做静默选择：`group_text_differs`（同一录制的几个副本文本不同：导出保留规范成员即非 `copy` 目录及其文本，并在预览里提示，直到有经批准的订正）；`nonstandard_text_differs`（非标准目录名如 `demo_0022 copy`、`demo_0038_failure`，池里没有原件，且文本与所在任务目录名不同：不知道哪个对；这类目录默认不导出，除非配方设置 `include_nonstandard`，也可以用订正定下文本）。

## Exports / 导出

```bash
uv run levi pool export pi05-mix --format lerobot_v21 --name pi05-mix-v1 \
    [--output-dir /data/exports] [--fps 10] [--camera-map observation.images.wrist=observation.images.hand] \
    [--hardlink] [--human-as-success] [--timing resample|retime] [--filter-static|--no-filter-static] [--on-error-max-fraction 0.1] [--dry-run]
uv run levi pool export --resume <job-id>      # continue an interrupted or failed export
uv run levi pool jobs [log <id> | delete <id> [--files] | clear-failed]   # states, logs, clearing
uv run levi pool clean [--dry-run] [--all-partials]                         # expired partials, old job files
```

| Format | What it writes |
| --- | --- |
| `lerobot_v21` | One LeRobot v2.1 dataset for openpi / π0.5. Raw captures go through LEVI's conversion pipeline (the same checks and options as `levi convert`: resample to `fps`, static-frame filter); LeRobot v2.x episodes are copied with new indices. Episodes follow the recipe's task order, then source order, then episode order; one merged task table. Camera keys become `observation.images.hand` / `observation.images.view1` (`cameras` maps raw capture cameras, `camera_map` LeRobot keys). Every source must share fps, state and action dimensions and video resolution — otherwise nothing is written and the refusal lists the differences. Raw captures failing the capture checks are left out and listed (`conversion_preflight`). |
| `recap_value` | The same plus RECAP rewards, returns and labels ([RECAP](RECAP.md)); raw captures are retimed without filtering (one row per executed step). The outcome is the human label, else the robot's flag; episodes with neither are left out (`no_outcome`), and an episode with conflicting human labels is left out (`label_conflict`) instead of falling back to the operator's key. `--human-as-success` counts human-category episodes without an outcome (demonstrations, RLinf's `sft`) as successes, recorded as `outcome_source: sft_demonstration`. |
| `raw_capture` | Raw capture folders copied into `<task>/demo_NNNN`, renumbered per task, with `task_description.txt`. With `--hardlink` only the videos are hard-linked (they are most of the bytes; never edit them in place); metadata and CSV files are always copied. A task text that is empty, `.` or `..` becomes the folder `task`, and every path written is checked to lie inside the staging folder. |

`pool_export.json` also records the effective conversion parameters (`conversion`: fps, timing, static-frame filter, orientation, cameras, …), the timing mode used (`timing`, see [Frame rates](#frame-rates--帧率)), each episode's `group`, the held-out lists (or `heldout_disabled`) and the warnings that applied. Two tasks that would be written under one text are refused at planning; `camera_map` and `cameras` must map to distinct output keys; LeRobot sources whose `observation.state` names differ from the output's are refused with the differences. A dry run runs the same held-out group check as the export and leaves no plan behind.

Each episode row in `meta/episodes.jsonl` of a LeRobot export carries `pool_key`, `pool_source` and `pool_fingerprint`; `meta/levi_provenance.jsonl` maps new to source episode indices. Not in this version: LeRobot v3 output (needs lerobot's v2.1→v3 converter; convert the v2.1 export with it) and exporting a recipe as a training manifest without copying data. To move an export to another machine see [Remote transfer](#remote-transfer--远程传输).

导出格式：LeRobot v2.1（多来源合并，按任务顺序→来源→片段排序，统一相机键和 fps，状态/动作维度或分辨率不一致时拒绝并列出差异）、RECAP 价值数据集（结局按“人工标签 > 机器人标志”，无结局的片段排除并列出）、原始采集目录（按任务重编号，可硬链接）。暂不支持：LeRobot v3 输出、导出为训练清单。传到其他机器见 [远程传输](#remote-transfer--远程传输)。


## Frame rates / 帧率

Stored videos run at 10 fps for human collection and at about 9.4–10 fps for policy rollouts (online-RL runs about 9.5, with single episodes down to about 8.6; direct deployment and student policies about 9.96). LeRobot and RECAP exports are written at 10 fps by default (`--fps`, or the fps field of the export panel). How a slower rollout meets that rate depends on the timing mode (`--timing resample|retime`, the **Timing** select of the export panel, `options.timing` of the API; `levi/pool/timing.py`, `levi/conversion/options.py`):

- **`retime`** (default of `recap_value`) keeps every captured row and declares the rows at the export fps. A rollout captured at 9.5 fps and declared at 10 fps has a timeline about 5% shorter than the recording (6% at 9.4 fps), so its motion plays about 5% faster than it happened. Rows and pixels are unchanged.
- **`resample`** (default of `lerobot_v21`) drops rows to reach the export fps and never adds any. A raw rollout measured below the export fps cannot be converted at it, and the export is refused before anything is converted (`Raw captures converted at … fps, not 10 …; lower the export fps to 9 or use timing retime`). Lower `--fps` to the nearest integer below the slowest measured rate, or choose `retime`.
- A raw capture copy (`raw_capture`) has no time axis: the option is ignored (recorded as `null`) and the panel hides it.
- LeRobot v2.x sources are copied as they are, so their own fps must equal the export fps in either mode.

**Preview notes.** The index keeps each raw episode's measured rate (`measured_fps`; an index from before this column needs a rescan, and until then no note appears). With an export fps and a timing mode, the composition preview and `levi pool recipe show NAME --format … --fps … [--timing …]` add:

- `source_fps_below_export` (`resample`): how many raw episodes are recorded below the export fps, the slowest rate, and the fixes (`suggested_fps`, or `retime`). It is a note in the preview and in a dry run, not an error; it carries `refused: true` because the export will certainly fail, and the export panel disables **Dry run** and **Start export** while it stands.
- `retime_time_scale` (`retime`, informational, `level: info`): shown only when some raw source differs from the export fps by more than 2%; it gives the episode count and the largest deviation (`max_deviation_percent`, `direction`: e.g. "the time axis is up to 5.3% shorter").

**Record.** `pool_export.json` has `timing` (`mode`, `export_fps`, `source_fps_min/max`, `time_scale_min/max`, `episodes_off_by_over_2_percent`; `null` for a raw capture copy) and, per episode of a LeRobot or RECAP export, `source_fps` (measured from the video; a LeRobot source's own fps) and `time_scale` (exported duration ÷ recorded duration: `source_fps / fps` for `retime`, 1 for `resample`, which picks rows by time). `params.timing` and `conversion.timing` hold the mode actually used, also when the default applied.

存储的视频：人工采集为 10 fps，策略 rollout 约 9.4–10 fps（在线 RL 约 9.5，个别片段低至约 8.6；直接部署和学生策略约 9.96）。LeRobot 与 RECAP 导出默认按 10 fps 写入（`--fps`，或导出面板的 fps 字段）。较慢的 rollout 如何对上这个帧率，取决于时间模式（`--timing resample|retime`，导出面板的“时间模式”下拉框，API 的 `options.timing`；`levi/pool/timing.py`、`levi/conversion/options.py`）：

- **`retime`**（`recap_value` 的默认）保留每个采集行，并按导出 fps 声明。按 9.5 fps 采集、按 10 fps 声明的 rollout，时间轴比实际录制短约 5%（9.4 fps 时约 6%），动作播放比实际发生时快约 5%。行数和画面不变。
- **`resample`**（`lerobot_v21` 的默认）只丢行、不补行。实测低于导出 fps 的原始 rollout 无法按该 fps 转换，导出会在转换开始前被拒绝（`Raw captures converted at … fps, not 10 …; lower the export fps to 9 or use timing retime`）。可把 `--fps` 降到不高于最慢实测帧率的最大整数，或改用 `retime`。
- 原始采集目录拷贝（`raw_capture`）没有时间轴：该选项被忽略（记录为 `null`），面板也不显示。
- LeRobot v2.x 来源原样复制，所以无论哪种模式，自身的 fps 都必须等于导出 fps。

**预览提示。** 索引记录每个原始片段的实测帧率（`measured_fps`；旧索引没有这一列，需要重新扫描，在此之前不会出现提示）。给出导出 fps 和时间模式后，组合预览和 `levi pool recipe show 名称 --format … --fps … [--timing …]` 会增加：

- `source_fps_below_export`（`resample`）：有多少原始片段的录制帧率低于导出 fps、最慢的帧率，以及解决办法（`suggested_fps`，或改用 `retime`）。它在预览和试运行中只是提示而不是错误；带 `refused: true`，因为导出必然失败，所以该提示存在时导出面板会禁用“试运行”和“开始导出”。
- `retime_time_scale`（`retime`，仅供参考，`level: info`）：只有某个原始来源与导出 fps 相差超过 2% 时才显示，给出受影响的片段数和最大偏差（`max_deviation_percent`、`direction`，例如“时间轴最多缩短 5.3%”）。

**记录。** `pool_export.json` 有 `timing`（`mode`、`export_fps`、`source_fps_min/max`、`time_scale_min/max`、`episodes_off_by_over_2_percent`；原始采集目录拷贝为 `null`），LeRobot 与 RECAP 导出的每个片段还有 `source_fps`（由视频实测；LeRobot 来源为其自身 fps）和 `time_scale`（导出时长 ÷ 录制时长：`retime` 为 `source_fps / fps`，`resample` 按时间取行，为 1）。`params.timing` 和 `conversion.timing` 记录实际使用的模式，使用默认值时也一样。

## Jobs: states, resume, errors, cleanup / 作业：状态、续做、错误、清理

A scan, export or push runs as a worker process; the worker owns the job record (`<workspace>/pool/jobs/<id>.json`), so the final state survives the service. The page, `levi pool jobs` and `GET /api/levi/pool/jobs/{id}` show:

| State | Meaning |
| --- | --- |
| `planned` / `running` | Planned; the worker works (its heartbeat, `<id>.beat.json`, is rewritten every 2 s) |
| `stalled` | The worker is alive but nothing moved (no heartbeat, or no unit finished) for `LEVI_POOL_STALL_SECONDS` |
| `cancelling` / `cancelled` | Cancel was asked and is being carried out / done: an export's `.partial` folder is removed |
| `interrupted` | The worker is gone without finishing: the service stopped or restarted (`levi stop`), the process was killed (SIGTERM, SIGKILL, out of memory), the machine went down. The `.partial` folder and its journal stay; the record says why (`reason`, `interrupted_at`) |
| `failed` | A fatal error (disk full, too many bad episodes, …): `error.json` has type, message, stage and a hint. The partial output is kept when it holds finished work |
| `done` / `done_with_errors` | Finished; `done_with_errors` when some episodes failed and were left out (`errors.jsonl`) |

The API adds `age_seconds` (since the last heartbeat or progress write) and `idle_seconds` (since the work last moved), `resumable`, `rerunnable`, `partial`, `error_info` and `failures`. A job whose worker has vanished is reported `interrupted` on the next read, never left as `running`; older records that say `succeeded` read as `done`.

### Resume, re-run, cancel

An export works in **units** and writes a journal into its `.<name>.partial` folder: `resume.json` (the plan's hash, options, counters) and `resume.units.jsonl` (append-only; one line per finished unit with the size and sha256 of each file it made: a converted part of raw episodes, a merged video, a copied episode). Raw captures are converted in parts of several episodes; a part that fails is retried episode by episode.

`levi pool export --resume <job-id>`, `POST /api/levi/pool/jobs/{id}/resume` and the **Resume** button continue an interrupted or failed export:

1. The frozen plan must match the journal (same options, episodes and target) and the source episodes must be unchanged since the scan (stat signatures), the held-out lists must be as planned, and the held-out check runs again. If not, the resume is refused with the reason (409) and **Re-run** is the way on.
2. Every finished unit is verified on disk (the file exists, its size matches and, up to 64 MiB, its sha256); a unit that fails is done again.
3. The rest is converted, the dataset is finalised and `.partial` is renamed. The result equals an uninterrupted run's (`pool_export.json` also records `resumed`, `resumes` and `interruptions`).

**Re-run** (`POST …/rerun`, button) plans again from the saved recipe and options and starts it, removing the old unfinished output first. **Cancel** (`POST …/cancel`) is told apart from a stop by a marker written before the signal: a cancel removes the partial output at once; a service stop, a kill or a crash keeps it. Cancelling an interrupted or failed job removes its partial too. A push resumes by running rsync again (`--partial` keeps what arrived); a raw-capture copy resumes per episode like an export; an interrupted scan simply starts again as a new job (the index is replaced atomically). `LEVI_POOL_AUTO_RESUME=1` resumes interrupted exports when the service starts.

### Stopping the service

`levi stop` refuses while pool, conversion, RECAP or segmentation jobs run (it lists them with their progress and exits 3): `levi stop --wait [minutes]` waits for them (60 by default) and then stops, `levi stop --force` stops anyway (a pool export is then marked `interrupted` with the reason `service stopped` and can be resumed; the other job kinds behave as before). The core's stop route (`POST /api/levi/agent/v1/core/stop`) answers 409 with the list unless `{"force": true}`; `levi agent core stop --force` is the same. Ctrl+C on the foreground `levi` cannot ask first: it interrupts the exports the same way.

### Errors and logs

- `<id>.log`: one timestamped log per job (stages, warnings, tracebacks), rotated at 4 MiB into `<id>.log.1`; `<id>.stdio` holds whatever the process printed outside it. `GET /api/levi/pool/jobs/{id}/log?kb=64`, `levi pool jobs log <id>`, the page's **View log**.
- An episode that fails to convert is written to `<id>.errors.jsonl` (episode, unit, stage, exception type, message, traceback, time) and left out with the reason `convert_error` (a capture-check failure keeps `conversion_preflight` and is listed there too); the export continues and ends `done_with_errors`. It stops (`failed`, resumable) when more than `on_error_max_fraction` (default 0.1; `--on-error-max-fraction`) of the planned episodes failed to convert, or on a fatal error (disk full, out of memory, permission). `pool_export.json` records `errors`, the exclusions with their reasons, `resumed`, `resumes` and `interruptions`.
- Exit codes and signals become sentences with a hint (143 stopped by the service or the user, 137/-9 killed and probably out of memory, disk full, permission denied, a missing source, a held-out refusal). `GET …/error-report` (the page's **Copy error report**) bundles the state, `error.json`, the failed episodes and the log tail.
- Before it starts an export estimates its size (the planned sources' videos ×1.3, minus what an unfinished partial already holds) against the free space of the target volume less `LEVI_POOL_FREE_MARGIN_GIB`, and refuses at planning (also in a dry run) and at start with the numbers. With `--timing resample` it also refuses before converting when a raw source runs below the export fps.

### Clearing and deleting jobs

Each job in the page's recent-jobs list has **Clear record** (removes the record, log and progress files; the exported data stays) and, for exports, **Delete record and files** (also removes the export directory and the partial leftover *of that job*). Several jobs can be selected (**Clear selected**, **Delete selected (with files)**), and **Clear all failed and interrupted jobs** removes the records and partial folders of failed, interrupted and cancelled jobs; it never touches a finished export. CLI: `levi pool jobs delete <id> [--files] [--yes] [--force]`, `levi pool jobs clear-failed [--yes]`; API: `GET /jobs/{id}/delete-preview?files=`, `DELETE /jobs/{id}?files=&force=`, `POST /jobs/delete` (`{ids, files, force}`), `POST /jobs/clear-failed`. The confirmation shows the exact path, its size, episodes, format, creation time and whether it was sent to a remote; Cancel has the focus by default.

The server deletes a directory only when all of this holds, and checks it again right before the first file goes:

1. it is the job's recorded target (or its `.partial`) **and that job produced it**: `pool_export.json` names the job (`job_id`), a partial's journal likewise. When a failed job and its successful re-run share a name, deleting the failed record with files leaves the directory of the successful one;
2. it carries the pool marker (`pool_export.json`, or the partial's `resume.json`) and holds the episodes the export recorded; otherwise something else changed it, and it needs a second confirmation (`--force`, the dialog's **Delete anyway**);
3. it lies inside `LEVI_EXPORT_ROOTS` or the workspace's export folder;
4. it is not inside a pool source dataset and does not contain a source or a pool root;
5. it is not a symbolic link and resolves to its own name inside its parent; links inside it are unlinked, never followed;
6. no push of it is running and no other live job uses it. A running job is never cleared: cancel it first.

Every removal is appended to `<workspace>/pool/deleted.jsonl` (time, job, what, path, bytes, files, who and how; `GET /api/levi/pool/deleted`); nothing else of a deleted export is kept. The response and the page report the bytes freed.

### Cleanup

`levi pool clean [--dry-run] [--all-partials]`, the page's **Cleanup** section (`GET/POST /api/levi/pool/cleanup`: partials and old jobs with sizes and ages, delete buttons, the free space of the volumes) and `levi clean` share one sweeper, which also runs at service start and after every job. Rules: a finished export leaves no `.partial` and no temporary file; a cancelled job's partial goes at once; an interrupted or failed export keeps its partial for `LEVI_POOL_PARTIAL_TTL`; a finished job's log and files go after `LEVI_POOL_JOB_TTL` (its record becomes a summary); stale `*.tmp` and `.index.*.parquet` files go after an hour. A live job's files (its worker's identity is checked) and finished exports are never touched; `--all-partials` also removes partials that could still be resumed. A partial found without a job or journal is only removed by `--all-partials`.

作业是一个工作进程；工作进程自己维护作业记录（`<workspace>/pool/jobs/<id>.json`），所以最终状态在服务停止后也在。页面、`levi pool jobs` 和 `GET /api/levi/pool/jobs/{id}` 显示的状态：

| 状态 | 含义 |
| --- | --- |
| `planned` / `running` | 已计划 / 运行中（心跳 `<id>.beat.json` 每 2 秒更新） |
| `stalled` | 进程还在，但 `LEVI_POOL_STALL_SECONDS` 内没有任何进展（没有心跳，或没有完成任何单元） |
| `cancelling` / `cancelled` | 已请求取消 / 已取消：导出的 `.partial` 目录被删除 |
| `interrupted` | 工作进程没做完就没了：服务被停止或重启（`levi stop`）、进程被杀（SIGTERM、SIGKILL、内存不足）、机器宕机。`.partial` 目录和日志保留；记录里写明原因（`reason`、`interrupted_at`） |
| `failed` | 致命错误（磁盘满、失败片段太多等）：`error.json` 记录类型、信息、阶段和处理建议；已有完成的工作时保留未完成输出 |
| `done` / `done_with_errors` | 已完成；有片段失败并被排除时为 `done_with_errors`（见 `errors.jsonl`） |

接口另外给出 `age_seconds`（距上次心跳或进度写入）、`idle_seconds`（距上次有进展）、`resumable`、`rerunnable`、`partial`、`error_info`、`failures`。工作进程消失的作业，下一次读取时就是 `interrupted`，不会一直显示 `running`；旧记录里的 `succeeded` 读作 `done`。

**继续、重新运行、取消。** 导出按“单元”推进，并在 `.<名称>.partial` 里写日志：`resume.json`（计划哈希、选项、计数）和 `resume.units.jsonl`（只追加；每个完成的单元一行，含所产出文件的大小和 sha256：一批原始片段的转换结果、一个合并后的视频、一个拷贝的片段）。原始采集按若干片段一批转换；一批失败时逐个片段重试。`levi pool export --resume <作业号>`、`POST /api/levi/pool/jobs/{id}/resume` 和“继续”按钮从日志接着做：（1）冻结的计划必须与日志一致（选项、片段、目标相同），来源片段自扫描以来没有变化，留出清单与计划一致，并重新做留出检查——否则拒绝并说明原因（409），此时用“重新运行”；（2）每个已完成单元都在磁盘上核对（文件存在、大小一致，64 MiB 以内再核对 sha256），核对不过的单元重做；（3）其余部分转换、收尾并把 `.partial` 改名，结果与不中断的运行一致（`pool_export.json` 还记录 `resumed`、`resumes`、`interruptions`）。“重新运行”按保存的配方和选项重新规划并启动，先清除旧的未完成输出。“取消”通过信号前写下的标记与“被停止”区分：取消立即删除未完成输出；服务停止、被杀或崩溃则保留。取消一个已中断或失败的作业同样会删除其未完成输出。推送靠再次运行 rsync 继续（`--partial` 保留已到达的部分）；原始采集拷贝和导出一样按片段续做；中断的扫描直接作为新作业重来（索引是原子替换的）。`LEVI_POOL_AUTO_RESUME=1` 让服务启动时自动继续被中断的导出。

**停止服务。** 有训练池、转换、RECAP 或分割作业在运行时，`levi stop` 会拒绝（列出作业和进度，退出码 3）：`levi stop --wait [分钟]` 等它们结束（默认 60 分钟）后再停止，`levi stop --force` 强行停止（训练池导出会标为 `interrupted`，原因 `service stopped`，可继续；其他类型作业行为不变）。核心服务的停止接口在没有 `{"force": true}` 时返回 409 和作业列表；`levi agent core stop --force` 同理。在前台按 Ctrl+C 无法先询问，会用同样方式中断导出。

**错误与日志。** `<id>.log` 是每个作业一份带时间戳的日志（阶段、警告、堆栈），超过 4 MiB 轮转为 `<id>.log.1`；`<id>.stdio` 保存日志之外进程打印的内容。可用 `GET …/log?kb=64`、`levi pool jobs log <id>` 或页面“查看日志”读取。转换失败的片段写入 `<id>.errors.jsonl`（片段、单元、阶段、异常类型、信息、堆栈、时间），以 `convert_error` 为由被排除（未通过采集检查的仍为 `conversion_preflight`，同样列入）；导出继续，结束时为 `done_with_errors`。失败片段超过计划片段的 `on_error_max_fraction`（默认 0.1，`--on-error-max-fraction`）或遇到致命错误（磁盘满、内存不足、权限）时导出停止（`failed`，可继续）。`pool_export.json` 记录 `errors`、带原因的排除、`resumed`、`resumes`、`interruptions`。退出码和信号会变成带建议的句子（143 被服务或用户停止，137/-9 被杀、多半内存不足，磁盘满，权限不足，来源缺失，留出拒绝）；`GET …/error-report`（页面“复制错误报告”）打包状态、`error.json`、失败片段和日志末尾。导出开始前会估算大小（计划来源的视频 ×1.3，减去未完成输出已占的部分），与目标磁盘剩余空间减 `LEVI_POOL_FREE_MARGIN_GIB` 比较，不够就在规划（含试运行）和启动时拒绝并给出数字；`--timing resample` 时如有原始来源帧率低于导出 fps，也在开始转换前拒绝。

**清除和删除作业。** 页面“最近的作业”里每个作业有“清除记录”（删除记录、日志和进度文件，导出的数据保留）；导出作业还有“删除记录和文件”（同时删除该作业产生的导出目录和未完成残留）。可多选（“清除所选”“删除所选（含文件）”），“清除全部失败/中断的作业”会删除失败、中断和已取消作业的记录及其未完成目录，绝不动已完成的导出。命令行：`levi pool jobs delete <id> [--files] [--yes] [--force]`、`levi pool jobs clear-failed [--yes]`；接口：`GET /jobs/{id}/delete-preview?files=`、`DELETE /jobs/{id}?files=&force=`、`POST /jobs/delete`、`POST /jobs/clear-failed`。确认框显示确切路径、大小、片段数、格式、创建时间和是否传到过远程，默认焦点在“取消”。服务端只在下列条件全部满足时删除目录，并在删第一个文件前再检查一次：（1）它是该作业记录的目标（或其 `.partial`），并且**是该作业产生的**（`pool_export.json` 记有 `job_id`，未完成目录的日志同理）——失败作业与其成功重跑同名时，带文件删除失败作业的记录不会动成功作业的目录；（2）带有训练池标记（`pool_export.json`，或未完成目录的 `resume.json`）且片段数与导出记录一致，否则说明被别的东西改过，需要二次确认（`--force`、对话框里的“仍然删除”）；（3）在 `LEVI_EXPORT_ROOTS` 或工作区导出目录之内；（4）不在任何来源数据集之内，也不包含来源或池根；（5）不是符号链接，且解析后仍是其父目录下的同名目录，目录内的链接只解除、不跟随；（6）没有正在运行的推送，也没有其他运行中的作业在用它。运行中的作业不能清除，先取消。每次删除都追加到 `<workspace>/pool/deleted.jsonl`（时间、作业、对象、路径、字节数、文件数、谁以何种方式；`GET /api/levi/pool/deleted`），被删除导出的其他内容一概不保留；返回值和页面显示释放的字节数。

**清理。** `levi pool clean [--dry-run] [--all-partials]`、页面“清理”区（`GET/POST /api/levi/pool/cleanup`：未完成目录和旧作业的大小与年龄、删除按钮、磁盘剩余空间）和 `levi clean` 共用一个清理程序，服务启动时和每个作业结束后也会运行。规则：完成的导出不留 `.partial` 和临时文件；取消的作业立即删除其未完成目录；中断或失败的导出保留 `LEVI_POOL_PARTIAL_TTL`；已结束作业的日志和文件在 `LEVI_POOL_JOB_TTL` 后删除（记录缩为摘要）；过期一小时的 `*.tmp`、`.index.*.parquet` 会被清掉。运行中作业的文件（会核对工作进程身份）和完成的导出永远不动；`--all-partials` 连仍可继续的未完成目录也删除；没有作业也没有日志的未完成目录只有 `--all-partials` 会删。

## The page / 训练池页面

`/pool` (top navigation **Training pool / 训练池**, and a card on the Workbench). It is the same backend as `levi pool …`.

- **Pool folders** (top): the roots, the last scan time and counts, **Scan now** with a progress bar and Cancel, and the recent jobs (scans, exports, pushes) with their status.
- **Filters** (left): category (原始人工采集 / 原始 rollout / LEVI 处理后 / 外部 / 归档), source (searchable, counts), a task search, outcome (all / robot flag success / verified success), a gripper facet, three policy facets (策略模型 / Policy model, 检查点 / Checkpoint, 运行方式 / How it was run: 直接部署, DSRL, RLT, SFE, 学生策略, 未知; each with counts and shown only when the selection holds rollouts) and date. *Show hidden* switches on held-out episodes (留出测试集), copies and the archive; each shows how many it hides.
- **Tasks and episodes** (centre): the task table (episodes per category, frames, success rate, how its rollouts were run, **+ Add** puts the task at the end of the composition; a task with more than 100 available episodes, or more than the composition's balanced count, opens a chooser first: number of episodes (a number field and a slider, default the balanced count), share of successes (natural share / all successes / all failures / custom %) and how to pick (smart pick / random / in order), with a hint of the resulting split and a warning when the share cannot be met; clicking a task narrows the episode table to it) and the paged episode table with source, category, task, frames, outcome, the policy label (rollouts) and badges for held-out, copy, non-standard and not exportable. An episode of a source registered in LEVI links to the viewer; otherwise its path is shown. A held-out row has no checkbox and cannot be part of an export: the server excludes it whatever the page sends.
- **Composition** (right): the ordered task list (drag a task by its grip, or use its up / down buttons; the order is the export order); each task shows *Picked N of M · successes a · failures b* with **Edit** (the same chooser), a warning when the requested mix or count cannot be met, and **Show picked episodes** (the episodes with quality score and why); the per-task cap (for tasks with no count of their own), seed, non-standard folders, the constraints taken from the filters, and a live preview (episodes, frames, held-out excluded, the overall success / failure mix with the category and method shares, and every other exclusion with its reason). Recipes are saved, loaded and deleted by name.
- **Export**: format, dataset name, output directory (a folder outside `LEVI_EXPORT_ROOTS` is flagged in the field and refused by the server), fps, timing (LeRobot and RECAP: a select with a one-line explanation), camera mapping, hard links (raw capture copy only), **Dry run** and **Start export** with progress. A finished export links to its `pool_export.json` and offers **Send to remote / 传到远程**.

Page routes beyond the table above: `GET /api/levi/pool/facets` (facet counts and what the toggles hide), `outcome=robot_flag_success|verified_success`, `date_from`, `date_to` on `tasks` and `episodes`, and each episode row carries `viewer` (its LEVI viewer path or `null`).

页面在 `/pool`（顶部导航“训练池”，工作台也有入口），与 `levi pool …` 共用后端：顶部是池目录、上次扫描和“立即扫描”；左侧分面（类别、来源、任务搜索、结局、策略模型、检查点、运行方式、日期，可显示留出测试集、副本、归档并显示被隐藏的数量）；中间是任务表和片段表（已登记的数据集可跳到片段查看器，否则显示路径）；右侧是组合（有序任务列表，可拖动或用上下按钮排序；每个任务显示“选 N / 共 M · 成功 a · 失败 b”，可编辑片段数、成功占比和选取方式，可查看所选片段及质量分；片段数超过 100 的任务添加时先弹出选择面板；每任务上限、种子、实时预览（含成功/失败构成）及各类排除原因、命名保存/载入/删除）和导出面板（格式、名称、输出目录、fps、时间模式、相机映射、硬链接、试运行、进度、`pool_export.json` 链接）。留出片段在页面上不可选，服务端也会排除。

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

## API / 接口

All routes are behind the service's UI token and same-origin check.

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/api/levi/pool/status` | Settings, the last scan's summary, recent jobs, free space of the export volumes |
| GET | `/api/levi/pool/sources?category=&show_archive=` | Sources with format, category, episodes, copies, held-out, exportable |
| GET | `/api/levi/pool/tasks?category=&source=&search=&format=&show_heldout=&show_copies=&show_archive=` | Per task: episodes, frames, categories, success rate, spellings, `policy_methods`, `grippers`; takes the same policy, `robot` and `gripper` filters |
| GET | `/api/levi/pool/episodes?…&task=&outcome=&policy=&policy_model=&policy_checkpoint=&policy_method=&robot=&gripper=&limit=&offset=` | The index, paged |
| POST | `/api/levi/pool/scan?rehash=` | Start a scan job |
| GET | `/api/levi/pool/jobs`, `/api/levi/pool/jobs/{id}` | Scan and export jobs with progress and result |
| GET / PUT / DELETE | `/api/levi/pool/recipes`, `/api/levi/pool/recipes/{name}` | Recipe CRUD (the body's `name` must match the URL) |
| POST | `/api/levi/pool/preview` | `{ "recipe": {…}, "format": "recap_value", "fps"?, "timing"? }` → counts and exclusions (with `fps` and a format that has a time axis, the `warnings` also judge the raw captures at that fps and `timing`: see [Frame rates](#frame-rates--帧率)); per task `available`, `successes`, `failures`, `selected`, `selected_successes`, `selected_failures`, `shortfall`, `notes`; the overall `mix` |
| POST | `/api/levi/pool/selection` | `{ "recipe": {…}, "task": "…" }` → the episodes picked for that task (`quality_score`, `sel_stratum`, `selection_reason`, `viewer`) and the task's report; 404 for a task the recipe lacks |
| POST | `/api/levi/pool/suggest` | `{ "recipe": {…}, "task": "…" }` → `available`, `successes`, `failures`, `suggested_count` (the balanced default) for adding that task |
| POST | `/api/levi/pool/export` | `{ "recipe_name": "…" or "recipe": {…}, "options": {"format", "name", "output_dir", "fps", "timing", "cameras", "camera_map", "hardlink", …}, "dry_run": false }`; 403 for a path outside `LEVI_EXPORT_ROOTS` or inside a source, 400 for an existing target or an empty selection |
| GET | `/api/levi/pool/facets?category=&show_heldout=&show_copies=&show_archive=` | Facet counts for the page (including `policy_models`, `policy_checkpoints`, `policy_methods`, `robots`, `grippers`) and what the toggles hide |
| POST | `/api/levi/pool/jobs/{id}/cancel` | Stop a running job for good (an export's partial is removed); an interrupted or failed job becomes cancelled and loses its partial |
| POST | `/api/levi/pool/jobs/{id}/resume` | Continue an interrupted or failed job (409 with the reason when an export's unfinished output cannot be trusted) |
| POST | `/api/levi/pool/jobs/{id}/rerun` | Plan the job again from its saved recipe and options and start it |
| GET | `/api/levi/pool/jobs/{id}/log?kb=64`, `/api/levi/pool/jobs/{id}/error-report` | The log tail; state, structured error, failed episodes and log tail |
| GET / DELETE | `/api/levi/pool/jobs/{id}/delete-preview?files=`, `/api/levi/pool/jobs/{id}?files=&force=` | What clearing or deleting a job would remove; do it (409 with the reason when refused) |
| POST | `/api/levi/pool/jobs/delete`, `/api/levi/pool/jobs/clear-failed` | Bulk clear or delete `{ids, files, force}`; clear failed, interrupted and cancelled jobs and their partials |
| GET / POST | `/api/levi/pool/cleanup`, `/api/levi/pool/deleted` | Partials, old jobs and free space; delete named ones or `{sweep: true}`; the deletion log |
| GET | `/api/levi/pool/jobs/{id}/summary` | `pool_export.json` of a finished export job |
| GET | `/api/levi/pool/corrections`, `/api/levi/pool/corrections/{version}?status=&batch=` | Task correction versions with counts; one version's proposals with status and index match |
| GET | `/api/levi/pool/corrections/copies` | Copies whose task texts differ, for a person to decide |
| POST | `/api/levi/pool/corrections/{version}/review` | A person's decision `{ "decision": "approved" \| "rejected", "reviewer": "…", "sha256": "<the version's>", "ids" \| "batch" \| "all", "exclude"?, "note"? }`; 403 for an agent credential, 401 without the UI token |
| GET | `/api/levi/pool/remotes` | Registered remote targets |
| PUT / DELETE | `/api/levi/pool/remotes/{name}` | Register (`{"spec": "[user@]host:/path", "port"?}`) or forget a target; unknown fields such as `password` are refused |
| POST | `/api/levi/pool/push` | `{ "target": "…", "export_job": "…" or "source": "<export dir>", "dry_run": false }`: rsync over SSH as a cancellable job |

所有路由都在服务的界面令牌和同源检查之后：

- **只读查询**：`status`（设置、上次扫描摘要、近期作业）、`sources`、`tasks`、`episodes`（分页）和 `facets`（页面的分面计数，以及开关隐藏了多少）。`tasks`、`episodes` 和 `facets` 都接受策略模型、检查点和运行方式过滤。
- **扫描与作业**：`POST scan?rehash=` 启动扫描作业；`jobs` 和 `jobs/{id}` 给出进度和结果；`POST jobs/{id}/cancel` 停止运行中的扫描、导出或推送；`jobs/{id}/summary` 返回已完成导出的 `pool_export.json`。
- **选择**：`recipes` 是选择的增删改查（请求体的 `name` 必须与 URL 一致）；`POST preview` 返回数量和排除原因，每个任务有 `available`、`successes`、`failures`、`selected`、`shortfall` 等字段，以及整体 `mix`；`POST selection` 返回某个任务选中的片段和该任务的报告，选择里没有该任务时返回 404；`POST suggest` 返回加入某个任务时的可用数量和均衡的默认数量。
- **任务文本订正**：`GET corrections` 列出版本和各状态数量，`GET corrections/{version}` 列出提议、状态和与索引的匹配，`GET corrections/copies` 列出文本不同的副本；`POST corrections/{version}/review` 记录人的批准或驳回（agent 凭据返回 403，缺界面令牌返回 401）。
- **导出与传输**：`POST export` 接受 `recipe_name` 或 `recipe`、`options` 和 `dry_run`；路径在 `LEVI_EXPORT_ROOTS` 之外或在源数据集内部时返回 403。`remotes` 和 `remotes/{name}` 登记或删除远程目标（不接受 `password` 等未知字段）；`POST push` 以可取消的作业通过 SSH 运行 rsync。
