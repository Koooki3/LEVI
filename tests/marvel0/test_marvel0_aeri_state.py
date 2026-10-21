"""``levi/marvel0/aeri_state.py``: which state terminal D is in and which keys
the page may press for it. Pure: the facts are written by the test.

The timeline walks the **real** run of 18 Oct (r08): the journal, the screen,
the service steps of the recording, cut at the moments that matter; the rest
(waits, faults, stalls, disagreeing sources, stops) is built from the real
templates and journal line shapes. A state that is wrong here would press a
key in front of the arm."""

import json
from pathlib import Path

import pytest
from test_marvel0_aeri_journal import FIX, run_dir
from test_marvel0_aeri_screen import wait_text

from levi.marvel0 import aeri_journal as jr
from levi.marvel0 import aeri_screen as sc
from levi.marvel0 import aeri_state as st

T0 = 1_792_549_800.0
NAME = "aeri-20261018-ext-r08"
SECRET = b"s" * 16
SCREEN = (FIX / "screen.txt").read_bytes()
PROFILE = st.PROFILES["harvest-5bc750ca"]
HERE = Path(__file__).resolve().parent


def cut(marker: str) -> bytes:
    text = SCREEN.decode()
    at = text.index(marker)
    end = text.find("\r\n", at)
    if marker.startswith("请输入"):
        return text[: at + len(marker)].encode()
    return text[: end + 2 if end >= 0 else len(text)].encode()


RECORD = {
    "schema": st.RECORD_SCHEMA,
    "run_id": "20261021T101500-a1b2c3",
    "name": NAME,
    "mode": "external",
    "task": "pick the pepper into the white plate",
    "rounds": 5,
    "steps": 200,
    "phase": "started",
    "profile": "harvest-5bc750ca",
    "console_lang": "zh",
}


class Rig:
    """Facts written by hand and looked at the way the supervisor looks."""

    def __init__(self, tmp_path, profile=PROFILE, record=None, lang="zh"):
        self.tmp = Path(tmp_path)
        self.profile = profile
        self.lex = sc.lexicon(profile.id, lang)
        self.now = T0
        self.mem = st.Memory()
        self.record = dict(RECORD if record is None else record)
        self.n = 0
        self.last = None

    def jview(self, upto=None, extra=()) -> jr.JView:
        self.n += 1
        folder = run_dir(self.tmp / f"j{self.n}", upto)
        if extra:
            with open(folder / jr.JOURNAL, "ab") as handle:
                handle.writelines(json.dumps(row).encode() + b"\n" for row in extra)
        return jr.JournalReader(folder / jr.JOURNAL, folder).poll()

    def screen(self, data) -> sc.ScreenView:
        buf = sc.ScreenBuf()
        buf.feed(data if isinstance(data, bytes) else data.encode())
        return sc.read(buf, self.lex)

    def facts(self, j=None, screen=b"", **kw) -> st.Facts:
        f = st.Facts(
            now=self.now,
            record=dict(self.record),
            profile=self.profile,
            alive=True,
            keyboard=True,
            j=j if j is not None else jr.JView(),
            screen=self.screen(screen),
            runner=jr.RView(present=True, state="running", pid=1),
            runner_age_s=1.0,
            runner_seen=True,
            icanon=False,
            started_age_s=30.0,
            external=True,
        )
        for key, value in kw.items():
            setattr(f, key, value)
        return f

    def look(self, j=None, screen=b"", settle=True, **kw) -> st.Derived:
        """One look; with ``settle`` a second one 1.2 s later on the same facts."""
        f = self.facts(j, screen, **kw)
        d = st.derive(f, self.mem)
        self.mem = d.mem
        if settle:
            self.now += 1.2
            f.now = self.now
            d = st.derive(f, self.mem)
            self.mem = d.mem
        self.last = f
        return d

    def keys(self, d, **ctx) -> list:
        c = st.Context(
            now=self.now,
            secret=SECRET,
            seq=ctx.pop("seq", 7),
            run_id=self.record["run_id"],
            presence_age_s=ctx.pop("presence_age_s", 1.0),
            **ctx,
        )
        return st.offer(d, self.last, c)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def allowed(keys) -> list:
    return [k["id"] for k in keys if k["allowed"]]


# --- the real run, moment by moment ----------------------------------------------------------


def test_services_before_anything_is_committed(rig):
    d = rig.look(rig.jview(12), cut("6/7 各轮"), icanon=True)
    assert (d.state, d.reason) == ("services", "service_step")
    assert d.prompt is None and rig.keys(d) == [] and not d.may_move
    d = rig.look(rig.jview(13), cut("6/7 各轮"), icanon=True)
    assert (d.state, d.reason) == ("services", "policy_warmup")


@pytest.mark.parametrize(
    ("marker", "state", "action"),
    [
        ("首轮回位会打开夹爪", "await_home_o", "home_o"),
        ("检查机械臂返回 Home", "await_home_p", "home_p"),
        ("确认知道回位会开爪", "await_home_h", "home_h"),
    ],
)
def test_the_three_homing_keys_come_one_prompt_at_a_time(rig, marker, state, action):
    d = rig.look(rig.jview(14), cut(marker))
    assert d.state == state and d.settled and d.prompt["kind"] == action
    keys = rig.keys(d)
    assert allowed(keys) == [action, "quit"]
    key = keys[0]
    assert key["slot"] == ("first" if action == "home_o" else "second")
    assert key["level"] == "B"
    assert key["motion"] == ("arm_moves" if action == "home_h" else "none")
    assert d.may_move is (action == "home_h")


