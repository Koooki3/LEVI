# Event intelligence: signal facts, profiles and candidates

[中文](EVENTS.zh-CN.md)

`levi.events` reads what a robot's recorded signals say happened, and when. **Only a temporal plan that turns it on uses it** ([below](#in-a-plan-event-intelligence)): there its candidates choose extra frames for the boundary refinement. The opt-in online stream described below is a separate adapter; no runtime service mounts it by default. For the recorded-data path, a plan without it, the interface and the CLI behave as before, as do the signal lines an agent reads (`levi.agent.signals`) and anchored reviews (`levi.agent.anchored`). The recorded-data readers and causal kernel are pure computations over arrays and records: no model, no Torch, no files written. The optional adapter retains encoded evidence only through an explicit evidence-store callback.

| Module | What it holds |
| --- | --- |
| `gripper` | Open/close crossings of one gripper channel with hysteresis; the kernel behind the signal lines and anchored reviews. |
| `motion` | Speed and stillness on the recorded timestamps. |
| `contracts` | `SignalObservation` and `EventCandidate`, the records the readers produce. |
| `stream` | Causal candidates of a bounded observed prefix, with an explicit declaration and fixed thresholds. |
| `signal_profiles` | What each recorded channel means: role, actor, units, frame, which end of a gripper is open. |
| `facts` | The signal facts of one episode as `SignalObservation`s, per channel and actor. |
| `change_points` | Penalised change points of each actor's speed, gripper level and rotation speed, as `EventCandidate`s. |
| `calibrate` | The script that chose the change-point penalty on development gold labels. |
| `third_party.json`, `third_party` | The structured record of outside sources this package uses, and the rules it keeps. |
| `boundary_metrics` | Boundary Recall at several tolerances, false candidates per minute, boundary MAE/P90, Segment F1 at several IoU thresholds. |
| `candidates` | Every source's candidates of one episode, merged and in evidence priority. |
| `clips` | Clips of 1 to 12 s cut at gripper moves and end-effector speed minima, with reason codes (VPP2 step P1; model-free, offline). |
| `sampling` | The evidence planner: a refinement's frames shared among windows, greedily, in tiers. |
| `ablation` | The equal-budget comparison of runs with and without event candidates (scaffolding; no model call). |

## Records

Both records are rebuildable from the episode's table and never belong in an annotation bundle. Each keeps the table row (`source_frame_index`) next to its time in seconds (`time_s` / `center_time_s` and a `time_window_s`), so nothing downstream converts between them with `frame / fps`, which is wrong on a table with dropped frames. The JSON field `schema_version` names the schema (`levi.signal_observation.v1`, `levi.event_candidate.v1`), as in LEVI's other contracts.

- `SignalObservation`: one fact (`gripper_open`, `gripper_close`, `height_low`, `height_high`, `still`, `change_point`, ...) with its actor, its sources (feature, dimension, index, `measured` / `commanded` / `derived`, units, frame), an optional value and an optional `source_sha256` of the table it was read from. A point fact's window runs from the previous finite timestamp to its own: the change happened between the two samples.
- `EventCandidate`: a place where something may have changed. `salience` (0 to 1) orders candidates for evidence gathering; **it is not a probability** and must not be shown as one. `boundary_probability` stays `null` until a calibrated estimate exists. `status` is `proposed`, `merged` or `rejected`.

## Signal profiles

A profile (`levi.signal_profile.v1`) lists channels with a `role` (`gripper`, `position`, `rotation`, or `ignore` in a declaration), the `feature` and `index` (and `dimension` name), the `actor_id`, `kind` (`commanded` for `action`, else `measured`), `units`, `frame`, `axis`, a gripper's `open_level` (`high` or `low`) and an optional `valid_range`.

- `infer(info)` applies the signal lines' rules to `meta/info.json`: a gripper is a float vector dimension whose name matches `grip|finger|claw|jaw`; a position is one named `x`/`y`/`z` (the first of each axis in a column); a rotation `rx`/`ry`/`rz` or `roll`/`pitch`/`yaw`. A dimension naming `left`, `right`, `arm_N` or `robot_N` belongs to that actor; the rest to `arm_0`. **Nothing in a name says which end of a gripper is open**, so an inferred `open_level` is always empty and readers fall back to the level the episode starts at, which reads an episode that starts with a closed gripper upside down.
- A declaration fixes that, and **a declaration always wins**. `resolve(info, declared)` lays it over the inferred profile: a declared channel replaces the inferred one at the same (feature, index) or adds one, and `role: "ignore"` removes one the names wrongly suggested. An inferred channel that stands for the same thing in the same feature at another index (same actor and axis for a position or rotation; same actor and kind for a gripper) is dropped, listed in the profile's `overridden` and reported with a `SignalProfileWarning` (a warning, not an error: replacing the inference is what a declaration is for). Declared channels come first, so a reader that takes one channel per role takes the declared one. A declaration that names a column the dataset lacks, an index outside it, a dimension name that does not match, or one axis of one actor twice is refused, as is a column whose names do not match its shape.
- `from_action_contract(contract, feature)` turns a registered action contract (`levi.counterfactual`, [COUNTERFACTUAL.md](COUNTERFACTUAL.md)) into a declaration: dimension order, units, frame and which gripper value is open (`fr3-robotiq`: 1.0 = open, so `high`).
- A dataset may carry its own declaration in `meta/levi_signal_profile.json`; `for_dataset(root)` reads it. LEVI never writes that file.
- `open_levels(profile)` gives the declared ends in the form `levi.agent.signals.summarize(open_levels=...)` takes.

