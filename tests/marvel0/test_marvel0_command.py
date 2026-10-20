"""``levi/marvel0/command.py``: the closed-schema renderer of the demo client
command. The command is text to copy: nothing is executed or read here. Golden
tests for the default form and for every bound of the form; the flags that
guard the arm come from the site file alone."""

import ast
import json
import math
import re
import shlex
from pathlib import Path

import pytest
from site_sample import make_site

from levi.marvel0 import command as C
from levi.marvel0 import models as M

PROMPT = "pick the eggplant in the blue plate"
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = "examples/fr3_local/run_robotiq_client_collect_data.py"


def model(**kw):
    base = {
        "id": "pi05_fr3_all_step49999",
        "config": "pi05_fr3_all_state",
        "cfg_capable": False,
        "restricted": False,
        "verified": True,
        "step": None,
        "version": None,
        "variant": None,
        "size_bytes": 1,
        "warnings": (),
    }
    return M.Model(**{**base, **kw})


def form_for(tmp_path, body=None, site=None, mdl=None, **override):
    site = site or make_site(tmp_path, **override)
    mdl = mdl or model()
    return site, mdl, C.validate(site, mdl, {"prompt": PROMPT, **(body or {})})


def rendered(tmp_path, body=None, **kw):
    site, mdl, form = form_for(tmp_path, body, **kw)
    return C.render(site, mdl, form)


def refused(tmp_path, body, **kw):
    """The FormError of validating ``body`` on the sample site."""
    site = kw.pop("site", None) or make_site(tmp_path)
    mdl = kw.pop("mdl", None) or model()
    with pytest.raises(C.FormError) as caught:
        C.validate(site, mdl, {"prompt": PROMPT, **body})
    return caught.value


def golden_argv(tmp_path, tail=None):
    root = tmp_path / "rollouts"
    return [
        "env",
        "-u",
        "VIRTUAL_ENV",
        "no_proxy=localhost,127.0.0.1,::1",
        "uv",
        "run",
        "--no-sync",
        "--with",
        "pyrealsense2==2.55.1.6486",
        "python",
        SCRIPT,
        "--remote-host=127.0.0.1",
        "--remote-port=8000",
        "--view-camera-id=262622274256",
        "--wrist-camera-id=262622275490",
        "--franka-server-url=http://127.0.0.1:5000/",
        f"--prompt={PROMPT}",
        "--max-timesteps=500",
        "--eval-num=5",
        "--min-z=0.025",
        "--home-pose",
        "0.481902",
        "-0.011145",
        "0.235951",
        "3.119139",
        "0.008288",
        "-0.883962",
        "--home-duration-s=3.0",
        "--max-xy=0.85",
        "--max-step-m=0.1",
        "--max-step-rad=0.35",
        "--rx-range",
        "1.5",
        "4.03",
        "--ry-range",
        "-0.74",
        "0.96",
        "--rz-range",
        "-2.44",
        "0.77",
        "--binarize-gripper",
        "--require-matching-server",
        "--no-allow-unknown-prompt",
        "--levi-mode=off",
        "--new",
        "--no-step-by-step",
        *(tail or ["--rtc", "--rtc-execute-horizon=6"]),
        f"--rollout-root={root}",
        "--rollout-group=pi05_fr3_all_step49999_rtc_demo",
        "--eval-note=marvel0 page: pi05_fr3_all_step49999 rtc=on horizon=6",
    ]


# --- the golden command ---------------------------------------------------------------


def test_the_default_form_renders_the_documented_command(tmp_path):
    preview = rendered(tmp_path)
    assert preview.argv == golden_argv(tmp_path)
    assert preview.cwd == str(tmp_path / "openpi")
    assert preview.env == ["no_proxy=localhost,127.0.0.1,::1"]


def test_the_command_text_is_exactly_this(tmp_path):
    root = tmp_path / "rollouts"
    expected = f"""\
cd {tmp_path / "openpi"} && \\
  env -u VIRTUAL_ENV no_proxy=localhost,127.0.0.1,::1 \\
  uv run --no-sync --with pyrealsense2==2.55.1.6486 python {SCRIPT} \\
  --remote-host=127.0.0.1 --remote-port=8000 --view-camera-id=262622274256 --wrist-camera-id=262622275490 --franka-server-url=http://127.0.0.1:5000/ \\
  '--prompt={PROMPT}' \\
  --max-timesteps=500 --eval-num=5 --min-z=0.025 \\
  --home-pose 0.481902 -0.011145 0.235951 3.119139 0.008288 -0.883962 --home-duration-s=3.0 \\
  --max-xy=0.85 --max-step-m=0.1 --max-step-rad=0.35 --rx-range 1.5 4.03 --ry-range -0.74 0.96 --rz-range -2.44 0.77 \\
  --binarize-gripper --require-matching-server --no-allow-unknown-prompt \\
  --levi-mode=off --new --no-step-by-step --rtc --rtc-execute-horizon=6 \\
  --rollout-root={root} --rollout-group=pi05_fr3_all_step49999_rtc_demo '--eval-note=marvel0 page: pi05_fr3_all_step49999 rtc=on horizon=6'"""
    assert rendered(tmp_path).command == expected