def test_the_homing_prompt_is_not_offered_before_it_has_settled(rig):
    d = rig.look(rig.jview(14), cut("首轮回位会打开夹爪"), settle=False)
    assert d.state == "await_home_o" and not d.settled
    keys = rig.keys(d)
    assert allowed(keys) == []
    assert [k["why_not"]["code"] for k in keys] == ["not_settled", "not_settled"]


def test_while_the_first_home_runs_the_arm_moves_and_no_key_is_offered(rig):
    d = rig.look(rig.jview(15), cut("确认知道回位会开爪"))
    assert (d.state, d.reason, d.may_move) == ("closing", "startup_home", True)
    assert rig.keys(d) == []


def test_after_the_home_is_committed_d_prepares_and_nothing_waits(rig):
    d = rig.look(rig.jview(22), cut("Home verified"))
    assert d.state == "busy" and d.prompt is None


def test_the_object_list_waits_for_y_n_or_q(rig):
    d = rig.look(rig.jview(25), cut("清单对就按 y 确认"))
    assert d.state == "await_inventory" and d.prompt["kind"] == "inventory"
    assert d.inventory["confirmed"] is False and len(d.inventory["items"]) == 4
    keys = rig.keys(d)
    assert allowed(keys) == ["inventory_yes", "inventory_no", "quit"]
    by = {k["id"]: k for k in keys}
    assert by["inventory_yes"]["level"] == "B" and by["inventory_no"]["level"] == "A"
    assert by["quit"]["byte"] == "q" and by["inventory_yes"]["byte"] == "y"


def test_enter_is_never_offered_at_the_object_list(rig):
    d = rig.look(rig.jview(25), cut("清单对就按 y 确认"))
    assert not {"release_round", "resume_ask"} & {k["id"] for k in rig.keys(d)}


def test_a_confirmed_list_with_no_release_prompt_yet_is_awaited_not_guessed(rig):
    d = rig.look(rig.jview(26), cut("清单已确认"))
    assert (d.state, d.reason) == ("busy", "awaiting_prompt") and d.prompt is None


def test_the_release_prompt_of_round_one_and_two(rig):
    d = rig.look(rig.jview(26), cut("第 1/5 轮：确认场景就绪"))
    assert d.state == "await_release" and d.prompt["n"] == 1 and d.prompt["total"] == 5
    assert d.inventory["confirmed"] is True
    keys = rig.keys(d)
    assert allowed(keys) == ["release_round", "quit"]
    assert keys[0]["motion"] == "arm_moves" and keys[0]["byte"] == "Enter"
    # round 2: the second prompt of the real recording (the end of the file)
    d = rig.look(rig.jview(93), cut("第 2/5 轮：确认场景就绪"))
    assert d.state == "await_release" and d.prompt["n"] == 2
    assert d.progress["done"] == 1 and d.progress["round"] == 2


def test_a_released_round_is_planning_until_the_forward_loop_starts(rig):
    d = rig.look(rig.jview(27), cut("第 1/5 轮已放行"))
    assert d.state == "busy" and d.reason == "planning" and rig.keys(d) == []


def test_the_forward_loop_offers_the_three_verdicts_and_nothing_else(rig):
    d = rig.look(rig.jview(34), cut("当前正向子任务 步 66/200"))
    assert (d.state, d.may_move) == ("forward_running", True)
    assert d.prompt["id"].startswith("round:aeri-20261018-ext-r08:round1")
    assert (d.progress["step"], d.progress["max_steps"]) == (66, 200)
    keys = rig.keys(d)
    assert allowed(keys) == ["verdict_success", "verdict_failure", "verdict_discard"]
    assert all(k["slot"] == "second" and k["motion"] == "arm_moves" for k in keys)


def test_without_the_hint_on_the_screen_the_forward_loop_offers_nothing(rig):
    d = rig.look(rig.jview(34), cut("第 1/5 轮已放行"))
    assert (d.state, d.reason) == ("busy", "awaiting_prompt")
    assert rig.keys(d) == []


def test_after_a_verdict_on_the_screen_the_round_is_latched_and_offers_nothing(rig):
    text = cut("当前正向子任务 步 66/200").decode() + (
        "[第 1/5 轮] 已收到人工判定：成功；本轮提前结束，回位后继续下一轮\r\n"
    )
    d = rig.look(rig.jview(34), text)
    # (the hint is no longer D's last line)
    assert d.progress["verdict_latched"] is True
    assert allowed(rig.keys(d)) == []


def test_a_verdict_in_the_journal_latches_the_round(rig):
    verdict = {
        "sequence_no": 34,
        "record": "note",
        "note": {"code": "operator_verdict", "detail": '{"label":"success"}'},
    }
    d = rig.look(rig.jview(34, extra=[verdict]), cut("当前正向子任务 步 66/200"))
    assert d.state == "forward_running" and d.progress["verdict_latched"] is True
    by = {k["id"]: k for k in rig.keys(d)}
    assert all(k["why_not"]["code"] == "verdict_once" for k in by.values())


def test_a_verdict_this_page_sent_latches_the_round_before_d_answers(rig):
    d = rig.look(
        rig.jview(34),
        cut("当前正向子任务 步 66/200"),
        verdict_round_id="aeri-20261018-ext-r08:round1:s26",
    )
    assert d.progress["verdict_latched"] is True and allowed(rig.keys(d)) == []


