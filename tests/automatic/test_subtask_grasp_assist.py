"""GRASP_ASSIST in the subtask_loop run (design STEP5-AERI-RESET-DESIGN R2-A / R3, P3b), on fakes: reachable only with the
reset role and ``subtask.grasp_assist``; sits between the service switch at home and the first reset episode; its
hand-back starts that episode where the arm stands (policy epoch + 1, the old chunk cleared, state[6] the command, no
env.reset, no home); an abort holds and waits for a person without a home; a fault locks."""

import pytest
from assist_rig import K
from assist_rig import build as build_rig
from subtask_rig import FORWARD, REVERSE, build, committed, notes, sub_config
from test_subtask_reset_role import ENTRIES, SwitchPolicy

from levi.automatic import grasp_assist as ga
from levi.automatic import state_machine as sm
from levi.automatic import subtask_loop as st
from levi.automatic.orchestrator import ConfigError

BAND = "pink plate|0.15|0.4"  # the first reverse subtask picks the pink plate


class AssistGate(st.ScriptedGate):
    """Terminal D's two questions plus the takeover's keys (bytes), scripted."""

    def __init__(self, keys, **kw):
        super().__init__(**kw)
        self.keys = list(keys)
        self.reports: list = []
        self.views: list = []

    def assist_key(self, view):
        self.views.append(view)
        self.trace.append(("assist_key",))
        return K(self.keys.pop(0)) if self.keys else "stop"

    def assist_report(self, kind, data):
        self.reports.append((kind, data))
        self.trace.append(("assist_report", kind))


class AssistPolicy(SwitchPolicy):
    def quiesce(self, *, policy_epoch):
        self.trace.append(("quiesce", policy_epoch))
        return super().quiesce(policy_epoch=policy_epoch)


def make(
    tmp_path, keys, *, env_kw=None, band=BAND, assist=True, plans=None, rounds=1, **over
):
    fence = sm.MotionFence()
    rig = build_rig(fence=fence, env_kw=env_kw, band=band)
    gate = AssistGate(keys)
    extra = (
        {
            "subtask_grasp_assist": {
                "enabled": True,
                "holding_band": [band],
                **over.pop("assist_over", {}),
            }
        }
        if assist
        else {}
    )
    cfg = sub_config(
        episodes=rounds,
        subtask_reverse_policy_role="reset",
        subtask_reset_prompts=ENTRIES,
        **extra,
        **over,
    )
    r = build(
        tmp_path,
        cfg=cfg,
        fence=fence,
        gate=gate,
        grasp_assist=ga.AssistRig(rig.port, rig.limits) if assist else None,
        plans=plans or [st.plan_answer(FORWARD), st.plan_answer(REVERSE)] * rounds,
        checks=st.checks(*[True] * (4 * rounds)),
        policy_factory=lambda clock, trace, power: AssistPolicy(
            clock, trace=trace, power=power
        ),
    )
    r.policy.robot = r.robot
    r.rig = rig
    return r


def sidecar(r, note):
    import json

    return json.loads((r.orch.journal.directory / note["file"]).read_text())


def trace_of(r, *kinds):
    return [t for t in r.trace if t[0] in kinds]


def homes(r):
    return len(trace_of(r, "home"))


def reasons(r):
    return [(a, b, why) for a, b, why in committed(r.orch)]


# --- the happy path: close, lift, hand back ------------------------------------------------


def test_the_takeover_sits_between_the_service_switch_and_the_first_reset_episode(
    tmp_path,
):
    r = make(tmp_path, ["c", "u", "g"])
    assert r.orch.run() == "COMPLETED"
    moves = reasons(r)
    assert ("SCENE_ASSESS", "GRASP_ASSIST", "grasp_assist_started") in moves
    assert ("GRASP_ASSIST", "RESET_ACTIVE", "subtask_dispatched") in moves
    # Once per reverse loop: the second reverse subtask starts without it.
    assert [m for m in moves if m[1] == "GRASP_ASSIST"] == [
        ("SCENE_ASSESS", "GRASP_ASSIST", "grasp_assist_started")
    ]
    order = [t for t in r.trace if t[0] in ("ensure", "assist_key", "acquire")]
    first_reset = next(
        i
        for i, t in enumerate(order)
        if t[:1] == ("acquire",) and t[1] == "Reset: pick the pink plate onto the table"
    )
    assert order.index(("ensure", "reset")) < order.index(("assist_key",)) < first_reset
    assert len([t for t in order if t == ("assist_key",)]) == 3


