"""The hosted run with real processes: ``hostchild.py`` under a real pty with
the fake client (``fake_client.py``) behind it. Nothing of the robot is
touched: the fake client opens no socket, camera or device, and "moves" the
arm by appending lines to a file. It is the only client these tests run; the
real one is never started (the launch is the fake profile: no scope, the fake
script in place of ``uv run python run_robotiq_client_collect_data.py``).

These tests prove what the scripted ones cannot: that the keyboard works end to
end, that a stray key really becomes "RUN screws" in a client that is not told
off, that a hold stops the steps and a kill leaves no more, and that a client
whose core died is hung up."""

import json
import os
import signal
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from conftest import scaled
from test_marvel0_run import (
    GROUP,
    MODEL,
    PROMPT,
    SCREWS,
    Machine,
    refusal,
)

from levi.marvel0 import command, run

pytestmark = pytest.mark.skipif(
    not os.path.exists("/dev/ptmx"), reason="needs a pseudo-terminal"
)
SCREWS_FILE = "pick_screws_of_same_size_into_the_empty_slot_of_screw_box"


@pytest.fixture
def live(tmp_path, monkeypatch):
    m = Machine(tmp_path, monkeypatch, live=True)
    yield m
    m.close()
    child = m.sup._child
    pgid = child.pid if child is not None else None
    for spawned in m.children:
        pgid = spawned.pid
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass


def gone(pid, timeout=5.0) -> bool:
    """Whether the process is gone (a zombie is gone)."""
    deadline = time.monotonic() + scaled(timeout)
    while time.monotonic() < deadline:
        if run.proc_state(pid) in (None, "Z"):
            return True
        time.sleep(0.05)
    return False


def steady(m, seconds=0.6) -> bool:
    """Whether the fake arm did nothing for ``seconds``."""
    before = len(m.arm())
    time.sleep(seconds)
    return len(m.arm()) == before


def to_gate(m, max_steps=30, **cfg):
    m.fake(**{"connect_s": 0.1, "mux_s": 0.05, **cfg})
    m.start(max_steps=max_steps)
    return m.wait_state("at_gate")


# --- the whole way through -----------------------------------------------------------------


def test_a_run_from_the_start_to_q_at_the_gate(live):
    m = live
    m.fake(connect_s=0.1, mux_s=0.05)
    m.start(max_steps=400)
    view = m.wait_state("at_gate")
    assert view["episode"]["no"] == 1 and view["actions"]["enter"]["allowed"]
    m.press("enter")
    m.wait_state("running")
    assert m.press("success")["sent"] == "success"
    view = m.wait_state("at_gate", **{"episode.no": 2})
    assert view["last_episode"]["outcome"] == "success"
    assert view["episode"]["counted"] == 1
    m.press("end_at_gate")
    view = m.wait_state("done")
    assert view["reason"]["code"] == "user_quit" and view["exit"] == {
        "code": 0,
        "signal": None,
    }
    # what the client saw and did
    first = Path(m.site.rollout_root) / GROUP / "pick_the_eggplant_in_the_blue_plate"
    assert (first / "demo_0001" / "metadata.json").is_file()
    kinds = [row["kind"] for row in m.arm()]
    assert kinds.count("home") == 60 and kinds.count("step") > 0
    assert [r["key"] for r in m.history() if r["event"] == "key"] == [
        "enter",
        "success",
        "end_at_gate",
    ]
    exit_file = m.log_path().with_suffix(".exit.json")
    assert json.loads(exit_file.read_text())["code"] == 0
    assert run.proc_state(m.record()["leader"]["pid"]) in (None, "Z")
    assert m.sup.active() is None and m.sessions() == [
        f"{GROUP}__pick_the_eggplant_in_the_blue_plate.json"
    ]


def test_the_client_gets_an_allowlisted_environment_a_keyboard_and_the_rendered_flags(
    live,
):
    m = live
    to_gate(m, connect_s=0.1)
    start = m.client()
    assert start["env"] == run.child_env(m.environ)
    for name in (
        "LEVI_UI_TOKEN",
        "HF_TOKEN",
        "ROS_DISTRO",
        "CONDA_PREFIX",
        "LD_LIBRARY_PATH",
        "DISPLAY",
    ):
        assert name not in start["env"]
    assert start["tty"] is True
    assert start["sid"] == start["pgid"] == m.record()["leader"]["pid"]
    assert start["cwd"] == m.site.openpi_checkout
    site = m.site
    form = command.validate(site, MODEL, m.form())
    preview = command.render(site, MODEL, form)
    assert start["argv"] == [
        *preview.argv[len(command.PREFIX) :],
        "--no-display-images",
    ]
    assert "--levi-mode=off" in start["argv"] and "--no-step-by-step" in start["argv"]
    assert not any(
        a.startswith("--levi-mode=on") or a == "--display-images" for a in start["argv"]
    )


