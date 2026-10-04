"""``levi doctor`` (read-only installation check) and ``levi install``
(profiles, a plan an agent can read, idempotent steps, a person's steps
listed and never done)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from levi import doctor, install
from levi.live import fakevlm

PROJECT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """A workspace that does not exist yet, and no secret in the report."""
    ws = tmp_path / "ws"
    monkeypatch.setattr(doctor, "ROOT", ws)
    monkeypatch.setattr(install, "ROOT", ws)
    monkeypatch.setenv("HF_TOKEN", "hf_secret_value_123")
    monkeypatch.setenv("LEVI_POOL_ROOTS", "/data/secret-root")
    monkeypatch.delenv("LEVI_POOL_HELDOUT", raising=False)
    return ws


def run_quiet(**kw):
    return doctor.run(ollama_ports=(), vllm_ports=(), **kw)


def test_the_doctor_creates_nothing_and_shows_no_value(isolated):
    report = run_quiet()
    assert not isolated.exists()
    text = json.dumps(report)
    assert "hf_secret_value_123" not in text and "/data/secret-root" not in text
    by_id = {c["id"]: c for c in report["checks"]}
    assert by_id["hf-token"]["level"] == "ok"
    # Pool roots set without held-out lists: exports would be refused.
    assert by_id["pool"]["level"] == "warn" and by_id["pool"]["human"]
    assert "LEVI_POOL_ROOTS set" in by_id["pool"]["message"]
    assert report["schema"] == "levi.doctor.v1"
    assert report["exit_code"] == doctor.EXIT[report["status"]]
    for c in report["checks"]:
        assert set(c) == {"id", "group", "level", "message", "fix", "human"}
        assert c["level"] in ("ok", "info", "warn", "fail")


def test_the_verdict_separates_agent_fixable_failures_from_a_persons():
    ok = doctor.check("a", "g", "ok", "fine")
    warn = doctor.check("b", "g", "warn", "hm", "do x")
    human_warn = doctor.check("c", "g", "warn", "hm", "a token", human=True)
    fail = doctor.check("d", "g", "fail", "broken", "levi build")
    human_fail = doctor.check("e", "g", "fail", "no ffmpeg", "sudo apt", human=True)
    assert doctor.verdict([ok]) == "ok"
    assert doctor.verdict([ok, warn]) == "warn"
    assert doctor.verdict([ok, warn, human_warn]) == "human"
    assert doctor.verdict([human_fail, warn]) == "human"
    assert doctor.verdict([human_fail, fail]) == "fail"
    assert doctor.EXIT == {"ok": 0, "warn": 1, "fail": 2, "human": 10}


def test_a_stale_build_is_found_by_the_source_hash(tmp_path, monkeypatch):
    project = tmp_path / "checkout"
    (project / "src").mkdir(parents=True)
    (project / "src/page.tsx").write_text("one")
    (project / "package.json").write_text("{}")
    (project / ".next").mkdir()
    (project / ".next/BUILD_ID").write_text("0.3.0")
    monkeypatch.setattr(doctor, "PROJECT", project)
    level = lambda: next(c for c in doctor.frontend_checks() if c["id"] == "build")
    assert level()["level"] == "warn" and "no source stamp" in level()["message"]
    doctor.write_build_stamp(project)
    assert level()["level"] == "ok"
    (project / "src/page.tsx").write_text("two")
    assert level()["level"] == "warn" and "stale" in level()["message"]
    (project / ".next/BUILD_ID").unlink()
    assert level()["level"] == "fail"


def test_lev_ports_are_looked_up_never_connected(monkeypatch):
    monkeypatch.setattr(doctor, "listening_ports", lambda: {7860})
    rows = {c["id"]: c["level"] for c in doctor.service_checks(7860, 7861)}
    assert rows == {"ui-port": "warn", "api-port": "ok"}


def test_robot_ports_are_never_connected_to():
    with pytest.raises(ValueError):
        doctor._get(8000, "/health")
    with pytest.raises(SystemExit):
        doctor.main(["--vllm-ports", "8000"])


def test_a_local_model_server_is_asked_for_its_models_on_loopback():
    server, _, port = fakevlm.serve(0)
    try:
        rows = doctor.model_server_checks((), (port,))
    finally:
        server.shutdown()
    row = next(r for r in rows if r["id"] == f"vllm-{port}")
    assert row["level"] == "ok" and "serves" in row["message"]


def test_doctor_json_from_the_command_line(isolated, capsys):
    code = doctor.main(["--json", "--vllm-ports", "none", "--ollama-ports", "none"])
    report = json.loads(capsys.readouterr().out)
    assert code == report["exit_code"]
    assert {c["group"] for c in report["checks"]} >= {
        "system",
        "frontend",
        "workspace",
        "workers",
        "live",
    }


# --- levi install ---------------------------------------------------------------------


def test_the_plan_changes_nothing_and_names_what_needs_a_person(isolated, capsys):
    code = install.main(["--profile", "live", "--profile", "sam3", "--plan", "--json"])
    plan = json.loads(capsys.readouterr().out)
    assert not isolated.exists()
    assert plan["schema"] == "levi.install.plan.v1"
    assert plan["profiles"] == ["core", "live", "sam3"]
    steps = {s["id"]: s for s in plan["steps"]}
    assert {"python-deps", "env-file", "workspace", "frontend-deps", "build"} <= set(
        steps
    )
    weights = steps["sam3-weights"]
    assert weights["kind"] == "human" and weights["token"] and weights["consent"]
    assert steps["live-config"]["kind"] == "human"
    assert steps["sam3-deps"]["needs_yes"]  # a large download waits for --yes
    for s in plan["steps"]:
        for key in ("network", "download", "sudo", "token", "consent", "done"):
            assert key in s
    assert code == install.EXIT_HUMAN


def test_install_steps_are_idempotent_and_wait_for_a_person(tmp_path, monkeypatch):
    project = tmp_path / "checkout"
    project.mkdir()
    (project / ".env.example").write_text("# example\n")
    monkeypatch.setattr(install, "PROJECT", project)
    env = install.Step("env-file", "core", ".env", "auto", ["cp"])
    big = install.Step("big", "core", "big", "auto", ["true"], download_bytes=2 << 30)
    person = install.Step("tok", "core", "token", "human", ["hf auth login"])
    result = install.execute([env, big, person], log=lambda *a, **k: None)
    assert (project / ".env").read_text() == "# example\n"
    rows = {r["id"]: r["result"] for r in result["steps"]}
    assert rows == {
        "env-file": "done",
        "big": "waiting for --yes (downloads about 2.0 GiB)",
        "tok": "for a person",
    }
    assert install.summary_code(result) == install.EXIT_HUMAN
    # An existing .env is never touched.
    (project / ".env").write_text("MINE=1\n")
    install.execute([env], log=lambda *a, **k: None)
    assert (project / ".env").read_text() == "MINE=1\n"


def test_a_failed_step_stops_the_rest(tmp_path):
    bad = install.Step("bad", "core", "bad", "auto", ["false"])
    after = install.Step("after", "core", "after", "auto", ["true"])
    done = install.Step("done", "core", "done", "auto", ["false"], done=True)
    result = install.execute([bad, after, done], log=lambda *a, **k: None)
    rows = {r["id"]: r["result"] for r in result["steps"]}
    assert rows["bad"].startswith("failed") and rows["after"].startswith("skipped")
    assert rows["done"] == "already done"
    assert install.summary_code(result) == install.EXIT_FAILED


def test_levi_doctor_and_install_are_commands(tmp_path):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "LEVI_WORKSPACE": str(tmp_path / "ws"),
        "LEVI_LIVE_HOME": str(tmp_path / "live-home"),
        "LEVI_DROID_SAMPLE": "off",
    }
    plan = subprocess.run(
        [sys.executable, "-m", "levi.cli", "install", "--plan", "--json"],
        cwd=PROJECT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert json.loads(plan.stdout)["schema"] == "levi.install.plan.v1"
    assert not (tmp_path / "ws").exists()


# --- review fixes: the running frontend, --json output, plan exit codes ------------


def _step(steps, id_):
    return next(s for s in steps if s.id == id_)


def test_the_frontend_is_not_rebuilt_while_this_checkout_serves(isolated, monkeypatch):
    monkeypatch.setattr(install, "_build_state", lambda: "stale")
    monkeypatch.setattr(
        install, "serving", lambda ui_port=7860: "port 7860 is listening"
    )
    steps = install.plan(["core"])
    assert _step(steps, "build").kind == "human"
    assert _step(steps, "stop-service").kind == "human"
    # Only the build step: the others would really run here.
    result = install.execute([_step(steps, "build")], log=lambda *a, **k: None)
    assert result["steps"][0]["result"] == "for a person"
    monkeypatch.setattr(install, "serving", lambda ui_port=7860: "")
    steps = install.plan(["core"])
    assert _step(steps, "build").kind == "auto"
    assert not [s for s in steps if s.id == "stop-service"]


def test_build_state_classifies_a_temporary_next(tmp_path, monkeypatch):
    """``install._build_state`` on a temporary checkout: no ``.next`` is
    missing, a build without a stamp unknown, a stamp of these sources
    current, and a source change after the stamp stale."""
    project = tmp_path / "checkout"
    (project / "src").mkdir(parents=True)
    (project / "src/page.tsx").write_text("one")
    (project / "package.json").write_text("{}")
    monkeypatch.setattr(install, "PROJECT", project)
    monkeypatch.setattr(doctor, "PROJECT", project)
    assert install._build_state() == "missing" and not install._built()
    (project / ".next").mkdir()
    (project / ".next/BUILD_ID").write_text("0.3.0")
    assert install._build_state() == "unknown" and not install._built()
    doctor.write_build_stamp(project)
    assert install._build_state() == "current" and install._built()
    (project / "src/page.tsx").write_text("two")
    assert install._build_state() == "stale" and not install._built()
    doctor.write_build_stamp(project)
    assert install._build_state() == "current"
    (project / ".next/BUILD_ID").unlink()
    assert install._build_state() == "missing"


def test_python_dependencies_wait_for_a_stop_while_this_checkout_serves(
    isolated, monkeypatch
):
    """``uv sync`` changes the packages a running LEVI imports: while it
    runs, the step is a person's and a ``stop-service`` step comes first."""
    monkeypatch.setattr(install, "_build_state", lambda: "current")
    monkeypatch.setattr(install, "_has", lambda module: False)
    monkeypatch.setattr(
        install, "serving", lambda ui_port=7860: "port 7860 is listening"
    )
    steps = install.plan(["core"])
    ids = [s.id for s in steps]
    deps = _step(steps, "python-deps")
    assert deps.kind == "human" and not deps.done
    assert "stop it first" in deps.note
    assert ids.index("stop-service") < ids.index("python-deps")
    assert not deps.public()["needs_yes"]
    result = install.execute([deps], log=lambda *a, **k: None)
    assert result["steps"][0]["result"] == "for a person"
    # Not serving: LEVI runs it itself, and there is nothing to stop.
    monkeypatch.setattr(install, "serving", lambda ui_port=7860: "")
    steps = install.plan(["core"])
    assert _step(steps, "python-deps").kind == "auto"
    assert "stop-service" not in [s.id for s in steps]


