# In-chunk acceleration adapters for the openpi FR3 client (experimental)

Two generations live side by side; neither touches the user's client code.

- **v1 / v2** (`accel.py`, `run_collect_accel.py`): the rule that hurries gripper-free (v1) and curved or pre-gripper (v2) stretches of a plain chunk. `accel.py` is the rule (pure numpy, no robot, server or camera imports); `run_collect_accel.py` is the entry point the workstation's command runs: the existing collector with its policy query wrapped (and its action check, only to count which actions were accepted), so every gate, clamp, jump check, key and the recording stay the collector's own. Plain inference only. What it does and why: [docs/CHUNK_ACCEL.md](../../docs/CHUNK_ACCEL.md).
- **v3, the phase clock** (`phase.py`, `schedulers.py`, `run_collect_phase.py`, `client_check.py`): one 10 Hz clock for plain, RTC and VLASH inference. A phase advances lambda rows per tick, every tick issues the chunk's interpolation at that phase, and lambda is bounded by speed, lag, curvature, the gripper windows and a leash against the measured pose (a last target the arm has left behind is moved back just inside the leash, at most one step limit per tick); a close waits for the arm about 0.5 s, counted across chunk reloads and re-queries while the close stays within 16 mm of where the wait began (a close moved further gets its own wait; after 1.0 s of waiting in all it goes out once it has waited 2 ticks at its current place, and always by 2.0 s), and lambda stays at 1 after a close until the fingers have settled and after an open until they have opened; the client's own 0.10 m / 0.35 rad check, 10 Hz, binary gripper and controller settings are unchanged. The contract is `fr3_accel_test/decisions/2026-11-01-phase-clock-design.md`; `phase.py` is the engine (WP1), `schedulers.py` the three mode schedulers (WP2), `run_collect_phase.py` the entry and `client_check.py` the source check both the installer and the entry run.

## Install

Install only the isolated files into an openpi checkout. The installer checks that the client still has what the adapters wrap: for v1/v2 `_query_policy`, `_run_episode`, `_clamp_action`, `EvalLog` with its header, and `RolloutRecorder._session_metadata`, `close(extra)` and `abort(detail)`; for v3 also the RTC hook (`_rtc_chunker`, called by `_run_episode`), the calls the loop makes on the scheduler and the recorder, `_policy_payload` / `_chunk_of` / `_check_rtc_server` / `wrap_to_pi`, `RtcChunker.__init__`'s `_request`, `execute_horizon` and `period_s`, the collector's `scheduler_factory` / `server_validator`, `RolloutRecorder.stage`, and the VLASH runtime's `VlashChunker`, `_Request`, `vector`, `check_server` (`client_check.py` names the file, the invariant and the likely fix in every refusal).

```sh
python3 integrations/chunk_accel/install.py --openpi-root /path/to/openpi           # dry run
python3 integrations/chunk_accel/install.py --openpi-root /path/to/openpi --apply
```

It writes only `lab/chunk_accel/{accel,run_collect_accel,phase,schedulers,run_collect_phase,client_check}.py` and `lab/chunk_accel/installation-receipt.json`, refuses symlinks and different existing files (to update an adapter, delete its installed file first; `accel.py` and `run_collect_accel.py` are never rewritten, and a v1/v2-only install is extended by adding the four new files next to them), and never installs packages or starts anything. The dry run prints the files with their sha256 and the sha256 of the four client files it checked (`rc` = `run_robotiq_client.py`, `rtcc` = `rtc_chunker.py`, `coll` = `run_robotiq_client_collect_data.py`, `vrt` = `lab/vlash/runtime.py`) and whether `vrt` is the reviewed one (`client_check.VRT_REVIEWED_SHA256`, which `schedulers.REVIEWED_VRT_SHA256` must equal). `--apply` writes the same into the receipt, last. Applying again after the client changed re-baselines the receipt, so do it only after the golden tests have been run on the new client.

## v3 entry: `run_collect_phase.py`

