"""The real entry (fr3_run.execute) of a subtask_loop run whose reverse loop uses the reset service
(subtask.reverse_policy_role reset, design STEP5-AERI-RESET-DESIGN, R1 / P1b), on the fake FR3 modules, a fake
systemd, ``/proc`` and ``nvidia-smi`` and a fake VLM transport: no socket, GPU, robot or unit tool. The real
``PolicySwitcher`` makes the switches, so the run shows where in the flow they happen."""

import json
from dataclasses import replace

import pytest
from fr3_swap_fakes import refuse_unit_tools
from test_fr3_policy_swap_entry import Swap, write
from test_fr3_subtask_entry import (  # noqa: F401  (autouse fixture)
    no_network,
    to_subtask,
)

from levi.automatic import fr3_policy_swap as S
from levi.automatic import fr3_run as R
from levi.automatic import subtask_loop as st
from levi.automatic.journal import Journal, plan_digest

ENTRIES = ("pink plate|table|pick the pink plate onto the table",)
SENTENCE = "Reset: pick the pink plate onto the table"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    refuse_unit_tools(monkeypatch)


def reset_swap(tmp_path, monkeypatch, **cfg_over):
    s = Swap(tmp_path, monkeypatch, mode="single_reset_policy", scenes=())
    _, vllm, queries = to_subtask(s.r, monkeypatch)
    plan = s.r.job["plan"]
    config = replace(
        s.r.job["config"],
        subtask_reverse_policy_role="reset",
        subtask_reset_prompts=ENTRIES,
        plan_sha256=plan_digest(plan),
        **cfg_over,
    )
    s.r.job["config"] = config
    return s, vllm, queries


def test_the_reverse_loop_runs_on_the_reset_service_switched_in_at_home(
    tmp_path, monkeypatch
):
    s, _, queries = reset_swap(tmp_path, monkeypatch)
    assert s.execute() == "COMPLETED"
    assert s.violations == []
    assert s.unit_calls() == [
        ("start", "levi-aeri-policy-forward"),
        ("stop", "levi-aeri-policy-forward"),
        ("start", "levi-aeri-policy-reset"),
        ("stop", "levi-aeri-policy-reset"),
    ]
    prompts = [p for p, _ in queries if p not in s.r.job["texts"].values()]
    assert prompts == [st.render("pink_plate_2", "pink_plate_1"), SENTENCE]
    log = s.log
    assert [r["service_state"] for r in log if r["record"] == "committed"] == [
        "forward",
        "reset",
        "none",
    ]
    assert not [r for r in log if r["record"] in ("aborted", "refused")]
    events = Journal.read(s.run_dir).events
    roles = [
        json.loads(e.note.detail)["role"]
        for e in events
        if e.record == "note" and e.note.code == st.NOTE_POLICY_ROLE
    ]
    assert roles == ["forward", "reset"]


def capture_policy(monkeypatch):
    made = []

    class Recording(S.SwitchingPolicy):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made.append(self)

    monkeypatch.setattr(S, "SwitchingPolicy", Recording)
    return made


def test_the_bridge_accepts_the_reset_sentence_only_when_the_run_has_the_reset_role(
    tmp_path, monkeypatch
):
    made = capture_policy(monkeypatch)
    s, _, _ = reset_swap(tmp_path, monkeypatch)
    assert s.execute() == "COMPLETED"
    allowed = made[0].prompt_allowed
    assert allowed(SENTENCE) is True
    assert allowed(st.render("pink_plate_2", "table")) is True
    assert allowed("Reset: pick the blue plate onto the table") is False
    assert allowed("put the pink plate on the blue plate; open the gripper") is False

    (tmp_path / "plain").mkdir()
    plain = Swap(tmp_path / "plain", monkeypatch, mode="single_reset_policy", scenes=())
    to_subtask(plain.r, monkeypatch)
    assert plain.execute() == "COMPLETED"
    assert made[1].prompt_allowed(SENTENCE) is False  # no reset role: no sentence
    assert made[1].prompt_allowed(st.render("pink_plate_2", "table")) is True


def test_the_binding_needs_both_policy_endpoints_for_the_reset_role(
    tmp_path, monkeypatch
):
    s, _, _ = reset_swap(tmp_path, monkeypatch)
    path = write(tmp_path / "binding.json", s.r.binding)
    value, _ = R.load_binding(path, s.r.job)
    assert set(value["policies"]) == {"forward", "reset"}
    only_forward = dict(
        s.r.binding, policies={"forward": s.r.binding["policies"]["forward"]}
    )
    with pytest.raises(ValueError, match="policy endpoint roles"):
        R.load_binding(write(tmp_path / "b2.json", only_forward), s.r.job)
    # Without the setting the same two-endpoint binding is refused, as before.
    plain = replace(
        s.r.job["config"],
        subtask_reverse_policy_role="forward",
        subtask_reset_prompts=None,
    )
    with pytest.raises(ValueError, match="policy endpoint roles"):
        R.load_binding(
            write(tmp_path / "b3.json", s.r.binding), dict(s.r.job, config=plain)
        )


def test_a_reset_service_that_cannot_start_locks_the_run_before_any_reverse_episode(
    tmp_path, monkeypatch
):
    s, _, queries = reset_swap(tmp_path, monkeypatch)

    def reset_start_fails(argv):
        if (
            argv[:2] == ["systemd-run", "--user"]
            and "--unit=levi-aeri-policy-reset" in argv
        ):
            s.units.run_fails = 2  # the start and its one retry

    s.extra_hooks.append(reset_start_fails)
    assert s.execute() == "FAULT_LOCKED"
    assert SENTENCE not in [p for p, _ in queries]
    assert [r["code"] for r in s.log if r["record"] == "aborted"]
    events = Journal.read(s.run_dir).events
    assert [e.to_state for e in events if e.record == "committed"][-1] == "FAULT_LOCKED"
    assert s.units.units == {}  # nothing is left running
