# AERI no-progress observation design

[中文](AERI_NO_PROGRESS.zh-CN.md)

Status: design only, based on `020007fe`, task `T-C-NO-PROGRESS-DESIGN`.
No classifier, runner hook, configuration default, UI, or automatic robot
behaviour is implemented by this document. Tests below are specifications, not
executed tests. This does not complete the automatic `WAIT_HUMAN` acceptance of
`L2-AERI-EARLY-HUMAN-RESET`.

## Evidence and limits

The workspace aggregate `LEVI/lab/aeri-external/EXPERIENCE.md` §§2,4,5 and
`REPORT-20261018.md` §§1,3,6 distinguish unsuitable reset instructions, changed
container placement, repeated failed checks, guard interruptions, and human
interpretations. They motivate earlier assistance; they do not establish a
scene-distance threshold, reachability model, or detector accuracy. No sessions,
request queues, held-out examples, or source rollouts were read for this design.
The issue proposed two consecutive failed checks plus an unchanged scene. That
count is an existing product proposal, not a calibrated operating point; the
scene comparator and its thresholds remain unvalidated. Keep detection disabled
until a complete reviewed profile and evidence-backed acceptance are available.
Historical plans are context; the current source determines existing behaviour.

Relevant source at the stated base:

- `orchestrator.py`: `Episode`, `reverse_human_scope`,
  `reverse_human_request`, `_finish_reverse_subtask`, `_reverse_stop_guard`,
  `_next_subtask`. A false check allows a bounded retry; missing checks follow
  the existing unavailable path. A human request is scoped and is not a verdict.
- `adapters/signal_events.py`: `CachedObserver` caches existing observations;
  `SignalEvents` checks stream identity, serial, observation age, clock and measured
  state. `levi/events/stream.py` detects signal events, not task progress.
- `console.py`: the existing reverse human request uses a fresh scope. Acceptance
  does not mean motion stopped, home reached, or the arm is safe to approach.

These are source names under `levi/automatic/` unless otherwise qualified.

## Signals and false positives

| Input | Permitted inference | Not established / false positives |
| --- | --- | --- |
| Host episode, step, policy epoch, plan/subtask identity | Which execution produced evidence; whether an acknowledged step occurred | A step or elapsed time is not physical or semantic progress |
| Fresh measured pose/gripper with units and provenance | Activity or stillness over a valid measurement window | Holding, contact, slow motion, compliant grasp and settling can be useful; commands are not measurements |
| Fresh camera frames with capture identity and frozen camera/layout/calibration | Inputs for a separately validated scene comparator | Exposure, shadows, occlusion, camera movement and wrist motion can hide or imitate object change |
| Validated task-relative scene comparison | Changed / unchanged / unknown under its exact contract | Pixel equality is not evidence freshness; pixel difference is not goal progress or reachability |
| Completed subtask check | The check returned true / false / unavailable | Repeated false does not prove unreachable, unsafe or impossible; model answers are not human labels |
| Existing guard/fault/stop status | Suppress or invalidate an advisory | Detector silence cannot weaken a guard or certify physical safety |

No online semantic scene comparator with validated thresholds and a dedicated
read-only consumer is established here. Do not add a standalone classifier that
silently treats velocity, frame hashes, timeouts, or external agent notes as that
comparator. No extra observe call, model call, service, or robot poll is proposed.
Use existing retained, bounded evidence only; no arbitrary file paths or logs.

## Proposed pure interface and ownership

Future module (not created): `levi/automatic/no_progress.py`.

```python
observe(state, sample, *, profile, now_ns) -> (state, observation)
finish_attempt(state, result, *, profile, now_ns) -> (state, observation)
close(state, *, reason) -> (state, observation)
```

Pure deterministic functions: no clock reads, sleeping, I/O, adapter calls,
threads, commands or state-machine transitions. The host supplies a monotonic
clock and validates bounded data before construction. Reject booleans as integers,
nonfinite numbers, negative times, unbounded strings/collections and extra fields.
Limits belong to the reviewed profile/contract, not values inferred from a run.

