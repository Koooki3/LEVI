"""The control rate of the demo client command (``--control-hz``), in
``levi/marvel0/command.py``: exactly 5, 10, 20, 25 or 50; 10 is the default and
writes nothing, so the command at 10 Hz is the one this page made before the
option existed (``fixtures/command_golden_10hz.json``, taken from main before
the option was written); the other rates write the flag, scale the per-step
caps above 10 Hz, lower the default RTC horizon, and warn.

The expected numbers are written out here, not computed with the module's
formulas, so a formula that drifts fails a test."""

import json
import sys
from pathlib import Path

import pytest
from site_sample import make_site
from test_marvel0_command import PROMPT, golden_argv, model, refused, rendered
from test_marvel0_command import form_for as make_form

from levi.marvel0 import command as C

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "scripts"))
GOLDEN = json.loads((HERE / "fixtures" / "command_golden_10hz.json").read_text())

# hz: (--max-step-m, --max-step-rad, the default RTC execute horizon,
#      latency beats, the warning codes of the default form, in order).
RATES = {
    5: ("0.1", "0.35", 6, 2),
    20: ("0.05", "0.175", 4, 6),
    25: ("0.04", "0.14", 2, 8),
    50: ("0.02", "0.07", 1, 15),
}
RATE_CODES = {
    5: ["min_z_below_task_table", "control_hz_not_trained", "control_hz_untested"],
    10: ["min_z_below_task_table"],
    20: [
        "min_z_below_task_table",
        "control_hz_not_trained",
        "control_hz_untested",
        "episode_short",
    ],
    25: [
        "min_z_below_task_table",
        "control_hz_not_trained",
        "control_hz_untested",
        "episode_short",
    ],
    50: [
        "rtc_never_fits",
        "min_z_below_task_table",
        "control_hz_not_trained",
        "control_hz_untested",
        "step_cap_tight",
        "episode_short",
    ],
}


def codes(preview):
    return [w["code"] for w in preview.warnings]


# --- 10 Hz is what it always was ------------------------------------------------------------


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=lambda c: c["name"])
@pytest.mark.parametrize("explicit", [False, True], ids=["omitted", "explicit_10"])
def test_the_command_at_10_hz_is_byte_identical_to_main(tmp_path, case, explicit):
    site = make_site(tmp_path)
    mdl = model(**case["model"])
    body = {**case["body"], **({"control_hz": 10} if explicit else {})}
    preview = C.render(site, mdl, C.validate(site, mdl, body))
    sub = lambda text: text.replace("{ROOT}", str(tmp_path))
    assert preview.argv == [sub(t) for t in case["argv"]]
    assert preview.command == sub(case["command"])
    assert preview.cwd == sub(case["cwd"]) and preview.env == case["env"]
    assert preview.warnings == case["warnings"]


def test_the_golden_file_covers_the_forms_that_matter():
    names = {c["name"] for c in GOLDEN["cases"]}
    assert {
        "default_rtc_on",
        "rtc_off_open_loop_default",
        "rtc_horizon_8_margin_low",
        "unverified_model",
        "restricted_screws_below_table",
    } <= names
    assert all(not any("control" in t for t in c["argv"]) for c in GOLDEN["cases"])
    assert all("hz" not in c["argv"][-1] for c in GOLDEN["cases"])


def test_10_hz_never_writes_the_flag_and_the_default_is_10(tmp_path):
    assert C.CONTROL_HZ_DEFAULT == 10 and C.CONTROL_HZ_OPTIONS == (5, 10, 20, 25, 50)
    assert C.REFERENCE_HZ == 10
    assert not any("control-hz" in t for t in rendered(tmp_path).argv)
    _, _, form = make_form(tmp_path)
    assert form.control_hz == 10


# --- the other rates ---------------------------------------------------------------------------


