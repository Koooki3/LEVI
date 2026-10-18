"""Owned judge (VLM) scene/goal CPU codecs round-tripped through the real
OwnedCalls/Invocation contract with fake execution, clock and worker.

No model, GPU, network, robot port or real cycle is used. The fake worker plays
the part of the future worker-side factory: it only sees the payload bytes.
"""

import base64
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace as NS

import cv2
import numpy as np
import pytest

from levi.agent.schema import ProviderConfig
from levi.automatic import provider_model as pm
from levi.automatic.adapters import vlm_codec as vc
from levi.automatic.adapters.owned_calls import OwnedCalls, canonical
from levi.automatic.adapters.paused_frames import PauseBinding, PausedEvidence
from levi.automatic.intervention import InterventionUnproven
from levi.domain import aeri
from levi.live.owned_runtime import _cpu_payload

RUN = "test-run"
NOW = 50_000_000_000


def make_spec():
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
        "object_at_source": "Test operator source: the object is inside the region.",
        "visible_marker": "The test marker is visible; optional only.",
    }
    return pm.freeze(
        {
            "schema": pm.SPEC_SCHEMA,
            "id": "test-provider",
            "version": "1",
            "run_id": RUN,
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
    )


def jpeg(fill):
    ok, data = cv2.imencode(".jpg", np.full((16, 24, 3), fill, dtype=np.uint8))
    assert ok
    return data.tobytes()


class Rig:
    """Fake original cycle, borrowed execution and worker around one codec."""

    def __init__(self, tmp_path, **overrides):
        self.spec = overrides.pop("spec", None) or make_spec()
        self.clock = NS(domain=aeri.host_clock_domain(), ns=NOW)
        self.clock.now = lambda: self.clock.ns
        self.runtime = NS(lease_id="lease-1", lease_epoch=1)
        authority = object()
        self.cycle = NS(
            authority=authority,
            runtime=self.runtime,
            run_id=RUN,
            clock=self.clock,
            failed=False,
            interrupted=0,
        )
        self.cycle.interrupt = lambda **_: setattr(
            self.cycle, "interrupted", self.cycle.interrupted + 1
        )
        self.answers = {}
        self.dispatched, self.worker_views = [], []
        self.evidence_folder = tmp_path / "evidence"
        self.evidence_folder.mkdir()
        self.evidence = NS(folder=self.evidence_folder, keep_frame=self.keep_frame)
        self.paused_images = [
            ("side", jpeg(10)),
            ("wrist", jpeg(20)),
        ]
        self.freeze_calls = []
        self.codec = vc.VlmCodec(
            self.spec,
            clock=self.clock,
            views=overrides.pop("views", ("side", "wrist")),
            max_side=overrides.pop("max_side", 64),
            jpeg_quality=overrides.pop("jpeg_quality", 80),
            freeze=overrides.pop("freeze", self.freeze),
        )
        assert not overrides
        self.execution = NS(
            authority=authority,
            runtime=self.runtime,
            lease=NS(lease_id="lease-1", lease_epoch=1),
            dispatch_existing=self.dispatch,
            collect_existing=self.collect,
        )
        self.calls = OwnedCalls(
            self.execution,
            self.cycle,
            prepare_scene=self.codec.prepare_scene,
            prepare_goal=self.codec.prepare_goal,
            prepare_chunk=self.unused,
            decode_scene=self.codec.decode_scene,
            decode_goal=self.codec.decode_goal,
            decode_chunk=self.unused,
            completion_adapter=self.unused,
        )

    @staticmethod
    def unused(*_):
        raise AssertionError("policy codec is not part of the judge role")

    def keep_frame(self, ref, data):
        name = hashlib.sha256(ref.encode()).hexdigest() + ".jpg"
        (self.evidence_folder / name).write_bytes(data)
        return {"file": name, "sha256": hashlib.sha256(data).hexdigest()}

    def freeze(self, request, observation):
        self.freeze_calls.append((request, observation))
        images, picks = [], []
        for index, (role, data) in enumerate(self.paused_images):
            images.append(
                {
                    "role": role,
                    "offset_s": -0.2 * index,
                    "step": 5,
                    "jpeg_b64": base64.b64encode(data).decode("ascii"),
                }
            )
            picks.append(NS(role=role, jpeg_sha256=hashlib.sha256(data).hexdigest()))
        binding = PauseBinding(
            request["run_id"],
            request["episode_id"],
            "reset",
            3,
            1,
            "tx-1",
            7,
            NOW - 10,
            self.clock.domain,
            5,
        )
        proof = NS(picks=tuple(picks), start_picks=())
        return PausedEvidence(binding, proof, NS(views=(("side", ()),)), tuple(images))

    # --- fake borrowed execution / worker -------------------------------------------------
    def dispatch(self, request, payload, **_):
        _cpu_payload(payload)
        self.dispatched.append(payload)
        self.active = request

    def collect(self, request, *, timeout_s):
        payload = self.dispatched[-1]
        opened = vc.unpack_payload(payload)
        self.worker_views.append([f["view"] for f in opened["frames"]])
        for frame in opened["frames"]:
            pixels = cv2.imdecode(
                np.frombuffer(frame["bytes"], dtype=np.uint8), cv2.IMREAD_COLOR
            )
            assert pixels is not None  # worker-side factory decodes the JPEG
        raw = self.answers.get(opened["kind"], self.answer)(opened)
        result = [raw]
        _cpu_payload(result)
        return NS(request=request, result=result)

    def answer(self, opened, required=True, optional=None, **change):
        frames = opened["frames"]
        refs = [{"ref": f["ref"], "sha256": f["sha256"]} for f in frames]
        value = {
            "schema": pm.ANSWER_SCHEMA,
            "spec_sha256": self.spec.sha256,
            "model_digest": "a" * 64,
            "observed_ns": opened["observed_ns"],
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
        value.update(change)
        return json.dumps(value)

    # --- orchestrator-shaped requests ------------------------------------------------------
    def scene_request(self, target="initial_state"):
        return {
            "run_id": RUN,
            "request_id": f"{RUN}.forward.0001:scene",
            "episode_id": f"{RUN}.forward.0001",
            "target": target,
        }

    def goal_request(self, final=True):
        return {
            "run_id": RUN,
            "request_id": f"{RUN}.reset.0001" + (":final" if final else ":active"),
            "episode_id": f"{RUN}.reset.0001",
            "episode_role": "reset",
            "target": "reset_goal",
            "observed_through_ns": NOW,
            "observed_from_step": 0,
            "observed_through_step": 5,
            "event_ids": ["e1"],
        }

    def observation(self, ns=NOW, views=("side", "wrist")):
        return {
            "observed_ns": ns,
            "images_bgr": {
                view: np.full((16, 24, 3), 30 + i, dtype=np.uint8)
                for i, view in enumerate(views)
            },
        }

    def run(self, kind, request, *, observation=None, epoch=3, **extra):
        observation = observation or self.observation()
        inv = self.calls.invocation(
            kind,
            request,
            role="judge",
            policy_epoch=epoch,
            deadline_ns=NOW + 3_000_000_000,
            interrupted=lambda: False,
            observation=observation if kind == "goal" else None,
            observe=(lambda: observation) if kind == "scene" else None,
            min_observed_ns=NOW - 100,
            evidence=self.evidence,
            **extra,
        )
        token = NS(
            runtime=self.runtime,
            role="judge",
            episode_id=request["episode_id"],
            policy_epoch=epoch,
        )
        return inv, inv.decode(inv.call(token).result)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# --- round trips ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "required,decision",
    [(True, "ready"), (False, "reset_required"), (None, "unknown")],
)
def test_scene_round_trip_keeps_unknown_distinct(rig, required, decision):
    rig.answers["scene"] = lambda o: rig.answer(o, required=required)
    inv, raw = rig.run("scene", rig.scene_request())
    assert type(raw) is bytes
    message = aeri.parse(raw, "scene", specs=rig.spec.contract.spec())
    assert message.decision == decision
    if required is None:
        # undecided is neither success nor failure and carries its reason
        assert message.unknown_reason == "model_undecided"
        assert [p.value for p in message.predicate_results if p.required] == [None]
    # both configured views were encoded in order and retained as JPEG
    assert rig.worker_views[0] == ["side", "wrist"]
    assert [ref.split(":")[0] for ref, _ in inv.prepared.frames] == ["side", "wrist"]
    assert len(list(rig.evidence_folder.iterdir())) == 2
    assert message.provider == "vlm" and message.model_digest == "a" * 64


@pytest.mark.parametrize(
    "required,decision",
    [(True, "confirmed"), (False, "rejected"), (None, "unknown")],
)
def test_goal_round_trip_keeps_unknown_distinct(rig, required, decision):
    rig.answers["reset_judgement"] = lambda o: rig.answer(o, required=required)
    inv, raw = rig.run("goal", rig.goal_request())
    message = aeri.parse(raw, "judgement", specs=rig.spec.contract.spec())
    assert message.decision == decision
    assert message.observed_through_ns == NOW
    assert inv.prepared.binding.source_kind == "paused"
    assert [ref.split(":")[0] for ref, _ in inv.prepared.frames] == ["side", "wrist"]
    # the paused window came from the injected freeze (PausedFrames.freeze) once
    assert len(rig.freeze_calls) == 1


def test_active_goal_uses_active_phase_and_frozen_deadline(rig):
    inv, raw = rig.run("goal", rig.goal_request(final=False))
    opened = vc.unpack_payload(inv.prepared.payload)
    assert opened["provider"]["request"]["phase"] == "reset_active"
    assert opened["provider"]["request"]["spec_sha256"] == rig.spec.sha256
    assert opened["question"] == pm.question(rig.spec, opened["provider"])
    assert opened["max_output_tokens"] == 512
    assert aeri.parse(raw, "judgement", specs=rig.spec.contract.spec())


def test_payload_and_prepared_request_cross_ipc_as_cpu_values(rig):
    inv, _ = rig.run("scene", rig.scene_request())
    _cpu_payload(inv.prepared.payload)
    _cpu_payload(inv.prepared.request)
    assert inv.prepared.request == canonical(rig.scene_request())


def test_scene_resize_never_upscales_and_caps_longest_side(tmp_path):
    rig = Rig(tmp_path, max_side=12)
    inv, _ = rig.run("scene", rig.scene_request())
    for _, data in inv.prepared.frames:
        pixels = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        assert max(pixels.shape[:2]) == 12
    (tmp_path / "big").mkdir()
    big = Rig(tmp_path / "big", max_side=640)
    inv, _ = big.run("scene", big.scene_request())
    pixels = cv2.imdecode(
        np.frombuffer(inv.prepared.frames[0][1], dtype=np.uint8), cv2.IMREAD_COLOR
    )
    assert pixels.shape[:2] == (16, 24)


# --- construction: no defaults, frozen spec ---------------------------------------------------


def test_every_injected_value_is_required(rig):
    kwargs = {
        "clock": rig.clock,
        "views": ("side",),
        "max_side": 64,
        "jpeg_quality": 80,
        "freeze": rig.freeze,
    }
    for missing in kwargs:
        partial = {k: v for k, v in kwargs.items() if k != missing}
        with pytest.raises(TypeError):
            vc.VlmCodec(rig.spec, **partial)
    with pytest.raises(TypeError):
        vc.VlmCodec(**kwargs)


@pytest.mark.parametrize(
    "change",
    [
        {"max_side": 0},
        {"max_side": True},
        {"jpeg_quality": 0},
        {"jpeg_quality": 101},
        {"views": ()},
        {"views": ("side", "side")},
        {"views": ("side", "wrist", "extra")},  # over the frozen image budget
        {"views": ("si:de",)},
        {"views": ("",)},
        {"freeze": None},
        {"freeze": "not callable"},
    ],
)
def test_invalid_injected_values_rejected(rig, change):
    kwargs = {
        "clock": rig.clock,
        "views": ("side",),
        "max_side": 64,
        "jpeg_quality": 80,
        "freeze": rig.freeze,
    }
    kwargs.update(change)
    with pytest.raises(ValueError):
        vc.VlmCodec(rig.spec, **kwargs)


def test_spec_hash_mismatch_or_untyped_spec_rejected(rig):
    good = {
        "clock": rig.clock,
        "views": ("side",),
        "max_side": 64,
        "jpeg_quality": 80,
        "freeze": rig.freeze,
    }
    with pytest.raises(ValueError):
        vc.VlmCodec(pm.FrozenSpec(rig.spec.data, "0" * 64), **good)
    with pytest.raises(ValueError):
        vc.VlmCodec(rig.spec.value, **good)
    forged = json.loads(rig.spec.data)
    forged["prompt_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        vc.VlmCodec(
            pm.FrozenSpec(
                pm.canonical(forged), hashlib.sha256(pm.canonical(forged)).hexdigest()
            ),
            **good,
        )


def test_clock_must_be_the_host_clock_domain(rig):
    foreign = NS(domain="foreign:clock", now=lambda: NOW)
    with pytest.raises(ValueError):
        vc.VlmCodec(
            rig.spec,
            clock=foreign,
            views=("side",),
            max_side=64,
            jpeg_quality=80,
            freeze=rig.freeze,
        )


# --- preparation refusals ---------------------------------------------------------------------


def refuses(rig, kind, request, **kwargs):
    with pytest.raises(InterventionUnproven):
        rig.run(kind, request, **kwargs)
    assert rig.dispatched == []  # never reached the worker


def test_request_with_another_spec_hash_or_plan_rejected(rig):
    refuses(rig, "scene", {**rig.scene_request(), "spec_sha256": "c" * 64})
    refuses(rig, "scene", {**rig.scene_request(), "plan_sha256": "c" * 64})


def test_request_with_other_run_or_undefined_field_rejected(rig):
    refuses(rig, "scene", {**rig.scene_request(), "extra_field": 1})
    other = {
        **rig.scene_request(),
        "run_id": "other",
        "episode_id": "other.forward.0001",
    }
    with pytest.raises(InterventionUnproven):
        rig.codec.prepare_scene(
            other,
            rig.observation(),
            NS(kind="scene", policy_epoch=3, deadline_ns=NOW + 1),
        )


def test_operator_or_control_keys_never_reach_the_model(rig):
    request = {**rig.scene_request(), "operator_label": "success"}
    with pytest.raises(InterventionUnproven):
        rig.codec.prepare_scene(
            request,
            rig.observation(),
            NS(kind="scene", policy_epoch=3, deadline_ns=NOW + 1),
        )
    assert rig.dispatched == []


def test_scene_target_not_in_contract_rejected(rig):
    refuses(rig, "scene", rig.scene_request(target="reset_goal"))


def test_missing_view_or_bad_pixels_rejected(rig):
    refuses(
        rig, "scene", rig.scene_request(), observation=rig.observation(views=("side",))
    )
    bad = rig.observation()
    bad["images_bgr"]["side"] = np.zeros((16, 24), dtype=np.uint8)
    refuses(rig, "scene", rig.scene_request(), observation=bad)
    bad = rig.observation()
    bad["images_bgr"]["side"] = np.zeros((16, 24, 3), dtype=np.float32)
    refuses(rig, "scene", rig.scene_request(), observation=bad)
    bad = rig.observation()
    bad["images_bgr"]["side"] = b"jpeg"
    refuses(rig, "scene", rig.scene_request(), observation=bad)


def test_stale_observation_or_deadline_beyond_frozen_plan_rejected(rig):
    stale = rig.observation(ns=NOW - 2_000_000_000)
    with pytest.raises(InterventionUnproven):
        rig.codec.prepare_scene(
            rig.scene_request(),
            stale,
            NS(kind="scene", policy_epoch=3, deadline_ns=NOW + 1_000),
        )
    with pytest.raises(InterventionUnproven):
        rig.codec.prepare_scene(
            rig.scene_request(),
            rig.observation(),
            NS(kind="scene", policy_epoch=3, deadline_ns=NOW + 6_000_000_000),
        )


def test_context_kind_must_match_the_method(rig):
    with pytest.raises(InterventionUnproven):
        rig.codec.prepare_scene(
            rig.scene_request(),
            rig.observation(),
            NS(kind="goal", policy_epoch=3, deadline_ns=NOW + 1_000),
        )
    with pytest.raises(InterventionUnproven):
        rig.codec.prepare_goal(
            rig.goal_request(),
            rig.observation(),
            NS(kind="scene", policy_epoch=3, deadline_ns=NOW + 1_000),
        )


def test_forward_goal_is_not_supported_by_the_frozen_reset_spec(rig):
    request = {
        **rig.goal_request(),
        "episode_id": f"{RUN}.forward.0001",
        "episode_role": "forward",
        "target": "forward_goal",
    }
    refuses(rig, "goal", request)


# --- paused source binding --------------------------------------------------------------------


def test_goal_requires_matching_pause_binding(rig):
    for field, value in (
        ("policy_epoch", 4),
        ("clock_domain", "foreign:clock"),
        ("episode_id", f"{RUN}.reset.0002"),
        ("role", "forward"),
        ("run_id", "other"),
    ):
        original = rig.freeze

        def freeze(request, observation, field=field, value=value, original=original):
            evidence = original(request, observation)
            return replace(
                evidence, binding=replace(evidence.binding, **{field: value})
            )

        rig.codec = vc.VlmCodec(
            rig.spec,
            clock=rig.clock,
            views=("side", "wrist"),
            max_side=64,
            jpeg_quality=80,
            freeze=freeze,
        )
        rig.calls.preparers["goal"] = rig.codec.prepare_goal
        refuses(rig, "goal", rig.goal_request())


def test_goal_images_must_match_the_frozen_proof(rig):
    original = rig.freeze

    def tampered(request, observation):
        evidence = original(request, observation)
        images = list(evidence.images)
        images[0] = {**images[0], "jpeg_b64": base64.b64encode(jpeg(99)).decode()}
        return replace(evidence, images=tuple(images))

    def missing_proof(request, observation):
        evidence = original(request, observation)
        return replace(
            evidence, proof=NS(picks=evidence.proof.picks[:1], start_picks=())
        )

    def over_budget(request, observation):
        evidence = original(request, observation)
        return replace(evidence, images=evidence.images * 2)

    def not_jpeg(request, observation):
        evidence = original(request, observation)
        images = list(evidence.images)
        raw = b"not-a-jpeg"
        images[0] = {**images[0], "jpeg_b64": base64.b64encode(raw).decode()}
        picks = list(evidence.proof.picks)
        picks[0] = NS(role="side", jpeg_sha256=hashlib.sha256(raw).hexdigest())
        return replace(
            evidence,
            images=tuple(images),
            proof=NS(picks=tuple(picks), start_picks=()),
        )

    def bad_base64(request, observation):
        evidence = original(request, observation)
        images = list(evidence.images)
        images[0] = {**images[0], "jpeg_b64": "@@@"}
        return replace(evidence, images=tuple(images))

    def not_evidence(request, observation):
        return {"images": []}

    for freeze in (
        tampered,
        missing_proof,
        over_budget,
        not_jpeg,
        bad_base64,
        not_evidence,
    ):
        rig.codec = vc.VlmCodec(
            rig.spec,
            clock=rig.clock,
            views=("side", "wrist"),
            max_side=64,
            jpeg_quality=80,
            freeze=freeze,
        )
        rig.calls.preparers["goal"] = rig.codec.prepare_goal
        refuses(rig, "goal", rig.goal_request())


def test_freeze_failure_is_unproven_not_swallowed(rig):
    def boom(request, observation):
        raise RuntimeError("window gone")

    rig.codec = vc.VlmCodec(
        rig.spec,
        clock=rig.clock,
        views=("side", "wrist"),
        max_side=64,
        jpeg_quality=80,
        freeze=boom,
    )
    rig.calls.preparers["goal"] = rig.codec.prepare_goal
    refuses(rig, "goal", rig.goal_request())


# --- existing Invocation fences stay in force with this codec -------------------------------


def test_invocation_still_rejects_wrong_epoch_clock_and_hash(rig):
    for change in ("epoch", "clock", "hash", "observed", "request"):
        original = rig.codec.prepare_scene

        def mutated(request, observation, context, change=change, original=original):
            prepared = original(request, observation, context)
            field, value = {
                "epoch": ("policy_epoch", prepared.binding.policy_epoch + 1),
                "clock": ("clock_domain", "foreign:clock"),
                "hash": ("payload_sha256", "00" * 32),
                "observed": ("observed_ns", prepared.binding.observed_ns - 1),
                "request": ("episode_id", "test-run.reset.0009"),
            }[change]
            return replace(
                prepared, binding=replace(prepared.binding, **{field: value})
            )

        rig.calls.preparers["scene"] = mutated
        refuses(rig, "scene", rig.scene_request())


# --- decoding refusals ------------------------------------------------------------------------


def decode_refused(rig, kind, answer, now=None):
    key = "scene" if kind == "scene" else "reset_judgement"
    rig.answers[key] = answer
    request = rig.scene_request() if kind == "scene" else rig.goal_request()
    with pytest.raises(InterventionUnproven):
        rig.run(kind, request)
    assert rig.cycle.interrupted >= 1  # existing Invocation behaviour: no fallback


@pytest.mark.parametrize("kind", ["scene", "goal"])
@pytest.mark.parametrize(
    "change",
    [
        "missing_predicate",
        "downgrade",
        "bad_json",
        "spec",
        "model",
        "time",
        "ref",
        "hash",
        "string",
        "extra_field",
        "missing_field",
        "duplicate",
        "duplicate_key",
        "no_evidence",
        "other_view_frame",
    ],
)
def test_malformed_or_overreaching_model_answer_rejected(rig, kind, change):
    def answer(opened):
        value = json.loads(rig.answer(opened))
        if change == "missing_predicate":
            value["predicates"].pop(0)
        if change == "downgrade":
            value["predicates"][0]["required"] = False
        if change == "bad_json":
            return "oops"
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
        if change == "extra_field":
            value["decision"] = "ready"
        if change == "missing_field":
            del value["model_digest"]
        if change == "duplicate":
            value["predicates"][1] = value["predicates"][0]
        if change == "duplicate_key":
            return json.dumps(value)[:-1] + ', "observed_ns": 1}'
        if change == "no_evidence":
            value["predicates"][0]["evidence_refs"] = []
        if change == "other_view_frame":
            value["predicates"][0]["evidence_refs"] = [
                {"ref": "top:zzz", "sha256": "d" * 64}
            ]
        return json.dumps(value)

    decode_refused(rig, kind, answer)


@pytest.mark.parametrize(
    "result",
    [None, [], ["a", "b"], "", b"", {"text": "x"}, [1], 7, [None], [b"\xff\xfe"]],
)
def test_unusable_worker_result_rejected(rig, result):
    inv = rig.calls.invocation(
        "scene",
        rig.scene_request(),
        role="judge",
        policy_epoch=3,
        deadline_ns=NOW + 3_000_000_000,
        interrupted=lambda: False,
        observe=lambda: rig.observation(),
        min_observed_ns=NOW - 100,
        evidence=rig.evidence,
    )
    inv.call(
        NS(
            runtime=rig.runtime,
            role="judge",
            episode_id=rig.scene_request()["episode_id"],
            policy_epoch=3,
        )
    )
    with pytest.raises(InterventionUnproven):
        inv.decode(result)


def test_bytes_and_single_item_results_are_accepted(rig):
    for wrap in (lambda raw: raw, lambda raw: raw.encode(), lambda raw: (raw,)):
        inv = rig.calls.invocation(
            "scene",
            rig.scene_request(),
            role="judge",
            policy_epoch=3,
            deadline_ns=NOW + 3_000_000_000,
            interrupted=lambda: False,
            observe=lambda: rig.observation(),
            min_observed_ns=NOW - 100,
            evidence=rig.evidence,
        )
        token = NS(
            runtime=rig.runtime,
            role="judge",
            episode_id=rig.scene_request()["episode_id"],
            policy_epoch=3,
        )
        inv.call(token)
        opened = vc.unpack_payload(inv.prepared.payload)
        assert (
            aeri.parse(
                inv.decode(wrap(rig.answer(opened))),
                "scene",
                specs=rig.spec.contract.spec(),
            ).decision
            == "ready"
        )


def test_timeout_is_never_folded_into_a_decision(rig):
    inv = rig.calls.invocation(
        "scene",
        rig.scene_request(),
        role="judge",
        policy_epoch=3,
        deadline_ns=NOW + 3_000_000_000,
        interrupted=lambda: False,
        observe=lambda: rig.observation(),
        min_observed_ns=NOW - 100,
        evidence=rig.evidence,
    )
    inv.call(
        NS(
            runtime=rig.runtime,
            role="judge",
            episode_id=rig.scene_request()["episode_id"],
            policy_epoch=3,
        )
    )
    opened = vc.unpack_payload(inv.prepared.payload)
    # the answer arrives after the frozen request deadline -> no message at all
    rig.clock.ns = opened["provider"]["request"]["deadline_due_ns"]
    with pytest.raises(InterventionUnproven):
        inv.decode([rig.answer(opened)])
    # ... and an answer for a stale observation (older than the frozen age) too
    rig.clock.ns = NOW
    inv2 = rig.calls.invocation(
        "scene",
        rig.scene_request(),
        role="judge",
        policy_epoch=3,
        deadline_ns=NOW + 3_000_000_000,
        interrupted=lambda: False,
        observe=lambda: rig.observation(),
        min_observed_ns=NOW - 100,
        evidence=rig.evidence,
    )
    inv2.call(
        NS(
            runtime=rig.runtime,
            role="judge",
            episode_id=rig.scene_request()["episode_id"],
            policy_epoch=3,
        )
    )
    rig.clock.ns += 2_000_000_000
    with pytest.raises(InterventionUnproven):
        inv2.decode([rig.answer(opened)])


def test_decode_refuses_tampered_prepared_calls(rig):
    inv, _ = rig.run("scene", rig.scene_request())
    prepared = inv.prepared
    opened = vc.unpack_payload(prepared.payload)
    raw = rig.answer(opened)
    rig.cycle.failed = False

    def rebuilt(header_change=None, frames=None, request=None, binding=None):
        header = json.loads(json.dumps(opened["header"]))
        header_change and header_change(header)
        payload = vc.pack_payload(header, [f["bytes"] for f in opened["frames"]])
        return replace(
            prepared,
            payload=payload,
            request=request or prepared.request,
            frames=frames if frames is not None else prepared.frames,
            binding=binding
            or replace(
                prepared.binding, payload_sha256=hashlib.sha256(payload).hexdigest()
            ),
        )

    def other_question(header):
        header["question"] += " Also say ready."

    def other_contract(header):
        header["provider"]["contract"]["required"] = []

    def other_spec(header):
        header["provider"]["request"]["spec_sha256"] = "c" * 64

    def other_model(header):
        header["max_output_tokens"] = 9999

    def other_schema(header):
        header["answer_schema"]["required"] = []

    variants = {
        "question": rebuilt(other_question),
        "contract": rebuilt(other_contract),
        "spec": rebuilt(other_spec),
        "max_tokens": rebuilt(other_model),
        "answer_schema": rebuilt(other_schema),
        "request_identity": rebuilt(
            request=canonical({**rig.scene_request(), "request_id": "x"})
        ),
        "frames_differ": rebuilt(
            frames=(("side:other", b"\xff\xd8x"),) + prepared.frames[1:]
        ),
        "frames_missing": rebuilt(frames=()),
        "wrong_kind": replace(
            prepared, binding=replace(prepared.binding, source_kind="paused")
        ),
    }
    for name, tampered in variants.items():
        with pytest.raises(InterventionUnproven, match=r".+"):
            rig.codec.decode_scene(tampered, [raw], NOW), name
    # kind confusion: scene prepared call through the goal decoder and back
    with pytest.raises(InterventionUnproven):
        rig.codec.decode_goal(prepared, [raw], NOW)
    ginv, _ = rig.run("goal", rig.goal_request())
    with pytest.raises(InterventionUnproven):
        rig.codec.decode_scene(
            ginv.prepared, [rig.answer(vc.unpack_payload(ginv.prepared.payload))], NOW
        )


def test_payload_envelope_is_strict(rig):
    inv, _ = rig.run("scene", rig.scene_request())
    payload = inv.prepared.payload
    for bad in (
        b"",
        b"garbage",
        payload[:-1],
        payload + b"x",
        payload[:12] + b"\x00" * 8 + payload[20:],
        "text",
        None,
    ):
        with pytest.raises(ValueError):
            vc.unpack_payload(bad)


def test_codec_keeps_no_state_between_calls(rig):
    rig.run("scene", rig.scene_request())
    before = dict(vars(rig.codec))
    rig.run("goal", rig.goal_request())
    assert vars(rig.codec).keys() == before.keys()
    assert all(vars(rig.codec)[k] is v for k, v in before.items())


# --- Forward goal uses a separate explicitly frozen job criterion --------------------


@pytest.fixture
def forward_rig(tmp_path):
    from test_provider_model import forward_spec

    spec, _, _ = forward_spec(tmp_path, make_spec())
    rig = Rig(tmp_path, spec=spec)
    old_freeze = rig.codec._freeze

    def freeze(request, observation):
        evidence = old_freeze(request, observation)
        return replace(evidence, binding=replace(evidence.binding, role="forward"))

    rig.codec._freeze = freeze

    def answer(opened, value=True):
        from test_provider_model import forward_answer

        return forward_answer(spec, opened["provider"], value)

    rig.answers["forward_judgement"] = answer
    return rig


def forward_goal(rig, final=True):
    return {
        **rig.goal_request(final=final),
        "episode_id": f"{RUN}.forward.0001",
        "request_id": f"{RUN}.forward.0001" + (":final" if final else ":active"),
        "episode_role": "forward",
        "target": "forward_goal",
    }


@pytest.mark.parametrize("final", [True, False])
@pytest.mark.parametrize(
    "value,decision", [(True, "confirmed"), (False, "rejected"), (None, "unknown")]
)
def test_forward_owned_round_trip_is_explicit_and_paused(
    forward_rig, final, value, decision
):
    from test_provider_model import forward_answer

    rig = forward_rig
    rig.answers["forward_judgement"] = lambda opened: forward_answer(
        rig.spec, opened["provider"], value
    )
    inv, raw = rig.run("goal", forward_goal(rig, final))
    result = aeri.parse(raw, "judgement", specs=rig.spec.contract.spec())
    assert result.decision == decision
    assert result.episode_role == "forward"
    assert result.target == "forward_goal"
    assert len(rig.dispatched) == 1 and len(rig.freeze_calls) == 1
    opened = vc.unpack_payload(rig.dispatched[0])
    assert opened["provider"]["request"]["phase"] == (
        "forward_final" if final else "forward_active"
    )
    assert inv.prepared.binding.episode_role == "forward"


def test_forward_spec_cannot_answer_scene_or_reset(forward_rig):
    rig = forward_rig
    refuses(rig, "scene", rig.scene_request())
    refuses(rig, "goal", rig.goal_request())
    assert not rig.freeze_calls and not rig.dispatched


@pytest.mark.parametrize(
    "field,value",
    [
        ("role", "reset"),
        ("policy_epoch", 4),
        ("episode_id", f"{RUN}.forward.0002"),
        ("run_id", "other"),
        ("clock_domain", "foreign:clock"),
    ],
)
def test_forward_requires_original_paused_binding(forward_rig, field, value):
    rig = forward_rig
    freeze = rig.codec._freeze

    def bad(request, observation):
        evidence = freeze(request, observation)
        return replace(evidence, binding=replace(evidence.binding, **{field: value}))

    rig.codec._freeze = bad
    refuses(rig, "goal", forward_goal(rig))
    assert not rig.dispatched


@pytest.mark.parametrize(
    "fault",
    [
        "bad_json",
        "extra",
        "missing",
        "spec",
        "model",
        "time",
        "downgrade",
        "no_evidence",
        "old_frame",
        "guessed_predicate",
    ],
)
def test_forward_owned_decode_rejects_malformed_or_foreign_answers(forward_rig, fault):
    from test_provider_model import forward_answer

    rig = forward_rig

    def bad(opened):
        if fault == "bad_json":
            return "{"
        answer = json.loads(forward_answer(rig.spec, opened["provider"]))
        row = answer["predicates"][0]
        if fault == "extra":
            answer["label"] = "success"
        elif fault == "missing":
            answer.pop("model_digest")
        elif fault == "spec":
            answer["spec_sha256"] = "c" * 64
        elif fault == "model":
            answer["model_digest"] = "c" * 64
        elif fault == "time":
            answer["observed_ns"] -= 1
        elif fault == "downgrade":
            row["required"] = False
        elif fault == "no_evidence":
            row["evidence_refs"] = []
        elif fault == "old_frame":
            row["evidence_refs"][0]["sha256"] = "c" * 64
        else:
            row["name"] = "object_at_source"
        return json.dumps(answer)

    rig.answers["forward_judgement"] = bad
    with pytest.raises(InterventionUnproven):
        rig.run("goal", forward_goal(rig))
    assert len(rig.dispatched) == 1
