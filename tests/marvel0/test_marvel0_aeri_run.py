"""``levi/marvel0/aeri_run.py``: the hosted terminal D, its start, its keys, its
Stop, its end and its adoption after a restart, on the scripted machine
(``aeri_machine.py``): a temporary site, a prepared run, a launcher that is a
fake child holding the client's end of the keyboard pty, and **terminal D played
from the real recording of 18 Oct** (r08's journal, screen and heartbeat). The
test reads **every byte the page ever writes** to the keyboard. No process is
started and nothing is signalled (``kill`` and ``killpg`` are recorded).

The tests with real processes (the launcher under a pty and the fake terminal D)
are in ``test_marvel0_aeri_run_live.py``."""

import json
import os
import signal
import stat
import threading
import time
from pathlib import Path

import pytest
from aeri_machine import NAME, AeriMachine, cut

from levi.marvel0 import aeri_run
from levi.marvel0 import aeri_state as st
from levi.marvel0.aeri_state import Refuse

FAST = (("SETTLE_S", 0.15), ("ARM_DELAY_S", 0.15), ("KEY_GAP_S", 0.3))
PHRASE = f"请输入 execute {NAME}：".encode()


@pytest.fixture(name="m")
def machine(tmp_path, monkeypatch):
    for key, value in FAST:
        monkeypatch.setattr(st, key, value)
    mm = AeriMachine(tmp_path, monkeypatch)
    mm.prepare(rounds=5, steps=200)
    yield mm
    mm.close()


@pytest.fixture
def slow(tmp_path, monkeypatch):
    """The production delays (settle 1 s, arm delay 1.5 s, key gap 3 s)."""
    mm = AeriMachine(tmp_path, monkeypatch)
    mm.prepare(rounds=5, steps=200)
    yield mm
    mm.close()


def refusal(call, *args, **kw):
    """The ``Refuse`` a call raises, as ``(status, code, extra)``."""
    with pytest.raises(Refuse) as caught:
        call(*args, **kw)
    exc = caught.value
    return exc.status, exc.code, exc.extra


def at(m, journal=None, screen=None, cbreak=True, **kw):
    """Play terminal D up to a moment of the real run and wait for the keys of
    that prompt to be open."""
    m.script(journal=journal, screen=screen, cbreak=cbreak, **kw)


def open_keys(m, action=None):
    view = m.wait_key(action) if action else m.look()
    return view, {k["id"]: k for k in view["keys"]}


def started(m):
    m.start()
    return m


def at_phrase(m):
    started(m)
    m.script(screen=PHRASE, cbreak=False, runner=False)
    return m.wait_key("execute")


def at_inventory(m):
    started(m)
    m.script(journal=25, screen="清单对就按 y 确认", cbreak=True, steps=9)
    return m.wait_key("inventory_yes")


def at_release(m):
    started(m)
    m.script(journal=26, screen="第 1/5 轮：确认场景就绪", cbreak=True, steps=9)
    return m.wait_key("release_round")


def at_forward(m):
    started(m)
    m.script(journal=34, screen="当前正向子任务 步 66/200", cbreak=True, steps=9)
    return m.wait_key("verdict_success")


# --- the start -----------------------------------------------------------------------------


def test_a_start_hosts_the_very_command_a_terminal_would_run(m):
    m.host.client_argv = lambda argv: argv  # (the real argv, never run here)
    answer = m.start()
    assert answer["accepted"] is True and answer["state"] == "launching"
    spawned = m.spawned[0]
    spec_path = spawned["argv"][-1]
    spec = json.loads(Path(spec_path).read_text())
    printed = m.execute_text().split(" && ", 1)[1]
    assert " ".join(spec["argv"]) == printed
    assert spec["argv"][:4] == ["env", "-u", "VIRTUAL_ENV", f"PYTHONPATH={m.worktree}"]
    assert spec["argv"][4:9] == [
        "uv",
        "run",
        "--no-sync",
        "--with",
        "pyrealsense2==2.55.1.6486",
    ]
    assert spec["mode"] == "aeri" and spec["hangup_grace_s"] == 3.0
    assert spec["cwd"] == m.aeri.openpi_root
    assert spec["out_log"] == str(m.typescript)
    assert spec["events_file"].endswith("terminal_d.events")
    events = (m.runs / NAME / "terminal_d.events").read_text()
    assert "start: env -u VIRTUAL_ENV" in events


def test_terminal_d_gets_an_allowlisted_environment(m):
    m.start()
    env = m.spawned[0]["env"]
    assert env == aeri_run.child_env(m.environ, m.aeri)
    for name in ("LEVI_UI_TOKEN", "HF_TOKEN", "ROS_DISTRO", "CONDA_PREFIX", "DISPLAY"):
        assert name not in env
    assert env["LEVI_AERI_CONSOLE_LANG"] == "zh" and env["UV_OFFLINE"] == "1"
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert env["DBUS_SESSION_BUS_ADDRESS"].startswith("unix:path=")
    assert env["TERM"] == "dumb" and env["PYTHONUNBUFFERED"] == "1"
    assert not any(k.startswith("LEVI_") and k != "LEVI_AERI_CONSOLE_LANG" for k in env)
    assert "LEVI_AERI_CONSOLE" not in env, "the console must stay on"


def test_the_console_language_and_uv_offline_follow_the_site_file(m):
    from dataclasses import replace

    m.site = replace(m.site, aeri=replace(m.aeri, console_lang="en", uv_offline=False))
    m.start()
    env = m.spawned[0]["env"]
    assert env["LEVI_AERI_CONSOLE_LANG"] == "en" and "UV_OFFLINE" not in env


def test_the_record_and_the_files_are_private(m):
    m.start()
    rec = m.record()
    assert rec["phase"] == "started" and rec["name"] == NAME
    assert rec["leader"]["pid"] == m.child.pid and rec["profile"] == "test-profile"
    assert stat.S_IMODE(os.stat(m.folder).st_mode) == 0o700
    for sub in m.folder.iterdir():
        if sub.is_file():
            assert stat.S_IMODE(os.stat(sub).st_mode) == 0o600, sub
    logs = list((m.folder / "logs").iterdir())
    assert any(p.name.endswith(".spec.json") for p in logs)
    assert all(stat.S_IMODE(os.stat(p).st_mode) == 0o600 for p in logs)


def test_the_page_never_holds_the_arm_lock(m):
    """D takes the arm lock itself after the phrase; a page that held it would
    make D refuse to start (``already owned``)."""
    m.start()
    assert not (m.tmp / "arm.lock").exists()
    assert m.sup._master is not None and os.get_inheritable(m.sup._master) is False


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"enabled": False}, "aeri_disabled"),
        ({"profile_cgroup": "0::/user.slice/other.service\n"}, "not_product"),
        ({"e_running": True}, "readiness_failed"),
        ({"git_head": "0123456789" * 4}, "code_pin_mismatch"),
        ({"git_dirty": " M levi/automatic/console.py\n"}, "code_pin_mismatch"),
    ],
)
def test_a_start_that_must_be_refused(m, change, code):
    for key, value in change.items():
        setattr(m, key, value)
    m.present()
    assert refusal(m.sup.challenge, {"purpose": "start", "name": NAME})[1] == code
    assert m.spawned == []