def test_serving_reads_the_socket_table(monkeypatch):
    monkeypatch.setattr(doctor, "listening_ports", lambda: {7860})
    assert "7860" in install.serving()


def test_a_build_without_a_stamp_is_rebuilt_only_with_yes(isolated, monkeypatch):
    monkeypatch.setattr(install, "_build_state", lambda: "unknown")
    monkeypatch.setattr(install, "serving", lambda ui_port=7860: "")
    build = _step(install.plan(["core"]), "build")
    assert build.kind == "auto" and build.public()["needs_yes"]
    assert "no source stamp" in build.confirm
    ran = []
    monkeypatch.setattr(
        install, "_run_step", lambda s, stdout=None: ran.append(s.id) or (True, "")
    )
    result = install.execute([build], log=lambda *a, **k: None)
    assert not ran and result["steps"][0]["result"].startswith("waiting for --yes")
    install.execute([build], yes=True, log=lambda *a, **k: None)
    assert ran == ["build"]


def test_json_output_is_one_document_even_when_steps_print(monkeypatch, capfd):
    noisy = install.Step(
        "noisy", "core", "noisy", "auto", ["printf", "%s-%s\\n", "raw", "child-output"]
    )
    monkeypatch.setattr(install, "plan", lambda profiles: [noisy])
    code = install.main(["--json", "--no-doctor"])
    out, err = capfd.readouterr()
    document = json.loads(out)
    assert document["schema"] == "levi.install.result.v1" and code == 0
    assert "raw-child-output" in err and "raw-child-output" not in out


