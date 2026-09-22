"""What LEVI keeps after a task, so the next task on the same data starts better.

Plan §6.6 and §10.2. A finished task leaves three things behind:

- a task ledger (``datasets/<name>/tasks/<run>/ledger.json``): the facts of
  one run -- scope, calls, time, tokens, quality warnings, what was published;
- the dataset's local memory (``workbench/memory/<name>.json``): one canonical
  record per dataset, updated in place from committed work, never a new
  article per run;
- improvement candidates (``workbench/improvements/<name>/<slug>.json``): one
  file per candidate, moved through the self-improvement state machine.

Everything here is deterministic. Closing a task makes no model call; a
candidate changes only harness parameters that a task reads at plan time, and
a running task keeps the parameters it was planned with.
"""
