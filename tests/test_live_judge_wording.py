"""The judge-side task text (``[judge.task_text]`` of ``live.toml``): the lab's
own wording of an instruction for the judgements' questions, never for the
robot policy and never written back to a rollout or a dataset state. The table's
matching and priority, its configuration, and where it reaches: the online
judgement, the background review and the time segments. A fake model server
and a protocol stand-in replace the model; no GPU."""

import json

import pytest
from test_live_online import Ask, cfg, judge_with, request
from test_live_pipeline import env  # noqa: F401  (fixture)

from levi.live import config as live_config
from levi.live import generic, online

ORIGINAL = "pick the eggplant on the bread"
LAB = "put the eggplant onto the bread"
TABLE = {ORIGINAL: LAB}


# --- matching -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "task,folder,expected",
    [
        # By the instruction's text, however it is cased, spaced or underscored.
        (ORIGINAL, None, (LAB, "task_text")),
        ("Pick  the EGGPLANT on the bread ", None, (LAB, "task_text")),
        ("pick_the_eggplant_on_the_bread", None, (LAB, "task_text")),
        # By the task folder's name (underscores are spaces).
        ("something else", "Pick_The_Eggplant_On_The_Bread", (LAB, "task_folder")),
        # Nothing matches: quoted as recorded.
        ("pick the eggplant from the bread", "other_task", None),
        ("", None, None),
    ],
)
def test_the_table_is_keyed_by_the_normalized_text_or_folder_name(
    task, folder, expected
):
    got = generic.judge_task(task, TABLE, folder)
    assert got == (expected if expected else (task, None))
    # An empty or missing table changes nothing, anywhere.
    assert generic.judge_task(task, {}, folder) == (task, None)
    assert generic.judge_task(task, None, folder) == (task, None)


def test_the_task_folder_is_tried_before_the_text_and_the_first_match_wins():
    table = {
        "pick the eggplant on the bread": "by the text",
        "bread_task": "by the folder",
    }
    assert generic.judge_task(ORIGINAL, table, "bread_task") == (
        "by the folder",
        "task_folder",
    )
    assert generic.judge_task(ORIGINAL, table, "another_folder") == (
        "by the text",
        "task_text",
    )
    # An entry that says what the instruction says ends the lookup: the text
    # entry behind it does not apply, and nothing counts as rewritten.
    table = {"bread_task": ORIGINAL, ORIGINAL: LAB}
    assert generic.judge_task(ORIGINAL, table, "bread_task") == (ORIGINAL, None)


def test_a_wording_is_never_looked_up_again_and_is_cleaned_like_a_task():
    chain = {ORIGINAL: LAB, LAB: "put the eggplant into the sink"}
    assert generic.judge_task(ORIGINAL, chain)[0] == LAB
    assert generic.judge_task(ORIGINAL, {ORIGINAL: "a  b\tc"})[0] == "a b c"


def test_the_spec_s_questions_quote_the_wording_in_place_of_the_placeholder():
    spec = generic.anchored_spec(LAB, "generic-final.v2.json")
    assert LAB in spec["question"] and LAB in spec["start"]["question"]
    assert ORIGINAL not in spec["question"] + spec["start"]["question"]
    assert "{task}" not in spec["question"] + spec["start"]["question"]


# --- the configuration -----------------------------------------------------------------


def load(tmp_path, toml):
    path = tmp_path / "live.toml"
    path.write_text(toml)
    return live_config.load(path)


def test_the_table_is_read_from_live_toml_and_survives_the_effective_file(tmp_path):
    c = load(
        tmp_path,
        '[judge.task_text]\n"Pick_the_eggplant on the  bread" = "put the eggplant onto the bread"\n',
    )
    assert c.judge.task_text == {"Pick_the_eggplant on the  bread": LAB}
    assert live_config.Config().judge.task_text == {}  # off by default
    # The worker reads the rendered effective file: the table must round-trip,
    # with quotes and backslashes in a wording intact.
    c.judge.task_text['a "quoted" key'] = 'say "hi" \\ there'
    path = tmp_path / "effective.toml"
    path.write_text(live_config.render(c))
    assert live_config.load(path).judge.task_text == c.judge.task_text


