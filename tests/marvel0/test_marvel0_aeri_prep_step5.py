"""The step-5 part of the prepare form (``aeri_prep``): ``reset_policy`` (the reverse
loop on the reset policy service) and ``early_check`` (the VLM's early done check),
on the made-up machine of ``marvel0_aeri_world.py``. The site's optional template
``step5.base.json`` is a fixture here (``fixtures/aeri_templates``): nothing reads
the maintainer's ``lab/``. What the real AERI code of this repository (``levi.automatic``)
accepts is checked on the files that are written: ``cli.load_job`` for the job and
``fr3_run.load_binding`` for the binding, so a rendered file is not only what these
tests expect but what the run itself loads."""

import hashlib
import json
import sys

import pytest
from marvel0_aeri_world import (
    DEFAULT_PROFILES,
    FIXTURES,
    NOW,
    World,
    installed_profiles,
)

from levi.automatic import cli, fr3_run
from levi.automatic import subtask_loop as st
from levi.marvel0 import aeri_prep, aeri_ready
from levi.marvel0.aeri_prep import Refuse

STEP5 = json.loads((FIXTURES / aeri_prep.STEP5_FILE).read_text())
FULL = {"start_home": True, "early_check": True, "reset_policy": True}
GRASP = {**FULL, "grasp_assist": True}
EARLY_ONLY = {"start_home": True, "early_check": True, "reset_policy": False}


def machine(tmp_path, monkeypatch, caps=None, step5="listed"):
    """A made-up machine whose default profile has ``caps`` (None: the real
    capabilities of the default pin, which has neither) and whose templates folder
    holds ``step5.base.json`` ("listed" in SOURCES.sha256, "unlisted", or None)."""
    table = dict(DEFAULT_PROFILES)
    if caps is not None:
        table["harvest-5bc750ca"] = caps
    installed_profiles(monkeypatch, table)
    world = World(tmp_path)
    if step5 is not None:
        put_step5(world, STEP5, listed=step5 == "listed")
    return world


def put_step5(world, value, listed=True, raw=None):
    world.put_step5(value, listed=listed, raw=raw)


def mutated(**blocks):
    value = json.loads(json.dumps(STEP5))
    for key, change in blocks.items():
        if change is None:
            value.pop(key)
        else:
            change(value[key])
    return value


def prepare(world, **change):
    page = aeri_prep.state_dir(world.aeri, aeri_prep.StateRoot(world.state), True)
    host = aeri_prep.Host(now=lambda: NOW, registry=lambda: world.registry)
    return aeri_prep.prepare(world.aeri, world.form(**change), host, page)


def refusal(world, **change) -> Refuse:
    with pytest.raises(Refuse) as caught:
        prepare(world, **change)
    return caught.value


def runs(world):
    return sorted(p.name for p in world.runs.iterdir())


def asked(error):
    return error.code, error.extra["field"], error.extra["reason"]


EARLY = {"on_events": True}  # the form's other defaults: gripper_open, 8, 60, 3


def live(world, name, tmp_path):
    """The prepared run's two files loaded by the AERI code of this repository.
    The service paths the template names are only plausible: the files that must
    exist for the policy switch (an executable, a folder) are made real."""
    run = world.runs / name
    binding = json.loads((run / "binding.json").read_text())
    for service in binding["policy_swap"]["services"].values():
        service["argv"][0] = sys.executable
        service["working_directory"] = str(tmp_path)
    path = tmp_path / "binding-live.json"
    path.write_text(json.dumps(binding))
    job = cli.load_job(run / "job.yaml")
    return job, fr3_run.load_binding(path, job)[0]


# --- the capability of the pinned code version -------------------------------------------


def test_the_default_pin_has_neither_capability(tmp_path, monkeypatch):
    """F18 of the design: ``harvest-5bc750ca`` has no early_check; neither it nor
    the other two older pins has the reset keys (only ``main-ab38c7b1`` does), so
    both are refused for the default."""
    from levi.marvel0 import aeri_state

    for pid, p in aeri_state.PROFILES.items():
        assert p.reset_policy is (pid == "main-ab38c7b1"), pid
    assert aeri_state.PROFILES["harvest-5bc750ca"].early_check is False
    assert aeri_state.PROFILES["main-297a74c3"].early_check is True
    world = World(tmp_path)  # (the real table: installed_profiles is not used)
    before = runs(world)
    for change in ({"reset_policy": True}, {"early_check": EARLY}):
        assert asked(refusal(world, **change))[2] == "unsupported"
    assert runs(world) == before