def test_the_text_splits_back_into_the_argv(tmp_path):
    preview = rendered(tmp_path)
    words = shlex.split(preview.command.replace("\\\n", " "))
    assert words == ["cd", str(tmp_path / "openpi"), "&&", *preview.argv]


def test_the_prompt_is_one_element_with_its_flag(tmp_path):
    preview = rendered(tmp_path, {"prompt": "stack the plates of same color together"})
    assert "--prompt=stack the plates of same color together" in preview.argv
    assert sum(t.startswith("--prompt") for t in preview.argv) == 1


def test_a_tricky_but_allowed_text_is_still_one_quoted_word(tmp_path):
    body = {"prompt": "put 3 screws; then (stop) & wait | #1 <ok> *", "note": "a;b&c|d"}
    preview = rendered(tmp_path, body)
    words = shlex.split(preview.command.replace("\\\n", " "))
    assert words[3:] == preview.argv
    assert "--prompt=put 3 screws; then (stop) & wait | #1 <ok> *" in words
    assert "--eval-note=a;b&c|d" in words


def test_every_flag_is_one_token_and_only_the_tuple_flags_spread(tmp_path):
    argv = rendered(tmp_path).argv[len(C.PREFIX) :]
    kinds = {flag: kind for flag, kind, _ in C.FLAGS}
    seen, i = [], 0
    while i < len(argv):
        name = argv[i].partition("=")[0]
        if name.startswith("--no-") and "--" + name[5:] in kinds:
            name = "--" + name[5:]
        assert name in kinds, argv[i]
        seen.append(name)
        if kinds[name] == "value":
            assert "=" in argv[i]
        elif kinds[name] == "switch":
            assert "=" not in argv[i]
        else:
            assert "=" not in argv[i]
            numbers = argv[i + 1 : i + 1 + C.TUPLE_ARITY[name]]
            assert len(numbers) == C.TUPLE_ARITY[name]
            assert all(not t.startswith("--") for t in numbers)
            i += C.TUPLE_ARITY[name]
        i += 1
    assert len(seen) == len(set(seen))


def test_rtc_off_renders_the_open_loop_horizon(tmp_path):
    preview = rendered(tmp_path, {"rtc": False})
    assert "--open-loop-horizon=6" in preview.argv
    assert "--rtc" not in preview.argv
    assert not any(t.startswith("--rtc-execute-horizon") for t in preview.argv)
    assert "--rollout-group=pi05_fr3_all_step49999_demo" in preview.argv
    assert (
        "--eval-note=marvel0 page: pi05_fr3_all_step49999 rtc=off horizon=6"
        in preview.argv
    )


def test_rtc_on_has_no_open_loop_horizon(tmp_path):
    argv = rendered(tmp_path).argv
    assert not any(t.startswith("--open-loop-horizon") for t in argv)


def test_the_given_values_are_used(tmp_path):
    body = {
        "max_steps": 1200,
        "eval_num": 20,
        "min_z": 0.06,
        "rtc_horizon": 5,
        "rollout_group": "my_group-1.a",
        "note": "morning check",
    }
    argv = rendered(tmp_path, body).argv
    for token in (
        "--max-timesteps=1200",
        "--eval-num=20",
        "--min-z=0.06",
        "--rtc-execute-horizon=5",
        "--rollout-group=my_group-1.a",
        "--eval-note=morning check",
    ):
        assert token in argv


def test_the_defaults_come_from_the_site(tmp_path):
    site = make_site(
        tmp_path,
        caps={
            "max_steps_default": 700,
            "eval_num_default": 7,
            "rtc_horizon_default": 4,
        },
        safety={"min_z_default": 0.04},
    )
    argv = rendered(tmp_path, site=site).argv
    for token in (
        "--max-timesteps=700",
        "--eval-num=7",
        "--min-z=0.04",
        "--rtc-execute-horizon=4",
    ):
        assert token in argv
    off = rendered(
        tmp_path,
        {"rtc": False},
        site=make_site(tmp_path, caps={"open_loop_default": 3}),
    ).argv
    assert "--open-loop-horizon=3" in off


# --- the bounds ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, -1, 0.0, -0.5])
def test_min_z_must_be_positive(tmp_path, value):
    error = refused(tmp_path, {"min_z": value})
    assert (error.code, error.field) == ("min_z_not_positive", "min_z")


@pytest.mark.parametrize("value", [0.01, 0.0249, 1e-9])
def test_min_z_cannot_go_below_the_site_floor(tmp_path, value):
    assert refused(tmp_path, {"min_z": value}).code == "min_z_below_floor"


