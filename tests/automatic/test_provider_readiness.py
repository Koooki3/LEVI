import base64
import json
import socket
import threading
import time

import pytest
from test_provider_model import injected, payload
from test_provider_model import (
    setup as setup,  # noqa: PLC0414 - pytest fixture re-export
)

from levi.automatic import provider_model as pm
from levi.live import aeri_online, online


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def refuse(*a, **kw):
        raise AssertionError("review probe attempted network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def c5_request(judge, image):
    return {
        "schema": online.REQUEST_SCHEMA,
        "task": "test-only instruction",
        "images": [
            {
                "role": role,
                "offset_s": offset,
                "jpeg_b64": base64.b64encode(image).decode(),
            }
            for role, offsets in online.views_of(judge.spec)
            for offset in offsets
        ],
    }


@pytest.mark.parametrize("stop", ["abort", "timeout"])
def test_c5_retains_shared_owner_until_actual_model_exit(setup, stop):
    spec, _, _, image, judge, gate = setup
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()

    def c5_ignores_cancel(spec, folder, names):
        entered.set()
        try:
            assert release.wait(3)
            return json.dumps(
                {"object_state": "resting_at_destination", "stable": "yes"}
            ), {}
        finally:
            exited.set()

    judge._ask = c5_ignores_cancel
    judge.config.online.timeout_s = 0.1
    result = {}
    thread = threading.Thread(
        target=lambda: result.update(
            value=judge.handle(pm.canonical(c5_request(judge, image)))
        )
    )
    thread.start()
    try:
        assert entered.wait(1)
        if stop == "abort":
            assert judge.abort("review-cancel")
        thread.join(1)
        assert not thread.is_alive()
        assert not exited.is_set()
        assert judge._busy.locked()
        assert gate.completed == 0
        second = aeri_online.query(
            judge, spec.sha256, payload(setup), [image], ask=injected()
        )
        assert second["status"] == "unavailable" and second["reason"] == "busy"
        assert not exited.is_set()
        assert gate.admitted == 1
    finally:
        release.set()
        thread.join(1)
        assert exited.wait(1)


def test_slow_gate_check_cannot_delay_the_deadline_response(setup):
    spec, _, _, image, judge, gate = setup
    entered, release = threading.Event(), threading.Event()

    def check(now):
        time.sleep(0.3)
        return True, None, None

    gate.online_check = check

    def slow(spec, item, folder, names, abort):
        entered.set()
        assert release.wait(2)
        return "{}", {}

    started = time.monotonic()
    try:
        result = aeri_online.query(
            judge, spec.sha256, payload(setup, duration=70_000_000), [image], ask=slow
        )
        elapsed = time.monotonic() - started
        assert result["status"] == "unavailable" and result["reason"] == "timeout"
        assert elapsed < 0.2
        assert judge._busy.locked()
    finally:
        release.set()
        deadline = time.monotonic() + 1
        while judge._busy.locked() and time.monotonic() < deadline:
            time.sleep(0.01)


import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest
from test_provider_model import submit

from levi.automatic import fr3_providers as fp
from levi.automatic import provider_worker as pw
from levi.domain import aeri


class Clock:
    domain = aeri.host_clock_domain()
    now = staticmethod(time.monotonic_ns)
    advance = staticmethod(lambda ns: time.sleep(ns / 1e9))
    __call__ = staticmethod(time.monotonic_ns)


def bridge(setup, spec, kind="scene"):
    _, root, evidence, _, _, _ = setup

    class Store:
        folder = evidence

        def keep_frame(self, ref, raw):
            name = hashlib.sha256(ref.encode()).hexdigest() + ".jpg"
            (self.folder / name).write_bytes(raw)
            return {"ref": ref, "file": name, "sha256": hashlib.sha256(raw).hexdigest()}

    robot = NS(
        observe=lambda: {
            "observed_ns": time.monotonic_ns(),
            "images_bgr": {"side": np.zeros((16, 16, 3), dtype=np.uint8)},
        }
    )
    provider = fp.FileProvider(
        root, kind, robot, Clock(), spec.contract, model_spec=spec
    )
    provider.use_evidence(Store())
    return provider


def request(number):
    return {
        "request_id": f"test-run.forward.0001:scene{number}",
        "run_id": "test-run",
        "episode_id": "test-run.forward.0001",
        "target": "initial_state",
    }


def test_history_cursor_reaches_new_actual_bridge_request(setup):
    spec, root, evidence, _, judge, _ = setup
    candidate = next(
        request(n)
        for n in range(10000)
        if fp.key(request(n)["request_id"]).startswith("ff")
    )
    target = fp.key(candidate["request_id"]) + ".json"
    calls = []

    def ask(*args):
        calls.append(1)
        return injected()(*args)

    with pw.Worker(
        root, spec, evidence, aeri_online.SharedChannel(judge, ask=ask)
    ) as worker:
        history = payload(setup)
        history["observed_ns"] -= 10_000_000_000
        history["request"]["submitted_ns"] -= 10_000_000_000
        history["request"]["deadline_due_ns"] -= 10_000_000_000
        for n in range(1000):
            old = copy.deepcopy(history)
            ident = f"test-run.forward.0001:history{n}"
            while fp.key(ident) + ".json" >= target:
                ident += "x"
            old["request"]["request_id"] = ident
            name = submit(root, old)
            assert worker.process("scene", name)
        assert calls == []
        provider = bridge(setup, spec)
        ticket = provider.submit(candidate)
        assert worker.poll() == 0
        assert worker.poll() == 1
        assert not worker.process("scene", ticket.path.name)
        assert (
            aeri.parse(provider.collect(ticket, timeout_ns=0), "scene").decision
            == "ready"
        )
        assert calls == [1]
        assert len(list((root / "scene" / "requests").glob("*.json"))) == 1001
        assert len(list((root / "scene" / "processed").glob("*.json"))) == 1001


def test_two_legal_slow_replies_preserve_ready_across_own_heartbeat(setup):
    oldspec, root, evidence, _, judge, _ = setup
    value = oldspec.value
    value["observation_max_age_ns"] = 5_000_000_000
    spec = pm.freeze(value)
    judge.register_aeri_spec(spec)

    def ask(*args):
        time.sleep(2.2)
        return injected()(*args)

    with pw.Worker(
        root, spec, evidence, aeri_online.SharedChannel(judge, ask=ask)
    ) as worker:
        provider = bridge(setup, spec)
        first = provider.submit(request(1))
        assert worker.process("scene", first.path.name)
        result = aeri.parse(provider.collect(first, timeout_ns=0), "scene")
        assert result.decision == "ready"
        assert (root / "READY.json").exists()
        assert provider.reachable()
        second = provider.submit(request(2))
        assert worker.process("scene", second.path.name)
        assert (
            aeri.parse(provider.collect(second, timeout_ns=0), "scene").decision
            == "ready"
        )
        assert provider.reachable()


@pytest.mark.parametrize("mode", ["human_assisted", "single_reset_policy"])
def test_actual_execute_file_worker_clock_and_legitimate_modes(
    setup, tmp_path, monkeypatch, mode
):
    import threading
    from dataclasses import replace

    from test_fr3_bridge import R, rig

    original_provider = fp.FileProvider
    original_clock = R.bridge.Clock
    original_verifier = R.providers.OnlineVerifier
    (tmp_path / "run").mkdir()
    r = rig(tmp_path / "run", monkeypatch, mode)
    cfg = r.job["config"]
    r.job["config"] = replace(
        cfg, termination=replace(cfg.termination, allow_early_stop=False)
    )
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
            from levi.live.online import result

            body = result(
                "ok",
                spec={"id": "generic-final", "version": 1},
                outcome="success",
                undecided=False,
                answer={"object_state": "resting_at_destination", "stable": "yes"},
            )
            return NS(
                pending=NS(wait=lambda t: True, take=lambda: body, cancel=lambda: None)
            )

    sdk = NS(OnlineJudge=SDKJudge, fetch_spec=lambda url: NS())
    r.modules = (*r.modules[:3], sdk, *r.modules[4:])
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

    def poll():
        while not done.wait(0.002):
            worker.poll()

    thread = threading.Thread(target=poll)
    thread.start()
    try:
        state = R.execute(r.job, r.binding, load_modules=lambda binding: r.modules)
        assert state == ("COMPLETED" if mode == "human_assisted" else "WAIT_HUMAN")
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


@pytest.mark.parametrize("stop", ["deadline", "cancel", "abort"])
def test_final_gate_probe_is_owned_and_cancellable_with_prompt_response(setup, stop):
    spec, _, _, image, judge, gate = setup
    entered, release = threading.Event(), threading.Event()
    calls = []

    def blocked_check(now):
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return True, None, None

    gate.online_check = blocked_check
    cancel = threading.Event()
    item = payload(setup, duration=70_000_000 if stop == "deadline" else None)
    found = {}
    thread = threading.Thread(
        target=lambda: found.update(
            value=aeri_online.query(
                judge, spec.sha256, item, [image], cancel=cancel, ask=injected()
            )
        )
    )
    thread.start()
    try:
        assert entered.wait(1)
        if stop == "cancel":
            cancel.set()
        if stop == "abort":
            assert judge.abort("probe-test")
        thread.join(0.2)
        assert not thread.is_alive()
        assert found["value"]["status"] == "unavailable"
        assert judge._busy.locked() and gate.completed == 0
        assert len(calls) == 1
        assert (
            aeri_online.query(
                judge, spec.sha256, payload(setup), [image], ask=injected()
            )["reason"]
            == "busy"
        )
    finally:
        release.set()
        thread.join(2)
        deadline = time.monotonic() + 1
        while judge._busy.locked() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not judge._busy.locked() and gate.completed == 1


def test_future_evidence_bootstrap_rejects_symlink_chain_and_replaced_anchor(setup):
    spec, root, evidence, _, judge, _ = setup
    future = evidence / "future" / "evidence"
    channel = aeri_online.SharedChannel(judge, ask=injected())
    with pw.Worker(root, spec, future, channel) as worker:
        assert not future.exists()
        destination = evidence / "outside"
        destination.mkdir()
        (evidence / "future").symlink_to(destination, target_is_directory=True)
        with pytest.raises(OSError):
            worker.open_evidence()
        assert worker.evidence_fd is None
        (evidence / "future").unlink()
        future.mkdir(parents=True)
        worker.open_evidence()
        future.rename(future.with_name("old"))
        future.mkdir()
        with pytest.raises(ValueError, match="replaced"):
            worker.open_evidence()


def test_heartbeat_rejects_a_different_live_c5_owner(setup):
    spec, root, evidence, image, judge, _ = setup
    entered, release = threading.Event(), threading.Event()

    def blocked_c5(spec, folder, names):
        entered.set()
        assert release.wait(2)
        return json.dumps(
            {"object_state": "resting_at_destination", "stable": "yes"}
        ), {}

    judge._ask = blocked_c5
    with pw.Worker(
        root, spec, evidence, aeri_online.SharedChannel(judge, ask=injected())
    ) as worker:
        thread = threading.Thread(
            target=lambda: judge.handle(pm.canonical(c5_request(judge, image)))
        )
        thread.start()
        try:
            assert entered.wait(1)
            assert not worker.refresh_ready()
            assert not (root / "READY.json").exists()
            assert not fp.ready(root, spec.contract, ("scene",), model_spec=spec)
        finally:
            release.set()
            thread.join(2)
            assert not thread.is_alive()
        assert worker.refresh_ready()
        assert fp.ready(root, spec.contract, ("scene",), model_spec=spec)
