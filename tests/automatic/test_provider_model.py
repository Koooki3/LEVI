"""Actual file/contract/Judge paths with injected model only; no model/network/GPU."""

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from levi.agent.schema import ProviderConfig
from levi.automatic import provider_model as pm
from levi.automatic import provider_worker as pw
from levi.domain import aeri
from levi.live import aeri_online, online
from levi.live.config import Config


class Gate:
    def __init__(self):
        self.open = True
        self.admitted = 0
        self.completed = 0

    def online_admit(self, _):
        if not self.open:
            return False, "gate_closed", "test gate"
        self.admitted += 1
        return True, None, None

    def online_check(self, _):
        return (True, None, None) if self.open else (False, "gate_closed", "test gate")

    def online_done(self):
        self.completed += 1


@pytest.fixture
def setup(tmp_path):
    profile = ProviderConfig(
        name="test-vlm",
        kind="openai-local",
        base_url="http://127.0.0.1:18100",
        model="test-model",
        model_digest="a" * 64,
        max_images=2,
        allow_localhost=True,
        vision=True,
        structured_output=True,
    )
    contract = {
        "id": "test-initial",
        "version": "1",
        "status": "confirmed",
        "predicates": {
            "required": ["object_at_source"],
            "optional": ["visible_marker"],
        },
        "observations": {"preferred": ["side"], "require_visible_evidence": True},
    }
    definitions = {
        "object_at_source": "Test operator source: the object is inside the marked fixture region.",
        "visible_marker": "The test marker is visible; optional and does not decide readiness.",
    }
    value = {
        "schema": pm.SPEC_SCHEMA,
        "id": "test-provider",
        "version": "1",
        "run_id": "test-run",
        "plan_sha256": "b" * 64,
        "contract": contract,
        "definitions": definitions,
        "source": {
            "ref": "test-operator-signed-fixture",
            "sha256": pm.digest({"contract": contract, "definitions": definitions}),
        },
        "provider": profile.model_dump(mode="json"),
        "deadlines_ns": {
            "scene": 5_000_000_000,
            "reset_final": 5_000_000_000,
            "reset_active": 3_000_000_000,
        },
        "image_budget": 2,
        "max_output_tokens": 512,
        "observation_max_age_ns": 1_000_000_000,
        "validity_ns": 1_000_000_000,
    }
    spec = pm.freeze(value)
    root, evidence = tmp_path / "worker", tmp_path / "evidence"
    root.mkdir()
    evidence.mkdir()
    ok, jpeg = cv2.imencode(".jpg", np.zeros((16, 16, 3), dtype=np.uint8))
    assert ok
    image = jpeg.tobytes()
    (evidence / "side.jpg").write_bytes(image)
    config = Config()
    config.service.workspace = str(tmp_path / "workspace")
    config.online.spec = "generic-final.v1.json"
    gate = Gate()
    judge = online.Judge(config, gate)
    judge.provider = lambda: profile
    judge.register_aeri_spec(spec)
    return spec, root, evidence, image, judge, gate


def payload(setup, kind="scene", *, duration=None):
    spec, _root, evidence, image, *_ = setup
    now = time.monotonic_ns()
    request = {
        "run_id": "test-run",
        "request_id": "test-run." + kind + ".1:q",
        "episode_id": "test-run."
        + ("forward" if kind == "scene" else "reset")
        + ".0001",
        "target": "initial_state" if kind == "scene" else "reset_goal",
        "spec_sha256": spec.sha256,
        "plan_sha256": "b" * 64,
        "submitted_ns": now,
        "phase": "scene" if kind == "scene" else "reset_final",
        "deadline_due_ns": now + (duration or 5_000_000_000),
    }
    if kind == "reset_judgement":
        request.update(
            episode_role="reset",
            observed_from_step=0,
            observed_through_step=1,
            observed_through_ns=now,
        )
    contract = spec.contract
    return {
        "schema": "levi.aeri.provider_request.v1",
        "kind": kind,
        "request": request,
        "clock_domain": aeri.host_clock_domain(),
        "observed_ns": now,
        "frames": [
            {
                "view": "side",
                "ref": "side:test",
                "file": "side.jpg",
                "sha256": hashlib.sha256(image).hexdigest(),
            }
        ],
        "evidence_root": str(evidence),
        "contract": {
            "id": contract.contract_id,
            "version": contract.contract_version,
            "required": list(contract.required),
            "optional": list(contract.optional),
        },
    }


