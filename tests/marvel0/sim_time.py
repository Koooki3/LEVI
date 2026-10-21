"""A clock for the timing tests of the guided start: time moves only when every
thread that works for the page is waiting for it.

The page's real code runs on real threads (the operations it starts, the checks
it runs side by side) but sleeps and waits on **this** clock: a stand-in for a
5 s start-up calls ``clock.sleep(5)`` and the clock jumps there once nothing else
can run. So a chain that takes 19 s of the machine's time takes a few
milliseconds of the test's, and the order of events is the one the real
machine would show, not the one a sequential fake gives.

Threads are scheduled one at a time (a thread that starts another waits until
that one blocks or ends), so the fake machine is never touched by two threads at
once and a run is the same every time. **A thread that goes on after waiting
for another never leaves a moment when the world looks quiet**: the caller of
``spawn`` keeps its own slot while the new thread runs (it waits for that thread
to block or end, it does not give its slot up), and a thread that ends hands its
slot to the threads waiting in ``join`` for it, inside the same locked step. A
thread that gave its slot up before it resumed let the test's thread see a quiet
world, move the clock, and so resume a call after a later moment than the one it
ended at (about one run in eight in ``tests/marvel0/test_marvel0_sim_time.py``).

``Clock.spawn`` starts a thread of the world, ``Clock.sleep`` blocks one in the
world's time, ``Clock.join`` waits for one, ``Clock.run_calls`` is the page's
way of running several calls at once (``ActHost.parallel``), and
``Clock.wait_tick`` is the pause of the ticker (``Act._wait_tick``) that ends
early when the page announces a change."""

import threading

TIMEOUT_S = 20.0  # real seconds: a test that waits longer is stuck


class Stopped(BaseException):
    """Raised in a sleeping thread of the world when the world is closed."""


class Clock:
    def __init__(self, start=0.0, on_move=None):
        self.t = float(start)
        self.on_move = on_move  # called with the new time after the clock moved
        self._cv = threading.Condition()
        self._running = 0  # threads of the world that are neither asleep nor done
        self._sleepers = []  # (wake time, order, event)
        self._order = 0
        self._tls = threading.local()
        self._threads = []
        self._records = {}  # thread -> its state (see ``spawn``)
        self._closed = False
        self.errors = []  # exceptions of world threads

    # -- the world's threads
    def _inside(self) -> bool:
        return getattr(self._tls, "world", False)

    def spawn(self, fn, name="world"):
        """Start ``fn`` as a thread of the world and return once it has blocked
        or ended. A thread of the world that calls this keeps its own slot
        meanwhile (it is not at rest, so the world is not either); the test's
        thread is not counted and waits for the world to rest."""
        inside = self._inside()
        # ``settled``: the thread has blocked (slept, joined) or ended once;
        # ``ended``; ``joiners``: threads waiting in ``join`` for it, whose
        # slots it hands back when it ends.
        record = {"settled": False, "ended": False, "joiners": 0}
        with self._cv:
            self._running += 1  # (the new thread)

        def body():
            self._tls.world = True
            self._tls.record = record
            try:
                fn()
            except Stopped:
                pass
            except BaseException as exc:  # noqa: BLE001 - reported to the test
                self.errors.append(exc)
            finally:
                with self._cv:
                    self._running += record["joiners"] - 1
                    record.update(settled=True, ended=True, joiners=0)
                    self._cv.notify_all()

        thread = threading.Thread(target=body, daemon=True, name=name)
        self._threads.append(thread)
        self._records[thread] = record
        thread.start()
        with self._cv:
            if inside:
                ok = self._cv.wait_for(lambda: record["settled"], TIMEOUT_S)
            else:
                ok = self._cv.wait_for(lambda: self._running == 0, TIMEOUT_S)
            if not ok:
                raise AssertionError("a thread of the world did not block")
        return thread

    def sleep(self, seconds):
        """Block the calling thread of the world for ``seconds`` of its time."""
        event = threading.Event()
        record = getattr(self._tls, "record", None)
        with self._cv:
            self._order += 1
            self._sleepers.append((self.t + float(seconds), self._order, event))
            self._running -= 1
            if record is not None:
                record["settled"] = True
            self._cv.notify_all()
        if not event.wait(TIMEOUT_S):
            raise AssertionError("the clock never reached a sleeping thread")
        if self._closed:
            raise Stopped

    def join(self, thread):
        """Wait for ``thread`` (a thread of the world) from a thread of the
        world or from the test. A thread of the world gives its slot up while it
        waits and gets it back from the thread it waits for, when that ends (in
        the same locked step as its own end), so the world is never seen at
        rest between the end of one and the resumption of the other."""
        record = self._records.get(thread)
        if self._inside() and record is not None:
            mine = getattr(self._tls, "record", None)
            with self._cv:
                if not record["ended"]:
                    record["joiners"] += 1
                    self._running -= 1
                    if mine is not None:
                        mine["settled"] = True
                    self._cv.notify_all()
        thread.join(TIMEOUT_S)
        if thread.is_alive():
            raise AssertionError("a thread of the world never ended")
        # (a joiner's slot came back with the end of the thread it waited for)

    def run_calls(self, calls):
        """``ActHost.parallel``: the results of ``calls``, each in a thread of
        the world, all begun before any is waited for."""
        results = [None] * len(calls)
        errors = [None] * len(calls)

        def work(i, call):
            try:
                results[i] = call()
            except BaseException as exc:  # noqa: BLE001 - raised below
                errors[i] = exc

        threads = [
            self.spawn(lambda i=i, c=c: work(i, c), f"call-{i}")
            for i, c in enumerate(calls)
        ]
        for thread in threads:
            self.join(thread)
        for exc in errors:
            if exc is not None:
                raise exc
        return results

    def close(self):
        """End the world: every sleeping thread is woken and stops; threads that
        run for ever (a monitor that writes twice a second) end here."""
        with self._cv:
            self._closed = True
            for _, _, event in self._sleepers:
                self._running += 1
                event.set()
            self._sleepers = []
        for thread in self._threads:
            thread.join(TIMEOUT_S)

    # -- moving the clock (the test's thread)
    def quiesce(self):
        with self._cv:
            if not self._cv.wait_for(lambda: self._running == 0, TIMEOUT_S):
                raise AssertionError("the world did not come to rest")

    def step(self, limit):
        """Move to the next moment something sleeps until, or to ``limit``,
        whichever is first; wake what is due. Returns the new time."""
        self.quiesce()
        with self._cv:
            if self._sleepers:
                limit = min(limit, min(s[0] for s in self._sleepers))
            self.t = max(self.t, limit)
            due = sorted(s for s in self._sleepers if s[0] <= self.t)
            self._sleepers = [s for s in self._sleepers if s[0] > self.t]
        if self.on_move:
            self.on_move(self.t)
        for _, _, event in due:  # (one at a time: the run is the same every time)
            with self._cv:
                self._running += 1
            event.set()
            self.quiesce()
        self.quiesce()
        return self.t

    def wait_tick(self, wake, seconds):
        """``Act._wait_tick``: pause for ``seconds`` of the clock, or less when
        ``wake`` (the page's event for "something changed") is set."""
        end = self.t + seconds
        while self.t < end:
            self.quiesce()
            if wake.is_set():
                return
            self.step(end)
            if wake.is_set():
                return
        self.quiesce()
