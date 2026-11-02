"""The arm controller that runs and the client command (``levi/marvel0/command.py``,
``arm_facts.py``, ``catalog_api.py``, ``run.py``): with the anyspeedup controller
rule v3's ``chunk_accel_alpha`` is the alpha of the running A's preset (an empty
field is that alpha, another one is refused with a reason), rules v1 and v2 are
refused for a preset other than ``today``, the default rollout group and the run
record say which controller ran, and rule v3 is refused when A is not the
controller the control file names. With the lab's controller, or without a
control file, nothing is asked and the command is what it always was."""

import pytest
from arm_controller_helpers import ANY_ROOT
from fastapi import FastAPI
from fastapi.testclient import TestClient
from site_sample import make_checkpoints, write_site
from test_marvel0_catalog_route import preview
from test_marvel0_command import PROMPT, form_for, refused, rendered
from test_marvel0_deploy import forbid_real_machine  # noqa: F401 - the guard
from test_marvel0_run import ATTEST
from test_marvel0_run import Machine as RunMachine
from test_marvel0_stack_act_control import GOOD

from levi.marvel0 import arm_facts, catalog_api, command, models, run
from levi.marvel0 import control_site as cs
from levi.marvel0 import site as marvel0_site


@pytest.fixture(autouse=True)
def fresh_cache():
    cs.clear_cache()
    yield
    cs.clear_cache()


# --- the client command ---------------------------------------------------------------------

V3 = {"chunk_accel": True, "chunk_accel_rule": "v3"}
ARM = command.Arm  # (what the arm controller asks of the form)


def alpha_flag(argv):
    return next(t for t in argv if t.startswith("--phase-alpha="))


def facts(**over):
    """The ``arm`` of the stack document, quiet unless a test says otherwise."""
    quiet = {
        "configured": None,
        "enabled": False,
        "running": None,
        "status": "idle",
        "alpha": None,
        "problem": None,
        "ramp_ok": True,
        "suffix": "",
    }
    return {**quiet, **over}


def running(preset, **over):
    """The facts of an A the page started as anyspeedup at ``preset``."""
    alpha = {"today": 0.005, "S1": 0.01}[preset]
    return facts(
        **{
            "configured": {"controller": "anyspeedup", "preset": preset},
            "enabled": True,
            "running": {"controller": "anyspeedup", "preset": preset},
            "status": "match",
            "alpha": alpha,
            "ramp_ok": preset == "today",
            "suffix": f"_any-{preset}",
            **over,
        }
    )


def test_without_an_arm_the_command_is_what_it_always_was(tmp_path):
    # (lab controller, no control file, a stack that is off: validate() gets nothing)
    site, mdl, form = form_for(tmp_path, V3)
    assert form.chunk_accel_alpha == 0.005
    plain = command.render(site, mdl, form)
    assert alpha_flag(plain.argv) == "--phase-alpha=0.005"
    for alpha in (0.005, 0.01):
        _, _, chosen = form_for(tmp_path, {**V3, "chunk_accel_alpha": alpha})
        assert chosen.chunk_accel_alpha == alpha
    explicit = command.validate(site, mdl, {"prompt": PROMPT, **V3}, command.NO_ARM)
    assert command.render(site, mdl, explicit, command.NO_ARM).argv == plain.argv
    assert form.rollout_group.endswith("_rtc_accel3_demo")
    # and the quiet facts ask nothing either
    assert arm_facts.arm_of(facts()) == command.NO_ARM


@pytest.mark.parametrize("arm_alpha", [0.005, 0.01])
def test_an_empty_alpha_is_the_arm_controllers_alpha(tmp_path, arm_alpha):
    site, mdl, _ = form_for(tmp_path)
    arm = ARM(alpha=arm_alpha)
    form = command.validate(site, mdl, {"prompt": PROMPT, **V3}, arm)
    assert form.chunk_accel_alpha == arm_alpha
    preview = command.render(site, mdl, form, arm)
    assert alpha_flag(preview.argv) == f"--phase-alpha={arm_alpha:g}"
    assert f"alpha={arm_alpha:g}" in form.note


