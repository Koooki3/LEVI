"""``levi/marvel0/models.py``: the validated model catalog of the policy deploy
panel. The checkpoints folder is a temporary tree shaped like the maintainer's
(read-only listing, 2026-10-19); nothing of the real machine is looked at."""

import dataclasses
import json
import os
import re

import pytest
from site_sample import (
    REAL_TREE,
    make_checkpoint,
    make_checkpoints,
    make_site,
)

from levi.automatic import policies
from levi.marvel0 import models as M

FORWARD = [
    "pi05_fr3_all_step49999",
    "recap_cfg_r1_step10000_jax",
    "recap_cfg_r2_best_step14300_jax",
    "recap_cfg_r2_step15000_jax",
]


@pytest.fixture(autouse=True)
def fresh_cache():
    M.cache_clear()
    yield
    M.cache_clear()


def reasons(catalog):
    return {item["id"]: item["reason"] for item in catalog.skipped}


def real_catalog(tmp_path, **override):
    make_checkpoints(tmp_path)
    return M.build(make_site(tmp_path, **override))


def test_the_real_tree_lists_the_four_forward_checkpoints(tmp_path):
    catalog = real_catalog(tmp_path)
    assert [m.id for m in catalog.models] == FORWARD
    assert catalog.problems == ()


def test_everything_else_is_skipped_with_a_reason(tmp_path):
    catalog = real_catalog(tmp_path)
    assert reasons(catalog) == {
        "recap_cfg_r2_best_step_14300": "not_deployable",
        "recap_global_step_10000": "not_deployable",
        "reset_r1_step20000": "excluded_by_site",
    }
    assert [item["id"] for item in catalog.skipped] == sorted(reasons(catalog))


def test_the_reset_checkpoint_is_still_not_a_forward_one_without_the_exclusion(
    tmp_path,
):
    catalog = real_catalog(tmp_path, catalog={"excluded": []})
    assert reasons(catalog)["reset_r1_step20000"] == "role_not_forward"
    assert "reset_r1_step20000" not in [m.id for m in catalog.models]


def test_a_norm_stats_file_only_under_assets_is_not_enough(tmp_path):
    root = make_checkpoints(tmp_path, ["pi05_fr3_all_step49999"])
    make_checkpoint(
        root,
        "recap_cfg_r9_jax",
        config="pi05_fr3_all_state_cfg",
        top_norm=False,
        assets_norm=True,
    )
    catalog = M.build(make_site(tmp_path))
    assert reasons(catalog) == {"recap_cfg_r9_jax": "no_toplevel_norm_stats"}
    assert [m.id for m in catalog.models] == ["pi05_fr3_all_step49999"]


def test_a_norm_stats_link_at_the_top_is_not_a_file(tmp_path):
    root = make_checkpoints(tmp_path, [])
    folder = make_checkpoint(
        root,
        "linked_jax",
        config="pi05_fr3_all_state",
        top_norm=False,
        assets_norm=True,
    )
    os.symlink(folder / "assets" / "norm_stats.json", folder / "norm_stats.json")
    assert reasons(M.build(make_site(tmp_path))) == {
        "linked_jax": "no_toplevel_norm_stats"
    }


def test_a_config_the_site_does_not_know_is_skipped(tmp_path):
    root = make_checkpoints(tmp_path, [])
    make_checkpoint(root, "other_jax", config="pi05_other", role="forward")
    assert reasons(M.build(make_site(tmp_path))) == {"other_jax": "config_unknown"}


def test_a_checkpoint_without_a_config_is_skipped(tmp_path):
    root = make_checkpoints(tmp_path, [])
    make_checkpoint(root, "nameless_jax", role="forward")
    assert reasons(M.build(make_site(tmp_path))) == {"nameless_jax": "config_unknown"}


