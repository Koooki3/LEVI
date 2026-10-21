"""The preparation of an AERI run (``levi/marvel0/aeri_prep.py``): the versioned
port of ``new_run.py``, the code pin, the templates, the run names and the
read-only check, on a made-up machine (``aeri_world.py``) with a real git
repository and real child processes. Nothing here reaches the real machine
except, when it is present, a read of the real ``new_run.py`` to run it next to
the port on the same inputs."""

import ast
import dataclasses
import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from aeri_world import DEFAULT_PROFILES, NOW, World, installed_profiles

from levi.marvel0 import aeri_prep, record
from levi.marvel0 import site as marvel0_site
from levi.marvel0.aeri_prep import Refuse

EXPECTED = Path(__file__).parent / "fixtures" / "aeri_templates" / "expected"


@pytest.fixture
def world(tmp_path, monkeypatch):
    installed_profiles(monkeypatch, DEFAULT_PROFILES)
    return World(tmp_path)


def page_dir(world):
    return aeri_prep.state_dir(world.aeri, aeri_prep.StateRoot(world.state), True)


def prep_host(world):
    return aeri_prep.Host(now=lambda: NOW, registry=lambda: world.registry)


def prepare(world, **change):
    return aeri_prep.prepare(
        world.aeri, world.form(**change), prep_host(world), page_dir(world)
    )


def refusal(world, **change) -> Refuse:
    with pytest.raises(Refuse) as caught:
        prepare(world, **change)
    return caught.value


def tree(root) -> dict:
    """Every file under ``root`` with its content hash (and every folder)."""
    out = {}
    for path in sorted(Path(root).rglob("*")):
        key = str(path.relative_to(root))
        out[key] = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "dir"
        )
    return out


def fill(world, text):
    return (
        text.replace("{X}", str(world.ext))
        .replace("{WT}", str(world.wt))
        .replace("{OPENPI}", str(world.openpi))
        .replace("{LOCAL_MODEL}", str(world.local_model))
    )


# --- the port is the script --------------------------------------------------------------

CASES = {
    "ext1": {
        "mode": "external",
        "rounds": 5,
        "steps": 200,
        "gripper_confirm_s": None,
        "start_home": False,
    },
    "ext2": {
        "mode": "external",
        "rounds": 10,
        "steps": 3000,
        "gripper_confirm_s": 4.0,
        "start_home": True,
    },
    "loc1": {
        "mode": "local",
        "rounds": 3,
        "steps": 50,
        "gripper_confirm_s": None,
        "start_home": False,
    },
}


@pytest.mark.parametrize("case", CASES)
def test_the_files_are_the_ones_new_run_writes(world, case):
    """The three expected folders were produced by the real ``new_run.py`` (copied
    next to its templates, with a stand-in check); the port must write the same
    job, the same binding and the same RUN.json fields."""
    prepared = prepare(world, **CASES[case])
    run = world.runs / prepared["name"]
    expected = EXPECTED / case
    job = (run / "job.yaml").read_text()
    header, body = job.split("\n", 1)
    theirs = json.loads(fill(world, (expected / "RUN.json").read_text()))
    # (the expected folders are r01, r02 and loc-r01 of one run of the script;
    # each case here starts on an empty runs folder)
    renamed = theirs["name"]
    assert header.startswith(f"# {CASES[case]['mode'].upper()} mode run, written by")

    def same_name(text):
        return fill(world, text).replace(renamed, prepared["name"])

    assert body == same_name((expected / "job.yaml").read_text())
    assert (run / "binding.json").read_text() == same_name(
        (expected / "binding.json").read_text()
    )
    mine = json.loads((run / "RUN.json").read_text())
    theirs = json.loads(same_name((expected / "RUN.json").read_text()))
    assert {k: mine[k] for k in theirs} == theirs
    assert set(mine) - set(theirs) == {
        "aeri_head",
        "profile",
        "start_home",
        "gripper_confirm_s",
        "prepared_at",
        "prepared_by",
        "files_sha256",
        "templates_sha256",
    }
    assert mine["prepared_by"] == "marvel0-page"
    assert mine["aeri_head"] == world.head()[:8]


def _find_new_run():
    """The real script of the maintainer's workspace, when this machine has it."""
    try:
        main = marvel0_site.main_checkout()
    except Exception:  # noqa: BLE001
        return None
    path = main / "lab" / "aeri-external" / "tools" / "new_run.py"
    return path if path.is_file() else None


REAL_NEW_RUN = _find_new_run()


