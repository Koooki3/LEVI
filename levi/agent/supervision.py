"""External teacher gate for existing model annotation phases.

Teacher feedback is a proposal correction, never plan/pilot/commit approval.
The model result and settled cost are retained before waiting, so resuming does
not repeat an already completed model request.
"""

import time

from .schema import ModelOutput, TaskContext
from .store import Conflict, digest


class AwaitingTeacher(ValueError):
    pass


def require_teacher(store, context, run_id=None):
    if context.supervision == "none":
        return
    if not context.teacher_grant or not context.allow_media_egress:
        raise ValueError(
            "Supervision requires a selected teacher connection and explicit evidence-sharing consent"
        )
    try:
        grant = store.get("grants", context.teacher_grant)
    except KeyError:
        raise ValueError("Teacher connection is unavailable") from None
    if not grant["enabled"] or (
        grant.get("expires") and grant["expires"] <= time.time()
    ):
        raise AwaitingTeacher(
            "Teacher connection is disconnected or expired; execution remains paused"
        )
    if context.repo_id not in grant["datasets"] or (
        grant.get("run_id") and grant["run_id"] != run_id
    ):
        raise PermissionError("Teacher connection does not cover this task")


def gate(
    wb,
    run_id,
    context,
    summary,
    evidence,
    phase,
    fingerprint,
    output,
    error=None,
    raw=None,
):
    if context.supervision == "none":
        return output
    require_teacher(wb.store, context, run_id)
    key = f"{run_id}:{summary['episode_index']}:{phase}"
    with wb.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            record = wb.store.get("teaching", key)
        except KeyError:
            record = None
        if record and record["fingerprint"] != fingerprint:
            # What the learner was shown changed (the harness moved on while
            # this run waited): the old review no longer applies. Keep it,
            # marked superseded, and ask again for what is shown now.
            stamp = time.strftime("%Y%m%dT%H%M%S")
            wb.store.save(
                db,
                "teaching",
                f"{key}:superseded-{stamp}",
                {**record, "status": "superseded", "superseded_at": time.time()},
            )
            wb.store.event(
                run_id, "supervision.superseded", teaching_id=key, archived=stamp
            )
            record = None
        if record is None:
            record = {
                "id": key,
                "run_id": run_id,
                "episode": summary["episode_index"],
                "phase": phase,
                "fingerprint": fingerprint,
                "revision": 0,
                "status": "pending",
                "teacher_grant": context.teacher_grant,
                "mode": context.supervision,
                "learner_output": output.model_dump(),
                # Why LEVI refused the learner's answer, if it did: the
                # teacher must revise or reject, never accept it as is.
                **({"learner_error": error} if error else {}),
                **({"learner_raw": raw} if raw else {}),
                "summary": summary,
                "evidence": evidence,
                "created_at": time.time(),
            }
            wb.store.save(db, "teaching", key, record)
            run = wb.store.get("runs", run_id)
            run["prepared"] = sorted(
                set(run.get("prepared", []) + [summary["episode_index"]])
            )
            wb.store.save(db, "runs", run_id, run)
            wb.store.event(
                run_id,
                "supervision.requested",
                teaching_id=key,
                episode=record["episode"],
                phase=phase,
            )
    if record["status"] == "pending":
        raise AwaitingTeacher(
            "Waiting for the assigned teacher; cached model output and draft evidence are preserved"
        )
    if record["status"] == "rejected":
        raise AwaitingTeacher(
            "Teacher rejected this phase; revise the task plan before continuing"
        )
    result = ModelOutput.model_validate(record["accepted_output"])
    wb.validate_proposals(context, result.proposals, evidence, summary)
    return result


def authorized(wb, run_id, who):
    run = wb.store.get("runs", run_id)
    context = TaskContext.model_validate(run["context"])
    if context.supervision == "none":
        raise ValueError("This task has no teacher assignment")
    who.require("read", context.repo_id)
    if not who.human and who.id != context.teacher_grant:
        raise PermissionError(
            "Only the assigned teacher may access this teaching session"
        )
    require_teacher(wb.store, context, run_id)
    return run, context


def pending(wb, run_id, who):
    run = wb.store.get("runs", run_id)
    if run["context"].get("supervision", "none") == "none":
        # Asking is legitimate; the answer is simply that nothing waits here.
        who.require("read", run["context"]["repo_id"])
        return {
            "items": [],
            "scope": "annotation_phase",
            "promoted": False,
            "supervision": "none",
            "note": "This task has no teacher gate, so no phase waits for review.",
        }
    authorized(wb, run_id, who)
    return {
        "items": [
            item for item in wb.store.list("teaching") if item["run_id"] == run_id
        ],
        "scope": "annotation_phase",
        "promoted": False,
    }


def feedback(wb, run_id, who, teaching_id, revision, decision, note, output):
    run, context = authorized(wb, run_id, who)
    if run["status"] in {"cancelled", "succeeded"} or run.get("control") == "cancel":
        raise Conflict("Teaching session is closed")
    record = wb.store.get("teaching", teaching_id)
    if record["run_id"] != run_id:
        raise PermissionError("Teaching item belongs to a different task")
    if decision == "revise":
        if output is None:
            raise ValueError("A revised structured output is required")
        wb.validate_proposals(
            context, output.proposals, record["evidence"], record["summary"]
        )
    elif output is not None:
        raise ValueError("Only a revision may replace model output")
    elif decision == "accept" and record.get("learner_error"):
        raise ValueError(
            f"The learner's answer failed validation ({record['learner_error']}); "
            "revise or reject it"
        )
    payload = {
        "decision": decision,
        "note": note,
        "output": output.model_dump() if output else None,
    }
    request = digest({"teacher": who.id, "revision": revision, **payload})
    with wb.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        require_teacher(wb.store, context, run_id)
        current = wb.store.get("teaching", teaching_id)
        if current.get("feedback_request") == request:
            return current
        if current["revision"] != revision or current["status"] != "pending":
            raise Conflict(
                "Teaching revision changed; feedback is immutable once accepted"
            )
        current.update(
            status="rejected" if decision == "reject" else "accepted",
            revision=revision + 1,
            feedback=payload,
            teacher=who.id,
            teacher_usage={"source": "unknown", "tokens": None},
            feedback_request=request,
            reviewed_at=time.time(),
        )
        if decision != "reject":
            current["accepted_output"] = (
                output.model_dump() if output else current["learner_output"]
            )
        wb.store.save(db, "teaching", teaching_id, current)
        wb.store.event(
            run_id,
            "supervision.feedback",
            teaching_id=teaching_id,
            decision=decision,
            teacher=who.id,
            teacher_usage={"source": "unknown", "tokens": None},
            revision=current["revision"],
        )
    # Outside the transaction: a file and a memory note the next task's
    # learner is given (levi/harness/teaching.py).
    from levi.harness.layout import harness_lock
    from levi.harness.teaching import record

    with harness_lock(wb.store.state, run["dataset_key"]):
        record(wb.store, current, run["dataset_key"], context.workflow.get("kind"))
    return current