def test_the_forward_loop_closing_and_the_reverse_loop_offer_no_key(rig):
    d = rig.look(rig.jview(37), cut("当前正向子任务结束"))
    assert (d.state, d.reason, d.may_move) == ("closing", "forward_stopping", True)
    d = rig.look(rig.jview(45), cut("当前正向子任务结束"))
    assert (d.state, d.reason) == ("closing", "robot_home")
    d = rig.look(rig.jview(59), cut("[第 1/5 轮] reset 策略正在复位环境"))
    assert (d.state, d.may_move) == ("reverse_running", True)
    assert rig.keys(d) == [] and d.prompt is None


def test_d_that_has_completed_is_winding_down_and_stop_is_not_offered(rig):
    d = rig.look(rig.jview(97), cut("7/7 收尾"))
    assert (d.state, d.reason) == ("stopping", "completing")
    assert d.winding and d.stop_open is False and d.stop_phase == "idle"
    assert st.end_entry(d)["allowed"] is False


def test_a_run_that_is_over_is_ended_with_its_exit(rig):
    rig.record["phase"] = "ended"
    d = rig.look(rig.jview(97), SCREEN, alive=False, exit={"code": 0, "signal": None})
    assert (d.state, d.reason) == ("ended", "exit_0")
    assert d.alive is False and d.stop_open is False


@pytest.mark.parametrize(
    ("exit_", "reason"),
    [
        ({"code": 0, "signal": None}, "exit_0"),
        ({"code": 1, "signal": None}, "exit_1"),
        ({"code": 2, "signal": None}, "exit_2"),
        ({"code": 130, "signal": None}, "exit_130"),
        ({"code": None, "signal": 9}, "exit_signal"),
        ({"code": 97, "signal": None}, "exit_other"),
        (None, "exit_unknown"),
        ({"code": None, "signal": None}, "exit_unknown"),
    ],
)
def test_how_d_ended(rig, exit_, reason):
    rig.record["phase"] = "ended"
    d = rig.look(None, b"", alive=False, exit=exit_)
    assert (d.state, d.reason) == ("ended", reason)


def test_no_run_is_idle(rig):
    for record in (None, {}, {"phase": "never"}):
        f = rig.facts()
        f.record = record
        d = st.derive(f, st.Memory())
        assert (d.state, d.reason) == ("idle", "no_run") and not d.alive


# --- the phrase, and a run that has not shown anything yet ------------------------------------


def test_the_phrase_is_a_line_not_a_key(rig):
    text = f"请输入 execute {NAME}："
    d = rig.look(None, text, icanon=True)
    assert d.state == "await_phrase" and d.prompt["kind"] == "phrase"
    keys = rig.keys(d)
    assert allowed(keys) == ["execute"]
    assert keys[0]["level"] == "C" and keys[0]["phrase"] == f"execute {NAME}"
    assert keys[0]["motion"] == "may_move"


def test_the_phrase_wants_a_canonical_terminal_and_the_keys_a_cbreak_one(rig):
    text = f"请输入 execute {NAME}："
    d = rig.look(None, text, icanon=False, settle=False)
    assert d.state == "unknown" or d.state == "await_phrase"
    d = rig.look(rig.jview(25), cut("清单对就按 y 确认"), icanon=True, settle=False)
    assert d.state == "busy" and d.agree["termios"] == "canonical"


def test_a_phrase_for_another_run_is_not_offered(rig):
    d = rig.look(None, "请输入 execute aeri-20261018-ext-r99：", icanon=True)
    assert d.state == "unknown" and d.reason == "source_conflict"


def test_the_policy_switch_phrase_is_offered_only_as_its_own_key(rig):
    text = f"请输入 execute {NAME} after policy switch check："
    d = rig.look(None, text, icanon=True)
    assert d.state == "await_phrase_variant"
    keys = rig.keys(d)
    by = {k["id"]: k for k in keys}
    assert by["execute_variant"]["allowed"] and by["execute_variant"]["phrase"] == (
        f"execute {NAME} after policy switch check"
    )
    assert by["execute"]["allowed"] is False
    assert by["execute"]["why_not"]["code"] == "swap_unclean"


def test_a_launch_that_has_shown_nothing_is_launching_and_later_unknown(rig):
    d = rig.look(None, b"", icanon=True, started_age_s=3.0)
    assert (d.state, d.reason) == ("launching", "starting")
    d = rig.look(None, b"", icanon=True, started_age_s=st.START_TIMEOUT_S + 1)
    assert (d.state, d.reason) == ("unknown", "start_timeout")


# --- waits --------------------------------------------------------------------------------------


def waited(reason, extra=()):
    return [
        {
            "sequence_no": 34,
            "record": "committed",
            "from_state": "VERIFY_INITIAL",
            "to_state": "WAIT_HUMAN",
            "reason": reason,
        },
        *extra,
    ]


def test_a_wait_that_needs_a_second_step_asks_first_without_moving(rig):
    d = rig.look(
        rig.jview(21, extra=waited("operator_stop")),
        wait_text("harvest-5bc750ca", "operator_stop"),
    )
    assert d.state == "await_resume" and d.variant == "confirm_reason"
    assert d.wait["confirm"] is True and d.wait["enter"] is True
    keys = rig.keys(d)
    assert allowed(keys) == ["resume_ask", "quit"]
    ask = keys[0]
    assert (ask["level"], ask["motion"], ask["variant"]) == (
        "A",
        "none",
        "confirm_reason",
    )


