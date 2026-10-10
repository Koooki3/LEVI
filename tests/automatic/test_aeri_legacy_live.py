"""The compatibility path to the live service (levi/automatic/adapters/
legacy_live.py): online judgement (C5) results mapped to AERI judgements,
and AERI states written as the client's session states (C2), checked
against the live service's own gate (gpumgr.gate) and cold-start guard."""

import time

import pytest
from aeri_sessions import RunPair
from live_helpers import template

from levi.automatic import state_machine as sm
from levi.automatic.adapters import legacy_live as L
from levi.domain import aeri
from levi.live import config as live_config
from levi.live import controller, gpumgr, online, sessions

RUN = "r-c5"
EP = f"{RUN}.forward.0002"
CLOCK = "host-mono:fake-boot"
SPEC = {"id": "generic-final", "version": 1}


def body(status, **kw):
    kw.setdefault("spec", SPEC if status == "ok" else None)
    return online.result(status, **kw)


def mapped(found):
    return L.from_c5(
        found,
        run_id=RUN,
        request_id="req-1",
        episode_id=EP,
        episode_role="forward",
        target="forward_goal",
        observed_from_step=0,
        observed_through_step=40,
        observed_through_ns=1_000,
        produced_ns=2_000,
        clock_domain=CLOCK,
    )


def decided(answer):
    spec = online.load_spec("generic-final.v1.json")
    verdict = online.decide(spec, answer)
    return body(
        "ok",
        answer=answer,
        reading=verdict["reading"],
        outcome=verdict["outcome"],
        undecided=verdict["undecided"],
        checks=verdict["checks"],
        model="qwen-fake",
    )


# --- C5 -> judgement: the golden table --------------------------------------------------------


@pytest.mark.parametrize(
    "answer, decision, reason",
    [
        (
            {"object_state": "resting_at_destination", "stable": "yes"},
            "confirmed",
            None,
        ),
        ({"object_state": "elsewhere", "stable": "yes"}, "rejected", None),
        ({"object_state": "in_gripper", "stable": "unclear"}, "rejected", None),
        # final_state reads "unclear" as a failure that is undecided: unknown,
        # never a rejection.
        (
            {"object_state": "unclear", "stable": "unclear"},
            "unknown",
            "model_undecided",
        ),
        (
            {"object_state": "resting_at_destination", "stable": "unclear"},
            "unknown",
            "model_undecided",
        ),
    ],
)
def test_answers_decided_by_the_live_rule_map_by_outcome_and_undecided(
    answer, decision, reason
):
    found = mapped(decided(answer))
    assert found.kind == "judgement"
    assert found.decision == decision and found.unknown_reason == reason
    assert found.provider == "vlm" and found.model_name == "qwen-fake"
    assert found.legacy_c5 is not None
    assert found.valid_until_ns - found.produced_ns == 5_000 * 1_000_000


def test_a_supported_reading_turned_failure_by_a_veto_is_a_rejection():
    found = mapped(
        body(
            "ok",
            answer={"object_state": "resting_at_destination", "stable": "yes"},
            reading="supported",
            outcome="failure",
            undecided=False,
        )
    )
    assert found.decision == "rejected"
    assert [(v.id, v.state) for v in found.vetoes] == [("c5-rule", "confirmed")]


def test_an_undecided_success_is_unknown_model_undecided():
    found = mapped(
        body(
            "ok",
            answer={"object_state": "unclear", "stable": "yes"},
            reading="unknown",
            outcome="success",
            undecided=True,
        )
    )
    assert found.decision == "unknown" and found.unknown_reason == "model_undecided"
    # Regression: the audit copy of C5's own values is kept (it used to be
    # dropped whenever the contract refused the combination).
    assert found.legacy_c5 is not None
    assert (found.legacy_c5.outcome, found.legacy_c5.undecided) == ("success", True)


