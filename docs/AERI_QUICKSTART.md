# AERI quickstart

[中文](AERI_QUICKSTART.zh-CN.md)

AERI is LEVI's automatic evaluation pipeline: it runs a policy on a robot round after round, judges each round and keeps one journal of what happened. This page gets you from nothing to a finished run in about five minutes **without a robot**: a dry run drives in-process fakes only. Do this once before your first real-robot evaluation; it shows you every file, command and result you will meet later.

For how the parts fit together, read [figure 6](ARCHITECTURE.md#6-automatic-evaluation-three-parts-one-authority) (who may move the robot) and [figure 8](ARCHITECTURE.md#8-an-aeri-run-start-to-finish) (a run start to finish) of the architecture atlas. The full reference is [Automatic evaluation pipeline](AUTOMATIC_PIPELINE.md).

## 1 A dry run with fakes

A dry run uses the fake robot in `integrations/fr3_automatic/fake.py`. Every network connection is refused while it runs, nothing real has motion authority, and the results go to a temporary folder unless you pass `--keep`.

### Two files

Put both in one folder (called `JOBDIR` below).

`initial-state.yaml`, the initial state contract: what must be true before a round may start.

```yaml
initial_state:
  id: stack-plates-initial
  version: "1"
  predicates:
    required: [object_at_source, gripper_open]
  observations:
    require_visible_evidence: true
    min_evidence_refs: 1
```

`automatic-eval.yaml`, the job file (the launch wizard in the web UI can write one for you). Replace `JOBDIR` with the folder's absolute path.

```yaml
schema_version: levi.aeri.job.v1
experiment:
  name: rehearsal-1
  episodes: 2
  random_seed: 42
  execution_mode: shadow
policies:
  forward:
    max_steps: 30
  reset:
    max_steps: 20
task:
  instruction: stack the plates
  reset_instruction: "Reset: stack the plates"
  initial_state_spec: initial-state.yaml
termination:
  min_steps: 5
  settle_steps: 3
  cooldown_steps: 5
reset:
  strategy: single_reset_policy
  max_attempts: 1
  on_unknown: reset
recording:
  rollout_root: JOBDIR/rollouts
  group: aeri
  forward_folder: stack__rehearsal-1
  reset_folder: reset_stack__rehearsal-1
```

The job file is a strict YAML subset: write mappings in block form as above (inline `{ … }` mappings are refused), and the run name has no dots. Create `JOBDIR/rollouts` before you start.

### Six commands

Each command is read-only or touches the fakes only.

| Command | What you should see | What it tells you |
| --- | --- | --- |
| `uv run levi automatic doctor --config JOBDIR/automatic-eval.yaml` | `ok` for the contract snapshots, the fakes, the job file, the launch check and the rollout folder | The machine and the job are ready for a dry run. Continue only when every line is `ok` |
| `uv run levi automatic validate --config JOBDIR/automatic-eval.yaml` | `valid: run rehearsal-1, 2 episodes, reset strategy single_reset_policy, …, plan <short hash>` | The job is accepted; the short hash identifies its frozen plan |
| `uv run levi automatic plan --config JOBDIR/automatic-eval.yaml --mode dry_run` | `launchable: yes`, and a `W_CONTRACT_DRAFT` warning: the initial state contract is still a draft | A draft contract is fine for a dry run; a person confirms it before a real run |
| `uv run levi automatic run --config JOBDIR/automatic-eval.yaml --dry-run --keep JOBDIR/kept` | `dry run (fakes only, no motion authority): COMPLETED, 12 transitions, 2 forward episodes, 0 resets, fake robot motions 24`, in about a second | The whole round loop ran end to end on the fakes |
| `uv run levi automatic status --run-dir JOBDIR/kept/.aeri/runs/rehearsal-1` | `rehearsal-1: COMPLETED, 35 lines, …` | Reads the run journal without writing to it |
| `uv run levi automatic report --run-dir JOBDIR/kept/.aeri/runs/rehearsal-1 --config JOBDIR/automatic-eval.yaml --format md` | A table: forward episodes 2, autonomous success rate 1.000 (2/2), early stops 2, saved steps 38, … | The metrics a real run reports |

The outputs above were recorded on 2026-10-15 with these exact files. The plan hash depends on the job, including its folder, so yours will differ.

### Reading the result

`JOBDIR/kept/.aeri/runs/rehearsal-1/state_journal.jsonl` is the run journal. Only the part that runs the robot writes it; the page and the vision model read it. The report's *autonomous* rates are the run's own verdicts, not ground truth: a person's blind labels (`uv run levi automatic label`, confirmed one by one at a terminal) are the truth the report compares against.

## 2 Your first real-robot run

> **Real-robot runs are on hold** until the terminal-only release lands in main. From then on only terminal D answers the scene question and releases a round; the page and the vision model only show what they see.

**Status.** The steps below are software tested with fakes and offline runs; hardware acceptance is pending. A real run needs a person at the robot with the emergency stop at hand, and is never started over HTTP.

1. Start the robot stack (arm control, gripper, robot server, health monitor) and the live service; write the job file, the binding and the scene contract, and have a person confirm the contract.
2. Run the read-only preflight `python scripts/aeri.py check --config JOB --binding BINDING`. It prints the plan and binding digests.
3. In one terminal (terminal D in figure 8), run `python scripts/aeri.py execute …` with the digests from step 2, and type the confirmation phrase it asks for. Only this process sends motion.
4. Each round: reset the scene, then answer whether it is ready in terminal D. The page shows the question; the answer that releases the round is typed in terminal D. On main the terminal asks for a typed `resume`; the one-key console (Enter, `y` / `n` / `u`) is on the integration branch.
5. Follow progress and label on the page. Stop from the page or with Ctrl+C in terminal D. A fault locks the run: read the reason in the journal and start a new run.

The details of each step, the error cases and the commands for stopping and resuming are in [Automatic evaluation pipeline](AUTOMATIC_PIPELINE.md); the README section [Running an AERI evaluation](../README.md#running-an-aeri-evaluation) has the same five steps next to figure 8.
