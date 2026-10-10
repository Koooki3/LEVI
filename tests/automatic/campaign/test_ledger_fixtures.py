"""Not a test module: synthetic campaigns shared by the ledger, report and
guided tests. Everything is made up and seeded; no real rollout is read."""

import random

from levi.automatic.campaign.ledger import CampaignLayout, EpisodeFact, SegmentPlan

CAMPAIGN = "c20261010-synthetic"


def williams_two(rounds):
    """AB, BA, AB, ...: the two-arm Williams order per round."""
    return [("A", "B") if r % 2 else ("B", "A") for r in range(1, rounds + 1)]


def layout(
    *,
    arms=("A", "B"),
    per_arm=30,
    segment_trials=5,
    campaign=CAMPAIGN,
    reruns=None,
    order=None,
    layout_source="card_set",
):
    """Segments of ``segment_trials`` cards; every arm runs the same cards in
    a round. ``reruns``: {segment number: extra run count}."""
    rounds = per_arm // segment_trials
    reruns = reruns or {}
    orders = order or (
        williams_two(rounds)
        if len(arms) == 2
        else [
            tuple(arms[(i + r) % len(arms)] for i in range(len(arms)))
            for r in range(rounds)
        ]
    )
    segments = []
    number = 1
    for r in range(1, rounds + 1):
        cards = tuple(f"r{r:02d}c{j}" for j in range(1, segment_trials + 1))
        for arm in orders[r - 1]:
            runs = [f"{campaign}__{arm}__s{number:02d}"]
            runs += [
                f"{campaign}__{arm}__s{number:02d}r{k + 2}"
                for k in range(reruns.get(number, 0))
            ]
            segments.append(SegmentPlan(number, arm, r, cards, tuple(runs)))
            number += 1
    return CampaignLayout(campaign, tuple(segments), layout_source=layout_source)


def fact(run, number, *, source="aeri", status="valid", card=None, **kw):
    card_source = kw.pop(
        "card_source",
        "unconfirmed"
        if card is None
        else ("operator_confirmed" if source == "legacy_client" else "run_manifest"),
    )
    episode = (
        f"{run}.forward.{number:04d}"
        if source == "aeri"
        else f"models/{run}/task/demo_{number:04d}"
    )
    return EpisodeFact(
        run_id=run,
        episode_id=kw.pop("episode_id", episode),
        number=number,
        source=source,
        status=status,
        card=card,
        card_source=card_source,
        **kw,
    )


def synthetic_facts(
    lay,
    *,
    rates=None,
    seed=20261010,
    judge_error=0.1,
    max_steps=300,
    adjudicate_every=4,
    controls=True,
):
    """One valid AERI episode per planned card, outcomes drawn from
    ``rates`` (per arm), the automatic verdict wrong with probability
    ``judge_error``, every ``adjudicate_every``-th episode adjudicated."""
    rng = random.Random(seed)
    rates = rates or {"A": 0.4, "B": 0.7, "C": 0.55, "D": 0.5}
    by_run = {}
    counter = 0
    for seg in lay.segments:
        run = seg.run_ids[0]
        facts = []
        for j, _card in enumerate(seg.cards, start=1):
            counter += 1
            ok = rng.random() < rates[seg.arm]
            truth = "success" if ok else "failure"
            wrong = rng.random() < judge_error
            verdict = (
                {"success": "failure", "failure": "success"}[truth] if wrong else truth
            )
            if rng.random() < 0.05:
                verdict = "undecided"
            steps = rng.randint(40, 250) if ok else max_steps
            early = ok and verdict == "success" and rng.random() < 0.7
            control = controls and j == 1
            labels = {
                "autonomous_verdict": verdict,
                "posthoc_verdict": verdict if verdict != "undecided" else truth,
                "operator_label": truth,
            }
            if counter % adjudicate_every == 0:
                labels["adjudicated_ground_truth"] = truth
            facts.append(
                fact(
                    run,
                    j,
                    started_at=f"2026-10-10T10:{counter % 60:02d}:00Z",
                    ended_at=f"2026-10-10T10:{counter % 60:02d}:40Z",
                    steps=steps if not (early and not control) else max(10, steps - 30),
                    max_steps=max_steps,
                    stop_reason="goal_verified"
                    if early and not control
                    else "horizon_exhausted",
                    ended_by="early_stop" if early and not control else "budget",
                    control=control,
                    would_stop_step=(steps - 30)
                    if control and verdict == "success"
                    else None,
                    control_recorded=control,
                    layout_fidelity="attested",
                    labels=labels,
                    failure_mode=None
                    if ok
                    else rng.choice(["missed_grasp", "dropped"]),
                )
            )
        by_run[run] = facts
    return by_run
