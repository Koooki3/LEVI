"""Opt-in actual FR3 entry; fake SDK/injected CPU model, no hardware or GPU."""

import hashlib
import json
import socket
import threading
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest
from test_provider_model import setup as setup  # noqa: PLC0414

from levi.automatic import fr3_providers as fp
from levi.automatic import provider_model as pm
from levi.automatic import provider_worker as pw
from levi.automatic.adapters.signal_events import SignalEvents
from levi.domain import aeri
from levi.live import aeri_online


@pytest.fixture(autouse=True)
def no_network(monkeypatch, tmp_path):
    from levi.automatic import fr3_run as R

    monkeypatch.setattr(R, "ARM_LOCK_FILE", str(tmp_path / "owned-arm.lock"))

    def refuse(*args, **kwargs):
        raise AssertionError("signal entry tried real network I/O")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def signal_config():
    # Explicit operator test fixture; these numbers are never runtime defaults.
    return {
        "schema": "levi.aeri.signal_events.binding.v1",
        "actor_id": "arm_0",
        "profile": {
            "schema_version": "levi.signal_profile.v1",
            "origin": "declared",
            "overridden": [],
            "channels": [
                {
                    "role": "position",
                    "feature": "pose",
                    "index": i,
                    "axis": axis,
                    "kind": "measured",
                    "units": "m",
                    "frame": "base",
                    "actor_id": "arm_0",
                }
                for i, axis in enumerate("xyz")
            ],
        },
        "bindings": [
            {"feature": "pose", "index": i, "path": ["pose", i], "kind": "measured"}
            for i in range(3)
        ],
        "config": {
            "max_history": 128,
            "max_age_ns": 500_000_000,
            "still_ns": 1_000_000,
            "motion_speed_m_s": 0.01,
            "max_gap_ns": 1_000_000_000,
        },
    }


