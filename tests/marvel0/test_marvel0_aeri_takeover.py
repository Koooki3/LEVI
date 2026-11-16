"""The takeover (``GRASP_ASSIST``) on the hosted terminal D page: when its keys are
offered, which byte each one is, what the page reads from D's screen, and when it
offers nothing. Pure: the facts are written by the test (the rig of
``test_marvel0_aeri_state.py``), on the words of the ``main-ab38c7b1`` profile,
the one with the capability. The keys press real bytes only through
``test_marvel0_aeri_takeover_run.py`` (the supervisor on the scripted machine)."""

import dataclasses
import json
from pathlib import Path

import pytest
from test_marvel0_aeri_state import SECRET, Rig, allowed, cut

from levi.marvel0 import aeri_screen as sc
from levi.marvel0 import aeri_state as st

HERE = Path(__file__).resolve().parent
MAIN = st.PROFILES["main-ab38c7b1"]
HARVEST = st.PROFILES["harvest-5bc750ca"]
ENTER_ASSIST = {
    "sequence_no": 34,
    "record": "committed",
    "from_state": "VERIFY_INITIAL",
    "to_state": "GRASP_ASSIST",
    "reason": "grasp_assist_started",
}
LETTERS = {
    "assist_x_pos": b"x",
    "assist_x_neg": b"X",
    "assist_y_pos": b"y",
    "assist_y_neg": b"Y",
    "assist_z_pos": b"z",
    "assist_z_neg": b"Z",
    "assist_close": b"c",
    "assist_lift": b"u",
    "assist_open": b"o",
    "assist_handback": b"g",
    "assist_abort": b"a",
}


def assist_lines(profile=MAIN, opening="0.299", fact="holding_candidate", steps=3):
    """D's takeover banner and waiting line, from the profile's own templates (the
    round tag D puts in front of each)."""
    t = profile.data["texts"]["zh"]
    none = t["assist_none"]
    tag = "[第 1/5 轮] "
    view = t["assist_view"].format(
        x=0.481,
        y=-0.011,
        z=0.236,
        travel=0.01,
        steps=steps,
        max_steps=40,
        cmd=0.0,
        opening=none if opening is None else opening,
        fact=none if fact is None else fact,
    )
    return (
        tag + t["st_grasp_assist"] + "\r\n" + tag + t["assist_title"] + "\r\n"
        "" + tag + view + "\r\n"
    )


def screen_of(**kw) -> bytes:
    return (cut("Home verified").decode() + assist_lines(**kw)).encode()


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path, profile=MAIN)


def in_assist(rig, upto=21):
    return rig.jview(upto, extra=[ENTER_ASSIST])


# --- the table of keys ----------------------------------------------------------------------


def test_the_page_offers_the_eleven_keys_of_the_takeover_in_the_waiting_line(rig):
    d = rig.look(in_assist(rig), screen_of())
    assert (d.state, d.reason) == ("await_assist", "prompt_open")
    assert d.prompt["kind"] == "assist" and d.settled and d.may_move
    keys = rig.keys(d)
    assert [k["id"] for k in keys] == list(st.ASSIST_ACTIONS)
    assert allowed(keys) == list(st.ASSIST_ACTIONS)
    assert [k["id"] for k in keys] == list(LETTERS)


def test_every_button_is_the_letter_the_profile_pins_and_no_other_byte(rig):
    """The profile's ``assist_keys`` is copied from ``grasp_assist.KEY_CHARS`` by
    the generator; the page's table has the same letters for the same keys."""
    pinned = MAIN.data["assist_keys"]
    mine = {
        k.wire.decode(): k.id for k in st.KEYS.values() if k.id.startswith("assist_")
    }
    assert mine == {
        letter: "assist_" + name.replace("+", "_pos").replace("-", "_neg")
        for letter, name in pinned.items()
    }
    assert {k: v.wire for k, v in st.KEYS.items() if k in LETTERS} == LETTERS
    d = rig.look(in_assist(rig), screen_of())
    for entry in rig.keys(d):
        assert entry["byte"] == LETTERS[entry["id"]].decode()
        assert st.KEYS[entry["id"]].kinds == ("assist",)
    assert set(LETTERS.values()) <= st.ALL_BYTES


