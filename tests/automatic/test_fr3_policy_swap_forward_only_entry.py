"""fr3_run with a forward-only ``policy_swap`` (human_assisted reset) on the
fake FR3 modules and fake systemd, ``/proc`` and ``nvidia-smi``: one managed
unit for the whole run, started before any arm I/O. No unit tool runs, no GPU
is queried and no socket is opened."""

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fr3_swap_fakes import refuse_unit_tools
from test_fr3_policy_swap_entry import RUN, Swap, write

from levi.automatic import fr3_policy_swap as S
from levi.automatic import fr3_run as R
from levi.automatic import state_machine as sm
from levi.automatic.journal import Journal


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    refuse_unit_tools(monkeypatch)
    monkeypatch.setattr(R, "ARM_LOCK_FILE", str(tmp_path / "physical-arm.lock"))


def forward_only(tmp_path, monkeypatch, **kw):
    return Swap(tmp_path, monkeypatch, mode="human_assisted", scenes=(), **kw)


def test_the_run_has_one_unit_from_before_the_arm_to_the_end(tmp_path, monkeypatch):
    s = forward_only(tmp_path, monkeypatch)
    assert s.execute() == "COMPLETED"
    assert s.violations == []
    assert s.unit_calls() == [
        ("start", "levi-aeri-policy-forward"),
        ("stop", "levi-aeri-policy-forward"),
    ]
    assert s.units.units == {} and s.proc.sockets == {}
    assert sm.check_journal(Journal.read(s.run_dir).events) == []
    calls = s.r.calls
    assert calls.index(next(c for c in calls if c[0] == "env")) > calls.index(
        ("metadata", "forward")
    )
    assert ("metadata", "reset") not in calls
    states = [r["service_state"] for r in s.log if r["record"] == "committed"]
    assert states == ["forward", "none"]
    assert [r["record"] for r in s.log].count("warm") == 1
    assert S.pending(s.r.job["rollout_root"]) == []


def test_the_unit_output_goes_to_a_private_file_under_the_rollout_root(
    tmp_path, monkeypatch, capsys
):
    s = forward_only(tmp_path, monkeypatch)
    s.units.banner = "INFO serving\n"
    assert s.execute() == "COMPLETED"
    start = s.starts_any()[0]
    path = s.units.log_files["levi-aeri-policy-forward"]
    assert f"--property=StandardOutput=append:{path}" in start
    assert path == str(
        S.service_log_dir(s.r.job["rollout_root"]) / f"{RUN}-forward.service.log"
    )
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert "INFO serving" in Path(path).read_text()
    err = capsys.readouterr().err  # a healthy run prints only the start/home gates
    assert "INFO serving" not in err
    assert all(
        line.split(" verified")[0] in ("Start pose", "Home")
        for line in err.splitlines()
    )


def test_a_failed_start_shows_the_service_log_before_any_arm_io(
    tmp_path, monkeypatch, capsys
):
    s = forward_only(tmp_path, monkeypatch)
    s.units.banner = "RuntimeError: checkpoint is corrupt\n"
    client = s.r.modules[0]
    connect = client.websocket_client_policy.WebsocketClientPolicy

    def wrong(host, port):
        made = connect(host, port)
        made.get_server_metadata = lambda: {
            "config": "cfg",
            "checkpoint_dir": "/models/other",
        }
        return made

    client.websocket_client_policy = NS(WebsocketClientPolicy=wrong)
    with pytest.raises(S.SwapError) as caught:
        s.execute()
    assert caught.value.code == "handshake_refused"
    assert "checkpoint is corrupt" in capsys.readouterr().err
    assert not any(c[0] == "env" for c in s.r.calls)  # no arm I/O
    assert s.units.units == {} and s.violations == []


def test_a_warm_up_that_counts_motion_refuses_the_start(tmp_path, monkeypatch, capsys):
    # The forward service is warmed once, before any session exists (int/aeri-flow's
    # start-up warm-up and the switcher's warm-up are one call): a counted motion
    # step refuses the start, stops the unit and leaves the run id unused.
    s = forward_only(tmp_path, monkeypatch)
    s.units.banner = "INFO ready\n"
    client = s.r.modules[0]
    query = client._query_policy
    queries = []

    def moving(*a):
        queries.append(a)
        if len(queries) == 1:  # the warm-up: something counted a step
            s.robots[-1].steps += 1
        return query(*a)

    client._query_policy = moving
    with pytest.raises(
        ValueError, match="policy warm-up refused: motion_during_switch"
    ):
        s.execute()
    assert len(queries) == 1  # warmed once, never twice
    assert [r["code"] for r in s.log if r["record"] == "aborted"] == [
        "motion_during_switch"
    ]
    assert s.units.units == {} and ("route", "pose") not in s.r.calls
    assert "INFO ready" in capsys.readouterr().err
    assert not s.run_dir.exists()


def test_check_lists_only_the_forward_unit(tmp_path, monkeypatch, capsys):
    s = forward_only(tmp_path, monkeypatch)
    path = write(tmp_path / "binding.json", s.r.binding)
    monkeypatch.setattr(R.cli, "load_job", lambda p: s.r.job)
    monkeypatch.setattr(R, "execute", lambda *a, **k: pytest.fail("executed"))
    job_path = tmp_path / "job.yaml"
    job_path.write_text("fake parser")
    assert R.main(["--config", str(job_path), "--binding", str(path), "--check"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["policy_swap"]["units"] == {"forward": "levi-aeri-policy-forward"}
    assert s.units.calls == [] and s.r.calls == []


def test_the_services_must_name_exactly_the_policy_roles(tmp_path, monkeypatch):
    s = forward_only(tmp_path, monkeypatch)
    (tmp_path / "two").mkdir()
    two = Swap(tmp_path / "two", monkeypatch)  # forward and reset
    with pytest.raises(
        ValueError, match="must name exactly the binding's policy roles"
    ):
        R.load_binding(
            write(
                tmp_path / "b.json",
                dict(s.r.binding, policy_swap=two.r.binding["policy_swap"]),
            ),
            s.r.job,
        )
    with pytest.raises(
        ValueError, match="must name exactly the binding's policy roles"
    ):
        R.load_binding(
            write(
                tmp_path / "c.json",
                dict(two.r.binding, policy_swap=s.r.binding["policy_swap"]),
            ),
            two.r.job,
        )
