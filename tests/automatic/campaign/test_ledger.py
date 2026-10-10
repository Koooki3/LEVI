"""The campaign trial ledger (T-CP-05, levi/automatic/campaign/ledger.py):
trial ids, slots and order; discarded, incomplete, rerun and deviated
counts; cards a person has not confirmed stay out of the pairs; the five
label bases; idempotent re-derivation; and an AERI run read from disk."""

import json
from pathlib import Path

import pytest
import test_ledger_fixtures as fx
from campaign_guard import aeri_home_fixture, guard_fixture  # noqa: F401

from levi.automatic.campaign import ledger as L


def rows_of(led, arm=None):
    return [r for r in led.rows if arm is None or r.arm == arm]


# ------------------------------------------------------------------ derivation


def test_trial_ids_slots_order_and_preceding_arm():
    lay = fx.layout(per_arm=10, segment_trials=5)
    led = L.derive(lay, fx.synthetic_facts(lay))
    assert len(led.rows) == 20 and not led.unplanned_runs
    first = led.rows[0]
    assert first.trial_id == f"{fx.CAMPAIGN}:1:r01c1:A"
    assert (first.slot, first.segment, first.order_index) == (1, 1, 0)
    assert first.preceded_by_arm is None and first.rerun_of is None
    # AB in round 1, BA in round 2: segment 2 (B) follows A, segment 3 (B)
    # follows B, segment 4 (A) follows B.
    assert [(r.segment, r.arm, r.preceded_by_arm) for r in led.rows[::5]] == [
        (1, "A", None),
        (2, "B", "A"),
        (3, "B", "B"),
        (4, "A", "B"),
    ]
    assert [r.order_index for r in led.rows] == list(range(20))
    assert all(
        r.card_source == "run_manifest" or r.card_source == "schedule_order"
        for r in led.rows
    )
    assert all(r.pairable for r in led.rows)
    c = L.counts(led, lay)
    assert c["A"]["planned"] == c["B"]["planned"] == 10
    assert c["A"]["missing"] == c["B"]["missing"] == 0


def test_aeri_episodes_without_a_card_take_the_next_unfinished_one():
    lay = fx.layout(per_arm=5, segment_trials=5, arms=("A", "B"))
    run = lay.segments[0].run_ids[0]
    facts = {run: [fx.fact(run, n, max_steps=300) for n in range(1, 4)]}
    led = L.derive(lay, facts)
    assert [(r.card, r.card_source, r.slot) for r in led.rows] == [
        ("r01c1", "schedule_order", 1),
        ("r01c2", "schedule_order", 2),
        ("r01c3", "schedule_order", 3),
    ]
    c = L.counts(led, lay)
    assert c["A"]["missing"] == 2 and c["B"]["missing"] == 5


def test_discarded_and_incomplete_hold_no_card_and_a_rerun_fills_the_rest():
    lay = fx.layout(per_arm=5, segment_trials=5, reruns={1: 1})
    first, rerun = lay.segments[0].run_ids
    facts = {
        first: [
            fx.fact(first, 1),
            fx.fact(
                first, 2, status="discarded", labels={"operator_label": "discarded"}
            ),
            fx.fact(first, 3),
            fx.fact(first, 4, status="incomplete"),
        ],
        rerun: [fx.fact(rerun, 1), fx.fact(rerun, 2), fx.fact(rerun, 3)],
    }
    led = L.derive(lay, facts)
    cards = [(r.run_id == rerun, r.status, r.card) for r in rows_of(led, "A")]
    assert cards == [
        (False, "valid", "r01c1"),
        (False, "discarded", None),
        (False, "valid", "r01c2"),
        (False, "incomplete", None),
        (True, "valid", "r01c3"),
        (True, "valid", "r01c4"),
        (True, "valid", "r01c5"),
    ]
    assert {r.rerun_of for r in rows_of(led, "A") if r.run_id == rerun} == {first}
    c = L.counts(led, lay)["A"]
    assert c == {
        "rows": 7,
        "valid": 5,
        "discarded": 1,
        "incomplete": 1,
        "deviated": 0,
        "from_reruns": 3,
        "unconfirmed": 0,
        "card_problems": 0,
        "pairable": 5,
        "planned": 5,
        "missing": 0,
    }
    # A discarded episode has no trial id and is never paired.
    assert all(
        r.trial_id is None and not r.pairable for r in led.rows if r.status != "valid"
    )


