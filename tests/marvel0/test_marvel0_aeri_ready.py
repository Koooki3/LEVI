"""The passive readiness of an AERI run and the card's document
(``levi/marvel0/aeri_ready.py``). Each row's decision is tested on plain facts;
the parsers of what ``nvidia-smi``, ``/proc/meminfo`` and ``systemctl`` print are
tested on text those programs really printed; the reader (cache, one read at a
time, the age of a cached read) with real threads; and the real readers once
against this machine, where they must give a well-formed answer whatever the
machine is."""

import json
import re
import shutil
import threading
import time
from pathlib import Path

import pytest
from aeri_world import DEFAULT_PROFILES, NOW, World, installed_profiles

from levi.marvel0 import (
    aeri_leftovers,
    aeri_prep,
    aeri_ready,
    control_site,
    net,
    status,
)
from levi.marvel0 import aeri_state as aeri_state_module
from levi.marvel0 import site as marvel0_site

# Real output of the programs (21 Oct 2026, this machine).
NVIDIA_SMI = "22, 32607\n"
SYSTEMCTL_SHOW_LOADED = "LoadState=loaded\nActiveState=inactive\nSubState=dead\n"
SYSTEMCTL_SHOW_RUNNING = "LoadState=loaded\nActiveState=active\nSubState=running\n"
SYSTEMCTL_SHOW_NOT_FOUND = "LoadState=not-found\nActiveState=inactive\nSubState=dead\n"
LIST_UNITS = (
    "levi-aeri-client-marvel0-20261021T192459-54e767.scope loaded active running "
    "FR3 evaluation client (terminal E), hosted by the marvel0 page\n"
    "levi-aeri-vlm-8100.service loaded active running AERI vision model\n"
    "levi-aeri-policy-forward.service loaded active running AERI policy\n"
    "levi-aeri-policy-manual-eggplant.scope loaded active running policy by hand\n"
    "● levi-aeri-policy-manual-marvel0-pi05.service loaded failed failed deploy\n"
)


@pytest.fixture
def world(tmp_path, monkeypatch):
    installed_profiles(monkeypatch, DEFAULT_PROFILES)
    w = World(tmp_path)
    w.facts["stack"] = World.stack_doc()
    w.set_live("stopped")
    return w


def facts_of(world, **replace):
    reader = aeri_ready.Readiness(world.host(**replace))
    return reader.facts(fresh=True)


def rows_of(world, **replace):
    return {r["code"]: r for r in aeri_ready.rows(facts_of(world, **replace))}


def levels(world, **replace):
    return {code: r["level"] for code, r in rows_of(world, **replace).items()}


def prepare(world):
    prepper = world.preparer()
    return prepper.prepare(world.prepare_body())


# --- the parsers, on what the programs print ---------------------------------------------


def test_nvidia_smi_output_is_used_and_summed():
    assert aeri_ready.parse_gpu(NVIDIA_SMI) == (22, 32607)
    assert aeri_ready.parse_gpu("100, 200\n300, 400\n") == (400, 600)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n",
        "No devices were found\n",
        "22 MiB, 32607 MiB\n",
        "22\n",
        "40000, 32607\n",
        "a, b\n",
    ],
)
def test_anything_else_from_nvidia_smi_is_unknown(text):
    assert aeri_ready.parse_gpu(text) is None


def test_meminfo_gives_available_mib():
    text = "MemTotal:       129450224 kB\nMemAvailable:   99259152 kB\n"
    assert aeri_ready.parse_meminfo(text) == 99259152 // 1024
    assert aeri_ready.parse_meminfo("MemTotal: 5 kB\n") is None
    assert aeri_ready.parse_meminfo("") is None
    assert aeri_ready.parse_meminfo(None) is None


def test_systemctl_show_is_read_by_property_name():
    assert aeri_ready.parse_show(SYSTEMCTL_SHOW_RUNNING) == {
        "load": "loaded",
        "active": "active",
        "sub": "running",
    }
    assert (
        aeri_ready.parse_show("MainPID=0\n" + SYSTEMCTL_SHOW_LOADED)["active"]
        == "inactive"
    )
    assert aeri_ready.parse_show("LoadState=loaded\n") is None
    assert aeri_ready.parse_show("") is None


@pytest.mark.parametrize(
    "text, state",
    [
        (SYSTEMCTL_SHOW_RUNNING, "running"),
        (SYSTEMCTL_SHOW_LOADED, "stopped"),
        (SYSTEMCTL_SHOW_NOT_FOUND, "absent"),
        ("LoadState=loaded\nActiveState=activating\nSubState=start\n", "running"),
        ("LoadState=loaded\nActiveState=deactivating\nSubState=stop\n", "running"),
        ("LoadState=loaded\nActiveState=failed\nSubState=failed\n", "stopped"),
        ("LoadState=loaded\nActiveState=maintenance\nSubState=x\n", None),
    ],
)
def test_the_live_service_state(text, state):
    assert aeri_ready.live_state(aeri_ready.parse_show(text)) == state


def test_a_unit_that_cannot_be_shown_is_unknown():
    assert aeri_ready.live_state(None) is None


def test_the_unit_list_is_sorted_into_leftovers_and_others():
    items = status.parse_units(LIST_UNITS)
    assert {i["name"] for i in items} >= {"levi-aeri-vlm-8100.service"}
    left = aeri_leftovers.leftover_units(items)
    assert {u["name"]: u["kind"] for u in left} == {
        "levi-aeri-vlm-8100.service": "vlm",
        "levi-aeri-policy-forward.service": "policy",
    }
    every = aeri_leftovers.any_aeri_unit(items)
    assert "levi-aeri-policy-manual-eggplant.scope" in every, "it still blocks a run"
    assert "levi-aeri-policy-manual-marvel0-pi05.service" in every
    assert not [n for n in every if "client" in n], (
        "the evaluation client is not a leftover"
    )