def expected_argv(tmp_path, hz):
    """The default form's argv at ``hz``, from the 10 Hz one."""
    cap_m, cap_rad, horizon, _ = RATES[hz]
    argv = golden_argv(tmp_path, ["--rtc", f"--rtc-execute-horizon={horizon}"])
    argv.insert(argv.index("--max-timesteps=500") + 1, f"--control-hz={hz}")
    argv[argv.index("--max-step-m=0.1")] = f"--max-step-m={cap_m}"
    argv[argv.index("--max-step-rad=0.35")] = f"--max-step-rad={cap_rad}"
    argv[-1] = (
        f"--eval-note=marvel0 page: pi05_fr3_all_step49999 rtc=on horizon={horizon} hz={hz}"
    )
    return argv


@pytest.mark.parametrize("hz", sorted(RATES))
def test_a_rate_writes_the_flag_the_scaled_caps_and_the_horizon(tmp_path, hz):
    preview = rendered(tmp_path, {"control_hz": hz})
    assert preview.argv == expected_argv(tmp_path, hz)
    assert preview.argv.count(f"--control-hz={hz}") == 1


@pytest.mark.parametrize("hz", [20, 50])
def test_the_flag_sits_next_to_the_step_limit_in_the_text(tmp_path, hz):
    text = rendered(tmp_path, {"control_hz": hz}).command
    assert (
        f"\n  --max-timesteps=500 --control-hz={hz} --eval-num=5 --min-z=0.025 \\\n"
        in text
    )
    cap_m, cap_rad, _, _ = RATES[hz]
    assert f"--max-step-m={cap_m} --max-step-rad={cap_rad} --rx-range" in text


@pytest.mark.parametrize("hz", [20, 25, 50])
def test_above_10_hz_the_speed_the_caps_allow_is_the_one_of_10_hz(tmp_path, hz):
    preview = rendered(tmp_path, {"control_hz": hz})
    by_flag = dict(t.split("=", 1) for t in preview.argv if "=" in t)
    assert float(by_flag["--max-step-m"]) * hz == pytest.approx(1.0)
    assert float(by_flag["--max-step-rad"]) * hz == pytest.approx(3.5)


def test_at_5_hz_the_caps_stay_and_so_the_speed_they_allow_halves(tmp_path):
    preview = rendered(tmp_path, {"control_hz": 5})
    assert "--max-step-m=0.1" in preview.argv and "--max-step-rad=0.35" in preview.argv
    assert preview.control["speed_cap_m_s"] == 0.5
    assert preview.control["speed_cap_rad_s"] == 1.75


@pytest.mark.parametrize("hz", [20, 25, 50])
def test_the_scaling_follows_the_site_file_not_a_constant(tmp_path, hz):
    site = make_site(tmp_path, safety={"max_step_m": 0.06, "max_step_rad": 0.2})
    mdl = model()
    form = C.validate(site, mdl, {"prompt": PROMPT, "control_hz": hz})
    argv = C.render(site, mdl, form).argv
    wanted = {20: ("0.03", "0.1"), 25: ("0.024", "0.08"), 50: ("0.012", "0.04")}[hz]
    assert (f"--max-step-m={wanted[0]}", f"--max-step-rad={wanted[1]}") == (
        next(t for t in argv if t.startswith("--max-step-m=")),
        next(t for t in argv if t.startswith("--max-step-rad=")),
    )
    # and a site with a smaller cap is never raised by a lower rate
    low = C.validate(site, mdl, {"prompt": PROMPT, "control_hz": 5})
    argv = C.render(site, mdl, low).argv
    assert "--max-step-m=0.06" in argv and "--max-step-rad=0.2" in argv


def test_everything_else_in_the_command_is_what_it_is_at_10_hz(tmp_path):
    base = rendered(tmp_path, {"control_hz": 10, "rtc_horizon": 4}).argv
    for hz in (5, 20, 25, 50):
        argv = rendered(tmp_path, {"control_hz": hz, "rtc_horizon": 4}).argv
        changed = {
            t.split("=", 1)[0] for t in set(argv) ^ set(base) if t.startswith("--")
        }
        assert changed <= {
            "--control-hz",
            "--max-step-m",
            "--max-step-rad",
            "--eval-note",
        }, hz
        for flag in (
            "--home-duration-s=3.0",
            "--max-xy=0.85",
            "--min-z=0.025",
            "--max-timesteps=500",
            "--eval-num=5",
            "--binarize-gripper",
        ):
            assert flag in argv, (hz, flag)


