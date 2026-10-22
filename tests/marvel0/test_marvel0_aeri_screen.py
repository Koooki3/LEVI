"""``levi/marvel0/aeri_screen.py``: terminal D's screen as lines, kinds and the
prompt that waits. No process, no file but the fixtures.

``fixtures/aeri_r08/screen.txt`` is the screen of the real run of 18 Oct (r08,
code harvest-5bc750ca, zh), paths cut, with the typed phrase's echo removed as
the page's pty has no echo. Everything D said in it must be recognised or
named foreign: a line nobody knows is how a changed sentence shows up."""

import json
import re
from pathlib import Path

import pytest

from levi.marvel0 import aeri_screen as sc

HERE = Path(__file__).resolve().parent
R08 = (HERE / "fixtures" / "aeri_r08" / "screen.txt").read_bytes()
PROFILES = sorted(sc.prompts()["profiles"])


def lex(profile="harvest-5bc750ca", lang="zh") -> sc.Lexicon:
    return sc.lexicon(profile, lang)


def screen(text, *, profile="harvest-5bc750ca", lang="zh") -> sc.ScreenView:
    buf = sc.ScreenBuf()
    buf.feed(text if isinstance(text, bytes) else text.encode())
    return sc.read(buf, lex(profile, lang))


def sample(template: str, **given) -> str:
    """A template filled in: digits for the numbers, a word for the rest."""
    fields = {
        name: given.get(
            name, "3" if name in ("n", "total", "steps", "max_steps") else "abc"
        )
        for name in re.findall(r"\{([a-z_]+)\}", template)
    }
    return template.format(**fields)


# --- the terminal --------------------------------------------------------------------------


def test_lines_end_at_the_newline_and_the_unfinished_one_is_kept_apart():
    buf = sc.ScreenBuf()
    buf.feed(b"one\r\ntwo\r\nType execute r1: ")
    assert [line.text for line in buf.lines] == ["one", "two"]
    assert buf.partial.text == "Type execute r1: "
    assert buf.tail() == ["one", "two", "Type execute r1: "]
    buf.feed(b"\r\nthree\n")
    assert [line.text for line in buf.lines][-2:] == ["Type execute r1: ", "three"]
    assert buf.partial is None


def test_a_lone_carriage_return_goes_back_to_the_start_of_the_line():
    buf = sc.ScreenBuf()
    buf.feed(b"step 1\rstep 2\r\nnext\r\n")
    assert [line.text for line in buf.lines] == ["step 2", "next"]


def test_backspace_and_other_controls_do_not_become_text():
    buf = sc.ScreenBuf()
    buf.feed(b"abx\bc\x07\x00d\r\n")
    assert buf.lines[-1].text == "abcd"


def test_colour_and_cursor_sequences_are_removed_also_when_a_read_cuts_them():
    buf = sc.ScreenBuf()
    buf.feed(b"\x1b[31mred\x1b[0m \x1b[2K\x1b[1;")
    buf.feed(b"5Hok \x1b]0;title\x07done\r\n")
    assert buf.lines[-1].text == "red ok done"


def test_a_character_cut_between_two_reads_is_read_whole():
    word = "放行".encode()
    buf = sc.ScreenBuf()
    buf.feed(word[:2])
    buf.feed(word[2:] + b"\r\n")
    assert buf.lines[-1].text == "放行"
    assert buf.lines[-1].end == len(word) + 2


def test_a_line_knows_where_it_started_and_a_reprinted_prompt_gets_a_new_start():
    buf = sc.ScreenBuf()
    buf.feed("ab\r\n放行\r\n放行\r\n".encode())
    starts = [line.start for line in buf.lines]
    assert starts == [0, 4, 4 + 6 + 2]
    assert buf.lines[1].text == buf.lines[2].text and starts[1] != starts[2]


def test_a_buffer_that_began_late_counts_from_its_base():
    buf = sc.ScreenBuf(base=1000)
    buf.feed(b"x\r\n")
    assert buf.lines[0].start == 1000 and buf.lines[0].end == 1003


def test_an_answered_unfinished_line_ends_so_the_next_line_does_not_stick_to_it():
    buf = sc.ScreenBuf()
    buf.feed("请输入 execute r1：".encode())
    buf.answered()
    buf.feed("1/7 预检\r\n".encode())
    assert [line.text for line in buf.lines] == ["请输入 execute r1：", "1/7 预检"]
    buf.answered()  # (nothing unfinished: nothing happens)
    assert buf.count == 2


