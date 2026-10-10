# Automatic evaluation pipeline (AERI): contracts and journal

[中文](AUTOMATIC_PIPELINE.zh-CN.md)

**Status: runs on fakes only.** This page documents the integration
contracts (`levi/domain/aeri.py`), the durable journal
(`levi/automatic/journal.py`), the state machine and orchestrator
(`levi/automatic/state_machine.py`, `orchestrator.py`), the termination
arbiter (`termination.py`), the provider adapters
(`levi/automatic/adapters/`) and the in-process fakes
(`integrations/fr3_automatic/fake.py`). There is no real robot adapter, no
command and no page yet; nothing here moves a robot, starts a model or opens
a port, and nothing tested here counts as verified on the robot.

The pipeline joins three parts through versioned messages:

- **A** event intelligence proposes events and judges goals and scenes;
- **B** the performance runtime grants resources and serves policy chunks;
- **C** the automatic pipeline is the only part with motion authority and
  the only writer of the run journal.

## Contracts (`levi.aeri.*.v1`)

| Schema | From → to | Model(s) |
| --- | --- | --- |
| `levi.aeri.event.v1` | A → C | `EventProposal` |
| `levi.aeri.judgement.v1` | A → C | `Judgement` or `JudgementUnavailable` (field `kind`) |
| `levi.aeri.scene.v1` | A → C | `SceneAssessment` or `SceneUnavailable` |
| `levi.aeri.runtime.v1` | B ↔ A/C | `WorkloadSpec`, `ResourceLease`, `AdmissionRejected`, `ResourcePressure`, `PolicyHandle`, `ChunkRequest`, `ChunkResponse`, `QuiesceAck`, `RuntimeUnavailable` |
| `levi.aeri.run_event.v1` | C only | `RunEvent` (one journal line) |

Every message carries `schema`, `minor`, `run_id` and `emitted_wall_ns`
(wall clock, audit only, never compared).

**Reading a message.** Always use `aeri.parse(raw, contract)` (or
`aeri.validate(dict, contract)` for a message built in Python). It refuses,
with an `AeriError` whose `code` says why:

| Code | Cause |
| --- | --- |
| `E_TOO_LARGE` | more than 256 KiB |
| `E_JSON` | not JSON |
| `E_DUPLICATE_KEY` | a key appears twice in one object |
| `E_NONFINITE` | `NaN`, `Infinity`, or a number that overflows (`1e400`) |
| `E_CONTROL_FIELD` | a robot or reset command key anywhere in an A or B message (`robot_stop`, `execute_reset`, `go_home`, `resume`, `command`, any `robot_`/`execute_`/`cmd_`/`force_` prefix…), after Unicode normalisation: compatibility forms folded, zero-width, soft-hyphen and combining characters dropped, camelCase split, case folded, and spaces, `-`, `.`, `/`, `:` read as `_` (so `robotStop`, `robot.stop`, `ｒｏｂｏｔ＿ｓｔｏｐ` are all caught) |
| `E_SCHEMA` | wrong schema id, wrong type (strict: `true` is not `1`, `"1"` is not `1`), missing field, value out of range |
| `E_SCHEMA_TOO_NEW` | a newer `minor` than this reader knows (fail closed) |
| `E_UNKNOWN_FIELD` | a field the contract does not define, at any depth |
| `E_INCONSISTENT` | fields that contradict each other (below) |
| `E_SPEC_MISMATCH` | with `specs=`: a predicate the cited spec does not declare |

`model_validate_json` is not used: it silently keeps the last of two
duplicate keys. Pydantic's default (lax) mode turns `true` and `"1"` into
the integer 1; the AERI models are strict and refuse both. Tests pin both
behaviours.

**Three kinds of result, never mixed.** `decision: confirmed | rejected`;
`decision: unknown` (the evidence was read and does not settle it; it needs an
`unknown_reason`); and `kind: unavailable` (nothing was judged: gate closed,
busy, timeout, model error…). Neither "unknown" nor "unavailable" is ever a
success. Only `gate_closed`, `busy` and `admission_rejected` (with
`retry_after_ms`) may be retryable.

**Consistency rules (selection).** `confirmed` needs at least one required
predicate, all true, and no confirmed or undecided veto; `rejected` needs a
false required predicate or a confirmed veto. A scene is `ready` only when
every required predicate is true; `failed_predicates` and
`unknown_predicates` must list exactly the required predicates that are
false or unreadable. `valid_until_ns` is later than `produced_ns` and at
most `MAX_RESULT_VALIDITY_MS` (30 s) after it; a lease lasts at most
`MAX_LEASE_MS` (10 min). An episode id is `<run_id>.<forward|reset>.<NNNN>`
and must belong to the message's run. A policy endpoint is loopback only and
never names port 5000, 5001, 5100, 7470 or 8000. An episode result follows
its goal verification exactly: `verified` is `success`, `contradicted` is
`failure`, `undecided` and `unavailable` are `unknown` (not judged is never
counted as a failure). When a judgement carries the online judgement's own
values (`legacy_c5`), they must agree with the decision: an undecided answer
is `unknown` / `model_undecided` whatever its outcome (a success with an
undecided veto, a disputed exemption or a missing input is undecided), a
decided success `confirmed`, a decided failure `rejected`. The run event has no field for operator labels, and its
states are the 13 AERI states only. These cross-field rules are not
expressible in JSON Schema, so the snapshots cannot see them change: the
`E_INCONSISTENT` fixtures guard them.