def test_the_log_of_a_live_client_is_private_and_free_of_paths(live):
    m = live
    to_gate(m)
    assert oct(os.stat(m.log_path()).st_mode & 0o777) == "0o600"
    text = "\n".join(m.sup.log(200)["lines"])
    assert "=== ready | ep 1" in text and "Rollout data: " in text
    assert str(m.tmp) not in text and "demo_XXXX" in text


# --- the incident of 2026-10-20 -------------------------------------------------------------


def test_the_page_never_sends_s_at_the_start_gate(live):
    m = live
    to_gate(m)
    assert refusal(m.press, "success")[1] == "state_not_allowed"
    assert refusal(m.press, "failure")[1] == "state_not_allowed"
    assert refusal(m.press, "discard")[1] == "state_not_allowed"
    assert refusal(m.press, "exit_home")[1] == "state_not_allowed"
    time.sleep(0.8)
    view = m.look()
    assert view["state"] == "at_gate"
    assert [row for row in m.arm() if row["kind"] != "none"] == []
    assert all(
        SCREWS not in line
        for line in m.sup.log(200)["lines"]
        if line.startswith("  ->")
    )
    assert m.sessions() == [f"{GROUP}__pick_the_eggplant_in_the_blue_plate.json"]


def test_an_s_that_reaches_the_gate_runs_the_screws_task_and_the_page_freezes_it(live):
    """The fake is faithful to the incident (an ``s`` at the gate is "RUN
    screws" and starts), and the page's watcher still sees the task change."""
    m = live
    to_gate(m)
    os.write(m.sup._master, b"s")  # what a stray keystroke does; not the page's way
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        m.health()
        view = m.look()
        if view["state"] == "held":
            break
        time.sleep(0.05)
    assert view["state"] == "held" and view["reason"]["code"] == "held_task_changed"
    assert f"{GROUP}__{SCREWS_FILE}.json" in m.sessions()
    assert any(f'-> "{SCREWS}"' in line for line in m.sup.log(200)["lines"])
    # frozen: no further motion
    assert steady(m)
    m.end()
    assert m.wait_state("done")["reason"]["code"] == "ended_by_page"


def run_two_label_clicks(m):
    """Start a long episode, label it, and click the label again twice while
    the client saves the episode; returns what the second clicks answered."""
    # (the client reads one key per step: at 0.3 s a step, both clicks land
    # before it has read the first, as two quick clicks do on a real page)
    m.fake(connect_s=0.1, mux_s=1.2, step_s=0.3)
    m.start(max_steps=2000)
    m.wait_state("at_gate")
    m.press("enter")
    m.wait_state("running")
    m.press("success")
    answers = []
    for _ in range(2):
        m.sup._keyed = False  # (the in-flight rule is not what is under test)
        try:
            answers.append(m.press("success")["sent"])
        except run.Refuse as exc:
            answers.append(exc.code)
    return answers


def test_two_label_clicks_during_a_slow_save_do_not_become_a_second_task(live):
    m = live
    answers = run_two_label_clicks(m)
    assert answers == ["key_locked", "key_locked"]
    m.wait_state("at_gate", **{"episode.no": 2})
    time.sleep(0.8)
    assert m.look()["state"] == "at_gate", (
        "nothing was queued: no second episode started"
    )
    assert m.sessions() == [f"{GROUP}__pick_the_eggplant_in_the_blue_plate.json"]
    assert not any(f'-> "{SCREWS}"' in line for line in m.sup.log(200)["lines"])
    homes = [r for r in m.arm() if r["kind"] == "home"]
    assert len(homes) == 60, "one home before the episode, one after it"


def test_without_the_label_lock_the_same_clicks_do_run_the_screws_task(
    live, monkeypatch
):
    """The negative control: remove the one-label-per-episode rule and the very
    same clicks make the client start the screws task nobody asked for."""
    m = live
    monkeypatch.setattr(run.Supervisor, "_locks", lambda self, f, d: (False, False))
    answers = run_two_label_clicks(m)
    assert answers == ["success", "success"]
    deadline = time.monotonic() + 10
    while (
        time.monotonic() < deadline
        and f"{GROUP}__{SCREWS_FILE}.json" not in m.sessions()
    ):
        m.health()
        m.look()
        time.sleep(0.05)
    assert f"{GROUP}__{SCREWS_FILE}.json" in m.sessions()


