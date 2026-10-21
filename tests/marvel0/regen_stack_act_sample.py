"""Regenerates ``src/components/marvel0/__tests__/marvel0-stack-act.samples.json``:
the exact answers of the arm stack's routes (``GET /api/levi/marvel0/stack``
with its ``act`` part, and the writes of ``stack_act_api.py``) on a fully fake
marvel0, which the frontend tests parse with the page's own readers, so the
page and the server agree on one shape.

    uv run --no-sync python tests/marvel0/regen_stack_act_sample.py

Every answer is built in process: the fake machine of ``stack_act_machine.py``
(temporary folders, a scripted ``systemctl``/``systemd-run``, the socket tables
and the health file as values, a clock moved by hand) behind a ``Stack`` with
its ``Act``, the routes on a test client. No unit is started, no process
signalled, no port opened, nothing of the real machine looked at.
``tests/marvel0/test_marvel0_stack_act_sample.py`` fails, naming this command,
when the committed file differs from what this builds.

The file has three parts: ``views`` (the whole stack document in each state a
page shows), ``answers`` (what a write answers) and ``refusals`` (the status
and detail of a refused write)."""

import contextlib
import json
import os
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from regen_sample import FOLDER, ROOT, fixed_zone
from stack_act_machine import (
    CONTROLLERS_OUT,
    SERVER_PORT,
    ActMachine,
    ready_lines,
)
from test_marvel0_stack import _sock

from levi.marvel0 import net, stack, stack_act, stack_act_api, stack_api

SAMPLE = FOLDER / "marvel0-stack-act.samples.json"
PREFIX = "/api/levi/marvel0/stack"
MARKER_AGE_S = 90.0


@contextlib.contextmanager
def machine(**kw):
    """A fresh fake machine and a client on its routes (patches undone after)."""
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as patch:
        m = ActMachine(Path(tmp), **kw)
        real_view = stack.marker_view

        def marker_view(path, now):
            # ctime cannot be set back on a real file: the sample reads the
            # mtime for both ages, so the text does not depend on the clock.
            out = real_view(path, now)
            return {**out, "ctime_age_s": out["age_s"]}

        patch.setattr(stack, "marker_view", marker_view)

        # A marker made during the build gets the machine's clock as its time,
        # not the real one, so its age is the same on every build.
        made = m.act._do_touch

        def touch(r):
            out = made(r)
            os.utime(m.marker_path, (m.t, m.t))
            m.act._remember_marker("created")
            return out

        patch.setattr(m.act, "_do_touch", touch)
        patch.setattr(stack_api, "supervisor", lambda: m.sup)
        patch.setattr(stack_api, "_VIEW", [])
        patch.delenv("LEVI_UI_TOKEN", raising=False)
        app = FastAPI()
        app.include_router(stack_api.router)
        app.include_router(stack_act_api.router)
        m.client = TestClient(app)
        yield m


def view(m):
    stack_api.invalidate()
    return m.client.get(PREFIX).json()


def post(m, path, body, **kw):
    """``(status, json)`` of a write."""
    answer = m.client.post(f"{PREFIX}/{path}", json=body, **kw)
    return answer.status_code, answer.json()


def refusal(m, path, body):
    status, data = post(m, path, body)
    assert status >= 400, (path, status, data)
    return {"status": status, "detail": data["detail"]}


def keep_fresh(m):
    """The monitor keeps writing: the health file is as old as the clock says."""
    state = m.act._context(m.sup._read())
    health = state.r["health"]
    m.health(
        hardware=health.get("hardware_active"),  # (null stays null: no manager)
        controller=health.get("controller_active"),
        red=health["state"] == "red",
        mode=health.get("robot_mode_name"),
    )


