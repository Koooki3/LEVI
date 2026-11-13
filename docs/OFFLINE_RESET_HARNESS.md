# Offline reset and re-grasp harness

[中文](OFFLINE_RESET_HARNESS.zh-CN.md)

The **Offline reset rehearsal** card is in the existing
`/marvel_workstation` Autonomy step. It is collapsed initially and makes no
requests until opened. It uses its own API and fixed fake executor; the
existing real-run controls and stop dock remain available. Building this
branch does not deploy it to a running workstation.

## What runs

The scenario starts with the synthetic final state of a preceding task:

1. Observe and bind the scene, object and observation sequence.
2. Select a seeded random destination within the synthetic fixture region.
3. Verify empty-hand evidence, request opening, confirm a new measured opening,
   approach validated grasp geometry, then confirm arrival before closing.
   Lift and verify measured gripper closure plus object following.
4. Clear old policy chunks, advance the epoch and obtain a new observation.
5. Admit only the matching reset policy with bound holding evidence.
6. Move to the destination, release, and verify the final object position,
   open gripper and object–gripper separation from a fresh observation.

This order matters: moving the object to its destination before re-grasp is
not a reset demonstration. The final check is required after release; a
successful command acknowledgement is insufficient. `holding` records the
historical pre-handoff evidence, while `final_observation` describes release.

Missing, stale, cross-run or cross-epoch evidence blocks progress. The grasp
retry budget is two; unknown holding stops immediately. Stop is cooperative
between skills. It never commands an emergency stop or returns the real arm
home, and handoff never calls `env.reset()`.

Opening and closure have separate measured facts: not fully open does not
prove closed. The synthetic adapter uses normalized aperture `>=0.9` for open
and `<=0.3` for closed; an intermediate aperture cannot request a lift. These
are offline fixture thresholds, not reviewed hardware limits. Contradictory
open and closed evidence blocks progress. Holding proofs retain both capture
times; handoff binds the final proof sequence and capture time, requires
increasing proof sequences/times, and requires both captures within the existing
100 ms observation age limit. Missing or expired proof times block handoff even
when its new observation is fresh.

## Boundaries and provider interface

`Planner.propose(skill, target)` returns a typed `SkillProposal` with a known
skill and whitelisted target. The present provider is **scripted**, not an
LLM. The measurements, motion and policy are synthetic. A language-model
provider can later propose skills through this boundary; proposals must not
become executable code, raw joint commands or unvalidated coordinates.

The workstation scenario card has no external model request, hardware adapter, camera
input, geometric reachability solver or
physics/contact validation. The unreachable case is an injected failure.
Reopening a saved run replays its evidence in the UI; it does not replay
commands on hardware. Token counts are unavailable (`null`), not a fabricated
zero; the scripted run's actual external model call count is zero.

### Recorded observations with the actual reset policy

`scripts/offline_reset_policy.py` is a separate, explicit offline runner. It
loads the local `reset_r1_step20000` checkpoint through its own source snapshot
and original transforms, with fixed `(10,32)` noise. It reads prepared RGB/state
NPZ files, never live sensors or a policy server. `--samples`, `--checkpoint`,
`--openpi-root`, `--tokenizer` and a new `--output` directory are required for
inference. No weight change, download or physical action occurs. The original
command-based gripper input remains unchanged; measured aperture is stored
separately for diagnosis.

`levi.automatic.policy_replay` prepares three observations per source episode:
before its first close, at that close, and eight recorded rows later. Timestamps
come from the CSV, not an assumed FPS. The two historical reset instructions
are absent from the checkpoint's training prompt inventory and remain explicitly
labelled unseen. Seeded new inference cannot reproduce missing historical
action chunks or sampling noise exactly.

`regrasp_gate.advance_regrasp` checks proposals in the same typed gate used by
the synthetic flow. It requests individual skills, never admits the whole
unvalidated model pose. On recorded observations without independent empty-hand,
grasp geometry and holding evidence, it returns unknown and refuses execution.
The optional `closed_measured` fact defaults to unknown and follows `source`
in `GateObservation`, preserving earlier positional arguments. A close command
or `open_measured=false` supplies no closure or holding proof.
This refusal is not a measured improvement in grasp success. Saved images cannot
show what would happen after replacing an action; these runs are not closed loop.

Every synthetic harness run contains `execution_mode="offline"`, `adapter="fake"`,
`physical_execution=false` and `handoff.physical_allowed=false`. Offline
eligibility has no connection to real execution admission. No new real reset
mode is added to the existing AERI dispatcher.

## Run without the website

Use the project's existing Python environment, from this checkout:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python -B -m levi.automatic.offline_report \
  --workspace ./lab/offline-reset-check --seed 42
```

The seven scenarios cover normal reset, empty first grasp with retry, unknown
holding, stale observation, policy mismatch, unreachable target and stop.
The report checks expected failures as well as successes. It writes
`reset-harness-report.json` and the individual run records under the selected
workspace. Repeating the same matrix request retrieves existing records;
use a fresh workspace for a new code revision. No server, GPU or weights are
needed. In a shared robotics workspace, use its resource and CPU rules.

## HTTP and persistence

Prefix: `/api/levi/marvel0/reset-harness`.

| Method | Suffix                | Input / result                                        |
| ------ | --------------------- | ----------------------------------------------------- |
| GET    | empty                 | Scenarios, latest 50 runs and corrupt-record issues   |
| POST   | `/runs`               | `{request_id, scenario_id, seed}` → queued run        |
| GET    | `/runs/{run_id}`      | Latest evidence, including terminal results           |
| POST   | `/runs/{run_id}/stop` | `{request_id}` → current run and durable stop request |

Writes use the existing Web UI identity guard; they accept neither Bearer
agent credentials nor physical parameters. Seeds are integers in
`[0, 2^32)`. Request IDs bind the scenario and seed: a conflicting reuse
returns 409, while a matching repeat never executes again.

The API store is `$LEVI_WORKSPACE/offline-reset-harness` (the normal workspace
default applies). One fixed subprocess runs at a time, with a maximum of
1000 retained runs. No automatic deletion occurs. A second launch while a
worker holds its lease returns 409. The child receives a minimal environment,
no GPU visibility and a process-local socket guard; the HTTP server's sockets
are unaffected. Each status write is atomic. Locks are held across interpreter
startup, and a saved PID is never used to signal a process. A worker missing
completion evidence is reported failed, not restarted. Corrupt records remain
on disk and are listed separately.

## Implementation and checks

- [Core state machine](../levi/automatic/offline_harness.py),
  [scenarios](../levi/automatic/offline_scenarios.py),
  [worker](../levi/automatic/offline_worker.py).
- [Job store](../levi/marvel0/reset_harness.py),
  [API](../levi/marvel0/reset_harness_api.py),
  [Autonomy card](../src/components/marvel0/reset-harness-card.tsx).
- [State tests](../tests/automatic/test_offline_harness.py),
  [handoff tests](../tests/automatic/test_offline_handoff.py),
  [real subprocess/API tests](../tests/marvel0/test_reset_harness.py).

Run these targeted tests before changing the contract. Before integrating a
real provider, separately validate perception, calibration, grasp geometry,
collision checking, controller limits and an operator-reviewed physical
execution gate. Passing these software fixtures supplies none of that evidence.