def test_a_success_whose_fields_disagree_is_never_confirmed():
    found = mapped(
        body(
            "ok",
            answer={"object_state": "elsewhere", "stable": "yes"},
            reading="supported",
            outcome="success",
            undecided=False,
        )
    )
    assert found.decision == "unknown"
    assert found.unknown_reason == "conflicting_predicates"
    assert found.legacy_c5 is not None and found.legacy_c5.outcome == "success"


def test_a_decided_success_with_an_unclear_field_keeps_its_values():
    found = mapped(
        body(
            "ok",
            answer={"object_state": "resting_at_destination", "stable": "unclear"},
            reading="unknown",
            outcome="success",
            undecided=False,
        )
    )
    assert found.kind == "judgement" and found.decision == "unknown"
    assert found.legacy_c5.reading == "unknown"


@pytest.mark.parametrize(
    "wrong",
    [("confirmed", None, []), ("rejected", None, []), ("unknown", "occluded", [])],
)
def test_a_wrong_mapping_is_refused_not_passed_without_its_audit_copy(
    monkeypatch, wrong
):
    """The contract re-checks the mapping against legacy_c5; the adapter
    no longer strips legacy_c5 to get a wrong decision through."""
    monkeypatch.setattr(L, "decide", lambda *args: wrong)
    found = mapped(
        body(
            "ok",
            answer={"object_state": "unclear", "stable": "unclear"},
            reading="unknown",
            outcome="failure",
            undecided=True,
        )
    )
    assert found.kind == "unavailable" and found.code == "contract_violation"
    assert "undecided=True" in found.detail


@pytest.mark.parametrize("elapsed", ["abc", float("inf"), [1], 1e300])
def test_odd_elapsed_times_never_raise(elapsed):
    found = mapped(
        body(
            "ok",
            answer={"object_state": "elsewhere", "stable": "yes"},
            reading="contradicted",
            outcome="failure",
            undecided=False,
            elapsed=0,
        )
        | {"elapsed_s": elapsed}
    )
    assert found.kind == "judgement" and found.decision == "rejected"


@pytest.mark.parametrize(
    "status, reason, code, retryable",
    [
        (
            "unavailable",
            "gate_closed: policy_inferring: g/t is running",
            "gate_closed",
            True,
        ),
        ("unavailable", "gate_pending: episode_imminent: soon", "gate_closed", True),
        ("unavailable", "busy: another online judgement is in progress", "busy", True),
        ("unavailable", "service_busy: changing state", "busy", True),
        ("unavailable", "cold_start: vLLM is not running", "cold_start", False),
        ("unavailable", "vllm_starting: vLLM is still starting", "cold_start", False),
        ("unavailable", "no_room: no memory", "no_room", False),
        ("unavailable", "wake_failed: did not wake", "provider_error", False),
        ("unavailable", "vllm_failed: failed repeatedly", "provider_error", False),
        (
            "unavailable",
            "shutting_down: the live service is stopping",
            "shutting_down",
            False,
        ),
        ("unavailable", "something_new: ?", "provider_error", False),
        # The online judgement's timeout is status=error, not unavailable.
        ("error", "timeout: no answer within 20 s", "timeout", False),
        ("error", "invalid_answer: bad json", "invalid_answer", False),
        ("error", "model_error: RuntimeError: x", "model_error", False),
        ("error", "internal_error: KeyError", "provider_error", False),
        ("error", "invalid_request: images", "contract_violation", False),
        ("weird", None, "contract_violation", False),
    ],
)
def test_no_answer_maps_to_unavailable_never_unknown(status, reason, code, retryable):
    found = mapped(body(status, reason=reason))
    assert found.kind == "unavailable"
    assert found.code == code and found.retryable is retryable


def test_gate_closed_and_busy_are_degradations_not_errors():
    for reason in ("gate_closed: x", "busy: y"):
        found = mapped(body("unavailable", reason=reason))
        assert found.retryable and found.code in aeri.RETRYABLE_CODES


