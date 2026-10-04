"""Nothing in LEVI names one machine: the live service's defaults are
relative to its home or to the checkout, a machine's own folders must be
configured, ``levi live doctor`` and ``preflight`` say what is missing and
how to fix it, and an unusable GPU lock file is never passed over silently."""

import json
import os
import re
import stat
from pathlib import Path

import pytest

from levi.live import cli, gpumgr
from levi.live import config as live_config

PROJECT = Path(__file__).resolve().parents[1]
# A real home or machine folder: /home/<name>, /Users/<name>, /root/, /mnt/,
# ~/work/ (the maintainer's layout). Placeholders such as /home/<user>/ and
# ${HOME} are fine: the character after the prefix must start a real name.
DEVELOPER_PATH = re.compile(
    r"(?<![\w<{$.])(?:/(?:home|Users)/[A-Za-z0-9._-]+|/root/|/mnt/|~/work/)"
)


SCANNED = (
    ".py",
    ".sh",
    ".toml",
    ".json",
    ".md",
    ".ts",
    ".tsx",
    ".yml",
    ".yaml",
    ".mjs",
)


def shipped_files():
    """Everything a release carries except the tests: code, scripts,
    integrations, frontend, docs, CI and configuration files."""
    for folder in (
        "levi",
        "backend",
        "scripts",
        "integrations",
        "src",
        "docs",
        ".github",
    ):
        for path in (PROJECT / folder).rglob("*"):
            parts = set(path.relative_to(PROJECT).parts)
            if parts & {"node_modules", ".venv", "__pycache__", "tests", "__tests__"}:
                continue
            if path.is_file() and path.suffix in SCANNED:
                yield path
    for path in PROJECT.glob("*"):
        if path.is_file() and (
            path.suffix in SCANNED or path.name in (".env.example", "Dockerfile")
        ):
            yield path


def test_no_shipped_file_hardcodes_a_developer_path():
    """AGENTS.md: never hardcode a developer path. Placeholders such as
    ``/home/<someone>/`` are fine; a real home directory is not."""
    found = [
        f"{path.relative_to(PROJECT)}:{n}: {line.strip()[:100]}"
        for path in shipped_files()
        for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1)
        if DEVELOPER_PATH.search(line)
    ]
    assert not found, "\n".join(found)


def test_the_scan_would_catch_one():
    for found in (
        'x = "/home/alice/work/data"',
        "the default is /home/alice",
        "/Users/bob/Library/x",
        "cd /root/levi",
        "data in /mnt/nvme/rollouts",
        "cd ~/work/wenkai/LEVI",
    ):
        assert DEVELOPER_PATH.search(found), found
    for fine in (
        "``/home/<someone>/...``",
        "/home/<user>/data",
        "${HOME}/x",
        "~/.levi-live/workspace",
        "/path/to/rollouts",
        "src/app/home/page.tsx",
    ):
        assert not DEVELOPER_PATH.search(fine), fine
    names = {p.relative_to(PROJECT).as_posix() for p in shipped_files()}
    assert {
        "README.md",
        "INSTALL.md",
        "docs/LIVE.md",
        ".github/workflows/test.yml",
    } <= names
    assert "eslint.config.mjs" in names


def test_live_defaults_are_relative_to_the_home_or_the_checkout_or_empty():
    c = live_config.Config()
    assert c.service.workspace == "~/.levi-live/workspace"
    assert c.workspace == Path.home() / ".levi-live/workspace"
    assert c.watch.roots == [] and c.fr3.health_file == "" and c.gpu.lock_file == ""
    assert c.vllm_script == PROJECT / "scripts/vllm/serve.sh"
    assert c.vllm_pid_dir == Path.home() / ".levi-live/vllm"
    assert c.gpu.lock_unavailable == "continue"
    # The rendered defaults read back (no roots is a valid file, not an error).
    assert "roots = []" in live_config.render()


