"""The old WAIT_HUMAN path of ``fr3_run --execute`` (no console: stdout is not
a terminal, e.g. ``| tee run.log``, or ``LEVI_AERI_CONSOLE=0``). Since
L2-UI2-PROVIDER-RELEASE a real run without a console is refused before any
connection, so this path is reached only through ``without_a_terminal``; it
stays under test while its code exists. Fake IO only,
the actual bridge and orchestrator, no robot, GPU or network.

Review SHOULD 3 (user decision 2026-10-14): lines typed while the run moved
are flushed before the prompt, and the dangerous waits (``CONFIRM_REASONS``)
need ``resume`` and then a line ``y``."""

import re
import termios
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_fr3_bridge import (  # noqa: F401 - imported pytest safety fixture
    no_network,
    rig,
    without_a_terminal,
)

from levi.automatic import console as C
from levi.automatic import console_text
from levi.automatic import fr3_run as R
from levi.automatic import state_machine as sm
from levi.automatic.journal import Journal


def _reasons(from_state, to_state="WAIT_HUMAN"):
    return {
        reason
        for rule in sm.TRANSITIONS.get((from_state, to_state), [])
        for reason in rule.reasons
    }


# ``legacy`` enters the wait from the run's first PREFLIGHT, so it takes the
# confirming waits the state machine allows there. The others (a home the
# bridge refused, ROBOT_HOME -> WAIT_HUMAN only) are tested below on the old
# path's answer itself; the state machine is not widened for a test.
CONFIRMING = sorted(C.CONFIRM_REASONS & _reasons("PREFLIGHT"))
HOME_ONLY = sorted(C.CONFIRM_REASONS - _reasons("PREFLIGHT"))
SINGLE = ["preflight_failed", "scene_reset_required"]


class Terminal:
    """A terminal on stdin: ``ahead`` holds lines typed before a prompt
    (gone after a flush), ``answers`` the lines typed after one. An answer
    may be ``(line, typed_ahead)``: that line, then more keys already typed
    behind it. No more lines: EOF."""

    def __init__(self, answers, ahead=()):
        self.answers = list(answers)
        self.ahead = list(ahead)
        self.flushes = 0
        self.prompts = 0

    def isatty(self):
        return True

    def fileno(self):
        return 97

    def flush_input(self, fd, queue):
        assert (fd, queue) == (97, termios.TCIFLUSH)
        self.flushes += 1
        self.ahead.clear()

    def input(self, prompt=""):
        self.prompts += 1
        if self.ahead:
            return self.ahead.pop(0)
        if not self.answers:
            raise EOFError
        answer = self.answers.pop(0)
        if callable(answer):
            answer = answer(self.rig)  # something happens while the person types
        if isinstance(answer, tuple):
            answer, behind = answer
            self.ahead.extend(behind)
        return answer


def legacy(
    tmp_path, monkeypatch, reason, answers, ahead=(), *, tracking=False, hook=None
):
    """The run's first PREFLIGHT sends it to WAIT_HUMAN for ``reason`` and the
    old path asks the terminal. ``tracking``: the run is registered as main()
    always does (``standalone_inputs``), so the path waits in ``select``;
    ``hook(orch, term)`` runs at every select poll."""
    r = rig(tmp_path, monkeypatch)
    run = R.Orchestrator.run

    def remembered(self, *a, **kw):
        r.orch = self
        return run(self, *a, **kw)

    monkeypatch.setattr(R.Orchestrator, "run", remembered)
    if tracking:
        r.job["standalone_inputs"] = {"job": {}, "binding_sha256": "d" * 64}
        monkeypatch.setattr(
            R.launch, "register_standalone", lambda job, facts: {"created_wall_ns": 0}
        )
        monkeypatch.setattr(R.launch, "standalone_status", lambda *a, **kw: None)
        monkeypatch.setattr(
            R.control,
            "CommandPump",
            lambda runner: SimpleNamespace(start=lambda: None, close=lambda: None),
        )
        polls = []

        def select(rlist, wlist, xlist, timeout=None):
            polls.append(1)
            assert len(polls) < 2000, "the terminal path never stopped waiting"
            if hook is not None:
                hook(r.orch, term)
            if term.ahead or term.answers:
                return rlist, [], []
            time.sleep(0.002)
            return [], [], []

        monkeypatch.setattr(R.select, "select", select)
    O = R.Orchestrator
    original, seen = O._preflight, []

    def preflight(self):
        if not seen:
            seen.append(1)
            self._to_human(reason)
        else:
            original(self)

    monkeypatch.setattr(O, "_preflight", preflight)
    # Unreachable on a real run since L2-UI2-PROVIDER-RELEASE (no terminal:
    # refused); the old path is kept under test while its code exists.
    without_a_terminal(monkeypatch)
    term = Terminal(answers, ahead)
    term.rig = r
    monkeypatch.setattr(R.sys, "stdin", term)
    monkeypatch.setattr(termios, "tcflush", term.flush_input)
    monkeypatch.setattr("builtins.input", term.input)
    state = R.execute(r.job, r.binding, load_modules=lambda b: r.modules)
    return r, term, state


