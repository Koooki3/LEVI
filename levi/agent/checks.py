"""Problems in staged temporal segments, named only when there is one.

A staging receipt used to invite a routine boundary check for every episode;
agents looked and almost never changed anything (2026-09-23: 20 checks, one
re-staging). These checks run on what was staged and speak only when the
segments break a general annotation rule (built-in knowledge annotation-003,
-007 and -012) -- with the times, so the agent looks exactly there.

- ``meet``: two intervals of the same subtask touch. Either the effector
  never let go (one attempt) or a new approach lies between them.
- ``unknown``: an outcome is ``unknown`` although the recording goes on to
  show the robot doing something with a known outcome right after it --
  ``unknown`` is for what the recording does not show.

Measured against a full-dataset reference (2026-09-24): ``unknown`` lines
were right 78-90% of the time. A third check -- the recorded gripper opening
and the arm rising inside one interval, as a sign of two attempts -- was
right about half the time and dropped: whether the effector *left* the
object is a judgement on the frames (a short lift back onto the same rim is
the same attempt), and signals that ask for splits make agents over-split.

None of these place a boundary; the frames still decide.
"""

import re
from itertools import pairwise

UNKNOWN = "unknown"
# A subtask whose plan says its outcome is always unknown (an idle / "other"
# class) is never asked to settle one.
ALWAYS_UNKNOWN = re.compile(r"\balways\W+(?:\w+\W+){0,2}`?unknown", re.IGNORECASE)


def always_unknown(definitions):
    """Subtask ids whose plan definition makes ``unknown`` their only outcome."""
    return {
        d["id"]
        for d in definitions or []
        if ALWAYS_UNKNOWN.search(d.get("success_when") or "")
    }


def _t(x):
    return f"{x:.1f}"


def meet(segments):
    out = []
    for a, b in pairwise(segments):
        if a["subtask"] == b["subtask"] and abs(a["end"] - b["start"]) < 1e-6:
            out.append(
                f"{a['subtask']} {_t(a['start'])}–{_t(a['end'])} and "
                f"{_t(b['start'])}–{_t(b['end'])} meet: one attempt if the "
                "effector never let go, otherwise an approach lies between them"
            )
    return out


def unknown(segments, exempt=()):
    out = []
    for a, b in pairwise(segments):
        if a["subtask"] in exempt:
            continue
        if a.get("outcome") == UNKNOWN and b.get("outcome") not in (None, UNKNOWN):
            out.append(
                f"{a['subtask']} {_t(a['start'])}–{_t(a['end'])} is unknown, but "
                f"the recording goes on ({b['subtask']} {b['outcome']} from "
                f"{_t(b['start'])}): what the next segment shows settles it"
            )
    return out


def staged(segments, exempt=()):
    """Every problem of one episode's staged segments (proposal dicts with
    ``subtask_id`` or plain ``subtask`` rows), layer by layer."""
    layers = {}
    for s in segments:
        if s.get("end") is None:
            continue
        layers.setdefault(s.get("layer") or "activity", []).append(
            {
                "subtask": s.get("subtask_id") or s.get("subtask"),
                "start": float(s["start"]),
                "end": float(s["end"]),
                "outcome": s.get("outcome"),
            }
        )
    out = []
    for rows in layers.values():
        rows.sort(key=lambda s: s["start"])
        out += meet(rows) + unknown(rows, exempt)
    return out
