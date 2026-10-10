"""The trial schedule of a campaign: which arm runs which layout slots, in
what order (design X3 §1.2).

A campaign of ``k`` arms runs ``n`` trials per arm in segments of ``s``
trials. A segment is one ordinary AERI run of one arm; a round is a set of
slots (layout cards, or bare positions when there are no cards) that every
arm runs once, segment after segment.

Kinds (``KINDS``):

- ``counterbalanced_segments`` (default): each round deals ``s`` cards and
  every arm runs them in one segment; the arm order of each round is a row
  of a Williams design, so each arm directly follows each other arm equally
  often over a full cycle of rows (first-order carryover balance; Williams
  1949, doi:10.1071/CH9490149, ``williams1949`` in the X3 registry). Two
  arms alternate AB, BA.
- ``randomized_blocks``: a block is one card and every arm runs it once, in
  a seeded random order (``s`` is 1).
- ``latin_square``: the rows of a cyclic ``k x k`` Latin square, one per
  round (each arm in each position once per cycle).
- ``interleaved``: the fixed order A, B, ... every round (predictable;
  exploratory only).
- ``blocked``: all segments of A, then all of B... (order and drift are
  confounded; exploratory only).

Determinism: every random choice is a sort by ``sha256(seed, purpose,
item)``, so the same seed gives the same schedule in any process, Python
version or platform (no ``random`` module state, no hash seed).
"""

import hashlib
import json
import math
from dataclasses import dataclass

KINDS = (
    "counterbalanced_segments",
    "randomized_blocks",
    "latin_square",
    "interleaved",
    "blocked",
)
# The kinds a confirmatory conclusion may come from (other conditions apply:
# preregistration, drift checks...); the others are exploratory only.
CONFIRMATORY_KINDS = ("counterbalanced_segments", "randomized_blocks", "latin_square")
DEFAULT_SEGMENT_TRIALS = {
    "counterbalanced_segments": 5,
    "randomized_blocks": 1,
    "latin_square": 5,
    "interleaved": 1,
    "blocked": 5,
}
# Kinds whose segments always hold one trial.
SINGLE_TRIAL_KINDS = ("randomized_blocks",)
SCHEDULE_VERSION = 1


class ScheduleError(ValueError):
    pass


def conclusion_level(kind: str) -> str:
    """``confirmatory_eligible`` or ``exploratory`` (design X3 §1.2: blocked
    and interleaved orders support exploratory conclusions only)."""
    return "confirmatory_eligible" if kind in CONFIRMATORY_KINDS else "exploratory"


