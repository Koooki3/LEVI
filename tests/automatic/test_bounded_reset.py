"""Bounded reset with real host Clock, FileEvents, file providers and verifier.

Only SDK I/O, model answers and the forward C5 transport are injected. No
candidate event files exist: reset success must come from the final request.
"""

import hashlib
import json
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS

import cv2
import pytest
from test_fr3_bridge import no_network, rig  # noqa: F401
from test_provider_model import setup as provider_setup

from levi.automatic import cli
from levi.automatic import fr3_bridge as B
from levi.automatic import fr3_providers as P
from levi.automatic import fr3_run as R
from levi.automatic import provider_model as pm
from levi.automatic import provider_worker as pw
from levi.automatic import reset_manager as rm
from levi.automatic import state_machine as sm
from levi.automatic.journal import Journal
from levi.automatic.orchestrator import Orchestrator
from levi.live import aeri_online

# Synthetic test parameters, never a physical recommendation.
SETTLE = {
    "sample_interval_ns": 1_000_000,
    "stable_window_ns": 3_000_000,
    "max_wait_ns": 40_000_000,
    "position_tolerance_m": 0.0001,
    "rotation_tolerance_rad": 0.001,
    "gripper_tolerance": 0.001,
}
setup = provider_setup


@pytest.mark.parametrize(
    "attack",
    [
        "none",
        "default",
        "final_false",
        "final_unknown",
        "final_timeout",
        "final_foreign",
        "final_missing",
        "final_optional",
        "old_evidence",
        "post_reset_unknown",
        "post_home_unknown",
        "post_reset_write_none",
        "post_reset_write_exception",
        "post_reset_missing_frames",
        "post_reset_changed_frames",
        "post_reset_missing_record",
        "post_reset_unsaved_frames",
        "post_home_write_none",
        "post_home_write_exception",
        "post_home_missing_frames",
        "post_home_changed_frames",
        "post_home_missing_record",
        "post_home_unsaved_frames",
        "post_home_write_none_resume",
        "stop_final",
        "fault_final",
        "stop_settle",
        "fault_settle",
        "moving_xyz",
        "moving_rotation",
        "moving_gripper",
        "missing_gripper",
        "nonfinite_gripper",
        "stale_sample",
        "future_sample",
        "repeated_sample",
        "wrap_rotation",
        "home_failure",
        "seal_failure",
        "proof_failure",
        "stop_settle_proof",
        "fault_settle_proof",
        "stop_final_proof",
        "fault_final_proof",
        "stop_post_reset",
        "fault_post_reset",
        "quiesce_failure",
        "missing_pose",
        "wrong_clock",
        "zero_gripper",
        "missing_getstate",
        "fallback_gripper",
        "mismatched_gripper",
        "stale_generation",
        "boolean_source",
        "nonfinite_source",
        "out_of_range_source",
    ],
)
def test_complete_file_worker_reset_without_events(
    setup, tmp_path, monkeypatch, attack
):
    import sys

    actual = (P.FileProvider, P.FileEvents, P.OnlineVerifier, B.Clock)
    run = tmp_path / "run"
    run.mkdir()
    r = rig(run, monkeypatch, "single_reset_policy")
    original_env = r.modules[2].RobotEnv

    class MeasuredEnv(original_env):
        def _post(self, route, payload=None):
            response = super()._post(route, payload)
            if route == "getstate":
                self.measurement_count = getattr(self, "measurement_count", 0) + 1
                if attack == "fallback_gripper":
                    return response
                value = (
                    0.0
                    if attack == "zero_gripper"
                    and orch_ref
                    and orch_ref[0].state == "RESET_VERIFY"
                    else 1.0
                )
                if attack == "moving_gripper":
                    value = self.measurement_count % 2
                if attack == "boolean_source":
                    value = True
                if attack == "nonfinite_source":
                    value = float("nan")
                if attack == "out_of_range_source":
                    value = 1.1
                response["gripper_pos"] = value
            return response

        def get_observation(self):
            response = (
                {}
                if attack in {"missing_getstate", "stale_generation"}
                else self._post("getstate")
            )
            observed = super().get_observation()
            observed["robot_state"]["gripper_position"] = (
                1.0
                if attack == "stale_generation"
                else float(response.get("gripper_pos", 0.0))
            )
            if attack == "mismatched_gripper":
                observed["robot_state"]["gripper_position"] = 0.0
            return observed

    r.modules[2].RobotEnv = MeasuredEnv
    contract = replace(
        r.job["contract"],
        required=("object_at_source", "gripper_open"),
        optional=("scene_clear",),
        preferred_views=("side", "wrist"),
    )
    r.job["contract"] = contract
    mode = "wait_human" if attack == "default" else "verify_final"
    r.job["config"] = replace(
        r.job["config"],
        initial_state=contract,
        termination=replace(r.job["config"].termination, allow_early_stop=False),
        reset_horizon_completion=mode,
        reset_settle=None if mode == "wait_human" else SETTLE,
    )
    r.job["plan"]["bounded"] = {
        "mode": mode,
        "settle": None if mode == "wait_human" else SETTLE,
    }
    digest = hashlib.sha256(
        json.dumps(r.job["plan"], sort_keys=True).encode()
    ).hexdigest()
    r.job["plan_sha256"] = digest
    r.job["config"] = replace(r.job["config"], plan_sha256=digest)
    value = setup[0].value
    value.update(run_id=r.job["config"].run_id, plan_sha256=digest)
    value["contract"] = {
        "id": contract.contract_id,
        "version": contract.contract_version,
        "status": contract.status,
        "predicates": {
            "required": list(contract.required),
            "optional": list(contract.optional),
        },
        "observations": {
            "preferred": list(contract.preferred_views),
            "require_visible_evidence": True,
            "min_evidence_refs": 1,
            "require_all_views": True,
        },
    }
    value["definitions"] = {
        n: "Operator-approved synthetic fixture predicate."
        for n in (*contract.required, *contract.optional)
    }
    value["source"]["sha256"] = pm.digest(
        {"contract": value["contract"], "definitions": value["definitions"]}
    )
    spec = pm.freeze(value)
    path = tmp_path / "model-spec.json"
    path.write_bytes(spec.data)
    r.binding["providers"].update(
        model_spec_file=str(path), model_spec_sha256=spec.sha256
    )
    judge = setup[4]
    judge.register_aeri_spec(spec)
    asks, orch_ref, scene_count = [], [], []

    def ask(spec, item, folder, names, cancel):
        request = item["request"]
        asks.append(
            (
                item["kind"],
                request.get("phase"),
                request["request_id"],
                request["target"],
            )
        )
        ready = True
        if item["kind"] == "scene":
            scene_count.append(request["target"])
            if request["target"] == "post_reset":
                if attack == "stop_post_reset":
                    orch_ref[0].stop("stop-post-scene")
                if attack == "fault_post_reset":
                    orch_ref[0].robot.latched = "synthetic fault"
            ready = len(scene_count) != 1
            if attack == "post_reset_unknown" and request["target"] == "post_reset":
                ready = None
            if attack == "post_home_unknown" and len(scene_count) > 2:
                ready = None
        elif attack in {"final_false", "final_unknown"}:
            ready = False if attack == "final_false" else None
        if item["kind"] == "reset_judgement":
            assert request["phase"] == "reset_final" and request["request_id"].endswith(
                ":final"
            )
            if attack == "stop_final":
                orch_ref[0].stop("stop-final")
            if attack == "fault_final":
                orch_ref[0].robot.latched = "synthetic fault"
            if attack == "final_timeout":
                raise TimeoutError("synthetic timeout")
        refs = [{"ref": f["ref"], "sha256": f["sha256"]} for f in item["frames"]]
        predicates = [
            {
                "name": n,
                "required": n in contract.required,
                "value": ready if n in contract.required else False,
                "evidence_refs": refs,
            }
            for n in (*contract.required, *contract.optional)
        ]
        answer = {
            "schema": pm.ANSWER_SCHEMA,
            "spec_sha256": spec.sha256,
            "model_digest": spec.value["provider"]["model_digest"],
            "observed_ns": item["observed_ns"],
            "predicates": predicates,
        }
        if item["kind"] == "reset_judgement":
            if attack == "final_foreign":
                answer["spec_sha256"] = "0" * 64
            if attack == "final_missing":
                predicates.pop(1)
            if attack == "final_optional":
                predicates[1].update(required=False, value=False)
            if attack == "old_evidence":
                predicates[0]["evidence_refs"] = [
                    {"ref": "side:old", "sha256": "0" * 64}
                ]
        return json.dumps(answer), {}

    create = Orchestrator.create

    def creating(*args, **kwargs):
        orch = create(*args, **kwargs)
        orch_ref.append(orch)
        if attack == "stale_generation":
            orch.robot.env._post("getstate")
        original_observe = orch.robot.observe
        count = []

        def observing():
            observed = original_observe()
            if orch.state == "RESET_VERIFY":
                count.append(observed["observed_ns"])
                n = len(count)
                if attack == "stop_settle":
                    orch.stop("stop-settle")
                if attack == "fault_settle":
                    orch.robot.latched = "synthetic fault"
                if attack == "moving_xyz":
                    observed["pose"][0] += n * 0.01
                if attack == "moving_rotation":
                    observed["pose"][3] += n * 0.1
                if attack == "wrap_rotation":
                    observed["pose"][3] = (
                        3.141592653589793 if n % 2 else -3.141592653589793
                    )
                if attack == "missing_pose":
                    observed.pop("pose")
                if attack == "wrong_clock":
                    orch.clock.domain = "foreign:host"
                if attack == "missing_gripper":
                    observed["robot_state"].pop("gripper_position")
                if attack == "nonfinite_gripper":
                    observed["robot_state"]["gripper_position"] = float("nan")
                if attack == "stale_sample":
                    observed["observed_ns"] = count[0] - 1_000_000_000
                if attack == "future_sample":
                    observed["observed_ns"] += 1_000_000_000
                if attack == "repeated_sample":
                    observed["observed_ns"] = count[0]
            return observed

        orch.robot.observe = observing
        if attack == "quiesce_failure":
            orch.policy.quiesce = lambda **kw: {
                "ok": False,
                "detail": "test quiesce failed",
            }
        if attack == "home_failure":
            orch.robot.home = lambda **kw: NS(
                executed="unknown", detail="test home failed"
            )
        if attack == "seal_failure":
            orch.recorder.seal = lambda *args: (_ for _ in ()).throw(OSError("seal"))
        if attack == "proof_failure":
            orch.evidence.write = lambda *args, **kwargs: None
        original_write = orch.evidence.write

        def writing(record, *args, **kwargs):
            target = "reset_settle" if "settle" in attack else "reset_goal"
            if attack.endswith("_proof") and record.get("target") == target:
                if attack.startswith("stop"):
                    orch.stop("stop-proof")
                if attack.startswith("fault"):
                    orch.robot.latched = "synthetic proof-write fault"
            scene_target = (
                "post_reset" if attack.startswith("post_reset_") else "initial_state"
            )
            targeted_scene = record.get("target") == scene_target and (
                scene_target == "post_reset" or len(scene_count) > 2
            )
            if targeted_scene:
                if "write_none" in attack:
                    return None
                if "write_exception" in attack:
                    raise OSError("synthetic scene record write failure")
                if "missing_record" in attack:
                    return {**record, "frames": []}
                if "unsaved_frames" in attack:
                    record["frame_unsaved"] = ["side:synthetic_unsaved"]
            kept = original_write(record, *args, **kwargs)
            if targeted_scene and "missing_frames" in attack:
                for path in orch.evidence.frames.iterdir():
                    path.unlink()
            if targeted_scene and "changed_frames" in attack:
                for path in orch.evidence.frames.iterdir():
                    path.write_bytes(b"synthetic changed frame")
            return kept

        orch.evidence.write = writing
        if attack.endswith("_resume"):
            original_run = orch.run

            def running():
                state = original_run()
                assert state == "WAIT_HUMAN"
                assert orch.resume(
                    "synthetic-resume",
                    expected_seq=orch.journal.next_seq,
                    environment_handled=True,
                    health_rechecked=True,
                ).ok
                return original_run()

            orch.run = running
        return orch

    monkeypatch.setattr(Orchestrator, "create", creating)
    monkeypatch.setattr(B, "Clock", actual[3])
    monkeypatch.setattr(P, "FileEvents", actual[1])
    monkeypatch.setattr(P, "OnlineVerifier", actual[2])
    r.modules[4].imencode = cv2.imencode
    monkeypatch.setitem(sys.modules, "cv2", r.modules[4])
    stopped, started = threading.Event(), []

    class Attached(actual[0]):
        def use_evidence(self, store):
            super().use_evidence(store)
            if not started:
                worker = pw.Worker(
                    r.binding["providers"]["file_root"],
                    spec,
                    store.folder,
                    aeri_online.SharedChannel(judge, ask=ask),
                )
                worker.__enter__()

                def poll():
                    while not stopped.wait(0.001):
                        worker.poll()

                thread = threading.Thread(target=poll)
                started.append((worker, thread))
                thread.start()

    monkeypatch.setattr(P, "FileProvider", Attached)

    # Real OnlineVerifier; only C5 transport returns an injected model verdict.
    class Buffer:
        def __init__(self):
            self.rows = []

        def add(self, *a):
            self.rows.append(a)

        def __len__(self):
            return len(self.rows)

        def snapshot(self):
            return self.rows

        def start_frame(self):
            return None

    class Pending:
        def wait(self, timeout):
            return True

        def take(self):
            return {
                "schema": "levi.online.judge.result.v1",
                "status": "ok",
                "outcome": "success",
                "undecided": False,
                "reading": "supported",
                "spec": {"id": "generic-final", "version": 1},
                "answer": {"object_state": "supported", "stable": True},
                "model": "test-model",
            }

        def cancel(self):
            pass

    forward = NS(
        new_buffer=lambda hz: Buffer(), submit=lambda *a, **kw: NS(pending=Pending())
    )
    r.modules[3].OnlineJudge = lambda *a, **kw: forward
    try:
        state = R.execute(r.job, r.binding, load_modules=lambda _: r.modules)
    finally:
        stopped.set()
        for worker, thread in started:
            thread.join(2)
            assert not thread.is_alive()
            worker.close()
    scan = Journal.read(Path(r.job["rollout_root"]) / ".aeri/runs/real-test")
    assert sm.check_journal(scan.events) == []
    results = [
        e.episode_result
        for e in scan.events
        if e.record == "committed" and e.episode_result
    ]
    successes = {"none", "wrap_rotation", "zero_gripper"}
    if attack in successes:
        assert state == "COMPLETED"
        assert results[0].stop_reason == "horizon_exhausted"
        assert results[0].judgement_ids == ["real-test.reset.0001:final:judgement"]
        assert scene_count == ["initial_state", "post_reset", "initial_state"]
        evidence = orch_ref[0].evidence.folder
        proof = json.loads((evidence / "real-test.reset.0001:settle.json").read_text())
        assert proof["parameters"] == SETTLE and proof["plan_sha256"] == digest
        samples = proof["observations"]
        assert proof["observed_stable"] and len(samples) >= 2
        assert (
            samples[-1]["observed_ns"] - samples[0]["observed_ns"]
            >= SETTLE["stable_window_ns"]
        )
        assert all(
            s["gripper_measurement"]["route"] == "getstate"
            and s["gripper_measurement"]["field"] == "gripper_pos"
            for s in samples
        )
        if attack == "zero_gripper":
            assert all(s["measured_gripper"] == 0.0 for s in samples)
        final_proof = json.loads(
            (evidence / "real-test.reset.0001:final.json").read_text()
        )
        assert final_proof["judgement"]["spec_id"] == contract.contract_id
    else:
        assert state in {"WAIT_HUMAN", "FAULT_LOCKED"}
        assert not any(e.to_state == "FORWARD_ACTIVE" for e in scan.events)
    assert not list(
        Path(r.binding["providers"]["file_root"]).glob("events/**/event-*.json")
    )
    assert all(
        phase == "reset_final"
        for kind, phase, _, _ in asks
        if kind == "reset_judgement"
    )
    if attack in {"default", "stop_settle_proof", "fault_settle_proof"}:
        assert not any(kind == "reset_judgement" for kind, _, _, _ in asks)
    assert r.calls[-1][0] == "closed"


