"""A whole fake AERI world for the end-to-end tests (T-C-13/14/15): the
orchestrator with the real recorder layer (rollout folders, run manifest,
session files), an Initial State Contract, and the in-process fakes. No
socket, no robot, no GPU.

``world(root, ...)`` starts a run under ``root``; ``reopen(old)`` stands in
for the orchestrator process dying and a new one taking over, with fresh
fakes and a fresh recorder (which renames what the journal never sealed).
Wall-clock times written to files are pinned (``WALL``), so two runs with
the same seed write the same bytes."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from aeri_harness import RUN, config, fake, rig

from levi.automatic import scene_assessment as sa
from levi.automatic.adapters import events as ev
from levi.automatic.orchestrator import Orchestrator
from levi.automatic.recorder import RolloutRecorder, SessionFiles

GROUP = "aeri_fake"
FOLDERS = {"forward": "stack_plates__r-sm", "reset": "reset_stack_plates__r-sm"}
TEXTS = {"forward": "stack the plates", "reset": "Reset: stack the plates"}
WALL = 1_791_600_000.0
CONTRACT = sa.contract_from(
    {
        "initial_state": {
            "id": ev.SCENE_CONTRACT[0],
            "version": ev.SCENE_CONTRACT[1],
            "predicates": {"required": list(ev.SCENE_PREDICATES)},
            "observations": {"require_visible_evidence": True, "min_evidence_refs": 1},
        }
    }
)
READY = {"decision": "ready", "evidence": 1}
# N rounds: forward -> home -> scene (a reset every other round) -> next.
ROUNDS = [
    {"decision": "reset_required"},  # the initial check: reset first
    READY,  # after the reset
    READY,  # the next initial check
    {"decision": "reset_required"},  # after forward 1
    READY,  # after that reset
    READY,  # initial check
    READY,  # after forward 2: skip the reset
]


@dataclass
class World:
    root: Path
    r: object
    recorder: RolloutRecorder
    sessions: SessionFiles
    io_hook: object = None
    old_robots: list = field(default_factory=list)

    @property
    def orch(self):
        return self.r.orch

    def demo(self, role, name="demo_0001") -> Path:
        return self.root / GROUP / FOLDERS[role] / name


def _events(clock, script=None):
    return ev.FakeEventStream(
        script
        or {
            "forward": [{"step": 8, "event_type": "object_settled"}],
            "reset": [{"step": 6, "event_type": "object_settled"}],
        },
        clock,
        RUN,
    )


def make_config(**over):
    base = {
        "episodes": 3,
        "forward_folder": FOLDERS["forward"],
        "reset_folder": FOLDERS["reset"],
        "initial_state": CONTRACT,
        "max_reset_attempts": 1,
    }
    base.update(over)
    return config(**base)


def _parts(root, directory, clock, io_hook):
    recorder = RolloutRecorder(
        root,
        run_id=RUN,
        run_dir=directory,
        group=GROUP,
        texts=TEXTS,
        wall=lambda: WALL,
        io_hook=io_hook,
    )
    sessions = SessionFiles(
        root,
        run_id=RUN,
        group=GROUP,
        folders=FOLDERS,
        texts=TEXTS,
        wall=lambda: WALL,
        pid=4242,
    )
    return recorder, sessions


def world(
    root,
    *,
    cfg=None,
    scene=None,
    events=None,
    verifier=None,
    policy=None,
    robot=None,
    seed=7,
    io_hook=None,
    crash_hook=None,
) -> World:
    import random

    root = Path(root)
    directory = root / ".aeri" / "runs" / RUN
    clock = fake.FakeClock()
    cfg = cfg or make_config()
    recorder, sessions = _parts(root, directory, clock, io_hook)
    if policy is None:
        policy = {"rng": random.Random(seed)}

    def made(part):
        # A part given as ``callable(clock)`` is built on this world's clock.
        return part(clock) if callable(part) else part

    r = rig(
        directory,
        cfg=cfg,
        clock=clock,
        recorder=recorder,
        scene=made(scene)
        if scene is not None
        else ev.FakeSceneAssessor(list(ROUNDS), clock, RUN, default=READY),
        events=made(events) if events is not None else _events(clock),
        verifier=made(verifier),
        policy=made(policy),
        robot=robot,
        create=False,
    )
    r.orch = Orchestrator.create(
        directory, cfg, crash_hook=crash_hook, listener=sessions, **r.parts()
    )
    return World(root, r, recorder, sessions, io_hook)


def reopen(old: World, **kw) -> World:
    """The old process is gone; a new one restores the run."""
    old.r.orch.close()
    old.recorder.close()
    clock = old.r.clock
    recorder, sessions = _parts(old.root, old.r.directory, clock, None)
    r = rig(
        old.r.directory,
        cfg=old.r.config,
        clock=clock,
        recorder=recorder,
        scene=ev.FakeSceneAssessor([], clock, RUN, default=READY),
        events=_events(clock),
        create=False,
        **kw,
    )
    r.orch = Orchestrator.restore(r.directory, r.config, listener=sessions, **r.parts())
    return World(
        old.root, r, recorder, sessions, old_robots=[*old.old_robots, old.r.robot]
    )


# --- what must hold on disk ------------------------------------------------------------------------


VOLATILE = (
    "emitted_wall_ns",
    "prev_sha256",
    "updated_wall_ns",
    "process",
    "host",
    "rollout_dir",
    "rollout_root",
)


def _strip(value):
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def canonical(w: World) -> bytes:
    """The run's journal, rollouts, manifest and session files with the
    process identity and the real wall clock taken out."""
    out = []
    journal = w.r.directory / "state_journal.jsonl"
    for line in journal.read_bytes().splitlines():
        out.append(json.dumps(_strip(json.loads(line)), sort_keys=True).encode())
    for path in sorted(w.root.rglob("*")):
        if not path.is_file() or path == journal or "torn" in path.parts:
            continue
        if (
            path.name in ("journal.lock", "state.json")
            or path.name.startswith(".")
            and path.name.endswith(".tmp")
        ):
            continue
        data = path.read_bytes()
        if path.suffix == ".json":
            data = json.dumps(_strip(json.loads(data)), sort_keys=True).encode()
        out.append(str(path.relative_to(w.root)).encode() + b"\n" + data)
    return b"\n--\n".join(out)
