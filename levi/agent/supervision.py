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


def gate(wb, run_id, context, summary, evidence, phase, fingerprint, output):
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
            raise Conflict("Teaching evidence changed; create a new reviewed task")
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
    return current
