"""LEVI Live compatibility (T-C-15): rollouts and session files written by
the fake AERI world are taken in by the real live service (its controller
and worker process, ``once`` mode, against the fake model server
``levi.live.fakevlm`` on a loopback port the test opens itself). Nothing in
``levi/live`` or in the live tests is changed. No robot, no GPU.

The capture files (videos, pose and gripper CSVs) come from the live tests'
own template demo through a media sink; everything AERI owns (identity,
``events.csv``, ``metadata.json``, the steps file, the ``.complete``
marker, the manifest and the session files) is written by the recorder
layer."""

import json
import shutil
import time
from pathlib import Path

import pytest
from aeri_harness import RUN
from aeri_world import FOLDERS, GROUP, make_config, world
from conftest import scaled
from test_live_pipeline import Env

from levi.automatic.adapters import events as ev
from levi.live import criteria, mirror

OWN = {"metadata.json", "events.csv", ".complete", "aeri_steps.csv"}
STEPS = 30  # the template demo's frame count


class TemplateMedia:
    """A media sink that seals the live tests' template capture files."""

    def __init__(self, template: Path):
        self.template = Path(template)

    def open(self, folder):
        pass

    def frame(self, folder, step, obs):
        pass

    def finish(self, folder, frames):
        if frames != STEPS:
            # Not the template's length (a reset that stopped early): no
            # video at all, as NullMedia would seal it.
            return {"video_frames_match_csv": True, "stalled": [], "videos": []}
        videos = []
        for path in sorted(self.template.iterdir()):
            if path.name in OWN or path.name.startswith("."):
                continue
            target = Path(folder) / path.name
            if path.is_dir():
                shutil.copytree(path, target)
            else:
                shutil.copy2(path, target)
            if path.suffix == ".mp4":
                videos.append(path.name)
        return {"video_frames_match_csv": True, "stalled": [], "videos": videos}

    def abort(self, folder):
        pass


def judge(clock):
    # Resets stop early on a confirmed goal; forward episodes run their
    # whole horizon (the template has exactly STEPS frames).
    return ev.FakeGoalVerifier(
        lambda request, number: (
            {"decision": "confirmed"}
            if ".reset." in request["episode_id"]
            else {"decision": "unknown"}
        ),
        clock,
        RUN,
    )


@pytest.fixture
def live(tmp_path, demo_template):
    made = []

    def make(**fake_options):
        made.append(Env(tmp_path, demo_template, **fake_options))
        return made[-1]

    yield make
    for item in made:
        item.close()


def aeri_rollouts(env, demo_template, *, exclude_reset: bool):
    root = env.tmp / "rollouts"
    if exclude_reset:
        env.config.watch.exclude = [f"{GROUP}/{FOLDERS['reset']}"]
    w = world(
        root,
        cfg=make_config(episodes=2, forward_max_steps=STEPS, reset_max_steps=20),
        verifier=judge,
    )
    w.recorder.media = TemplateMedia(demo_template)
    assert w.orch.run() == "COMPLETED"
    for role, count in (("forward", 2), ("reset", 2)):
        folder = root / GROUP / FOLDERS[role]
        demos = sorted(folder.glob("demo_*"))
        assert len(demos) == count
        for demo in demos:
            assert criteria.check(demo, now=time.time() + 1).state == "complete"
    w.orch.close()
    return w


def datasets(env) -> dict:
    """Live dataset name -> its task folder, from the service's own state."""
    out = {}
    for path in sorted((env.ws / "live" / "datasets").glob("*.json")):
        state = mirror.load_state(env.config, path.stem)
        if isinstance(state, dict) and state.get("task_folder"):
            out[path.stem] = state
    return out


def test_aeri_rollouts_are_labelled_by_the_live_service_and_resets_are_excluded(
    live, demo_template
):
    env = live()
    aeri_rollouts(env, demo_template, exclude_reset=True)
    env.run(max_seconds=scaled(300))
    found = datasets(env)
    by_task = {s["task_folder"]: (name, s) for name, s in found.items()}
    assert FOLDERS["reset"] not in by_task  # watch.exclude: never labelled
    _, state = by_task[FOLDERS["forward"]]
    assert {d: r["state"] for d, r in state["demos"].items()} == {
        "demo_0001": "done",
        "demo_0002": "done",
    }
    # The live verdict is its own (auto); AERI wrote no agent label and no
    # operator label, so nothing of AERI's reaches the live agreement.
    for row in state["demos"].values():
        assert row["verdict"]["review"] == "auto"
    for demo in sorted(Path(state["capture"]).glob("demo_*")):
        meta = json.loads((demo / "metadata.json").read_text())
        assert criteria.agent_label(meta) is None
        assert meta["eval"]["verdict_by"] == "aeri"


def test_without_the_exclusion_the_reset_folder_is_a_separate_dataset(
    live, demo_template
):
    """What ``watch.exclude`` prevents: the reset rollouts are complete
    rollouts too, and would be labelled with the forward task's spec."""
    env = live()
    aeri_rollouts(env, demo_template, exclude_reset=False)
    env.run(max_seconds=scaled(300))
    tasks = {s["task_folder"] for s in datasets(env).values()}
    assert {FOLDERS["forward"], FOLDERS["reset"]} <= tasks
