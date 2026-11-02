"""The arm controller choice and the client command (``levi/marvel0/command.py``,
``catalog_api.py``, ``run.py``): when the stack starts the anyspeedup controller,
rule v3's ``chunk_accel_alpha`` is the alpha of its preset (an empty field is that
alpha, another one is refused with a reason); with the lab's controller, or
without a control file, nothing is forced and the command is what it always was."""

import pytest
from arm_controller_helpers import ANY_ROOT
from fastapi import FastAPI
from fastapi.testclient import TestClient
from site_sample import make_checkpoints, write_site
from test_marvel0_catalog_route import preview
from test_marvel0_command import PROMPT, form_for, refused, rendered
from test_marvel0_deploy import forbid_real_machine  # noqa: F401 - the guard
from test_marvel0_run import Machine as RunMachine
from test_marvel0_stack_act_control import GOOD

from levi.marvel0 import catalog_api, command, models, run
from levi.marvel0 import control_site as cs
from levi.marvel0 import site as marvel0_site


@pytest.fixture(autouse=True)
def fresh_cache():
    cs.clear_cache()
    yield
    cs.clear_cache()


# --- the client command ---------------------------------------------------------------------

V3 = {"chunk_accel": True, "chunk_accel_rule": "v3"}


def alpha_flag(argv):
    return next(t for t in argv if t.startswith("--phase-alpha="))


def test_without_an_arm_alpha_the_command_is_what_it_always_was(tmp_path):
    # (lab controller, no control file, a stack that is off: validate() gets None)
    site, mdl, form = form_for(tmp_path, V3)
    assert form.chunk_accel_alpha == 0.005
    plain = command.render(site, mdl, form)
    assert alpha_flag(plain.argv) == "--phase-alpha=0.005"
    for alpha in (0.005, 0.01):
        _, _, chosen = form_for(tmp_path, {**V3, "chunk_accel_alpha": alpha})
        assert chosen.chunk_accel_alpha == alpha
    explicit = command.validate(site, mdl, {"prompt": PROMPT, **V3}, None)
    assert command.render(site, mdl, explicit, None).argv == plain.argv


@pytest.mark.parametrize("arm_alpha", [0.005, 0.01])
def test_an_empty_alpha_is_the_arm_controllers_alpha(tmp_path, arm_alpha):
    site, mdl, _ = form_for(tmp_path)
    form = command.validate(site, mdl, {"prompt": PROMPT, **V3}, arm_alpha)
    assert form.chunk_accel_alpha == arm_alpha
    preview = command.render(site, mdl, form, arm_alpha)
    assert alpha_flag(preview.argv) == f"--phase-alpha={arm_alpha:g}"
    assert f"alpha={arm_alpha:g}" in form.note


@pytest.mark.parametrize("arm_alpha, other", [(0.005, 0.01), (0.01, 0.005)])
def test_another_alpha_than_the_arm_controllers_is_refused_with_the_reason(
    tmp_path, arm_alpha, other
):
    site, mdl, _ = form_for(tmp_path)
    ok = command.validate(
        site, mdl, {"prompt": PROMPT, **V3, "chunk_accel_alpha": arm_alpha}, arm_alpha
    )
    assert ok.chunk_accel_alpha == arm_alpha
    with pytest.raises(command.FormError) as caught:
        command.validate(
            site, mdl, {"prompt": PROMPT, **V3, "chunk_accel_alpha": other}, arm_alpha
        )
    error = caught.value
    assert error.code == "chunk_accel_alpha_arm_mismatch"
    assert error.field == "chunk_accel_alpha"
    assert error.extra == {"alpha": f"{arm_alpha:g}"}
    assert "anyspeedup" in error.message and f"{arm_alpha:g}" in error.message


def test_an_alpha_the_page_does_not_offer_keeps_its_own_refusal(tmp_path):
    site, mdl, _ = form_for(tmp_path)
    with pytest.raises(command.FormError) as caught:
        command.validate(
            site, mdl, {"prompt": PROMPT, **V3, "chunk_accel_alpha": 0.5}, 0.01
        )
    assert caught.value.code == "chunk_accel_alpha_invalid"


def test_an_arm_alpha_the_phase_clock_has_no_tuning_for_is_refused(tmp_path):
    site, mdl, _ = form_for(tmp_path)
    with pytest.raises(command.FormError) as caught:
        command.validate(site, mdl, {"prompt": PROMPT, **V3}, 0.02)
    assert caught.value.code == "chunk_accel_alpha_arm_mismatch"


def test_forms_without_the_phase_clock_are_not_touched_by_the_arm_alpha(tmp_path):
    site, mdl, _ = form_for(tmp_path)
    for body in (
        {},
        {"chunk_accel": True},
        {"chunk_accel": True, "chunk_accel_rule": "v2"},
        {"rtc": False, "inference_mode": "plain"},
    ):
        with_arm = command.validate(site, mdl, {"prompt": PROMPT, **body}, 0.01)
        without = command.validate(site, mdl, {"prompt": PROMPT, **body})
        assert with_arm == without, body
    # and a stray alpha without rule v3 is refused as it always was
    error = refused(tmp_path, {"chunk_accel_alpha": 0.01})
    assert error.code == "chunk_accel_v3_field_needs_v3"


