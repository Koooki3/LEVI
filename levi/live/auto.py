"""The automatic approver: the one place LEVI lets software approve and commit.

In every other LEVI workspace only a person approves a plan and commits
annotations; nothing here changes that. The live service annotates rollouts
while nobody is at the screen, so its worker acts as ``live-auto``:

- **Opt-in twice.** The supervisor passes ``LEVI_LIVE_AUTO_APPROVE=1`` to its
  worker only when started with ``--auto-approve`` (or ``pipeline.auto_approve``),
  and the workspace must carry ``live/workspace.json``, which only
  ``levi live`` writes. A principal built from an HTTP request is never
  ``auto``, so neither the UI nor a connected agent can become it.
- **Only its own runs.** It may approve, run and commit a run or changeset
  that ``live-auto`` itself planned; a person's plan or draft in the same
  workspace is refused.
- **A short list of calls** (``ALLOWED``): no resets, no cleaning, no
  publishing improvements, no pilot review (its plans waive the pilot in the
  plan, which approving covers).
- **Stamped and audited.** Its approvals carry ``reviewer_type: auto``, the
  segments it commits carry ``levi.review: auto``, and every state-changing
  call it makes is appended to ``<workspace>/live/audit.jsonl`` when allowed
  and again with its outcome (completed or failed); refusals too. Pure reads
  (``runs.get``, ``runs.events``, ``changes.diff``, ``changes.validate``, ``anchored.get``) are not
  logged.
- **No outcome labels.** Committing its changes never writes a human outcome
  label: the automatic verdict stays an anchored-review record (see
  ``worker.py``). The training pool therefore never sees it as ground truth.

Standard library only apart from the ``Principal`` it returns.
"""

import os
import time

from . import jsonio

ENABLE_ENV = "LEVI_LIVE_AUTO_APPROVE"
PRINCIPAL_ID = "live-auto"
MARKER = "workspace.json"
AUDIT = "audit.jsonl"
AUDIT_MAX_BYTES = 5 * 1024 * 1024

ALLOWED = frozenset(
    {
        "runs.plan",
        "plans.approve",
        "runs.execute",
        "runs.resume",
        "runs.pause",
        "runs.cancel",
        "runs.get",
        "runs.events",
        "changes.diff",
        "changes.validate",
        "changes.approve",
        "changes.commit",
        "anchored.get",
    }
)
# Every call except the pure reads is audited when allowed and again with its
# outcome (completed / failed); refusals are always audited.
READS = frozenset(
    {"runs.get", "runs.events", "changes.diff", "changes.validate", "anchored.get"}
)
AUDITED = ALLOWED - READS
# Calls that act on an existing run: the run must be the approver's own.
OWN_RUN_ONLY = frozenset(
    {
        "plans.approve",
        "runs.execute",
        "runs.resume",
        "runs.pause",
        "runs.cancel",
        "changes.approve",
        "changes.commit",
    }
)
# Calls on a changeset: only the subtask-segment drafts of a plain temporal run
# may be approved or committed by the approver (see ``_segments_only``).
CHANGESET_CALLS = frozenset({"changes.approve", "changes.commit"})
_BRIEF = ("run_id", "changeset_id", "revision", "repo_id", "episodes", "pilot")


def live_dir():
    from levi.paths import ROOT

    return ROOT / "live"


def marker_present() -> bool:
    return (live_dir() / MARKER).is_file()


def write_marker(directory, detail=None):
    """Declare a workspace to be a live workspace (``levi live`` only)."""
    jsonio.write(
        directory / MARKER,
        {
            "schema": "levi.live.workspace.v1",
            "created_at": time.time(),
            **(detail or {}),
        },
        indent=1,
    )


def enabled() -> bool:
    return os.environ.get(ENABLE_ENV) == "1" and marker_present()


def principal():
    """The automatic approver, or PermissionError when it is not switched on
    for this process and workspace."""
    from levi.agent.security import Principal

    if not enabled():
        raise PermissionError(
            "The automatic approver is off: start the live service with "
            "--auto-approve in a live workspace"
        )
    return Principal(PRINCIPAL_ID, human=True, auto=True)


def audit(record: dict):
    jsonio.append_line(
        live_dir() / AUDIT,
        {"time": time.time(), "principal": PRINCIPAL_ID, **record},
        max_bytes=AUDIT_MAX_BYTES,
    )


def brief(arguments: dict) -> dict:
    out = {}
    for key in _BRIEF:
        if key in arguments:
            value = arguments[key]
            if isinstance(value, list):
                value = value[:50]
            out[key] = value
    return out


def _segments_only(store, run, changeset_id):
    """The approver may approve and commit subtask segments of a temporal run,
    nothing else. An anchored review proposes an *outcome*, whose commit writes
    a human outcome label (``annotations/outcomes``): that is a person's call
    (D5), however the run came about."""
    from levi.agent.formats import ANNOTATIONS

    flow = (run.get("context") or {}).get("workflow") or {}
    if flow.get("kind") != "temporal" or flow.get("anchored"):
        raise PermissionError(
            "The automatic approver may approve and commit only subtask "
            "segments of a temporal run, never a review or an outcome"
        )
    change = store.get("changes", changeset_id)
    for proposal in change.get("proposals") or []:
        kind = ANNOTATIONS.get(proposal.get("kind"))
        if kind is None or kind.layer != "language":
            raise PermissionError(
                f"The automatic approver may not approve or commit a "
                f"{proposal.get('kind')!r} proposal"
            )


def authorize(workbench, name: str, arguments: dict):
    """Refuse (PermissionError) unless this call is one the approver may make
    now; records the decision either way."""
    record = {"tool": name, **brief(arguments)}
    try:
        if not enabled():
            raise PermissionError(
                "The automatic approver is off in this process or workspace"
            )
        if name not in ALLOWED:
            raise PermissionError(f"The automatic approver may not call {name}")
        if name in OWN_RUN_ONLY:
            store = workbench.store
            run_id = arguments.get("run_id")
            if run_id is None and arguments.get("changeset_id"):
                run_id = store.get("changes", arguments["changeset_id"])["run_id"]
            run = store.get("runs", run_id)
            if run.get("principal") != PRINCIPAL_ID:
                raise PermissionError(
                    "The automatic approver acts only on runs it planned itself"
                )
            if name in CHANGESET_CALLS:
                _segments_only(store, run, arguments["changeset_id"])
    except PermissionError as exc:
        audit({**record, "decision": "refused", "reason": str(exc)})
        raise
    except KeyError as exc:
        audit({**record, "decision": "refused", "reason": f"unknown record {exc}"})
        raise
    if name in AUDITED:
        audit({**record, "decision": "allowed"})


def record_result(name: str, arguments: dict, error=None):
    """What an audited call came to (called by the dispatcher after it ran)."""
    if name in AUDITED:
        audit(
            {
                "tool": name,
                **brief(arguments),
                "decision": "failed" if error else "completed",
                **(
                    {"error": f"{type(error).__name__}: {error}"[:300]} if error else {}
                ),
            }
        )