def test_a_wait_that_resumes_on_enter_alone_is_a_key_that_may_move_the_arm(rig):
    d = rig.look(
        rig.jview(21, extra=waited("subtask_not_released")),
        wait_text("harvest-5bc750ca", "subtask_not_released"),
    )
    assert (
        d.state == "await_resume"
        and d.variant == "direct"
        and d.wait["confirm"] is False
    )
    ask = rig.keys(d)[0]
    assert (ask["level"], ask["motion"], ask["variant"]) == ("B", "may_move", "direct")


def test_a_wait_that_asks_to_end_the_run_offers_only_q(rig):
    d = rig.look(
        rig.jview(21, extra=waited("startup_home_not_executed")),
        wait_text("harvest-5bc750ca", "startup_home_not_executed"),
    )
    assert d.state == "await_resume" and d.wait["enter"] is False
    assert allowed(rig.keys(d)) == ["quit"]


def test_the_wait_carries_whether_the_scene_must_be_put_right_first(rig):
    d = rig.look(
        rig.jview(21, extra=waited("scene_reset_required")),
        wait_text("harvest-5bc750ca", "scene_reset_required"),
    )
    assert d.wait["restore_scene"] is True
    d = rig.look(
        rig.jview(21, extra=waited("operator_stop")),
        wait_text("harvest-5bc750ca", "operator_stop"),
    )
    assert d.wait["restore_scene"] is False


def test_the_second_step_answers_y_or_n_and_enter_is_gone(rig):
    texts = sc.prompts()["profiles"]["harvest-5bc750ca"]["texts"]["zh"]
    screen = (
        wait_text("harvest-5bc750ca", "operator_stop")
        + "[第 1/5 轮] "
        + texts["confirm_resume"]
        + "\r\n"
    )
    d = rig.look(rig.jview(21, extra=waited("operator_stop")), screen)
    assert d.state == "await_resume_confirm"
    keys = rig.keys(d)
    assert allowed(keys) == ["resume_confirm", "resume_cancel", "quit"]
    assert {"resume_ask", "release_round"}.isdisjoint({k["id"] for k in keys})
    by = {k["id"]: k for k in keys}
    assert (
        by["resume_confirm"]["slot"] == "second"
        and by["resume_confirm"]["level"] == "B"
    )


def test_a_wait_on_the_screen_that_is_not_the_journals_is_not_trusted(rig):
    j = rig.jview(21, extra=waited("operator_stop"))
    line = wait_text("harvest-5bc750ca", "policy_error")
    d = rig.look(j, line, settle=False)
    assert d.state == "busy" and d.reason == "awaiting_prompt"
    rig.now += st.DRIFT_S + 1
    d = rig.look(j, line, settle=False)
    assert d.state == "unknown"


def test_a_wait_the_journal_has_and_the_screen_has_not_shown_yet_is_awaited(rig):
    d = rig.look(rig.jview(21, extra=waited("operator_stop")), b"", settle=False)
    assert (d.state, d.reason) == ("busy", "awaiting_prompt")


# --- sources that disagree ----------------------------------------------------------------------


def test_a_prompt_the_journal_expects_and_the_screen_never_shows_turns_unknown(rig):
    j = rig.jview(26)
    d = rig.look(j, cut("清单已确认"), settle=False)
    assert d.state == "busy"
    rig.now += st.DRIFT_S + 1
    d = rig.look(j, cut("清单已确认"), settle=False)
    assert (d.state, d.reason) == ("unknown", "source_conflict")
    assert rig.keys(d) == []


def test_a_changed_sentence_is_drift_not_a_prompt(rig):
    j = rig.jview(26)
    changed = (
        cut("第 1/5 轮：确认场景就绪").decode().replace("按 Enter 放行", "请按回车放行")
    )
    d = rig.look(j, changed, settle=False)
    assert d.state == "busy" and d.prompt is None
    rig.now += st.DRIFT_S + 1
    d = rig.look(j, changed, settle=False)
    assert (d.state, d.reason) == ("unknown", "screen_drift")
    assert d.agree["screen"] == "drift" and d.may_move is True


def test_a_list_on_the_screen_that_is_not_the_journals_is_refused(rig):
    other = (
        cut("清单对就按 y 确认").decode().replace("purple_eggplant_1", "green_cup_1")
    )
    d = rig.look(rig.jview(25), other)
    assert (d.state, d.reason) == ("unknown", "inventory_mismatch")
    assert rig.keys(d) == []


def test_a_list_the_journal_cannot_name_is_refused(rig):
    j = rig.jview(25)
    j = jr.JView(**{**j.__dict__, "inventory_ids": None})
    d = rig.look(j, cut("清单对就按 y 确认"))
    assert (d.state, d.reason) == ("unknown", "inventory_mismatch")


def test_a_release_prompt_of_another_total_is_refused(rig):
    other = cut("第 1/5 轮：确认场景就绪").decode().replace("第 1/5 轮", "第 1/9 轮")
    d = rig.look(rig.jview(26), other)
    assert (d.state, d.reason) == ("unknown", "source_conflict")


def test_a_refused_start_pose_means_no_release_prompt(rig):
    refused = {
        "sequence_no": 26,
        "record": "note",
        "note": {"code": "start_pose_checked", "detail": "refused: 40 mm from home"},
    }
    d = rig.look(
        rig.jview(26, extra=[refused]), cut("第 1/5 轮：确认场景就绪"), settle=False
    )
    assert d.state == "busy" and d.prompt is None