def test_config_and_plan_freeze_new_reset_mode(tmp_path):
    from test_aeri_cli import CONTRACT, job_text

    path = tmp_path / "job.yaml"
    (tmp_path / "initial-state.yaml").write_text(
        CONTRACT.replace('  version: "1"', '  version: "1"\n  status: confirmed')
    )
    text = job_text(tmp_path / "rollouts").replace(
        "  min_steps: 5", "  allow_early_stop: false\n  min_steps: 5"
    )
    path.write_text(text)
    old = cli.load_job(path)
    addition = "  horizon_completion: verify_final\n  settle:\n" + "".join(
        f"    {k}: {v}\n" for k, v in SETTLE.items()
    )
    path.write_text(text.replace("  max_attempts: 1", addition + "  max_attempts: 1"))
    new = cli.load_job(path)
    assert new["plan_sha256"] != old["plan_sha256"]
    assert new["plan"]["run"]["reset_horizon_completion"] == "verify_final"
    assert new["plan"]["run"]["reset_settle"] == SETTLE
    from levi.automatic import launch

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setenv(launch.HOME_ENV, str(tmp_path / "isolated-launch"))
        path.write_text(text)
        request = launch.LaunchRequest(job_path=str(path), execution_mode="dry_run")
        approved = launch.plan(request)
        path.write_text(
            text.replace("  max_attempts: 1", addition + "  max_attempts: 1")
        )
        with pytest.raises(launch.LaunchRefused) as caught:
            launch.launch(
                request,
                plan_sha256=approved.plan_sha256,
                launch_token=approved.launch_token,
            )
        assert caught.value.code == "E_PLAN_CHANGED"
    finally:
        monkeypatch.undo()
    assert "reviewed_pose_stability_capability_required" in R.recipe(path)["blockers"]
    for edit in [
        lambda s: s.replace("verify_final", "yes"),
        lambda s: s.replace("single_reset_policy", "human_assisted"),
        lambda s: s.replace("allow_early_stop: false", "allow_early_stop: true"),
        lambda s: s.replace("    gripper_tolerance: 0.001\n", ""),
        lambda s: s.replace(
            "    position_tolerance_m: 0.0001", "    position_tolerance_m: 0"
        ),
        lambda s: s.replace(
            "    sample_interval_ns: 1000000", "    sample_interval_ns: 0"
        ),
        lambda s: s.replace("    stable_window_ns: 3000000", "    stable_window_ns: 1"),
        lambda s: s.replace(
            "    gripper_tolerance: 0.001", "    gripper_tolerance: .nan"
        ),
        lambda s: s.replace(
            "    max_wait_ns: 40000000", "    max_wait_ns: 40000000000"
        ),
    ]:
        path.write_text(
            edit(text.replace("  max_attempts: 1", addition + "  max_attempts: 1"))
        )
        with pytest.raises(cli.JobError):
            cli.load_job(path)
    # The arbitration guard still refuses a raw horizon outcome.
    assert (
        rm.check_after(rm.SingleResetPolicy(), "reset_horizon_exhausted", 1, False)
        == "WAIT_HUMAN"
    )