@pytest.mark.skipif(REAL_NEW_RUN is None, reason="the workspace's new_run.py is absent")
@pytest.mark.skipif(
    not Path("/home/marvel/work/wenkai/LEVI/.venv/bin/python").exists(),
    reason="new_run.py runs its check with a fixed interpreter",
)
@pytest.mark.parametrize("case", CASES)
def test_the_port_agrees_with_the_script_it_was_ported_from_today(world, case):
    """The same inputs through the script as it is now and through the port:
    when the script changes, this fails and says the port must follow."""
    real = world.tmp / "real"
    (real / "tools").mkdir(parents=True)
    (real / "runs").mkdir()
    (real / "templates").mkdir()
    for name in aeri_prep.TEMPLATE_FILES:
        (real / "templates" / name).write_bytes((world.templates / name).read_bytes())
    script = real / "tools" / "new_run.py"
    script.write_text(REAL_NEW_RUN.read_text())
    form = CASES[case]
    name = "aeri-20261021-" + ("ext" if form["mode"] == "external" else "loc") + "-r01"
    argv = [
        "/home/marvel/work/wenkai/LEVI/.venv/bin/python",
        str(script),
        "--mode",
        form["mode"],
        "--task",
        "pick the pepper into the white plate",
        "--rounds",
        str(form["rounds"]),
        "--steps",
        str(form["steps"]),
        "--aeri-worktree",
        str(world.wt),
        "--name",
        name,
    ]
    if form["gripper_confirm_s"] is not None:
        argv += ["--gripper-confirm-s", str(form["gripper_confirm_s"])]
    if form["start_home"]:
        argv.append("--start-home")
    done = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    theirs = real / "runs" / name
    prepared = prepare(world, **form)
    mine = world.runs / prepared["name"]
    assert prepared["name"] == name

    def norm(text, root, local):
        return (
            text.replace(str(root), "{X}")
            .replace("/home/marvel/work/wenkai/openpi", "{OPENPI}")
            .replace(str(world.openpi), "{OPENPI}")
            .replace(str(world.wt), "{WT}")
            .replace(
                "/home/marvel/work/wenkai/LEVI/lab/models/Qwen--Qwen3-VL-4B-Instruct-FP8",
                "{LOCAL_MODEL}",
            )
            .replace(str(world.local_model), "{LOCAL_MODEL}")
            if local is None
            else text
        )

    for file in ("job.yaml", "binding.json"):
        a = norm((theirs / file).read_text(), real.joinpath("."), None)
        b = norm((mine / file).read_text(), world.ext, None)
        if file == "job.yaml":
            a, b = a.split("\n", 1)[1], b.split("\n", 1)[1]
        assert a == b, file
    their_run = json.loads(norm((theirs / "RUN.json").read_text(), real, None))
    my_run = json.loads(norm((mine / "RUN.json").read_text(), world.ext, None))
    assert {k: my_run[k] for k in their_run if k != "aeri_head"} == {
        k: v for k, v in their_run.items() if k != "aeri_head"
    }


def test_the_run_folder_holds_exactly_the_files_of_a_run(world):
    external = prepare(world)
    run = world.runs / external["name"]
    assert sorted(p.name for p in run.iterdir()) == [
        "RUN.json",
        "binding.json",
        "check.json",
        "job.yaml",
        "queue",
    ]
    assert (run / "queue").is_dir() and not any((run / "queue").iterdir())
    assert (run / "check.json").read_text().strip().startswith("{")
    local = prepare(world, mode="local")
    assert sorted(p.name for p in (world.runs / local["name"]).iterdir()) == [
        "RUN.json",
        "binding.json",
        "check.json",
        "job.yaml",
    ]


def test_the_command_is_the_one_typed_in_a_terminal_and_the_one_hosted(world):
    prepared = prepare(world)
    command = prepared["execute"]
    head, _, rest = command.partition(" && ")
    assert head == f"cd {world.openpi}"
    argv = shlex.split(rest)
    run = world.runs / prepared["name"]
    assert argv == aeri_prep.execute_argv(
        world.aeri, run, prepared["plan_sha256"], prepared["binding_sha256"]
    )
    assert shlex.join(argv) == rest, "nothing in it needed quoting"
    assert argv[:4] == ["env", "-u", "VIRTUAL_ENV", f"PYTHONPATH={world.wt}"]
    assert argv[argv.index("execute") + 1 :] == [
        "--config",
        f"{run}/job.yaml",
        "--binding",
        f"{run}/binding.json",
        "--expect-plan",
        prepared["plan_sha256"],
        "--expect-binding",
        prepared["binding_sha256"],
    ]
    assert prepared["confirm"] == f"execute {prepared['name']}"


# --- the check ---------------------------------------------------------------------------


def test_the_check_is_read_only_niced_and_given_nothing_else(world):
    prepared = prepare(world)
    seen = world.seen_check()
    run = world.runs / prepared["name"]
    assert seen["argv"] == [
        "scripts/aeri.py",
        "check",
        "--config",
        f"{run}/job.yaml",
        "--binding",
        f"{run}/binding.json",
    ]
    assert seen["cwd"] == str(world.wt)
    assert seen["nice"] == 19
    # python adds LC_CTYPE and the like to a bare environment: the three we set,
    # and nothing of ours (no token, no LEVI_*).
    ours = {
        k: v
        for k, v in seen["env"].items()
        if k in {"PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PATH"}
    }
    assert ours == {
        "PYTHONPATH": str(world.wt),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": "/usr/bin:/bin",
    }
    assert not [k for k in seen["env"] if k.startswith(("LEVI", "HF_", "HOME", "USER"))]


