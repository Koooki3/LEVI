"""The hosted AERI terminal D with real processes: ``hostchild.py`` under real
ptys with the fake terminal D (``fake_aeri.py``) behind it, driven by the page's
supervisor. Nothing of the robot is touched: the fake opens no socket, camera or
device and "moves" the arm by appending lines to a file. The real AERI is never
started.

These tests prove what the scripted ones cannot: that the keys really reach D's
prompts through the page's pty, that Ctrl+C is a real SIGINT that D wraps up on,
that a second one would break its shutdown (so the page never sends it), that a
D that ignores the stop can be ended, that a run found again after a restart is
stopped once, and that a D whose keyboard went away is hung up (SIGINT first,
SIGKILL after the grace)."""

import json
import os
import signal
import subprocess
import sys
import time

import pytest
from aeri_machine import FAKE_AERI, NAME, AeriMachine
from conftest import scaled

from levi.marvel0 import aeri_run
from levi.marvel0 import aeri_state as st
from levi.marvel0.aeri_state import Refuse

pytestmark = pytest.mark.skipif(
    not os.path.exists("/dev/ptmx"), reason="needs a pseudo-terminal"
)
FAST = (("SETTLE_S", 0.15), ("ARM_DELAY_S", 0.15), ("KEY_GAP_S", 0.3))


@pytest.fixture(name="live")
def live_machine(tmp_path, monkeypatch):
    for key, value in FAST:
        monkeypatch.setattr(st, key, value)
    m = AeriMachine(tmp_path, monkeypatch, live=True)
    m.prepare(rounds=2, steps=60)
    yield m
    m.close()
    for child in m.children:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except OSError:
            pass


def arm(m) -> list:
    path = m.rollout / "fake_arm.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def steady(m, seconds=0.6) -> bool:
    before = len(arm(m))
    time.sleep(seconds)
    return len(arm(m)) == before


def journal(m) -> list:
    path = m.run_dir / "state_journal.jsonl"
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def notes(m, code) -> list:
    return [
        r["note"]["detail"]
        for r in journal(m)
        if r.get("note") and r["note"]["code"] == code
    ]


def exit_code(m) -> dict | None:
    return m.look()["d"]["exit"]


def to_forward(m, steps=400):
    m.prepare(rounds=2, steps=steps)
    m.start()
    m.wait_state("await_phrase")
    m.press("execute")
    m.wait_state("await_inventory", timeout=30)
    m.press("inventory_yes")
    m.wait_state("await_release")
    m.press("release_round")
    return m.wait_state("forward_running")


# --- the whole way through ----------------------------------------------------------------------


def test_a_run_from_the_phrase_to_q_with_every_key_a_person_presses(live):
    m = live
    m.prepare(rounds=2, steps=200)
    m.start()
    view = m.wait_state("await_phrase")
    assert view["keys"][0]["phrase"] == f"execute {NAME}"
    m.press("execute")
    m.wait_state("await_inventory", timeout=30)
    assert m.look()["inventory"]["items"][1]["id"] == "red_pepper_1"
    m.press("inventory_yes")
    m.wait_state("await_release")
    m.press("release_round")
    view = m.wait_state("forward_running")
    assert view["progress"]["round"] == 1 and view["progress"]["rounds"] == 2
    m.press("verdict_success")
    view = m.wait_state("await_release", timeout=40, **{"state.prompt.n": 2})
    assert view["progress"]["done"] == 1
    m.press("quit")
    view = m.wait_state("ended", timeout=30)
    assert view["state"]["reason"] == "exit_0" and view["d"]["exit"] == {
        "code": 0,
        "signal": None,
    }
    keys = [r["key"] for r in m.history() if r["event"] == "key"]
    assert keys == [
        "execute",
        "inventory_yes",
        "release_round",
        "verdict_success",
        "quit",
    ]
    assert notes(m, "subtask_inventory_confirmed") and notes(m, "operator_verdict")
    assert json.loads(notes(m, "operator_verdict")[0])["label"] == "success"
    kinds = [row["kind"] for row in arm(m)]
    assert kinds.count("step") > 0 and "hold" not in kinds
    assert "terminal D exited rc=0" in (m.runs / NAME / "terminal_d.events").read_text()
    exit_file = json.loads((m.folder / "logs" / m.record()["exit_file"]).read_text())
    assert exit_file["code"] == 0 and exit_file["signal"] is None
    assert m.sup.active() is None and view["after_exit"]["swap_clean"] is True


