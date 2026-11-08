# Training manifests

A training manifest tells a fixed training recipe which frames of a dataset go into its loss and with what weight. It also records the evidence behind each choice. LEVI never rewrites the dataset: the trainer loads the same LeRobot dataset as before and reads the manifest next to it. Operations are task-generic. They read episode outcomes and frame labels, never the words a particular task uses.

```bash
levi export operations                                    # built-in operations and their parameters
levi export manifest local/<name> --operation verified_success [--param fallback=exclude] [--param undecided=include] \
    [--task "stack the plates of same color together"] [--episodes 0-49] \
    [--anchored-run <run id>] [--allow-candidate-anchored] [--anchored-task "<task it is valid for>"] \
    [--recap-revision <id>] [--allow-stale] [--prompt-template v1-en] [--subtask-max-chars 80] \
    [--tempo none|per_event_uniform] [--tempo-param frames=8] [--tempo-param segments=reviewed] [--tempo-param count=segment] [--tempo-param outside=keep] \
    [--output <new dir in the workspace>] [--json]
levi export tempos                                        # tempo steps (--tempo) and their parameters
levi export list local/<name>                             # manifests written for a dataset
levi export diff <manifest dir A> <manifest dir B>        # how different two training inputs are
```

The API is `GET /api/levi/manifest/operations`, `GET /api/levi/manifest?repo_id=…` and `POST /api/levi/manifest` ([API](API.md)). A manifest can be made for a registered LeRobot v2.x dataset or for a raw capture's browsing view. It is written to `outputs/LEVI/exports/<name>/training_manifests/<timestamp>-<operation>/`, or `<timestamp>-<operation>-<tempo>` when a [tempo step](#tempo-step) ran, so that a name pattern for an operation never picks up a manifest whose weights a tempo step rescaled. Each namespace keeps its own evidence and its own manifests.

## Operations

| Operation | Frames in the loss | Needs |
| --- | --- | --- |
| `all_rollouts` | every frame in scope, weight 1 (the no-curation baseline) | — |
| `robot_flag_success` | episodes the robot flagged success (`levi_outcome`, or `is_success` in `meta/episodes.jsonl`) | — |
| `verified_success` | episodes whose verdict is success: a human label first, then the anchored review (only for the tasks it is declared valid for), then the `fallback` (`robot_flag`, the default, or `exclude`). An anchored success the review left undecided is out by default; `undecided=include` keeps it | an anchored review run, or human outcome labels in scope (a task without anchored-review rules is verified by people alone) |
| `advantage_positive_mask` | frames the RECAP value model labels positive; `unlabelled` frames are excluded (default) or included | a RECAP revision |
| `advantage_weighted` | every frame, weight `positive_weight` (1.0) or `negative_weight` (0.2); frames without a label get `unlabelled_weight` (0.0) | a RECAP revision |

`--task` and `--episodes` narrow the scope for every operation. Frames outside the scope stay in the file with `include = false` and the reason `out_of_scope`, so one file always covers the whole dataset. `--anchored-task` names the tasks the anchored review's spec has been validated for. Episodes of any other task keep the robot's flag even when the review did cover them. RECAP labels that are stale (the capture or an outcome label changed after they were computed) are refused unless you pass `--allow-stale`. The manifest then lists the reasons.

## Tempo step

`--tempo` is an optional step that runs **after** the operation and may rescale `weight` inside the dataset's subtask segments. It is not a sixth operation: operations are mutually exclusive and decide which frames and episodes enter the loss, while a tempo step decides how much each retained frame counts against the rest of its segment, so it combines with any operation. It only multiplies `weight`. It never changes `include`, `exclude_reason` or any other column.

```bash
levi export tempos
levi export manifest local/<name> --operation all_rollouts --tempo per_event_uniform --tempo-param frames=8
levi export diff <manifest dir without --tempo> <manifest dir with it>
```

