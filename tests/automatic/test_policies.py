"""Evaluation-policy checkpoint discovery (levi/automatic/policies.py): read only,
no openpi, no weights, no recomputed hashes, no symlink following."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from levi.automatic import policies as pol

HEX_A = "a" * 64
HEX_B = "b" * 64


def _w(path: Path, text: str | bytes = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def _jax(root: Path, name: str, version: dict | str | None = None, **extra) -> Path:
    d = root / name
    _w(d / "params" / "manifest.ocdbt", b"\0" * 100)
    _w(d / "params" / "d" / "blob", b"\0" * 400)
    _w(d / "norm_stats.json", "{}")
    _w(d / "assets" / "norm_stats.json", "{}")
    if isinstance(version, dict):
        _w(d / "VERSION.json", json.dumps(version))
    elif isinstance(version, str):
        _w(d / "VERSION.json", version)
    for fn, body in extra.items():
        _w(d / fn.replace("__", "."), body)
    return d


def _torch(root: Path, name: str) -> Path:
    d = root / name
    _w(d / "actor" / "model_state_dict" / "full_weights.pt", b"\0" * 50)
    return d


def _by_name(entries):
    return {e.name: e for e in entries}


# --- classification ---------------------------------------------------------------------


def test_lists_deployable_and_pytorch_dirs(tmp_path):
    _jax(tmp_path, "sft_jax")
    _torch(tmp_path, "src_pt")
    _w(tmp_path / "note.txt")
    rep = pol.discover_report(tmp_path)
    got = _by_name(rep.entries)
    assert set(got) == {"sft_jax", "src_pt"}
    assert (
        got["sft_jax"].deployable
        and got["sft_jax"].is_jax
        and got["sft_jax"].kind == "jax"
    )
    assert got["src_pt"].kind == "pytorch" and not got["src_pt"].deployable
    assert "note.txt" in rep.skipped
    assert [e.name for e in pol.select(rep.entries)] == ["sft_jax"]


def test_jax_dir_without_norm_stats_is_not_deployable(tmp_path):
    d = _jax(tmp_path, "m")
    (d / "norm_stats.json").unlink()
    (d / "assets" / "norm_stats.json").unlink()
    (e,) = pol.discover(tmp_path)
    assert e.is_jax and not e.deployable
    assert "norm_stats.json:missing" in e.problems


def test_unrecognised_dir_is_unknown_not_an_error(tmp_path):
    _w(tmp_path / "weird" / "something.bin", b"abc")
    (e,) = pol.discover(tmp_path)
    assert (e.kind, e.role, e.deployable) == ("unknown", "unknown", False)
    assert "layout_not_recognised" in e.problems
    assert e.size_bytes == 3


def test_version_json_fields(tmp_path):
    _jax(
        tmp_path,
        "r2_jax",
        {
            "version": "r2",
            "model": "m",
            "role": "best",
            "step": 14300,
            "config": "pi05_fr3_all_state_cfg",
            "parallel_to": "r1 (kept unchanged; compare, do not replace)",
            "sibling_r2": "other_jax",
            "verified_2026_10_09": ["conversion"],
            "not_verified": ["real robot"],
        },
    )
    (e,) = pol.discover(tmp_path)
    assert (e.version, e.model, e.variant, e.step) == ("r2", "m", "best", 14300)
    assert (e.config, e.config_source) == ("pi05_fr3_all_state_cfg", "VERSION.json")
    assert e.version_note == "r1 (kept unchanged; compare, do not replace)"
    assert e.siblings == ("other_jax",)
    assert e.verified == ("conversion",) and e.not_verified == ("real robot",)
    assert e.role == "forward" and e.role_source == "convention:config"


def test_explicit_role_wins_and_reset_is_never_guessed(tmp_path):
    _jax(tmp_path, "rst", {"role": "reset", "config": "pi05_fr3_all_state"})
    _jax(tmp_path, "fwd", {"policy_role": "forward", "role": "best"})
    _jax(tmp_path, "other", {"config": "something_else"})
    got = _by_name(pol.discover(tmp_path))
    assert (got["rst"].role, got["rst"].role_source) == ("reset", "VERSION.json")
    assert got["rst"].variant is None  # "reset" is a role, not a variant
    assert (got["fwd"].role, got["fwd"].variant) == ("forward", "best")
    assert got["other"].role == "unknown"
    entries = list(got.values())
    assert [e.name for e in pol.select(entries, "reset")] == ["rst"]


def test_no_reset_checkpoint_gives_empty_reset_list(tmp_path):
    _jax(tmp_path, "a", {"config": "pi05_fr3_all_state"})
    _jax(tmp_path, "b", {"config": "pi05_fr3_all_state_cfg"})
    entries = pol.discover(tmp_path)
    assert pol.select(entries, "reset") == []
    assert len(pol.select(entries, "forward")) == 2


def test_recipe_overrides(tmp_path):
    _jax(tmp_path, "x")
    (e,) = pol.discover(tmp_path)
    assert e.config is None and e.role == "unknown"
    recipe = {"configs": {"x": "cfg1"}, "roles": {"x": "reset"}}
    (e,) = pol.discover(tmp_path, recipe=recipe)
    assert (e.config, e.config_source) == ("cfg1", "recipe")
    assert (e.role, e.role_source) == ("reset", "recipe")


def test_config_from_readme_and_training_name_is_not_used_to_serve(tmp_path):
    _jax(
        tmp_path,
        "a_jax",
        README_DEPLOY__md="# Title here\n\n```\n  --policy.config pi05_fr3_all_state_cfg \\\n  --policy.dir x\n```\n",
    )
    _jax(
        tmp_path,
        "b_jax",
        README_DEPLOY__md="# T\n\n| | |\n|---|---|\n| 配置名 | `fr3_robotiq_state` |\n",
    )
    got = _by_name(
        pol.discover(tmp_path, recipe={"configs": {"b_jax": "pi05_fr3_all_state"}})
    )
    assert (got["a_jax"].config, got["a_jax"].config_source) == (
        "pi05_fr3_all_state_cfg",
        "README_DEPLOY.md",
    )
    assert got["a_jax"].readme_title == "Title here"
    b = got["b_jax"]
    assert b.training_config_name == "fr3_robotiq_state"
    assert b.config == "pi05_fr3_all_state"
    assert "training_config_name_differs_from_serving_config" in b.warnings


def test_cfg_checkpoint_with_plain_config_warns(tmp_path):
    _jax(tmp_path, "model_cfg_r9_jax", {"config": "pi05_fr3_all_state"})
    (e,) = pol.discover(tmp_path)
    assert "cfg_checkpoint_without_cfg_config" in e.warnings


# --- sha256 records ---------------------------------------------------------------------


def test_sha256_records_are_read_not_computed(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a hash was computed")

    monkeypatch.setattr(hashlib, "sha256", boom)
    monkeypatch.setattr(hashlib, "new", boom)
    _jax(tmp_path, "none_jax")
    _jax(
        tmp_path,
        "conv_jax",
        CONVERSION__json=json.dumps(
            {"source_sha256": HEX_A, "norm_stats_sha256": HEX_B}
        ),
    )
    d = _torch(tmp_path, "own_pt")
    _w(d / "checkpoint.sha256", f"{HEX_A}  actor/a.pt\n{HEX_B}  actor/b.pt\n")
    _torch(tmp_path, "listed_pt")
    _w(
        tmp_path / "any_name.sha256",
        f"{HEX_A}  listed_pt/actor/a\n{HEX_B}  listed_pt/x\n{HEX_A}  other/y\n",
    )
    got = _by_name(pol.discover(tmp_path))
    assert (
        got["none_jax"].sha256_state == "missing"
        and got["none_jax"].sha256_records == ()
    )
    (rec,) = got["conv_jax"].sha256_records
    assert (rec.kind, rec.covers, rec.entries) == (
        "conversion",
        "source_weights+norm_stats",
        2,
    )
    assert got["conv_jax"].converted
    (rec,) = got["own_pt"].sha256_records
    assert (rec.source, rec.entries) == ("own_pt/checkpoint.sha256", 2)
    (rec,) = got[
        "listed_pt"
    ].sha256_records  # root-level list, attributed by path prefix
    assert (rec.source, rec.entries) == ("any_name.sha256", 2)
    assert got["listed_pt"].sha256_state == "recorded"


def test_malformed_sha256_and_conversion_hashes(tmp_path):
    d = _jax(
        tmp_path,
        "m",
        CONVERSION__json=json.dumps({"source_sha256": "zz", "norm_stats_sha256": 5}),
    )
    _w(d / "bad.sha256", "not-a-hash  file\n")
    (e,) = pol.discover(tmp_path)
    assert e.sha256_state == "missing"
    assert any("bad_lines" in p for p in e.problems)


# --- robustness -------------------------------------------------------------------------


def test_bad_json_files_record_a_reason(tmp_path):
    _jax(tmp_path, "a", "{not json", CONVERSION__json="[1]")
    _jax(tmp_path, "b", "x" * (pol.MAX_META_BYTES + 10))
    got = _by_name(pol.discover(tmp_path))
    assert got["a"].deployable
    assert any(p.startswith("VERSION.json:bad_json") for p in got["a"].problems)
    assert "CONVERSION.json:not_an_object" in got["a"].problems
    assert not got["a"].converted
    assert "VERSION.json:too_large" in got["b"].problems
    assert got["a"].version is None


def test_wrong_types_in_version_json_do_not_crash(tmp_path):
    _jax(
        tmp_path,
        "t",
        {
            "step": True,
            "version": 3,
            "not_verified": "x",
            "role": ["a"],
            "sibling_x": 1,
        },
    )
    (e,) = pol.discover(tmp_path)
    assert (e.step, e.version, e.not_verified, e.siblings) == (None, None, (), ())
    assert e.role == "unknown"


def test_symlinks_are_never_followed(tmp_path):
    outside = tmp_path / "outside"
    _jax(outside, "secret_jax", {"config": "pi05_fr3_all_state"})
    root = tmp_path / "root"
    root.mkdir()
    os.symlink(outside / "secret_jax", root / "link_jax")
    d = _jax(root, "real_jax")
    os.symlink(outside / "secret_jax" / "norm_stats.json", d / "CONVERSION.json")
    os.symlink(outside / "secret_jax", d / "params" / "escape")
    os.symlink(outside / "secret_jax" / "norm_stats.json", root / "extra.sha256")
    rep = pol.discover_report(root)
    assert [e.name for e in rep.entries] == ["real_jax"]
    assert "link_jax" in rep.skipped
    (e,) = rep.entries
    assert not e.converted
    assert e.size_bytes == 504  # 100+400+2+2 bytes; the escaping links were not walked
    assert e.sha256_records == ()
    assert any(p.startswith("CONVERSION.json:unreadable") for p in e.problems)


def test_symlinked_root_resolves_and_missing_root_is_reported(tmp_path):
    real = tmp_path / "real"
    _jax(real, "a")
    os.symlink(real, tmp_path / "alias")
    assert [e.name for e in pol.discover(tmp_path / "alias")] == ["a"]
    rep = pol.discover_report(tmp_path / "nope")
    assert rep.entries == () and rep.problems == ("root_missing",)
    f = _w(tmp_path / "file")
    assert pol.discover_report(f).problems == ("root_not_a_directory",)


def test_hidden_and_fifo_entries_are_skipped(tmp_path):
    _jax(tmp_path, ".hidden_jax")
    _jax(tmp_path, "ok")
    if hasattr(os, "mkfifo"):
        os.mkfifo(tmp_path / "ok" / "VERSION.json")  # must not block the reader
    rep = pol.discover_report(tmp_path)
    assert [e.name for e in rep.entries] == ["ok"]
    assert ".hidden_jax" in rep.skipped
    assert "VERSION.json:not_a_regular_file" in rep.entries[0].problems


def test_same_identity_is_flagged_on_both(tmp_path):
    v = {"version": "r2", "model": "m", "role": "best", "step": 1}
    _jax(tmp_path, "one_jax", v)
    _jax(tmp_path, "two_jax", v)
    _jax(tmp_path, "three_jax", {**v, "step": 2})
    got = _by_name(pol.discover(tmp_path))
    assert any(
        p.startswith("duplicate_identity:two_jax") for p in got["one_jax"].problems
    )
    assert any(
        p.startswith("duplicate_identity:one_jax") for p in got["two_jax"].problems
    )
    assert not any("duplicate" in p for p in got["three_jax"].problems)


def test_names_equal_after_folding_are_flagged(tmp_path):
    # NFC vs NFD spelling of the same name: two directories on Linux, one for a human
    _jax(tmp_path, "café_jax")
    _jax(tmp_path, "café_jax")
    entries = pol.discover(tmp_path)
    assert len(entries) == 2
    assert all(any(p.startswith("name_conflict:") for p in e.problems) for e in entries)


def test_entry_cap_and_file_cap(tmp_path):
    for i in range(5):
        _jax(tmp_path, f"m{i}")
    rep = pol.discover_report(tmp_path, max_entries=3)
    assert rep.truncated and [e.name for e in rep.entries] == ["m0", "m1", "m2"]
    rep = pol.discover_report(tmp_path, max_files_per_entry=2)
    assert all(
        e.size_truncated and "size_is_a_lower_bound" in e.warnings for e in rep.entries
    )
    assert not pol.discover_report(tmp_path).truncated


def test_discovery_changes_nothing(tmp_path):
    d = _jax(tmp_path, "m", {"config": "pi05_fr3_all_state"}, CONVERSION__json="{}")
    _w(tmp_path / "x.sha256", f"{HEX_A}  m/f\n")

    def snap():
        out = {}
        for p in sorted(tmp_path.rglob("*")):
            st = p.lstat()
            out[str(p)] = (st.st_mtime_ns, st.st_size)
        return out

    before = snap()
    pol.discover(tmp_path)
    pol.discover_report(tmp_path)
    assert snap() == before and d.exists()


def test_to_dict_is_json_serialisable(tmp_path):
    _jax(
        tmp_path,
        "m",
        {"config": "pi05_fr3_all_state", "step": 3},
        CONVERSION__json=json.dumps({"source_sha256": HEX_A}),
    )
    rep = pol.discover_report(tmp_path)
    data = json.loads(json.dumps(rep.to_dict()))
    assert data["entries"][0]["sha256_records"][0]["kind"] == "conversion"
    assert data["entries"][0]["role"] == "forward"


def test_import_has_no_openpi_or_heavy_modules():
    code = (
        "import sys; import levi.automatic.policies; "
        "bad=[m for m in sys.modules if m.split('.')[0] in ('openpi','jax','torch')]; "
        "print(bad)"
    )
    root = Path(__file__).resolve().parents[2]
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(root)},
        check=True,
    ).stdout.strip()
    assert out == "[]"


# --- review round: conservative roles ---------------------------------------------------


def _role(tmp_path, name="m_jax", version=None, **extra):
    _jax(tmp_path, name, version, **extra)
    got = _by_name(pol.discover(tmp_path))
    return got[name]


import pytest


@pytest.mark.parametrize(
    "name,version",
    [
        ("pi05_fr3_reset_step5000", {"config": "pi05_fr3_all_state"}),
        ("RESET_jax", {"config": "pi05_fr3_all_state"}),
        ("pi05_recovery_jax", {"config": "pi05_fr3_all_state"}),
        ("pi05_recover_jax", {"config": "pi05_fr3_all_state"}),
        ("pi05_return_jax", {"config": "pi05_fr3_all_state"}),
        ("pi05_go_home_jax", {"config": "pi05_fr3_all_state"}),
        ("m_jax", {"config": "pi05_fr3_all_state", "policy_role": "reset_v2"}),
        ("m_jax", {"config": "pi05_fr3_all_state", "policy_role": "recovery"}),
        ("m_jax", {"config": "pi05_fr3_all_state", "policy_role": "best"}),
        ("m_jax", {"config": "pi05_fr3_all_state", "policy_role": 3}),
        ("m_jax", {"config": "pi05_fr3_all_state", "role": "reset_best"}),
        ("m_jax", {"config": "pi05_fr3_all_state", "role": "go_home"}),
        ("m_jax", {"config": "pi05_fr3_all_state", "model": "arm_reset_policy"}),
        ("m_jax", {"config": "pi05_fr3_all_state", "note": "to Return the arm"}),
        ("m_jax", {"config": "pi05_fr3_all_state", "reset_policy": True}),  # key name
        ("m_jax", {"config": "pi05_fr3_reset_state"}),
    ],
)
def test_reset_hints_never_give_forward_or_reset(tmp_path, name, version):
    e = _role(tmp_path, name, version)
    assert e.role == "unknown", (e.role, e.role_source)
    assert pol.select([e], "forward") == []
    assert pol.select([e], "reset") == []
    assert pol.select([e]) == [e]  # still a deployable checkpoint, role unknown


def test_hint_in_readme_title_withholds_forward(tmp_path):
    e = _role(
        tmp_path,
        "m_jax",
        {"config": "pi05_fr3_all_state"},
        README_DEPLOY__md="# Reset policy, step 9\n",
    )
    assert e.role == "unknown"


@pytest.mark.parametrize("val", ["Reset", " RESET\t", "reset"])
def test_declared_reset_is_normalised(tmp_path, val):
    e = _role(tmp_path, "m_jax", {"policy_role": val})
    assert (e.role, e.role_source) == ("reset", "VERSION.json")
    assert [x.name for x in pol.select([e], "reset")] == ["m_jax"]


@pytest.mark.parametrize("val", ["Forward", " forward ", "FORWARD"])
def test_declared_forward_is_normalised(tmp_path, val):
    e = _role(tmp_path, "m_jax", {"role": val})
    assert (e.role, e.role_source) == ("forward", "VERSION.json")


def test_declared_forward_with_a_reset_hint_is_withheld(tmp_path):
    e = _role(tmp_path, "reset_jax", {"policy_role": "forward"})
    assert e.role == "unknown"
    assert "role_hint_conflicts_with_declaration" in e.problems


def test_conflicting_declarations_are_unknown(tmp_path):
    e = _role(tmp_path, "m_jax", {"policy_role": "forward", "role": "reset"})
    assert e.role == "unknown" and "role_declarations_conflict" in e.problems


def test_variant_roles_like_best_keep_the_config_convention(tmp_path):
    e = _role(tmp_path, "m_jax", {"role": "best", "config": "pi05_fr3_all_state_cfg"})
    assert (e.role, e.role_source, e.variant) == (
        "forward",
        "convention:config",
        "best",
    )


def test_select_include_unknown_is_explicit(tmp_path):
    _jax(tmp_path, "a_jax", {"config": "pi05_fr3_all_state"})
    _jax(tmp_path, "b_jax", {"config": "other"})
    _jax(tmp_path, "c_jax", {"policy_role": "reset"})
    es = pol.discover(tmp_path)
    assert [e.name for e in pol.select(es, "forward")] == ["a_jax"]
    assert [e.name for e in pol.select(es, "forward", include_unknown=True)] == [
        "a_jax",
        "b_jax",
    ]
    assert [e.name for e in pol.select(es, "reset", include_unknown=True)] == [
        "b_jax",
        "c_jax",
    ]
    assert [e.name for e in pol.select(es, "unknown")] == ["b_jax"]


# --- review round: hashes are not "verified" --------------------------------------------


def test_conversion_only_hashes_are_indirect(tmp_path):
    _jax(
        tmp_path,
        "c_jax",
        CONVERSION__json=json.dumps(
            {
                "source_sha256": HEX_A,
                "norm_stats_sha256": HEX_B,
                "source": "other/dir/full_weights.pt",
                "norm_stats_from": "checkpoints/sft",
            }
        ),
    )
    (e,) = pol.discover(tmp_path)
    assert e.sha256_state == "indirect" and e.params_hashed is False
    (rec,) = e.sha256_records
    assert (
        "other/dir/full_weights.pt" in rec.subject and "checkpoints/sft" in rec.subject
    )
    assert rec.covers == "source_weights+norm_stats"


def test_manifest_of_params_is_recorded_and_params_hashed(tmp_path):
    d = _jax(tmp_path, "p_jax")
    _w(d / "SHA256SUMS.sha256", f"{HEX_A}  params/d/blob\n{HEX_B}  norm_stats.json\n")
    (e,) = pol.discover(tmp_path)
    assert (e.sha256_state, e.params_hashed) == ("recorded", True)
    assert e.sha256_records[0].subject == "this_dir"


def test_manifest_without_weights_is_recorded_but_not_params_hashed(tmp_path):
    d = _jax(tmp_path, "p_jax")
    _w(d / "x.sha256", f"{HEX_A}  norm_stats.json\n")
    (e,) = pol.discover(tmp_path)
    assert (e.sha256_state, e.params_hashed) == ("recorded", False)


def test_manifest_plus_conversion_is_recorded(tmp_path):
    d = _jax(tmp_path, "p_jax", CONVERSION__json=json.dumps({"source_sha256": HEX_A}))
    _w(d / "x.sha256", f"{HEX_A}  params/d/blob\n")
    (e,) = pol.discover(tmp_path)
    assert e.sha256_state == "recorded" and len(e.sha256_records) == 2


def test_root_list_marks_weights_by_prefix(tmp_path):
    _torch(tmp_path, "pt")
    _w(tmp_path / "l.sha256", f"{HEX_A}  pt/actor/model_state_dict/full_weights.pt\n")
    (e,) = pol.discover(tmp_path)
    assert (e.sha256_state, e.params_hashed) == ("recorded", True)


def test_hash_list_paths_cannot_claim_other_dirs(tmp_path):
    _jax(tmp_path, "hidden")  # not listed: hidden names are skipped...
    _jax(tmp_path, "foo")
    _w(
        tmp_path / "l.sha256",
        f"{HEX_A}  ../foo/params/x\n{HEX_A}  /foo/params/y\n{HEX_A}  .foo/params/z\n{HEX_A}  bar/../foo/params/w\n",
    )
    (e,) = [x for x in pol.discover(tmp_path) if x.name == "foo"]
    assert e.sha256_state == "missing"


def test_leading_dot_slash_is_still_attributed(tmp_path):
    _jax(tmp_path, "foo")
    _w(tmp_path / "l.sha256", f"{HEX_A}  ./foo/params/x\n")
    (e,) = [x for x in pol.discover(tmp_path) if x.name == "foo"]
    assert e.sha256_state == "recorded"


def test_extra_sha256_lists_in_a_directory_are_reported(tmp_path):
    d = _jax(tmp_path, "m")
    for i in range(10):
        _w(d / f"l{i}.sha256", f"{HEX_A}  f{i}\n")
    (e,) = pol.discover(tmp_path)
    assert "sha256_lists_truncated" in e.problems


# --- review round: robustness -----------------------------------------------------------


@pytest.mark.parametrize("bad", [None, "a\0b", "", 5])
def test_bad_root_argument_returns_a_reason(bad):
    rep = pol.discover_report(bad)
    assert rep.entries == () and len(rep.problems) == 1
    assert rep.problems[0] in ("root_invalid", "root_missing")
    assert pol.discover(bad) == []


def test_nul_and_none_report_root_invalid():
    assert pol.discover_report(None).problems == ("root_invalid",)
    assert pol.discover_report("a\0b").problems == ("root_invalid",)


def test_directories_and_links_count_toward_the_size_cap(tmp_path):
    d = _jax(tmp_path, "m")
    for i in range(50):
        (d / "flood" / f"sub{i}").mkdir(parents=True)
        os.symlink("params", d / "flood" / f"ln{i}")
    (e,) = pol.discover(tmp_path, max_files_per_entry=20)
    assert e.size_truncated and "size_is_a_lower_bound" in e.warnings
    (e,) = pol.discover(tmp_path)
    assert not e.size_truncated


def test_norm_stats_reason_is_kept(tmp_path):
    d = _jax(tmp_path, "m")
    (d / "norm_stats.json").unlink()
    (d / "assets" / "norm_stats.json").unlink()
    os.mkfifo(d / "norm_stats.json")
    (e,) = pol.discover(tmp_path)
    assert not e.deployable
    assert "norm_stats.json:not_a_regular_file" in e.problems
    assert "norm_stats.json:missing" not in e.problems


def test_deployable_needs_assets_dir(tmp_path):
    d = _jax(tmp_path, "m")
    import shutil

    shutil.rmtree(d / "assets")
    (e,) = pol.discover(tmp_path)
    assert e.is_jax and not e.deployable
    assert "assets:missing" in e.problems
    assert pol.select([e]) == []


def test_standard_openpi_layout_with_norm_stats_only_under_assets(tmp_path):
    d = _jax(tmp_path, "m")
    (d / "norm_stats.json").unlink()
    _w(d / "assets" / "asset_id" / "norm_stats.json", "{}")
    (d / "assets" / "norm_stats.json").unlink()
    (e,) = pol.discover(tmp_path)
    assert e.deployable


def test_config_sources_disagreeing_is_warned(tmp_path):
    _jax(
        tmp_path,
        "a",
        {"config": "pi05_fr3_all_state"},
        README_DEPLOY__md="# T\n--policy.config pi05_fr3_all_state_cfg\n",
    )
    _jax(
        tmp_path,
        "b",
        None,
        README_DEPLOY__md="# T\n--policy.config c1\n--policy.config c2\n",
    )
    _jax(
        tmp_path,
        "c",
        {"config": "same"},
        README_DEPLOY__md="# T\n--policy.config same\n",
    )
    got = _by_name(pol.discover(tmp_path))
    assert "config_sources_disagree" in got["a"].warnings
    assert "config_sources_disagree" in got["b"].warnings
    assert "config_sources_disagree" not in got["c"].warnings


def test_dotdot_inside_a_hash_path_is_not_attributed(tmp_path):
    _jax(tmp_path, "foo")
    _jax(tmp_path, "bar")
    _w(tmp_path / "l.sha256", f"{HEX_A}  foo/../bar/params/x\n")
    got = _by_name(pol.discover(tmp_path))
    assert got["foo"].sha256_state == "missing" and got["bar"].sha256_state == "missing"
