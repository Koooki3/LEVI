"""``levi/marvel0/run.py``, the supervisor on a scripted client: the start and
everything that refuses it, the keys (what reaches the keyboard and what never
does), hold, end, the watcher's own holds, and the take-over after a restart.

The client is scripted (``Machine`` in ``test_marvel0_run.py``): the test writes
its session file and log lines and reads every byte the page sent to its
keyboard. Nothing is started and nothing is signalled."""

import fcntl
import json
import os
import signal
import stat
import threading
import time

import pytest
from test_marvel0_run import (
    ATTEST,
    GROUP,
    PLATES,
    PROMPT,
    SCREWS,
    SP,
    Machine,
    make_machine,  # noqa: F401 - the fixture
    refusal,
)

from levi.marvel0 import record, run


def flock_busy(path) -> bool:
    """Whether another descriptor holds the flock on ``path`` now."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def at_gate(m, no=1):
    m.gate(no)
    view = m.look()
    assert view["state"] == "at_gate", view["reason"]
    return view


def running(m, no=1, step=3):
    m.set_session("running", no=no, step=step)
    m.say("Homing over 30 steps -> [0.4]", "Home reached.")
    view = m.look()
    assert view["state"] == "running", view["reason"]
    return view


def judging(m, no=1):
    m.set_session("running", no=no, step=30)
    m.say(
        f"\nEpisode {no} ended: timeout  (30 steps, 3.1 s)",
        "  hit the 30-step limit. Judge it:",
    )
    view = m.look()
    assert view["state"] == "awaiting_label", view["reason"]
    return view


# --- the start ---------------------------------------------------------------------------


def test_a_start_launches_the_form_once_with_the_allowlisted_environment(machine):
    m = machine
    answer = m.start()
    assert answer["created"] is True and answer["state"] == "starting"
    assert len(m.spawned) == 1
    line, env = m.spawned[0]["argv"], m.spawned[0]["env"]
    assert line[:3] == [run.sys.executable, "-I", m.host.hostchild]
    assert env == run.child_env(m.environ)
    assert "LEVI_UI_TOKEN" not in env and "HF_TOKEN" not in env
    assert m.spawned[0]["cwd"] == m.site.openpi_checkout
    spec = json.loads(m.log_path().with_suffix(".spec.json").read_text())
    assert spec["cwd"] == m.site.openpi_checkout
    assert spec["argv"][0] == run.sys.executable
    assert spec["argv"][-1] == "--no-display-images"
    flags = spec["argv"][2:]
    assert f"--rollout-group={GROUP}" in flags
    assert f"--prompt={PROMPT}" in flags
    assert (
        "--levi-mode=off" in flags and "--new" in flags and "--no-step-by-step" in flags
    )
    for path in (m.log_path(), m.log_path().with_suffix(".spec.json")):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    rec = m.record()
    assert rec["phase"] == "started" and rec["leader"]["pid"] == m.child.pid
    assert rec["session_file"] == f"{GROUP}__pick_the_eggplant_in_the_blue_plate.json"
    assert m.sup.active()["kind"] == "marvel0-client"
    assert m.view()["state"] == "starting"


def test_the_whole_run_holds_the_arm_lock_and_lets_go_at_the_end(machine):
    m = machine
    arm = m.tmp / "arm.lock"
    assert not flock_busy(arm)
    m.start()
    assert flock_busy(arm), "the arm lock is held for the run"
    assert flock_busy(arm) and not flock_busy(m.hub / run.LOCK_FILE), (
        "the run lock is not"
    )
    m.child.exits(0)
    m.set_session("stopped", "user_quit")
    assert m.look()["state"] == "done"
    assert not flock_busy(arm)
    assert m.sup._master is None and m.sup._arm_fd is None and m.sup.active() is None


def test_a_second_start_while_a_client_runs_is_refused_by_the_checks(machine):
    m = machine
    m.start()
    status, code, extra = refusal(m.start)
    assert (status, code, extra["first"]) == (409, "preflight_failed", "run_idle")
    assert len(m.spawned) == 1


def test_a_request_sent_twice_starts_once(machine):
    m = machine
    form = m.form()
    c = m.sup.challenge({"purpose": "start", "form": form})
    body = {
        "request_id": "req-start-twice",
        "form": form,
        "challenge": c["challenge"],
        "confirm": c["confirm"],
        "attest": dict(ATTEST),
    }
    first = m.sup.start(body)
    second = m.sup.start({**body, "challenge": "a-token-that-was-never-issued"})
    assert second["replayed"] is True and second["run_id"] == first["run_id"]
    assert len(m.spawned) == 1
    assert (
        refusal(m.sup.start, {**body, "form": m.form(max_steps=20)})[1]
        == "request_id_used"
    )


@pytest.mark.parametrize(
    "mutate, code, first",
    [
        (lambda m: setattr(m, "enabled", False), "run_disabled", None),
        (
            lambda m: setattr(m, "profile", "0::/user.slice/other.service\n"),
            "not_product",
            None,
        ),
        (
            lambda m: setattr(m, "deploy", {**m.ready_view(), "state": "starting"}),
            "preflight_failed",
            "policy_ready",
        ),
        (
            lambda m: setattr(m, "deploy", m.ready_view(ours=False)),
            "preflight_failed",
            "policy_ready",
        ),
        (
            lambda m: setattr(m, "deploy", m.ready_view(model="other_model")),
            "preflight_failed",
            "policy_matches",
        ),
        (
            lambda m: setattr(m, "deploy", m.ready_view(rtc=False)),
            "preflight_failed",
            "policy_matches",
        ),
        (
            lambda m: setattr(m, "deploy", m.ready_view(established=1)),
            "preflight_failed",
            "no_connection",
        ),
        (
            lambda m: setattr(
                m,
                "deploy",
                {**m.ready_view(), "listener": {"ours": True, "established": None}},
            ),
            "preflight_failed",
            "no_connection",
        ),
        (
            lambda m: setattr(
                m,
                "aeri",
                [{"backend": "standalone", "runner_alive": True, "run_id": "a"}],
            ),
            "preflight_failed",
            "no_aeri",
        ),
        (lambda m: m.health(red=True), "preflight_failed", "monitor_green"),
        (lambda m: m.health(age=30), "preflight_failed", "monitor_green"),
        (lambda m: m.health_path.unlink(), "preflight_failed", "monitor_green"),
        (
            lambda m: os.chmod(m.site.rollout_root, 0o500),
            "preflight_failed",
            "rollout_writable",
        ),
    ],
    ids=[
        "disabled",
        "not the product",
        "policy starting",
        "policy not ours",
        "other model",
        "other rtc",
        "a client connected",
        "connections unreadable",
        "an AERI run",
        "monitor red",
        "monitor stale",
        "monitor missing",
        "rollout not writable",
    ],
)
def test_a_start_is_refused_and_nothing_launches(machine, mutate, code, first):
    m = machine
    mutate(m)
    try:
        _, got, extra = refusal(m.start)
    finally:
        os.chmod(m.site.rollout_root, 0o700)
    assert got == code and extra.get("first") == first
    assert m.spawned == [] and not flock_busy(m.tmp / "arm.lock")
    assert not (m.workspace.state / "marvel0" / "run" / "record.json").exists()


def test_an_arm_lock_held_by_another_program_refuses_the_start(machine):
    m = machine
    other = os.open(m.tmp / "arm.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(other, fcntl.LOCK_EX)
    try:
        # (the page sees it before any click: the check row and can_start)
        view = m.view()
        rows = {c["code"]: c["level"] for c in view["preflight"]}
        assert rows["arm_lock"] == "refuse" and view["can_start"] is False
        assert refusal(m.start)[2]["first"] == "arm_lock"
        # (the challenge refuses on the row; the lock itself would refuse again)
        assert (
            refusal(m.sup.challenge, {"purpose": "start", "form": m.form()})[2]["first"]
            == "arm_lock"
        )
    finally:
        os.close(other)
    assert m.spawned == []


def test_an_active_session_refuses_the_start_and_an_ended_one_does_not(machine):
    m = machine
    folder = os.path.join(m.site.rollout_root, ".eval_sessions")
    os.makedirs(folder)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(m.host.now()))
    path = os.path.join(folder, "g__task.json")
    for state, ok in (("running", False), ("stopped", True)):
        with open(path, "w") as handle:
            json.dump({"group": "g", "state": state, "updated_at": stamp}, handle)
        if ok:
            assert m.start()["created"]
        else:
            assert refusal(m.start)[2]["first"] == "no_session"


def test_an_unreadable_session_folder_refuses_the_start(machine):
    m = machine
    folder = os.path.join(m.site.rollout_root, ".eval_sessions")
    os.makedirs(folder)
    with open(os.path.join(folder, "g__t.json"), "w") as handle:
        handle.write("{ not json")
    assert refusal(m.start)[2]["first"] == "no_session"


def test_the_phrase_the_checklist_and_the_token_are_all_needed(machine):
    m = machine
    form = m.form()
    c = m.sup.challenge({"purpose": "start", "form": form})
    body = {
        "request_id": "req-gates-0001",
        "form": form,
        "challenge": c["challenge"],
        "confirm": c["confirm"],
        "attest": dict(ATTEST),
    }
    assert (
        refusal(m.sup.start, {**body, "confirm": "start client"})[1]
        == "confirm_required"
    )
    assert (
        refusal(m.sup.start, {**body, "confirm": "start client other"})[1]
        == "confirm_required"
    )
    for key in ATTEST:
        assert (
            refusal(m.sup.start, {**body, "attest": {**ATTEST, key: False}})[1]
            == "attest_required"
        )
    assert refusal(m.sup.start, {**body, "attest": {}})[1] == "attest_required"
    assert (
        refusal(m.sup.start, {**body, "request_id": "short"})[1] == "request_id_invalid"
    )
    assert m.spawned == []
    # none of those spent the token
    assert m.sup.start(body)["created"] is True


@pytest.mark.parametrize(
    "how", ["missing", "unknown", "used", "expired", "stale", "purpose"]
)
def test_the_start_token_is_checked(machine, how):
    m = machine
    form = m.form()
    c = m.sup.challenge({"purpose": "start", "form": form})
    token, form_sent = c["challenge"], form
    if how == "missing":
        token = ""
    elif how == "unknown":
        token = "never-issued"
    elif how == "used":
        m.sup.challenges.consume(
            token,
            "start",
            {"form": run._form_fingerprint(m.sup._form(m.site, form)[1])},
        )
    elif how == "expired":
        m.clock.advance(31)
    elif how == "stale":
        form_sent = m.form(max_steps=20)  # another form than the token was issued for
    elif how == "purpose":
        token = m.sup.challenges.issue("enter", {})["challenge"]
    body = {
        "request_id": f"req-token-{how}",
        "form": form_sent,
        "challenge": token,
        "confirm": c["confirm"],
        "attest": dict(ATTEST),
    }
    got = refusal(m.sup.start, body)[1]
    assert (
        got
        == {
            "missing": "challenge_invalid",
            "unknown": "challenge_invalid",
            "used": "challenge_used",
            "expired": "challenge_expired",
            "stale": "challenge_stale",
            "purpose": "challenge_invalid",
        }[how]
    )
    assert m.spawned == []


def test_the_start_challenge_says_why_a_start_would_be_refused(machine):
    m = machine
    m.health(red=True)
    status, code, extra = refusal(
        m.sup.challenge, {"purpose": "start", "form": m.form()}
    )
    assert (status, code, extra["first"]) == (409, "preflight_failed", "monitor_green")
    assert [c["code"] for c in extra["checks"] if c["level"] != "warn"] == [
        "monitor_green"
    ]


def test_the_start_challenge_lists_the_checks_of_the_form_too(machine):
    m = machine
    answer = m.sup.challenge({"purpose": "start", "form": m.form()})
    rows = {c["code"]: c["level"] for c in answer["preflight"]}
    # (the checks that need the form: the server's model and RTC, the z floor)
    assert rows["policy_matches"] == "ok" and rows["min_z"] == "ok"
    assert all(level in ("ok", "warn") for level in rows.values())


def test_a_restricted_task_below_its_floor_is_refused_at_the_challenge_and_the_start(
    machine,
):
    """RIG-08: the client lets an explicit --min-z beat its task table, so a
    floor of 0.03 would run the screws task with the fingertips at the table."""
    m = machine
    for prompt, floor in ((SCREWS, 0.075), (PLATES, 0.05325)):
        form = m.form(model_id=SP, prompt=prompt, min_z=0.03)
        status, code, extra = refusal(
            m.sup.challenge, {"purpose": "start", "form": form}
        )
        assert (status, code, extra["floor"]) == (422, "min_z_below_task_floor", floor)
        body = {
            "request_id": "req-minz-0001",
            "form": form,
            "challenge": "x",
            "confirm": f"start client {SP}_rtc_demo",
            "attest": dict(ATTEST),
        }
        m.deploy = m.ready_view(model=SP)
        assert refusal(m.sup.start, body)[1] == "min_z_below_task_floor"
    assert m.spawned == []
    # at the floor it passes; so does an unlisted task far below it
    m.deploy = m.ready_view(model=SP)
    ok = m.sup.challenge(
        {"purpose": "start", "form": m.form(model_id=SP, prompt=SCREWS, min_z=0.075)}
    )
    assert ok["group"] == f"{SP}_rtc_demo"
    m.deploy = m.ready_view()
    eggplant = m.sup.challenge({"purpose": "start", "form": m.form(min_z=0.025)})
    assert [w["code"] for w in eggplant["warnings"]] == ["min_z_below_task_table"]


def test_a_form_the_command_module_refuses_keeps_its_own_code(machine):
    m = machine
    for form, code in (
        (m.form(model_id="nope"), "model_unknown"),
        (m.form(max_steps=10**6), "max_steps_out_of_range"),
        (m.form(min_z=0.001), "min_z_below_floor"),
        (m.form(prompt=""), "prompt_invalid"),
    ):
        status, got, _ = refusal(m.sup.challenge, {"purpose": "start", "form": form})
        assert (status, got) == (422, code)


def test_the_machine_wide_run_lock_and_the_process_lock_refuse_a_second_write(machine):
    m = machine
    # another LEVI instance holds the run lock
    other = os.open(m.hub / run.LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(other, fcntl.LOCK_EX)
    try:
        assert refusal(m.start)[:2] == (423, "busy")
    finally:
        os.close(other)
    assert m.spawned == []
    # a write of this process is going on
    assert m.sup._guard.acquire(blocking=False)
    try:
        assert refusal(m.start)[:2] == (423, "busy")
    finally:
        m.sup._guard.release()
    assert m.start()["created"]


def test_a_launch_that_fails_leaves_nothing_behind(machine):
    m = machine

    def broken(argv, **kw):
        raise OSError("no such launcher")

    m.host.spawn = broken
    assert refusal(m.start)[:2] == (502, "start_failed")
    assert not flock_busy(m.tmp / "arm.lock")
    view = m.view()
    assert (view["state"], view["reason"]["code"]) == ("failed", "spawn_failed")
    assert m.sup._master is None and m.sup.active() is None
    # a new start is possible
    m.host.spawn = m.spawn
    assert m.start()["created"]


def test_a_start_outside_the_product_changes_nothing(machine):
    m = machine
    m.profile = "0::/user.slice/session-3.scope\n"
    doc = m.view()
    assert (
        doc["profile"] == "fake"
        and doc["actions"]["start"]["why_not"]["code"] == "not_product"
    )
    assert (
        refusal(m.sup.hold, {"request_id": "req-hold-0001"})[1] == "state_not_allowed"
    )
    assert not (m.workspace.state / "marvel0" / "run").exists() or not list(
        (m.workspace.state / "marvel0" / "run").glob("logs/*")
    )


# --- the view ----------------------------------------------------------------------------


def test_the_idle_view_lists_the_checks(machine):
    m = machine
    doc = m.view()
    assert doc["state"] == "idle" and doc["can_start"] is True
    assert [r["code"] for r in doc["preflight"]] == [
        "client_enabled",
        "product",
        "run_idle",
        "policy_ready",
        "no_connection",
        "no_session",
        "arm_lock",
        "monitor_green",
        "rollout_writable",
        "robot_server",
    ]
    assert {r["level"] for r in doc["preflight"]} == {"ok", "warn"}
    m.health(red=True)
    doc = m.view()
    assert doc["can_start"] is False
    assert doc["actions"]["start"]["why_not"]["params"] == {"first": "monitor_green"}


def test_the_view_is_cached_briefly_and_a_write_drops_the_cache(machine):
    m = machine
    m.sup._cache = None
    first = m.sup.view()
    assert m.sup.view() is first
    m.start()
    assert m.sup.view() is not first


def test_the_log_page_is_printable_and_has_no_path(machine):
    m = machine
    assert m.sup.log(10)["lines"] == []
    m.start()
    m.say(
        "Rollout data: /home/someone/rollouts/g/<task>/demo_XXXX",
        "\x1b[1;33mwarning\x1b[0m done",
        "x" * 500,
    )
    page = m.sup.log(5)
    assert page["schema"] == "levi.marvel0.run_log.v1" and page["size_bytes"] > 0
    text = "\n".join(page["lines"])
    assert "/home/someone" not in text and "demo_XXXX" in text
    assert "\x1b" not in text and all(len(line) <= 300 for line in page["lines"])


# --- the keys ----------------------------------------------------------------------------


def test_enter_at_the_gate_sends_one_byte_and_never_a_second(machine):
    m = machine
    m.start()
    at_gate(m)
    answer = m.press("enter")
    assert answer["sent"] == "enter" and m.typed() == b"\r"
    # it is not sent again by a second click, a new token or a repeated request
    assert refusal(m.sup.challenge, {"purpose": "enter"})[1] == "enter_locked"
    assert refusal(m.press, "enter")[1] == "enter_locked"
    assert m.typed() == b""
    # q behind an Enter that was not taken would be read as "quit" by the running loop
    assert refusal(m.press, "end_at_gate")[1] == "enter_locked"
    assert m.typed() == b""
    # the gate of the next episode is a new gate
    running(m)
    m.say("Episode 1 ended: success")
    m.set_session("standby", run.GATE_REASON, no=2)
    m.say('\n=== ready | ep 2 | 1/3 | "x" ===')
    assert m.look()["state"] == "at_gate"
    assert m.press("enter")["sent"] == "enter" and m.typed() == b"\r"


def test_a_label_is_sent_once_per_episode_whatever_is_clicked_next(machine):
    """The race of the review: a second click while the client saves the
    episode waits in the keyboard and becomes 'RUN screws' at the next gate."""
    m = machine
    m.start()
    at_gate(m)
    m.press("enter")
    m.typed()
    running(m)
    assert m.press("success")["sent"] == "success" and m.typed() == b"s"
    # the client still says running (it is saving the episode)
    for _ in range(3):
        view = m.look()
        assert view["state"] == "running" or view["state"] == "finishing"
        for action in ("success", "failure", "exit_home"):
            assert (
                refusal(m.sup.challenge, {"purpose": run.PURPOSES[action]})[1]
                == "key_locked"
            )
            assert not view["actions"][action]["allowed"]
        assert view["actions"]["hold"]["allowed"]
    m.say("Episode 1 ended: success  (12 steps, 1.0 s)")
    assert m.look()["state"] == "finishing"
    m.set_session("homing", "after episode")
    assert m.look()["state"] == "homing"
    assert m.typed() == b""
    # the next gate: nothing but Enter and q is offered, and s never is
    m.gate(no=2)
    view = m.look()
    assert view["state"] == "at_gate" and not view["actions"]["success"]["allowed"]
    assert refusal(m.press, "success")[1] == "state_not_allowed"
    assert m.typed() == b""


def test_the_step_limit_prompt_takes_one_label(machine):
    m = machine
    m.start()
    at_gate(m)
    m.press("enter")
    m.typed()
    judging(m)
    assert m.press("discard")["sent"] == "discard" and m.typed() == b"d"
    assert refusal(m.press, "failure")[1] == "key_locked"
    assert m.typed() == b""


def test_exit_and_go_home_needs_its_own_phrase_and_keeps_the_token(machine):
    m = machine
    m.start()
    at_gate(m)
    m.press("enter")
    m.typed()
    running(m)
    c = m.sup.challenge({"purpose": "exit_home"})
    body = {
        "request_id": "req-exit-0001",
        "action": "exit_home",
        "challenge": c["challenge"],
        "confirm": "",
    }
    assert refusal(m.sup.key, body)[:2] == (400, "confirm_required")
    assert (
        refusal(m.sup.key, {**body, "confirm": "exit and go home now"})[1]
        == "confirm_required"
    )
    assert m.typed() == b""
    assert m.sup.key({**body, "confirm": "exit and go home"})["sent"] == "exit_home"
    assert m.typed() == b"q"
    assert m.view()["actions"]["exit_home"]["blind"] is True


def test_q_at_the_gate_ends_the_run_without_motion(machine):
    m = machine
    m.start()
    at_gate(m)
    assert m.press("end_at_gate")["sent"] == "end_at_gate" and m.typed() == b"q"
    assert m.record()["end_requested"] is True
    view = m.look()
    assert (view["state"], view["reason"]["code"]) == ("finishing", "end_requested")
    for action in ("enter", "end_at_gate", "success"):
        assert not view["actions"][action]["allowed"]
    m.child.exits(0)
    m.set_session("stopped", "user_quit")
    view = m.look()
    assert (view["state"], view["reason"]["code"]) == ("done", "user_quit")
    assert m.signals == [], "nothing was signalled"


def test_a_request_sent_twice_sends_the_key_once(machine):
    m = machine
    m.start()
    at_gate(m)
    c = m.sup.challenge({"purpose": "enter"})
    body = {
        "request_id": "req-enter-twice",
        "action": "enter",
        "challenge": c["challenge"],
        "confirm": "",
    }
    first = m.sup.key(body)
    again = m.sup.key({**body, "challenge": "other"})
    assert again["replayed"] is True and again["sent"] == first["sent"]
    assert m.typed() == b"\r"
    assert refusal(m.sup.key, {**body, "action": "end_at_gate"})[1] == "request_id_used"


def test_a_token_from_another_moment_does_not_send_a_key(machine):
    m = machine
    m.start()
    at_gate(m)
    old = m.sup.challenge({"purpose": "enter"})
    # the client moved to the gate of the next episode in between
    m.gate(no=2)
    m.look()
    body = {
        "request_id": "req-stale-0001",
        "action": "enter",
        "challenge": old["challenge"],
        "confirm": "",
    }
    assert refusal(m.sup.key, body)[1] == "challenge_stale"
    assert m.typed() == b""
    # a token is used once; another action cannot use it
    fresh = m.sup.challenge({"purpose": "enter"})
    ok = {**body, "request_id": "req-stale-0002", "challenge": fresh["challenge"]}
    assert m.sup.key(ok)["sent"] == "enter"
    again = {**ok, "request_id": "req-stale-0003"}
    assert refusal(m.sup.key, again)[1] in ("challenge_used", "enter_locked")
    assert m.typed() == b"\r"


def test_a_key_in_the_wrong_state_never_reaches_the_keyboard(machine):
    """A token issued for the gate does not send s once the run is running,
    and a stale page cannot choose the state."""
    m = machine
    m.start()
    at_gate(m)
    gate_token = m.sup.challenge({"purpose": "enter"})["challenge"]
    m.press("enter")
    m.typed()
    running(m)
    for action in run.KEY_RULES:
        if action in ("success", "failure", "exit_home"):
            continue
        _, code, _ = refusal(
            m.sup.key,
            {
                "request_id": f"req-wrong-{action}",
                "action": action,
                "challenge": gate_token,
                "confirm": "",
            },
        )
        assert code in ("state_not_allowed", "enter_locked", "key_locked"), (
            action,
            code,
        )
    assert m.typed() == b""


def test_no_state_lets_any_byte_but_the_five_reach_the_keyboard(machine):
    m = machine
    m.start()
    seen = set()
    scenes = [
        ("starting", lambda: None),
        ("at_gate", lambda: m.gate(1)),
        ("homing", lambda: m.set_session("homing", no=1)),
        ("running", lambda: m.set_session("running", no=1, step=4)),
        (
            "awaiting_label",
            lambda: (
                m.set_session("running", no=1, step=30),
                m.say(
                    "\nEpisode 1 ended: timeout  (30 steps, 3.1 s)",
                    "  hit the 30-step limit. Judge it:",
                ),
            ),
        ),
        ("finishing", lambda: m.set_session("stopped", "user_quit")),
    ]
    sent = {}
    for state, arrange in scenes:
        arrange()
        assert m.look()["state"] == state
        for action in run.KEY_RULES:
            m.sup._label_ep = m.sup._enter_ep = None
            m.sup._keyed = False
            try:
                m.press(
                    action,
                    **(
                        {"confirm": "exit and go home"} if action == "exit_home" else {}
                    ),
                )
            except run.Refuse:
                pass
            sent[(state, action)] = m.typed()
            seen.update(sent[(state, action)])
            m.sup._update(end_requested=False)
    assert seen <= set(b"\rsfdq")
    for (state, action), data in sent.items():
        if data:
            assert state in run.KEY_RULES[action][1], (state, action)
            assert data == run.KEY_RULES[action][0]
    assert {a for (st, a), d in sent.items() if st == "at_gate" and d} == {
        "enter",
        "end_at_gate",
    }
    assert {a for (st, a), d in sent.items() if st == "running" and d} == {
        "success",
        "failure",
        "exit_home",
    }
    assert {a for (st, a), d in sent.items() if st == "awaiting_label" and d} == {
        "success",
        "failure",
        "discard",
    }
    assert not any(
        d for (st, _), d in sent.items() if st in ("starting", "homing", "finishing")
    )


def test_a_key_that_may_not_have_arrived_is_never_sent_again(machine, monkeypatch):
    m = machine
    m.start()
    at_gate(m)
    c = m.sup.challenge({"purpose": "enter"})
    body = {
        "request_id": "req-crash-0001",
        "action": "enter",
        "challenge": c["challenge"],
        "confirm": "",
    }
    real = run.os.write
    monkeypatch.setattr(
        run.os, "write", lambda fd, data: (_ for _ in ()).throw(OSError("pty gone"))
    )
    assert refusal(m.sup.key, body)[:2] == (502, "key_failed")
    monkeypatch.setattr(run.os, "write", real)
    assert m.typed() == b""
    # the same request is answered "unknown", never sent
    again = m.sup.key(body)
    assert again["replayed"] is True and again["reason"]["code"] == "request_incomplete"
    # and a new click is refused: the gate's Enter is spent
    assert refusal(m.press, "enter")[1] == "enter_locked"
    assert m.typed() == b""


def test_a_restart_between_the_record_and_the_byte_loses_the_key_and_never_doubles_it(
    machine,
):
    m = machine
    m.start()
    at_gate(m)
    # what a crash after "sent" is written but before the byte leaves behind
    c = m.sup.challenge({"purpose": "enter"})
    body = {
        "request_id": "req-sent-0001",
        "action": "enter",
        "challenge": c["challenge"],
        "confirm": "",
    }
    fingerprint = record.fingerprint("key", run._fingerprint_body(body))
    record.put_request(
        m.folder,
        "req-sent-0001",
        {"fingerprint": fingerprint, "action": "key", "status": "sent"},
    )
    again = m.sup.key(body)
    assert again["reason"]["code"] == "request_incomplete"
    assert m.typed() == b""


def test_the_history_holds_who_what_and_when_and_no_transcript(machine):
    m = machine
    m.start()
    at_gate(m)
    m.press("enter")
    running(m)
    m.say("[  1] secret transcript line")
    m.press("success")
    m.hold()
    rows = m.history()
    keys = [r for r in rows if r["event"] == "key"]
    assert [r["key"] for r in keys] == ["enter", "success"]
    for row in rows:
        assert row["actor"] == "person" and row["via"] == "product"
        assert set(row) <= {
            "time",
            "actor",
            "via",
            "event",
            "key",
            "request_id",
            "run_id",
            "state",
            "episode_no",
            "model_id",
            "group",
            "by",
            "code",
            "signal",
            "error",
        }
    text = (m.folder / record.HISTORY).read_text()
    assert str(m.tmp) not in text and "transcript" not in text and PROMPT not in text


# --- hold and end -----------------------------------------------------------------------


def test_hold_freezes_the_group_and_nothing_resumes_it(machine):
    m = machine
    m.start()
    at_gate(m)
    m.press("enter")
    running(m)
    answer = m.hold()
    assert answer == {
        "schema": "levi.marvel0.run_result.v1",
        "held": True,
        "already": False,
        "state": "held",
    }
    assert m.signals == [(m.child.pid, signal.SIGSTOP)]
    view = m.look()
    assert (view["state"], view["reason"]["code"]) == ("held", "held_person")
    assert view["held"]["by"] == "person" and view["may_move"] is False
    assert [a for a, e in view["actions"].items() if e["allowed"]] == ["end"]
    # idempotent: no second signal
    assert m.hold()["already"] is True and len(m.signals) == 1
    # no key, whatever the page asks
    for purpose in ("enter", "label", "exit_home", "end_at_gate"):
        assert refusal(m.sup.challenge, {"purpose": purpose})[1] == "state_not_allowed"


def test_hold_takes_the_run_lock_when_free_and_never_waits_for_it(machine):
    m = machine
    m.start()
    at_gate(m)
    other = os.open(m.hub / run.LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(other, fcntl.LOCK_EX)
    try:
        assert m.hold()["held"] is True, "another instance holds the lock: held anyway"
    finally:
        os.close(other)
    assert m.signals == [(m.child.pid, signal.SIGSTOP)]
    note = json.loads((m.hub / run.LOCK_FILE).read_text() or "{}")
    assert note.get("action") != "hold", "(the other holder's note, or none)"
    # free: the hold takes it (and lets go)
    m.sup._update(held_at=None, held_by=None)
    m.signals.clear()
    m.look()
    m.hold()
    assert json.loads((m.hub / run.LOCK_FILE).read_text())["action"] == "hold"
    assert not flock_busy(m.hub / run.LOCK_FILE)


def test_hold_works_in_every_live_state_and_nowhere_else(machine):
    m = machine
    assert refusal(m.hold)[1] == "state_not_allowed"
    m.start()
    for arrange in (
        lambda: None,
        lambda: m.gate(1),
        lambda: m.set_session("homing", no=1),
        lambda: m.set_session("running", no=1, step=3),
        lambda: m.set_session("stopped", "user_quit"),
    ):
        arrange()
        m.signals.clear()
        m.record()  # (the record is read, never changed, by looking)
        m.sup._update(held_at=None, held_by=None)
        m.look()
        assert m.hold()["held"] is True
        assert m.signals == [(m.child.pid, signal.SIGSTOP)]


def test_end_kills_a_held_client_and_nothing_else_is_sent(machine):
    m = machine
    m.start()
    at_gate(m)
    m.press("enter")
    running(m)
    assert refusal(m.end)[1] == "state_not_allowed", "a running client is held first"
    assert m.signals == []
    m.hold()
    assert refusal(m.sup.end, {"request_id": "req-end-0001", "confirm": "end client"})[
        :2
    ] == (400, "confirm_required")
    answer = m.end()
    assert answer["ended"] is True
    assert m.signals == [(m.child.pid, signal.SIGSTOP), (m.child.pid, signal.SIGKILL)]
    view = m.look()
    assert (view["state"], view["reason"]["code"]) == ("done", "ended_by_page")
    assert view["exit"] == {"code": None, "signal": 9}
    assert not flock_busy(m.tmp / "arm.lock") and m.sup.active() is None
    assert any(n == "orphan_dirs" for n in [x["code"] for x in view["notices"]])


def test_end_is_offered_for_a_lost_client_and_a_client_that_never_reached_the_gate(
    machine,
):
    m = machine
    m.start()
    assert m.view()["actions"]["end"]["allowed"] is True  # starting
    m.end()
    assert m.signals == [(m.child.pid, signal.SIGKILL)]
    assert m.look()["state"] == "done"


def test_only_stop_and_kill_are_ever_sent(machine):
    m = machine
    m.start()
    for sig in (signal.SIGTERM, signal.SIGCONT, signal.SIGINT, signal.SIGHUP, 0):
        with pytest.raises(ValueError):
            m.sup._signal(sig)
    assert m.signals == []


def test_a_signal_cannot_go_to_a_process_that_is_not_the_launcher(machine):
    m = machine
    m.start()
    m.child.exits(0)
    assert m.sup._signal(signal.SIGSTOP) is False and m.signals == []


# --- the watcher --------------------------------------------------------------------------


def test_the_watcher_notices_the_end_and_lets_go(machine):
    m = machine
    m.start()
    at_gate(m)
    m.press("enter")
    running(m)
    m.child.exits(1)
    m.set_session("fault", "exception")
    assert m.sup.tick() is False
    view = m.view()
    assert (view["state"], view["reason"]["code"]) == ("failed", "exit_code")
    assert view["exit"]["code"] == 1
    assert m.record()["phase"] == "ended" and m.sup.tick() is False
    assert not flock_busy(m.tmp / "arm.lock")
    # and a new run can start
    assert m.start()["created"]


def test_the_page_holds_when_the_monitor_shows_red_on_two_distinct_writes(machine):
    m = machine
    m.start()
    at_gate(m)
    m.press("enter")
    running(m)
    m.health(red=True)
    m.look()
    assert m.signals == [], "one red write is not enough"
    m.clock.advance(0.5)
    m.health(red=True)  # the same state, a new write
    m.look()
    assert m.signals == [(m.child.pid, signal.SIGSTOP)]
    view = m.look()
    assert (view["state"], view["reason"]["code"]) == ("held", "held_monitor_red")


def test_the_same_write_read_twice_is_one_write_and_green_resets_the_count(machine):
    m = machine
    m.start()
    at_gate(m)
    m.press("enter")
    running(m)
    m.health(red=True)
    for _ in range(4):
        m.clock.advance(0.4)  # (time passes; the file is the same write)
        m.look()
    assert m.signals == []
    m.clock.advance(0.5)
    m.health(red=False)
    m.look()
    m.clock.advance(0.5)
    m.health(red=True)
    m.look()
    assert m.signals == [], "red, green, red is two reds apart: not consecutive"


def test_red_at_the_gate_blocks_enter_and_does_not_hold(machine):
    m = machine
    m.start()
    at_gate(m)
    for _ in range(3):
        m.clock.advance(0.5)
        m.health(red=True)
        m.look()
    assert m.signals == []
    assert refusal(m.sup.challenge, {"purpose": "enter"})[1] == "monitor_red"
    assert m.press("end_at_gate")["sent"] == "end_at_gate"


def test_red_blocks_a_label_at_the_prompt_but_hold_and_end_stay(machine):
    m = machine
    m.start()
    at_gate(m)
    m.press("enter")
    judging(m)
    m.health(red=True)
    view = m.look()
    assert view["actions"]["success"]["why_not"]["code"] == "monitor_red"
    assert view["actions"]["hold"]["allowed"]
    assert m.signals == [], "the arm is still: no hold of its own"


def test_a_stale_monitor_only_warns_and_blocks_the_next_enter(machine):
    m = machine
    m.start()
    m.health(age=5)
    at_gate(m)
    view = m.view()
    assert view["actions"]["enter"]["why_not"]["code"] == "monitor_not_green"
    assert "monitor_stale" in [n["code"] for n in view["notices"]]
    m.health()
    assert m.view()["actions"]["enter"]["allowed"]
    m.press("enter")
    running(m)
    m.health(age=5)
    view = m.look()
    assert view["actions"]["success"]["allowed"], "a label can still end the episode"
    assert m.signals == []


def test_a_silent_session_freezes_the_client(machine):
    m = machine
    m.start()
    at_gate(m)
    m.press("enter")
    running(m)
    m.clock.advance(60.0)  # the client says nothing for a minute
    m.look()
    assert m.signals == [(m.child.pid, signal.SIGSTOP)]
    assert m.look()["reason"]["code"] == "held_heartbeat_stale"


def test_a_client_that_switched_task_is_frozen(machine):
    """A key that reached the start gate (s: "RUN screws") retargets the
    client's session: its old file closes as stopped/task_changed."""
    m = machine
    m.start()
    at_gate(m)
    m.set_session("stopped", "task_changed", no=1)
    m.look()
    assert m.signals == [(m.child.pid, signal.SIGSTOP)]
    assert m.look()["reason"]["code"] == "held_task_changed"