@pytest.mark.parametrize("value", [0.235951, 0.24, 0.5, 5])
def test_min_z_must_stay_below_the_home_height(tmp_path, value):
    assert refused(tmp_path, {"min_z": value}).code == "min_z_above_home"


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_min_z_must_be_a_finite_number(tmp_path, value):
    assert refused(tmp_path, {"min_z": value}).code == "invalid_type"


@pytest.mark.parametrize("value", [True, "0.03", [0.03], {"z": 1}])
def test_min_z_must_be_a_number(tmp_path, value):
    assert refused(tmp_path, {"min_z": value}).code == "invalid_type"


def test_min_z_is_checked_as_it_is_written(tmp_path):
    # Written with ten significant digits: a value that rounds up to the home
    # height is refused as above it, not accepted and then failed late.
    assert refused(tmp_path, {"min_z": 0.23595099999999999}).code == "min_z_above_home"
    assert form_for(tmp_path, {"min_z": 0.025 + 1e-13})[2].min_z == 0.025
    assert form_for(tmp_path, {"min_z": 0.05 + 0.1})[2].min_z == 0.15


@pytest.mark.parametrize("value", [0.025, 0.03, 0.05325, 0.2359])
def test_min_z_between_the_floor_and_home_is_taken(tmp_path, value):
    _, _, form = form_for(tmp_path, {"min_z": value})
    assert form.min_z == value


def test_the_floor_can_be_raised_by_the_site(tmp_path):
    site = make_site(tmp_path, safety={"min_z_floor": 0.05, "min_z_default": 0.05})
    error = refused(tmp_path, {"min_z": 0.03}, site=site)
    assert error.code == "min_z_below_floor"


@pytest.mark.parametrize("value", [0, -3, 3001, 10**9])
def test_max_steps_out_of_range(tmp_path, value):
    error = refused(tmp_path, {"max_steps": value})
    assert (error.code, error.field) == ("max_steps_out_of_range", "max_steps")


@pytest.mark.parametrize("value", [1, 500, 3000])
def test_max_steps_in_range(tmp_path, value):
    assert form_for(tmp_path, {"max_steps": value})[2].max_steps == value


@pytest.mark.parametrize("value", [0, -1, 51, 10**6])
def test_eval_num_out_of_range(tmp_path, value):
    assert refused(tmp_path, {"eval_num": value}).code == "eval_num_out_of_range"


@pytest.mark.parametrize("value", [1, 5, 50])
def test_eval_num_in_range(tmp_path, value):
    assert form_for(tmp_path, {"eval_num": value})[2].eval_num == value


@pytest.mark.parametrize("field", ["max_steps", "eval_num", "rtc_horizon"])
@pytest.mark.parametrize("value", [True, "6", 6.5, 6.0, [6]])
def test_whole_numbers_must_be_whole_numbers(tmp_path, field, value):
    assert refused(tmp_path, {field: value}).code == "invalid_type"


@pytest.mark.parametrize("value", [0, -2, 9, 11])
def test_the_rtc_horizon_is_bounded_by_the_site(tmp_path, value):
    error = refused(tmp_path, {"rtc": True, "rtc_horizon": value})
    assert (error.code, error.field) == ("rtc_horizon_out_of_range", "rtc_horizon")


@pytest.mark.parametrize("value", [1, 6, 8])
def test_the_rtc_horizon_in_range(tmp_path, value):
    assert form_for(tmp_path, {"rtc_horizon": value})[2].rtc_horizon == value


@pytest.mark.parametrize("value", [0, 11])
def test_the_open_loop_horizon_is_bounded_by_the_site(tmp_path, value):
    error = refused(tmp_path, {"rtc": False, "rtc_horizon": value})
    assert error.code == "rtc_horizon_out_of_range"


def test_the_open_loop_horizon_may_be_larger_than_the_rtc_one(tmp_path):
    assert form_for(tmp_path, {"rtc": False, "rtc_horizon": 9})[2].rtc_horizon == 9
    assert (
        refused(tmp_path, {"rtc": True, "rtc_horizon": 9}).code
        == "rtc_horizon_out_of_range"
    )


@pytest.mark.parametrize("value", [1, "true", [True], 0])
def test_rtc_is_a_boolean(tmp_path, value):
    assert refused(tmp_path, {"rtc": value}).code == "invalid_type"


@pytest.mark.parametrize(
    "value",
    ["", "a b", "../x", "a/b", "-x", ".x", "a" * 81, "a..b", "x\n", "ü", "a$b", "a'b"],
)
def test_the_rollout_group_is_a_strict_slug(tmp_path, value):
    error = refused(tmp_path, {"rollout_group": value})
    assert (error.code, error.field) == ("rollout_group_invalid", "rollout_group")


@pytest.mark.parametrize(
    "value", ["pi05_fr3_all_step49999_rtc_demo", "A", "a.b-c_d", "a" * 80, "g1"]
)
def test_a_good_rollout_group(tmp_path, value):
    assert form_for(tmp_path, {"rollout_group": value})[2].rollout_group == value


