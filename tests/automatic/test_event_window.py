"""CPU event provenance plus installed standalone C5 client; never load robot SDK."""

import base64
import hashlib
import importlib.util
import json
import socket
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS

import cv2
import numpy as np
import pytest
from test_provider_model import setup as setup  # noqa: PLC0414

from levi.automatic import event_window as W
from levi.automatic import fr3_providers as P
from levi.automatic.fr3_media import Media
from levi.events.stream import StreamConfig
from levi.live import online


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("event test attempted network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def sdk():
    # Optional local integration: this helper imports no hardware SDK. The
    # source-ledger regressions below remain runnable without openpi installed.
    paths = [
        p / "openpi/examples/fr3_local/levi_online_judge.py" for p in Path.cwd().parents
    ]
    path = next((p for p in paths if p.is_file()), None)
    if path is None:
        pytest.skip("installed standalone FR3 C5 client is unavailable")
    spec = importlib.util.spec_from_file_location("event_window_test_c5", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def rig(tmp_path, *, max_history=8):
    clock = NS(domain="host-mono:event-test", stamp=1_000_000_000)
    clock.now = lambda: clock.stamp
    ctx = NS(
        stream="epoch-1",
        episode_id="event-run.forward.0000",
        role="forward",
        last_step=0,
        rollout=NS(path=tmp_path),
    )
    pixels = np.zeros((16, 16, 3), dtype=np.uint8)
    reads = []

    def observe():
        reads.append(ctx.last_step)
        pixels.fill((ctx.last_step + 1) * 10)
        return {
            "observed_ns": clock.stamp,
            "images_bgr": {"side": pixels, "wrist": pixels},
            "pose": [0.0] * 7,
            "robot_state": {"gripper_position": 1.0},
            "state_fresh": True,
        }

    observer = W.EventObserver(
        NS(observe=observe), clock, step_of=lambda: ctx.last_step
    )
    observer.begin(ctx.stream)
    config = StreamConfig(max_history=max_history)
    ledger = W.Ledger(observer, lambda: ctx, config, "event-run", cv2)

    def stage(step):
        ctx.last_step = step
        clock.stamp += 100_000_000
        obs = observer.observe()
        ledger.stage(tmp_path, obs)
        return obs

    return NS(
        clock=clock,
        ctx=ctx,
        pixels=pixels,
        reads=reads,
        observer=observer,
        ledger=ledger,
        stage=stage,
        config=config,
    )


def request(r, lo=0):
    return {
        "request_id": "window-1",
        "run_id": "event-run",
        "episode_id": r.ctx.episode_id,
        "episode_role": "forward",
        "target": "forward_goal",
        "event_ids": ["candidate-1"],
        "observed_from_step": lo,
        "observed_through_step": r.ctx.last_step,
        "observed_through_ns": r.clock.stamp,
    }


def verifier(r, sdk, *, offsets=(-0.2, 0), start=()):
    spec = sdk.JudgeSpec(
        "http://unused",
        "generic-final",
        1,
        [("side", offsets), ("wrist", offsets)],
        1280,
        start_roles=start,
    )
    judge = sdk.OnlineJudge(spec)
    job = {"config": NS(control_period_ns=100_000_000), "texts": {"forward": "stack"}}
    media = NS(
        event_ledger=lambda ep: r.ledger if ep == r.ctx.episode_id else None, buffers={}
    )
    v = P.OnlineVerifier(
        sdk,
        judge,
        r.clock,
        media,
        job,
        robot=NS(observe=lambda: pytest.fail("extra event observation")),
    )
    return v, judge


def answer(outcome="success"):
    return online.result(
        "ok",
        outcome=outcome,
        undecided=False,
        reading="supported" if outcome == "success" else "contradicted",
        spec={"id": "generic-final", "version": 1},
        answer={
            "object_state": "resting_at_destination"
            if outcome == "success"
            else "elsewhere",
            "stable": "yes",
        },
        model="fake-cpu",
    )


def test_stage_is_pre_action_and_recording_rows_do_not_advance_control_step(tmp_path):
    r = rig(tmp_path)
    r.stage(0)
    assert r.ledger.rows[0].recording_row is None
    r.ledger.commit(1)
    r.stage(1)  # failed chunk: no commit
    r.stage(2)
    r.ledger.commit(2)
    assert [s.control_step for s in r.ledger.rows] == [0, 1, 2]
    assert [s.recording_row for s in r.ledger.rows] == [1, None, 2]
    assert all(
        s.phase == "pre_action" and s.episode_id == r.ctx.episode_id
        for s in r.ledger.rows
    )
    r.pixels.fill(200)  # SDK reuses and mutates the same array
    first = r.ledger.rows[0]
    assert all(
        hashlib.sha256(data).hexdigest() == digest for _, data, digest in first.images
    )
    image = cv2.imdecode(np.frombuffer(first.images[0][1], np.uint8), cv2.IMREAD_COLOR)
    assert int(image[0, 0, 0]) == 10


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate",
        "identity",
        "folder",
        "episode",
        "role",
        "clock",
        "future",
        "reversed",
        "image",
    ],
)
def test_stage_rejects_untrusted_or_replayed_source(tmp_path, fault):
    r = rig(tmp_path)
    r.stage(0)
    r.ctx.last_step = 1
    r.clock.stamp += 100_000_000
    obs = r.observer.observe()
    folder = tmp_path
    if fault == "duplicate":
        r.ledger.stage(folder, obs)
    elif fault == "identity":
        obs = dict(obs)
    elif fault == "folder":
        folder = tmp_path / "other"
    elif fault == "episode":
        r.ctx.episode_id = "event-run.forward.0001"
    elif fault == "role":
        r.ctx.role = "reset"
    elif fault == "clock":
        r.clock.domain = "host-mono:new-boot"
    elif fault == "future":
        r.observer.latest[-1]["observed_ns"] += 1
        obs["observed_ns"] += 1
    elif fault == "reversed":
        r.clock.stamp -= 200_000_000
        obs = r.observer.observe()
    elif fault == "image":
        r.pixels.fill(250)
    with pytest.raises(ValueError):
        r.ledger.stage(folder, obs)