def test_the_digests_come_from_the_check_not_from_the_page(world):
    other = "ab" * 32
    world.set_check(override={"plan_sha256": other})
    prepared = prepare(world)
    assert prepared["plan_sha256"] == other
    assert other in prepared["execute"]


@pytest.mark.parametrize(
    "behaviour, reason",
    [
        ({"mode": "fail"}, "returncode"),
        ({"mode": "text"}, "output"),
        ({"override": {"plan_sha256": "nope"}}, "digest"),
        ({"override": {"binding_sha256": None}}, "digest"),
        ({"override": {"hardware_verified": True}}, "hardware_verified"),
        ({"override": {"hardware_verified": None}}, "hardware_verified"),
        ({"override": {"reset_strategy": "manual"}}, "strategy"),
    ],
)
def test_a_check_that_refuses_leaves_no_folder_and_no_record(world, behaviour, reason):
    before = tree(world.root)
    world.set_check(**behaviour)
    error = refusal(world)
    assert error.code == "check_failed" and error.extra["reason"] == reason
    assert error.status == 422
    after = tree(world.root)
    after.pop("seen.json", None), before.pop("seen.json", None)
    assert not [p for p in after if p.startswith("aeri-external/runs/aeri-")]
    # nothing else was written either, but the folder of the page's own records
    changed = {k for k in after if after[k] != before.get(k)}
    assert not [k for k in changed if "record.json" in k], changed


def test_the_failure_says_what_the_check_said_without_paths(world):
    world.set_check(mode="fail")
    error = refusal(world)
    assert error.extra["returncode"] == 2
    assert "subtask.max_steps is out of range" in error.extra["tail"]
    assert str(world.root) not in json.dumps(error.extra)


def test_a_check_that_takes_too_long_is_killed_and_cleaned_up(world, monkeypatch):
    monkeypatch.setattr(aeri_prep, "CHECK_TIMEOUT_S", 0.5)
    world.set_check(mode="sleep", seconds=30)
    error = refusal(world)
    assert (error.code, error.extra["reason"]) == ("check_failed", "timeout")
    assert not list(world.runs.iterdir())


def test_a_check_that_cannot_start_is_not_run(world):
    aeri = world.aeri
    broken = dataclasses.replace(aeri, levi_python=str(world.root / "no-such-python"))
    with pytest.raises(Refuse) as caught:
        aeri_prep.prepare(broken, world.form(), prep_host(world), page_dir(world))
    assert (caught.value.code, caught.value.extra["reason"]) == (
        "check_failed",
        "returncode",
    )
    assert caught.value.extra["returncode"] in (126, 127), "nice could not run it"
    assert not list(world.runs.iterdir())


def test_a_runner_that_is_not_there_at_all_is_not_run(world):
    def gone(argv, **_):
        raise FileNotFoundError(argv[0])

    with pytest.raises(Refuse) as caught:
        aeri_prep.run_check(world.aeri, world.runs / "x", gone)
    assert (caught.value.code, caught.value.extra["reason"]) == (
        "check_failed",
        "not_run",
    )


def test_the_check_runs_in_the_pinned_tree_and_leaves_it_clean(world):
    prepare(world)
    state = aeri_prep.git_state(str(world.wt))
    assert state == {"head": world.head(), "dirty": False}


# --- the gates ---------------------------------------------------------------------------


def test_nothing_is_written_when_a_gate_refuses(world):
    before = tree(world.runs)
    world.commit_change()
    assert refusal(world).code == "code_pin_mismatch"
    assert tree(world.runs) == before


def test_the_worktree_must_be_at_the_pinned_commit(world):
    world.commit_change()
    error = refusal(world)
    assert error.code == "code_pin_mismatch" and error.status == 412
    assert error.extra["pin"] != error.extra["head"]
    assert error.extra["dirty"] is False


@pytest.mark.parametrize("how", ["modify", "untracked"])
def test_a_dirty_worktree_is_refused(world, how):
    world.dirty(how)
    error = refusal(world)
    assert error.code == "code_pin_mismatch" and error.extra["dirty"] is True


def test_a_worktree_git_cannot_read_is_refused_not_assumed_clean(world):
    world.reparse_site(worktree=str(world.root / "not-a-repo"))
    (world.root / "not-a-repo").mkdir()
    error = refusal(world)
    assert error.code == "code_pin_mismatch"
    assert error.extra["pin"] and error.extra["dirty"] is None


