# ruff: noqa: C408
"""Forward (policy) CPU codec: prepare_chunk / decode_chunk / completion_adapter.

Fake worker only: no openpi import, no GPU, no robot port. Parity with the
evaluation client is checked against the ``_query_policy`` source text, read
only and executed with a recording client.
"""

import ast
import hashlib
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import test_owned_calls_orchestrator as local

from levi.automatic.adapters import forward_codec as fc
from levi.automatic.adapters.owned_calls import (
    OwnedCalls,
    PreparationContext,
    PreparedCall,
    canonical,
)
from levi.automatic.intervention import CompletionEvidence, InterventionUnproven
from levi.automatic.orchestrator import Orchestrator

CLIENT = Path(
    "/home/marvel/work/wenkai/openpi/examples/fr3_local/run_robotiq_client.py"
)
H, W, STATE, DIMS, HORIZON = 8, 6, 7, 7, 16
VERSION = {"config": "pi05_fr3_robotiq_v3", "checkpoint_dir_name": "ckpt-1"}
RUN = "run-x"
EPISODE = RUN + ".forward.0001"
DOMAIN = "mono:test-boot"
OBS_NS = 1_000


def config(**override):
    values = {
        "image_shape": (H, W, 3),
        "state_dim": STATE,
        "action_dim": DIMS,
        "max_horizon": HORIZON,
        "prompt": "pick the screw",
        "model_version": dict(VERSION),
        "action_contract": "fr3-robotiq@1",
        "wall_clock_ns": lambda: 123,
    }
    values.update(override)
    return fc.ForwardCodecConfig(**values)


def request(**override):
    value = {
        "request_id": EPISODE + ":c0",
        "run_id": RUN,
        "episode_id": EPISODE,
        "handle_id": "h-forward-1",
        "policy_epoch": 1,
        "obs_step": 3,
        "obs_ns": OBS_NS,
        "clock_domain": DOMAIN,
        "action_start_index": 5,
        "committed_prefix_steps": 0,
        "previous_chunk_ref": None,
        "prefix_conditioning": "none",
        "deadline": {
            "due_ns": 10_000,
            "clock_domain": DOMAIN,
            "budget_ms": 1,
            "hardness": "hard",
        },
    }
    value.update(override)
    return value


def observation(**override):
    rng = np.random.default_rng(0)
    view = rng.integers(0, 255, (H, W, 3), dtype=np.uint8)
    hand = rng.integers(0, 255, (H, W, 3), dtype=np.uint8)
    state = np.arange(STATE, dtype=np.float32) / 10
    raw = {
        "image": {"view1": view, "hand": hand},
        "robot_state": {"cartesian_position": state},
    }
    value = {
        "observed_ns": OBS_NS,
        "state_fresh": True,
        "images_bgr": {"side": view, "wrist": hand},
        "raw": raw,
    }
    value.update(override)
    return value


def context(**override):
    values = {"kind": "chunk", "policy_epoch": 1, "deadline_ns": 10_000}
    values.update(override)
    return PreparationContext(**values)


