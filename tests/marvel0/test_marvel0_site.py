"""``levi/marvel0/site.py``: the marvel0 site file (machine values for the policy
deploy and the client command). Closed schema, found through ``LEVI_MARVEL0_SITE``
or the main checkout, read through safe file reads, cached by file identity,
and the missing or invalid answers every marvel0 route gives. Temporary paths
only; nothing of the real machine is looked at."""

import copy
import json
import math
import os
import re
import tomllib
from pathlib import Path

import pytest
from fastapi import HTTPException
from site_sample import dump_toml, make_site, merged, sample, write_site

from levi.marvel0 import errors
from levi.marvel0 import site as S

ENV = "LEVI_MARVEL0_SITE"
ROOT = Path(__file__).resolve().parents[2]
FLAGS_OF_THE_CONTRACT = [
    "--max-xy=0.85",
    "--max-step-m=0.1",
    "--max-step-rad=0.35",
    "--rx-range 1.5 4.03",
    "--ry-range -0.74 0.96",
    "--rz-range -2.44 0.77",
    "--binarize-gripper",
    "--require-matching-server",
    "--no-allow-unknown-prompt",
    "--home-duration-s=3.0",
]


@pytest.fixture(autouse=True)
def fresh_cache():
    S.clear_cache()
    yield
    S.clear_cache()


def invalid(tmp_path, mutate):
    """The ``errors`` of parsing the sample after ``mutate(data)``."""
    data = sample(tmp_path)
    mutate(data)
    with pytest.raises(S.SiteInvalid) as caught:
        S.parse(data, "env")
    return caught.value.errors


# --- the value ------------------------------------------------------------------


def test_the_sample_parses_to_every_field(tmp_path):
    site = make_site(tmp_path)
    assert site.origin == "env"
    assert site.openpi_checkout == str(tmp_path / "openpi")
    assert site.python == str(tmp_path / "openpi" / ".venv" / "bin" / "python")
    assert site.rollout_root == str(tmp_path / "rollouts")
    assert site.live.unit == "levi-live.service"
    assert site.ros_prefixes == (
        str(tmp_path / "ros"),
        str(tmp_path / "franka_control"),
    )
    assert (site.port, site.server.remote_host) == (8000, "127.0.0.1")
    assert site.server.robot_server_url == "http://127.0.0.1:5000/"
    assert (site.cameras.view, site.cameras.wrist) == ("262622274256", "262622275490")
    assert site.home.pose[2] == 0.235951 and site.home.duration_s == 3.0
    assert site.safety.min_z_floor == 0.025 and site.safety.rx_range == (1.5, 4.03)
    assert site.safety.binarize_gripper is True
    assert site.safety.allow_unknown_prompt is False
    assert (site.caps.max_steps_default, site.caps.max_steps_cap) == (500, 3000)
    assert site.gpu.fraction_presets == (0.22, 0.3, 0.45)
    assert (site.unit.nice, site.unit.cpu_affinity) == (10, "8-15")
    assert site.catalog.known_configs[0] == "pi05_fr3_all_state"
    assert site.catalog.verified == ("pi05_fr3_all_step49999",)
    assert site.prompts.default == "pick the eggplant in the blue plate"
    assert site.source.manifest == "" and len(site.source.hash_files) == 5


def test_the_value_is_immutable_and_hashable(tmp_path):
    site = make_site(tmp_path)
    with pytest.raises(AttributeError):
        site.python = "/x"
    with pytest.raises(AttributeError):
        site.safety.max_xy = 9.0
    assert hash(site) == hash(make_site(tmp_path))


def test_the_schema_and_the_value_have_the_same_keys():
    from dataclasses import fields

    for section, keys in S.SCHEMA_KEYS.items():
        if section == "paths":
            names = {f.name for f in fields(S.Site)}
        else:
            names = {f.name for f in fields(getattr(S, section.capitalize()))}
        assert set(keys) <= names, section
        assert set(keys) == names or section == "paths", section


def test_the_port_defaults_to_8000(tmp_path):
    data = sample(tmp_path)
    del data["server"]["port"]
    assert S.parse(data, "env").port == 8000


def test_the_limits_the_owner_set_are_still_allowed(tmp_path):
    site = make_site(
        tmp_path,
        gpu={"fraction_presets": [0.22, 0.30, 0.45], "margin_mib": 1024},
        home={"duration_s": 1.0},
        server={"port": 1024},
    )
    assert (site.gpu.margin_mib, site.home.duration_s, site.port) == (1024, 1.0, 1024)