def test_a_folder_inside_a_repository_is_not_the_worktree(world):
    """``git -C <folder> rev-parse HEAD`` answers with the enclosing repository's
    HEAD; the pin must not be satisfied by that."""
    inside = world.wt / "scripts"
    assert aeri_prep.git_state(str(inside)) == {"head": None, "dirty": None}
    world.reparse_site(worktree=str(inside), pin_sha=world.head()[:8])
    error = refusal(world)
    assert error.code == "code_pin_mismatch" and error.extra["head"] is None


def test_the_pin_may_be_longer_and_must_be_a_prefix(world):
    world.reparse_site(pin_sha=world.head())
    assert prepare(world)["name"]
    world.reparse_site(
        pin_sha=world.head()[:7] + ("0" if world.head()[7] != "0" else "1")
    )
    assert refusal(world).code == "code_pin_mismatch"


@pytest.mark.parametrize(
    "pin, head, ok",
    [
        ("5bc750ca", "5bc750cacc12e375d77f5036b4976d560ac682a3", True),
        (
            "5bc750cacc12e375d77f5036b4976d560ac682a3",
            "5bc750cacc12e375d77f5036b4976d560ac682a3",
            True,
        ),
        ("5bc750cb", "5bc750cacc12e375d77f5036b4976d560ac682a3", False),
        ("5bc750c", "5bc750cacc12e375d77f5036b4976d560ac682a3", False),
        ("", "5bc750cacc12e375d77f5036b4976d560ac682a3", False),
        ("5BC750CA", "5bc750cacc12e375d77f5036b4976d560ac682a3", False),
        ("5bc750ca", None, False),
        (None, "5bc750cacc12e375d77f5036b4976d560ac682a3", False),
    ],
)
def test_pin_matches(pin, head, ok):
    assert aeri_prep.pin_matches(pin, head) is ok


@pytest.mark.parametrize("name", aeri_prep.TEMPLATE_FILES)
def test_a_changed_template_is_refused_and_named(world, name):
    path = world.templates / name
    path.write_text(path.read_text() + "\n")
    error = refusal(world)
    assert error.code == "templates_changed" and error.status == 412
    assert error.extra["which"] == [name]
    assert not list(world.runs.iterdir())


def test_a_template_that_is_gone_or_a_missing_sums_file_is_refused(world):
    (world.templates / "job.base.yaml").unlink()
    assert refusal(world).extra["which"] == ["job.base.yaml"]
    (world.templates / aeri_prep.SOURCES_FILE).unlink()
    assert refusal(world).extra["which"] == [aeri_prep.SOURCES_FILE]


def test_a_sums_file_of_the_old_location_is_matched_by_name(world):
    state = aeri_prep.templates_state(world.templates)
    assert state["ok"] is True and state["which"] == []
    text = (world.templates / aeri_prep.SOURCES_FILE).read_text()
    assert "/home/marvel/work/wenkai" in text, "the real file names its old folder"
    assert set(state["digests"]) == set(aeri_prep.TEMPLATE_FILES)


def test_the_repository_templates_are_the_real_ones_when_the_workspace_has_them():
    real = Path("/home/marvel/work/wenkai/LEVI/lab/aeri-external/templates")
    if not real.is_dir():
        pytest.skip("no workspace on this machine")
    fixtures = EXPECTED.parent
    for name in (*aeri_prep.TEMPLATE_FILES, aeri_prep.SOURCES_FILE):
        assert (fixtures / name).read_bytes() == (real / name).read_bytes(), (
            f"{name} was updated in the workspace: copy it to tests/marvel0/fixtures/aeri_templates/"
        )


def test_an_unknown_profile_is_refused(world, monkeypatch):
    installed_profiles(monkeypatch, {"bfdee2cf": False})
    error = refusal(world)
    assert error.code == "profile_unknown" and error.extra["profile"] == world.profile


def test_a_profile_table_that_cannot_be_read_is_unknown_not_empty(world, monkeypatch):
    module = installed_profiles(monkeypatch, DEFAULT_PROFILES)
    module.PROFILES = None
    assert aeri_prep.known_profiles() is None
    assert aeri_prep.profile_info(world.profile) == {"known": None, "start_home": None}
    assert refusal(world).code == "profile_unknown"


def test_the_profile_table_may_hold_values_or_records(monkeypatch):
    import types

    module = installed_profiles(monkeypatch, {})
    module.PROFILES = {
        "a": {"start_home": True},
        "b": types.SimpleNamespace(start_home=False),
        "c": types.SimpleNamespace(start_home="yes"),  # not a bool: not a profile
        3: {"start_home": True},
    }
    assert aeri_prep.known_profiles() == {
        "a": {"start_home": True},
        "b": {"start_home": False},
    }
    assert aeri_prep.profile_info("c")["known"] is False