@pytest.mark.parametrize(
    "steps,code",
    [
        ([], 0),
        ([install.Step("a", "core", "a", "auto", ["true"])], 1),
        ([install.Step("a", "core", "a", "auto", ["true"], done=True)], 0),
        ([install.Step("h", "core", "h", "human")], 10),
    ],
)
def test_plan_exit_codes(monkeypatch, capsys, steps, code):
    monkeypatch.setattr(install, "plan", lambda profiles: steps)
    assert install.main(["--plan", "--json"]) == code
    json.loads(capsys.readouterr().out)


def test_an_unsupported_platform_is_a_plan_step_not_a_crash(isolated, monkeypatch):
    from levi import bootstrap

    def unsupported():
        raise RuntimeError("Unsupported CPU architecture")

    monkeypatch.setattr(bootstrap, "bun_path", unsupported)
    monkeypatch.setattr(install, "serving", lambda ui_port=7860: "")
    steps = install.plan(["core", "pilot"])
    platform_step = _step(steps, "platform")
    assert platform_step.kind == "human" and "Unsupported" in platform_step.title
    assert _step(steps, "frontend-deps").kind == "human"
    assert _step(steps, "build").kind == "human"


def test_a_redirect_is_never_followed(monkeypatch):
    """A server answering 302 to a robot port: the probe reports it, it does
    not follow (the target would be :8000)."""
    import http.server
    import threading

    hits = []

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:8000/health")
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    followed = []
    real_get = doctor._get

    def spy(port, path, timeout=2.0):
        followed.append(port)
        return real_get(port, path, timeout)

    monkeypatch.setattr(doctor, "_get", spy)
    try:
        rows = doctor.model_server_checks((), (server.server_address[1],))
    finally:
        server.shutdown()
    row = rows[-1]
    assert row["level"] == "warn" and "302" in row["message"]
    assert 8000 not in followed and hits == ["/health"]


def test_no_rollout_root_in_the_live_check_is_a_persons(tmp_path, monkeypatch):
    toml = tmp_path / "live.toml"
    toml.write_text(f'[service]\nworkspace = "{tmp_path / "ws"}"\n')
    monkeypatch.setenv("LEVI_LIVE_CONFIG", str(toml))
    rows = [c for c in doctor.live_checks() if c["id"] == "live:watch.roots"]
    assert rows and all(c["human"] for c in rows)
    assert rows[0]["level"] == "fail"
