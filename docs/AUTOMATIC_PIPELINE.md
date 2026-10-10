# Automatic evaluation pipeline (AERI): contracts and journal

[中文](AUTOMATIC_PIPELINE.zh-CN.md)

**Status: foundation only.** This page documents the two pieces that exist:
the integration contracts (`levi/domain/aeri.py`) and the durable journal
primitive (`levi/automatic/journal.py`). There is no orchestrator, no state
machine, no robot adapter and no command yet; nothing here moves a robot,
starts a model or opens a port. Everything is tested with fakes only.

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
is `unknown` / `model_undecided`, a decided success `confirmed`, a decided
failure `rejected`. The run event has no field for operator labels, and its
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
2. the models against the snapshots **at `git merge-base HEAD main`**
   (`--base` picks another branch). A breaking change committed together
   with its rewritten snapshot passes the first check; this one catches it.
   A base that cannot be read (no git, not the top of a checkout, unknown
   branch, history too shallow for a merge base) fails the check instead of
   passing it, so CI must fetch `main` with its history. A snapshot missing
   at the base is a new contract.

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

**Idempotency.** Transaction ids are unique. An action's `idempotency_key`
is `sha256(run_id, episode_id, kind, step)`, deliberately without the control
epoch: a non-idempotent action (a home, a reset start, policy steps) whose key
was ever prepared is refused for good, in every later epoch and after any
recovery (the FR3 server cannot deduplicate a command). `by_command(command_id)` returns
what an operator command already did, and `expected_seq=` makes an append a
compare-and-set on the next line number.

**Reading back.** Only a last line that has no newline, cannot be decoded as
JSON or does not chain to the line before is **torn** (a crash): it is
ignored and, when a writer reopens the journal, set aside in `torn/`. A whole,
chained line that fails the contract (a newer `minor`, an unknown field, a
broken rule), a bad line anywhere before the last, or lines that break the
transaction rules (an edited file) make the journal **corrupt**: nothing is
cut, it opens read-only, is never written again, and its `effective_state`
is `FAULT_LOCKED`.

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