@pytest.mark.parametrize(
    "toml,message",
    [
        ('[judge]\nwording = "x"\n', "Unknown key"),
        ('[judge]\ntask_text = "x"\n', "must be dict"),
        ('[judge.task_text]\n"a b" = 3\n', "keys and wordings must be text"),
        ('[judge.task_text]\n"a b" = ""\n', "one non-empty line"),
        ('[judge.task_text]\n"a b" = "two\\nlines"\n', "one non-empty line"),
        ('[judge.task_text]\n"" = "x"\n', "a task text or a folder name"),
        ('[judge.task_text]\n"  _ " = "x"\n', "a task text or a folder name"),
        ('[judge.task_text]\n"Pick_It" = "x"\n"pick it" = "y"\n', "the same key as"),
        (f'[judge.task_text]\n"a" = "{"x" * 601}"\n', "over 600 characters"),
    ],
)
def test_a_wrong_table_is_an_error(tmp_path, toml, message):
    with pytest.raises(ValueError, match=message):
        load(tmp_path, toml)


# --- the online judgement ------------------------------------------------------------------


def question_of(ask):
    return ask.calls[-1]["question"]


def test_the_online_question_quotes_the_wording_and_the_result_says_so(tmp_path):
    c = cfg(tmp_path)
    c.judge.task_text = dict(TABLE)
    ask = Ask()
    judge = judge_with(c, ask=ask)
    code, body = judge.handle(json.dumps(request(task=ORIGINAL)).encode())
    assert code == 200 and body["status"] == "ok", body
    asked = question_of(ask)
    assert LAB in asked and ORIGINAL not in asked and "{task}" not in asked
    assert body["task_rewritten"] == "task_text"
    # The log names that the wording was used, and holds no task text.
    (line,) = online.read_log(c.live_dir)
    assert line["task_rewritten"] == "task_text"
    assert LAB not in json.dumps(line) and ORIGINAL not in json.dumps(line)


def test_the_online_judgement_matches_the_folder_the_client_names(tmp_path):
    c = cfg(tmp_path)
    c.judge.task_text = {"cups": "put the cup onto the plate"}
    ask = Ask()
    body = judge_with(c, ask=ask).handle(json.dumps(request()).encode())[1]
    # The request's episode.task_folder is "cups".
    assert "put the cup onto the plate" in question_of(ask)
    assert body["task_rewritten"] == "task_folder"


def test_without_an_entry_the_online_question_quotes_the_task_as_sent(tmp_path):
    c = cfg(tmp_path)
    c.judge.task_text = {"another task": "something else"}
    ask = Ask()
    body = judge_with(c, ask=ask).handle(json.dumps(request(task=ORIGINAL)).encode())[1]
    assert ORIGINAL in question_of(ask) and "something else" not in question_of(ask)
    assert body["task_rewritten"] is None


def test_the_wording_is_not_a_way_to_send_data_to_the_model(tmp_path):
    """The table lives in live.toml, not in the request: a request that brings
    its own wording (a key the contract does not define) is refused."""
    judge = judge_with(cfg(tmp_path))
    body = request()
    body["task_text"] = {ORIGINAL: LAB}
    code, answer = judge.handle(json.dumps(body).encode())
    assert code == 422 and "does not define: task_text" in answer["reason"]


# --- the background labelling ----------------------------------------------------------------


def texts(e):
    """Every text part of every real model request (the capability probe aside)."""
    out = []
    for payload in e.fake.calls:
        if payload.get("max_completion_tokens") == 1:
            continue
        content = payload["messages"][-1]["content"]
        parts = content if isinstance(content, list) else [{"text": content}]
        out += [
            p["text"] for p in parts if isinstance(p, dict) and p.get("type") == "text"
        ]
    return out