def test_an_explicit_rtc_horizon_is_kept_as_typed_at_every_rate(tmp_path):
    for hz in (5, 20, 25, 50):
        argv = rendered(tmp_path, {"control_hz": hz, "rtc_horizon": 6}).argv
        assert "--rtc-execute-horizon=6" in argv, hz


def test_the_open_loop_horizon_is_not_changed_by_the_rate(tmp_path):
    for hz in (5, 20, 50):
        argv = rendered(tmp_path, {"control_hz": hz, "rtc": False}).argv
        assert "--open-loop-horizon=6" in argv and "--rtc" not in argv, hz


def test_a_site_default_horizon_is_lowered_only_above_10_hz(tmp_path):
    site = make_site(tmp_path, caps={"rtc_horizon_default": 8})
    assert C.rtc_horizon_default(8, 5) == 8 and C.rtc_horizon_default(8, 10) == 8
    assert C.rtc_horizon_default(8, 20) == 4 and C.rtc_horizon_default(3, 20) == 3
    assert C.rtc_horizon_default(8, 50) == 1
    form = C.validate(site, model(), {"prompt": PROMPT, "control_hz": 10})
    assert form.rtc_horizon == 8


def test_a_note_of_your_own_is_not_touched_and_the_default_one_names_the_rate(tmp_path):
    own = rendered(tmp_path, {"control_hz": 25, "note": "my note"}).argv
    assert "--eval-note=my note" in own
    default = rendered(tmp_path, {"control_hz": 25}).argv
    assert default[-1].endswith("horizon=2 hz=25")


# --- the form is closed -----------------------------------------------------------------------


@pytest.mark.parametrize("value", [15, 0, -5, 1, 4, 11, 12, 30, 100, 1000, 2**70])
def test_a_rate_that_is_not_an_option_is_refused_with_a_code(tmp_path, value):
    error = refused(tmp_path, {"control_hz": value})
    assert (error.code, error.field) == ("control_hz_unsupported", "control_hz")
    assert error.extra == {"options": [5, 10, 20, 25, 50]}


@pytest.mark.parametrize(
    "value",
    [
        "10; rm -rf /",
        "20",
        "20.0",
        20.0,
        10.0,
        7.5,
        float("nan"),
        True,
        False,
        [20],
        {"hz": 20},
        "",
    ],
    ids=repr,
)
def test_a_rate_of_another_type_is_refused_as_not_a_whole_number(tmp_path, value):
    error = refused(tmp_path, {"control_hz": value})
    assert (error.code, error.field) == ("invalid_type", "control_hz")


def test_a_missing_or_null_rate_is_the_default(tmp_path):
    assert make_form(tmp_path, {"control_hz": None})[2].control_hz == 10
    assert make_form(tmp_path, {})[2].control_hz == 10


def test_the_rate_is_a_field_of_the_form_and_a_flag_of_the_form(tmp_path):
    assert "control_hz" in C.FIELDS
    assert ("--control-hz", "value", "form") in C.FLAGS
    assert refused(tmp_path, {"control-hz": 20}).code == "unknown_field"
    assert refused(tmp_path, {"hz": 20}).code == "unknown_field"


# --- the command checks its own output ----------------------------------------------------------


@pytest.fixture
def twenty(tmp_path):
    site, mdl, form = make_form(tmp_path, {"control_hz": 20})
    return site, C.render(site, mdl, form).argv