# --- the rows ----------------------------------------------------------------------------


def test_a_ready_machine_has_every_row_ok_and_the_stack_a_warning(world):
    got = levels(world)
    assert list(got) == list(aeri_ready.ROW_CODES)
    assert {c: v for c, v in got.items() if v != "ok"} == {
        "prepared": "refuse",
        "stack_ready": "warn",
    }
    prepare(world)
    got = levels(world)
    assert {c: v for c, v in got.items() if v != "ok"} == {"stack_ready": "warn"}
    assert aeri_ready.is_ready(list(rows_of(world).values()))


def test_unknown_refuses_like_refuse(world):
    prepare(world)
    for fact, code in (
        ("gpu", "gpu_free"),
        ("product", "product"),
    ):
        world.facts[fact] = None
        assert levels(world)[code] == "unknown", code
        assert not aeri_ready.is_ready(list(rows_of(world).values()))
        world.facts[fact] = {"gpu": (1000, 32607), "product": True}[fact]
    assert aeri_ready.is_ready(list(rows_of(world).values()))


def test_a_card_without_rows_is_not_ready():
    assert aeri_ready.is_ready([]) is False
    assert aeri_ready.is_ready([{"level": "ok"}, {"level": "warn"}]) is True
    assert aeri_ready.is_ready([{"level": "ok"}, {"level": "unknown"}]) is False
    assert aeri_ready.is_ready([{"level": "refuse"}]) is False


def test_the_switch_row_follows_the_control_file(world):
    # Off: a light read, no rows at all (the document is the switch; see the
    # cost tests below). On: the row is there and passes.
    world.facts["control"]["enabled"] = False
    assert levels(world) == {}
    world.facts["control"]["enabled"] = True
    assert levels(world)["aeri_enabled"] == "ok"


def test_the_product_row(world):
    world.facts["product"] = False
    assert levels(world)["product"] == "refuse"


def test_the_code_pin_row_reads_git_for_real(world):
    assert levels(world)["code_pin"] == "ok"
    world.dirty("untracked")
    row = rows_of(world)["code_pin"]
    assert row["level"] == "refuse" and row["params"]["dirty"] is True
    (world.wt / "untracked.txt").unlink()
    world.commit_change()
    row = rows_of(world)["code_pin"]
    assert row["level"] == "refuse" and row["params"]["head"] != row["params"]["pin"]


def test_the_code_pin_row_is_unknown_when_git_is_not_readable(world):
    def broken(argv, **_):
        raise OSError("no git")

    prep = aeri_prep.Host(now=lambda: NOW, run=broken, registry=lambda: world.registry)
    row = rows_of(world, prep=prep)["code_pin"]
    assert row["level"] == "unknown" and row["params"]["head"] is None


def test_the_profile_row_follows_the_table(world, monkeypatch):
    assert levels(world)["profile_known"] == "ok"
    installed_profiles(monkeypatch, {"bfdee2cf": False})
    assert levels(world)["profile_known"] == "refuse"
    module = installed_profiles(monkeypatch, DEFAULT_PROFILES)
    module.PROFILES = None
    assert levels(world)["profile_known"] == "unknown"


def test_the_prompts_row_follows_the_comparison_of_the_source_files(world):
    assert levels(world)["prompts_checked"] == "ok"
    world.facts["matches"] = (False, "differs:levi/automatic/console.py")
    row = rows_of(world)["prompts_checked"]
    assert row["level"] == "refuse"
    assert row["params"] == {"reason": "differs", "file": "levi/automatic/console.py"}
    world.facts["matches"] = (False, "unknown_profile")
    row = rows_of(world)["prompts_checked"]
    assert (row["level"], row["params"]["reason"]) == ("refuse", "unknown_profile")
    world.facts["matches"] = (False, "unreadable:levi/automatic/fr3_run.py")
    row = rows_of(world)["prompts_checked"]
    assert (row["level"], row["params"]["reason"]) == ("unknown", "unreadable")
    for nothing in (None, "ok", (True,), ()):
        world.facts["matches"] = nothing
        row = rows_of(world)["prompts_checked"]
        assert (row["level"], row["params"]["reason"]) == ("unknown", "unavailable")


def test_a_comparison_that_raises_is_unknown(world):
    def boom(worktree, profile):
        raise RuntimeError("boom")

    row = rows_of(world, profile_matches=boom)["prompts_checked"]
    assert (row["level"], row["params"]["reason"]) == ("unknown", "unavailable")


REAL_PROFILES = aeri_state_module.PROFILES

REAL_FILES = (
    "levi/automatic/console.py",
    "levi/automatic/console_text.py",
    "levi/automatic/fr3_run.py",
    "levi/automatic/subtask_units.py",
)


def test_the_real_comparison_on_a_worktree_with_the_real_files(world, monkeypatch):
    """``aeri_state.profile_matches`` itself, not a stand-in: a worktree holding
    this checkout's four AERI source files matches main's profile as long as they
    are the ones it was written from, and a changed byte is named."""
    from levi.marvel0 import aeri_state

    monkeypatch.setattr(aeri_state, "PROFILES", REAL_PROFILES)
    root = world.root / "real-files"
    for rel in REAL_FILES:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(aeri_state.__file__).parents[2] / rel, target)
    profile = "main-297a74c3"
    ok, detail = aeri_state.profile_matches(root, profile)
    world.reparse_site(profile=profile, worktree=str(root))
    host = world.host(profile_matches=aeri_state.profile_matches)
    row = {r["code"]: r for r in aeri_ready.rows(aeri_ready.collect(host))}[
        "prompts_checked"
    ]
    assert (row["level"] == "ok") is ok, detail
    (root / REAL_FILES[0]).write_text("# edited\n")
    row = {r["code"]: r for r in aeri_ready.rows(aeri_ready.collect(host))}[
        "prompts_checked"
    ]
    assert row["level"] == "refuse"
    assert row["params"] == {"reason": "differs", "file": REAL_FILES[0]}
    (root / REAL_FILES[0]).unlink()
    row = {r["code"]: r for r in aeri_ready.rows(aeri_ready.collect(host))}[
        "prompts_checked"
    ]
    assert (row["level"], row["params"]["reason"]) == ("unknown", "unreadable")