def test_unknown_readiness_refuses_as_firmly_as_a_failed_row(m):
    """``refuse`` and ``unknown`` refuse; so does a ``warn`` of a row that is not
    on the card's list of warning rows. (The first build refused *every* row that
    was not ``ok``, which turned the always-``warn`` ``stack_ready`` row of a
    machine that is ready into a refusal at every start: the real rows are in
    ``test_marvel0_aeri_run_gate.py``.)"""
    for level in ("refuse", "unknown", "warn"):
        m.snap = {
            "rows": [{"code": "gpu_free", "level": level, "params": {}}],
            "ready": False,
            "sha256": "x",
        }
        _status, code, extra = refusal(
            m.sup.challenge, {"purpose": "start", "name": NAME}
        )
        assert code == "readiness_failed" and extra["first"] == "gpu_free"
    m.snap = {
        "rows": [
            {"code": "gpu_free", "level": "ok", "params": {}},
            {"code": "stack_ready", "level": "warn", "params": {}},
        ],
        "ready": True,
        "sha256": "x",
    }
    m.present()
    assert m.sup.challenge({"purpose": "start", "name": NAME})["name"] == NAME


def test_a_snapshot_that_says_ready_but_has_a_row_that_is_not_ok_is_refused(m):
    m.snap = {
        "rows": [{"code": "x", "level": "unknown", "params": {}}],
        "ready": True,
        "sha256": "x",
    }
    assert (
        refusal(m.sup.challenge, {"purpose": "start", "name": NAME})[1]
        == "readiness_failed"
    )


def test_an_unreadable_e_probe_is_not_a_free_arm(m):
    def broken():
        raise RuntimeError("cannot tell")

    m.host.e_active = broken
    assert (
        refusal(m.sup.challenge, {"purpose": "start", "name": NAME})[1]
        == "readiness_failed"
    )


def test_a_site_without_the_aeri_section_is_disabled(m):
    from dataclasses import replace

    m.site = replace(m.site, aeri=None)
    assert (
        refusal(m.sup.challenge, {"purpose": "start", "name": NAME})[1]
        == "aeri_disabled"
    )
    view = m.doc()
    assert view["enabled"] is False and view["start"]["allowed"] is False
    assert view["start"]["why_not"]["code"] == "aeri_disabled"


def test_a_run_that_was_not_prepared_is_refused(m):
    assert (
        refusal(m.sup.challenge, {"purpose": "start", "name": "aeri-20261021-ext-r09"})[
            1
        ]
        == "not_prepared"
    )
    assert (
        refusal(m.sup.challenge, {"purpose": "start", "name": "../etc"})[1]
        == "not_prepared"
    )
    assert (
        refusal(m.sup.challenge, {"purpose": "start", "name": 5})[1] == "not_prepared"
    )


@pytest.mark.parametrize("victim", ["job.yaml", "binding.json"])
def test_a_file_changed_after_it_was_prepared_is_refused(m, victim):
    path = m.runs / NAME / victim
    path.write_text(path.read_text() + "\n#changed")
    assert (
        refusal(m.sup.challenge, {"purpose": "start", "name": NAME})[1]
        == "prepared_changed"
    )


@pytest.mark.parametrize(
    "field", ["plan_sha256", "binding_sha256", "execute", "confirm", "mode"]
)
def test_a_run_record_that_no_longer_reads_the_same_is_refused(m, field):
    path = m.runs / NAME / "RUN.json"
    data = json.loads(path.read_text())
    data[field] = "0" * 64 if "sha" in field else "changed"
    path.write_text(json.dumps(data))
    assert (
        refusal(m.sup.challenge, {"purpose": "start", "name": NAME})[1]
        == "prepared_changed"
    )


def test_the_printed_command_must_equal_the_hosted_one_byte_for_byte(m):
    path = m.runs / NAME / "RUN.json"
    data = json.loads(path.read_text())
    data["execute"] = data["execute"].replace("--no-sync", "--no-sync --offline")
    path.write_text(json.dumps(data))
    _status, code, extra = refusal(m.sup.challenge, {"purpose": "start", "name": NAME})
    assert code == "prepared_changed" and extra["what"] == "execute"
    data["execute"] = m.execute_text().replace(str(m.aeri.openpi_root), "/elsewhere")
    path.write_text(json.dumps(data))
    assert (
        refusal(m.sup.challenge, {"purpose": "start", "name": NAME})[1]
        == "prepared_changed"
    )


def test_the_pin_is_a_prefix_of_the_head_and_the_tree_is_clean(m):
    m.git_head = "abcdef12" + "0" * 32
    m.present()
    assert m.sup.challenge({"purpose": "start", "name": NAME})["name"] == NAME
    m.git_head = "abcdef13" + "0" * 32
    assert (
        refusal(m.sup.challenge, {"purpose": "start", "name": NAME})[1]
        == "code_pin_mismatch"
    )


def test_a_git_that_cannot_answer_does_not_pass_the_pin(m):
    m.host.git = lambda *a, **k: None
    assert (
        refusal(m.sup.challenge, {"purpose": "start", "name": NAME})[1]
        == "code_pin_mismatch"
    )


def test_the_profile_is_checked_against_the_real_bytes_of_the_worktree(m):
    (m.worktree / "levi/automatic/console.py").write_bytes(
        b"# AERI changed its console\n"
    )
    _status, code, extra = refusal(m.sup.challenge, {"purpose": "start", "name": NAME})
    assert (
        code == "profile_unknown"
        and extra["detail"] == "differs:levi/automatic/console.py"
    )
    assert m.spawned == []


def test_an_unknown_profile_in_the_site_file_is_refused(m):
    from dataclasses import replace

    m.site = replace(m.site, aeri=replace(m.aeri, profile="somebody-elses"))
    assert (
        refusal(m.sup.challenge, {"purpose": "start", "name": NAME})[1]
        == "profile_unknown"
    )


def _worktree(name):
    for parent in Path(__file__).resolve().parents:
        folder = parent / "lab" / "worktrees" / name
        if folder.is_dir():
            return folder
    return None


@pytest.mark.parametrize(
    ("profile", "worktree"),
    [("harvest-5bc750ca", "harvest-validation-fix"), ("bfdee2cf", "aeri-subtask-loop")],
)
def test_the_real_worktrees_match_their_profiles_when_they_are_there(profile, worktree):
    """The real function on the real files (the AERI worktrees of this machine):
    the profile table is only worth what it says about them."""
    folder = _worktree(worktree)
    if folder is None:
        pytest.skip(f"no {worktree} worktree on this machine")
    assert st.profile_matches(folder, profile) == (True, "ok")
    other = {"harvest-5bc750ca": "bfdee2cf", "bfdee2cf": "harvest-5bc750ca"}[profile]
    ok, detail = st.profile_matches(folder, other)
    assert ok is False and detail.startswith("differs:")


def test_a_name_that_is_taken_or_driven_elsewhere_is_refused(m, tmp_path):
    home = Path(m.environ["HOME"]) / ".levi-aeri" / "runs"
    home.mkdir(parents=True)
    (home / f"{NAME}.json").write_text("{}")
    assert (
        refusal(m.sup.challenge, {"purpose": "start", "name": NAME})[1]
        == "run_name_taken"
    )
    (home / f"{NAME}.json").unlink()
    (m.rollout / ".aeri" / "runs" / NAME).mkdir(parents=True)
    assert (
        refusal(m.sup.challenge, {"purpose": "start", "name": NAME})[1]
        == "run_name_taken"
    )
    (m.rollout / ".aeri" / "runs" / NAME).rmdir()
    os.mkfifo(m.runs / NAME / "terminal_d.in")
    assert (
        refusal(m.sup.challenge, {"purpose": "start", "name": NAME})[1]
        == "driven_elsewhere"
    )
    os.unlink(m.runs / NAME / "terminal_d.in")
    (m.runs / NAME / "terminal_d.events").write_text("[10:00:00] start: x\n")
    assert (
        refusal(m.sup.challenge, {"purpose": "start", "name": NAME})[1]
        == "driven_elsewhere"
    )
    (m.runs / NAME / "terminal_d.events").write_text(
        "[10:00:00] start: x\n[10:05:00] terminal D exited rc=0\n"
    )
    m.present()
    assert m.sup.challenge({"purpose": "start", "name": NAME})["name"] == NAME


