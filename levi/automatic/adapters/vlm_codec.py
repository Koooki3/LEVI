"""CPU codecs for the owned judge (VLM) role: scene and paused-goal calls.

``OwnedCalls`` takes four explicit callbacks for this role. ``VlmCodec`` provides
them over the frozen predicate-only contract that already serves the external C5
and file providers: the same ``FrozenSpec``, ``question``, ``model_result`` and
``validate_result`` from ``provider_model`` decide what is asked and what answer
is accepted. Nothing here chooses a model, prompt, image count or size: the
caller injects the frozen spec, the views, the longest image side and the JPEG
quality, and a missing or malformed value is refused at construction.

Only scalars, bytes and containers cross the owned IPC. The payload is one
``bytes`` envelope (a canonical-JSON header followed by the JPEG bytes the
header lists); ``unpack_payload`` is what a worker-side factory calls to read
it. This module starts no model, process, transport or camera, and installs no
authority. It keeps no state between calls, writes nothing and logs nothing: a
decoded answer goes only to the caller, so an automatic judgement cannot reach
an operator-facing path from here (unknown stays ``unknown``; an expired,
malformed or mismatching answer raises ``InterventionUnproven`` and is never
turned into a decision).
"""

import base64
import binascii
import hashlib
import struct

import numpy as np

from levi.domain import aeri

from .. import provider_model as pm
from ..intervention import InterventionUnproven
from .owned_calls import MAX_BYTES, PreparedCall, SourceBinding, canonical

PAYLOAD_SCHEMA = "levi.aeri.owned_vlm_call.v1"
MAGIC = b"LEVI-VLM-CALL1\n"
# Fields the codec adds to the orchestrator request; a request already carrying
# one is refused instead of trusted.
ADDED = ("spec_sha256", "plan_sha256", "submitted_ns", "deadline_due_ns", "phase")
KIND = {"scene": "scene", "goal": "reset_judgement"}
SOURCE = {"scene": "scene", "goal": "paused"}
MAX_SIDE_LIMIT = 8192
ANSWER_BYTES_PER_TOKEN = 16


def pack_payload(header, images):
    """One CPU ``bytes`` payload: magic, header length, canonical header, JPEGs."""
    body = canonical(header)
    return MAGIC + struct.pack(">Q", len(body)) + body + b"".join(images)


def unpack_payload(payload):
    """Strictly read a payload; raises ``ValueError`` for anything unexpected.

    Returns the header, the provider request (``provider``), the frozen
    ``question``, ``max_output_tokens`` and ``answer_schema``, and ``frames``:
    ``{ref, sha256, view, bytes}`` in order, each hash recomputed from its bytes.
    """
    if type(payload) is not bytes or not payload.startswith(MAGIC):
        raise ValueError("owned VLM payload envelope required")
    start = len(MAGIC) + 8
    if len(payload) < start:
        raise ValueError("owned VLM payload truncated")
    (size,) = struct.unpack(">Q", payload[len(MAGIC) : start])
    if size > len(payload) - start:
        raise ValueError("owned VLM payload header length invalid")
    header = pm.strict_json(payload[start : start + size])
    pm.exact(
        header,
        (
            "schema",
            "provider",
            "question",
            "max_output_tokens",
            "answer_schema",
            "spec_sha256",
            "model_digest",
        ),
        "owned VLM header",
    )
    provider = header["provider"]
    if header["schema"] != PAYLOAD_SCHEMA or not isinstance(provider, dict):
        raise ValueError("owned VLM payload schema mismatch")
    listed = provider.get("frames")
    if not isinstance(listed, list):
        raise ValueError("owned VLM frame list required")  # noqa: TRY004
    offset, frames = start + size, []
    for item in listed:
        pm.exact(item, ("ref", "sha256", "view", "length"), "owned VLM frame")
        length = item["length"]
        if type(length) is not int or length <= 0:
            raise ValueError("owned VLM frame length invalid")
        data = payload[offset : offset + length]
        offset += length
        if len(data) != length or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError("owned VLM frame bytes differ from the header")
        frames.append({**item, "bytes": data})
    if offset != len(payload):
        raise ValueError("owned VLM payload has trailing bytes")
    return {
        "header": header,
        "kind": provider.get("kind"),
        "provider": provider,
        "question": header["question"],
        "max_output_tokens": header["max_output_tokens"],
        "answer_schema": header["answer_schema"],
        "observed_ns": provider.get("observed_ns"),
        "frames": frames,
    }


