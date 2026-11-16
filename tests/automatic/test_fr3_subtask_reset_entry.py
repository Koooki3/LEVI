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

REAL_SUBTASK_SERVICES = R._subtask_services  # test_fr3_subtask_entry's rig replaces it
ENTRIES = ("pink plate|table|pick the pink plate onto the table",)
SENTENCE = "Reset: pick the pink plate onto the table"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    refuse_unit_tools(monkeypatch)


def reset_swap(tmp_path, monkeypatch, **cfg_over):
    s = Swap(tmp_path, monkeypatch, mode="single_reset_policy", scenes=())
    _, vllm, queries = to_subtask(s.r, monkeypatch)
    plan = s.r.job["plan"]
    cfg_over.setdefault("subtask_reset_prompts", ENTRIES)
    config = replace(
        s.r.job["config"],
        subtask_reverse_policy_role="reset",
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
    # the forward episode, then the reset service's warm-up and its episode
    assert prompts == [st.render("pink_plate_2", "pink_plate_1"), SENTENCE, SENTENCE]
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
    template = st.render("pink_plate_2", "table")
    assert allowed(SENTENCE, "reset") is True
    assert allowed(template, "forward") is True
    # never crossed: the reset sentence never reaches the forward policy and
    # a template text never reaches the reset policy
    assert allowed(SENTENCE, "forward") is False
    assert allowed(template, "reset") is False
    assert allowed("Reset: pick the blue plate onto the table", "reset") is False
    assert (
        allowed("put the pink plate on the blue plate; open the gripper", "forward")
        is False
    )
    assert allowed(template, "other") is False

    (tmp_path / "plain").mkdir()
    plain = Swap(tmp_path / "plain", monkeypatch, mode="single_reset_policy", scenes=())
    to_subtask(plain.r, monkeypatch)
    assert plain.execute() == "COMPLETED"
    assert (
        made[1].prompt_allowed(SENTENCE, "reset") is False
    )  # no reset role: no sentence
    assert made[1].prompt_allowed(st.render("pink_plate_2", "table"), "forward") is True


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


# --- the reset service's own warm-up and handshake prompt (L2-RESET-WARMUP-1114) -----


def hook_checks(s):
    """Every prompt the client's ``_check_prompt`` receives: the handshake of
    each service and, in this rig, each episode's prompt."""
    checked = []
    module = s.r.modules[0]
    module._check_prompt = lambda text, args, metadata: checked.append(text)
    return checked


def test_the_reset_service_is_warmed_up_and_checked_with_a_reset_sentence_not_the_job_text(
    tmp_path, monkeypatch
):
    s, _, queries = reset_swap(tmp_path, monkeypatch)
    checked = hook_checks(s)
    assert s.execute() == "COMPLETED"
    # The job's own text of the reset role (the reverse task's name in a real
    # job, `undo: <task>`) reaches no service: it is not a training sentence.
    stale = s.r.job["texts"]["reset"]
    assert stale not in checked
    assert stale not in [p for p, _ in queries]
    forward = s.r.job["texts"]["forward"]
    episode = st.render("pink_plate_2", "pink_plate_1")
    # handshake forward, forward episode, handshake reset, reset episode
    assert checked == [forward, episode, SENTENCE, SENTENCE]
    # the queries: the forward warm-up, the forward episode, the reset warm-up
    # and the reset episode
    assert [p for p, _ in queries] == [forward, episode, SENTENCE, SENTENCE]


def test_the_warm_up_sentence_is_the_first_sorted_of_the_frozen_ones(
    tmp_path, monkeypatch
):
    entries = (
        "pink plate|table|pick the pink plate onto the table",
        "purple eggplant|white plate|pick the eggplant into the blue plate",
    )
    first = "Reset: pick the eggplant into the blue plate"  # sorts before "...pink..."
    assert first == min(st.reset_prompt_table(entries).values())
    s, _, queries = reset_swap(tmp_path, monkeypatch, subtask_reset_prompts=entries)
    checked = hook_checks(s)
    assert s.execute() == "COMPLETED"
    # the reset service's handshake and warm-up use `first`, though the episode
    # that runs sends the table's sentence for its own subtask
    assert checked[2] == first
    assert [p for p, _ in queries][2] == first
    assert [p for p, _ in queries][3] == SENTENCE


def test_without_the_reset_role_the_texts_are_the_jobs_own(tmp_path, monkeypatch):
    plain = Swap(tmp_path, monkeypatch, mode="single_reset_policy", scenes=())
    to_subtask(plain.r, monkeypatch)
    assert R.policy_texts(plain.r.job) == plain.r.job["texts"]


# --- the preflight of every declared policy role (L2-RESET-PREFLIGHT-1114) -----------


def preflight_through_the_entry(s, tmp_path, monkeypatch, busy_port=None):
    """The real ``fr3_run._subtask_services`` with only ``subtask_units.for_run``
    (the systemd, /proc and nvidia-smi seam) replaced by fakes: it records what
    the entry passes and builds the real ``Services`` from it."""
    from test_subtask_units import Rig

    from levi.automatic import subtask_units as U

    seen = {}

    def for_run(vlm_binding, *, policies, say, t, **kwargs):
        seen["policies"] = policies
        rig = Rig(
            tmp_path / "preflight-rig",
            policy_ports=tuple(p["port"] for p in policies.values()),
        )
        if busy_port is not None:
            rig.proc.spawn(["foreign"], "0::/user.slice/other.scope", busy_port)
        seen["rig"] = rig
        return rig.services

    monkeypatch.setattr(U, "for_run", for_run)
    return seen


def test_the_entry_hands_every_declared_policy_role_to_the_preflight(
    tmp_path, monkeypatch
):
    s, _, _ = reset_swap(tmp_path, monkeypatch)
    # to_subtask() put a stand-in in place of the real function: undo that one
    monkeypatch.setattr(R, "_subtask_services", REAL_SUBTASK_SERVICES)
    seen = preflight_through_the_entry(s, tmp_path, monkeypatch)
    cfg = s.r.job["config"]
    switch = R.swap_config(s.r.binding, s.r.job)
    R._subtask_services(s.r.binding, s.r.job, cfg, None, switch)
    ports = {r: e["port"] for r, e in s.r.binding["policies"].items()}
    assert set(seen["policies"]) == {"forward", "reset"}
    assert {r: p["port"] for r, p in seen["policies"].items()} == ports
    assert {r: p["unit"] for r, p in seen["policies"].items()} == {
        "forward": "levi-aeri-policy-forward",
        "reset": "levi-aeri-policy-reset",
    }
    assert {p["mem_fraction"] for p in seen["policies"].values()} == {0.22}


def test_a_busy_reset_port_refuses_before_the_forward_loop_starts(
    tmp_path, monkeypatch
):
    from levi.automatic import subtask_units as U

    s, _, queries = reset_swap(tmp_path, monkeypatch)
    monkeypatch.setattr(R, "_subtask_services", REAL_SUBTASK_SERVICES)
    reset_port = s.r.binding["policies"]["reset"]["port"]
    assert reset_port != s.r.binding["policies"]["forward"]["port"]
    seen = preflight_through_the_entry(s, tmp_path, monkeypatch, busy_port=reset_port)
    with pytest.raises(U.ServiceFailed, match=f"port {reset_port}") as exc:
        s.execute()
    assert exc.value.step == 1 and exc.value.code == "port_held"
    # Nothing started, nothing moved, nothing asked of a policy.
    assert s.unit_calls() == []
    assert not any(c[0] == "env" for c in s.r.calls)
    assert queries == []
    assert seen["rig"].lock.held is False


def test_a_free_reset_port_lets_the_preflight_pass(tmp_path, monkeypatch):
    s, _, _ = reset_swap(tmp_path, monkeypatch)
    monkeypatch.setattr(R, "_subtask_services", REAL_SUBTASK_SERVICES)
    preflight_through_the_entry(s, tmp_path, monkeypatch)
    cfg = s.r.job["config"]
    switch = R.swap_config(s.r.binding, s.r.job)
    services = R._subtask_services(s.r.binding, s.r.job, cfg, None, switch)
    services.preflight()
    ports = tuple(e["port"] for e in s.r.binding["policies"].values())
    assert services.policy_ports == ports