def actions(rows=4, dims=DIMS):
    return np.linspace(-0.5, 0.5, rows * dims, dtype=np.float32).reshape(rows, dims)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.codec = fc.ForwardCodec(config())

    def prepare(self, req=None, obs=None, ctx=None):
        return self.codec.prepare_chunk(
            req or request(), obs or observation(), ctx or context()
        )

    def test_prepared_call_binding_and_payload_hash(self):
        prepared = self.prepare()
        self.assertIs(type(prepared), PreparedCall)
        self.assertEqual(prepared.request, canonical(request()))
        binding = prepared.binding
        self.assertEqual(
            (
                binding.run_id,
                binding.episode_id,
                binding.episode_role,
                binding.policy_epoch,
                binding.clock_domain,
                binding.observed_ns,
                binding.source_kind,
            ),
            (RUN, EPISODE, "forward", 1, DOMAIN, OBS_NS, "policy"),
        )
        self.assertEqual(
            binding.payload_sha256, hashlib.sha256(prepared.payload).hexdigest()
        )
        self.assertEqual(prepared.frames, ())

    def test_infer_input_equals_evaluation_client_query_policy(self):
        # Run the client's own _query_policy source (read only) on a recorder.
        if not CLIENT.exists():
            self.skipTest("evaluation client source not present")
        tree = ast.parse(CLIENT.read_text())
        # _query_policy and the helpers it has called since the 10-14 RTC change
        # (_policy_payload, _chunk_of); taking only _query_policy left
        # _chunk_of undefined (NameError on 2026-10-18).
        wanted = {"_query_policy", "_policy_payload", "_chunk_of"}
        nodes = [
            n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted
        ]
        assert any(n.name == "_query_policy" for n in nodes)
        scope = {"np": np}
        exec(compile(ast.Module(nodes, []), str(CLIENT), "exec"), scope)  # noqa: S102
        sent = []

        class Recorder:
            def infer(self, value):
                sent.append(value)
                return {"actions": actions()}

        obs = observation()
        scope["_query_policy"](
            Recorder(),
            obs["raw"],
            obs["raw"]["robot_state"]["cartesian_position"],
            "pick the screw",
        )
        mine = fc.decode_infer_input(self.prepare(obs=obs).payload)
        self.assertEqual(set(mine), set(sent[0]))
        for key in (
            "observation/image",
            "observation/wrist_image",
            "observation/state",
        ):
            np.testing.assert_array_equal(mine[key], sent[0][key])
            self.assertEqual(mine[key].dtype, sent[0][key].dtype)
            self.assertEqual(mine[key].shape, sent[0][key].shape)
        self.assertEqual(mine["prompt"], sent[0]["prompt"])
        # BGR -> RGB: view1 channel 0 becomes the last channel.
        np.testing.assert_array_equal(
            mine["observation/image"][..., 0], obs["raw"]["image"]["view1"][..., 2]
        )

    def test_infer_state_matches_evaluation_client_float32_conversion(self):
        if not CLIENT.exists():
            self.skipTest("evaluation client source not present")
        tree = ast.parse(CLIENT.read_text())
        conversion = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "state"
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and isinstance(node.value.func.value, ast.Name)
            and node.value.func.value.id == "np"
            and node.value.func.attr == "asarray"
            and any(
                keyword.arg == "dtype"
                and isinstance(keyword.value, ast.Attribute)
                and keyword.value.attr == "float32"
                for keyword in node.value.keywords
            )
        )
        for dtype in (np.float16, np.float32, np.float64):
            with self.subTest(dtype=dtype):
                obs = observation()
                raw_state = np.linspace(-0.3, 0.4, STATE, dtype=dtype)
                obs["raw"]["robot_state"]["cartesian_position"] = raw_state
                scope = {"np": np, "obs": obs["raw"]}
                exec(compile(ast.Module([conversion], []), str(CLIENT), "exec"), scope)  # noqa: S102
                mine = fc.decode_infer_input(self.prepare(obs=obs).payload)[
                    "observation/state"
                ]
                self.assertEqual(mine.dtype, scope["state"].dtype)
                self.assertEqual(mine.dtype, np.dtype(np.float32))
                np.testing.assert_array_equal(mine, scope["state"])
                self.assertIs(
                    obs["raw"]["robot_state"]["cartesian_position"], raw_state
                )
                self.assertEqual(raw_state.dtype, np.dtype(dtype))

    def test_float32_overflow_is_refused_before_policy_payload(self):
        obs = observation()
        obs["raw"]["robot_state"]["cartesian_position"] = np.full(STATE, 1e300)
        with self.assertRaises(InterventionUnproven):
            self.prepare(obs=obs)

    def test_payload_is_cpu_ipc_safe(self):
        prepared = self.prepare()
        self.assertIs(type(prepared.payload), bytes)
        self.assertLessEqual(len(prepared.payload), 16 * 1024 * 1024)

    def test_rejections(self):
        view = observation()["raw"]["image"]["view1"]
        cases = {
            "wrong kind": dict(ctx=context(kind="goal")),
            "reset role": dict(
                req=request(episode_id=RUN + ".reset.0001"),
            ),
            "obs_ns differs": dict(obs=observation(observed_ns=OBS_NS + 1)),
            "stale state": dict(obs=observation(state_fresh=False)),
            "no raw": dict(obs={k: v for k, v in observation().items() if k != "raw"}),
            "prefix conditioning": dict(req=request(prefix_conditioning="chunk")),
            "committed prefix": dict(req=request(committed_prefix_steps=2)),
            "bool epoch": dict(ctx=context(policy_epoch=True)),
            "no clock domain": dict(req=request(clock_domain="")),
        }
        for name, kwargs in cases.items():
            with self.subTest(name), self.assertRaises(InterventionUnproven):
                self.prepare(**kwargs)

        def raw_with(**changes):
            raw = observation()["raw"]
            for path, value in changes.items():
                group, key = path.split("__")
                raw[group][key] = value
            return observation(raw=raw)

        bad = {
            "image size": raw_with(image__view1=view[:-1]),
            "image dtype": raw_with(image__view1=view.astype(np.float32)),
            "image type": raw_with(image__hand=view.tolist()),
            "state dim": raw_with(robot_state__cartesian_position=np.zeros(STATE + 1)),
            "state nan": raw_with(
                robot_state__cartesian_position=np.full(STATE, np.nan, np.float32)
            ),
            "state int": raw_with(
                robot_state__cartesian_position=np.zeros(STATE, np.int32)
            ),
        }
        for name, obs in bad.items():
            with self.subTest(name), self.assertRaises(InterventionUnproven):
                self.prepare(obs=obs)
        # images_bgr that disagrees with raw is a different observation.
        other = observation()
        other["images_bgr"] = {"side": view + 1, "wrist": other["images_bgr"]["wrist"]}
        with self.assertRaises(InterventionUnproven):
            self.prepare(obs=other)

    def test_no_defaults_and_bad_config(self):
        required = {f for f in fc.ForwardCodecConfig.__dataclass_fields__}
        for name in required:
            values = {f: getattr(config(), f) for f in required if f != name}
            with self.subTest(missing=name), self.assertRaises(TypeError):
                fc.ForwardCodecConfig(**values)
        for override in (
            {"image_shape": (H, W)},
            {"image_shape": (0, W, 3)},
            {"state_dim": 0},
            {"action_dim": True},
            {"max_horizon": -1},
            {"prompt": ""},
            {"model_version": {"config": "x"}},
            {"model_version": {"config": "x", "checkpoint_dir_name": ""}},
            {"action_contract": ""},
            {"wall_clock_ns": None},
        ):
            with self.subTest(override), self.assertRaises(ValueError):
                config(**override)


