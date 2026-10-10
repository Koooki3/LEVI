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

**Comparable across reset modes (T-CL-05, design X2 §1.2).** A run's
``reset_mode`` (``single_reset_policy`` or ``human_assisted``, "policy
evaluation only") and ``scene_check`` (``provider`` or ``operator_attested``)
head the report, with where each came from (``mode_source``: the caller, the
run header, the manifest, the journal, or unknown). ``comparable`` lists the
fields that mean the same in both modes; compare runs of different modes on
those only (``mode_specific`` lists the rest):

- an intervention is *planned* in ``human_assisted`` when a scene check
  (``VERIFY_INITIAL``/``SCENE_ASSESS``) sends the run to ``WAIT_HUMAN`` for
  ``scene_reset_required``/``scene_unknown``: that is how the mode resets.
  Everything else is *unplanned* (``FAULT_LOCKED``, a reset policy out of
  attempts, a failed preflight, an operator's stop...). An unknown mode
  counts every intervention as unplanned (never flatters). The runs without
  a person count unplanned interventions (``longest_run_without_unplanned``,
  comparable); ``longest_run_without_any_human`` counts every one;
- ``turnaround`` runs from episode k's home reached (``ROBOT_HOME ->
  SCENE_ASSESS`` committed) to episode k+1's ``FORWARD_ACTIVE`` committed,
  split by the state the run was in: ``scene_ms`` (``SCENE_ASSESS``),
  ``reset_policy_ms`` (``RESET_*``), ``human_reset_ms`` (``WAIT_HUMAN``,
  ``FAULT_LOCKED``) and ``verify_ms`` (``PREFLIGHT``, ``VERIFY_INITIAL``);
  the parts add up to the whole. A window across a restart with another
  clock domain is ``unmeasured``; one the run ended in is ``no_next_episode``
  or ``open``. An episode that ended without its home reached
  (``ROBOT_HOME -> WAIT_HUMAN/FAULT_LOCKED``: a stop where the arm stands, a
  failed home) starts no window; ``turnaround_unmeasured`` counts both kinds
  of left-out turnaround by reason;
- ``time_per_valid_episode_ms`` = the journal's span on the monotonic clock
  (per clock domain, summed: the downtime between a crash and the restart
  is not counted, ``downtime_excluded: true``) / forward episodes sealed
  complete;
- ``human_minutes_per_valid_episode`` = minutes in ``WAIT_HUMAN`` or
  ``FAULT_LOCKED`` closed by a resume or an operator's stop / forward
  episodes sealed complete (a wait the run still is in is ``open_waits``).
  A fault during a planned wait splits it: the time before the fault is
  planned, the time after it unplanned;
- ``scene_decisions_by_human``: with ``scene_check: operator_attested`` a
  person answered the scene checks; those decisions are listed here and
  left out of the reset group's skip accuracy (a machine-provider rate).
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
# An operator's outcome label (T-CL-14) may also say the episode is not to
# be counted (``discarded``) or that the person could not tell
# (``unclear``); neither is ever truth. The other kinds keep two values.
OPERATOR_VALUES = ("success", "failure", "discarded", "unclear")
# How a forward episode ended, from its stop reason in the journal: its
# budget ran out, the detector stopped it early, the operator stopped it, or
# anything else (a fault, the policy, a crash, a missing reason).
ENDED_BY = ("budget", "early_stop", "operator_stop", "unknown")
_ENDED_OF_STOP = {
    "horizon_exhausted": "budget",
    "goal_verified": "early_stop",
    "operator_stop": "operator_stop",
}
# The automatic verdict for the agreement, from the final judgement:
# decided (success, failure), left undecided, or none (not judged).
AUTOMATIC_KINDS = ("success", "failure", "undecided", "none")
_AUTOMATIC_OF_VERIFICATION = {
    "verified": "success",
    "contradicted": "failure",
    "undecided": "undecided",
    "unavailable": "none",
}
# Below this many decided pairs a stratum gets its interval, no point
# estimate (``rate`` null, ``small_sample`` true).
AGREEMENT_MIN_N = 10
TRUTH = ("adjudicated_then_operator", "adjudicated")
LABEL_SCHEMA = "levi.aeri.label.v1"
RESET_MODES = ("single_reset_policy", "human_assisted")  # = modes.RESET_MODES
SCENE_CHECKS = ("provider", "operator_attested")
# A scene check sending the run to a person: the human_assisted mode's reset.
SCENE_STATES = frozenset({"VERIFY_INITIAL", "SCENE_ASSESS"})
PLANNED_REASONS = frozenset({"scene_reset_required", "scene_unknown"})
PERSON_STATES = frozenset({"WAIT_HUMAN", "FAULT_LOCKED"})
# Where a turnaround's time went, by the state the run was in.
TURNAROUND_PARTS = {
    "SCENE_ASSESS": "scene_ms",
    "RESET_ACTIVE": "reset_policy_ms",
    "RESET_VERIFY": "reset_policy_ms",
    "RESET_FINALIZE": "reset_policy_ms",
    "WAIT_HUMAN": "human_reset_ms",
    "FAULT_LOCKED": "human_reset_ms",
    "PREFLIGHT": "verify_ms",
    "VERIFY_INITIAL": "verify_ms",
}
PARTS = ("scene_ms", "reset_policy_ms", "human_reset_ms", "verify_ms")
# Fields that mean the same in both reset modes (design X2 §1.2).
COMPARABLE = (
    "autonomous",
    "early_termination",
    "turnaround.turnaround_ms",
    "turnaround.scene_ms",
    "turnaround.verify_ms",
    "turnaround.with_person",
    "time_per_valid_episode_ms",
    "human_minutes_per_valid_episode",
    "automation.unplanned",
    "automation.longest_run_without_unplanned",
    "agreement",
)
MODE_SPECIFIC = (
    "reset",
    "turnaround.reset_policy_ms",
    "turnaround.human_reset_ms",
    "automation.interventions",
    "automation.planned",
    "automation.longest_run_without_intervention",
    "automation.longest_run_without_any_human",
    "scene_decisions_by_human",
)
# An opaque principal id: no names, no addresses.
PRINCIPAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


class LabelRefused(ValueError):
    pass


def share(k: int, n: int) -> dict:
    """``{"n", "of", "rate", "wilson95"}``; rate and interval None for n=0."""
    return {"n": k, "of": n, "rate": ratio(k, n), "wilson95": wilson(k, n)}


def ended_by(stop_reason) -> str:
    """``budget``, ``early_stop``, ``operator_stop`` or ``unknown``."""
    return _ENDED_OF_STOP.get(stop_reason, "unknown")


def automatic_of(goal_verification) -> str:
    """``success``, ``failure``, ``undecided`` or ``none``."""
    return _AUTOMATIC_OF_VERIFICATION.get(goal_verification, "none")


def label_values(kind: str, subject: str) -> tuple:
    """The values a label of this kind may give this subject."""
    if kind == "operator_label" and subject == "task_outcome":
        return OPERATOR_VALUES
    return SUBJECTS.get(subject, ())


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
        if subject not in SUBJECTS or value not in label_values(kind, subject):
            raise LabelRefused(f"{subject}={value!r} is not a {kind}")
        try:
            aeri.episode_parts(episode_id)
        except (AttributeError, ValueError):
            raise LabelRefused(f"{episode_id!r} is not an episode id") from None
        # fullmatch: ``$`` alone would let a trailing newline through.
        if not isinstance(by, str) or not PRINCIPAL.fullmatch(by):
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
        """``{episode_id: value}``. An operator's current label that is
        ``discarded`` or ``unclear`` is no truth: the episode has none
        unless an adjudicated label gives it one."""
        if rule not in TRUTH:
            raise LabelRefused(f"truth is one of {', '.join(TRUTH)}")
        allowed = SUBJECTS.get(subject, ())
        found = {}
        kinds = ("adjudicated_ground_truth",)
        if rule == "adjudicated_then_operator":
            kinds = ("operator_label", *kinds)
        for kind in kinds:
            found.update(
                {e: v for e, v in self.latest(kind, subject).items() if v in allowed}
            )
        return found


def ended_forward(events) -> dict:
    """``{episode_id: EpisodeResult}`` of the forward episodes whose result
    the journal committed: the episodes an operator may label."""
    out = {}
    for event in events:
        if event.record != "committed" or event.episode_result is None:
            continue
        try:
            role = aeri.episode_parts(event.episode_id)[1]
        except (AttributeError, ValueError):
            continue
        if role == "forward":
            out[event.episode_id] = event.episode_result
    return out


def label_operator(run_dir, episode_id: str, value: str, *, by: str, note="") -> dict:
    """Append an operator's outcome label (``OPERATOR_VALUES``) for a
    forward episode of this run that has ended (its result is in the
    journal), whenever that is: while the run goes on, while it waits for a
    person, after it ended. A later label for the same episode is a
    correction: it is appended and becomes the current value, the earlier
    ones stay on file. Reads the journal without its lock and never writes
    it; only ``labels/operator_label.jsonl`` (and the labels' lock file)
    is written."""
    from .journal import Journal

    if value not in OPERATOR_VALUES:
        raise LabelRefused(
            f"{value!r} is not an operator label ({', '.join(OPERATOR_VALUES)})"
        )
    try:
        role = aeri.episode_parts(episode_id)[1]
    except (AttributeError, ValueError):
        raise LabelRefused(f"{episode_id!r} is not an episode id") from None
    if role != "forward":
        raise LabelRefused(
            f"{episode_id} is a {role} episode: only forward episodes are labelled"
        )
    if episode_id not in ended_forward(Journal.read(Path(run_dir)).events):
        raise LabelRefused(
            f"{episode_id} has not ended in this run (no result in its journal): "
            "only an ended forward episode takes a label"
        )
    return LabelStore(run_dir).add(
        "operator_label", episode_id, value, by=by, note=note, supersede=True
    )


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


def _guarded(k: int, n: int) -> dict:
    """``share`` without its point estimate below ``AGREEMENT_MIN_N``."""
    found = share(k, n)
    if n < AGREEMENT_MIN_N:
        found["rate"] = None
    return found


def _agree(records, operator: dict) -> dict:
    matrix = {op: {a: 0 for a in AUTOMATIC_KINDS} for op in ("success", "failure")}
    apart = {"unlabelled": 0, "discarded": 0, "unclear": 0}
    for r in records:
        value = operator.get(r.episode_id)
        if value in matrix:
            matrix[value][automatic_of(r.goal_verification)] += 1
        elif value in ("discarded", "unclear"):
            apart[value] += 1
        else:
            apart["unlabelled"] += 1
    s, f = matrix["success"], matrix["failure"]
    judged = s["success"] + s["failure"] + f["success"] + f["failure"]
    agree = s["success"] + f["failure"]
    return {
        "episodes": len(records),
        "operator_decided": sum(s.values()) + sum(f.values()),
        **apart,
        "matrix": matrix,
        "judged": judged,
        "agree": agree,
        "agreement": _guarded(agree, judged),
        "small_sample": judged < AGREEMENT_MIN_N,
        # The automatic verdict said success where the operator said failure;
        # failure where the operator said success.
        "false_success": _guarded(f["success"], f["success"] + f["failure"]),
        "missed_success": _guarded(s["failure"], s["success"] + s["failure"]),
        "undecided": s["undecided"] + f["undecided"],
        "none": s["none"] + f["none"],
    }


def agreement(records, operator: dict) -> dict:
    """The automatic verdict (the final judgement of each forward episode,
    in the journal) against the operator's current label (T-CL-14), in
    total and by how the episode ended (``ENDED_BY``). Only episodes the
    operator called success or failure are compared (``judged``: those the
    run decided too); an undecided verdict and no verdict (``none``) are
    counted apart, as are the operator's ``discarded`` and ``unclear`` and
    the unlabelled episodes. Below ``AGREEMENT_MIN_N`` decided pairs a
    stratum gets its Wilson interval and no point estimate. An episode the
    detector or the operator ended early is shorter than one run to its
    budget: only the ``budget`` stratum carries over to unattended runs."""
    forward = [r for r in records if r.role == "forward"]
    groups: dict = {name: [] for name in ENDED_BY}
    for r in forward:
        groups[ended_by(r.stop_reason)].append(r)
    return {
        "label_kind": "operator_label",
        "min_n": AGREEMENT_MIN_N,
        "note": "only the budget stratum carries over to unattended runs",
        "total": _agree(forward, operator),
        "by_ended_by": {name: _agree(groups[name], operator) for name in ENDED_BY},
    }


def reset(
    records, decisions, scene_truth: dict, events=(), *, by_human: bool = False
) -> dict:
    """``by_human``: a person answered the scene checks (``scene_check:
    operator_attested``); the skip accuracy is a machine provider's, so
    those decisions are left out of it (``scene_decisions_by_human``)."""
    resets = [r for r in records if r.role == "reset"]
    succeeded = sum(r.scene_reset == "succeeded" for r in resets)
    labelled = [] if by_human else [(e, s) for e, s in decisions if e in scene_truth]
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


def intervention_kind(event, reset_mode: str | None) -> str:
    """``planned`` or ``unplanned`` for a committed move into ``WAIT_HUMAN``
    or ``FAULT_LOCKED`` (see the module text)."""
    if (
        reset_mode == "human_assisted"
        and event.to_state == "WAIT_HUMAN"
        and event.from_state in SCENE_STATES
        and event.reason in PLANNED_REASONS
    ):
        return "planned"
    return "unplanned"


def _sealed_forward(event) -> bool:
    return (
        (event.from_state, event.to_state) == ("FORWARD_FINALIZE", "ROBOT_HOME")
        and event.episode_result is not None
        and event.episode_result.rollout.sealed == "complete"
    )


def automation(events, reset_mode: str | None = None) -> dict:
    """``reset_mode`` splits the interventions into planned and unplanned;
    None (unknown) makes every one unplanned."""
    interventions = []
    kinds = {"planned": [], "unplanned": []}
    longest = current = 0
    longest_unplanned = current_unplanned = 0
    resumed = 0
    waited_ms, unmeasured = 0, 0
    entered = None
    # Waits closed by a resume or an operator's stop, by the kind of the
    # move that started them (T-CL-05).
    person = {"planned": 0, "unplanned": 0}
    person_unmeasured = 0
    opened = None  # (mono_ns, clock_domain, kind) of the wait the run is in
    for event in events:
        if event.record != "committed":
            continue
        if event.to_state in PERSON_STATES and event.from_state != event.to_state:
            interventions.append(event.reason)
            found = intervention_kind(event, reset_mode)
            kinds[found].append(event.reason)
            longest = max(longest, current)
            current = 0
            if found == "unplanned":
                longest_unplanned = max(longest_unplanned, current_unplanned)
                current_unplanned = 0
            if entered is None:
                entered = (event.mono_ns, event.clock_domain)
            if opened is not None:
                # A new intervention during a wait (a fault while a person
                # resets the scene): the time so far stays with the first
                # one's kind, what follows belongs to the new one's.
                if opened[1] == event.clock_domain:
                    person[opened[2]] += (event.mono_ns - opened[0]) // 1_000_000
                else:
                    person_unmeasured += 1
            opened = (event.mono_ns, event.clock_domain, found)
        elif _sealed_forward(event):
            current += 1
            current_unplanned += 1
        elif event.reason == "human_resumed":
            resumed += 1
            if entered is not None:
                if entered[1] == event.clock_domain:
                    waited_ms += (event.mono_ns - entered[0]) // 1_000_000
                else:
                    unmeasured += 1  # a restart in between: clocks differ
            entered = None
        if opened is not None and (
            event.reason == "human_resumed"
            or (event.from_state == "WAIT_HUMAN" and event.to_state == "COMPLETED")
        ):
            if opened[1] == event.clock_domain:
                person[opened[2]] += (event.mono_ns - opened[0]) // 1_000_000
            else:
                person_unmeasured += 1
            opened = None
    longest = max(longest, current)
    longest_unplanned = max(longest_unplanned, current_unplanned)
    return {
        "interventions": len(interventions),
        "by_reason": _count(interventions),
        "resumes": resumed,
        "longest_run_without_intervention": longest,
        "current_run_without_intervention": current,
        "human_wait_ms": waited_ms,
        "human_waits_unmeasured": unmeasured,
        "reset_mode": reset_mode,
        "planned": len(kinds["planned"]),
        "unplanned": len(kinds["unplanned"]),
        "planned_by_reason": _count(kinds["planned"]),
        "unplanned_by_reason": _count(kinds["unplanned"]),
        "unplanned_share": share(len(kinds["unplanned"]), len(interventions)),
        "longest_run_without_unplanned": longest_unplanned,
        "current_run_without_unplanned": current_unplanned,
        # Every intervention, planned or not (= longest_run_without_intervention).
        "longest_run_without_any_human": longest,
        "person_ms": {
            "planned": person["planned"],
            "unplanned": person["unplanned"],
            "total": person["planned"] + person["unplanned"],
            "unmeasured": person_unmeasured,
            "open_waits": int(opened is not None),
        },
    }


# --- comparable across reset modes (T-CL-05) -------------------------------------------------------


def _spread(values) -> dict:
    values = list(values)
    if not values:
        return {"n": 0, "total": None, "mean": None, "p50": None, "max": None}
    return {
        "n": len(values),
        "total": sum(values),
        "mean": round(sum(values) / len(values), 1),
        "p50": quantile(values, 0.5),
        "max": max(values),
    }


def turnaround(events) -> dict:
    """Episode k's home reached to episode k+1's start, split by state (see
    the module text)."""
    done, unmeasured, no_next = [], 0, 0
    not_homed: list = []
    window = None
    for event in events:
        if event.record != "committed" or event.to_state is None:
            continue
        if (
            window is None
            and event.from_state == "ROBOT_HOME"
            and event.to_state in PERSON_STATES
        ):
            # The episode ended without its home reached (an operator's stop
            # where the arm stands, a failed home, a fault): no window starts.
            not_homed.append(f"{event.to_state}:{event.reason}")
        if window is not None:
            part = TURNAROUND_PARTS.get(window["state"])
            if part is None or event.clock_domain != window["domain"]:
                window["broken"] = True
            else:
                window[part] += (event.mono_ns - window["since"]) // 1_000_000
            window["state"], window["since"] = event.to_state, event.mono_ns
            window["domain"] = event.clock_domain
            window["person"] |= event.to_state in PERSON_STATES
            if event.to_state == "FORWARD_ACTIVE":
                if window["broken"]:
                    unmeasured += 1
                else:
                    done.append(window)
                window = None
            elif event.to_state == "COMPLETED":
                no_next += 1
                window = None
        elif (event.from_state, event.to_state) == ("ROBOT_HOME", "SCENE_ASSESS"):
            window = {
                "state": "SCENE_ASSESS",
                "since": event.mono_ns,
                "domain": event.clock_domain,
                "broken": False,
                "person": False,
                **{part: 0 for part in PARTS},
            }
    return {
        "n": len(done),
        "turnaround_ms": _spread(sum(w[p] for p in PARTS) for w in done),
        **{part: _spread(w[part] for w in done) for part in PARTS},
        "with_person": share(sum(w["person"] for w in done), len(done)),
        "unmeasured": unmeasured,
        "no_next_episode": no_next,
        "open": int(window is not None),
        # Turnarounds left out, by why: a window across a restart on another
        # clock, or an episode whose home was never reached.
        "turnaround_unmeasured": {
            "n": unmeasured + len(not_homed),
            "by_reason": _count(
                [*["clock_domain_changed"] * unmeasured]
                + [f"not_homed:{r}" for r in not_homed]
            ),
        },
    }


def _span_ms(events) -> tuple[int, int]:
    """The journal's span in ms, summed over runs of one clock domain (a
    restart starts a new one: the time between processes is not counted),
    and how many such runs there were."""
    total, domains = 0, 0
    first = last = domain = None
    for event in events:
        if event.clock_domain != domain:
            if first is not None:
                total += (last - first) // 1_000_000
            first, domain = event.mono_ns, event.clock_domain
            domains += 1
        last = event.mono_ns
    if first is not None:
        total += (last - first) // 1_000_000
    return total, domains


def _valid(records) -> int:
    return sum(r.role == "forward" and r.sealed == "complete" for r in records)


def time_per_valid_episode(events, records) -> dict:
    span, domains = _span_ms(events)
    valid = _valid(records)
    return {
        "span_ms": span,
        "clock_domains": domains,
        # The span is measured on the monotonic clock, one stretch per
        # process (clock domain), summed: the downtime between a crash and
        # the restart is not in it.
        "downtime_excluded": True,
        "basis": "monotonic span per clock domain, summed; restarts' downtime excluded",
        "valid_episodes": valid,
        "value": round(span / valid, 1) if valid else None,
    }


def human_minutes(person: dict, records) -> dict:
    valid = _valid(records)
    return {
        "person_ms": person["total"],
        "valid_episodes": valid,
        "value": round(person["total"] / 60_000 / valid, 4) if valid else None,
        "open_waits": person["open_waits"],
        "unmeasured": person["unmeasured"],
    }


def scene_by_human(decisions, scene_check: str | None) -> dict:
    by_human = scene_check == "operator_attested"
    found = list(decisions) if by_human else []
    return {
        "scene_check": scene_check,
        "decisions": len(found),
        "skipped": sum(s for _, s in found),
        "note": "a person's scene decisions; never in the automatic skip accuracy",
    }


def _header(events):
    for event in events:
        if event.record == "run_header":
            return getattr(event, "header", None)
    return None


def human_resets_in(manifest: dict) -> bool:
    """Whether the run manifest records a forward episode a person reset
    the scene for. The recorder (T-CL-02) writes it per episode
    (``episodes[*].after_human_resets`` non-empty, ``preceded_by:
    human_reset``); a manifest-level ``after_human_resets`` list is read
    too. A minor-0 manifest has neither."""
    if not isinstance(manifest, dict):
        return False
    if (
        isinstance(manifest.get("after_human_resets"), list)
        and (manifest["after_human_resets"])
    ):
        return True
    entries = manifest.get("episodes")
    return isinstance(entries, list) and any(
        isinstance(entry, dict)
        and (
            entry.get("preceded_by") == "human_reset"
            or (
                isinstance(entry.get("after_human_resets"), list)
                and bool(entry["after_human_resets"])
            )
        )
        for entry in entries
    )


def mode_of(
    events,
    *,
    manifest: dict | None = None,
    reset_mode: str | None = None,
    scene_check: str | None = None,
) -> dict:
    """The run's reset mode and scene check, and where each came from: the
    caller (``argument``), the run header (contract minor 1), the manifest,
    the journal, a default, or ``unknown``. A minor-0 journal has neither
    in its header: a reset episode in the journal means the reset policy;
    a person's reset in the manifest (``human_resets_in``) means
    human_assisted; else the mode is unknown (every intervention then
    counts as unplanned)."""
    for name, value, allowed in (
        ("reset_mode", reset_mode, RESET_MODES),
        ("scene_check", scene_check, SCENE_CHECKS),
    ):
        if value is not None and value not in allowed:
            raise ValueError(f"{name} is one of {', '.join(allowed)}")
    header = _header(events)
    manifest = manifest if isinstance(manifest, dict) else {}
    source = {}
    if reset_mode is not None:
        source["reset_mode"] = "argument"
    elif getattr(header, "reset_mode", None) in RESET_MODES:
        reset_mode, source["reset_mode"] = header.reset_mode, "run_header"
    elif manifest.get("reset_mode") in RESET_MODES:
        reset_mode, source["reset_mode"] = manifest["reset_mode"], "manifest"
    elif any(e.record == "committed" and e.to_state == "RESET_ACTIVE" for e in events):
        reset_mode, source["reset_mode"] = "single_reset_policy", "journal"
    elif human_resets_in(manifest):
        reset_mode, source["reset_mode"] = "human_assisted", "manifest"
    else:
        source["reset_mode"] = "unknown"
    if scene_check is not None:
        source["scene_check"] = "argument"
    elif getattr(header, "scene_check", None) in SCENE_CHECKS:
        scene_check, source["scene_check"] = header.scene_check, "run_header"
    elif manifest.get("scene_check") in SCENE_CHECKS:
        scene_check, source["scene_check"] = manifest["scene_check"], "manifest"
    else:
        # Before contract minor 1 only machine providers existed.
        scene_check, source["scene_check"] = "provider", "default"
    return {"reset_mode": reset_mode, "scene_check": scene_check, "source": source}


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
    reset_mode: str | None = None,
    scene_check: str | None = None,
) -> dict:
    """All groups for one run, with where the truth and the reset mode came
    from. ``reset_mode``/``scene_check``: the run's, when the caller knows
    them (its job plan); otherwise read from the run (``mode_of``)."""
    events = list(events)
    records = episodes(events, manifest=manifest, termination=termination)
    outcome_truth = labels.truth("task_outcome", truth) if labels else {}
    scene_truth = labels.truth("initial_state", truth) if labels else {}
    mode = mode_of(
        events, manifest=manifest, reset_mode=reset_mode, scene_check=scene_check
    )
    decisions = scene_decisions(events)
    by_human = mode["scene_check"] == "operator_attested"
    run_automation = automation(events, mode["reset_mode"])
    return {
        "schema": "levi.aeri.metrics.v1",
        "truth": truth,
        "truth_labels": {
            "task_outcome": len(outcome_truth),
            "initial_state": len(scene_truth),
        },
        "note": "autonomous_* rates are the run's own verdicts, not ground truth",
        "reset_mode": mode["reset_mode"],
        "scene_check": mode["scene_check"],
        "mode_source": mode["source"],
        "comparable": list(COMPARABLE),
        "mode_specific": list(MODE_SPECIFIC),
        "autonomous": autonomous(records),
        "early_termination": early_termination(
            records, outcome_truth, max_steps=max_steps
        ),
        "reset": reset(records, decisions, scene_truth, events, by_human=by_human),
        "automation": run_automation,
        "turnaround": turnaround(events),
        "time_per_valid_episode_ms": time_per_valid_episode(events, records),
        "human_minutes_per_valid_episode": human_minutes(
            run_automation["person_ms"], records
        ),
        "scene_decisions_by_human": scene_by_human(decisions, mode["scene_check"]),
        "agreement": agreement(
            records, labels.latest("operator_label") if labels else {}
        ),
    }