def test_the_moves_and_the_gripper_are_level_b_arm_moves_the_abort_is_level_a(rig):
    d = rig.look(in_assist(rig), screen_of())
    by = {k["id"]: k for k in rig.keys(d)}
    for name in list(LETTERS)[:9]:
        assert (by[name]["level"], by[name]["motion"]) == ("B", "arm_moves"), name
        assert by[name]["not_before"] is not None  # the delay and the gap of level B
    assert (by["assist_handback"]["level"], by["assist_handback"]["motion"]) == (
        "B",
        "may_move",
    )
    assert (by["assist_abort"]["level"], by["assist_abort"]["motion"]) == ("A", "none")
    assert by["assist_abort"]["not_before"] is None
    assert {k["slot"] for k in by.values()} == {"first"}
    # nothing here asks for a typed phrase or a tick
    assert {k["phrase"] for k in by.values()} == {None}
    assert not any(k in st.KEY_ATTEST for k in LETTERS)


def test_the_abort_needs_neither_the_monitor_nor_a_tab_but_the_moves_do(rig):
    d = rig.look(in_assist(rig), screen_of())
    stale = rig.keys(d, presence_age_s=60.0)
    assert allowed(stale) == ["assist_abort"]
    assert {k["why_not"]["code"] for k in stale if not k["allowed"]} == {
        "presence_stale"
    }
    red = rig.keys(d, monitor_state="red")
    assert allowed(red) == ["assist_abort"]
    off = rig.keys(d, enabled=False)
    assert allowed(off) == []
    assert {k["why_not"]["code"] for k in off} == {"aeri_disabled"}


def test_a_waiting_line_that_has_not_settled_offers_nothing_yet(rig):
    d = rig.look(in_assist(rig), screen_of(), settle=False)
    assert d.state == "await_assist" and not d.settled
    assert allowed(rig.keys(d)) == []
    assert {k["why_not"]["code"] for k in rig.keys(d)} == {"not_settled"}


def test_one_key_per_waiting_line_and_the_next_line_opens_the_keys_again(rig):
    d = rig.look(in_assist(rig), screen_of())
    pid = d.prompt["id"]
    sent = rig.keys(d, keyed=frozenset({pid}))
    assert allowed(sent) == []
    assert {k["why_not"]["code"] for k in sent} == {"key_sent"}
    pending = rig.keys(d, pending=True)
    assert {k["why_not"]["code"] for k in pending} == {"key_sent"}
    # D answers the key and prints its waiting line again: a new line, a new id
    again = screen_of() + assist_lines(steps=4).encode()
    d2 = rig.look(in_assist(rig), again)
    assert d2.prompt["id"] != pid and d2.prompt["kind"] == "assist"
    assert allowed(rig.keys(d2, keyed=frozenset({pid}))) == list(st.ASSIST_ACTIONS)


def test_while_d_carries_a_key_out_nothing_is_offered(rig):
    """D answers a key with a line about it before it moves: the waiting line is
    no longer the last one, so no key is open until it prints the next."""
    t = MAIN.data["texts"]["zh"]
    echo = (
        "[第 1/5 轮] "
        + t["assist_proposal"].format(
            x=0.491, y=-0.011, z=0.236, dx=0.01, dy=0.0, dz=0.0, checks="z_ok=True"
        )
        + "\r\n"
    )
    d = rig.look(in_assist(rig), screen_of() + echo.encode())
    assert d.prompt is None and (d.state, d.reason) == ("busy", "assist_working")
    assert d.may_move is True and rig.keys(d) == []


# --- what the page reads of D's line --------------------------------------------------------


@pytest.mark.parametrize(
    ("fact", "handback"),
    [
        ("holding_candidate", True),
        ("holding_verified", True),
        ("empty", False),
        ("open", False),
        ("unknown", False),
        ("refused", False),
        (None, False),  # none shown yet
        ("something_else", False),  # not a word of the facts: unreadable
    ],
)
def test_hand_back_is_offered_only_on_a_holding_candidate_or_verified(
    rig, fact, handback
):
    d = rig.look(in_assist(rig), screen_of(fact=fact))
    by = {k["id"]: k for k in rig.keys(d)}
    assert by["assist_handback"]["allowed"] is handback, fact
    assert d.assist["handback_ok"] is handback
    if not handback:
        assert by["assist_handback"]["why_not"]["code"] == "assist_no_holding"
        assert by["assist_handback"]["token"] is None
        assert "assist_no_holding" in st.WHY_NOT and "assist_no_holding" in st.REFUSALS
    # every other key of the takeover is untouched by the fact
    assert all(k["allowed"] for k in by.values() if k["id"] != "assist_handback")
    shown = fact if fact in st.ASSIST_FACTS or fact is None else "unreadable"
    assert d.assist["fact"] == shown


