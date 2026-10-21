"""Replay of the **whole real run of 18 Oct (r08)** through the real journal reader,
the real screen reader and ``derive``: the journal cut after every one of its 97
lines, with the screen D had printed by then, and every cut looked at again after
1.5 s, 10 s, 25 s, 100 s and 560 s of D doing nothing new.

What it pins: a D that is alive and legitimately waiting is never ``unknown``. The
first version of the page expected a release prompt at *every* SCENE_ASSESS; the
reverse loop of r08 has two of them (seq 49 after the forward loop, seq 71 after
a retry) where D asks nothing and the VLM takes as long as it takes (27.4 s for
the first one in r08; an external answer takes up to 540 s), so each round turned
``unknown`` (``source_conflict``, the arm may move) after 20 s.

The pairs of (journal lines, screen lines) below follow the order of the
recording and are checked against its lines. ``test_marvel0_aeri_state.py`` holds
the same files cut at single moments."""

import pytest
from test_marvel0_aeri_state import SCREEN, Rig

from levi.marvel0 import aeri_state as st

SCREEN_LINES = SCREEN.decode().split("\r\n")

# (journal lines, screen lines printed so far, a text of the last of them, the
# state D is in, whether D really waits longer than DRIFT_S here: for a person's
# key, or for the VLM). Journal lines are counted as a reader sees them (seq 49 is
# the 50th line); both counts only grow, and the text is checked against the
# recording by ``test_the_screen_lines_are_the_recordings``.
TIMELINE = [
    (1, 17, "请输入 execute", "services", False),
    (6, 21, "2/7 启动 VLM 服务", "services", False),
    (12, 29, "6/7 各轮", "services", False),
    (14, 30, "首轮回位会打开夹爪", "await_home_o", True),
    (14, 31, "检查机械臂返回 Home", "await_home_p", True),
    (14, 32, "确认知道回位会开爪", "await_home_h", True),
    (17, 35, "Home verified: error_mm=5.5", "busy", False),
    (22, 35, "Home verified: error_mm=5.5", "busy", False),
    (25, 42, "清单对就按 y 确认", "await_inventory", True),
    (26, 45, "第 1/5 轮：确认场景就绪", "await_release", True),
    (27, 46, "第 1/5 轮已放行", "busy", False),
    (34, 50, "子任务进行中：s 记本轮成功", "forward_running", True),
    (34, 55, "步 66/200", "forward_running", True),
    (34, 65, "步 193/200", "forward_running", True),
    (37, 66, "当前正向子任务结束", "closing", False),
    # the end of the forward loop: homing, then SCENE_ASSESS (subtask_forward_done,
    # seq 49): the VLM plans the reverse loop; D asks nothing, however long it takes
    (50, 69, "Home verified: error_mm=6.6", "busy", True),
    (59, 73, "m 请求停止自动复位", "reverse_running", True),
    (67, 75, "measured gripper=0.2088", "reverse_running", False),
    # the reverse loop's retry: SCENE_ASSESS again (subtask_continue, seq 71)
    (72, 75, "measured gripper=0.2088", "busy", True),
    (77, 79, "m 请求停止自动复位", "reverse_running", True),
    # the reverse loop is done (VERIFY_INITIAL), D checks the pose, asks for round 2
    (93, 84, "Home verified: error_mm=6.2", "busy", False),
    (93, 86, "第 2/5 轮：确认场景就绪", "await_release", True),
]
# After q the run goes WAIT_HUMAN -> COMPLETED within milliseconds with no wait
# block printed; and then wraps up (7/7).
ENDING = [
    (95, 87, "已按停止请求结束", "stopping_or_busy", False),
    (97, 89, "7/7 已停止", "stopping", False),
]
SECONDS = (0.0, 1.5, 10.0, 25.0, 100.0, 560.0)
BAD = {"source_conflict", "screen_drift", "termios_mismatch", "inventory_mismatch"}


def screen_of(k: int) -> bytes:
    """The first ``k`` lines of the recording, as D printed them."""
    return ("\r\n".join(SCREEN_LINES[:k]) + "\r\n").encode()


def looks(rig, n, k, seconds=SECONDS, journal=None):
    """The answers of ``derive`` for journal prefix ``n`` and ``k`` screen lines,
    one per time in ``seconds`` (D showing nothing new meanwhile)."""
    j = journal or rig.jview(n)
    out, mem = [], st.Memory()
    for t in seconds:
        f = rig.facts(j, screen_of(k))
        f.now = rig.now + t
        d = st.derive(f, mem)
        mem = d.mem
        out.append((t, d))
    return out


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_the_screen_lines_are_the_recordings():
    """The pairs are only as good as the files."""
    assert len(SCREEN_LINES) > 89
    for _n, k, text, _state, _long in [*TIMELINE, *ENDING]:
        assert text in SCREEN_LINES[k - 1], (k, text, SCREEN_LINES[k - 1][:60])
    assert sorted((n, k) for n, k, *_ in TIMELINE) == [(n, k) for n, k, *_ in TIMELINE]


@pytest.mark.parametrize(
    ("n", "k", "text", "state", "long"),
    TIMELINE,
    ids=[f"{n}-{k}-{state}" for n, k, _, state, _ in TIMELINE],
)
def test_a_waiting_d_is_never_unknown_however_long_it_waits(
    rig, n, k, text, state, long
):
    for t, d in looks(rig, n, k):
        if t > st.DRIFT_S and not long:
            continue  # (D does not really stay here: the journal moves on)
        assert d.state == state, (n, text, t, d.state, d.reason)
        assert d.reason not in BAD, (n, text, t, d.reason)