def raw_answer(spec, item, required=True, optional=None):
    refs = [{"ref": f["ref"], "sha256": f["sha256"]} for f in item["frames"]]
    return json.dumps(
        {
            "schema": pm.ANSWER_SCHEMA,
            "spec_sha256": spec.sha256,
            "model_digest": spec.value["provider"]["model_digest"],
            "observed_ns": item["observed_ns"],
            "predicates": [
                {
                    "name": "object_at_source",
                    "required": True,
                    "value": required,
                    "evidence_refs": refs if required is not None else [],
                },
                {
                    "name": "visible_marker",
                    "required": False,
                    "value": optional,
                    "evidence_refs": refs if optional is not None else [],
                },
            ],
        }
    )


def injected(required=True, optional=None):
    def ask(spec, item, folder, names, cancel):
        assert Path(folder, names[0]).read_bytes().startswith(b"\xff\xd8")
        return raw_answer(spec, item, required, optional), {"reported_tokens": 17}

    return ask


def submit(root, item):
    name = hashlib.sha256(item["request"]["request_id"].encode()).hexdigest() + ".json"
    folder = root / item["kind"] / "requests"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(pm.canonical(item))
    return name


def read_response(root, item, name):
    return aeri.parse(
        (root / item["kind"] / "responses" / name).read_bytes(),
        "scene" if item["kind"] == "scene" else "judgement",
    )


@pytest.mark.parametrize(
    "kind,required,decision",
    [
        ("scene", True, "ready"),
        ("scene", False, "reset_required"),
        ("scene", None, "unknown"),
        ("reset_judgement", True, "confirmed"),
        ("reset_judgement", False, "rejected"),
        ("reset_judgement", None, "unknown"),
    ],
)
def test_real_file_worker_model_contract(setup, kind, required, decision):
    spec, root, evidence, _, judge, gate = setup
    item = payload(setup, kind)
    name = submit(root, item)
    with pw.Worker(
        root,
        spec,
        evidence,
        aeri_online.SharedChannel(judge, ask=injected(required, False)),
    ) as worker:
        ready = json.loads((root / "READY.json").read_text())
        assert ready["identity"]["pid"] == os.getpid()
        assert ready["capabilities"] == ["scene", "reset_judgement"]
        assert ready["spec_sha256"] == spec.sha256 and ready["plan_sha256"] == "b" * 64
        assert worker.process(kind, name)
        response = read_response(root, item, name)
        assert response.decision == decision
        assert response.provider == "vlm" and response.model_digest == "a" * 64
        assert not worker.process(kind, name)  # Idempotent, no repeated inference.
    assert not (root / "READY.json").exists()
    assert gate.admitted == gate.completed == 2


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "downgrade",
        "bad_json",
        "spec",
        "model",
        "time",
        "ref",
        "hash",
        "string",
        "extra",
        "duplicate",
    ],
)
def test_model_answer_cannot_implicitly_grant_success(setup, change):
    spec, root, evidence, _, judge, _ = setup
    item = payload(setup)

    def ask(spec, item, *_):
        value = json.loads(raw_answer(spec, item))
        if change == "missing":
            value["predicates"].pop(0)
        if change == "downgrade":
            value["predicates"][0]["required"] = False
        if change == "bad_json":
            return "oops", {}
        if change == "spec":
            value["spec_sha256"] = "c" * 64
        if change == "model":
            value["model_digest"] = "c" * 64
        if change == "time":
            value["observed_ns"] -= 1
        if change == "ref":
            value["predicates"][0]["evidence_refs"][0]["ref"] = "side:old"
        if change == "hash":
            value["predicates"][0]["evidence_refs"][0]["sha256"] = "c" * 64
        if change == "string":
            value["predicates"][0]["value"] = "true"
        if change == "extra":
            value["decision"] = "ready"
        if change == "duplicate":
            value["predicates"][1] = value["predicates"][0]
        return json.dumps(value), {}

    name = submit(root, item)
    with pw.Worker(
        root, spec, evidence, aeri_online.SharedChannel(judge, ask=ask)
    ) as worker:
        assert worker.process("scene", name)
        assert read_response(root, item, name).kind == "unavailable"


