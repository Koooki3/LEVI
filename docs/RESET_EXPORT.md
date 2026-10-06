# Reset export / 复位数据导出

A reset episode is a forward demonstration run backwards: the robot starts where the demonstration ended and ends where it began, so a policy can be trained to put things back. The training pool ([Training pool](TRAINING_POOL.md)) writes reset episodes next to, or instead of, the forward ones, with the instruction `Reset: <forward task>`.

Reversing is not "play the video backwards". Four things have to be true of every reversed episode, and this page says how each is made true and where it cannot be.

1. Every camera, the state and the action are one sequence in one new order, frame for frame.
2. The action is rebuilt for reversed time (the stored action is the *next* state; the gripper command has to lead the fingers).
3. Every reversed step is something a robot could do. A release reversed is a grasp, and that is only true if the object is still where the open fingers can take it again.
4. What cannot be reversed is left out, flagged, or completed with a real recording. It is never papered over.

[中文](RESET_EXPORT.zh-CN.md)

## Use / 用法

```bash
# what would be reversed, and why not (writes nothing; a few seconds per episode)
uv run levi pool reset-analyze pi05-mix --limit 12

# forward and reset episodes in one LeRobot v2.1 dataset (the static-frame filter is off)
uv run levi pool export pi05-mix --format lerobot_v21 --name pi05-mix-reset --reset forward_and_reset
uv run levi pool export pi05-mix --format lerobot_v21 --name pi05-reset-only --reset reset_only \
    [--reset-template "Undo: {task}"] [--reset-max-release in_place|in_reach] [--reset-partial] \
    [--reset-contract fr3-robotiq@1] [--reset-review-model <connection>] \
    [--reset-bridge <forward key>=<recording key>] [--reset-any-outcome]
```

The pool page's export panel has the same settings under "Reset data" and a "Check reversibility" button. In the API: `ExportOptions.reset` (`direction`, `task_template`, `action_contract`, `max_release`, `on_ineligible`, `require_forward_success`, `release_camera`, `gripper_lead_rows`, `review_model`, `bridges`) and `POST /api/levi/pool/reset/analyze`. Without `reset`, or with `direction: "forward_only"`, an export is exactly what it was before.

Reset episodes are written only into `lerobot_v21` exports (a `raw_capture` copy is a copy of recordings, and a `recap_value` export would have to invent rewards for a task nobody did). The count you asked per task is the number of **forward** source episodes; each can give one reset episode, so a `forward_and_reset` export holds up to twice as many.

## What a reset episode holds / 写了什么

| | |
| --- | --- |
| Rows | The kept forward rows in reverse order. Timestamps are `frame_index / fps` again; `index`, `episode_index` and `task_index` continue the export's own counters. |
| Cameras | Each camera's frames are decoded once, put in the new order and encoded once (H.264, the export's fps). Nothing is stream-copied: a copy would still play forwards. |
| State | The forward state rows, reversed. |
| Action | Rebuilt, not reversed: for the contract's meaning *next state* (`action[t] = state[t+1]`, as LEVI's conversion writes it, and as `fr3-robotiq@1` declares), the reversed action of row k is the reversed state of row k+1, and the last row holds. Reversing the action column instead leaves every action one step off. The data is checked against the contract (dimensions, a binary gripper, and that the action really is the next state); an action of unknown meaning is refused. |
| Gripper | The command flips *before* the fingers move. Forward, the command opens at row r and the fingers finish at row m; reversed, the fingers close over those same rows, so the reversed command is already "closed" at row m. Plain reversal puts the flip after the motion. `meta`'s `motion_end` comes from the measured width when the capture has it, else from `gripper_lead_rows`. |
| Instruction | `Reset: <the forward text the export writes>` (after `task_text` and approved corrections). It is its own task in `meta/tasks.jsonl`; two forward tasks never collapse into one reset task, and a reset text equal to a forward text is refused. |
| Labels | No outcome is inherited. A forward success does not make a reset a success; `levi_outcome` is absent, so a trainer that needs one must get it from somewhere else. |
| Record | `meta/levi_reset.json`: options, thresholds, contract, and per reset episode its analysis, its **temporal map** (which source frame every output frame is, as runs with step ±1) and how it was made. `meta/episodes.jsonl` rows carry `levi_reset`. `pool_export.json` counts forward and reset episodes and lists every exclusion with its reason. `meta/levi_reset_capture_requests.json` lists what to record for episodes that could not be completed (below). |