def test_the_first_reverse_scene_assess_of_the_real_run_is_busy_not_unknown(rig):
    """The review's failing case (r08 seq 49), with the wait longer than 20 s:
    27.4 s in the recording, up to 540 s with an external answer."""
    j = rig.jview(50)
    assert j.to_state == "SCENE_ASSESS" and j.reason == "subtask_forward_done"
    assert j.inventory_confirmed and len(j.released) == 1 and j.reverse_done == 0
    assert st.expected_prompt(rig.facts(j, screen_of(69))) is None
    mem = st.Memory()
    for t in (0.0, 5.0, 27.4, 60.0, 541.0):
        f = rig.facts(j, screen_of(69))
        f.now = rig.now + t
        d = st.derive(f, mem)
        mem = d.mem
        assert (d.state, d.reason) == ("busy", "planning"), (t, d.state, d.reason)
        assert d.may_move is False and d.prompt is None
        assert d.stop_open is True, "Stop stays offered while D waits for the VLM"
        assert rig.keys(d) == []


def test_the_second_reverse_scene_assess_after_a_retry_is_busy_too(rig):
    j = rig.jview(72)
    assert j.to_state == "SCENE_ASSESS" and j.reason == "subtask_continue"
    assert st.expected_prompt(rig.facts(j, screen_of(75))) is None
    for t, d in looks(rig, 72, 75, (0.0, 30.0, 300.0)):
        assert d.state == "busy" and d.reason == "planning", (t, d.reason)


def test_an_external_answer_awaited_is_shown_as_such(rig):
    f = rig.facts(rig.jview(50), screen_of(69))
    f.queue_pending, f.queue_oldest_s = 1, 200.0
    f.now = rig.now + 200.0
    d = st.derive(f, st.Memory())
    assert (d.state, d.reason) == ("busy", "waiting_agent")


def test_every_cut_of_the_real_journal_is_looked_at_and_none_turns_unknown(rig):
    """Every journal prefix (1..97), with the screen the recording had by then:
    the walk of the whole run. Within the time a real D takes to print what the
    journal announced (under DRIFT_S) nothing is ``unknown``; where D really
    waits for longer (a person's key, the VLM) nothing is, ever."""
    longs = {n for n, _, _, _, long in TIMELINE if long}
    checked = 0
    for n in range(1, 98):
        k = [k for upto, k, *_ in TIMELINE + ENDING if upto <= n][-1]
        times = SECONDS if n in longs else (0.0, 1.5, 10.0)
        for t, d in looks(rig, n, k, times):
            assert d.state != "unknown", (n, k, t, d.reason)
            assert d.reason not in BAD, (n, k, t, d.reason)
            checked += 1
    assert checked > 97 * 3


@pytest.mark.parametrize(("n", "k", "text", "state", "long"), ENDING)
def test_the_end_of_the_real_run(rig, n, k, text, state, long):
    for t, d in looks(rig, n, k, (0.0, 1.5, 10.0)):
        assert d.state != "unknown", (n, t, d.reason)
        if state != "stopping_or_busy":
            assert d.state == state


def test_the_release_prompt_is_expected_only_where_d_asks_for_it(rig):
    """The rule behind the replay: a release is asked at VERIFY_INITIAL after the
    pose check and the confirmed list (before round 1 and after each reverse
    loop), never at SCENE_ASSESS."""
    asked = []
    for n in range(1, 98):
        j = rig.jview(n)
        kind = st.expected_prompt(rig.facts(j, b""))
        if kind == "release":
            asked.append(n)
            assert j.to_state == "VERIFY_INITIAL", n
        if j.to_state == "SCENE_ASSESS":
            assert kind is None, n
    assert 26 in asked and 93 in asked
    assert not set(asked) & set(range(50, 90))  # (the whole of the round)


def test_a_round_a_resume_abandoned_does_not_hide_the_next_release_prompt(rig):
    """Why the rule is not "releases <= reverse loops done": a round that a human
    resume abandoned never finishes its reverse loop. After it D comes back to
    VERIFY_INITIAL (PREFLIGHT, human_resumed) and asks for the next release."""
    resumed = [
        {
            "sequence_no": 40,
            "record": "committed",
            "from_state": "FORWARD_ACTIVE",
            "to_state": "WAIT_HUMAN",
            "reason": "policy_error",
        },
        {
            "sequence_no": 41,
            "record": "committed",
            "from_state": "WAIT_HUMAN",
            "to_state": "PREFLIGHT",
            "reason": "human_resumed",
        },
        {
            "sequence_no": 42,
            "record": "committed",
            "from_state": "PREFLIGHT",
            "to_state": "VERIFY_INITIAL",
            "reason": "preflight_passed",
        },
    ]
    j = rig.jview(34, extra=resumed)
    assert len(j.released) == 1 and j.reverse_done == 0  # (round 1 never completed)
    f = rig.facts(j, screen_of(45))
    assert st.expected_prompt(f) == "release"
    mem = st.Memory()
    for t in (0.0, 1.5, 3.0):
        f.now = rig.now + t
        d = st.derive(f, mem)
        mem = d.mem
    assert d.state == "await_release" and d.prompt["kind"] == "release"
