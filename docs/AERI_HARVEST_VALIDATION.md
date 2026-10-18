# AERI native harvest: validation plan

[中文](AERI_HARVEST_VALIDATION.zh-CN.md)

## Status and scope

This is a validation plan, not a test report. All runtime checks below are **NOT RUN**.
The user confirmed that the robot is in use: do not execute or queue tests, type checks,
schema generation/checks, builds, browsers, services, GPU jobs or robot actions.
Static review, a parse check or an existing test function is not a passing regression run.

Source candidate: `020007fee9e89543025df5617cf9ee615aeda0c8`.
Comparison baseline: `bfdee2cf53365d8e0065b3f019e471213fe871fe`.
These identify the product code inspected for this plan; the documentation commit will be
a descendant. Later integrations require a new frozen candidate and a revised evidence manifest.

Local coordination source: `LEVI/lab/levi-hub/programs/levi2/NATIVE-HARVEST-20261018.md`.
Experiment sources: `LEVI/lab/aeri-external/REPORT-20261018.md`, `EXPERIENCE.md`,
and the permitted aggregate `LEVI/lab/aeri-1017-user-session/harness-opt/CHANGES.md`.
No private sessions, queues, held-out answers, source datasets or robot endpoints are
needed for the synthetic checks below. The experiment's model judgements are not human
labels; the external-mode result does not validate local-model quality or physical safety.

## Freeze evidence before execution

The integration owner records the complete candidate commit and tree hashes, baseline,
clean working-tree status, exact scoped diff, dependency lockfile hashes, environment
and tool versions, test selectors, resource lease, gate implementation revision, and
independent review reference. Resolve a branch name to a full SHA; never use a moving
branch name as the evidence identity. Record the actual checked-out revision again after
the run. Uncommitted source or fixture changes mean the evidence is not for the frozen SHA.

Each check needs its command/arguments, start/end times, raw log location and digest,
selected/collected/passed/failed/error/skipped counts, child exit code, wrapper/gate exit
code and interruption status. A skip, no collection, cancellation, timeout, incomplete log
or outer success masking an inner failure is not a pass. Review every required selector
and parametrization, not only a suite total. Retain failed attempts with their reason.
A new code/test/schema/dependency change invalidates the old claim for the new candidate;
old logs remain historical evidence. Re-run affected checks and justify unaffected scope
before claiming aggregate acceptance.

## Execution gates, after the current prohibition is lifted

Follow workspace `LEVI/lab/AGENTS.md`, hub `COORDINATION.md`,
`manual/robot-activity.md` and the current `heavy_gate.py` contract.
First obtain the user's confirmation that robot use has ended. Then the registered owner
must establish the existing stable quiet window and resource leases; unknown/busy is not
quiet. Use an isolated task worktree. Full suites require `full-test` plus its worktree
lease and run one at a time. Do not shorten the gate, remove a marker, bypass admission,
or submit an automatic retry queue while execution remains prohibited.

The heavy gate supports only its allowlisted restartable jobs. Its own header excludes
builds, services and GPU work: those require their separate authorization and scheduling.
An interrupted attempt is never validation evidence. Inspect both the gate result and
the recorded child result; a later successful shell command cannot repair an earlier failure.
The separate hub command-chain candidate is preventive tooling, not proof that a gate
was passed or that the historical in-flight release completed.

## Targeted Python matrix

Paths below are repository-relative. Each `file::function` names existing source at the
frozen candidate. These are anchor selectors; execute the relevant whole files as well
after targeted checks, because the listed cases are not exhaustive. All rows: **NOT RUN**.

