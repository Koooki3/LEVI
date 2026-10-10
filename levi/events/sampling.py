"""Share a refinement's frame budget among windows, greedily, in tiers.

The engineering version of the design note's Overview -> Skim -> Focus:
no model decides where to look; LEVI does, by a fixed rule, within the
plan's frame cap (``workflow.max_evidence_frames``, less the coarse frames
already read) and the model's image limit:

1. **required**: global context and the draft. The coarse pass already
   spent its frames (the caller takes them off the cap); the windows around
   the draft's own boundaries come next and are never dropped. When they
   alone do not fit, ``plan`` raises ``Overflow`` (or ``ContextOverflow``
   for the image limit), as ``observations.frame_scope`` does: frames are
   never silently thinned.
2. **candidates**: windows around event candidates from the recorded
   signals, in their evidence priority, at most ``max_windows``.
3. **uncertain**: the coarse intervals where the picture changed most
   (``observations.changes``, the published ``evidence.refine_top_k``).

Within a tier every window is whole or absent: a window that does not fit is
skipped and the next, possibly smaller, one is tried. A window whose frames
are all already chosen costs nothing; it is recorded as ``covered``. Frame
positions are computed exactly as ``observations.frame_scope`` computes them
for a boundary (tested), so the frames planned here are the frames read.

Pure: arrays and numbers in, a ``Plan`` out.
"""

from dataclasses import dataclass, field

import numpy as np

REQUIRED, CANDIDATES, UNCERTAIN = "required", "candidates", "uncertain"
# The candidate readers and this planner, as an on plan freezes them
# (``candidates.algorithm``). Raise it with any change to how candidates are
# found, merged or ordered or how windows are chosen that its constants do
# not show: an approved plan must then be approved again.
PLANNER_VERSION = "levi.events.evidence_planner.v1"
# Most entries of one list in a plan's summary (the counts stay exact).
SUMMARY_ROWS = 32


def window_positions(times, at, window, spacing):
    """Table rows of the window around ``at``: the row nearest each instant
    from ``window`` before to ``window`` after, ``spacing`` apart, clipped to
    the episode. The arithmetic of ``observations.frame_scope``."""
    targets = np.arange(
        max(times[0], at - window),
        min(times[-1], at + window) + 1e-8,
        spacing,
    )
    return frozenset(int(np.abs(times - t).argmin()) for t in targets)


@dataclass(frozen=True)
class Window:
    """One window that may be refined: where, why, and how much it adds."""

    at: float
    tier: str
    key: str
    positions: frozenset
    detail: dict = field(default_factory=dict, compare=False)


@dataclass
class Plan:
    """Which windows a refinement reads. ``accepted`` in the order they were
    taken; ``covered`` cost nothing; ``skipped`` did not fit (``reason``:
    ``budget`` or ``max_windows``)."""

    positions: frozenset
    images: int
    budget: int
    cameras: int
    required_frames: int
    accepted: list = field(default_factory=list)
    covered: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    added: dict = field(default_factory=dict)

    def windows(self, tier=None):
        """Centres (seconds) of the accepted windows, of one tier or all."""
        return [w.at for w, _ in self.accepted if tier is None or w.tier == tier]

    def summary(self):
        """A compact, JSON-ready record of the plan (for run events and
        provenance)."""

        def row(w, frames=None):
            out = {"tier": w.tier, "key": w.key, "at": round(w.at, 3), **w.detail}
            if frames is not None:
                out["frames"] = frames
            return out

        return {
            "budget_images": self.budget,
            "images": self.images,
            "cameras": self.cameras,
            "required_frames": self.required_frames,
            "added_frames": dict(self.added),
            "accepted": [row(w, n) for w, n in self.accepted[:SUMMARY_ROWS]],
            "covered": [row(w) for w in self.covered[:SUMMARY_ROWS]],
            "skipped": [
                {**row(w), "reason": reason}
                for w, reason in self.skipped[:SUMMARY_ROWS]
            ],
            "counts": {
                "accepted": len(self.accepted),
                "covered": len(self.covered),
                "skipped": len(self.skipped),
            },
        }


def _images(positions, cameras):
    return len(positions) * max(1, cameras)