def test_the_measured_opening_and_the_fact_are_read_from_the_line(rig):
    d = rig.look(in_assist(rig), screen_of(opening="0.299", fact="holding_candidate"))
    assert d.assist == {
        "position": [0.481, -0.011, 0.236],
        "travel_m": 0.01,
        "steps": 3,
        "max_steps": 40,
        "command": "0.0",
        "opening": 0.299,
        "fact": "holding_candidate",
        "handback_ok": True,
    }
    doc = st.document(rig.last, d, st.Context(now=rig.now, secret=SECRET, seq=7))
    assert doc["takeover"] == d.assist and doc["state"]["id"] == "await_assist"
    json.dumps(doc)


@pytest.mark.parametrize("opening", [None, "abc", "9.999", "-0.100"])
def test_an_opening_that_is_not_a_reading_is_none(rig, opening):
    d = rig.look(in_assist(rig), screen_of(opening=opening))
    assert d.assist["opening"] is None
    assert d.state == "await_assist"


def test_there_is_no_takeover_field_outside_the_takeover(rig):
    d = rig.look(rig.jview(26), cut("第 1/5 轮：确认场景就绪"))
    doc = st.document(rig.last, d, st.Context(now=rig.now, secret=SECRET, seq=7))
    assert doc["takeover"] is None


# --- when nothing is offered ------------------------------------------------------------------


def assist_ids(keys):
    return [k["id"] for k in keys if k["id"].startswith("assist_")]


@pytest.mark.parametrize("upto", [12, 14, 21, 25, 26, 34, 37, 59, 90])
def test_no_takeover_key_is_offered_outside_the_takeover_state(rig, upto):
    """The takeover's waiting line is on the screen, and D is somewhere else in
    the journal (the real run's states, one by one): no key of it is offered."""
    d = rig.look(rig.jview(upto), screen_of())
    assert d.state != "await_assist"
    assert assist_ids(rig.keys(d)) == [], (upto, d.state)


def test_no_takeover_key_is_offered_in_any_state_of_the_committed_samples():
    sample = json.loads((HERE / "fixtures" / "aeri_run.sample.json").read_text())
    for name, doc in sample["samples"].items():
        ids = assist_ids(doc["keys"])
        if name == "unknown_assist_drift":  # a drift keeps the abort alone
            assert ids == ["assist_abort"], name
        else:
            assert bool(ids) == (doc["state"]["id"] == "await_assist"), name
        assert (doc["takeover"] is not None) == (doc["state"]["id"] == "await_assist")


def test_a_wait_after_an_abort_offers_only_the_end_of_the_run(rig):
    """The abort commits GRASP_ASSIST -> WAIT_HUMAN grasp_assist_aborted: the gate
    stays closed and Enter is refused by D, so the page does not offer it."""
    row = {
        "sequence_no": 40,
        "record": "committed",
        "from_state": "GRASP_ASSIST",
        "to_state": "WAIT_HUMAN",
        "reason": "grasp_assist_aborted",
    }
    j = rig.jview(21, extra=[ENTER_ASSIST, row])
    t = MAIN.data["texts"]["zh"]
    happened, todo = MAIN.data["wait_reasons"]["grasp_assist_aborted"]
    body = t["join"].format(happened=t[happened], todo=t[todo])
    text = screen_of() + f"[第 1/5 轮] {body}\r\n".encode()
    d = rig.look(j, text)
    assert d.state == "await_resume" and d.wait["enter"] is False
    keys = rig.keys(d)
    assert allowed(keys) == ["quit"] and assist_ids(keys) == []


def test_a_profile_without_the_capability_offers_nothing_for_the_takeover(rig):
    j = in_assist(rig)
    # the older profile does not know D's words: nothing it reads is the takeover
    harvest = Rig(rig.tmp / "h", profile=HARVEST)
    d = harvest.look(harvest.jview(21, extra=[ENTER_ASSIST]), screen_of(profile=MAIN))
    assert assist_ids(harvest.keys(d)) == []
    assert d.state != "await_assist"
    # and the new words with a profile that says it has no takeover: refused whole
    off = dataclasses.replace(MAIN, grasp_assist=False)
    lacking = Rig(rig.tmp / "o", profile=off)
    d = lacking.look(j, screen_of())
    assert (d.state, d.reason) == ("unknown", "source_conflict")
    assert lacking.keys(d) == []
    for profile in (HARVEST, off):
        assert profile.grasp_assist is False


