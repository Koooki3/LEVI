"""subtask.reverse_policy_role reset on fakes (design STEP5-AERI-RESET-DESIGN, R1 / P1b): the reverse loop's episodes
acquire the reset role with the frozen training sentence, the service is made ready once per loop at home (before the
loop's first episode), a switch the arm cannot take is a fault, and a run without the setting acts exactly as before."""

from aeri_harness import fake
from subtask_rig import (
    FORWARD,
    REVERSE,
    TracingPolicy,
    build,
    committed,
    notes,
    prompts,
    sub_config,
)

from levi.automatic import subtask_loop as st

ENTRIES = (
    "pink plate|table|pick the pink plate onto the table",
    "blue plate|pink plate|pick the blue plate onto the pink plate",
)
SENTENCES = [
    "Reset: pick the pink plate onto the table",
    "Reset: pick the blue plate onto the pink plate",
]


class SwitchPolicy(TracingPolicy):
    """A policy with ``fr3_policy_swap.SwitchingPolicy``'s contract: ``ensure_role``
    and ``acquire`` both make the role's service the running one, a real switch
    needs the arm at home, and a role already running is a no-op."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.robot = None
        self.active = "forward"
        self.refuse = None  # a refusal dict returned by the next real switch
        self.pose_at_acquire = []

    def _switch(self, role):
        """``(switched, refusal)``."""
        if role == self.active:
            return False, None
        if self.refuse is not None:
            return False, self.refuse
        if list(self.robot.pose) != list(fake.HOME_POSE):
            return False, {
                "kind": "unavailable",
                "code": "policy_switch_fault",
                "detail": "arm_not_home",
                "fault": True,
            }
        self.trace.append(("switch", role))
        self.active = role
        return True, None

    def ensure_role(self, role):
        self.trace.append(("ensure", role))
        switched, refusal = self._switch(role)
        return refusal or {"kind": "ready", "role": role, "switched": switched}

    def acquire(self, role, *, policy_epoch, prompt=None):
        _, refusal = self._switch(role)
        if refusal is not None:
            return refusal
        self.pose_at_acquire.append((role, list(self.robot.pose)))
        return super().acquire(role, policy_epoch=policy_epoch, prompt=prompt)


def make(tmp_path, *, reset=True, rounds=1, **over):
    extra = (
        {
            "subtask_reverse_policy_role": "reset",
            "subtask_reset_prompts": ENTRIES,
        }
        if reset
        else {}
    )
    plans = [st.plan_answer(FORWARD), st.plan_answer(REVERSE)] * rounds
    r = build(
        tmp_path,
        cfg=sub_config(episodes=rounds, **extra),
        plans=plans,
        checks=st.checks(*[True] * (4 * rounds)),
        policy_factory=lambda clock, trace, power: SwitchPolicy(
            clock, trace=trace, power=power
        ),
        **over,
    )
    r.policy.robot = r.robot
    return r


def roles(r):
    return [h["role"] for h in r.policy.acquired]


def ensures(r):
    return [t for t in r.trace if t[0] == "ensure"]


# --- default: nothing changes ----------------------------------------------------------


def test_without_the_setting_both_loops_use_the_forward_role_and_nothing_is_ensured(
    tmp_path,
):
    r = make(tmp_path, reset=False)
    assert r.orch.run() == "COMPLETED"
    assert roles(r) == ["forward"] * 4
    assert prompts(r) == [st.render(a, b) for a, b in FORWARD + REVERSE]
    assert ensures(r) == [] and not any(t[0] == "switch" for t in r.trace)
    assert notes(r.orch, st.NOTE_POLICY_ROLE) == []
    for info in r.recorder.described.values():
        assert "policy_role" not in info and "policy_prompt" not in info
    for started in notes(r.orch, st.NOTE_STARTED):
        assert "policy_role" not in started


# --- the reset role --------------------------------------------------------------------


def test_the_reverse_loop_runs_the_reset_role_on_the_frozen_sentences(tmp_path):
    r = make(tmp_path)
    assert r.orch.run() == "COMPLETED"
    assert roles(r) == ["forward", "forward", "reset", "reset"]
    forward = [st.render(a, b) for a, b in FORWARD]
    assert prompts(r) == forward + SENTENCES
    # The forward role never gets a reset sentence, nor the reset role a template.
    assert not set(SENTENCES) & set(prompts(r)[:2])
    assert not set(forward) & set(prompts(r)[2:])
    described = list(r.recorder.described.values())
    assert [d["policy_role"] for d in described] == ["forward"] * 2 + ["reset"] * 2
    assert [d["policy_prompt"] for d in described][2:] == SENTENCES
    # The subtask's own text stays the template (what the subtask means).
    assert [d["instruction"] for d in described][2:] == [
        st.render(a, b) for a, b in REVERSE
    ]


def test_the_service_is_made_ready_once_per_loop_at_home_before_its_first_episode(
    tmp_path,
):
    r = make(tmp_path)
    assert r.orch.run() == "COMPLETED"
    assert ensures(r) == [("ensure", "forward"), ("ensure", "reset")]
    assert [t for t in r.trace if t[0] == "switch"] == [("switch", "reset")]
    trace = r.trace
    home = trace.index(("home",))  # the end of the forward loop
    ensure = trace.index(("ensure", "reset"))
    first_reset = trace.index(("acquire", SENTENCES[0]))
    assert home < ensure < trace.index(("switch", "reset")) < first_reset
    # The first reverse subtask starts at home; the second one does not (no home
    # between subtasks), and by then the switch was long done.
    first = [p for role, p in r.policy.pose_at_acquire if role == "reset"]
    assert first[0] == list(fake.HOME_POSE) and first[1] != list(fake.HOME_POSE)
    switched = notes(r.orch, st.NOTE_POLICY_ROLE)
    assert [(n["loop"], n["role"], n["switched"]) for n in switched] == [
        ("forward", "forward", False),
        ("reverse", "reset", True),
    ]


def test_the_next_round_switches_back_at_home_after_the_reverse_loop(tmp_path):
    r = make(tmp_path, rounds=2)
    assert r.orch.run() == "COMPLETED"
    assert ensures(r) == [
        ("ensure", "forward"),
        ("ensure", "reset"),
        ("ensure", "forward"),
        ("ensure", "reset"),
    ]
    assert [t for t in r.trace if t[0] == "switch"] == [
        ("switch", "reset"),
        ("switch", "forward"),
        ("switch", "reset"),
    ]
    assert roles(r) == ["forward", "forward", "reset", "reset"] * 2


# --- refusals --------------------------------------------------------------------------


def test_a_reverse_subtask_the_table_does_not_cover_refuses_the_plan_before_anything_moves(
    tmp_path,
):
    r = make(tmp_path)
    r.orch._reset_table = {"pink plate|table": SENTENCES[0]}  # no blue plate entry
    assert r.orch.run() == "WAIT_HUMAN"
    assert [c[2] for c in committed(r.orch)][-1] == st.R_PLAN_REFUSED
    refused = notes(r.orch, st.NOTE_PLAN_REFUSED)
    assert [n["reason"] for n in refused] == ["no_reset_prompt"]
    # Nothing of the reverse loop started: no reset acquire, no template
    # fallback, and the service was not switched.
    assert roles(r) == ["forward", "forward"]
    assert ("ensure", "reset") not in r.trace and ("switch", "reset") not in r.trace


def test_a_dispatch_without_a_sentence_never_falls_back_to_the_template(tmp_path):
    # The plan check passed with the full table; the table then loses the second
    # entry after the first reverse episode was acquired.
    r = make(tmp_path)
    acquire = r.policy.acquire

    def losing(role, **kwargs):
        found = acquire(role, **kwargs)
        if role == "reset":
            r.orch._reset_table = {"pink plate|table": SENTENCES[0]}
        return found

    r.policy.acquire = losing
    assert r.orch.run() == "WAIT_HUMAN"
    assert roles(r) == ["forward", "forward", "reset"]
    assert prompts(r)[2:] == [SENTENCES[0]]
    assert [n["reason"] for n in notes(r.orch, st.NOTE_PLAN_REFUSED)] == [
        "no_reset_prompt"
    ]
    assert [c[2] for c in committed(r.orch)][-1] == st.R_PLAN_REFUSED


def test_a_switch_the_arm_cannot_take_locks_the_run_and_starts_no_reverse_episode(
    tmp_path,
):
    r = make(tmp_path)
    r.policy.refuse = {
        "kind": "unavailable",
        "code": "policy_switch_fault",
        "detail": "arm_not_home",
        "fault": True,
    }
    assert r.orch.run() == "FAULT_LOCKED"
    assert roles(r) == ["forward", "forward"]
    assert [n for c, n in r.orch.notes if c == "policy_switch_fault"]
    assert r.orch.fault_reason == "policy_error"


def test_a_refusal_that_is_not_a_fault_waits_for_a_person(tmp_path):
    r = make(tmp_path)
    r.policy.refuse = {"kind": "unavailable", "code": "query_still_running"}
    assert r.orch.run() == "WAIT_HUMAN"
    assert [c[2] for c in committed(r.orch)][-1] == "policy_error"
    assert roles(r) == ["forward", "forward"]


def test_a_policy_without_ensure_role_still_runs_with_every_server_up(tmp_path):
    # No policy_swap: the binding runs both services, acquire(role) picks one.
    class Plain(TracingPolicy):
        pass

    r = build(
        tmp_path,
        cfg=sub_config(
            subtask_reverse_policy_role="reset", subtask_reset_prompts=ENTRIES
        ),
        policy_factory=lambda clock, trace, power: Plain(
            clock, trace=trace, power=power
        ),
    )
    assert r.orch.run() == "COMPLETED"
    assert roles(r) == ["forward", "forward", "reset", "reset"]
    assert prompts(r)[2:] == SENTENCES