def test_the_catalog_offers_exactly_the_names_whose_default_form_is_accepted(
    tmp_path,
):
    # The catalog leaves out a folder whose default group is over the form's
    # 80 characters: its default form would be refused. The two limits are one.
    site = make_site(tmp_path)
    for length in range(60, 81):
        name = "m" * (length - len("_step49999")) + "_step49999"
        accepted = {}
        for rtc in (True, False):
            try:
                C.validate(site, model(id=name), {"prompt": PROMPT, "rtc": rtc})
                accepted[rtc] = True
            except C.FormError as error:
                assert error.code == "rollout_group_invalid", (length, rtc)
                accepted[rtc] = False
        assert M.default_group_fits(name) == all(accepted.values()), length
        assert M.default_group_fits(name) == (length <= 71), length


def test_the_rollout_group_defaults_from_the_model_and_the_mode(tmp_path):
    assert form_for(tmp_path)[2].rollout_group == "pi05_fr3_all_step49999_rtc_demo"
    off = form_for(tmp_path, {"rtc": False})[2]
    assert off.rollout_group == "pi05_fr3_all_step49999_demo"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "x" * 201,
        "a'b",
        'a"b',
        "a\\b",
        "a$b",
        "a`b",
        "a!b",
        "a\nb",
        "a\tb",
        "a\x00b",
        "a\x7fb",
    ],
)
def test_a_bad_note(tmp_path, value):
    error = refused(tmp_path, {"note": value})
    assert (error.code, error.field) == ("note_invalid", "note")
    assert error.extra["reason"] in {
        "empty",
        "too_long",
        "forbidden_char",
        "control_char",
    }


def test_a_note_cannot_start_with_a_dash_either(tmp_path):
    assert refused(tmp_path, {"note": "-x"}).code == "note_invalid"


def test_a_note_is_trimmed(tmp_path):
    assert form_for(tmp_path, {"note": "  morning check \n"})[2].note == "morning check"


def test_the_default_note_names_the_model_the_mode_and_the_horizon(tmp_path):
    assert (
        form_for(tmp_path)[2].note
        == "marvel0 page: pi05_fr3_all_step49999 rtc=on horizon=6"
    )
    off = form_for(tmp_path, {"rtc": False, "rtc_horizon": 4})[2]
    assert off.note == "marvel0 page: pi05_fr3_all_step49999 rtc=off horizon=4"


# --- task instructions ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,reason",
    [
        ("", "empty"),
        ("  ", "empty"),
        ("x" * 301, "too_long"),
        ("-x", "leading_dash"),
        ("--prompt=x", "leading_dash"),
        ("a'b", "forbidden_char"),
        ('a"b', "forbidden_char"),
        ("a\\b", "forbidden_char"),
        ("a$b", "forbidden_char"),
        ("a`b", "forbidden_char"),
        ("a!b", "forbidden_char"),
        ("a\nb", "control_char"),
        ("a\tb", "control_char"),
        ("a\x00b", "control_char"),
        ("a\x7fb", "control_char"),
    ],
)
def test_a_bad_task_instruction_names_the_reason(tmp_path, text, reason):
    error = refused(tmp_path, {"prompt": text})
    assert (error.code, error.field) == ("prompt_invalid", "prompt")
    assert error.extra == {"reason": reason}


def test_a_missing_task_instruction_is_empty(tmp_path):
    site, mdl = make_site(tmp_path), model()
    with pytest.raises(C.FormError) as caught:
        C.validate(site, mdl, {})
    assert caught.value.code == "prompt_invalid" and caught.value.extra == {
        "reason": "empty"
    }


def test_a_task_instruction_must_be_text(tmp_path):
    assert refused(tmp_path, {"prompt": 5}).code == "invalid_type"


def test_the_site_known_instructions_are_accepted(tmp_path):
    site = make_site(tmp_path)
    for text in site.prompts.known:
        assert form_for(tmp_path, {"prompt": text}, site=site)[2].prompt == text


def test_the_instruction_is_trimmed_before_it_is_checked(tmp_path):
    assert form_for(tmp_path, {"prompt": f"  {PROMPT} \n"})[2].prompt == PROMPT
    assert refused(tmp_path, {"prompt": "  -x"}).extra == {"reason": "leading_dash"}


def test_the_site_caps_the_length_of_an_instruction(tmp_path):
    site = make_site(tmp_path, caps={"prompt_max_chars": 20})
    assert refused(tmp_path, {"prompt": "x" * 21}, site=site).extra == {
        "reason": "too_long"
    }
    assert form_for(tmp_path, {"prompt": "x" * 20}, site=site)[2].prompt == "x" * 20