def test_the_templates_row_names_the_file_that_differs(world):
    path = world.templates / "binding.base.json"
    path.write_text(path.read_text() + " ")
    row = rows_of(world)["templates_sha"]
    assert row["level"] == "refuse" and row["params"]["which"] == ["binding.base.json"]
    (world.templates / aeri_prep.SOURCES_FILE).unlink()
    assert rows_of(world)["templates_sha"]["level"] == "unknown"


def test_the_prepared_row_says_why_not(world):
    row = rows_of(world)["prepared"]
    assert row["level"] == "refuse" and row["params"] == {
        "name": None,
        "reason": "none",
    }
    answer = prepare(world)
    name = answer["name"]
    assert rows_of(world)["prepared"]["level"] == "ok"
    (world.runs / name / "job.yaml").write_text("changed")
    row = rows_of(world)["prepared"]
    assert row["level"] == "refuse" and row["params"]["reason"] == "files_changed"


def test_the_name_row_ignores_the_runs_own_folder_and_sees_a_run_that_started(world):
    name = prepare(world)["name"]
    assert rows_of(world)["run_name_free"]["level"] == "ok"
    (world.rollout / ".aeri" / "runs" / name).mkdir()
    row = rows_of(world)["run_name_free"]
    assert row["level"] == "refuse" and row["params"]["where"] == ["rollout"]
    (world.rollout / ".aeri" / "runs" / name).rmdir()
    (world.registry / f"{name}.json").write_text("{}")
    assert rows_of(world)["run_name_free"]["params"]["where"] == ["registry"]


def test_the_cameras_row_compares_the_binding_with_the_site_file(world):
    assert levels(world)["cameras_config"] == "ok"
    world.facts["control"]["enabled"] = True
    world.site = marvel0_site.parse(
        {
            **world.site_dict(),
            "cameras": {"view": "111111111111", "wrist": "262622275490"},
        },
        "env",
    )
    row = rows_of(world)["cameras_config"]
    assert row["level"] == "refuse" and row["params"]["fields"] == ["side"]
    prepared = prepare(world)
    binding = world.runs / prepared["name"] / "binding.json"
    value = json.loads(binding.read_text())
    value["cameras"]["side"] = "111111111111"
    binding.write_text(json.dumps(value))
    assert rows_of(world)["cameras_config"]["level"] == "ok", (
        "the prepared binding is read"
    )


def test_the_gpu_row_follows_the_mode_of_the_prepared_run(world):
    world.facts["gpu"] = (22000, 32607)  # 10.0 GiB free
    row = rows_of(world)["gpu_free"]
    assert (row["level"], row["params"]["mode"]) == ("ok", "external")
    assert row["params"]["free_mib"] == 10607 and row["params"]["need_mib"] == 9216
    answer = world.preparer().prepare(world.prepare_body(mode="local"))
    assert answer["prepared"]["mode"] == "local"
    row = rows_of(world)["gpu_free"]
    assert (row["level"], row["params"]["mode"]) == ("refuse", "local")
    assert row["params"]["need_mib"] == 19 * 1024


@pytest.mark.parametrize("free, level", [(9216, "ok"), (9215, "refuse")])
def test_the_gpu_row_at_the_threshold(world, free, level):
    world.facts["gpu"] = (32607 - free, 32607)
    assert levels(world)["gpu_free"] == level


@pytest.mark.parametrize("mib, level", [(28 * 1024, "ok"), (28 * 1024 - 1, "refuse")])
def test_the_memory_row_at_the_threshold(world, mib, level):
    world.facts["meminfo"] = f"MemAvailable: {mib * 1024} kB\n"
    assert levels(world)["mem_available"] == level
    world.facts["meminfo"] = "garbage"
    assert levels(world)["mem_available"] == "unknown"


def test_the_lock_rows(world):
    world.facts["flock"] = {".gpu.lock": [123]}
    row = rows_of(world)["gpu_lock_free"]
    assert row["level"] == "refuse" and row["params"]["holders"] == 1
    world.facts["flock"] = {"levi-aeri-fr3-arm.lock": [4]}
    assert levels(world)["arm_lock_free"] == "refuse"
    assert levels(world)["gpu_lock_free"] == "ok"
    world.facts["flock"] = {".gpu.lock": None, "levi-aeri-fr3-arm.lock": None}
    got = levels(world)
    assert got["gpu_lock_free"] == "unknown" and got["arm_lock_free"] == "unknown"


def test_the_ports_row_lists_the_busy_ones(world):
    world.listen(8100, 8000, 8123)
    row = rows_of(world)["ports_free"]
    assert row["level"] == "refuse" and row["params"]["ports"] == [8000, 8100]
    world.listen(8110)
    row = rows_of(world)["ports_free"]
    assert row["level"] == "refuse" and row["params"]["ports"] == [8110]
    world.listen(8123, 5000)  # not ports AERI uses (and never connected to)
    assert levels(world)["ports_free"] == "ok"
    world.listen()
    assert levels(world)["ports_free"] == "ok"


def test_a_socket_table_that_cannot_be_read_is_not_taken_for_free_ports(world):
    row = rows_of(world, net_table=lambda: net.Table(False, (), "no /proc/net"))[
        "ports_free"
    ]
    assert row["level"] == "unknown"


def test_the_live_service_row_and_block(world):
    assert levels(world)["live_service"] == "ok"
    world.set_live("running")
    assert levels(world)["live_service"] == "refuse"
    world.set_live("absent")
    assert levels(world)["live_service"] == "ok"


