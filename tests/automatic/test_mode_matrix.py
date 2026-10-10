"""The reset-mode matrix (T-CL-01, levi/automatic/modes.py): its cells are
well formed, its snapshot is current, and fake-driven runs of each mode
reach exactly the transitions the matrix gives that mode: human_assisted
reaches what single_reset_policy reaches minus the reset transitions
(``RESET_ONLY_TRANSITIONS``). No network, no robot, no GPU."""

import json
from pathlib import Path

import pytest
from aeri_harness import build, config, crash_at, fake, restart
from test_aeri_orchestrator import no_network  # noqa: F401

from levi.automatic import metrics as M
from levi.automatic import modes
from levi.automatic import reset_manager as rm
from levi.automatic import state_machine as sm
from levi.automatic.adapters import events as ev

HERE = Path(__file__).resolve().parent
SNAPSHOT = HERE / "snapshots" / "mode_matrix.json"


def test_the_matrix_is_well_formed():
    assert modes.matrix_problems() == []
    assert modes.RESET_MODES == rm.STRATEGIES == M.RESET_MODES


def test_the_reset_only_transitions_are_those_touching_a_reset_state():
    derived = {pair for pair in sm.TRANSITIONS if modes.RESET_STATES & set(pair)}
    assert modes.RESET_ONLY_TRANSITIONS == derived
    for a, b in sm.TRANSITIONS:
        cell = modes.MODE_MATRIX[f"transition:{a}->{b}"][modes.HUMAN]
        assert (modes.kind(cell) == "n/a") == ((a, b) in modes.RESET_ONLY_TRANSITIONS)


def test_the_snapshot_is_current():
    """Regenerate with ``python -m levi.automatic.modes >
    tests/automatic/snapshots/mode_matrix.json`` after changing the matrix."""
    assert json.loads(SNAPSHOT.read_text()) == modes.snapshot()
    assert SNAPSHOT.read_text() == modes.snapshot_text()


# --- reachability ------------------------------------------------------------------------------------


def _pairs(orch) -> set:
    return {
        (e.from_state, e.to_state)
        for e in orch.journal.events
        if e.record == "committed" and e.from_state is not None
    }


def _resume(r, command_id):
    return r.orch.resume(
        command_id,
        expected_seq=r.orch.journal.next_seq,
        environment_handled=True,
        health_rechecked=True,
    )


class Runs:
    """Scenarios driven the same way in either mode; ``reached`` collects
    every committed transition."""

    def __init__(self, folder, mode):
        self.folder, self.mode, self.count, self.reached = Path(folder), mode, 0, set()

    def build(self, scene=(), cfg=None, events=None, **kw):
        """``events``: a function of the clock giving the event stream."""
        self.count += 1
        clock = fake.FakeClock()
        scene_fake = ev.FakeSceneAssessor(
            list(scene), clock, "r-sm", default={"decision": "ready", "evidence": 2}
        )
        events_fake = (
            events(clock)
            if events
            else ev.FakeEventStream(
                {
                    "forward": [{"step": 8, "event_type": "object_settled"}],
                    "reset": [{"step": 6, "event_type": "object_settled"}],
                },
                clock,
                "r-sm",
            )
        )
        return build(
            self.folder / str(self.count),
            cfg=config(reset_strategy=self.mode, **(cfg or {})),
            clock=clock,
            scene=scene_fake,
            events=events_fake,
            **kw,
        )

    def drive(self, r, rounds=4):
        for n in range(rounds):
            if r.orch.run() not in sm.HUMAN_STATES:
                break
            _resume(r, f"c-{n}")
        self.reached |= _pairs(r.orch)
        return r

    def crash_sweep(self, scene=(), cfg=None):
        """The orchestrator dies after every prepared line in turn; a new
        one takes over (FAULT_LOCKED) and the operator resumes."""
        for k in range(40):
            r = self.build(scene, cfg, crash_hook=crash_at("after_prepared", k))
            try:
                self.drive(r)
                return  # no k-th transaction: every one was tried
            except fake.SimulatedCrash:
                pass
            again = restart(r)
            self.reached |= _pairs(again.orch)
            self.drive(again)

    def crash_while_resuming(self, r):
        """The orchestrator dies inside a resume (WAIT_HUMAN or FAULT_LOCKED
        -> PREFLIGHT prepared, never committed)."""
        assert r.orch.state in sm.HUMAN_STATES
        r.orch.crash_hook = crash_at("after_prepared", 0)
        with pytest.raises(fake.SimulatedCrash):
            _resume(r, f"c-crash-{r.orch.journal.next_seq}")
        again = restart(r)
        self.reached |= _pairs(again.orch)
        return again

    def stop_at_step(self, r, step):
        original = r.robot.step

        def step_then_stop(action, *, token):
            if r.robot.step_calls == step:
                r.orch.stop("s-1")
            return original(action, token=token)

        r.robot.step = step_then_stop
        return r