def test_the_start_needs_the_phrase_the_checklist_and_a_token(m):
    m.present()
    c = m.sup.challenge({"purpose": "start", "name": NAME})
    body = {
        "request_id": m.rid(),
        "name": NAME,
        "challenge": c["challenge"],
        "confirm": f"start aeri {NAME}",
        "attest": {k: True for k in st.ATTEST},
    }
    assert (
        refusal(m.sup.start, {**body, "confirm": "start aeri other"})[1]
        == "confirm_required"
    )
    for item in st.ATTEST:
        attest = {k: k != item for k in st.ATTEST}
        assert (
            refusal(m.sup.start, {**body, "request_id": m.rid(), "attest": attest})[1]
            == "attest_required"
        )
    assert (
        refusal(m.sup.start, {**body, "request_id": "short"})[1] == "request_id_invalid"
    )
    assert (
        refusal(m.sup.start, {**body, "request_id": m.rid(), "challenge": "nope"})[1]
        == "challenge_invalid"
    )
    assert m.spawned == []
    assert m.sup.start(body)["accepted"] is True


def test_a_token_is_used_once(m):
    m.present()
    c = m.sup.challenge({"purpose": "start", "name": NAME})
    body = {
        "request_id": m.rid(),
        "name": NAME,
        "challenge": c["challenge"],
        "confirm": f"start aeri {NAME}",
        "attest": {k: True for k in st.ATTEST},
    }
    m.sup.start(body)
    m.child.exits(0)
    m.sup.tick()  # the first run is over
    again = {**body, "request_id": m.rid()}
    assert refusal(m.sup.start, again)[1] == "challenge_used"


def test_a_token_is_stale_when_the_readiness_changed(m):
    m.present()
    c = m.sup.challenge({"purpose": "start", "name": NAME})
    m.snap = {"rows": [], "ready": True, "sha256": "snap-0002"}
    body = {
        "request_id": m.rid(),
        "name": NAME,
        "challenge": c["challenge"],
        "confirm": f"start aeri {NAME}",
        "attest": {k: True for k in st.ATTEST},
    }
    assert refusal(m.sup.start, body)[1] == "challenge_stale"
    assert m.spawned == []


def test_a_token_for_another_run_is_stale(m):
    m.prepare(name="aeri-20261021-ext-r02", rounds=5)
    m.present()
    c = m.sup.challenge({"purpose": "start", "name": NAME})
    body = {
        "request_id": m.rid(),
        "name": "aeri-20261021-ext-r02",
        "challenge": c["challenge"],
        "confirm": "start aeri aeri-20261021-ext-r02",
        "attest": {k: True for k in st.ATTEST},
    }
    assert refusal(m.sup.start, body)[1] == "challenge_stale"


def test_an_expired_token_is_refused(m, monkeypatch):
    m.present()
    c = m.sup.challenge({"purpose": "start", "name": NAME})
    clock = [time.monotonic()]
    monkeypatch.setattr(
        m.sup.challenges, "_clock", lambda: clock[0] + st.CHALLENGE_TTL_S + 1
    )
    body = {
        "request_id": m.rid(),
        "name": NAME,
        "challenge": c["challenge"],
        "confirm": f"start aeri {NAME}",
        "attest": {k: True for k in st.ATTEST},
    }
    assert refusal(m.sup.start, body)[1] == "challenge_expired"


def test_a_second_start_while_d_lives_is_refused_and_a_repeated_request_is_answered_again(
    m,
):
    m.present()
    c = m.sup.challenge({"purpose": "start", "name": NAME})
    body = {
        "request_id": m.rid(),
        "name": NAME,
        "challenge": c["challenge"],
        "confirm": f"start aeri {NAME}",
        "attest": {k: True for k in st.ATTEST},
    }
    first = m.sup.start(body)
    again = m.sup.start(body)  # the same request: the stored answer
    assert again["replayed"] is True and again["run_id"] == first["run_id"]
    assert len(m.spawned) == 1
    other = {
        **body,
        "name": "aeri-20261021-ext-r02",
        "confirm": "start aeri aeri-20261021-ext-r02",
    }
    assert refusal(m.sup.start, other)[1] == "request_id_used"
    assert refusal(m.sup.challenge, {"purpose": "start", "name": NAME})[1] == "d_active"
    assert m.sup.active()["kind"] == "marvel0-aeri"


def test_a_launch_that_fails_is_recorded_and_the_next_start_is_possible(m):
    def broken(*a, **k):
        raise OSError("no systemd")

    m.host.spawn = broken
    assert refusal(m.start)[1] == "start_failed"
    assert m.record()["phase"] == "ended"
    assert m.sup.active() is None
    m.host.spawn = m.spawn
    assert m.start()["accepted"] is True


def test_the_page_runs_in_the_product_only(m):
    m.profile_cgroup = "0::/user.slice/someone.service\n"
    m.present()
    assert (
        refusal(m.sup.challenge, {"purpose": "start", "name": NAME})[1] == "not_product"
    )
    m.profile_cgroup = None
    assert (
        refusal(m.sup.challenge, {"purpose": "start", "name": NAME})[1] == "not_product"
    )


# --- keys ----------------------------------------------------------------------------------------


def test_the_phrase_is_typed_as_one_line_and_nothing_else(m):
    at_phrase(m)
    m.press("execute")
    assert m.typed() == f"execute {NAME}\r".encode()


def test_the_phrase_needs_the_typed_confirmation(m):
    view = at_phrase(m)
    body = m.body_for(view, "execute")
    for wrong in (
        "",
        "execute",
        f"execute {NAME} ",
        f"execute {NAME} after policy switch check",
    ):
        assert (
            refusal(m.sup.key, {**body, "request_id": m.rid(), "confirm": wrong})[1]
            == "confirm_required"
        )
    assert m.typed() == b""
    assert m.sup.key(body)["accepted"] is True


def test_after_the_phrase_the_next_line_does_not_stick_to_it(m):
    """No echo: the unfinished prompt line is ended when the phrase is sent, so
    the first service line is read as a line of its own."""
    at_phrase(m)
    m.press("execute")
    m.append_screen("1/7 预检：GPU 锁、显存、内存、端口 / 无需操作 / 停止：Ctrl+C\r\n")
    view = m.look()
    assert view["screen"]["tail"][-2:] == [
        PHRASE.decode(),
        "1/7 预检：GPU 锁、显存、内存、端口 / 无需操作 / 停止：Ctrl+C",
    ]
    assert m.sup._buf.lines[-1].text.startswith("1/7")


def test_the_policy_switch_phrase_is_typed_only_by_its_own_key(m):
    started(m)
    m.script(
        screen=f"请输入 execute {NAME} after policy switch check：".encode(),
        cbreak=False,
        runner=False,
    )
    view = m.wait_key("execute_variant")
    keys = {k["id"]: k for k in view["keys"]}
    assert keys["execute"]["why_not"]["code"] == "swap_unclean"
    body = m.body_for(view, "execute")
    assert refusal(m.sup.key, {**body, "token": "t-x"})[1] in (
        "state_not_allowed",
        "swap_unclean",
        "stale_page",
    )
    assert m.typed() == b""
    m.press("execute_variant")
    assert m.typed() == f"execute {NAME} after policy switch check\r".encode()


