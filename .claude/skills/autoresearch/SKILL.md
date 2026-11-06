---
name: autoresearch
description: Run a disciplined, measured experiment loop in LEVI work: tune a harness, prompt, threshold or replay parameter against a fixed evaluator, keep or discard each change, and confirm once on held-out data. Use when the user asks to optimise, tune, iterate on, ablate or "autoresearch" something that has a number to move. Not for bug fixes without a metric, one-off edits, or anything that moves the real robot.
---

# autoresearch (LEVI)

Method after Karpathy's autoresearch (a fixed evaluator, one editable surface, baseline first, keep or discard, a simplicity rule) and the two-loop structure of Orchestra's autoresearch skill (inner experiments, outer synthesis). Sources, pinned commits and what was deliberately left out: `ORIGIN.json`. This is not the upstream loop: it has a budget and a stop rule, it asks where the workspace rules reserve a decision to a person, and it never rewrites history.

## Before the first run (all of it, or do not start)

1. **Question and metric.** One sentence, one primary metric with its direction, a noise floor (the smallest difference worth keeping, taken from repeated runs or a paired interval), and the cost metrics reported next to it: wall time, local tokens, external tokens (counted apart, last usage line per message id).
2. **Fixed evaluator, read-only.** The scorer, the data split and the decoding are not part of the surface. Split the data into *tune* (iterate on it) and *confirm* (hash pinned before the first run, opened once per round). Frozen, held-out and blind sets are never used (R9, R11); when unsure whether a set qualifies, check with `takeover.py --overlap` and the pool's held-out loader instead of guessing. The coordinator and the teacher do not look at confirm content.
3. **One editable surface** (a file or a small directory) in its own worktree, on a branch `exp/<topic>-<date>`. Nothing else is edited.
4. **Budget and stop rule, written down first**: maximum runs, wall time, external tokens (the user sets the external token budget; none set means no external runs), and stop at the first of: two consecutive rounds with a confirm gain below about 0.02 (R10), or 8 consecutive discards.
5. **Gates.** Never the real robot. A GPU run holds the GPU lock (`driver.py run --resource gpu`), looks at `nvidia-smi` first, and needs `arm_guard.py` and `robot_activity.py` both at exit 0 (with the arm controller active nothing is shared). Heavy checks go through `heavy_gate.py`. No installs; caches live in `LEVI/lab/cache`, not `~/.cache`.
6. **Register runs the R13 way**: one `experiments.py` record per run, and every LEVI run paired with the no-LEVI direct call on the same model, decoding, episodes and scoring.

## Inner loop (tune data only)

Run the baseline unchanged first. Then, one hypothesis per iteration:

1. Write the hypothesis and the expected sign before editing.
2. Edit the surface only; commit small (`exp: ...`).
3. Run the evaluator with the output redirected to a log and read only its summary lines. A crash caused by a typo: fix and rerun once; otherwise log `crash` and move on.
4. Append one row to `results.tsv` (untracked, format in `references/round-record.md`).
5. **Keep** when the primary metric improves by more than the noise floor and no cost or guard metric got worse beyond its stated limit. **Discard** otherwise by restoring the surface from the last kept commit (`git restore --source=<kept> -- <surface>`). Never `git reset --hard`, never rewrite or force history; failed experiments stay as commits on the experiment branch.
6. Simplicity rule: the same result with less code is a win; a tiny gain bought with ugly complexity is a discard.

## Outer loop (about every 5 iterations, and at the end)

Synthesize: what moved the metric, what did not, what the discards have in common. Revise or drop hypotheses. Reusable lessons become candidates for dataset memory or `levi/knowledge`; a person commits them, and only after the confirm run. Nothing learned from confirm data may feed back into tuning.

## End of a round

Freeze the surface, run confirm once, and write the round report in Chinese under `levi-hub/reports/`: question, baseline, best kept change, confirm result with an interval, the cost table (three-way for a teacher/student comparison), what was discarded, what is unverified. Update the ledger (`hub.py issue set`, `hub.py progress set`), clean up (R8, `janitor.sh after <scope>`), and hand the result to the user: development done, reviewed and accepted are three states.

## Do not

- Run without a budget or "forever", or ask the user things the rules do not reserve to a person (real robot, a step no plan contains, sensitive files, budgets).
- Create background schedules or unattended agents.
- Treat a teacher (external agent) answer as gold; score against the human gold standard.
- Report a gain that is inside the noise floor, or measured on tune data, as a result.

References: `references/round-record.md` (record format and report outline), `references/evals.md` (how this skill is checked).