def test_a_profile_without_the_capability_refuses_each_ask_and_makes_no_folder(
    tmp_path, monkeypatch
):
    world = machine(tmp_path, monkeypatch, caps={"start_home": True})
    before = runs(world)
    assert asked(refusal(world, reset_policy=True)) == (
        "form_invalid",
        "reset_policy",
        "unsupported",
    )
    assert asked(refusal(world, early_check=EARLY)) == (
        "form_invalid",
        "early_check",
        "unsupported",
    )
    # not silently dropped: even an interval-only check is refused, never prepared without
    assert asked(refusal(world, early_check={"on_events": False}))[2] == "unsupported"
    assert runs(world) == before


def test_a_capability_that_is_not_exactly_true_is_not_one(tmp_path, monkeypatch):
    world = machine(
        tmp_path,
        monkeypatch,
        caps={"start_home": True, "reset_policy": "yes", "early_check": 1},
    )
    assert asked(refusal(world, reset_policy=True))[2] == "unsupported"
    assert asked(refusal(world, early_check=EARLY))[2] == "unsupported"


def test_the_document_tells_the_page_what_the_code_version_can_do(
    tmp_path, monkeypatch
):
    world = machine(tmp_path, monkeypatch, caps=FULL)
    doc = aeri_ready.Readiness(world.host()).view()
    assert doc["profile"]["reset_policy"] is True
    assert doc["profile"]["early_check"] is True
    row = {p["id"]: p for p in doc["profiles"]}
    assert row["harvest-5bc750ca"]["reset_policy"] is True
    assert row["bfdee2cf"]["reset_policy"] is False


# --- a default form is what it was ------------------------------------------------------------


def test_a_default_form_is_byte_identical_with_or_without_the_step5_template(
    tmp_path, monkeypatch
):
    plain = machine(tmp_path / "a", monkeypatch, caps=FULL, step5=None)
    other = machine(tmp_path / "b", monkeypatch, caps=FULL)
    one, two = prepare(plain), prepare(other)
    for file in ("job.yaml", "binding.json"):
        left = (plain.runs / one["name"] / file).read_text()
        right = (other.runs / two["name"] / file).read_text()
        assert left.replace(str(plain.ext), "X") == right.replace(str(other.ext), "X")
    run = json.loads((other.runs / two["name"] / "RUN.json").read_text())
    assert "reset_policy" not in run and "early_check" not in run
    assert set(run["templates_sha256"]) == {"job", "binding"}
    assert two["reset_policy"] is False and two["early_check"] is None
    # the job as the run loads it has no step-5 key: its plan is the old plan
    job = cli.load_job(other.runs / two["name"] / "job.yaml")
    assert "subtask_reverse_policy_role" not in job["plan"]["run"]
    assert "subtask_early_check" not in job["plan"]["run"]
    binding = json.loads((other.runs / two["name"] / "binding.json").read_text())
    assert set(binding["policies"]) == {"forward"}
    assert "signal_events" not in binding


def test_a_broken_unused_step5_template_does_not_block_a_default_run(
    tmp_path, monkeypatch
):
    world = machine(tmp_path, monkeypatch, caps=FULL, step5="unlisted")
    assert prepare(world)["name"]
    put_step5(world, None, raw=b"not json at all")
    assert prepare(world)["name"]


# --- reset_policy ---------------------------------------------------------------------------


def test_reset_policy_renders_the_role_the_sentences_and_the_service(
    tmp_path, monkeypatch
):
    world = machine(tmp_path, monkeypatch, caps=FULL)
    done = prepare(world, reset_policy=True)
    run = world.runs / done["name"]
    job = (run / "job.yaml").read_text()
    assert "  reverse_policy_role: reset\n" in job
    for entry in STEP5["reset"]["prompts"]:
        assert f"    - {entry}\n" in job
    binding = json.loads((run / "binding.json").read_text())
    assert binding["policies"]["reset"] == STEP5["reset"]["endpoint"]
    assert binding["policy_swap"]["services"]["reset"] == STEP5["reset"]["service"]
    assert set(binding["policies"]) == set(binding["policy_swap"]["services"])
    assert binding["policies"]["forward"] == {"host": "127.0.0.1", "port": 8000}
    assert "signal_events" not in binding  # no early check was asked for
    meta = json.loads((run / "RUN.json").read_text())
    assert meta["reset_policy"] is True and "early_check" not in meta
    assert (
        meta["templates_sha256"]["step5"]
        == hashlib.sha256(
            (world.templates / aeri_prep.STEP5_FILE).read_bytes()
        ).hexdigest()
    )
    assert done["reset_policy"] is True and done["intact"] is True