def test_the_handback_starts_the_reset_episode_where_the_arm_stands(tmp_path):
    r = make(tmp_path, ["c", "u", "g"])
    r.policy.inflight["stale"] = object()  # a chunk left from the old episode
    r.orch.run()
    note = notes(r.orch, "grasp_assist_handoff")[0]
    # R3: epoch + 1, the old chunk cleared, a fresh start, state[6] the command.
    first_reset = next(
        e
        for e in r.orch.journal.events
        if e.record == "committed" and e.to_state == "RESET_ACTIVE"
    )
    assert first_reset.policy_epoch == note["policy_epoch_before"] + 1
    assert note["policy_epoch_next"] == first_reset.policy_epoch
    full = sidecar(r, note)
    assert full["chunk_cleared"] is True and "stale" not in r.policy.inflight
    assert ("quiesce", note["policy_epoch_before"]) in r.trace
    assert note["policy_state6"] == 0.0 and note["opening"] == pytest.approx(
        0.2986, abs=1e-3
    )
    assert note["policy_state6"] != note["opening"]
    assert full["env_reset"] is False and full["homed"] is False
    assert note["by"] == "operator" and note["fact_status"] in (
        "holding_candidate",
        "holding_verified",
    )
    # No env.reset() and no extra home: the forward loop's end and the reverse
    # loop's end only, as without the takeover.
    assert not any(route in ("jointreset", "startimp") for route, _ in r.rig.env.posts)
    assert homes(r) == 2
    # The gate is open again for the policy and no assist token is left.
    assert r.rig.robot.admission_snapshot().mode == "open"
    assert r.rig.fence.current is None or r.rig.fence.current.kind != "assist"
    # The whole record is in the note's sidecar.
    assert full["fact_source"] == "verified_measurement" and full["travel_m"] >= 0.0


def test_no_home_command_between_the_switch_and_the_first_reset_episode(tmp_path):
    r = make(tmp_path, ["c", "u", "g"])
    r.orch.run()
    start = r.trace.index(("ensure", "reset"))
    first = next(
        i
        for i, t in enumerate(r.trace)
        if i > start and t[0] == "acquire" and t[1].startswith("Reset:")
    )
    assert ("home",) not in r.trace[start:first]


def test_each_key_and_each_fact_is_in_the_journal(tmp_path):
    r = make(tmp_path, ["x", "c", "u", "g"])
    r.orch.run()
    steps = notes(r.orch, "grasp_assist_step")
    keys = [s["key"] for s in steps]
    assert keys[0] == "enter" and keys[1:4] == ["x+", "close", "lift"]
    assert all(s["kind"] == "executed" for s in steps[1:4])
    facts = notes(r.orch, "holding_fact")
    # close, the lift's own candidate, the lift, and the re-check at g.
    assert [(f["stage"], f["status"]) for f in facts] == [
        ("close", "holding_candidate"),
        ("close", "holding_candidate"),
        ("lift", "holding_verified"),
        ("close", "holding_candidate"),
    ]
    assert facts[-1]["check"] == "handback" and facts[1]["check"] == "lift"
    assert all(f["source"] == "verified_measurement" for f in facts)
    assert sidecar(r, facts[2])["holding_verified"] is True
    # The page/console is told each target before it is sent, and each result.
    kinds = [k for k, _ in r.gate.reports]
    assert kinds[0] == "enter" and kinds.count("proposal") == 4 and "handback" in kinds
    proposal = next(d for k, d in r.gate.reports if k == "proposal")
    assert proposal["kind"] == "move" and proposal["target"] is not None
    assert r.gate.views[0]["opening"] == pytest.approx(
        1.0
    )  # the measured opening is shown
    assert r.gate.views[-1]["fact"] == "holding_verified"


# --- a refusal changes nothing; an abort holds and never homes ----------------------------


def test_a_refused_key_is_reported_and_the_takeover_goes_on(tmp_path):
    r = make(tmp_path, ["Z", "a"], env_kw={"start": (0.48, -0.011, 0.06)})
    assert r.orch.run() == "WAIT_HUMAN"
    refused = [d for k, d in r.gate.reports if k == "refused"]
    assert [d["code"] for d in refused] == ["z_below_floor"]
    assert r.rig.env.pose_targets == []
    assert r.orch.state == "WAIT_HUMAN"  # a person's mistake: no fault