def test_the_line_memory_is_bounded():
    buf = sc.ScreenBuf()
    for n in range(sc.LINES_KEPT + 50):
        buf.feed(f"l{n}\r\n".encode())
    assert len(buf.lines) == sc.LINES_KEPT and buf.count == sc.LINES_KEPT + 50
    long = sc.ScreenBuf()
    long.feed(b"x" * 100_000 + b"\r\n")
    assert len(long.lines[0].text) == sc.LINE_MAX


# --- the real screen --------------------------------------------------------------------------


def test_everything_d_said_in_the_real_run_is_recognised_or_named_foreign():
    view = screen(R08)
    odd = [s.text for s in view.seen if s.kind in ("unrecognised", "partial")]
    assert odd == []
    kinds = {s.kind for s in view.seen}
    assert {
        "warning",
        "svc",
        "home_o",
        "home_p",
        "home_h",
        "inv_title",
        "inv_row",
        "inv_todo",
        "inv_yes",
        "release",
        "released",
        "round_start",
        "forward_hint",
        "progress",
        "subtask_end",
        "reverse_hint",
        "status",
        "stopped",
        "svc_shutdown",
        "noise",
    } <= kinds


def test_third_party_lines_are_noise_and_d_lines_are_not():
    view = screen(R08)
    noise = [s.text for s in view.seen if s.kind == "noise"]
    assert any(t.startswith("Homing over") for t in noise)
    assert any("server config" in t for t in noise)
    assert any(t.startswith("Gripper reply confirmed") for t in noise)
    assert any(t.startswith("Home verified") for t in noise)
    assert not any("轮" in t and t.startswith("[") for t in noise)


def cut(marker: str, after: str = "") -> bytes:
    """The real screen up to and including the line that has ``marker``."""
    text = R08.decode()
    at = text.index(marker)
    end = text.index("\r\n", at) + 2 if "\r\n" in text[at:] else len(text)
    return text[:end].encode()


@pytest.mark.parametrize(
    ("upto", "kind"),
    [
        ("请输入 execute aeri-20261018-ext-r08：", "phrase"),
        ("首轮回位会打开夹爪", "home_o"),
        ("检查机械臂返回 Home", "home_p"),
        ("确认知道回位会开爪", "home_h"),
        ("清单对就按 y 确认", "inv_todo"),
        ("第 1/5 轮：确认场景就绪", "release"),
        ("子任务进行中：s 记本轮成功", "forward_hint"),
        ("当前正向子任务 步 66/200", "forward_hint"),
    ],
)
def test_the_prompt_that_waits_at_each_point_of_the_real_run(upto, kind):
    text = R08.decode()
    at = text.index(upto)
    end = text.find("\r\n", at)
    part = text[: end + 2 if end >= 0 else len(text)]
    if kind == "phrase":  # (the line stays unfinished: nothing is echoed)
        part = text[: at + len(upto)]
    view = screen(part)
    assert view.open is not None and view.open.kind == kind, view.open


def test_nothing_waits_while_d_works():
    for marker in (
        "1/7 预检：GPU 锁",
        "2/7 VLM 就绪",
        "第 1/5 轮已放行",
        "当前正向子任务结束",
        "[第 1/5 轮] reset 策略正在复位环境",
        "7/7 收尾",
    ):
        view = screen(cut(marker))
        assert view.open is None, (marker, view.open and view.open.text)


def test_the_object_list_is_read_from_the_screen_rows():
    view = screen(cut("清单对就按 y 确认"))
    assert [r["id"] for r in view.inventory_rows] == [
        "blue_plate_1",
        "white_plate_1",
        "red_pepper_1",
        "purple_eggplant_1",
    ]
    graspable = {r["id"]: r["graspable"] for r in view.inventory_rows}
    assert graspable == {
        "blue_plate_1": False,
        "white_plate_1": False,
        "red_pepper_1": True,
        "purple_eggplant_1": True,
    }
    assert view.inventory_rows[2] == {
        "id": "red_pepper_1",
        "colour": "red",
        "kind": "pepper",
        "on": "blue_plate_1",
        "graspable": True,
    }
    # once the list is answered it is no longer "open", but it is still known
    later = screen(cut("清单已确认"))
    assert later.inventory_rows == [] and len(later.inventory_rows_last) == 4


def test_a_second_list_replaces_the_first_one():
    one = cut("清单对就按 y 确认").decode()
    second = one.replace("blue_plate_1", "green_plate_1")
    view = screen((one + second).encode())
    assert "green_plate_1" in {r["id"] for r in view.inventory_rows}
    assert "blue_plate_1" not in {r["id"] for r in view.inventory_rows}