def test_a_second_connection_to_the_policy_server_freezes_the_client(machine):
    m = machine
    m.start()
    at_gate(m)
    m.press("enter")
    running(m)
    m.write_net(clients=1)
    assert m.look()["clients"] == 1 and m.signals == []
    m.write_net(clients=2)
    m.look()
    assert m.signals == [(m.child.pid, signal.SIGSTOP)]
    assert m.look()["reason"]["code"] == "held_extra_connection"


def test_a_client_that_never_reaches_the_gate_is_ended(machine):
    m = machine
    m.sup.start_timeout_s = 60.0
    m.start()
    m.clock.advance(30)
    assert m.look()["state"] == "starting"
    m.clock.advance(40)
    view = m.look()
    assert view["reason"]["code"] == "no_gate_in_time" or view["state"] == "failed"
    assert m.signals == [(m.child.pid, signal.SIGKILL)]
    view = m.look()
    assert (view["state"], view["reason"]["code"]) == ("failed", "no_gate_in_time")


def test_a_gate_the_page_cannot_confirm_offers_no_key_and_can_be_ended(machine):
    m = machine
    m.start()
    m.set_session("standby", run.GATE_REASON)
    m.say("=== READY (the client's words changed) ===")
    view = m.look()
    assert (view["state"], view["reason"]["code"]) == ("starting", "gate_not_confirmed")
    assert not any(view["actions"][a]["allowed"] for a in run.KEY_RULES)
    assert view["actions"]["end"]["allowed"] and view["actions"]["hold"]["allowed"]