| Record | Required fields and source |
| --- | --- |
| `scope` | Host-owned `run_id`, `round_id`, `episode_id`, `policy_epoch`, `loop=reverse`, `plan_sha256`, `subtask_id`, `attempt_id`, `process_generation`; no model-supplied association |
| `sample` | Scope, strictly increasing `serial`, acknowledged `step`, `observed_ns`, `received_ns`, `clock_domain`, `source_generation`, `quality`; optional measured activity and comparator result with retained evidence IDs/digests |
| `comparison` | `contract_id`, `version`, `profile_sha256`, exact baseline/current evidence IDs and capture identities, `relation=changed|unchanged|unknown`, reason code; absence means unknown |
| `result` | Scope, unique host completion ID, `check=true|false|unavailable`, finalized evidence references; explicit predecessor attempt ID only for a host-approved retry of the same subtask |
| `profile` | Frozen contract/version/hash, required sources, age/gap/window/sample bounds, comparator contract, failed-attempt criterion, bounded history limits; no automatic default thresholds |
| `observation` | `schema=levi.aeri.no_progress.v1`, scope, profile hash, state, reason codes, evidence IDs, eligible distinct sample/attempt counts, observed interval, `authority=advisory`, `safe_to_approach=null`; no stop command or task outcome |

`state` retains only bounded measurement summaries and completed-attempt summaries;
it never retains unbounded images or arbitrary model text. On overflow, reject the
new window as unknown instead of silently dropping adverse evidence. A future
journal record is a dedicated advisory note/sidecar, separate from judgement IDs,
operator labels and episode results. Do not expose raw evidence through existing
UI projections without their exact round/member visibility checks.

## Observation state machine

These names describe detector state, never runner states:

| State / event | Next state and output |
| --- | --- |
| No reviewed enabled profile | `disabled`; no suggestion |
| New valid scope | `collecting`; empty within-episode history |
| Missing, stale, duplicate-only, invalid, contradictory or incomparable evidence | `unknown` with reason; invalidate the candidate window, no suggestion |
| Sufficient valid evidence and comparator reports change | `collecting`, reset unchanged streak; do not declare success |
| Complete valid unchanged window plus eligible false completed attempts | `suspected`, emit one advisory for this scope/evidence window |
| More copies of the same evidence/result | No counter or duration increase, no new advisory |
| User acknowledges/dismisses advisory | Record advisory acknowledgement; no execution change; dismiss suppresses only this advisory, not future guards |
| Guard, stop, scope closure or terminal execution state | `closed`, invalidate advice; guard remains authoritative |

A sample can describe activity while a scene comparator reports unchanged; that
is not automatically contradictory because motion need not move the target.
Conflicting scene comparators or provenance invalidate that comparison. Missing
required sources break a streak; unknown is never converted to false or unchanged.
A timer alone cannot enter `suspected`. Fresh valid later evidence starts a new
window after unknown; previous partial windows do not resume.

Episode isolation is strict: new episode/epoch/process generation clears raw
history, timers, pending advisories and samples. Repeated-attempt detection is a
separate explicit reducer over finalized attempt summaries. It may compare two
episodes only when the host supplies the predecessor edge, same run/round/plan/
subtask and unchanged scene-comparison contract. It cannot infer lineage from
adjacent numbers, timestamps or provider identity. Manual scene handling, a new
plan, missing attempt, success or unavailable check breaks the failure streak.
Restart begins unknown; do not reconstruct runtime timers from old files.

Within a window, require one clock domain/generation and
`observed_ns <= received_ns <= now_ns`. Enforce profile age and inter-sample gap
bounds. Clock rollback, future observations, epoch changes or timestamp/serial
regression invalidate the window. Repeated polls of cached data cannot add time;
equal frame bytes with independently proven fresh capture may be valid scene
input, but a repeated frame ID is not a new sample. Different clock domains are
not subtracted. An async comparator result arriving after scope closure is ignored.

## Guard precedence and human confirmation