def use_final_v2(e):
    e.config.pipeline.temporal = False
    e.config.pipeline.anchored_spec = "generic-final.v2.json"
    e.config.validate()


def test_the_background_review_quotes_the_wording_and_the_state_keeps_the_original(env):  # noqa: F811
    from levi.live import stats

    e = env(answers={"start_state": "not_at_destination"})
    use_final_v2(e)
    e.config.judge.task_text = {e.rollouts.text: LAB}
    e.config.validate()
    e.rollouts.write(0)
    e.run()
    asked = texts(e)
    # Both questions of the review (the start check and the final frames).
    assert len(asked) == 2 and all(LAB in t for t in asked)
    assert not any(e.rollouts.text in t for t in asked)
    state = e.state()
    # The page and the dataset state keep the instruction as recorded.
    assert state["task_text"] == e.rollouts.text
    assert (e.rollouts.dir / "task_description.txt").read_text().strip() == (
        e.rollouts.text
    )
    verdict = state["demos"]["demo_0000"]["verdict"]
    assert verdict["outcome"] == "success"
    assert verdict["task_rewritten"] == "task_text"
    (record,) = stats.read(e.ws / "live")
    assert record["result"]["task_rewritten"] == "task_text"


def test_without_an_entry_nothing_is_rewritten_and_nothing_says_so(env):  # noqa: F811
    from levi.live import stats

    e = env(answers={"start_state": "not_at_destination"})
    use_final_v2(e)
    e.config.judge.task_text = {"an unrelated task": LAB}
    e.config.validate()
    e.rollouts.write(0)
    e.run()
    asked = texts(e)
    assert len(asked) == 2 and all(e.rollouts.text in t for t in asked)
    assert not any(LAB in t for t in asked)
    assert "task_rewritten" not in e.state()["demos"]["demo_0000"]["verdict"]
    (record,) = stats.read(e.ws / "live")
    assert record["result"]["task_rewritten"] is None


def test_the_time_segments_quote_the_wording_too_and_the_folder_entry_wins(env):  # noqa: F811
    e = env()
    e.config.judge.task_text = {
        "stack_the_plates": "stack the plates by colour",  # the folder's name
        e.rollouts.text: "never used: the folder is tried first",
    }
    e.config.validate()
    e.rollouts.write(0)
    e.run()
    asked = texts(e)
    # The temporal run (coarse and refine) and the release review: every
    # request quotes the wording, none the recorded text.
    assert len(asked) >= 3
    assert all("stack the plates by colour" in t for t in asked)
    assert not any(e.rollouts.text in t for t in asked)
    assert not any("never used" in t for t in asked)
    assert e.state()["task_text"] == e.rollouts.text
    row = e.state()["demos"]["demo_0000"]
    assert row["verdict"]["task_rewritten"] == "task_folder"


def test_an_episode_taken_in_with_the_background_off_records_the_wording_used():
    """With no worker the statistics record is built from the online result the
    client relayed: the wording it quoted is in it (null when none)."""
    verdict = {
        "outcome": "failure",
        "events": 1,
        "valid_events": 0,
        "undecided": True,
        "rule": "final_state",
        "source": "online",
        "spec": "generic-final",
        "spec_version": 2,
        "at": 1790000002.5,
    }
    row = {
        "state": "done",
        "completed_at": 1790000000.0,
        "verdict": {**verdict, "task_rewritten": "task_folder"},
        "online": {"status": "ok", "usage": {}},
    }
    c = live_config.Config()
    got = online.stats_record(c, "ds", "demo_0001", row, 1790000005.0)
    assert got["result"]["task_rewritten"] == "task_folder"
    assert got["result"]["verdict"]["undecided"] is True
    row["verdict"] = verdict
    assert (
        online.stats_record(c, "ds", "demo_0001", row, 1.0)["result"]["task_rewritten"]
        is None
    )
