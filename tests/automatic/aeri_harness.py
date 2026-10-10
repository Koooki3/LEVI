"""A run of the AERI orchestrator wired to in-process fakes only (no
socket, no process, no GPU, no robot). ``build`` starts a run;
``restart`` simulates the orchestrator process dying and a new one taking
over the same run directory with fresh fakes (a fresh robot connection
counts its commands from zero)."""

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path

from levi.automatic import state_machine as sm
from levi.automatic.adapters import events as ev
from levi.automatic.orchestrator import Orchestrator, RunConfig
from levi.automatic.termination import TerminationConfig

ROOT = Path(__file__).resolve().parents[2]
FAKE = ROOT / "integrations" / "fr3_automatic" / "fake.py"
RUN = "r-sm"
PLAN = "ab" * 32


def load_fake():
    name = "fr3_automatic_fake"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, FAKE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fake = load_fake()


@dataclass
class Rig:
    directory: Path
    config: RunConfig
    clock: object
    fence: sm.MotionFence
    robot: object
    policy: object
    recorder: object
    events: object
    verifier: object
    scene: object
    orch: Orchestrator | None = None
    crashes: list = field(default_factory=list)

    def parts(self) -> dict:
        return {
            "robot": self.robot,
            "policy": self.policy,
            "recorder": self.recorder,
            "events": self.events,
            "verifier": self.verifier,
            "scene": self.scene,
            "clock": self.clock,
            "fence": self.fence,
        }


def config(**over) -> RunConfig:
    termination = over.pop("termination", None) or TerminationConfig(
        min_steps=5, settle_steps=3, cooldown_steps=5
    )
    base = {
        "run_id": RUN,
        "plan_sha256": PLAN,
        "episodes": 2,
        "forward_max_steps": 40,
        "reset_max_steps": 20,
        "termination": termination,
    }
    base.update(over)
    return RunConfig(**base)


def rig(
    directory,
    *,
    cfg=None,
    clock=None,
    robot=None,
    policy=None,
    recorder=None,
    events=None,
    verifier=None,
    scene=None,
    crash_hook=None,
    create=True,
) -> Rig:
    cfg = cfg or config()
    clock = clock or fake.FakeClock()
    fence = sm.MotionFence()
    r = Rig(
        directory=Path(directory),
        config=cfg,
        clock=clock,
        fence=fence,
        robot=robot or fake.FakeRobot(clock, fence.check),
        policy=policy or fake.FakePolicy(clock),
        recorder=recorder or fake.FakeRecorder(),
        events=events
        or ev.FakeEventStream(
            {"forward": [{"step": 8, "event_type": "object_settled"}]},
            clock,
            cfg.run_id,
        ),
        verifier=verifier
        or ev.FakeGoalVerifier(
            [], clock, cfg.run_id, default={"decision": "confirmed"}
        ),
        scene=scene or ev.FakeSceneAssessor([], clock, cfg.run_id),
    )
    if isinstance(r.robot, dict):
        r.robot = fake.FakeRobot(clock, fence.check, **r.robot)
    if isinstance(r.policy, dict):
        r.policy = fake.FakePolicy(clock, **r.policy)
    if isinstance(r.recorder, dict):
        r.recorder = fake.FakeRecorder(**r.recorder)
    if create:
        r.orch = Orchestrator.create(
            r.directory, cfg, crash_hook=crash_hook, **r.parts()
        )
    return r


def build(tmp_path, **kwargs) -> Rig:
    return rig(Path(tmp_path) / ".aeri" / "runs" / RUN, **kwargs)


def restart(old: Rig, **kwargs) -> Rig:
    """The old process is gone (its lock released); a new one takes over."""
    if old.orch is not None:
        old.orch.close()
    old.recorder.close()
    new = rig(
        old.directory,
        cfg=kwargs.pop("cfg", old.config),
        clock=old.clock,
        create=False,
        **kwargs,
    )
    new.orch = Orchestrator.restore(new.directory, new.config, **new.parts())
    return new


def committed(orch) -> list:
    return [
        (e.from_state, e.to_state, e.reason)
        for e in orch.journal.events
        if e.record == "committed"
    ]


def results(orch) -> list:
    return [
        e.episode_result
        for e in orch.journal.events
        if e.record == "committed" and e.episode_result is not None
    ]


def crash_at(point: str, occurrence: int = 0):
    """A crash hook raising SimulatedCrash the ``occurrence``-th time the
    orchestrator reaches ``point``."""
    seen = {"n": 0}

    def hook(found):
        if found == point:
            if seen["n"] == occurrence:
                seen["n"] += 1
                raise fake.SimulatedCrash(f"crash at {point}#{occurrence}")
            seen["n"] += 1

    return hook
