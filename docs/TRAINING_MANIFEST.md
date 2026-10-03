# Training manifests

A training manifest tells a fixed training recipe which frames of a dataset go into its loss and with what weight. It also records the evidence behind each choice. LEVI never rewrites the dataset: the trainer loads the same LeRobot dataset as before and reads the manifest next to it. Operations are task-generic. They read episode outcomes and frame labels, never the words a particular task uses.

```bash
levi export operations                                    # built-in operations and their parameters
levi export manifest local/<name> --operation verified_success [--param fallback=exclude] [--param undecided=include] \
    [--task "stack the plates of same color together"] [--episodes 0-49] \
    [--anchored-run <run id>] [--allow-candidate-anchored] [--anchored-task "<task it is valid for>"] \
    [--recap-revision <id>] [--allow-stale] [--prompt-template v1] [--subtask-max-chars 120] \
    [--output <new dir in the workspace>] [--json]
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
| `prompt_task`, `prompt_subtask`, `prompt_has_subtask`, `prompt_subtask_skip`, `prompt_subtask_source`, `episode_task_count` | the frame's two prompts and how they came to be, see [Prompt columns](#prompt-columns) |

`manifest.json` records:
- `levi_commit`;
- the operation and its resolved parameters, and the scope;
- the dataset: catalog name, namespace and base, root, fps, and a content `fingerprint` (sha256 of the metadata files and of every episode's data parquet, the capture's `source_fingerprint`);
- the annotation revision and digest;
- the anchored review run: spec id, version and sha256, and the provider and model;
- the RECAP revision: checkpoint and its sha256, threshold and where it came from, lookahead, static filter, stale reasons;
- counts: included episodes by verdict source, frames and weight mass;
- `prompt`: the prompt template (version and text), the subtask length limit, and how many frames got a subtask, were refused and were cut (see [Prompt columns](#prompt-columns));
- the sha256 of `frames.parquet`;
- a per-episode table, which includes `rollout_source_demo` when the dataset has it.

## Prompt columns

Format revision: the manifest `schema` stays `levi.training_manifest.v1`. The six columns below are appended after `recap_positive` and `manifest.json` gains a `prompt` section; nothing that was there changes. A manifest exported before this has neither, and a reader that does not know them ignores them. `levi_manifest_reader.py` reports `has_prompt_columns`.

A trainer that wants the post-hoc relabelling input (the task text plus the subtask a frame is in) reads, per frame:

| Column | Meaning |
| --- | --- |
| `prompt_task` | the episode's task text alone: the first entry of its tasks (as before), or the task of its `task_index`. Null when the episode has no task |
| `prompt_subtask` | the task and the current subtask, joined by the template (default `"{task}; current subtask: {subtask}"`). **When the frame has no usable subtask this is the same text as `prompt_task`**, so a loader can read the column without a null check |
| `prompt_has_subtask` | true when `prompt_subtask` really carries a subtask |
| `prompt_subtask_skip` | why it does not (null when it does): `no_task`, `no_segment` (no time segment covers the frame), `unreviewed` (the segment is `auto`), `review_unrecognised` (a `subtask_review` value this LEVI does not know), `special` (`other`, `unknown` or `background`), `empty_text` (nothing left after cleaning) |
| `prompt_subtask_source` | the time segment the subtask came from and who stands behind it, e.g. `segment 0.500-end review=edited` (`human` when `subtask_review` is empty); null without a subtask |
| `episode_task_count` | how many task texts the episode lists. Only the first is used; a count above 1 is there for tracing |

The RECAP suffix `Advantage: positive` is not part of these columns. The reader appends it last, to either version.

**Only reviewed time segments.** The subtask comes from the time segment (the `subtask` annotation, its text) that covers the frame, and only when a person stands behind it: `subtask_review` empty (a person wrote it, or approved an agent's) or `edited` (an automatic segment a person changed). A segment the live service wrote and nobody reviewed (`auto`), and any other value, is never used: the frame keeps the task-only text and `manifest.json` counts it (`prompt.subtask_skipped`, `prompt.frames_rejected_unreviewed`, and `prompt.included_frames.rejected_unreviewed` for the frames that enter the loss). There is no option to switch this off.

**Template and language.** The template is versioned (`--prompt-template`, default `v1`): the version and its text are in `manifest.json` `prompt` and in the parquet column metadata (`levi.prompt_template`, `levi.prompt_template_text`); a version's wording never changes, a new wording is a new version. `v1` is English, `"{task}; current subtask: {subtask}"`, although the first draft of the format used Chinese words. The task texts of these datasets are English, and every prompt the policy was fine-tuned on is English; Chinese words inside an English prompt are a kind of input it has never seen in training and could hurt it, so the words LEVI adds follow the task text. Confirm this choice before training with it.

**Settings.** `--prompt-template` and `--subtask-max-chars` exist on the command line (and as `build()` arguments); `POST /api/levi/manifest` uses the defaults.

**Subtask text.** It is the segment's own text, trimmed of surrounding whitespace and edge punctuation, with inner whitespace runs collapsed to one space; capitalisation and wording stay as written. A text longer than `--subtask-max-chars` (default 120) is cut there; `prompt.truncated_frames` counts the frames affected.

**Mixing the two versions in training.** Both prompts are on every frame so the training team can choose the proportion (start with 1:1). With the reader:

```python
prompt = m.prompt(task, ep, frame, rng, mode="mix", mix_ratio=0.5, seed=epoch_seed)
```

`mode` is `"task"` (the default, the caller's `task`, exactly as before), `"subtask"` or `"mix"`. `mix_ratio` is the share of subtask prompts (0.5 is 1:1). The draw is a fixed function of (episode, frame, `seed`): a frame keeps its version in every epoch unless the caller passes another seed. A frame without a subtask always gets the task text, so over a whole dataset the share of subtask prompts is below `mix_ratio` by the share of frames without a segment. `"subtask"` and `"mix"` on a manifest without the columns raise `ValueError` rather than train on the task alone. In subtask versions the task wording is the manifest's `prompt_task`, not the caller's.

**Wiring into openpi** (nothing here changes openpi). `PromptFromLeRobotTask` looks the prompt up by each frame's `task_index`, so there are two ways to use these columns: (1) change the prompt source: add a transform, after the repack, that sets `data["prompt"] = m.prompt(task, episode_index, frame_index, rng, mode="mix", seed=…)` from the manifest, keeping `task_index` for everything else; (2) write a per-frame `task_index` at export time: give every distinct prompt text its own `task_index` in `meta/tasks.jsonl` and write the chosen one into each frame's `task_index` column. (2) fixes the mix at export time and needs a new dataset; (1) leaves the dataset as is and can vary the mix per run.

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
- `frames.parquet` 另有逐帧 prompt 列（`prompt_task` 只有任务；`prompt_subtask` 任务加当前子任务，没有可用子任务时与前者相同；`prompt_has_subtask`、`prompt_subtask_skip`、`prompt_subtask_source`、`episode_task_count`）。子任务只来自**人审过的时间片段**（`subtask_review` 为空或 `edited`）；实时标注服务写的 `auto` 段和任何未识别的值一律不用，`manifest.json` 的 `prompt` 记录拒绝了多少帧。模板版本化（`--prompt-template`，默认 `v1`，`"{task}; current subtask: {subtask}"`）；**模板用英文**：任务文本是英文，英文提示里夹中文词对模型是训练中没见过的输入、可能有害，训练前请用户确认。子任务文本去首尾空白和边缘标点，超过 `--subtask-max-chars`（默认 120）截断并计数。读取器 `m.prompt(..., mode="task"|"subtask"|"mix", mix_ratio=0.5, seed=…)`：`mix` 对（片段、帧、seed）确定，同一帧每个 epoch 取同一版本；`Advantage: positive` 后缀始终在最后；没有这些列的旧清单读取行为不变。这些列是向后兼容的附加列，清单 `schema` 仍是 `levi.training_manifest.v1`。openpi 侧两种接入方式：改 prompt 来源，或导出时写逐帧 `task_index`（详见英文部分）。
- `manifest.json`：记录 LEVI 提交、数据集内容指纹、命名空间、标注修订、锚定复核的运行与规格、RECAP 的检查点、修订与阈值，以及操作名与参数。

**训练端读取**：用单文件读取器 `integrations/training_manifest/levi_manifest_reader.py`，提供片段过滤、加权采样、逐动作步掩码、RECAP 条件提示。训练后用 `audit` 核对：被排除的帧从未被抽到，且各片段的抽样比例符合权重。

**Candidate anchored reviews.** The newest anchored review is used for the verdict column only when its spec is not a `candidate`. A candidate (not validated; the live service's generic release review is one) is passed over and listed in `manifest.json` under `anchored.skipped_candidate_runs`; name the run with `--anchored-run`, or pass `--allow-candidate-anchored` (API `allow_candidate_anchored`), to use it on purpose. The manifest then records `anchored.spec_status`.