@pytest.mark.parametrize(
    "edit",
    [
        lambda a: [t if t != "--control-hz=20" else "--control-hz=15" for t in a],
        lambda a: [t if t != "--control-hz=20" else "--control-hz=10" for t in a],
        lambda a: [t if t != "--control-hz=20" else "--control-hz=20.0" for t in a],
        lambda a: [t if t != "--control-hz=20" else "--control-hz=" for t in a],
        lambda a: [t if t != "--control-hz=20" else "--control-hz=abc" for t in a],
        lambda a: [t for t in a if t != "--control-hz=20"],
        lambda a: [*a, "--control-hz=20"],
        lambda a: [t if t != "--max-step-m=0.05" else "--max-step-m=0.1" for t in a],
        lambda a: [
            t if t != "--max-step-rad=0.175" else "--max-step-rad=0.35" for t in a
        ],
        lambda a: [t if t != "--max-step-m=0.05" else "--max-step-m=0.5" for t in a],
        lambda a: [t if t != "--max-step-m=0.05" else "--max-step-m=0.04" for t in a],
        lambda a: [t if t != "--control-hz=20" else "--control-hz" for t in a],
    ],
    ids=[
        "unknown_rate",
        "explicit_10_with_scaled_caps",
        "float_rate",
        "empty_rate",
        "text_rate",
        "caps_scaled_for_a_rate_that_is_gone",
        "rate_twice",
        "caps_not_scaled",
        "rad_cap_not_scaled",
        "cap_raised",
        "cap_scaled_for_another_rate",
        "rate_without_value",
    ],
)
def test_a_command_with_a_rate_or_cap_the_form_would_not_make_is_refused(twenty, edit):
    site, argv = twenty
    C.verify_argv(argv, site, False)
    with pytest.raises(C.SelfCheckError):
        C.verify_argv(edit(list(argv)), site, False)


# --- the warnings ---------------------------------------------------------------------------------


@pytest.mark.parametrize("hz", sorted(RATE_CODES))
def test_the_warnings_of_each_rate(tmp_path, hz):
    assert codes(rendered(tmp_path, {"control_hz": hz})) == RATE_CODES[hz]


def test_the_warning_of_a_rate_other_than_10_says_the_policy_is_trained_at_10(tmp_path):
    warnings = {w["code"]: w for w in rendered(tmp_path, {"control_hz": 20}).warnings}
    trained = warnings["control_hz_not_trained"]
    assert "trained on data recorded at 10 Hz" in trained["text"]
    assert "2 times that speed" in trained["text"]
    assert trained["params"] == {"hz": 20, "speed": "2"}
    half = {w["code"]: w for w in rendered(tmp_path, {"control_hz": 5}).warnings}
    assert half["control_hz_not_trained"]["params"]["speed"] == "0.5"


def test_the_first_use_note_names_the_owner_and_the_estop(tmp_path):
    warning = next(
        w
        for w in rendered(tmp_path, {"control_hz": 25}).warnings
        if w["code"] == "control_hz_untested"
    )
    assert "speed profile" in warning["text"] and "not been tried" in warning["text"]
    assert "owner present" in warning["text"] and "e-stop" in warning["text"]


def test_a_warning_only_carries_params_where_the_page_needs_numbers(tmp_path):
    for w in rendered(tmp_path).warnings:
        assert "params" not in w, w
    for w in rendered(tmp_path, {"control_hz": 50}).warnings:
        if w["code"] in {"min_z_below_task_table", "unverified_model"}:
            assert "params" not in w
        else:
            assert isinstance(w["params"], dict) and w["params"], w["code"]


@pytest.mark.parametrize(
    "hz, horizon, steps, expected",
    [
        (20, 4, 500, None),  # 4 + 6 beats = 10 of 10
        (20, 5, 500, (5, 6, 11)),
        (20, 6, 500, (6, 6, 12)),
        (25, 2, 500, None),  # 2 + 8 = 10
        (25, 3, 500, (3, 8, 11)),
        (5, 8, 500, None),  # 8 + 2 = 10
        (5, 9, 500, (9, 2, 11)),  # a wider site cap than the sample's
        (10, 7, 500, None),  # 7 + 3 = 10
        (10, 8, 500, (8, 3, 11)),
    ],
)
def test_the_rtc_margin_warning_counts_beats_of_the_chosen_rate(
    tmp_path, hz, horizon, steps, expected
):
    site = make_site(tmp_path, caps={"rtc_horizon_max": 9})
    mdl = model()
    form = C.validate(
        site,
        mdl,
        {
            "prompt": PROMPT,
            "control_hz": hz,
            "rtc_horizon": horizon,
            "max_steps": steps,
        },
    )
    found = [
        w for w in C.render(site, mdl, form).warnings if w["code"] == "rtc_margin_low"
    ]
    if expected is None:
        assert not found
        return
    (warning,) = found
    shown, beats, reached = expected
    assert f"Execute horizon {shown} plus about {beats} beats" in warning["text"]
    assert f"reaches {reached} of 10 actions per chunk" in warning["text"]
    if hz == 10:
        assert "params" not in warning
        assert "At 10 Hz a beat" not in warning["text"]
    else:
        assert warning["params"]["beats"] == beats
        assert warning["params"]["reached"] == reached
        assert f"At {hz} Hz a beat is" in warning["text"]