def test_whole_numbers_are_accepted_where_a_float_is_expected(tmp_path):
    site = make_site(tmp_path, safety={"max_xy": 1, "rx_range": [1, 4]})
    assert site.safety.max_xy == 1.0 and isinstance(site.safety.max_xy, float)
    assert site.safety.rx_range == (1.0, 4.0)


# --- what is refused ------------------------------------------------------------

BAD = [
    ("unknown top-level key", lambda d: d.update(extra=1), "extra", "unknown_key"),
    (
        "unknown nested key",
        lambda d: d["safety"].update(max_zz=1.0),
        "safety.max_zz",
        "unknown_key",
    ),
    ("missing key", lambda d: d["safety"].pop("max_xy"), "safety.max_xy", "missing"),
    ("missing section", lambda d: d.pop("gpu"), "gpu", "missing"),
    ("section is not a table", lambda d: d.update(gpu=[1]), "gpu", "type"),
    (
        "wrong schema",
        lambda d: d.update(schema="levi.marvel0.site.v2"),
        "schema",
        "range",
    ),
    (
        "string for an int",
        lambda d: d["server"].update(port="8000"),
        "server.port",
        "type",
    ),
    ("bool for an int", lambda d: d["server"].update(port=True), "server.port", "type"),
    ("port 0", lambda d: d["server"].update(port=0), "server.port", "range"),
    ("port 65536", lambda d: d["server"].update(port=65536), "server.port", "range"),
    (
        "port below 1024",
        lambda d: d["server"].update(port=1023),
        "server.port",
        "range",
    ),
    *[
        (
            f"port {taken} belongs to the robot side",
            lambda d, taken=taken: d["server"].update(port=taken),
            "server.port",
            "range",
        )
        for taken in (5000, 5001, 5100, 7470)
    ],
    (
        "an integer too big for a float",
        lambda d: d["safety"].update(max_xy=10**400),
        "safety.max_xy",
        "range",
    ),
    (
        "an integer too big for a float in the home pose",
        lambda d: d["home"].update(pose=[10**400, 0, 0, 0, 0, 0]),
        "home.pose",
        "range",
    ),
    (
        "an integer too big for a float in a fraction",
        lambda d: d["gpu"].update(fraction_presets=[10**400]),
        "gpu.fraction_presets",
        "range",
    ),
    (
        "the arm lock is the runner's, not a site value",
        lambda d: d["paths"].update(arm_lock_file="/tmp/levi-aeri-fr3-arm.lck"),
        "paths.arm_lock_file",
        "unknown_key",
    ),
    (
        "a margin below the 1024 MiB the deploy keeps",
        lambda d: d["gpu"].update(margin_mib=0),
        "gpu.margin_mib",
        "range",
    ),
    (
        "a margin of 1023 MiB",
        lambda d: d["gpu"].update(margin_mib=1023),
        "gpu.margin_mib",
        "range",
    ),
    (
        "a fraction above the 0.45 the owner allows",
        lambda d: d["gpu"].update(fraction_presets=[0.22, 0.46]),
        "gpu.fraction_presets",
        "range",
    ),
    (
        "a home move under one second",
        lambda d: d["home"].update(duration_s=0.3),
        "home.duration_s",
        "range",
    ),
    (
        "a home move of a nanosecond",
        lambda d: d["home"].update(duration_s=1e-9),
        "home.duration_s",
        "range",
    ),
    (
        "five numbers for a pose",
        lambda d: d["home"].update(pose=[0.4, 0.0, 0.2, 3.1, 0.0]),
        "home.pose",
        "type",
    ),
    (
        "infinite pose number",
        lambda d: d["home"].update(pose=[0.4, 0.0, math.inf, 3.1, 0.0, 0.0]),
        "home.pose",
        "range",
    ),
    (
        "relative python",
        lambda d: d["paths"].update(python="bin/python"),
        "paths.python",
        "not_absolute",
    ),
    (
        "tilde path",
        lambda d: d["paths"].update(rollout_root="~/rollouts"),
        "paths.rollout_root",
        "not_absolute",
    ),
    (
        "dot-dot path",
        lambda d: d["paths"].update(rollout_root="/a/../b"),
        "paths.rollout_root",
        "not_normalized",
    ),
    (
        "double slash path",
        lambda d: d["paths"].update(rollout_root="/a//b"),
        "paths.rollout_root",
        "not_normalized",
    ),
    (
        "trailing slash path",
        lambda d: d["paths"].update(rollout_root="/a/b/"),
        "paths.rollout_root",
        "not_normalized",
    ),
    (
        "space in a path",
        lambda d: d["paths"].update(rollout_root="/tmp/a b"),
        "paths.rollout_root",
        "unsafe_chars",
    ),
    (
        "dollar in a path",
        lambda d: d["paths"].update(rollout_root="/tmp/a$b"),
        "paths.rollout_root",
        "unsafe_chars",
    ),
    (
        "percent in a path",
        lambda d: d["paths"].update(checkpoints_root="/tmp/a%b"),
        "paths.checkpoints_root",
        "unsafe_chars",
    ),
    (
        "newline in a path",
        lambda d: d["paths"].update(openpi_checkout="/tmp/a\nb"),
        "paths.openpi_checkout",
        "unsafe_chars",
    ),
    (
        "ros prefix with a space",
        lambda d: d["ros"].update(path_prefixes=["/opt/ros x"]),
        "ros.path_prefixes",
        "unsafe_chars",
    ),
    (
        "camera serial with a letter",
        lambda d: d["cameras"].update(view="26262227425x"),
        "cameras.view",
        "range",
    ),
    (
        "camera serial too short",
        lambda d: d["cameras"].update(wrist="12345"),
        "cameras.wrist",
        "range",
    ),
    (
        "the same serial twice",
        lambda d: d["cameras"].update(wrist=d["cameras"]["view"]),
        "cameras.wrist",
        "inconsistent",
    ),
    (
        "floor not below the home height",
        lambda d: d["safety"].update(min_z_floor=0.30, min_z_default=0.30),
        "safety.min_z_floor",
        "inconsistent",
    ),
    (
        "floor of zero",
        lambda d: d["safety"].update(min_z_floor=0.0, min_z_default=0.025),
        "safety.min_z_floor",
        "range",
    ),
    (
        "default below the floor",
        lambda d: d["safety"].update(min_z_default=0.01),
        "safety.min_z_default",
        "inconsistent",
    ),
    (
        "default at the home height",
        lambda d: d["safety"].update(min_z_default=0.235951),
        "safety.min_z_default",
        "inconsistent",
    ),
    (
        "range with lo above hi",
        lambda d: d["safety"].update(rx_range=[4.03, 1.5]),
        "safety.rx_range",
        "range",
    ),
    (
        "range with lo equal hi",
        lambda d: d["safety"].update(ry_range=[0.5, 0.5]),
        "safety.ry_range",
        "range",
    ),
    (
        "string for a bool",
        lambda d: d["safety"].update(binarize_gripper="yes"),
        "safety.binarize_gripper",
        "type",
    ),
    (
        "fraction above 0.5",
        lambda d: d["gpu"].update(fraction_presets=[0.22, 0.6]),
        "gpu.fraction_presets",
        "range",
    ),
    (
        "fractions not ascending",
        lambda d: d["gpu"].update(fraction_presets=[0.45, 0.22]),
        "gpu.fraction_presets",
        "range",
    ),
    (
        "the same fraction twice",
        lambda d: d["gpu"].update(fraction_presets=[0.22, 0.22]),
        "gpu.fraction_presets",
        "range",
    ),
    (
        "no fraction",
        lambda d: d["gpu"].update(fraction_presets=[]),
        "gpu.fraction_presets",
        "range",
    ),
    (
        "five fractions",
        lambda d: d["gpu"].update(fraction_presets=[0.1, 0.2, 0.3, 0.4, 0.5]),
        "gpu.fraction_presets",
        "range",
    ),
    (
        "rtc horizon cap above 10",
        lambda d: d["caps"].update(rtc_horizon_max=11),
        "caps.rtc_horizon_max",
        "range",
    ),
    (
        "rtc default above its cap",
        lambda d: d["caps"].update(rtc_horizon_default=9),
        "caps.rtc_horizon_default",
        "inconsistent",
    ),
    (
        "steps default above the cap",
        lambda d: d["caps"].update(max_steps_default=4000),
        "caps.max_steps_default",
        "inconsistent",
    ),
    (
        "episode count default above the cap",
        lambda d: d["caps"].update(eval_num_default=60),
        "caps.eval_num_default",
        "inconsistent",
    ),
    (
        "open loop default above its cap",
        lambda d: d["caps"].update(open_loop_default=6, open_loop_max=5),
        "caps.open_loop_default",
        "inconsistent",
    ),
    (
        "task instruction cap above 300",
        lambda d: d["caps"].update(prompt_max_chars=301),
        "caps.prompt_max_chars",
        "range",
    ),
    (
        "cfg default above its cap",
        lambda d: d["caps"].update(cfg_scale_default=2.0),
        "caps.cfg_scale_default",
        "inconsistent",
    ),
    ("nice 20", lambda d: d["unit"].update(nice=20), "unit.nice", "range"),
    ("bool nice", lambda d: d["unit"].update(nice=True), "unit.nice", "type"),
    (
        "bad cpu list",
        lambda d: d["unit"].update(cpu_affinity="8-"),
        "unit.cpu_affinity",
        "range",
    ),
    (
        "short stop timeout",
        lambda d: d["unit"].update(stop_timeout_s=5),
        "unit.stop_timeout_s",
        "range",
    ),
    (
        "cfg config that is not known",
        lambda d: d["catalog"].update(cfg_configs=["other"]),
        "catalog.cfg_configs",
        "inconsistent",
    ),
    (
        "no known config",
        lambda d: d["catalog"].update(known_configs=[], cfg_configs=[]),
        "catalog.known_configs",
        "range",
    ),
    (
        "config name with a space",
        lambda d: d["catalog"].update(known_configs=["a b"], cfg_configs=[]),
        "catalog.known_configs",
        "range",
    ),
    (
        "live unit with a space",
        lambda d: d["live"].update(unit="levi live.service"),
        "live.unit",
        "range",
    ),
    (
        "host with a space",
        lambda d: d["server"].update(remote_host="a b"),
        "server.remote_host",
        "range",
    ),
    (
        "robot url that is not http",
        lambda d: d["server"].update(robot_server_url="ftp://127.0.0.1/"),
        "server.robot_server_url",
        "range",
    ),
    (
        "robot url with a space",
        lambda d: d["server"].update(robot_server_url="http://127.0.0.1:5000/ x"),
        "server.robot_server_url",
        "range",
    ),
    (
        "default task instruction with a quote",
        lambda d: d["prompts"].update(default="pick the 'eggplant'"),
        "prompts.default",
        "unsafe_chars",
    ),
    (
        "known task instruction with a leading dash",
        lambda d: d["prompts"].update(known=["-x", "stack plates"]),
        "prompts.known",
        "unsafe_chars",
    ),
    (
        "hash file that is absolute",
        lambda d: d["source"].update(hash_files=["/etc/passwd"]),
        "source.hash_files",
        "unsafe_chars",
    ),
    (
        "hash file with dot-dot",
        lambda d: d["source"].update(hash_files=["../x"]),
        "source.hash_files",
        "unsafe_chars",
    ),
    (
        "manifest that is absolute",
        lambda d: d["source"].update(manifest="/tmp/m.json"),
        "source.manifest",
        "unsafe_chars",
    ),
]