def test_the_screen_file_is_private_and_holds_the_whole_screen(live):
    m = live
    m.start()
    m.wait_state("await_phrase")
    text = m.typescript.read_text()
    assert f"请输入 execute {NAME}：" in text and "这将连接机器人" in text
    assert oct(os.stat(m.typescript).st_mode & 0o777) == "0o600"
    m.press("execute")
    m.wait_state("await_inventory", timeout=30)
    assert "1/7 预检" in m.typescript.read_text()


def test_with_the_startup_home_the_three_keys_come_one_after_the_other(live):
    m = live
    m.prepare(rounds=2, steps=60, start_home=True, arm_at_home=False)
    m.start()
    m.wait_state("await_phrase")
    m.press("execute")
    view = m.wait_state("await_home_o", timeout=30)
    assert [k["id"] for k in view["keys"]] == ["home_o", "quit"]
    assert not [r for r in arm(m) if r["kind"] == "home"], (
        "no motion before the three keys"
    )
    m.press("home_o")
    m.wait_state("await_home_p")
    assert not [r for r in arm(m) if r["kind"] == "home"]
    m.press("home_p")
    m.wait_state("await_home_h")
    assert not [r for r in arm(m) if r["kind"] == "home"], "p does not move the arm"
    m.press("home_h")
    m.wait_state("await_inventory", timeout=30)
    assert [r for r in arm(m) if r["kind"] == "home"], "h sent the arm home"
    started = [r["reason"] for r in journal(m) if r["record"] == "prepared"]
    assert started[0] == "startup_home_requested"


def test_the_object_list_rejected_makes_the_run_wait_for_a_person(live):
    m = live
    m.start()
    m.wait_state("await_phrase")
    m.press("execute")
    m.wait_state("await_inventory", timeout=30)
    m.press("inventory_no")
    view = m.wait_state("await_resume", timeout=30)
    assert view["wait"]["reason"] == "subtask_inventory_rejected"
    assert (
        view["keys"][0]["id"] == "resume_ask" and view["keys"][0]["variant"] == "direct"
    )
    m.press("quit")
    assert m.wait_state("ended", timeout=30)["d"]["exit"]["code"] == 0


def test_a_wait_that_asks_twice_is_resumed_with_enter_then_y(live):
    m = live
    m.prepare(rounds=2, steps=30, wait_at={"round": 1, "reason": "operator_stop"})
    m.start()
    m.wait_state("await_phrase")
    m.press("execute")
    m.wait_state("await_inventory", timeout=30)
    m.press("inventory_yes")
    m.wait_state("await_release")
    m.press("release_round")
    view = m.wait_state("await_resume", timeout=40)
    assert view["wait"]["confirm"] is True
    ask = next(k for k in view["keys"] if k["id"] == "resume_ask")
    assert (ask["level"], ask["motion"]) == ("A", "none")
    m.press("resume_ask")
    view = m.wait_state("await_resume_confirm")
    assert {k["id"] for k in view["keys"] if k["allowed"]} <= {
        "resume_confirm",
        "resume_cancel",
        "quit",
    }
    m.press("resume_confirm")
    view = m.wait_state("await_release", timeout=30, **{"state.prompt.n": 2})
    assert any(r.get("reason") == "human_resumed" for r in journal(m))
    assert view["keys"][0]["id"] == "release_round"


def test_the_second_step_can_be_cancelled_and_the_wait_comes_back(live):
    m = live
    m.prepare(rounds=2, steps=30, wait_at={"round": 1, "reason": "operator_stop"})
    m.start()
    m.wait_state("await_phrase")
    m.press("execute")
    m.wait_state("await_inventory", timeout=30)
    m.press("inventory_yes")
    m.wait_state("await_release")
    m.press("release_round")
    m.wait_state("await_resume", timeout=40)
    first = m.look()["state"]["prompt"]["id"]
    m.press("resume_ask")
    m.wait_state("await_resume_confirm")
    m.press("resume_cancel")
    view = m.wait_state("await_resume", timeout=20)
    assert view["state"]["prompt"]["id"] != first, (
        "the wait was printed again: a new prompt"
    )
    view = m.wait_key("resume_ask")
    assert [k["id"] for k in view["keys"] if k["allowed"]][:1] == ["resume_ask"]
    m.press("quit")
    m.wait_state("ended", timeout=30)