def test_legacy_episodes_need_a_person_to_confirm_their_card():
    lay = fx.layout(per_arm=5, segment_trials=5)
    run = lay.segments[0].run_ids[0]
    facts = {
        run: [
            fx.fact(run, 1, source="legacy_client", card="r01c1"),
            fx.fact(run, 2, source="legacy_client"),  # not confirmed
            fx.fact(run, 3, source="legacy_client", card="r01c3"),
            fx.fact(run, 4, source="legacy_client", card="r01c3"),  # twice
            fx.fact(run, 5, source="legacy_client", card="r09c9"),  # elsewhere
        ]
    }
    led = L.derive(lay, facts)
    got = [
        (r.card, r.card_source, r.candidate_card, r.card_problem, r.pairable)
        for r in led.rows
    ]
    assert got == [
        ("r01c1", "operator_confirmed", None, None, True),
        (None, "unconfirmed", "r01c2", None, False),
        ("r01c3", "operator_confirmed", None, None, True),
        ("r01c3", "operator_confirmed", None, "duplicate", False),
        ("r09c9", "operator_confirmed", None, "not_in_segment", False),
    ]
    c = L.counts(led, lay)["A"]
    assert (c["unconfirmed"], c["card_problems"], c["pairable"], c["missing"]) == (
        1,
        2,
        2,
        3,
    )


def test_a_valid_episode_beyond_the_last_card_is_flagged():
    lay = fx.layout(per_arm=5, segment_trials=5)
    run = lay.segments[0].run_ids[0]
    facts = {run: [fx.fact(run, n) for n in range(1, 7)]}
    led = L.derive(lay, facts)
    assert led.rows[-1].card_problem == "no_card_left"
    assert not led.rows[-1].pairable and led.rows[-1].trial_id is None


def test_deviated_layouts_are_kept_counted_and_need_a_reason():
    lay = fx.layout(per_arm=5, segment_trials=5)
    run = lay.segments[0].run_ids[0]
    with pytest.raises(L.LedgerError, match="reason"):
        fx.fact(run, 1, layout_fidelity="deviated")
    facts = {
        run: [
            fx.fact(run, 1, layout_fidelity="deviated", layout_reason="cup 2 cm left"),
            fx.fact(run, 2, layout_fidelity="verified"),
        ]
    }
    led = L.derive(lay, facts)
    assert led.rows[0].pairable and led.rows[0].layout_fidelity == "deviated"
    assert L.counts(led)["A"]["deviated"] == 1
    with pytest.raises(L.LedgerError, match="layout_fidelity"):
        fx.fact(run, 3, layout_fidelity="roughly")


def test_bad_layouts_and_facts_are_refused():
    seg = L.SegmentPlan(1, "A", 1, ("c1",), ("run-1",))
    with pytest.raises(L.LedgerError, match="segments 1 and 2"):
        L.CampaignLayout("c1", (seg, L.SegmentPlan(2, "B", 1, ("c1",), ("run-1",))))
    with pytest.raises(L.LedgerError, match="card twice"):
        L.SegmentPlan(1, "A", 1, ("c1", "c1"))
    with pytest.raises(L.LedgerError, match="not a valid id"):
        L.SegmentPlan(1, "A/B", 1, ("c1",))
    with pytest.raises(L.LedgerError, match="not a valid id"):
        L.CampaignLayout("../etc", (seg,))
    lay = L.CampaignLayout("c1", (seg,))
    with pytest.raises(L.LedgerError, match="listed under run"):
        L.derive(lay, {"run-1": [fx.fact("run-2", 1)]})
    with pytest.raises(L.LedgerError, match="appears twice"):
        L.derive(lay, {"run-1": [fx.fact("run-1", 1), fx.fact("run-1", 1)]})
    with pytest.raises(L.LedgerError, match="label kind"):
        fx.fact("run-1", 1, labels={"truth": "success"})
    with pytest.raises(L.LedgerError, match="autonomous_verdict"):
        fx.fact("run-1", 1, labels={"autonomous_verdict": "discarded"})


def test_runs_no_segment_names_are_left_out_and_listed():
    lay = fx.layout(per_arm=5, segment_trials=5)
    run = lay.segments[0].run_ids[0]
    led = L.derive(lay, {run: [fx.fact(run, 1)], "stray": [fx.fact("stray", 1)]})
    assert led.unplanned_runs == ["stray"] and len(led.rows) == 1