def test_total_byte_budget_includes_pinned_start_and_count_eviction(
    tmp_path, monkeypatch
):
    r = rig(tmp_path, max_history=2)
    r.stage(0)
    source_bytes = r.ledger.retained_bytes
    monkeypatch.setattr(W, "MAX_FRAME_BYTES", source_bytes * 2)
    r.stage(1)
    r.stage(2)
    assert r.ledger.retained_bytes <= source_bytes * 2
    assert [s.control_step for s in r.ledger.rows] == [2]
    assert r.ledger.first.control_step == 0


@pytest.mark.parametrize(
    "outcome,expected", [("success", "confirmed"), ("failure", "rejected")]
)
def test_actual_client_sends_frozen_jpegs_without_executed_rows(
    tmp_path, sdk, monkeypatch, outcome, expected
):
    r = rig(tmp_path)
    for k in range(3):
        r.stage(k)
    v, _ = verifier(r, sdk, start=("start_side", "start_wrist"))
    sent = []
    entered, release = threading.Event(), threading.Event()

    def post(url, body, timeout, opener):
        sent.append(body)
        entered.set()
        assert release.wait(2)
        return answer(outcome)

    monkeypatch.setattr(sdk, "post_judge", post)
    req = request(r)
    ticket = v.submit(req)
    assert not isinstance(ticket, bytes)
    assert entered.wait(1)
    assert all(p.source.recording_row is None for p in ticket.proof.picks)
    assert all(not data for p in ticket.proof.picks for _, data, _ in p.source.images)
    req["event_ids"].append("mutated")
    req["observed_through_step"] = 999
    for k in range(3, 7):
        r.stage(k)
    release.set()
    result = json.loads(v.collect(ticket, timeout_ns=1_000_000_000))
    assert result["decision"] == expected and result["event_ids"] == ["candidate-1"]
    assert (result["observed_from_step"], result["observed_through_step"]) == (0, 2)
    assert result["observed_through_ns"] == 1_300_000_000
    assert v.collect(ticket, timeout_ns=0) is None
    body = sent[0]
    assert set(body) == {"schema", "task", "episode", "images"}
    assert body["episode"]["steps"] is None
    assert all(set(im) == {"role", "offset_s", "jpeg_b64"} for im in body["images"])
    assert all(
        hashlib.sha256(base64.b64decode(im["jpeg_b64"])).hexdigest() == p.jpeg_sha256
        for im, p in zip(
            body["images"],
            (*ticket.proof.picks, *ticket.proof.start_picks),
            strict=True,
        )
    )
    assert r.reads == list(range(7))


@pytest.mark.parametrize(
    "fault",
    [
        "short_offsets",
        "one_view",
        "one_source",
        "evicted",
        "new_epoch",
        "clock",
        "future",
        "gap",
    ],
)
def test_actual_picks_fail_closed_even_when_input_prefix_covers_window(
    tmp_path, sdk, monkeypatch, fault
):
    r = rig(tmp_path, max_history=2 if fault == "evicted" else 8)
    for k in range(3):
        r.stage(k)
    req = request(r)
    v, j = verifier(
        r, sdk, offsets=(-0.01, 0) if fault == "short_offsets" else (-0.2, 0)
    )
    if fault == "one_view":
        j.spec.views[1] = ("wrist", (0,))
    elif fault == "one_source":
        req["observed_from_step"] = 2
        j.spec.views = [("side", (0,)), ("wrist", (0,))]
    elif fault == "new_epoch":
        r.observer.begin("epoch-2")
    elif fault == "clock":
        r.clock.domain = "host-mono:new-boot"
    elif fault == "future":
        req["observed_through_ns"] += 1
    elif fault == "gap":
        r.ledger.rows[1] = replace(r.ledger.rows[1], control_step=8)
    monkeypatch.setattr(
        sdk, "post_judge", lambda *a: pytest.fail("uncovered event reached model")
    )
    result = json.loads(v.submit(req))
    assert result["kind"] == "unavailable" and result["code"] == "contract_violation"