Physical guards and existing fault handling run independently of this observer.
Red health/latch, quiesce/seal failures and unproven existing safety conditions
retain their current fault paths; detector failure never catches or downgrades
them. An ordinary stop/human request or closed scope invalidates the advisory.
A late successful check cannot override an accepted stop. The observer has no
motion token, admission, home, resume, retry-budget or VLM power authority.

Future read-only wording: “Possible lack of reset progress; evidence: …; uncertain:
…. Review the scene. This is not confirmation that motion has stopped.” Do not say
“unreachable”, “reset failed”, “safe to approach”, or “you may move the object”.
If a person chooses the existing human-reset action, the terminal must fetch the
current `reverse_human_scope()` and submit the person's explicit request through
`reverse_human_request`, not replay a scope carried by an advisory. Never forge
`source=terminal`/`by=operator` from automatic detection. Existing quiesce/seal,
optional reviewed home, safety guards, `WAIT_HUMAN`, acknowledgement and separate
round release still apply. Acceptance allows an already admitted step to finish.
No elapsed quiet interval or detector state proves a physical hold or arrival.
Automatic transition to `WAIT_HUMAN` is a separate future safety change requiring
explicit authorization, interface review and physical acceptance; this design does
not grant it or widen the current reverse-only request window.

## Test contract for a later implementation

Do not run these tests until runtime validation is authorized. All new fixtures
must be synthetic, in-memory or temporary files, without network/GPU/real data.

| Proposed node / fixture | Required falsifiable assertion |
| --- | --- |
| `test_no_progress.py::test_missing_and_static_are_distinct` | Missing camera/state/comparator => unknown; repeated false without valid comparison never suggests; stationary hold alone never suggests |
| `test_no_progress.py::test_window_requires_distinct_fresh_samples` | Duplicate serial, frame ID or completion ID does not advance window/count; equal bytes with new verified capture is distinguished |
| `test_no_progress.py::test_clock_and_gap_boundaries` | Inject exact boundary, stale/future samples, rollback, cross-domain/generation and gap; no wall-clock sleeps; invalid windows cannot later recover their old duration |
| `test_no_progress.py::test_retry_lineage` | Explicit same-subtask predecessor permits summary aggregation; new round/episode samples stay isolated; missing edge, plan edit, human handling, success/unavailable reset streak |
| `test_no_progress.py::test_comparator_is_not_progress_truth` | Lighting/occlusion/wrist-motion/settling fixtures marked unknown by comparator contract cannot produce advice; changed resets streak without granting success |
| `test_no_progress.py::test_bounded_and_late` | Oversized/nonfinite/type-invalid payload rejected, history bounded; late asynchronous result after close ignored; new process cannot reuse old suggestion |
| `test_reverse_human_entry.py` with `subtask_rig.build` | Inject suspected/unknown advice during chunk/step/verify/check: zero changes in robot calls, homes, requests, stop generation, results and labels compared with advice disabled |
| Existing reverse scope tests plus explicit human action | Scoped acceptance only after human command, duplicates idempotent, old advice cannot stop next episode; guard/fault beats advice and accepted stop beats late check |

Reuse `tests/automatic/test_signal_events.py` fixtures `Clock`, `Robot` and its
existing age/identity/freshness tests; do not duplicate the observation adapter's
whole matrix in a pure reducer. Reuse reverse tests
`test_current_reverse_request_stops_without_a_verdict`,
`test_payload_identity_duplicates_and_stale_requests_do_not_register`,
`test_reverse_stop_safety_wins_even_when_no_home_is_due`, and
`test_resume_requires_an_independent_round_release` for the control boundary.
Existing tests cover scoped human stop and source freshness, not semantic scene
comparability, failure-streak lineage, or the advisory's noninterference.

Before implementing: choose a real read-only consumer, freeze the comparator and
source-quality contracts, obtain reviewed parameter provenance and define a
synthetic test oracle independent of the reducer. Before enabling: evaluate false
alerts and missed alerts separately with denominators and uncertainty on authorized
labelled data, preserving human/model label separation; no quality claim or chosen
threshold is supplied here. A future automatic action requires its own review and
human-approved physical validation even if advisory tests pass.