def events(r):
    return Journal.read(Path(r.job["rollout_root"]) / ".aeri/runs/real-test").events


def resumes(r):
    return [
        e for e in events(r) if e.record == "committed" and e.reason == "human_resumed"
    ]


@pytest.mark.parametrize("reason", SINGLE + CONFIRMING)
def test_a_resume_typed_while_the_run_moved_is_flushed(tmp_path, monkeypatch, reason):
    r, term, _ = legacy(tmp_path, monkeypatch, reason, ["stop"], ahead=["resume"])
    assert resumes(r) == []
    assert not any(e.to_state == "FORWARD_ACTIVE" for e in events(r))
    assert term.flushes >= 1


@pytest.mark.parametrize("reason", CONFIRMING)
@pytest.mark.parametrize("second", ["", "resume", "yes", "n"])
def test_resume_then_anything_but_y_does_not_resume(
    tmp_path, monkeypatch, reason, second
):
    r, term, _ = legacy(tmp_path, monkeypatch, reason, ["resume", second])
    assert resumes(r) == []
    assert term.prompts == 2


@pytest.mark.parametrize("reason", CONFIRMING)
def test_resume_then_eof_does_not_resume(tmp_path, monkeypatch, reason):
    r, term, _ = legacy(tmp_path, monkeypatch, reason, ["resume"])
    assert resumes(r) == []
    assert term.prompts == 2


@pytest.mark.parametrize("reason", CONFIRMING)
def test_a_y_typed_together_with_resume_is_flushed(tmp_path, monkeypatch, reason):
    """``resume`` and ``y`` pasted at once: the y was typed before the
    confirmation prompt and must not confirm."""
    r, term, _ = legacy(tmp_path, monkeypatch, reason, [("resume", ["y"]), ""])
    assert resumes(r) == []
    assert term.flushes >= 2


@pytest.mark.parametrize("reason", CONFIRMING)
def test_resume_then_y_resumes(tmp_path, monkeypatch, reason, capsys):
    r, term, state = legacy(tmp_path, monkeypatch, reason, ["resume", "y"])
    assert len(resumes(r)) == 1 and resumes(r)[0].to_state == "PREFLIGHT"
    assert state == "COMPLETED"
    assert term.prompts == 2
    out = capsys.readouterr().out
    assert "二次确认" in out and reason in out


@pytest.mark.parametrize("reason", SINGLE)
def test_other_reasons_resume_with_one_line(tmp_path, monkeypatch, reason, capsys):
    r, term, state = legacy(tmp_path, monkeypatch, reason, ["resume"])
    assert len(resumes(r)) == 1
    assert state == "COMPLETED"
    assert term.prompts == 1
    assert "二次确认" not in capsys.readouterr().out


