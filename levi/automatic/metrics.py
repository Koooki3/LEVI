"""Metrics of an automatic run (T-C-11, pipeline §11.2): early termination,
reset and automation, read from the run journal and from labels kept apart.

**Four kinds of label, never mixed (pipeline §9.4).**

- ``autonomous_verdict``: what the run decided, read from the journal's
  episode results only. It is never written here and never called ground
  truth: its rates are named ``autonomous_*``.
- ``posthoc_verdict``: an automatic review after the run.
- ``operator_label``: an operator's own judgement.
- ``adjudicated_ground_truth``: a label settled by a review protocol.

The last three live in ``<run_dir>/labels/<kind>.jsonl``, one append-only
file per kind (each line synced). Adding a label never touches another
kind's file; a second label of the same kind for the same episode and
subject is refused unless it says ``supersede=True``, and then it is
appended (the first stays on file). Subjects: ``task_outcome``
(``success``/``failure``) and ``initial_state`` (``ready``/
``reset_required``: was a reset needed before the episode that started).

**Truth** for the rates is ``adjudicated_ground_truth`` where it exists,
else ``operator_label`` (``truth="adjudicated_then_operator"``, the
default); ``truth="adjudicated"`` uses the first only. Episodes without a
truth label are counted (``unlabeled``) and left out of the rates. Every
rate carries its Wilson 95 % interval (``levi.live.stats.wilson``): with
the 20-30 episodes of an exploratory check the interval, not the rate, is
the result.

Definitions (forward episodes; control episodes, which never stop early,
are reported apart):

- an *early stop* is a forward episode ended by ``goal_verified``;
- precision = early stops truly successful / early stops;
- recall = early stops / truly successful episodes;
- false early stop rate = early stops truly failed / truly failed episodes;
- saved steps = ``forward_max_steps`` minus the steps the episode ran
  (from the run manifest), summed over early stops;
- skip accuracy = scene decisions that skipped a reset on a truly ready
  scene or ran one on a scene that truly needed it / labelled decisions;
  wrong-skip rate = skips on a scene that needed a reset / labelled skips;
- interventions = moves into ``WAIT_HUMAN`` or ``FAULT_LOCKED``; the
  longest run without one counts forward episodes sealed complete.
"""

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from levi.domain import aeri
from levi.live.stats import quantile, ratio, wilson

LABEL_KINDS = (
    "autonomous_verdict",
    "posthoc_verdict",
    "operator_label",
    "adjudicated_ground_truth",
)
STORED_KINDS = LABEL_KINDS[1:]
SUBJECTS = {
    "task_outcome": ("success", "failure"),
    "initial_state": ("ready", "reset_required"),
}
TRUTH = ("adjudicated_then_operator", "adjudicated")
LABEL_SCHEMA = "levi.aeri.label.v1"
# An opaque principal id: no names, no addresses.
PRINCIPAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


class LabelRefused(ValueError):
    pass


def share(k: int, n: int) -> dict:
    """``{"n", "of", "rate", "wilson95"}``; rate and interval None for n=0."""
    return {"n": k, "of": n, "rate": ratio(k, n), "wilson95": wilson(k, n)}


# --- labels ---------------------------------------------------------------------------------------


class LabelStore:
    """The non-autonomous labels of one run, one append-only file per kind."""

    def __init__(self, run_dir):
        self.folder = Path(run_dir) / "labels"

    def path(self, kind: str) -> Path:
        return self.folder / f"{kind}.jsonl"

    def lines(self, kind: str) -> list:
        if kind not in STORED_KINDS:
            raise LabelRefused(f"{kind} is not a stored label kind")
        try:
            text = self.path(kind).read_text()
        except FileNotFoundError:
            return []
        out = []
        for line in text.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue  # a torn last line
            if isinstance(value, dict) and value.get("kind") == kind:
                out.append(value)
        return out

    def add(
        self,
        kind: str,
        episode_id: str,
        value: str,
        *,
        subject: str = "task_outcome",
        by: str,
        note: str = "",
        supersede: bool = False,
    ) -> dict:
        if kind == "autonomous_verdict":
            raise LabelRefused("the autonomous verdict comes from the run journal only")
        if kind not in STORED_KINDS:
            raise LabelRefused(f"unknown label kind {kind!r}")
        if subject not in SUBJECTS or value not in SUBJECTS[subject]:
            raise LabelRefused(f"{subject}={value!r} is not a label")
        try:
            aeri.episode_parts(episode_id)
        except ValueError:
            raise LabelRefused(f"{episode_id!r} is not an episode id") from None
        if not isinstance(by, str) or not PRINCIPAL.match(by):
            raise LabelRefused("by is an opaque principal id (no names, no addresses)")
        earlier = [
            line
            for line in self.lines(kind)
            if line["episode_id"] == episode_id and line["subject"] == subject
        ]
        if earlier and not supersede:
            raise LabelRefused(
                f"{kind} already labels {episode_id} ({subject}); "
                "pass supersede=True to append a correction"
            )
        record = {
            "schema": LABEL_SCHEMA,
            "kind": kind,
            "episode_id": episode_id,
            "subject": subject,
            "value": value,
            "by": by,
            "note": str(note)[:300],
            "at_wall_ns": time.time_ns(),
            "supersedes": len(earlier) if earlier else None,
        }
        self.folder.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(record, sort_keys=True) + "\n").encode()
        descriptor = os.open(
            self.path(kind), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644
        )
        try:
            os.write(descriptor, line)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return record

    def latest(self, kind: str, subject: str = "task_outcome") -> dict:
        """``{episode_id: value}``, the last line of each episode winning."""
        out = {}
        for line in self.lines(kind):
            if line.get("subject") == subject:
                out[line["episode_id"]] = line["value"]
        return out

    def truth(self, subject: str = "task_outcome", rule: str = TRUTH[0]) -> dict:
        if rule not in TRUTH:
            raise LabelRefused(f"truth is one of {', '.join(TRUTH)}")
        found = {}
        if rule == "adjudicated_then_operator":
            found.update(self.latest("operator_label", subject))
        found.update(self.latest("adjudicated_ground_truth", subject))
        return found