@pytest.mark.parametrize(
    "change",
    [
        "deadline_missing",
        "deadline_extended",
        "plan",
        "spec",
        "run",
        "clock",
        "old_frame",
        "future_frame",
        "identity",
        "root",
        "symlink",
        "hash",
        "oversized",
    ],
)
def test_invalid_request_never_calls_model(setup, change):
    spec, root, evidence, image, judge, _ = setup
    item = payload(setup)
    if change == "deadline_missing":
        del item["request"]["deadline_due_ns"]
    if change == "deadline_extended":
        item["request"]["deadline_due_ns"] += 1
    if change == "plan":
        item["request"]["plan_sha256"] = "c" * 64
    if change == "spec":
        item["request"]["spec_sha256"] = "c" * 64
    if change == "run":
        item["request"]["run_id"] = "different-run"
    if change == "clock":
        item["clock_domain"] = "other:clock"
    if change == "old_frame":
        item["observed_ns"] -= 2_000_000_000
    if change == "future_frame":
        item["observed_ns"] += 2_000_000_000
    if change == "identity":
        item["request"]["episode_id"] = "different-run.forward.0001"
    if change == "root":
        item["evidence_root"] = str(root)
    if change == "symlink":
        (evidence / "side.jpg").unlink()
        outside = root / "outside.jpg"
        outside.write_bytes(image)
        (evidence / "side.jpg").symlink_to(outside)
    if change == "hash":
        (evidence / "side.jpg").write_bytes(image + b"changed")
    if change == "oversized":
        (evidence / "side.jpg").write_bytes(b"a" * (pw.MAX_FRAME_BYTES + 1))
    calls = []

    def ask(*args):
        calls.append(args)
        raise AssertionError("invalid request reached inference")

    name = submit(root, item)
    with pw.Worker(
        root, spec, evidence, aeri_online.SharedChannel(judge, ask=ask)
    ) as worker:
        assert worker.process("scene", name)
        assert read_response(root, item, name).kind == "unavailable"
    assert not calls