def test_the_forward_progress_and_the_round_are_read():
    view = screen(cut("当前正向子任务 步 66/200"))
    assert view.steps == (66, 200)
    assert view.round == 1
    assert view.last_index["forward_hint"] > view.last_index["released"]


def test_a_changed_sentence_is_unrecognised_and_closes_no_prompt_but_is_counted():
    text = cut("第 1/5 轮：确认场景就绪").decode()
    view = screen(text)
    assert view.open.kind == "release" and view.unrecognised_after_open == 0
    changed = text.replace("按 Enter 放行", "请按回车键放行")
    view = screen(changed)
    assert view.open is None, "the prompt is not recognised, so no key is offered"
    assert view.unrecognised >= 1


def test_foreign_and_unknown_lines_after_a_prompt_do_not_close_it():
    base = cut("第 1/5 轮：确认场景就绪").decode()
    for extra in (
        "Homing over 30 steps -> [ 0.48 ]  (rotating 11.0 deg)\r\n",
        "Home reached.\r\n",
        "  server config: pi05 (rebase dim=3)\r\n",
        "something a library printed\r\n",
        "\r\n",
    ):
        view = screen(base + extra)
        assert view.open is not None and view.open.kind == "release", extra
    view = screen(base + "something a library printed\r\n")
    assert view.unrecognised_after_open == 1


def test_a_d_line_after_a_prompt_closes_it():
    base = cut("第 1/5 轮：确认场景就绪").decode()
    view = screen(base + "第 1/5 轮已放行\r\n")
    assert view.open is None
    view = screen(base + "[第 1/5 轮] 随便什么 D 的话\r\n")
    assert view.open is None, "anything with D's round tag is D's"
    view = screen(base + "[准备] 随便什么\r\n")
    assert view.open is None


def test_a_finished_phrase_line_is_not_an_open_prompt():
    view = screen("请输入 execute r1：\r\n")
    assert view.open is None
    view = screen("请输入 execute r1：")
    assert view.open.kind == "phrase"
    assert view.open.group("phrase") == "execute r1"


def test_the_policy_switch_phrase_is_its_own_prompt():
    view = screen("请输入 execute r1 after policy switch check：")
    assert view.open.kind == "phrase_variant"


def test_the_english_phrase_prompt_ends_in_a_blank_and_is_still_found():
    view = screen("Type execute r1: ", lang="en")
    assert view.open is not None and view.open.kind == "phrase"
    assert view.open.group("phrase") == "execute r1"


def test_an_interrupt_acknowledged_by_d_is_seen_and_an_unrelated_line_is_not():
    text = "收到 SIGINT（Ctrl+C）：已停止下发运动指令，正在安全收尾，不会再开始新的一轮。\r\n"
    assert screen(text).interrupt is True
    assert screen("Ctrl+C was pressed\r\n").interrupt is False


def test_a_fault_block_names_its_code():
    view = screen("运行已锁定 FAULT_LOCKED：policy_error\r\n")
    assert view.fault == "policy_error"


# --- the wait block -------------------------------------------------------------------------


def wait_text(profile, reason, lang="zh", extra_happened=None, n=1, total=5):
    data = sc.prompts()["profiles"][profile]
    happened_key, todo_key = data["wait_reasons"][reason]
    texts = data["texts"][lang]
    happened = sample(texts[happened_key])
    todo = texts[todo_key]
    body = texts["join"].format(happened=happened, todo=todo)
    return f"{texts['tag'].format(n=n, total=total)} {body}\r\n"


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("lang", ["zh", "en"])
def test_every_wait_the_console_can_print_is_recognised_with_its_reason(profile, lang):
    data = sc.prompts()["profiles"][profile]
    for reason in data["wait_reasons"]:
        view = screen(wait_text(profile, reason, lang), profile=profile, lang=lang)
        assert view.open is not None and view.open.kind == "wait", (reason, view.seen)
        assert view.wait["reason"] == reason
        assert view.wait["todo_key"] == data["wait_reasons"][reason][1]


def test_the_wait_block_collects_the_phase_and_the_notes_under_it():
    data = sc.prompts()["profiles"]["harvest-5bc750ca"]
    texts = data["texts"]["zh"]
    block = (
        wait_text("harvest-5bc750ca", "subtask_not_released")
        + "[第 1/5 轮] "
        + sample(texts["sub_wait_forward"], n="1")
        + "\r\n  - policy_error: websocket closed\r\n  - motion_refused: held\r\n"
    )
    view = screen(block)
    assert view.open.kind == "wait"
    assert view.wait["phase"] == "sub_wait_forward"
    assert view.wait["restore_scene"] is True
    assert view.wait["recent"] == [
        {"code": "policy_error", "detail": "websocket closed"},
        {"code": "motion_refused", "detail": "held"},
    ]