def test_explicit_settings_win_over_every_default(tmp_path):
    """An existing live.toml that names every path keeps working unchanged."""
    toml = tmp_path / "live.toml"
    toml.write_text(
        f"""[service]
workspace = "{tmp_path / "ws"}"
[watch]
roots = ["{tmp_path / "rollouts"}"]
[fr3]
health_file = "{tmp_path / "health.json"}"
[gpu]
lock_file = "{tmp_path / "gpu.lock"}"
[vllm]
script = "{tmp_path / "serve-model.sh"}"
stop_script = "{tmp_path / "serve.sh"}"
pid_dir = "{tmp_path / "logs"}"
"""
    )
    c = live_config.load(toml)
    assert c.workspace == tmp_path / "ws"
    assert c.watch.roots == [str(tmp_path / "rollouts")]
    assert c.vllm_script == tmp_path / "serve-model.sh"
    assert c.vllm_stop_script == tmp_path / "serve.sh"
    assert c.vllm_pid_dir == tmp_path / "logs"
    assert c.gpu.lock_file == str(tmp_path / "gpu.lock")


def runnable(tmp_path):
    c = live_config.Config()
    c.service.workspace = str(tmp_path / "ws")
    c.service.home = str(tmp_path / "home")
    (tmp_path / "rollouts").mkdir(exist_ok=True)
    c.watch.roots = [str(tmp_path / "rollouts")]
    c.vllm.pid_dir = str(tmp_path / "pids")
    return c


def levels(found, key):
    return [c["level"] for c in found if c["key"] == key]


def test_a_runnable_configuration_passes_preflight(tmp_path):
    c = runnable(tmp_path)
    assert live_config.preflight(c) == []
    found = live_config.checks(c)
    assert levels(found, "vllm.script") == ["ok"]
    assert levels(found, "gpu.lock_file") == ["ok"]


def test_no_rollout_root_is_refused_with_its_fix(tmp_path):
    c = runnable(tmp_path)
    c.watch.roots = []
    with pytest.raises(ValueError, match=r"no rollout root.*--root PATH"):
        live_config.preflight(c)
    # Commands that do not watch (status, doctor) only warn.
    assert levels(live_config.checks(c, watching=False), "watch.roots") == ["warn"]


def test_a_missing_or_non_executable_script_and_an_unwritable_pid_dir_fail(tmp_path):
    c = runnable(tmp_path)
    c.vllm.script = str(tmp_path / "nope.sh")
    plain = tmp_path / "plain.sh"
    plain.write_text("#!/bin/sh\n")
    c.vllm.stop_script = str(plain)
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    c.vllm.pid_dir = str(blocker / "pids")  # under a file: cannot be made
    found = live_config.checks(c)
    assert levels(found, "vllm.script") == ["fail"]
    assert levels(found, "vllm.stop_script") == ["fail"]
    assert levels(found, "vllm.pid_dir") == ["fail"]
    with pytest.raises(ValueError) as refused:
        live_config.preflight(c)
    text = str(refused.value)
    assert "does not exist" in text and f"chmod +x {plain}" in text
    assert "docs/VLLM.md" in text
    # In manual mode the service never runs the scripts.
    c.gpu.mode = "manual"
    assert not [x for x in live_config.checks(c) if x["level"] == "fail"]


def test_a_lock_file_that_cannot_be_made_warns_or_fails_by_setting(tmp_path):
    c = runnable(tmp_path)
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    c.gpu.lock_file = str(blocker / "gpu.lock")
    assert levels(live_config.checks(c), "gpu.lock_file") == ["warn"]
    c.gpu.lock_unavailable = "wait"
    assert levels(live_config.checks(c), "gpu.lock_file") == ["fail"]
    with pytest.raises(ValueError, match="lock_unavailable"):
        live_config.from_dict({"gpu": {"lock_unavailable": "sometimes"}})


def test_paths_into_another_users_home_are_named(tmp_path, monkeypatch):
    real_home = os.environ["HOME"]
    monkeypatch.setenv("HOME", "/home/me")
    assert live_config.foreign_home("/home/someone/data") == "someone"
    assert live_config.foreign_home("/Users/someone/x") == "someone"
    assert live_config.foreign_home("/home/me/data") is None
    assert live_config.foreign_home("~/data") is None
    assert live_config.foreign_home("/data/rollouts") is None
    monkeypatch.setenv("HOME", real_home)  # the test's folders lie in the real home
    c = runnable(tmp_path)
    c.watch.roots.append("/home/someone-else-0/rollouts")
    c.gpu.lock_file = "/home/someone-else-0/hub/.gpu.lock"
    keys = [x["key"] for x in live_config.checks(c) if "another user" in x["message"]]
    assert keys == ["gpu.lock_file", "watch.roots"]