def test_the_evaluation_client_row(world):
    world.facts["hosted_e"] = True
    assert levels(world)["no_e_run"] == "refuse"
    world.facts["hosted_e"] = False
    world.facts["sessions"] = {
        "state": "ok",
        "sessions": [{"status": "running", "age_s": 5}],
    }
    assert levels(world)["no_e_run"] == "refuse", "a terminal client counts too"
    world.facts["sessions"] = {
        "state": "ok",
        "sessions": [
            {"status": "finished", "age_s": 5},
            {"status": "running", "age_s": 600},
        ],
    }
    assert levels(world)["no_e_run"] == "ok"
    world.facts["sessions"] = {"state": "unknown", "sessions": []}
    assert levels(world)["no_e_run"] == "unknown"
    world.facts["sessions"] = {"state": "ok", "sessions": []}
    world.facts["hosted_e"] = None
    assert levels(world)["no_e_run"] == "unknown"


@pytest.mark.parametrize(
    "state, level",
    [
        ("idle", "ok"),
        ("failed", "ok"),
        ("dead", "ok"),
        ("ready", "refuse"),
        ("starting", "refuse"),
        ("unknown", "unknown"),
    ],
)
def test_the_policy_deploy_row(world, state, level):
    world.facts["deploy"] = {"state": state}
    assert levels(world)["no_policy_deploy"] == level
    world.facts["deploy"] = None
    assert levels(world)["no_policy_deploy"] == "unknown"


def test_the_leftover_unit_row_names_units_and_the_vram(world):
    world.facts["units"] = status.parse_units(LIST_UNITS)
    row = rows_of(world)["no_aeri_unit_left"]
    assert row["level"] == "refuse"
    assert "levi-aeri-vlm-8100.service" in row["params"]["units"]
    assert row["params"]["vram_mib"] == 1000
    world.facts["units"] = []
    assert levels(world)["no_aeri_unit_left"] == "ok"
    world.facts["units"] = None
    assert levels(world)["no_aeri_unit_left"] == "unknown"


def test_the_swap_log_row_reads_the_real_logs(world):
    from levi.automatic import fr3_policy_swap

    assert levels(world)["swap_logs_clean"] == "ok"
    path = fr3_policy_swap.log_path(world.rollout, "aeri-20261021-ext-r00")
    log = fr3_policy_swap.SwapLog.create(
        path,
        run_id="aeri-20261021-ext-r00",
        units={"forward": "levi-aeri-policy-forward"},
    )
    assert levels(world)["swap_logs_clean"] == "ok", "a header alone is clean"
    log.append({"record": "prepared", "txn": "t1", "service_state": "forward"})
    row = rows_of(world)["swap_logs_clean"]
    assert row["level"] == "refuse" and row["params"]["count"] == 1
    assert "unfinished" in row["text"]
    log.append({"record": "acknowledged", "by_run": "aeri-20261021-ext-r01"})
    assert levels(world)["swap_logs_clean"] == "ok", "an acknowledged log is clean"


def test_the_swap_row_is_unknown_when_the_reader_fails(world):
    def failing(root):
        raise OSError("unreadable")

    assert levels(world, swap_pending=failing)["swap_logs_clean"] == "unknown"


def test_the_other_aeri_run_row(world):
    world.facts["aeri_runs"] = [{"run_id": "x", "runner_alive": False}]
    assert levels(world)["no_other_aeri"] == "ok"
    world.facts["aeri_runs"] = [{"run_id": "x", "runner_alive": True}]
    assert levels(world)["no_other_aeri"] == "refuse"
    world.facts["aeri_runs"] = []
    world.facts["hosted_d"] = True
    assert levels(world)["no_other_aeri"] == "refuse"
    world.facts["hosted_d"] = None
    assert levels(world)["no_other_aeri"] == "unknown"


@pytest.mark.parametrize(
    "health, level",
    [
        ("ok", "ok"),
        ("red", "refuse"),
        ("offline", "refuse"),
        ("missing", "refuse"),
        ("unknown", "unknown"),
    ],
)
def test_the_monitor_row(world, health, level):
    world.facts["stack"] = World.stack_doc(health=health)
    assert levels(world)["monitor_green"] == level


@pytest.mark.parametrize(
    "stack, level",
    [
        (World.stack_doc(), "warn"),
        (World.stack_doc(a="stopped"), "refuse"),
        (World.stack_doc(c="stopped"), "refuse"),
        (World.stack_doc(a="unknown"), "unknown"),
        (World.stack_doc(c="unknown"), "unknown"),
        (World.stack_doc(b="stopped", m="stopped"), "warn"),
        ({"terminals": "x", "health": {}}, "unknown"),
        (None, "unknown"),
    ],
)
def test_the_stack_row_wants_the_arm_and_the_robot_server_up(world, stack, level):
    world.facts["stack"] = stack
    assert levels(world)["stack_ready"] == level


def test_a_source_that_raises_makes_its_row_unknown_not_the_request_fail(world):
    def boom(*a, **k):
        raise RuntimeError("boom")

    got = levels(
        world,
        gpu=boom,
        meminfo=boom,
        net_table=boom,
        flock=boom,
        unit_show=boom,
        units=boom,
        aeri_runs=boom,
        stack_view=boom,
        deploy_view=boom,
        hosted_e=boom,
        hosted_d=boom,
        sessions=boom,
        product=boom,
    )
    unknown = {c for c, v in got.items() if v == "unknown"}
    assert unknown == {
        "product",
        "gpu_free",
        "gpu_lock_free",
        "mem_available",
        "ports_free",
        "live_service",
        "arm_lock_free",
        "no_e_run",
        "no_policy_deploy",
        "no_aeri_unit_left",
        "no_other_aeri",
        "monitor_green",
        "stack_ready",
    }


def test_every_row_has_its_three_texts_or_a_fallback():
    for code in aeri_ready.ROW_CODES:
        assert code in aeri_ready.CHECK_OK, code
        assert code in aeri_ready.CHECK_REFUSE, code