def test_without_the_hosted_run_module_nothing_is_known(monkeypatch):
    import levi.marvel0

    monkeypatch.setitem(sys.modules, "levi.marvel0.aeri_state", None)  # import fails
    monkeypatch.delattr(levi.marvel0, "aeri_state", raising=False)
    assert aeri_prep.known_profiles() is None


def test_the_real_profile_table_knows_the_default_when_the_module_exists():
    try:
        from levi.marvel0 import aeri_state
    except ImportError:
        pytest.skip("the hosted run's module is not here yet")
    table = aeri_prep.known_profiles()
    assert table is not None
    assert table[marvel0_site.AERI_DEFAULT_PROFILE]["start_home"] is True
    assert aeri_state.PROFILES


def test_startup_homing_needs_a_profile_that_has_it(world):
    world.reparse_site(profile="bfdee2cf", pin_sha=world.head()[:8])
    error = refusal(world, start_home=True)
    assert error.code == "start_home_unsupported" and error.status == 422
    assert prepare(world, start_home=False)["start_home"] is False


# --- the form ----------------------------------------------------------------------------


def good(**change):
    return {
        "mode": "external",
        "task": "pick the pepper into the white plate",
        "rounds": 5,
        "steps": 200,
        "gripper_confirm_s": None,
        "start_home": False,
        **change,
    }


def check_form(world, **change):
    return aeri_prep.validate_form(good(**change), world.aeri, {"start_home": True})


@pytest.mark.parametrize(
    "change, field, reason",
    [
        ({"mode": "agent"}, "mode", "range"),
        ({"mode": None}, "mode", "range"),
        ({"task": None}, "task", "type"),
        ({"task": 5}, "task", "type"),
        ({"task": ""}, "task", "pattern"),
        ({"task": "ab"}, "task", "pattern"),
        ({"task": "Pick the pepper"}, "task", "pattern"),
        ({"task": 'pick "the" pepper'}, "task", "pattern"),
        ({"task": "pick the pepper\n"}, "task", "pattern"),
        ({"task": "pick\tthe pepper"}, "task", "pattern"),
        ({"task": " pick the pepper"}, "task", "pattern"),
        ({"task": "pick the pepper "}, "task", "pattern"),
        ({"task": "123"}, "task", "pattern"),
        ({"task": "pick $(id)"}, "task", "pattern"),
        ({"task": "pick the pepper; rm"}, "task", "pattern"),
        ({"task": "a" * 121}, "task", "pattern"),
        ({"rounds": 0}, "rounds", "range"),
        ({"rounds": 51}, "rounds", "range"),
        ({"rounds": True}, "rounds", "type"),
        ({"rounds": 5.0}, "rounds", "type"),
        ({"rounds": "5"}, "rounds", "type"),
        ({"rounds": None}, "rounds", "type"),
        ({"steps": 0}, "steps", "range"),
        ({"steps": 3001}, "steps", "range"),
        ({"steps": True}, "steps", "type"),
        ({"gripper_confirm_s": 0.9}, "gripper_confirm_s", "range"),
        ({"gripper_confirm_s": 10.1}, "gripper_confirm_s", "range"),
        ({"gripper_confirm_s": float("nan")}, "gripper_confirm_s", "range"),
        ({"gripper_confirm_s": float("inf")}, "gripper_confirm_s", "range"),
        ({"gripper_confirm_s": True}, "gripper_confirm_s", "type"),
        ({"gripper_confirm_s": "4"}, "gripper_confirm_s", "type"),
        ({"start_home": "yes"}, "start_home", "type"),
        ({"start_home": 1}, "start_home", "type"),
        ({"start_home": None}, "start_home", "type"),
    ],
)
def test_the_form_refuses_what_it_cannot_take(world, change, field, reason):
    with pytest.raises(Refuse) as caught:
        check_form(world, **change)
    assert caught.value.code == "form_invalid" and caught.value.status == 422
    assert (caught.value.extra["field"], caught.value.extra["reason"]) == (
        field,
        reason,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"rounds": 1},
        {"rounds": 50},
        {"steps": 1},
        {"steps": 3000},
        {"gripper_confirm_s": 1.0},
        {"gripper_confirm_s": 10},
        {"gripper_confirm_s": 4},
        {"task": "a b"},
        {"task": "pick the pepper, then the plate."},
        {"task": "it's a-b 3"},
        {"task": "a" * 120},
        {"mode": "local"},
    ],
)
def test_the_form_takes_its_limits_and_the_new_run_pattern(world, change):
    assert check_form(world, **change)


def test_local_mode_needs_a_model_in_the_site_file(tmp_path, monkeypatch):
    installed_profiles(monkeypatch, DEFAULT_PROFILES)
    world = World(tmp_path, local=False)
    assert world.aeri.local_model is None
    with pytest.raises(Refuse) as caught:
        aeri_prep.validate_form(good(mode="local"), world.aeri, {"start_home": True})
    assert caught.value.extra == {"field": "mode", "reason": "unavailable"}