# --- what the journal says --------------------------------------------------------------------------


@dataclass(frozen=True)
class EpisodeRecord:
    episode_id: str
    role: str
    task_outcome: str
    stop_reason: str
    goal_verification: str
    scene_reset: str
    sealed: str
    steps: int | None = None
    control: bool = False

    @property
    def early_stop(self) -> bool:
        return self.stop_reason == "goal_verified"


def episodes(events, *, manifest: dict | None = None, termination=None) -> list:
    """Every episode result the journal committed, with its steps from the
    run manifest and whether it was a control episode."""
    steps = {
        e["episode_id"]: e.get("steps") for e in (manifest or {}).get("episodes", [])
    }
    out = []
    for event in events:
        if event.record != "committed" or event.episode_result is None:
            continue
        result = event.episode_result
        _, role, _ = aeri.episode_parts(event.episode_id)
        out.append(
            EpisodeRecord(
                episode_id=event.episode_id,
                role=role,
                task_outcome=result.task_outcome,
                stop_reason=result.stop_reason,
                goal_verification=result.goal_verification,
                scene_reset=result.scene_reset,
                sealed=result.rollout.sealed,
                steps=steps.get(event.episode_id),
                control=bool(
                    termination is not None
                    and role == "forward"
                    and termination.is_control(event.episode_id)
                ),
            )
        )
    return out


def scene_decisions(events) -> list:
    """``[(episode_id, skipped)]``: each move out of a scene check into an
    episode (``skipped``: a forward episode started without a reset)."""
    out = []
    for event in events:
        if event.record != "committed" or event.from_state not in (
            "VERIFY_INITIAL",
            "SCENE_ASSESS",
        ):
            continue
        if event.to_state == "FORWARD_ACTIVE":
            out.append((event.episode_id, True))
        elif event.to_state == "RESET_ACTIVE":
            out.append((event.episode_id, False))
    return out


# --- the three groups -----------------------------------------------------------------------------------


def autonomous(records) -> dict:
    forward = [r for r in records if r.role == "forward"]
    counts = {
        k: sum(r.task_outcome == k for r in forward)
        for k in ("success", "failure", "unknown")
    }
    return {
        "label_kind": "autonomous_verdict",
        "forward_episodes": len(forward),
        "outcomes": counts,
        # Unknown stays in the denominator: never a success, never dropped.
        "autonomous_success_rate": share(counts["success"], len(forward)),
        "stop_reasons": _count(r.stop_reason for r in forward),
    }


def early_termination(records, truth: dict, *, max_steps: int | None = None) -> dict:
    forward = [r for r in records if r.role == "forward"]
    treated = [r for r in forward if not r.control]
    labelled = [r for r in treated if r.episode_id in truth]
    tp = sum(r.early_stop and truth[r.episode_id] == "success" for r in labelled)
    fp = sum(r.early_stop and truth[r.episode_id] == "failure" for r in labelled)
    fn = sum(not r.early_stop and truth[r.episode_id] == "success" for r in labelled)
    tn = sum(not r.early_stop and truth[r.episode_id] == "failure" for r in labelled)
    early = [r for r in treated if r.early_stop]
    saved = [
        max_steps - r.steps
        for r in early
        if max_steps is not None and r.steps is not None
    ]
    control = [r for r in forward if r.control]
    control_labelled = [r for r in control if r.episode_id in truth]
    agree = [
        r
        for r in forward
        if r.episode_id in truth and r.task_outcome in ("success", "failure")
    ]
    return {
        "early_stops": len(early),
        "labelled": len(labelled),
        "unlabeled": len(treated) - len(labelled),
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "precision": share(tp, tp + fp),
        "recall": share(tp, tp + fn),
        "false_early_stop_rate": share(fp, fp + tn),
        "false_early_stops": fp,
        "saved_steps": {
            "total": sum(saved) if saved else None,
            "mean": round(sum(saved) / len(saved), 2) if saved else None,
            "episodes": len(saved),
        },
        "control": {
            "episodes": len(control),
            "labelled": len(control_labelled),
            "true_success": share(
                sum(truth[r.episode_id] == "success" for r in control_labelled),
                len(control_labelled),
            ),
        },
        # The autonomous verdict against the truth (unknown left out, counted).
        "verdict_agreement": share(
            sum(r.task_outcome == truth[r.episode_id] for r in agree), len(agree)
        ),
        "false_success": sum(
            r.task_outcome == "success" and truth[r.episode_id] == "failure"
            for r in agree
        ),
        "unknown_verdicts": sum(
            r.task_outcome == "unknown" for r in forward if r.episode_id in truth
        ),
    }