# --- other programs' lines, and words that changed --------------------------------------------------------


def test_lines_of_other_programs_between_the_prompts_do_not_close_them(live):
    m = live
    m.prepare(
        rounds=2,
        steps=60,
        foreign=[
            "Homing over 30 steps -> [ 0.48 -0.01 ]  (rotating 11.6 deg)",
            "Home reached.",
            "  server config: pi05_fr3_all_state  (rebase dim=3)",
            "warning: a library said something",
        ],
    )
    m.start()
    m.wait_state("await_phrase")
    m.press("execute")
    view = m.wait_key("inventory_yes")
    assert view["state"]["settled"]
    m.press("inventory_yes")
    view = m.wait_state("await_release")
    assert view["keys"][0]["id"] == "release_round"
    assert "warning: a library said something" in " ".join(view["screen"]["tail"])
    m.press("quit")
    m.wait_state("ended", timeout=30)


def test_a_reworded_prompt_is_drift_and_only_stop_is_left(live, monkeypatch):
    monkeypatch.setattr(st, "DRIFT_S", 0.8)
    m = live
    m.prepare(
        rounds=2, steps=60, reword={"sub_release": "第 {n}/{total} 轮：请按回车放行"}
    )
    m.start()
    m.wait_state("await_phrase")
    m.press("execute")
    m.wait_state("await_inventory", timeout=30)
    m.press("inventory_yes")
    view = m.wait_state("unknown", timeout=20)
    assert view["state"]["reason"] == "screen_drift"
    assert view["keys"] == [] and view["stop"]["allowed"] is True
    assert any("请按回车放行" in line for line in view["screen"]["tail"])
    m.stop()
    view = m.wait_state("ended", timeout=40)
    assert view["d"]["exit"]["code"] in (0, 130)
    assert not [r for r in arm(m) if r["kind"] == "step"], (
        "a drifted page started no round"
    )


# --- stop ---------------------------------------------------------------------------------------------------


def test_stop_is_a_real_sigint_the_arm_is_held_and_d_wraps_up(live):
    m = live
    to_forward(m, steps=2000)
    time.sleep(0.3)
    answer = m.stop()
    assert answer["via"] == "pty"
    view = m.wait_state("ended", timeout=40)
    assert view["d"]["exit"] == {"code": 130, "signal": None}
    kinds = [row["kind"] for row in arm(m)]
    assert "hold" in kinds, "D held the arm"
    after = len(arm(m))
    assert steady(m, 0.6) and len(arm(m)) == after
    text = m.typescript.read_text()
    assert "Ctrl+C" in text and "7/7" in text, (
        "D said it was stopping and shut its services down"
    )
    assert notes(m, "operator_interrupt"), "D noted who stopped it"
    assert "svc_not_stopped" not in text and "没有停止" not in text
    assert ["stop"] == [r["key"] for r in m.history() if r["event"] == "stop"]
    assert not [r for r in m.history() if r["event"] == "end"]


def test_a_second_ctrl_c_during_the_wrap_up_would_leave_the_units_running(live):
    """Why the page sends one Stop per run: the shutdown's ``systemctl`` calls
    are in the same foreground group and a second Ctrl+C kills them."""
    m = live
    m.prepare(rounds=2, steps=2000, shutdown_s=3.0)
    m.start()
    m.wait_state("await_phrase")
    m.press("execute")
    m.wait_state("await_inventory", timeout=30)
    m.press("inventory_yes")
    m.wait_state("await_release")
    m.press("release_round")
    m.wait_state("forward_running")
    m.stop()
    deadline = time.monotonic() + scaled(15)
    while "7/7" not in m.typescript.read_text() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert refusal(m.sup.stop, {"request_id": m.rid()})[1] == "stop_spent", (
        "the page refuses it"
    )
    os.write(m.sup._master, b"\x03")  # what a person at a terminal could do
    view = m.wait_state("ended", timeout=30)
    assert view["d"]["exit"]["code"] == 1
    assert "没有停止" in m.typescript.read_text()


def refusal(call, *args, **kw):
    with pytest.raises(Refuse) as caught:
        call(*args, **kw)
    exc = caught.value
    return exc.status, exc.code, exc.extra


