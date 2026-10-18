"""A subtask_loop run on in-process fakes only (no socket, no GPU, no robot):
the orchestrator with ``ScriptedSubtaskVlm``, ``FakeVlmPower`` and
``ScriptedGate`` sharing one ``trace`` list, so a test can read the order of
sleeps, wakes, VLM questions, policy acquisitions and terminal questions."""

from dataclasses import dataclass, field
from pathlib import Path

from aeri_harness import RUN, config, fake

from levi.automatic import state_machine as sm
from levi.automatic import subtask_loop as st
from levi.automatic.adapters import events as ev
from levi.automatic.orchestrator import Orchestrator
from levi.automatic.termination import TerminationConfig

OBJECTS = [("pink_plate_1", True), ("pink_plate_2", True), ("blue_plate_1", True)]
INVENTORY = st.inventory_answer(OBJECTS)
FORWARD = [("pink_plate_2", "pink_plate_1"), ("blue_plate_1", "table")]
REVERSE = [("pink_plate_2", "table"), ("blue_plate_1", "pink_plate_2")]


class SubtaskRobot(fake.FakeRobot):
    """The fake robot with the read-only start-pose check (``Robot.start_pose``):
    ok at the home pose unless ``pose_ok`` says otherwise."""

    def __init__(self, *args, pose_ok=None, trace=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.pose_ok = pose_ok
        self.pose_checks = 0
        self.trace = trace if trace is not None else []

    def start_pose(self):
        self.pose_checks += 1
        self.trace.append(("start_pose",))
        ok = self.pose == list(fake.HOME_POSE) if self.pose_ok is None else self.pose_ok
        return {"ok": ok, "text": "at home" if ok else "off home"}

    def home(self, *, token):
        self.trace.append(("home",))
        return super().home(token=token)


class TracingPolicy(fake.FakePolicy):
    def __init__(self, clock, *, trace, power, **kwargs):
        super().__init__(clock, **kwargs)
        self.trace = trace
        self.power = power
        self.awake_at_acquire: list = []

    def acquire(self, role, *, policy_epoch, prompt=None):
        self.trace.append(("acquire", prompt))
        self.awake_at_acquire.append(self.power.awake)
        return super().acquire(role, policy_epoch=policy_epoch, prompt=prompt)


@dataclass
class SubRig:
    directory: Path
    orch: Orchestrator
    robot: SubtaskRobot
    policy: TracingPolicy
    recorder: object
    vlm: st.ScriptedSubtaskVlm
    power: st.FakeVlmPower
    gate: st.ScriptedGate
    clock: object
    trace: list = field(default_factory=list)


def sub_config(**over):
    base = {
        "episodes": 1,
        "reset_strategy": "subtask_loop",
        "subtask_task": "stack the plates of same color together",
        "subtask_max_steps": 12,
        "initial_state": None,
        "termination": TerminationConfig(
            allow_early_stop=False, min_steps=5, settle_steps=3, cooldown_steps=5
        ),
    }
    base.update(over)
    return config(**base)


def build(
    tmp_path,
    *,
    inventory=(INVENTORY,),
    plans=None,
    checks=None,
    default_check=None,
    gate=None,
    power=None,
    pose_ok=None,
    on_call=None,
    policy=None,
    policy_factory=None,
    cfg=None,
    events=None,
    **over,
) -> SubRig:
    trace: list = []
    cfg = cfg or sub_config(**over)
    clock = fake.FakeClock()
    fence = sm.MotionFence()
    power = power or st.FakeVlmPower()
    power.trace = trace
    if plans is None:
        plans = [st.plan_answer(FORWARD), st.plan_answer(REVERSE)]
    if checks is None:
        checks = st.checks(True, True, True, True)
    vlm = st.ScriptedSubtaskVlm(
        inventory=list(inventory),
        plans=list(plans),
        checks=list(checks),
        power=power,
        trace=trace,
        on_call=on_call,
        default_check=default_check,
    )
    gate = gate or st.ScriptedGate()
    gate.trace = trace
    robot = SubtaskRobot(clock, fence.check, pose_ok=pose_ok, trace=trace)
    if policy is None and policy_factory is not None:
        policy = policy_factory(clock, trace, power)
    policy = policy or TracingPolicy(clock, trace=trace, power=power)
    recorder = fake.FakeRecorder()
    directory = Path(tmp_path) / ".aeri" / "runs" / RUN
    orch = Orchestrator.create(
        directory,
        cfg,
        robot=robot,
        policy=policy,
        recorder=recorder,
        # ``events``: a FakeEventStream script (episode id or role -> events).
        events=ev.FakeEventStream(events or {}, clock, cfg.run_id),
        verifier=ev.FakeGoalVerifier(
            [], clock, cfg.run_id, default={"decision": "confirmed"}
        ),
        scene=ev.FakeSceneAssessor([], clock, cfg.run_id),
        clock=clock,
        fence=fence,
        subtask_vlm=vlm,
        vlm_power=power,
        gate=gate,
    )
    return SubRig(
        directory, orch, robot, policy, recorder, vlm, power, gate, clock, trace
    )


def notes(orch, code=None) -> list:
    import json

    out = []
    for e in orch.journal.events:
        if (
            e.record == "note"
            and e.note is not None
            and (code is None or e.note.code == code)
        ):
            try:
                out.append(json.loads(e.note.detail))
            except ValueError:
                out.append(e.note.detail)
    return out


def committed(orch) -> list:
    return [
        (e.from_state, e.to_state, e.reason)
        for e in orch.journal.events
        if e.record == "committed"
    ]


def prompts(r) -> list:
    """The prompts of the acquired episodes, in order."""
    return [p for kind, *rest in r.trace if kind == "acquire" for p in rest]


def resume(orch, command_id="resume-1"):
    return orch.resume(
        command_id,
        expected_seq=orch.journal.next_seq,
        environment_handled=True,
        health_rechecked=True,
    )
