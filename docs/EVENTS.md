# Event intelligence: signal facts, profiles and candidates

[中文](EVENTS.zh-CN.md)

`levi.events` reads what a robot's recorded signals say happened, and when. **Only a temporal plan that turns it on uses it** ([below](#in-a-plan-event-intelligence)): there its candidates choose extra frames for the boundary refinement. Nothing else calls it: a plan without it, the interface and the CLI behave as before, as do the signal lines an agent reads (`levi.agent.signals`) and anchored reviews (`levi.agent.anchored`). The modules are pure functions over numpy arrays and pydantic records: no model, no Torch, no files written.

| Module | What it holds |
| --- | --- |
| `gripper` | Open/close crossings of one gripper channel with hysteresis; the kernel behind the signal lines and anchored reviews. |
| `motion` | Speed and stillness on the recorded timestamps. |
| `contracts` | `SignalObservation` and `EventCandidate`, the records the readers produce. |
| `signal_profiles` | What each recorded channel means: role, actor, units, frame, which end of a gripper is open. |
| `facts` | The signal facts of one episode as `SignalObservation`s, per channel and actor. |
| `change_points` | Penalised change points of each actor's speed, gripper level and rotation speed, as `EventCandidate`s. |
| `calibrate` | The script that chose the change-point penalty on development gold labels. |
| `third_party.json`, `third_party` | The structured record of outside sources this package uses, and the rules it keeps. |
| `boundary_metrics` | Boundary Recall at several tolerances, false candidates per minute, boundary MAE/P90, Segment F1 at several IoU thresholds. |
| `candidates` | Every source's candidates of one episode, merged and in evidence priority. |
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

`change_points.candidates(table, info, profile)` returns `EventCandidate`s of type `change_point` for every actor. Per actor it builds a multi-dimensional series from the profile -- the speed of its position (on the recorded timestamps, so a dropped frame is not a jump), its gripper level (a measured channel before a commanded one) and the speed of its rotation (unwrapped; degrees when the profile says `deg`) -- scales each feature to a robust spread (the larger of a quarter of its 1-99% range and its noise from first differences; a feature with no spread is left out; missing values are filled from their neighbours) and finds the exact penalised segmentation with a piecewise-constant mean and squared-error cost (optimal partitioning with pruning, the PELT family; Truong, Oudre and Vayatis 2020, arXiv:1801.00718, describe it and implement it in `ruptures`, BSD-2-Clause). LEVI does not depend on `ruptures`. With a minimum segment length the pruning is delayed by that length (a start that fails the pruning test at row t may still be the best start for ends before t + min_size), so the result is the exact optimum (of the block series, for a series longer than `MAX_ROWS`); the tests compare it with unpruned optimal partitioning on 1,000+ random series (noise, steps, random walks; minimum lengths 1 to 8).

A change point says the signals' level changed, not what happened: it is a candidate for evidence, never a subtask boundary by itself. `salience` is `gain / (gain + beta)` (the cost the change saves against the penalty): an ordering, not a probability.

Guards against over-cutting, each tested:

| Guard | Default |
| --- | --- |
| Penalty per change: `penalty * (d + 1) * log(n)` for `d` features, `n` rows | `penalty = 0.75` (calibrated, below) |
| Shortest segment (in rows, from the median sampling step) | `min_seconds = 0.5` |
| Longest series searched row by row; a longer one is averaged over blocks of `ceil(n / 3000)` rows first (each block mean times the square root of its length, so the penalty keeps its meaning), change points then fall on block starts and `bin_rows` says so. The search is quadratic in the worst case; 18,000 rows of noise (30 Hz, 10 minutes) take well under 2 s (tested) | `MAX_ROWS = 3000` |
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

Changing any of them, or turning the block on or off, changes the plan's digest: the plan must be approved again. The candidate algorithm is approved too: an on plan stores `event_algorithm` -- the planner's version (`levi.events.sampling.PLANNER_VERSION`) and the readers' constants (source priorities, merge distance, change-point penalty, minimum length and caps, gripper hysteresis, stillness, height turn) with the SHA-256 of their canonical JSON (`levi.events.candidates.algorithm`). It is part of the plan's digest, and starting or resuming the run refuses a plan whose algorithm differs from the running code's ("The event candidate algorithm changed"). A change to how candidates are found, merged or ordered, or how windows are chosen, that the constants do not show raises `PLANNER_VERSION`. An off plan has no such record. The model cache fingerprints the plan's context, so a run never reuses an answer made under other settings. The plan's estimate keeps its request count and adds `estimate.event_intelligence` (`extra_requests: 0`, the frames one window adds, the most extra frames per episode, the frame cap) and a sentence to its basis (more images in the refinement request cost more tokens, within `max_tokens`); `plan.event_intelligence` repeats the settings. The approval panel shows them read-only (mode, sources, windows, merge distance, penalty, planner, model-requested evidence fixed off, the most extra frames against the frame cap, no extra request); a plan without the block looks as before.

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

## Third-party sources

`levi/events/third_party.json` (`levi.third_party_registry.v1`) records every outside source this package depends on or borrows from: `source`, `version`, `licenses` (code, weights, data), `use` (`dependency`, `adopt-idea`, `adapt-code`, `vendored`), whether any of it is `shipped` or `code_copied`, whether it is `redistributable`, a `citation` with its checked `reference_key`, and `where` it is used. `levi.events.third_party.problems` enforces, in the test suite: a source whose code licence is non-commercial, missing or unverified may only be an idea (nothing copied or shipped); a citation needs a verified reference; a dependency must be declared in `pyproject.toml`; every arXiv number a `levi/events` module mentions must belong to a registered source. Today it lists `ruptures` (an idea, BSD-2-Clause, not installed), VideoSeek (an idea: its overview, skim and focus tools named the evidence planner's tiers; Lin et al. 2026, arXiv:2603.20185, code MIT, nothing copied) and the existing core dependencies NumPy and pydantic: this package adds no dependency. [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) remains the release inventory.