A reset export is never read back as a source: an episode with `levi_reset` is refused (`reset_already_reset`).

## Why a release is the problem / 为什么松爪是难点

Reaching, carrying and *grasping* reverse safely: a grasp reversed is the object lowered onto the surface it came from and released, which is what really happened, backwards. A **release** reversed is a grasp, and it is only valid if the object is still where the open fingers can take it. After most releases it is: the object drops a few centimetres and rests between the fingers. After some it is not: it rolls, falls flat or tips out of reach. Reversed, the gripper then closes on nothing while the object "flies" into it, and the recording never shows the arm going to where the object lay, taking it, and bringing it back, because nobody did that.

So the export measures each release and treats it by what it finds.

| Class | Meaning | What the export does |
| --- | --- | --- |
| `in_place` | The object did not move. | Reversed as it is, closing fingers included. |
| `in_reach` | The object settled between the open fingers (it dropped, but a closing gripper would take it). | The frames from the hold frame to the rest frame (the fall) are cut out of the reversed episode. Every frame left is real and consistent, and the gripper closes on an object that is there. |
| `escaped` | The object left the fingers' reach. | The reset is not made (`reset_release_escaped`): left out, reversed from its last safe hold on, or completed with a recording. |
| `unknown` | Nothing could be measured: the arm left at once, the object is still moving or blurred, the match is ambiguous, no camera. | Same as `escaped` (`reset_release_unknown`). Never reversed on a guess. |

`max_release: "in_place"` accepts only the first kind.

### How the measure works / 度量

After a release the arm is often still for a few rows while the object falls and settles, and the wrist camera moves with the hand. While the arm has not moved, the hold frame (object between the closed fingers) and a rest frame (fingers open, object at rest) are the same scene from the same place, so whatever differs is the object and the fingers. The analysis compares the middle of the two images, where the object sits between the fingers: the correlation at the same place (`same`), the best match anywhere near the middle at several sizes (`best`, `dx`, `dy`, `scale`: a dropped object is lower and smaller), how flat the hold frame's middle is (`texture`: a plain tray matches itself) and how sharp the rest frame is (`sharp`: blur means it is still moving). It picks the rest frame as the earliest sharp one that already shows the object in reach, so the arm has moved least and the state jumps least at the seam; the rest frame that decides the class is the last sharp one, where the object ended up.

This is a measurement, not recognition: it does not know what the object is. The thresholds (`levi/pool/reset/profile.py`, version string recorded in every export) were set on real FR3 + Robotiq captures at 10 Hz and are deliberately strict: colour similarity alone never accepts a release, because a plate or a tabletop looks the same with or without the object. A different robot or camera needs its own look at the numbers: `levi pool reset-analyze` prints them. An optional local vision model (`review_model`) can **veto** an accepted release (`vlm_veto`), never rescue a refused one: published results put models judging whether a manipulation worked at roughly 0.6–0.8 balanced accuracy, biased towards "it worked", which is enough to catch a mistake and not enough to decide.

### Measured on this workspace's data / 实测

Analysis only, 300 random teleoperation captures of `data_collection_robotiq` (10 Hz, 640×480, no static filter), at the shipped thresholds:

<!-- RESET-MEASURED -->

## What cannot be reversed / 无法反转的部分

For an episode with a release that is `escaped` or `unknown`, the options are:

- **Leave it out** (`on_ineligible: "exclude"`, the default). The forward episode is unaffected. The export lists it with the reason and the measures (`meta/levi_reset.json`, `analysis_of_excluded`).
- **Reverse from its last safe hold on** (`"partial"`). The reset starts with the object already in the gripper, at the pose where the forward episode held it before the problem release. It is flagged (`levi_reset.scope: "partial"`, generation `partial`): it teaches carrying and putting the object back, not taking it from where it came to rest, and its start is not the forward task's real end state.
- **Complete it with a recording** (`bridges`). When the problem is a single release, the export can put a real recording in front of the reversed rest: a pool episode that starts from the forward episode's last pose with the object where it lies, approaches, grasps it and brings it to the pose where the reversed episode takes over. The join is checked, not assumed: the recording starts within 3 cm of the forward end; the gripper closes and stays closed; the measured width (if any) says something is held; pose and rotation at the join match the forward hold; and the two wrist-camera frames at the join show the same scene. A failed check leaves the episode out with its reason (`reset_bridge_*`). The recording must be a separate pool episode (it is checked against the held-out lists and removals like any other source) and is not exported as a forward episode.