@pytest.mark.parametrize(
    "mutate,field,code", [b[1:] for b in BAD], ids=[b[0] for b in BAD]
)
def test_a_bad_site_is_refused_with_the_field_and_a_code(tmp_path, mutate, field, code):
    assert {"field": field, "code": code} in invalid(tmp_path, mutate)


# The fixed motion limits sit only a little above the client's defaults
# (0.85, 0.10, 0.35): a typo such as 8.5 must not pass as a site value.
SAFETY_BOUNDS = [("max_xy", 1.0), ("max_step_m", 0.2), ("max_step_rad", 0.5)]


@pytest.mark.parametrize("key,bound", SAFETY_BOUNDS)
def test_a_motion_limit_just_above_its_bound_is_refused(tmp_path, key, bound):
    above = math.nextafter(bound, math.inf)
    found = invalid(tmp_path, lambda d: d["safety"].update({key: above}))
    assert {"field": f"safety.{key}", "code": "range"} in found


@pytest.mark.parametrize("key,bound", SAFETY_BOUNDS)
def test_a_motion_limit_at_its_bound_is_accepted(tmp_path, key, bound):
    site = make_site(tmp_path, safety={key: bound})
    assert getattr(site.safety, key) == bound


@pytest.mark.parametrize("key,bound", SAFETY_BOUNDS)
def test_a_motion_limit_of_zero_is_refused(tmp_path, key, bound):
    found = invalid(tmp_path, lambda d: d["safety"].update({key: 0.0}))
    assert {"field": f"safety.{key}", "code": "range"} in found


