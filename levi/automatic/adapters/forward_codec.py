"""Forward (policy) CPU codec for OwnedCalls: chunk prepare, decode, completion.

Pure CPU and NumPy. It opens no connection, imports no openpi, JAX or Torch
and supplies no physical or model default: image shape, state and action
dimensions, horizon bound, prompt, model version, action contract and the wall
clock are injected through ``ForwardCodecConfig`` and any missing or malformed
value is refused. It only transforms what the original Cycle hands it.

The infer input is the one ``_query_policy`` in the evaluation client builds
(``examples/fr3_local/run_robotiq_client.py``): BGR frames ``view1`` and
``hand`` reversed to RGB, the Cartesian pose as ``observation/state`` and the
prompt. ``decode_infer_input`` rebuilds that dict inside the worker from the
payload bytes. The response mirrors ``fr3_bridge.Policy.request``.
"""

import hashlib
import json
import struct
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from ..intervention import CompletionEvidence, InterventionUnproven
from .owned_calls import MAX_BYTES, PreparedCall, SourceBinding, canonical

MAGIC = b"LEVIFWD1"
SCHEMA = "levi.aeri.runtime.v1"


def _refuse(why):
    raise InterventionUnproven("forward codec: " + why)


def _int(value):
    return type(value) is int


def _positive(value):
    return _int(value) and value > 0


@dataclass(frozen=True)
class ForwardCodecConfig:
    """Every field is required; there are no defaults."""

    image_shape: tuple  # (height, width, channels), uint8 BGR as the camera gives
    state_dim: int
    action_dim: int
    max_horizon: int
    prompt: str
    model_version: dict  # {"config": ..., "checkpoint_dir_name": ...}
    action_contract: str
    wall_clock_ns: Callable  # emitted_wall_ns source (the bridge uses time.time_ns)

    def __post_init__(self):
        shape = self.image_shape
        if (
            type(shape) is not tuple
            or len(shape) != 3
            or not all(_positive(n) for n in shape)
        ):
            raise ValueError("image_shape must be (height, width, channels) > 0")
        if not (
            _positive(self.state_dim)
            and _positive(self.action_dim)
            and _positive(self.max_horizon)
        ):
            raise ValueError("state_dim, action_dim and max_horizon must be > 0")
        if type(self.prompt) is not str or not self.prompt:
            raise ValueError("a non-empty prompt is required")
        version = self.model_version
        if (
            type(version) is not dict
            or set(version) != {"config", "checkpoint_dir_name"}
            or not all(type(v) is str and v for v in version.values())
        ):
            raise ValueError("model_version needs config and checkpoint_dir_name")
        if type(self.action_contract) is not str or not self.action_contract:
            raise ValueError("a non-empty action_contract is required")
        if not callable(self.wall_clock_ns):
            raise ValueError("wall_clock_ns must be callable")  # noqa: TRY004
        object.__setattr__(self, "model_version", dict(version))


def _array(value, *, name, shape, kind, writable_copy=True):
    if type(value) is not np.ndarray or value.dtype.kind not in kind:
        _refuse(name + " must be a NumPy array of the injected type")
    if value.shape != shape:
        _refuse(f"{name} shape {value.shape} != {shape}")
    return np.ascontiguousarray(value)


def _encode(header, blobs):
    head = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    return MAGIC + struct.pack(">I", len(head)) + head + b"".join(blobs)


def _split(payload):
    if (
        type(payload) is not bytes
        or len(payload) < len(MAGIC) + 4
        or not payload.startswith(MAGIC)
    ):
        _refuse("payload is not a forward codec message")
    (size,) = struct.unpack(">I", payload[len(MAGIC) : len(MAGIC) + 4])
    start = len(MAGIC) + 4
    if start + size > len(payload):
        _refuse("payload header truncated")
    try:
        header = json.loads(payload[start : start + size])
    except ValueError:
        _refuse("payload header is not JSON")
    return header, payload[start + size :]


def decode_infer_input(payload):
    """Worker side: rebuild the dict the evaluation client passes to ``infer``."""
    header, body = _split(payload)
    out, offset = {}, 0
    for key in ("observation/image", "observation/wrist_image", "observation/state"):
        spec = header["arrays"][key]
        dtype = np.dtype(spec["dtype"])
        count = int(np.prod(spec["shape"]))
        end = offset + count * dtype.itemsize
        if end > len(body):
            _refuse("payload body truncated")
        out[key] = (
            np.frombuffer(body[offset:end], dtype=dtype).reshape(spec["shape"]).copy()
        )
        offset = end
    if offset != len(body):
        _refuse("payload has trailing bytes")
    out["prompt"] = header["prompt"]
    return out