| Step | Effect |
| --- | --- |
| `none` (default) | no tempo step. The manifest is byte for byte what it was before the step existed: no `tempo` key, no new column or per-episode field |
| `per_event_uniform` | every subtask segment (an *event*) is rescaled so that its weights add up to `frames` (times the share of its frames the operation kept), however long it is |

`per_event_uniform` parameters (`--tempo-param key=value`, repeatable):

| Parameter | Default | Meaning |
| --- | --- | --- |
| `frames` | 8 | N, the total weight each event gets. A whole number from 1 to 100000. 8 is our own placeholder for a first comparison, not a tuned value and not the paper's: the spec this was written from does not record the paper's N |
| `segments` | `reviewed` | the review policy that says which segments are events: `reviewed` (review empty or `edited`, the policy the [prompt columns](#prompt-columns) use; a segment the live service wrote and nobody reviewed is not an event) or `all`. Its name and version are in the fingerprint |
| `count` | `segment` | what T is in `frames` / T: `segment` (every frame the segment covers, kept by the operation or not) or `included` (only the frames the operation kept, the first version of this step; see below) |
| `outside` | `keep` | what happens to included frames in no event: `keep` (their weight stays as the operation gave it) or `scale` (multiplied by the mean factor the events received, so the share of the weight in events is what it was before the step; see [Frames outside events](#frames-outside-events)) |

**What it approximates, and what it does not.** VPP2 (arXiv 2610.10270) trains event by event: from each event it draws N frames evenly, y = (o_0, o_⌊T/N⌋, …, o_T), whatever the event's length T, so long events are thinned and short ones are kept dense. A manifest lists only `include` and `weight` per frame; it cannot say "draw N frames out of this event and train on those". `per_event_uniform` is therefore a **weight approximation**: it gives each event the same total contribution to the loss, independent of its length. That is the property the draw has in expectation. It is not the paper's method and results are not to be described as reproducing it: every included frame stays, with its whole action chunk (nothing is drawn, so nothing is repeated or skipped), and the trainer still decides what to sample.

**The rule.** For each event let T be the number of frames the segment covers (`count=segment`, the default): the frames it owns, whether or not the operation kept them. The weight of each **included** frame is multiplied by `frames` / T. A frame the operation excluded stays at weight 0 and counts in T as weight 0. Where the operation gave the kept frames weight 1 (every operation except `advantage_weighted`), the event's weights add up to `frames` times the share of its frames the operation kept (up to float32 rounding): exactly `frames` for an event kept whole. With `advantage_weighted` the ratio between its positive and negative frames survives, and the event adds up to `frames` times the mean weight the operation gave over all of its frames, excluded ones counting 0. A frame in no event keeps its weight (with `outside=scale`, see [Frames outside events](#frames-outside-events), it is rescaled by one factor for the whole dataset).

**Why T counts the excluded frames.** The first version divided by the kept frames only; that is still available as `count=included`. It made an event's total independent of what the operation kept. With `advantage_positive_mask` the one positive frame left in a 40-frame event received the whole `frames` (weight 8, against 0.2 for each frame of an event kept whole). With `advantage_weighted` a `negative_weight` going from 0.001 to 0 moved the event's total from about `frames` times its positive share up to `frames`, because the negative frames then left T: a jump for an infinitesimal change. Any later filter that excludes frames inside an event would have the same effect on the frames it leaves. With `count=segment` every kept frame weighs `frames` divided by the frames the segment covers, whatever the operation kept, and an event's total follows the operation's cutoff continuously. Use `count=included` to compare with the first version.

**Why weight only.** The reader's `action_mask` counts action target k of a chunk only when frame t+k is included. Thinning a segment with `include = false` would therefore mask most targets of the frames that stay, and delete their action supervision, while any test that checks only the kept frames' own actions would not notice. A weight of `frames` / T keeps every chunk whole and has the same expectation as drawing one frame in T / `frames`. `action_mask`, `episodes()` and `sampling_weights` need no change; the reader is untouched.

**Which segments are events.** The subtask segments the manifest already lists (`subtask_id`), one event per segment, not per id: the same id twice is two events. A frame covered by two segments belongs to the one that starts last, the same one its `subtask_id` column names, so no frame is scaled twice.

`segments` names a **review policy** (`manifest_prompts.REVIEW_POLICIES`): one predicate on a segment's `subtask_review`, with a name and a version. `reviewed` (version 1, the default) accepts review empty or `edited`; it is the same predicate the [prompt columns](#prompt-columns) apply, so the two cannot disagree about what is reviewed. `all` accepts every segment. The policy's name and version are in `tempo.segment_policy` and in the fingerprint, so a new version of what `reviewed` means is a different input even when every parameter is the same. A policy that accepts a further kind of review is added there and reaches the prompt columns and the tempo step together.

**An unreviewed segment is not an event, but it is not invisible.** Frames are first assigned to segments (the later-starting one owns the shared frames, whatever its review), and only then is each segment judged. An `auto` or unrecognised segment is therefore never scaled and its frames count as outside every event, but when it starts after a reviewed event starts and overlaps it, it takes the shared frames: they go outside, and the reviewed event's T counts only the frames it still owns. Example: a reviewed event covering 8 frames with an automatic segment starting at its fifth frame has T = 5, not 8. The step never lets an unreviewed segment become an event; it can change the weights of a reviewed one by taking its frames.

| Case | Behaviour |
| --- | --- |
| no kept frame (the operation excluded the event's frames, the segment covers no frame, or a later overlapping segment owns them) | nothing to scale: left alone, never divided by; counted in `events_empty` |
| 0 < T < `frames` | weights go **up**, by `frames` / T, so that a whole event still adds up to `frames`; a one-frame event gets the whole `frames`. Counted in `events_upweighted`; check `max_factor` before training, since a very short event then weighs as much as a long one |
| the operation excluded some of the event's frames | the kept frames get `frames` / T with T counting the excluded ones, so the event adds up to less than `frames`; counted in `events_partly_excluded`, with the frames in `frames_excluded_in_events` (counted under either `count`; `count=included` divides by the kept frames, so such an event adds up to `frames` anyway) |
| T = `frames` | factor 1, unchanged (`events_unchanged`) |
| T > `frames` | weights go down (`events_downweighted`) |
| no segment in the episode | no weight changes (with `outside=keep`); the frames count in `frames_outside_events` |
| overlapping segments | the later-starting one owns the shared frames (above) |
| non-finite or negative weight | cannot arise: the factor is positive and finite, and a non-finite weight from the operation is refused with an error |

### Frames outside events

An included frame in no event keeps the weight the operation gave it (`outside=keep`, the default), while the frames of an event weigh `frames` / T each, which is below 1 whenever the segment covers more than `frames` frames. A frame outside is then drawn more often than one inside. At 10 fps a reviewed 3-second segment with `frames=8` weighs 8/30 = 0.27 per frame, and a frame of an unreviewed automatic segment, which with `segments=reviewed` is not an event and so is outside, weighs 1: 3.75 times as much, and a weighted sampler such as the reader's draws it that much more often. Labelling a whole episode does not remove this when its segments are automatic: only reviewed or edited ones are events.

`manifest.json` records the gap in `tempo.events`: the weight inside and outside events before and after the step (`weight_in_events_before`, `weight_in_events_after`, `weight_outside_events_before`, `weight_outside_events_after`), the share of the weight inside events before and after (`in_event_share_before`, `in_event_share_after`), the mean weight per frame inside and outside (`mean_weight_in_events`, `mean_weight_outside_events`) and their ratio `outside_to_inside_weight_ratio`. When that ratio is above 2 or below 1/2 (`imbalance_warn_ratio`), `tempo.warnings` holds an `outside_weight_imbalance` warning with the numbers; `levi export manifest` prints it with the rest of `tempo`.

What to do about it, in the order of how little each assumes:

- review or edit the automatic segments (they then become events);
- `--tempo-param outside=scale`: every included frame in no event is multiplied by the mean factor the events received, weight after over weight before over the whole dataset (`outside_factor`), so the share of the weight in events is the one before the step and an outside frame weighs on average about what an event frame does. The factor is one number for the dataset, so an episode with no event at all is scaled like the others. Without any event it is 1;
- `segments=all` makes every segment an event, unreviewed ones included, which lets segments nobody reviewed steer the weights;
- compare with `levi export diff` against the manifest without the step.

**Recorded.** `manifest.json` gets a `tempo` object (absent for `none`): `name`, `summary`, `version`, `params`, a `segment_policy` (the review policy's name and version), a `fingerprint` (sha256 over name, version, the resolved parameters and the review policy, so a different `frames`, `segments`, `count` or `outside` is a different fingerprint, and a change to what the step does bumps `version`), and `events`: segments read, events, events scaled/empty/up-/unchanged/down-weighted/partly excluded, frames inside, excluded inside and outside events, episodes with events, the smallest and largest factor, and how the weight divides between the frames inside and outside events before and after the step (see [Frames outside events](#frames-outside-events)); `warnings` lists what to look at before training. Each per-episode entry gets `tempo_events_scaled`. The dataset's own `fingerprint` is unchanged by the step, so manifests with and without it stay comparable. `levi export diff` prints each side's tempo and `tempo_differs`, true when the fingerprints differ **even if the weights happen to agree** (no annotated segment, say), and `levi export list` shows the step of the manifests that have one. The HTTP API (`POST /api/levi/manifest`) does not take `tempo` yet, and it refuses a field it does not take (422) instead of ignoring it, so a request asking for a tempo step cannot silently get a manifest without one; use the command line or `build(..., tempo=..., tempo_params=...)`.

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
- the tempo step when one ran (`tempo`: name, version, parameters, `fingerprint`, what it found; see [Tempo step](#tempo-step));
- the dataset: catalog name, namespace and base, root, fps, and a content `fingerprint` (sha256 of the metadata files and of every episode's data parquet, the capture's `source_fingerprint`);
- the annotation revision and digest;
- the anchored review run: spec id, version and sha256, and the provider and model;
- the RECAP revision: checkpoint and its sha256, threshold and where it came from, lookahead, static filter, stale reasons;
- counts: included episodes by verdict source, frames and weight mass;
- `prompt`: the prompt template (version and text), the subtask length limit, and how many frames got a subtask, were refused and were cut (see [Prompt columns](#prompt-columns));
- the sha256 of `frames.parquet`;
- a per-episode table, which includes `rollout_source_demo` when the dataset has it.

## Prompt columns

Format revision: the manifest `schema` stays `levi.training_manifest.v1`. The seven columns below are appended after `recap_positive` and `manifest.json` gains a `prompt` section; nothing that was there changes. A manifest exported before this has neither, and a reader that does not know them ignores them. `levi_manifest_reader.py` reports `has_prompt_columns`.

A trainer that wants the post-hoc relabelling input (the task text plus the subtask a frame is in) reads, per frame:

| Column | Meaning |
| --- | --- |
| `prompt_task` | the episode's task text alone: the first entry of its tasks (as before), or the task of its `task_index`. Null when the episode has no task |
| `prompt_subtask` | the task and the current subtask, joined by the template (default `"{task}; current subtask: {subtask}"`). **When the frame has no usable subtask this is the same text as `prompt_task`**, so a loader can read the column without a null check |
| `prompt_has_subtask` | true when `prompt_subtask` really carries a subtask |
| `prompt_subtask_skip` | why it does not (null when it does): `no_task`, `no_segment` (no time segment covers the frame), `unreviewed` (the segment is `auto`), `review_unrecognised` (a `subtask_review` value this LEVI does not know), `special` (`other`, `unknown` or `background`, also with punctuation or other case: `Unknown.`), `reserved_word` (the cleaned text contains `advantage:`, `task:`, `state:` or `action:`, in any case: it would pass for the prompt's own structure or the RECAP tag), `empty_text` (nothing left after cleaning) |
| `prompt_subtask_source` | the time segment the subtask came from and who stands behind it, e.g. `segment 0.500-end review=edited origin=human` or `segment 0.000-0.300 review=human origin=agent run=<run id>` (`human` when `subtask_review` is empty); null without a subtask |
| `prompt_subtask_origin` | `human`, `agent_run` or `edited`, see below; null without a subtask. This is the column to filter on |
| `episode_task_count` | how many task texts the episode lists. Only the first is used; a count above 1 is there for tracing |

The RECAP suffix `Advantage: positive` is not part of these columns. The reader appends it last, to either version.

**Reviewed time segments, and what that does not tell.** The subtask comes from the time segment (the `subtask` annotation, its text) that covers the frame, and only when `subtask_review` is empty or `edited` (an automatic segment a person changed). A segment the live service wrote and nobody reviewed (`auto`), and any other value, is never used: the frame keeps the task-only text and `manifest.json` counts it (`prompt.subtask_skipped`, `prompt.frames_rejected_unreviewed`, and `prompt.included_frames.rejected_unreviewed` for the frames that enter the loss). There is no option to switch this off.

**An empty review is not proof that a person looked.** Only the live service marks its segments `auto`. `subtask_review` empty therefore also covers a segment an agent run proposed and a script approved with a person's identity (the evaluation drivers do this). The manifest cannot tell the two apart, so it records the origin instead: `prompt_subtask_origin` is `human` (no agent origin), `agent_run` (an agent run's segment with an empty review mark; the run id is in `prompt_subtask_source`) or `edited`. `manifest.json` `prompt` counts the frames per origin (`frames_with_subtask_by_origin`, `frames_with_subtask_from_agent_runs`, `frames_with_subtask_by_agent_run`). Whether to use only `human` and `edited` segments is for the training team to decide by filtering on that column; the reader does it with `subtask_origins=("human", "edited")` (default: all origins).

**Template and language.** The template is versioned (`--prompt-template`, default `v1-en`): the version and its text are in `manifest.json` `prompt` and in the parquet column metadata (`levi.prompt_template`, `levi.prompt_template_text`); a version's wording never changes, a new wording is a new version. `v1-en` is English, `"{task}; current subtask: {subtask}"`. The task texts of these datasets are English, and every prompt the policy was fine-tuned on is English; Chinese words inside an English prompt are a kind of input it has never seen in training and could hurt it, so the words LEVI adds follow the task text. The language is still to be confirmed before a training run uses it; the format's first draft (called draft v1 in the project plan) was Chinese, and `v1-en` is its English wording. A Chinese wording would be a new version, never a change to `v1-en`.

**Settings.** `--prompt-template` and `--subtask-max-chars` exist on the command line (and as `build()` arguments); `POST /api/levi/manifest` takes neither and uses the defaults (a request that names one is refused with 422, not ignored).

**Subtask text.** It is the segment's own text, trimmed of surrounding whitespace and edge punctuation, with inner whitespace runs collapsed to one space; capitalisation and wording stay as written. Control and format characters (zero-width, bidi marks, escapes) are removed. A text longer than `--subtask-max-chars` (default 80) is cut there; `prompt.truncated_frames` counts the frames affected.

**Length is counted in characters, openpi counts tokens.** openpi cuts a prompt at `max_token_len` tokens from the end: 200 for pi05, 48 for pi0 (`Pi0Config`). In the discrete-state format `Task: …, State: …;\nAction: ` the part that is cut off is the state and `Action: `, so a prompt that is too long damages the whole sample, not only the subtask. 80 English characters are roughly 15 to 25 tokens, which fits pi05 after the task text, the template and the RECAP suffix; for pi0 a subtask does not fit at all with a long task text, and Chinese or other non-ASCII text costs about one token per character or more. `prompt.max_prompt_chars` and `prompt.max_prompt_utf8_bytes` give the longest second prompt (before the RECAP suffix; bytes bound the token count from above in any language); check the longest few with the real tokenizer before training, and lower `--subtask-max-chars` if they come close to the budget.

**Mixing the two versions in training.** Both prompts are on every frame so the training team can choose the proportion (start with 1:1). With the reader:

```python
prompt = m.prompt(task, ep, frame, rng, mode="mix", mix_ratio=0.5, seed=0)
```

`mode` is `"task"` (the default, the caller's `task`, exactly as before), `"subtask"` or `"mix"`. `mix_ratio` is the share of subtask prompts among frames that have a usable subtask (0.5 is 1:1). The draw is a fixed function of (episode, frame, `seed`): with a constant `seed` a frame keeps its version in every epoch; passing `seed=epoch` draws the mix again each epoch. A frame without a usable subtask (no segment, `auto`, special, reserved word, empty) always gets the task text, so over a whole dataset the share of subtask prompts is `mix_ratio` times the share of frames that have one: 0.5 with 60 % of frames covered gives 30 %. `"subtask"` and `"mix"` on a manifest without the columns raise `ValueError` rather than train on the task alone. In subtask versions the task wording is the manifest's `prompt_task`, not the caller's.

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
- `frames.parquet` 另有逐帧 prompt 列（`prompt_task` 只有任务；`prompt_subtask` 任务加当前子任务，没有可用子任务时与前者相同；`prompt_has_subtask`、`prompt_subtask_skip`、`prompt_subtask_source`、`episode_task_count`）。子任务只来自**人审过的时间片段**（`subtask_review` 为空或 `edited`）；实时标注服务写的 `auto` 段和任何未识别的值一律不用，`manifest.json` 的 `prompt` 记录拒绝了多少帧。模板版本化（`--prompt-template`，默认 `v1-en`，`"{task}; current subtask: {subtask}"`）；**模板用英文**：任务文本是英文，英文提示里夹中文词对模型是训练中没见过的输入、可能有害，语言待用户确认（项目计划里的中文“草案 v1”对应这里的英文 `v1-en`，换中文要新开一个版本）。子任务文本去首尾空白和边缘标点，超过 `--subtask-max-chars`（默认 80）截断并计数；清洗还会去掉零宽和控制字符，含 `advantage:`、`task:`、`state:`、`action:`（不分大小写）的文本不用（`reserved_word`），`Unknown.` 这类带标点的特殊标签也不用。长度按字符算而 openpi 按 token 截断（pi05 为 200，pi0 为 48，超长从末尾截，会切掉 `State` 和 `Action:`），`manifest.json` 的 `prompt` 记录最长 prompt 的字符数和 UTF-8 字节数，训练前请用真实 tokenizer 抽查。**review 为空不等于有人看过**：脚本用人工身份批准的 agent 片段同样为空；清单用 `prompt_subtask_origin`（`human`、`agent_run`、`edited`）和按来源的帧数记录，是否只用人工和 edited 片段由训练团队按该列过滤（读取器参数 `subtask_origins`，默认全部）。`mix_ratio` 是在有可用子任务的帧里取子任务版本的比例，整个数据集里的比例是它乘以有子任务的帧占比。读取器 `m.prompt(..., mode="task"|"subtask"|"mix", mix_ratio=0.5, seed=…)`：`mix` 对（片段、帧、seed）确定，同一帧每个 epoch 取同一版本；`Advantage: positive` 后缀始终在最后；没有这些列的旧清单读取行为不变。这些列是向后兼容的附加列，清单 `schema` 仍是 `levi.training_manifest.v1`。openpi 侧两种接入方式：改 prompt 来源，或导出时写逐帧 `task_index`（详见英文部分）。
- **节奏步骤**（`--tempo none|per_event_uniform`，参数用 `--tempo-param frames=8`、`--tempo-param segments=reviewed`、`--tempo-param count=segment`、`--tempo-param outside=keep`，`levi export tempos` 列出）：在操作之后再跑的可选一步，只改 `weight`，**不改 `include`** 和任何其他列，所以可以和任何操作搭配；`none`（默认）时清单与加这一步之前逐字节相同（没有 `tempo` 键）。
  - `per_event_uniform` 是 VPP2 “每个事件均匀取 N 帧”在清单侧的**权重近似**，不是论文的方法：清单只能逐帧给 `include` 和 `weight`，说不出“从事件里抽 N 帧再训练”，所以改为让每个事件对 loss 的总贡献相同、与长度无关；不能说成复现了论文。每个保留的帧及其完整动作块都还在，不抽、不重复。
  - 规则：事件 = 清单里已有的子任务片段（每个片段一个事件，同一个 id 出现两次是两个事件；两个片段重叠的帧归后开始的那个，与 `subtask_id` 列一致）。`segments` 取的是**审阅策略**（`manifest_prompts.REVIEW_POLICIES`）：对 `subtask_review` 的一个有名字、有版本的判定；`reviewed`（版本 1，默认）接受 review 为空或 `edited`，与 prompt 列用的是同一个判定，两边不会对“什么算审过”各执一词；`all` 接受全部。策略名和版本进 `tempo.segment_policy` 和指纹。未审的 `auto` 或未识别片段不是事件，但并非不起作用：先把帧分给片段（后开始的重叠片段拥有共有帧，不论其审阅状态），再判断各片段是否事件，所以后开始并与已审事件重叠的 `auto` 段会占走共有帧，这些帧算事件外，已审事件的 T 只数它还拥有的帧（例：覆盖 8 帧的已审事件，第 5 帧处起一个自动段，T=5 而不是 8）。步骤不会让未审片段成为事件，但它可以占走已审事件的帧而改变其权重。T = 该片段覆盖的帧数（`count=segment`，默认；不论操作是否保留这些帧），每个 `include=true` 的帧的 `weight` 乘以 `frames`/T；操作排除的帧仍为 0，在 T 里按权重 0 计入。操作给保留帧的权重是 1 时（除 `advantage_weighted` 外的操作），事件内权重之和等于 `frames` 乘以操作保留的帧占比（整段都保留时正好是 `frames`）；`advantage_weighted` 时正负帧的权重比例保留，事件之和等于 `frames` 乘以操作在片段全部帧上给的平均权重（排除的帧按 0）。不在任何事件里的帧权重不变（`outside=scale` 除外，见下）。
  - 为什么 T 要算上被排除的帧：第一版只除以保留的帧数（现在是 `count=included`，留作对照），事件总量因此与操作保留了多少无关：`advantage_positive_mask` 下 40 帧的事件只剩 1 个正帧时，这一帧独得整个 `frames`（权重 8，而整段保留的事件里每帧只有 0.2）；`advantage_weighted` 的 `negative_weight` 从 0.001 改成 0，负帧离开 T，事件总量从约 `frames` 乘正帧占比跳到 `frames`，参数变化无穷小而结果跳变；以后任何在事件内排除帧的过滤也会放大剩下的帧。默认的 `count=segment` 下，每个保留帧的权重是 `frames` 除以片段覆盖的帧数，与操作保留了什么无关，事件总量随操作的取舍连续变化。
  - 为什么只改权重：读取器的 `action_mask` 规定动作块第 k 个目标只有在 frame+k 被 included 时才计入损失；把帧标成 `include=false` 来抽帧会遮掉留下来的帧的大部分动作目标，等于删掉动作监督，而只检查保留帧自身动作的测试发现不了。
  - 参数：`frames`（N，整数 1 到 100000，默认 8；8 是我们自己定的起点，不是调出来的，也不是论文的值，参考的规格里没有记论文的 N）；`segments`（审阅策略名：`reviewed` 默认，只取 review 为空或 `edited` 的片段，与 prompt 列的策略一致，实时服务写的 `auto` 片段不是事件；`all` 取全部片段）；`count`（`segment` 默认：T 取片段覆盖的全部帧；`included`：T 只取操作保留的帧，第一版的做法）；`outside`（`keep` 默认：不在任何事件里的保留帧权重不变；`scale`：把这些帧乘以各事件得到的平均因子，即全数据集的事件内权重之和（步骤之后）除以（步骤之前），使事件内权重占总权重的份额与这一步之前相同）。
  - 边界：事件没有保留的帧（操作把事件的帧都排除了、片段没有覆盖任何帧、或帧被后开始的重叠片段占了）不处理也不除零，计入 `events_empty`；T<`frames`（含只有 1 帧的片段）权重会**上调**到整段事件之和仍为 `frames`，计入 `events_upweighted`，训练前看 `max_factor`；操作排除了事件的一部分帧时，事件之和小于 `frames`，计入 `events_partly_excluded`（排除的帧数见 `frames_excluded_in_events`）；T=`frames` 不变；T>`frames` 下调；没有片段的帧权重不变。权重不会是 NaN 或负数，操作给出非有限权重时直接报错。不在事件里的帧权重不变，而事件内每帧权重是 `frames`/T，片段超过 `frames` 帧时小于 1，所以事件外的帧被抽到的频率更高（10 fps 下已审的 3 秒片段每帧 8/30≈0.27，未审的 `auto` 片段在 `segments=reviewed` 下不是事件，每帧仍是 1，高 3.75 倍）；标整条片段并不能消除这一点，除非它们被审过或编辑过。`manifest.json` 的 `tempo.events` 记录事件内外的权重（步骤前后）、事件内权重占比、每帧平均权重和比值 `outside_to_inside_weight_ratio`，比值大于 2 或小于 1/2 时 `tempo.warnings` 给出 `outside_weight_imbalance` 警告。处理办法：审阅或编辑自动片段（它们就成了事件）；`outside=scale`；`segments=all`（未审片段也会参与定权重，慎用）；用 `levi export diff` 与不加这一步的清单对比。
  - 记录：`manifest.json` 的 `tempo`（`none` 时没有）含名称、版本、参数、指纹（名称、版本和解析后参数的 sha256，参数不同指纹就不同）和统计；每个片段条目有 `tempo_events_scaled`；`levi export diff` 输出两边的 `tempo` 和 `tempo_differs`（指纹不同即为真，即使两份权重恰好相同）。数据集自身的指纹不受影响。清单目录名在加了节奏步骤时是 `<时间>-<操作>-<步骤>`（`none` 仍是 `<时间>-<操作>`），按操作名取“最新清单”不会拿到权重被改过的那份。HTTP 接口暂不支持 `tempo`，并且对不认识的字段返回 422 而不是忽略（`tempo`、`prompt_template` 之类不会被静默丢掉后返回一份没有它们的清单），用命令行或 `build(..., tempo=..., tempo_params=...)`。
- `manifest.json`：记录 LEVI 提交、数据集内容指纹、命名空间、标注修订、锚定复核的运行与规格、RECAP 的检查点、修订与阈值，以及操作名与参数。

**训练端读取**：用单文件读取器 `integrations/training_manifest/levi_manifest_reader.py`，提供片段过滤、加权采样、逐动作步掩码、RECAP 条件提示。训练后用 `audit` 核对：被排除的帧从未被抽到，且各片段的抽样比例符合权重。

**Candidate anchored reviews.** The newest anchored review is used for the verdict column only when its spec is not a `candidate`. A candidate (not validated; the live service's generic release review is one) is passed over and listed in `manifest.json` under `anchored.skipped_candidate_runs`; name the run with `--anchored-run`, or pass `--allow-candidate-anchored` (API `allow_candidate_anchored`), to use it on purpose. The manifest then records `anchored.spec_status`.
