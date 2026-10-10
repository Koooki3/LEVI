# RECAP value dataset / RECAP 价值数据集

Export target `recap_value` (`levi/conversion/outputs/recap_value.py`): a dataset for training the value function of RECAP — "RL with Experience and Corrections via Advantage-conditioned Policies", the method behind π\*0.6 ([paper](https://arxiv.org/abs/2511.14759)).

## Is there an official format? / 官方格式

No. Physical Intelligence has not released RECAP training code or a dataset specification (openpi issue #857 is open). Three concrete references exist, and this export satisfies all three at once:

| Reference | What it reads |
| --- | --- |
| π\*0.6 paper | per-step reward −1, terminal 0 on success / −C_fail on failure; per-task normalization of the value to (−1, 0) by the maximum episode length; 201-bin distributional value; per-episode human success labels; human corrections force the improvement indicator |
| [RLinf RECAP](https://github.com/RLinf/RLinf) (`examples/offline_rl/advantage_labeling/recap`, pinned commit `db66ac56`) | a LeRobot dataset; for `rollout` datasets a per-frame `is_success` column read **at each episode's last frame** (`sft` datasets are all successes); step 1 writes `meta/returns.parquet` (`returns_{tag}.parquet` when tagged) with `episode_index int64, frame_index int64, return float32, reward float32, prompt string` and flat `return`/`reward` entries in `meta/stats.json`, whose `min`/`max` the value model normalizes with; defaults `gamma = 1.0`, `failure_reward = -300` |
| [LeRobot RECAP proposal](https://github.com/huggingface/lerobot/pull/3245) (open) | `meta/episode_labels.csv` with `episode_index,success` |
| [hzm8341/pi0.6](https://github.com/hzm8341/pi0.6) (unofficial) | its own `recap_labels.jsonl` / `lerobot_fields.npz`; not targeted — derive from the columns below if needed |

## What the export contains / 导出内容

A LeRobot v2.1 dataset (same layout, state/action and videos as the `lerobot_v21` target) plus:

| File / column | Content |
| --- | --- |
| `is_success` (bool, per frame) | `True` only on the last frame of a successful episode |
| `next.reward` (float32, per frame) | −1 per step; last step 0 (success) or `failure_reward` (failure) |
| `next.done` (bool, per frame) | `True` on each episode's last frame |
| `meta/returns.parquet` | RLinf's sidecar, byte-for-byte the values RLinf's own `compute_returns.py` produces (tested); `G_t = r_t + gamma · G_{t+1}` |
| `meta/stats.json` `return`, `reward` | RLinf's flat `{mean, std, min, max}` |
| `meta/episode_labels.csv` | `episode_index,success` (1/0) |
| `meta/levi_recap.json` | manifest: dataset type, gamma, failure reward, reward/return definition, outcome counts and label sources, per-task maximum episode length and the normalization rule, suggested 201 value bins, fps, consumer references and suggested RLinf repack keys |

Running RLinf step 1 (`compute_returns.py`) on the export regenerates the same sidecar, so step 1 can be skipped or used as a cross-check; continue with RLinf step 2 (value model SFT).

## Defaults and why / 默认设置

- `timing = retime`, `filter_static = false`: the per-step reward assumes one row per executed policy action. Resampling or static-frame filtering would silently change episode lengths and therefore returns — the target warns if either is re-enabled.
- `dataset_type = rollout`, `failure_reward = -300`, `gamma = 1.0`, no tag — RLinf's defaults.

## Outcome labels / 成功与失败标签

Priority: human label (sidebar dot, see [CONVERSION.md](CONVERSION.md#结局标签--episode-outcome-labels)) > `policy_rollout` capture metadata (`eval.outcome`) > `dataset_type = sft` (everything a success). An episode with no label makes the target **unsupported** for `rollout`, with three solutions offered in the Workbench:

1. label outcomes in LEVI (a raw capture is browsable through its view);
2. exclude the unlabeled episodes;
3. export as demonstrations (`sft`: every episode a success — the setting for teleoperation data).

Warnings (export still allowed): all episodes share one outcome (the value function cannot learn to separate them), single task (per-task normalization is trivial — fine for a first reproduction), no human-intervention data (the forced improvement indicator for corrections cannot be set), frames dropped by timing/filtering.

## Sources / 来源

- From a raw capture (`robot_capture`, `image_sequence`): the full pipeline with lossless retime — every video stream-copied, no re-encode.
- From an existing LeRobot v2.x dataset (`lerobot`): parquet rewritten with the RECAP columns, videos hard-linked (copied across filesystems), episodes optionally excluded and re-indexed, human labels applied; takes seconds. If that dataset was converted with resampling or filtering, the inspection flags it — for RECAP, re-export from the raw capture.

Real check: the `pick_screws_of_same_size_into_the_empty_slot_of_screw_box` eval batch (171 demos, 124 failure / 47 success) inspects as `recap_value: warnings` (single task, no interventions).

## Consuming it in RLinf / 在 RLinf 中使用

RLinf's value dataset repacks inputs by robot type; LEVI's camera features are `observation.images.hand` (wrist) and `observation.images.view1` by default. Either name the cameras to match an existing RLinf entry through the `cameras` option (e.g. `observation.images.image` / `observation.images.wrist_image` for `libero_v2`), or add an entry using the `suggested_repack_keys` from `meta/levi_recap.json`:

```python
"levi_fr3": {
    "observation/image": "observation.images.view1",
    "observation/wrist_image": "observation.images.hand",
    "observation/state": "observation.state",
    "actions": "action",
    "prompt": "prompt",
},
```

## Value model and advantage labels in LEVI / LEVI 中的价值模型与优势标签

The export above trains a value model; LEVI can also **run** one. A RECAP value model (RLinf's `ValueCriticModel`: SigLIP2-so400m + Gemma3-270M prefix, a Gemma value expert reading a `[CLS]` token, 201 bins over [−1, 0]) predicts V(o_t) for every frame. LEVI turns the values into RLinf's N-step advantage and a positive / negative label per frame, keeps them per dataset and shows them in the episode viewer: the annotations timeline gets a **VALUE MODEL** section under PERSISTENT and EVENTS — an ADVANTAGE row of runs (green positive, red negative, stronger the further from the threshold) and a VALUE row with the V(o_t) curve and a playhead marker — with the checkpoint and threshold in its header and a compute / recompute control (checkpoint, progress, cancel, error). The episode list shows each episode's positive fraction as a small badge.

The core stays model-free: inference runs in its own uv environment, `integrations/recap_value` ([README](../integrations/recap_value/README.md)), and a `fake` provider (a deterministic time-to-go curve, no model) runs with LEVI's own Python for tests and for trying the page.

### Checkpoints

Checkpoints live in `$LEVI_WORKSPACE/checkpoints/recap_value/<name>/` (`LEVI_RECAP_VALUE_CHECKPOINT_DIR` overrides it, inside the workspace): the weights and a `manifest.json` (`levi.recap_value.checkpoint.v1`). Importing copies the file — a hard link on the same filesystem — computes its sha256 and never moves or deletes the source:

```bash
uv run levi recap import /path/to/run/checkpoints/global_step_3000 --name fr3-step3000 \
  --views base=observation.images.view1,left_wrist=observation.images.hand \
  --siglip siglip2-so400m-patch14-224 --gemma3 gemma-3-270m --tokenizer gemma-3-270m
uv run levi recap set fr3-step3000 --return-min -700 --unified-threshold -0.0123
uv run levi recap checkpoints [--verify]     # readiness; --verify re-hashes the weights
uv run levi recap import --fake --name demo  # a fake checkpoint for the page
```

The source may be a `global_step_*` folder, its `actor` folder or `full_weights.pt` itself; the step is read from the path. When the worker environment exists, import reads the weights' shapes and fills `critic_expert_variant` from them (`variant_source: inferred`). Base-model paths are relative to the checkpoint folder or absolute inside the workspace.

Every choice the worker would otherwise have to guess is a manifest field; a checkpoint is **not ready** until the ones without a safe default are set:

| Field | Default | Meaning / what to confirm from the training run |
| --- | --- | --- |
| `critic_expert_variant` | *(none)* | value expert size; RLinf's yaml default is `gemma_1m`, its `from_checkpoint` default `gemma_100m` — confirm, or let import infer it from the weights |
| `views` | *(none)* | which camera fills `base_0_rgb`, `left_wrist_0_rgb`, `right_wrist_0_rgb`; `null` is a zero image with its mask off (RLinf's LiberoInputs keeps a wrist view, FrankaEEInputs pads both wrist slots) |
| `base_models.siglip`, `.gemma3`, `.tokenizer` | *(none)* | folders with `config.json` (weights optional — the checkpoint replaces them all) and the Gemma3 tokenizer |
| `return_min`, `return_max` | from the dataset | RLinf's `data.return_min/max` (or the training datasets' `stats.json`); without them LEVI uses this dataset's own returns and says so (`return_range_source: dataset`) |
| `unified_threshold` | *(none)* | the training mixture's `unified_threshold`; without it the threshold comes from this dataset |
| `max_token_len`, `num_bins`, `v_min`, `v_max`, `precision` | 200, 201, −1, 0, `bfloat16` | RLinf defaults |
| `gamma`, `failure_reward`, `lookahead`, `positive_quantile` | 1.0, −300, 10, 0.3 | RLinf defaults (`advantage_lookahead_step`, `positive_quantile`) |
| `env_type`, `model_type` | none, `pi05` | RLinf's robot/env type and openpi variant; `model_type` sets the mask of a zero-padded camera slot (on only for `pi0_fast`, as LiberoInputs / FrankaEEInputs do) |
| `action_dim`, `action_horizon` | 32, 50 | passed to the model config as RLinf's `from_checkpoint` does (not used by the value forward) |
| `static_filter` | none | the static-pose filter the training data went through (see below) |
| `provenance` | {} | where each number came from (`key=text`, e.g. the tag or job of `unified_threshold`) |

`--preset fr3_recap` fills every field of the FR3 RECAP run (RLinf 807e5fdd plus its local FR3 patches: `fr3_recap` goes through the libero branch of `build_input_transforms`, so `observation.images.view1` is `base_0_rgb`, `observation.images.hand` is `left_wrist_0_rgb`, `right_wrist_0_rgb` is zeros with its mask off, the prompt is the LeRobot task; `gemma_1m`, 201 bins over [−1, 0], `action_dim` 7, `action_horizon` 10, lookahead 10, quantile 0.3, the training static filter). The return range and the threshold belong to a returns / advantages tag and are given explicitly. A checkpoint is not ready when its views or `model_type` contradict its `env_type` preset, or when `critic_expert_variant` contradicts the variant the weights' shapes show. `levi recap inspect <name>` builds the model on the CPU and loads the weights with RLinf's rule, reporting `missing` / `unexpected` (the FR3 advantage job logged 0 / 0).

### Base models

`levi recap base import <folder> [--name] [--official repo] [--sha256-file list] [--weights] [--label TEXT]` copies (hard-links) a model folder's config, preprocessor and tokenizer files — weights only with `--weights`, since the checkpoint replaces every weight — into `checkpoints/recap_value/_base/<name>/`, where manifests reference it as `../_base/<name>`. It checks each file against the official Hub release (`google/gemma-3-270m`, `google/siglip2-so400m-patch14-224`: git blob ids and LFS hashes, which the Hub publishes even for the gated Gemma) and against a package's `sha256sum` list; a mismatch is refused unless `--label` marks the folder development-only. Results computed with an unverified folder carry `dev_only_base_models` in their revision and a "dev base model" mark in the timeline. `levi recap base list` shows the folders and their checks.

### Training static-pose filter

The FR3 RECAP training data (`sft_recap`, `rollouts_recap`) was cut from raw captures by `filter_static_pose_frames.py`: the later frame of an adjacent pose pair is dropped when the gripper command is unchanged, the wrapped Euler step is ≤ 0.01 rad and the XYZ step is < 5 mm, except within two rows of a gripper command flip — about a quarter of all frames. The value model has never seen those frames, and its returns and N-step advantages count kept steps. A checkpoint whose manifest has `static_filter` therefore labels a raw-capture view (converted one row per captured frame: `timing: retime`, no static filter of its own) over the kept frames only: the filter decision is read from the capture's `end_effector_pose.csv` / `gripper_state.csv` / `frames.csv` exactly as the script does (`levi/recap/static_filter.py`, checked against the script and against `rollouts_recap`'s episode lengths), values are computed on kept frames, returns and advantages run over the kept sequence, and the dropped frames stay unlabelled (the timeline leaves gaps; the header shows `static filter k/N`). `levi recap run --static-filter auto|on|off` (API `static_filter`): `auto` applies it to raw-capture views and leaves LeRobot datasets as they are (taken as already filtered, like the training sets); `on` refuses a dataset it cannot filter; `off` labels every frame. LEVI's own conversion filter (compared with the last kept frame) is a different rule and is not used.

### Unified threshold

`levi recap threshold local/a local/b … [--positive-quantile q] [--set <checkpoint> --provenance-text TEXT]` computes RLinf's unified threshold over the current revisions of several datasets together — the (1 − q) percentile of every frame's continuous advantage, `sft` datasets included — and can store it on a checkpoint with its provenance. The revisions must share checkpoint weights, lookahead, return range and gamma. When the training run's `meta/mixture_config.yaml` is available, prefer its value (`--unified-threshold`, provenance naming the job).

### Running

From the page (VALUE MODEL → Compute advantages), from a terminal (`uv run levi recap run local/<name> --checkpoint <n> [--episodes 0,3] [--threshold X] [--dataset-type auto|rollout|sft|value_only]`, `uv run levi recap show local/<name> [--model <checkpoint>] [--episode N]`) or the API (`/api/annotation/recap/status|run|jobs/<id>|jobs/<id>/cancel|summary|episodes/<n>`, `repo_id` in the query). One job per dataset at a time. Job folders stay bounded: once a job's result is published, its plan (`plans/<job>.json`), worker output (`results/<job>.*`) and progress file are removed (its record and log stay); a failed or cancelled job keeps them for diagnosis; and only the newest 20 finished job records of a dataset are kept — older ones go with all their files. A running job is never touched. A job runs in its own process group like a SAM3 job, is stopped past `LEVI_RECAP_VALUE_TIMEOUT_SECONDS` (default 21600) or after `LEVI_RECAP_VALUE_STALL_SECONDS` (default 1800) without progress, and is refused under `LEVI_CPU_ONLY=1` (the fake provider stays available) or with less than `LEVI_RECAP_VALUE_MIN_FREE_MIB` (default 6000) of free GPU memory. `LEVI_RECAP_VALUE_WORKER_PYTHON` names the worker's Python (default `integrations/recap_value/.venv/bin/python`), `LEVI_RECAP_VALUE_BATCH_SIZE` the inference batch (default 32) and `LEVI_RECAP_VALUE_DEVICE` the device (`auto`, `cuda`, `cuda:N`, `cpu`). Agents read the labels with `recap.status` and `recap.get` (read-only).

Returns need each episode's outcome, with the export's priority: a human label, then the capture's `levi_outcome`, then an RLinf-format RECAP dataset's per-episode `is_success` in `meta/episodes.jsonl`. Episodes without one are left out and listed (`skipped_episodes` in the summary); naming them explicitly is refused. `dataset_type: sft` treats every episode as a success and, as in RLinf, labels every frame positive.

#### Dataset type: `auto`, the dataset setting and values only

The value V(o_t) does not read outcomes; returns, advantages and labels do. The dataset type is therefore a property of the data, not of a model or a run. `dataset_type` (API `run`, `levi recap run --dataset-type`) is `auto` (the default), `rollout`, `sft` or `value_only`. `auto` resolves, in order, to: the dataset setting (`source: user`); the type a LEVI `recap_value` export wrote into `meta/levi_recap.json` (`manifest`); `rollout` when any episode has an outcome (`outcomes`); otherwise the old default `rollout` (`fallback`), which refuses a dataset without outcomes as before. Each result records the type, its `dataset_type_source` and a readable `dataset_type_reason`. **Values only** (`value_only`, asked for or set as the dataset type) stores V(o_t) and leaves advantages, the threshold, the return range and every label empty: `labels: false` in `status.current` and the episode payload, whose `positive` and `advantage` lists are empty (so a viewer shows "no advantage labels", never a negative band); `positive_fraction` and `mean_advantage` null in the summary; no `advantage` (label) column in `advantages.parquet`. Unlabelled rollouts are therefore never turned into all-positive demonstrations. Training manifests that need labels (`advantage_positive_mask`, `advantage_weighted`) refuse a values-only result; other operations carry its values with null labels. `levi recap threshold` refuses it too, and a comparison with a values-only side compares values only (`labels_unavailable`).

The dataset setting is stored in `recap_values/<name>/dataset.json`: `levi recap settings local/<name> [--dataset-type auto|rollout|sft|value_only]`, or `GET/POST /api/recap/settings` (`{"repo_id", "dataset_type"}`; `auto` removes the setting). `GET /api/recap/status` reports it as `dataset_type: {setting, dataset_type, source, reason}`, and each result records `dataset_type`, `dataset_type_source` and the requested type (`request.dataset_type_requested`).

Each episode in `summary.json` carries `mean_value`, `min_value` and `max_value` (the range of its value curve). Results computed before this field existed lack the last two keys; readers treat them as optional.

数据集类型（中文）：价值 V(o_t) 不读结局，回报、优势和布尔标签才需要结局，所以“数据标签规则”属于数据集，不属于模型或某次计算。`dataset_type` 取 `auto`（默认）、`rollout`、`sft` 或 `value_only`。`auto` 依次取：数据集设置（`user`）、LEVI `recap_value` 导出在 `meta/levi_recap.json` 写的类型（`manifest`）、任一片段有结局时为 `rollout`（`outcomes`），否则回退为旧默认 `rollout`（`fallback`，没有结局时和以前一样拒绝）；每个结果记录类型、`dataset_type_source` 和 `dataset_type_reason`。**仅价值**（`value_only`，需在请求或数据集设置里指定）只保存 V(o_t)，优势、阈值、回报范围和所有标签都为空（`labels: false`；片段接口的 `positive`、`advantage` 为空列表，界面显示“没有优势标签”而不是负优势；`advantages.parquet` 不写标签列），因此未标注的策略 rollout 不会被当成全正的示范数据。需要标签的训练清单操作（`advantage_positive_mask`、`advantage_weighted`）和 `levi recap threshold` 拒绝仅价值结果；对比时只比较价值（`labels_unavailable`）。数据集设置存在 `recap_values/<名称>/dataset.json`，用 `levi recap settings` 或 `GET/POST /api/recap/settings` 查看和修改（`auto` 即删除设置）。作业目录有上限：结果发布后删除该作业的计划（`plans/<作业>.json`）、worker 输出（`results/<作业>.*`）和进度文件，保留作业记录和日志；失败或取消的作业保留这些文件供诊断；每个数据集只保留最近 20 个已结束作业的记录，更早的连同其文件一起删除；运行中的作业从不处理。`summary.json` 每个片段另有 `min_value`、`max_value`（价值曲线的最小、最大值），旧结果没有这两项，读取时按可选处理。

### Advantage and threshold

RLinf's formula (`compute_advantages.py`, commit 807e5fd), per episode of n frames with lookahead N:

```text
R_t = normalize(G_t − G_{t+N})   if t + N < n, V_next = V(o_{t+N})
R_t = normalize(G_t)             otherwise,    V_next = 0
A_t = R_t + γ^min(N, n−t) · V_next − V(o_t)          normalize(x) = (x − return_min)/(return_max − return_min) − 1
```

G is the export's return (−1 per step, terminal 0 or `failure_reward`); with γ ≠ 1 the reward sum is discounted instead. A frame is positive when A_t ≥ threshold. The threshold is, in order: the request's `threshold` (`manual`); the checkpoint's `unified_threshold` (`checkpoint`); else the (1 − `positive_quantile`) percentile of this run's advantages (`dataset_quantile`). RLinf computes the unified threshold over all its training datasets together, so a threshold from one dataset is only comparable within that dataset. The tests run RLinf's own loop on the same values and require identical results.

### Storage

New results are kept one per value model (next section); this paragraph describes the original per-run layout (`LEVI_RECAP_STORE_LAYOUT=revisions`, and results written before the per-model default), whose files a model result's version folder repeats with `result.json` in place of `revision.json`. `outputs/LEVI/workbench/recap_values/<catalog name>/revisions/<id>/` holds one `episode-NNNNNN.parquet` per episode (`episode_index, frame_index, timestamp, value, value_next, reward_sum, return, advantage, positive`), `advantages.parquet` with the columns of RLinf's `meta/advantages_{tag}.parquet` (keyed by this dataset's episode and frame indices — map through `source_demo` when a converted dataset renumbers episodes), `revision.json` (checkpoint name, sha256 and manifest, request, threshold and its source, return range, outcomes, view fingerprint, worker provenance, LEVI commit) and `summary.json`; `current.json` names the revision shown. Results are per catalog name — a namespace keeps its own — and outside agent bundles. A revision becomes **stale** when the capture's fingerprint (or a LeRobot dataset's revision) or an outcome label changes after it was computed.

#### One result per value model (the default layout)

`LEVI_RECAP_STORE_LAYOUT` chooses the layout new results are written in: `models` (the default): one result per (dataset, value model), so computing again with the same checkpoint replaces its result instead of adding another one; or `revisions`: the original layout above, one revision per run (set it to go back). Both layouts are always read, side by side; switching never moves or deletes an existing revision. When both exist, `current.json` names the newest computation (a model or a revision), the result list shows model results first and then the original revisions, and a name that is both a model and a revision id means the model.

```text
recap_values/<name>/models/<checkpoint>/head.json        {"version": ...}: the live version
recap_values/<name>/models/<checkpoint>/v/<version>/     episode-NNNNNN.parquet, advantages.parquet,
                                                         summary.json, result.json (written last)
recap_values/<name>/current.json                         {"model": ...} (or {"revision_id": ...})
```

- **Replacing.** A run over the whole dataset writes a new version — every file fsynced, `result.json` last — then replaces `head.json` atomically and points `current.json` at the model. A crash before that switch leaves the previous version live (the next publication removes the unfinished directory under the dataset lock); after it, the new one. A replaced version is marked `RETIRED` and stays readable for 15 minutes, so a page or export that resolved it just before the switch finishes reading it; later publications reclaim it.
- **Subsets merge.** A run that names its episodes (`--episodes`, API `episodes`) merges them into the model's result: untouched episodes are hard-linked from the previous version, the summary, `advantages.parquet`, outcomes and coverage are rebuilt over all of them, and each episode in `summary.json` records `computed_at`, `job_id` and the `version` it was computed in (`result.json` lists them as `merged_from`, keeps this run's request as `last_request`, and counts the static filter's frames over every episode). Every setting that decides the numbers must be identical (`levi/recap/signature.py`): the whole checkpoint manifest except its descriptive fields (name, notes, provenance, step, import details) — weights, camera `views`, `critic_expert_variant`, `max_token_len`, `model_type`, `env_type`, value bins, precision, base models, advantage fields —, the run's dataset type, lookahead, gamma, failure reward, return range, threshold and their sources, the static filter, the base models' import records, the dataset fingerprint, LEVI's own computation version (`RECAP_COMPUTE_VERSION` in `levi/recap/advantage.py`, incremented whenever the return, advantage, threshold or label logic changes; the LEVI commit itself is not compared) and the worker's software (RLinf commit, torch, transformers and its patch, video decoder; not the device, batch size or timings). Every field is classified there as affecting the values or not, a test fails until a new field is classified, and an unclassified field counts as affecting. An episode the run was asked for but skipped (for example by the static filter) leaves the result with its reason. Otherwise the run is refused with 409 before the worker starts (and checked again, worker versions included, when the values arrive): a threshold or return range taken from the subset's own statistics would differ from the whole dataset's, so such a subset needs a stored threshold and return range or a whole-dataset run. A run over the whole dataset replaces the result whatever its parameters.
- **Names and versions.** In the API a model result's `revision_id` is the checkpoint name (an original-layout revision keeps its timestamp id); every payload also carries `model`, `version` and `layout`. `GET /api/recap/results` lists one row per model (then any original-layout revisions); `/api/recap/revisions` is the same list under its old name. `summary`, `episodes/<n>` accept `version=` and `compare` accepts `version_a=` / `version_b=`: a result recomputed since answers 409 with `{"detail": <text>, "code": "recomputed", "revision_id", "version"}` (the current version), so a page reloads instead of mixing versions; other refusals keep `{"detail": <text>}`. A comparison and a training manifest resolve their versions once and read every episode from them; the manifest records `model`, `version` and `result_digest` (the sha256 of `advantages.parquet`), since a replaced version cannot be looked up later. `levi export manifest --recap-model <checkpoint>` (alias `--recap-revision`) chooses the result.

- **Clearing.** `levi recap clear local/<name> … | --all [--include-jobs] [--apply]` lists the RECAP results of the named datasets — both layouts, plus `current.json`; with `--include-jobs` also the job records, plans and worker outputs — with their sizes, and deletes them only with `--apply`. A folder under `recap_values` of a dataset no longer registered can be named directly. It is refused while a job of that dataset runs and when a symbolic link lies on the way to anything it would remove (nothing is removed then), never touches checkpoints, datasets or the dataset setting (`dataset.json`), and appends each removed item (or a failure) to the folder's `cleared.jsonl` as it goes. A dry run writes nothing. The layout a job publishes in is fixed in its plan when it starts.

按价值模型保存结果（中文）：`LEVI_RECAP_STORE_LAYOUT` 决定新结果的写法：`models`（默认）：每个“数据集 × 价值模型”只有一份结果，同一模型再算一次就覆盖它；或 `revisions`：上面的原布局，每次运行一个 revision（设为它即可回退）。两种布局始终都能读，切换不会移动或删除已有 revision；两者并存时，`current.json` 指向最近一次计算（模型或 revision），结果列表先列模型结果再列原 revision，名称同时是模型名和 revision id 时按模型解析。整数据集重算先完整写出新版本（各文件 fsync，最后写 `result.json`），再原子替换 `head.json`；切换前崩溃则旧版本继续有效，未写完的目录由下一次发布在数据集锁内清掉；被替换的版本标记 `RETIRED`，保留 15 分钟供正在读它的页面或导出读完，之后回收。只算部分片段（`--episodes`）时，若决定数值的全部设置（`levi/recap/signature.py`：检查点清单中除名称、备注、来源说明、步数、导入信息外的所有字段，含相机映射 `views`、`critic_expert_variant`、`max_token_len`、`model_type`、`env_type`、分箱、精度、基础模型；本次运行的数据类型、前瞻步数、折扣、失败惩罚、回报范围、阈值及来源、静止过滤、基础模型导入记录、数据集指纹、LEVI 自身的计算版本 `RECAP_COMPUTE_VERSION`（回报、优势、阈值或标签逻辑一改就递增；不比较 LEVI 提交号）；worker 的 RLinf 提交、torch、transformers 及补丁、视频解码方式，不含设备、批大小和耗时）完全一致；每个字段都在那里明确归类，新增字段未归类时测试失败，运行时未归类的字段按“影响数值”处理，就按片段合并进该模型的结果，未重算的片段以硬链接沿用，`summary.json` 逐片段记 `computed_at`、`job_id`、`version`，`result.json` 记 `merged_from` 和 `last_request`，静止过滤帧数按全部片段重算；点名重算却被跳过的片段从结果中移出并保留原因；参数不同则在启动 worker 前返回 409（结果写入时再核对一次），请改为整数据集重算。API 里模型结果的 `revision_id` 就是检查点名，并带 `model`、`version`、`layout`；`GET /api/recap/results` 每个模型一行（`/api/recap/revisions` 为旧名）；`summary`、`episodes/<n>` 可带 `version=`，`compare` 可带 `version_a=`/`version_b=`，结果已被重算时返回 409，响应体为 `{"detail": 文字, "code": "recomputed", "revision_id", "version"}`（当前版本），其他拒绝仍只有 `detail`。对比和训练清单在开始时固定版本，清单记录 `model`、`version` 和 `result_digest`（`advantages.parquet` 的 sha256）；`levi export manifest --recap-model <检查点>`（旧名 `--recap-revision`）指定结果。`levi recap clear local/<名称> … | --all [--include-jobs] [--apply]` 列出（加 `--apply` 才删除）两种布局的结果和 `current.json`，`--include-jobs` 连同作业记录、计划和 worker 输出；也可直接写 `recap_values` 下已不在目录里的数据集文件夹名；该数据集有作业在跑、或要删除的路径上有符号链接时拒绝（此时什么都不删）；从不动检查点、数据集和数据集设置；每删一项（或失败一项）立即记入该文件夹的 `cleared.jsonl`；干跑不写任何文件。作业写入哪种布局在启动时写进计划，之后不随设置改变。

### Historical versions and comparisons / 历史版本与结果对比

In the episode viewer's **VALUE MODEL** section, choose a published result for A and optionally a second result for B. The pickers list **one row per value model** — model, training step, computation time and episode coverage — and recomputing with the same model replaces that row's result (in the `models` layout). Results in the original per-run layout (and every row from an older backend) are folded to the newest run of each checkpoint, and hidden when the checkpoint also has a model result; **Show earlier per-run results** lists them all again, and a selected result always stays listed. The details under the pickers show the result version (or the per-run id), label rule, exact threshold, return range, calculation parameters and episode/frame coverage. Browsing a result does not change `current.json` or the checkpoint selected for the next computation.

Every read is pinned to the version the list showed (`version`, `version_a`, `version_b`). When a result is recomputed meanwhile — on this page or by someone else — the read answers 409 and the page reloads the list and shows the new version with a short notice, instead of an error or a mix of versions; the A/B choices stay, since a model result keeps its name. Automatic reloads are limited to one every few seconds.

The compute controls choose only the checkpoint. The label rule is not chosen there: a run sends no `dataset_type`, so the backend resolves `auto` (see "Dataset type" above), and the controls show the outcome and its source — the dataset setting, the export's metadata, the outcome labels, or the fallback to policy rollouts, which still needs outcome labels. A demonstrations (SFT) rule says that every label will be positive; a values-only rule says that only the Value curve is computed. A **values-only** result never shows advantage or positive/negative wording: its advantage rows say "Values only (no success/failure labels)", the playhead readout shows V alone, and the curve's marker is neutral.

A and B have separate advantage rows and overlaid Value curves (A solid, B dashed). A missing episode or a frame excluded by static filtering stays blank; the viewer never fills it from another revision. Dataset comparison metrics use only common episode and original frame indices with matching timestamps. They include Value/continuous-advantage means, mean absolute differences, correlation, positive fractions, label agreement and the counts of labels that differ. Coverage counts include episodes and frames present in only one result. A constant curve or fewer than two points has no correlation; no common frames produces no numerical comparison. A failed comparison leaves A readable and suspends B's overlay.

Above the tables, **comparison charts** (Recharts, colours from the design tokens: A in the solid curve's colour, B in the dashed curve's with a dashed outline) show label agreement and positive-frame rates on all shared frames and per outcome, mean Value of success and failure episodes for A and B, the two Value distributions on common bins, the per-frame |B − A| distribution, and per-episode mean-Value differences (B − A). The per-episode chart keeps at most the 60 episodes with the largest differences (the current episode always, outlined) and says how many there were. Each chart has a text summary naming A and B and a **Table view** with the exact numbers; the drawing itself is hidden from assistive technology. The charts read two aggregates of `compare`: `by_outcome` (shared episodes grouped by the outcome both results saved, `unknown` otherwise: episodes, frames, mean of episode-mean V for A and B, frame-weighted label agreement and positive rates, null for a values-only side) and `distribution` (`value`: 20-bin counts of A's and B's V over common edges; `abs_diff`: 20-bin counts of |B − A| per frame), so a large dataset is never sent frame by frame. Against an older backend without them, the page derives the outcome groups and an episode-mean histogram from `per_episode`.

Values are normalized using each result's own return range. Inverse-normalized Value metrics are also supplied when both ranges are valid: `V_return = (V + 1) * (return_max - return_min) + return_min`. The interface marks differences in return range, threshold, lookahead, gamma, failure reward, dataset type, static filter, precision and value-bin support. Normalized differences and label disagreement alone do not establish which model is better. Rollout labels use the stored exact threshold with `advantage_continuous >= threshold`; SFT labels remain all positive even when continuous advantages lie below it.

The read-only API is:

```text
GET /api/recap/results?repo_id=local/<name>          (old name: /revisions)
GET /api/recap/summary?repo_id=local/<name>&revision_id=<id>[&version=<v>]
GET /api/recap/episodes/<n>?repo_id=local/<name>&revision_id=<id>[&version=<v>]
GET /api/recap/compare?repo_id=local/<name>&a=<id>&b=<id>[&version_a=<v>&version_b=<v>]
```

Omitting `revision_id` preserves the current-result behavior. Invalid revision identifiers return 400 and unknown revisions return 404. Different saved source fingerprints or dataset metadata revisions, inconsistent timestamps, duplicate frame identities or missing published episode files return 409. Historical results can still be browsed when stale, with a warning. A native LeRobot metadata revision covers metadata file statistics, not parquet or video contents; it cannot establish identical inputs. If either historical result has only this metadata revision or lacks a source fingerprint, comparison reports that provenance could not be verified. Outcome separation uses only matching outcome labels saved with both revisions, so editing a label later does not change historical metrics; these are descriptive metrics over the computed subset.

在片段回放页的 **VALUE MODEL** 区选择历史结果 A，也可再选择 B。选择框里**每个价值模型一行**（模型、训练步数、计算时间、覆盖片段数）；`models` 布局下用同一模型重新计算会覆盖该行的结果。旧的“每次运行一个 revision”布局里的结果（以及旧后端返回的所有行）按检查点只显示最新一次，该检查点已有模型结果时整组隐藏；勾选 **显示按次保存的旧结果** 可全部列出，已选中的结果始终保留在列表里。选择框下方的详情显示结果版本（或旧 revision 编号）、标签规则、精确阈值、回报范围、计算参数与已计算的片段和帧数。查看历史结果不改写 `current.json`，也不改变下一次计算选用的检查点。

每次读取都固定在列表显示的版本上（`version`、`version_a`、`version_b`）。结果在此期间被重新计算（本页或别人）时，读取返回 409，页面自动重新读取列表、显示新版本并给出简短提示，不报错、也不会混用两个版本；模型结果的名称不变，所以 A/B 选择保持不变。自动重新读取最多每几秒一次。

重新计算只选检查点，不再选“数据标签规则”：请求不带 `dataset_type`，由后端按 `auto` 判定（见上文“数据集类型”），界面显示判定结果和依据——数据集设置、导出元数据、结局标签，或回退为策略 rollout（仍需要结局标签）。判定为示范数据（SFT）时提示所有标签都为正；判定为仅价值时提示只计算价值曲线。**仅价值**结果不出现任何优势或正/负语义：优势行显示“仅价值（无成败标签）”，播放头读数只显示 V，曲线上的圆点为中性样式。

两份结果的优势标签分行显示，Value 曲线叠加显示，A 为实线、B 为虚线。未计算的片段和静止过滤排除的帧留空，不借用其他版本补齐。数据集对比只统计相同片段、相同原始帧编号且时间戳一致的共同帧，给出 Value/连续优势的均值、平均绝对差、相关系数、正标签比例、标签一致率和差异计数，同时列出各版本独有的片段与帧。常数曲线或少于两个点时无相关系数；没有共同帧时无数值对比。对比失败时仍可查看 A，B 暂停叠加。

表格上方是**对比统计图**（Recharts；颜色取设计令牌，A 与实线曲线同色，B 与虚线曲线同色并带虚线描边）：全部共有帧及各结局分组的标签一致率和正优势帧比例、A/B 在成功与失败片段上的平均价值、A/B 价值在同一组区间上的分布、逐帧 |B − A| 分布、逐片段平均价值差（B − A）。逐片段图最多显示差值最大的 60 个片段（当前片段始终保留并描边），并注明总数。每张图都有点名 A、B 的文字摘要和带精确数值的**表格视图**，图形本身对辅助技术隐藏。图表读取 `compare` 的两个聚合字段：`by_outcome`（共有片段按两份结果保存且一致的结局分组，否则为 `unknown`：片段数、帧数、A/B 片段平均价值的均值、按帧加权的标签一致率和正优势比例，仅价值一侧为 null）和 `distribution`（`value`：A、B 的价值在同一组边界上的 20 个区间计数；`abs_diff`：逐帧 |B − A| 的 20 个区间计数），大数据集不会逐帧传给页面。旧后端没有这两个字段时，页面用 `per_episode` 推出结局分组和按片段平均价值的直方图。

各版本的 Value 使用各自的回报范围归一化；两份范围均有效时，API 也给出按上述公式恢复到原回报单位的 Value 对比。界面明确提示回报范围、阈值、前瞻步数、折扣因子、失败惩罚、数据类型、静止过滤、精度和价值分箱范围的差异。归一化数值变化或标签不一致不能直接说明哪个模型更好。rollout 按保存的精确阈值执行 `advantage_continuous >= threshold`；SFT 即使连续优势低于阈值，布尔标签也全部为正。

上面的 API 只读；省略 `revision_id` 时仍读取当前结果。非法 revision 返回 400，不存在的结果返回 404。保存的来源指纹或数据集元数据版本不同、时间戳不一致、帧编号重复或已发布的片段文件缺失时返回 409。历史结果失效后仍可查看，但会显示提示。原生 LeRobot 的元数据版本只覆盖元数据文件统计，不覆盖 parquet 或视频内容，不能证明输入完全相同；任一历史结果只有元数据版本或缺少来源指纹时，会说明来源无法核实。成功/失败区分统计只使用两份结果保存且一致的结局标签，之后修改标签不会改变历史统计。这些指标只描述实际计算的子集。

### When a real checkpoint arrives

Import it, then confirm against the training run (its Hydra config and `meta/mixture_config.yaml`): `critic_expert_variant` (compare with the inferred one), the camera mapping `views` (the value dataset's repack keys), `max_token_len`, `return_min` / `return_max` (`data.return_min/max` or the datasets' stats), `unified_threshold` and `positive_quantile` for the tag, `gamma` / `failure_reward` / `lookahead`, the base models (SigLIP2 so400m-patch14-224, Gemma3-270M and its tokenizer — gated on the Hub; import them with `levi recap base import`) and whether the training data was static-filtered. For the FR3 RECAP run, `--preset fr3_recap` covers all of this except the return range and the threshold. The worker then loads the weights strictly and refuses on any missing or unexpected key (an absent `lm_head.weight`, unused by the value, is only reported).

### 中文摘要

LEVI 可以直接运行 RECAP 价值模型（RLinf `ValueCriticModel`），为每一帧计算 V(o_t)，按 RLinf 的 N 步优势公式得到 A_t 与正/负标签，结果按数据集（命名空间各自独立）保存在 `outputs/LEVI/workbench/recap_values/<名称>/`，并在回放页标注时间轴的 **VALUE MODEL** 区显示（优势段绿/红、价值曲线、计算/重算控件），片段列表用小徽标显示各片段的正样本比例。检查点放在 `checkpoints/recap_value/<名称>/`，用 `levi recap import` 导入（复制或硬链接，不改动源文件），`manifest.json` 记录 worker 原本只能猜测的全部选择：专家规模 `critic_expert_variant`、相机映射 `views`、基础模型与分词器路径、回报范围 `return_min/return_max`、统一阈值 `unified_threshold` 等；缺少必填项时检查点标记为未就绪。阈值优先级：请求中的 `threshold` > 检查点的 `unified_threshold` > 本数据集优势值的 (1 − positive_quantile) 分位数。推理在独立环境 `integrations/recap_value` 中运行（`setup.sh` 安装并打上 openpi 的 transformers 补丁），`fake` 提供者无需模型即可试用页面。拿到真实检查点后，按上表逐项确认这些字段。FR3 RECAP 训练（`env_type` 为 `fr3_recap`）可用 `--preset fr3_recap` 一次填好（view1→base、hand→左腕、右腕置零且 mask 关闭、gemma_1m、action_dim 7/horizon 10、训练静止帧过滤），回报范围与统一阈值另行给出。基础模型用 `levi recap base import` 放入 `checkpoints/recap_value/_base/`，逐文件与官方版本或包内 sha256 清单比对，不符的只能标为开发用。训练数据经过静止帧过滤（5 mm / 0.01 rad，夹爪指令翻转前后 2 帧保留）时，对原始采集视图默认只在保留帧上计算价值、回报与优势，被滤掉的帧不打标签；`--static-filter auto|on|off` 控制。`levi recap threshold` 按 RLinf 方法在多个数据集上合并计算统一阈值。