def reached(folder, mode) -> set:
    runs = Runs(folder, mode)
    # Ready scenes; a scene that is not ready first and after episode 1.
    runs.drive(runs.build(cfg={"episodes": 2}))
    runs.drive(runs.build([{"decision": "reset_required"}]))
    runs.drive(
        runs.build(
            [{"decision": "ready", "evidence": 2}, {"decision": "reset_required"}],
            cfg={"episodes": 2},
        )
    )
    # A reset that never puts the scene back (its horizon), then a person.
    runs.drive(
        runs.build(
            [{"decision": "reset_required"}, {"decision": "reset_required"}],
            events=lambda clock: ev.FakeEventStream({}, clock, "r-sm"),
        )
    )
    # Out of reset attempts / unknown with on_unknown=wait_human: a person.
    runs.drive(
        runs.build(
            [{"decision": "ready", "evidence": 2}, {"decision": "reset_required"}],
            cfg={"episodes": 2, "max_reset_attempts": 0},
        )
    )
    runs.drive(
        runs.build(
            [{"decision": "unknown", "evidence": 2}],
            cfg={"on_scene_unknown": "wait_human"},
        )
    )
    # A failed preflight; a red light during the forward episode.
    runs.drive(runs.build(robot={"preflight_ok": False}), rounds=1)
    runs.drive(runs.build(robot={"step_faults": {5: "red_light"}}), rounds=1)
    # An operator's stop mid-episode: staying put, then a stop while waiting;
    # homed, then a resume after the last episode.
    r = runs.stop_at_step(runs.build(cfg={"home_after_operator_stop": False}), 3)
    assert r.orch.run() == "WAIT_HUMAN"
    assert r.orch.stop("s-2").ok
    runs.reached |= _pairs(r.orch)
    runs.drive(runs.stop_at_step(runs.build(), 3))
    # Crashes at every transaction: forward path, then the reset path.
    runs.crash_sweep()
    runs.crash_sweep([{"decision": "reset_required"}])
    # Crashes inside a resume, from WAIT_HUMAN and from FAULT_LOCKED.
    r = runs.build(
        [{"decision": "unknown", "evidence": 2}], cfg={"on_scene_unknown": "wait_human"}
    )
    assert r.orch.run() == "WAIT_HUMAN"
    locked = runs.crash_while_resuming(r)
    runs.drive(runs.crash_while_resuming(locked))
    return runs.reached


_REACHED: dict = {}


def reached_by(mode, tmp_path_factory) -> set:
    if mode not in _REACHED:
        _REACHED[mode] = reached(tmp_path_factory.mktemp(f"reach-{mode}"), mode)
    return _REACHED[mode]


def expected(mode) -> set:
    return {
        tuple(c.removeprefix("transition:").split("->"))
        for c, cells in modes.MODE_MATRIX.items()
        if c.startswith("transition:") and modes.kind(cells[mode]) != "n/a"
    }