def test_at_50_hz_not_even_a_horizon_of_1_fits(tmp_path):
    preview = rendered(tmp_path, {"control_hz": 50, "rtc_horizon": 1})
    (warning,) = [w for w in preview.warnings if w["code"] == "rtc_never_fits"]
    assert (
        "200 ms" in warning["text"]
        and "even an execute horizon of 1 reaches 16" in warning["text"]
    )
    assert warning["params"] == {"hz": 50, "beats": 15, "chunk": 10, "chunk_ms": 200.0}
    assert "rtc_margin_low" not in codes(preview)
    # open loop has no RTC margin
    assert "rtc_never_fits" not in codes(
        rendered(tmp_path, {"control_hz": 50, "rtc": False})
    )


@pytest.mark.parametrize(
    "hz, steps, short",
    [
        (5, 10, False),  # below 10 Hz the rule is silent
        (10, 100, False),
        (20, 500, True),  # 25 s
        (20, 600, False),  # exactly 30 s
        (20, 599, True),
        (25, 750, False),
        (25, 749, True),
        (50, 1500, False),
        (50, 1499, True),
        (50, 3000, False),
    ],
)
def test_a_short_episode_is_warned_about_above_10_hz_and_the_cap_is_not_raised(
    tmp_path, hz, steps, short
):
    preview = rendered(tmp_path, {"control_hz": hz, "max_steps": steps})
    assert ("episode_short" in codes(preview)) is short
    assert f"--max-timesteps={steps}" in preview.argv
    if short:
        warning = next(w for w in preview.warnings if w["code"] == "episode_short")
        assert warning["params"] == {
            "steps": steps,
            "hz": hz,
            "seconds": steps / hz,
            "minimum": 30,
        }
        assert "not raised for you" in warning["text"]


def test_the_step_cap_of_the_site_stays_in_steps_whatever_the_rate(tmp_path):
    site = make_site(tmp_path)
    mdl = model()
    for hz in C.CONTROL_HZ_OPTIONS:
        with pytest.raises(C.FormError) as caught:
            C.validate(
                site, mdl, {"prompt": PROMPT, "control_hz": hz, "max_steps": 3001}
            )
        assert caught.value.code == "max_steps_out_of_range"
        assert C.validate(
            site, mdl, {"prompt": PROMPT, "control_hz": hz, "max_steps": 3000}
        )


@pytest.mark.parametrize(
    "hz, tight", [(5, False), (10, False), (20, False), (25, False), (50, True)]
)
def test_the_step_cap_warning_is_for_caps_below_the_training_steps(tmp_path, hz, tight):
    preview = rendered(tmp_path, {"control_hz": hz})
    assert ("step_cap_tight" in codes(preview)) is tight
    if tight:
        warning = next(w for w in preview.warnings if w["code"] == "step_cap_tight")
        assert warning["params"] == {
            "hz": 50,
            "cap_m": 0.02,
            "cap_rad": 0.07,
            "p99_m": 0.0384,
            "p99_rad": 0.0883,
        }
        assert "reject chunks" in warning["text"]