def test_a_name_with_unsafe_characters_is_skipped_and_printable(tmp_path):
    root = make_checkpoints(tmp_path, [])
    for name in ("has space", "has$dollar", "line\nbreak"):
        make_checkpoint(root, name, config="pi05_fr3_all_state")
    catalog = M.build(make_site(tmp_path))
    assert catalog.models == ()
    assert set(reasons(catalog).values()) == {"unsafe_name"}
    assert sorted(reasons(catalog)) == ["has?dollar", "has?space", "line?break"]


def test_a_name_with_dot_dot_is_skipped_as_unsafe(tmp_path):
    # "a..b" passes the name pattern but its default rollout group is refused
    # by the command renderer (no dot-dot in a group), so it is not offered.
    root = make_checkpoints(tmp_path, [])
    make_checkpoint(root, "a..b", config="pi05_fr3_all_state", role="forward")
    catalog = M.build(make_site(tmp_path))
    assert catalog.models == ()
    assert reasons(catalog) == {"a..b": "unsafe_name"}


def long_name(length):
    return "a" * (length - len("_step49999")) + "_step49999"


def test_a_folder_name_whose_default_group_is_over_80_is_skipped(tmp_path):
    root = make_checkpoints(tmp_path, [])
    name = long_name(75)  # offered by the old rules; "<name>_rtc_demo" is 84
    assert len(name) == 75
    make_checkpoint(root, name, config="pi05_fr3_all_state", role="forward")
    catalog = M.build(make_site(tmp_path))
    assert catalog.models == ()
    assert reasons(catalog) == {name: "name_too_long"}


def test_the_longest_name_whose_default_group_fits_is_offered(tmp_path):
    root = make_checkpoints(tmp_path, [])
    fits, over = long_name(71), long_name(72)  # 71 + 9 = 80, 72 + 9 = 81
    for name in (fits, over):
        make_checkpoint(root, name, config="pi05_fr3_all_state", role="forward")
    catalog = M.build(make_site(tmp_path))
    assert [m.id for m in catalog.models] == [fits]
    assert reasons(catalog) == {over: "name_too_long"}
    assert len(M.default_group(fits, True)) == 80


def test_a_name_that_is_both_unsafe_and_long_says_unsafe_first(tmp_path):
    root = make_checkpoints(tmp_path, [])
    make_checkpoint(root, "x " + "a" * 78, config="pi05_fr3_all_state")
    assert set(reasons(M.build(make_site(tmp_path))).values()) == {"unsafe_name"}


def test_a_link_in_the_folder_is_not_followed(tmp_path):
    root = make_checkpoints(tmp_path, ["pi05_fr3_all_step49999"])
    os.symlink(root / "pi05_fr3_all_step49999", root / "alias_jax")
    assert [m.id for m in M.build(make_site(tmp_path)).models] == [
        "pi05_fr3_all_step49999"
    ]


def test_a_checkpoint_that_is_not_jax_even_if_deployable_is_skipped(tmp_path):
    make_checkpoints(tmp_path, ["pi05_fr3_all_step49999"])
    site = make_site(tmp_path)

    def discover(root, **kwargs):
        report = policies.discover_report(root, **kwargs)
        entries = tuple(dataclasses.replace(e, kind="pytorch") for e in report.entries)
        return dataclasses.replace(report, entries=entries)

    catalog = M.build(site, discover=discover)
    assert catalog.models == ()
    assert reasons(catalog) == {"pi05_fr3_all_step49999": "not_jax"}


def test_only_the_cfg_config_is_cfg_capable(tmp_path):
    by_id = {m.id: m for m in real_catalog(tmp_path).models}
    assert not by_id["pi05_fr3_all_step49999"].cfg_capable
    assert by_id["recap_cfg_r2_best_step14300_jax"].cfg_capable
    assert by_id["recap_cfg_r1_step10000_jax"].config == "pi05_fr3_all_state_cfg"