def test_a_restricted_model_takes_only_the_clients_two_instructions(tmp_path):
    restricted = model(id="pi05_fr3_sp_v1", restricted=True)
    error = refused(tmp_path, {"prompt": "wave at the camera"}, mdl=restricted)
    assert (error.code, error.field) == ("prompt_unknown_for_model", "prompt")
    for text in M.RESTRICTED_PROMPTS:
        assert form_for(tmp_path, {"prompt": text}, mdl=restricted)[2].prompt == text
    # The site lists more instructions for the other models; the client of a
    # two-task specialist refuses those, so the page does too.
    others = [
        t for t in make_site(tmp_path).prompts.known if t not in M.RESTRICTED_PROMPTS
    ]
    assert others, "the sample lists instructions the client does not know"
    for text in others:
        assert refused(tmp_path, {"prompt": text}, mdl=restricted).code == (
            "prompt_unknown_for_model"
        )


def test_the_restricted_instructions_are_the_clients_tasks():
    assert M.RESTRICTED_PROMPTS == (
        "pick screws of same size into the empty slot of screw box",
        "stack the plates of same color together",
    )
    # (TASKS of examples/fr3_local/run_robotiq_client.py in the openpi checkout,
    # which lies outside this repository: copied, and named in the setup docs.)


def test_the_unknown_instruction_rule_matches_the_client_normalisation(tmp_path):
    restricted = model(id="pi05_fr3_sp_v1", restricted=True)
    for text in (
        "stack_the_plates_of_same_color_together",
        "stack the plates of same color together ",
        "pick_screws_of_same_size_into_the_empty_slot_of_screw_box",
    ):
        assert form_for(tmp_path, {"prompt": text}, mdl=restricted)[2]
    assert refused(tmp_path, {"prompt": "Stack The Plates"}, mdl=restricted).code == (
        "prompt_unknown_for_model"
    )


def test_an_unrestricted_model_takes_any_clean_instruction(tmp_path):
    assert (
        form_for(tmp_path, {"prompt": "wave at the camera"})[2].prompt
        == "wave at the camera"
    )


def test_the_restricted_rule_holds_even_if_the_site_allows_unknown_prompts_in_the_client(
    tmp_path,
):
    site = make_site(tmp_path, safety={"allow_unknown_prompt": True})
    restricted = model(id="pi05_fr3_sp_v1", restricted=True)
    error = refused(tmp_path, {"prompt": "wave"}, site=site, mdl=restricted)
    assert error.code == "prompt_unknown_for_model"


def test_prompt_problem_gives_the_reason_or_none(tmp_path):
    site, mdl = make_site(tmp_path), model()
    assert C.prompt_problem(PROMPT, site, mdl) is None
    assert C.prompt_problem("a$b", site, mdl) == "forbidden_char"
    restricted = model(id="pi05_fr3_sp_v1", restricted=True)
    assert C.prompt_problem("wave", site, restricted) == "unknown_for_model"
    assert C.prompt_problem(PROMPT, site, restricted) == "unknown_for_model"
    assert C.prompt_problem(M.RESTRICTED_PROMPTS[1], site, restricted) is None


# --- what the form cannot touch --------------------------------------------------------------------

LOCKED = [
    "max_xy",
    "max_step_m",
    "max_step_rad",
    "rx_range",
    "ry_range",
    "rz_range",
    "binarize_gripper",
    "require_matching_server",
    "allow_unknown_prompt",
    "home_duration_s",
    "home_pose",
    "levi_mode",
    "remote_host",
    "remote_port",
    "view_camera_id",
    "wrist_camera_id",
    "franka_server_url",
    "rollout_root",
    "new",
    "step_by_step",
    "max-xy",
    "--max-xy",
    "levi-mode",
    "unknown",
]


@pytest.mark.parametrize("name", LOCKED)
def test_no_fixed_flag_is_accepted_from_the_form(tmp_path, name):
    for value in (0.5, "on", True, [1, 2], None):
        error = refused(tmp_path, {name: value})
        assert (error.code, error.field) == ("unknown_field", name)


def test_the_form_has_exactly_the_documented_fields():
    import dataclasses

    assert [f.name for f in dataclasses.fields(C.Form)] == [
        "model_id",
        "prompt",
        "max_steps",
        "eval_num",
        "min_z",
        "rtc",
        "rtc_horizon",
        "rollout_group",
        "note",
    ]


def test_the_flags_of_the_form_are_a_closed_list():
    from_form = {flag for flag, _, source in C.FLAGS if source == "form"}
    assert from_form == {
        "--prompt",
        "--max-timesteps",
        "--eval-num",
        "--min-z",
        "--rtc",
        "--rtc-execute-horizon",
        "--open-loop-horizon",
        "--rollout-group",
        "--eval-note",
    }
    assert {flag for flag, _, source in C.FLAGS if source == "const"} == {
        "--levi-mode",
        "--new",
        "--step-by-step",
    }