# --- hold and end on the real process tree -----------------------------------------------------


def test_hold_stops_the_steps_and_end_leaves_no_further_motion(live):
    m = live
    m.fake(connect_s=0.1, step_s=0.01)
    m.start(max_steps=3000)
    m.wait_state("at_gate")
    m.press("enter")
    m.wait_state("running")
    time.sleep(0.3)
    pid = m.record()["leader"]["pid"]
    assert not steady(m, 0.2), "the fake arm is stepping"
    m.hold()
    view = m.wait_state("held")
    assert run.proc_state(pid) == "T"
    assert steady(m, 0.8), "a hold stops the steps"
    assert view["reason"]["code"] == "held_person"
    # the whole group is stopped, the client too
    start = m.client()
    assert run.proc_state(start["pid"]) == "T"
    m.end()
    view = m.wait_state("done")
    assert (view["reason"]["code"], view["exit"]["signal"]) == ("ended_by_page", 9)
    assert gone(pid) and gone(start["pid"])
    assert steady(m, 0.8), "an end leaves no further motion"
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)


def test_hold_in_the_middle_of_the_blind_homing_freezes_it(live):
    m = live
    m.fake(connect_s=0.1, home_steps=100, home_step_s=0.05)
    m.start(max_steps=30)
    m.wait_state("at_gate")
    m.press("enter")
    m.wait_state("homing")
    time.sleep(0.3)
    moved = len(m.arm())
    assert moved > 0
    m.hold()
    m.wait_state("held")
    assert steady(m, 0.8) and len(m.arm()) <= moved + 2


def test_the_fake_shows_why_sigterm_is_not_a_hold(live):
    """Why hold is SIGSTOP: a SIGTERM at the step-limit prompt becomes 'd'
    and then a blind homing (RC:1236, RC:924), exactly what a hold must not do."""
    m = live
    m.fake(connect_s=0.1, home_steps=40, home_step_s=0.02)
    m.start(max_steps=15)
    m.wait_state("at_gate")
    m.press("enter")
    m.wait_state("awaiting_label")
    before = len([r for r in m.arm() if r["kind"] == "home"])
    os.kill(m.client()["pid"], signal.SIGTERM)  # the test, not the page
    deadline = time.monotonic() + 8
    while (
        time.monotonic() < deadline
        and len([r for r in m.arm() if r["kind"] == "home"]) < before + 40
    ):
        time.sleep(0.05)
    assert len([r for r in m.arm() if r["kind"] == "home"]) >= before + 40


def test_a_client_that_never_reaches_the_gate_is_killed(live):
    m = live
    m.sup.start_timeout_s = 1.5
    m.fake(silent=True, no_session=True)
    m.start()
    pid = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        m.health()
        view = m.look()
        pid = pid or m.record()["leader"]["pid"]
        if view["state"] == "failed":
            break
        time.sleep(0.05)
    assert (view["state"], view["reason"]["code"]) == ("failed", "no_gate_in_time")
    assert gone(pid)
    assert not (m.tmp / "arm.lock").exists() or not run.gpumgr.flock_holders(
        m.tmp / "arm.lock"
    )


def test_a_client_that_crashes_is_a_failed_run_with_its_log(live):
    m = live
    m.fake(connect_s=0.1, crash_at_step=3, step_s=0.02)
    m.start(max_steps=100)
    m.wait_state("at_gate")
    m.press("enter")
    view = m.wait_state("failed")
    assert view["reason"]["code"] == "exit_code" and view["exit"]["code"] == 1
    assert any("Traceback" in line for line in m.sup.log(200)["lines"])
    assert not run.gpumgr.flock_holders(m.tmp / "arm.lock")


def test_a_client_that_refuses_to_start_is_a_failed_run(live):
    m = live
    m.fake(refuse=True)
    m.start()
    view = m.wait_state("failed")
    assert view["exit"]["code"] == 2
    assert any("REFUSING TO START" in line for line in m.sup.log(50)["lines"])


# --- text the client words differently -----------------------------------------------------------