**Clocks.** All `*_ns` fields of a message share its `clock_domain`, written
`host-mono:<boot_id>` for this machine's monotonic clock.
`aeri.check_fresh(message)` (a judgement, scene assessment or lease) reads
**the consumer's own clock** (`time.monotonic_ns()` and this host's domain;
tests may pass both `now_ns` and `local`, never one alone) and raises
`E_CLOCK_DOMAIN` for another clock domain (it cannot be compared, so it
counts as expired), `E_FUTURE` when `produced_ns`/`granted_ns` is more than
`FUTURE_TOLERANCE_NS` (100 ms) ahead of that clock, and `E_EXPIRED` once the
validity has passed. It is a necessary check, not a sufficient one: the run,
episode, epoch and request fences come on top. A chunk response's
`received_ns` is filled by the receiving side from its own clock, never by
the server, and is for audit only: whether a chunk met its deadline is
decided by `aeri.check_deadline(deadline)` on the receiver's clock (a passed
hard deadline raises `E_EXPIRED`, a soft one returns `False`).

**Versions.** A consumer accepts every `minor` up to its own and refuses a
newer one. A minor version may add optional fields, register event types,
loosen bounds or add control keys. Anything else (a removed field, a new
required field, any change to a closed enum, a type, a pattern or a default,
a shorter control-key list) needs a new major version.

## Schema snapshots

The models are the source. `uv run levi dev check-contracts` checks, besides
the older `docs/architecture/contracts.json`:

1. `docs/architecture/aeri/v1/<contract>.schema.json` against the models,
   byte for byte, spelling out every breaking difference;
2. the models against the snapshots **at `git merge-base HEAD <base>`**. The
   base is `--base <ref>` if given, else the `LEVI_CONTRACT_BASE`
   environment variable, else a local `main`, else `origin/main`; a named
   ref that does not resolve is not replaced by another. A breaking change
   committed together with its rewritten snapshot passes the first check;
   this one catches it, on the branch before the merge (on `main` itself it
   compares `main` with `main`). A base that cannot be read (no git, a
   source tree outside a checkout, no such ref, history too shallow for a
   merge base) fails the check instead of passing it, and says how to fix
   it. CI runs `levi dev check-contracts --base origin/main` with the full
   history fetched (`actions/checkout` leaves no local `main` on pull
   requests and tag pushes). A snapshot missing at the base is a new
   contract.

`aeri.RELEASED` is `False` while v1 is not released: until then a breaking
difference against the base is printed as a note and does not fail. Set it
to `True` when v1 is released; from then on it fails, and a breaking change
needs a new major version.

`uv run levi dev check-contracts --write` rewrites the snapshots (the AERI
schemas and `contracts.json` together: nothing is written when the AERI part
is refused). It refuses a change that is breaking within v1 unless
`--accept-breaking` is given; that flag is only for the time before v1 is
released (it is refused once `RELEASED` is set, and refused without
`--write`). The control-key list, the retryable codes, the forbidden ports
and the validity limits are part of each snapshot (`x-levi-*`), so a change
to them shows up in review.

Fixtures for every contract are in `tests/automatic/fixtures/<contract>/`
(`valid/`, and `invalid/` named `<case>.<ERROR_CODE>.json`); they are
generated by `tests/automatic/aeri_factory.py`.

## The run journal (`levi/automatic/journal.py`)

One run, one directory (`<rollout_root>/.aeri/runs/<run_id>/`, a dot folder
the live service does not scan):

| File | What it is |
| --- | --- |
| `state_journal.jsonl` | the only source of truth: append-only `levi.aeri.run_event.v1` lines, line 0 the run header (plan sha256, contract versions, LEVI commit), each line chained to the previous one by `prev_sha256` |
| `journal.lock` | the process identity of the single writer, which holds an `flock` on the run folder itself; a second writer is refused (`JournalBusy`), even if this file was removed, and the kernel releases the lock when the holder dies |
| `state.json` | a snapshot derived after each commit (temporary file, fsync, replace, fsync of the folder); never read to decide anything |
| `torn/` | bytes of a torn last line, kept before the file is cut back to whole lines |

**Durability.** Each line is written with one `write` and then `fsync`; the
folder is synced when the file is created. The live service's own JSON
writer (`levi/live/jsonio`) does not fsync and is not used.