def test_the_files_of_a_reset_policy_run_load_in_the_aeri_code(tmp_path, monkeypatch):
    world = machine(tmp_path, monkeypatch, caps=FULL)
    done = prepare(world, reset_policy=True)
    job, binding = live(world, done["name"], tmp_path)
    cfg = job["config"]
    assert cfg.subtask_reverse_policy_role == "reset"
    assert st.reset_prompt_table(cfg.subtask_reset_prompts) == {
        "purple eggplant|white plate": "Reset: pick the eggplant into the blue plate",
        "purple eggplant|table": "Reset: pick the eggplant onto the table",
    }
    # round trip: the sentences the AERI code reads are the template's, unchanged
    assert sorted(cfg.subtask_reset_prompts) == sorted(STEP5["reset"]["prompts"])
    assert job["plan"]["deployments"]["reset"] == STEP5["reset"]["deployment"]
    assert set(binding["policies"]) == {"forward", "reset"}
    assert set(binding["policy_swap"]["services"]) == {"forward", "reset"}


def test_the_reset_service_must_be_what_the_policy_switch_accepts(
    tmp_path, monkeypatch
):
    """The checkpoint of the job and the service's argv are one declaration in
    the template; the load above fails if they drift apart."""
    world = machine(tmp_path, monkeypatch, caps=FULL)

    def wrong_dir(block):
        block["deployment"]["checkpoint_dir"] = "/path/to/elsewhere"

    put_step5(world, mutated(reset=wrong_dir))
    done = prepare(world, reset_policy=True)  # the page's shape check passes
    with pytest.raises(ValueError):  # the AERI code's own check does not
        live(world, done["name"], tmp_path)


def test_a_site_without_a_step5_template_refuses_reset_policy(tmp_path, monkeypatch):
    world = machine(tmp_path, monkeypatch, caps=FULL, step5=None)
    before = runs(world)
    assert asked(refusal(world, reset_policy=True)) == (
        "form_invalid",
        "reset_policy",
        "template",
    )
    assert runs(world) == before


def test_a_template_without_the_reset_block_refuses_it(tmp_path, monkeypatch):
    world = machine(tmp_path, monkeypatch, caps=FULL)
    put_step5(world, mutated(reset=None))
    assert asked(refusal(world, reset_policy=True))[1:] == ("reset_policy", "template")
    assert prepare(world, early_check=EARLY)["name"]  # the other block still works


@pytest.mark.parametrize(
    "change",
    [
        lambda b: b["deployment"].update(checkpoint_dir="relative/dir"),
        lambda b: b["deployment"].update(checkpoint_dir="/a/../b"),
        lambda b: b["deployment"].update(config="has space"),
        lambda b: b["deployment"].update(extra="x"),
        lambda b: b["endpoint"].update(host="0.0.0.0"),
        lambda b: b["endpoint"].update(port=5000),
        lambda b: b["endpoint"].update(port=7470),
        lambda b: b["endpoint"].update(port="8001"),
        lambda b: b["service"].update(unit="levi-live"),
        lambda b: b["service"].update(mem_fraction=0.9),
        lambda b: b["service"].update(mem_fraction=True),
        lambda b: b["service"]["argv"].__setitem__(-1, "8002"),  # --port differs
        lambda b: b.update(prompts=[]),
        lambda b: b.update(prompts=["not an entry"]),
        # a plain YAML scalar would drop the trailing space (review 11-16)
        lambda b: b.update(prompts=[b["prompts"][0] + " "]),
        lambda b: b.update(prompts=[b["prompts"][0], b["prompts"][0]]),
        lambda b: b.pop("service"),
    ],
)
def test_an_unusable_reset_block_is_refused_before_a_folder_is_made(
    tmp_path, monkeypatch, change
):
    world = machine(tmp_path, monkeypatch, caps=FULL)
    put_step5(world, mutated(reset=change))
    before = runs(world)
    assert asked(refusal(world, reset_policy=True))[1:] == ("reset_policy", "template")
    assert runs(world) == before


@pytest.mark.parametrize("port", [8000, 8100])
def test_a_reset_port_of_the_forward_policy_or_the_vlm_is_refused(
    tmp_path, monkeypatch, port
):
    world = machine(tmp_path, monkeypatch, caps=FULL)

    def same_port(block):
        block["endpoint"]["port"] = port
        block["service"]["argv"][-1] = str(port)

    put_step5(world, mutated(reset=same_port))
    before = runs(world)
    assert asked(refusal(world, reset_policy=True))[1:] == ("reset_policy", "template")
    assert runs(world) == before


def test_a_step5_template_that_is_there_but_unreadable_is_a_changed_template(
    tmp_path, monkeypatch
):
    world = machine(tmp_path, monkeypatch, caps=FULL)
    path = world.templates / aeri_prep.STEP5_FILE
    real = tmp_path / "elsewhere.json"
    real.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(real)  # a link is not a regular file: not "absent"
    error = refusal(world, reset_policy=True)
    assert error.code == "templates_changed"
    assert error.extra["which"] == [aeri_prep.STEP5_FILE]
    assert aeri_prep.step5_state(world.templates)["state"] == "unreadable"
    path.unlink()
    assert aeri_prep.step5_state(world.templates)["state"] == "absent"