def _live_ws(path):
    from levi.live import auto

    (path / "live").mkdir(parents=True, exist_ok=True)
    auto.write_marker(path / "live", {"config": str(path / "live.toml")})
    return path


def _started(home, ws):
    c = live_config.Config()
    c.service.home, c.service.workspace = str(home), str(ws)
    home.mkdir(parents=True, exist_ok=True)
    cli.record_start(c)


def test_live_commands_follow_the_service_that_ran_with_this_home(
    tmp_path, monkeypatch
):
    """``levi live status`` and the rest, given no --workspace, act on the
    workspace the last ``levi live start`` with this home ran on."""
    home = tmp_path / "home"
    ws = _live_ws(tmp_path / "ws")
    _started(home, ws)
    monkeypatch.setenv("LEVI_LIVE_HOME", str(home))
    args = cli.build_parser().parse_args(["status"])
    assert cli.resolve_config(args).workspace == ws.resolve()
    # Not a live workspace (any more): the default.
    from levi.live import auto

    (ws / "live" / auto.MARKER).unlink()
    assert (
        cli.resolve_config(args).workspace
        == Path(live_config.DEFAULT_WORKSPACE).expanduser().resolve()
    )
    # An explicit workspace always wins.
    args = cli.build_parser().parse_args(["status", "--workspace", str(tmp_path / "x")])
    assert cli.resolve_config(args).workspace == (tmp_path / "x").resolve()


def test_a_status_file_alone_names_only_a_workspace_a_start_used(tmp_path, monkeypatch):
    """Before started.json existed: the status file's workspace counts only
    when a ``start`` ran there (its service log), never a ``once`` target."""
    home = tmp_path / "home"
    home.mkdir()
    ws = _live_ws(tmp_path / "ws")
    (home / "status.json").write_text(json.dumps({"workspace": str(ws)}))
    assert cli.remembered_workspace(home) is None
    (ws / "live/logs").mkdir(parents=True)
    (ws / "live/logs/live.log").write_text("")
    assert cli.remembered_workspace(home) == str(ws.resolve())


def test_once_never_changes_the_workspace_start_resolves_to(tmp_path, monkeypatch):
    home = tmp_path / "home"
    real = _live_ws(tmp_path / "real")
    _started(home, real)
    monkeypatch.setenv("LEVI_LIVE_HOME", str(home))
    (tmp_path / "rollouts").mkdir()
    code = cli.main(
        [
            "once",
            "--workspace",
            str(tmp_path / "trial"),
            "--root",
            str(tmp_path / "rollouts"),
            "--gpu-mode",
            "manual",
            "--max-seconds",
            "5",
        ]
    )
    assert code == 0
    # The once run wrote its status into the same home...
    status = json.loads((home / "status.json").read_text())
    assert Path(status["workspace"]).resolve() == (tmp_path / "trial").resolve()
    # ...but a later `start` (or status) without --workspace still means the real one.
    args = cli.build_parser().parse_args(["start"])
    assert cli.resolve_config(args).workspace == real.resolve()


def test_fake_vlm_without_a_home_uses_a_scratch_home(tmp_path, monkeypatch):
    import tempfile

    default_home = tmp_path / "default-home"
    monkeypatch.setattr(live_config, "DEFAULT_HOME", str(default_home))
    monkeypatch.delenv("LEVI_LIVE_HOME")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    (tmp_path / "rollouts").mkdir()
    code = cli.main(
        [
            "once",
            "--fake-vlm",
            "--workspace",
            str(tmp_path / "trial"),
            "--root",
            str(tmp_path / "rollouts"),
            "--max-seconds",
            "5",
        ]
    )
    assert code == 0
    assert not default_home.exists()  # the real home is untouched
    scratch = list((tmp_path / "tmp").glob("levi-live-fake-home-*"))
    assert len(scratch) == 1 and (scratch[0] / "status.json").is_file()
    assert not (scratch[0] / cli.STARTED).exists()