def test_the_rendered_command_is_checked_against_the_arm_alpha_too(tmp_path):
    site, mdl, form = form_for(tmp_path, {**V3, "chunk_accel_alpha": 0.005})
    assert command.render(site, mdl, form).argv  # (no arm alpha: as before)
    with pytest.raises(command.SelfCheckError):
        command.render(site, mdl, form, 0.01)


def test_the_golden_command_of_the_lab_is_unchanged(tmp_path):
    assert rendered(tmp_path, V3).argv == command.render(*form_for(tmp_path, V3)).argv


# --- the preview route and the hosted run follow the control file ------------------------------


@pytest.fixture
def route(tmp_path, monkeypatch):
    """The preview route on a fake site, with no control file unless a test writes one."""
    make_checkpoints(tmp_path)
    monkeypatch.setenv("LEVI_MARVEL0_SITE", str(write_site(tmp_path)))
    monkeypatch.setenv(cs.ENV, str(tmp_path / "no-control.toml"))
    marvel0_site.clear_cache()
    models.cache_clear()
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


def test_the_preview_route_forces_the_alpha_of_the_anyspeedup_preset(
    route,
    monkeypatch,
):
    client, tmp_path = route
    write_control(
        tmp_path,
        monkeypatch,
        f'arm_controller = "anyspeedup"\narm_preset = "S1"\nanyspeedup_root = "{ANY_ROOT}"\n',
    )
    reply = preview(client, **V3)
    assert reply.status_code == 200, reply.text
    assert alpha_flag(reply.json()["argv"]) == "--phase-alpha=0.01"
    reply = preview(client, **V3, chunk_accel_alpha=0.01)
    assert reply.status_code == 200
    reply = preview(client, **V3, chunk_accel_alpha=0.005)
    assert reply.status_code == 422
    body = reply.json()
    detail = body.get("detail", body)
    assert detail["code"] == "chunk_accel_alpha_arm_mismatch"
    assert detail["field"] == "chunk_accel_alpha" and detail["alpha"] == "0.01"
    assert str(tmp_path) not in reply.text


def test_the_preview_route_with_the_today_preset_refuses_the_other_alpha(
    route,
    monkeypatch,
):
    client, tmp_path = route
    write_control(
        tmp_path,
        monkeypatch,
        f'arm_controller = "anyspeedup"\narm_preset = "today"\nanyspeedup_root = "{ANY_ROOT}"\n',
    )
    assert alpha_flag(preview(client, **V3).json()["argv"]) == "--phase-alpha=0.005"
    assert preview(client, **V3, chunk_accel_alpha=0.01).status_code == 422


@pytest.mark.parametrize("arm", ["", 'arm_controller = "lab"\n', None])
def test_the_preview_route_leaves_the_choice_alone_without_the_anyspeedup_controller(
    route,
    monkeypatch,
    arm,
):
    client, tmp_path = route
    if arm is not None:
        write_control(tmp_path, monkeypatch, arm)
    assert alpha_flag(preview(client, **V3).json()["argv"]) == "--phase-alpha=0.005"
    reply = preview(client, **V3, chunk_accel_alpha=0.01)
    assert reply.status_code == 200
    assert alpha_flag(reply.json()["argv"]) == "--phase-alpha=0.01"


def test_the_hosted_run_asks_the_same_question(rig):
    body = rig.form(**V3, rtc=False)
    # lab: the form is as it was, whatever the alpha
    rig.arm_alpha = None
    _, form, _ = rig.sup._form(rig.site, {**body, "chunk_accel_alpha": 0.005})
    assert form.chunk_accel_alpha == 0.005
    rig.arm_alpha = 0.01
    with pytest.raises(run.FormRefuse) as caught:
        rig.sup._form(rig.site, {**body, "chunk_accel_alpha": 0.005})
    assert caught.value.code == "chunk_accel_alpha_arm_mismatch"
    _, form, preview_ = rig.sup._form(rig.site, body)
    assert form.chunk_accel_alpha == 0.01
    assert alpha_flag(preview_.argv) == "--phase-alpha=0.01"


def test_the_default_arm_alpha_of_a_host_is_read_from_the_control_file(
    tmp_path, monkeypatch
):
    assert run.Host().arm_alpha is run._arm_alpha
    monkeypatch.setenv(cs.ENV, str(tmp_path / "absent.toml"))
    cs.clear_cache()
    assert run._arm_alpha() is None
    write_control(
        tmp_path,
        monkeypatch,
        f'arm_controller = "anyspeedup"\narm_preset = "S1"\nanyspeedup_root = "{ANY_ROOT}"\n',
    )
    assert run._arm_alpha() == 0.01
    monkeypatch.setattr(cs, "arm_alpha", lambda: 1 / 0)
    assert run._arm_alpha() is None, "what cannot be read forces nothing"