| Area / production entry | Existing test anchors | Required invariant |
| --- | --- | --- |
| Gripper deadline; `levi/automatic/fr3_bridge.py` | `tests/automatic/test_fr3_real_gripack.py::test_confirmation_uses_sample_completion_deadline`; `test_unsteady_sample_at_deadline_is_refused_without_another_poll` in the same file | A valid completion at the boundary is handled correctly; late/unstable samples and stop/red remain refused. Default timeout is not silently changed. |
| Clamp diagnostics; same bridge | `tests/automatic/test_fr3_bridge.py::test_clamp_refusal_preserves_diagnostics_without_motion`; `test_guard_diagnostics_survive_journal_note_budget` | Diagnostic failure/budget exhaustion cannot permit motion or clear a latch. |
| Home settle and startup; `fr3_run.py`, `orchestrator.py` | `tests/automatic/test_fr3_real_home.py::test_home_at_target_cannot_succeed_with_concurrent_safety_failure`; `tests/automatic/test_startup_home.py::test_preflight_requires_fresh_confirmation_before_home`; `test_crash_at_home_transaction_never_replays` | Fresh confirmation precedes default-off startup home; crash/unknown execution does not replay motion; reaching target cannot override stop/red. |
| Startup through reverse human entry | `tests/automatic/test_startup_home.py::test_startup_then_reverse_human_resume_requires_new_release`; `test_startup_then_reverse_human_red_guard_locks_without_recovery` | Production execute with fake modules requires a new release after human reset; red guard never becomes an automatic recovery path. |
| Reverse request identity; `orchestrator.py`, `console.py` | `tests/automatic/test_reverse_human_entry.py::test_closed_old_window_cannot_stop_next_reverse_episode`; `test_failed_seal_closes_window_and_locks`; `test_resume_requires_an_independent_round_release` | Stale requests cannot stop a later episode; failed sealing locks; human request supplies no verdict. Soft stop is not an emergency stop or safe-approach proof. |
| Frozen settings and prompt contract; `subtask_loop.py`, `subtask_vlm.py` | `tests/automatic/test_aeri_subtask_job.py::test_unset_inventory_budget_preserves_the_exact_legacy_plan`; `tests/automatic/test_aeri_subtask_loop.py::test_plan_gripper_fact_contract`; `test_zero_provider_budget_keeps_gpu_and_headroom_guards` | Omitted settings retain the prior hash; target rules agree with validation; actual zero is distinct from unknown and does not bypass guards. |
| Truncation and adapter composition | `tests/automatic/test_fr3_subtask_entry.py::test_truncated_answers_wait_even_when_the_json_is_valid`; `test_extra_body_cannot_bypass_the_frozen_output_budget`; `test_external_queue_504_waits_and_late_answer_cannot_cross_requests` | Length termination preserves evidence and waits; late answers cannot cross request identity or bypass frozen budgets. |
| Native external bridge; `external_bridge.py` | `tests/automatic/test_external_bridge.py::test_publish_complete_unique_and_preserve_input`; `test_caller_budget_timeout_releases_slot_before_caller_deadline`; `test_invalid_report_does_not_change_a_valid_answer` | Complete publication, bounded lifetime/capacity, optional report isolation. No live server is required by these synthetic anchors. |
| External self-report; `external_report.py` | `tests/automatic/test_external_report.py::test_exact_utf8_binding_preserves_unknown_and_true_zero`; `test_client_revalidates_binding_without_promoting_self_report` | Request/answer binding and independent null/zero fields; self-report never becomes metered usage or human truth. |
| Report projection and labels; `request_evidence_view.py` | `tests/automatic/test_fr3_subtask_entry.py::test_optional_report_persists_privately_without_changing_standard_usage`; `tests/automatic/test_request_evidence_view.py::test_production_fake_orchestrator_and_label_writers_bind_each_request` | Label-gated projection uses exact round/member association; discard/retraction hides evidence. Temporary API location is substituted: this is not HTTP authentication or browser coverage. |
| Private evidence routing | `tests/test_dataset_asset_boundary.py::test_raw_dot_components_and_nested_private_runs_are_refused`; `test_asset_symlink_canonical_destination_must_remain_public`; `test_public_assets_keep_get_head_range_and_internal_symlinks` | Private AERI evidence cannot be reached through public assets; public serving behavior remains intact. |
| Refinement image cap; `levi/agent/observations.py` | `tests/test_harness_cost.py::test_a_split_rechecks_its_new_singleton`; `tests/test_events_planning.py::test_event_refinement_batches_respect_the_actual_request_image_cap` | New singleton is checked; coarser spacing retries from the beginning; all-invalid cases refuse; no rewrite of completed shards or candidate algorithm. |

Also cover the existing files `tests/automatic/test_aeri_contracts.py`,
`test_api_subtasks.py`, `test_subtask_units.py`, `test_fr3_real_startpose.py`,
`test_console.py`, `test_console_subtask.py`, `test_aeri_cli.py`,
`test_human_assisted_evidence.py`, and `tests/test_workbench.py`.
This includes unchanged permission and legacy behavior surrounding the changed paths.
Selecting only a new test is not evidence that its real caller still composes correctly.

## Frontend, Parquet, schema and documentation matrix

All rows: **NOT RUN**. No formatter, type checker, generated fixture, browser or schema
command was executed for this plan.