def test_start_frame_does_not_expand_reported_bounds(tmp_path, sdk, monkeypatch):
    r = rig(tmp_path)
    for k in range(5):
        r.stage(k)
    v, _ = verifier(r, sdk, start=("start_side",))
    monkeypatch.setattr(sdk, "post_judge", lambda *a: answer())
    ticket = v.submit(request(r, lo=2))
    result = json.loads(v.collect(ticket, timeout_ns=1_000_000_000))
    assert result["observed_from_step"] == 2
    assert ticket.proof.start_picks[0].source.control_step == 0


@pytest.mark.parametrize("stop", ["cancel", "clock"])
def test_late_reply_cannot_resurrect_cancelled_or_changed_clock_ticket(
    tmp_path, sdk, monkeypatch, stop
):
    r = rig(tmp_path)
    r.stage(0)
    r.stage(1)
    v, _ = verifier(r, sdk, offsets=(-0.1, 0))
    entered, release = threading.Event(), threading.Event()

    def post(*args):
        entered.set()
        assert release.wait(2)
        return answer()

    monkeypatch.setattr(sdk, "post_judge", post)
    ticket = v.submit(request(r))
    assert entered.wait(1)
    if stop == "cancel":
        v.cancel(ticket, "operator stop")
    else:
        r.clock.domain = "host-mono:new-boot"
    release.set()
    result = v.collect(ticket, timeout_ns=1_000_000_000)
    assert (
        result is None
        if stop == "cancel"
        else json.loads(result)["kind"] == "unavailable"
    )


def test_actual_event_sender_uses_real_online_judge_and_current_request_owner(
    tmp_path, sdk, setup, monkeypatch
):
    r = rig(tmp_path)
    r.stage(0)
    r.stage(1)
    v, j = verifier(r, sdk, offsets=(-0.1, 0))
    server, gate = setup[4:]
    actual_spec = online.spec_answer(server.spec, server.config)
    j.spec = sdk.parse_spec("http://unused", actual_spec)
    calls = []

    def model(spec, folder, names):
        calls.extend(names)
        assert all(cv2.imread(str(Path(folder) / n)) is not None for n in names)
        return json.dumps(
            {"object_state": "resting_at_destination", "stable": "yes"}
        ), {}

    server._ask = model
    monkeypatch.setattr(
        sdk,
        "post_judge",
        lambda url, body, timeout, opener: server.handle(json.dumps(body).encode())[1],
    )
    ticket = v.submit(request(r))
    result = json.loads(v.collect(ticket, timeout_ns=2_000_000_000))
    assert result.get("decision") == "confirmed" and result.get("event_ids") == [
        "candidate-1"
    ], result
    assert calls and gate.admitted == gate.completed == 1


def test_file_events_without_cached_mapping_do_not_observe_or_send(tmp_path):
    clock = NS(now=lambda: 100, domain="host-mono:event-test")
    v = P.OnlineVerifier(
        None,
        None,
        clock,
        NS(event_ledger=lambda ep: None),
        None,
        robot=NS(observe=lambda: pytest.fail("extra observe")),
    )
    req = {
        "request_id": "q",
        "run_id": "event-run",
        "episode_id": "event-run.forward.0000",
        "episode_role": "forward",
        "target": "forward_goal",
        "event_ids": ["event"],
        "observed_from_step": 0,
        "observed_through_step": 1,
        "observed_through_ns": 100,
    }
    assert json.loads(v.submit(req))["kind"] == "unavailable"


