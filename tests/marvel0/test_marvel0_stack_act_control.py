"""The control file (``levi/marvel0/control_site.py``): a missing file is a normal
state that switches the writes off, a present one is a closed schema whose
switches default to off, and nothing it says reaches a page as a path."""

import json
import os
import tomllib
from pathlib import Path

import pytest
from test_marvel0_deploy import forbid_real_machine  # noqa: F401 - the guard

from levi.marvel0 import control_site as cs

GOOD = """\
schema = "levi.marvel0.control.v1"
[stack]
enabled = true
franka_control = "/opt/rig/franka_control"
robot_ip = "10.42.0.157"
com_port = "/dev/serial/by-id/usb-X-if00-port0"
server_port = 5000
reset_joint_target = [-0.8063, -0.0623, 0.913, -2.4036, 0.058, 2.3011, 1.5844]
marker_wait_s = 60
[client]
enabled = true
"""


@pytest.fixture(autouse=True)
def fresh_cache():
    cs.clear_cache()
    yield
    cs.clear_cache()


def write(tmp_path, text, mode=0o644):
    path = tmp_path / "control.toml"
    path.write_text(text)
    path.chmod(mode)
    return path


def read_file(tmp_path, text, **kw):
    path = write(tmp_path, text, **kw)
    return cs.read({cs.ENV: str(path)})


def codes(loaded):
    return {(e["field"], e["code"]) for e in loaded.errors}


def test_the_template_is_a_valid_file_and_switches_nothing_on():
    site = cs.parse(tomllib.loads(cs.TEMPLATE))
    assert site.stack.enabled is False and site.client.enabled is False
    assert site.stack.server_port == 5000 and site.stack.marker_wait_s == 60.0


def test_a_missing_file_is_none_and_not_an_error(tmp_path):
    env = {cs.ENV: str(tmp_path / "absent.toml")}
    assert cs.load(env) is None
    loaded = cs.read(env)
    assert loaded.state == "missing"
    view = cs.view(loaded)
    assert view["stack_enabled"] is False and view["client_enabled"] is False
    assert view["code"] == "control.missing"


def test_a_valid_file_gives_the_values_and_the_switches(tmp_path):
    loaded = read_file(tmp_path, GOOD)
    assert loaded.state == "ok"
    site = loaded.site
    assert site.stack.enabled and site.client.enabled
    assert site.stack.franka_control == "/opt/rig/franka_control"
    assert site.stack.health_file == "/opt/rig/franka_control/run/fr3_health.json"
    assert (
        site.stack.reset_joint_target[0] == -0.8063
        and len(site.stack.reset_joint_target) == 7
    )
    assert site.origin == "env"
    assert cs.view(loaded)["code"] == "control.enabled"


def test_the_switches_default_to_off(tmp_path):
    loaded = read_file(tmp_path, 'schema = "levi.marvel0.control.v1"\n')
    assert loaded.state == "ok"
    assert loaded.site.stack.enabled is False and loaded.site.client.enabled is False
    assert cs.view(loaded)["code"] == "control.disabled"


def test_a_stack_switched_on_needs_every_machine_value(tmp_path):
    text = 'schema = "levi.marvel0.control.v1"\n[stack]\nenabled = true\n'
    loaded = read_file(tmp_path, text)
    assert loaded.state == "invalid"
    assert codes(loaded) == {
        ("stack.franka_control", "missing"),
        ("stack.robot_ip", "missing"),
        ("stack.com_port", "missing"),
        ("stack.reset_joint_target", "missing"),
    }
    assert cs.view(loaded)["stack_enabled"] is False
    with pytest.raises(cs.ControlSiteError):
        cs.load({cs.ENV: str(tmp_path / "control.toml")})


@pytest.mark.parametrize(
    "change, field, code",
    [
        ('schema = "levi.marvel0.control.v1"', "schema", "range"),
        ("enabled = true", "stack.enabled", None),
        (
            'franka_control = "/opt/rig/franka_control"',
            "stack.franka_control",
            "not_absolute",
        ),
        ('robot_ip = "10.42.0.157"', "stack.robot_ip", "range"),
        ('com_port = "/dev/serial/by-id/usb-X-if00-port0"', "stack.com_port", "range"),
        ("server_port = 5000", "stack.server_port", "range"),
        ("marker_wait_s = 60", "stack.marker_wait_s", "range"),
        ("reset_joint_target = [", "stack.reset_joint_target", None),
    ],
)
def test_a_bad_value_is_named_by_field_and_code(tmp_path, change, field, code):
    bad = {
        'schema = "levi.marvel0.control.v1"': 'schema = "levi.marvel0.control.v2"',
        "enabled = true": 'enabled = "yes"',
        'franka_control = "/opt/rig/franka_control"': 'franka_control = "relative/dir"',
        'robot_ip = "10.42.0.157"': 'robot_ip = "127.0.0.1"',
        'com_port = "/dev/serial/by-id/usb-X-if00-port0"': 'com_port = "/tmp/pty"',
        "server_port = 5000": "server_port = 8000",
        "marker_wait_s = 60": "marker_wait_s = 5",
        "reset_joint_target = [": "reset_joint_target = [9, ",
    }[change]
    loaded = read_file(tmp_path, GOOD.replace(change, bad, 1))
    assert loaded.state == "invalid"
    assert field in {f for f, _ in codes(loaded)}
    if code:
        assert (field, code) in codes(loaded)