def views() -> tuple:
    out, answers, refusals = {}, {}, {}

    with machine() as m:
        out["idle"] = view(m)

    with machine() as m:
        m.m_ready()
        out["monitor_ready"] = view(m)
        refusals["start_a_blocked"] = refusal(
            m,
            "a/start",
            {
                "request_id": "start-a-blocked1",
                "confirm": "start arm stack",
                "attest": {"desk_done": True, "estop_hand": True},
            },
        )

    with machine() as m:
        m.ready_for_a(aged_s=MARKER_AGE_S)
        out["ready_to_start_a"] = view(m)
        refusals["start_a_without_attest"] = refusal(
            m,
            "a/start",
            {
                "request_id": "start-a-noattest",
                "confirm": "start arm stack",
                "attest": {"desk_done": True},
            },
        )
        status, answers["start_a"] = post(
            m,
            "a/start",
            {
                "request_id": "start-a-000001",
                "confirm": "start arm stack",
                "attest": {"desk_done": True, "estop_hand": True},
            },
        )
        assert status == 202, answers["start_a"]
        m.emit("a", *ready_lines("a")[:2])
        m.t += 12
        keep_fresh(m)
        out["a_starting"] = view(m)
        m.emit("a", *ready_lines("a")[2:])
        m.a_up(lines=False)
        keep_fresh(m)
        out["a_ready"] = view(m)
        refusals["start_c_before_b"] = refusal(
            m,
            "c/start",
            {
                "request_id": "start-c-too-early",
                "confirm": "start robot server",
                "attest": {"estop_hand": True, "hands_clear": True, "no_client": True},
            },
        )
        status, answers["start_b"] = post(
            m,
            "b/start",
            {
                "request_id": "start-b-000001",
                "confirm": "start gripper stack",
                "attest": {"hands_clear": True},
            },
        )
        assert status == 202, answers["start_b"]
        m.emit("b", *ready_lines("b")[:2])
        m.t += 4
        keep_fresh(m)
        out["b_starting"] = view(m)
        m.emit("b", *ready_lines("b")[2:])
        out["b_ready"] = view(m)
        for action in ("open", "close"):
            status, answers[f"gripper_{action}"] = post(
                m,
                "b/gripper",
                {
                    "request_id": f"gripper-{action}-0001",
                    "action": action,
                    "confirm": f"gripper {action}",
                    "attest": {"hands_clear": True},
                },
            )
            assert status == 202, answers[f"gripper_{action}"]
        out["gripper_success"] = view(m)
        m.gripper_reached, m.gripper_stalled = False, True
        status, _ = post(
            m,
            "b/gripper",
            {
                "request_id": "gripper-close-0002",
                "action": "close",
                "confirm": "gripper close",
                "attest": {"hands_clear": True},
            },
        )
        assert status == 202
        out["gripper_failed"] = view(m)
        m.gripper_reached, m.gripper_stalled = True, False
        status, answers["preflight_c"] = post(
            m, "c/preflight", {"request_id": "pre-c-000001"}
        )
        assert status == 202, answers["preflight_c"]
        m.controllers_out, m.controllers_code = CONTROLLERS_OUT, 0
        post(m, "c/preflight", {"request_id": "pre-c-000002"})
        status, answers["start_c"] = post(
            m,
            "c/start",
            {
                "request_id": "start-c-000001",
                "confirm": "start robot server",
                "attest": {"estop_hand": True, "hands_clear": True, "no_client": True},
            },
        )
        assert status == 202, answers["start_c"]
        m.emit("c", *ready_lines("c", SERVER_PORT)[:1])
        m.t += 3
        keep_fresh(m)
        out["c_starting"] = view(m)
        m.emit("c", *ready_lines("c", SERVER_PORT)[1:])
        m.c_up(lines=False)
        out["c_ready"] = view(m)
        m.socks.append(_sock(40000, net.ESTABLISHED, remote_port=SERVER_PORT))
        out["c_ready_with_client"] = view(m)
        refusals["stop_c_with_client"] = refusal(
            m,
            "c/stop",
            {"request_id": "stop-c-client-01", "confirm": "stop robot server"},
        )
        m.socks.pop()
        status, answers["stop_c"] = post(
            m, "c/stop", {"request_id": "stop-c-000001", "confirm": "stop robot server"}
        )
        assert status == 202, answers["stop_c"]
        out["c_stopped_arm_held"] = view(m)

    with machine() as m:
        m.ready_for_a(aged_s=MARKER_AGE_S)
        m.act.start("a", m.start_body("a"))
        m.emit(
            "a",
            "[ros2_control_node-2] [WARN] [1.0] [controller_manager]: "
            "Could not enable FIFO RT scheduling policy: with error number <1>(Operation not permitted).",
        )
        m.t += 6
        keep_fresh(m)
        out["a_failed"] = view(m)

    with machine() as m:
        status, answers["sequence_start"] = post(
            m,
            "sequence/start",
            {
                "request_id": "sequence-000001",
                "confirm": "start arm stack",
                "attest": {"desk_done": True, "estop_hand": True},
            },
        )
        assert status == 202, answers["sequence_start"]
        out["sequence_running"] = view(m)
        a_is_up = False
        for _ in range(60):
            seq = m.act.advance()
            if seq["state"] != "running":
                break
            if seq.get("step") == "wait":
                m.t += 40  # the marker's wait passes
                keep_fresh(m)
            if "a" in m.units and not a_is_up:
                m.a_up()  # the unit printed its lines
                a_is_up = True
        out["sequence_waiting_b"] = view(m)

    chain_ticks = {"desk_done": True, "estop_hand": True, "hands_clear": True}
    chain_ticks["no_client"] = True

    def chain_press(m, rid, **options):
        return post(
            m,
            "sequence/start",
            {
                "request_id": rid,
                "confirm": "start arm stack",
                "attest": chain_ticks,
                "through": "c",
                **options,
            },
        )

    def until(m, ready, stop):
        """The chain's ticks, with the machine answering, until ``stop(seq)``.
        Every look takes half a second of the machine's time, so the steps of the
        sample say how long they took."""
        advance = m.act.advance

        def slow():
            out = advance()
            m.t += 0.5
            return out

        m.act.advance = slow
        try:
            return m.run_chain(ready=ready, stop_at=stop)
        finally:
            m.act.advance = advance

    with machine() as m:
        # "Start whole stack": one request carries every confirmation (A: Desk
        # done, e-stop hand; B: hands clear; C: e-stop hand, hands clear, no
        # client) and the guided start goes on by itself through B and C.
        m.m_ready()
        m.no_manager()
        status, answers["sequence_start_chain"] = chain_press(m, "chain-000001")
        assert status == 202, answers["sequence_start_chain"]
        out["chain_running_m"] = view(m)
        until(m, ("a", "b", "c"), lambda seq: seq["step"] == "b")
        out["chain_running_b"] = view(m)
        until(m, ("a", "b", "c"), lambda seq: seq["step"] == "c_preflight")
        out["chain_running_c_check"] = view(m)
        until(m, ("a", "b", "c"), lambda seq: seq["step"] == "c")
        out["chain_running_c"] = view(m)
        until(m, ("a", "b", "c"), None)
        keep_fresh(m)
        out["chain_done"] = view(m)
        # pressed again with everything up: nothing to do, 200
        status, answers["sequence_start_already"] = chain_press(m, "chain-000002")
        assert status == 200, answers["sequence_start_already"]
        # a single start while a guided stop is live is refused
        post(
            m,
            "sequence/stop",
            {
                "request_id": "sequence-stop-0010",
                "confirm": "stop whole stack",
                "attest": {},
            },
        )
        refusals["start_c_while_stopping"] = refusal(
            m,
            "c/start",
            {
                "request_id": "start-c-stopping",
                "confirm": "start robot server",
                "attest": {"estop_hand": True, "hands_clear": True, "no_client": True},
            },
        )
        refusals["marker_touch_while_stopping"] = refusal(
            m,
            "marker",
            {"request_id": "marker-stopping", "op": "touch", "confirm": "set marker"},
        )
        out["stop_all_started_blocks_starts"] = view(m)

    running_job = {
        "state": "running",
        "count": 2,
        "kinds": ["pytest-dir"],
        "empty": 1,
        "why": None,
    }
    with machine() as m:
        # Heavy jobs of the gate still run when the marker is old enough: A waits
        # for them (their end is not known), the document says how many and for
        # how long it has waited; the start ends when they do not stop.
        m.m_ready()
        m.no_manager()
        m.heavy = running_job
        status, answers["sequence_start_chain_heavy"] = chain_press(m, "chain-000010")
        assert status == 202
        until(m, ("a", "b", "c"), lambda seq: seq["step"] == "wait")
        m.t += 12
        keep_fresh(m)
        m.act.advance()
        out["chain_waiting_heavy"] = view(m)
        m.t += stack_act.HEAVY_WAIT_CAP_S
        keep_fresh(m)
        m.act.advance()
        out["chain_failed_heavy"] = view(m)

    with machine() as m:
        # The heavy jobs cannot be told (no log, no answer from systemd): the fixed
        # wait of old applies and the document says so.
        m.m_ready()
        m.no_manager()
        m.heavy = {
            "state": "unknown",
            "count": 0,
            "kinds": [],
            "empty": 0,
            "why": "log_missing",
        }
        chain_press(m, "chain-000011")
        until(m, ("a", "b", "c"), lambda seq: seq["step"] == "wait")
        m.t += 5
        keep_fresh(m)
        m.act.advance()
        out["chain_waiting_heavy_unknown"] = view(m)

    with machine() as m:
        # The careful path: the chain waits before B and before C for the
        # person's own start (a fresh window with its own confirmations).
        m.m_ready()
        m.no_manager()
        chain_press(m, "chain-000003", pause_before_b=True, pause_before_c=True)
        until(m, ("a",), None)  # (waiting_confirm is not "running": it returns)
        out["chain_paused_b"] = view(m)

    with machine() as m:
        # Pausing before C only: B runs by itself, then the chain waits.
        m.m_ready()
        m.no_manager()
        chain_press(m, "chain-000006", pause_before_c=True)
        until(m, ("a", "b"), None)
        keep_fresh(m)
        out["chain_paused_c"] = view(m)

    with machine() as m:
        # B never becomes ready: the chain ends at B with the reason; C was
        # not reached; what is up (M, A) stays up.
        m.m_ready()
        m.no_manager()
        chain_press(m, "chain-000004")
        until(m, ("a",), lambda seq: seq["step"] == "b" and "b" in m.units)
        m.t += m.specs.units["b"].ready_timeout_s + 1
        keep_fresh(m)
        m.act.advance()
        out["chain_failed_b"] = view(m)

    with machine() as m:
        # The request without a confirmation a stage it will reach needs.
        m.m_ready()
        m.no_manager()
        refusals["sequence_start_chain_without_attest"] = refusal(
            m,
            "sequence/start",
            {
                "request_id": "chain-000005",
                "confirm": "start arm stack",
                "attest": {"desk_done": True, "estop_hand": True},
                "through": "c",
            },
        )

    with machine() as m:
        # "Stop whole stack" cannot tell whether C runs: it stops there.
        m.start_a(aged_s=MARKER_AGE_S)
        keep_fresh(m)
        m.table_ok = False
        post(
            m,
            "sequence/stop",
            {
                "request_id": "sequence-stop-0011",
                "confirm": "stop whole stack",
                "attest": {},
            },
        )
        m.act.advance()
        out["stop_all_c_unknown"] = view(m)

    with machine() as m:
        # A stop that was asked for and is carried by nobody (the page was
        # restarted in the middle of it): the unit is still there.
        m.all_up(aged_s=MARKER_AGE_S)
        keep_fresh(m)
        m.act._write_record("c", phase="stopping", stop_requested_at=m.t)
        out["stop_unfinished"] = view(m)

    with machine() as m:
        # "Stop whole stack": C, then A, then B, one request. The guided stop
        # starts nothing, leaves M running, and each step waits for its unit.
        m.all_up(aged_s=MARKER_AGE_S)
        keep_fresh(m)
        out["stop_all_ready"] = view(m)
        status, answers["sequence_stop"] = post(
            m,
            "sequence/stop",
            {
                "request_id": "sequence-stop-0001",
                "confirm": "stop whole stack",
                "attest": {},
            },
        )
        assert status == 202, answers["sequence_stop"]
        out["stop_all_started"] = view(m)
        m.act.advance()  # C is told to stop
        keep_fresh(m)
        m.act.advance()  # C is gone: A is next
        keep_fresh(m)
        out["stop_all_running"] = view(m)
        for _ in range(8):
            if m.act.advance()["state"] != "running":
                break
            keep_fresh(m)
        out["stop_all_done"] = view(m)

    with machine() as m:
        # A stop that fails ends the sequence at its step, with the reason;
        # pressing the button again skips what is already stopped.
        m.all_up(aged_s=MARKER_AGE_S)
        m.stop_fails = {"a"}
        post(
            m,
            "sequence/stop",
            {
                "request_id": "sequence-stop-0002",
                "confirm": "stop whole stack",
                "attest": {},
            },
        )
        for _ in range(8):
            if m.act.advance()["state"] != "running":
                break
            keep_fresh(m)
        out["stop_all_failed"] = view(m)

    with machine() as m:
        # Refused up front: a client is connected to C (the override phrase of
        # C's own stop is not part of the sequence), a hosted run is active.
        m.all_up(aged_s=MARKER_AGE_S)
        m.socks.append(_sock(40000, net.ESTABLISHED, remote_port=SERVER_PORT))
        out["stop_all_blocked"] = view(m)
        refusals["sequence_stop_with_client"] = refusal(
            m,
            "sequence/stop",
            {
                "request_id": "sequence-stop-0003",
                "confirm": "stop whole stack",
                "attest": {},
            },
        )
        m.socks.pop()
        m.run_active = True
        refusals["sequence_stop_run_active"] = refusal(
            m,
            "sequence/stop",
            {
                "request_id": "sequence-stop-0004",
                "confirm": "stop whole stack",
                "attest": {},
            },
        )
        m.run_active = False
        refusals["sequence_stop_wrong_phrase"] = refusal(
            m,
            "sequence/stop",
            {"request_id": "sequence-stop-0005", "confirm": "stop", "attest": {}},
        )
        # a stop that was asked for can be cancelled; nothing further is stopped
        post(
            m,
            "sequence/stop",
            {
                "request_id": "sequence-stop-0006",
                "confirm": "stop whole stack",
                "attest": {},
            },
        )
        status, answers["sequence_cancel"] = post(
            m, "sequence/cancel", {"request_id": "sequence-cancel-01"}
        )
        assert status == 200, answers["sequence_cancel"]
        out["stop_all_cancelled"] = view(m)

    with machine() as m:
        # Nothing of the stack is running: the button is off, and a press that
        # still arrives is answered "already stopped".
        m.m_ready()
        out["stop_all_nothing"] = view(m)
        status, answers["sequence_stop_nothing"] = post(
            m,
            "sequence/stop",
            {
                "request_id": "sequence-stop-0007",
                "confirm": "stop whole stack",
                "attest": {},
            },
        )
        assert status == 200, answers["sequence_stop_nothing"]

    with machine() as m:
        # A is up and the check whether a gripper stack runs (made before A) is
        # older than its validity: B reads unknown, the start button is there
        # (a press makes the check), and the press starts nothing.
        m.start_a(aged_s=MARKER_AGE_S)
        m.t += stack_act.B_PROBE_TTL_S + 1
        m.a_up(lines=False)
        out["b_check_old"] = view(m)
        refusals["start_b_check_old"] = refusal(
            m,
            "b/start",
            {
                "request_id": "start-b-old-check",
                "confirm": "start gripper stack",
                "attest": {"hands_clear": True},
            },
        )
        out["b_check_made"] = view(m)

    with machine() as m:
        # The same, by the button "check the gripper stack" (``b/probe``): it asks
        # B's controller manager only, touches no FCI port, and is allowed with A
        # up. ``checks.can_probe_b`` says when the button is there.
        m.start_a(aged_s=MARKER_AGE_S)
        m.t += stack_act.B_PROBE_TTL_S + 1
        m.a_up(lines=False)
        assert view(m)["act"]["checks"]["can_probe_b"] is True
        status, answers["probe_b"] = post(
            m, "b/probe", {"request_id": "probe-b-000001"}
        )
        assert status == 202, answers["probe_b"]
        out["b_check_by_button"] = view(m)
        m.start_b()
        refusals["probe_b_with_unit"] = refusal(
            m, "b/probe", {"request_id": "probe-b-000002"}
        )

    with machine() as m:
        # The FCI check's own socket (TIME_WAIT) lingers: the arm's sockets read
        # active with no A anywhere, and the page says so, not "foreign".
        m.fci_time_wait = True
        m.ready_for_a(aged_s=MARKER_AGE_S)
        out["a_check_sockets_draining"] = view(m)

    with machine() as m:
        # M was restarted: the health file is old. C is the page's own and up;
        # it cannot be vouched for, and A and B still wait for it.
        m.all_up(aged_s=MARKER_AGE_S)
        m.doc()  # (the first look latches what is ready)
        m.t += 3600
        out["c_health_lost"] = view(m)

    with machine(enabled=False) as m:
        out["control_disabled"] = view(m)
        refusals["control_disabled"] = refusal(
            m,
            "a/start",
            {
                "request_id": "start-a-disabled1",
                "confirm": "start arm stack",
                "attest": {"desk_done": True, "estop_hand": True},
            },
        )

    return out, answers, refusals


