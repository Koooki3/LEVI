"""Campaign schedules (levi/automatic/campaign/schedule.py, T-CP-02): Williams
balance, Latin squares, card dealing, determinism, conclusion levels."""

import collections
import itertools
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from levi.automatic.campaign import schedule as sched

ROOT = Path(__file__).resolve().parents[3]
ARMS = "ABCDEFGH"


def carryover(rows):
    """Ordered neighbour pairs within each row."""
    return collections.Counter(
        (row[i], row[i + 1]) for row in rows for i in range(len(row) - 1)
    )


@pytest.mark.parametrize("k", range(2, 9))
def test_williams_rows_balance_first_order_carryover_and_position(k):
    rows = sched.williams_rows(k)
    assert len(rows) == (k if k % 2 == 0 else 2 * k)
    assert all(sorted(row) == list(range(k)) for row in rows)
    pairs = carryover(rows)
    wanted = {(i, j) for i in range(k) for j in range(k) if i != j}
    assert set(pairs) == wanted
    assert len(set(pairs.values())) == 1  # every ordered pair equally often
    for position in range(k):
        column = collections.Counter(row[position] for row in rows)
        assert len(set(column.values())) == 1 and len(column) == k


def test_two_arms_alternate_ab_ba():
    built = sched.build("c", "counterbalanced_segments", ["A", "B"], 10, 2, seed=1)
    orders = [
        "".join(s.arm for s in built.segments if s.round == r) for r in range(1, 6)
    ]
    assert set(orders) == {"AB", "BA"}
    assert all(a != b for a, b in itertools.pairwise(orders))  # alternating


@pytest.mark.parametrize("k", [2, 3, 4])
@pytest.mark.parametrize("seed", [0, 7, 123456789])
def test_counterbalanced_schedules_are_balanced_over_full_cycles(k, seed):
    rows = len(sched.williams_rows(k))
    segment = 2
    trials = rows * segment  # exactly one full cycle of Williams rows
    built = sched.build(
        "c", "counterbalanced_segments", ARMS[:k], trials, segment, seed
    )
    by_round = collections.defaultdict(list)
    for s in built.segments:
        by_round[s.round].append(s)
    assert len(by_round) == rows
    orders = [[s.arm for s in by_round[r]] for r in sorted(by_round)]
    pairs = carryover(orders)
    assert len(pairs) == k * (k - 1) and len(set(pairs.values())) == 1
    for position in range(k):
        assert len({o[position] for o in orders}) == k
        counts = collections.Counter(o[position] for o in orders)
        assert len(set(counts.values())) == 1
    # Every arm runs the same slots in a round (same cards, same order).
    for segments in by_round.values():
        assert len({s.slots for s in segments}) == 1
    trials_per_arm = collections.Counter(t.arm for t in built.trials())
    assert set(trials_per_arm.values()) == {trials}


@pytest.mark.parametrize("k", [2, 3, 4, 5])
def test_latin_square_puts_each_arm_in_each_position_once_per_cycle(k):
    built = sched.build("c", "latin_square", ARMS[:k], k * 3, 3, seed=5)
    orders = collections.defaultdict(list)
    for s in built.segments:
        orders[s.round].append(s.arm)
    rows = [orders[r] for r in sorted(orders)]
    assert len(rows) == k
    for position in range(k):
        assert sorted(row[position] for row in rows) == list(ARMS[:k])


def test_randomized_blocks_run_every_arm_once_per_block_card():
    cards = [f"c{i}" for i in range(1, 6)]
    built = sched.build("c", "randomized_blocks", "ABC", 10, None, seed=3, cards=cards)
    assert built.segment_trials == 1
    assert all(s.trials == 1 for s in built.segments)
    by_round = collections.defaultdict(list)
    for s in built.segments:
        by_round[s.round].append(s)
    assert len(by_round) == 10
    for segments in by_round.values():
        assert sorted(s.arm for s in segments) == ["A", "B", "C"]
        assert len({s.slots for s in segments}) == 1
    # The order differs between blocks (random), deterministic per seed.
    assert len({tuple(s.arm for s in v) for v in by_round.values()}) > 1
    with pytest.raises(sched.ScheduleError):
        sched.build("c", "randomized_blocks", "AB", 4, 2, seed=1)


