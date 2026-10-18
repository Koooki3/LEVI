"""A fake keyboard with a hard deadline (no pytest-timeout on this machine):
the console tests of the subtask_loop branch type keys when a prompt text
appears; if the awaited text never appears within ``deadline_s`` (monotonic),
the test fails with that text and the screen so far instead of hanging and
holding the full-test lease."""

import time

from test_console import Keys

DEADLINE_S = 10.0


class DeadlineKeys(Keys):
    def __init__(self, out, script=(), stale="", log=None, deadline_s=DEADLINE_S):
        super().__init__(out, script, stale, log)
        self.deadline_s = deadline_s
        self._idle_since = time.monotonic()

    def _expired(self):
        waiting = self.script[0][0] if self.script else "(no scripted key left)"
        screen = self.out.getvalue()[-2000:]
        raise AssertionError(
            f"fake keyboard: no key for {self.deadline_s:.0f} s; waiting for "
            f"{waiting!r}; console output so far:\n{screen}"
        )

    def poll(self, timeout=0.0):
        key = super().poll(timeout)
        now = time.monotonic()
        if key is not None:
            self._idle_since = now
        elif now - self._idle_since > self.deadline_s:
            self._expired()
        return key

    def flush(self):
        super().flush()
        self._idle_since = time.monotonic()