def test_a_step5_template_that_does_not_match_sources_is_a_changed_template(
    tmp_path, monkeypatch
):
    for how in ("unlisted", "edited"):
        world = machine(tmp_path / how, monkeypatch, caps=FULL, step5="listed")
        if how == "unlisted":
            put_step5(world, STEP5, listed=False)
        else:
            path = world.templates / aeri_prep.STEP5_FILE
            path.write_text(path.read_text() + " ")
        error = refusal(world, reset_policy=True)
        assert error.code == "templates_changed"
        assert error.extra["which"] == [aeri_prep.STEP5_FILE]
        assert runs(world) == []


# --- early_check ------------------------------------------------------------------------------


def test_an_early_check_on_events_renders_the_job_keys_and_the_signal_events(
    tmp_path, monkeypatch
):
    world = machine(tmp_path, monkeypatch, caps=FULL)
    done = prepare(world, early_check=EARLY)
    run = world.runs / done["name"]
    text = (run / "job.yaml").read_text()
    assert (
        "  early_check:\n    on_events: true\n    event_types:\n      - gripper_open\n"
        "    settle_steps: 8\n    every_steps: 60\n    max_checks: 3\n"
    ) in text
    assert "reverse_policy_role" not in text
    binding = json.loads((run / "binding.json").read_text())
    assert binding["signal_events"] == STEP5["signal_events"]
    meta = json.loads((run / "RUN.json").read_text())
    assert meta["early_check"]["event_types"] == ["gripper_open"]
    assert meta["early_check"]["every_steps"] == 60
    assert done["early_check"] == meta["early_check"]
    job, loaded = live(world, done["name"], tmp_path)
    cfg = job["config"].subtask_early_check
    assert (cfg.on_events, cfg.event_types, cfg.settle_steps) == (
        True,
        ("gripper_open",),
        8,
    )
    assert (cfg.every_steps, cfg.max_checks) == (60, 3)
    assert loaded["signal_events"] == STEP5["signal_events"]


def test_the_form_defaults_follow_the_design_with_the_gripper_open_event_only():
    assert aeri_prep.EARLY_CHECK_FORM == {
        "on_events": True,
        "event_types": ["gripper_open"],
        "settle_steps": 8,
        "every_steps": 60,
        "max_checks": 3,
    }
    assert aeri_prep.EARLY_FORM_EVENT_TYPES == ("gripper_open",)


def test_an_interval_only_check_needs_no_signal_events(tmp_path, monkeypatch):
    world = machine(tmp_path, monkeypatch, caps=EARLY_ONLY, step5=None)
    done = prepare(
        world, early_check={"on_events": False, "every_steps": 80, "max_checks": 2}
    )
    run = world.runs / done["name"]
    assert "signal_events" not in json.loads((run / "binding.json").read_text())
    job, _ = live(world, done["name"], tmp_path)
    cfg = job["config"].subtask_early_check
    assert (cfg.on_events, cfg.every_steps, cfg.max_checks) == (False, 80, 2)
    meta = json.loads((run / "RUN.json").read_text())
    assert "step5" not in meta["templates_sha256"]  # the template was not used


def test_every_key_of_the_check_may_be_set(tmp_path, monkeypatch):
    world = machine(tmp_path, monkeypatch, caps=FULL)
    done = prepare(
        world,
        early_check={
            "on_events": True,
            "event_types": ["gripper_open"],
            "settle_steps": 12,
            "every_steps": None,  # no interval fallback
            "max_checks": 5,
        },
    )
    text = (world.runs / done["name"] / "job.yaml").read_text()
    assert "    settle_steps: 12\n" in text and "every_steps" not in text
    assert "    max_checks: 5\n" in text
    assert (
        live(world, done["name"], tmp_path)[0]["config"].subtask_early_check.every_steps
        is None
    )


def test_both_asks_together_make_one_consistent_run(tmp_path, monkeypatch):
    world = machine(tmp_path, monkeypatch, caps=FULL)
    done = prepare(world, reset_policy=True, early_check=EARLY)
    job, binding = live(world, done["name"], tmp_path)
    assert job["config"].subtask_reverse_policy_role == "reset"
    assert job["config"].subtask_early_check.on_events is True
    assert set(binding["policies"]) == {"forward", "reset"}
    assert "signal_events" in binding