The collector with hooks (design section 4): plain `rc._query_policy` (A) + `rc._clamp_action` (B); RTC `rc._rtc_chunker` (C, `args.rtc` stays true, so the client's own server check and log headers stand) + B; VLASH `collector.main(scheduler_factory=..., server_validator=vrt.check_server)` (D) + B; all modes wrap `rc._run_episode`, `rc.EvalLog.__init__` and `RolloutRecorder.{stage, _session_metadata, close, abort}` (E). In RTC and VLASH the action reaches the client's clamp untouched; in plain the engine's target for the tick replaces the placeholder row of the plan.

```
python lab/chunk_accel/run_collect_phase.py --phase-mode {plain|rtc|vlash} --phase-lambda-max <1..3> --phase-alpha {0.005|0.01}
    plain: --open-loop-horizon N      rtc: --rtc --rtc-execute-horizon N      vlash: --vlash-overlap-steps K
```

It refuses to start on: `--control-hz` other than 10, step-by-step, an unbinarized gripper, lambda outside [1, 3] (the page offers 2.0 / 2.5 / 3.0), alpha not 0.005 or 0.01 (it must match `set_clip.py --show`), a mode that disagrees with `--rtc`, client files that fail the source check, a loaded client module that is not the file that was checked, and a VLASH run on a `lab/vlash/runtime.py` other than the reviewed bytes (plain and RTC only record that difference). `RELAXED[2026-11-01] E11`: unlike `run_collect_accel.py` it accepts RTC and VLASH.

Recorded (design section 8):

- `policy.chunk_accel` in every demo's metadata and in the session's eval log header: rule `v3`, mode, lambda, alpha, every `PhaseParams` field, the derived values, the sha256 of `phase.py`, `schedulers.py`, `run_collect_phase.py`, `client_check.py` and of `rc`, `rtcc`, `coll`, `vrt`, the openpi root, `relaxed_tag`, and `code_drift` (true when the install receipt is missing or any hashed file differs from it; `receipt.drift` names the files).
- `eval.chunk_accel` per episode (`eval.abort_detail.chunk_accel` when it was aborted): the engine's counts (ticks, rows advanced, lambda statistics, which cap bound, leash holds and resets, step and distance maxima, blends, switch jumps, gripper events, ...) plus the scheduler's pipeline counters, and `trace {file, sha256, lines}`.
- one JSON-lines trace per episode next to the eval log: `<eval log without .json>.phase/ep_NNNN.jsonl`, with the engine's `tick` (the commanded target), `chunk`, `request` and gripper lines, the scheduler's `request_meta` / `reply` lines, this hook's `sent` line (what the client's clamp returned and its notes) and a final `end` line. It is flushed on every non-tick line and every 10 ticks, and closed in a `finally`, so a crash loses at most one second. A write failure is a warning, never a stop; an episode without an eval log (no directory for the trace) is refused before it starts.

## Tests

- v1 / v2: `python3 -m unittest integrations/chunk_accel/test_accel.py` (every test class there is also run by `tests/marvel0/test_chunk_accel_runtime.py`, which fails if one is left out).
- v3 engine and schedulers: `test_phase.py`, `test_schedulers.py` (the speed and gripper cases run at lambda 2.5 and at the page default 3.0).
- v3 entry, install and end to end: `e2e/test_phase_e2e.py` (pytest). The source check and installer tests and the hook tests on stub client modules need no openpi; one test feeds the page's own argv for the three phase forms (`tests/marvel0/fixtures/chunk_accel_argv.json`) through the entry's parser and `validate()`, so the page and the entry cannot drift apart unnoticed; the subprocess tests (two episodes per mode at lambda 2.5 and 3.0, the failure cases) run the real collector and client from the openpi checkout (`$CHUNK_ACCEL_OPENPI_ROOT`, no default, with its `.venv`; `$CHUNK_ACCEL_FAKES_DIR` names `franka_control/tests/live`, also without a default: the tests that need them skip when they are unset) against `e2e/lag_robot_server.py` (the franka_control mock server with an arm that lags 0.27 s behind its target, a 2.65 Hz mode and a gripper that moves 0.2 s after its command; ports 5071-5079) and `e2e/fake_policy_server.py` (plain / rtc / vlash metadata, a scripted pick-and-place or straight path, injected 46 / 75 mm switch jumps, phantom gripper flips in unexecuted tails, the recorded reply-latency distribution; ports 8041-8049). Neither server ever binds 5000, 5001, 5100, 7470, 8000 or 8010, and ports are picked by binding, never by connecting. No GPU, no ROS, no real arm.

```sh
OMP_NUM_THREADS=1 nice -n 19 taskset -c 8-11 .venv/bin/python -m pytest -q integrations/chunk_accel/e2e \
    --basetemp=.state/tmp/pt-wp3 -p no:cacheprovider
```

## Options of the v1 / v2 entry

Client options beyond the collector's: `--accel-speed` (m/s along a ramp, default 0.25, at most 0.30), `--accel-tau` (the controller's setpoint low-pass in s, which sets the target's lead: 0.2 at `setpoint_filter_alpha` 0.005, 0.1 at 0.01; the settle time stays 0.2 s either way) and `--accel-rule` (`v1`, the default: straight gripper-free chunks; `v2`: also curved chunks along their path and the actions before a gripper change, settled before the gripper command, with a 6 cm target-to-arm guard; any other value is refused at start). The workstation writes `--accel-rule=v2` only when v2 is chosen; see "Rule v2" in [docs/CHUNK_ACCEL.md](../../docs/CHUNK_ACCEL.md). v2 needs this version of the client: an install made before `--accel-rule` existed stops at start on that unrecognized option, so delete the two installed files and run `install.py --apply` again before the first v2 run.