def test_a_transaction_in_flight_is_no_release_prompt(rig):
    prepared = {
        "sequence_no": 26,
        "record": "prepared",
        "from_state": "VERIFY_INITIAL",
        "to_state": "FORWARD_ACTIVE",
        "reason": "subtask_dispatched",
    }
    d = rig.look(
        rig.jview(26, extra=[prepared]), cut("第 1/5 轮：确认场景就绪"), settle=False
    )
    assert d.state == "busy" and d.prompt is None


def test_a_heartbeat_that_stopped_while_d_lives_is_unknown(rig):
    d = rig.look(
        rig.jview(26),
        cut("第 1/5 轮：确认场景就绪"),
        runner_age_s=st.RUNNER_STALE_S + 1,
    )
    assert (d.state, d.reason) == ("unknown", "runner_stale") and d.may_move
    assert rig.keys(d) == []


def test_a_heartbeat_that_was_never_seen_does_not_turn_the_page_careful(rig):
    d = rig.look(
        rig.jview(26),
        cut("第 1/5 轮：确认场景就绪"),
        runner_seen=False,
        runner_age_s=None,
    )
    assert d.state == "await_release"


def test_without_the_keyboard_or_the_termios_there_is_no_key(rig):
    j = rig.jview(26)
    d = rig.look(j, cut("第 1/5 轮：确认场景就绪"), keyboard=False)
    assert (d.state, d.reason) == ("unknown", "no_keyboard")
    d = rig.look(j, cut("第 1/5 轮：确认场景就绪"), icanon=None)
    assert (d.state, d.reason) == ("unknown", "termios_unreadable")


def test_a_pty_in_the_wrong_mode_is_waited_for_and_then_refused(rig):
    j = rig.jview(26)
    d = rig.look(j, cut("第 1/5 轮：确认场景就绪"), icanon=True, settle=False)
    assert (d.state, d.reason) == ("busy", "awaiting_prompt")
    assert d.agree["termios"] == "canonical"
    rig.now += st.DRIFT_S + 1
    d = rig.look(j, cut("第 1/5 轮：确认场景就绪"), icanon=True, settle=False)
    assert (d.state, d.reason) == ("unknown", "termios_mismatch")


def test_a_run_whose_profile_is_missing_is_unknown(rig):
    d = rig.look(rig.jview(26), SCREEN, profile=None)
    assert (d.state, d.reason) == ("unknown", "profile_missing")


def test_a_startup_prompt_on_a_profile_without_startup_home_gives_no_key(tmp_path):
    rig = Rig(tmp_path, profile=st.PROFILES["bfdee2cf"])
    d = rig.look(rig.jview(14), cut("首轮回位会打开夹爪"))
    assert allowed(rig.keys(d)) == []


# --- stop, stall, fault, orphan --------------------------------------------------------------


def test_stop_is_open_in_every_live_state_that_is_not_winding_down(rig):
    cases = [
        (rig.jview(26), cut("第 1/5 轮：确认场景就绪"), {}),
        (rig.jview(34), cut("当前正向子任务 步 66/200"), {}),
        (rig.jview(37), cut("当前正向子任务结束"), {}),
        (rig.jview(59), cut("[第 1/5 轮] reset 策略正在复位环境"), {}),
        (rig.jview(22), cut("Home verified"), {}),
        (None, b"", {"icanon": True}),
        (rig.jview(26), b"gibberish", {}),
    ]
    for j, screen, kw in cases:
        d = rig.look(j, screen, **kw)
        assert d.stop_open is True, d.state


def test_stop_is_open_in_unknown_too(rig):
    d = rig.look(rig.jview(26), cut("第 1/5 轮：确认场景就绪"), runner_age_s=99.0)
    assert d.state == "unknown" and d.stop_open is True


def test_after_a_stop_the_run_is_stopping_until_d_acknowledges_it(rig):
    j = rig.jview(34)
    d = rig.look(
        j, cut("当前正向子任务 步 66/200"), stop_written="sent", stop_age_s=1.0
    )
    assert (d.state, d.reason, d.stop_phase) == ("stopping", "stop_sent", "sent")
    assert d.winding and d.stop_open is False and rig.keys(d) == []
    assert st.end_entry(d)["allowed"] is False, "End is for a wrap-up that is stuck"
    interrupted = (
        cut("当前正向子任务 步 66/200").decode()
        + "收到 SIGINT（Ctrl+C）：已停止下发运动指令，正在安全收尾，不会再开始新的一轮。\r\n"
    )
    d = rig.look(j, interrupted, stop_written="sent", stop_age_s=2.0)
    assert d.stop_phase == "acked"


def test_the_note_of_the_interrupt_is_an_acknowledgement_too(rig):
    note = {
        "sequence_no": 34,
        "record": "note",
        "note": {"code": "operator_interrupt", "detail": "SIGINT while running"},
    }
    d = rig.look(rig.jview(34, extra=[note]), b"", stop_written="sent", stop_age_s=1.0)
    assert d.stop_phase == "acked"


def test_an_unacknowledged_stop_opens_one_retry_and_only_when_the_handlers_exist(rig):
    j = rig.jview(34)
    screen = cut("当前正向子任务 步 66/200")
    d = rig.look(j, screen, stop_written="sent", stop_age_s=st.STOP_ACK_S + 1)
    assert d.stop_phase == "unconfirmed" and d.stop_open is True
    d = rig.look(j, screen, stop_written="retried", stop_age_s=st.STOP_ACK_S + 9)
    assert d.stop_phase == "retried" and d.stop_open is False
    # before the heartbeat existed SIGINT was a KeyboardInterrupt: no retry
    d = rig.look(
        j,
        screen,
        stop_written="sent",
        stop_age_s=st.STOP_ACK_S + 1,
        runner_seen=False,
        runner_age_s=None,
    )
    assert d.stop_phase == "sent" and d.stop_open is False