def _rank(seed: int, purpose: str, item) -> str:
    text = json.dumps([seed, purpose, item], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def permuted(items, seed: int, purpose: str) -> list:
    """``items`` in a seeded order (stable: ties broken by the item)."""
    return sorted(items, key=lambda item: (_rank(seed, purpose, item), str(item)))


def williams_rows(k: int) -> list[list[int]]:
    """The rows of a Williams design for ``k`` treatments: ``k`` rows when
    ``k`` is even, ``2k`` (the square and its mirror) when it is odd. Over
    all rows every ordered pair (i, j), i != j, appears as neighbours
    equally often, and every treatment holds every position equally often."""
    if k < 1:
        raise ScheduleError("a design needs at least one arm")
    first = [0]
    low, high, take_low = 1, k - 1, True
    while len(first) < k:
        if take_low:
            first.append(low)
            low += 1
        else:
            first.append(high)
            high -= 1
        take_low = not take_low
    rows = [[(x + shift) % k for x in first] for shift in range(k)]
    if k % 2:
        rows += [list(reversed(row)) for row in rows]
    return rows


def latin_rows(k: int) -> list[list[int]]:
    """A cyclic ``k x k`` Latin square."""
    if k < 1:
        raise ScheduleError("a design needs at least one arm")
    return [[(row + column) % k for column in range(k)] for row in range(k)]


def round_sizes(trials_per_arm: int, segment_trials: int) -> list[int]:
    """Trials per round: ``segment_trials`` each, the last one what is left."""
    if trials_per_arm < 1 or segment_trials < 1:
        raise ScheduleError("trials_per_arm and segment_trials are at least 1")
    rounds = math.ceil(trials_per_arm / segment_trials)
    last = trials_per_arm - segment_trials * (rounds - 1)
    return [segment_trials] * (rounds - 1) + [last]


def slot_name(position: int) -> str:
    """The slot of a campaign without layout cards (positions 1, 2...)."""
    return f"slot{position:02d}"


def deal(cards, sizes: list[int], seed: int) -> list[tuple[str, ...]]:
    """The slots of each round: ``sizes[r]`` distinct cards, dealt from a
    seeded deck that is reshuffled when it runs out (every card is used
    about equally often); within a round, in a seeded order. Without cards
    (``None``), the slots are bare positions."""
    if cards is None:
        return [tuple(slot_name(p) for p in range(1, size + 1)) for size in sizes]
    cards = list(cards)
    if len(set(cards)) != len(cards):
        raise ScheduleError("card ids repeat")
    if sizes and max(sizes) > len(cards):
        raise ScheduleError(
            f"a round needs {max(sizes)} distinct cards; the card set has {len(cards)}"
        )
    queue: list = []
    deck = 0
    out = []
    for number, size in enumerate(sizes, 1):
        chosen: list = []
        held: list = []
        while len(chosen) < size:
            if not queue:
                queue = permuted(cards, seed, f"deck{deck}")
                deck += 1
            card = queue.pop(0)
            if card in chosen:
                held.append(card)
            else:
                chosen.append(card)
        queue = held + queue
        out.append(tuple(permuted(chosen, seed, f"order{number}")))
    return out


@dataclass(frozen=True)
class Segment:
    """One run of one arm: ``index`` (1-based, in execution order), the
    round, the slots it runs (in order) and the arm that ran before it."""

    index: int
    arm: str
    round: int
    slots: tuple
    preceded_by: str | None

    @property
    def trials(self) -> int:
        return len(self.slots)


@dataclass(frozen=True)
class Trial:
    trial_id: str
    round: int
    slot: str
    arm: str
    segment: int
    order_index: int
    preceded_by_arm: str | None


@dataclass(frozen=True)
class Schedule:
    campaign_id: str
    kind: str
    seed: int
    arms: tuple
    trials_per_arm: int
    segment_trials: int
    rounds: tuple  # the slots of each round
    segments: tuple

    @property
    def conclusion_level(self) -> str:
        return conclusion_level(self.kind)

    @property
    def switches(self) -> int:
        """Policy starts the schedule needs: the first, then one at each
        change of arm between consecutive segments."""
        if not self.segments:
            return 0
        return 1 + sum(
            1
            for before, after in zip(self.segments, self.segments[1:])
            if before.arm != after.arm
        )

    def trials(self) -> list[Trial]:
        out = []
        order = 0
        for segment in self.segments:
            for slot in segment.slots:
                order += 1
                out.append(
                    Trial(
                        trial_id=f"{self.campaign_id}:{segment.round}:{slot}:{segment.arm}",
                        round=segment.round,
                        slot=slot,
                        arm=segment.arm,
                        segment=segment.index,
                        order_index=order,
                        preceded_by_arm=segment.preceded_by,
                    )
                )
        return out

    def to_json(self) -> dict:
        return {
            "version": SCHEDULE_VERSION,
            "kind": self.kind,
            "seed": self.seed,
            "arms": list(self.arms),
            "trials_per_arm": self.trials_per_arm,
            "segment_trials": self.segment_trials,
            "conclusion_level": self.conclusion_level,
            "switches": self.switches,
            "rounds": [list(slots) for slots in self.rounds],
            "segments": [
                {
                    "index": s.index,
                    "arm": s.arm,
                    "round": s.round,
                    "slots": list(s.slots),
                    "preceded_by": s.preceded_by,
                }
                for s in self.segments
            ],
        }


def _round_orders(kind: str, arms: list, rounds: int, seed: int) -> list[list]:
    """The arm order of each round (not used by ``blocked``)."""
    k = len(arms)
    symbols = permuted(arms, seed, "arms")  # index -> arm
    if kind == "interleaved":
        return [list(arms) for _ in range(rounds)]
    if kind == "randomized_blocks":
        return [permuted(arms, seed, f"block{r}") for r in range(1, rounds + 1)]
    rows = williams_rows(k) if kind == "counterbalanced_segments" else latin_rows(k)
    order = permuted(list(range(len(rows))), seed, "rows")
    return [[symbols[x] for x in rows[order[r % len(rows)]]] for r in range(rounds)]


def build(
    campaign_id: str,
    kind: str,
    arms,
    trials_per_arm: int,
    segment_trials: int | None,
    seed: int,
    cards=None,
) -> Schedule:
    """The schedule (pure: the same arguments give the same schedule)."""
    if kind not in KINDS:
        raise ScheduleError(
            f"unknown schedule kind {kind!r}; one of {', '.join(KINDS)}"
        )
    arms = sorted(arms)
    if len(arms) < 2 or len(set(arms)) != len(arms):
        raise ScheduleError("a schedule needs at least two distinct arms")
    if segment_trials is None:
        segment_trials = DEFAULT_SEGMENT_TRIALS[kind]
    if kind in SINGLE_TRIAL_KINDS and segment_trials != 1:
        raise ScheduleError(f"{kind}: a segment is one trial (segment_trials: 1)")
    sizes = round_sizes(trials_per_arm, segment_trials)
    slots = deal(cards, sizes, seed)
    plan = []  # (arm, round number)
    if kind == "blocked":
        for arm in arms:
            plan += [(arm, r) for r in range(1, len(sizes) + 1)]
    else:
        orders = _round_orders(kind, arms, len(sizes), seed)
        for r, order in enumerate(orders, 1):
            plan += [(arm, r) for arm in order]
    segments = []
    previous = None
    for index, (arm, r) in enumerate(plan, 1):
        segments.append(Segment(index, arm, r, slots[r - 1], previous))
        previous = arm
    return Schedule(
        campaign_id=campaign_id,
        kind=kind,
        seed=seed,
        arms=tuple(arms),
        trials_per_arm=trials_per_arm,
        segment_trials=segment_trials,
        rounds=tuple(slots),
        segments=tuple(segments),
    )