def _covering(mode):
    """The transitions a mode has (its cells that are not n/a)."""
    return pytest.param(
        mode,
        marks=pytest.mark.mode_matrix(
            *(f"transition:{a}->{b}" for a, b in sorted(expected(mode)))
        ),
        id=mode,
    )


@pytest.mark.parametrize("reset_mode", [_covering(m) for m in modes.RESET_MODES])
def test_fake_runs_reach_exactly_the_transitions_of_their_mode(
    reset_mode, tmp_path_factory
):
    """Each mode reaches its own transitions and none of the others (the
    reset transitions stay unreached in human_assisted)."""
    found = reached_by(reset_mode, tmp_path_factory)
    assert found == expected(reset_mode), {
        "missing": sorted(expected(reset_mode) - found),
        "unexpected": sorted(found - expected(reset_mode)),
    }


def test_human_assisted_reaches_the_reset_policy_mode_minus_its_reset_transitions(
    tmp_path_factory,
):
    single = reached_by(modes.SINGLE, tmp_path_factory)
    human = reached_by(modes.HUMAN, tmp_path_factory)
    assert single == set(sm.TRANSITIONS)
    assert human == single - modes.RESET_ONLY_TRANSITIONS


# --- the arbitration in each mode ------------------------------------------------------------------


@pytest.mark.mode_matrix(*(f"arbitration:plan:{d}" for d in rm.DECISIONS))
def test_the_scene_decision_in_each_mode(reset_mode):
    strategy = rm.strategy_for(reset_mode)
    human = reset_mode == modes.HUMAN
    assert rm.check_plan(strategy, "ready", 0) == rm.ResetPlan("forward", "scene_ready")
    for decision, reason in (
        ("reset_required", "scene_reset_required"),
        ("unknown", "scene_unknown"),
        ("unavailable", "scene_unknown"),
    ):
        found = rm.check_plan(strategy, decision, 0)
        assert found == rm.ResetPlan("wait_human" if human else "reset", reason)
        # Out of attempts, both ask a person; never a forward start.
        assert rm.check_plan(strategy, decision, 1).action == "wait_human"


@pytest.mark.mode_matrix(
    *(f"arbitration:after_reset:{o}" for o in sorted(sm.RESET_OUTCOMES)),
    modes=(modes.SINGLE,),
)
def test_after_a_reset_episode_in_the_reset_policy_mode():
    strategy = rm.strategy_for(modes.SINGLE, max_attempts=2)
    for outcome in sm.RESET_OUTCOMES:
        found = rm.check_after(strategy, outcome, 1, False)
        again = outcome in {"reset_verified"} | rm.RETRYABLE_OUTCOMES
        assert found == ("VERIFY_INITIAL" if again else "WAIT_HUMAN"), outcome


def test_the_documentation_tables_are_current():
    """``docs/AUTOMATIC_PIPELINE*.md``: ``levi docs sync`` writes the table
    from the matrix, ``levi docs check`` compares (levi/docs.py)."""
    from levi import docs

    targets = {f: m for n, f, m in docs.generated() if n == modes.DOCS_SECTION}
    assert set(targets) == set(modes.DOCS_FILES.values())
    for lang, file in modes.DOCS_FILES.items():
        assert targets[file]() == modes.markdown(lang)
    assert not [p for p in docs.check() if "AUTOMATIC_PIPELINE" in p]
    for lang in ("en", "zh"):
        table = modes.markdown(lang)
        assert table.count("\n") == len(modes.MODE_MATRIX) + 2
        for capability in modes.MODE_MATRIX:
            assert f"| `{capability}` |" in table


def test_every_sentence_has_its_chinese_wording():
    assert modes.sentences() == set(modes.ZH)
    zh = modes.markdown("zh")
    assert not any(sentence in zh for sentence in modes.sentences())