def test_a_winding_down_run_without_progress_is_stalled_and_end_opens(rig):
    j = rig.jview(34)
    screen = cut("当前正向子任务 步 66/200")
    d = rig.look(j, screen, stop_written="sent", stop_age_s=1.0, settle=False)
    assert d.state == "stopping"
    rig.now += st.STALL_S - 5
    d = rig.look(j, screen, stop_written="sent", stop_age_s=140.0, settle=False)
    assert d.state == "stopping"
    rig.now += 6
    d = rig.look(j, screen, stop_written="sent", stop_age_s=150.0, settle=False)
    assert (d.state, d.reason, d.stalled) == ("stalled", "no_progress", True)
    doc_end = st.end_entry(d)
    assert doc_end["allowed"] is True and doc_end["phrase"] == "end terminal D"


def test_the_heartbeat_is_not_progress(rig):
    """The heartbeat thread keeps writing while a stuck main thread does not
    move: a younger ``runner.json`` must not postpone the stall."""
    j = rig.jview(34)
    screen = cut("当前正向子任务 步 66/200")
    rig.look(j, screen, stop_written="sent", stop_age_s=1.0, settle=False)
    d = None
    for _ in range(4):
        rig.now += 40
        d = rig.look(
            j,
            screen,
            stop_written="sent",
            stop_age_s=rig.now - T0,
            runner_age_s=0.5,
            # (the heartbeat file is rewritten every time: a new stamp)
            runner=jr.RView(
                present=True,
                state="running",
                pid=1,
                updated_wall_ns=int(rig.now * 1e9),
            ),
            settle=False,
        )
    assert d.state == "stalled"


def test_a_journal_line_a_service_step_or_a_new_screen_line_is_progress(rig):
    screen = cut("当前正向子任务 步 66/200")
    base = {"stop_written": "sent", "settle": False}
    rig.look(rig.jview(34), screen, stop_age_s=1.0, **base)
    rig.now += 100
    d = rig.look(rig.jview(36), screen, stop_age_s=100.0, **base)  # a journal line
    rig.now += 100
    d = rig.look(rig.jview(36), screen, stop_age_s=200.0, **base)
    assert d.state == "stopping", "100 s since the journal line"
    rig.now += 100
    more = screen + "   当前正向子任务 步 70/200\r\n".encode()
    d = rig.look(rig.jview(36), more, stop_age_s=300.0, **base)  # a new screen line
    assert d.state == "stopping"
    rig.now += 100
    d = rig.look(
        rig.jview(36), more, stop_age_s=400.0, steps=jr.SView(steps=99), **base
    )
    assert d.state == "stopping", "a service step"


def test_an_external_answer_awaited_postpones_the_stall(rig):
    j = rig.jview(34)
    screen = cut("当前正向子任务 步 66/200")
    kw = {
        "stop_written": "sent",
        "queue_pending": 1,
        "queue_oldest_s": 100.0,
        "settle": False,
    }
    rig.look(j, screen, stop_age_s=1.0, **kw)
    rig.now += st.STALL_S + 10
    d = rig.look(j, screen, stop_age_s=200.0, **kw)
    assert d.state == "stopping", "an answer is awaited (up to 540 s)"
    rig.now += st.STALL_QUEUE_S
    d = rig.look(j, screen, stop_age_s=900.0, **kw)
    assert d.state == "stalled"


def test_a_locked_run_offers_no_key_and_no_stop(rig):
    locked = [
        {
            "sequence_no": 34,
            "record": "committed",
            "from_state": "FORWARD_ACTIVE",
            "to_state": "FAULT_LOCKED",
            "reason": "policy_error",
        }
    ]
    screen = "运行已锁定 FAULT_LOCKED：policy_error\r\n"
    d = rig.look(rig.jview(34, extra=locked), screen)
    assert (d.state, d.reason) == ("fault_locked", "fault_locked")
    assert d.params == {"code": "policy_error"}
    assert d.stop_open is False and rig.keys(d) == [] and d.winding
    assert st.end_entry(d)["allowed"] is False
    rig.now += st.STALL_S + 1
    d = rig.look(rig.jview(34, extra=locked), screen, settle=False)
    assert d.state == "stalled"


def test_a_run_found_again_after_a_restart_is_orphaned_without_a_key(rig):
    d = rig.look(
        rig.jview(26),
        cut("第 1/5 轮：确认场景就绪"),
        adopted=True,
        keyboard=False,
        stop_written="sent",
        stop_age_s=3.0,
    )
    assert (d.state, d.reason) == ("orphaned", "adopted_after_restart")
    assert rig.keys(d) == [] and d.stop_open is False


# --- the keys, as a table -----------------------------------------------------------------------


def test_the_bytes_the_page_can_write_are_a_closed_set():
    assert st.ALL_BYTES == {
        b"\x03",
        b"\r",
        b"y",
        b"n",
        b"q",
        b"o",
        b"p",
        b"h",
        b"s",
        b"f",
        b"d",
    }
    for forbidden in (
        b"\x04",
        b"\x1a",
        b"\x1c",
        b"\x1b",
        b"Y",
        b"N",
        b"Q",
        b"m",
        b"u",
        b"1",
    ):
        assert forbidden not in st.ALL_BYTES
    wires = {k.wire for k in st.KEYS.values() if k.wire is not None}
    assert wires | {st.STOP_BYTE} == st.ALL_BYTES
    assert set(st.KEYS) == set(st.ACTIONS)


