"""The optional ``[aeri]`` section of the site file and of the control file
(``site.py``, ``control_site.py``): a missing section is a normal state, a
malformed one disables AERI with the errors listed and never invalidates the
rest of the file, and nothing can switch it on by accident."""

import copy
import dataclasses
import os

import pytest
from site_sample import dump_toml, make_site, merged, write_site

from levi.marvel0 import control_site as cs
from levi.marvel0 import site as marvel0_site

PATHS = {
    "worktree": "/p/worktrees/harvest",
    "runs_root": "/p/aeri-external/runs",
    "templates_root": "/p/aeri-external/templates",
    "rollout_root": "/p/rollouts",
    "openpi_root": "/p/openpi",
    "levi_python": "/p/LEVI/.venv/bin/python",
    "levi_bin": "/p/LEVI/.venv/bin/levi",
}
GOOD = {"pin_sha": "5bc750ca", **PATHS}


def parse(tmp_path, aeri):
    return marvel0_site.parse(merged(tmp_path, aeri=aeri), "env")


# --- the site file ---------------------------------------------------------------------


def test_a_site_without_the_section_is_valid_and_has_no_aeri(tmp_path):
    site = make_site(tmp_path)
    assert site.aeri is None
    assert site.aeri_errors == ()


def test_a_good_section_gives_the_values_and_the_defaults(tmp_path):
    aeri = parse(tmp_path, GOOD).aeri
    assert aeri.profile == "harvest-5bc750ca", "the version validated on the robot"
    assert aeri.console_lang == "zh"
    assert aeri.uv_offline is True
    assert aeri.local_model is None
    assert aeri.tools_root == "/p/aeri-external/tools", "next to the runs folder"
    assert aeri.pin_sha == "5bc750ca"
    assert aeri.worktree == "/p/worktrees/harvest"


def test_the_values_the_file_gives_replace_the_defaults(tmp_path):
    aeri = parse(
        tmp_path,
        {
            **GOOD,
            "profile": "bfdee2cf",
            "console_lang": "en",
            "uv_offline": False,
            "tools_root": "/p/tools2",
            "local_model": "/p/models/qwen",
        },
    ).aeri
    assert (aeri.profile, aeri.console_lang, aeri.uv_offline) == (
        "bfdee2cf",
        "en",
        False,
    )
    assert aeri.tools_root == "/p/tools2"
    assert aeri.local_model == "/p/models/qwen"


def test_the_site_stays_immutable_and_hashable_with_the_section(tmp_path):
    site = parse(tmp_path, GOOD)
    assert hash(site) == hash(parse(tmp_path, GOOD))
    with pytest.raises(dataclasses.FrozenInstanceError):
        site.aeri = None


@pytest.mark.parametrize(
    "change, error",
    [
        ({"pin_sha": None}, ("aeri.pin_sha", "missing")),
        ({"worktree": None}, ("aeri.worktree", "missing")),
        ({"runs_root": None}, ("aeri.runs_root", "missing")),
        ({"pin_sha": "5BC750CA"}, ("aeri.pin_sha", "range")),
        ({"pin_sha": "5bc750c"}, ("aeri.pin_sha", "range")),
        ({"pin_sha": "5bc750ca" * 6}, ("aeri.pin_sha", "range")),
        ({"pin_sha": 5750}, ("aeri.pin_sha", "type")),
        ({"profile": "Harvest"}, ("aeri.profile", "range")),
        ({"profile": ""}, ("aeri.profile", "range")),
        ({"console_lang": "fr"}, ("aeri.console_lang", "range")),
        ({"uv_offline": "yes"}, ("aeri.uv_offline", "type")),
        ({"worktree": "relative/path"}, ("aeri.worktree", "not_absolute")),
        ({"worktree": "/p/work tree"}, ("aeri.worktree", "unsafe_chars")),
        ({"worktree": "/p/$HOME"}, ("aeri.worktree", "unsafe_chars")),
        ({"worktree": "/p/a;b"}, ("aeri.worktree", "unsafe_chars")),
        ({"levi_bin": "/p/a'b"}, ("aeri.levi_bin", "unsafe_chars")),
        ({"runs_root": "/p/../q"}, ("aeri.runs_root", "not_normalized")),
        ({"surprise": 1}, ("aeri.surprise", "unknown_key")),
    ],
)
def test_a_bad_section_is_listed_and_disables_aeri_only(tmp_path, change, error):
    values = copy.deepcopy(GOOD)
    for key, value in change.items():
        if value is None:
            values.pop(key)
        else:
            values[key] = value
    site = parse(tmp_path, values)  # does not raise: the rest of the file is fine
    assert site.aeri is None
    assert error in site.aeri_errors
    assert site.live.unit == "levi-live.service", "the other sections are intact"