def test_the_confirmation_prompt_is_english_with_the_english_console(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv(console_text.ENV_LANG, "en")
    _, _, state = legacy(tmp_path, monkeypatch, "operator_stop", ["resume", "y"])
    assert state == "COMPLETED"
    out = capsys.readouterr().out
    assert "second confirmation" in out and "operator_stop" in out
    assert not re.search(r"[一-鿿]", out), out


# --- review round 3: the confirmation cannot outlive the wait it confirms --------------------------


def stop_commits(r):
    return [
        e
        for e in events(r)
        if e.record == "committed" and e.authority.command_id == "terminal-stop"
    ]


@pytest.mark.parametrize("reason", CONFIRMING)
def test_a_refused_confirmation_commits_the_terminal_stop(
    tmp_path, monkeypatch, reason
):
    r, _, state = legacy(tmp_path, monkeypatch, reason, ["resume", "no"])
    assert resumes(r) == []
    (stop,) = stop_commits(r)
    assert (stop.to_state, stop.reason) == ("COMPLETED", "operator_stop")
    assert state == "COMPLETED"


def test_eof_at_the_first_prompt_is_a_terminal_stop(tmp_path, monkeypatch):
    """N1: end-of-input at the first line stops the run, no traceback."""
    r, _, state = legacy(tmp_path, monkeypatch, "preflight_failed", [])
    assert resumes(r) == []
    (stop,) = stop_commits(r)
    assert (stop.to_state, stop.reason) == ("COMPLETED", "operator_stop")
    assert state == "COMPLETED"


@pytest.mark.parametrize("reason", CONFIRMING)
def test_a_stop_during_the_confirmation_does_not_resume(tmp_path, monkeypatch, reason):
    """A stop (Ctrl+C / SIGTERM through stop_probe) while the person types the
    confirmation: the y that follows must not resume."""
    flag = {"stop": False}
    monkeypatch.setattr(R.bridge.Robot, "stopped", lambda self: flag["stop"])

    def y_after_a_stop(r):
        flag["stop"] = True
        return "y"

    r, _, _ = legacy(tmp_path, monkeypatch, reason, ["resume", y_after_a_stop])
    assert resumes(r) == []


@pytest.mark.parametrize("reason", CONFIRMING)
def test_a_journal_line_during_the_confirmation_makes_the_y_stale(
    tmp_path, monkeypatch, reason
):
    """The resume carries the sequence seen when the wait was shown: a line
    written meanwhile (here a note) makes it ``stale_sequence``."""

    def y_after_a_line(r):
        r.orch.journal.note("x_meanwhile", "", authority=r.orch._auth("operator"))
        return "y"

    r, _, _ = legacy(tmp_path, monkeypatch, reason, ["resume", y_after_a_line, "stop"])
    assert resumes(r) == []


@pytest.mark.parametrize("reason", CONFIRMING)
def test_a_fault_during_the_confirmation_is_not_resumed(tmp_path, monkeypatch, reason):
    """A fault locks the run while the person types the confirmation (the
    robot only held: ``stopped()`` stays false): the y must not resume a
    fault the person has not seen."""

    def y_after_a_fault(r):
        r.orch._contain(RuntimeError("meanwhile"))
        return "y"

    r, _, state = legacy(tmp_path, monkeypatch, reason, ["resume", y_after_a_fault])
    assert state == "FAULT_LOCKED"
    assert resumes(r) == []


# --- tracking mode: main() always registers the run, so the path waits in select ------------------


@pytest.mark.parametrize("reason", CONFIRMING)
def test_tracking_resume_then_y_resumes(tmp_path, monkeypatch, reason):
    r, term, state = legacy(
        tmp_path, monkeypatch, reason, ["resume", "y"], tracking=True
    )
    assert len(resumes(r)) == 1 and state == "COMPLETED"
    assert term.prompts == 2


@pytest.mark.parametrize("reason", CONFIRMING)
def test_tracking_a_page_resume_during_the_confirmation_ends_the_terminal_wait(
    tmp_path, monkeypatch, reason
):
    """The page resumes while the confirmation is asked: the driver is not
    held in input(), and the terminal adds no second resume."""

    def page(orch, term):
        if term.prompts == 1 and orch.state == "WAIT_HUMAN":
            R.control.resume_checked(
                orch,
                "page-resume",
                expected_seq=orch.journal.next_seq,
                environment_handled=True,
                health_rechecked=True,
            )

    r, term, state = legacy(
        tmp_path, monkeypatch, reason, ["resume"], tracking=True, hook=page
    )
    assert [e.authority.command_id for e in resumes(r)] == ["page-resume"]
    assert term.prompts == 1  # never blocked in the second input()
    assert state == "COMPLETED"


@pytest.mark.parametrize("reason", CONFIRMING)
def test_tracking_a_stop_during_the_confirmation_does_not_resume(
    tmp_path, monkeypatch, reason
):
    flag = {"stop": False}
    monkeypatch.setattr(R.bridge.Robot, "stopped", lambda self: flag["stop"])

    def hook(orch, term):
        if term.prompts == 1:  # the person has not typed the y yet
            flag["stop"] = True

    r, term, _ = legacy(
        tmp_path, monkeypatch, reason, ["resume"], tracking=True, hook=hook
    )
    assert resumes(r) == []
    assert term.prompts == 1  # the stop ended the wait, not a blocking input()


@pytest.mark.parametrize("reason", CONFIRMING)
def test_tracking_a_fault_during_the_confirmation_is_not_resumed(
    tmp_path, monkeypatch, reason
):
    def hook(orch, term):
        if term.prompts == 1 and orch.state == "WAIT_HUMAN":
            orch._contain(RuntimeError("meanwhile"))

    r, _, state = legacy(
        tmp_path, monkeypatch, reason, ["resume", "y"], tracking=True, hook=hook
    )
    assert state == "FAULT_LOCKED"
    assert resumes(r) == []


# --- waits that only ROBOT_HOME enters (L2-HOME-EXECNO-ORCH) ----------------------------------------


def test_every_confirming_wait_is_tested_from_a_state_that_allows_it():
    """A confirming wait PREFLIGHT cannot enter is either one a home left
    (ROBOT_HOME -> WAIT_HUMAN) or a subtask_loop wait entered after a subtask
    episode (``sm.SUBTASK_TO_HUMAN``). Of the first kind there is only
    ``home_not_executed``, and it stays off PREFLIGHT: there no home was
    refused, and the start pose check it arms (``_check_start_pose``) is set
    only on the ROBOT_HOME branch. Each of the second kind (today
    ``subtask_retry_limit``: hold, no home) must be a confirming case of
    test_console_subtask_waits.CASES, which drives it from its real entry, so
    a new subtask confirming wait that nobody tests fails here."""
    from test_console_subtask_waits import CASES as SUBTASK_CASES

    # camera_stale (feat/aeri-cam-meta): entered after hold/quiesce/seal from
    # ROBOT_HOME or RESET_FINALIZE, never PREFLIGHT; driven from its real entry
    # in test_camera_frame_bus.py.
    assert sorted(set(HOME_ONLY) - sm.SUBTASK_TO_HUMAN) == [
        "camera_stale",
        "home_not_executed",
    ]
    assert {"home_not_executed"} <= _reasons("ROBOT_HOME")
    subtask_only = set(HOME_ONLY) & sm.SUBTASK_TO_HUMAN
    assert subtask_only  # subtask_retry_limit: if it moves, revisit this test
    assert subtask_only <= {case[0] for case in SUBTASK_CASES if case[2]}
    assert "home_not_executed" not in sm.TO_HUMAN
    assert CONFIRMING and set(CONFIRMING) | set(HOME_ONLY) == set(C.CONFIRM_REASONS)


class _Waiting:
    """The parts of an orchestrator the old path reads: its state and its
    journal (the latest committed move into WAIT_HUMAN gives the reason)."""

    def __init__(self, reason):
        self.state = "WAIT_HUMAN"
        self.journal = SimpleNamespace(
            next_seq=7,
            events=[
                SimpleNamespace(
                    record="committed", to_state="WAIT_HUMAN", reason=reason
                )
            ],
        )


@pytest.mark.parametrize("reason", HOME_ONLY)
@pytest.mark.parametrize(
    ("answers", "expected", "prompts"),
    [
        (["resume", "y"], "resume", 2),
        (["resume"], "stop", 2),
        (["resume", ""], "stop", 2),
        (["resume", "resume"], "stop", 2),
        ([("resume", ["y"]), ""], "stop", 2),
        (["stop"], "stop", 1),
    ],
)
def test_the_old_path_asks_twice_on_a_home_only_wait(
    monkeypatch, capsys, reason, answers, expected, prompts
):
    """The old path's answer on a wait only ROBOT_HOME enters: ``resume`` alone
    never resumes, a ``y`` typed together with it is flushed, only a ``y``
    after the second prompt does."""
    term = Terminal(answers)
    monkeypatch.setattr(R.sys, "stdin", term)
    monkeypatch.setattr(termios, "tcflush", term.flush_input)
    monkeypatch.setattr("builtins.input", term.input)
    robot = SimpleNamespace(stopped=lambda: False)
    assert R._legacy_answer(_Waiting(reason), robot, tracking=False) == (expected, 7)
    assert term.prompts == prompts
    if prompts == 2:
        assert reason in capsys.readouterr().out
