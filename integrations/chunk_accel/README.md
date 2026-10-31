# In-chunk acceleration adapter for the openpi FR3 client (experimental)

`accel.py` is the rule (pure numpy, no robot, server or camera imports); `run_collect_accel.py` is the entry point the workstation's command runs: the existing collector with its policy query wrapped (and its action check, only to count which actions were accepted), so every gate, clamp, jump check, key and the recording stay the collector's own. Plain inference only. What it does and why: [docs/CHUNK_ACCEL.md](../../docs/CHUNK_ACCEL.md).

Install only the isolated files into an openpi checkout (the installer checks that the client still has what the adapter wraps: `_query_policy`, `_run_episode`, `_clamp_action`, `EvalLog` with its header, and `RolloutRecorder._session_metadata`, `close(extra)` and `abort(detail)`):

```sh
python3 integrations/chunk_accel/install.py --openpi-root /path/to/openpi           # dry run
python3 integrations/chunk_accel/install.py --openpi-root /path/to/openpi --apply
```

It writes only `lab/chunk_accel/{accel,run_collect_accel}.py`, refuses symlinks and different existing files (to update, delete the two installed files first), and never installs packages or starts anything. Tests: `python3 -m unittest integrations/chunk_accel/test_accel.py` (also run by `tests/marvel0/test_chunk_accel_runtime.py`).

Client options beyond the collector's: `--accel-speed` (m/s along a ramp, default 0.25, at most 0.30) and `--accel-tau` (the controller's setpoint low-pass in s, which sets the target's lead: 0.2 at `setpoint_filter_alpha` 0.005, 0.1 at 0.01; the settle time stays 0.2 s either way).
