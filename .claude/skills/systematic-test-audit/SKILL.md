---
name: systematic-test-audit
description: Use whenever modifying, optimizing, adding, changing, removing, or refactoring any part of LEVI and the work calls for running systematic tests (not a one-line, obviously-safe edit). Pairs the test run with an active audit for bugs, logic errors/conflicts, and thread/process/concurrency-management defects — not just "did the tests I already had pass."
---

# LEVI systematic test + defect audit

This repository's own history gives concrete reasons for this skill: a
conversion-pipeline validation check that would have rejected the majority of
real capture data (`success_flag`) went unnoticed until real data was run
through it; an mtime-based cache-invalidation scheme for multi-process
annotation sync was provably wrong under real concurrent writes; a test
fixture silently leaked real directories into the live workspace on every
run because only half its path resolution was isolated. None of these were
caught by "do the existing tests still pass" — each needed someone to
actively look for the failure mode, not just confirm the absence of a
regression.

When a change is more than a trivial, obviously-isolated edit, do not stop
at running the existing suite. Before declaring the work done:

## 1. Run the real checks, not just the fast ones

- `uv run levi check` (frontend validate + `ruff check .`) and
  `uv run pytest --basetemp=.state/tmp/build/pytest -q` — both, every time
  Python or TypeScript/React source changed, not just the side that was
  edited (they share contracts: pydantic `Options`, the annotation atom
  schema, the `.state` layout).
- If the change touches the conversion pipeline, annotation storage, or
  anything reading/writing `.state`, run it against real or realistic data
  where practical, not only the synthetic pytest fixtures — synthetic
  fixtures are small and clean by construction and have already been shown
  in this repo to pass while real data fails (fps mismatches, schema
  variants, scale).

## 2. Actively hunt for these specific failure classes, don't just wait to trip over them

- **Concurrency and threading**: anything touching `levi/jobs.py`'s worker
  pool, `backend/app.py`'s per-dataset in-memory `_states` cache, or any
  file read/write that could happen from two LEVI processes sharing one
  `LEVI_WORKSPACE` at once. Ask: what happens if two callers hit this at the
  same instant? Does a cache ever get trusted past the point where another
  writer could have invalidated it? If a fix relies on timestamps
  (mtime, `datetime.now()`) to detect "did something change," verify the
  actual granularity empirically on this filesystem before trusting it —
  don't assume sub-second resolution is fine.
- **Logic errors and silent scope mismatches**: default parameter values,
  schema assumptions (a field that means one thing for one data source and
  something else — or nothing — for another), and anything that "should
  never happen" but isn't actually validated. When two similar-looking data
  sources or code paths exist (e.g. two capture-collector schemas, two
  dataset versions), check the assumption holds for *both*, not just the one
  originally in front of you.
- **Test isolation**: does every path a test touches actually resolve inside
  that test's own `tmp_path`, or does part of it fall through to a module
  global (`ROOT`, `STATE`, a client, a cache) that was never monkeypatched?
  A passing test that quietly wrote into the real, live workspace is worse
  than a failing one — it hides the bug and pollutes the environment for
  everyone after.
- **Workspace/filesystem invariants**: anything that creates, moves, or
  names a directory under `.state` — confirm it matches the conventions in
  [`.state.md`](../../.state.md) (flat layout, no incidental subfolders,
  per-dataset artifacts named by catalog name and per-run artifacts by
  timestamp — never a hash) and doesn't leave orphaned or half-written state
  behind on failure (outputs are staged and renamed into place).

## 3. Fix what's found, then re-verify

Findings from this audit get fixed in the same pass, not deferred — then
the full check in step 1 runs again to confirm the fix didn't introduce a
new regression. If a defect is found but deliberately not fixed (e.g. a
known, documented trade-off like `levi/maintenance.py`'s narrow cleanup
allowlist), say so explicitly and record the reasoning rather than silently
dropping it.