@pytest.mark.parametrize("mode", ["human_assisted", "single_reset_policy"])
def test_actual_signal_entry_uses_worker_current_frames_and_role_isolation(
    setup, tmp_path, monkeypatch, mode
):
    from dataclasses import replace

    from test_fr3_bridge import R, rig

    original_provider = fp.FileProvider
    original_clock = R.bridge.Clock
    original_verifier = R.providers.OnlineVerifier
    (tmp_path / "run").mkdir()
    r = rig(tmp_path / "run", monkeypatch, mode)
    cfg = r.job["config"]
    cfg = replace(cfg, control_period_ns=10_000_000)
    r.job["config"] = cfg
    r.binding["signal_events"] = signal_config()
    # Preserve the real OnlineVerifier over the actual Media recorder buffers.
    original_signal_poll = SignalEvents.poll
    observed_events = []

    def tracking_poll(self, stream_id, **kwargs):
        wires = original_signal_poll(self, stream_id, **kwargs)
        observed_events.extend(json.loads(wire) for wire in wires)
        return wires

    monkeypatch.setattr(SignalEvents, "poll", tracking_poll)
    c = r.job["contract"]
    value = setup[0].value
    value.update(run_id=cfg.run_id, plan_sha256=r.job["plan_sha256"])
    value["contract"] = {
        "id": c.contract_id,
        "version": c.contract_version,
        "status": c.status,
        "predicates": {"required": list(c.required), "optional": list(c.optional)},
        "observations": {
            "preferred": list(c.preferred_views),
            "require_visible_evidence": c.require_visible_evidence,
            "min_evidence_refs": c.min_evidence_refs,
            "require_all_views": c.require_all_views,
        },
    }
    value["definitions"] = {
        n: "Operator approved mock fixture: object is inside the marked source region."
        for n in (*c.required, *c.optional)
    }
    value["source"]["sha256"] = pm.digest(
        {"contract": value["contract"], "definitions": value["definitions"]}
    )
    spec = pm.freeze(value)
    specpath = tmp_path / "execute-spec.json"
    specpath.write_bytes(spec.data)
    r.binding["providers"].update(
        model_spec_file=str(specpath), model_spec_sha256=spec.sha256
    )
    judge = setup[4]
    judge.register_aeri_spec(spec)
    calls = []
    phases = []
    verified = []
    c5_model_calls = []

    def c5_model(spec, folder, names):
        import cv2

        for name in names:
            image = cv2.imread(str(Path(folder) / name))
            assert image is not None
            c5_model_calls.append(image.copy())
        return json.dumps(
            {"object_state": "resting_at_destination", "stable": "yes"}
        ), {}

    judge._ask = c5_model

    def ask(spec, item, folder, names, cancel):
        calls.append(item["kind"])
        phases.append(item["request"]["phase"])
        refs = [{"ref": f["ref"], "sha256": f["sha256"]} for f in item["frames"]]
        ready = not (
            mode == "single_reset_policy"
            and item["kind"] == "scene"
            and item["request"]["target"] == "initial_state"
        )
        return json.dumps(
            {
                "schema": pm.ANSWER_SCHEMA,
                "spec_sha256": spec.sha256,
                "model_digest": spec.value["provider"]["model_digest"],
                "observed_ns": item["observed_ns"],
                "predicates": [
                    {"name": n, "required": True, "value": ready, "evidence_refs": refs}
                    for n in c.required
                ],
            }
        ), {}

    done = threading.Event()
    monkeypatch.setattr(fp, "FileProvider", original_provider)
    monkeypatch.setattr(R.bridge, "Clock", original_clock)
    monkeypatch.setattr(R.providers, "OnlineVerifier", original_verifier)

    class Buffer:
        def __init__(self):
            self.rows = []

        def add(self, *row):
            self.rows.append(row)

        def __len__(self):
            return len(self.rows)

        def snapshot(self):
            return self.rows

        def start_frame(self):
            return self.rows[0] if self.rows else None

    class SDKJudge:
        def __init__(self, *a, **kw):
            pass

        def new_buffer(self, hz):
            return Buffer()

        def submit(self, *a, **kw):
            verified.append(a)
            assert a[0] and a[0][-1][2]["view1"].dtype == np.uint8
            import cv2
            from test_provider_readiness import c5_request

            ok, image = cv2.imencode(".jpg", a[0][-1][2]["view1"])
            assert ok
            body = judge.handle(pm.canonical(c5_request(judge, image.tobytes())))
            return NS(
                pending=NS(wait=lambda t: True, take=lambda: body, cancel=lambda: None)
            )

    sdk = NS(OnlineJudge=SDKJudge, fetch_spec=lambda url: NS())
    import cv2

    # Retain real JPEG bytes from fakeSDK images, never fake JPEG envelopes.
    media_cv2 = NS(**vars(r.modules[4]))
    media_cv2.imencode = cv2.imencode
    r.modules = (*r.modules[:3], sdk, media_cv2, r.modules[5])
    run_path = Path(r.job["rollout_root"]) / ".aeri/runs" / cfg.run_id
    assert not run_path.exists()
    worker = pw.Worker(
        r.binding["providers"]["file_root"],
        spec,
        run_path / "evidence",
        aeri_online.SharedChannel(judge, ask=ask),
    )
    worker.__enter__()
    assert not run_path.exists(), "worker readiness must never create the new run"
    assert fp.ready(
        r.binding["providers"]["file_root"],
        spec.contract,
        ("scene", "reset_judgement"),
        model_spec=spec,
    )
    bindingpath = tmp_path / "frozen-binding.json"
    bindingpath.write_text(json.dumps(r.binding))
    checked, _ = R.load_binding(bindingpath, r.job)
    assert checked == r.binding
    # A scene/reset worker never claims events readiness: the internal source
    # has its own fully validated binding. The old file path remains blocked.
    assert (
        "events"
        not in json.loads(
            (Path(r.binding["providers"]["file_root"]) / "READY.json").read_text()
        )["capabilities"]
    )
    old_binding = dict(r.binding)
    del old_binding["signal_events"]
    bindingpath.write_text(json.dumps(old_binding))
    with pytest.raises(ValueError, match="frame-bearing events"):
        R.load_binding(bindingpath, r.job)
    bindingpath.write_text(json.dumps(r.binding))

    def poll():
        while not done.wait(0.002):
            worker.poll()

    thread = threading.Thread(target=poll)
    thread.start()
    try:
        state = R.execute(r.job, r.binding, load_modules=lambda binding: r.modules)
        assert state == ("COMPLETED" if mode == "human_assisted" else "WAIT_HUMAN")
        if mode == "human_assisted":
            assert c5_model_calls and all(
                image.shape == (4, 4, 3) for image in c5_model_calls
            )
        assert observed_events, (
            "assembled must produce events without manual event files"
        )
        assert all(e["status"] == "candidate" for e in observed_events)
        assert all(
            e["evidence_refs"]
            and all(ref["step"] == e["step"] for ref in e["evidence_refs"])
            for e in observed_events
        )
        assert (
            verified
            if mode == "human_assisted"
            else any(e["episode_role"] == "reset" for e in observed_events)
        )
        assert not (Path(r.binding["providers"]["file_root"]) / "events").exists()
        for event in observed_events:
            for ref in event["evidence_refs"]:
                files = list((run_path / "evidence" / "frames").glob("*.jpg"))
                matches = [
                    p
                    for p in files
                    if hashlib.sha256(p.read_bytes()).hexdigest() == ref["sha256"]
                ]
                assert matches
                assert (
                    cv2.imdecode(
                        np.frombuffer(matches[0].read_bytes(), dtype=np.uint8),
                        cv2.IMREAD_COLOR,
                    )
                    is not None
                )
        assert calls.count("scene") >= (1 if mode == "human_assisted" else 2)
        assert ("reset_judgement" in calls) == (mode == "single_reset_policy")
        if mode == "single_reset_policy":
            assert "reset_active" in phases and "reset_final" not in phases
        from levi.automatic.journal import Journal

        events = Journal.read(run_path).events
        assert all(event.clock_domain == aeri.host_clock_domain() for event in events)
        with pytest.raises(ValueError, match="already exists"):
            R.execute(r.job, r.binding, load_modules=lambda binding: r.modules)
    finally:
        done.set()
        thread.join(2)
        assert not thread.is_alive()
        worker.close()