def test_a_start_gate_in_other_words_offers_no_key_and_can_be_ended(live):
    m = live
    m.fake(connect_s=0.1, override={"gate_banner": "\n=== READY | episode {ep} ===\n"})
    m.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        m.health()
        view = m.look()
        time.sleep(0.05)
    assert (view["state"], view["reason"]["code"]) == ("starting", "gate_not_confirmed")
    assert not any(view["actions"][a]["allowed"] for a in run.KEY_RULES)
    assert refusal(m.press, "enter")[1] == "state_not_allowed"
    assert view["actions"]["end"]["allowed"]
    m.end()
    assert m.wait_state("done")["reason"]["code"] == "ended_by_page"


def test_a_step_limit_prompt_in_other_words_offers_no_label(live):
    m = live
    m.fake(
        connect_s=0.1,
        override={"judge_prompt": "  step budget used up, label it:\n  [s] [f] [d]"},
    )
    m.start(max_steps=10)
    m.wait_state("at_gate")
    m.press("enter")
    view = m.wait_state("finishing", timeout=10)
    assert view["reason"]["code"] == "episode_closing"
    assert not any(view["actions"][a]["allowed"] for a in run.KEY_RULES)
    assert view["actions"]["hold"]["allowed"]


# --- the monitor ----------------------------------------------------------------------------------


def test_two_red_monitor_writes_freeze_the_running_client(live):
    m = live
    m.fake(connect_s=0.1, step_s=0.01)
    m.start(max_steps=3000)
    m.wait_state("at_gate")
    m.press("enter")
    m.wait_state("running")
    m.health(red=True)
    m.look()
    assert m.look()["state"] == "running", "one red write"
    time.sleep(0.05)
    m.health(red=True)
    view = m.look()
    assert (view["state"], view["reason"]["code"]) == ("held", "held_monitor_red")
    assert steady(m, 0.8)


# --- the core dies --------------------------------------------------------------------------------

CORE = textwrap.dedent(
    """
    import os, subprocess, sys, time
    hostchild, spec, log = sys.argv[1:4]
    master, slave = os.openpty()
    out = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    child = subprocess.Popen(
        [sys.executable, "-I", hostchild, spec],
        stdin=slave, stdout=out, stderr=out, start_new_session=True, close_fds=True,
    )
    os.close(slave)
    print(child.pid, flush=True)
    time.sleep(600)
    """
)


def launch_with_a_core(m, tmp_path, **fake):
    """The launcher and the fake client under a pty whose master a separate
    'core' process holds; returns (core, launcher pid, client pid)."""
    root = Path(m.site.rollout_root)
    m.fake(connect_s=0.1, step_s=0.02, **fake)
    argv = [
        sys.executable,
        str(Path(__file__).with_name("fake_client.py")),
        f"--rollout-root={root}",
        f"--rollout-group={GROUP}",
        f"--prompt={PROMPT}",
        "--max-timesteps=100000",
    ]
    spec = tmp_path / "core-spec.json"
    spec.write_text(
        json.dumps(
            {
                "argv": argv,
                "cwd": str(tmp_path),
                "exit_file": str(tmp_path / "exit.json"),
            }
        )
    )
    core = subprocess.Popen(
        [
            sys.executable,
            "-c",
            CORE,
            m.host.hostchild,
            str(spec),
            str(tmp_path / "core.log"),
        ],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    launcher = int(core.stdout.readline())
    log = tmp_path / "core.log"
    deadline = time.monotonic() + 10
    # (until the client sits at its start gate: a terminal that hung up before
    # it set its keyboard up would make it run without one)
    while time.monotonic() < deadline and "=== ready | ep 1" not in (
        log.read_text() if log.exists() else ""
    ):
        time.sleep(0.05)
    start = json.loads((root / "fake_start.json").read_text())
    return core, launcher, start["pid"]


def test_a_launcher_whose_core_died_hangs_the_client_up(live, tmp_path):
    m = live
    core, launcher, client = launch_with_a_core(m, tmp_path)
    try:
        # (the fake client sits at its start gate: nothing but time passes)
        assert run.proc_state(client) not in (None, "Z")
        core.kill()
        core.wait()
        assert gone(client, 6) and gone(launcher, 6), "no keyboard, no client"
        assert steady(m, 0.5)
    finally:
        for pid in (client, launcher):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        core.kill()


def test_a_client_that_ignores_the_hangup_is_killed_by_the_launcher(live, tmp_path):
    """The kernel's SIGHUP is not enough for a client that ignores it; the
    launcher's own handler kills the group."""
    m = live
    core, launcher, client = launch_with_a_core(m, tmp_path, ignore_hup=True)
    try:
        core.kill()
        core.wait()
        assert gone(client, 6) and gone(launcher, 6), "no keyboard, no client"
        assert steady(m, 0.5)
    finally:
        for pid in (client, launcher):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        core.kill()


def test_a_client_in_a_group_of_its_own_is_hung_up_too(live, tmp_path):
    """The kernel hangs up the terminal's foreground group; a client that put
    itself elsewhere (``uv`` may) is reached by the launcher's own handler."""
    m = live
    core, launcher, client = launch_with_a_core(m, tmp_path, own_group=True)
    try:
        assert os.getpgid(client) != os.getpgid(launcher)
        core.kill()
        core.wait()
        assert gone(client, 6) and gone(launcher, 6)
        assert steady(m, 0.5)
    finally:
        for pid in (client, launcher):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        core.kill()


def test_a_frozen_client_is_hung_up_too_when_its_core_dies(live, tmp_path):
    m = live
    core, launcher, client = launch_with_a_core(m, tmp_path)
    try:
        os.killpg(launcher, signal.SIGSTOP)  # what a hold does
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and run.proc_state(client) != "T":
            time.sleep(0.05)
        assert run.proc_state(client) == "T"
        core.kill()
        core.wait()
        assert gone(client, 6) and gone(launcher, 6)
    finally:
        for pid in (client, launcher):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        core.kill()


def test_the_launcher_ignores_the_signals_meant_for_the_client(live, tmp_path):
    m = live
    core, launcher, client = launch_with_a_core(m, tmp_path)
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            os.kill(launcher, sig)
        time.sleep(0.4)
        assert run.proc_state(launcher) not in (None, "Z"), "the launcher stays"
        assert run.proc_state(client) not in (None, "Z"), (
            "the client is not touched by them"
        )
    finally:
        os.killpg(launcher, signal.SIGKILL)
        core.kill()
        core.wait()


def test_the_launcher_refuses_to_run_a_client_without_a_keyboard(tmp_path):
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "argv": [sys.executable, "-c", "print('ran')"],
                "cwd": str(tmp_path),
                "exit_file": str(tmp_path / "exit.json"),
            }
        )
    )
    done = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(run.__file__).with_name("hostchild.py")),
            str(spec),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert done.returncode == run.EXIT_NO_TTY and "ran" not in done.stdout
    assert json.loads((tmp_path / "exit.json").read_text())["code"] == run.EXIT_NO_TTY