def test_every_problem_is_listed_not_just_the_first(tmp_path):
    def mutate(data):
        data["server"]["port"] = 0
        data["paths"]["python"] = "relative"
        data["extra"] = 1

    found = invalid(tmp_path, mutate)
    assert {"field": "server.port", "code": "range"} in found
    assert {"field": "paths.python", "code": "not_absolute"} in found
    assert {"field": "extra", "code": "unknown_key"} in found


def test_the_errors_hold_neither_values_nor_paths(tmp_path):
    found = invalid(tmp_path, lambda d: d["paths"].update(python="rel/path"))
    assert "rel/path" not in json.dumps(found)
    for item in found:
        assert set(item) == {"field", "code"}


def test_a_site_that_is_not_a_table_is_refused():
    with pytest.raises(S.SiteInvalid):
        S.parse([], "env")


def test_unsafe_text_is_the_class_systemd_and_the_shell_would_change():
    for text in ("a b", "a$b", "a%b", "a\tb", "a\nb", "a\x00b", "a\x7fb", "a\x1fb"):
        assert S.unsafe_text(text), repr(text)
    for text in ("/opt/ros/jazzy", "a-b_c.d:e", "x=1,y"):
        assert not S.unsafe_text(text), repr(text)


# --- where the file is ----------------------------------------------------------