def test_each_action_answers_only_its_own_prompt_kinds():
    for spec in st.KEYS.values():
        assert spec.kinds and set(spec.kinds) <= set(st.PROMPT_KINDS)
    enters = [a for a, s in st.KEYS.items() if s.wire == b"\r"]
    assert sorted(enters) == ["release_round", "resume_ask"]
    assert st.KEYS["release_round"].kinds == ("release",)
    assert st.KEYS["resume_ask"].kinds == ("resume",)
    ys = [a for a, s in st.KEYS.items() if s.wire == b"y"]
    assert sorted(ys) == ["inventory_yes", "resume_confirm"]
    assert st.KEYS["inventory_yes"].kinds == ("inventory",)
    assert st.KEYS["resume_confirm"].kinds == ("confirm_resume",)
    assert not any(s.wire == b"q" and "round" in s.kinds for s in st.KEYS.values())
    assert {k for a in st.VERDICTS for k in st.KEYS[a].kinds} == {"round"}


def test_the_phrase_lines():
    assert st.phrase_for("execute", NAME) == f"execute {NAME}"
    assert st.phrase_for("execute_variant", NAME) == (
        f"execute {NAME} after policy switch check"
    )


def test_a_key_that_was_sent_for_a_prompt_is_not_offered_again(rig):
    d = rig.look(rig.jview(26), cut("第 1/5 轮：确认场景就绪"))
    keys = rig.keys(d, keyed=frozenset({d.prompt["id"]}))
    assert all(k["why_not"]["code"] == "key_sent" for k in keys)
    keys = rig.keys(d, unconfirmed=d.prompt["id"])
    assert all(k["why_not"]["code"] == "key_unconfirmed" for k in keys)


def test_a_reprinted_prompt_is_a_new_prompt_and_its_keys_are_open_again(rig):
    text = cut("第 1/5 轮：确认场景就绪")
    d1 = rig.look(rig.jview(26), text)
    again = text + b"\r\n" + text.split(b"\r\n")[-2] + b"\r\n"
    d2 = rig.look(rig.jview(26), again)
    assert d1.prompt["id"] != d2.prompt["id"]
    keys = rig.keys(d2, keyed=frozenset({d1.prompt["id"]}))
    assert allowed(keys) == ["release_round", "quit"]


def test_the_monitor_must_be_green_for_the_keys_that_move_but_not_for_q(rig):
    d = rig.look(rig.jview(26), cut("第 1/5 轮：确认场景就绪"))
    keys = {k["id"]: k for k in rig.keys(d, monitor_state="red")}
    assert keys["release_round"]["why_not"]["code"] == "monitor_not_green"
    assert keys["quit"]["allowed"] is True


def test_a_tab_that_has_not_reported_cannot_press_a_key_that_moves(rig):
    d = rig.look(rig.jview(26), cut("第 1/5 轮：确认场景就绪"))
    for age in (None, st.PRESENCE_S + 1):
        keys = {k["id"]: k for k in rig.keys(d, presence_age_s=age)}
        assert keys["release_round"]["why_not"]["code"] == "presence_stale"
        assert keys["quit"]["allowed"] is True


def test_a_key_that_appears_is_greyed_for_a_moment_and_the_next_waits_for_the_gap(rig):
    d = rig.look(rig.jview(26), cut("第 1/5 轮：确认场景就绪"))
    keys = {k["id"]: k for k in rig.keys(d)}
    release = keys["release_round"]
    assert release["not_before"] is not None and keys["quit"]["not_before"] is None
    due = st.not_before_epoch(release)
    assert abs(due - (d.since + st.ARM_DELAY_S)) < 0.01
    keys = {k["id"]: k for k in rig.keys(d, last_taken_at=d.since + 100)}
    assert (
        st.not_before_epoch(keys["release_round"])
        >= d.since + 100 + st.KEY_GAP_S - 0.01
    )


def test_the_tokens_fit_one_state_only(rig):
    d = rig.look(rig.jview(26), cut("第 1/5 轮：确认场景就绪"))
    a = {k["id"]: k["token"] for k in rig.keys(d, seq=7)}
    assert a == {k["id"]: k["token"] for k in rig.keys(d, seq=7)}, (
        "the same on every poll"
    )
    assert a["release_round"] != a["quit"]
    b = {k["id"]: k["token"] for k in rig.keys(d, seq=8)}
    assert all(a[k] != b[k] for k in a)
    other = st.token_for(b"x" * 16, "run", 7, "release:1", "release_round")
    assert other != st.token_for(SECRET, "run", 7, "release:1", "release_round")


def test_the_sequence_changes_with_the_state_and_not_with_the_noise(rig):
    d1 = rig.look(rig.jview(34), cut("当前正向子任务 步 50/200"))
    d2 = rig.look(rig.jview(34), cut("当前正向子任务 步 66/200"))
    assert st.seq_signature(d1, True) == st.seq_signature(d2, True)
    d3 = rig.look(rig.jview(37), cut("当前正向子任务结束"))
    assert st.seq_signature(d3, True) != st.seq_signature(d2, True)


# --- profiles ------------------------------------------------------------------------------------