class DecodeTests(unittest.TestCase):
    def setUp(self):
        self.codec = fc.ForwardCodec(config())
        self.prepared = self.codec.prepare_chunk(request(), observation(), context())

    def decode(self, result, now=2_000, prepared=None):
        return self.codec.decode_chunk(prepared or self.prepared, result, now)

    def test_chunk_response_matches_bridge_shape(self):
        message = self.decode({"actions": actions()})
        self.assertEqual(
            set(message),
            {
                "schema",
                "minor",
                "run_id",
                "emitted_wall_ns",
                "kind",
                "request_id",
                "episode_id",
                "policy_epoch",
                "action_contract",
                "actions",
                "valid_from_action_index",
                "prefix_conditioning_applied",
                "model_version",
                "timing",
                "clock_domain",
            },
        )
        self.assertEqual(message["kind"], "chunk_response")
        self.assertEqual(message["action_contract"], "fr3-robotiq@1")
        self.assertEqual(message["model_version"], VERSION)
        self.assertEqual(message["valid_from_action_index"], 5)
        self.assertEqual(message["prefix_conditioning_applied"], "none")
        self.assertEqual(message["emitted_wall_ns"], 123)
        self.assertEqual(message["clock_domain"], DOMAIN)
        self.assertEqual(message["timing"], {"total_ms": 0})
        self.assertEqual(message["actions"], actions().tolist())
        self.assertIs(type(message["actions"][0][0]), float)
        # Schema-valid in the unchanged AERI parser.
        from levi.domain import aeri

        aeri.validate({**message, "received_ns": 2_000}, "runtime")

    def test_accepts_list_actions_and_matching_optional_fields(self):
        message = self.decode(
            {
                "actions": actions().tolist(),
                "request_id": request()["request_id"],
                "model_version": dict(VERSION),
                "action_contract": "fr3-robotiq@1",
            }
        )
        self.assertEqual(len(message["actions"]), 4)

    def test_rejections(self):
        good = actions()
        nan = good.copy()
        nan[1, 2] = np.nan
        inf = good.copy()
        inf[0, 0] = np.inf
        cases = {
            "not dict": good,
            "none": None,
            "no actions": {},
            "wrong dims": {"actions": actions(dims=DIMS - 1)},
            "1d": {"actions": good[0]},
            "3d": {"actions": good[None]},
            "empty": {"actions": np.zeros((0, DIMS), np.float32)},
            "too long": {"actions": actions(HORIZON + 1)},
            "nan": {"actions": nan},
            "inf": {"actions": inf},
            "bool": {"actions": np.ones((2, DIMS), bool)},
            "object": {"actions": np.full((2, DIMS), None, object)},
            "string": {"actions": "abc"},
            "ragged": {"actions": [[0.0] * DIMS, [0.0] * (DIMS - 1)]},
            "unavailable": {"kind": "unavailable", "code": "policy_error"},
            "other contract": {"actions": good, "action_contract": "other@1"},
            "other version": {
                "actions": good,
                "model_version": {"config": "x", "checkpoint_dir_name": "y"},
            },
            "other request": {"actions": good, "request_id": "elsewhere"},
        }
        for name, result in cases.items():
            with self.subTest(name), self.assertRaises(InterventionUnproven):
                self.decode(result)

    def test_stale_or_foreign_clock_rejected(self):
        for now in (10_000, 20_000, OBS_NS - 1):
            with self.subTest(now=now), self.assertRaises(InterventionUnproven):
                self.decode({"actions": actions()}, now=now)
        with self.assertRaises(InterventionUnproven):
            self.decode({"actions": actions()}, now=True)

    def test_prepared_must_be_this_codecs_chunk(self):
        binding = replace(self.prepared.binding, source_kind="scene")
        with self.assertRaises(InterventionUnproven):
            self.decode(
                {"actions": actions()}, prepared=replace(self.prepared, binding=binding)
            )
        with self.assertRaises(InterventionUnproven):
            self.decode(
                {"actions": actions()},
                prepared=replace(self.prepared, payload=b"x" * 10),
            )
        with self.assertRaises(InterventionUnproven):
            self.decode({"actions": actions()}, prepared=object())