Episodes that could be completed but have no recording get an entry in `meta/levi_reset_capture_requests.json`: the pose to start from, the pose to end holding the object at, the hold width, the cameras and fps, and a short instruction. Recording them (or the whole reset, as an ordinary episode with the task text `Reset: …`) is the way to close the gap; the pool takes the recording back like any other.

### Can signal processing or a local model fill the gap? / 能否补全

**Part of it, and that part is done here.** Everything that can be decided from signals is: event detection from the gripper command and width, where the finger motion ends, whether the arm stayed still, the pose jump at the seam, and the image measures above. Where the object stayed within reach, cutting the fall leaves only real, consistent frames; no synthesis is needed.

**The rest cannot be synthesised from the recording.** Where the object left the fingers' reach, the missing part is new behaviour (go to where it lay, take it, bring it back) and new camera images of it. A planner can draw the arm's path (`levi.pool.reset.bridge.reference` is a minimum-jerk path used for the capture request) but a path without the matching images is not training data, and the action cannot be checked against pixels that were never taken. A local vision-language model cannot do it either: it can look at a frame and say what it sees, it does not produce a physically consistent, multi-camera, action-consistent video. Generative video and world models were checked as well: the ones that publish multi-view, action-conditioned results derive their actions from the video (pseudo-actions) or only change appearance, so none is action-consistent ground truth for a VLA. Published work that reverses demonstrations (Reverse to Advance, TR-DRL, Green-VLA) excludes the irreversible parts instead; this export does the same and adds the measured, cut-and-join treatment of the common case and the recorded bridge for the rest.

## Guarantees and limits / 保证与限制

- Sources are never written. Reset videos are new files (never a hard link of a source), written into the export's staging folder like everything else; an interrupted export resumes from its journal (`reset|<episode>|<camera>` units are verified by size and hash).
- Held-out and removed episodes are refused for forward and reset alike, and so are bridge recordings; a reset export is not a source for another one.
- The action contract is a declared fact checked against the data. Only `fr3-robotiq@1` is registered; another robot needs its own contract in `levi.counterfactual` before its episodes are reversed.
- The static-frame filter is off by default for reset exports (the rows in which an object settles are the evidence). With it on, or at a lower fps than the capture, the analysis still reads the retained rows, which may be too few to see the object settle: more episodes become `unknown`.
- LeRobot v3 sources are not reversed (the pool already lists them as not exportable).
- A reset episode says what the demonstration did, backwards. It is **not** evidence that a robot succeeded at resetting, and carries no success label. A training stack that requires one per episode needs another source of it.
- Cost: one decode and one encode per camera per reset episode, plus a scratch copy of one decoded video at a time (about 0.9 MB per 640×480 frame) under the staging folder, removed when the stage ends.

## Reasons an episode is left out / 排除原因

`reset_forward_failed`, `reset_forward_unlabeled` (only finished tasks are reversed; a human recording without an outcome counts), `reset_already_reset`, `reset_action_contract` (the data does not follow the contract), `reset_release_escaped`, `reset_release_unknown`, `reset_release_in_reach` (with `max_release: "in_place"`), `reset_unreadable`, and for bridges `reset_bridge_missing`, `reset_bridge_contract`, `reset_bridge_start_mismatch`, `reset_bridge_no_grasp`, `reset_bridge_never_reaches_anchor`, `reset_bridge_nothing_held`, `reset_bridge_visual_mismatch`, `reset_bridge_visual_unchecked`. A release's own reason is one of `no_hold_frame`, `arm_left_before_settle`, `no_release_camera`, `object_still_moving`, `ambiguous_match`, `low_texture`, `object_left_the_fingers`, `vlm_veto`.

## Related work / 相关工作

Checked against the papers and repositories themselves (2026-10): Reverse to Advance (arXiv 2607.13455) reverses easy tasks and recomputes actions from adjacent poses; TR-DRL (arXiv 2505.13925) filters reversed transitions with a forward dynamics model, in simulation; Green-VLA (arXiv 2602.00919) reverses only reversible skills and excludes placements with a possible drop; ReWiND uses reversed video only to train a progress model; HALTER and FLARE record their reset skills instead of reversing. LeRobot's own dataset tools have no reverse. None of them fills the gap this page describes with synthesised data.