def test_a_stop_at_the_phrase_cancels_before_anything_is_connected(live):
    m = live
    m.start()
    m.wait_state("await_phrase")
    answer = m.stop()
    assert answer["via"] == "pty"
    view = m.wait_state("ended", timeout=20)
    assert view["d"]["exit"]["code"] == 130
    assert (
        "已在连接机器人之前取消" in m.typescript.read_text()
        or "cancelled" in m.typescript.read_text()
    )
    assert (
        arm(m) == []
        and not (m.rollout / ".aeri" / "runs" / NAME / "state_journal.jsonl").exists()
    )


def test_a_d_that_ignores_the_stop_is_stalled_and_end_kills_its_groups(
    live, monkeypatch
):
    monkeypatch.setattr(st, "STALL_S", 1.0)
    m = live
    m.prepare(rounds=2, steps=4000, hang_after_stop=True)
    m.start()
    m.wait_state("await_phrase")
    m.press("execute")
    m.wait_state("await_inventory", timeout=30)
    m.press("inventory_yes")
    m.wait_state("await_release")
    m.press("release_round")
    m.wait_state("forward_running")
    m.stop()
    view = m.wait_state("stalled", timeout=20)
    assert view["end"]["allowed"] is True and view["keys"] == []
    # the arm still "moves": D ignored the stop. End kills it.
    answer = m.end()
    assert answer["accepted"] is True
    view = m.wait_state("ended", timeout=20)
    assert view["d"]["exit"]["signal"] == 9
    assert steady(m, 0.6), "no motion after the kill"


def test_a_heartbeat_that_stopped_makes_the_state_unknown_but_stop_stays(
    live, monkeypatch
):
    monkeypatch.setattr(st, "RUNNER_STALE_S", 0.6)
    m = live
    m.prepare(rounds=2, steps=4000, heartbeat_dead_after_s=2.0)
    m.start()
    m.wait_state("await_phrase")
    m.press("execute")
    m.wait_state("await_inventory", timeout=30)
    view = m.wait_state("unknown", timeout=20)
    assert view["state"]["reason"] == "runner_stale" and view["keys"] == []
    assert view["stop"]["allowed"] is True
    m.stop()
    m.wait_state("ended", timeout=40)


def test_a_launch_that_d_refuses_ends_with_its_code(live):
    m = live
    m.prepare(rounds=2, steps=30, refuse_start_pose=True)
    m.start()
    m.wait_state("await_phrase")
    m.press("execute")
    view = m.wait_state("ended", timeout=30)
    assert view["state"]["reason"] == "exit_2" and view["d"]["exit"]["code"] == 2
    assert "start pose refused" in m.typescript.read_text()


def test_a_service_that_fails_ends_the_run_with_one(live):
    m = live
    m.prepare(rounds=2, steps=30, service_fail={"step": 2, "code": "vlm_not_ready"})
    m.start()
    m.wait_state("await_phrase")
    m.press("execute")
    view = m.wait_state("ended", timeout=30)
    assert view["d"]["exit"]["code"] == 1
    assert "vlm_not_ready" in " ".join(view["screen"]["tail"])


def test_a_run_whose_swap_log_is_unfinished_is_typed_with_the_variant_phrase(live):
    m = live
    m.prepare(rounds=2, steps=30, swap_unclean=True)
    m.start()
    m.wait_state("await_phrase_variant")
    view = m.wait_key("execute_variant")
    assert [k["id"] for k in view["keys"] if k["allowed"]] == ["execute_variant"]
    m.press("execute_variant")
    m.wait_state("await_inventory", timeout=30)
    m.press("quit")
    m.wait_state("ended", timeout=30)


# --- a restart ----------------------------------------------------------------------------------------------


def test_a_run_found_again_after_a_restart_is_stopped_once_and_ends(live):
    m = live
    to_forward(m, steps=4000)
    time.sleep(0.3)
    new = aeri_run.Supervisor(m.host, site_loader=lambda: m.site, workspace=m.workspace)
    view = new.view()
    assert view["state"]["id"] in ("orphaned", "stopping")
    assert view["d"]["keyboard"] is False and view["keys"] == []
    assert new._rec["stop_sent_epoch"] is not None
    deadline = time.monotonic() + scaled(40)
    while time.monotonic() < deadline:
        view = new.view()
        new._cache = None
        new.tick()
        if view["state"]["id"] == "ended":
            break
        time.sleep(0.1)
    assert view["state"]["id"] == "ended" and view["d"]["exit"]["code"] == 130
    assert "7/7" in m.typescript.read_text() and notes(m, "operator_interrupt")
    # exactly one Ctrl+C reached D: the shutdown was not broken
    assert "没有停止" not in m.typescript.read_text()
    m.sup._master = None  # (the old supervisor's keyboard is of no use any more)