def test_the_fixed_flags_follow_the_site_alone(tmp_path):
    site = make_site(
        tmp_path,
        safety={"max_xy": 0.5, "binarize_gripper": False, "rx_range": [1.0, 3.0]},
        home={"duration_s": 4.5},
    )
    argv = rendered(tmp_path, site=site).argv
    assert "--max-xy=0.5" in argv and "--no-binarize-gripper" in argv
    assert argv[argv.index("--rx-range") : argv.index("--rx-range") + 3] == [
        "--rx-range",
        "1.0",
        "3.0",
    ]
    assert "--home-duration-s=4.5" in argv


def test_the_connection_comes_from_the_site(tmp_path):
    site = make_site(
        tmp_path,
        server={
            "port": 8123,
            "remote_host": "10.0.0.2",
            "robot_server_url": "http://10.0.0.2:5000/",
        },
        cameras={"view": "111111111", "wrist": "222222222"},
    )
    argv = rendered(tmp_path, site=site).argv
    for token in (
        "--remote-host=10.0.0.2",
        "--remote-port=8123",
        "--view-camera-id=111111111",
        "--wrist-camera-id=222222222",
        "--franka-server-url=http://10.0.0.2:5000/",
    ):
        assert token in argv


def test_the_unattended_mode_cannot_be_asked_for(tmp_path):
    argv = rendered(tmp_path).argv
    assert [t for t in argv if t.startswith("--levi-mode")] == ["--levi-mode=off"]
    assert refused(tmp_path, {"levi_mode": "on"}).code == "unknown_field"


def test_the_client_is_always_started_in_the_new_run_manual_stepping_off_form(tmp_path):
    argv = rendered(tmp_path).argv
    assert argv.count("--new") == 1 and argv.count("--no-step-by-step") == 1


def test_the_rollout_root_is_the_sites(tmp_path):
    assert f"--rollout-root={tmp_path / 'rollouts'}" in rendered(tmp_path).argv


def test_the_model_must_be_the_one_in_the_body(tmp_path):
    site, mdl = make_site(tmp_path), model()
    error = None
    with pytest.raises(C.FormError) as caught:
        C.validate(site, mdl, {"model_id": "other", "prompt": PROMPT})
    error = caught.value
    assert (error.code, error.field) == ("model_unknown", "model_id")
    assert (
        C.validate(site, mdl, {"model_id": mdl.id, "prompt": PROMPT}).model_id == mdl.id
    )


# --- warnings -----------------------------------------------------------------------------------------


def codes(preview):
    return [w["code"] for w in preview.warnings]


def test_the_default_form_warns_only_about_the_task_table(tmp_path):
    assert codes(rendered(tmp_path)) == ["min_z_below_task_table"]


def test_an_unverified_model_is_warned_about(tmp_path):
    preview = rendered(tmp_path, mdl=model(verified=False))
    assert "unverified_model" in codes(preview)


@pytest.mark.parametrize("horizon,warned", [(6, False), (7, False), (8, True)])
def test_a_small_rtc_margin_is_warned_about(tmp_path, horizon, warned):
    preview = rendered(tmp_path, {"rtc_horizon": horizon})
    assert ("rtc_margin_low" in codes(preview)) is warned


def test_the_margin_warning_is_for_rtc_only(tmp_path):
    assert "rtc_margin_low" not in codes(
        rendered(tmp_path, {"rtc": False, "rtc_horizon": 10})
    )


@pytest.mark.parametrize(
    "min_z,warned", [(0.025, True), (0.05, True), (0.05325, False), (0.06, False)]
)
def test_a_floor_below_the_measured_task_tables_is_warned_about(
    tmp_path, min_z, warned
):
    assert (
        "min_z_below_task_table" in codes(rendered(tmp_path, {"min_z": min_z}))
    ) is warned


SCREWS, PLATES = M.RESTRICTED_PROMPTS


@pytest.mark.parametrize(
    "prompt,min_z,warned",
    [
        # The client's table: screws 0.075, plates 0.05325 (TASK_MIN_Z); an
        # explicit --min-z beats it there, so a number between them is a lower
        # floor than the client would have used for screws.
        (SCREWS, 0.06, True),
        (SCREWS, 0.07, True),
        (SCREWS, 0.075, False),
        (SCREWS, 0.1, False),
        (PLATES, 0.06, False),
        (PLATES, 0.05, True),
        ("stack_the_plates_of_same_color_together", 0.05, True),
        (PROMPT, 0.06, False),
        (PROMPT, 0.05, True),
    ],
)
def test_the_floor_is_compared_with_the_table_of_the_task(
    tmp_path, prompt, min_z, warned
):
    preview = rendered(tmp_path, {"prompt": prompt, "min_z": min_z})
    assert ("min_z_below_task_table" in codes(preview)) is warned


def test_the_warning_names_the_floor_of_the_task(tmp_path):
    text = {
        w["code"]: w["text"]
        for w in rendered(tmp_path, {"prompt": SCREWS, "min_z": 0.06}).warnings
    }["min_z_below_task_table"]
    assert "0.075" in text and "0.06 " in text