| Existing source/check | Acceptance |
| --- | --- |
| `src/components/automatic/__tests__/subtask-loop.test.tsx`: “each token keeps its own null and zero value (%s)”; “open evidence is removed on same-instance visible → hidden → missing updates (%s)” | Both EN/ZH parametrizations, token field independence, and complete removal of previously rendered evidence. Component fixtures are not production API-generated fixtures. |
| Same file: “the view has no control: no button, input, select or textarea”; “the API client has no write for a subtask or a round” | Read-only rendering does not acquire lifecycle or robot authority. |
| `src/utils/__tests__/parquetCache.test.ts`: “relocates once when an old firstIndex would clip a same-size rewrite to empty”; “a correct first row cannot conceal a wrong middle index” | Rebuild stale global indexes once; validate the complete requested interval. Preserve the lower-level row-offset clipping contract. |
| `src/app/[org]/[dataset]/[episode]/__tests__/fetch-data.test.ts`: “unordered shared file fallback excludes neighbouring charts, language and timestamps”; “cross-episode fallback validates selected ranges without requiring unsampled gaps” | Full shared-file fallback is legitimate only with verified per-episode global-index selection; missing/duplicate/unidentifiable rows refuse rather than leak neighbours. Unselected gaps need not be complete. |
| `package.json`: `type-check`, `lint`, `format:check`, `test`, `validate` | Both app and test tsconfigs must pass. Preserve the isolated test runner. Missing tools are blocked/unverified, not passed. |
| `levi dev check-contracts`; existing `tests/automatic/test_aeri_contracts.py` | Record the exact comparison baseline and effective command options; inspect schema drift against frozen plans. Static JSON parsing is insufficient. Do not silently regenerate snapshots to make a mismatch disappear. |
| `levi docs check`; bilingual links and examples | Commands, configuration and claims match the frozen source; no example grants robot permission. This new checklist does not certify the rest of the documentation. |
| Later browser/build stage | Separate approved gate. Verify real API-generated synthetic projection through the UI, locales, hide/retract lifecycle and absent fields; do not fabricate generated fixtures while execution is prohibited. |

## Five extension work items: separate evidence boundaries

These extensions were authorized after the source candidate above. They are **outside the
frozen matrix: independent candidates, later integrations, or design-only**. Display protection
was separately reviewed as d4f01aaa and integrated as 98c2ab6e after 020007fe; its runtime
tests are still NOT RUN. This records provenance, not validation of the later integration.
Record their actual reviewed SHAs and file/test inventory before adding them to a new
frozen candidate; this plan does not invent test selectors for unfinished work.

| Extension | Evidence to require / limit |
| --- | --- |
| Frozen-contract display protection | Preserve raw `FORMAT_STATUS`/`VIEW_RULE`, plan digest and old records; prove existing public projections do not expose internal wording. No new display module without an actual consumer. |
| Explicit sanitized log-export import | Bounded parsing of explicitly supplied sanitized exports with synthetic fixtures; reject ambiguous identity/usage, preserve provenance and unknowns. No reading real sessions/queues, automatic ingestion or invented operator labels. |
| No-progress detection design | Define observation freshness, state transitions, false positives and future tests; no control wiring or new default threshold. Manual reverse entry does not implement automatic detection. |
| Trusted policy declaration design | Specify trusted producer, exact identity/digest coverage and read-only consumer. No MODELREG resumption, training-data scan or capability inferred from experiment success. |
| Cross-module review and this validation plan | Freeze the reviewed SHA, retain findings and dispositions. The current combined review covers the external-evidence and startup/reverse chains, not the entire project. Static no-blocker review is not execution, integration, user acceptance or physical proof. |

Hub import-idempotency, sampler-bounds and command-chain branches remain separate hub
candidates. Product checks do not validate them; their own exact SHA, review and later
execution records are required. Do not import real experience records to validate a tool.

## Human and physical gates

Software acceptance requires the complete reviewed candidate, successful approved checks
and explicit residual limitations. Merge, deploy/restart and physical acceptance are
separate decisions; none follows from this document. Confirm no active real evaluation
before any service restart under the existing rules.

For a later explicitly approved robot session, a person reviews the frozen binding,
camera identity, scene/reachability, policy/task compatibility and stop procedure.
Validate default-off startup-home opt-in and fresh confirmation, independent round
release, measured home tolerance, gripper deadline behavior and clamp diagnostics without
relaxing the latch. Validate reverse human entry and red/stop priority with the person
present. A soft-stop acknowledgement does not prove it is safe to approach the arm.
Fault/unknown/partial motion does not authorize automatic homing or replay.

Operator labels must be supplied by a person through the native round-label workflow.
External model reports remain separate. Paired local/external quality, wording effects,
automatic reset reliability, no-progress thresholds and token/cost savings need their
own approved experiments; this candidate has no such result.

Post-baseline design provenance: no-progress 385f562c → 68c284c6; trusted policy
declarations 825ee844 → 3bb498bc. Both are design-only, with no executable implementation.

The independent aeri-startup-fault-chain tree contains
`tests/automatic/test_startup_home.py::test_startup_reverse_policy_identity_mismatch_waits_without_motion`
and `test_startup_reverse_session_heartbeat_failure_cannot_resume_or_home` in the same file.
Fault-chain candidate fd770b65 → integrated 94dc8be1 has fixed-SHA independent static review; NOT RUN and outside 020007fe. Policy mismatch expects WAIT_HUMAN/policy_error; session heartbeat is not driver-lease failure coverage.
Sanitized log-import candidate 6d77d5a4 → integrated 05010a49 also has fixed-SHA independent static review; NOT RUN. Neither integration extends the evidence of the 020007fe matrix automatically.