def test_row_texts_are_filled_from_the_params():
    r = aeri_ready.row("gpu_free", False, free_mib=100, need_mib=9216, mode="external")
    assert (
        "100" in r["text"] and "9216" in r["text"] and r["params"]["mode"] == "external"
    )
    assert aeri_ready.row("gpu_free", False)["text"], (
        "a missing param leaves the text whole"
    )
    assert aeri_ready.row("stack_ready", True, warn=True, cards={})["level"] == "warn"
    assert (
        "warn" not in aeri_ready.row("stack_ready", True, warn=True, cards={})["params"]
    )


# --- the document ------------------------------------------------------------------------


def document(world, **replace):
    return aeri_ready.document(facts_of(world, **replace))


def test_the_document_of_a_prepared_ready_machine(world):
    answer = prepare(world)
    doc = document(world)
    assert doc["schema"] == aeri_ready.SCHEMA
    assert doc["enabled"] is True and doc["ready"] is True
    assert (
        doc["prepared"]["name"] == answer["name"] and doc["prepared"]["intact"] is True
    )
    assert doc["profile"] == {
        "id": "harvest-5bc750ca",
        "known": True,
        "start_home": True,
        "console_lang": "zh",
        "pin_sha": world.head()[:8],
        "head": world.head()[:8],
        "clean": True,
    }
    assert [p["id"] for p in doc["profiles"]] == sorted(DEFAULT_PROFILES)
    assert {p["id"] for p in doc["profiles"] if p["default"]} == {"harvest-5bc750ca"}
    assert doc["limits"]["modes"] == ["external", "local"]
    assert doc["limits"]["steps"]["max"] == 3000
    assert doc["confirm"] == {
        "prepare": "prepare aeri run",
        "live_stop": "stop live service",
        "leftovers": "stop leftover aeri units",
    }
    assert doc["notices"][0]["code"] == "gates_scope"
    assert doc["site"] == {
        "state": "ok",
        "code": "aeri_site.ok",
        "errors": [],
        "template": None,
    }
    assert doc["control"] == {"state": "ok", "code": "aeri.enabled", "errors": []}
    assert doc["sampled_at"].startswith("2026-10-21T")


def test_the_document_never_names_a_path_except_the_command(world):
    prepare(world)
    world.facts["units"] = status.parse_units(LIST_UNITS)
    doc = document(world)
    text = json.dumps({**doc, "prepared": {**doc["prepared"], "execute": ""}})
    assert str(world.root) not in text and "/home/" not in text
    assert doc["prepared"]["execute"].startswith("cd ")


def test_the_document_closed_key_sets(world):
    doc = document(world)
    assert set(doc) == {
        "schema",
        "sampled_at",
        "enabled",
        "control",
        "site",
        "profile",
        "profiles",
        "limits",
        "readiness",
        "ready",
        "readiness_sha256",
        "prepared",
        "live_service",
        "leftovers",
        "confirm",
        "notices",
    }
    assert set(doc["live_service"]) == {
        "state",
        "port",
        "listener",
        "can_stop",
        "why_not",
    }
    assert set(doc["leftovers"]) == {"state", "units", "swap", "can_stop", "why_not"}
    assert set(doc["profile"]) == {
        "id",
        "known",
        "start_home",
        "console_lang",
        "pin_sha",
        "head",
        "clean",
    }
    assert set(doc["limits"]) == {
        "modes",
        "rounds",
        "steps",
        "gripper_confirm_s",
        "task",
    }
    for row in doc["readiness"]:
        assert set(row) == {"code", "level", "text", "params"}
        assert row["level"] in ("ok", "warn", "refuse", "unknown")
    prepare(world)
    assert set(document(world)["prepared"]) == {
        "name",
        "mode",
        "task",
        "rounds",
        "steps",
        "gripper_confirm_s",
        "start_home",
        "profile",
        "aeri_head",
        "prepared_at",
        "plan_sha256",
        "binding_sha256",
        "files_sha256",
        "intact",
        "problems",
        "execute",
        "confirm",
    }


def test_an_unconfigured_card_has_no_rows_and_a_template(world):
    world.site = marvel0_site.parse(
        {k: v for k, v in world.site_dict().items() if k != "aeri"}, "env"
    )
    doc = document(world)
    assert (
        doc["site"]["state"] == "unconfigured"
        and doc["site"]["code"] == "aeri_site.no_section"
    )
    assert doc["site"]["template"] == marvel0_site.AERI_TEMPLATE
    assert doc["readiness"] == [] and doc["ready"] is False
    assert doc["profile"] is None and doc["limits"] is None and doc["prepared"] is None
    assert doc["live_service"] is None and doc["leftovers"] is None
    assert doc["profiles"] == []


def test_a_malformed_section_is_invalid_with_the_errors_and_no_rows(world):
    world.site = marvel0_site.parse(world.site_dict(pin_sha="zz"), "env")
    doc = document(world)
    assert doc["site"]["state"] == "invalid"
    assert {"field": "aeri.pin_sha", "code": "range"} in doc["site"]["errors"]
    assert doc["readiness"] == [] and doc["ready"] is False
    assert doc["site"]["template"] is None


def test_a_missing_site_file_is_unconfigured_and_an_invalid_one_invalid(world):
    missing = marvel0_site.SiteLoad("missing", None, marvel0_site.Path("x"), "env", [])
    doc = document(world, site=lambda: missing)
    assert (
        doc["site"]["state"] == "unconfigured"
        and doc["site"]["code"] == "aeri_site.missing_file"
    )
    invalid = marvel0_site.SiteLoad(
        "invalid",
        None,
        marvel0_site.Path("x"),
        "env",
        [{"field": "caps.x", "code": "range"}],
    )
    doc = document(world, site=lambda: invalid)
    assert doc["site"]["state"] == "invalid"
    assert doc["site"]["errors"] == [{"field": "caps.x", "code": "range"}]