def test_assembled_entry_uses_cached_event_window_after_failed_chunk(
    tmp_path, sdk, setup, monkeypatch
):
    from test_fr3_bridge import R
    from test_fr3_bridge import rig as entry_rig
    from test_fr3_signal_entry import signal_config

    from levi.automatic.journal import Journal

    original_verifier, original_clock = P.OnlineVerifier, R.bridge.Clock
    r = entry_rig(tmp_path, monkeypatch)
    monkeypatch.setattr(R.bridge, "Clock", original_clock)
    monkeypatch.setattr(R.providers, "OnlineVerifier", original_verifier)
    monkeypatch.setattr(R, "ARM_LOCK_FILE", str(tmp_path / "isolated-fake-arm.lock"))
    cfg = replace(r.job["config"], control_period_ns=10_000_000, forward_max_steps=12)
    r.job["config"] = cfg
    r.binding["signal_events"] = signal_config()
    server, gate = setup[4:]
    spec = sdk.parse_spec(
        "http://unused", online.spec_answer(server.spec, server.config)
    )
    monkeypatch.setattr(sdk, "fetch_spec", lambda url: spec)
    actual_model_calls, bodies = [], []

    def model(spec, folder, names):
        actual_model_calls.extend(names)
        assert all(cv2.imread(str(Path(folder) / n)) is not None for n in names)
        return json.dumps(
            {"object_state": "resting_at_destination", "stable": "yes"}
        ), {}

    server._ask = model

    def post(url, body, timeout, opener):
        bodies.append(body)
        return server.handle(json.dumps(body).encode())[1]

    monkeypatch.setattr(sdk, "post_judge", post)
    client, oldcv = r.modules[0], r.modules[4]
    media_cv = NS(**vars(cv2))
    media_cv.VideoWriter, media_cv.VideoWriter_fourcc = (
        oldcv.VideoWriter,
        oldcv.VideoWriter_fourcc,
    )
    r.modules = (*r.modules[:3], sdk, media_cv, r.modules[5])
    query, queries = client._query_policy, []

    def failed_first_query(*args):
        queries.append(1)
        if len(queries) == 1:
            raise OSError("injected recoverable chunk failure")
        return query(*args)

    client._query_policy = failed_first_query
    frozen = []
    freeze = W.Ledger.freeze

    def tracking(self, *args):
        result = freeze(self, *args)
        frozen.append(result[0])
        return result

    monkeypatch.setattr(W.Ledger, "freeze", tracking)
    state = R.execute(r.job, r.binding, load_modules=lambda binding: r.modules)
    assert state == "COMPLETED"
    assert frozen and actual_model_calls
    assert any(
        p.source.control_step == 0 and p.source.recording_row is None
        for proof in frozen
        for p in proof.picks
    )
    assert all(p.source.phase == "pre_action" for proof in frozen for p in proof.picks)
    assert all(
        "step" not in image
        for body in bodies
        if body["episode"]["steps"] is None
        for image in body["images"]
    )
    events = Journal.read(Path(r.job["rollout_root"]) / ".aeri/runs/real-test").events
    assert any(e.reason == "goal_verified" for e in events)
    assert gate.admitted == gate.completed


def test_media_stage_uses_original_observation_identity_without_recording_commit(
    tmp_path, sdk
):
    r = rig(tmp_path)
    media = Media(cv2, None, sdk, sdk.OnlineJudge(None), 10)
    media.active[tmp_path] = {"pending": None}
    media.use_event_source(r.observer, lambda: r.ctx, r.config, "event-run")
    r.clock.stamp += 100_000_000
    obs = r.observer.observe()
    media.frame(tmp_path, 999, obs)  # the recorder's step argument is not control time
    ledger = media.event_ledger(r.ctx.episode_id)
    assert ledger.rows[-1].control_step == 0
    assert ledger.rows[-1].recording_row is None
    assert not media.buffers
    r.ctx.last_step = 1
    r.clock.stamp += 100_000_000
    obs = r.observer.observe()
    media.frame(tmp_path, 999, dict(obs))
    assert len(ledger.rows) == 1  # same values without source identity are not accepted


def test_cancelled_event_keeps_actual_model_owner_until_worker_exit(
    tmp_path, sdk, setup, monkeypatch
):
    r = rig(tmp_path)
    r.stage(0)
    r.stage(1)
    v, j = verifier(r, sdk, offsets=(-0.1, 0))
    server, gate = setup[4:]
    j.spec = sdk.parse_spec(
        "http://unused", online.spec_answer(server.spec, server.config)
    )
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()

    def model(*args):
        entered.set()
        try:
            assert release.wait(2)
            return json.dumps(
                {"object_state": "resting_at_destination", "stable": "yes"}
            ), {}
        finally:
            exited.set()

    server._ask = model
    monkeypatch.setattr(
        sdk,
        "post_judge",
        lambda url, body, timeout, opener: server.handle(json.dumps(body).encode())[1],
    )
    ticket = v.submit(request(r))
    assert entered.wait(1)
    try:
        v.cancel(ticket, "operator stop")
        assert v.collect(ticket, timeout_ns=0) is None
        assert server._busy.locked() and gate.completed == 0
        assert not exited.is_set()
    finally:
        release.set()
        assert exited.wait(1)
        assert ticket.submission.pending.wait(1)
    assert not server._busy.locked() and gate.completed == 1
    assert v.collect(ticket, timeout_ns=0) is None
