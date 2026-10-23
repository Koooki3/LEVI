# Experimental VLASH adapter for openpi FR3

This ports future-state-aware chunk scheduling from [VLASH](https://github.com/mit-han-lab/vlash) to the existing openpi absolute FR3 action/state wire contract. It does not load official LeRobot/Torch weights or claim delay-augmented training. Checkpoints must use the reviewed state-conditioned FR3 configuration and matching normalization/pose/gripper conventions. State-blind configurations are refused.

Install only the isolated adapters after the two existing FR3 client extension hooks have been reviewed and applied:

```sh
python3 integrations/vlash/install.py --openpi-root /path/to/openpi
python3 integrations/vlash/install.py --openpi-root /path/to/openpi --apply
```

The installer writes only `lab/vlash/{runtime,serve_policy_vlash,run_collect_vlash}.py`, refuses symlinks and different existing files, and never installs packages or starts services. The existing client files are not copied or committed by this installer.

The webpage selects exactly one of plain, RTC and VLASH. VLASH uses `lab/vlash/serve_policy_vlash.py` and `lab/vlash/run_collect_vlash.py`; both accept `--vlash-overlap-steps`. Overlap must be at least one and smaller than the action horizon. The client requires the exact server protocol, space and overlap before initializing cameras or the robot. A request carries `vlash_future_state`; RTC fields are rejected. The ordinary checkpoint loader, normalization and output transforms remain authoritative.

The next chunk is prefetched using the current chunk's final absolute action as its future state and fresh images from the trigger tick. Early replies wait until the current chunk finishes, and the next chunk begins at row zero. Late replies block at the boundary; this implementation never raises robot speed or quantizes/skips actions. Safety-clamped targets, rejected jumps and failed execution invalidate predictions. Reset drains/discards earlier replies before socket reuse; episode close waits for its outstanding call. Diagnostics are recorded under `vlash`, independently of RTC.

Offline checks do not establish physical tracking, calibration, stopping or task success. The UI labels this adapter experimental; `delay_training_verified` remains false. Real inference/performance and physical acceptance require separate operator-controlled validation.
