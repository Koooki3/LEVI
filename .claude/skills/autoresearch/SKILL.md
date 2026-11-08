---
name: autoresearch
description: >-
  Run a measured experiment study in LEVI work: tune a harness, prompt, threshold or replay
  parameter against a fixed evaluator over several rounds, keep or discard each change, and
  confirm once on held-out data. Use when the user asks to optimise, tune, sweep, calibrate or
  "autoresearch" something that has a fixed evaluator and a number to move. Not for bug fixes,
  one-off edits or a single comparison, and never for anything that moves the real robot.
---

# autoresearch (LEVI)

For the maintainer's workspace (`LEVI/lab/...` paths are git-ignored lab files; "§0" and "§4" mean `LEVI/lab/AGENTS.md`). Method after Karpathy's autoresearch (fixed evaluator, one editable surface, baseline first, keep or discard, simplicity rule) and Orchestra's two loops (inner experiments, outer synthesis). Sources, pinned commits, what was left out and why: `ORIGIN.json`.

## Before the study (all of it, or do not start)

1. **Question, metric, guards.** One sentence; one primary metric with its direction, measured on a named **development gold** set; guard metrics with limits written first (wall time, local tokens, external tokens counted apart, a safety or regression count). Agreement with a teacher is a tune-time diagnostic only, never the primary or the confirm metric.
2. **Data split.** *Tune* (iterate on it) and *confirm* (the list of episode ids pinned by hash before the first run, opened once per study). Frozen, held-out and blind sets are not part of this loop (R9, R11). To check that an episode is not in one, compare its id with what `gold_tool.py --set frozen|heldout next` and `gold_tool_screws.py next` print (ids and status only); if still unsure, ask. Never read `.env`: the pool's held-out list lives there and is not visible to an agent shell.
3. **One editable surface** (a file or small directory) in its own worktree on a local branch `exp/<topic>-<date>`. For prompt or instruction files write each iteration as a new version file (`<name>.vK.md`) and record its name and hash in the run record (R13: a used instruction file is never edited). Kept changes land through a normal `feat/<topic>` worktree and review; an abandoned experiment branch is archived under `refs/archive/` (R21).
4. **Budget and stop rule, written down.** Maximum runs and wall time; external tokens (the user set no cap on 2026-11-06, so they are counted and reported, not limited: count every run with the last-usage rule, keep the running total in the round report and flag a run that is more than about twice its estimate). Stop at the first of: budget used up; 8 consecutive discards; two consecutive rounds whose best tune gain is below the noise floor (about 0.02 for a 0..1 ratio, an explicit equivalent otherwise; R10).
5. **Gates.** Never the real robot; a result that would change a real-robot client's defaults needs a person's review before it is adopted (§0). GPU: hold the lock (`driver.py run --resource gpu --resource worktree:<abs path>`), `nvidia-smi` first, no sharing while a real-robot policy server is in use or the arm controller is active, otherwise total VRAM at most about 85% (R4). The arm gate is `arm_guard.py` plus `robot_activity.py`. CPU evaluators run under `nice -n 19 taskset -c <cpus>`; a job of several cores or minutes that `heavy_gate.py` does not take (it takes pytest and bun jobs only) runs after the two probes and, with the controller active, only after asking. No installs; `UV_CACHE_DIR` and `BUN_INSTALL_CACHE_DIR` point into `LEVI/lab/cache`, not `~/.cache`.
6. **Shared-state writes take a lease** (`driver.py run --resource ledger`, per `manual/driver-commands.md`): the ledger, reports and progress.

## Baseline

Run the unchanged baseline k times (k = 3 unless the evaluator is deterministic) and take the noise floor from the spread. For a LEVI annotation evaluation pair it with the no-LEVI direct call (R13).

## Inner loop (tune data only)

One hypothesis per iteration:

1. Write the hypothesis and the expected sign before editing.
2. Edit the surface only; commit small (`exp: ...`).
3. Run the evaluator with the output redirected to a log and read only its summary lines. A typo-level crash: fix and rerun once; otherwise log `crash` and move on.
4. Append a row to `results.tsv` under `<worktree>/.state/tmp/` (git-ignored; format in `references/round-record.md`).
5. **Keep** when the primary metric improves by more than the noise floor and no guard got worse beyond its limit. **Discard** with `git revert --no-edit <the exp commit>`: history stays, nothing is reset or rewritten, no force.
6. Simplicity: the same result with less code is a win; a tiny gain bought with ugly complexity is a discard.

When the knobs are few and discrete, replace hill climbing by one pre-registered sweep on tune (the grid written first), pick the best subject to the guards, then confirm.

## Outer loop (about every 5 iterations, and at the end)

Synthesize: what moved the metric, what did not, what the discards share; revise or drop hypotheses. Reusable lessons become candidates for dataset memory or `levi/knowledge` (a person commits them, only after the confirm run; `knowledge promote` refuses text that contains times or ranges). Nothing learned from confirm data feeds back into tuning.

## End of the study

Freeze the surface, open confirm once, and write the round report in Chinese under `levi-hub/reports/` (question, baseline and noise floor, best kept change, confirm result with an interval, cost table, discards, what is unverified, reproduce commands). Update the ledger (`hub.py issue add` with its five required fields when none exists, else `issue set`; `progress set`; R14 for evaluation rounds), clean up (R8) and hand the result to the user: developed, reviewed and accepted are three states (§0).

## Teacher and student

When the study compares an external agent (the teacher) with the local model plus harness, score against the human gold where it exists, otherwise against a calibrated panel of independent agent reviewers whose agreement with human gold was measured on the overlap. The teacher and the coordinator do not see confirm content: reference answers for confirm come from fresh, blind answerers and only aggregate scores reach the coordinator. The three-way table (teacher, local with harness, local direct) shows aggregates only on confirm.

## Do not

- Run without a budget, or "forever".
- Ask the user for what §0 and §4 do not reserve to a person; decide it yourself and note why.
- Create background schedules or unattended agents unless the user asks for it in this session, then follow §4.
- Report a gain inside the noise floor, or one measured on tune data, as a result.

References: `references/round-record.md` (record format, report outline), `references/evals.md` (how this skill is checked).