def test_the_environment_names_the_file(tmp_path):
    path, source = S.find_path({ENV: str(tmp_path / "x.toml")}, checkouts=list)
    assert (path, source) == (tmp_path / "x.toml", "env")


def test_a_home_relative_name_is_this_users_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path, source = S.find_path({ENV: "~/marvel0/site.toml"}, checkouts=list)
    assert (path, source) == (tmp_path / "marvel0" / "site.toml", "env")


@pytest.mark.parametrize("named", ["~zzz_nouser/site.toml", "~root/site.toml"])
def test_another_users_home_is_never_expanded(named):
    # "~name" would resolve to somebody else's folder (or raise): it stays a
    # relative name, which the loader reports as not absolute.
    path, source = S.find_path({ENV: named}, checkouts=list)
    assert source == "env" and not path.is_absolute()
    loaded = S.load({ENV: named}, checkouts=list)
    assert loaded.state == "invalid"
    assert loaded.errors == [{"field": ENV, "code": "not_absolute"}]


def test_without_the_variable_it_is_in_the_main_checkout(tmp_path):
    main = tmp_path / "main"
    (main / ".git").mkdir(parents=True)
    worktree = tmp_path / "wt"
    (worktree).mkdir()
    (worktree / ".git").write_text("gitdir: elsewhere")
    path, source = S.find_path({}, checkouts=lambda: [worktree, main])
    assert (path, source) == (main / "lab" / "marvel0" / "site.toml", "main_checkout")


def test_a_blank_variable_counts_as_unset(tmp_path):
    main = tmp_path / "main"
    (main / ".git").mkdir(parents=True)
    path, source = S.find_path({ENV: "  "}, checkouts=lambda: [main])
    assert source == "main_checkout" and path.parent == main / "lab" / "marvel0"