def test_the_table_of_the_keys_is_not_reachable_for_a_profile_without_it(rig):
    """Second layer: even for a prompt of the takeover kind (derived on the
    capable profile), the list of actions is empty when the facts' profile has
    no takeover."""
    d = rig.look(in_assist(rig), screen_of())
    assert d.kind == "assist"
    f = rig.last
    assert st._actions_of(d, f) == list(st.ASSIST_ACTIONS)
    f.profile = dataclasses.replace(MAIN, grasp_assist=False)
    assert st._actions_of(d, f) == []
    f.profile = None
    assert st._actions_of(d, f) == []


def test_a_waiting_line_in_other_words_is_drift_and_offers_nothing(rig):
    t = MAIN.data["texts"]["zh"]
    line = t["assist_view"].split("；")[0].format(x=0.1, y=0.2, z=0.3)
    text = (
        cut("Home verified").decode() + "[第 1/5 轮] " + line + " 变了\r\n"
    ).encode()
    d = rig.look(in_assist(rig), text)
    assert assist_ids(rig.keys(d)) == [] and d.prompt is None


def test_after_the_hand_back_the_reset_policy_runs_and_nothing_is_offered(rig):
    t = MAIN.data["texts"]["zh"]
    back = "[第 1/5 轮] " + t["assist_handback"].format(
        fact="holding_candidate", state6=0.0
    )
    d = rig.look(in_assist(rig), screen_of() + (back + "\r\n").encode())
    assert (d.state, d.reason) == ("reverse_running", "reset_active")
    assert d.may_move and rig.keys(d) == [] and d.prompt is None


def test_a_hand_back_older_than_the_waiting_line_is_not_a_hand_back(rig):
    t = MAIN.data["texts"]["zh"]
    back = "[第 1/5 轮] " + t["assist_handback"].format(
        fact="holding_candidate", state6=0.0
    )
    text = cut("Home verified").decode() + back + "\r\n" + assist_lines()
    d = rig.look(in_assist(rig), text.encode())
    assert d.state == "await_assist"


def test_the_waiting_line_needs_the_keyboard_in_the_mode_of_single_keys(rig):
    d = rig.look(in_assist(rig), screen_of(), icanon=True)
    assert d.state != "await_assist" or d.prompt is None
    assert assist_ids(rig.keys(d)) == []


# --- the words ------------------------------------------------------------------------------


def test_every_sentence_of_the_takeover_is_a_line_of_d_and_the_waiting_line_is_one_kind():
    lex = sc.lexicon("main-ab38c7b1", "zh")
    t = MAIN.data["texts"]["zh"]
    fields = {
        "x": 0.1,
        "y": 0.2,
        "z": 0.3,
        "dx": 0.0,
        "dy": 0.0,
        "dz": 0.0,
        "travel": 0.0,
        "steps": 1,
        "max_steps": 2,
        "cmd": 0.0,
        "opening": "0.2",
        "fact": "empty",
        "command": "close",
        "checks": "a",
        "state6": 0.0,
        "err": "1.0",
        "code": "c",
        "detail": "d",
        "why": "w",
        "step_mm": 10,
        "lift_mm": 20,
        "max_step_mm": 20,
        "min_z": 0.05,
        "max_xy": 0.85,
        "max_travel": 0.35,
        "max_steps_": 40,
        "follow_mm": 5,
    }
    kinds = {}
    for key, template in t.items():
        if not key.startswith("assist_"):
            continue
        text = template.format(
            **{k: v for k, v in fields.items() if "{" + k in template}
        )
        seen = lex.classify(sc.Line("[第 1/5 轮] " + text, 0, 1))
        assert seen.kind not in ("unrecognised", "blank", "noise"), key
        kinds[key] = seen.kind
    assert kinds["assist_view"] == "assist_view"
    assert kinds["assist_handback"] == "assist_handback"
    assert {kinds[k] for k in ("assist_abort", "assist_fault")} == {"assist_end"}
    assert {
        kinds[k]
        for k in (
            "assist_proposal",
            "assist_proposal_gripper",
            "assist_proposal_handback",
            "assist_refused",
            "assist_result_ok",
            "assist_result_bad",
        )
    } == {"assist_echo"}
    # only the waiting line is a prompt that waits for a key
    assert {k for k, v in kinds.items() if v == "assist_view"} == {"assist_view"}
    assert "assist_view" in sc.OPEN_KINDS


