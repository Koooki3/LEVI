"""Regenerates the samples of the hosted evaluation run that the frontend tests
parse with the page's own readers, so the page and the server agree on one shape:

* ``src/components/marvel0/__tests__/marvel0-run.samples.json``: the exact
  ``levi.marvel0.run.v1`` documents of ``GET /run`` in every state the page
  shows (``actions[...].{allowed, why_not, blind}``, the start checks, notices);
* ``src/components/marvel0/__tests__/marvel0-run.answers.json``: the answers of
  the other routes (challenge, start, key, hold, end, log) and the refusals
  (``{status, detail}``) the page words.

    uv run --no-sync python tests/marvel0/regen_run_sample.py

The documents are built in process by the pure functions of ``levi/marvel0/run.py``
(``derive`` and ``document``) from facts written out here; the answers by the
supervisor on the scripted machine of ``test_marvel0_run.py`` (temporary
folders, a fake clock, a client the script plays; ``killpg`` is recorded, not
sent). No client, process or socket is started and nothing of the real machine
is looked at. ``tests/marvel0/test_marvel0_run_sample.py`` fails, naming this
command, when a committed file differs from what this builds.

Keep this file in step with the document: a field added to ``run.document`` shows
up here by regenerating; a state added to ``run.STATES`` fails the test until it
has a sample."""

import tempfile
from pathlib import Path
from unittest import mock

import pytest
from regen_deploy_sample import _lines
from regen_sample import FOLDER, ROOT, fixed_zone
from test_marvel0_run import ATTEST, SP, Machine

from levi.marvel0 import run

SAMPLE = FOLDER / "marvel0-run.samples.json"
ANSWERS = FOLDER / "marvel0-run.answers.json"
NOW = 1_792_395_000.0  # 2026-10-19 07:30:00 UTC
MODEL = "pi05_fr3_all_step49999"
GROUP = f"{MODEL}_rtc_demo"
PROMPT = "pick the eggplant in the blue plate"
TASK = "pick_the_eggplant_in_the_blue_plate"
SESSION_ID = "0123456789abcdef0123456789abcdef"
RUN_ID = "20261019T073000-4f2a91"
GREEN = run.Monitor("ok", 0.4, NOW - 0.4)
RED = run.Monitor("red", 0.3, NOW - 0.3)
STALE = run.Monitor("offline", 7.2, NOW - 7.2)


def checks(**broken) -> list:
    """The form-independent start checks; ``broken`` maps a code to ``False``
    (refuses), ``None`` (unknown) or a dict of params."""
    rows = []
    for code in (
        "client_enabled",
        "product",
        "run_idle",
        "policy_ready",
        "no_connection",
        "no_session",
        "arm_lock",
        "monitor_green",
        "rollout_writable",
        "robot_server",
    ):
        ok = broken.get(code, True)
        params = {}
        if isinstance(ok, dict):
            ok, params = False, ok
        rows.append(run.check_row(code, ok, **params))
    return rows


def record(**extra) -> dict:
    return {
        "schema": run.RECORD_SCHEMA,
        "run_id": RUN_ID,
        "phase": "started",
        "created_epoch": NOW - 300.0,
        "group": GROUP,
        "task_folder": TASK,
        "model_id": MODEL,
        "rtc": True,
        "prompt": PROMPT,
        "session_id": SESSION_ID,
        **extra,
    }


def session(state, reason="", no=1, step=0, counted=0, last=None) -> dict:
    return {
        "schema": "levi.eval.session.v1",
        "session_id": SESSION_ID,
        "state": state,
        "reason": reason,
        "group": GROUP,
        "task_folder": TASK,
        "episode": {
            "no": no,
            "target": 5,
            "counted": counted,
            "demo_index": no,
            "step": step,
            "max_steps": 400,
        },
        "fr3": {
            "ok": True,
            "robot_mode_name": "Move",
            "hardware_active": True,
            "state_age_s": 0.1,
        },
        "last_episode": last,
    }


LAST = {"outcome": "success", "steps": 212, "duration_s": 24.6}


