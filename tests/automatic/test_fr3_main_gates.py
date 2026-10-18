"""fr3_run.main --execute gates; fake rig, no SDK, robot, network or GPU."""

import itertools
import json

import pytest
from test_fr3_bridge import R, rig


@pytest.fixture
def gate(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(r.binding))
    _, digest = R.load_binding(path, r.job)
    state = {"executed": [], "inputs": [], "tty": True}
    monkeypatch.setattr(R.cli, "load_job", lambda *a: dict(r.job))
    monkeypatch.setattr(R.launch, "job_fact", lambda *a: {"fixed": True})
    monkeypatch.setattr(R.launch, "index_path", lambda run_id: tmp_path / "absent")
    monkeypatch.setattr(
        R, "execute", lambda job, binding: state["executed"].append(job) or "COMPLETED"
    )
    monkeypatch.setattr(R.sys.stdin, "isatty", lambda: state["tty"])

    def answer(prompt):
        state["inputs"].append(prompt)
        return "execute " + r.job["config"].run_id

    monkeypatch.setattr("builtins.input", answer)

    def run(plan=None, binding=None):
        return R.main(
            [
                "--config",
                "fixture",
                "--binding",
                str(path),
                "--execute",
                "--expect-plan",
                plan or r.job["plan_sha256"],
                "--expect-binding",
                binding or digest,
            ]
        )

    return NS_(run=run, state=state, tmp=tmp_path)


class NS_:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_reviewed_digests_tty_and_confirmation_reach_execute(gate):
    assert gate.run() == 0
    assert len(gate.state["executed"]) == 1
    assert gate.state["executed"][0]["standalone_inputs"]["job"] == {"fixed": True}


@pytest.mark.parametrize("which", ["plan", "binding"])
def test_wrong_expect_digest_refused_before_prompt(gate, which, capsys):
    code = gate.run(**{which: "0" * 64})
    assert code == 2
    assert "must match the reviewed --check output" in capsys.readouterr().err
    assert gate.state["inputs"] == [] and gate.state["executed"] == []


def test_non_tty_refused_before_prompt(gate, capsys):
    gate.state["tty"] = False
    assert gate.run() == 2
    assert "operator terminal" in capsys.readouterr().err
    assert gate.state["inputs"] == [] and gate.state["executed"] == []


def test_job_fact_changed_during_confirmation_is_refused(gate, monkeypatch, capsys):
    counter = itertools.count()
    monkeypatch.setattr(R.launch, "job_fact", lambda *a: {"n": next(counter)})
    assert gate.run() == 2
    assert "changed during operator confirmation" in capsys.readouterr().err
    assert len(gate.state["inputs"]) == 1 and gate.state["executed"] == []


def test_already_registered_run_id_is_refused(gate, monkeypatch, capsys):
    present = gate.tmp / "registered.json"
    present.write_text("{}")
    monkeypatch.setattr(R.launch, "index_path", lambda run_id: present)
    assert gate.run() == 2
    assert "already registered" in capsys.readouterr().err
    assert gate.state["executed"] == []