def test_the_background_watcher_runs_and_stops_with_the_run(tmp_path, monkeypatch):
    m = Machine(tmp_path, monkeypatch)
    m.host.background = True
    m.host.sleep = time.sleep
    m.host.monotonic = time.monotonic
    m.host.now = time.time
    monkeypatch.setattr(run, "TICK_S", 0.02)
    try:
        m.health()
        m.start()
        thread = m.sup._watcher
        assert thread is not None and thread.is_alive()
        m.child.exits(0)
        m.set_session("stopped", "user_quit")
        thread.join(5)
        assert not thread.is_alive()
        assert m.record()["phase"] == "ended"
    finally:
        m.close()


# --- after a restart -----------------------------------------------------------------------


def second_service(m):
    """A new process's supervisor on the same workspace, which knows the
    launcher only from the record. The old service is dead: its descriptors
    (the pty master, the arm lock) are gone with it."""
    for name in ("_master", "_arm_fd"):
        fd = getattr(m.sup, name)
        setattr(m.sup, name, None)
        if fd is not None:
            os.close(fd)
    return run.Supervisor(
        m.host, site_loader=lambda: m.site, catalog=m.sup.catalog, workspace=m.workspace
    )


def test_a_client_that_outlived_the_service_is_frozen_and_can_only_be_ended(machine):
    m = machine
    m.start()
    at_gate(m)
    m.press("enter")
    running(m)
    after = second_service(m)
    after.adopt()
    assert m.signals == [(m.child.pid, signal.SIGSTOP)]
    m.sup = after
    view = m.look()
    assert (view["state"], view["reason"]["code"]) == ("held", "held_host_restarted")
    assert [a for a, e in view["actions"].items() if e["allowed"]] == ["end"]
    assert flock_busy(m.tmp / "arm.lock"), "the arm lock is taken again"
    m.end()
    assert m.signals[-1] == (m.child.pid, signal.SIGKILL)
    m.child.exits(-9)
    assert m.look()["state"] == "done"
    for fd in (after._arm_fd,):
        if fd is not None:
            os.close(fd)