def plan(
    times,
    *,
    required,
    tiers,
    window,
    spacing,
    cameras,
    cap,
    limit=None,
    max_windows=None,
    held=frozenset(),
    held_images=0,
    ledger_cap=None,
):
    """The windows a refinement reads.

    ``required``: boundary instants that must be read (seconds). ``tiers``:
    ``[(name, [(at, key, detail), ...]), ...]`` in priority order, each list
    already in its own order. ``max_windows``: ``{tier: n}``, the most
    windows (that add frames) a tier may take. ``cap``: the frame cap left
    for this refinement, in images (frames x cameras); ``limit``: the
    model's image limit, or None.

    ``ledger_cap``: the plan's frame cap for the whole episode, checked
    against what the episode's evidence ledger will hold afterwards:
    ``held_images`` (its rows now) plus every camera's frame at each chosen
    position not in ``held`` (positions every camera already holds). A
    refinement whose frames were persisted but whose request never finished
    is planned again on resume, under the image limit of that moment; greedy
    plans under two limits are not nested, so without this the two together
    could pass the cap. On a first refinement the ledger holds only the
    coarse frames, which ``cap`` already leaves out, and the plan is the one
    made without it (tested).
    """
    from levi.agent.observations import ContextOverflow, Overflow

    times = np.asarray(times, dtype=float)
    cameras = max(1, int(cameras))
    chosen = set()
    for at in required:
        chosen |= window_positions(times, at, window, spacing)

    def ledger_ok(positions):
        return (
            ledger_cap is None
            or held_images + _images(set(positions) - set(held), cameras) <= ledger_cap
        )

    def check(positions):
        images = _images(positions, cameras)
        if images > cap or not ledger_ok(positions):
            raise Overflow(
                "Observation coverage exceeds approved frame cap; revise the "
                "plan, do not silently undersample"
            )
        if limit is not None and images > limit:
            raise ContextOverflow(
                f"{images} images exceed the {limit} the model's context holds"
            )

    check(chosen)
    budget = cap if limit is None else min(cap, limit)
    result = Plan(
        positions=frozenset(),
        images=0,
        budget=budget,
        cameras=cameras,
        required_frames=len(chosen),
        added={REQUIRED: len(chosen)},
    )
    taken = {}
    for tier, items in tiers:
        most = (max_windows or {}).get(tier)
        result.added.setdefault(tier, 0)
        for at, key, detail in items:
            w = Window(
                float(at),
                tier,
                str(key),
                window_positions(times, float(at), window, spacing),
                dict(detail or {}),
            )
            new = w.positions - chosen
            if not new:
                result.covered.append(w)
                continue
            if most is not None and taken.get(tier, 0) >= most:
                result.skipped.append((w, "max_windows"))
                continue
            if _images(chosen | new, cameras) > budget or not ledger_ok(chosen | new):
                result.skipped.append((w, "budget"))
                continue
            chosen |= new
            taken[tier] = taken.get(tier, 0) + 1
            result.added[tier] += len(new)
            result.accepted.append((w, len(new)))
    if not chosen:
        # frame_scope reads the first and last frame when nothing is asked.
        chosen = {0, len(times) - 1}
        check(chosen)
    result.positions = frozenset(chosen)
    result.images = _images(chosen, cameras)
    return result


def legacy(times, *, required, ranked, window, spacing, cameras, cap, limit=None):
    """What the refinement reads without event intelligence: the draft's
    boundaries plus the published change windows, dropped from the least
    changed (the end of ``ranked``) until the rest fit -- the rule of
    ``observations.harness_windows``. Returns the kept window centres, or
    raises like ``plan`` when the draft alone does not fit. For like-for-like
    comparisons (``ablation``)."""
    times = np.asarray(times, dtype=float)
    # Raises when the draft alone does not fit, as ``plan`` does.
    base = plan(
        times,
        required=required,
        tiers=[],
        window=window,
        spacing=spacing,
        cameras=cameras,
        cap=cap,
        limit=limit,
    )
    drafted = set()
    for at in required:
        drafted |= window_positions(times, at, window, spacing)
    kept = list(ranked)
    while kept:
        chosen = set(drafted)
        for at in kept:
            chosen |= window_positions(times, at, window, spacing)
        if _images(chosen, cameras) <= base.budget:
            return kept, frozenset(chosen)
        kept = kept[:-1]
    return [], base.positions