def test_the_takeover_t_words_are_in_english_too():
    lex = sc.lexicon("main-ab38c7b1", "en")
    t = MAIN.data["texts"]["en"]
    line = "[Round 1/5] " + t["assist_view"].format(
        x=0.1,
        y=0.2,
        z=0.3,
        travel=0.0,
        steps=1,
        max_steps=2,
        cmd=0.0,
        opening=t["assist_none"],
        fact="empty",
    )
    seen = lex.classify(sc.Line(line, 0, 1))
    assert seen.kind == "assist_view"
    assert lex.assist_fields(seen)["opening"] is None
    assert lex.assist_fields(seen)["fact"] == "empty"


def test_the_waiting_line_is_the_screens_open_prompt_only_when_it_is_the_last_line():
    lex = sc.lexicon("main-ab38c7b1", "zh")
    buf = sc.ScreenBuf()
    buf.feed(screen_of())
    assert sc.read(buf, lex).open.kind == "assist_view"
    buf.feed("[第 1/5 轮] 某一行\r\n".encode())  # a line of D that is nobody's
    assert (
        sc.read(buf, lex).open is None or sc.read(buf, lex).open.kind != "assist_view"
    )


# --- D working on a key (review M1) --------------------------------------------------------


def echo_line(**kw) -> str:
    t = MAIN.data["texts"]["zh"]
    fields = {"x": 0.491, "y": -0.011, "z": 0.236, "dx": 0.01, "dy": 0.0, "dz": 0.0}
    return (
        "[第 1/5 轮] "
        + t["assist_proposal"].format(**fields, checks=kw.get("checks", "z_ok=True"))
        + "\r\n"
    )


def test_a_takeover_that_has_run_for_minutes_is_not_a_conflict_while_d_works_on_a_key(
    rig,
):
    """The journal says GRASP_ASSIST from the entry on; after the key is taken D
    prints its line and works before the next waiting line. The silence is counted
    from D's last line, not from the entry (review M1)."""
    d = rig.look(in_assist(rig), screen_of())
    assert d.state == "await_assist"
    rig.now += 30.0  # the operator took half a minute for the first key
    text = screen_of() + echo_line().encode()
    d = rig.look(in_assist(rig), text, settle=False)
    assert (d.state, d.reason) == ("busy", "assist_working")
    assert d.may_move is True and d.prompt is None
    assert rig.keys(d) == []
    doc = st.document(rig.last, d, st.Context(now=rig.now, secret=SECRET, seq=7))
    assert doc["agree"]["journal"] == "ok" and doc["state"]["may_move"] is True


def test_silence_after_d_answered_a_key_is_a_conflict_only_after_the_allowance(rig):
    rig.look(in_assist(rig), screen_of())
    rig.now += 30.0
    text = screen_of() + echo_line().encode()
    d = rig.look(in_assist(rig), text, settle=False)
    assert d.reason == "assist_working"
    # the same screen, nothing new, for less than the allowance: still working
    rig.now += st.ASSIST_QUIET_S - 1
    d = rig.look(in_assist(rig), text, settle=False)
    assert (d.state, d.reason) == ("busy", "assist_working") and d.may_move
    # longer than any key can take: a real drift
    rig.now += 2.0
    d = rig.look(in_assist(rig), text, settle=False)
    assert (d.state, d.reason) == ("unknown", "source_conflict")
    assert d.may_move is True and rig.keys(d) == []
    doc = st.document(rig.last, d, st.Context(now=rig.now, secret=SECRET, seq=7))
    assert doc["agree"]["journal"] == "conflict"


def test_a_new_line_of_d_restarts_the_count(rig):
    rig.look(in_assist(rig), screen_of())
    rig.now += 30.0
    text = screen_of() + echo_line().encode()
    rig.look(in_assist(rig), text, settle=False)
    rig.now += st.ASSIST_QUIET_S - 1
    t = MAIN.data["texts"]["zh"]
    result = "[第 1/5 轮] " + t["assist_result_ok"].format(err="1.0", fact="x") + "\r\n"
    d = rig.look(in_assist(rig), text + result.encode(), settle=False)
    rig.now += st.ASSIST_QUIET_S - 1
    d = rig.look(in_assist(rig), text + result.encode(), settle=False)
    assert d.reason == "assist_working"