@pytest.mark.parametrize("arm_alpha, other", [(0.005, 0.01), (0.01, 0.005)])
def test_another_alpha_than_the_arm_controllers_is_refused_with_the_reason(
    tmp_path, arm_alpha, other
):
    site, mdl, _ = form_for(tmp_path)
    arm = ARM(alpha=arm_alpha)
    ok = command.validate(
        site, mdl, {"prompt": PROMPT, **V3, "chunk_accel_alpha": arm_alpha}, arm
    )
    assert ok.chunk_accel_alpha == arm_alpha
    with pytest.raises(command.FormError) as caught:
        command.validate(
            site, mdl, {"prompt": PROMPT, **V3, "chunk_accel_alpha": other}, arm
        )
    error = caught.value
    assert error.code == "chunk_accel_alpha_arm_mismatch"
    assert error.field == "chunk_accel_alpha"
    assert error.extra == {"alpha": f"{arm_alpha:g}"}
    assert "anyspeedup" in error.message and f"{arm_alpha:g}" in error.message
    # a continued run with an alpha of the earlier controller can fix it
    assert "make a new command" in error.message


def test_an_alpha_the_page_does_not_offer_keeps_its_own_refusal(tmp_path):
    site, mdl, _ = form_for(tmp_path)
    with pytest.raises(command.FormError) as caught:
        command.validate(
            site,
            mdl,
            {"prompt": PROMPT, **V3, "chunk_accel_alpha": 0.5},
            ARM(alpha=0.01),
        )
    assert caught.value.code == "chunk_accel_alpha_invalid"


def test_an_arm_alpha_the_phase_clock_has_no_tuning_for_is_refused(tmp_path):
    site, mdl, _ = form_for(tmp_path)
    with pytest.raises(command.FormError) as caught:
        command.validate(site, mdl, {"prompt": PROMPT, **V3}, ARM(alpha=0.02))
    assert caught.value.code == "chunk_accel_alpha_arm_mismatch"


def test_forms_without_the_phase_clock_are_not_touched_by_the_arm_alpha(tmp_path):
    site, mdl, _ = form_for(tmp_path)
    for body in (
        {},
        {"chunk_accel": True},
        {"chunk_accel": True, "chunk_accel_rule": "v2"},
        {"rtc": False, "inference_mode": "plain"},
    ):
        with_arm = command.validate(
            site, mdl, {"prompt": PROMPT, **body}, ARM(alpha=0.01)
        )
        without = command.validate(site, mdl, {"prompt": PROMPT, **body})
        assert with_arm == without, body
    # and a stray alpha without rule v3 is refused as it always was
    error = refused(tmp_path, {"chunk_accel_alpha": 0.01})
    assert error.code == "chunk_accel_v3_field_needs_v3"


def test_the_rendered_command_is_checked_against_the_arm_too(tmp_path):
    site, mdl, form = form_for(tmp_path, {**V3, "chunk_accel_alpha": 0.005})
    assert command.render(site, mdl, form).argv  # (nothing asked: as before)
    with pytest.raises(command.SelfCheckError):
        command.render(site, mdl, form, ARM(alpha=0.01))
    with pytest.raises(command.SelfCheckError):
        command.render(site, mdl, form, ARM(ramp_ok=True, alpha=0.01, suffix="_x"))


def test_the_golden_command_of_the_lab_is_unchanged(tmp_path):
    assert rendered(tmp_path, V3).argv == command.render(*form_for(tmp_path, V3)).argv


# --- the ramp rules (v1, v2) hard-code the lab controller's lag ---------------------------------


@pytest.mark.parametrize("rule", [None, "v1", "v2"])
def test_the_ramp_rules_are_refused_when_the_preset_is_not_today(tmp_path, rule):
    site, mdl, _ = form_for(tmp_path)
    body = {"prompt": PROMPT, "chunk_accel": True, "rtc": False}
    if rule:
        body["chunk_accel_rule"] = rule
    with pytest.raises(command.FormError) as caught:
        command.validate(site, mdl, body, ARM(ramp_ok=False, alpha=0.01))
    assert caught.value.code == "chunk_accel_rule_arm_preset"
    assert caught.value.field == "chunk_accel_rule"
    assert "0.2 s" in caught.value.message
    # today (or the lab's controller) is as it was
    assert command.validate(site, mdl, body, ARM(ramp_ok=True, alpha=0.005))
    assert command.validate(site, mdl, body)