def test_a_refused_form_writes_nothing(world):
    page_dir(world)  # (the page's own folder is made when the card is first used)
    before = tree(world.root)
    assert refusal(world, rounds=99).code == "form_invalid"
    assert refusal(world, task="Bad").code == "form_invalid"
    assert tree(world.root) == before


def test_the_limits_are_the_owners(world):
    assert aeri_prep.STEPS["max"] == 3000
    assert aeri_prep.ROUNDS["max"] == 50
    assert aeri_prep.GRIPPER_CONFIRM_S["default"] == 4.0


# --- the names ---------------------------------------------------------------------------


def test_the_first_name_of_the_day_is_r01_and_the_next_is_r02(world):
    assert prepare(world)["name"] == "aeri-20261021-ext-r01"
    assert prepare(world)["name"] == "aeri-20261021-ext-r02"
    assert prepare(world, mode="local")["name"] == "aeri-20261021-loc-r01"


@pytest.mark.parametrize("where", ["runs_root", "rollout", "registry", "page"])
def test_a_name_used_in_any_of_the_four_places_is_not_reused(world, where):
    taken = "aeri-20261021-ext-r01"
    if where == "runs_root":
        (world.runs / taken).mkdir()
    elif where == "rollout":
        (world.rollout / ".aeri" / "runs" / taken).mkdir()
    elif where == "registry":
        (world.registry / f"{taken}.json").write_text("{}")
    else:
        first = prepare(world)
        assert first["name"] == taken
        import shutil

        shutil.rmtree(world.runs / taken)  # the folder is gone; the record remains
    assert prepare(world)["name"] == "aeri-20261021-ext-r02"


def test_a_gap_is_filled_and_other_families_do_not_count(world):
    (world.runs / "aeri-20261021-ext-r01").mkdir()
    (world.runs / "aeri-20261021-ext-r03").mkdir()
    (world.runs / "aeri-20261021-loc-r02").mkdir()
    (world.runs / "aeri-20261020-ext-r02").mkdir()
    (world.runs / "notes").mkdir()
    assert prepare(world)["name"] == "aeri-20261021-ext-r02"


def test_taken_names_lists_where_each_one_is(world):
    (world.runs / "aeri-20261021-ext-r01").mkdir()
    (world.rollout / ".aeri" / "runs" / "aeri-20261021-ext-r01").mkdir()
    (world.registry / "aeri-20261021-ext-r01.json").write_text("{}")
    found = aeri_prep.taken_names(world.aeri, world.registry, ["aeri-20261021-ext-r01"])
    assert found == {
        "aeri-20261021-ext-r01": ["runs_root", "rollout", "registry", "page"]
    }
    assert aeri_prep.name_where(
        world.aeri, world.registry, "aeri-20261021-ext-r01", ["aeri-20261021-ext-r01"]
    ) == ["runs_root", "rollout", "registry", "page"]
    assert (
        aeri_prep.name_where(world.aeri, world.registry, "aeri-20261021-ext-r09") == []
    )


def test_a_name_taken_between_listing_and_creation_is_skipped(world, monkeypatch):
    real = aeri_prep.taken_names
    calls = []

    def late(aeri, registry, record_names=(), day=None, tag=None):
        found = real(aeri, registry, record_names, day, tag)
        if not calls:  # someone else takes r01 right after we looked
            (world.runs / "aeri-20261021-ext-r01").mkdir()
        calls.append(1)
        return found

    monkeypatch.setattr(aeri_prep, "taken_names", late)
    assert prepare(world)["name"] == "aeri-20261021-ext-r02"
    assert len(calls) == 2


def test_names_run_out_with_run_name_taken(world, monkeypatch):
    monkeypatch.setattr(aeri_prep, "NAME_TRIES", 3)
    monkeypatch.setattr(
        aeri_prep, "taken_names", lambda *a, **k: {}
    )  # a registry that never tells
    (world.runs / "aeri-20261021-ext-r01").mkdir()
    error = refusal(world)
    assert error.code == "run_name_taken" and error.status == 409
    assert error.extra["name"] == "aeri-20261021-ext-r01"


def test_a_missing_runs_folder_is_a_configuration_error(world):
    import shutil

    shutil.rmtree(world.runs)
    error = refusal(world)
    assert error.code == "aeri_unconfigured" and error.extra["reason"] == "runs_root"


# --- the prepared run, read again ----------------------------------------------------------


def reread(world, name, head="same"):
    return aeri_prep.read_prepared(
        world.aeri, name, world.head() if head == "same" else head
    )