def test_the_end_lines_count_as_d_answering_too(rig):
    rig.look(in_assist(rig), screen_of())
    rig.now += 30.0
    t = MAIN.data["texts"]["zh"]
    end = "[第 1/5 轮] " + t["assist_abort"].format(why="operator_abort") + "\r\n"
    d = rig.look(in_assist(rig), screen_of() + end.encode(), settle=False)
    assert (d.state, d.reason) == ("busy", "assist_working") and d.may_move


def test_the_allowance_covers_a_settle_and_a_close_wait_with_a_margin():
    assert st.ASSIST_QUIET_S >= st.ASSIST_SETTLE_S + st.ASSIST_CLOSE_WAIT_S + 5
    assert (
        st.ASSIST_NOT_TAKEN_S >= 8.0 and st.ASSIST_NOT_TAKEN_S > st.ASSIST_CLOSE_WAIT_S
    )


def test_the_waits_are_the_takeovers_own_constants():
    """The page's copies of ``SETTLE_S`` and ``CLOSE_WAIT_S`` are those of
    grasp_assist.py at the profile's commit (skipped where it is not in the clone)."""
    import re
    import subprocess

    commit = MAIN.commit
    root = Path(__file__).resolve().parents[2]
    done = subprocess.run(
        ["git", "-C", str(root), "show", f"{commit}:levi/automatic/grasp_assist.py"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if done.returncode != 0:
        pytest.skip(f"commit {commit} is not in this clone")
    src = done.stdout

    def number(name):
        return float(re.search(rf"^{name} = ([0-9.]+)", src, re.MULTILINE).group(1))

    assert number("SETTLE_S") == st.ASSIST_SETTLE_S
    assert number("CLOSE_WAIT_S") == st.ASSIST_CLOSE_WAIT_S


# --- lines that are not D's after the waiting line (review L1) -----------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Traceback (most recent call last):",
        '  File "/x/y.py", line 3, in <module>',
        "warning: something of another program",
    ],
)
def test_a_line_that_is_not_ds_after_the_waiting_line_withdraws_every_key_but_the_abort(
    rig, line
):
    d = rig.look(in_assist(rig), screen_of())
    assert allowed(rig.keys(d)) == list(st.ASSIST_ACTIONS)
    text = screen_of() + (line + "\r\n").encode()
    d = rig.look(in_assist(rig), text)
    assert (d.state, d.reason) == ("unknown", "screen_drift"), line
    assert d.may_move is True and d.kind == "assist"
    keys = rig.keys(d)
    assert [k["id"] for k in keys] == ["assist_abort"], line
    assert allowed(keys) == ["assist_abort"]
    abort = keys[0]
    assert (abort["byte"], abort["level"], abort["motion"]) == ("a", "A", "none")
    doc = st.document(rig.last, d, st.Context(now=rig.now, secret=SECRET, seq=7))
    # the readings of a line the page no longer trusts are not shown
    assert doc["agree"]["screen"] == "drift" and doc["takeover"] is None
    assert doc["state"]["prompt"]["kind"] == "assist"


def test_an_unfinished_line_after_the_waiting_line_leaves_the_abort_alone(rig):
    d = rig.look(in_assist(rig), screen_of() + b"Traceback (most rec")
    assert d.state == "unknown" and allowed(rig.keys(d)) == ["assist_abort"]


def test_the_abort_under_a_drift_follows_the_ordinary_rules_of_a_key(rig):
    d = rig.look(in_assist(rig), screen_of() + b"warning: x\r\n")
    pid = d.prompt["id"]
    assert rig.keys(d, keyed=frozenset({pid}))[0]["why_not"]["code"] == "key_sent"
    assert rig.keys(d, enabled=False)[0]["why_not"]["code"] == "aeri_disabled"
    # the abort needs no tab and no green monitor (level A), like q
    assert allowed(rig.keys(d, presence_age_s=60.0, monitor_state="red")) == [
        "assist_abort"
    ]
    off = Rig(rig.tmp / "u", profile=MAIN)
    d = off.look(in_assist(off), screen_of() + b"warning: x\r\n", settle=False)
    assert allowed(off.keys(d)) == []  # not settled yet