@pytest.mark.parametrize(
    "early, field",
    [
        ("yes", "early_check"),
        (True, "early_check"),
        ({"unknown": 1}, "early_check"),
        ({"on_events": "yes"}, "early_check.on_events"),
        ({"event_types": "gripper_open"}, "early_check.event_types"),
        ({"event_types": [1]}, "early_check.event_types"),
        ({"settle_steps": True}, "early_check.settle_steps"),
        ({"settle_steps": 1.5}, "early_check.settle_steps"),
        ({"max_checks": "3"}, "early_check.max_checks"),
        ({"every_steps": "60"}, "early_check.every_steps"),
    ],
)
def test_a_badly_typed_early_check_is_refused(tmp_path, monkeypatch, early, field):
    world = machine(tmp_path, monkeypatch, caps=FULL)
    error = refusal(world, early_check=early)
    assert (error.code, error.extra["field"], error.extra["reason"]) == (
        "form_invalid",
        field,
        "type",
    )
    assert runs(world) == []


@pytest.mark.parametrize(
    "early, field",
    [
        ({"event_types": []}, "early_check.event_types"),
        ({"event_types": ["gripper_open", "gripper_open"]}, "early_check.event_types"),
        # not offered by the form (another agent adds it to the registry)
        ({"event_types": ["object_dropped"]}, "early_check.event_types"),
        (
            {"event_types": ["gripper_open", "object_dropped"]},
            "early_check.event_types",
        ),
        ({"event_types": ["nope"]}, "early_check.event_types"),
        ({"settle_steps": -1}, "early_check.settle_steps"),
        ({"every_steps": 39}, "early_check.every_steps"),
        ({"max_checks": 0}, "early_check.max_checks"),
        ({"max_checks": 21}, "early_check.max_checks"),
        ({"on_events": False, "every_steps": None}, "early_check"),  # no trigger
    ],
)
def test_an_early_check_outside_the_jobs_bounds_is_refused(
    tmp_path, monkeypatch, early, field
):
    world = machine(tmp_path, monkeypatch, caps=FULL)
    error = refusal(world, early_check=early)
    assert (error.code, error.extra["field"], error.extra["reason"]) == (
        "form_invalid",
        field,
        "range",
    )
    assert runs(world) == []


# --- the event source: the measured gripper, never the command ------------------------------------


def commanded(block):
    block["profile"]["channels"][0].update(kind="commanded", feature="action")
    block["bindings"][0].update(kind="commanded", feature="action", path=["pose", 6])


def measured_path_wrong(block):
    block["bindings"][0]["path"] = [
        "pose",
        6,
    ]  # kind says measured, source is the command


def channel_kind_wrong(block):
    block["profile"]["channels"][0]["kind"] = "commanded"


def a_second_commanded_binding(block):
    block["bindings"].append(
        {
            "feature": "observation.state",
            "index": 6,
            "path": ["pose", 6],
            "kind": "commanded",
        }
    )


def a_second_commanded_channel(block):
    """A measured gripper channel, and beside it the commanded one."""
    block["profile"]["channels"].append(
        {
            "role": "gripper",
            "feature": "action",
            "index": 6,
            "kind": "commanded",
            "units": "normalized",
            "valid_range": [0, 1],
            "open_level": "high",
            "actor_id": "arm_0",
        }
    )
    block["bindings"].append(
        {"feature": "action", "index": 6, "path": ["pose", 6], "kind": "commanded"}
    )


def no_gripper(block):
    """A declaration the binding itself accepts (three measured positions) and
    that cannot make a gripper event."""
    block["profile"]["channels"] = [
        {
            "role": "position",
            "feature": "pose",
            "index": i,
            "axis": axis,
            "kind": "measured",
            "units": "m",
            "frame": "base",
            "actor_id": "arm_0",
        }
        for i, axis in enumerate("xyz")
    ]
    block["bindings"] = [
        {"feature": "pose", "index": i, "path": ["pose", i], "kind": "measured"}
        for i in range(3)
    ]
    assert fr3_run.signal_binding(block)  # the declaration is valid on its own


@pytest.mark.parametrize(
    "change",
    [
        commanded,
        measured_path_wrong,
        channel_kind_wrong,
        a_second_commanded_binding,
        a_second_commanded_channel,
        no_gripper,
    ],
)
def test_an_event_source_that_is_not_the_measured_gripper_is_refused(
    tmp_path, monkeypatch, change
):
    """The early check on events is driven by what the gripper did. A template
    whose gripper channel is the commanded value (or that has no gripper channel)
    would make an event out of what was merely asked of it."""
    world = machine(tmp_path, monkeypatch, caps=FULL)
    put_step5(world, mutated(signal_events=change))
    assert asked(refusal(world, early_check=EARLY)) == (
        "form_invalid",
        "early_check",
        "template",
    )
    assert runs(world) == []
    # a check without events never reads the declaration, so it still prepares
    assert prepare(world, early_check={"on_events": False})["name"]