def _positive_int(value, name, maximum):
    if type(value) is not int or not 0 < value <= maximum:
        raise ValueError(f"{name}: positive bounded integer required")


class VlmCodec:
    """Injected scene/goal codecs for ``OwnedCalls`` (judge role only).

    ``freeze(request, observation)`` must return a ``PausedEvidence``; the usual
    value is ``lambda req, obs: paused_frames.freeze(req, obs, module, judge)``
    so the goal images come from the existing retained paused window.
    """

    def __init__(self, spec, *, clock, views, max_side, jpeg_quality, freeze):
        if type(spec) is not pm.FrozenSpec:
            raise ValueError("a FrozenSpec is required")
        if hashlib.sha256(spec.data).hexdigest() != spec.sha256:
            raise ValueError("frozen spec hash mismatch")
        # Re-run the full frozen-spec checks (prompt, schema, source, endpoint).
        if pm.FrozenSpec.load(spec.value) != spec:
            raise ValueError("frozen spec is not its own canonical form")
        if (
            not callable(getattr(clock, "now", None))
            or getattr(clock, "domain", None) != aeri.host_clock_domain()
        ):
            raise ValueError("an injected clock in this host's clock domain required")
        if (
            type(views) is not tuple
            or not views
            or len(set(views)) != len(views)
            or any(not isinstance(v, str) or not v or ":" in v for v in views)
            or len(views) > spec.value["image_budget"]
        ):
            raise ValueError("explicit unique views within the frozen image budget")
        _positive_int(max_side, "max_side", MAX_SIDE_LIMIT)
        _positive_int(jpeg_quality, "jpeg_quality", 100)
        if not callable(freeze):
            raise ValueError("an injected paused-window freeze is required")  # noqa: TRY004
        self._spec, self._clock, self._views = spec, clock, views
        self._max_side, self._quality, self._freeze = max_side, jpeg_quality, freeze

    # --- preparation -------------------------------------------------------------------

    def prepare_scene(self, request, observation, context):
        try:
            observed = self._observed(observation)
            ref_key = self._ref_key(request)
            images = observation.get("images_bgr")
            if not isinstance(images, dict) or any(
                v not in images for v in self._views
            ):
                raise InterventionUnproven("every configured scene view is required")
            frames = tuple(
                (f"{view}:{ref_key}", self._jpeg(images[view])) for view in self._views
            )
            return self._call("scene", request, observed, context, frames)
        except InterventionUnproven:
            raise
        except Exception as exc:
            raise InterventionUnproven("scene preparation failed; no fallback") from exc

    def prepare_goal(self, request, observation, context):
        try:
            observed = self._observed(observation)
            ref_key = self._ref_key(request)
            if request.get("episode_role") != (
                "forward" if self._spec.is_forward else "reset"
            ) or request.get("target") != (
                "forward_goal" if self._spec.is_forward else "reset_goal"
            ):
                raise InterventionUnproven(
                    "the frozen spec judges its explicitly bound goal role only"
                )
            evidence = self._freeze(request, observation)
            self._check_pause(evidence, request, context, observed)
            frames = tuple(
                (f"{role}:{ref_key}:{index}", data)
                for index, (role, data) in enumerate(self._paused_jpegs(evidence))
            )
            return self._call("goal", request, observed, context, frames)
        except InterventionUnproven:
            raise
        except Exception as exc:
            raise InterventionUnproven("goal preparation failed; no fallback") from exc

    def _observed(self, observation):
        observed = (
            observation.get("observed_ns") if isinstance(observation, dict) else None
        )
        if type(observed) is not int:
            raise InterventionUnproven("integer observation time required")
        return observed

    def _ref_key(self, request):
        if type(request) is not dict:
            raise InterventionUnproven("exact AERI request required")
        if (
            aeri.operator_keys(request)
            or aeri.control_keys(request)
            or set(ADDED) & set(request)
        ):
            raise InterventionUnproven("request carries operator/control/frozen keys")
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise InterventionUnproven("request identity required")
        return hashlib.sha256(request_id.encode()).hexdigest()[:16]

    def _jpeg(self, pixels):
        import cv2

        if (
            type(pixels) is not np.ndarray
            or pixels.dtype != np.uint8
            or pixels.ndim != 3
            or pixels.shape[2] != 3
            or min(pixels.shape[:2]) < 1
        ):
            raise InterventionUnproven("a uint8 BGR image is required")
        height, width = pixels.shape[:2]
        scale = self._max_side / max(height, width)
        if scale < 1:  # shrink only; never invent detail
            pixels = cv2.resize(
                pixels,
                (max(1, round(width * scale)), max(1, round(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
        ok, data = cv2.imencode(
            ".jpg", pixels, [cv2.IMWRITE_JPEG_QUALITY, self._quality]
        )
        if not ok:
            raise InterventionUnproven("current camera frame encoding failed")
        return data.tobytes()

    def _check_pause(self, evidence, request, context, observed):
        from .paused_frames import PauseBinding, PausedEvidence

        binding = getattr(evidence, "binding", None)
        if type(evidence) is not PausedEvidence or type(binding) is not PauseBinding:
            raise InterventionUnproven("PausedFrames.freeze evidence required")
        if (
            (binding.run_id, binding.episode_id, binding.role)
            != (request["run_id"], request["episode_id"], request["episode_role"])
            or type(binding.policy_epoch) is not int
            or binding.policy_epoch != context.policy_epoch
            or binding.clock_domain != self._clock.domain
            or type(binding.committed_ns) is not int
            or binding.committed_ns > observed
        ):
            raise InterventionUnproven("paused evidence belongs to another source")

    def _paused_jpegs(self, evidence):
        images = evidence.images
        picks = tuple(evidence.proof.picks) + tuple(evidence.proof.start_picks)
        if (
            type(images) is not tuple
            or not 0 < len(images) <= self._spec.value["image_budget"]
            or len(picks) != len(images)
        ):
            raise InterventionUnproven("paused images differ from the frozen proof")
        out = []
        for image, pick in zip(images, picks, strict=True):
            pm.exact(image, ("role", "offset_s", "step", "jpeg_b64"), "paused image")
            role = image["role"]
            try:
                data = base64.b64decode(image["jpeg_b64"], validate=True)
            except (binascii.Error, TypeError, ValueError) as exc:
                raise InterventionUnproven("paused image is not base64") from exc
            if (
                not isinstance(role, str)
                or not role
                or ":" in role
                or pick.role != role
                or not data.startswith(b"\xff\xd8")
                or pick.jpeg_sha256 != hashlib.sha256(data).hexdigest()
            ):
                raise InterventionUnproven("paused image differs from the frozen proof")
            out.append((role, data))
        return out

    def _provider_kind(self, kind):
        return (
            "forward_judgement"
            if kind == "goal" and self._spec.is_forward
            else KIND[kind]
        )

    def _call(self, kind, request, observed, context, frames):
        spec, value = self._spec, self._spec.value
        if context.kind != kind:
            raise InterventionUnproven("preparation context kind mismatch")
        if (
            type(context.policy_epoch) is not int
            or type(context.deadline_ns) is not int
        ):
            raise InterventionUnproven("integer epoch and deadline required")
        role = "forward" if spec.is_forward else "reset"
        phase = (
            "scene"
            if kind == "scene"
            else role + "_final"
            if request["request_id"].endswith(":final")
            else role + "_active"
        )
        contract = spec.contract
        provider = {
            "schema": "levi.aeri.provider_request.v1",
            "kind": self._provider_kind(kind),
            "request": {
                **request,
                "spec_sha256": spec.sha256,
                "plan_sha256": value["plan_sha256"],
                # The earliest honest bound: this frame is the request's source.
                "submitted_ns": observed,
                "phase": phase,
                "deadline_due_ns": context.deadline_ns,
            },
            "clock_domain": self._clock.domain,
            "observed_ns": observed,
            "frames": [
                {
                    "ref": ref,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "view": ref.split(":", 1)[0],
                    "length": len(data),
                }
                for ref, data in frames
            ],
            "contract": {
                "id": contract.contract_id,
                "version": contract.contract_version,
                "required": list(contract.required),
                "optional": list(contract.optional),
            },
        }
        pm.validate_input(spec, provider, now_ns=self._clock.now())
        header = {
            "schema": PAYLOAD_SCHEMA,
            "provider": provider,
            "question": pm.question(spec, provider),
            "max_output_tokens": value["max_output_tokens"],
            "answer_schema": pm.answer_schema(),
            "spec_sha256": spec.sha256,
            "model_digest": value["provider"]["model_digest"],
        }
        payload = pack_payload(header, [data for _, data in frames])
        if len(payload) > MAX_BYTES:
            raise InterventionUnproven("owned VLM payload exceeds the byte bound")
        binding = SourceBinding(
            request["run_id"],
            request["episode_id"],
            request["episode_id"].rsplit(".", 2)[1],
            context.policy_epoch,
            self._clock.domain,
            observed,
            SOURCE[kind],
            hashlib.sha256(payload).hexdigest(),
        )
        return PreparedCall(canonical(request), payload, binding, frames)

    # --- decoding ----------------------------------------------------------------------

    def decode_scene(self, prepared, result, now):
        return self._decode("scene", prepared, result, now)

    def decode_goal(self, prepared, result, now):
        return self._decode("goal", prepared, result, now)

    def _decode(self, kind, prepared, result, now):
        try:
            opened = self._reopen(kind, prepared)
            raw = self._text(result)
            provider = opened["provider"]
            message = pm.model_result(self._spec, provider, raw, produced_ns=now)
            pm.validate_result(self._spec, provider, message, now_ns=now)
            return canonical(message)
        except InterventionUnproven:
            raise
        except Exception as exc:
            raise InterventionUnproven(
                f"{kind} answer rejected by the frozen contract"
            ) from exc

    def _reopen(self, kind, prepared):
        """Re-derive everything the answer is judged against from the payload."""
        if (
            type(prepared) is not PreparedCall
            or type(prepared.binding) is not SourceBinding
        ):
            raise InterventionUnproven("the prepared call is required")
        opened = unpack_payload(prepared.payload)
        spec, header = self._spec, opened["header"]
        provider, binding = opened["provider"], prepared.binding
        request = pm.strict_json(prepared.request)
        carried = {k: v for k, v in provider["request"].items() if k not in ADDED}
        added = {k: v for k, v in provider["request"].items() if k in ADDED}
        if (
            opened["kind"] != self._provider_kind(kind)
            or binding.source_kind != SOURCE[kind]
            or canonical(carried) != canonical(request)
            or set(added) != set(ADDED)
            or header["spec_sha256"] != spec.sha256
            or added["spec_sha256"] != spec.sha256
            or header["model_digest"] != spec.value["provider"]["model_digest"]
            or header["max_output_tokens"] != spec.value["max_output_tokens"]
            or header["answer_schema"] != pm.answer_schema()
            or header["question"] != pm.question(spec, provider)
            or provider.get("clock_domain") != self._clock.domain
            or binding.clock_domain != self._clock.domain
            or binding.observed_ns != provider["observed_ns"]
            or binding.payload_sha256 != hashlib.sha256(prepared.payload).hexdigest()
            or (binding.run_id, binding.episode_id)
            != (request.get("run_id"), request.get("episode_id"))
            or prepared.frames
            != tuple((f["ref"], f["bytes"]) for f in opened["frames"])
        ):
            raise InterventionUnproven(
                "prepared call differs from the frozen spec, question or source"
            )
        return opened

    def _text(self, result):
        if type(result) in (list, tuple):
            if len(result) != 1:
                raise InterventionUnproven("exactly one model answer required")
            result = result[0]
        if type(result) is bytes:
            result = result.decode("utf-8")
        limit = self._spec.value["max_output_tokens"] * ANSWER_BYTES_PER_TOKEN
        if (
            type(result) is not str
            or not result.strip()
            or len(result.encode()) > limit
        ):
            raise InterventionUnproven("a bounded model answer text is required")
        return result
