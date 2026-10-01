# Training manifests

A training manifest tells a fixed training recipe which frames of a dataset go into its loss and with what weight. It also records the evidence behind each choice. LEVI never rewrites the dataset: the trainer loads the same LeRobot dataset as before and reads the manifest next to it. Operations are task-generic. They read episode outcomes and frame labels, never the words a particular task uses.

```bash
levi export operations                                    # built-in operations and their parameters
levi export manifest local/<name> --operation verified_success [--param fallback=exclude] [--param undecided=include] \
    [--task "stack the plates of same color together"] [--episodes 0-49] \
    [--anchored-run <run id>] [--allow-candidate-anchored] [--anchored-task "<task it is valid for>"] \
    [--recap-revision <id>] [--allow-stale] [--output <new dir in the workspace>] [--json]
levi export list local/<name>                             # manifests written for a dataset
levi export diff <manifest dir A> <manifest dir B>        # how different two training inputs are
```

The API is `GET /api/levi/manifest/operations`, `GET /api/levi/manifest?repo_id=…` and `POST /api/levi/manifest` ([API](API.md)). A manifest can be made for a registered LeRobot v2.x dataset or for a raw capture's browsing view. It is written to `outputs/LEVI/exports/<name>/training_manifests/<timestamp>-<operation>/`. Each namespace keeps its own evidence and its own manifests.

## Operations

| Operation | Frames in the loss | Needs |
| --- | --- | --- |
| `all_rollouts` | every frame in scope, weight 1 (the no-curation baseline) | — |
| `robot_flag_success` | episodes the robot flagged success (`levi_outcome`, or `is_success` in `meta/episodes.jsonl`) | — |
| `verified_success` | episodes whose verdict is success: a human label first, then the anchored review (only for the tasks it is declared valid for), then the `fallback` (`robot_flag`, the default, or `exclude`). An anchored success the review left undecided is out by default; `undecided=include` keeps it | an anchored review run, or human outcome labels in scope (a task without anchored-review rules is verified by people alone) |
| `advantage_positive_mask` | frames the RECAP value model labels positive; `unlabelled` frames are excluded (default) or included | a RECAP revision |
| `advantage_weighted` | every frame, weight `positive_weight` (1.0) or `negative_weight` (0.2); frames without a label get `unlabelled_weight` (0.0) | a RECAP revision |

`--task` and `--episodes` narrow the scope for every operation. Frames outside the scope stay in the file with `include = false` and the reason `out_of_scope`, so one file always covers the whole dataset. `--anchored-task` names the tasks the anchored review's spec has been validated for. Episodes of any other task keep the robot's flag even when the review did cover them. RECAP labels that are stale (the capture or an outcome label changed after they were computed) are refused unless you pass `--allow-stale`. The manifest then lists the reasons.

## Files

`frames.parquet` has one row per frame of the dataset:

| Column | Meaning |
| --- | --- |
| `episode_index`, `frame_index`, `timestamp` | the dataset's own indices (join key for the trainer) |
| `source_demo`, `task` | a raw capture's demo folder; the episode's task |
| `include`, `weight` | what the operation decided; `weight` is 0 when not included |
| `exclude_reason` | why a frame is out, e.g. `robot_flag_failure`, `anchored_failure`, `unverified`, `advantage_negative`, `advantage_unlabelled`, `out_of_scope` |
| `episode_success`, `episode_success_source` | the verdict and where it came from: `human`, `anchored`, `robot_flag` |
| `robot_flag`, `human_label`, `anchored_outcome`, `anchored_undecided` | each source on its own. `anchored_undecided` is true when the anchored outcome rests on something undecided: a required label with only unknown events or an unknown start-check waiver; for a success, also an undecided veto or a waiver its own events contest (see [Anchored review](ANCHORED_REVIEW.md#start-check-and-vetoes)). `verified_success` leaves an undecided anchored success out (`exclude_reason` `anchored_undecided`); pass `undecided=include` to keep it. Human labels are not affected |
| `subtask_id`, `subtask_outcome`, `subtask_attempt` | the active annotation's subtask covering the frame |
| `subtask_review` | who stands behind that segment: `auto` (written by the live service, unreviewed), `edited` (an automatic one a person changed), empty (a person, or an agent a person reviewed); `manifest.json` `annotation.subtask_auto_share` is the share of labelled frames that are `auto` |
| `recap_value`, `recap_advantage`, `recap_positive` | RECAP V(o_t), A_t and its label (null where not labelled) |

`manifest.json` records:
- `levi_commit`;
- the operation and its resolved parameters, and the scope;
- the dataset: catalog name, namespace and base, root, fps, and a content `fingerprint` (sha256 of the metadata files and of every episode's data parquet, the capture's `source_fingerprint`);
- the annotation revision and digest;
- the anchored review run: spec id, version and sha256, and the provider and model;
- the RECAP revision: checkpoint and its sha256, threshold and where it came from, lookahead, static filter, stale reasons;
- counts: included episodes by verdict source, frames and weight mass;
- the sha256 of `frames.parquet`;
- a per-episode table, which includes `rollout_source_demo` when the dataset has it.

`levi export diff` reports the episodes and frames only one manifest includes. It also gives `input_difference`, the total-variation distance between the two sampling distributions: 0 means the same training input, 1 means disjoint inputs. Two arms whose difference is only a few percent cannot be told apart by training.

## Reading a manifest in a trainer

[`integrations/training_manifest/levi_manifest_reader.py`](../integrations/training_manifest/levi_manifest_reader.py) is one standalone file that needs only numpy and pyarrow. Copy it into the training repository. It checks that `frames.parquet` matches its manifest and that the dataset's metadata matches the fingerprint. It then answers the four questions a loader has:

```python
from levi_manifest_reader import ManifestFrames, audit

m = ManifestFrames.open(manifest_dir, dataset_root)       # refuses a changed dataset
dataset = LeRobotDataset(repo_id, root=dataset_root, episodes=m.episodes())
weights = m.sampling_weights(zip(episode_of_item, frame_of_item))
sampler = torch.utils.data.WeightedRandomSampler(weights, num_samples, generator=g)
mask = m.action_mask(ep, frame, action_horizon)           # [H] bool, one per action target
prompt = m.prompt(task, ep, frame, rng)                   # RECAP CFG: "\nAdvantage: positive" on 90 % of positive frames
```

For openpi (`pi05_fr3_*` configs), without changing openpi's main branch:
- **Data.** Wrap `create_torch_dataset`'s `LeRobotDataset` so it loads only `m.episodes()`.
- **Mask.** Attach `mask` to each sample. Action target k counts only when frame t+k is in the same episode and included; padding past the episode end counts as excluded, like `action_is_pad`. Frames are never concatenated across a gap.
- **Loss.** Replace the `jnp.mean` over `Pi0.compute_loss`'s [B, H] with `sum(mask * loss) / max(sum(mask), 1)`.
- **Sampling.** Draw with the weighted sampler. To keep demonstrations at a fixed share, sample the demonstration set and the manifest's rollouts as two streams.
- **CFG arm.** Apply `m.prompt` as a prompt transform, and serve the policy with the positive suffix. Record that inference prompt in the checkpoint's metadata.

**Checking that the data reached the loss.** After a short run, give `audit(m, drawn)` the (episode, frame) pairs the sampler produced. Excluded frames must never appear (`excluded_drawn == 0`), and each episode's share of draws should follow its weight mass (`observed` against `expected`).

## 中文摘要

训练清单（training manifest）告诉固定的训练配方：数据集中哪些帧进入 loss、权重多少，并记录每个决定依据的证据。LEVI 不改写数据集，训练端照常加载同一个 LeRobot 数据集，再读取旁边的清单。

**命令与接口**：`levi export manifest local/<名称> --operation <操作>` 生成清单，`levi export diff A B` 比较两份训练输入的差异。接口为 `/api/levi/manifest`。

**内置操作**（与任务无关）：
- `all_rollouts`：全部帧；
- `robot_flag_success`：机器人标志为成功的片段；
- `verified_success`：按“人工标签 > 锚定复核（仅限声明为已验证的任务）> 机器人标志”判为成功的片段；锚定复核给出的成功若结论未定（起始检查或否决规则无法判断），默认排除，`undecided=include` 可保留；只有人工标签、没有锚定复核也可以运行；
- `advantage_positive_mask`：只保留 RECAP 正优势帧；
- `advantage_weighted`：按优势正负加权。

**输出文件**：
- `frames.parquet`：每帧一行，包括 include/weight、片段成败及其来源、当前标注的子任务与结果、RECAP 值/优势/正负；
- `manifest.json`：记录 LEVI 提交、数据集内容指纹、命名空间、标注修订、锚定复核的运行与规格、RECAP 的检查点、修订与阈值，以及操作名与参数。

**训练端读取**：用单文件读取器 `integrations/training_manifest/levi_manifest_reader.py`，提供片段过滤、加权采样、逐动作步掩码、RECAP 条件提示。训练后用 `audit` 核对：被排除的帧从未被抽到，且各片段的抽样比例符合权重。

**Candidate anchored reviews.** The newest anchored review is used for the verdict column only when its spec is not a `candidate`. A candidate (not validated; the live service's generic release review is one) is passed over and listed in `manifest.json` under `anchored.skipped_candidate_runs`; name the run with `--anchored-run`, or pass `--allow-candidate-anchored` (API `allow_candidate_anchored`), to use it on purpose. The manifest then records `anchored.spec_status`.