def test_a_site_loader_that_raises_is_invalid_not_an_error(world):
    def boom():
        raise RuntimeError("boom")

    doc = document(world, site=boom)
    assert doc["site"]["state"] == "invalid" and doc["readiness"] == []


def test_the_control_file_states(world):
    world.facts["control"]["enabled"] = False
    doc = document(world)
    assert doc["enabled"] is False and doc["control"]["code"] == "aeri.disabled"
    assert doc["ready"] is False and doc["readiness"] == []
    assert doc["live_service"] is None and doc["leftovers"] is None
    assert doc["prepared"] is None
    assert doc["site"]["state"] == "ok" and doc["profile"]["id"] == world.profile
    world.facts["control"]["enabled"] = None
    doc = document(world)
    assert doc["control"]["code"] == "aeri.missing" and doc["enabled"] is False

    def boom():
        raise RuntimeError("boom")

    doc = document(world, control=boom)
    assert doc["control"]["code"] == "aeri.invalid" and doc["enabled"] is False


def test_the_live_block_offers_the_stop_only_when_it_is_safe(world):
    world.set_live("running")
    doc = document(world)
    assert (
        doc["live_service"]["can_stop"] is True
        and doc["live_service"]["why_not"] is None
    )
    assert doc["live_service"]["state"] == "running"
    cases = [
        ("aeri_runs", [{"runner_alive": True}], "d_active"),
        (
            "sessions",
            {
                "state": "ok",
                "sessions": [{"status": "running", "age_s": 1}],
            },
            "e_active",
        ),
    ]
    for fact, value, code in cases:
        before = world.facts[fact]
        world.facts[fact] = value
        block = document(world)["live_service"]
        assert block["can_stop"] is False and block["why_not"]["code"] == code
        world.facts[fact] = before
    world.facts["flock"] = {"levi-aeri-fr3-arm.lock": [9]}
    assert document(world)["live_service"]["why_not"]["code"] == "arm_in_use"
    world.facts["flock"] = {}
    world.facts["product"] = False
    assert document(world)["live_service"]["why_not"]["code"] == "not_product"
    world.facts["product"] = True
    world.set_live("stopped")
    assert document(world)["live_service"]["why_not"]["code"] == "not_running"


def test_the_live_stop_does_not_need_the_gpu_lock_free(world):
    world.set_live("running")
    world.facts["flock"] = {".gpu.lock": [9]}
    assert document(world)["live_service"]["can_stop"] is True, (
        "the live service holds it"
    )


def test_an_unreadable_obstacle_withholds_the_stop(world):
    world.set_live("running")
    world.facts["flock"] = {"levi-aeri-fr3-arm.lock": None}
    block = document(world)["live_service"]
    assert block["can_stop"] is False and block["why_not"]["code"] == "unreadable"


def test_the_leftover_block(world):
    world.facts["units"] = status.parse_units(LIST_UNITS)
    block = document(world)["leftovers"]
    assert block["state"] == "present" and block["can_stop"] is True
    assert [u["name"] for u in block["units"]] == [
        "levi-aeri-policy-forward.service",
        "levi-aeri-vlm-8100.service",
    ] or {u["name"] for u in block["units"]} == {
        "levi-aeri-policy-forward.service",
        "levi-aeri-vlm-8100.service",
    }
    world.facts["flock"] = {".gpu.lock": [4]}
    block = document(world)["leftovers"]
    assert block["can_stop"] is False and block["why_not"]["code"] == "gpu_in_use"
    world.facts["flock"] = {}
    world.facts["units"] = []
    block = document(world)["leftovers"]
    assert block["state"] == "none" and block["why_not"]["code"] == "nothing_to_stop"
    world.facts["units"] = None
    block = document(world)["leftovers"]
    assert block["state"] == "unknown" and block["why_not"]["code"] == "unreadable"


def test_the_swap_part_of_the_leftover_block(world):
    block = document(world)["leftovers"]
    assert block["swap"] == {"state": "clean", "count": 0}


# --- the digest a challenge and a start must agree on ------------------------------------


def snap(world, **replace):
    return aeri_ready.Readiness(world.host(**replace)).snapshot()


def test_the_digest_ignores_numbers_and_follows_levels(world):
    prepare(world)
    first = snap(world)["sha256"]
    world.facts["gpu"] = (3000, 32607)  # still ok: the number drifted
    assert snap(world)["sha256"] == first
    world.facts["gpu"] = (30000, 32607)  # gpu_free refuses now
    assert snap(world)["sha256"] != first
    world.facts["gpu"] = (1000, 32607)
    assert snap(world)["sha256"] == first


def test_another_prepared_run_with_the_same_levels_is_another_digest(world):
    prepare(world)
    first = snap(world)["sha256"]
    world.preparer().prepare(world.prepare_body(request_id="req-prepare-0002"))
    second = snap(world)
    assert second["prepared"]["name"] == "aeri-20261021-ext-r02"
    assert second["ready"] is True
    assert second["sha256"] != first, (
        "the run a start would use is part of what was agreed"
    )


def test_the_digest_follows_the_profile_the_head_and_the_prepared_run(world):
    answer = prepare(world)
    first = snap(world)["sha256"]
    (world.runs / answer["name"] / "job.yaml").write_text("changed")
    assert snap(world)["sha256"] != first, (
        "a changed file changes a level and the digests"
    )
    world.commit_change()
    assert snap(world)["sha256"] != first


def test_the_snapshot_has_the_shape_the_hosted_run_reads(world):
    prepare(world)
    got = snap(world)
    assert set(got) == {"rows", "ready", "sha256", "sampled_at", "prepared"}
    assert got["ready"] is True and got["prepared"]["intact"] is True
    assert all(set(r) == {"code", "level", "params"} for r in got["rows"])
    assert [r["code"] for r in got["rows"]] == list(aeri_ready.ROW_CODES)
    assert re.fullmatch(r"[0-9a-f]{64}", got["sha256"])