@pytest.mark.parametrize(
    ("scene", "action", "wire"),
    [
        (at_inventory, "inventory_yes", b"y"),
        (at_inventory, "inventory_no", b"n"),
        (at_inventory, "quit", b"q"),
        (at_release, "release_round", b"\r"),
        (at_release, "quit", b"q"),
        (at_forward, "verdict_success", b"s"),
        (at_forward, "verdict_failure", b"f"),
        (at_forward, "verdict_discard", b"d"),
    ],
)
def test_a_key_sends_exactly_its_byte(m, scene, action, wire):
    scene(m)
    answer = m.press(action)
    assert answer["accepted"] is True and answer["evidence"] == "pending"
    assert m.typed() == wire


@pytest.mark.parametrize(
    ("marker", "action", "wire"),
    [
        ("首轮回位会打开夹爪", "home_o", b"o"),
        ("检查机械臂返回 Home", "home_p", b"p"),
        ("确认知道回位会开爪", "home_h", b"h"),
    ],
)
def test_the_homing_keys_send_o_p_h(m, marker, action, wire):
    started(m)
    m.script(journal=14, screen=marker, cbreak=True, steps=9)
    m.press(action)
    assert m.typed() == wire


def synthetic_wait(m, reason, screen_reason=None):
    from test_marvel0_aeri_screen import wait_text

    started(m)
    m.script(journal=21, screen="Home verified", cbreak=True, steps=9)
    m.append_journal(
        [
            {
                "sequence_no": 34,
                "record": "committed",
                "from_state": "VERIFY_INITIAL",
                "to_state": "WAIT_HUMAN",
                "reason": reason,
            }
        ]
    )
    m.append_screen(wait_text("harvest-5bc750ca", screen_reason or reason))


def test_a_wait_that_asks_twice_sends_enter_then_y(m):
    synthetic_wait(m, "operator_stop")
    m.press("resume_ask")
    assert m.typed() == b"\r"
    texts = json.loads(Path(st.aeri_screen.PROMPTS_FILE).read_text())["profiles"][
        "harvest-5bc750ca"
    ]["texts"]["zh"]
    m.append_journal([])
    m.append_screen("[第 1/5 轮] " + texts["confirm_resume"] + "\r\n")
    view = m.wait_key("resume_confirm")
    assert view["state"]["id"] == "await_resume_confirm"
    m.press("resume_confirm")
    assert m.typed() == b"y"


def test_enter_cannot_be_sent_at_the_second_step_nor_y_at_the_first(m):
    synthetic_wait(m, "operator_stop")
    view = m.wait_key("resume_ask")
    body = m.body_for(view, "resume_ask")
    assert (
        refusal(m.sup.key, {**body, "action": "resume_confirm", "request_id": m.rid()})[
            1
        ]
        == "state_not_allowed"
    )
    assert (
        refusal(m.sup.key, {**body, "action": "inventory_yes", "request_id": m.rid()})[
            1
        ]
        == "state_not_allowed"
    )
    assert m.typed() == b""


def test_enter_and_y_are_never_sent_at_the_wrong_prompt(m):
    view = at_inventory(m)
    body = m.body_for(view, "inventory_yes")
    for wrong in (
        "release_round",
        "resume_ask",
        "resume_confirm",
        "verdict_success",
        "home_h",
        "execute",
    ):
        assert refusal(m.sup.key, {**body, "action": wrong, "request_id": m.rid()})[
            1
        ] in (
            "state_not_allowed",
            "stale_page",
        ), wrong
    assert m.typed() == b""


def test_q_cannot_be_sent_while_a_round_runs(m):
    view = at_forward(m)
    body = m.body_for(view, "verdict_success")
    for wrong in ("quit", "inventory_yes", "release_round", "resume_ask"):
        assert (
            refusal(m.sup.key, {**body, "action": wrong, "request_id": m.rid()})[1]
            == "state_not_allowed"
        )
    assert m.typed() == b""


def test_s_f_d_cannot_be_sent_at_a_release_prompt(m):
    view = at_release(m)
    body = m.body_for(view, "release_round")
    for wrong in st.VERDICTS:
        assert (
            refusal(m.sup.key, {**body, "action": wrong, "request_id": m.rid()})[1]
            == "state_not_allowed"
        )
    assert m.typed() == b""


def test_a_stale_view_sends_nothing(m):
    view = at_release(m)
    body = m.body_for(view, "release_round")
    for seen in (
        {**body["seen"], "state_seq": body["seen"]["state_seq"] - 1},
        {**body["seen"], "state_seq": body["seen"]["state_seq"] + 1},
        {**body["seen"], "prompt_id": "release:1"},
        {**body["seen"], "run_id": "20260101T000000-000000"},
        {},
    ):
        assert (
            refusal(m.sup.key, {**body, "seen": seen, "request_id": m.rid()})[1]
            == "stale_page"
        )
    assert (
        refusal(m.sup.key, {**body, "token": "t-0000", "request_id": m.rid()})[1]
        == "stale_page"
    )
    assert m.typed() == b""
    assert m.sup.key(body)["accepted"] is True


def test_a_click_on_a_prompt_that_was_printed_again_is_stale(m):
    view = at_release(m)
    body = m.body_for(view, "release_round")
    line = "第 1/5 轮：确认场景就绪后按 Enter 放行本轮正向子任务循环；q 结束运行\r\n"
    m.append_screen("\r\n" + line)
    time.sleep(0.05)
    assert refusal(m.sup.key, body)[1] == "stale_page"
    assert m.typed() == b""


def test_a_tab_that_never_looked_cannot_press_a_key_that_moves(m):
    view = at_release(m)
    body = m.body_for(view, "release_round", tab_id="tab-99999999")
    assert refusal(m.sup.key, body)[1] == "presence_stale"
    assert (
        refusal(m.sup.key, {**body, "request_id": m.rid(), "tab_id": "../etc/passwd"})[
            1
        ]
        == "presence_stale"
    )
    assert m.typed() == b""
    quit_body = m.body_for(view, "quit", tab_id="tab-99999999")
    assert m.sup.key(quit_body)["accepted"] is True, (
        "q needs no tab: it never moves the arm"
    )
    assert m.typed() == b"q"


def test_a_tab_that_stopped_reporting_is_stale(m, monkeypatch):
    view = at_release(m)
    body = m.body_for(view, "release_round")
    m.sup._tabs[m.tab] -= st.PRESENCE_S + 1
    assert refusal(m.sup.key, body)[1] == "presence_stale"
    m.sup.presence({"tab_id": m.tab, "visible": False})
    assert m.look()["presence"]["ok"] is False
    assert m.typed() == b""


def test_a_key_that_just_appeared_is_refused_until_its_delay_is_over(slow):
    m = slow
    started(m)
    m.script(journal=26, screen="第 1/5 轮：确认场景就绪", cbreak=True, steps=9)
    view = m.wait_until(
        lambda v: any(k["id"] == "release_round" and k["allowed"] for k in v["keys"])
    )
    entry = next(k for k in view["keys"] if k["id"] == "release_round")
    body = m.body_for(view, "release_round")
    due = st.not_before_epoch(entry)
    if time.time() < due - 0.2:
        status, code, extra = refusal(m.sup.key, body)
        assert (status, code) == (429, "key_too_soon") and extra["wait_s"] > 0
        assert m.typed() == b""
    time.sleep(max(0.0, due - time.time()) + 0.1)
    view = m.look()
    assert m.sup.key(m.body_for(view, "release_round"))["accepted"] is True