def test_the_phase_clock_and_unaccelerated_forms_are_offered_with_any_preset(tmp_path):
    site, mdl, _ = form_for(tmp_path)
    arm = ARM(ramp_ok=False, alpha=0.01)
    assert command.validate(site, mdl, {"prompt": PROMPT, **V3}, arm)
    assert command.validate(site, mdl, {"prompt": PROMPT}, arm)
    assert command.validate(site, mdl, {"prompt": PROMPT, "rtc": False}, arm)


# --- the default rollout group says which controller ran ------------------------------------------


def test_the_default_group_gets_the_controllers_suffix_and_a_named_one_does_not(
    tmp_path,
):
    site, mdl, _ = form_for(tmp_path)
    plain = command.validate(site, mdl, {"prompt": PROMPT, **V3})
    today = command.validate(
        site, mdl, {"prompt": PROMPT, **V3}, ARM(suffix="_any-today")
    )
    s1 = command.validate(site, mdl, {"prompt": PROMPT, **V3}, ARM(suffix="_any-S1"))
    assert today.rollout_group == plain.rollout_group + "_any-today"
    assert s1.rollout_group == plain.rollout_group + "_any-S1"
    # a lab run and an anyspeedup run at "today" (both alpha 0.005) cannot mix
    assert today.rollout_group != plain.rollout_group
    named = command.validate(
        site,
        mdl,
        {"prompt": PROMPT, **V3, "rollout_group": "mine"},
        ARM(suffix="_any-S1"),
    )
    assert named.rollout_group == "mine"
    # the suffix reaches the command
    preview = command.render(site, mdl, s1, ARM(suffix="_any-S1"))
    assert f"--rollout-group={s1.rollout_group}" in preview.argv
    # and every default mode, not only rule v3
    rtc = command.validate(site, mdl, {"prompt": PROMPT}, ARM(suffix="_any-S1"))
    assert rtc.rollout_group.endswith("_rtc_demo_any-S1")


def test_a_default_group_that_no_longer_fits_is_refused_with_its_own_code(tmp_path):
    site, _, _ = form_for(tmp_path)
    from test_marvel0_command import model

    long = model(id="m" * 60)
    with pytest.raises(command.FormError) as caught:
        command.validate(site, long, {"prompt": PROMPT, **V3}, ARM(suffix="_any-today"))
    assert caught.value.code == "rollout_group_invalid"


# --- the warnings ---------------------------------------------------------------------------


def test_the_alpha_warning_names_the_tool_that_reads_the_anyspeedup_filter_back(
    tmp_path,
):
    site, mdl, form = form_for(tmp_path, V3)
    lab = command.render(site, mdl, form)
    codes = [w["code"] for w in lab.warnings]
    assert "chunk_accel_v3_alpha" in codes
    assert "set_clip.py" in next(
        w["text"] for w in lab.warnings if w["code"] == "chunk_accel_v3_alpha"
    )
    arm = ARM(alpha=0.005, anyspeedup=True)
    form = command.validate(site, mdl, {"prompt": PROMPT, **V3}, arm)
    got = command.render(site, mdl, form, arm).warnings
    codes = [w["code"] for w in got]
    assert (
        "chunk_accel_v3_alpha_anyspeedup" in codes
        and "chunk_accel_v3_alpha" not in codes
    )
    text = next(
        w["text"] for w in got if w["code"] == "chunk_accel_v3_alpha_anyspeedup"
    )
    assert "set_shaping.py --show" in text and "set_clip.py" not in text


@pytest.mark.parametrize("problem", ["changed", "foreign", "unknown"])
def test_the_problem_warning_tells_what_to_do(problem):
    warning = command.arm_warning(problem)
    assert warning["code"] == "arm_controller_problem"
    assert "Stop A, change the control file, start A" in warning["text"]
    assert warning["params"] == {"problem": problem}


# --- arm_facts: the facts, as the command asks for them -------------------------------------------


def test_the_facts_turn_into_what_the_command_asks():
    got = arm_facts.arm_of(running("S1"))
    assert got == ARM(alpha=0.01, ramp_ok=False, suffix="_any-S1", anyspeedup=True)
    assert arm_facts.arm_of(running("today")) == ARM(
        alpha=0.005, ramp_ok=True, suffix="_any-today", anyspeedup=True
    )
    # an A that does not match: no alpha is forced, the problem is the start's to refuse
    changed = running("S1", problem="changed", alpha=None, status="changed")
    assert arm_facts.arm_of(changed).alpha is None
    assert arm_facts.problem_of(changed) == "changed"
    assert arm_facts.problem_of(facts(problem="nonsense")) is None
    # odd values are not trusted
    assert arm_facts.arm_of(None) == command.NO_ARM
    assert arm_facts.arm_of(facts(alpha=True)).alpha is None
    assert arm_facts.arm_of(facts(alpha=float("nan"))).alpha is None
    assert arm_facts.arm_of(facts(suffix=5)).suffix == ""