def test_a_record_whose_launcher_is_gone_ends_as_a_failed_run(machine):
    m = machine
    m.start()
    at_gate(m)
    m.children.clear()  # no process answers to the recorded identity any more
    after = second_service(m)
    m.sup = after
    view = m.view()
    assert (view["state"], view["reason"]["code"]) == ("failed", "host_restarted")
    assert m.signals == []
    assert after.active() is None
    assert m.start()["created"]


def test_a_reused_pid_is_not_signalled(machine):
    m = machine
    m.start()
    after = second_service(m)
    m.host.identity = lambda pid: {"start_ticks": "other", "boot": "b"}
    after.adopt()
    assert m.signals == []
    assert after._rec["phase"] == "ended"


def test_a_signal_after_a_restart_checks_the_launcher_is_still_the_same_process(
    machine,
):
    # (the take-over looked once; a signal sent later must look again: the
    # launcher may have gone and its pid been given to another process)
    m = machine
    m.start()
    at_gate(m)
    after = second_service(m)
    after.adopt()
    held = [(m.child.pid, signal.SIGSTOP)]
    assert m.signals == held
    m.host.identity = lambda pid: {"start_ticks": "other", "boot": "b"}
    assert after._signal(signal.SIGKILL) is False
    assert m.signals == held, "nothing went to the pid's new owner"
    if after._arm_fd is not None:
        os.close(after._arm_fd)


