"""A profile may keep several episodes (one request each) in flight."""

import threading
import time

import pytest

from levi.agent.runtime import STOP, run_episodes
from levi.agent.schema import ProviderConfig


def test_one_at_a_time_is_the_plain_loop():
    seen = []
    assert run_episodes(seen.append, [3, 1, 2]) is None
    assert seen == [3, 1, 2]
    seen = []
    assert (
        run_episodes(lambda e: seen.append(e) or (STOP if e == 1 else None), [3, 1, 2])
        is STOP
    )
    assert seen == [3, 1]


def test_width_bounds_what_runs_at_once_and_all_episodes_run():
    lock, running, peak, done = threading.Lock(), [0], [0], []

    def step(episode):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.02)
        with lock:
            running[0] -= 1
            done.append(episode)

    assert run_episodes(step, list(range(7)), width=2) is None
    assert sorted(done) == list(range(7)) and peak[0] == 2


def test_a_failure_starts_no_more_episodes_and_is_raised():
    started = []

    def step(episode):
        started.append(episode)
        time.sleep(0.01)
        if episode == 1:
            raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        run_episodes(step, list(range(10)), width=2)
    assert len(started) <= 3


def test_a_stop_request_starts_no_more_episodes():
    started = []

    def step(episode):
        started.append(episode)
        time.sleep(0.01)
        return STOP if episode == 0 else None

    assert run_episodes(step, list(range(10)), width=2) is STOP
    assert len(started) <= 3


def test_profiles_default_to_one_request_in_flight():
    config = ProviderConfig(name="p", base_url="https://example.invalid/v1", model="m")
    assert config.requests_in_flight == 1
    with pytest.raises(ValueError):
        ProviderConfig(
            name="p",
            base_url="https://example.invalid/v1",
            model="m",
            requests_in_flight=0,
        )
