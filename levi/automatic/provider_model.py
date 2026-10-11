"""Frozen predicate-only model contract; no robot actions or inferred geometry."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass

from levi.domain import aeri

from .scene_assessment import arbitrate, contract_from

SPEC_SCHEMA = "levi.aeri.model_spec.v1"
ANSWER_SCHEMA = "levi.aeri.predicate_answer.v1"
SHA = re.compile(r"^[a-f0-9]{64}$")


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def strict_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    def constant(_):
        raise ValueError("nonfinite JSON")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def exact(value, keys, where):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f"{where}: exact fields required")


def positive(value, where, maximum):
    if type(value) is not int or not 0 < value <= maximum:
        raise ValueError(f"{where}: positive bounded integer required")


def hash_value(value, where):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError(f"{where}: sha256 required")


def answer_schema():
    evidence = {
        "type": "object",
        "additionalProperties": False,
        "required": ["ref", "sha256"],
        "properties": {"ref": {"type": "string"}, "sha256": {"type": "string"}},
    }
    predicate = {
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "required", "value", "evidence_refs"],
        "properties": {
            "name": {"type": "string"},
            "required": {"type": "boolean"},
            "value": {"type": ["boolean", "null"]},
            "evidence_refs": {"type": "array", "maxItems": 32, "items": evidence},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "spec_sha256",
            "model_digest",
            "observed_ns",
            "predicates",
        ],
        "properties": {
            "schema": {"const": ANSWER_SCHEMA},
            "spec_sha256": {"type": "string"},
            "model_digest": {"type": "string"},
            "observed_ns": {"type": "integer"},
            "predicates": {"type": "array", "maxItems": 32, "items": predicate},
        },
    }


def prompt_template(value):
    return (
        "Read only these frozen physical predicates from the supplied current images. "
        "Return true/false/null for every named predicate; null if unreadable. "
        "Do not decide readiness, success, actions or motion. Do not infer a predicate "
        "definition from its name. Cite only supplied frame refs and hashes.\n"
        + canonical(
            {
                "contract": value["contract"],
                "definitions": value["definitions"],
                "schema": answer_schema(),
            }
        ).decode()
    )


@dataclass(frozen=True)
class FrozenSpec:
    # Canonical bytes prevent callers changing dictionaries after registration.
    data: bytes
    sha256: str

    @property
    def value(self):
        return strict_json(self.data)

    @property
    def contract(self):
        return contract_from(self.value["contract"])

    @classmethod
    def load(cls, value):
        exact(
            value,
            (
                "schema",
                "id",
                "version",
                "run_id",
                "plan_sha256",
                "contract",
                "definitions",
                "source",
                "provider",
                "deadlines_ns",
                "image_budget",
                "max_output_tokens",
                "observation_max_age_ns",
                "validity_ns",
                "prompt_sha256",
                "answer_schema_sha256",
            ),
            "spec",
        )
        if value["schema"] != SPEC_SCHEMA:
            raise ValueError("model spec schema mismatch")
        for field in ("id", "version", "run_id"):
            if not isinstance(value[field], str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value[field]
            ):
                raise ValueError(f"invalid {field}")
        hash_value(value["plan_sha256"], "plan")
        contract = contract_from(value["contract"])
        if contract.status != "confirmed":
            raise ValueError("physical Initial State Contract is not confirmed")
        names = (*contract.required, *contract.optional)
        exact(value["definitions"], names, "predicate definitions")
        for text in value["definitions"].values():
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 4000:
                raise ValueError("explicit physical predicate definition required")
        exact(value["source"], ("ref", "sha256"), "frozen definition source")
        if (
            not isinstance(value["source"]["ref"], str)
            or not value["source"]["ref"].strip()
        ):
            raise ValueError("operator frozen source reference required")
        hash_value(value["source"]["sha256"], "definition source")
        # The frozen source is the exact contract plus definitions, not a guessed name.
        if value["source"]["sha256"] != digest(
            {"contract": value["contract"], "definitions": value["definitions"]}
        ):
            raise ValueError("frozen source content hash mismatch")
        from levi.agent.schema import ProviderConfig

        exact(value["provider"], ProviderConfig.model_fields, "frozen model profile")
        provider = ProviderConfig.model_validate(value["provider"], strict=True)
        if (
            provider.kind != "openai-local"
            or not provider.enabled
            or not provider.model_digest
            or not provider.vision
            or not provider.structured_output
            or not provider.max_images
        ):
            raise ValueError("bound shared local vision model required")
        import ipaddress
        from urllib.parse import urlsplit

        url = urlsplit(provider.base_url)
        if (
            url.scheme != "http"
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in ("", "/")
            or not provider.allow_localhost
            or not ipaddress.ip_address(url.hostname).is_loopback
            or url.port in aeri.ROBOT_PORTS
        ):
            raise ValueError("explicit numeric loopback model endpoint required")
        exact(
            value["deadlines_ns"],
            ("scene", "reset_final", "reset_active"),
            "frozen deadlines",
        )
        for phase, duration in value["deadlines_ns"].items():
            positive(duration, phase, 300_000_000_000)
        positive(value["image_budget"], "image budget", min(provider.max_images, 32))
        positive(value["max_output_tokens"], "output tokens", provider.context_tokens)
        positive(value["observation_max_age_ns"], "observation age", 30_000_000_000)
        positive(
            value["validity_ns"],
            "result validity",
            aeri.MAX_RESULT_VALIDITY_MS * 1_000_000,
        )
        if (
            value["prompt_sha256"]
            != hashlib.sha256(prompt_template(value).encode()).hexdigest()
        ):
            raise ValueError("prompt hash mismatch")
        if value["answer_schema_sha256"] != digest(answer_schema()):
            raise ValueError("answer schema hash mismatch")
        data = canonical(value)
        return cls(data, hashlib.sha256(data).hexdigest())


def freeze(value):
    """Hash an explicitly supplied operator source/model/plan; never approve it."""
    value = strict_json(canonical(value))
    value["prompt_sha256"] = hashlib.sha256(prompt_template(value).encode()).hexdigest()
    value["answer_schema_sha256"] = digest(answer_schema())
    return FrozenSpec.load(value)


def validate_input(spec, payload, *, now_ns):
    value, contract = spec.value, spec.contract
    if payload.get("schema") != "levi.aeri.provider_request.v1":
        raise ValueError("provider request schema mismatch")
    if payload.get("clock_domain") != aeri.host_clock_domain():
        raise ValueError("provider clock domain mismatch")
    expected = {
        "id": contract.contract_id,
        "version": contract.contract_version,
        "required": list(contract.required),
        "optional": list(contract.optional),
    }
    if payload.get("contract") != expected:
        raise ValueError("request Initial State Contract mismatch")
    request = payload.get("request")
    if not isinstance(request, dict):
        raise TypeError("request identity required")
    allowed = {
        "request_id",
        "run_id",
        "episode_id",
        "target",
        "episode_role",
        "observed_from_step",
        "observed_through_step",
        "observed_through_ns",
        "event_ids",
        "spec_sha256",
        "plan_sha256",
        "submitted_ns",
        "deadline_due_ns",
        "phase",
    }
    if set(request) - allowed:
        raise ValueError("undefined provider request field")
    if (
        request.get("run_id") != value["run_id"]
        or request.get("plan_sha256") != value["plan_sha256"]
    ):
        raise ValueError("request run/plan binding missing or mismatched")
    if request.get("spec_sha256") != spec.sha256:
        raise ValueError("request frozen model spec mismatch")
    episode = request.get("episode_id", "")
    if not isinstance(episode, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\.(forward|reset)\.[0-9]{4,6}", episode
    ):
        raise ValueError("invalid episode identity")
    run, group, _ = aeri.episode_parts(episode)
    if (
        run != value["run_id"]
        or not isinstance(request.get("request_id"), str)
        or not request["request_id"]
    ):
        raise ValueError("request identity mismatch")
    phase = request.get("phase")
    kind = payload.get("kind")
    if kind == "scene":
        if phase != "scene" or request.get("target") not in (
            "initial_state",
            "post_reset",
            "post_forward",
        ):
            raise ValueError("scene phase/target mismatch")
    elif kind == "reset_judgement":
        if (
            phase not in ("reset_final", "reset_active")
            or request.get("episode_role") != "reset"
            or group != "reset"
            or request.get("target") != "reset_goal"
        ):
            raise ValueError(
                "reset uses only the Initial State Contract, never generic-final"
            )
        for field in ("observed_from_step", "observed_through_step"):
            if type(request.get(field)) is not int or request[field] < 0:
                raise ValueError("reset observation steps required")
        if request["observed_from_step"] > request["observed_through_step"]:
            raise ValueError("reset observation steps reversed")
    else:
        raise ValueError("unsupported provider capability (no frame-bearing events)")
    observed, submitted, due = (
        payload.get("observed_ns"),
        request.get("submitted_ns"),
        request.get("deadline_due_ns"),
    )
    if any(type(x) is not int for x in (observed, submitted, due)):
        raise ValueError("explicit frozen request deadline required")
    if (
        not 0 <= submitted <= observed <= now_ns
        or now_ns - observed > value["observation_max_age_ns"]
    ):
        raise ValueError("stale/future observation")
    if not submitted < due <= submitted + value["deadlines_ns"][phase] or now_ns >= due:
        raise ValueError("deadline expired or exceeds the frozen plan")
    if kind == "reset_judgement" and request.get("observed_through_ns") != observed:
        raise ValueError("reset observation time mismatch")
    frames = payload.get("frames")
    if not isinstance(frames, list) or not 1 <= len(frames) <= value["image_budget"]:
        raise ValueError("image budget violation")
    refs = set()
    for frame in frames:
        if (
            not isinstance(frame, dict)
            or not isinstance(frame.get("ref"), str)
            or not frame["ref"]
        ):
            raise ValueError("frame identity missing")
        if frame["ref"] in refs:
            raise ValueError("duplicate frame reference")
        refs.add(frame["ref"])
        hash_value(frame.get("sha256"), "frame")
        if not isinstance(frame.get("view"), str) or not frame["ref"].startswith(
            frame["view"] + ":"
        ):
            raise ValueError("frame view/reference mismatch")
    return due


def question(spec, payload):
    return (
        prompt_template(spec.value)
        + "\nCurrent request: "
        + canonical(
            {
                "spec_sha256": spec.sha256,
                "model_digest": spec.value["provider"]["model_digest"],
                "observed_ns": payload["observed_ns"],
                "frames": [
                    {k: f[k] for k in ("ref", "sha256", "view")}
                    for f in payload["frames"]
                ],
            }
        ).decode()
    )


def model_result(spec, payload, raw, *, produced_ns):
    validate_input(spec, payload, now_ns=produced_ns)
    answer = strict_json(raw)
    exact(
        answer,
        ("schema", "spec_sha256", "model_digest", "observed_ns", "predicates"),
        "model answer",
    )
    if (
        answer["schema"] != ANSWER_SCHEMA
        or answer["spec_sha256"] != spec.sha256
        or answer["model_digest"] != spec.value["provider"]["model_digest"]
        or type(answer["observed_ns"]) is not int
        or answer["observed_ns"] != payload["observed_ns"]
    ):
        raise ValueError("model answer identity/spec/model/time mismatch")
    contract = spec.contract
    rows = answer["predicates"]
    if not isinstance(rows, list) or len(rows) != len(contract.required) + len(
        contract.optional
    ):
        raise ValueError("complete frozen predicate set required")
    current = {f["ref"]: f["sha256"] for f in payload["frames"]}
    predicates, seen = [], set()
    for row in rows:
        exact(row, ("name", "value", "required", "evidence_refs"), "predicate")
        name, result = row["name"], row["value"]
        if name in seen or name not in (*contract.required, *contract.optional):
            raise ValueError("duplicate/undefined predicate")
        seen.add(name)
        if type(row["required"]) is not bool or row["required"] != (
            name in contract.required
        ):
            raise ValueError("required/optional predicate downgrade")
        if result is not None and type(result) is not bool:
            raise ValueError("predicate is true/false/null only")
        if not isinstance(row["evidence_refs"], list) or len(row["evidence_refs"]) > 32:
            raise ValueError("bounded evidence references required")
        evidence = []
        for ref in row["evidence_refs"]:
            exact(ref, ("ref", "sha256"), "model frame evidence")
            if current.get(ref["ref"]) != ref["sha256"]:
                raise ValueError("model evidence is not this request's retained frame")
            evidence.append({"kind": "frame", **ref})
        if result is not None and not evidence:
            raise ValueError("a read predicate needs current evidence")
        predicates.append(
            {
                "name": name,
                "required": row["required"],
                "value": result,
                "evidence_refs": evidence,
            }
        )
    failed = [p["name"] for p in predicates if p["required"] and p["value"] is False]
    unread = [p["name"] for p in predicates if p["required"] and p["value"] is None]
    request = payload["request"]
    common = {
        "minor": aeri.MINOR,
        "run_id": request["run_id"],
        "emitted_wall_ns": time.time_ns(),
        "request_id": request["request_id"],
        "episode_id": request["episode_id"],
        "target": request["target"],
        "predicate_results": predicates,
        "produced_ns": produced_ns,
        "valid_until_ns": produced_ns + spec.value["validity_ns"],
        "clock_domain": payload["clock_domain"],
        "provider": "vlm",
        "model_name": spec.value["provider"]["model"],
        "model_digest": spec.value["provider"]["model_digest"],
        "confidence_kind": "none",
    }
    evidence = [
        {"kind": "frame", "ref": f["ref"], "sha256": f["sha256"]}
        for f in payload["frames"]
    ]
    scene = {
        **common,
        "target": request["target"] if payload["kind"] == "scene" else "post_reset",
        "schema": aeri.SCHEMAS["scene"],
        "kind": "scene",
        "assessment_id": request["request_id"] + ":assessment",
        "contract_id": contract.contract_id,
        "contract_version": contract.contract_version,
        "evidence_refs": evidence,
        "observed_ns": payload["observed_ns"],
        "failed_predicates": failed,
        "unknown_predicates": unread,
        "decision": "reset_required" if failed else "unknown" if unread else "ready",
        "unknown_reason": "model_undecided" if unread and not failed else None,
    }
    message = aeri.validate(scene, "scene", specs=contract.spec())
    verdict = arbitrate(message, contract)
    if verdict.decision == "unknown" and scene["decision"] == "ready":
        scene.update(decision="unknown", unknown_reason="insufficient_evidence")
        message = aeri.validate(scene, "scene", specs=contract.spec())
    if payload["kind"] == "scene":
        return message.model_dump(mode="json", by_alias=True)
    decision = {
        "ready": "confirmed",
        "reset_required": "rejected",
        "unknown": "unknown",
    }[verdict.decision]
    result = {
        **common,
        "schema": aeri.SCHEMAS["judgement"],
        "kind": "judgement",
        "judgement_id": request["request_id"] + ":judgement",
        "episode_role": "reset",
        "decision": decision,
        "unknown_reason": (scene["unknown_reason"] or "insufficient_evidence")
        if decision == "unknown"
        else None,
        "observed_from_step": request["observed_from_step"],
        "observed_through_step": request["observed_through_step"],
        "observed_through_ns": payload["observed_ns"],
        "spec_id": contract.contract_id,
        "spec_version": contract.contract_version,
    }
    return aeri.validate(result, "judgement", specs=contract.spec()).model_dump(
        mode="json", by_alias=True
    )


def validate_result(spec, payload, value, *, now_ns):
    """Re-fence an online response at the file boundary, independent of its producer."""
    validate_input(spec, payload, now_ns=now_ns)
    kind = "scene" if payload["kind"] == "scene" else "judgement"
    message = aeri.validate(value, kind, specs=spec.contract.spec())
    request, contract = payload["request"], spec.contract
    if message.kind == "unavailable":
        raise ValueError("model response unavailable")
    if any(
        getattr(message, k) != request[k]
        for k in ("run_id", "request_id", "episode_id", "target")
    ):
        raise ValueError("response identity mismatch")
    identity = (
        (message.contract_id, message.contract_version)
        if kind == "scene"
        else (message.spec_id, message.spec_version)
    )
    observed = message.observed_ns if kind == "scene" else message.observed_through_ns
    if (
        identity != contract.key
        or observed != payload["observed_ns"]
        or message.provider != "vlm"
        or message.model_name != spec.value["provider"]["model"]
        or message.model_digest != spec.value["provider"]["model_digest"]
        or message.valid_until_ns - message.produced_ns > spec.value["validity_ns"]
        or message.produced_ns > now_ns
    ):
        raise ValueError("response spec/model/observation/time mismatch")
    aeri.check_fresh(message, now_ns=now_ns, local=payload["clock_domain"])
    predicates = {p.name: p for p in message.predicate_results}
    if set(predicates) != {*contract.required, *contract.optional}:
        raise ValueError("complete frozen response predicate set required")
    current = {f["ref"]: f["sha256"] for f in payload["frames"]}
    cited = []
    for name, predicate in predicates.items():
        if predicate.required != (name in contract.required):
            raise ValueError("response downgraded required predicate")
        if predicate.value is not None and not predicate.evidence_refs:
            raise ValueError("response read predicate lacks evidence")
        cited.extend(predicate.evidence_refs)
    cited.extend(getattr(message, "evidence_refs", []))
    if any(e.kind != "frame" or current.get(e.ref) != e.sha256 for e in cited):
        raise ValueError("response cites old/unretained evidence")
    failed = any(predicates[n].value is False for n in contract.required)
    unread = any(predicates[n].value is None for n in contract.required)
    # Preferred views and minimum evidence govern reset confirmation too.
    views = {e.ref.split(":", 1)[0] for e in cited}
    refs = {e.ref for e in cited}
    enough = not contract.require_visible_evidence or (
        len(refs) >= contract.min_evidence_refs
        and (
            not contract.require_all_views
            or set(contract.preferred_views).issubset(views)
        )
    )
    expected = (
        "reset_required" if failed else "unknown" if unread or not enough else "ready"
    )
    if kind == "judgement":
        expected = {
            "reset_required": "rejected",
            "unknown": "unknown",
            "ready": "confirmed",
        }[expected]
        if (
            message.episode_role != "reset"
            or message.observed_from_step != request["observed_from_step"]
            or message.observed_through_step != request["observed_through_step"]
        ):
            raise ValueError("reset response observation window mismatch")
    if message.decision != expected:
        raise ValueError("response decision differs from frozen predicate arbitration")
    return message