@pytest.mark.parametrize("port", [5001, 5100, 7470, 8000, 80, 70000, True])
def test_the_servers_port_is_never_a_policy_or_learner_port(tmp_path, port):
    loaded = read_file(
        tmp_path,
        GOOD.replace("server_port = 5000", f"server_port = {str(port).lower()}"),
    )
    assert ("stack.server_port", "range") in codes(loaded) or (
        "stack.server_port",
        "type",
    ) in codes(loaded)


def test_an_unknown_key_or_section_is_an_error(tmp_path):
    top = GOOD.replace('v1"\n', 'v1"\nextra = 1\n', 1)
    loaded = read_file(tmp_path, top + "[dual]\nenabled = true\n")
    assert {("extra", "unknown_key"), ("dual", "unknown_key")} <= codes(loaded)
    loaded = read_file(tmp_path, GOOD.replace("[client]", "[client]\nlevel = 3"))
    assert ("client.level", "unknown_key") in codes(loaded)


@pytest.mark.parametrize("bad", ["a b", "a%b", "a$b"])
def test_a_path_that_systemd_would_misread_is_refused(tmp_path, bad):
    loaded = read_file(
        tmp_path, GOOD.replace("/opt/rig/franka_control", f"/opt/{bad}/fc")
    )
    assert ("stack.franka_control", "unsafe") in codes(loaded)


def test_the_file_must_be_the_owners_and_not_writable_by_others(tmp_path):
    assert read_file(tmp_path, GOOD, mode=0o664).errors == (
        {"field": "file", "code": "writable"},
    )
    path = tmp_path / "control.toml"
    path.chmod(0o644)
    link = tmp_path / "link.toml"
    link.symlink_to(path)
    assert cs.read({cs.ENV: str(link)}).errors == (
        {"field": "file", "code": "not_regular"},
    )
    assert read_file(tmp_path, "not toml [[[").errors == (
        {"field": "file", "code": "not_toml"},
    )
    assert read_file(tmp_path, "x = '" + "a" * 20000 + "'").errors == (
        {"field": "file", "code": "too_large"},
    )


def test_the_view_holds_no_path_or_value_of_the_file(tmp_path):
    loaded = read_file(tmp_path, GOOD)
    text = json.dumps(cs.view(loaded))
    for secret in ("/opt/rig", "10.42.0.157", "serial", "0.8063"):
        assert secret not in text
    bad = read_file(tmp_path, GOOD.replace("10.42.0.157", "999.1.1.1"))
    assert "999" not in json.dumps(cs.view(bad))


def test_the_file_is_parsed_again_only_when_it_changed(tmp_path, monkeypatch):
    path = write(tmp_path, GOOD)
    env = {cs.ENV: str(path)}
    first = cs.read(env)
    assert cs.read(env) is first
    path.write_text(GOOD.replace("enabled = true", "enabled = false", 1))
    path.chmod(0o644)
    os.utime(path, (1, 1))
    assert cs.read(env).site.stack.enabled is False


def test_the_default_place_is_in_the_main_checkout(tmp_path):
    main = tmp_path / "main"
    (main / ".git").mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    (work / ".git").write_text("gitdir: elsewhere")
    path, origin = cs.find_path({}, lambda: [work, main])
    assert (
        origin == "main_checkout" and path == main / "lab" / "marvel0" / "control.toml"
    )
    path, origin = cs.find_path({cs.ENV: "~/x/control.toml"}, lambda: [main])
    assert origin == "env" and path == Path("~/x/control.toml").expanduser()
    assert (
        cs.read({cs.ENV: "relative/control.toml"}).errors[0]["code"] == "not_absolute"
    )


def test_the_setting_is_documented_in_both_languages():
    root = Path(cs.__file__).resolve().parents[2]
    doc = (root / "docs" / "MARVEL0_STACK.md").read_text(encoding="utf-8")
    assert cs.ENV in doc and "control.toml" in doc


# What the fixes of the verifiers' residual findings added; each is named in the
# document, in both halves (English first, Chinese after the line "---").
DOCUMENTED = (
    "b/probe",
    "can_probe_b",
    "b.probe_stale",
    "b_probe_started",
    "a.sockets_draining",
    "hardware_active",
    "stop_c_first",
    "stop_c_unknown",
    "c.health_lost",
    "a_lost",
    "timeout_killed",
    "255",
    "GRIPPER_END_WAIT_S",
    "stop_left_processes",
    "stopping",
)


def test_the_document_names_what_the_residual_fixes_added_in_both_languages():
    root = Path(cs.__file__).resolve().parents[2]
    doc = (root / "docs" / "MARVEL0_STACK.md").read_text(encoding="utf-8")
    english, chinese = doc.split("\n---\n", 1)
    for term in DOCUMENTED:
        assert term in english, f"{term} is not in the English half"
        assert term in chinese, f"{term} is not in the Chinese half"
