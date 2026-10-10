"""``levi automatic label`` (T-CL-14): an operator's outcome label for an
ended forward episode, confirmed at a terminal, in both reset modes. The
command never shows the automatic verdict and never writes the journal."""

import json

import pytest
from test_aeri_cli import CONTRACT, as_json, call, job_text

from levi.automatic import cli, modes
from levi.automatic import metrics as M

RUN = "r-cli"


class Terminal:
    def __init__(self, tty: bool):
        self.tty = tty

    def isatty(self):
        return self.tty


@pytest.fixture
def run_dir(tmp_path, capsys, reset_mode):
    root = tmp_path / "rollouts"
    root.mkdir()
    (tmp_path / "initial-state.yaml").write_text(CONTRACT)
    job = tmp_path / "automatic-eval.yaml"
    job.write_text(job_text(root, strategy=reset_mode))
    keep = tmp_path / "kept"
    code, found = as_json(
        capsys,
        "run",
        "--config",
        str(job),
        "--dry-run",
        "--keep",
        str(keep),
        "--scenes",
        "ready,reset_required",
    )
    assert code == 0
    assert found["state"] == (
        "WAIT_HUMAN" if reset_mode == modes.HUMAN else "COMPLETED"
    )
    return keep / ".aeri" / "runs" / RUN


@pytest.fixture
def terminal(monkeypatch, capsys):
    """A terminal whose person types ``answers`` in turn (an exception is
    raised); ``asked`` keeps the questions, which go to stderr."""
    state = {"answers": [], "asked": []}

    def ask(prompt=""):
        seen = capsys.readouterr()
        assert not seen.out  # stdout is kept for the result
        state["asked"].append(prompt + seen.err)
        answer = state["answers"].pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def use(tty=True, *answers):
        monkeypatch.setattr(cli.sys, "stdin", Terminal(tty))
        monkeypatch.setattr("builtins.input", ask)
        state["answers"][:] = list(answers)
        state["asked"].clear()
        return state

    return use


def label(capsys, run_dir, value, episode=f"{RUN}.forward.0001", *more):
    return call(
        capsys,
        "label",
        "--run-dir",
        str(run_dir),
        "--episode",
        episode,
        "--value",
        value,
        *more,
    )


@pytest.mark.mode_matrix("cli:label")
def test_label_confirms_at_a_terminal_and_writes_only_the_label(
    run_dir, capsys, terminal, reset_mode
):
    journal = run_dir / "state_journal.jsonl"
    before = journal.read_bytes()
    manifest = (run_dir / "manifest.json").read_bytes()
    labels = run_dir / "labels" / "operator_label.jsonl"
    # Not a terminal: refused, nothing asked, nothing written.
    asked = terminal(False)
    code, out = label(capsys, run_dir, "success")
    assert code == cli.EXIT_REFUSED and not asked["asked"] and not labels.exists()
    # A terminal, the wrong word typed: refused.
    asked = terminal(True, "yes")
    code, out = label(capsys, run_dir, "success")
    assert code == cli.EXIT_REFUSED and not labels.exists()
    assert "success" in asked["asked"][0]
    # Typed the value: written, and nothing about the run's own verdict.
    asked = terminal(True, "success")
    code, out = label(
        capsys, run_dir, "success", f"{RUN}.forward.0001", "--principal", "op-7"
    )
    assert code == cli.EXIT_OK, out
    assert "none yet" in asked["asked"][0] or "none yet" in out
    assert "verified" not in out and "goal_verification" not in out
    (line,) = M.LabelStore(run_dir).lines("operator_label")
    assert (line["value"], line["by"], line["episode_id"]) == (
        "success",
        "op-7",
        f"{RUN}.forward.0001",
    )
    # A correction says what it replaces; the earlier label stays on file.
    asked = terminal(True, "unclear")
    code, out = label(capsys, run_dir, "unclear", f"{RUN}.forward.0001", "--json")
    assert code == cli.EXIT_OK
    assert json.loads(out)["record"]["supersedes"] == 1
    assert "current label: success" in asked["asked"][0]
    assert M.LabelStore(run_dir).latest("operator_label") == {
        f"{RUN}.forward.0001": "unclear"
    }
    assert journal.read_bytes() == before
    assert (run_dir / "manifest.json").read_bytes() == manifest
    # The report reads it (unclear: apart, never compared).
    code, out = call(capsys, "report", "--run-dir", str(run_dir), "--format", "json")
    total = json.loads(out)["agreement"]["total"]
    assert code == 0 and total["unclear"] == 1 and total["judged"] == 0


@pytest.mark.parametrize(
    ("episode", "more"),
    [
        (f"{RUN}.forward.0009", ()),  # never ran
        ("nonsense", ()),
        (f"{RUN}.forward.0001", ("--principal", "Ada Lovelace")),
        (f"{RUN}.forward.0001", ("--principal", "ada@example.org")),
    ],
)
def test_label_refuses_before_asking(run_dir, capsys, terminal, episode, more):
    asked = terminal(True, "success")
    code, _ = label(capsys, run_dir, "success", episode, *more)
    assert code == cli.EXIT_REFUSED and not asked["asked"]
    assert not (run_dir / "labels").exists()


def test_label_rechecks_the_episode_after_the_confirmation(
    run_dir, capsys, terminal, monkeypatch
):
    """The check before the question and the write are separate reads: the
    write checks again (a run directory swapped in between is refused)."""
    asked = terminal(True, "success")
    real = M.label_operator
    calls = []

    def recheck(*args, **kwargs):
        calls.append(args)
        (run_dir / "state_journal.jsonl").rename(run_dir / "moved.jsonl")
        return real(*args, **kwargs)

    monkeypatch.setattr(M, "label_operator", recheck)
    code, _ = label(capsys, run_dir, "success")
    assert calls and code == cli.EXIT_REFUSED and asked["asked"]
    assert not (run_dir / "labels" / "operator_label.jsonl").exists()


def test_label_refuses_a_value_outside_the_four(run_dir, capsys, terminal):
    terminal(True, "maybe")
    with pytest.raises(SystemExit) as stop:
        label(capsys, run_dir, "maybe")
    assert stop.value.code == 2


@pytest.mark.parametrize("gone", [EOFError(), KeyboardInterrupt()])
def test_a_terminal_closed_at_the_question_writes_nothing(
    run_dir, capsys, terminal, gone
):
    asked = terminal(True, gone)
    code, out = label(capsys, run_dir, "failure", f"{RUN}.forward.0001", "--json")
    assert code == cli.EXIT_REFUSED and asked["asked"]
    assert json.loads(out)["ok"] is False
    assert not (run_dir / "labels" / "operator_label.jsonl").exists()