def test_the_module_function_reads_once_and_keeps_nothing(world):
    got = aeri_ready.snapshot(world.host())
    assert got["rows"] and aeri_ready.snapshot(world.host())["sha256"] == got["sha256"]


# --- the reader --------------------------------------------------------------------------


def test_a_second_view_within_the_cache_time_does_not_read_again(world):
    reader = aeri_ready.Readiness(world.host())
    reader.view()
    reader.view()
    assert world.reads["gpu"] == 1
    world.advance(aeri_ready.CACHE_S - 0.1)
    reader.view()
    assert world.reads["gpu"] == 1
    world.advance(0.2)
    reader.view()
    assert world.reads["gpu"] == 2


def test_a_fresh_snapshot_never_answers_from_the_cache(world):
    reader = aeri_ready.Readiness(world.host())
    reader.view()
    reader.snapshot()
    reader.snapshot()
    assert world.reads["gpu"] == 3


def test_a_slow_read_is_not_kept_longer_than_a_quick_one(world):
    """The age of a cached read counts from when it began: a read that took
    almost the whole cache time is that old the moment it is stored."""
    slow = world.host()
    real_gpu = slow.gpu

    def gpu():
        world.advance(aeri_ready.CACHE_S - 0.1)  # the read takes that long
        return real_gpu()

    slow.gpu = gpu
    reader = aeri_ready.Readiness(slow)
    reader.view()
    assert world.reads["gpu"] == 1
    world.advance(0.2)  # CACHE_S + 0.1 after it began, 0.2 after it ended
    reader.view()
    assert world.reads["gpu"] == 2, "the read had already used its cache time"


def test_a_clock_that_goes_backwards_never_serves_a_cache(world):
    reader = aeri_ready.Readiness(world.host())
    reader.view()
    world.advance(-5)
    reader.view()
    assert world.reads["gpu"] == 2


def test_readers_arriving_together_share_one_read(world):
    gate = threading.Event()
    host = world.host()
    real_gpu = host.gpu

    def gpu():
        gate.wait(5)
        return real_gpu()

    host.gpu = gpu
    reader = aeri_ready.Readiness(host)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(reader.view())) for _ in range(4)
    ]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(10)
    assert len(results) == 4 and world.reads["gpu"] == 1
    assert len({json.dumps(r, sort_keys=True) for r in results}) == 1


def test_forgetting_makes_the_next_view_read_again(world):
    reader = aeri_ready.Readiness(world.host())
    reader.view()
    reader.forget()
    reader.view()
    assert world.reads["gpu"] == 2


# --- the real readers, on this machine -----------------------------------------------------


def test_the_real_gpu_reader_gives_numbers_or_nothing():
    got = aeri_ready.read_gpu()
    assert got is None or (isinstance(got[0], int) and 0 <= got[0] <= got[1])


def test_the_real_memory_reader_reads_this_machine():
    text = aeri_ready.read_meminfo()
    if text is None:
        pytest.skip("no /proc/meminfo")
    assert aeri_ready.parse_meminfo(text) > 0


def test_the_real_unit_reader_sees_a_unit_that_does_not_exist():
    got = aeri_ready.show_unit("levi-a2-test-no-such-unit.service")
    assert got is None or aeri_ready.live_state(got) == "absent"


def test_the_real_unit_list_has_the_shape_the_card_reads():
    part = status.units_part()
    assert part["state"] in ("ok", "unknown")
    if part["state"] == "ok":
        for item in part["items"]:
            assert set(item) == {"name", "active", "sub"}
        assert isinstance(aeri_leftovers.leftover_units(part["items"]), list)


def test_the_real_lock_and_socket_readers():
    from levi.live import gpumgr

    got = gpumgr.flock_holders(marvel0_site.ARM_LOCK_FILE)
    assert got is None or isinstance(got, list)
    table = net.read()
    assert isinstance(table.ok, bool)


def test_the_real_host_collects_without_raising_and_with_every_row():
    """The production seams (the real stack, deploy and run supervisors,
    ``launch.runs()``) all answer or fail into ``unknown``; nothing raises. The
    card is unconfigured here (no site file in this workspace), so the facts
    are the short ones."""
    facts = aeri_ready.collect(aeri_ready.production_host())
    doc = aeri_ready.document(facts)
    assert doc["schema"] == aeri_ready.SCHEMA
    assert doc["ready"] is False


def test_the_control_view_inside_the_document_is_the_control_files(world):
    ctl = control_site.aeri_view(
        control_site.ControlLoad(
            "ok",
            control_site.ControlSite(
                control_site.StackControl(),
                control_site.ClientControl(),
                aeri=control_site.AeriControl(enabled=True),
            ),
        )
    )
    assert document(world)["control"] == {
        k: ctl[k] for k in ("state", "code", "errors")
    }


# --- what a read costs ---------------------------------------------------------------------
#
# A full read starts processes (git three times, nvidia-smi, systemctl twice, the
# stack and deploy views) and the page asks every three seconds, from every tab,
# while the arm controller runs. The light reads start none.


SOURCES = (
    "gpu",
    "meminfo",
    "net_table",
    "flock",
    "unit_show",
    "units",
    "aeri_runs",
    "swap_pending",
    "stack_view",
    "deploy_view",
    "sessions",
    "profile_matches",
    "state_root",
)


def counting(world, **replace):
    """A host whose every source (and the process runner of git and the stand-in
    program) counts its calls and answers as the world does; ``calls`` is
    ``{name: count}``."""
    calls: dict = {}
    base = world.host(**replace)

    def watch(name, real):
        def look(*args, **kwargs):
            calls[name] = calls.get(name, 0) + 1
            return real(*args, **kwargs)

        return look

    for name in SOURCES:
        setattr(base, name, watch(name, getattr(base, name)))
    base.prep.run = watch("run", base.prep.run)
    base.prep.registry = watch("registry", base.prep.registry)
    return base, calls