def test_without_a_git_directory_anywhere_the_first_checkout_is_used(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    assert S.main_checkout(lambda: [first, second]) == first


def test_the_real_lookup_finds_a_checkout(tmp_path):
    assert S.main_checkout().is_absolute()


# --- reading it -----------------------------------------------------------------


def load_at(path, **extra):
    return S.load({ENV: str(path)}, checkouts=list, **extra)


def test_a_missing_file_says_where_it_looked(tmp_path):
    loaded = load_at(tmp_path / "none.toml")
    assert loaded.state == "missing"
    assert loaded.site is None and loaded.errors == []
    assert loaded.looked_at == tmp_path / "none.toml" and loaded.origin == "env"


def test_a_good_file_loads(tmp_path):
    loaded = load_at(write_site(tmp_path))
    assert loaded.state == "ok" and loaded.errors == []
    assert loaded.site.origin == "env" and loaded.site.port == 8000


def test_a_file_with_a_bad_value_is_invalid_with_the_errors(tmp_path):
    loaded = load_at(write_site(tmp_path, server={"port": 0}))
    assert loaded.state == "invalid" and loaded.site is None
    assert loaded.errors == [{"field": "server.port", "code": "range"}]


def test_a_relative_variable_is_invalid(tmp_path):
    loaded = S.load({ENV: "site.toml"}, checkouts=list)
    assert loaded.state == "invalid"
    assert loaded.errors == [{"field": ENV, "code": "not_absolute"}]


def test_text_that_is_not_toml_is_invalid(tmp_path):
    path = tmp_path / "s.toml"
    path.write_text("this is = = not toml")
    os.chmod(path, 0o644)
    assert load_at(path).errors == [{"field": "file", "code": "not_toml"}]
    path.write_bytes(b"\xff\xfe\x00")
    assert load_at(path).errors == [{"field": "file", "code": "not_toml"}]


@pytest.mark.parametrize(
    "text",
    [
        "x = " + "[" * 20000,  # nesting deeper than the parser's stack
        "x = {a = " * 3000,
        "x = 1" + "0" * 5000,  # an integer longer than Python converts
    ],
    ids=["nested arrays", "nested tables", "long integer"],
)
def test_text_the_parser_cannot_take_is_invalid_not_a_crash(tmp_path, text):
    path = tmp_path / "s.toml"
    path.write_text(text + "\n")
    os.chmod(path, 0o644)
    assert load_at(path).errors == [{"field": "file", "code": "not_toml"}]


@pytest.mark.parametrize(
    "named",
    [
        "",
        "   ",
        "\x00",
        "a\x00b",
        "/",
        "//",
        "~",
        "~/",
        "~x/y",
        "x" * 5000,
        "/" + "x/" * 3000,
    ],
)
def test_loading_never_raises(tmp_path, monkeypatch, named):
    monkeypatch.setenv("HOME", str(tmp_path))
    loaded = S.load({ENV: named}, checkouts=lambda: [Path("/nonexistent-main")])
    assert loaded.state in ("ok", "missing", "invalid")
    assert loaded.looked_at is not None


def test_a_symbolic_link_is_not_followed(tmp_path):
    real = write_site(tmp_path, name="real.toml")
    link = tmp_path / "link.toml"
    link.symlink_to(real)
    loaded = load_at(link)
    assert loaded.state == "invalid"
    assert loaded.errors == [{"field": "file", "code": "not_regular"}]


def test_a_directory_is_not_a_site_file(tmp_path):
    (tmp_path / "d.toml").mkdir()
    assert load_at(tmp_path / "d.toml").errors == [
        {"field": "file", "code": "not_regular"}
    ]


def test_a_fifo_is_refused_without_blocking(tmp_path):
    fifo = tmp_path / "f.toml"
    os.mkfifo(fifo)
    assert load_at(fifo).errors == [{"field": "file", "code": "not_regular"}]


def test_a_file_of_another_user_is_refused(tmp_path, monkeypatch):
    path = write_site(tmp_path)
    monkeypatch.setattr(S.os, "geteuid", lambda: os.stat(path).st_uid + 1)
    assert load_at(path).errors == [{"field": "file", "code": "not_owner"}]


@pytest.mark.parametrize("mode", [0o664, 0o646, 0o666, 0o620])
def test_a_file_others_can_write_is_refused(tmp_path, mode):
    path = write_site(tmp_path)
    os.chmod(path, mode)
    assert load_at(path).errors == [{"field": "file", "code": "writable"}]


def test_a_file_read_only_for_everyone_is_fine(tmp_path):
    path = write_site(tmp_path)
    os.chmod(path, 0o444)
    assert load_at(path).state == "ok"


def test_a_big_file_is_refused(tmp_path):
    path = write_site(tmp_path)
    with open(path, "a") as out:
        out.write("# " + "x" * S.MAX_BYTES + "\n")
    assert load_at(path).errors == [{"field": "file", "code": "too_large"}]


def test_a_file_that_cannot_be_read_is_invalid(tmp_path):
    path = write_site(tmp_path)
    os.chmod(path, 0o000)
    try:
        if os.access(path, os.R_OK):  # running as root
            pytest.skip("permissions do not apply")
        assert load_at(path).errors == [{"field": "file", "code": "unreadable"}]
    finally:
        os.chmod(path, 0o644)


def test_a_file_in_a_missing_folder_is_missing(tmp_path):
    assert load_at(tmp_path / "no" / "such" / "site.toml").state == "missing"


def test_a_name_with_a_nul_byte_is_unreadable_not_a_crash(tmp_path):
    loaded = S.load({ENV: str(tmp_path) + "/a\x00b/site.toml"}, checkouts=list)
    assert loaded.state == "invalid"
    assert loaded.errors == [{"field": "file", "code": "unreadable"}]


def test_the_result_is_reused_until_the_file_changes(tmp_path):
    path = write_site(tmp_path)
    first = load_at(path)
    assert load_at(path) is first
    path.write_text(dump_toml(merged(tmp_path, safety={"max_xy": 0.5})))
    os.utime(path, ns=(1, 2_000_000_000))
    second = load_at(path)
    assert second is not first and second.site.safety.max_xy == 0.5


def test_a_changed_size_alone_is_noticed(tmp_path):
    path = write_site(tmp_path)
    os.utime(path, ns=(1, 1_000_000_000))
    first = load_at(path)
    path.write_text(dump_toml(merged(tmp_path, gpu={"margin_mib": 2048})) + "# more\n")
    os.utime(path, ns=(1, 1_000_000_000))
    assert (
        load_at(path).site.gpu.margin_mib == 2048 and first.site.gpu.margin_mib == 1024
    )


def test_a_file_that_goes_away_is_missing_again(tmp_path):
    path = write_site(tmp_path)
    assert load_at(path).state == "ok"
    path.unlink()
    assert load_at(path).state == "missing"


def test_the_cache_does_not_mix_two_files(tmp_path):
    a = write_site(tmp_path, name="a.toml", safety={"max_xy": 0.5})
    b = write_site(tmp_path, name="b.toml", safety={"max_xy": 0.6})
    assert load_at(a).site.safety.max_xy == 0.5
    assert load_at(b).site.safety.max_xy == 0.6


# --- the refusals every route gives ---------------------------------------------


def test_require_returns_the_site(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, str(write_site(tmp_path)))
    assert S.require().port == 8000


def test_a_missing_file_is_a_412_with_the_path_and_a_template(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, str(tmp_path / "none.toml"))
    with pytest.raises(HTTPException) as caught:
        S.require()
    assert caught.value.status_code == 412
    assert caught.value.detail == {
        "code": "site_missing",
        "message": "marvel0 site file missing",
        "looked_at": str(tmp_path / "none.toml"),
        "template": S.TEMPLATE,
    }


def test_an_invalid_file_is_a_412_with_the_errors_and_no_path(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, str(write_site(tmp_path, server={"port": 0})))
    with pytest.raises(HTTPException) as caught:
        S.require()
    assert caught.value.status_code == 412
    assert caught.value.detail == {
        "code": "site_invalid",
        "message": "marvel0 site file is invalid",
        "errors": [{"field": "server.port", "code": "range"}],
    }
    assert str(tmp_path) not in json.dumps(caught.value.detail)


def test_the_codes_are_the_error_modules(tmp_path):
    assert (errors.SITE_MISSING, errors.SITE_INVALID) == (
        "site_missing",
        "site_invalid",
    )
    exc = errors.fail(409, "busy", "text", operation={"a": 1})
    assert exc.status_code == 409
    assert exc.detail == {"code": "busy", "message": "text", "operation": {"a": 1}}


# --- the template ----------------------------------------------------------------


def test_the_template_is_toml_without_a_machine_path():
    data = tomllib.loads(S.TEMPLATE)
    assert data["schema"] == S.SCHEMA
    assert "/home/" not in S.TEMPLATE and "/mnt/" not in S.TEMPLATE
    assert data["paths"]["openpi_checkout"].startswith("/path/to/")


def test_the_template_parses_as_it_is_and_with_real_paths(tmp_path):
    assert S.parse(tomllib.loads(S.TEMPLATE), "env").port == 8000
    text = S.TEMPLATE.replace("/path/to", str(tmp_path))
    site = S.parse(tomllib.loads(text), "env")
    assert site.openpi_checkout == str(tmp_path / "openpi")


def test_the_template_and_the_sample_agree_on_every_number(tmp_path):
    template = tomllib.loads(S.TEMPLATE)
    for name in ("paths", "ros"):
        template.pop(name)
    expected = sample(tmp_path)
    for name in ("paths", "ros"):
        expected.pop(name)
    assert template == expected


def test_the_arm_lock_is_the_one_the_aeri_runner_takes():
    text = (ROOT / "levi/automatic/fr3_run.py").read_text()
    literal = re.search(r'^ARM_LOCK_FILE = "([^"]+)"', text, re.MULTILINE).group(1)
    assert S.ARM_LOCK_FILE == literal
    assert "arm_lock_file" not in tomllib.loads(S.TEMPLATE)["paths"], "not a site value"


def test_the_site_reports_the_runners_arm_lock(tmp_path, monkeypatch):
    assert make_site(tmp_path).arm_lock_file == S.ARM_LOCK_FILE
    monkeypatch.setattr(S, "ARM_LOCK_FILE", str(tmp_path / "arm.lock"))
    assert make_site(tmp_path).arm_lock_file == str(tmp_path / "arm.lock")


def test_no_marvel0_source_file_holds_a_home_path():
    for source in sorted((ROOT / "levi/marvel0").glob("*.py")):
        assert "/home/" not in source.read_text(), source.name


# --- what the page may see ------------------------------------------------------


def test_the_public_view_is_the_contract(tmp_path):
    view = S.public_view(make_site(tmp_path))
    assert view["schema"] == "levi.marvel0.site_view.v1" and view["source"] == "env"
    assert view["server"] == {
        "port": 8000,
        "remote_host": "127.0.0.1",
        "robot_server_url": "http://127.0.0.1:5000/",
    }
    assert view["cameras"] == {"view": "262622274256", "wrist": "262622275490"}
    assert view["home"]["pose"][:3] == [0.481902, -0.011145, 0.235951]
    assert view["min_z"] == {"floor": 0.025, "default": 0.025}
    assert view["caps"]["max_steps"] == {"default": 500, "cap": 3000}
    assert view["caps"]["rtc_horizon"] == {"default": 6, "max": 8}
    assert view["caps"]["cfg_scale"] == {"default": 1.0, "max": 1.5}
    assert view["fixed_flags"] == FLAGS_OF_THE_CONTRACT
    assert view["gpu"] == {"fraction_presets": [0.22, 0.3, 0.45], "margin_mib": 1024}
    assert view["unit"] == {
        "nice": 10,
        "cpu_affinity": "8-15",
        "stop_timeout_s": 90,
        "start_timeout_s": 600,
    }
    assert view["names"] == {"openpi_checkout": "openpi", "rollout_root": "rollouts"}
    assert view["prompts"]["restricted_prefixes"] == ["pi05_fr3_sp"]
    assert len(view["prompts"]["known"]) == 5
    # What a restricted model is offered: the client's two, not the site's list.
    assert view["prompts"]["restricted"] == [
        "pick screws of same size into the empty slot of screw box",
        "stack the plates of same color together",
    ]
    assert view["source_hash"] == {"manifest": False, "files": 5}


def test_the_public_view_holds_no_path_and_no_machine_name(tmp_path):
    view = S.public_view(make_site(tmp_path))
    text = json.dumps(view)
    assert str(tmp_path) not in text
    assert not re.search(r"(?<![\w.:-])/(?:[A-Za-z0-9_.~+@%=,-]+/)+", text)

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                assert "path" not in key, key
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            assert not value.startswith("/"), value

    walk(view)


def test_the_fixed_flags_follow_the_site_alone(tmp_path):
    site = make_site(
        tmp_path,
        safety={"max_xy": 0.5, "binarize_gripper": False, "allow_unknown_prompt": True},
        home={"duration_s": 4.5},
    )
    flags = S.fixed_flags(site)
    assert flags[0] == "--max-xy=0.5"
    assert "--no-binarize-gripper" in flags and "--allow-unknown-prompt" in flags
    assert flags[-1] == "--home-duration-s=4.5"


def test_numbers_print_like_the_documented_commands():
    assert S.number(3.0) == "3.0" and S.number(0.1) == "0.1" and S.number(1.5) == "1.5"
    assert S.number(-0.011145) == "-0.011145" and S.number(0.481902) == "0.481902"
    assert S.number(1) == "1.0" and S.number(0.025) == "0.025"
    assert S.number(1e-7) == "1e-07"


# --- task instructions ----------------------------------------------------------


@pytest.mark.parametrize(
    "text,reason",
    [
        ("", "empty"),
        ("   ", "empty"),
        ("x" * 301, "too_long"),
        ("-pick", "leading_dash"),
        ("pick 'it'", "forbidden_char"),
        ('pick "it"', "forbidden_char"),
        ("pick\\it", "forbidden_char"),
        ("pick $it", "forbidden_char"),
        ("pick `it`", "forbidden_char"),
        ("pick it!", "forbidden_char"),
        ("pick\nit", "control_char"),
        ("pick\tit", "control_char"),
        ("pick\x00it", "control_char"),
        ("pick\x7fit", "control_char"),
        ("pick\x85it", "control_char"),
        ("pick\u2028it", "control_char"),
        ("pick\u200bit", "control_char"),
    ],
)
def test_a_task_instruction_with_a_forbidden_character_is_named(text, reason):
    assert S.prompt_reason(text, 300) == reason


def test_a_plain_task_instruction_passes():
    assert S.prompt_reason("pick the eggplant in the blue plate", 300) is None
    assert S.prompt_reason("x" * 300, 300) is None
    assert S.prompt_reason("put 3 screws, then stop; (ok) #1 & more", 300) is None
    assert S.prompt_reason("a" * 11, 10) == "too_long"


def test_the_deep_copy_of_a_sample_is_independent(tmp_path):
    first = sample(tmp_path)
    second = copy.deepcopy(first)
    second["safety"]["max_xy"] = 0.1
    assert sample(tmp_path)["safety"]["max_xy"] == 0.85