def test_a_drift_withdraws_the_abort_too_for_a_profile_without_the_takeover(rig):
    lacking = Rig(rig.tmp / "o", profile=dataclasses.replace(MAIN, grasp_assist=False))
    d = lacking.look(in_assist(lacking), screen_of() + b"warning: x\r\n")
    assert lacking.keys(d) == []


def test_the_byte_a_is_read_by_no_other_prompt_of_terminal_d():
    """The abort byte under a drift is sent where D's last line is the takeover's
    waiting line: at ab38c7b1 nothing else of terminal D reads ``a``. Pinned from
    the profile's key tables and, where the commit is in the clone, from the
    sources themselves (console.py's own tables and its takeover handler)."""
    import ast
    import re
    import subprocess

    keys = MAIN.data["keys"]
    others = {
        *keys["enter"],
        *keys["quit"],
        *keys["verdict"],
        *keys["confirm"],
        *keys["cancel"],
        "o",
        "p",
        "h",  # the three of the startup home (CODE_START_HOME)
        "m",  # the reverse loop's request for a human reset
    }
    assert "a" not in others and "A" not in others
    assert st.KEYS["assist_abort"].kinds == ("assist",)
    assert [k for k, s in st.KEYS.items() if s.wire == b"a"] == ["assist_abort"]
    # no other key of the page's own table is the letter either
    assert MAIN.data["assist_keys"]["a"] == "abort"
    root = Path(__file__).resolve().parents[2]

    def show(path):
        return subprocess.run(
            ["git", "-C", str(root), "show", f"{MAIN.commit}:{path}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    console = show("levi/automatic/console.py")
    if console.returncode != 0:
        pytest.skip(f"commit {MAIN.commit} is not in this clone")
    tree = ast.parse(console.stdout)
    tables = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "").endswith(
            ("_KEYS", "QUIT", "ENTER")
        ):
            tables[node.targets[0].id] = ast.literal_eval(node.value)
    assert "LABEL_KEYS" in tables and "a" not in tables["LABEL_KEYS"]
    for name, table in tables.items():
        assert "a" not in table and "A" not in table, name
    # the only places the source writes the letter as a key
    found = re.findall(r"""[\"']a[\"']""", console.stdout)
    assert len(found) == 1
    assert '"a" in text and self._assist_pending is None' in console.stdout
    for path in ("levi/automatic/fr3_run.py", "levi/automatic/console_text.py"):
        text = show(path)
        assert re.search(r"""key\w* *(==|in) *[\"']a[\"']""", text.stdout) is None


def test_blank_lines_after_the_waiting_line_do_not(rig):
    d = rig.look(in_assist(rig), screen_of() + b"\r\n\r\n")
    assert d.state == "await_assist" and allowed(rig.keys(d)) == list(st.ASSIST_ACTIONS)


def test_a_tagged_line_of_d_the_page_does_not_know_closes_the_waiting_line(rig):
    """Under D's own round tag it is D's line: it ends the open prompt (no key)."""
    text = screen_of() + "[第 1/5 轮] 一句页面不认识的话\r\n".encode()
    d = rig.look(in_assist(rig), text)
    assert d.state != "await_assist" and d.prompt is None and rig.keys(d) == []


# --- the capability is tied to the key table (review L4) ------------------------------------


def test_every_capable_profile_pins_exactly_the_table_the_page_types():
    capable = [p for p in st.PROFILES.values() if p.grasp_assist]
    assert [p.id for p in capable] == ["main-ab38c7b1"]
    for profile in capable:
        assert profile.data["assist_keys"] == st.assist_key_table()
    # and the table is the eleven letters of KEY_CHARS, derived from KEYS
    assert st.assist_key_table() == {
        "x": "x+",
        "X": "x-",
        "y": "y+",
        "Y": "y-",
        "z": "z+",
        "Z": "z-",
        "c": "close",
        "u": "lift",
        "o": "open",
        "g": "handback",
        "a": "abort",
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda keys: keys.update({"h": keys.pop("g")}),  # a letter moved
        lambda keys: keys.pop("a"),  # a key gone
        lambda keys: keys.update({"w": "x+"}),  # a key more
        lambda keys: keys.update({"x": "x-"}),  # a letter for another key
        lambda keys: keys.clear(),
    ],
)
def test_a_capable_profile_whose_key_letters_differ_from_the_table_loses_the_takeover(
    monkeypatch, mutate
):
    import copy

    data = copy.deepcopy(sc.prompts())
    mutate(data["profiles"]["main-ab38c7b1"]["assist_keys"])
    monkeypatch.setattr(sc, "prompts", lambda: data)
    profiles = st._profiles()
    assert profiles["main-ab38c7b1"].grasp_assist is False
    # the others are untouched, and the rest of the profile still reads
    assert profiles["main-ab38c7b1"].reset_policy is True
    assert profiles["harvest-5bc750ca"].grasp_assist is False