def facts(rec, sess=None, proc="running", log=None, age=300.0, **extra) -> run.Facts:
    return run.Facts(
        now=NOW,
        record=rec,
        proc=proc,
        session=sess,
        session_age_s=0.6 if sess is not None else None,
        log=log or run.LogView(),
        icanon=False,
        started_age_s=age,
        **extra,
    )


def built(f, monitor=GREEN, preflight=None, **ctx) -> dict:
    d = run.derive(f)
    context = run.Context(monitor=monitor, preflight=preflight or [], seq=7, **ctx)
    return run.document(f, d, context)


def samples() -> dict:
    gate1 = run.LogView(gate_ep=1)
    out = {}
    out["idle_ready"] = built(
        run.Facts(now=NOW, record=None, proc="gone"), preflight=checks()
    )
    out["preflight_failing"] = built(
        run.Facts(now=NOW, record=None, proc="gone"),
        preflight=checks(
            policy_ready={"state": "idle"},
            no_connection={"count": 2},
            arm_lock=False,
            monitor_green={"state": "offline"},
            no_session=None,
            robot_server=False,
        ),
    )
    out["disabled"] = built(
        run.Facts(now=NOW, record=None, proc="gone"),
        preflight=checks(client_enabled=False),
        enabled=False,
    )
    out["not_product"] = built(
        run.Facts(now=NOW, record=None, proc="gone"),
        preflight=checks(product=False),
        profile="fake",
        profile_code="profile.not_product_unit",
    )
    out["preflight"] = built(
        run.Facts(now=NOW, record=None, proc="gone", preflighting=True)
    )
    out["starting"] = built(facts(record(), None, age=42.0))
    out["starting_gate_unconfirmed"] = built(
        facts(record(), session("standby", run.GATE_REASON), log=run.LogView())
    )
    out["at_gate"] = built(
        facts(record(), session("standby", run.GATE_REASON), log=gate1),
        armed={"purpose": "enter", "expires_at": NOW + 21.5},
    )
    out["at_gate_enter_sent"] = built(
        facts(record(), session("standby", run.GATE_REASON), log=gate1),
        enter_locked=True,
        keyed=True,
        key_age_s=8.0,
    )
    out["at_gate_monitor_stale"] = built(
        facts(record(), session("standby", run.GATE_REASON), log=gate1),
        monitor=STALE,
    )
    out["homing"] = built(
        facts(
            record(),
            session("homing", "", no=1),
            log=run.LogView(gate_ep=1, home_over=1),
        )
    )
    out["running"] = built(
        facts(
            record(),
            session("running", "", no=1, step=137),
            log=run.LogView(gate_ep=1, home_over=1, home_reached=1),
        ),
        clients=1,
    )
    out["running_label_sent"] = built(
        facts(
            record(),
            session("running", "", no=1, step=219),
            log=run.LogView(gate_ep=1, home_reached=1),
        ),
        label_locked=True,
        keyed=True,
        clients=1,
    )
    out["running_monitor_red"] = built(
        facts(
            record(),
            session("running", "", no=1, step=88),
            log=run.LogView(gate_ep=1, home_reached=1),
        ),
        monitor=RED,
        clients=1,
    )
    out["running_second_client"] = built(
        facts(
            record(),
            session("running", "", no=1, step=88),
            log=run.LogView(gate_ep=1, home_reached=1),
        ),
        clients=2,
    )
    out["awaiting_label"] = built(
        facts(
            record(),
            session("running", "", no=2, step=400, counted=1, last=LAST),
            log=run.LogView(gate_ep=2, ended_ep=2, ended_outcome="timeout", judge_ep=2),
        ),
        clients=1,
    )
    out["finishing_episode"] = built(
        facts(
            record(),
            session("running", "", no=1, step=212, last=None),
            log=run.LogView(gate_ep=1, ended_ep=1, ended_outcome="success"),
        ),
        label_locked=True,
        clients=1,
    )
    out["finishing_run"] = built(
        facts(
            record(),
            session("stopped", "user_quit", no=2, counted=1, last=LAST),
            log=run.LogView(gate_ep=2, closing=True),
        )
    )
    out["held"] = built(
        facts(
            record(held_at=NOW - 12.0, held_by="person"),
            session("running", "", no=1, step=140),
            proc="stopped",
        ),
        clients=1,
    )
    out["held_monitor_red"] = built(
        facts(
            record(held_at=NOW - 3.0, held_by="monitor_red"),
            session("running", "", no=1, step=140),
            proc="stopped",
        ),
        monitor=RED,
    )
    out["done"] = built(
        facts(
            record(phase="ended", exit={"code": 0, "signal": None}),
            session("stopped", "user_quit", no=3, counted=2, last=LAST),
            proc="gone",
        ),
        preflight=checks(),
    )
    out["failed"] = built(
        facts(
            record(phase="ended", exit={"code": 1, "signal": None}),
            session("fault", "exception", no=1),
            proc="gone",
        ),
        preflight=checks(),
    )
    out["failed_no_gate"] = built(
        facts(
            record(
                phase="ended",
                exit={"code": None, "signal": 9},
                kill_reason="no_gate_in_time",
            ),
            None,
            proc="gone",
        ),
        preflight=checks(),
    )
    out["lost"] = built(
        facts(record(), session("running", "", no=1, step=90), keyboard=False)
    )
    out["lost_heartbeat"] = built(
        run.Facts(
            now=NOW,
            record=record(),
            proc="running",
            session=session("running", "", no=1, step=90),
            session_age_s=14.0,
            log=run.LogView(gate_ep=1),
            icanon=False,
            started_age_s=300.0,
        ),
        monitor=RED,
    )
    return out