def test_every_warning_has_a_code_and_a_text_without_a_path(tmp_path):
    preview = rendered(tmp_path, {"rtc_horizon": 8}, mdl=model(verified=False))
    assert set(codes(preview)) == {
        "unverified_model",
        "rtc_margin_low",
        "min_z_below_task_table",
    }
    for warning in preview.warnings:
        assert set(warning) == {"code", "text"} and warning["text"]
        assert "/" not in warning["text"]


def test_the_hands_sentence_says_what_this_page_does_not_do():
    assert C.HANDS == (
        "Keep a hand on the physical e-stop. This page never starts the arm stack or this client."
    )


# --- the self-check ------------------------------------------------------------------------------------


def good(tmp_path):
    site = make_site(tmp_path)
    return site, list(rendered(tmp_path).argv)


def test_a_rendered_command_passes_its_own_check(tmp_path):
    site, argv = good(tmp_path)
    C.verify_argv(argv, site, False)
    off = rendered(tmp_path, {"rtc": False}).argv
    C.verify_argv(off, site, False)


def test_the_self_check_holds_a_restricted_model_to_the_known_instructions(tmp_path):
    site = make_site(tmp_path)
    restricted = model(id="pi05_fr3_sp_v1", restricted=True)
    # A hand-made form skips validate(); render still refuses to print it.
    form = C.Form(
        restricted.id, "wave at the camera", 500, 5, 0.06, True, 6, "group", "note"
    )
    with pytest.raises(C.SelfCheckError, match="prompt_unknown_for_model"):
        C.render(site, restricted, form)
    good_form = C.Form(restricted.id, SCREWS, 500, 5, 0.075, True, 6, "group", "note")
    argv = C.render(site, restricted, good_form).argv
    C.verify_argv(argv, site, True)
    # The same words on a model that takes any instruction are fine.
    other = C.Form(
        "pi05_fr3_all_step49999",
        "wave at the camera",
        500,
        5,
        0.06,
        True,
        6,
        "group",
        "note",
    )
    assert C.render(site, model(), other).argv


def broken(argv, edit):
    out = list(argv)
    edit(out)
    return out


def swap(old, new):
    def edit(argv):
        argv[argv.index(old)] = new

    return edit


CHECKS = [
    ("a second min-z", lambda a: a.append("--min-z=0.5")),
    ("an unknown flag", lambda a: a.append("--dangerous")),
    ("a positional", lambda a: a.append("extra")),
    ("an empty value", swap("--eval-num=5", "--eval-num=")),
    ("a value flag without a value", swap("--eval-num=5", "--eval-num")),
    ("a tuple with one number missing", lambda a: a.pop(a.index("--rx-range") + 1)),
    ("a tuple with a word", swap("4.03", "x")),
    ("a tuple with a flag inside", swap("4.03", "--new")),
    ("a changed safety limit", swap("--max-xy=0.85", "--max-xy=5")),
    ("a changed safety range", swap("0.96", "9")),
    ("a safety switch turned off", swap("--binarize-gripper", "--no-binarize-gripper")),
    (
        "unknown prompts allowed",
        swap("--no-allow-unknown-prompt", "--allow-unknown-prompt"),
    ),
    ("the unattended mode", swap("--levi-mode=off", "--levi-mode=on")),
    ("the mode flag in two words", swap("--levi-mode=off", "--levi-mode")),
    ("step by step turned on", swap("--no-step-by-step", "--step-by-step")),
    ("a missing flag", lambda a: a.remove("--new")),
    ("a missing min-z", lambda a: a.remove("--min-z=0.025")),
    ("a min-z under the floor", swap("--min-z=0.025", "--min-z=0.01")),
    ("a min-z of zero", swap("--min-z=0.025", "--min-z=0")),
    ("a min-z at the home height", swap("--min-z=0.025", "--min-z=0.235951")),
    ("a min-z that is not a number", swap("--min-z=0.025", "--min-z=low")),
    (
        "a rollout root elsewhere",
        lambda a: a.__setitem__(-3, "--rollout-root=/tmp/elsewhere"),
    ),
    (
        "a bad rollout group",
        swap("--rollout-group=pi05_fr3_all_step49999_rtc_demo", "--rollout-group=../x"),
    ),
    ("steps above the cap", swap("--max-timesteps=500", "--max-timesteps=3001")),
    ("an episode count above the cap", swap("--eval-num=5", "--eval-num=51")),
    (
        "an rtc horizon above the cap",
        swap("--rtc-execute-horizon=6", "--rtc-execute-horizon=9"),
    ),
    ("rtc without a horizon", lambda a: a.remove("--rtc-execute-horizon=6")),
    ("a horizon without rtc", lambda a: a.remove("--rtc")),
    ("rtc and open loop together", lambda a: a.insert(-3, "--open-loop-horizon=6")),
    ("another host", swap("--remote-host=127.0.0.1", "--remote-host=10.9.9.9")),
    ("another port", swap("--remote-port=8000", "--remote-port=8001")),
    ("a changed camera", swap("--view-camera-id=262622274256", "--view-camera-id=1")),
    (
        "a changed robot url",
        swap(
            "--franka-server-url=http://127.0.0.1:5000/",
            "--franka-server-url=http://x/",
        ),
    ),
    ("a changed home pose", swap("0.481902", "0.5")),
    ("a prompt with a quote", swap(f"--prompt={PROMPT}", "--prompt=pick 'it'")),
    ("a prompt starting with a dash", swap(f"--prompt={PROMPT}", "--prompt=-x")),
    ("a note with a dollar", lambda a: a.__setitem__(-1, "--eval-note=a$b")),
    ("a changed prefix", lambda a: a.__setitem__(0, "sudo")),
    ("a missing environment guard", lambda a: a.pop(1)),
    ("a shortened prefix", lambda a: a.__delitem__(slice(0, 11))),
]