`facts.read(table, info, profile)` reads every gripper of every actor (not only the first that moved, as the signal lines do), height turns and still spans, each with its source channel; with a declared `open_level` the gripper facts are right side up whatever the episode starts with.

## Boundary and segment metrics

`boundary_metrics` judges candidates against reference boundaries (per episode, in seconds):

- `boundary_scores(reference, candidates, durations, tolerances=(0.1, 0.2))`: per tolerance, Boundary Recall (the share of reference boundaries with a candidate within the tolerance; one candidate counts for one boundary, maximum one-to-one matching), precision, F1, false candidates (matching none) and false candidates per minute of episode, and the MAE/P90 of matched errors. Across tolerances: candidates per minute and how far each reference boundary is from its nearest candidate (`nearest_mae`, `nearest_p90`; a missed boundary counts at that distance; `no_candidate` counts boundaries in episodes with no candidate at all).
- `segment_f1(reference, candidates, ious=(0.3, 0.5, 0.7))`: Segment F1 per IoU threshold with the matching rule of `levi.harness.grading` (same subtask, best IoU at or above the threshold, each candidate once); `mean` averages per-episode F1 as `grading.grade` does (at IoU 0.3 it equals grading's `segment_f1`), `pooled` pools all matches.
- `boundaries(segments)` turns contiguous segments into their inner boundaries.

`levi.agent.evaluation.temporal` (one tolerance, identity-matched rows) and `levi.harness.grading` (one IoU) are unchanged.

## Change points

`change_points.candidates(table, info, profile)` returns `EventCandidate`s of type `change_point` for every actor. Per actor it builds a multi-dimensional series from the profile -- the speed of its position (on the recorded timestamps, so a dropped frame is not a jump), its gripper level (a measured channel before a commanded one) and the speed of its rotation (unwrapped; degrees when the profile says `deg`) -- scales each feature to a robust spread (the larger of a quarter of its 1-99% range and its noise from first differences; a feature with no spread is left out; missing values are filled from their neighbours) and finds the exact penalised segmentation with a piecewise-constant mean and squared-error cost (optimal partitioning with pruning, the PELT family; Truong, Oudre and Vayatis 2020, arXiv:1801.00718, describe it and implement it in `ruptures`, BSD-2-Clause). LEVI does not depend on `ruptures`. With a minimum segment length the pruning is delayed by that length (a start that fails the pruning test at row t may still be the best start for ends before t + min_size), so the result is the exact optimum; the tests compare it with unpruned optimal partitioning on 1,000+ random series (noise, steps, random walks; minimum lengths 1 to 8). A series longer than `MAX_ROWS` is searched over blocks that keep their rows' sufficient statistics (count `W`, sum `S`, sum of squared norms `Q`); a run of blocks costs `sum(Q) - |sum(S)|^2 / sum(W)`, exactly its rows' squared error, and the penalty counts rows, so the result is the exact optimum of the unbinned problem with change points restricted to block starts and the minimum length rounded up to whole blocks (tested on random series with unequal blocks, several features and constant stretches, against unpruned partitioning over block starts).

A change point says the signals' level changed, not what happened: it is a candidate for evidence, never a subtask boundary by itself. `salience` is `gain / (gain + beta)` (the cost the change saves against the penalty): an ordering, not a probability.

Guards against over-cutting, each tested:

| Guard | Default |
| --- | --- |
| Penalty per change: `penalty * (d + 1) * log(n)` for `d` features, `n` rows (the rows of the episode, also when it is searched in blocks) | `penalty = 0.75` (calibrated, below) |
| Shortest segment (in rows, from the median sampling step) | `min_seconds = 0.5` |
| Longest series searched row by row; a longer one is searched over blocks of `ceil(n / 3000)` rows (the last takes the rows left over), each kept as its rows' count, sum and sum of squares, so the cost and the penalty are those of the rows; change points then fall on block starts and `bin_rows` says so, gains stay in row units, and the minimum length holds in rows (`min_size` is rounded up to whole blocks). Until 2026-10-15 (`change_points.VERSION` v1) a block was its mean times the square root of its length and the penalty counted blocks: with a shorter last block that turned a constant level into a change. The penalty 0.75 was calibrated before this fix; series longer than 3000 rows have not been recalibrated. The search is quadratic in the worst case; 18,000 rows of noise (30 Hz, 10 minutes) take well under 2 s (tested) | `MAX_ROWS = 3000` |
| Most change points per minute of episode; past it the penalty is raised by 1.5x until the result fits; when equally strong changes would all vanish at once, exactly the strongest that fit are kept, ties broken by time (`capped` says so) | `max_per_minute = 30` (set beforehand, not tuned) |

On pure noise scaled the same way the default penalty gives well under one change point per minute; twice the penalty gives none (tested).

### Calibration

`python -m levi.events.calibrate --root <dir> --gold <dir> ... --report-gold <dir> ... [--exclusion-list <list.json> ...]` scores candidates of raw robot captures against reference segments (the segments' inner boundaries at the capture's timestamps) for a grid of penalties. It picks the largest penalty whose F1 at 0.5 s is within 0.01 of the best (of near-equal settings, the one that cuts least), among the penalties that make at most one change point per minute on pure noise (synthetic, `noise_rate`): dense reference annotations would otherwise pull the penalty down to where noise is cut (0.3 gives 5.8 per minute on noise).

It never reads a test set, and refuses the whole run rather than skip an episode when a gold folder's path, an episode's source path (after `--map`) or the folder it resolves to contains `frozen`, `heldout` or `held-out`, or when the source is on an exclusion list (`--exclusion-list` files and the `LEVI_POOL_HELDOUT` lists, in the training pool's held-out format) by path or by the sha256 of one of its videos (videos are hashed, never decoded). It reads only pose and gripper CSVs, skips void episodes (`episode_success: "void"`), writes nothing but `--out`, and records every episode's source with the SHA-256 of the files it read, the refuse patterns, the SHA-256 of each exclusion list and the noise rate of each penalty.

Run of 2026-10-10 (penalties 0.1 to 8), with the screws and plates frozen test set lists and the generic v2 frozen test family list as exclusion lists:

```
python -m levi.events.calibrate --root <workspace> \
  --gold <gold>/screws-devgold --gold <gold>/diag15 --report-gold <gold>/generic-pilot \
  --map <plates source>=<plates copy> --exclude-source data_collection_robotiq \
  --exclusion-list <gold>/frozen/screws-frozen-v1.json \
  --exclusion-list <gold>/frozen/plates-frozen-v1.json \
  --exclusion-list <gold>/frozen/generic-frozen-v1.heldout.json \
  --penalties 0.1,0.2,0.3,0.5,0.75,1,1.5,2,3,4,6,8
```

- Calibration: screws development gold labels (12 episodes, a locked set) and the plates diagnostic set (15 episodes, read from a copy whose frame counts matched the gold labels): 27 episodes, 10.1 minutes, 36.2 reference boundaries per minute. Test sets (frozen and held-out families) were not read.
- Report (not used for the choice): generic v2 pilot gold labels, 6 of 9 episodes: one void episode (a human hand in view) and the 2 sourced from the robotiq demonstration collection (reserved until the scale-up phase) were left out. Four of the six come from the pools left over after the frozen and held-out splits (in no blind set): report only, never tune on them.

| Set | Penalty | Candidates/min | Recall@0.2 s | Recall@0.5 s | F1@0.5 s | False/min@0.5 s | Nearest MAE / P90 (s) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Calibration | 0.2 (best F1; 18.7/min on noise) | 28.3 | 0.29 | 0.56 | 0.62 | 8.1 | 0.48 / 1.12 |
| Calibration | **0.75 (chosen)** | 26.6 | 0.27 | 0.52 | 0.60 | 7.7 | 0.52 / 1.33 |
| Calibration | 2.0 | 15.3 | 0.19 | 0.37 | 0.52 | 2.0 | 0.95 / 2.38 |
| Report | 0.75 | 20.1 | 0.17 | 0.42 | 0.44 | 11.1 | 0.78 / 1.91 |

What this says: about half the reference boundaries have a change point within 0.5 s, a fifth to a quarter within 0.2 s. On the report set false candidates rise by half, so change points alone are a weak boundary signal -- they are meant to order evidence gathering, together with the other sources, not to segment. Small sets: differences of 0.01-0.02 are within noise.

## In a plan: event intelligence

A temporal plan may set `workflow.event_intelligence`; anything else refuses it. Omitted, `null` and `{"mode": "off"}` all mean off and are stored as no key at all, so a plan without it freezes, caches and estimates exactly what it did before the block existed (a test compares six representative plans with a snapshot taken on main, byte for byte). `{"mode": "candidates"}` turns it on; the plan then freezes every setting with its value:

| Setting | Default | What it does |
| --- | --- | --- |
| `sources` | `gripper`, `height`, `still`, `change_point` | Which readers give candidates. |
| `max_windows` | 4 (1-16) | Candidate windows one episode's refinement may add. An uncalibrated heuristic. |
| `merge_seconds` | 0.5 | Candidates closer than this to a stronger one are merged into it. An uncalibrated heuristic. |
| `change_point_penalty` | 0.75 | The change-point penalty (calibrated above, on development gold labels). |
| `planner` | `greedy` | The only planner: whole windows in priority order, a window that does not fit is skipped. |
| `active_evidence` | `false` | A model asking for more evidence by itself: not available, and `true` is refused. |

Changing any of them, or turning the block on or off, changes the plan's digest: the plan must be approved again. The candidate algorithm is approved too: an on plan stores `event_algorithm` -- the planner's version (`levi.events.sampling.PLANNER_VERSION`) and the readers' constants (source priorities, merge distance, change-point penalty, minimum length and caps, gripper hysteresis, stillness, height turn) with the SHA-256 of their canonical JSON (`levi.events.candidates.algorithm`). It is part of the plan's digest, and starting or resuming the run refuses a plan whose algorithm differs from the running code's ("The event candidate algorithm changed"). A change to how candidates are found, merged or ordered, or how windows are chosen, that the constants do not show raises `PLANNER_VERSION`; a change to the change-point search or its cost raises `levi.events.change_points.VERSION`, which is one of the constants (v2 since the block fix of 2026-10-15, so an on plan approved before it must be approved again). An off plan has no such record. The model cache fingerprints the plan's context, so a run never reuses an answer made under other settings. The plan's estimate keeps its request count and adds `estimate.event_intelligence` (`extra_requests: 0`, the frames one window adds, the most extra frames per episode, the frame cap) and a sentence to its basis (more images in the refinement request cost more tokens, within `max_tokens`); `plan.event_intelligence` repeats the settings. The approval panel shows them read-only (mode, sources, windows, merge distance, penalty, planner, model-requested evidence fixed off, the most extra frames against the frame cap, no extra request); a plan without the block looks as before.

**In a run LEVI executes.** After the coarse request, LEVI reads the episode's candidates from the run's snapshot (once per episode; the run event `event_candidates` counts them) and plans the refinement's frames (`levi.events.sampling`), within the plan's frame cap less the coarse frames, and the model's image limit:

1. the draft's own boundaries (and its boundary candidates): never dropped; when they alone do not fit, the refinement coarsens or is split in batches exactly as before;
2. candidate windows, most salient first, at most `max_windows`;
3. the published change windows (`evidence.refine_top_k`), by the same rule: a window that does not fit is skipped and the next tried. Without event intelligence they are dropped from the least changed until the rest fit, so where the budget is tight the two choose differently: an on plan whose episode has no candidates is not the same as an off plan.

A window is whole or skipped (never thinned); one whose frames are all chosen already costs nothing and is recorded as covered. Window frames are computed with the arithmetic of `observations.frame_scope` (tested), so the planned count is the count the run is held to. The plan is recorded per refinement (run event `event_evidence_plan`: what each tier added, what was skipped and why) and in the episode's shard (`event_plan`: the candidate read and every batch's plan); the change set's provenance (`event_intelligence`) names the settings, the algorithm, per episode the candidate read and each batch's accepted windows, and a summary. A long draft refined in several batches gets no candidate windows, as it gets no change windows.

**Resuming.** A refinement whose frames were persisted but whose request did not finish (a provider error, the GPU gate, a pause) is planned again when the run resumes, under the image limit of that moment -- for a local model a fitted value that moves between requests. Greedy plans under two limits are not nested, so each plan counts the frames the episode's evidence ledger already holds as free and new frames against the plan's frame cap for the whole episode: the ledger never passes `max_evidence_frames` (5000 synthetic resumes, 0 over; a runtime test reproduced the failure without it), and a first refinement plans exactly as without the ledger (tested). Which windows are read depends on the image limit: a repeated or resumed run reads the same frames only under the same limit (`event_evidence_plan` records `budget_images`; group comparisons by it).

What does not change: the coarse pass, the number of requests (candidates add frames to the refinement request, never a request; `Budget.max_calls` still stops the run), the prompt (the model's request states the workflow without `event_intelligence`, and the candidates are never shown as findings), validation, and everything after.

**When the signals say nothing.** A candidate read that fails or finds nothing leaves the run to its pictures, and says so with a reason code: `unreadable` (a snapshot file), `invalid_signals` (the table, `meta` or a signal declaration does not hold what the readers need), `internal_error` (anything else, a defect in LEVI included) or `no_candidates` (the read worked and found none). The code is in the `event_candidates` event, the shard and the provenance, whose `summary` counts the episodes with candidates and those without, by reason, so an on result that ran without candidates is visible as such. Messages leave this machine's paths out; the provenance carries only the code and the error's type.

**For an external agent.** `runs.prepare` returns `events_first` (per episode, the candidate instants whose windows fit what the frame cap has left, at the spacing `evidence.refine` uses) and `events_policy`. `events.candidates` (`run_id`, `episode`; the episode must be prepared) lists the kept candidates (`id`, `event_type`, `actor_id`, `at`, `window`, `salience`), how many were merged, the same `suggested_around_seconds`, the frames left and, when the read failed or found nothing, `error_code` (and the error's type). It is a read capability: text only, no frame, path or hash (a test pins its fields). Candidates come from the recorded state and action columns, not the cameras, so it needs no media authorisation, while every frame still does (`evidence.refine` and `evidence.read` are refused without it). A plan without event intelligence refuses it.

### Candidates

`candidates.read(table, info, profile, stats, episode_index, sources, merge_seconds, change_point_penalty)` turns the readers' output into `EventCandidate`s: gripper crossings (`gripper_open`, `gripper_close`), height turns (`height_low`, `height_high`), the start and end of each still span (`still_start`, `still_end`) and change points. Each source has a fixed priority -- gripper 0.9, change point 0.7 times its own salience, height 0.5, still 0.3. **These priorities, `max_windows` and `merge_seconds` are uncalibrated heuristics**, set by hand and not tuned on any gold set; only the change-point penalty was calibrated. They are to be calibrated on development gold labels only (screws development gold, the plates diagnostic set; never a frozen or held-out set), with the model-free coverage of `ablation plan` first and then the annotation comparison below; a new value is a new `event_algorithm` and needs a new approval. Candidates within `merge_seconds` of a stronger one are merged into it: it keeps its time, takes their sources and its window grows to cover theirs; the merged ones stay in the list with status `merged`. Ties go to more distinct sources, then the earlier, then the id. The result is the kept candidates in priority order, then the merged ones; the order does not depend on the input order (tested).

### Pending GPU window

Whether event candidates make annotation better at the same frame budget needs the local model, so it is not measured yet. To run when the GPU is free (holding the GPU lock), on development episodes only (never a frozen or held-out set; tune nothing on a test set):

1. Two temporal runs of the same episodes with the same model, decoding, budget and frame cap, one with `event_intelligence: {"mode": "candidates"}` and one without (and optionally one with `max_windows` 2), plus the usual no-LEVI control.
2. `python -m levi.events.ablation plan --spec <spec.json>`: where each arm's refinement would look and how many reference boundaries it covers, model-free (the spec format is in the module's docstring).
3. `python -m levi.events.ablation score --reference <ref.json> --durations <durations.json> --arm off=<a.json> --arm candidates=<b.json>`: boundary scores at 0.1/0.2/0.5 s and segment F1 per arm, and each arm's difference from `off`.

The command line refuses any input path naming a frozen or held-out set and writes only to stdout or `--out`. Also pending: the requests' token cost per arm (more refinement images per request) and wall time.

## Clip proposer (VPP2 step P1)

`clips.propose_clips(signals, params)` cuts one whole recorded episode into clips of 1 to 12 seconds at the places a robot episode turns: where the gripper opens or closes and where the end-effector slows nearly to a stop. It is the model-free first step of the data-processing description of VPP2 (Video Prediction Policy 2, arXiv:2610.10270, Sections 2 and 3.1 and Appendix A as read in v1; Section 4 was not read). **From the paper: the 1 to 12 s range and the two cues.** The paper then has a vision-language model merge neighbouring clips and move their ends; **that step is not implemented here**. The paper does not say how a velocity minimum is found, which thresholds apply, or what to do with a clip that comes out too long or too short. All of that is **ours (self-chosen)**, set by reasoning and tuned on no gold set. This module does not reproduce the paper's pipeline, and no result from it may be described as a reproduction.

It is offline (it reads the whole episode, future included), pure and deterministic: no model, no random numbers, no file read, nothing but numpy imported. Input and output are plain data, so other projects can call it through an adapter of their own. It does not describe a clip, refine a boundary by looking at pictures, trim an idle start or end, or use rotation, and a cue is a place to look, never a subtask boundary by itself.

**Input.** `ClipSignals(times, positions, gripper, gripper_bounds)`: seconds that strictly increase; an end-effector position per sample (shape `(n, d)`); one gripper channel (either end may be open); optionally the channel's own range. At least one of position and gripper is needed. Nothing is inferred from names: `signals_from_table(table, feature, position, gripper_index)` makes one from a table whose columns the caller declares. **Input that cannot be read is refused**, never repaired: `ClipInputError` has a stable `code` (`empty`, `too_few_samples`, `not_numeric`, `shape_mismatch`, `non_finite`, `non_monotonic`, `no_signal`, `bad_bounds`, `bad_table`, `bad_params`, `bad_record`) and a message naming the cause and the first bad row. A NaN, a repeated or reversed timestamp, an empty array or a length mismatch ends the call; dropping corrupt rows belongs to the filter before this step (VPP2 step P0).

**Parameters** (`ClipParams`, one named dataclass; `PARAM_SOURCES` says which are the paper's):

| Parameter | Default | From | Why |
| --- | --- | --- | --- |
| `min_s`, `max_s` | 1.0 s, 12.0 s | paper | "Semantically coherent clips lasting 1-12 seconds". `max_s >= 2 * min_s` is required: otherwise some lengths (9 s between 5 and 8 s) have no cut into clips of the range. |
| `smooth_s` | 0.5 s | ours | Spans 5 to 15 samples at 10 to 30 Hz, so one noisy step does not make a minimum; shorter than a real pause. The package already treats 0.5 s as the shortest meaningful state (`motion.STILL_SECONDS`, `change_points.MIN_SECONDS`). |
| `min_prominence` | 0.2 | ours | A minimum counts if the smoothed speed rises on both sides by at least this share of the episode's fast speed (its 95th percentile, as `motion` uses). A 20% ripple on a fast stroke is not a stop. |
| `max_level` | 0.5 | ours | ... and the minimum is at most this share of the fast speed: the arm slows to half or less. |
| `min_gap_s` | 2.0 s | ours | Two speed cues closer than this keep the more prominent; a slowdown is the weakest cue, and two within 2 s are more likely one pause with a ripple. Gripper cues are exempt. |
| `split_window` | 0.25 | ours | A long clip is cut at a held-back speed cue within this share of a piece around the even cut point; wider would trade the balance of the pieces for a somewhat deeper dip. |
| `gripper_min_travel` | 0.2 | ours | With a given gripper range (`gripper_bounds`), a channel that moves less than this share of it has no events: the rule `facts.read` uses. The crossing thresholds are the shared kernel's (`gripper.LOW`, `gripper.HIGH`). |
| `gripper_min_travel_abs` | 0.001 | ours | Without a `gripper_bounds`, a channel that moves less than this in its own units (1 mm for a width in metres) has no events: read on its own range, jitter would otherwise span the whole [0, 1] and cut a boundary at every wiggle. Set it for another unit (a 0 to 255 count). |
| `min_fast_speed` | 0.03 | ours | If the episode's fast speed (95th percentile) is below this, in position units per second (2 cm/s for metres: an arm that is essentially idle), there are no speed cues: the path length of position noise grows with the sampling rate (about 2e-2 per second for sigma 2e-4 m at 30 Hz) and would otherwise be read as motion with dips. Set it for another unit. |

`ClipParams.sha256()` hashes the parameters' canonical JSON (an integer and the same float hash alike); any change to a value changes it. **These values are untested on real data**; they are a starting point. Changing a value changes the hash; a change to how clips are found that no parameter shows raises `VERSION`.

**Steps.**

1. *Cues.* Gripper crossings come from `gripper.read` (hysteresis scaled to the given `gripper_bounds`, or, without them, to the episode's own range and then only if the channel moved at least `gripper_min_travel_abs`), at the crossing's time. `evaluate_gold` and `signals_from_table` take `gripper_bounds`; `evaluate_gold` without it reads each episode on its own range. The speed is the end-effector path length over a centred window of `smooth_s` divided by the window (uneven sampling is not a jump). An interior local minimum is a cue when it passes `max_level` and `min_prominence`; the cue sits in the middle of its valley at half the prominence, so a long pause is cut in its middle, not at a noise ripple. Cues closer than `min_s` to either end are dropped. Of the speed cues the most prominent come first, and one within `min_gap_s` of a kept cue is dropped. A gripper cue is never dropped here and wins over a speed cue.
2. *Merge short.* The shortest clip under `min_s` loses one of its two boundaries: the weaker one (a speed cue before a gripper cue, the less prominent speed cue first), then the one whose merge stays within `max_s`, then the shorter merge, then the earlier. The cut that goes is listed in `dropped`. Greedy, not an optimum.
3. *Split long.* A clip over `max_s` is cut into the fewest pieces (`ceil(length / max_s)`), one cut at a time from the left, each leaving a rest that the remaining pieces can still fill within `[min_s, max_s]`. The cut goes at the most prominent speed cue within `split_window` of the even cut point (one that was dropped in step 1 or merged away), else at the even cut point. A ripple that is no cue does not move a cut.
4. An episode shorter than `min_s` is one clip flagged `too_short_episode`; it is never dropped.

**Output and reason codes.** `ClipProposal` holds `clips`, `dropped`, the `params` and the `algorithm_version` (`levi.events.clips.v1`); `as_dict()` is JSON with `schema_version` `levi.clips.v1`, the parameters, `params_sha256` and `param_sources`; `ClipProposal.from_dict` reads it back and refuses a record whose parameters do not match their hash. A clip has `start_s`, `end_s`, a `start_reason` and an `end_reason` and `flags`; `reasons` lists every code that applies.

| Code | Where | Meaning |
| --- | --- | --- |
| `boundary_gripper` | boundary | The gripper crossed (opened or closed) here. |
| `boundary_speed_min` | boundary | The end-effector speed has a significant minimum here. |
| `split_long` | boundary | An over-long clip was cut here, at a speed cue that step 1 had held back. |
| `split_long_even` | boundary | An over-long clip was cut into equal pieces: no cue was near. The weakest evidence. |
| `episode_start`, `episode_end` | clip edge | The edge of the episode. |
| `merge_short` | clip flag, `dropped.why` | A boundary inside this clip was dropped because a neighbour was shorter than `min_s`. |
| `too_short_episode` | clip flag | The whole episode is shorter than `min_s`. |
| `near_edge`, `min_gap` | `dropped.why` | A cue found in step 1 was within `min_s` of an end, or within `min_gap_s` of a stronger cue. |

**What is guaranteed (tested on 400 random signals with fixed seeds, and by hand).** The clips cover the episode from its first to its last timestamp without gap or overlap, in order; every clip is within `[min_s, max_s]` (to 1e-9 s) except a lone `too_short_episode` clip; the same input gives the same output. A gripper crossing keeps its boundary unless it lies within `min_s` of an end or of another crossing, where the length limit wins and `dropped` says so.

### Judging it

`clips.evaluate(episodes, params, signals_of=..., baseline=...)` scores the proposed boundaries with `boundary_metrics` (recall, precision, F1 and false candidates per minute at 0.2, 0.5 and 1.0 s) and the clips with Segment F1, against reference boundaries. With `baseline=candidates_baseline()` it scores **the current candidates pipeline on the same reference, the same episodes and the same tolerances**: all its kept candidates, and its first n where the proposer made n boundaries. The baseline is an evidence ordering with many candidates per minute, not a clip proposer, so read its recall beside its count. An episode that either arm cannot read is scored in neither and is listed under `skipped`.

`clips.evaluate_gold(folders, root, params)` reads gold folders (the layout of `calibrate`) **only through `calibrate.Guard`**: a folder or source naming `frozen`, `heldout` or `held-out`, or on an exclusion list (`exclusion_lists`, `LEVI_POOL_HELDOUT`), ends the run. It is the one function in the module that reads files. Run it on development gold only. **Status: measured on synthetic signals only** (`tests/test_events_clips.py`); how its boundaries compare with human boundaries and with the existing candidates on real data is not known.

## Third-party sources

`levi/events/third_party.json` (`levi.third_party_registry.v1`) records every outside source this package depends on or borrows from: `source`, `version`, `licenses` (code, weights, data), `use` (`dependency`, `adopt-idea`, `adapt-code`, `vendored`), whether any of it is `shipped` or `code_copied`, whether it is `redistributable`, a `citation` with its checked `reference_key`, and `where` it is used. `levi.events.third_party.problems` enforces, in the test suite: a source whose code licence is non-commercial, missing or unverified may only be an idea (nothing copied or shipped); a citation needs a verified reference; a dependency must be declared in `pyproject.toml`; every arXiv number a `levi/events` module mentions must belong to a registered source. Today it lists `ruptures` (an idea, BSD-2-Clause, not installed), VideoSeek (an idea: its overview, skim and focus tools named the evidence planner's tiers; Lin et al. 2026, arXiv:2603.20185, code MIT, nothing copied), VPP2 (an idea: the 1 to 12 s clip range and the two cues of the clip proposer; Guo et al. 2026, arXiv:2610.10270; nothing copied, the rest of the proposer is ours) and the existing core dependencies NumPy and pydantic: this package adds no dependency. [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) remains the release inventory.


## Opt-in online observation stream

`levi.events.stream.CausalSignals` reads one observed prefix at a time. Its
`feed(Sample(step, observed_ns, values), now_ns=...)` uses a frozen, declared
`SignalProfile`: gripper units, range and open polarity must be explicit;
position channels must be measured xyz in metres in one declared frame.
Thresholds are fixed by `StreamConfig`, never fitted to future samples.
Missing, invalid or out-of-range signals and observation gaps break continuity.
They report `unknown` rather than inventing a crossing. Rotation, change-point
and object-semantic detectors are not implemented online.

`levi.automatic.adapters.signal_events.CachedObserver` wraps an existing robot.
It delegates each existing `observe()` exactly once, returns the original
observation unchanged, and caches only a bounded snapshot. `SignalEvents.poll`
consumes that snapshot; it makes no robot or camera request. The caller supplies
an explicit `step_of` source for the current pre-action control-loop step, the same
clock object, the robot actor, frozen channel `Binding`s, an owned evidence root
and a bounded `keep_frame(ref, encoded_bytes)` callback. `EvidenceStore.keep_frame`
implements this callback. `open` / `poll` / `close` have the same wire interface
as `FileEvents`; the output parses as `levi.aeri.event.v1`.

SDK `pose[6]` is a **command**, so its events are named
`gripper_command_open` / `gripper_command_close`. A measured gripper channel
binds `robot_state.gripper_position` and requires `gripper_measurement` from the
same `getstate` read: route `getstate`, field `gripper_pos`, strictly advancing
`generation`, `read_from_ns` / `read_through_ns` inside the current observation
call, matching `clock_domain`, and a normalized `value` equal to the measured
field. A command never stands in for missing measurement. The adapter uses
observed `pose[0:3]` for measured position channels; every channel must belong to
the explicitly selected robot actor.

A candidate carries a stream-epoch event ID, sequence number, actor, exact
observed step window, clock, signal kind/units, the declared profile hash in the
detector version, and current frame references. The adapter JPEG-encodes the
cached images, retains those exact bytes and rereads the bounded regular file
without following symlinks. It compares the stored SHA-256 to the encoded bytes and checks queued evidence again before delivery.
A hash of ndarray pixels is not a retained JPEG hash. Missing, skipped, corrupt
or expired evidence produces a degraded status and no candidate message.
Future, stale, wrong-step or wrong-clock observations are refused; closing or
reopening fences the observation cache, and an old stream ID is rejected.

Storage is bounded: 2 images and 16 MiB of pixels in the observer, at most 64
profile channels, a configurable 2–4096 scalar samples (default 128), 128 pending
messages and 128 retractable candidates, and at most 1024 episode identities per
adapter instance. The caller owns the evidence-store byte budget; events do not
remove retained evidence. `status` reports unknown reasons, dropped history and
dropped messages. `retract(stream_id, event_id)` can explicitly withdraw a
current-epoch candidate within that bounded history.

**Validation boundary:** CPU/mock tests exercise observation delegation, prefix
causality, units and polarity, missing-signal fallback, actual JPEG hashes,
`EvidenceStore` / `FileEvents` compatibility, bounded storage and step/clock/epoch
attacks. The FR3 entry mounts the adapter only with the reviewed declaration below. Live judging and background
annotation defaults do not mount it. GPU admission, live service wiring and real-robot validation
remain separate integration gates. A candidate requests fresh evidence and an
existing stable-window judgement; it is never a success verdict or a motion
command.


### Explicit FR3 event binding

The operator entry `python -m levi.automatic.fr3_run` accepts an optional inline
`signal_events` object in its existing binding. Without it, `FileEvents` remains
the source. Its exact fields are `schema` (`levi.aeri.signal_events.binding.v1`),
`actor_id` (`arm_0`), `profile`, `bindings` and `config`. Null and partial
objects are refused. `profile` requires `schema_version`, `origin: declared`,
nonempty `channels` and empty `overridden`; each channel explicitly declares
role, feature, index, actor, kind and units. Grippers additionally declare range
and open polarity. Measured xyz must declare axes, metres and the FR3 `base`
frame. No task name or dataset inference supplies these semantics.

Each binding explicitly supplies `feature`, integer `index`, `kind` and `path`:
measured xyz uses `["pose", 0]` through `["pose", 2]`; measured grippers use
`["robot_state", "gripper_position"]`; commanded grippers use `["pose", 6]`.
`config` must explicitly supply every `StreamConfig` field: `max_history`,
`max_age_ns`, `still_ns`, `motion_speed_m_s` and `max_gap_ns`. Values and physical
thresholds come from the operator; this entry provides no fallback thresholds.
The shared gripper detector retains its existing fixed normalized hysteresis.

`--check` returns the declaration and selected event source. The complete inline
bytes belong to `binding_sha256`; `--execute --expect-binding` checks that
approval before and after terminal confirmation. A changed declaration,
threshold or binding needs new binding approval, even if the job's plan SHA is
unchanged. One `CachedObserver` wraps the existing Robot for the policy,
recorder, scene/reset providers and verifier. Its read-only step source is the
current orchestrator control step, independent of homing/reset action counters.
No event polling adds an observation or HTTP call. The actual run EvidenceStore
is attached after orchestrator creation and before any event stream opens.

After a rejected candidate, a fresh observation at the same control step is
consumed once without feeding the causal detector again. Only advancing control
steps produce new candidates. Observation time must still advance and pass the
age, step, clock and episode checks; queued evidence is rechecked before delivery.

A model worker with this fully validated frame-bearing source may use the
job's existing early-stop setting; the external FileEvents path still rejects
that model-worker combination. Its scene/reset READY heartbeat need not claim
an external events capability. Signal candidates retain real current JPEGs,
and existing forward Media/C5 or reset ISC verification still decides whether
to stop. Candidate generation does not bypass GPU admission, watchdogs, stop
signals, motion fences, terminal judgement or human approval. CPU tests exercise
the actual entry with fake SDK and injected models; actual GPU timing and
physical execution remain unverified.


### Event verification uses staged observation sources

In the explicit FR3 signal entry, the current cached observation is staged before
an action is sent. A private ledger binds its stream epoch, source serial, run,
episode, role, host clock, control step and observation time to that same rollout
path and original observation object. Images become immutable JPEG bytes with
hashes. `phase=pre_action` and the separately recorded `recording_row` distinguish
an observation from an acknowledged action: a failed policy chunk advances the
control step without creating a recording row.

The ledger retains at most the binding's frozen `max_history` recent sources, plus
one pinned start source, all within a **16 MiB total JPEG budget** (the
existing signal frame byte cap). Old sources are evicted to stay within the
budget; an evicted candidate, a missing source, a reversed timestamp, or a
clock/episode/epoch mismatch makes verification unavailable. High-entropy frames
near the service's 1280-pixel size limit can exhaust the total budget after only
a few observations, before the requested event window is covered. Missing real
observations cannot be repaired with invented frames or weaker evidence rules.
Synthetic shared-host CPU timings measure stage cost only; they do not validate
the real camera window capacity or the complete control-loop deadline. Only selected JPEGs
are decoded, one at a time. The encoded event request also has a 16 MiB total
budget and uses the existing service image-count and image-size limits.

An event request takes no extra robot observation and adds nothing to the legacy
recording buffer. It uses the actual C5 client's fixed-offset picker and frozen
spec. Every required end view must select at least two distinct source serials,
with its actual earliest/latest selections covering the candidate through the
settling endpoint. A start image serves only the spec's start check; it cannot
extend these reported bounds. Insufficient actual picks return unavailable;
offsets and model decisions stay as declared. The four HTTP top-level fields
and existing encoding/transport stay compatible; optional image `step` and
episode `steps` are omitted/null because pre-action sources are not executed CSV
rows. Control steps and hashes remain in the private proof, not extra HTTP keys.

A ticket freezes actual sampled bounds and event IDs before transport. Later
frames, buffer eviction and request mutation cannot change that proof; a
cancelled or already delivered ticket cannot produce another answer. The C5
model worker retains its existing request ownership until it exits. The old
no-event terminal request and the reset Initial State Contract path retain their
compatibility behavior. File events without a matching cached source ledger are
unavailable. This does not open the running-policy GPU gate or establish physical
safety; hardware use still requires operator approval.