class CompletionTests(unittest.TestCase):
    def returned(self, req, **override):
        value = {
            "request": req,
            "sdk_ack": ("ack", 1),
            "stop_proof": SimpleNamespace(request=req),
            "observation": object(),
            "result": {"actions": actions()},
        }
        value.update(override)
        return SimpleNamespace(**value)

    def adapter(self, verified=None, roles=("forward",)):
        seen = [] if verified is None else verified
        return fc.make_completion_adapter(
            verify_ack=lambda req, ack: seen.append((req, ack)) or ack == ("ack", 1),
            roles=roles,
        )

    def test_same_request_object_and_fields(self):
        req = SimpleNamespace(role="forward")
        seen = []
        evidence = self.adapter(seen)(req, self.returned(req))
        self.assertIs(type(evidence), CompletionEvidence)
        self.assertIs(evidence.request, req)
        self.assertIs(evidence.stop_proof.request, req)
        self.assertEqual(seen, [(req, ("ack", 1))])

    def test_rejections(self):
        req = SimpleNamespace(role="forward")
        twin = SimpleNamespace(role="forward")  # equal-looking, different object
        cases = {
            "other request object": self.returned(twin),
            "stop proof other request": self.returned(
                req, stop_proof=SimpleNamespace(request=twin)
            ),
            "bad ack": self.returned(req, sdk_ack=("forged", 1)),
            "no ack": self.returned(req, sdk_ack=None),
            "withdrawn result": self.returned(req, result=None),
            "no observation": self.returned(req, observation=None),
            "gpu-like result": self.returned(req, result={"actions": object()}),
            "bad key": self.returned(req, result={1: 2}),
            "wrong role": self.returned(SimpleNamespace(role="judge")),
        }
        for name, returned in cases.items():
            with self.subTest(name), self.assertRaises(InterventionUnproven):
                self.adapter()(
                    returned.request if name == "wrong role" else req, returned
                )

    def test_required_arguments(self):
        for kwargs in (
            {"roles": ("forward",)},
            {"verify_ack": lambda r, a: True},
            {"verify_ack": None, "roles": ("forward",)},
            {"verify_ack": lambda r, a: True, "roles": ()},
        ):
            with self.subTest(kwargs), self.assertRaises((TypeError, ValueError)):
                fc.make_completion_adapter(**kwargs)