@pytest.mark.parametrize(
    "fault",
    [
        "profile",
        "inferred",
        "actor",
        "kind",
        "threshold",
        "bool",
        "frame",
        "path",
        "extra",
        "null",
    ],
)
def test_actual_binding_parser_refuses_incomplete_or_illegal_signal_declaration(
    tmp_path, monkeypatch, fault
):
    from test_fr3_bridge import R, rig

    r = rig(tmp_path, monkeypatch)
    value = signal_config()
    if fault == "profile":
        del value["profile"]
    if fault == "inferred":
        value["profile"]["origin"] = "inferred"
    if fault == "actor":
        value["profile"]["channels"][0]["actor_id"] = "arm_1"
    if fault == "kind":
        del value["profile"]["channels"][0]["kind"]
    if fault == "threshold":
        del value["config"]["motion_speed_m_s"]
    if fault == "bool":
        value["config"]["still_ns"] = True
    if fault == "frame":
        value["profile"]["channels"][0]["frame"] = "world"
    if fault == "path":
        value["bindings"][0]["path"] = ["pose", 6]
    if fault == "extra":
        value["config"]["guess"] = 1
    r.binding["signal_events"] = None if fault == "null" else value
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(r.binding))
    with pytest.raises(ValueError):
        R.load_binding(path, r.job)
    assert not r.calls