def test_the_profiles_and_their_capabilities():
    assert st.DEFAULT_PROFILE == "harvest-5bc750ca"
    assert set(st.PROFILES) == {"harvest-5bc750ca", "bfdee2cf", "main-297a74c3"}
    assert st.PROFILES["harvest-5bc750ca"].start_home
    assert not st.PROFILES["bfdee2cf"].start_home
    assert st.PROFILES["main-297a74c3"].camera_stale
    assert "camera_stale" in st.PROFILES["main-297a74c3"].confirm_reasons
    assert "camera_stale" not in st.PROFILES["harvest-5bc750ca"].confirm_reasons
    assert st.profile_known("bfdee2cf") and not st.profile_known("a1b2c3d4")
    assert not st.profile_known(None) and not st.profile_known(5)


def test_a_worktree_is_the_profile_only_if_its_four_files_are_byte_for_byte(
    tmp_path, monkeypatch
):
    import hashlib

    files = {}
    for rel in st.PROFILES["harvest-5bc750ca"].sources_sha256:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"content of {rel}".encode())
        files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    fake = st.Profile(
        id="fake",
        commit="0000000",
        title="t",
        start_home=True,
        progress_line=True,
        camera_stale=False,
        data={"sources_sha256": files, "confirm_reasons": [], "no_resume_reasons": []},
    )
    monkeypatch.setitem(st.PROFILES, "fake", fake)
    assert st.profile_matches(tmp_path, "fake") == (True, "ok")
    victim = tmp_path / "levi/automatic/console_text.py"
    victim.write_bytes(victim.read_bytes() + b"\n")
    ok, detail = st.profile_matches(tmp_path, "fake")
    assert ok is False and detail == "differs:levi/automatic/console_text.py"
    victim.unlink()
    assert st.profile_matches(tmp_path, "fake") == (
        False,
        "unreadable:levi/automatic/console_text.py",
    )
    assert st.profile_matches(tmp_path, "nope") == (False, "unknown_profile")


def test_a_file_of_the_worktree_that_is_a_link_is_not_read(tmp_path, monkeypatch):
    rel = "levi/automatic/console.py"
    target = tmp_path / "elsewhere.py"
    target.write_bytes(b"x")
    (tmp_path / "levi/automatic").mkdir(parents=True)
    (tmp_path / rel).symlink_to(target)
    monkeypatch.setitem(
        st.PROFILES,
        "fake",
        st.Profile(
            "fake", "0", "t", True, True, False, {"sources_sha256": {rel: "0" * 64}}
        ),
    )
    assert st.profile_matches(tmp_path, "fake")[0] is False


# --- the document -----------------------------------------------------------------------------------


def test_the_document_has_a_closed_set_of_fields_and_no_path_or_judgement(rig):
    d = rig.look(rig.jview(34), cut("当前正向子任务 步 66/200"))
    c = st.Context(
        now=rig.now,
        secret=SECRET,
        seq=7,
        run_id="r",
        presence_age_s=1.0,
        tail=["x"],
        pid=123,
    )
    doc = st.document(rig.last, d, c)
    json.dumps(doc)  # serialisable
    sample = json.loads((HERE / "fixtures" / "aeri_run.sample.json").read_text())
    expected = set(next(iter(sample["samples"].values())))
    assert set(doc) == expected
    text = json.dumps(doc)
    assert "/home/" not in text and "subtask_check" not in text
    assert doc["state"]["id"] == "forward_running"
    assert doc["fallback"] == {"stop_cli": "scripts/aeri_page_stop.py"}
    assert doc["end"]["allowed"] is False and doc["stop"]["allowed"] is True


def test_the_closed_enums_of_the_codes_file_are_the_modules():
    codes = json.loads((HERE / "fixtures" / "aeri_codes.json").read_text())
    assert tuple(codes["states"]) == st.STATES
    assert tuple(codes["actions"]) == st.ACTIONS
    assert tuple(codes["why_not"]) == st.WHY_NOT
    assert tuple(codes["notices"]) == st.NOTICES
    assert tuple(codes["stop_phases"]) == st.STOP_PHASES
    assert tuple(codes["variants"]) == st.VARIANTS
    assert tuple(codes["slots"]) == st.SLOTS and tuple(codes["levels"]) == st.LEVELS


def test_every_refusal_has_a_status_and_a_message():
    for code, (status, text) in st.REFUSALS.items():
        assert 400 <= status < 600 and text, code
    exc = st.Refuse("key_sent")
    assert (exc.status, exc.code) == (409, "key_sent")


def test_a_round_that_was_discarded_makes_the_next_release_prompt_count_past_the_total(
    rig,
):
    """``d`` discards a round and D plays another: the prompt reads "round 6/5".
    It is still the release prompt of this run (only the total is compared)."""
    text = cut("第 2/5 轮：确认场景就绪").decode().replace("第 2/5 轮", "第 6/5 轮")
    d = rig.look(rig.jview(93), text)
    assert d.state == "await_release" and (d.prompt["n"], d.prompt["total"]) == (6, 5)
    assert d.progress["round"] == 6 and allowed(rig.keys(d)) == [
        "release_round",
        "quit",
    ]


def test_the_discarded_rounds_are_counted_from_the_journal(rig):
    verdict = {
        "sequence_no": 90,
        "record": "note",
        "note": {"code": "operator_verdict", "detail": '{"label":"discard"}'},
    }
    d = rig.look(rig.jview(93, extra=[verdict]), cut("第 2/5 轮：确认场景就绪"))
    assert d.progress["discarded"] == 1 and d.progress["done"] == 0