def test_every_problem_is_listed(tmp_path):
    site = parse(tmp_path, {**GOOD, "pin_sha": "zz", "levi_bin": "x", "extra": 1})
    fields = {field for field, _ in site.aeri_errors}
    assert fields == {"aeri.pin_sha", "aeri.levi_bin", "aeri.extra"}


def test_a_section_that_is_not_a_table_is_a_type_error(tmp_path):
    site = parse(tmp_path, "yes")
    assert site.aeri is None
    assert site.aeri_errors == (("aeri", "type"),)


def test_the_errors_hold_neither_values_nor_paths(tmp_path):
    site = parse(tmp_path, {**GOOD, "worktree": "/p/with space"})
    text = repr(site.aeri_errors)
    assert "/p/" not in text and "space" not in text


def test_the_section_goes_through_the_file_reader(tmp_path):
    path = write_site(tmp_path, aeri=GOOD)
    os.chmod(path, 0o644)
    loaded = marvel0_site.load({marvel0_site.ENV: str(path)})
    assert loaded.state == "ok"
    assert loaded.site.aeri.pin_sha == "5bc750ca"
    path.write_text(dump_toml(merged(tmp_path, aeri={**GOOD, "pin_sha": "no"})))
    marvel0_site.clear_cache()
    again = marvel0_site.load({marvel0_site.ENV: str(path)})
    assert again.state == "ok", "a bad [aeri] never makes the whole file invalid"
    assert again.site.aeri is None


def test_an_unknown_top_level_key_is_still_an_error(tmp_path):
    with pytest.raises(marvel0_site.SiteInvalid) as caught:
        marvel0_site.parse(merged(tmp_path, aerx=GOOD), "env")
    assert {"field": "aerx", "code": "unknown_key"} in caught.value.errors


def test_the_public_site_view_does_not_change(tmp_path):
    base = marvel0_site.public_view(make_site(tmp_path))
    with_aeri = marvel0_site.public_view(parse(tmp_path, GOOD))
    assert base == with_aeri, "the AERI section is not part of GET /site"


def test_the_template_is_toml_with_placeholders_and_parses_as_a_section(tmp_path):
    import tomllib

    table = tomllib.loads(marvel0_site.AERI_TEMPLATE)
    site = parse(tmp_path, table["aeri"])
    assert site.aeri is not None, site.aeri_errors
    assert site.aeri.profile == marvel0_site.AERI_DEFAULT_PROFILE
    assert "/home/" not in marvel0_site.AERI_TEMPLATE


def test_the_default_profile_is_the_one_validated_on_the_robot():
    assert marvel0_site.AERI_DEFAULT_PROFILE == "harvest-5bc750ca"


def test_the_paths_are_only_the_characters_shlex_leaves_alone(tmp_path):
    import shlex

    site = parse(tmp_path, {**GOOD, "worktree": "/p/a+b=c@d:e,f.g_h-i"})
    assert site.aeri is not None
    for value in dataclasses.astuple(site.aeri):
        if isinstance(value, str) and value.startswith("/"):
            assert shlex.quote(value) == value


# --- the control file ------------------------------------------------------------------

CONTROL_HEAD = 'schema = "levi.marvel0.control.v1"\n'


