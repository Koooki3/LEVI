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

From the page (VALUE MODEL → Compute advantages), from a terminal (`uv run levi recap run local/<name> --checkpoint <n> [--episodes 0,3] [--threshold X] [--sft]`, `uv run levi recap show local/<name> [--episode N]`) or the API (`/api/annotation/recap/status|run|jobs/<id>|jobs/<id>/cancel|summary|episodes/<n>`, `repo_id` in the query). One job per dataset at a time. A job runs in its own process group like a SAM3 job, is stopped past `LEVI_RECAP_VALUE_TIMEOUT_SECONDS` (default 21600) or after `LEVI_RECAP_VALUE_STALL_SECONDS` (default 1800) without progress, and is refused under `LEVI_CPU_ONLY=1` (the fake provider stays available) or with less than `LEVI_RECAP_VALUE_MIN_FREE_MIB` (default 6000) of free GPU memory. `LEVI_RECAP_VALUE_WORKER_PYTHON` names the worker's Python (default `integrations/recap_value/.venv/bin/python`), `LEVI_RECAP_VALUE_BATCH_SIZE` the inference batch (default 32) and `LEVI_RECAP_VALUE_DEVICE` the device (`auto`, `cuda`, `cuda:N`, `cpu`). Agents read the labels with `recap.status` and `recap.get` (read-only).

Returns need each episode's outcome, with the export's priority: a human label, then the capture's `levi_outcome`, then an RLinf-format RECAP dataset's per-episode `is_success` in `meta/episodes.jsonl`. Episodes without one are left out and listed (`skipped_episodes` in the summary); naming them explicitly is refused. `dataset_type: sft` treats every episode as a success and, as in RLinf, labels every frame positive.

#### Dataset type: `auto`, the dataset setting and values only

The value V(o_t) does not read outcomes; returns, advantages and labels do. The dataset type is therefore a property of the data, not of a model or a run. `dataset_type` (API `run`, `levi recap run --dataset-type`) is `rollout` (the default, unchanged), `sft` or `auto`. `auto` resolves, in order, to: the dataset setting (`source: user`); the type a LEVI `recap_value` export wrote into `meta/levi_recap.json` (`manifest`); `rollout` when any episode has an outcome (`outcomes`); otherwise **values only** (`value_only`, `no_outcomes`). A values-only result stores V(o_t) and leaves advantages, the threshold, the return range and every label null (`labels: false` in `status.current` and the episode payload; `positive_fraction` and `mean_advantage` null in the summary). Unlabelled rollouts are therefore never turned into all-positive demonstrations. Training manifests that need labels (`advantage_positive_mask`, `advantage_weighted`) refuse a values-only result; other operations carry its values with null labels. `levi recap threshold` refuses it too, and a comparison with a values-only side compares values only (`labels_unavailable`).

The dataset setting is stored in `recap_values/<name>/dataset.json`: `levi recap settings local/<name> [--dataset-type auto|rollout|sft]`, or `GET/POST /api/recap/settings` (`{"repo_id", "dataset_type"}`; `auto` removes the setting). `GET /api/recap/status` reports it as `dataset_type: {setting, dataset_type, source}`, and each result records `dataset_type`, `dataset_type_source` and the requested type (`request.dataset_type_requested`).

Each episode in `summary.json` carries `mean_value`, `min_value` and `max_value` (the range of its value curve). Results computed before this field existed lack the last two keys; readers treat them as optional.

数据集类型（中文）：价值 V(o_t) 不读结局，回报、优势和布尔标签才需要结局，所以“数据标签规则”属于数据集，不属于模型或某次计算。`dataset_type` 取 `rollout`（默认，行为不变）、`sft` 或 `auto`。`auto` 依次取：数据集设置（`user`）、LEVI `recap_value` 导出在 `meta/levi_recap.json` 写的类型（`manifest`）、任一片段有结局时为 `rollout`（`outcomes`），否则为**仅价值**（`value_only`）：只保存 V(o_t)，优势、阈值、回报范围和所有标签都为空（`labels: false`），因此未标注的策略 rollout 不会被当成全正的示范数据。需要标签的训练清单操作（`advantage_positive_mask`、`advantage_weighted`）和 `levi recap threshold` 拒绝仅价值结果；对比时只比较价值（`labels_unavailable`）。数据集设置存在 `recap_values/<名称>/dataset.json`，用 `levi recap settings` 或 `GET/POST /api/recap/settings` 查看和修改（`auto` 即删除设置）。`summary.json` 每个片段另有 `min_value`、`max_value`（价值曲线的最小、最大值），旧结果没有这两项，读取时按可选处理。

### Advantage and threshold

RLinf's formula (`compute_advantages.py`, commit 807e5fd), per episode of n frames with lookahead N:

```text
R_t = normalize(G_t − G_{t+N})   if t + N < n, V_next = V(o_{t+N})
R_t = normalize(G_t)             otherwise,    V_next = 0
A_t = R_t + γ^min(N, n−t) · V_next − V(o_t)          normalize(x) = (x − return_min)/(return_max − return_min) − 1
```

G is the export's return (−1 per step, terminal 0 or `failure_reward`); with γ ≠ 1 the reward sum is discounted instead. A frame is positive when A_t ≥ threshold. The threshold is, in order: the request's `threshold` (`manual`); the checkpoint's `unified_threshold` (`checkpoint`); else the (1 − `positive_quantile`) percentile of this run's advantages (`dataset_quantile`). RLinf computes the unified threshold over all its training datasets together, so a threshold from one dataset is only comparable within that dataset. The tests run RLinf's own loop on the same values and require identical results.

### Storage

`outputs/LEVI/workbench/recap_values/<catalog name>/revisions/<id>/` holds one `episode-NNNNNN.parquet` per episode (`episode_index, frame_index, timestamp, value, value_next, reward_sum, return, advantage, positive`), `advantages.parquet` with the columns of RLinf's `meta/advantages_{tag}.parquet` (keyed by this dataset's episode and frame indices — map through `source_demo` when a converted dataset renumbers episodes), `revision.json` (checkpoint name, sha256 and manifest, request, threshold and its source, return range, outcomes, view fingerprint, worker provenance, LEVI commit) and `summary.json`; `current.json` names the revision shown. Results are per catalog name — a namespace keeps its own — and outside agent bundles. A revision becomes **stale** when the capture's fingerprint (or a LeRobot dataset's revision) or an outcome label changes after it was computed.

### Historical versions and comparisons / 历史版本与结果对比

In the episode viewer's **VALUE MODEL** section, choose a published result for A and optionally a second result for B. Each choice identifies its checkpoint, training step, computation time and revision, and shows its exact threshold, return range, calculation parameters and episode/frame coverage. Browsing a result does not change `current.json` or the checkpoint selected for the next computation. Choose **Demonstrations (SFT)** for shared demonstration data; this calculation treats every episode as successful and every advantage label as positive. The computation type is separate from the historical result being viewed.

A and B have separate advantage rows and overlaid Value curves (A solid, B dashed). A missing episode or a frame excluded by static filtering stays blank; the viewer never fills it from another revision. Dataset comparison metrics use only common episode and original frame indices with matching timestamps. They include Value/continuous-advantage means, mean absolute differences, correlation, positive fractions, label agreement and the counts of labels that differ. Coverage counts include episodes and frames present in only one result. A constant curve or fewer than two points has no correlation; no common frames produces no numerical comparison. A failed comparison leaves A readable and suspends B's overlay.

Values are normalized using each result's own return range. Inverse-normalized Value metrics are also supplied when both ranges are valid: `V_return = (V + 1) * (return_max - return_min) + return_min`. The interface marks differences in return range, threshold, lookahead, gamma, failure reward, dataset type, static filter, precision and value-bin support. Normalized differences and label disagreement alone do not establish which model is better. Rollout labels use the stored exact threshold with `advantage_continuous >= threshold`; SFT labels remain all positive even when continuous advantages lie below it.

The read-only API is:

```text
GET /api/recap/revisions?repo_id=local/<name>
GET /api/recap/summary?repo_id=local/<name>&revision_id=<id>
GET /api/recap/episodes/<n>?repo_id=local/<name>&revision_id=<id>
GET /api/recap/compare?repo_id=local/<name>&a=<id>&b=<id>
```

Omitting `revision_id` preserves the current-result behavior. Invalid revision identifiers return 400 and unknown revisions return 404. Different saved source fingerprints or dataset metadata revisions, inconsistent timestamps, duplicate frame identities or missing published episode files return 409. Historical results can still be browsed when stale, with a warning. A native LeRobot metadata revision covers metadata file statistics, not parquet or video contents; it cannot establish identical inputs. If either historical result has only this metadata revision or lacks a source fingerprint, comparison reports that provenance could not be verified. Outcome separation uses only matching outcome labels saved with both revisions, so editing a label later does not change historical metrics; these are descriptive metrics over the computed subset.

在片段回放页的 **VALUE MODEL** 区选择历史结果 A，也可再选择 B。每份结果显示检查点、训练步数、计算时间、revision、精确阈值、回报范围、计算参数与已计算的片段和帧数。查看历史结果不改写 `current.json`，也不改变下一次计算选用的检查点。共同示范数据应选择 **示范数据（SFT）**：计算时所有片段视为成功，优势布尔标签全部为 `True`。计算类型与正在查看的历史结果各自独立。

