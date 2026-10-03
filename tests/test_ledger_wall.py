"""A run's wall time does not include the time it waited for a person."""

from test_agent_workbench import bench, execute  # noqa: F401  (fixture and helper)

from levi.harness import cost, ledger


def ev(kind, at, **data):
    return {"type": kind, "time": at, **data}


def test_a_review_left_open_and_cancelled_later_is_not_wall_time():
    events = [
        ev("planned", 100),
        ev("model_step", 110),
        ev("waiting_for_review", 130),
        ev("action.started", 600, tool="changes.diff"),  # a person looking
        ev("cancel_requested", 2500),
        ev("closed", 2600),
    ]
    assert ledger.idle_seconds(events, 2500) == 2370


def test_a_review_that_ends_in_a_resume_costs_only_the_wait():
    events = [
        ev("planned", 0),
        ev("waiting_for_review", 20),  # the pilot
        ev("pilot_reviewed", 500, accepted=True),
        ev("action.started", 520, tool="runs.execute"),
        ev("model_step", 540),
        ev("waiting_for_review", 560),
        ev("committed", 900),
    ]
    assert ledger.idle_seconds(events, 900) == 500 + 340
    # The live service's own resume and a revised plan end a wait too.
    assert (
        ledger.idle_seconds(
            [
                ev("waiting_for_review", 10),
                ev("auto_resumed", 70),
                ev("model_step", 80),
            ],
            80,
        )
        == 60
    )
    assert ledger.idle_seconds([ev("model_step", 1), ev("model_step", 5)], 5) == 0


def test_the_ledger_of_a_cancelled_review_run_reads_the_work_not_the_wait(bench):  # noqa: F811
    wb, context = bench
    run = execute(wb, wb.plan(context))
    assert run["status"] == "waiting_for_review"
    before = ledger.build(wb.store, run["id"])
    assert before["wall_seconds"] < 60
    # Forty minutes later somebody cancels it (the live service archives old
    # open reviews).
    last = max(e["time"] for e in wb.store.events(run["id"]))
    wb.store.event(run["id"], "cancel_requested", time=last + 2400)
    after = ledger.build(wb.store, run["id"])
    assert after["wall_seconds"] == before["wall_seconds"] < 60
    assert 2399 <= after["waiting_for_person_seconds"] <= 2401
    # So the cost record does not show a run that got forty times slower, which
    # is what filed a "cost-rise" improvement proposal before.
    record = cost.of(after, run, [])
    assert record["seconds"]["wall"] < 60