def test_an_intent_without_a_launch_is_over_after_a_restart(machine):
    m = machine
    m.start()
    rec = dict(m.record())
    rec.pop("leader")
    rec["phase"] = "intent"
    record.write_record(m.folder, rec)
    after = second_service(m)
    after.adopt()
    assert after._rec["phase"] == "ended" and m.signals == []


def test_a_service_without_a_workspace_run_assumes_nothing(machine):
    m = machine
    after = second_service(m)
    after.adopt()
    assert after.view()["state"] == "idle" and after.active() is None


def test_the_job_row_for_levi_stop(machine):
    m = machine
    assert m.sup.active() is None
    m.start()
    m.clock.advance(12)
    job = m.sup.active()
    assert job["kind"] == "marvel0-client" and job["id"] == m.record()["run_id"]
    assert 11 <= job["running_seconds"] <= 13
    m.end()
    assert m.sup.active() is None


def test_the_prune_keeps_the_newest_logs(machine):
    m = machine
    m.start()
    logs = m.folder / "logs"
    for n in range(15):
        for suffix in (".log", ".exit.json", ".spec.json"):
            (logs / f"client-2026010{n % 9 + 1}T0000{n:02d}Z{suffix}").write_text("x")
    m.child.exits(0)
    m.set_session("stopped", "user_quit")
    m.look()
    stems = {p.name.split(".")[0] for p in logs.iterdir()}
    assert len(stems) <= run.LOG_KEEP + 1
    assert m.record()["log_file"].split(".")[0] in stems