# --------------------------------------------------------------------- labels


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (
            {
                "autonomous_verdict": "success",
                "posthoc_verdict": "failure",
                "operator_label": "failure",
                "adjudicated_ground_truth": "success",
            },
            {
                "autonomous_verdict": ("success", "autonomous_verdict"),
                "posthoc_verdict": ("failure", "posthoc_verdict"),
                "operator_label": ("failure", "operator_label"),
                "adjudicated_ground_truth": ("success", "adjudicated_ground_truth"),
                "adjudicated_then_operator": ("success", "adjudicated_ground_truth"),
            },
        ),
        (
            {"autonomous_verdict": "undecided", "operator_label": "success"},
            {
                "autonomous_verdict": (None, None),
                "posthoc_verdict": (None, None),
                "operator_label": ("success", "operator_label"),
                "adjudicated_ground_truth": (None, None),
                "adjudicated_then_operator": ("success", "operator_label"),
            },
        ),
        (
            {"autonomous_verdict": "none", "operator_label": "unclear"},
            dict.fromkeys(L.LABEL_BASES, (None, None)),
        ),
        (
            {"operator_label": "discarded"},
            dict.fromkeys(L.LABEL_BASES, (None, None)),
        ),
    ],
)
def test_label_bases_never_mix(labels, expected):
    entry = {"labels": labels}
    assert {b: L.label_value(entry, b) for b in L.LABEL_BASES} == expected


def test_an_unknown_basis_is_refused():
    with pytest.raises(L.LedgerError, match="label basis"):
        L.label_value({"labels": {}}, "truth")


# ----------------------------------------------------------------- the file


def test_rederiving_gives_the_same_bytes_and_writes_nothing(tmp_path):
    lay = fx.layout(per_arm=10, segment_trials=5)
    facts = fx.synthetic_facts(lay)
    path = tmp_path / "campaign" / L.LEDGER_FILE
    assert L.write_ledger(path, L.derive(lay, facts)) is True
    first = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    assert L.write_ledger(path, L.derive(lay, facts)) is False
    assert path.read_bytes() == first and path.stat().st_mtime_ns == mtime
    rows = L.read_ledger(path)
    assert rows == L.derive(lay, facts).rows
    assert not list(path.parent.glob("*.partial"))
    path.write_bytes(first + b'{"schema": "other"}\n')
    with pytest.raises(L.LedgerError, match="line 21"):
        L.read_ledger(path)


# ---------------------------------------------------------------- AERI runs


@pytest.fixture
def aeri_run(tmp_path, monkeypatch):
    # The campaign guard (autouse) already refuses every connect. A second
    # monkeypatch of socket.socket.connect here would be undone AFTER the
    # guard's uninstall and leave the guard's connect in place for every later
    # test (the live-service tests then could not reach their own server).
    # Only for this test (undone afterwards): a module-level sys.path insert
    # stays for the whole session and changed which modules later tests import.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    from aeri_harness import config
    from test_aeri_recorder import FOLDERS, build_run

    cfg = config(
        episodes=3, forward_folder=FOLDERS["forward"], reset_folder=FOLDERS["reset"]
    )
    r = build_run(tmp_path, cfg=cfg)
    assert r.orch.run() == "COMPLETED"
    r.orch.close() if hasattr(r.orch, "close") else None
    return r


def test_an_aeri_run_is_read_from_its_manifest_journal_and_labels(aeri_run):
    from levi.automatic import metrics

    run_dir = aeri_run.directory
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    forward = [e for e in manifest["episodes"] if e["role"] == "forward"]
    assert len(forward) == 3
    # The campaign fields the recorder will write (design §1.6): a card
    # and a deviated layout on the second episode.
    forward[1]["campaign"] = {
        "card": "r01c2",
        "layout_fidelity": "deviated",
        "layout_reason": "plate rotated",
    }
    manifest_path.write_text(json.dumps(manifest))
    first, second, third = (e["episode_id"] for e in forward)
    metrics.label_operator(run_dir, first, "success", by="op-1")
    metrics.label_operator(run_dir, third, "discarded", by="op-1")
    metrics.LabelStore(run_dir).add(
        "adjudicated_ground_truth", second, "failure", by="rev-1"
    )
    # CL14: the first decided label is written blind; a change after the
    # verdict was revealed is a revision, and the blind value is kept.
    metrics.label_operator(run_dir, second, "failure", by="op-1")
    metrics.label_operator(run_dir, second, "success", by="op-1")
    facts, run = L.read_aeri_run(run_dir, max_steps=40)
    assert run["run_id"] == aeri_run.config.run_id
    assert run["state"] == "COMPLETED" and run["plan_sha256"] == "ab" * 32
    assert [f.status for f in facts] == ["valid", "valid", "discarded"]
    a, b, c = facts
    assert a.labels["operator_label"] == "success"
    assert a.labels["autonomous_verdict"] in L.VERDICTS
    assert a.card is None and b.card == "r01c2" and b.card_source == "run_manifest"
    assert b.layout_fidelity == "deviated" and b.layout_reason == "plate rotated"
    assert b.labels["adjudicated_ground_truth"] == "failure"
    assert b.labels["operator_label"] == "success"
    assert (b.operator_blind, b.revised_after_reveal) == ("failure", True)
    assert (a.operator_blind, a.revised_after_reveal) == ("success", False)
    assert c.labels["operator_label"] == "discarded"
    assert a.steps is not None and a.max_steps == 40 and a.ended_at.endswith("Z")
    assert a.stop_reason in ("goal_verified", "horizon_exhausted")
    lay = L.CampaignLayout(
        "c1",
        (L.SegmentPlan(1, "A", 1, ("r01c1", "r01c2", "r01c3"), (run["run_id"],)),),
    )
    led = L.derive(lay, {run["run_id"]: facts})
    # The first episode takes the next unfinished card (r01c1); the second
    # holds the card its manifest names.
    assert [(r.card, r.status) for r in led.rows] == [
        ("r01c1", "valid"),
        ("r01c2", "valid"),
        (None, "discarded"),
    ]
    # Reading never writes the run's files.
    before = {p: p.stat().st_mtime_ns for p in run_dir.rglob("*") if p.is_file()}
    L.read_aeri_run(run_dir)
    after = {p: p.stat().st_mtime_ns for p in run_dir.rglob("*") if p.is_file()}
    assert before == after