**Transactions.** `prepared` is on disk before the caller acts;
`acknowledged` records what the controller reports (`yes`, `no`, `unknown`:
that it executed, not that the goal was reached); only `committed` changes
the state. A real action is committed only after `executed: yes`; anything
else is `aborted`. A `none` action needs no acknowledgement. One transaction
is open at a time, `from_state` is the current state, the control epoch
never goes back, and nothing is prepared after `COMPLETED`.

**Idempotency: rules for the orchestrator.** Transaction ids are unique.

- A **physical action** (`home`, `policy_steps`, a reset start included) is
  always `non_idempotent: true`; the contract refuses anything else.
- Every non-idempotent action carries a **`step`**, assigned by the
  orchestrator and **increasing per (episode, action kind)**. `step: null` on
  a non-idempotent action is refused by the contract; a step that is not
  higher than the last one prepared for that episode and kind is refused by
  the journal (even one never used).
- `idempotency_key = sha256(run_id, episode_id, kind, step)` (helper
  `aeri.action_key`), deliberately **without the control epoch**; the
  contract checks it on every prepared line. A key once prepared is refused
  for good, in every later epoch and after any recovery: the FR3 server
  cannot deduplicate a command, so a physical action is never sent twice.
- Two legitimate actions of the same kind in one episode (a second home)
  simply use the next step.
- **Retry after `executed: no`**: the controller reported that the action did
  not run, the transaction was aborted. A retry is a new logical command: a
  new (higher) step and `retry_of: <transaction id>` naming that closed
  transaction, of the same kind and episode. `retry_of` pointing at an
  action whose outcome was `yes`, `unknown` or never acknowledged is
  refused; an `unknown` outcome goes to `FAULT_LOCKED`, never to a retry.

`by_command(command_id)` returns what an operator command already did, and `expected_seq=` makes an append a
compare-and-set on the next line number.

**Reading back.** Only a last line that has no newline, or that cannot be
decoded as JSON, is **torn** (a crash: each line is one write followed by
fsync, so a crash leaves at most a line without its newline): it is ignored
and, when a writer reopens the journal, set aside in `torn/`. A whole line
that decodes but does not chain to the line before (an edit, which a crash
never produces; this is stricter than design note X1 §5), a whole chained
line that fails the contract (a newer `minor`, an unknown field, a broken
rule), a bad line anywhere before the last, or lines that break the
transaction rules make the journal **corrupt**: nothing is cut, it opens
read-only, is never written again, and both `Journal.state` and
`Scan.effective_state` say `FAULT_LOCKED`.

**Recovery.** `Journal.open(...)` then `recover(authority=<recovery
principal>)` after any restart. It never replays or sends anything:

- the run reached `COMPLETED`: nothing to do;
- a corrupt journal: reports `FAULT_LOCKED` / `journal_corrupt` and writes
  nothing;
- anything else (a dangling transaction, or a run stopped in any state,
  `WAIT_HUMAN` included): the dangling transaction is aborted with a
  `crash_before_commit` note, and a new transaction under a new control
  epoch moves the run to `FAULT_LOCKED` / `recovery_ambiguous`. The new
  epoch voids every motion token issued before the crash.

Leaving `FAULT_LOCKED` takes an operator's command: the journal accepts a
transaction out of it only from an operator with a `command_id`, only to
`PREFLIGHT`, and with a `none` action (a recovery may only re-enter
`FAULT_LOCKED`). A commit must keep the reason, episode and policy epoch it
prepared. Tests kill a child process with SIGKILL at each crash point
(`before_prepared`, `after_prepared`, `after_execute`,
`after_acknowledged`, `after_committed`) and cut the file at every byte;
recovery is consistent in every case and the stand-in robot command is
never repeated.

**Limits.** Only the run folder and its parent are synced at creation, not
every ancestor; the journal is never rotated while a run lasts (a run
writes a few lines per transition, not per control step).

## The state machine (`levi/automatic/state_machine.py`)

The normal path of a run:

```
PREFLIGHT -> VERIFY_INITIAL -> FORWARD_ACTIVE -> FORWARD_STOPPING -> FORWARD_FINALIZE
  -> ROBOT_HOME -> SCENE_ASSESS -> FORWARD_ACTIVE (next episode) ... -> COMPLETED
VERIFY_INITIAL / SCENE_ASSESS -> RESET_ACTIVE -> RESET_VERIFY -> RESET_FINALIZE
  -> VERIFY_INITIAL (home first, then a fresh initial-state check)
```

`TRANSITIONS` lists, for each allowed `(from, to)`, the reasons, the
principals (`orchestrator`, `operator`, `safety_guard`, `recovery`) and the
one action kind a transition may name; `check_transition` refuses anything
else (`E_ILLEGAL`, `E_REASON`, `E_AUTHORITY`, `E_ACTION`, `E_STEP`,
`E_EPISODE`, `E_RESULT`), and `check_journal` re-reads a whole journal
against the same table. The rules that matter most:

- motion states (`FORWARD_ACTIVE`, `RESET_ACTIVE`) are entered only from a
  scene decision (`VERIFY_INITIAL`, `SCENE_ASSESS`), by the orchestrator,
  with a `policy_steps` action;