def test_deadline_cuts_model_but_holds_shared_mutex_until_actual_exit(setup):
    spec, _, _, image, judge, gate = setup
    item = payload(setup, duration=70_000_000)
    entered, released, cancelled = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    def slow(spec, item, folder, names, abort):
        entered.set()
        assert released.wait(3)
        if abort.is_set():
            cancelled.set()
        return raw_answer(spec, item), {}

    result = aeri_online.query(judge, spec.sha256, item, [image], ask=slow)
    assert (
        entered.is_set()
        and result["status"] == "unavailable"
        and result["reason"] == "timeout"
    )
    assert judge._busy.locked() and gate.completed == 0
    assert (
        aeri_online.query(judge, spec.sha256, payload(setup), [image], ask=injected())[
            "reason"
        ]
        == "busy"
    )
    # C5 uses precisely the same mutex, so it cannot enter either.
    assert not judge._busy.acquire(blocking=False)
    import base64

    c5 = {
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
    code, body = judge.handle(pm.canonical(c5))
    assert code == 200 and body["status"] == "unavailable" and "busy" in body["reason"]
    released.set()
    deadline = time.monotonic() + 2
    while judge._busy.locked() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not judge._busy.locked() and cancelled.is_set() and gate.completed == 1


@pytest.mark.parametrize("stop", ["cancel", "gate", "shutdown"])
def test_inflight_cancel_and_gate_closure_never_publish_success(setup, stop):
    spec, root, evidence, _, judge, gate = setup
    item = payload(setup)
    name = submit(root, item)
    entered = threading.Event()

    def slow(spec, item, folder, names, abort):
        entered.set()
        assert abort.wait(3)
        return raw_answer(spec, item), {}

    with pw.Worker(
        root, spec, evidence, aeri_online.SharedChannel(judge, ask=slow)
    ) as worker:
        thread = threading.Thread(target=lambda: worker.process("scene", name))
        thread.start()
        assert entered.wait(2)
        if stop == "cancel":
            folder = root / "scene" / "cancelled"
            folder.mkdir(exist_ok=True)
            (folder / name).write_text('{"reason":"stop"}')
        elif stop == "gate":
            gate.open = False
        else:
            assert judge.abort("test shutdown")
        thread.join(3)
        assert not thread.is_alive()
        assert read_response(root, item, name).kind == "unavailable"


def test_worker_restart_does_not_repeat_ambiguous_inference(setup):
    spec, root, evidence, _, judge, _ = setup
    item = payload(setup)
    name = submit(root, item)
    intent = root / "scene" / "processed" / name
    intent.parent.mkdir(parents=True)
    raw = (root / "scene" / "requests" / name).read_bytes()
    intent.write_bytes(
        pm.canonical(
            {
                "request_sha256": hashlib.sha256(raw).hexdigest(),
                "spec_sha256": spec.sha256,
            }
        )
    )

    def unexpected(*_):
        raise AssertionError("ambiguous request repeated")

    with pw.Worker(
        root, spec, evidence, aeri_online.SharedChannel(judge, ask=unexpected)
    ) as worker:
        assert worker.process("scene", name)
        result = read_response(root, item, name)
        assert result.kind == "unavailable" and result.code == "provider_error"
        assert not worker.process("scene", name)


def test_missing_or_changed_channel_never_publishes_ready(setup):
    spec, root, evidence, _, judge, gate = setup
    gate.open = False
    with pw.Worker(root, spec, evidence, aeri_online.SharedChannel(judge)) as worker:
        assert not (root / "READY.json").exists()
        assert not worker.refresh_ready()
    gate.open = True
    wrong = spec.value["provider"]
    wrong["model_digest"] = "c" * 64
    judge.provider = lambda: ProviderConfig.model_validate(wrong)
    with pw.Worker(root, spec, evidence, aeri_online.SharedChannel(judge)):
        assert not (root / "READY.json").exists()
    assert not judge._busy.locked()


def test_check_is_pure_file_no_network_no_sdk_no_write(setup, tmp_path):
    spec, *_ = setup
    path = tmp_path / "frozen.json"
    path.write_bytes(spec.data)
    before = {
        str(p.relative_to(tmp_path)): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    code = """import socket,sys
from pathlib import Path
from levi.automatic.provider_worker import main
socket.socket=lambda *a,**k: (_ for _ in ()).throw(AssertionError('network'))
Path.write_bytes=lambda *a,**k: (_ for _ in ()).throw(AssertionError('write'))
assert main(['check','--spec',sys.argv[1]])==0
assert 'levi.inference.provider' not in sys.modules
assert 'httpx' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", code, str(path)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    after = {
        str(p.relative_to(tmp_path)): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    assert before == after


@pytest.mark.parametrize(
    "change", ["definition", "source", "draft", "model", "profile", "prompt", "schema"]
)
def test_incomplete_freeze_is_blocked(setup, change):
    spec, *_ = setup
    value = spec.value
    if change == "definition":
        del value["definitions"]["object_at_source"]
    if change == "source":
        value["source"]["sha256"] = "c" * 64
    if change == "draft":
        value["contract"]["status"] = "draft"
    if change == "model":
        value["provider"]["model_digest"] = None
    if change == "profile":
        del value["provider"]["context_tokens"]
    if change == "prompt":
        value["prompt_sha256"] = "c" * 64
    if change == "schema":
        value["answer_schema_sha256"] = "c" * 64
    with pytest.raises(ValueError):
        pm.FrozenSpec.load(value)


def test_file_bridge_submit_worker_collect_uses_same_frozen_plan_and_deadline(setup):
    from types import SimpleNamespace as NS

    from levi.automatic import fr3_providers as fp

    spec, root, evidence, _, judge, _ = setup

    class Clock:
        domain = aeri.host_clock_domain()
        now = staticmethod(time.monotonic_ns)
        advance = staticmethod(lambda ns: time.sleep(ns / 1e9))

    class Store:
        folder = evidence

        def keep_frame(self, ref, raw):
            (self.folder / "retained.jpg").write_bytes(raw)
            return {
                "ref": ref,
                "file": "retained.jpg",
                "sha256": hashlib.sha256(raw).hexdigest(),
            }

    robot = NS(
        observe=lambda: {
            "observed_ns": time.monotonic_ns(),
            "images_bgr": {"side": np.zeros((16, 16, 3), dtype=np.uint8)},
        }
    )
    with pw.Worker(
        root, spec, evidence, aeri_online.SharedChannel(judge, ask=injected())
    ) as worker:
        provider = fp.FileProvider(
            root, "scene", robot, Clock(), spec.contract, model_spec=spec
        )
        provider.use_evidence(Store())
        request = {
            "request_id": "test-run.forward.0001:scene1",
            "run_id": "test-run",
            "episode_id": "test-run.forward.0001",
            "target": "initial_state",
        }
        ticket = provider.submit(request)
        assert ticket.request["spec_sha256"] == spec.sha256
        assert (
            ticket.request["deadline_due_ns"] - ticket.request["submitted_ns"]
            == 5_000_000_000
        )
        assert worker.poll() == 1
        message = aeri.parse(provider.collect(ticket, timeout_ns=1_000_000), "scene")
        assert message.decision == "ready"
        # A static heartbeat cannot masquerade as this live worker/spec.
        row = json.loads((root / "READY.json").read_text())
        row["identity"]["start_ticks"] += 1
        (root / "READY.json").write_text(json.dumps(row))
        assert not provider.reachable()


def test_job_binding_requires_identical_run_plan_contract_deadlines_and_no_fake_events(
    setup, tmp_path
):
    from dataclasses import replace

    from levi.automatic.fr3_run import model_binding
    from levi.automatic.orchestrator import RunConfig
    from levi.automatic.termination import TerminationConfig

    spec, *_ = setup
    path = tmp_path / "model-spec.json"
    path.write_bytes(spec.data)
    provider = {"model_spec_file": str(path), "model_spec_sha256": spec.sha256}
    config = RunConfig(
        run_id="test-run",
        plan_sha256="b" * 64,
        episodes=1,
        initial_state=spec.contract,
        termination=TerminationConfig(allow_early_stop=False),
    )
    job = {"config": config, "plan_sha256": "b" * 64, "contract": spec.contract}
    assert model_binding(provider, job).sha256 == spec.sha256
    for changed in (
        replace(config, run_id="different"),
        replace(config, plan_sha256="c" * 64),
        replace(config, scene_timeout_ns=6_000_000_000),
    ):
        with pytest.raises(ValueError, match="frozen job"):
            model_binding(provider, {**job, "config": changed})
    with pytest.raises(ValueError, match="allow_early_stop:false"):
        model_binding(
            provider,
            {
                **job,
                "config": replace(
                    config, termination=TerminationConfig(allow_early_stop=True)
                ),
            },
        )
    path.unlink()
    with pytest.raises(OSError):
        model_binding(provider, job)


def test_explicit_online_mount_is_file_only_and_default_render_compatible(
    setup, tmp_path
):
    from levi.live.config import render

    spec, *_ = setup
    config = Config()
    assert "aeri_specs" not in render(config)
    path = tmp_path / "model-spec.json"
    path.write_bytes(spec.data)
    config.online.aeri_specs = [str(path)]
    assert "aeri_specs" in render(config)
    endpoint = online.Endpoint(config, Gate())
    assert endpoint.judge._aeri_specs[spec.sha256].sha256 == spec.sha256
    path.unlink()
    with pytest.raises(OSError):
        online.Endpoint(config, Gate())


@pytest.mark.parametrize(
    "paths",
    ["/absolute/spec.json", ["relative.json"], [None], ["/x", "/x"], ["/x"] * 33],
)
def test_frozen_config_rejects_invalid_registration_paths_without_channel(paths):
    from levi.live.config import _online_problems

    config = Config()
    config.online.aeri_specs = paths
    assert any("online.aeri_specs" in message for message in _online_problems(config))


def test_frozen_config_checks_owned_spec_bytes_without_model(setup, tmp_path):
    from levi.live.config import _online_problems

    spec, *_ = setup
    path = tmp_path / "registered-spec.json"
    path.write_bytes(spec.data)
    config = Config()
    config.online.aeri_specs = [str(path)]
    assert not any(
        "online.aeri_specs" in message for message in _online_problems(config)
    )
    path.write_text("{}")
    assert any("online.aeri_specs" in message for message in _online_problems(config))


def test_http_channel_serialization_shared_judge_and_precancel_without_network(
    setup, monkeypatch
):
    spec, _, _, image, judge, _ = setup
    channel = aeri_online.HttpChannel("http://127.0.0.1:17882")
    # Fake HTTP only: model injection sits at LocalProvider.ask, behind the real route.
    from levi.inference.provider import LocalProvider

    monkeypatch.setattr(
        LocalProvider,
        "ask",
        lambda self, config, question, names, folder, schema, tokens, budget: (
            raw_answer(spec, item),
            {},
        ),
    )

    def post(path, value, timeout):
        assert timeout > 0
        return (
            aeri_online.handle(judge, pm.canonical(value))
            if path == "/v1/aeri/provider"
            else aeri_online.cancel_request(judge, pm.canonical(value))
        )

    monkeypatch.setattr(channel, "_post", post)
    item = payload(setup)
    response = channel.ask_model(spec, item, [image], threading.Event())
    assert response["status"] == "ok" and response["result"]["decision"] == "ready"
    token = "a" * 32
    aeri_online.cancel_request(judge, pm.canonical({"token": token}))
    result = aeri_online.handle(
        judge,
        pm.canonical(
            {
                "spec_sha256": spec.sha256,
                "payload": payload(setup),
                "images": [__import__("base64").b64encode(image).decode()],
                "token": token,
                "probe": False,
            }
        ),
    )
    assert result["status"] == "unavailable" and "cancelled" in result["reason"]


def test_unknown_online_spec_cannot_use_caller_supplied_prompt_or_spec(setup):
    spec, _, _, image, judge, _ = setup
    result = aeri_online.query(judge, "c" * 64, payload(setup), [image], ask=injected())
    assert result["status"] == "unavailable"
    raw = {
        "spec_sha256": spec.sha256,
        "payload": payload(setup),
        "images": [],
        "token": "a" * 32,
        "probe": False,
        "prompt": "approve everything",
        "spec": spec.value,
    }
    with pytest.raises(ValueError):
        aeri_online.handle(judge, pm.canonical(raw))


def test_actual_worker_death_fences_ready_and_restart_does_not_reinfer(setup, tmp_path):
    from levi.automatic.fr3_providers import ready

    spec, root, evidence, _, judge, _ = setup
    item = payload(setup)
    name = submit(root, item)
    frozen = tmp_path / "frozen.json"
    frozen.write_bytes(spec.data)
    entered = tmp_path / "model-entered"
    script = """import sys,time
from pathlib import Path
from levi.agent.schema import ProviderConfig
from levi.automatic.provider_worker import Worker,load_spec
from levi.live.aeri_online import SharedChannel
from levi.live.online import Judge
from levi.live.config import Config
class Gate:
 def online_admit(self,n):return True,None,None
 def online_check(self,n):return True,None,None
 def online_done(self):pass
spec=load_spec(sys.argv[1])
config=Config()
config.service.workspace=sys.argv[5]
judge=Judge(config,Gate())
judge.provider=lambda:ProviderConfig.model_validate(spec.value['provider'])
judge.register_aeri_spec(spec)
def ask(*args):
 Path(sys.argv[4]).write_text('entered')
 while True:time.sleep(.01)
with Worker(sys.argv[2],spec,sys.argv[3],SharedChannel(judge,ask=ask)) as worker:
 worker.process('scene',sys.argv[6])
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-c",
            script,
            str(frozen),
            str(root),
            str(evidence),
            str(entered),
            str(tmp_path / "child-workspace"),
            name,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 3
        while (
            not entered.exists()
            and process.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert entered.exists()
        assert ready(root, spec.contract, ("scene",), model_spec=spec)
        row = json.loads((root / "READY.json").read_text())
        assert row["identity"]["pid"] == process.pid
        # Only terminate the test's own worker, after its exact identity was read.
        process.kill()
        process.communicate(timeout=3)
        assert not ready(root, spec.contract, ("scene",), model_spec=spec)

        def unexpected(*_):
            raise AssertionError("restart repeated an interrupted model call")

        with pw.Worker(
            root, spec, evidence, aeri_online.SharedChannel(judge, ask=unexpected)
        ) as worker:
            assert worker.process("scene", name)
            assert read_response(root, item, name).kind == "unavailable"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)


def test_second_worker_cannot_remove_live_owner_ready(setup):
    spec, root, evidence, _, judge, _ = setup
    channel = aeri_online.SharedChannel(judge, ask=injected())
    with pw.Worker(root, spec, evidence, channel):
        before = (root / "READY.json").read_bytes()
        with (
            pytest.raises(ValueError, match="another provider worker"),
            pw.Worker(root, spec, evidence, channel),
        ):
            pass
        assert (root / "READY.json").read_bytes() == before


@pytest.mark.parametrize(
    "change",
    ["run", "request", "episode", "model", "time", "missing", "downgrade", "evidence"],
)
def test_untrusted_online_result_refenced_at_worker_boundary(setup, change):
    spec, root, evidence, _, judge, _ = setup
    item = payload(setup)

    class ChangedChannel(aeri_online.SharedChannel):
        def ask_model(self, spec, payload, images, cancel):
            reply = super().ask_model(spec, payload, images, cancel)
            assert reply["status"] == "ok"
            value = reply["result"]
            if change == "run":
                value["run_id"] = "wrong-run"
            if change == "request":
                value["request_id"] = "wrong-request"
            if change == "episode":
                value["episode_id"] = "test-run.forward.0002"
            if change == "model":
                value["model_digest"] = "c" * 64
            if change == "time":
                value["observed_ns"] -= 1
            if change == "missing":
                value["predicate_results"].pop(1)
            if change == "downgrade":
                value["predicate_results"][0]["required"] = False
            if change == "evidence":
                value["predicate_results"][0]["evidence_refs"][0]["sha256"] = "c" * 64
            return reply

    name = submit(root, item)
    with pw.Worker(
        root, spec, evidence, ChangedChannel(judge, ask=injected())
    ) as worker:
        assert worker.process("scene", name)
        assert read_response(root, item, name).kind == "unavailable"


def test_slow_gpu_admission_is_inside_actual_request_deadline(setup):
    spec, _, _, image, judge, gate = setup
    entered, finish = threading.Event(), threading.Event()
    original = gate.online_admit

    def slow(now):
        entered.set()
        assert finish.wait(2)
        return original(now)

    gate.online_admit = slow
    started = time.monotonic()
    result = aeri_online.query(
        judge, spec.sha256, payload(setup, duration=70_000_000), [image], ask=injected()
    )
    assert result["status"] == "unavailable" and result["reason"] == "timeout"
    assert (
        time.monotonic() - started < 0.5 and entered.is_set() and judge._busy.locked()
    )
    finish.set()
    deadline = time.monotonic() + 2
    while judge._busy.locked() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not judge._busy.locked() and gate.admitted == gate.completed == 1