def test_the_threads_of_a_run_are_not_leaked(machine):
    m = machine
    m.start()
    names = [t.name for t in threading.enumerate()]
    assert "levi-marvel0-run" not in names, (
        "no watcher thread without a background host"
    )


# --- more on the token and the replay ------------------------------------------------------------


def test_any_change_of_state_voids_the_tokens_of_the_state_before(machine):
    m = machine
    m.start()
    at_gate(m)
    m.press("enter")
    m.typed()
    running(m)
    token = m.sup.challenge({"purpose": "label"})["challenge"]
    judging(m)  # the step limit was reached: a new state, the same kind of key
    body = {
        "request_id": "req-voided-0001",
        "action": "success",
        "challenge": token,
        "confirm": "",
    }
    assert refusal(m.sup.key, body)[1] == "challenge_stale"
    assert m.typed() == b""
    assert m.press("success")["sent"] == "success", "a token asked for now works"


def test_a_run_that_ends_leaves_no_token_open(machine):
    m = machine
    m.start()
    at_gate(m)
    m.sup.challenge({"purpose": "enter"})
    assert m.view()["armed"]["purpose"] == "enter"
    m.child.exits(1)
    m.set_session("fault", "exception")
    view = m.look()
    assert view["state"] == "failed" and view["armed"] is None