def test_one_key_per_prompt_and_a_second_click_sends_nothing(m):
    view = at_release(m)
    body = m.body_for(view, "release_round")
    m.sup.key(body)
    assert m.typed() == b"\r"
    again = {**body, "request_id": m.rid()}
    assert refusal(m.sup.key, again)[1] in ("key_sent", "stale_page", "challenge_used")
    quit_body = (
        m.body_for(m.doc(), "quit")
        if any(k["id"] == "quit" and k["allowed"] for k in m.doc()["keys"])
        else None
    )
    if quit_body:
        assert refusal(m.sup.key, quit_body)[1] in ("key_sent", "stale_page")
    assert m.typed() == b""


def test_the_same_request_sent_twice_is_answered_not_acted_on_twice(m):
    view = at_release(m)
    body = m.body_for(view, "release_round")
    first = m.sup.key(body)
    second = m.sup.key(body)
    assert second["replayed"] is True and second["prompt_id"] == first["prompt_id"]
    assert m.typed() == b"\r"


def test_two_clicks_at_once_write_one_byte(m):
    view = at_release(m)
    results = []

    def click(n):
        body = m.body_for(view, "release_round", request_id=f"req-race-{n:04d}-test")
        try:
            results.append(("ok", m.sup.key(body)))
        except Refuse as exc:
            results.append(("refused", exc.code))

    threads = [threading.Thread(target=click, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert [r for r in results if r[0] == "ok"].__len__() == 1, results
    assert {r[1] for r in results if r[0] == "refused"} <= {
        "busy",
        "key_sent",
        "stale_page",
    }
    assert m.typed() == b"\r"


def test_a_key_not_seen_taken_is_never_sent_again(m, monkeypatch):
    monkeypatch.setattr(st, "KEY_NOT_TAKEN_S", 0.3)
    view = at_release(m)
    body = m.body_for(view, "release_round")
    m.sup.key(body)
    assert m.typed() == b"\r"
    time.sleep(0.5)
    doc = m.look()
    assert {"code": "key_unconfirmed", "level": "warn"} in doc["notices"]
    keys = {k["id"]: k for k in doc["keys"]}
    assert all(k["why_not"]["code"] == "key_unconfirmed" for k in keys.values())
    assert refusal(
        m.sup.key,
        {
            **body,
            "request_id": m.rid(),
            "seen": {**body["seen"], "state_seq": doc["state"]["seq"]},
        },
    )[1] in (
        "key_unconfirmed",
        "stale_page",
    )
    assert m.typed() == b""
    assert m.sup.stop({"request_id": m.rid()})["accepted"] is True, (
        "Stop is still there"
    )


def test_a_key_seen_taken_clears_the_wait_and_the_next_key_waits_for_the_gap(m):
    view = at_release(m)
    m.sup.key(m.body_for(view, "release_round"))
    # D takes it: the note of the release in its journal
    m.script(journal=27, screen="第 1/5 轮已放行", cbreak=True)
    doc = m.look()
    assert doc["state"]["id"] == "busy"
    assert m.sup._pending is None and m.sup._last_taken is not None
    assert "key_unconfirmed" not in {n["code"] for n in doc["notices"]}


def test_one_verdict_per_round(m):
    view = at_forward(m)
    body = m.body_for(view, "verdict_failure")
    m.sup.key(body)
    assert m.typed() == b"f"
    # D takes it and prints the line; the round stays latched
    m.append_screen(
        "[第 1/5 轮] 已收到人工判定：失败；本轮提前结束，回位后继续下一轮\r\n"
    )
    m.append_journal(
        [
            {
                "sequence_no": 40,
                "record": "note",
                "note": {"code": "operator_verdict", "detail": '{"label":"failure"}'},
            }
        ]
    )
    doc = m.look()
    assert doc["progress"]["verdict_latched"] is True
    assert not [k for k in doc["keys"] if k["allowed"]]
    for action in st.VERDICTS:
        assert refusal(m.sup.key, {**body, "action": action, "request_id": m.rid()})[
            1
        ] in (
            "verdict_once",
            "state_not_allowed",
            "stale_page",
        )
    assert m.typed() == b""


def test_a_verdict_sent_by_the_page_latches_the_round_even_if_d_never_answers(
    m, monkeypatch
):
    monkeypatch.setattr(st, "KEY_NOT_TAKEN_S", 0.2)
    view = at_forward(m)
    m.sup.key(m.body_for(view, "verdict_success"))
    time.sleep(0.4)
    doc = m.look()
    assert doc["progress"]["verdict_latched"] is True
    assert not [k for k in doc["keys"] if k["allowed"]]


def test_the_monitor_must_be_green_for_a_key_that_moves(m):
    view = at_release(m)
    m.health(red=True)
    view = m.look()
    keys = {k["id"]: k for k in view["keys"]}
    assert keys["release_round"]["why_not"]["code"] == "monitor_not_green"
    assert keys["quit"]["allowed"] is True
    body = m.body_for(view, "release_round", token=keys["quit"]["token"])
    assert refusal(m.sup.key, body)[1] in ("monitor_not_green", "stale_page")
    assert m.typed() == b""


def test_a_stale_monitor_file_is_not_green(m):
    at_release(m)
    m.health(age=60)
    view = m.look()
    assert view["monitor"]["state"] != "ok"
    assert {k["id"]: k for k in view["keys"]}["release_round"]["allowed"] is False


def test_a_screen_that_drifted_offers_nothing_but_stop(m, monkeypatch):
    monkeypatch.setattr(st, "DRIFT_S", 0.3)
    started(m)
    changed = (
        cut("第 1/5 轮：确认场景就绪").decode().replace("按 Enter 放行", "请按回车放行")
    )
    m.script(journal=26, screen=changed.encode(), cbreak=True, steps=9)
    m.look()  # (the expectation begins here)
    time.sleep(0.5)
    view = m.look()
    assert (view["state"]["id"], view["state"]["reason"]) == ("unknown", "screen_drift")
    assert view["keys"] == [] and view["stop"]["allowed"] is True
    assert {"code": "screen_drift", "level": "warn"} in view["notices"]


def test_an_object_list_the_journal_does_not_know_offers_nothing(m):
    started(m)
    other = (
        cut("清单对就按 y 确认").decode().replace("purple_eggplant_1", "green_cup_1")
    )
    m.script(journal=25, screen=other.encode(), cbreak=True, steps=9)
    view = m.look()
    assert view["state"]["reason"] == "inventory_mismatch" and view["keys"] == []


def test_the_page_never_writes_a_byte_outside_its_closed_table(m, monkeypatch):
    written = []
    real = os.write

    def spy(fd, data):
        if fd == m.sup._master:
            written.append(bytes(data))
        return real(fd, data)

    monkeypatch.setattr(os, "write", spy)
    for scene, action in ((at_inventory, "inventory_yes"), (at_inventory, "quit")):
        pass
    view = at_inventory(m)
    m.sup.key(m.body_for(view, "inventory_yes"))
    m.stop()
    allowed_bytes = st.ALL_BYTES | {f"execute {NAME}\r".encode()}
    assert written and all(w in allowed_bytes for w in written), written


# --- stop ---------------------------------------------------------------------------------------


def test_stop_writes_ctrl_c_once_and_never_a_second_time(m):
    at_forward(m)
    answer = m.stop()
    assert answer == {
        "schema": st.RESULT_SCHEMA,
        "accepted": True,
        "phase": "sent",
        "via": "pty",
    }
    assert m.typed() == b"\x03"
    assert m.record()["stop_sent_epoch"] is not None
    assert refusal(m.sup.stop, {"request_id": m.rid()})[1] == "stop_spent"
    assert m.typed() == b""
    doc = m.look()
    assert doc["state"]["id"] == "stopping" and doc["stop"]["allowed"] is False
    assert doc["stop"]["why_not"]["code"] == "stop_spent"
    assert doc["keys"] == []


def test_stop_works_without_a_document_and_without_a_fresh_one(m):
    started(m)
    m.script(screen=PHRASE, cbreak=False, runner=False)
    # (no document was ever read since the start)
    m.sup._cache = None
    answer = m.stop()
    assert answer["accepted"] is True and answer["via"] == "pty"
    # (in the canonical mode the byte waits in the line buffer until D reads
    # the line; with real ptys it is a SIGINT at once: see the live tests)
    assert m.record()["stop_sent_epoch"] is not None


def test_stop_does_not_wait_for_a_click_that_is_being_handled(m):
    view = at_release(m)
    assert m.sup._guard.acquire(blocking=False)  # a slow key is in flight
    try:
        assert refusal(m.sup.key, m.body_for(view, "release_round"))[1] == "busy"
        assert m.stop()["accepted"] is True
    finally:
        m.sup._guard.release()
    assert m.typed() == b"\x03"


def test_stop_does_not_wait_for_the_run_lock(m):
    at_forward(m)
    from levi.marvel0 import stack

    lock = stack.StackLock(m.hub / ".marvel0-run.lock", "/proc/locks", time.time)
    with lock.hold("marvel0-run", "start"):
        assert m.stop()["accepted"] is True
    assert m.typed() == b"\x03"


def test_stop_is_open_in_unknown_and_busy_states(m, monkeypatch):
    monkeypatch.setattr(st, "DRIFT_S", 0.2)
    started(m)
    m.script(journal=26, screen=b"gibberish\r\n", cbreak=True, steps=9)
    m.look()
    time.sleep(0.4)
    view = m.look()
    assert view["state"]["id"] == "unknown" and view["stop"]["allowed"] is True
    assert m.stop()["accepted"] is True


def test_an_unacknowledged_stop_may_be_repeated_once(m, monkeypatch):
    monkeypatch.setattr(st, "STOP_ACK_S", 0.2)
    at_forward(m)
    m.stop()
    m.typed()
    time.sleep(0.4)
    doc = m.look()
    assert doc["stop"]["phase"] == "unconfirmed" and doc["stop"]["allowed"] is True
    assert {"code": "stop_unconfirmed", "level": "warn"} in doc["notices"]
    answer = m.stop()
    assert answer["phase"] == "retried"
    assert m.typed() == b"\x03"
    assert refusal(m.sup.stop, {"request_id": m.rid()})[1] == "stop_spent"
    assert m.typed() == b""


def test_a_stop_that_d_acknowledged_is_not_repeated(m, monkeypatch):
    monkeypatch.setattr(st, "STOP_ACK_S", 0.2)
    at_forward(m)
    m.stop()
    m.append_screen(
        "收到 SIGINT（Ctrl+C）：已停止下发运动指令，正在安全收尾，不会再开始新的一轮。\r\n"
    )
    time.sleep(0.4)
    doc = m.look()
    assert doc["stop"]["phase"] == "acked" and doc["stop"]["allowed"] is False


def test_no_retry_of_a_stop_before_d_had_its_handlers(m, monkeypatch):
    """Before the heartbeat file exists SIGINT is a KeyboardInterrupt, not a
    flag: a second one would interrupt the shutdown it started."""
    monkeypatch.setattr(st, "STOP_ACK_S", 0.2)
    started(m)
    m.script(screen=b"2/7 VLM\r\n", runner=False)
    m.stop()
    time.sleep(0.4)
    doc = m.look()
    assert doc["stop"]["phase"] == "sent" and doc["stop"]["allowed"] is False


def test_stop_falls_back_to_the_pid_of_d_when_the_pty_cannot_carry_it(m):
    at_forward(m)
    m.child.isig(False)  # the terminal would not turn ^C into a signal
    answer = m.stop()
    assert answer["via"] == "pid"
    assert m.typed() == b""
    pid = m.child.pid
    assert ("kill", pid, signal.SIGINT) in m.signals
    assert not [s for s in m.signals if s[2] in (signal.SIGKILL, signal.SIGSTOP)]


def test_stop_goes_to_the_foreground_group_when_nothing_else_works(m):
    at_forward(m)
    m.child.isig(False)
    (m.run_dir / "runner.json").unlink()
    m.host.tcgetpgrp = lambda fd: 4242
    answer = m.stop()
    assert answer["via"] == "group" and ("killpg", 4242, signal.SIGINT) in m.signals


def test_a_stop_with_no_way_to_send_it_says_so(m):
    at_forward(m)
    m.child.isig(False)
    (m.run_dir / "runner.json").unlink()

    def nothing(fd):
        raise OSError("no foreground group")

    m.host.tcgetpgrp = nothing
    assert refusal(m.sup.stop, {"request_id": m.rid()})[1] == "signal_failed"


def test_a_pid_whose_identity_does_not_match_is_never_signalled(m):
    at_forward(m)
    m.child.isig(False)
    body = json.loads((m.run_dir / "runner.json").read_text())
    body["identity"] = {"boot": "b", "start_ticks": "999"}
    (m.run_dir / "runner.json").write_text(json.dumps(body))
    m.host.tcgetpgrp = lambda fd: 4242
    answer = m.stop()
    assert answer["via"] == "group"
    assert not [s for s in m.signals if s[0] == "kill"]


def test_stop_is_refused_for_a_run_that_is_over(m):
    at_forward(m)
    m.child.exits(0)
    m.sup.tick()
    assert refusal(m.sup.stop, {"request_id": m.rid()})[1] == "d_gone"


def test_stop_is_not_offered_to_a_locked_run(m):
    started(m)
    m.script(journal=34, screen="当前正向子任务 步 66/200", cbreak=True, steps=9)
    m.append_journal(
        [
            {
                "sequence_no": 40,
                "record": "committed",
                "from_state": "FORWARD_ACTIVE",
                "to_state": "FAULT_LOCKED",
                "reason": "policy_error",
            }
        ]
    )
    doc = m.look()
    assert doc["state"]["id"] == "fault_locked" and doc["stop"]["allowed"] is False
    assert refusal(m.sup.stop, {"request_id": m.rid()})[1] == "state_not_allowed"
    assert doc["keys"] == []


def test_the_history_records_the_stop_and_the_keys_without_screen_text(m):
    at_release(m)
    m.press("release_round")
    m.stop()
    rows = m.history()
    kinds = [r.get("key") for r in rows if r["event"] in ("key", "stop")]
    assert kinds == ["release_round", "stop"]
    text = json.dumps(rows)
    assert "/home/" not in text and "确认场景" not in text and "放行本轮" not in text
    assert all(r["actor"] == "person" and r["via"] == "product" for r in rows)
    doc = m.look()
    assert [h["key"] for h in doc["history"]] == ["start", "release_round", "stop"]
    assert doc["history"][1]["byte"] == "Enter"
    assert doc["history"][2]["byte"] == "Ctrl+C"


# --- end ---------------------------------------------------------------------------------------


def test_end_is_refused_while_d_can_still_wrap_up(m):
    at_forward(m)
    assert (
        refusal(m.sup.end, {"request_id": m.rid(), "confirm": "end terminal D"})[1]
        == "end_not_allowed"
    )
    m.stop()
    assert (
        refusal(m.sup.end, {"request_id": m.rid(), "confirm": "end terminal D"})[1]
        == "end_not_allowed"
    )
    assert not [s for s in m.signals if s[2] == signal.SIGKILL]


def test_end_needs_its_phrase(m):
    at_forward(m)
    assert (
        refusal(m.sup.end, {"request_id": m.rid(), "confirm": "end"})[1]
        == "confirm_required"
    )


def test_end_kills_the_launchers_groups_when_the_wrap_up_is_stuck(m, monkeypatch):
    monkeypatch.setattr(st, "STALL_S", 0.3)
    at_forward(m)
    m.stop()
    time.sleep(0.5)
    doc = m.look()
    assert doc["state"]["id"] == "stalled" and doc["end"]["allowed"] is True
    answer = m.end()
    assert answer["accepted"] is True
    kills = [s for s in m.signals if s[2] == signal.SIGKILL]
    assert ("killpg", m.child.pid, signal.SIGKILL) in kills
    assert all(s[0] == "killpg" for s in kills)
    after = m.look()
    assert after["state"]["id"] == "ended" and after["d"]["exit"]["signal"] == 9


# --- the end of a run ---------------------------------------------------------------------------


def test_a_run_that_ended_is_recorded_released_and_looked_at(m):
    at_forward(m)
    m.child.exits(0)
    doc = m.look()
    assert doc["state"]["id"] == "ended" and doc["state"]["reason"] == "exit_0"
    assert doc["d"]["alive"] is False and doc["d"]["exit"] == {
        "code": 0,
        "signal": None,
    }
    assert doc["after_exit"] == {
        "units": [],
        "ports": {"8000": "free", "8100": "free"},
        "swap_clean": True,
    }
    assert m.record()["phase"] == "ended" and m.sup._master is None
    events = (m.runs / NAME / "terminal_d.events").read_text()
    assert events.strip().splitlines()[-1].endswith("terminal D exited rc=0")
    assert m.sup.active() is None
    assert doc["start"]["allowed"] is True
    assert doc["keys"] == [] and doc["stop"]["allowed"] is False


def test_a_run_killed_by_the_hangup_chain_is_recorded_with_its_signal(m):
    at_forward(m)
    m.child.exits(-9)
    doc = m.look()
    assert doc["state"]["reason"] == "exit_signal" and doc["d"]["exit"] == {
        "code": None,
        "signal": 9,
    }
    assert (m.runs / NAME / "terminal_d.events").read_text().strip().endswith("rc=137")


def test_a_leftover_unit_is_shown_after_the_exit_and_nothing_is_stopped(m):
    at_forward(m)
    m.host.units = lambda: ["levi-aeri-policy-forward", f"{aeri_run.UNIT_PREFIX}x"]
    m.host.swap_clean = lambda root: False
    m.child.exits(2)
    doc = m.look()
    assert doc["after_exit"]["units"] == ["levi-aeri-policy-forward"]
    assert doc["after_exit"]["swap_clean"] is False
    assert not [s for s in m.signals if s[0] in ("kill", "killpg")]


def test_the_exit_file_of_the_launcher_is_read_when_the_child_was_not_ours(m):
    """After a restart the launcher is not our child: its exit comes from the
    file it wrote."""
    at_forward(m)
    rec = m.record()
    exit_file = m.folder / "logs" / rec["exit_file"]
    exit_file.write_text(json.dumps({"pid": 1, "code": 130, "signal": None}))
    other = aeri_run.Supervisor(
        m.host, site_loader=lambda: m.site, workspace=m.workspace
    )
    m.child.exits(0)  # gone
    other.adopt()
    assert other._rec["phase"] == "ended" and other._rec["exit"] == {
        "code": 130,
        "signal": None,
    }


# --- after a restart -------------------------------------------------------------------------


def restarted(m):
    """A new process finds the record of a run whose launcher lives."""
    return aeri_run.Supervisor(
        m.host, site_loader=lambda: m.site, workspace=m.workspace
    )


def test_a_run_found_again_is_orphaned_gets_one_sigint_and_no_keyboard(m):
    at_forward(m)
    old = m.sup
    new = restarted(m)
    doc = new.view()
    assert doc["state"]["id"] in ("orphaned", "stopping")
    assert doc["d"]["keyboard"] is False and doc["keys"] == []
    assert doc["stop"]["allowed"] is False
    sigints = [s for s in m.signals if s[2] == signal.SIGINT]
    assert sigints == [("kill", m.child.pid, signal.SIGINT)]
    assert new._rec["stop_sent_epoch"] is not None
    new.view()
    new.adopt()
    assert [s for s in m.signals if s[2] == signal.SIGINT] == sigints, "once"
    assert new.active()["kind"] == "marvel0-aeri"
    del old


def test_an_adopted_run_never_takes_the_arm_lock_and_is_never_frozen(m):
    at_forward(m)
    restarted(m).view()
    assert not (m.tmp / "arm.lock").exists()
    assert not [s for s in m.signals if s[2] in (signal.SIGSTOP, signal.SIGKILL)]


def test_an_adopted_run_refuses_a_second_d_and_a_start(m):
    at_forward(m)
    new = restarted(m)
    new.adopt()
    m.sup = new
    m.present()
    assert refusal(new.challenge, {"purpose": "start", "name": NAME})[1] == "d_active"


def test_an_adopted_run_rebuilds_its_screen_from_the_file(m):
    at_forward(m)
    new = restarted(m)
    doc = new.view()
    assert doc["screen"]["bytes"] > 0
    assert any("当前正向子任务" in line for line in doc["screen"]["tail"])


def test_without_a_heartbeat_the_adopted_run_gets_the_sigint_through_its_group(m):
    started(m)
    m.script(screen=b"2/7 VLM\r\n", runner=False)
    restarted(m).adopt()
    assert ("killpg", m.child.pid, signal.SIGINT) in m.signals


def test_an_adopted_run_whose_launcher_is_gone_is_over(m):
    started(m)
    m.child.exits(0)
    new = restarted(m)
    doc = new.view()
    assert doc["state"]["id"] == "ended" and new.active() is None


def test_with_no_record_there_is_no_run(m):
    new = restarted(m)
    assert new.view()["state"]["id"] == "idle" and new.active() is None


def test_a_record_that_cannot_be_read_means_no_run_rather_than_an_error(m):
    started(m)
    (m.folder / "record.json").write_text("{not json")
    new = restarted(m)
    assert new.view()["state"]["id"] == "idle"


def test_the_hosted_d_and_the_client_exclude_each_other(m, monkeypatch):
    """``run_api.run_active()`` (the arm stack's and the deploy's question)
    includes the hosted D."""
    from levi.marvel0 import aeri_run_api, run_api

    class NoClient:
        def adopt(self):
            pass

        def active(self):
            return None

    monkeypatch.setattr(run_api, "supervisor", lambda: NoClient())
    monkeypatch.setattr(aeri_run_api, "supervisor", lambda: m.sup)
    assert run_api.run_active() is False
    started(m)
    assert (
        run_api.run_active() is True
        and aeri_run_api.active_job()["id"] == m.record()["run_id"]
    )
    m.child.exits(0)
    m.sup.tick()
    assert run_api.run_active() is False


def test_the_start_gate_asks_the_client_alone_not_the_hosted_d(m, monkeypatch):
    from levi.marvel0 import run_api

    class Client:
        def __init__(self):
            self.live = False

        def adopt(self):
            pass

        def active(self):
            return {"kind": "marvel0-client"} if self.live else None

    client = Client()
    monkeypatch.setattr(run_api, "supervisor", lambda: client)
    assert aeri_run._e_active() is False
    client.live = True
    assert aeri_run._e_active() is True


# --- the document ----------------------------------------------------------------------------


def test_the_document_is_closed_path_free_and_has_no_judgement(m):
    at_forward(m)
    doc = m.look()
    sample = json.loads(
        (Path(__file__).parent / "fixtures" / "aeri_run.sample.json").read_text()
    )
    assert set(doc) == set(next(iter(sample["samples"].values())))
    text = json.dumps(doc)
    assert "/home/" not in text and str(m.tmp) not in text
    assert "subtask_check" not in text and "subtask_plan" not in text
    assert (
        doc["schema"] == st.SCHEMA and doc["name"] == NAME and doc["mode"] == "external"
    )
    assert doc["profile"]["id"] == "test-profile" and doc["fallback"] == {
        "stop_cli": "scripts/aeri_page_stop.py"
    }
    assert {"code": "not_estop", "level": "info"} in doc["notices"]


def test_the_screen_tail_cuts_paths_to_their_last_segment(m):
    at_forward(m)
    m.append_screen("checkpoint at /home/someone/openpi/checkpoints/pi05/params ok\r\n")
    tail = m.look()["screen"]["tail"]
    assert any(
        line == "checkpoint at params ok" or line.endswith("params ok") for line in tail
    )
    assert not any("/home/" in line for line in tail)


def test_the_tail_is_at_most_sixty_lines(m):
    at_forward(m)
    m.append_screen("".join(f"line {n}\r\n" for n in range(200)))
    assert len(m.look()["screen"]["tail"]) == st.SCREEN_TAIL


def test_a_resume_the_page_did_not_send_is_reported(m):
    from test_marvel0_aeri_screen import wait_text

    synthetic_wait(m, "subtask_not_released")
    m.append_screen("\r\n[第 1/5 轮] 页面已处理，继续\r\n")
    doc = m.look()
    assert {"code": "inbox_resume", "level": "warn"} in doc["notices"]
    del wait_text


def test_the_locks_are_read_from_the_lock_table_and_the_sockets(m):
    import fcntl

    at_forward(m)
    doc = m.look()
    assert doc["locks"]["arm"] == "free" and doc["locks"]["ports"] == {
        "8000": "free",
        "8100": "free",
        "8110": "free",
    }
    arm = os.open(m.tmp / "arm.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(arm, fcntl.LOCK_EX)
    m.write_net(listening=[8100])
    try:
        doc = m.look()
        assert doc["locks"]["arm"] in ("held_by_d", "held_elsewhere")
        assert doc["locks"]["ports"]["8100"] in ("d_unit", "other")
        assert doc["locks"]["ports"]["8000"] == "free"
    finally:
        os.close(arm)


def test_the_log_route_gives_the_screen_without_paths(m):
    at_forward(m)
    m.append_screen("loaded /home/someone/x/model.bin\r\n")
    log = m.sup.log(50)
    assert log["schema"] == st.LOG_SCHEMA and log["bytes"] > 0
    assert "loaded model.bin" in log["lines"] and not any(
        "/home/" in line for line in log["lines"]
    )


def test_presence_is_bounded_and_forgotten(m):
    for n in range(300):
        m.sup.presence({"tab_id": f"tab-{n:08d}", "visible": True})
    assert len(m.sup._tabs) == 300
    m.sup._tabs = {t: at - 200 for t, at in m.sup._tabs.items()}
    m.sup.presence({"tab_id": "tab-new-0001", "visible": True})
    assert list(m.sup._tabs) == ["tab-new-0001"]
    assert (
        refusal(m.sup.presence, {"tab_id": "x", "visible": True})[1]
        == "request_id_invalid"
    )


# --- a key is written after its intent is on disk, and never again -----------------------------


def test_a_key_is_written_only_after_the_intent_to_send_it_is_on_disk(m, monkeypatch):
    from levi.marvel0 import record

    view = at_release(m)
    body = m.body_for(view, "release_round")
    real = record.put_request

    def failing(directory, request_id, entry, replace=False):
        if entry.get("status") == "sent":
            raise OSError(28, "No space left on device")
        return real(directory, request_id, entry, replace=replace)

    monkeypatch.setattr(record, "put_request", failing)
    with pytest.raises(OSError):
        m.sup.key(body)
    assert m.typed() == b"", (
        "nothing was sent: a byte that may not be remembered is not sent"
    )
    monkeypatch.setattr(record, "put_request", real)
    doc = m.look()
    assert all(k["why_not"]["code"] == "key_sent" for k in doc["keys"]), (
        "and it is not tried again"
    )


def test_a_request_that_was_sent_but_never_answered_is_not_sent_again(m):
    """The core stopped after writing a key and before it answered: asking again
    with the same request id says what is unknown; no byte goes out."""
    from levi.marvel0 import record

    view = at_release(m)
    body = m.body_for(view, "release_round")
    fingerprint = record.fingerprint(
        "key",
        {
            k: v
            for k, v in body.items()
            if k not in ("challenge", "attest", "token", "seen", "tab_id")
        },
    )
    record.put_request(
        m.folder,
        body["request_id"],
        {"fingerprint": fingerprint, "action": "key", "status": "sent", "time": "t"},
    )
    answer = m.sup.key(body)
    assert answer["replayed"] is True and answer["accepted"] is False
    assert answer["reason"] == "request_incomplete"
    assert m.typed() == b""


def test_the_keyboard_pty_does_not_echo(m):
    """No echo: nothing is read back from the master, and the typed phrase must
    not be shown anywhere. The page turns it off, the fake terminal does not."""
    import termios

    m.start()
    attrs = termios.tcgetattr(m.child.keyboard)
    assert not attrs[3] & (
        termios.ECHO | termios.ECHOE | termios.ECHOK | termios.ECHONL
    )
    assert os.get_blocking(m.sup._master) is False, (
        "and the master never blocks a write"
    )


def test_a_restart_after_a_stop_does_not_send_another_one(m):
    """Stop was clicked, then the product restarted: the run is adopted, and a
    second Ctrl+C would break the wrap-up D is in."""
    at_forward(m)
    m.stop()
    m.typed()
    restarted(m).adopt()
    assert not [s for s in m.signals if s[2] == signal.SIGINT]
    assert not [s for s in m.signals if s[0] == "killpg"]


def test_anything_d_writes_to_the_keyboard_pty_is_read_and_dropped(m):
    """D's screen is the launcher's other pty; if D ever wrote to its fd 0
    (it never does), the master must not fill up and stall it."""
    import select

    started(m)
    os.write(m.child.keyboard, b"something on the keyboard pty\r\n")
    ready, _, _ = select.select([m.sup._master], [], [], 0.5)
    assert ready, "it is there to be read"
    m.sup.tick()
    ready, _, _ = select.select([m.sup._master], [], [], 0.1)
    assert not ready


def test_the_document_names_the_pid_of_d_not_only_the_launchers(m):
    started(m)
    assert m.doc()["d"]["pid"] == m.child.pid, "no heartbeat yet: the launcher's"
    m.script(journal=26, screen="第 1/5 轮：确认场景就绪", steps=9, runner=False)
    m.heartbeat(pid=424242)
    assert m.doc()["d"]["pid"] == 424242


def test_an_ended_run_is_still_shown_after_a_restart(m):
    at_forward(m)
    m.child.exits(0)
    m.look()  # recorded as ended
    doc = restarted(m).view()
    assert doc["state"]["id"] == "ended" and doc["state"]["reason"] == "exit_0"
    assert (
        doc["d"]["exit"] == {"code": 0, "signal": None}
        and doc["after_exit"] is not None
    )
    assert doc["name"] == NAME and doc["keys"] == []
