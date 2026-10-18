"""The console on the home-gated event-pause flow: the real Journal and the
orchestrator over a fake robot, no GPU, socket or real robot. Pins that the
state table's home rows match what ``fix/aeri-home-gate`` actually writes."""

import contextlib
import io
from types import SimpleNamespace

from test_event_pause_judge import build

from levi.automatic import console as C


class Out(io.StringIO):
    def isatty(self):
        return True


class Keys:
    """Types Enter once the console has asked for it."""

    def __init__(self, out):
        self.out, self.mark, self.pressed = out, 0, 0

    def poll(self, timeout=0.0):
        needle = "按 [Enter]"
        at = self.out.getvalue().find(needle, self.mark)
        if at < 0:
            return None
        self.mark = at + len(needle)
        self.pressed += 1
        return "\n"

    def flush(self):
        pass

    @contextlib.contextmanager
    def cbreak(self):
        yield


def test_event_pause_success_homes_then_the_console_asks_for_the_reset(tmp_path):
    r = build(tmp_path, episodes=2)
    out = Out()
    keys = Keys(out)
    console = C.Console(keys, out)
    # the console only asks the robot whether it was told to stop
    console.bind(r.orch, SimpleNamespace(stopped=lambda: False))
    r.orch.listener = C.tee(console, None)
    # What ConsoleScene does: the reset prompt precedes the evidence capture.
    submit = r.orch.scene.submit
    seen = []

    def prompted(request):
        if request.get("target") == "post_forward":
            seen.append(
                (
                    r.robot.home_calls,
                    C.status_row(*C.latest(r.orch.journal.events)).say,
                )
            )
            console.reset_prompt(request)
        return submit(request)

    r.orch.scene.submit = prompted
    assert r.orch.run() == "COMPLETED"
    assert r.robot.home_calls == 2
    # the prompt came after the first home, and the real journal reads as "home"
    assert seen[0] == (1, "st_home_done")
    text = out.getvalue()
    assert "机械臂已回位。请复位环境" in text
    assert keys.pressed == 1  # one Enter; the last round has no reset prompt
    # the note is the real one the orchestrator wrote, and the home was real
    events = r.orch.journal.events
    codes = [e.note.code for e in events if e.record == "note"]
    assert "robot_homed" in codes and "operator_reset_confirmed" in codes
    # The prompt follows the committed home (robot_home_reached); the
    # robot_homed note is written after the scene request returns.
    marks = [
        e.reason if e.record == "committed" else e.note.code
        for e in events
        if e.record == "committed" or e.note is not None
    ]
    assert marks.index("robot_home_reached") < marks.index("operator_reset_confirmed")
    # blindness: no verdict or internal state name reached the screen
    for hidden in ("goal_verified", "ROBOT_HOME", "FINALIZE", "robot_home_reached"):
        assert hidden not in text, hidden