def test_a_fresh_run_is_intact_and_carries_what_was_prepared(world):
    prepared = prepare(
        world, mode="external", rounds=7, steps=900, gripper_confirm_s=4.0
    )
    assert prepared["intact"] is True and prepared["problems"] == []
    assert (prepared["mode"], prepared["rounds"], prepared["steps"]) == (
        "external",
        7,
        900,
    )
    assert prepared["gripper_confirm_s"] == 4.0 and prepared["start_home"] is True
    assert prepared["profile"] == world.profile
    assert prepared["aeri_head"] == world.head()[:8]
    assert set(prepared["files_sha256"]) == {"job", "binding"}
    run = world.runs / prepared["name"]
    assert (
        prepared["files_sha256"]["job"]
        == hashlib.sha256((run / "job.yaml").read_bytes()).hexdigest()
    )
    assert (
        prepared["files_sha256"]["binding"]
        == hashlib.sha256((run / "binding.json").read_bytes()).hexdigest()
    )
    assert reread(world, prepared["name"]) == prepared


@pytest.mark.parametrize(
    "mutate, problem",
    [
        (lambda run: (run / "job.yaml").write_text("changed\n"), "files_changed"),
        (lambda run: (run / "binding.json").write_text("{}\n"), "files_changed"),
        (lambda run: (run / "job.yaml").unlink(), "files_changed"),
        (lambda run: (run / "RUN.json").unlink(), "run_json_missing"),
        (lambda run: (run / "RUN.json").write_text("{not json"), "run_json_missing"),
        (lambda run: (run / "RUN.json").write_text("[1]"), "run_json_missing"),
        (
            lambda run: (run / "RUN.json").write_text(
                (run / "RUN.json")
                .read_text()
                .replace(run.name, "aeri-20261021-ext-r99")
            ),
            "run_json_missing",
        ),
    ],
)
def test_a_changed_prepared_run_says_what_changed(world, mutate, problem):
    name = prepare(world)["name"]
    mutate(world.runs / name)
    got = reread(world, name)
    assert got["intact"] is False and problem in got["problems"]


def edit_run_json(world, name, **change):
    path = world.runs / name / "RUN.json"
    value = json.loads(path.read_text())
    for key, new in change.items():
        if new is None:
            value.pop(key)
        else:
            value[key] = new
    path.write_text(json.dumps(value))


def test_missing_digests_are_a_problem(world):
    name = prepare(world)["name"]
    edit_run_json(world, name, files_sha256=None)
    assert "digest_missing" in reread(world, name)["problems"]
    edit_run_json(world, name, files_sha256={"job": "x", "binding": "y"})
    assert "digest_missing" in reread(world, name)["problems"]


def test_a_run_prepared_by_the_script_is_not_one_the_page_hosts(world):
    name = prepare(world)["name"]
    edit_run_json(world, name, files_sha256=None, profile=None)
    got = reread(world, name)
    assert not got["intact"]
    assert "digest_missing" in got["problems"] and "profile_changed" in got["problems"]


def test_the_code_moving_after_prepare_is_a_problem(world):
    name = prepare(world)["name"]
    world.commit_change()
    got = reread(world, name)
    assert got["problems"] == ["code_changed"]
    assert reread(world, name, head=None)["intact"] is True, (
        "unknown head: not compared"
    )


def test_another_profile_in_the_site_file_is_a_problem(world):
    name = prepare(world)["name"]
    world.reparse_site(profile="main-297a74c3")
    assert "profile_changed" in reread(world, name)["problems"]


@pytest.mark.parametrize(
    "name", ["../x", "aeri-20261021-ext-r01/../r02", "x", "", None, 5]
)
def test_only_our_run_names_are_read(world, name):
    assert aeri_prep.read_prepared(world.aeri, name) is None


def test_a_missing_folder_or_a_link_is_not_a_prepared_run(world):
    assert reread(world, "aeri-20261021-ext-r42") is None
    (world.root / "elsewhere").mkdir()
    os.symlink(world.root / "elsewhere", world.runs / "aeri-20261021-ext-r43")
    assert reread(world, "aeri-20261021-ext-r43") is None


# --- what is written, and where ------------------------------------------------------------


def test_only_the_new_run_folder_and_the_pages_own_folder_change(world):
    before = tree(world.root)
    prepared = prepare(world)
    after = tree(world.root)
    changed = {k for k in after if after[k] != before.get(k)}
    allowed = (
        f"aeri-external/runs/{prepared['name']}",
        "state/marvel0",
        "seen.json",
    )
    stray = [k for k in changed if not k.startswith(allowed)]
    assert stray == [], stray
    assert set(before) <= set(after), "nothing was removed"


def test_the_pages_record_names_the_newest_run(world):
    first = prepare(world)
    value = aeri_prep.read_page_record(page_dir(world))
    assert value["name"] == first["name"] and value["mode"] == "external"
    assert value["files_sha256"] == first["files_sha256"]
    second = prepare(world, mode="local")
    assert aeri_prep.read_page_record(page_dir(world))["name"] == second["name"]