def test_a_wait_that_only_asks_for_enter_does_not_ask_to_restore_the_scene():
    view = screen(wait_text("harvest-5bc750ca", "operator_stop"))
    assert view.wait["restore_scene"] is False


def test_the_second_step_of_a_wait_belongs_to_the_wait_line_before_it():
    texts = sc.prompts()["profiles"]["harvest-5bc750ca"]["texts"]["zh"]
    block = (
        wait_text("harvest-5bc750ca", "operator_stop")
        + "[第 1/5 轮] "
        + texts["confirm_resume"]
        + "\r\n"
    )
    view = screen(block)
    assert view.open.kind == "confirm_resume"
    assert view.wait["reason"] == "operator_stop"


def test_a_cancelled_second_step_is_followed_by_the_wait_printed_again():
    texts = sc.prompts()["profiles"]["harvest-5bc750ca"]["texts"]["zh"]
    block = (
        wait_text("harvest-5bc750ca", "operator_stop")
        + "[第 1/5 轮] "
        + texts["confirm_resume"]
        + "\r\n"
        + "[第 1/5 轮] "
        + texts["confirm_cancelled"]
        + "\r\n\r\n"
        + wait_text("harvest-5bc750ca", "operator_stop")
    )
    view = screen(block)
    assert view.open.kind == "wait"
    first = [s for s in view.seen if s.kind == "wait"]
    assert len(first) == 2 and first[0].start != first[1].start


# --- the words, all of them -----------------------------------------------------------------


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("lang", ["zh", "en"])
def test_every_template_of_every_profile_is_found_in_its_own_kind(profile, lang):
    """Each sentence D can print, filled in, is recognised, and is not read as
    another one: the table is the page's only knowledge of D's words."""
    lexicon = lex(profile, lang)
    data = sc.prompts()["profiles"][profile]
    texts = data["texts"][lang]
    for key, template in texts.items():
        if key in ("tag", "tag_setup", "join") or key.startswith(("wait_", "todo_")):
            continue
        line = sc.Line(sample(template, grasp=texts["sub_inv_grasp"]), 0, 1)
        seen = lexicon.classify(line)
        if key in lexicon.texts and (
            key in sc.BODY_KINDS or key.startswith(("st_", "sub_wait_"))
        ):
            continue  # a tagged body: below
        if len(re.sub(r"\{[a-z_]+\}", "", template).strip()) < sc.MIN_LITERAL and not (
            key in sc.KINDS or key.startswith(("svc_", "sum_"))
        ):
            continue  # a fragment of a longer sentence is never a line
        assert seen.kind not in ("unrecognised", "blank"), (profile, lang, key, seen)


@pytest.mark.parametrize("profile", PROFILES)
def test_every_tagged_sentence_is_found_under_the_round_tag_and_the_setup_tag(profile):
    data = sc.prompts()["profiles"][profile]
    texts = data["texts"]["zh"]
    lexicon = lex(profile)
    for key, kind in sc.BODY_KINDS.items():
        if key not in texts:
            continue
        for tag in ("[第 2/5 轮]", "[准备]"):
            line = sc.Line(f"{tag} {sample(texts[key])}", 0, 1)
            seen = lexicon.classify(line)
            assert seen.kind == kind, (key, tag, seen)
            assert seen.tagged
            assert (seen.n, seen.total) == ((2, 5) if tag != "[准备]" else (None, None))


def test_the_profiles_keep_the_words_of_their_code_version():
    harvest = sc.prompts()["profiles"]["harvest-5bc750ca"]["texts"]["zh"]
    pin = sc.prompts()["profiles"]["bfdee2cf"]["texts"]["zh"]
    main = sc.prompts()["profiles"]["main-297a74c3"]["texts"]["zh"]
    assert "startup_home_object_handled" in harvest
    assert "startup_home_object_handled" not in pin
    assert "progress_subtask" in harvest and "progress_subtask" not in pin
    assert "wait_camera_stale" in main and "wait_camera_stale" not in harvest


def test_a_language_the_profile_has_no_words_for_is_refused():
    with pytest.raises(ValueError):
        sc.lexicon("harvest-5bc750ca", "fr")


