# In-chunk acceleration (experimental)

The workstation's client command has one more switch: **In-chunk acceleration**. With it, the command runs the FR3 collector through the isolated entry point `lab/chunk_accel/run_collect_accel.py`; the policy server, the collector's gates, clamps, jump checks, keys and recording are unchanged. It works with **plain inference at the policy's 10 Hz only**; asking for it with RTC or VLASH is refused (`chunk_accel_requires_plain`), at another control rate too (`chunk_accel_requires_10hz`). Asking for it without naming a mode means plain inference. Runs are filed under `<model>_accel_demo` unless a group is given.

## What it does

Every chunk the server returns goes through one rule before the collector executes it. A chunk is hurried only if all of these hold:

- no gripper change anywhere in the chunk, including the actions after the executed part (so the approach to a grasp or a release is never hurried), and the same gripper value as the command the arm was last given (the measured state's, which also catches a close the client threw away or sent late);
- at least 1 s of executed actions since the last gripper change (the gripper is still closing or opening);
- the executed actions stay within 5 mm of the straight segment from the first to the last one (a press-and-retract along it is not straight), and turn by at most 0.15 rad.

A hurried chunk is executed as a short ramp from its first to its last executed action, in `m` ticks instead of `n`:

    m = clip(ceil(max(D / (v dt), angle / (w dt), (0.2 s / dt) ln(D / 5 mm))), 2, n)

`D` is the chord, `v` 0.25 m/s, `w` 0.8 rad/s. The last term leaves the arm time to come within 5 mm of the last action before the next observation (0.2 s: how fast the arm closes in on a held target). Along the ramp the target leads by `tau`, the controller's setpoint low-pass (0.2 s, `--accel-tau`), times the ramp speed (the inverse of that low-pass) but never leaves the segment between the first and the last action, and two consecutive targets are at most 5 cm apart (the collector refuses a target 10 cm from the measured pose). Because the hurried chunk is shorter, the collector executes it and queries the policy sooner. After a ramp, at most 3 leading actions of the next chunk that lie behind the ramp's end along its direction are dropped, but only if that chunk keeps moving the same way, so a policy that does not see velocity does not pull the target back.

Each demo records the settings in `policy.chunk_accel` and the episode's counts in `eval.chunk_accel` (`eval.abort_detail.chunk_accel` for an aborted episode): chunks, hurried, ticks saved, dropped actions, and why the others were not hurried. The client refuses to start with RTC, VLASH, a control rate other than 10 Hz, step-by-step mode or an unbinarized gripper. Install it (below) before the first run: the page does not check that the client file exists.

## Expected gain

From the latest evaluations of `pi05_fr3_all_step49999` (10 Hz, read-only analysis in the workspace): 48 % of the executed chunks of plain runs, 54 % of RTC runs and 34 % of VLASH runs are straight and gripper-free; 16–17 % contain a gripper change. With this rule (the gripper hold and the settle term included) 46 % of plain chunks are hurried, and an open-loop replay of those episodes through the identified controller and arm model gives a **median 17.9 % shorter plain episode (3.6 s)**; the same rule on the RTC and VLASH paths gives 16.2 % and 16.7 %. In that replay no target was more than 7.0 cm from the arm (the client refuses 10 cm), the arm's path stayed within 1.6–2.0 mm (p95) of the unhurried one, and the arm was as close to its target at the next observation as without acceleration (median 7–10 mm). The replay does not re-run the policy, so how the policy reacts, and the task outcome, need the real robot. With the setpoint filter at 0.1 s (`setpoint_filter_alpha` 0.01, approved, not yet built) pass `--accel-tau=0.1`: it shortens the lead only; the settle term keeps 0.2 s, because the lightly damped arm still rings (with 0.1 s there too it was 24 mm from the target at the next observation, p90, and peaked at 0.46–0.50 m/s).

## Limits

Experimental: it has not run on the real robot. Real-robot use needs a supervised check with a hand on the e-stop; the preview warns (`chunk_accel_experimental`). RTC and VLASH are not combined with it (both schedule chunks themselves). Install the client with [the adapter instructions](../integrations/chunk_accel/README.md).