@pytest.mark.parametrize(
    "safety, tight",
    [
        ({"max_step_m": 0.1, "max_step_rad": 0.35}, False),
        ({"max_step_m": 0.05, "max_step_rad": 0.35}, True),  # 0.02 m at 25 Hz
        ({"max_step_m": 0.1, "max_step_rad": 0.15}, True),  # 0.06 rad at 25 Hz
        ({"max_step_m": 0.1, "max_step_rad": 0.25}, False),  # 0.1 rad is still above
    ],
)
def test_either_scaled_cap_below_the_training_steps_is_tight(tmp_path, safety, tight):
    site = make_site(tmp_path, safety=safety)
    mdl = model()
    form = C.validate(site, mdl, {"prompt": PROMPT, "control_hz": 25})
    warnings = C.render(site, mdl, form).warnings
    assert ("step_cap_tight" in [w["code"] for w in warnings]) is tight


# --- what the page shows next to the field ------------------------------------------------------------


EXPECTED_SUMMARY = {
    5: {
        "period_ms": 200.0,
        "episode_s": 100.0,
        "step_cap_m": 0.1,
        "step_cap_rad": 0.35,
        "speed_cap_m_s": 0.5,
        "speed_cap_rad_s": 1.75,
        "chunk_s": 2.0,
        "horizon_s": 1.2,
        "latency_beats": 2,
        "rtc": {"horizon": 6, "reached": 8, "margin": 2},
    },
    10: {
        "period_ms": 100.0,
        "episode_s": 50.0,
        "step_cap_m": 0.1,
        "step_cap_rad": 0.35,
        "speed_cap_m_s": 1.0,
        "speed_cap_rad_s": 3.5,
        "chunk_s": 1.0,
        "horizon_s": 0.6,
        "latency_beats": 3,
        "rtc": {"horizon": 6, "reached": 9, "margin": 1},
    },
    20: {
        "period_ms": 50.0,
        "episode_s": 25.0,
        "step_cap_m": 0.05,
        "step_cap_rad": 0.175,
        "speed_cap_m_s": 1.0,
        "speed_cap_rad_s": 3.5,
        "chunk_s": 0.5,
        "horizon_s": 0.2,
        "latency_beats": 6,
        "rtc": {"horizon": 4, "reached": 10, "margin": 0},
    },
    25: {
        "period_ms": 40.0,
        "episode_s": 20.0,
        "step_cap_m": 0.04,
        "step_cap_rad": 0.14,
        "speed_cap_m_s": 1.0,
        "speed_cap_rad_s": 3.5,
        "chunk_s": 0.4,
        "horizon_s": 0.08,
        "latency_beats": 8,
        "rtc": {"horizon": 2, "reached": 10, "margin": 0},
    },
    50: {
        "period_ms": 20.0,
        "episode_s": 10.0,
        "step_cap_m": 0.02,
        "step_cap_rad": 0.07,
        "speed_cap_m_s": 1.0,
        "speed_cap_rad_s": 3.5,
        "chunk_s": 0.2,
        "horizon_s": 0.02,
        "latency_beats": 15,
        "rtc": {"horizon": 1, "reached": 16, "margin": -6},
    },
}


@pytest.mark.parametrize("hz", sorted(EXPECTED_SUMMARY))
def test_the_summary_shows_every_derived_value(tmp_path, hz):
    summary = rendered(tmp_path, {"control_hz": hz}).control
    assert summary == {
        "inference_mode": "rtc",
        "vlash": None,
        "hz": hz,
        "reference_hz": 10,
        **EXPECTED_SUMMARY[hz],
    }


def test_the_summary_of_an_open_loop_form_has_no_rtc_margin(tmp_path):
    summary = rendered(tmp_path, {"control_hz": 20, "rtc": False}).control
    assert summary["rtc"] is None and summary["horizon_s"] == 0.3


def test_the_summary_follows_the_form_not_the_defaults(tmp_path):
    summary = rendered(
        tmp_path, {"control_hz": 25, "max_steps": 1000, "rtc_horizon": 5}
    ).control
    assert summary["episode_s"] == 40.0 and summary["horizon_s"] == 0.2
    assert summary["rtc"] == {"horizon": 5, "reached": 13, "margin": -3}