def test_a_template_without_signal_events_refuses_an_event_check(tmp_path, monkeypatch):
    world = machine(tmp_path, monkeypatch, caps=FULL)
    put_step5(world, mutated(signal_events=None))
    assert asked(refusal(world, early_check=EARLY))[1:] == ("early_check", "template")


def test_the_early_check_on_events_is_refused_without_a_step5_template(
    tmp_path, monkeypatch
):
    world = machine(tmp_path, monkeypatch, caps=FULL, step5=None)
    assert asked(refusal(world, early_check=EARLY))[1:] == ("early_check", "template")


def test_a_malformed_template_file_is_refused_not_guessed(tmp_path, monkeypatch):
    world = machine(tmp_path, monkeypatch, caps=FULL)
    for value in (
        {"schema": "other", "reset": STEP5["reset"]},
        {**STEP5, "extra": 1},
        [1],
    ):
        put_step5(world, value)
        assert asked(refusal(world, reset_policy=True))[2] == "template"
    put_step5(world, None, raw=b"{not json")
    assert refusal(world, reset_policy=True).code == "templates_changed"


def test_a_failed_check_leaves_no_folder_for_a_step5_run(tmp_path, monkeypatch):
    world = machine(tmp_path, monkeypatch, caps=FULL)
    world.behaviour.write_text(json.dumps({"mode": "fail"}))
    with pytest.raises(Refuse) as caught:
        prepare(world, reset_policy=True, early_check=EARLY)
    assert caught.value.code == "check_failed"
    assert runs(world) == []


# --- grasp_assist -----------------------------------------------------------------------------


def test_grasp_assist_renders_the_templates_limits_and_the_operator_slots(
    tmp_path, monkeypatch
):
    world = machine(tmp_path, monkeypatch, caps=GRASP)
    done = prepare(world, reset_policy=True, grasp_assist=True)
    run = world.runs / done["name"]
    text = (run / "job.yaml").read_text()
    assert (
        "  grasp_assist:\n    enabled: true\n    grasp_source: operator\n"
        "    handoff: operator\n    step_m: 0.01\n    max_steps: 40\n"
        "    max_travel_m: 0.35\n    follow_error_m: 0.005\n"
        "    max_follow_misses: 2\n    holding_band:\n"
        "      - purple eggplant|0.15|0.4\n"
    ) in text
    # the page never offers the planned hand-over by the measured fact
    assert "holding_fact" not in text
    meta = json.loads((run / "RUN.json").read_text())
    assert meta["grasp_assist"] is True and meta["reset_policy"] is True
    assert done["grasp_assist"] is True and done["intact"] is True
    # the binding is the reset-policy run's: the takeover adds nothing to it
    other = prepare(world, reset_policy=True)
    assert (run / "binding.json").read_text().replace(done["name"], "X") == (
        world.runs / other["name"] / "binding.json"
    ).read_text().replace(other["name"], "X")


def test_the_jobs_grasp_assist_is_the_templates_through_the_aeri_loader(
    tmp_path, monkeypatch
):
    """Round trip: what ``cli.load_job`` (the AERI code of this repository) reads
    from the written job is the template's block, value for value."""
    world = machine(tmp_path, monkeypatch, caps=GRASP)
    done = prepare(world, reset_policy=True, grasp_assist=True)
    job, binding = live(world, done["name"], tmp_path)
    cfg = job["config"].subtask_grasp_assist
    template = STEP5["grasp_assist"]
    assert cfg.describe() == {
        "enabled": True,
        "grasp_source": "operator",
        "handoff": "operator",
        **{k: v for k, v in template.items() if k != "holding_band"},
        "holding_band": sorted(template["holding_band"]),
    }
    assert job["plan"]["run"]["subtask_grasp_assist"] == cfg.describe()
    assert set(cfg.bands()) == {"purple eggplant"}
    assert job["config"].subtask_reverse_policy_role == "reset"
    assert set(binding["policies"]) == {"forward", "reset"}


def test_grasp_assist_with_other_numbers_round_trips_too(tmp_path, monkeypatch):
    world = machine(tmp_path, monkeypatch, caps=GRASP)

    def other(block):
        block.update(
            step_m=0.02,
            max_steps=7,
            max_travel_m=1,
            follow_error_m=0.01,
            max_follow_misses=5,
            holding_band=["purple eggplant|0.2|0.35", "white plate|0.1|0.2"],
        )

    put_step5(world, mutated(grasp_assist=other))
    done = prepare(world, reset_policy=True, grasp_assist=True)
    cfg = live(world, done["name"], tmp_path)[0]["config"].subtask_grasp_assist
    assert (cfg.step_m, cfg.max_steps, cfg.max_travel_m) == (0.02, 7, 1.0)
    assert (cfg.follow_error_m, cfg.max_follow_misses) == (0.01, 5)
    assert cfg.holding_band == ("purple eggplant|0.2|0.35", "white plate|0.1|0.2")