def test_the_running_controller_is_the_pages_own_a_only():
    assert arm_facts.running_of(running("S1")) == ("anyspeedup", "S1")
    assert arm_facts.running_of(facts()) == (None, None)
    assert arm_facts.running_of(
        facts(running={"controller": "lab", "preset": None})
    ) == ("lab", None)
    assert arm_facts.running_of(facts(running={"controller": "x", "preset": 3})) == (
        None,
        None,
    )


def test_an_unreadable_stack_document_is_answered_from_the_control_file(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(arm_facts, "from_document", lambda fresh=False: None)
    monkeypatch.setenv(cs.ENV, str(tmp_path / "absent.toml"))
    cs.clear_cache()
    assert arm_facts.read() == arm_facts.from_file()
    assert arm_facts.read()["problem"] is None
    write_control(
        tmp_path,
        monkeypatch,
        f'arm_controller = "anyspeedup"\narm_preset = "S1"\nanyspeedup_root = "{ANY_ROOT}"\n',
    )
    got = arm_facts.read()
    assert got["problem"] == "unknown" and got["status"] == "unknown"
    assert got["alpha"] is None and got["ramp_ok"] is False
    write_control(tmp_path, monkeypatch, 'arm_controller = "lab"\n')
    assert arm_facts.read()["problem"] is None


def test_the_document_is_read_from_the_stack_api(monkeypatch):
    from levi.marvel0 import stack_api

    seen = []

    class Sup:
        def view(self):
            seen.append("fresh")
            return {"act": {"arm": running("S1")}}

    monkeypatch.setattr(stack_api, "stack_view", lambda: {"act": {"arm": facts()}})
    monkeypatch.setattr(stack_api, "supervisor", lambda: Sup())
    assert arm_facts.read()["status"] == "idle"
    assert arm_facts.read(fresh=True)["status"] == "match" and seen == ["fresh"]
    monkeypatch.setattr(stack_api, "stack_view", lambda: {"act": {"error": "x"}})
    assert arm_facts.from_document() is None


# --- the preview route and the hosted run follow the A that runs --------------------------------


@pytest.fixture
def route(tmp_path, monkeypatch):
    """The preview route on a fake site, with no control file unless a test writes one."""
    make_checkpoints(tmp_path)
    monkeypatch.setenv("LEVI_MARVEL0_SITE", str(write_site(tmp_path)))
    monkeypatch.setenv(cs.ENV, str(tmp_path / "no-control.toml"))
    marvel0_site.clear_cache()
    models.cache_clear()
    monkeypatch.setattr(arm_facts, "from_document", lambda fresh=False: None)
    app = FastAPI()
    app.include_router(catalog_api.router)
    yield TestClient(app), tmp_path
    marvel0_site.clear_cache()
    models.cache_clear()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    machine = RunMachine(tmp_path, monkeypatch)
    yield machine
    machine.close()


def write_control(tmp_path, monkeypatch, arm):
    path = tmp_path / "control.toml"
    body = GOOD.replace("marker_wait_s = 60\n", "marker_wait_s = 60\n" + arm, 1)
    path.write_text(body)
    path.chmod(0o644)
    monkeypatch.setenv(cs.ENV, str(path))
    cs.clear_cache()
    return path


def with_facts(monkeypatch, value):
    monkeypatch.setattr(arm_facts, "read", lambda fresh=False: value)


@pytest.mark.parametrize("preset, alpha", [("today", 0.005), ("S1", 0.01)])
def test_the_preview_route_forces_the_alpha_of_the_running_anyspeedup_a(
    route, monkeypatch, preset, alpha
):
    client, tmp_path = route
    with_facts(monkeypatch, running(preset))
    reply = preview(client, **V3)
    assert reply.status_code == 200, reply.text
    argv = reply.json()["argv"]
    assert alpha_flag(argv) == f"--phase-alpha={alpha:g}"
    assert (
        f"--rollout-group=pi05_fr3_all_step49999_rtc_accel3_demo_any-{preset}" in argv
    )
    other = 0.01 if alpha == 0.005 else 0.005
    reply = preview(client, **V3, chunk_accel_alpha=other)
    assert reply.status_code == 422
    body = reply.json()
    detail = body.get("detail", body)
    assert detail["code"] == "chunk_accel_alpha_arm_mismatch"
    assert detail["field"] == "chunk_accel_alpha" and detail["alpha"] == f"{alpha:g}"
    assert str(tmp_path) not in reply.text
    assert preview(client, **V3, chunk_accel_alpha=alpha).status_code == 200


def test_the_preview_route_refuses_the_ramp_rules_for_s1_and_not_for_today(
    route, monkeypatch
):
    client, _ = route
    with_facts(monkeypatch, running("S1"))
    for rule in ("v1", "v2"):
        reply = preview(client, chunk_accel=True, rtc=False, chunk_accel_rule=rule)
        assert reply.status_code == 422
        detail = reply.json().get("detail", reply.json())
        assert detail["code"] == "chunk_accel_rule_arm_preset"
        assert detail["field"] == "chunk_accel_rule"
    assert preview(client, chunk_accel=True, rtc=False).status_code == 422
    with_facts(monkeypatch, running("today"))
    assert preview(client, chunk_accel=True, rtc=False).status_code == 200


@pytest.mark.parametrize("problem", ["changed", "foreign", "unknown"])
def test_the_preview_warns_when_rule_v3_would_be_refused_at_the_start(
    route, monkeypatch, problem
):
    client, _ = route
    with_facts(
        monkeypatch,
        facts(
            configured={"controller": "anyspeedup", "preset": "S1"},
            enabled=True,
            status=problem,
            problem=problem,
            ramp_ok=False,
        ),
    )
    reply = preview(client, **V3)
    assert reply.status_code == 200
    got = {w["code"]: w for w in reply.json()["warnings"]}
    assert got["arm_controller_problem"]["params"] == {"problem": problem}
    # nothing is forced for a controller that is not known: the alpha stays free
    assert preview(client, **V3, chunk_accel_alpha=0.01).status_code == 200
    # no warning for a form without rule v3
    assert "arm_controller_problem" not in {
        w["code"] for w in preview(client).json()["warnings"]
    }


@pytest.mark.parametrize("arm", ["", 'arm_controller = "lab"\n', None])
def test_the_preview_route_leaves_the_choice_alone_without_the_anyspeedup_controller(
    route, monkeypatch, arm
):
    client, tmp_path = route
    if arm is not None:
        write_control(tmp_path, monkeypatch, arm)
    # (the real facts: no stack document in this process, the control file alone)
    assert alpha_flag(preview(client, **V3).json()["argv"]) == "--phase-alpha=0.005"
    reply = preview(client, **V3, chunk_accel_alpha=0.01)
    assert reply.status_code == 200
    assert alpha_flag(reply.json()["argv"]) == "--phase-alpha=0.01"
    assert "_any-" not in " ".join(reply.json()["argv"])


def test_the_hosted_run_forces_what_the_running_a_asks(rig):
    rig.arm_now = running("S1")
    body = rig.form(**V3)
    with pytest.raises(run.FormRefuse) as caught:
        rig.sup._form(rig.site, {**body, "chunk_accel_alpha": 0.005})
    assert caught.value.code == "chunk_accel_alpha_arm_mismatch"
    _, form, preview_ = rig.sup._form(rig.site, body)
    assert form.chunk_accel_alpha == 0.01
    assert alpha_flag(preview_.argv) == "--phase-alpha=0.01"
    # the lab's A: as it was
    rig.arm_now = facts()
    _, form, _ = rig.sup._form(rig.site, {**body, "chunk_accel_alpha": 0.005})
    assert form.chunk_accel_alpha == 0.005


def test_a_start_looks_at_the_stack_again_but_a_challenge_may_use_the_cache(rig):
    rig.arm_now = running("today")
    form = rig.form(**V3)
    challenge = rig.sup.challenge({"purpose": "start", "form": form})
    assert rig.arm_asked == [False]
    rig.sup.start(
        {
            "request_id": rig.rid(),
            "form": form,
            "challenge": challenge["challenge"],
            "confirm": challenge["confirm"],
            "attest": dict(ATTEST),
        }
    )
    assert rig.arm_asked == [False, True]


def test_rule_v3_is_refused_at_the_start_when_the_a_that_runs_is_not_the_files(rig):
    for problem in ("changed", "foreign", "unknown"):
        rig.arm_now = facts(
            configured={"controller": "anyspeedup", "preset": "S1"},
            enabled=True,
            status=problem,
            problem=problem,
            ramp_ok=False,
        )
        form = rig.form(**V3)
        with pytest.raises(run.Refuse) as caught:
            rig.sup.challenge({"purpose": "start", "form": form})
        assert caught.value.code == "preflight_failed", problem
        assert caught.value.extra["first"] == "arm_controller_matches", problem
        row = next(
            r
            for r in caught.value.extra["checks"]
            if r["code"] == "arm_controller_matches"
        )
        assert row["level"] == "refuse" and problem in row["text"]
        assert "stop A, change the control file, start A" in row["text"]
    assert rig.spawned == []
    # without rule v3 the same A starts a client as it always could
    rig.arm_now = facts(
        configured={"controller": "anyspeedup", "preset": "S1"},
        enabled=True,
        status="foreign",
        problem="foreign",
        ramp_ok=False,
    )
    challenge = rig.sup.challenge({"purpose": "start", "form": rig.form()})
    rows = {r["code"] for r in challenge["preflight"]}
    assert "arm_controller_matches" not in rows


def test_the_check_is_passed_and_listed_for_a_matching_a(rig):
    rig.arm_now = running("today")
    challenge = rig.sup.challenge({"purpose": "start", "form": rig.form(**V3)})
    row = next(
        r for r in challenge["preflight"] if r["code"] == "arm_controller_matches"
    )
    assert row["level"] == "ok"


# --- the run records which controller it ran with --------------------------------------------------


def test_the_run_record_and_view_say_which_arm_controller_the_page_started(rig):
    rig.arm_now = running("S1")
    answer = rig.start(**V3)
    assert answer["created"] is True
    rec = rig.record()
    assert (rec["arm_controller"], rec["arm_preset"]) == ("anyspeedup", "S1")
    assert rec["chunk_accel_alpha"] == 0.01
    # the default group is told apart, in the record, the session file and the confirm phrase
    assert rec["group"].endswith("_any-S1")
    assert rec["session_file"].startswith(rec["group"] + "__")
    view = rig.view()
    assert (view["run"]["arm_controller"], view["run"]["arm_preset"]) == (
        "anyspeedup",
        "S1",
    )


def test_the_confirm_phrase_names_the_suffixed_group(rig):
    rig.arm_now = running("today")
    form = rig.form(**V3)
    challenge = rig.sup.challenge({"purpose": "start", "form": form})
    assert challenge["group"].endswith("_any-today")
    assert challenge["group"] in challenge["confirm"]
    assert run._group_of(form, "_any-today") == challenge["group"]
    assert run._group_of(form) == challenge["group"].removesuffix("_any-today")
    assert run._group_of({**form, "rollout_group": "mine"}, "_any-today") == "mine"


def test_a_client_started_beside_an_a_the_page_did_not_start_claims_no_controller(rig):
    rig.arm_now = facts(status="foreign")
    rig.start()
    rec = rig.record()
    assert rec["arm_controller"] is None and rec["arm_preset"] is None
    assert rec["group"].endswith("_demo") or "_any-" not in rec["group"]
    assert rig.view()["run"]["arm_controller"] is None


def test_a_lab_a_is_recorded_as_the_lab_without_a_suffix(rig):
    rig.arm_now = facts(
        configured={"controller": "lab", "preset": None},
        enabled=True,
        running={"controller": "lab", "preset": None},
        status="match",
    )
    rig.start()
    rec = rig.record()
    assert (rec["arm_controller"], rec["arm_preset"]) == ("lab", None)
    assert "_any-" not in rec["group"]


def test_a_record_from_before_has_no_arm_in_its_view(rig):
    rig.start()
    from levi.marvel0 import record

    folder = rig.folder
    old = {
        k: v
        for k, v in rig.record().items()
        if k not in ("arm_controller", "arm_preset")
    }
    with record.locked(folder):
        record.write_record(folder, old)
    run_view = rig.view()["run"]
    assert run_view["arm_controller"] is None and run_view["arm_preset"] is None


def test_a_host_reads_the_facts_through_arm_facts_by_default():
    assert run.Host().arm_facts is arm_facts.read