def reset(records, decisions, scene_truth: dict, events=()) -> dict:
    resets = [r for r in records if r.role == "reset"]
    succeeded = sum(r.scene_reset == "succeeded" for r in resets)
    labelled = [(e, s) for e, s in decisions if e in scene_truth]
    correct = sum(
        (s and scene_truth[e] == "ready")
        or (not s and scene_truth[e] == "reset_required")
        for e, s in labelled
    )
    skips = [(e, s) for e, s in labelled if s]
    runs = [(e, s) for e, s in labelled if not s]
    durations = _reset_durations(events)
    return {
        "resets": len(resets),
        "autonomous_reset_success_rate": share(succeeded, len(resets)),
        "decisions": len(decisions),
        "skipped": sum(s for _, s in decisions),
        "labelled_decisions": len(labelled),
        "skip_accuracy": share(correct, len(labelled)),
        "wrong_skip_rate": share(
            sum(scene_truth[e] == "reset_required" for e, _ in skips), len(skips)
        ),
        "unneeded_reset_rate": share(
            sum(scene_truth[e] == "ready" for e, _ in runs), len(runs)
        ),
        "reset_duration_ms": {
            "n": len(durations),
            "p50": quantile(durations, 0.5),
            "max": max(durations) if durations else None,
        },
    }


def _reset_durations(events) -> list:
    out, started = [], {}
    for event in events:
        if event.record != "committed":
            continue
        if event.to_state == "RESET_ACTIVE":
            started[event.episode_id] = (event.mono_ns, event.clock_domain)
        elif event.from_state == "RESET_FINALIZE" and event.episode_id in started:
            begun, domain = started.pop(event.episode_id)
            if domain == event.clock_domain:
                out.append((event.mono_ns - begun) // 1_000_000)
    return out


def automation(events) -> dict:
    interventions = []
    longest = current = 0
    resumed = 0
    waited_ms, unmeasured = 0, 0
    entered = None
    for event in events:
        if event.record != "committed":
            continue
        if (
            event.to_state in ("WAIT_HUMAN", "FAULT_LOCKED")
            and event.from_state != event.to_state
        ):
            interventions.append(event.reason)
            longest = max(longest, current)
            current = 0
            if entered is None:
                entered = (event.mono_ns, event.clock_domain)
        elif (
            (event.from_state, event.to_state) == ("FORWARD_FINALIZE", "ROBOT_HOME")
            and event.episode_result is not None
            and event.episode_result.rollout.sealed == "complete"
        ):
            current += 1
        elif event.reason == "human_resumed":
            resumed += 1
            if entered is not None:
                if entered[1] == event.clock_domain:
                    waited_ms += (event.mono_ns - entered[0]) // 1_000_000
                else:
                    unmeasured += 1  # a restart in between: clocks differ
            entered = None
    longest = max(longest, current)
    return {
        "interventions": len(interventions),
        "by_reason": _count(interventions),
        "resumes": resumed,
        "longest_run_without_intervention": longest,
        "current_run_without_intervention": current,
        "human_wait_ms": waited_ms,
        "human_waits_unmeasured": unmeasured,
    }


def _count(values) -> dict:
    out: dict = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


def report(
    events,
    *,
    labels: LabelStore | None = None,
    manifest: dict | None = None,
    termination=None,
    max_steps: int | None = None,
    truth: str = TRUTH[0],
) -> dict:
    """All three groups for one run, with where the truth came from."""
    records = episodes(events, manifest=manifest, termination=termination)
    outcome_truth = labels.truth("task_outcome", truth) if labels else {}
    scene_truth = labels.truth("initial_state", truth) if labels else {}
    return {
        "schema": "levi.aeri.metrics.v1",
        "truth": truth,
        "truth_labels": {
            "task_outcome": len(outcome_truth),
            "initial_state": len(scene_truth),
        },
        "note": "autonomous_* rates are the run's own verdicts, not ground truth",
        "autonomous": autonomous(records),
        "early_termination": early_termination(
            records, outcome_truth, max_steps=max_steps
        ),
        "reset": reset(records, scene_decisions(events), scene_truth, events),
        "automation": automation(events),
    }