def control(tmp_path, text):
    path = tmp_path / "control.toml"
    path.write_text(CONTROL_HEAD + text)
    os.chmod(path, 0o644)
    cs.clear_cache()
    return cs.read({cs.ENV: str(path)})


def test_a_control_file_without_the_section_has_aeri_off(tmp_path):
    loaded = control(tmp_path, "[client]\nenabled = true\n")
    assert loaded.state == "ok"
    assert loaded.site.aeri.enabled is False
    view = cs.aeri_view(loaded)
    assert view == {
        "state": "ok",
        "code": "aeri.disabled",
        "enabled": False,
        "errors": [],
    }


def test_no_file_means_aeri_off_and_missing(tmp_path):
    cs.clear_cache()
    loaded = cs.read({cs.ENV: str(tmp_path / "nope.toml")})
    assert loaded.state == "missing"
    assert cs.aeri_view(loaded) == {
        "state": "missing",
        "code": "aeri.missing",
        "enabled": False,
        "errors": [],
    }


def test_the_section_turns_aeri_on_only_when_it_says_true(tmp_path):
    on = control(tmp_path, "[aeri]\nenabled = true\n")
    assert cs.aeri_view(on) == {
        "state": "ok",
        "code": "aeri.enabled",
        "enabled": True,
        "errors": [],
    }
    off = control(tmp_path, "[aeri]\nenabled = false\n")
    assert cs.aeri_view(off)["enabled"] is False


@pytest.mark.parametrize(
    "text, errors",
    [
        ('[aeri]\nenabled = "true"\n', [("aeri.enabled", "type")]),
        ("[aeri]\nenabled = 1\n", [("aeri.enabled", "type")]),
        ("[aeri]\nenabled = true\nsurprise = 1\n", [("aeri.surprise", "unknown_key")]),
        ("aeri = true\n", [("aeri", "type")]),
        ("aeri = [1]\n", [("aeri", "type")]),
    ],
)
def test_a_malformed_section_is_off_with_the_errors_never_on(tmp_path, text, errors):
    loaded = control(tmp_path, text)
    assert loaded.state == "ok", "the stack and the client keep their switches"
    assert loaded.site.aeri.enabled is False
    assert loaded.site.aeri_errors == tuple(errors)
    view = cs.aeri_view(loaded)
    assert view["enabled"] is False
    assert view["state"] == "invalid" and view["code"] == "aeri.invalid"
    assert view["errors"] == [{"field": f, "code": c} for f, c in errors]


def test_a_bad_section_does_not_switch_the_client_or_the_stack_off(tmp_path):
    loaded = control(tmp_path, "[client]\nenabled = true\n[aeri]\nenabled = 5\n")
    assert loaded.site.client.enabled is True
    assert cs.view(loaded)["client_enabled"] is True


def test_an_invalid_file_means_aeri_off(tmp_path):
    loaded = control(tmp_path, "[stack]\nenabled = 5\n")
    assert loaded.state == "invalid"
    view = cs.aeri_view(loaded)
    assert view["enabled"] is False and view["code"] == "aeri.invalid"


def test_an_unknown_top_level_key_of_the_control_file_is_still_an_error(tmp_path):
    loaded = control(tmp_path, "[aerx]\nenabled = true\n")
    assert loaded.state == "invalid"


def test_the_template_switches_aeri_off_and_parses(tmp_path):
    path = tmp_path / "t.toml"
    path.write_text(cs.TEMPLATE)
    os.chmod(path, 0o644)
    cs.clear_cache()
    loaded = cs.read({cs.ENV: str(path)})
    assert loaded.state == "ok", loaded.errors
    assert loaded.site.aeri.enabled is False
    assert cs.aeri_view(loaded)["code"] == "aeri.disabled"


def test_the_control_view_of_the_stack_and_client_is_unchanged(tmp_path):
    loaded = control(tmp_path, "[aeri]\nenabled = true\n")
    assert set(cs.view(loaded)) == {
        "state",
        "code",
        "stack_enabled",
        "client_enabled",
        "errors",
        "env",
    }