def test_abort_holds_and_waits_for_a_person_with_no_home(tmp_path):
    r = make(tmp_path, ["x", "a"])
    assert r.orch.run() == "WAIT_HUMAN"
    assert reasons(r)[-1] == ("GRASP_ASSIST", "WAIT_HUMAN", "grasp_assist_aborted")
    assert homes(r) == 1  # only the forward loop's end
    assert not any(t[0] == "acquire" and t[1].startswith("Reset:") for t in r.trace)
    assert r.robot.holds >= 1
    # The gate stays closed and no assist token is left.
    assert r.rig.robot.admission_snapshot().mode == "closed"
    assert r.rig.fence.current is None
    assert notes(r.orch, "grasp_assist_step")[-1]["key"] == "abort"
    assert [k for k, _ in r.gate.reports][-1] == "abort"
    assert r.orch.journal.state == "WAIT_HUMAN"


def test_a_stop_in_the_takeover_aborts_it_the_same_way(tmp_path):
    r = make(tmp_path, [])  # no key: the gate answers "stop" (q)
    assert r.orch.run() == "WAIT_HUMAN"
    assert reasons(r)[-1][2] == "grasp_assist_aborted" and homes(r) == 1


def test_a_comms_fault_locks_and_keeps_the_gate_closed(tmp_path):
    r = make(tmp_path, ["c", "g"])
    r.rig.env.fail_route = "close_gripper"
    assert r.orch.run() == "FAULT_LOCKED"
    assert notes(r.orch, "grasp_assist_fault")
    assert reasons(r)[-1][:2] == ("GRASP_ASSIST", "FAULT_LOCKED")
    assert r.rig.robot.admission_snapshot().mode == "closed"
    assert homes(r) == 1
    assert [k for k, _ in r.gate.reports][-1] == "fault"


def test_an_empty_gripper_cannot_be_handed_over(tmp_path):
    r = make(tmp_path, ["c", "g", "a"], env_kw={"object_opening": 0.01})
    assert r.orch.run() == "WAIT_HUMAN"
    refused = [d["code"] for k, d in r.gate.reports if k == "refused"]
    assert refused == ["holding_fact_empty"]
    assert not any(t[0] == "acquire" and t[1].startswith("Reset:") for t in r.trace)


# --- unreachable with defaults; configuration is checked before anything moves -----------


def test_without_the_setting_the_state_is_never_reached(tmp_path):
    r = make(tmp_path, [], assist=False)
    assert r.orch.run() == "COMPLETED"
    assert not any("GRASP_ASSIST" in (a, b) for a, b, _ in reasons(r))
    assert r.gate.views == []


def test_a_disabled_block_is_not_a_takeover(tmp_path):
    r = make(
        tmp_path,
        [],
        assist=False,
        subtask_grasp_assist={"enabled": False, "holding_band": [BAND]},
    )
    assert r.orch.run() == "COMPLETED"
    assert not any("GRASP_ASSIST" in (a, b) for a, b, _ in reasons(r))


def test_the_first_objects_holding_band_is_needed_before_anything_moves(tmp_path):
    r = make(tmp_path, ["c", "u", "g"], band="blue plate|0.15|0.4")
    assert r.orch.run() == "WAIT_HUMAN"
    assert reasons(r)[-1][2] == "subtask_plan_refused"
    assert notes(r.orch, st.NOTE_PLAN_REFUSED)[-1]["reason"] == "no_holding_band"
    assert r.gate.views == [] and r.rig.env.posts == []


def test_the_takeover_needs_the_reset_role_the_rig_and_the_gate(tmp_path):
    with pytest.raises(ConfigError, match="reverse_policy_role reset"):
        sub_config(
            subtask_grasp_assist={"enabled": True, "holding_band": [BAND]},
        )
    with pytest.raises(ConfigError, match="AssistRig"):
        build(
            tmp_path / "c1",
            cfg=sub_config(
                subtask_reverse_policy_role="reset",
                subtask_reset_prompts=ENTRIES,
                subtask_grasp_assist={"enabled": True, "holding_band": [BAND]},
            ),
        )
    rig = build_rig()
    with pytest.raises(ConfigError, match="gate"):
        build(
            tmp_path / "c2",
            cfg=sub_config(
                subtask_reverse_policy_role="reset",
                subtask_reset_prompts=ENTRIES,
                subtask_grasp_assist={"enabled": True, "holding_band": [BAND]},
            ),
            grasp_assist=ga.AssistRig(rig.port, rig.limits),
        )
    with pytest.raises(ConfigError, match="belongs to"):
        build(tmp_path / "c9", grasp_assist=ga.AssistRig(rig.port, rig.limits))