def test_the_prompts_file_is_what_the_regeneration_writes():
    """The words are copied from the AERI sources by a script; a hand edit of the
    file (or a change of that script) shows here, and the drift check
    (``scripts/check_aeri_prompts.py``) says whether a worktree still matches."""
    data = json.loads(sc.PROMPTS_FILE.read_text(encoding="utf-8"))
    assert data["schema"] == "levi.marvel0.aeri_prompts.v1"
    assert data["default"] in data["profiles"]
    for pid, profile in data["profiles"].items():
        assert set(profile["texts"]) == {"zh", "en"}, pid
        assert set(profile["texts"]["zh"]) == set(profile["texts"]["en"]), pid
        assert set(profile["sources_sha256"]) == {
            "levi/automatic/console_text.py",
            "levi/automatic/console.py",
            "levi/automatic/subtask_units.py",
            "levi/automatic/fr3_run.py",
        }
        for table in ("enter", "quit", "confirm", "cancel"):
            assert profile["keys"][table], (pid, table)
        assert profile["keys"]["verdict"] == {
            "s": "success",
            "f": "failure",
            "d": "discard",
        }


# --- the cost of a look (review of step 5, M4: 15.7 ms per watcher tick) ---------------------


def counting(lexicon, monkeypatch) -> list:
    """Every call of ``lexicon.classify`` from here on, by line text."""
    calls: list = []
    real = lexicon.classify

    def spy(line, index=-1):
        calls.append(line.text)
        return real(line, index)

    monkeypatch.setattr(lexicon, "classify", spy)
    return calls


def test_a_look_at_a_screen_that_did_not_change_classifies_nothing(monkeypatch):
    """The watcher looks four times a second; with 1500 lines on the screen each
    look classified every one of them again (about 15 ms of CPU)."""
    lexicon = lex()
    buf = sc.ScreenBuf()
    buf.feed(R08 + "请输入 execute r1：".encode())  # (an unfinished line too)
    first = sc.read(buf, lexicon)
    calls = counting(lexicon, monkeypatch)
    assert sc.read(buf, lexicon) is first
    assert calls == []
    buf.feed(b"")  # (an empty read changes nothing)
    assert sc.read(buf, lexicon) is first


def test_each_new_line_is_classified_once_and_the_view_is_the_one_from_scratch(
    monkeypatch,
):
    """Read after every piece D prints, past the end of the line memory: each
    committed line is classified once, and every view equals the one a fresh
    buffer given the same bytes at once makes."""
    import dataclasses

    monkeypatch.setattr(sc, "LINES_KEPT", 40)
    lexicon = lex()
    calls = counting(lexicon, monkeypatch)
    buf = sc.ScreenBuf()
    data = R08 * 2
    fed = 0
    for size in [1, 7, 64, 300, 2048] * 400:
        if fed >= len(data):
            break
        buf.feed(data[fed : fed + size])
        fed += size
        view = sc.read(buf, lexicon)
        fresh = sc.ScreenBuf()
        fresh.feed(data[:fed])
        before = len(calls)
        want = sc.read(fresh, lex())
        assert len(calls) == before, "(the fresh one has its own lexicon)"
        assert dataclasses.asdict(view) == dataclasses.asdict(want), fed
    assert buf.count > 40 * 3 and len(buf.lines) == 40
    assert len(calls) == buf.count, "every committed line once, no more"


def test_another_lexicon_classifies_the_screen_again():
    buf = sc.ScreenBuf()
    buf.feed(R08)
    zh = sc.read(buf, lex(lang="zh"))
    en = sc.read(buf, lex(lang="en"))
    assert en is not zh
    assert en.counts != zh.counts, "(the zh screen is unrecognised in English)"
    fresh = sc.ScreenBuf()
    fresh.feed(R08)
    assert en.counts == sc.read(fresh, lex(lang="en")).counts


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("lang", ["zh", "en"])
def test_the_index_by_first_character_skips_no_template_that_matches(profile, lang):
    """``classify`` tries only the templates whose fixed head starts with the
    line's first character (or that have none): the same first match as trying
    every template in order, for every sentence D can print and the r08 screen."""
    lexicon = lex(profile, lang)
    texts = sc.prompts()["profiles"][profile]["texts"][lang]
    lines = [sample(t, grasp=texts["sub_inv_grasp"]) for t in texts.values()]
    lines += R08.decode().split("\r\n") + ["", "   ", "x", "Homing over 30 steps"]
    for text in lines:
        if lexicon.tag_re.match(text):
            continue  # (a tagged line never reaches the plain templates)
        everyone = next(
            (key for key, _k, regex, _n in lexicon.plain if regex.match(text.rstrip())),
            None,
        )
        assert lexicon.classify(sc.Line(text, 0, 1)).key == everyone, text