def test_a_corrupt_journal_is_refused(aeri_run):
    from levi.automatic.journal import JOURNAL

    journal = aeri_run.directory / JOURNAL
    lines = journal.read_bytes().split(b"\n")
    edited = json.loads(lines[1])
    edited["prev_sha256"] = "0" * 64
    lines[1] = json.dumps(edited).encode()
    journal.write_bytes(b"\n".join(lines))
    with pytest.raises(L.LedgerError, match="corrupt"):
        L.read_aeri_run(aeri_run.directory)


def test_the_blind_label_is_kept_apart_from_the_current_one():
    f = fx.fact("run-1", 1, labels={"operator_label": "success"})
    assert f.operator_blind == "success"  # nothing revised: the same value
    revised = fx.fact(
        "run-1",
        2,
        labels={"operator_label": "success"},
        revised_after_reveal=True,
        operator_blind="failure",
    )
    lay = L.CampaignLayout("c1", (L.SegmentPlan(1, "A", 1, ("c1", "c2"), ("run-1",)),))
    led = L.derive(lay, {"run-1": [f, revised]})
    entry = led.labels[revised.episode_id]
    assert entry["operator_blind"] == "failure" and entry["revised_after_reveal"]
    assert L.label_value(entry, "operator_label") == ("success", "operator_label")
    with pytest.raises(L.LedgerError, match="blind"):
        fx.fact(
            "run-1", 3, labels={"operator_label": "success"}, revised_after_reveal=True
        )


def test_one_arm_cannot_hold_a_card_twice_in_a_round_across_segments():
    segs = (
        L.SegmentPlan(1, "A", 1, ("c1", "c2"), ("r1",)),
        L.SegmentPlan(2, "A", 1, ("c1", "c2"), ("r2",)),
    )
    lay = L.CampaignLayout("c1", segs)
    led = L.derive(
        lay,
        {
            "r1": [fx.fact("r1", 1, card="c1"), fx.fact("r1", 2, card="c2")],
            "r2": [fx.fact("r2", 1, card="c1"), fx.fact("r2", 2)],
        },
    )
    assert [(r.card, r.card_problem) for r in led.rows] == [
        ("c1", None),
        ("c2", None),
        ("c1", "duplicate"),
        (None, "no_card_left"),
    ]
    ids = [r.trial_id for r in led.rows if r.trial_id]
    assert len(ids) == len(set(ids)) == 2


def test_a_reset_policy_campaign_assigns_no_cards():
    lay = fx.layout(per_arm=5, segment_trials=5, layout_source="none")
    led = L.derive(lay, fx.synthetic_facts(lay))
    assert all(r.card is None and r.card_problem is None for r in led.rows)
    c = L.counts(led)
    assert c["A"]["card_problems"] == 0 and c["A"]["unconfirmed"] == 0


def test_a_bad_episode_id_in_a_run_manifest_is_a_ledger_error(aeri_run):
    manifest_path = aeri_run.directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    next(e for e in manifest["episodes"] if e["role"] == "forward")["episode_id"] = "x"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(L.LedgerError, match="not an episode id"):
        L.read_aeri_run(aeri_run.directory)