@pytest.mark.parametrize("which", ["remembered", "env", "started_here", "default"])
def test_fake_vlm_refuses_any_real_service_workspace(tmp_path, monkeypatch, which):
    home = tmp_path / "home"
    monkeypatch.setenv("LEVI_LIVE_HOME", str(home))
    target = _live_ws(tmp_path / "ws")
    if which == "remembered":
        _started(home, target)
    elif which == "env":
        monkeypatch.setenv("LEVI_LIVE_WORKSPACE", str(target))
    elif which == "started_here":
        (target / "live/logs").mkdir(parents=True)
        (target / "live/logs/live.log").write_text("")
    else:
        monkeypatch.setattr(live_config, "DEFAULT_WORKSPACE", str(target))
    args = cli.build_parser().parse_args(
        ["once", "--fake-vlm", "--auto-approve", "--workspace", str(target)]
    )
    with pytest.raises(ValueError, match="scratch"):
        cli.check_fake_target(args, None)


def test_start_refuses_a_configuration_that_cannot_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LEVI_LIVE_HOME", str(tmp_path / "home"))
    code = cli.main(
        ["start", "--workspace", str(tmp_path / "ws"), "--no-core"]
    )  # no --root
    assert code == 2
    err = capsys.readouterr().err
    assert "no rollout root" in err and "--root PATH" in err
    assert not (tmp_path / "ws").exists()  # nothing was created


def test_an_unconfigured_health_file_is_not_a_missing_monitor():
    from levi.live import sessions

    row = sessions.read_fr3("")
    assert row["state"] == "missing"
    assert "no health file configured (fr3.health_file)" == row["detail"]


# --- the GPU lock -------------------------------------------------------------------


def test_an_unusable_lock_file_has_a_state_and_follows_the_setting(tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    path = str(blocker / "gpu.lock")
    lock = gpumgr.GpuLock(path)
    assert lock.acquire() is True  # "continue": the old behaviour...
    assert lock.state == "unavailable" and lock.unavailable  # ...but said
    assert "gpu.lock" in lock.detail and not lock.held
    assert lock.public()["on_unavailable"] == "continue"
    strict = gpumgr.GpuLock(path, allow_unavailable=False)
    assert strict.acquire() is False and strict.state == "unavailable"
    assert gpumgr.GpuLock("").acquire() and gpumgr.GpuLock("").state == "disabled"
    good = gpumgr.GpuLock(str(tmp_path / "gpu.lock"))
    assert good.acquire() and good.state == "held"
    other = gpumgr.GpuLock(str(tmp_path / "gpu.lock"))
    assert other.acquire() is False and other.state == "busy"
    good.release()
    assert good.state == "free"


def test_the_doctor_shows_an_unavailable_lock_and_the_config_checks(
    tmp_path, monkeypatch
):
    c = runnable(tmp_path)
    c.vllm.script = str(tmp_path / "nope.sh")
    home = Path(c.service.home)
    home.mkdir(parents=True)
    (home / "status.json").write_text(
        json.dumps(
            {
                "workspace": c.service.workspace,
                "gpu": {
                    "lock": {
                        "state": "unavailable",
                        "detail": "x: Permission denied",
                        "on_unavailable": "continue",
                    }
                },
            }
        )
    )
    report = cli.diagnose(c)
    text = "\n".join(report["warnings"])
    assert "GPU lock unavailable: x: Permission denied" in text
    assert "vllm.script" in text and "does not exist" in text
    assert report["gpu_lock"]["state"] == "unavailable"
    assert any(x["key"] == "vllm.script" for x in report["config_checks"])
    # An unconfigured health monitor is not a warning.
    assert not [w for w in report["warnings"] if "FR3" in w]


def test_the_script_environment_names_the_pid_dir_and_the_model(tmp_path):
    c = runnable(tmp_path)
    env = gpumgr.script_env(c)
    assert env["LEVI_VLLM_PID_DIR"] == str(tmp_path / "pids")
    assert env["LEVI_VLLM_MODEL"] == c.vllm.model
    assert env["LEVI_VLLM_SERVED_NAME"] == c.vllm.served_model
    c.vllm.model = ""
    assert "LEVI_VLLM_MODEL" not in gpumgr.script_env(c)


def test_a_shipped_script_is_executable_in_git():
    for script in (PROJECT / "scripts/vllm").glob("*.sh"):
        assert script.stat().st_mode & stat.S_IXUSR, script
        assert os.access(script, os.X_OK)
