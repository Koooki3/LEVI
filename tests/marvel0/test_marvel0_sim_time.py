"""The clock of the timing tests (``sim_time.py``) keeps its promise: threads of the
world run one at a time, and time moves only when every one of them is waiting.

A thread that goes on after waiting for another (``spawn`` from a thread of the
world, ``join``) must go on at the moment the other ended or blocked. It used to
give its slot up before it resumed, so the test's thread could see a quiet world
in between, move the clock, and the thread went on at a later moment (about one
run in eight): the guided start's timing test saw its checks before C end up to a
look late. Nothing here starts a process or sleeps in real time."""

from sim_time import Clock

START = 1000.0
RUNS = 300


def settle(clock, until, done):
    """Move the clock the way the test's thread does, until ``done()``."""
    for _ in range(200):
        clock.step(until)
        if done():
            return
    raise AssertionError("the world never got there")


def joined_run(clock) -> dict:
    """One world: a monitor that wakes every half second (there is always a later
    moment for the clock to jump to) and a thread that runs two calls side by
    side, 5 s and 1.2 s, and goes on after both."""
    out = {}
    clock.spawn(lambda: [clock.sleep(0.5) for _ in range(100)], "monitor")

    def op():
        clock.run_calls([lambda: clock.sleep(5.0), lambda: clock.sleep(1.2)])
        out["resumed"] = clock.t

    clock.spawn(op, "op")
    settle(clock, START + 20, lambda: "resumed" in out)
    return out


def spawned_run(clock) -> dict:
    out = {}
    clock.spawn(lambda: [clock.sleep(0.5) for _ in range(100)], "monitor")

    def op():
        child = clock.spawn(lambda: clock.sleep(2.0), "child")
        out["after_spawn"] = clock.t  # (the child has blocked: no time passed)
        clock.join(child)
        out["after_join"] = clock.t

    clock.spawn(op, "op")
    settle(clock, START + 20, lambda: "after_join" in out)
    return out


def test_a_thread_resumes_at_the_moment_the_thread_it_joined_ended():
    for _ in range(RUNS):
        clock = Clock(START)
        try:
            out = joined_run(clock)
        finally:
            clock.close()
        assert out["resumed"] == START + 5.0


def test_a_thread_that_starts_another_goes_on_before_any_time_has_passed():
    for _ in range(RUNS):
        clock = Clock(START)
        try:
            out = spawned_run(clock)
        finally:
            clock.close()
        assert out["after_spawn"] == START
        assert out["after_join"] == START + 2.0


def test_a_thread_that_already_ended_is_joined_without_waiting():
    clock = Clock(START)
    out = {}
    try:
        child = clock.spawn(lambda: None, "child")

        def op():
            clock.join(child)
            out["joined"] = clock.t

        clock.spawn(op, "op")
        settle(clock, START + 1, lambda: "joined" in out)
    finally:
        clock.close()
    assert out["joined"] == START


def test_the_clock_moves_only_when_every_thread_waits():
    clock = Clock(START)
    order = []
    try:
        for n, seconds in enumerate((3.0, 1.0, 2.0)):
            clock.spawn(
                lambda s=seconds, n=n: (clock.sleep(s), order.append((n, clock.t))),
                f"sleeper-{n}",
            )
        settle(clock, START + 10, lambda: len(order) == 3)
    finally:
        clock.close()
    assert order == [(1, START + 1.0), (2, START + 2.0), (0, START + 3.0)]


def test_an_error_in_a_thread_of_the_world_is_kept_for_the_test():
    clock = Clock(START)
    try:

        def fails():
            raise ValueError("boom")

        clock.spawn(fails, "fails")
    finally:
        clock.close()
    assert [type(e) for e in clock.errors] == [ValueError]