def refusal(call, *args, **kw) -> dict:
    """``{"status", "detail"}`` of the refusal a call raises, as a route says it."""
    with pytest.raises(run.Refuse) as caught:
        call(*args, **kw)
    exc = run.http(caught.value)
    return {"status": exc.status_code, "detail": exc.detail}


class Counter:
    """Deterministic stand-ins for the random tokens and ids."""

    def __init__(self):
        self.n = 0

    def urlsafe(self, nbytes=16):
        self.n += 1
        return f"challenge-{self.n:04d}-" + "x" * 10

    def hex(self, nbytes=3):
        return "4f2a91"


def answers() -> dict:
    """The answers of the other routes, from the scripted machine."""
    out, refused = {}, {}
    ids = Counter()
    with (
        tempfile.TemporaryDirectory() as tmp,
        pytest.MonkeyPatch.context() as patch,
        mock.patch.object(run.secrets, "token_urlsafe", ids.urlsafe),
        mock.patch.object(run.secrets, "token_hex", ids.hex),
    ):
        m = Machine(Path(tmp), patch)
        form = m.form()
        out["challenge_start"] = m.sup.challenge({"purpose": "start", "form": form})
        token = out["challenge_start"]
        body = {
            "request_id": "req-sample-0001",
            "form": form,
            "challenge": token["challenge"],
            "confirm": token["confirm"],
            "attest": dict(ATTEST),
        }
        out["started"] = m.sup.start(body)
        out["started_again"] = m.sup.start(body)
        m.gate()
        m.look()
        out["challenge_enter"] = m.sup.challenge({"purpose": "enter"})
        out["key_sent"] = m.sup.key(
            {
                "request_id": "req-sample-0002",
                "action": "enter",
                "challenge": out["challenge_enter"]["challenge"],
                "confirm": "",
            }
        )
        m.set_session("running", no=1, step=40)
        m.say("Homing over 30 steps -> [0.4]", "Home reached.", "[ 40] [0.1 0.2]")
        m.look()
        out["challenge_label"] = m.sup.challenge({"purpose": "label"})
        out["hold"] = m.hold()
        out["hold_again"] = m.hold()
        out["log"] = m.sup.log(5)
        out["ended"] = m.end()
        refused["state_not_allowed"] = refusal(m.sup.challenge, {"purpose": "enter"})
    with (
        tempfile.TemporaryDirectory() as tmp,
        pytest.MonkeyPatch.context() as patch,
        mock.patch.object(run.secrets, "token_urlsafe", ids.urlsafe),
        mock.patch.object(run.secrets, "token_hex", ids.hex),
    ):
        m = Machine(Path(tmp), patch)
        form = m.form()
        token = m.sup.challenge({"purpose": "start", "form": form})
        good = {
            "request_id": "req-sample-0003",
            "form": form,
            "challenge": token["challenge"],
            "confirm": token["confirm"],
            "attest": dict(ATTEST),
        }
        refused["confirm_required"] = refusal(
            m.sup.start, {**good, "confirm": "start client"}
        )
        refused["attest_required"] = refusal(
            m.sup.start, {**good, "attest": {**ATTEST, "estop": False}}
        )
        refused["request_id_invalid"] = refusal(
            m.sup.start, {**good, "request_id": "short"}
        )
        refused["challenge_invalid"] = refusal(
            m.sup.start, {**good, "challenge": "never-issued"}
        )
        m.clock.advance(31)
        refused["challenge_expired"] = refusal(m.sup.start, good)
        m.clock.advance(-31)
        low = m.form(
            model_id=SP,
            prompt="pick screws of same size into the empty slot of screw box",
            min_z=0.03,
        )
        refused["min_z_below_task_floor"] = refusal(
            m.sup.challenge, {"purpose": "start", "form": low}
        )
        refused["max_steps_out_of_range"] = refusal(
            m.sup.challenge, {"purpose": "start", "form": m.form(max_steps=10**6)}
        )
        m.deploy = m.ready_view(established=2)
        m.health(red=True)
        refused["preflight_failed"] = refusal(
            m.sup.challenge, {"purpose": "start", "form": form}
        )
        m.deploy = m.ready_view()
        m.health()
        m.enabled = False
        refused["run_disabled"] = refusal(
            m.sup.challenge, {"purpose": "start", "form": form}
        )
        m.enabled = True
        m.profile = "0::/user.slice/session-3.scope\n"
        refused["not_product"] = refusal(
            m.sup.challenge, {"purpose": "start", "form": form}
        )
        m.profile = (
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
            "levi-product.service\n"
        )
        m.start()
        m.gate()
        m.look()
        m.press("enter")
        m.set_session("running", no=1, step=3)
        m.look()
        m.press("success")
        refused["key_locked"] = refusal(m.sup.challenge, {"purpose": "label"})
        refused["state_not_allowed"] = refusal(m.sup.challenge, {"purpose": "enter"})
        refused["busy"] = _busy(m)
        m.hold()
        refused["confirm_required_end"] = refusal(
            m.sup.end, {"request_id": m.rid(), "confirm": "end client"}
        )
    with (
        tempfile.TemporaryDirectory() as tmp,
        pytest.MonkeyPatch.context() as patch,
        mock.patch.object(run.secrets, "token_urlsafe", ids.urlsafe),
        mock.patch.object(run.secrets, "token_hex", ids.hex),
    ):
        m = Machine(Path(tmp), patch)
        m.start()
        m.gate()
        m.look()
        m.press("enter")
        refused["enter_locked"] = refusal(m.sup.challenge, {"purpose": "enter"})
        m.set_session("standby", run.GATE_REASON, no=2)
        m.say('\n=== ready | ep 2 | 1/3 | "x" ===')
        m.look()
        m.health(red=True)
        refused["monitor_red"] = refusal(m.sup.challenge, {"purpose": "enter"})
        m.health(age=5)
        refused["monitor_not_green"] = refusal(m.sup.challenge, {"purpose": "enter"})
    text_out = {"answers": out, "refusals": refused}
    return text_out


def _busy(m) -> dict:
    assert m.sup._guard.acquire(blocking=False)
    try:
        return refusal(
            m.sup.end, {"request_id": m.rid(), "confirm": run.PHRASES["end"]}
        )
    finally:
        m.sup._guard.release()


def render(value, tmp_names=()) -> str:
    text = "\n".join(_lines(value, 0, "", "")) + "\n"
    for name in tmp_names:
        text = text.replace(name, "/path/to")
    return text


def build() -> str:
    with fixed_zone():
        return render(samples())


def build_answers() -> str:
    with fixed_zone():
        return render(answers())


if __name__ == "__main__":
    SAMPLE.write_text(build())
    print(f"wrote {SAMPLE.relative_to(ROOT)}")
    ANSWERS.write_text(build_answers())
    print(f"wrote {ANSWERS.relative_to(ROOT)}")