def test_a_small_number_is_written_as_a_plain_yaml_number():
    for value, text in ((0.005, "0.005"), (1e-05, "0.00001"), (2, "2"), (0.5, "0.5")):
        assert aeri_prep._yaml_number(value) == text


def test_grasp_assist_is_refused_by_a_version_without_the_capability(
    tmp_path, monkeypatch
):
    world = machine(tmp_path, monkeypatch, caps=FULL)  # reset keys, no takeover
    before = runs(world)
    error = refusal(world, reset_policy=True, grasp_assist=True)
    assert asked(error) == ("form_invalid", "grasp_assist", "unsupported")
    assert runs(world) == before
    # the real table: only the main-ab38c7b1 profile has it, not the default pin
    real = World(tmp_path / "real")
    put_step5(real, STEP5)
    assert asked(refusal(real, reset_policy=True, grasp_assist=True))[2] == (
        "unsupported"
    )
    assert runs(real) == []


def test_grasp_assist_without_the_reset_policy_is_refused(tmp_path, monkeypatch):
    world = machine(tmp_path, monkeypatch, caps=GRASP)
    error = refusal(world, grasp_assist=True)
    assert asked(error) == ("form_invalid", "grasp_assist", "unsupported")
    assert error.extra["detail"] == "needs_reset_policy"
    assert asked(refusal(world, grasp_assist=True, reset_policy=False))[2] == (
        "unsupported"
    )
    assert runs(world) == []


@pytest.mark.parametrize("value", ["true", 1, None, {"handoff": "holding_fact"}, []])
def test_grasp_assist_is_a_bool_and_nothing_else(tmp_path, monkeypatch, value):
    """The form has no way to say ``holding_fact`` (or any other setting): it is
    a switch. A value that is not a bool is refused before anything else."""
    world = machine(tmp_path, monkeypatch, caps=GRASP)
    assert asked(refusal(world, reset_policy=True, grasp_assist=value)) == (
        "form_invalid",
        "grasp_assist",
        "type",
    )
    assert runs(world) == []


def test_a_site_without_a_grasp_assist_block_refuses_it(tmp_path, monkeypatch):
    world = machine(tmp_path, monkeypatch, caps=GRASP, step5=None)
    assert asked(refusal(world, reset_policy=True, grasp_assist=True)) == (
        "form_invalid",
        "reset_policy",
        "template",
    )
    put_step5(world, mutated(grasp_assist=None))
    assert asked(refusal(world, reset_policy=True, grasp_assist=True)) == (
        "form_invalid",
        "grasp_assist",
        "template",
    )
    assert prepare(world, reset_policy=True)["name"]  # the other asks still work


@pytest.mark.parametrize(
    "change",
    [
        lambda b: b.pop("holding_band"),
        lambda b: b.pop("step_m"),
        lambda b: b.update(extra=1),
        # the two slots are the page's, not the template's
        lambda b: b.update(handoff="holding_fact"),
        lambda b: b.update(handoff="operator"),
        lambda b: b.update(grasp_source="operator"),
        lambda b: b.update(enabled=True),
        lambda b: b.update(step_m=0.03),  # the hard limit is 2 cm
        lambda b: b.update(step_m=0),
        lambda b: b.update(step_m="0.01"),
        lambda b: b.update(step_m=True),
        lambda b: b.update(max_steps=0),
        lambda b: b.update(max_steps=201),
        lambda b: b.update(max_steps=40.0),
        lambda b: b.update(max_steps=True),
        lambda b: b.update(max_travel_m=1.5),
        lambda b: b.update(max_travel_m=-1),
        lambda b: b.update(follow_error_m=0.02),
        lambda b: b.update(max_follow_misses=0),
        lambda b: b.update(max_follow_misses=6),
        lambda b: b.update(holding_band=[]),
        lambda b: b.update(holding_band="purple eggplant|0.15|0.4"),
        lambda b: b.update(holding_band=[1]),
        lambda b: b.update(holding_band=["purple eggplant|0.15|0.4 "]),
        lambda b: b.update(holding_band=["purple eggplant|0.4|1.5"]),
        lambda b: b.update(holding_band=["Purple Eggplant|0.15|0.4"]),
        lambda b: b.update(holding_band=["purple eggplant|holding_fact|0.4"]),
        # one entry per object
        lambda b: b.update(holding_band=["a b|0.1|0.2", "a b|0.3|0.4"]),
        # lo above hi
        lambda b: b.update(holding_band=["purple eggplant|0.4|0.15"]),
    ],
)
def test_a_bad_grasp_assist_block_is_refused_before_a_folder_is_made(
    tmp_path, monkeypatch, change
):
    world = machine(tmp_path, monkeypatch, caps=GRASP)
    put_step5(world, mutated(grasp_assist=change))
    before = runs(world)
    assert asked(refusal(world, reset_policy=True, grasp_assist=True))[1:] == (
        "grasp_assist",
        "template",
    )
    assert runs(world) == before