def test_interleaved_and_blocked_are_exploratory():
    inter = sched.build("c", "interleaved", "AB", 3, None, seed=9)
    assert [s.arm for s in inter.segments] == list("ABABAB")
    blocked = sched.build("c", "blocked", "AB", 4, 2, seed=9)
    assert [s.arm for s in blocked.segments] == list("AABB")
    assert blocked.switches == 2
    assert inter.conclusion_level == blocked.conclusion_level == "exploratory"
    for kind in sched.CONFIRMATORY_KINDS:
        built = sched.build(
            "c", kind, "AB", 4, 1 if kind == "randomized_blocks" else 2, 1
        )
        assert built.conclusion_level == "confirmatory_eligible"


def test_the_same_seed_gives_the_same_schedule_and_another_seed_another():
    cards = [f"c{i:02d}" for i in range(1, 9)]
    first = sched.build("c", "counterbalanced_segments", "ABC", 12, 4, 42, cards)
    again = sched.build("c", "counterbalanced_segments", "CBA", 12, 4, 42, cards[::-1])
    assert first.to_json() == again.to_json()
    others = {
        json.dumps(
            sched.build(
                "c", "counterbalanced_segments", "ABC", 12, 4, s, cards
            ).to_json()
        )
        for s in range(8)
    }
    assert len(others) > 1


def test_the_schedule_does_not_depend_on_the_process_or_hash_seed():
    code = (
        "import json,sys; from levi.automatic.campaign import schedule as s; "
        "b=s.build('c','counterbalanced_segments','ABCD',9,2,11,"
        "['k%d'%i for i in range(7)]); sys.stdout.write(json.dumps(b.to_json()))"
    )
    outputs = set()
    for seed in ("0", "1", "4242"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        env["PYTHONPATH"] = os.pathsep.join(
            filter(None, [str(ROOT), env.get("PYTHONPATH")])
        )
        outputs.add(
            subprocess.run(
                [sys.executable, "-c", code],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
    assert len(outputs) == 1


def test_cards_are_distinct_within_a_round_and_used_evenly():
    cards = [f"c{i}" for i in range(7)]
    rounds = sched.deal(cards, [3] * 14, seed=2)
    assert all(len(set(r)) == len(r) == 3 for r in rounds)
    used = collections.Counter(itertools.chain.from_iterable(rounds))
    assert set(used) == set(cards)
    assert max(used.values()) - min(used.values()) <= 1
    with pytest.raises(sched.ScheduleError):
        sched.deal(["a", "b"], [3], seed=1)
    assert sched.deal(None, [2, 1], 0) == [("slot01", "slot02"), ("slot01",)]


def test_rounds_absorb_a_remainder_and_trials_are_numbered():
    built = sched.build("cx", "counterbalanced_segments", "AB", 5, 2, seed=1)
    assert [len(r) for r in built.rounds] == [2, 2, 1]
    trials = built.trials()
    assert [t.order_index for t in trials] == list(range(1, 11))
    assert trials[0].trial_id.startswith("cx:1:slot01:")
    assert trials[0].preceded_by_arm is None
    second = next(t for t in trials if t.segment == 2)
    assert second.preceded_by_arm == built.segments[0].arm


def test_unknown_kinds_and_single_arms_are_refused():
    with pytest.raises(sched.ScheduleError):
        sched.build("c", "zigzag", "AB", 4, 2, 1)
    with pytest.raises(sched.ScheduleError):
        sched.build("c", "blocked", "A", 4, 2, 1)
    with pytest.raises(sched.ScheduleError):
        sched.round_sizes(0, 2)