def test_malformed_results_fail_closed():
    assert mapped({"schema": "something.else"}).code == "contract_violation"
    assert (
        mapped(body("ok", reading="supported", outcome=None)).code == "invalid_answer"
    )
    other = body(
        "ok",
        spec={"id": "plates-release", "version": 3},
        reading="supported",
        outcome="success",
        answer={"object_state": "resting_at_destination", "stable": "yes"},
    )
    assert mapped(other).code == "not_supported"


def test_every_mapped_message_passes_the_contract_again():
    for answer in (
        {"object_state": "resting_at_destination", "stable": "yes"},
        {"object_state": "unclear", "stable": "unclear"},
    ):
        found = mapped(decided(answer))
        assert (
            aeri.parse(aeri.dump(found), "judgement", specs=aeri.BUILTIN_SPECS) == found
        )


# --- AERI state -> C2 session state, against the real gate ------------------------------------------


def cfg():
    c = live_config.Config()
    c.gpu.mode = "timeshare"
    return c


def session(state, task):
    class S:
        pass

    s = S()
    s.state, s.crashed, s.group, s.task_folder = state, False, "g", task
    s.updated_at, s.path, s.reset_wait_s = None, f"/s/g__{task}.json", None
    return s


def assert_truthful(state, gate):
    """Closed for inference exactly while a policy may infer. A finished
    run vouches for nothing: with a policy server still listening the live
    gate stays closed as ``unknown_client`` (its own caution, not ours)."""
    assert (gate.code == "policy_inferring") == (state in sm.POLICY_MAY_INFER), (
        state,
        gate,
    )
    if state not in sm.POLICY_MAY_INFER and state != "COMPLETED":
        assert gate.open, (state, gate)


@pytest.mark.parametrize("state", sm.STATES)
def test_the_gate_is_closed_exactly_while_a_policy_may_infer(state):
    files = L.legacy_sessions(state)
    assert set(files.values()) <= set(L.LEGACY_STATES)
    found = {("g", role): session(value, role) for role, value in files.items()}
    gate = gpumgr.gate(cfg(), "timeshare", found, True)
    assert_truthful(state, gate)

    class Stub:
        sessions = found

    unfinished = controller.Controller._evaluation_unfinished(Stub())
    # No vLLM cold start while the run is between or inside episodes.
    if state not in ("PREFLIGHT", "FAULT_LOCKED", "COMPLETED"):
        assert unfinished, state


def test_the_reset_policy_s_active_state_is_running_in_the_reset_file():
    assert L.legacy_sessions("RESET_ACTIVE") == {
        "reset": "running",
        "forward": "standby",
    }
    assert L.legacy_sessions("FORWARD_ACTIVE") == {
        "forward": "running",
        "reset": "standby",
    }
    assert L.legacy_state("COMPLETED", stopped=True) == "stopped"


def test_a_new_state_name_would_open_the_gate_which_is_why_none_is_written():
    """Design finding X1 §6: an unknown state passes the reader and opens the gate."""
    found = {("g", "reset"): session("reset_running", "reset")}
    assert gpumgr.gate(cfg(), "timeshare", found, True).open
    assert set(L.LEGACY_OF.values()) <= set(L.LEGACY_STATES)


def test_written_session_files_are_read_by_the_live_reader_and_gate(tmp_path_factory):
    root = tmp_path_factory.mktemp("rollouts")
    pair = RunPair(root, template(tmp_path_factory), run_id=RUN)
    for state in sm.STATES:
        files = L.legacy_sessions(state)
        active = L.ACTIVE_ROLE.get(state, "forward")
        now = time.time()
        pair.write(active, files[active], aeri_state=state, now=now)
        sessions._CACHE.clear()
        found = sessions.read_sessions([root], now=now + 1)
        assert sorted(s.state for s in found.values()) == sorted(
            [files[active], "standby"]
        )
        assert_truthful(state, gpumgr.gate(cfg(), "timeshare", found, True))