def test_a_bad_grasp_assist_block_does_not_block_the_runs_that_do_not_use_it(
    tmp_path, monkeypatch
):
    world = machine(tmp_path, monkeypatch, caps=GRASP)
    put_step5(world, mutated(grasp_assist=lambda b: b.update(step_m=9)))
    assert prepare(world)["name"]
    assert prepare(world, reset_policy=True)["name"]
    assert prepare(world, early_check=EARLY)["name"]


def test_a_step5_template_with_an_unknown_block_is_not_a_step5_template(
    tmp_path, monkeypatch
):
    world = machine(tmp_path, monkeypatch, caps=GRASP)
    value = mutated()
    value["handoff"] = {"mode": "holding_fact"}
    put_step5(world, value)
    assert asked(refusal(world, reset_policy=True, grasp_assist=True))[2] == "template"


def test_a_default_form_is_byte_identical_whatever_the_grasp_assist_block(
    tmp_path, monkeypatch
):
    """The takeover adds nothing to a run that does not ask for it: job, binding
    and RUN.json are the same bytes (the run's name aside) with or without the
    capability and with or without the block in the site's template."""
    world = machine(tmp_path, monkeypatch, caps=FULL, step5=None)
    docs = [prepare(world)]  # no step-5 template, no capability: the old run
    installed_profiles(monkeypatch, {**DEFAULT_PROFILES, "harvest-5bc750ca": GRASP})
    put_step5(world, STEP5)
    docs.append(prepare(world))
    put_step5(world, mutated(grasp_assist=None))
    docs.append(prepare(world))
    assert len({d["name"] for d in docs}) == 3

    def of(doc, file):
        return (world.runs / doc["name"] / file).read_text().replace(doc["name"], "X")

    for file in ("job.yaml", "binding.json"):
        assert of(docs[1], file) == of(docs[0], file) == of(docs[2], file), file
    runs_json = [json.loads(of(d, "RUN.json")) for d in docs]
    for run in runs_json:
        assert "grasp_assist" not in run and "reset_policy" not in run
        assert set(run["templates_sha256"]) == {"job", "binding"}
        assert run["plan_sha256"] and run["binding_sha256"]
    # RUN.json differs only where the run's own name goes into a digest
    named = ("execute", "plan_sha256", "binding_sha256", "files_sha256")
    stable = [{k: v for k, v in r.items() if k not in named} for r in runs_json]
    assert stable[0] == stable[1] == stable[2]
    assert [d["grasp_assist"] for d in docs] == [False, False, False]
    job = cli.load_job(world.runs / docs[1]["name"] / "job.yaml")
    assert "subtask_grasp_assist" not in job["plan"]["run"]


def test_a_reset_policy_run_is_byte_identical_with_or_without_the_takeover_block(
    tmp_path, monkeypatch
):
    with_block = machine(tmp_path / "a", monkeypatch, caps=GRASP)
    without = machine(tmp_path / "b", monkeypatch, caps=GRASP)
    put_step5(without, mutated(grasp_assist=None))
    one = prepare(with_block, reset_policy=True)
    two = prepare(without, reset_policy=True)
    for file in ("job.yaml", "binding.json"):
        left = (with_block.runs / one["name"] / file).read_text()
        right = (without.runs / two["name"] / file).read_text()
        assert left.replace(str(with_block.ext), "X") == right.replace(
            str(without.ext), "X"
        )
        assert "grasp_assist" not in left
    meta = json.loads((with_block.runs / one["name"] / "RUN.json").read_text())
    assert "grasp_assist" not in meta


def test_the_document_tells_the_page_which_versions_have_the_takeover(
    tmp_path, monkeypatch
):
    world = machine(tmp_path, monkeypatch, caps=GRASP)
    doc = aeri_ready.Readiness(world.host()).view()
    assert doc["profile"]["grasp_assist"] is True
    row = {p["id"]: p for p in doc["profiles"]}
    assert row["harvest-5bc750ca"]["grasp_assist"] is True
    assert row["bfdee2cf"]["grasp_assist"] is False


def test_a_template_number_that_is_not_finite_is_refused(tmp_path, monkeypatch):
    world = machine(tmp_path, monkeypatch, caps=GRASP)
    for value in (float("nan"), float("inf")):
        put_step5(world, mutated(grasp_assist=lambda b, v=value: b.update(step_m=v)))
        assert asked(refusal(world, reset_policy=True, grasp_assist=True))[1:] == (
            "grasp_assist",
            "template",
        )