class RoundTripTests(local.OwnedCallsTests):
    """Original Orchestrator + OwnedCalls with a fake worker and this codec."""

    def mount_codec(self, r, *, prepare=None, completion=None):
        self.mount(r)  # scene/goal codecs and the fake borrowed executor
        state = np.arange(STATE, dtype=np.float32)
        observe = r.robot.observe

        def with_raw():
            obs = observe()
            image = np.full((H, W, 3), obs["observed_ns"] % 200, np.uint8)
            obs["raw"] = {
                "image": {"view1": image, "hand": image + 1},
                "robot_state": {"cartesian_position": state},
            }
            return obs

        r.robot.observe = with_raw
        codec = fc.ForwardCodec(config(action_dim=r.config.action_dims))
        self.sent, execution = [], self.executor
        dispatch, old = execution.dispatch_existing, self.calls

        def spy(request, payload, **kw):
            if request.role == "forward":
                # The worker side rebuilds the infer input from the bytes; the
                # fixture executor only needs an opaque marker to answer.
                self.sent.append(fc.decode_infer_input(payload))
                payload = canonical({"kind": "chunk"})
            return dispatch(request, payload, **kw)

        execution.dispatch_existing = spy
        self.calls = OwnedCalls(
            execution,
            r.cycle,
            prepare_scene=old.preparers["scene"],
            prepare_goal=old.preparers["goal"],
            prepare_chunk=prepare(codec) if prepare else codec.prepare_chunk,
            decode_scene=old.decoders["scene"],
            decode_goal=old.decoders["goal"],
            decode_chunk=codec.decode_chunk,
            completion_adapter=completion
            or fc.make_completion_adapter(
                verify_ack=lambda req, ack: (
                    ack == ("explicit-fake-ack", req.request_epoch)
                ),
                roles=("forward", "reset", "judge"),
            ),
        )
        r.orch = Orchestrator(
            r.orch.journal,
            config=r.config,
            intervention=r.cycle,
            owned_calls=self.calls,
            evidence=r.orch.evidence,
            **r.parts(),
        )
        return r

    def test_fake_worker_round_trip_through_original_orchestrator(self):
        r = self.mount_codec(self.make())
        self.assertEqual(r.orch.run(), "COMPLETED")
        self.assertEqual(r.robot.step_calls, 14)
        self.assertEqual(len(self.sent), 2)  # chunk before and after the not-goal
        for value in self.sent:
            self.assertEqual(value["prompt"], "pick the screw")
            self.assertEqual(value["observation/image"].shape, (H, W, 3))
        self.assertEqual(len(self.begin_attempts), len(self.finished))
        self.assertIsNone(r.owner.snapshot(now_ns=r.clock.now())["request"])

    def test_wrong_epoch_clock_or_hash_never_reaches_the_worker(self):
        for change in ("epoch", "clock", "hash"):
            with self.subTest(change):
                r = self.make()

                def prepare(codec, change=change):
                    def mutated(*args):
                        prepared = codec.prepare_chunk(*args)
                        field, value = {
                            "epoch": (
                                "policy_epoch",
                                prepared.binding.policy_epoch + 1,
                            ),
                            "clock": ("clock_domain", "foreign:clock"),
                            "hash": ("payload_sha256", "00" * 32),
                        }[change]
                        return replace(
                            prepared,
                            binding=replace(prepared.binding, **{field: value}),
                        )

                    return mutated

                self.mount_codec(r, prepare=prepare)
                self.assertEqual(r.orch.run(), "FAULT_LOCKED")
                self.assertEqual(self.sent, [])
                self.assertEqual(r.robot.step_calls, 0)
                self.assertTrue(r.cycle.failed)

    def test_completion_for_a_different_request_object_locks(self):
        r = self.make()
        real = fc.make_completion_adapter(
            verify_ack=lambda req, ack: ack == ("explicit-fake-ack", req.request_epoch),
            roles=("forward", "reset", "judge"),
        )

        def other_request(request, returned):
            twin = (
                SimpleNamespace(**vars(request))
                if hasattr(request, "__dict__")
                else None
            )
            if twin is None:
                self.skipTest("request token has no __dict__")
            evidence = real(request, returned)
            return replace(evidence, request=twin)

        self.mount_codec(r, completion=other_request)
        self.assertEqual(r.orch.run(), "FAULT_LOCKED")
        self.assertEqual(r.robot.step_calls, 0)

    def test_nan_action_from_worker_locks_without_motion(self):
        r = self.make()
        self.mount_codec(r)
        collect = self.executor.collect_existing

        def poisoned(request, *, timeout_s):
            done = collect(request, timeout_s=timeout_s)
            if request.role == "forward":
                bad = np.zeros((16, r.config.action_dims), np.float32)
                bad[0, 0] = np.nan
                done.result = {"actions": bad}
            return done

        self.executor.collect_existing = poisoned
        self.assertEqual(r.orch.run(), "FAULT_LOCKED")
        self.assertEqual(r.robot.step_calls, 0)
        self.assertTrue(r.cycle.failed)


# Do not re-run the parent suite's own tests under this subclass.
for _name in [n for n in dir(local.OwnedCallsTests) if n.startswith("test_")]:
    if _name not in RoundTripTests.__dict__:
        setattr(RoundTripTests, _name, None)

if __name__ == "__main__":
    unittest.main()