def test_the_cache_time_is_well_under_the_pages_poll():
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "components"
        / "marvel0"
        / "autonomy-logic.ts"
    ).read_text()
    poll_ms = int(re.search(r"readyPollMs: ([\d_]+)", source).group(1).replace("_", ""))
    assert aeri_ready.CACHE_S * 1000 * 2 <= poll_ms, (
        "a poll must see what is now, not what the last poll saw"
    )


def test_a_switched_off_card_reads_nothing_but_the_switch_and_the_site(world):
    world.facts["control"]["enabled"] = False
    host, calls = counting(world)
    doc = aeri_ready.Readiness(host).view()
    assert calls == {}, "no process, no folder, no source"
    assert doc["enabled"] is False and doc["ready"] is False
    assert doc["readiness"] == [] and doc["site"]["state"] == "ok"
    assert doc["profile"]["id"] == world.profile and doc["profile"]["known"] is True
    assert doc["profile"]["head"] is None and doc["profile"]["clean"] is None
    assert doc["profiles"] and doc["limits"] and doc["confirm"]
    assert doc["prepared"] is None
    assert doc["live_service"] is None and doc["leftovers"] is None
    assert doc["control"]["code"] == "aeri.disabled"


def test_a_missing_or_broken_control_section_is_a_switched_off_card(world):
    for value in (None, False):
        world.facts["control"]["enabled"] = value
        host, calls = counting(world)
        doc = aeri_ready.Readiness(host).view()
        assert calls == {} and doc["enabled"] is False and doc["readiness"] == []

    def boom():
        raise RuntimeError("boom")

    host, calls = counting(world, control=boom)
    doc = aeri_ready.Readiness(host).view()
    assert calls == {} and doc["enabled"] is False and doc["readiness"] == []


def test_switching_it_on_reads_everything_the_next_time(world):
    world.facts["control"]["enabled"] = False
    reader = aeri_ready.Readiness(world.host())
    assert reader.view()["readiness"] == []
    world.facts["control"]["enabled"] = True
    world.advance(aeri_ready.CACHE_S + 0.1)
    assert len(reader.view()["readiness"]) == len(aeri_ready.ROW_CODES)


@pytest.mark.parametrize(
    "fact, row",
    [("hosted_e", "no_e_run"), ("hosted_d", "no_other_aeri")],
)
def test_while_the_page_runs_its_own_client_or_d_only_the_reason_is_read(
    world, fact, row
):
    """The arm controller is up then and no start is possible: one row says why,
    nothing else is asked of the machine."""
    world.facts[fact] = True
    if fact == "hosted_d":
        world.facts["hosted_e"] = True  # (the page's answer for E counts D too)
    host, calls = counting(world)
    reader = aeri_ready.Readiness(host)
    doc = reader.view()
    assert calls == {}, "nothing was started or scanned"
    assert [r["code"] for r in doc["readiness"]] == [row]
    assert doc["readiness"][0]["level"] == "refuse" and doc["ready"] is False
    assert doc["live_service"] is None and doc["leftovers"] is None
    assert doc["enabled"] is True and doc["site"]["state"] == "ok"
    snap = reader.snapshot()
    assert snap["ready"] is False and [r["code"] for r in snap["rows"]] == [row]
    assert calls == {}


def test_a_hosted_probe_that_cannot_answer_does_not_make_a_light_read(world):
    def boom():
        raise RuntimeError("boom")

    host, calls = counting(world, hosted_e=boom, hosted_d=boom)
    doc = aeri_ready.Readiness(host).view()
    assert len(doc["readiness"]) == len(aeri_ready.ROW_CODES), "a full read"
    assert calls["gpu"] == 1
    codes = {r["code"]: r["level"] for r in doc["readiness"]}
    assert codes["no_e_run"] == "unknown" and codes["no_other_aeri"] == "unknown"


def test_a_full_read_is_one_batch_each_source_asked_once(world):
    host, calls = counting(world)
    doc = aeri_ready.Readiness(host).view()
    assert len(doc["readiness"]) == len(aeri_ready.ROW_CODES)
    # git: top level, HEAD, status; the two lock files are two questions; every
    # other source once (the stand-in program is not run: no prepared run)
    allowed = {"run": 3, "flock": 2}
    assert calls
    for name, n in calls.items():
        assert n <= allowed.get(name, 1), (name, calls)


def test_the_write_gates_of_a_light_read_do_not_start_a_batch(world):
    """A stop asked while the page runs its own client is refused for that, from
    the light read."""
    from levi.marvel0.aeri_prep import Refuse

    world.facts["hosted_e"] = True
    world.set_live("running")
    prep = world.preparer()
    body = {"request_id": "req-live-stop-1", "confirm": "stop live service"}
    with pytest.raises(Refuse) as caught:
        prep.live_stop(body)
    assert caught.value.code == "e_active"
    body = {"request_id": "req-left-stop-1", "confirm": "stop leftover aeri units"}
    with pytest.raises(Refuse) as caught:
        prep.leftovers_stop(body)
    assert caught.value.code == "e_active"
    assert world.systemctl.calls == []


def test_the_rows_that_may_be_warn_are_named_and_no_other_is(world):
    """The contract with the start gate: ``warn`` only for ``WARN_OK``."""
    assert aeri_ready.WARN_OK == aeri_ready.WARN_ROWS == ("stack_ready",)
    assert set(aeri_ready.WARN_OK) <= set(aeri_ready.ROW_CODES)
    seen = set()
    for stack in (World.stack_doc(), World.stack_doc(a="unknown", c="unknown")):
        world.facts["stack"] = stack
        for r in aeri_ready.rows(facts_of(world)):
            if r["level"] == "warn":
                seen.add(r["code"])
    assert seen == {"stack_ready"}