def test_models_of_the_restricted_family_are_marked(tmp_path):
    root = make_checkpoints(tmp_path, [])
    make_checkpoint(root, "pi05_fr3_sp_v1", config="pi05_fr3_all_state")
    make_checkpoint(root, "screws_plates_jax", config="pi05_fr3_sp_state")
    make_checkpoint(root, "plain_jax", config="pi05_fr3_all_state")
    site = make_site(
        tmp_path,
        catalog={
            "known_configs": [
                "pi05_fr3_all_state",
                "pi05_fr3_all_state_cfg",
                "pi05_fr3_sp_state",
            ]
        },
    )
    by_id = {m.id: m for m in M.build(site).models}
    assert by_id["pi05_fr3_sp_v1"].restricted
    assert by_id["screws_plates_jax"].restricted
    assert not by_id["plain_jax"].restricted


def test_the_client_restricted_family_is_marked_whatever_the_site_lists(tmp_path):
    root = make_checkpoints(tmp_path, [])
    make_checkpoint(root, "pi05_fr3_sp_v1", config="pi05_fr3_all_state")
    site = make_site(tmp_path, catalog={"restricted_prefixes": []})
    (model,) = M.build(site).models
    assert model.restricted


def test_a_model_is_verified_only_when_the_site_says_so(tmp_path):
    by_id = {m.id: m for m in real_catalog(tmp_path).models}
    assert by_id["pi05_fr3_all_step49999"].verified
    assert by_id["pi05_fr3_all_step49999"].labels == ()
    for name in FORWARD[1:]:
        assert not by_id[name].verified
        assert by_id[name].labels == ("unverified",)


def test_the_facts_of_a_checkpoint_come_from_its_folder(tmp_path):
    by_id = {m.id: m for m in real_catalog(tmp_path).models}
    best = by_id["recap_cfg_r2_best_step14300_jax"]
    assert (best.step, best.variant) == (14300, "best")
    assert best.size_bytes > 0
    assert by_id["pi05_fr3_all_step49999"].config == "pi05_fr3_all_state"


def test_the_site_decides_which_configs_make_a_forward_policy(tmp_path):
    root = make_checkpoints(tmp_path, [])
    make_checkpoint(root, "spare_jax", config="pi05_spare")
    site = make_site(
        tmp_path,
        catalog={
            "known_configs": ["pi05_fr3_all_state", "pi05_spare"],
            "cfg_configs": [],
        },
    )
    assert [m.id for m in M.build(site).models] == ["spare_jax"]


def test_a_missing_folder_is_a_problem_not_an_error(tmp_path):
    catalog = M.build(make_site(tmp_path))
    assert catalog.models == () and catalog.skipped == ()
    assert catalog.problems == ("root_missing",)


def test_too_many_folders_is_reported(tmp_path):
    make_checkpoints(tmp_path, ["pi05_fr3_all_step49999"])
    site = make_site(tmp_path)

    def discover(root, **kwargs):
        return dataclasses.replace(
            policies.discover_report(root, **kwargs), truncated=True
        )

    assert M.build(site, discover=discover).problems == ("entries_truncated",)


def test_warnings_are_codes_without_paths(tmp_path):
    make_checkpoints(tmp_path, ["pi05_fr3_all_step49999"])
    site = make_site(tmp_path)

    def discover(root, **kwargs):
        report = policies.discover_report(root, **kwargs)
        entry = dataclasses.replace(
            report.entries[0],
            warnings=(
                "config_sources_disagree",
                "x:/abs/path/y",
                "role_withheld:reset_hint:name",
            ),
        )
        return dataclasses.replace(report, entries=(entry,))

    (model,) = M.build(site, discover=discover).models
    assert model.warnings == (
        "config_sources_disagree",
        "role_withheld:reset_hint:name",
    )


def test_the_default_rollout_group_names_the_model_and_the_mode():
    assert (
        M.default_group("pi05_fr3_all_step49999", True)
        == "pi05_fr3_all_step49999_rtc_demo"
    )
    assert (
        M.default_group("pi05_fr3_all_step49999", False)
        == "pi05_fr3_all_step49999_demo"
    )


