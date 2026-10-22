"""Pose-only worker races on in-process fakes; no server is started."""

import threading

import pytest

from levi.marvel0.manual import Pose
from levi.marvel0.manual_worker import Ack, PoseWorker, WorkerRefused

POSE = Pose((0.5, 0.0, 0.4), (0.0, 0.0, 0.0, 1.0))


class Sink:
    def __init__(self):
        self.sent = []
        self.closed = False
        self.answer = Ack(True, "synthetic")

    def send(self, pose):
        self.sent.append(pose)
        return self.answer

    def close_admission(self):
        self.closed = True


def test_close_is_idempotent_and_no_further_release_is_possible():
    sink = Sink()
    worker = PoseWorker(sink)
    assert worker.send(POSE).acknowledged is True
    worker.close_admission()
    worker.close_admission()
    with pytest.raises(WorkerRefused, match="admission_closed"):
        worker.send(POSE)
    assert len(sink.sent) == 1
    assert worker.snapshot() == {
        "closed": True,
        "close_confirmed": True,
        "in_flight": False,
        "sent": 1,
    }


@pytest.mark.parametrize("answer", [None, {}, Ack(False, "unknown"), Ack(1, "invalid")])
def test_bad_or_negative_ack_permanently_closes_release_gate(answer):
    sink = Sink()
    sink.answer = answer
    worker = PoseWorker(sink)
    assert worker.send(POSE).acknowledged is False
    assert worker.snapshot()["closed"] and sink.closed
    with pytest.raises(WorkerRefused, match="admission_closed"):
        worker.send(POSE)
    assert len(sink.sent) == 1


def test_raised_send_has_unknown_delivery_and_no_retry():
    class Broken(Sink):
        def send(self, pose):
            self.sent.append(pose)
            raise TimeoutError("may already have reached the fake actuator")

    sink = Broken()
    worker = PoseWorker(sink)
    assert worker.send(POSE) == Ack(False, "delivery_unknown")
    assert worker.snapshot()["sent"] == 1
    assert worker.snapshot()["closed"]


def test_unconfirmed_sink_closure_is_not_silently_treated_as_confirmed():
    class Broken(Sink):
        def close_admission(self):
            raise RuntimeError("unknown closure")

    worker = PoseWorker(Broken())
    with pytest.raises(WorkerRefused, match="close_unproven"):
        worker.close_admission()
    assert worker.snapshot()["closed"]
    assert not worker.snapshot()["close_confirmed"]
    with pytest.raises(WorkerRefused, match="close_unproven"):
        worker.close_admission()
    with pytest.raises(WorkerRefused, match="admission_closed"):
        worker.send(POSE)


def test_only_one_call_in_flight_and_close_does_not_wait_for_it():
    entered, release = threading.Event(), threading.Event()

    class Blocking(Sink):
        def send(self, pose):
            self.sent.append(pose)
            entered.set()
            assert release.wait(2)
            return self.answer

    sink = Blocking()
    worker = PoseWorker(sink)
    thread = threading.Thread(target=lambda: worker.send(POSE))
    thread.start()
    try:
        assert entered.wait(2)
        with pytest.raises(WorkerRefused, match="worker_busy"):
            worker.send(POSE)
        worker.close_admission()
        assert sink.closed
        assert worker.snapshot()["in_flight"]
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    assert worker.snapshot()["sent"] == 1
    assert not worker.snapshot()["in_flight"]