- `policy_steps` and `home` are physical and not idempotent: the action must
  say `non_idempotent` and carry a `step` (`step=None` is refused). The
  orchestrator numbers the physical actions of an episode (0: its steps,
  1: its home) and never reuses an episode number, so the journal's
  idempotency key refuses a second attempt in any epoch, after any recovery;
- every live state may move to `FAULT_LOCKED` (orchestrator, safety guard or
  recovery). `WAIT_HUMAN` and `FAULT_LOCKED` are left only by an operator's
  command (`command_id`) to `PREFLIGHT`, never straight back to motion. An
  operator principal always names its command, on every rule;
- the home after a forward episode is `ROBOT_HOME -> SCENE_ASSESS` with the
  reason `robot_home_reached`; after an operator's stop the run may instead
  go `ROBOT_HOME -> WAIT_HUMAN` (`operator_stop`, the operator's command, no
  motion);
- the episode result is committed on `FORWARD_FINALIZE -> ROBOT_HOME`,
  `RESET_FINALIZE -> VERIFY_INITIAL | WAIT_HUMAN`, and on a move from an
  episode state to `FAULT_LOCKED` (an episode ended by a fault: its result
  says `task_outcome: unknown` and the fault as `stop_reason`, following
  pipeline §4.3 where the design X1 §2.5 had no result; the stop reasons
  gained `recorder_failed` and `home_failed` for this). An episode that ran
  no step discards its recording (`recorder_abort`).