# --- the keyboard goes away ----------------------------------------------------------------------------------


def launcher(tmp_path, m, **knobs):
    """The launcher and the fake D under a pty whose master this test holds
    (closing it is the core dying)."""
    rollout = m.rollout
    config = m.runs / NAME / "job.yaml"
    m.prepare(rounds=2, steps=4000, **knobs)
    out = tmp_path / "hang-screen.log"
    spec = tmp_path / "hang-spec.json"
    spec.write_text(
        json.dumps(
            {
                "mode": "aeri",
                "argv": [
                    sys.executable,
                    str(FAKE_AERI),
                    "execute",
                    "--config",
                    str(config),
                    "--binding",
                    str(m.runs / NAME / "binding.json"),
                    "--expect-plan",
                    "a" * 64,
                    "--expect-binding",
                    "b" * 64,
                ],
                "cwd": str(tmp_path),
                "exit_file": str(tmp_path / "hang-exit.json"),
                "out_log": str(out),
                "events_file": str(tmp_path / "hang-events"),
                "hangup_grace_s": 1.0,
            }
        )
    )
    master, slave = os.openpty()
    log = (tmp_path / "hang-launcher.log").open("w")
    proc = subprocess.Popen(
        [sys.executable, "-I", m.host.hostchild, str(spec)],
        stdin=slave,
        stdout=log,
        stderr=log,
        start_new_session=True,
        close_fds=True,
        env={"LEVI_AERI_CONSOLE_LANG": "zh", "PATH": os.environ["PATH"]},
    )
    os.close(slave)
    deadline = time.monotonic() + scaled(20)
    while (
        "请输入" not in (out.read_text() if out.exists() else "")
        and time.monotonic() < deadline
    ):
        time.sleep(0.05)
    os.write(master, f"execute {NAME}\r".encode())
    while "子任务进行中" not in (out.read_text() if out.exists() else ""):
        if time.monotonic() > deadline:
            break
        # (inventory and release need their keys)
        text = out.read_text()
        if "清单对就按 y" in text and "清单已确认" not in text:
            time.sleep(0.4)
            os.write(master, b"y")
        elif "确认场景就绪" in text and "已放行" not in text:
            time.sleep(0.4)
            os.write(master, b"\r")
        time.sleep(0.1)
    return proc, master, out, rollout, log


def test_a_launcher_whose_core_died_sends_sigint_first_and_d_wraps_up(live, tmp_path):
    m = live
    proc, master, out, _rollout, log = launcher(tmp_path, m)
    try:
        assert "子任务进行中" in out.read_text()
        os.close(master)  # the core is gone
        t0 = time.monotonic()
        assert proc.wait(scaled(20)) == 130, (
            "D ended on SIGINT within the grace and was not killed"
        )
        assert time.monotonic() - t0 < 5
        text = out.read_text()
        assert "Ctrl+C" in text and "7/7" in text
        record = json.loads((tmp_path / "hang-exit.json").read_text())
        assert record["code"] == 130
        assert "terminal D exited rc=130" in (tmp_path / "hang-events").read_text()
        after = len(arm(m))
        time.sleep(0.5)
        assert len(arm(m)) == after
    finally:
        log.close()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass


def test_a_launcher_whose_core_died_kills_a_d_that_does_not_answer_after_the_grace(
    live, tmp_path
):
    m = live
    proc, master, out, _rollout, log = launcher(tmp_path, m, ignore_sigint=True)
    try:
        assert "子任务进行中" in out.read_text()
        os.close(master)
        t0 = time.monotonic()
        assert proc.wait(scaled(20)) == -signal.SIGKILL, (
            "the launcher dies with its group"
        )
        assert not (tmp_path / "hang-exit.json").exists(), (
            "a killed launcher records nothing"
        )
        assert 0.8 <= time.monotonic() - t0 < 6, "the grace was waited for"
        after = len(arm(m))
        time.sleep(0.5)
        assert len(arm(m)) == after, "no motion once it was killed"
    finally:
        log.close()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
