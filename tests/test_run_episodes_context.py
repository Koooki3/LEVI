"""Pausing a run cuts its in-flight model requests."""


def test_episodes_run_in_parallel_keep_their_run_s_context():
    """Pausing a run cuts its in-flight model requests through a context
    variable (transport.requests_of). Episodes processed in a thread pool
    (requests_in_flight > 1) must see it, or a pause waits for the slowest
    request to finish."""
    from levi.agent.runtime import run_episodes
    from levi.inference.transport import _OWNER, requests_of

    seen = []

    def step(episode):
        seen.append(_OWNER.get())

    with requests_of("run-7"):
        run_episodes(step, [0, 1, 2, 3], width=2)
        run_episodes(step, [4], width=1)
    assert seen == ["run-7"] * 5