**Motion fence.** `MotionFence` holds the one current `MotionToken`
(`run_id`, `transaction_id`, `control_epoch`, `kind`, `episode_id`,
`policy_epoch`, expiry on the orchestrator's clock). A robot adapter calls
`fence.check(token, kind, now)` before every command. Policy-step tokens are
issued after the commit into an active state; a home token between the home
transaction's prepared and acknowledged lines. The orchestrator revokes the
token before it checks or prepares any other transaction, so a late thread,
a late chunk or a refused transition cannot leave motion authorised.

## The orchestrator (`levi/automatic/orchestrator.py`)

`Orchestrator.create(run_dir, RunConfig, robot=, policy=, recorder=, events=,
verifier=, scene=, clock=, fence=)` starts a run; `run()` drives it until it
needs a person (`WAIT_HUMAN`, `FAULT_LOCKED`) or ends (`COMPLETED`). Every
state change is `prepare` (synced) -> act -> `acknowledge` -> `commit`;
`executed: no` aborts and moves to `WAIT_HUMAN` or `FAULT_LOCKED`,
`executed: unknown` always to `FAULT_LOCKED`. Nothing is retried
automatically.

- **Threads.** `run()` belongs to one thread; `stop()` and `resume()` may
  come from any thread. One re-entrant state lock serialises every "read the
  state, check, prepare, commit", so no transaction is prepared from a state
  other than the one it was checked against. `stop()` never waits for it:
  it registers the stop at once (in a registry the loop reads at every step
  and before every episode, under its own small lock) and returns
  `stop_requested`, even while an adapter call inside a transaction is slow
  or hung; only in `WAIT_HUMAN`, with the lock free within 50 ms, does it end
  the run itself (`completed`). `stop()` only registers: it does not hold
  the robot itself (that is the next safe point of the loop, or, when an
  adapter hangs, the robot side's watchdog and the token's expiry). A stop
  stays registered until the run reaches a person on it, where every
  registered stop is consumed (ids beyond the first are noted as
  `stop_commands_merged`). Each registration has a generation: a resume
  clears only the stops registered before it began (written as a
  `stop_command_lost` note); a stop that arrives during a resume survives
  it and takes effect at the next decision point.
- **Exceptions.** Any exception escaping the loop (an adapter, the journal)
  revokes the motion token first, then holds the robot, closes an open
  transaction as `executed: unknown`, moves the run to `FAULT_LOCKED`
  (`watchdog_timeout`) and only then propagates. When even that cannot be
  written, the orchestrator halts in memory (`halted`): it refuses to run or
  resume, and a restart (`restore`) recovers from the journal.
- **Notes.** Audit notes from the loop (dropped chunks or judgements, bad
  events) are counted in memory and written as one summary line per code at
  the next transaction boundary, at most `note_lines_per_episode` lines per
  episode (then one `notes_suppressed` line, and another at the end of
  `run()` for anything suppressed since); nothing is fsync'd per note inside
  the loop. Notes that explain a lock or a disputed verdict
  (`orchestrator_exception`, `hold_failed`, `motion_unacknowledged`,
  `recorder_error`, `early_stop_disputed`, `stop_command_lost`) are written
  beyond the budget. A hold, a quiesce or a move to `FAULT_LOCKED` acts
  first; the backlog of notes is written after it.
- **Inside an episode** the loop never waits for a judgement: requests are
  submitted and collected with a zero timeout, and withdrawn after
  `judge_request_timeout_ns`. A chunk is waited for at most its hard
  deadline and accepted only for the current policy epoch, request and
  episode, on time by C's own clock, with the right dimensions and
  `valid_from_action_index >= action_start_index`; anything else is dropped
  with a journal note (`chunk_dropped_*`). `chunk_failure_limit` failures in
  a row stop the episode (`policy_error`).
- **Safety.** A latched guard or a red light stops at once into
  `FAULT_LOCKED` (`safety_stop`, by `safety_guard`); a command the robot did
  not acknowledge (`unknown`) is `watchdog_timeout`; a recorder failure is
  `recorder_failed` and the rollout becomes `incomplete_*` with no
  `.complete` marker; a failed home is `home_failed`. Every home first
  passes the safety check (no latch, no red light), else `safety_stop`.
- **Scene.** Unknown or unavailable never skips a reset: with
  `on_scene_unknown = "reset"` (default) the reset policy runs, otherwise the
  run waits for a person; `max_reset_attempts` bounds the resets between
  two forward episodes. A reset that reaches its horizon is sealed, homed
  and waits for a person (`reset_horizon_exhausted`).
- **Results.** The forward result is committed before the home
  (`robot_home: not_attempted`, `scene_reset: unknown` at that moment);
  `task_outcome` follows `goal_verification` exactly, so unknown and
  unavailable are never a success. A final judgement is always asked once
  the policy is quiesced; an early stop (`goal_verified`) is a success only
  when that final judgement confirms it too, otherwise it is `undecided`
  (or `unavailable`) with an `early_stop_disputed` note. The final judgement
  must cover the end of the episode: its evidence from step 0 through the
  last step, observed no earlier than the request (after the quiesce);
  otherwise it is dropped (`judgement_dropped_stale`) and the goal is
  `unavailable`. Camera frames
  unchanged for `camera_stall_limit` observations, or a judge ignored for
  contract violations, make the final judgement `unavailable`. An episode
  counts towards `episodes` only once its rollout is sealed complete.
- **Stops.** A stop is checked before the scene assessment, after it,
  inside the transaction that opens the episode and at every step: a stop
  never opens a new episode. An episode the stop caught before its first
  step is discarded (`incomplete`, `unknown`), not counted and not homed;
  the run waits for a person. After a stop that interrupted steps, the arm
  is homed first (`home_after_operator_stop = true`, the design's path) or
  left where it stands (`false`) before the run waits for a person. Both
  rules hold for forward and reset episodes (`ROBOT_HOME -> WAIT_HUMAN`,
  `RESET_FINALIZE -> WAIT_HUMAN` without a home), and for a stop that
  arrives after the steps ended (during the quiesce, the final judgement or
  the seal).
- **Restart.** `Orchestrator.restore(run_dir, config, ...)` opens the
  journal and runs its recovery: every run that had not completed is
  `FAULT_LOCKED` (`recovery_ambiguous`); nothing is replayed. When the crash
  cut an episode short (the last committed state is inside an episode with
  no result), that move carries the episode's result: `task_outcome:
  unknown`, `stop_reason: orchestrator_crash`, the rollout `complete` only if
  its seal was committed, and `robot_home: failed` when a home was prepared
  and never committed (it may have run). Counters
  (episode numbers, policy epoch, completed episodes) are read back from
  the journal.
- **Operator commands.** `resume(command_id, expected_seq=,
  environment_handled=True, health_rechecked=True)` moves `WAIT_HUMAN` or
  `FAULT_LOCKED` to `PREFLIGHT`, where the run stays until `run()` is called
  again (a resume moves nothing by itself); repeating a command returns its first
  result (a command id another command used is refused), a stale
  `expected_seq` is refused. `stop(command_id)`: the same stop again, while
  pending (`stop_requested`, repeated) or after it took effect and before
  the next resume (`repeated`), changes nothing, so it never ends a run that
  is already waiting for a person; an id a resume used, or a stop id used
  before the last resume, is refused (`command_used`), and the stop must be
  sent again with a new id. `stop(command_id)` stops the
  current episode in a controlled way and waits at the next decision point;
  in `WAIT_HUMAN` it ends the run (`COMPLETED`).
- **AERI state on the client's session file (C2).** See
  `adapters/legacy_live.py` below.

`RunConfig` holds every parameter (episodes, horizons, timeouts, limits,
`termination`). The run's sequence numbers, steps and episode numbers come
from the journal, so a seeded run on fakes replays identically.

## The termination arbiter (`levi/automatic/termination.py`)

Four stages, never skipped: a **candidate** (an event of priority
`goal_candidate` or of a type in `goal_event_types`) -> **evidence** (one
request at a time, covering the candidate and `settle_steps` after it) ->
**confirmation** (a fresh `confirmed` judgement for this run, episode,
target and request, not overtaken by a retraction; `confirmations` in a
row) -> a **stop request** (`goal_verified`).

Never a stop: `unknown`, `unavailable`, a timeout, a judgement for another
run or episode, an unsolicited or repeated answer, an expired one, one from
another clock domain or produced in the future, one whose evidence ends
before the settling window, a contract violation. Each is dropped with a
note and the episode goes on to its horizon (`on_unknown:
continue_to_horizon`, the only v1 policy). A dropped answer withdraws its
request so the arbiter never waits for ever; after `violation_limit`
contract violations the judge is ignored for the episode (and gives no
final judgement), and after as many event contract violations the event
provider is cut off for the episode; either way the run waits for a person
at the next decision point (`contract_violation_limit`), before the end of
the run. A late event never moves the candidate back, and a confirmation
must cover the candidate (`observed_from_step` at or before it).

| `TerminationConfig` | Default | Meaning |
| --- | --- | --- |
| `allow_early_stop` | `true` | `false`: confirmations are recorded, never acted on |
| `min_steps` | 10 | no request before this step |
| `settle_steps` | 8 | the evidence reaches this many steps past the candidate |
| `cooldown_steps` | 15 | steps between two requests |
| `max_requests` | 6 | requests per episode |
| `confirmations` | 1 | confirmed judgements in a row for a stop |
| `control_fraction` | 0.0 | share of episodes without early stops (chosen from the episode id with `control_seed`), to measure false early stops |
| `goal_event_types` | `object_settled` | event types that make a candidate |
| `violation_limit` | 3 | contract violations before the judge is ignored |

The defaults are not calibrated on any data.

## Reset arbitration (`scene_assessment.py`, `reset_manager.py`)

**Only a `ready` scene with enough evidence skips a reset.** A scene
assessment is the provider's claim; `scene_assessment.arbitrate` reads it
against the task's **Initial State Contract** (`RunConfig.initial_state`):

| Assessment | Verdict |
| --- | --- |
| `ready`, every required predicate of the contract read true, at least `min_evidence_refs` frame or clip references (when `require_visible_evidence`) | `ready`: the next forward episode starts |
| `ready` that leaves out or does not read a required predicate | `unknown` (`scene_missing_predicate` note) |
| `ready` with too little visible evidence | `unknown` (`scene_insufficient_evidence` note) |
| an assessment of another contract | `unavailable` (`scene_contract_mismatch` note) |
| `reset_required`, `unknown`, unavailable | as they are |

Without a contract (`initial_state = None`, the default) the provider's
decision stands, as before. **The contract file is a draft (HA-23):** its
format below is the smallest one that carries pipeline §6.1 and waits for
the user's confirmation.

```yaml
initial_state:
  id: stack-plates-initial
  version: "1"
  status: draft            # draft until the user confirms (HA-23)
  robot:
    home_pose: fr3_safe_home
    gripper: open
  predicates:
    required: [object_at_source, gripper_open]
    optional: []
  observations:
    preferred: [side, wrist]
    require_visible_evidence: true
    min_evidence_refs: 1
```

`scene_assessment.load_contract(text)` reads it. The reader takes a strict
YAML subset (no new dependency): block mappings indented by spaces, lists
of scalars (`- a` or `[a, b]`), quoted or plain scalars, `true`/`false`/
`null`, finite numbers and `#` comments; tabs, anchors, aliases, tags,
block scalars, flow mappings, several documents and duplicate keys are
refused, and unknown keys too. A document that starts with `{` is read as
JSON. The predicate names are the scene provider's: the contract
registers `(id, version)` with exactly these names for `aeri.parse`.

**What happens when the scene is not ready** is the reset strategy's choice
(`RunConfig.reset_strategy`, `reset_manager.py`):

| Strategy | Not ready |
| --- | --- |
| `single_reset_policy` (default) | `reset_required` runs the reset policy; `unknown`/`unavailable` too with `on_scene_unknown = "reset"`, else a person; at most `max_reset_attempts` resets between two forward episodes, then a person |
| `human_assisted` | always a person (no reset policy runs) |

`atomic_skill_sequence` and `scripted_safe_reset` (pipeline §6.4) are
refused in v1. `reset_manager.check_plan` refuses a strategy that would
start a forward episode on any scene but `ready`, and `check_after` one that
would go on after a reset that reached its horizon, was stopped or lost its
policy. A reset at its horizon is sealed (the failed rollout is kept, its
result `failure` / `horizon_exhausted`), homed, and waits for a person; a
failed home locks the run (`home_failed`) and nothing moves again until an
operator's resume, which leads through `PREFLIGHT` and a fresh
initial-state check. A repeated resume (same command id) resumes once.

## Provider adapters (`levi/automatic/adapters/`)

**Fake providers (`events.py`).** `FakeEventStream`, `FakeGoalVerifier` and
`FakeSceneAssessor` hand over bytes of the contracts and are read with
`aeri.parse`; scripts set the decision (`confirmed`, `rejected`, `unknown`,
`ready`, `reset_required`), an unavailable code at submit or collect, a
delay, an expired, future or other-domain validity, a newer minor, a
control key, contradicting predicates, an unregistered predicate, another
episode or request. `make_request` refuses any request carrying operator or
evaluation keys or a robot command.

**Online judgement (C5) -> judgement (`legacy_live.py`, `from_c5`).** Only
public names of `levi.live.online` are read; nothing there is changed.

| C5 `status` | Condition | Result |
| --- | --- | --- |
| `ok` | `undecided` (also an undecided success) | `unknown` / `model_undecided` |
| `ok` | `outcome=success` | `confirmed`; when the answer fields do not both support it, `unknown` / `conflicting_predicates` |
| `ok` | `outcome=failure` | `rejected` (with a confirmed `c5-rule` veto when both fields look fine) |
| `unavailable` | `gate_closed`, `gate_pending`, `busy`, `service_busy` | `Unavailable`, retryable: a degradation, not an error |
| `unavailable` | `cold_start`, `vllm_starting`, `no_room`, `wake_failed`, `vllm_failed`, `shutting_down` | `Unavailable`, not retryable |
| `error` | `timeout:` | `Unavailable(timeout)`: a timeout is never `unknown` |
| `error` | `invalid_answer:`, `model_error:`, `internal_error:`, `invalid_request:` | `Unavailable(invalid_answer / model_error / provider_error / contract_violation)` |
| other | | `Unavailable(contract_violation)` |

The mapped judgement always keeps C5's own values (`legacy_c5`), and the
contract re-checks the mapping against them: a wrong mapping is returned as
`Unavailable(contract_violation)` (with the values in `detail`), never let
through without its audit copy.

**AERI state -> client session state (C2).** Only the client's seven states
are written; every state in which a policy may infer is `running`:

| AERI state | C2 state of the active role's file |
| --- | --- |
| `PREFLIGHT` | `standby` |
| `VERIFY_INITIAL`, `SCENE_ASSESS`, `WAIT_HUMAN`, `FORWARD_FINALIZE`, `RESET_VERIFY` | `waiting_reset` |
| `FORWARD_ACTIVE`, `FORWARD_STOPPING` | `running` (forward file) |
| `RESET_ACTIVE` | `running` (reset file; the forward file says `standby`) |
| `ROBOT_HOME`, `RESET_FINALIZE` | `homing` |
| `FAULT_LOCKED` | `fault` |
| `COMPLETED` | `finished` (`stopped` after an operator's stop) |

Tests feed every state to the live service's own gate (`gpumgr.gate`) and
cold-start guard: the gate closes for inference exactly while a policy may
infer. Writing `waiting_reset`, or any new state name, for the reset
policy's active state would open it.

## Recorder layer (`levi/automatic/recorder.py`)

`RolloutRecorder(root, run_id=, run_dir=, group=, texts=, media=)` writes
every episode as a rollout folder the live service already reads
(interface C1): `<root>/<group>/<task_folder>/demo_NNNN`, `NNNN` the
episode number, forward and reset episodes in their own task folders
(`RunConfig.forward_folder`, `reset_folder`). Give each run its own task
folders: a number already used in the folder (`demo_`, `incomplete_` or
`discarded_`) is never overwritten; the episode cannot open and the run
locks (`recorder_failed`).

**Sealing** keeps the live service's rule (`levi.live.criteria`): the
steps file (`aeri_steps.csv`) is synced, the media sink finishes,
`events.csv` gets its `episode_end` row and `metadata.json` its
`stopped_at`, `media_storage.video_frames_match_csv` and
`cameras.stall_detection.stalled`, each written whole and synced; both are
read back; **`.complete` is created last** (temporary file, fsync, rename,
fsync of the folder). A failed write anywhere before the marker raises, so
no marker is left on a rollout that is not whole; the run locks
(`recorder_failed`) and the folder becomes `incomplete_NNNN` with
`eval.abort_reason` (`fr3_fault` for a safety stop, which the live service
reads as an FR3 fault). Sealing twice returns the first result.

**Labels.** `eval.outcome` stays `unlabeled`, `eval.verdict_by` is `aeri`,
and there is no `eval.agent_label`: the automatic verdict is recorded in
the run journal only, never as an operator or agent label. `eval.aeri`
names the run and the episode.

**Run manifest** (`<run_dir>/manifest.json`, `levi.aeri.manifest.v1`):
every episode the run opened, its folder and state (`opening`, `open`,
`complete`, `incomplete`), a reset's `after_forward` and a forward
episode's `after_resets`. An entry is written before its folder exists. It
is a derived view; the journal stays the source of truth.

**After a restart** `Orchestrator.restore` calls `recorder.recover(...)`:
every rollout of this run whose seal the journal did not commit becomes
`incomplete_*` (`orchestrator_crash`), its marker removed if a crash came
between the marker and the commit, so a folder never says complete while
the journal says incomplete. Folders of other runs are never touched.

**Session files (C2).** `SessionFiles(root, run_id=, group=, folders=)`
is the orchestrator's `listener`: after every committed state it rewrites
both role files (`<root>/.eval_sessions/<group>__<task_folder>.json`)
with the client's seven states only (table above), ISO times,
`levi.reset_wait_s: null`, `levi.mode: unattended` on the forward file,
`levi.enabled: false` on the reset file, `episode_role` and
`aeri{run_id, state, control_epoch}`. A failed session write is a note
(`session_write_failed`), never a stop. `heartbeat()` rewrites the last
state (the live reader calls a session crashed after 10 s without an
update and with its process gone). Exclude the reset folder from the live
service (`watch.exclude`) so the forward spec never labels a reset.

**Media.** A `MediaSink` (`open`, `frame`, `finish`, `abort`) writes the
capture format (videos, pose and gripper CSVs) and reports its facts. The
default `NullMedia` writes no video.

## Metrics (`levi/automatic/metrics.py`)

`metrics.report(events, labels=, manifest=, termination=, max_steps=)`
reads a run journal (and the run manifest for step counts) and returns
three groups; every rate is `{"n", "of", "rate", "wilson95"}` with the
Wilson 95 % interval of the live service's statistics
(`levi.live.stats.wilson`). With the 20-30 episodes of an exploratory check
read the interval, not the rate.

| Group | Metrics |
| --- | --- |
| autonomous | forward episodes, outcomes, `autonomous_success_rate` (unknown stays in the denominator), stop reasons |
| early termination | confusion of early stops (`goal_verified`) against the truth; precision (early stops truly successful / early stops), recall (early stops / truly successful episodes), false early stop rate (early stops truly failed / truly failed episodes), saved steps (`max_steps` minus the steps run, summed over early stops), control episodes apart, agreement of the verdict with the truth and false successes |
| reset | resets, `autonomous_reset_success_rate`, scene decisions and skips; skip accuracy (a skip on a truly ready scene or a reset on a scene that truly needed one / labelled decisions), wrong-skip rate, unneeded-reset rate, reset durations |
| automation | interventions (moves into `WAIT_HUMAN` or `FAULT_LOCKED`) by reason, resumes, longest run of forward episodes without one, the time people waited (only when the clock domain did not change) |

**Four kinds of label, never mixed** (pipeline §9.4).
`autonomous_verdict` is the journal's own episode result: never written
anywhere else, never called ground truth (its rates are `autonomous_*`).
`posthoc_verdict`, `operator_label` and `adjudicated_ground_truth` live in
`<run_dir>/labels/<kind>.jsonl`, one append-only file per kind (each line
synced), written with `LabelStore.add(kind, episode_id, value, subject=,
by=)`. Adding a label never touches another kind's file; a second label of
the same kind, episode and subject is refused unless it says
`supersede=True`, and then it is appended (the first stays). `by` is an
opaque principal id (no names, no addresses). Subjects: `task_outcome`
(`success`/`failure`) and `initial_state` (`ready`/`reset_required`: did
the scene need a reset before the episode that started). The truth for the
rates is the adjudicated label where there is one, else the operator's
(`truth="adjudicated"` uses adjudicated labels only); episodes without one
are counted as `unlabeled` and left out.

## Fakes (`integrations/fr3_automatic/fake.py`)

`FakeClock` (moves only when advanced), `FakeRobot` (moves only under the
fence's token; scripted 503s that latch after three, stale states that
latch after six, frozen poses, red lights, latches, lost replies, a crash
after a command was sent, a home outside its tolerance, camera stalls),
`FakePolicy` (fixed latency on the fake clock; timeouts, server errors,
NaN, wrong dimensions or epoch, early `valid_from`, failing acquire or
quiesce, a crashed server) and `FakeRecorder` (a writer thread; a failed
write surfaces at the next commit or at the seal, `.complete` only after
every write, `abort` gives `incomplete_*`). The module imports no network,
process or LEVI code.

**How the fakes differ from the real robot** (`fake.FIDELITY`): no
dynamics or stopping distance; the home always ends in tolerance unless
scripted (the real `_go_home` does not check); an e-stop is visible to the
fake, while on the real FR3 the software staleness interlock does not see
it; the latch counters imitate `Fr3Guard` only in part; health is read in
the same step (the real C3 file is written at 2 Hz); no GPU contention or
cold start; `FakeRecorder` writes no files (`RolloutRecorder` does, but its
default media sink writes no video); camera frames are counters; there is
no network. **Passing these tests proves the
state-machine logic only, never behaviour on the robot.**

**Tests** (`tests/automatic/test_aeri_*.py`): the state table and the fence;
the fakes; the C5 mapping table and the C2 gate truthfulness; the arbiter;
the orchestrator on the fault list of the design (wrong success, unknown,
judge timeout or offline, flapping events, a late forward chunk in the reset
phase, a crashed policy server, camera stalls, no policy resources, a reset
at its horizon, a failed home, e-stop or FR3 fault, recorder write and seal
failures, restarts at every stage of every transaction and under SIGKILL,
a double resume), and 150 seeded random runs with crashes and resumes.

**Not yet:** the reset strategy beyond one reset policy and
`max_reset_attempts`, recorder files that `criteria.check` accepts and the
run manifest, renaming unsealed rollouts after a restart, metrics, the CLI
and the real FR3 adapter.