def test_the_launcher_reports_a_client_that_cannot_start(tmp_path):
    master, slave = os.openpty()
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "argv": ["/nonexistent/client"],
                "cwd": str(tmp_path),
                "exit_file": str(tmp_path / "exit.json"),
            }
        )
    )
    child = subprocess.Popen(
        [
            sys.executable,
            "-I",
            str(Path(run.__file__).with_name("hostchild.py")),
            str(spec),
        ],
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    os.close(slave)
    try:
        assert child.wait(20) == run.EXIT_SPAWN
        assert (
            json.loads((tmp_path / "exit.json").read_text())["code"] == run.EXIT_SPAWN
        )
    finally:
        os.close(master)


def test_the_launcher_ends_with_the_clients_exit_code(tmp_path):
    master, slave = os.openpty()
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "argv": [sys.executable, "-c", "raise SystemExit(3)"],
                "cwd": str(tmp_path),
                "exit_file": str(tmp_path / "exit.json"),
            }
        )
    )
    child = subprocess.Popen(
        [
            sys.executable,
            "-I",
            str(Path(run.__file__).with_name("hostchild.py")),
            str(spec),
        ],
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    os.close(slave)
    try:
        assert child.wait(20) == 3
        record_ = json.loads((tmp_path / "exit.json").read_text())
        assert (record_["code"], record_["signal"]) == (3, None)
        assert stat.S_IMODE(os.stat(tmp_path / "exit.json").st_mode) == 0o600
        assert sorted(os.listdir(tmp_path)) == ["exit.json", "spec.json"], (
            "the record is written whole: no temporary file is left"
        )
    finally:
        os.close(master)


def test_the_master_is_not_inherited_by_the_children_of_the_core(live):
    m = live
    to_gate(m)
    assert os.get_inheritable(m.sup._master) is False
    assert os.get_inheritable(m.sup._arm_fd) is False
    # the client and its launcher hold neither the master nor the arm lock
    client = m.client()["pid"]
    for pid in (m.record()["leader"]["pid"], client):
        held = {
            os.readlink(f"/proc/{pid}/fd/{fd}") for fd in os.listdir(f"/proc/{pid}/fd")
        }
        assert not any("ptmx" in target for target in held)
        assert not any("arm.lock" in target for target in held)