WIDTH = 80


def _flat(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _lines(value, indent, prefix, comma) -> list:
    """``value`` as prettier (``bun run format``) prints JSON, so the committed
    file passes ``format:check``: objects always open, an array of scalars on
    one line when it fits in ``WIDTH`` columns (with its key and comma), an
    array that holds an object always open."""
    pad = "  " * indent
    tail = "," if comma else ""
    if isinstance(value, dict) and value:
        out = [f"{pad}{prefix}{{"]
        items = list(value.items())
        for n, (key, item) in enumerate(items):
            out += _lines(item, indent + 1, f"{_flat(key)}: ", n < len(items) - 1)
        return [*out, f"{pad}}}{tail}"]
    if isinstance(value, list) and value:
        if not any(isinstance(item, (dict, list)) for item in value):
            flat = f"{pad}{prefix}[{', '.join(_flat(i) for i in value)}]{tail}"
            if len(flat) <= WIDTH:
                return [flat]
        out = [f"{pad}{prefix}["]
        for n, item in enumerate(value):
            out += _lines(item, indent + 1, "", n < len(value) - 1)
        return [*out, f"{pad}]{tail}"]
    return [f"{pad}{prefix}{_flat(value)}{tail}"]


def build() -> str:
    with fixed_zone():
        v, a, r = views()
    doc = {"views": v, "answers": a, "refusals": r}
    return "\n".join(_lines(doc, 0, "", False)) + "\n"


if __name__ == "__main__":
    SAMPLE.write_text(build())
    print(f"wrote {SAMPLE.relative_to(ROOT)}")
