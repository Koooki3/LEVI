"""A run's wall time does not include the time it waited for a person."""

from test_agent_workbench import bench, execute  # noqa: F401  (fixture and helper)

from levi.harness import cost, ledger


def ev(kind, at, **data):
    return {"type": kind, "time": at, **data}


def spans(events, finished):
    return ledger.idle_spans(events, finished)


def test_a_review_left_open_and_cancelled_later_is_not_wall_time():
    events = [
        ev("planned", 100),
        ev("model_step", 110),
        ev("waiting_for_review", 130),
        ev("action.started", 600, tool="changes.diff"),  # a person looking
        ev("cancel_requested", 2500),
        ev("closed", 2600),
    ]
    got = spans(events, 2500)
    assert got["idle"] == got["waiting_for_person"] == 2370 and got["stood_down"] == 0


def test_a_review_that_ends_in_a_launch_costs_only_the_wait():
    events = [
        ev("planned", 0),
        ev("waiting_for_review", 20),  # the pilot
        ev("pilot_reviewed", 500, accepted=True),
        ev("launched", 520),
        ev("model_step", 540),
        ev("waiting_for_review", 560),
        ev("committed", 900),
    ]
    assert spans(events, 900)["idle"] == 500 + 340
    assert spans([ev("model_step", 1), ev("model_step", 5)], 5)["idle"] == 0


def test_a_refused_call_or_a_review_message_does_not_end_the_wait():
    """``action.started`` is written before the call runs: a refused
    runs.execute, or a tasks.advance that only said "review the pilot", starts
    nothing and writes no ``launched``."""
    events = [
        ev("launched", 0),
        ev("waiting_for_review", 20),
        ev("action.started", 100, tool="runs.execute", principal="p"),
        ev("action.failed", 100, tool="runs.execute", principal="p"),
        ev("action.started", 200, tool="tasks.advance", principal="p"),
        ev("action.completed", 200, tool="tasks.advance", principal="p"),
        ev("launched", 400),
        ev("model_step", 410),
    ]
    assert spans(events, 410)["idle"] == 380
    # A journal from before ``launched`` existed falls back to the completed
    # (never the failed) call.
    old = [e for e in events if e["type"] != "launched"][:-1]
    old.append(ev("action.completed", 400, tool="runs.execute"))
    assert spans(old, 400)["idle"] == 380
    assert spans(old[:-1], 400)["idle"] == 380  # no resume at all: to the end


def test_the_wait_for_a_plan_to_be_approved_is_a_persons():
    events = [
        ev("planned", 0),
        ev("action.started", 50, tool="runs.get"),
        ev("plan_approved", 300),
        ev("launched", 301),
        ev("model_step", 320),
    ]
    assert spans(events, 320)["waiting_for_person"] == 300
    # An unapproved plan is not counted (an external agent may work without one).
    assert spans([ev("planned", 0), ev("model_step", 5)], 5)["idle"] == 0


def test_a_stand_down_for_the_policy_server_is_its_own_figure():
    events = [
        ev("launched", 0),
        ev("model_step", 10),
        ev("action.started", 20, tool="runs.pause", principal="live-auto"),
        ev("pause_requested", 20),
        ev("launched", 50),  # the gate opened, the worker resumed
        ev("action.started", 60, tool="runs.pause", principal="a-person"),
        ev("pause_requested", 60),
        ev("launched", 160),
        ev("model_step", 170),
    ]
    got = spans(events, 170)
    assert got == {"idle": 130, "waiting_for_person": 100, "stood_down": 30}


def test_overlapping_waits_are_counted_once():
    events = [
        ev("launched", 0),
        ev("waiting_for_review", 10),
        ev("plan_revised", 20),  # the plan is open again while the review waits
        ev("plan_approved", 60),
        ev("launched", 100),
    ]
    assert spans(events, 100) == {
        "idle": 90,
        "waiting_for_person": 90,
        "stood_down": 0,
    }


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
    assert after["idle_seconds"] >= after["waiting_for_person_seconds"]
    assert after["stood_down_seconds"] == 0
    # So the cost record does not show a run that got forty times slower, which
    # is what filed a "cost-rise" improvement proposal before.
    record = cost.of(after, run, [])
    assert record["seconds"]["wall"] < 60


def test_launch_writes_the_event_the_ledger_resumes_on(bench):  # noqa: F811
    from levi.agent.planning import approve

    wb, context = bench
    run = approve(wb, wb.plan(context)["id"], 1, "fixture-human")
    wb.launch(run["id"], pilot=True)
    events = wb.store.events(run["id"])
    assert [e["type"] for e in events].count("launched") == 1
