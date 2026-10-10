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
- recall = early stops / truly successful episodes the detector could have
  stopped (an early stop or the horizon; not those a person, a fault or the
  policy ended);
- false early stop rate = control episodes (no early stop allowed) in which
  the detector would have stopped, among the control episodes that truly
  failed (pipeline §5.5); ``available: false`` without such episodes. The
  treatment group's early stops truly failed / truly failed episodes is
  reported as a lower bound only (the stop hid what came after it);
- saved steps = ``forward_max_steps`` minus the steps the episode ran
  (from the run manifest), summed over early stops;
- skip accuracy = scene decisions that skipped a reset on a truly ready
  scene or ran one on a scene that truly needed it / labelled decisions;
  wrong-skip rate = skips on a scene that needed a reset / labelled skips;
- interventions = moves into ``WAIT_HUMAN`` or ``FAULT_LOCKED``; the
  longest run without one counts forward episodes sealed complete.
"""

import fcntl
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
        """The whole lines of a kind's file. A last line without its newline
        (a write cut short) is no label and is ignored; any whole line that
        cannot be read makes the file unusable (``LabelRefused``): skipping
        it would hide a person's label."""
        if kind not in STORED_KINDS:
            raise LabelRefused(f"{kind} is not a stored label kind")
        try:
            data = self.path(kind).read_bytes()
        except FileNotFoundError:
            return []
        out = []
        # What follows the last newline is torn.
        for number, line in enumerate(data.split(b"\n")[:-1]):
            try:
                value = json.loads(line)
            except ValueError:
                value = None
            if not isinstance(value, dict) or value.get("kind") != kind:
                raise LabelRefused(
                    f"{self.path(kind)} line {number + 1} is not a {kind} label: "
                    "a person must look at the file before anything is added"
                )
            out.append(value)
        return out

    def _isolate_torn(self, kind: str) -> None:
        """Move a torn last line aside (``labels/torn/``) and cut the file
        back to whole lines, so the next label starts on its own line."""
        path = self.path(kind)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return
        if not data or data.endswith(b"\n"):
            return
        keep = data.rfind(b"\n") + 1
        folder = self.folder / "torn"
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / f"{kind}-{time.time_ns()}.bin").open("wb") as handle:
            handle.write(data[keep:])
            handle.flush()
            os.fsync(handle.fileno())
        with path.open("r+b") as handle:
            handle.truncate(keep)
            handle.flush()
            os.fsync(handle.fileno())

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
        self.folder.mkdir(parents=True, exist_ok=True)
        # One writer at a time: the check for an earlier label and the
        # append happen under one lock, so two processes cannot both pass it.
        with (self.folder / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._isolate_torn(kind)
            return self._add(kind, episode_id, value, subject, by, note, supersede)

    def _add(self, kind, episode_id, value, subject, by, note, supersede) -> dict:
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
    # Control episodes: the step at which the detector would have stopped.
    would_stop_step: int | None = None
    # The run manifest holds the episode's control record (written at a
    # successful seal); without it ``would_stop_step`` says nothing.
    control_recorded: bool = True

    @property
    def early_stop(self) -> bool:
        return self.stop_reason == "goal_verified"

    @property
    def could_stop(self) -> bool:
        """The detector had the whole episode: it stopped it early or the
        episode ran to its horizon (not ended by a person, a fault or the
        policy)."""
        return self.stop_reason in ("goal_verified", "horizon_exhausted")


def episodes(events, *, manifest: dict | None = None, termination=None) -> list:
    """Every episode result the journal committed, with its steps from the
    run manifest and whether it was a control episode."""
    entries = {e["episode_id"]: e for e in (manifest or {}).get("episodes", [])}
    out = []
    for event in events:
        if event.record != "committed" or event.episode_result is None:
            continue
        result = event.episode_result
        _, role, _ = aeri.episode_parts(event.episode_id)
        entry = entries.get(event.episode_id, {})
        if "control" in entry:  # what the run recorded (manifest)
            control = role == "forward" and entry["control"] is True
        else:
            control = bool(
                termination is not None
                and role == "forward"
                and termination.is_control(event.episode_id)
            )
        out.append(
            EpisodeRecord(
                episode_id=event.episode_id,
                role=role,
                task_outcome=result.task_outcome,
                stop_reason=result.stop_reason,
                goal_verification=result.goal_verification,
                scene_reset=result.scene_reset,
                sealed=result.rollout.sealed,
                steps=entry.get("steps"),
                control=control,
                would_stop_step=entry.get("would_stop_step") if control else None,
                control_recorded="control" in entry,
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
    """Early-termination metrics. The false early stop rate comes from the
    control group only (pipeline §5.5): a stopped episode hides whatever
    would have gone wrong after the stop, so only episodes run to their
    horizon with the stop withheld show whether it would have been false.
    Without labelled control episodes that truly failed it is
    ``available: false`` and carries no number. The treatment group's own
    figure is a lower bound and is named so."""
    forward = [r for r in records if r.role == "forward"]
    treated = [r for r in forward if not r.control]
    labelled = [r for r in treated if r.episode_id in truth]
    eligible = [r for r in labelled if r.could_stop]
    tp = sum(r.early_stop and truth[r.episode_id] == "success" for r in eligible)
    fp = sum(r.early_stop and truth[r.episode_id] == "failure" for r in eligible)
    fn = sum(not r.early_stop and truth[r.episode_id] == "success" for r in eligible)
    tn = sum(not r.early_stop and truth[r.episode_id] == "failure" for r in eligible)
    early = [r for r in treated if r.early_stop]
    saved = [
        max_steps - r.steps
        for r in early
        if max_steps is not None and r.steps is not None
    ]
    control = [r for r in forward if r.control]
    control_labelled = [r for r in control if r.episode_id in truth]
    # The denominator: control episodes that truly failed, ran to their
    # end (the detector had the whole episode), were sealed and carry their
    # control record. The rest are counted apart by reason (review C3
    # fixes, I-c): counting them as "would not have stopped" flatters.
    left_out = {"cut_short": 0, "not_recorded": 0}
    failed = []
    for r in control_labelled:
        if truth[r.episode_id] != "failure":
            continue
        if not r.could_stop:
            left_out["cut_short"] += 1
        elif r.sealed != "complete" or not r.control_recorded:
            left_out["not_recorded"] += 1
        else:
            failed.append(r)
    would = [r for r in control_labelled if r.would_stop_step is not None]
    if failed:
        rate = {
            "available": True,
            "source": "control",
            **share(sum(r.would_stop_step is not None for r in failed), len(failed)),
            "left_out": left_out,
        }
    else:
        rate = {
            "available": False,
            "source": "control",
            "reason": (
                "no labelled control episode that truly failed"
                if control_labelled
                else "no labelled control episodes (termination.control_fraction)"
            ),
            **share(0, 0),
            "left_out": left_out,
        }
    agree = [
        r
        for r in forward
        if r.episode_id in truth and r.task_outcome in ("success", "failure")
    ]
    return {
        "early_stops": len(early),
        "labelled": len(labelled),
        "unlabeled": len(treated) - len(labelled),
        "cut_short": len(labelled) - len(eligible),
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "precision": share(tp, tp + fp),
        "recall": share(tp, tp + fn),
        "false_early_stop_rate": rate,
        "treatment_false_early_stop_lower_bound": share(fp, fp + tn),
        "false_early_stops": fp,
        "saved_steps": {
            "total": sum(saved) if saved else None,
            "mean": round(sum(saved) / len(saved), 2) if saved else None,
            "episodes": len(saved),
        },
        "control": {
            "episodes": len(control),
            "labelled": len(control_labelled),
            "would_stop": len(would),
            "would_stop_false": share(
                sum(truth[r.episode_id] == "failure" for r in would), len(would)
            ),
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
