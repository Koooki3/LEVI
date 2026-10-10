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