def test_a_capable_profile_without_a_key_table_loses_the_takeover(monkeypatch):
    import copy

    data = copy.deepcopy(sc.prompts())
    del data["profiles"]["main-ab38c7b1"]["assist_keys"]
    monkeypatch.setattr(sc, "prompts", lambda: data)
    assert st._profiles()["main-ab38c7b1"].grasp_assist is False


# --- a hang that keeps printing foreign lines is still a hang (re-review N1) ---------------


def hang_after_echo(rig, foreign, seconds, every=5.0):
    """D answered a key (the echo) and then only other programs' lines come, one
    every ``every`` s, for ``seconds``; returns the last look."""
    rig.look(in_assist(rig), screen_of())
    rig.now += 30.0
    text = screen_of() + echo_line().encode()
    d = rig.look(in_assist(rig), text, settle=False)
    assert d.reason == "assist_working"
    waited = 0.0
    while waited < seconds:
        rig.now += every
        waited += every
        text += (foreign + "\r\n").encode()
        d = rig.look(in_assist(rig), text, settle=False)
    return d


@pytest.mark.parametrize(
    "foreign",
    [
        "warning: some other program speaks",
        "Traceback (most recent call last):",
        "hostchild: something",
        "server config: x",
    ],
)
def test_foreign_lines_every_few_seconds_do_not_keep_a_hung_key_working(rig, foreign):
    d = hang_after_echo(rig, foreign, st.ASSIST_QUIET_S + 10)
    assert (d.state, d.reason) == ("unknown", "source_conflict"), foreign
    assert d.may_move is True and rig.keys(d) == []
    # and still working before the allowance has run out
    young = Rig(rig.tmp / "y", profile=MAIN)
    d = hang_after_echo(young, foreign, st.ASSIST_QUIET_S - 6)
    assert (d.state, d.reason) == ("busy", "assist_working")


def test_the_gripper_reply_line_during_a_key_is_not_a_conflict(rig):
    """``Gripper reply confirmed:`` is printed by the bridge in the middle of a
    gripper key, after D's echo (fr3_bridge._send): it is neither drift nor a
    reason to wait longer."""
    d = hang_after_echo(rig, "Gripper reply confirmed: ok", 10.0, every=5.0)
    assert (d.state, d.reason) == ("busy", "assist_working") and d.may_move
    assert rig.keys(d) == []


def test_d_s_own_lines_keep_a_long_key_working_past_the_allowance(rig):
    rig.look(in_assist(rig), screen_of())
    rig.now += 30.0
    text = screen_of() + echo_line().encode()
    rig.look(in_assist(rig), text, settle=False)
    t = MAIN.data["texts"]["zh"]
    for n in range(6):  # a line of D's own every 15 s: 90 s in all
        rig.now += 15.0
        text += (
            "[第 1/5 轮] "
            + t["assist_result_bad"].format(code=f"c{n}", detail="d")
            + "\r\n"
        ).encode()
        d = rig.look(in_assist(rig), text, settle=False)
        assert (d.state, d.reason) == ("busy", "assist_working"), n


def test_the_journal_moving_keeps_the_count_too(rig):
    rig.look(in_assist(rig), screen_of())
    rig.now += 30.0
    text = screen_of() + echo_line().encode()
    rig.look(in_assist(rig), text, settle=False)
    rig.now += st.ASSIST_QUIET_S - 1
    note = {
        "sequence_no": 35,
        "record": "note",
        "note": {"code": "grasp_assist_step", "detail": "{}"},
    }
    j = rig.jview(21, extra=[ENTER_ASSIST, note])
    d = rig.look(j, text, settle=False)
    rig.now += 5.0
    d = rig.look(j, text, settle=False)
    assert d.reason == "assist_working"  # 24 s after the echo, 5 s after the row