def test_the_public_numbers_hold_what_the_page_needs_to_do_the_same_sums(tmp_path):
    site = make_site(tmp_path)
    assert C.control_public(site) == {
        "options": [5, 10, 20, 25, 50],
        "default": 10,
        "reference_hz": 10,
        "action_horizon": 10,
        "latency_beats": 3,
        "short_episode_s": 30,
        "step_caps": {"m": 0.1, "rad": 0.35},
        "train_step_p99": {"m": 0.0384, "rad": 0.0883},
    }
    other = make_site(tmp_path, safety={"max_step_m": 0.06, "max_step_rad": 0.2})
    assert C.control_public(other)["step_caps"] == {"m": 0.06, "rad": 0.2}


# --- the client the rate is written for (read-only drift check) -----------------------------------

CLIENT_STRINGS = (
    "control_hz_field",
    "control_hz_spelling",
    "control_hz_metadata",
    "trained_at_10_fps",
    "step_cap_translation",
    "step_cap_rotation",
    "step_cap_measured_from",
    "loop_period",
    "home_steps_by_seconds",
    "rtc_period",
    "rtc_delay_ticks",
    "post_step_verify_zero",
)


def client_fixture() -> dict:
    return json.loads((HERE / "fixtures" / "client_prompts.json").read_text())[
        "prompts"
    ]


def client_tree(tmp_path, drop=None, name="client") -> Path:
    """A folder of client files holding every source string of the fixture except ``drop``'s."""
    root = tmp_path / name
    root.mkdir()
    texts: dict = {}
    for key, entry in client_fixture().items():
        if key != drop:
            texts.setdefault(entry["file"], []).append(entry["source"])
    for filename in {e["file"] for e in client_fixture().values()}:
        (root / filename).write_text("\n".join(texts.get(filename, [])) + "\n")
    return root


def test_the_drift_check_pins_the_flag_spelling_and_everything_the_rate_rules_stand_on():
    fixture = client_fixture()
    assert set(CLIENT_STRINGS) <= set(fixture)
    sources = "\n".join(fixture[key]["source"] for key in CLIENT_STRINGS)
    for literal in (
        "control_hz: float = 10.0",  # the flag --control-hz and its default
        "--control-hz",  # spelled as the page writes it
        "trained with action_horizon=10 at 10 fps",
        "if raw_step > args.max_step_m:",
        "if raw_rot.max() > args.max_step_rad:",
        "np.asarray(action[:3], np.float64) - current[:3]",
        "period = 1.0 / args.control_hz",
        "int(args.home_duration_s * args.control_hz)",
        "math.ceil(latency_s / self.period_s)",
        "post_step_verify_delay: float = 0.0",
    ):
        assert literal in sources, literal
    # and the page writes exactly that flag
    assert ("--control-hz", "value", "form") in C.FLAGS


@pytest.mark.parametrize("key", CLIENT_STRINGS)
def test_the_drift_check_fails_when_the_client_drops_a_string_the_rate_stands_on(
    tmp_path, capsys, key
):
    from check_client_prompts import main

    assert main(["--root", str(client_tree(tmp_path))]) == 0
    capsys.readouterr()
    assert main(["--root", str(client_tree(tmp_path, drop=key, name="dropped"))]) == 1
    assert f"MISSING {key}" in capsys.readouterr().out


def test_vlash_summary_keeps_its_mode_overlap_and_full_chunk_horizon(tmp_path):
    summary = rendered(
        tmp_path,
        {"inference_mode": "vlash", "rtc": False, "vlash_overlap_steps": 3},
    ).control
    assert summary == {
        "inference_mode": "vlash",
        "vlash": {"overlap_steps": 3, "experimental": True},
        "hz": 10,
        "reference_hz": 10,
        "period_ms": 100.0,
        "episode_s": 50.0,
        "step_cap_m": 0.1,
        "step_cap_rad": 0.35,
        "speed_cap_m_s": 1.0,
        "speed_cap_rad_s": 3.5,
        "chunk_s": 1.0,
        "horizon_s": 1.0,
        "latency_beats": 3,
        "rtc": None,
    }
