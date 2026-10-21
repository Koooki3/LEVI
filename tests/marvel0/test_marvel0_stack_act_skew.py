"""A check that finished a moment AFTER the read the next tick works from is fresh
(the reads are cached ~2 s). The first real use of Start whole stack ended
``b_not_seen`` because such a result read as "from the future" (21 Oct)."""

from levi.marvel0 import stack_act


def test_a_result_a_moment_newer_than_the_read_is_fresh():
    item = {"at": 1000.0}
    assert stack_act._fresh(item, 600, 999.0)  # the read is 1 s older: fresh
    assert stack_act._fresh(item, 600, 995.5)
    assert stack_act._fresh(item, 600, 1000.0)
    assert stack_act._fresh(item, 600, 1600.0)  # exactly the ttl


def test_a_result_from_far_in_the_future_or_older_than_the_ttl_is_stale():
    item = {"at": 1000.0}
    assert not stack_act._fresh(item, 600, 990.0)  # a clock that went back
    assert not stack_act._fresh(item, 600, 1601.0)  # older than the ttl
    assert not stack_act._fresh(None, 600, 1000.0)


def test_the_b_probe_read_a_second_before_it_still_counts(tmp_path):
    # the same through the real helper that turns a probe into B's state
    from stack_act_machine import ActMachine

    m = ActMachine(tmp_path)
    probe = {"at": 5000.0, "state": "absent", "by": "probe"}
    word = m.act._b_idle({"key": "b"}, probe, 4999.0)  # the cached read is 1 s older
    assert word["state"] == "idle", word