# --- the cache --------------------------------------------------------------------


class Counting:
    def __init__(self):
        self.calls = 0

    def __call__(self, root, **kwargs):
        self.calls += 1
        return policies.discover_report(root, **kwargs)


def test_the_catalog_is_read_once_in_thirty_seconds(tmp_path):
    make_checkpoints(tmp_path, ["pi05_fr3_all_step49999"])
    site, discover = make_site(tmp_path), Counting()
    first = M.current(site, now=100.0, discover=discover)
    assert M.current(site, now=129.9, discover=discover) is first
    assert discover.calls == 1
    again = M.current(site, now=130.1, discover=discover)
    assert again is not first and discover.calls == 2


def test_get_uses_the_cache_and_knows_ids(tmp_path):
    make_checkpoints(tmp_path)
    site, discover = make_site(tmp_path), Counting()
    model = M.get(site, "pi05_fr3_all_step49999", now=1.0, discover=discover)
    assert model.config == "pi05_fr3_all_state"
    assert M.get(site, "recap_cfg_r1_step10000_jax", now=2.0, discover=discover)
    assert M.get(site, "reset_r1_step20000", now=3.0, discover=discover) is None
    assert M.get(site, "nope", now=3.0, discover=discover) is None
    assert discover.calls == 1


def test_clearing_the_cache_reads_again(tmp_path):
    make_checkpoints(tmp_path, ["pi05_fr3_all_step49999"])
    site, discover = make_site(tmp_path), Counting()
    M.current(site, now=1.0, discover=discover)
    M.cache_clear()
    M.current(site, now=2.0, discover=discover)
    assert discover.calls == 2


def test_another_site_does_not_get_the_cached_answer(tmp_path):
    make_checkpoints(tmp_path)
    discover = Counting()
    first = M.current(make_site(tmp_path), now=1.0, discover=discover)
    other = M.current(
        make_site(tmp_path, catalog={"excluded": ["pi05_fr3_all_step49999"]}),
        now=2.0,
        discover=discover,
    )
    assert discover.calls == 2
    assert "pi05_fr3_all_step49999" in [m.id for m in first.models]
    assert "pi05_fr3_all_step49999" not in [m.id for m in other.models]


# --- the page's view ---------------------------------------------------------------


def test_the_view_is_the_contract(tmp_path):
    site = make_site(tmp_path)
    make_checkpoints(tmp_path)
    view = M.view(M.build(site), site)
    assert set(view) == {"schema", "models", "skipped", "problems", "defaults"}
    assert view["schema"] == "levi.marvel0.models.v1"
    assert view["defaults"] == {
        "rtc": True,
        "fraction": 0.22,
        "cfg_guidance_scale": 1.0,
    }
    assert view["problems"] == []
    sft = view["models"][0]
    assert set(sft) == {
        "id",
        "config",
        "cfg_capable",
        "restricted_prompts",
        "verified",
        "labels",
        "step",
        "version",
        "variant",
        "size_bytes",
        "warnings",
        "rollout_group_default",
    }
    assert sft["id"] == "pi05_fr3_all_step49999" and sft["labels"] == []
    assert sft["rollout_group_default"] == {
        "rtc": "pi05_fr3_all_step49999_rtc_demo",
        "plain": "pi05_fr3_all_step49999_demo",
    }
    assert view["models"][1]["labels"] == ["unverified"]
    assert {"id": "reset_r1_step20000", "reason": "excluded_by_site"} in view["skipped"]


def test_the_view_holds_no_path(tmp_path):
    site = make_site(tmp_path)
    make_checkpoints(tmp_path)
    text = json.dumps(M.view(M.build(site), site))
    assert str(tmp_path) not in text
    assert not re.search(r"(?<![\w.:-])/(?:[A-Za-z0-9_.~+@%=,-]+/)+", text)


def test_the_real_tree_has_the_names_the_site_sample_knows():
    assert set(FORWARD) <= set(REAL_TREE)
