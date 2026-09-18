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