class ForwardCodec:
    """``prepare_chunk`` and ``decode_chunk`` with the OwnedCalls signatures."""

    def __init__(self, config):
        if type(config) is not ForwardCodecConfig:
            raise ValueError("an explicit ForwardCodecConfig is required")
        self.config = config

    # prepare_chunk(request, observation, context) -> PreparedCall
    def prepare_chunk(self, request, observation, context):
        cfg = self.config
        if getattr(context, "kind", None) != "chunk":
            _refuse("only chunk preparation is supported")
        epoch = context.policy_epoch
        if not _int(epoch) or epoch < 0:
            _refuse("policy epoch must be an integer")
        episode = request.get("episode_id")
        if type(episode) is not str or episode.count(".") < 2:
            _refuse("episode id is malformed")
        role = episode.rsplit(".", 2)[1]
        if role != "forward":
            _refuse("policy chunks belong to the forward episode role")
        domain = request.get("clock_domain")
        if type(domain) is not str or not domain:
            _refuse("request clock domain missing")
        if (
            request.get("prefix_conditioning") != "none"
            or request.get("committed_prefix_steps") != 0
        ):
            _refuse("prefix conditioning is not supported by this deployment")
        if request.get("policy_epoch") != epoch:
            _refuse("request and context policy epoch differ")
        observed = observation.get("observed_ns")
        if not _int(observed) or observed != request.get("obs_ns"):
            _refuse("policy request does not use this observation")
        if observation.get("state_fresh") is not True:
            _refuse("no proof that the robot state is current")
        raw = observation.get("raw")
        try:
            view, hand = raw["image"]["view1"], raw["image"]["hand"]
            state = raw["robot_state"]["cartesian_position"]
        except (KeyError, TypeError):
            _refuse("raw observation lacks image.view1/hand or robot_state")
        shape = cfg.image_shape
        view = _array(view, name="view1", shape=shape, kind="u")
        hand = _array(hand, name="hand", shape=shape, kind="u")
        if view.dtype != np.uint8 or hand.dtype != np.uint8:
            _refuse("camera frames must be uint8")
        state = _array(state, name="state", shape=(cfg.state_dim,), kind="f")
        # The evaluation loop normalizes Cartesian state before _query_policy.
        with np.errstate(over="ignore", invalid="ignore"):
            state = state.astype(np.float32)
        if not np.isfinite(state).all():
            _refuse("robot state is not finite")
        staged = observation.get("images_bgr")
        if staged is not None and (
            type(staged) is not dict
            or not np.array_equal(staged.get("side"), view)
            or not np.array_equal(staged.get("wrist"), hand)
        ):
            _refuse("staged images differ from the raw observation")
        # Same conversion as _query_policy: BGR -> RGB, contiguous.
        arrays = {
            "observation/image": np.ascontiguousarray(view[..., ::-1]),
            "observation/wrist_image": np.ascontiguousarray(hand[..., ::-1]),
            "observation/state": state,
        }
        header = {
            "format": "levi.forward_codec.v1",
            "request_id": request["request_id"],
            "policy_epoch": epoch,
            "observed_ns": observed,
            "prompt": cfg.prompt,
            "arrays": {
                key: {"dtype": value.dtype.str, "shape": list(value.shape)}
                for key, value in arrays.items()
            },
        }
        payload = _encode(header, [value.tobytes() for value in arrays.values()])
        if not 0 < len(payload) <= MAX_BYTES:
            _refuse("payload exceeds the frozen IPC bound")
        return PreparedCall(
            canonical(request),
            payload,
            SourceBinding(
                request["run_id"],
                episode,
                role,
                epoch,
                domain,
                observed,
                "policy",
                hashlib.sha256(payload).hexdigest(),
            ),
        )

    # decode_chunk(prepared, result, now) -> chunk_response dict
    def decode_chunk(self, prepared, result, now):
        cfg = self.config
        if (
            type(prepared) is not PreparedCall
            or type(prepared.binding) is not SourceBinding
            or prepared.binding.source_kind != "policy"
            or prepared.binding.payload_sha256
            != hashlib.sha256(prepared.payload).hexdigest()
        ):
            _refuse("decode needs this codec's prepared policy call")
        header, _ = _split(prepared.payload)
        request = json.loads(prepared.request)
        if (
            header.get("request_id") != request["request_id"]
            or header.get("policy_epoch") != prepared.binding.policy_epoch
        ):
            _refuse("payload does not belong to the prepared request")
        if not _int(now) or now < prepared.binding.observed_ns:
            _refuse("decode time precedes the observation")
        if now >= request["deadline"]["due_ns"]:
            _refuse("policy answer is past the request deadline")
        if type(result) is not dict or result.get("kind") == "unavailable":
            _refuse("worker did not return an action result")
        if result.get("request_id", request["request_id"]) != request["request_id"]:
            _refuse("worker answered another request")
        if result.get("action_contract", cfg.action_contract) != cfg.action_contract:
            _refuse("worker action contract differs from the injected one")
        if result.get("model_version", cfg.model_version) != cfg.model_version:
            _refuse("worker model version differs from the injected one")
        actions = result.get("actions")
        if type(actions) not in (np.ndarray, list, tuple):
            _refuse("actions missing or of an unsupported type")
        try:
            chunk = np.asarray(actions)
        except ValueError:
            _refuse("actions are ragged")
        if chunk.dtype.kind not in "fiu":
            _refuse("actions must be numeric")
        chunk = chunk.astype(np.float32)
        if (
            chunk.ndim != 2
            or chunk.shape[1] != cfg.action_dim
            or not 0 < chunk.shape[0] <= cfg.max_horizon
        ):
            _refuse(f"expected (H<={cfg.max_horizon}, {cfg.action_dim}) {chunk.shape}")
        if not np.isfinite(chunk).all():
            _refuse("actions contain NaN or infinity")
        wall = cfg.wall_clock_ns()
        if not _int(wall) or wall < 0:
            _refuse("wall clock returned a non-integer")
        return {
            "schema": SCHEMA,
            "minor": 0,
            "run_id": request["run_id"],
            "emitted_wall_ns": wall,
            "kind": "chunk_response",
            "request_id": request["request_id"],
            "episode_id": request["episode_id"],
            "policy_epoch": request["policy_epoch"],
            "action_contract": cfg.action_contract,
            "actions": chunk.tolist(),
            "valid_from_action_index": request["action_start_index"],
            "prefix_conditioning_applied": "none",
            "model_version": dict(cfg.model_version),
            "timing": {
                "total_ms": max(0, (now - prepared.binding.observed_ns) // 1_000_000)
            },
            "clock_domain": prepared.binding.clock_domain,
        }


def _cpu_only(value):
    """Same rule as the owned IPC boundary: scalars, bytes, containers, arrays."""
    if value is None or type(value) in (str, bytes, bool, int, float):
        return
    if type(value) in (list, tuple):
        for item in value:
            _cpu_only(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                _refuse("result keys must be strings")
            _cpu_only(item)
        return
    if type(value) is np.ndarray and not value.dtype.hasobject:
        return
    _refuse("result is not CPU data")


def make_completion_adapter(*, verify_ack, roles):
    """``completion_adapter(request, returned) -> CompletionEvidence``.

    ``returned`` is what the borrowed executor's ``collect_existing`` gives
    back (request, sdk_ack, stop_proof, observation, result). ``verify_ack``
    is injected by whoever owns the worker identity (launch id, capability);
    this adapter never accepts an ack by shape alone. The evidence keeps the
    very request object Cycle passed in, because Cycle compares by identity.
    """
    if not callable(verify_ack):
        raise ValueError("verify_ack is required")  # noqa: TRY004
    if (
        type(roles) is not tuple
        or not roles
        or not all(type(r) is str and r for r in roles)
    ):
        raise ValueError("roles must be a non-empty tuple of role names")

    def adapt(request, returned):
        if getattr(request, "role", None) not in roles:
            _refuse("request role is not covered by this adapter")
        if getattr(returned, "request", None) is not request:
            _refuse("completion belongs to another request object")
        ack = getattr(returned, "sdk_ack", None)
        if ack is None or verify_ack(request, ack) is not True:
            _refuse("SDK idle ack not verified")
        proof = getattr(returned, "stop_proof", None)
        if getattr(proof, "request", None) is not request:
            _refuse("stop proof belongs to another request")
        if getattr(returned, "observation", None) is None:
            _refuse("completion has no post-request observation")
        result = getattr(returned, "result", None)
        if result is None:
            _refuse("result was withdrawn")
        _cpu_only(result)
        return CompletionEvidence(request, result, proof, returned.observation)

    return adapt
