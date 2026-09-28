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

### When a real checkpoint arrives

Import it, then confirm against the training run (its Hydra config and `meta/mixture_config.yaml`): `critic_expert_variant` (compare with the inferred one), the camera mapping `views` (the value dataset's repack keys), `max_token_len`, `return_min` / `return_max` (`data.return_min/max` or the datasets' stats), `unified_threshold` and `positive_quantile` for the tag, `gamma` / `failure_reward` / `lookahead`, the base models (SigLIP2 so400m-patch14-224, Gemma3-270M and its tokenizer — gated on the Hub; import them with `levi recap base import`) and whether the training data was static-filtered. For the FR3 RECAP run, `--preset fr3_recap` covers all of this except the return range and the threshold. The worker then loads the weights strictly and refuses on any missing or unexpected key (an absent `lm_head.weight`, unused by the value, is only reported).

### 中文摘要

LEVI 可以直接运行 RECAP 价值模型（RLinf `ValueCriticModel`），为每一帧计算 V(o_t)，按 RLinf 的 N 步优势公式得到 A_t 与正/负标签，结果按数据集（命名空间各自独立）保存在 `outputs/LEVI/workbench/recap_values/<名称>/`，并在回放页标注时间轴的 **VALUE MODEL** 区显示（优势段绿/红、价值曲线、计算/重算控件），回合列表显示正样本比例徽标。检查点放在 `checkpoints/recap_value/<名称>/`，用 `levi recap import` 导入（复制或硬链接，不改动源文件），`manifest.json` 记录所有不可猜测的选择：专家规模 `critic_expert_variant`、相机映射 `views`、基础模型与分词器路径、回报范围 `return_min/return_max`、统一阈值 `unified_threshold` 等；缺少必填项时检查点标记为未就绪。阈值优先级：请求中的 `threshold` > 检查点的 `unified_threshold` > 本数据集优势值的 (1 − positive_quantile) 分位数。推理在独立环境 `integrations/recap_value` 中运行（`setup.sh` 安装并打上 openpi 的 transformers 补丁），`fake` 提供者无需模型即可试用页面。拿到真实检查点后请按上表逐项确认字段。FR3 RECAP 训练（`env_type` 为 `fr3_recap`）可用 `--preset fr3_recap` 一次填好（view1→base、hand→左腕、右腕置零且 mask 关闭、gemma_1m、action_dim 7/horizon 10、训练静止帧过滤），回报范围与统一阈值另行给出。基础模型用 `levi recap base import` 放入 `checkpoints/recap_value/_base/`，逐文件与官方版本或包内 sha256 清单比对，不符的只能标为开发用。训练数据经过静止帧过滤（5 mm / 0.01 rad，夹爪指令翻转前后 2 帧保留）时，对原始采集视图默认只在保留帧上计算价值、回报与优势，被滤掉的帧不打标签；`--static-filter auto|on|off` 控制。`levi recap threshold` 按 RLinf 方法在多个数据集上合并计算统一阈值。