@pytest.mark.parametrize(
    "change", ["enable", "threshold", "semantics", "after_confirmation"]
)
def test_actual_approval_entry_cannot_reuse_old_binding_digest(
    tmp_path, monkeypatch, change
):
    from test_fr3_bridge import R, rig

    r = rig(tmp_path, monkeypatch)
    if change != "enable":
        r.binding["signal_events"] = signal_config()
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(r.binding))
    _, old_digest = R.load_binding(path, r.job)

    def alter():
        if change == "enable":
            r.binding["signal_events"] = signal_config()
        elif change == "semantics":
            r.binding["signal_events"]["profile"]["channels"][0]["dimension"] = (
                "reviewed-new-name"
            )
        else:
            r.binding["signal_events"]["config"]["still_ns"] += 1
        path.write_text(json.dumps(r.binding))

    monkeypatch.setattr(R.cli, "load_job", lambda *a: r.job)
    monkeypatch.setattr(
        R, "execute", lambda *a: pytest.fail("stale approval entered SDK")
    )
    monkeypatch.setattr(R.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: alter() or "execute real-test")
    if change != "after_confirmation":
        alter()
    assert (
        R.main(
            [
                "--config",
                "fixture",
                "--binding",
                str(path),
                "--execute",
                "--expect-plan",
                r.job["plan_sha256"],
                "--expect-binding",
                old_digest,
            ]
        )
        == 2
    )
    assert not r.calls


@pytest.mark.parametrize("fault", ["red", "no_ack", "home_error"])
def test_signal_wrapper_preserves_actual_robot_fault_fence(
    tmp_path, monkeypatch, fault
):
    from test_fr3_bridge import R, rig

    r = rig(tmp_path, monkeypatch, faults=(fault,))
    r.binding["signal_events"] = signal_config()
    assert (
        R.execute(r.job, r.binding, load_modules=lambda b: r.modules) == "FAULT_LOCKED"
    )
    assert r.calls[-1][0] == "closed"


def test_signal_wrapper_stop_probe_reaches_bottom_robot_before_any_action(
    tmp_path, monkeypatch
):
    import signal

    from test_fr3_bridge import R, rig

    r = rig(tmp_path, monkeypatch)
    r.binding["signal_events"] = signal_config()
    client = r.modules[0]
    query = client._query_policy

    def stop_inside_query(*args):
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        return query(*args)

    client._query_policy = stop_inside_query
    state = R.execute(r.job, r.binding, load_modules=lambda b: r.modules)
    assert state == "FAULT_LOCKED"
    # Existing stop/fault handling is preserved; no pose or home was sent.
    assert r.calls[-1] == ("closed", 0)
    assert not any(c[0] == "route" for c in r.calls)


@pytest.mark.parametrize("fault", ["epoch", "step", "clock", "missing_frames"])
def test_assembled_cached_signal_attacks_never_grant_early_stop(
    tmp_path, monkeypatch, fault
):
    from test_fr3_bridge import R, rig

    from levi.automatic.journal import Journal

    r = rig(tmp_path, monkeypatch)
    r.binding["signal_events"] = signal_config()
    poll = SignalEvents.poll
    wires = []

    def attacking(self, sid, **kwargs):
        value = list(self.observer.latest)
        if fault == "epoch":
            value[0] = "old-epoch"
        elif fault == "step":
            value[4] += 1
        elif fault == "clock":
            value[5] = "host-mono:old"
        else:
            value[-1]["images_bgr"] = {}
        self.observer.latest = tuple(value)
        result = poll(self, sid, **kwargs)
        wires.extend(result)
        return result

    monkeypatch.setattr(SignalEvents, "poll", attacking)
    if fault == "missing_frames":
        assert (
            R.execute(r.job, r.binding, load_modules=lambda b: r.modules) == "COMPLETED"
        )
        scan = Journal.read(Path(r.job["rollout_root"]) / ".aeri/runs/real-test")
        assert not any(e.reason == "goal_confirmed" for e in scan.events)
        assert r.calls[-1] == ("closed", 7)  # home + complete six-step horizon
    else:
        with pytest.raises(ValueError):
            R.execute(r.job, r.binding, load_modules=lambda b: r.modules)
        assert r.calls[-1] == ("closed", 0)  # attack before first policy action
    assert not wires