# What the self-check says for each break; every other one "differs".
SAYS = {
    "a second min-z": "appears twice",
    "an unknown flag": "unknown flag",
    "a positional": "unknown flag",
    "the mode flag in two words": "has no value",
    "an empty value": "has no value",
    "a value flag without a value": "has no value",
    "a tuple with one number missing": "needs 2 numbers",
    "a tuple with a word": "needs 2 numbers",
    "a tuple with a flag inside": "needs 2 numbers",
    "a missing min-z": "missing or malformed",
    "rtc without a horizon": "missing or malformed",
    "a horizon without rtc": "missing or malformed",
    "a min-z that is not a number": "missing or malformed",
    "a min-z under the floor": "min_z_below_floor",
    "a min-z of zero": "min_z_not_positive",
    "a min-z at the home height": "min_z_above_home",
    "steps above the cap": "max_steps_out_of_range",
    "an episode count above the cap": "eval_num_out_of_range",
    "an rtc horizon above the cap": "rtc_horizon_out_of_range",
    "a bad rollout group": "rollout_group_invalid",
    "a prompt with a quote": "prompt_invalid",
    "a prompt starting with a dash": "prompt_invalid",
    "a note with a dollar": "note_invalid",
    "a changed prefix": "fixed prefix",
    "a missing environment guard": "fixed prefix",
    "a shortened prefix": "fixed prefix",
}


@pytest.mark.parametrize("edit", [c[1] for c in CHECKS], ids=[c[0] for c in CHECKS])
def test_the_self_check_refuses_a_broken_command(tmp_path, edit, request):
    name = request.node.callspec.id
    site, argv = good(tmp_path)
    with pytest.raises(C.SelfCheckError, match=SAYS.get(name, "differs from what")):
        C.verify_argv(broken(argv, edit), site, False)


def test_the_self_check_is_what_render_ends_with(tmp_path, monkeypatch):
    site, mdl, form = form_for(tmp_path)
    calls = []
    monkeypatch.setattr(
        C, "verify_argv", lambda argv, s, r: calls.append((list(argv), s, r))
    )
    preview = C.render(site, mdl, form)
    assert calls == [(preview.argv, site, mdl.restricted)]


def test_render_refuses_a_form_for_another_model(tmp_path):
    site, _, form = form_for(tmp_path)
    with pytest.raises(ValueError):
        C.render(site, model(id="other"), form)


def test_quote_is_shlex_quote():
    assert C.quote("a b") == "'a b'" and C.quote("--x=1") == "--x=1"


# --- purity ------------------------------------------------------------------------------------------------


def test_the_renderer_imports_no_process_socket_or_file_module():
    tree = ast.parse((ROOT / "levi/marvel0/command.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(
                (node.module or "").split(".")[0] if not node.level else "relative"
            )
    assert imported <= {
        "math",
        "re",
        "shlex",
        "dataclasses",
        "relative",
        "__future__",
    }, imported
    calls = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert not calls & {"open", "exec", "eval", "compile", "__import__", "input"}


def test_the_preview_holds_a_path_only_where_the_text_to_copy_needs_one(tmp_path):
    preview = rendered(tmp_path)
    assert str(tmp_path) not in json.dumps(preview.warnings)
    assert str(tmp_path) not in json.dumps(preview.env)
    pattern = re.compile(r"(?<![\w.:-])/(?:[A-Za-z0-9_.~+@%=,-]+/)+")
    assert not pattern.search(json.dumps(preview.warnings))
    assert str(tmp_path / "openpi") in preview.command


def test_the_prefix_is_the_aeri_form():
    assert C.PREFIX == [
        "env",
        "-u",
        "VIRTUAL_ENV",
        "no_proxy=localhost,127.0.0.1,::1",
        "uv",
        "run",
        "--no-sync",
        "--with",
        "pyrealsense2==2.55.1.6486",
        "python",
        SCRIPT,
    ]