def test_the_open_token_is_shown_by_purpose_and_never_by_value(machine):
    m = machine
    m.start()
    at_gate(m)
    assert m.view()["armed"] is None
    token = m.sup.challenge({"purpose": "enter"})
    armed = m.view()["armed"]
    assert armed["purpose"] == "enter" and armed["expires_at"] == token["expires_at"]
    assert token["challenge"] not in json.dumps(m.view())
    sent = m.sup.key(
        {
            "request_id": "req-armed-0001",
            "action": "enter",
            "challenge": token["challenge"],
            "confirm": "",
        }
    )
    assert sent["sent"] == "enter"
    assert m.view()["armed"] is None, "a used token is not open"


def test_the_replay_fingerprint_drops_the_token_and_the_checklist():
    base = {"request_id": "req-0001-abcd", "form": {"a": 1}, "confirm": "x"}
    one = {**base, "challenge": "t1", "attest": {"estop": True}}
    two = {**base, "challenge": "t2", "attest": {"estop": True, "extra": True}}
    other = {**base, "form": {"a": 2}, "challenge": "t1", "attest": {"estop": True}}
    fp = lambda body: record.fingerprint("start", run._fingerprint_body(body))
    assert fp(one) == fp(two)
    assert fp(one) != fp(other)
    assert record.fingerprint("start", one) != record.fingerprint("key", one)