两份结果的优势标签分行显示，Value 曲线叠加显示，A 为实线、B 为虚线。未计算的片段和静止过滤排除的帧留空，不借用其他版本补齐。数据集对比只统计相同片段、相同原始帧编号且时间戳一致的共同帧，给出 Value/连续优势的均值、平均绝对差、相关系数、正标签比例、标签一致率和差异计数，同时列出各版本独有的片段与帧。常数曲线或少于两个点时无相关系数；没有共同帧时无数值对比。对比失败时仍可查看 A，B 暂停叠加。

各版本的 Value 使用各自的回报范围归一化；两份范围均有效时，API 也给出按上述公式恢复到原回报单位的 Value 对比。界面明确提示回报范围、阈值、前瞻步数、折扣因子、失败惩罚、数据类型、静止过滤、精度和价值分箱范围的差异。归一化数值变化或标签不一致不能直接说明哪个模型更好。rollout 按保存的精确阈值执行 `advantage_continuous >= threshold`；SFT 即使连续优势低于阈值，布尔标签也全部为正。

上面的 API 只读；省略 `revision_id` 时仍读取当前结果。非法 revision 返回 400，不存在的结果返回 404。保存的来源指纹或数据集元数据版本不同、时间戳不一致、帧编号重复或已发布的片段文件缺失时返回 409。历史结果失效后仍可查看，但会显示提示。原生 LeRobot 的元数据版本只覆盖元数据文件统计，不覆盖 parquet 或视频内容，不能证明输入完全相同；任一历史结果只有元数据版本或缺少来源指纹时，会说明来源无法核实。成功/失败区分统计只使用两份结果保存且一致的结局标签，之后修改标签不会改变历史统计。这些指标只描述实际计算的子集。

### When a real checkpoint arrives

Import it, then confirm against the training run (its Hydra config and `meta/mixture_config.yaml`): `critic_expert_variant` (compare with the inferred one), the camera mapping `views` (the value dataset's repack keys), `max_token_len`, `return_min` / `return_max` (`data.return_min/max` or the datasets' stats), `unified_threshold` and `positive_quantile` for the tag, `gamma` / `failure_reward` / `lookahead`, the base models (SigLIP2 so400m-patch14-224, Gemma3-270M and its tokenizer — gated on the Hub; import them with `levi recap base import`) and whether the training data was static-filtered. For the FR3 RECAP run, `--preset fr3_recap` covers all of this except the return range and the threshold. The worker then loads the weights strictly and refuses on any missing or unexpected key (an absent `lm_head.weight`, unused by the value, is only reported).

### 中文摘要

LEVI 可以直接运行 RECAP 价值模型（RLinf `ValueCriticModel`），为每一帧计算 V(o_t)，按 RLinf 的 N 步优势公式得到 A_t 与正/负标签，结果按数据集（命名空间各自独立）保存在 `outputs/LEVI/workbench/recap_values/<名称>/`，并在回放页标注时间轴的 **VALUE MODEL** 区显示（优势段绿/红、价值曲线、计算/重算控件），片段列表用小徽标显示各片段的正样本比例。检查点放在 `checkpoints/recap_value/<名称>/`，用 `levi recap import` 导入（复制或硬链接，不改动源文件），`manifest.json` 记录 worker 原本只能猜测的全部选择：专家规模 `critic_expert_variant`、相机映射 `views`、基础模型与分词器路径、回报范围 `return_min/return_max`、统一阈值 `unified_threshold` 等；缺少必填项时检查点标记为未就绪。阈值优先级：请求中的 `threshold` > 检查点的 `unified_threshold` > 本数据集优势值的 (1 − positive_quantile) 分位数。推理在独立环境 `integrations/recap_value` 中运行（`setup.sh` 安装并打上 openpi 的 transformers 补丁），`fake` 提供者无需模型即可试用页面。拿到真实检查点后，按上表逐项确认这些字段。FR3 RECAP 训练（`env_type` 为 `fr3_recap`）可用 `--preset fr3_recap` 一次填好（view1→base、hand→左腕、右腕置零且 mask 关闭、gemma_1m、action_dim 7/horizon 10、训练静止帧过滤），回报范围与统一阈值另行给出。基础模型用 `levi recap base import` 放入 `checkpoints/recap_value/_base/`，逐文件与官方版本或包内 sha256 清单比对，不符的只能标为开发用。训练数据经过静止帧过滤（5 mm / 0.01 rad，夹爪指令翻转前后 2 帧保留）时，对原始采集视图默认只在保留帧上计算价值、回报与优势，被滤掉的帧不打标签；`--static-filter auto|on|off` 控制。`levi recap threshold` 按 RLinf 方法在多个数据集上合并计算统一阈值。
