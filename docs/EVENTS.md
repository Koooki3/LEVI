# Event intelligence: signal facts, profiles and candidates

[中文](EVENTS.zh-CN.md)

`levi.events` reads what a robot's recorded signals say happened, and when. **Today it is a library only**: nothing in a plan, a run, the interface, the API or the CLI calls it yet, and the signal lines an agent reads (`levi.agent.signals`) and anchored reviews (`levi.agent.anchored`) behave as before. The modules are pure functions over numpy arrays and pydantic records: no model, no Torch, no files written.

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

## Records

Both records are rebuildable from the episode's table and never belong in an annotation bundle. Each keeps the table row (`source_frame_index`) next to its time in seconds (`time_s` / `center_time_s` and a `time_window_s`), so nothing downstream converts between them with `frame / fps`, which is wrong on a table with dropped frames. The JSON field `schema_version` names the schema (`levi.signal_observation.v1`, `levi.event_candidate.v1`), as in LEVI's other contracts.

- `SignalObservation`: one fact (`gripper_open`, `gripper_close`, `height_low`, `height_high`, `still`, `change_point`, ...) with its actor, its sources (feature, dimension, index, `measured` / `commanded` / `derived`, units, frame), an optional value and an optional `source_sha256` of the table it was read from. A point fact's window runs from the previous finite timestamp to its own: the change happened between the two samples.
- `EventCandidate`: a place where something may have changed. `salience` (0 to 1) orders candidates for evidence gathering; **it is not a probability** and must not be shown as one. `boundary_probability` stays `null` until a calibrated estimate exists. `status` is `proposed`, `merged` or `rejected`.

## Signal profiles

A profile (`levi.signal_profile.v1`) lists channels with a `role` (`gripper`, `position`, `rotation`, or `ignore` in a declaration), the `feature` and `index` (and `dimension` name), the `actor_id`, `kind` (`commanded` for `action`, else `measured`), `units`, `frame`, `axis`, a gripper's `open_level` (`high` or `low`) and an optional `valid_range`.

- `infer(info)` applies the signal lines' rules to `meta/info.json`: a gripper is a float vector dimension whose name matches `grip|finger|claw|jaw`; a position is one named `x`/`y`/`z` (the first of each axis in a column); a rotation `rx`/`ry`/`rz` or `roll`/`pitch`/`yaw`. A dimension naming `left`, `right`, `arm_N` or `robot_N` belongs to that actor; the rest to `arm_0`. **Nothing in a name says which end of a gripper is open**, so an inferred `open_level` is always empty and readers fall back to the level the episode starts at, which reads an episode that starts with a closed gripper upside down.
- A declaration fixes that. `resolve(info, declared)` lays it over the inferred profile channel by channel: a declared channel replaces the inferred one at the same (feature, index) or adds one, and `role: "ignore"` removes one the names wrongly suggested. A declaration that names a column the dataset lacks, an index outside it or a dimension name that does not match is refused.
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
| Penalty per change: `penalty * (d + 1) * log(n)` for `d` features, `n` rows | `penalty = 0.5` (calibrated, below) |
| Shortest segment (in rows, from the median sampling step) | `min_seconds = 0.5` |
| Longest series searched row by row; a longer one is averaged over blocks of `ceil(n / 3000)` rows first (each block mean times the square root of its length, so the penalty keeps its meaning), change points then fall on block starts and `bin_rows` says so. The search is quadratic in the worst case; 18,000 rows of noise (30 Hz, 10 minutes) take well under 2 s (tested) | `MAX_ROWS = 3000` |
| Most change points per minute of episode; past it the penalty is raised by 1.5x until the result fits; when equally strong changes would all vanish at once, exactly the strongest that fit are kept, ties broken by time (`capped` says so) | `max_per_minute = 30` (set beforehand, not tuned) |

On pure noise scaled the same way the default penalty gives well under one change point per minute; twice the penalty gives none (tested).

### Calibration

`python -m levi.events.calibrate --root <dir> --gold <dir> ... --report-gold <dir> ...` scores candidates of raw robot captures against reference segments (the segments' inner boundaries at the capture's timestamps) for a grid of penalties, and picks the largest penalty whose F1 at 0.5 s is within 0.01 of the best (of near-equal settings, the one that cuts least). It refuses any folder whose path contains `frozen`, `heldout` or `held-out`, reads only pose and gripper CSVs (never video) and writes nothing but `--out`. The report records every episode's source and the SHA-256 of the files it read.

Run of 2026-10-10 (penalties 0.1 to 8):

- Calibration: screws development gold labels (12 episodes, a locked set) and the plates diagnostic set (15 episodes, read from a copy whose frame counts matched the gold labels): 27 episodes, 10.1 minutes, 36.2 reference boundaries per minute. Test sets (frozen and held-out families) were not read.
- Report (not used for the choice): generic v2 pilot gold labels, 7 of 9 episodes; the 2 sourced from the robotiq demonstration collection were excluded (that collection is reserved until the scale-up phase).

| Set | Penalty | Candidates/min | Recall@0.2 s | Recall@0.5 s | F1@0.5 s | False/min@0.5 s | Nearest MAE / P90 (s) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Calibration | 0.2 (best F1) | 24.2 | 0.25 | 0.48 | 0.58 | 6.6 | 0.58 / 1.53 |
| Calibration | **0.5 (chosen)** | 24.1 | 0.23 | 0.48 | 0.57 | 6.8 | 0.59 / 1.52 |
| Calibration | 2.0 | 15.3 | 0.19 | 0.37 | 0.52 | 2.0 | 0.95 / 2.38 |
| Report | 0.5 | 23.3 | 0.20 | 0.46 | 0.44 | 13.3 | 0.72 / 1.87 |

What this says: F1 is flat from 0.1 to 1 because the per-minute cap binds there (the reference annotations are denser than the cap); about half the reference boundaries have a change point within 0.5 s, a quarter within 0.2 s. On the report set false candidates double, so change points alone are a weak boundary signal -- they are meant to order evidence gathering, together with the other sources, not to segment. Small sets: differences of 0.01-0.02 are within noise.

## Third-party sources

`levi/events/third_party.json` (`levi.third_party_registry.v1`) records every outside source this package depends on or borrows from: `source`, `version`, `licenses` (code, weights, data), `use` (`dependency`, `adopt-idea`, `adapt-code`, `vendored`), whether any of it is `shipped` or `code_copied`, whether it is `redistributable`, a `citation` with its checked `reference_key`, and `where` it is used. `levi.events.third_party.problems` enforces, in the test suite: a source whose code licence is non-commercial, missing or unverified may only be an idea (nothing copied or shipped); a citation needs a verified reference; a dependency must be declared in `pyproject.toml`; every arXiv number a `levi/events` module mentions must belong to a registered source. Today it lists `ruptures` (an idea, BSD-2-Clause, not installed) and the existing core dependencies NumPy and pydantic: this package adds no dependency. [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) remains the release inventory.