def test_a_foreign_or_broken_record_is_not_a_record(world):
    directory = page_dir(world)
    assert aeri_prep.read_page_record(directory) is None
    record.write_record(directory, {"schema": "other", "name": "aeri-20261021-ext-r01"})
    assert aeri_prep.read_page_record(directory) is None
    record.write_record(
        directory, {"schema": aeri_prep.RECORD_SCHEMA, "name": "../../x"}
    )
    assert aeri_prep.read_page_record(directory) is None
    (directory / "record.json").write_text("{broken")
    assert aeri_prep.read_page_record(directory) is None


def test_the_state_folder_is_private_and_not_inside_source_data(world):
    directory = page_dir(world)
    assert oct(directory.stat().st_mode & 0o777) == "0o700"
    assert (directory / "requests").is_dir()
    with pytest.raises(record.StateDirUnusable):
        aeri_prep.state_dir(world.aeri, aeri_prep.StateRoot(world.runs / "x"), True)


def test_the_state_folder_refuses_a_link(world):
    base = world.root / "state2"
    (base / "marvel0").mkdir(parents=True)
    os.symlink(world.root, base / "marvel0" / "aeri_prep")
    with pytest.raises(record.StateDirUnusable):
        aeri_prep.state_dir(world.aeri, aeri_prep.StateRoot(base), True)


def test_the_audit_line_has_no_path_and_no_output(world):
    directory = page_dir(world)
    aeri_prep.audit(
        directory,
        {"event": "prepare", "name": "aeri-20261021-ext-r01", "code": None},
        aeri_prep.time.time(),
    )
    line = json.loads((directory / "history.jsonl").read_text().splitlines()[0])
    assert line["actor"] == "person" and line["via"] == "product"
    assert "code" not in line and "/" not in json.dumps(line)


# --- git ---------------------------------------------------------------------------------


def test_git_state_of_a_clean_dirty_and_foreign_tree(world, tmp_path):
    assert aeri_prep.git_state(str(world.wt)) == {"head": world.head(), "dirty": False}
    world.dirty("untracked")
    assert aeri_prep.git_state(str(world.wt))["dirty"] is True
    assert aeri_prep.git_state(str(tmp_path / "nowhere")) == {
        "head": None,
        "dirty": None,
    }


def test_git_state_never_writes_into_the_tree(world):
    before = tree(world.wt)
    aeri_prep.git_state(str(world.wt))
    after = tree(world.wt)
    assert {k: v for k, v in after.items() if not k.startswith(".git")} == {
        k: v for k, v in before.items() if not k.startswith(".git")
    }


def test_git_state_without_a_git_binary_is_unknown(world):
    def no_git(argv, **_):
        raise FileNotFoundError("git")

    assert aeri_prep.git_state(str(world.wt), no_git) == {"head": None, "dirty": None}


# --- the table of refusals ---------------------------------------------------------------


def test_every_refusal_the_code_raises_is_in_the_table():
    used = set()
    for module in ("aeri_prep", "aeri_ready", "aeri_leftovers", "aeri_api"):
        source = (Path(aeri_prep.__file__).with_name(module + ".py")).read_text()
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "Refuse"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                used.add(node.args[0].value)
    assert used <= set(aeri_prep.REFUSALS), used - set(aeri_prep.REFUSALS)
    # and every refusal in the table has an HTTP status and a message
    for code, (status, text) in aeri_prep.REFUSALS.items():
        assert isinstance(status, int) and 400 <= status < 600 and text, code


def test_a_message_is_filled_and_never_fails_for_a_missing_fact():
    assert (
        "prepare aeri run"
        in Refuse("confirm_required", phrase="prepare aeri run").message
    )
    assert Refuse("confirm_required").message  # no phrase: the text stands as it is


def test_the_http_answer_has_the_error_shape():
    exc = aeri_prep.http(Refuse("form_invalid", field="task", reason="pattern"))
    assert exc.status_code == 422
    assert exc.detail == {
        "code": "form_invalid",
        "message": "The form is not valid",
        "field": "task",
        "reason": "pattern",
    }


# --- removing what a failed prepare made ---------------------------------------------------


def test_only_a_run_folder_of_ours_directly_under_the_runs_folder_is_removed(world):
    runs = world.runs
    ours = runs / "aeri-20261021-ext-r07"
    notes = runs / "notes"
    deep = runs / "sub" / "aeri-20261021-ext-r08"
    outside = world.root / "elsewhere" / "aeri-20261021-ext-r09"
    for folder in (ours, notes, deep, outside):
        folder.mkdir(parents=True)
        (folder / "keep.txt").write_text("x")
    link = runs / "aeri-20261021-ext-r10"
    os.symlink(notes, link)
    for folder in (notes, deep, outside, link):
        aeri_prep._remove_made(folder, runs)
        assert folder.exists() or folder.is_symlink(), folder
        assert (notes / "keep.txt").exists()
    aeri_prep._remove_made(ours, runs)
    assert not ours.exists()
    aeri_prep._remove_made(ours, runs)  # already gone: nothing to do
