"""Real provider file transport: no scripted readiness or goal decisions.

A local provider reads requests and writes complete AERI wire messages. The
orchestrator validates and fences all answers. Missing or malformed providers
never grant motion. The transport itself never executes commands.
"""

import hashlib
import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from levi.domain import aeri

from . import camera_meta
from .adapters import legacy_live
from .journal import write_durable


def key(request_id):
    return hashlib.sha256(request_id.encode()).hexdigest()


def read_answer(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, 256 * 1024 + 1)
        return data if len(data) <= 256 * 1024 else b"{}"
    finally:
        os.close(fd)


def ready(root, contract, capabilities, *, now=None, model_spec=None):
    raw = read_answer(Path(root) / "READY.json")
    try:
        value = json.loads(raw) if raw else {}
    except (ValueError, UnicodeDecodeError):
        return False
    now = time.time_ns() if now is None else now
    stamp = value.get("updated_wall_ns") if isinstance(value, dict) else None
    if model_spec is not None:
        from levi.children import identity

        recorded = value.get("identity") if isinstance(value, dict) else None
        pid = recorded.get("pid") if isinstance(recorded, dict) else None
        actual = identity(pid) if type(pid) is int and pid > 0 else None
        if (
            actual is None
            or recorded
            != {
                "pid": pid,
                "start_ticks": int(actual["start_ticks"]),
                "boot_id": actual["boot_id"] if "boot_id" in actual else actual["boot"],
            }
            or value.get("spec_sha256") != model_spec.sha256
            or value.get("plan_sha256") != model_spec.value["plan_sha256"]
            or value.get("run_id") != model_spec.value["run_id"]
            or value.get("model_digest") != model_spec.value["provider"]["model_digest"]
        ):
            return False
    return bool(
        isinstance(value, dict)
        and value.get("schema") == "levi.aeri.provider_ready.v1"
        and value.get("contract_id") == contract.contract_id
        and value.get("contract_version") == contract.contract_version
        and isinstance(value.get("capabilities"), list)
        and all(isinstance(x, str) for x in value["capabilities"])
        and set(capabilities).issubset(set(value["capabilities"]))
        and type(stamp) is int
        and 0 <= now - stamp <= 10_000_000_000
    )


@dataclass
class OnlineTicket:
    request_id: str
    submitted_ns: int
    request: dict
    submission: object
    proof: object = None
    cancelled: bool = False
    delivered: bool = False


@dataclass
class Ticket:
    request_id: str
    submitted_ns: int
    request: dict
    path: Path
    observed_ns: int
    frames: tuple[tuple[str, str, str], ...]
    payload: dict | None = None


def retained_hash(path):
    """Hash a bounded regular evidence file without following a final symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 16 * 1024 * 1024:
            return None
        digest, size = hashlib.sha256(), 0
        while data := os.read(fd, 64 * 1024):
            size += len(data)
            if size > 16 * 1024 * 1024:
                return None
            digest.update(data)
        return digest.hexdigest() if size == info.st_size else None
    finally:
        os.close(fd)


def _encode_jpeg(pixels):
    import cv2

    ok, data = cv2.imencode(".jpg", pixels)
    if not ok:
        raise ValueError("current camera frame encoding failed")
    return data.tobytes()


class FileProvider:
    def __init__(self, root, kind, robot, clock, contract, *, model_spec=None):
        self.root, self.kind = Path(root), kind
        self.robot, self.clock, self.contract = robot, clock, contract
        self.store = None
        self.model_spec = model_spec
        self.pending = {}
        # One JPEG per camera frame (view, seq): a frame shared by two
        # requests is encoded once (frame bus; no metadata: every time).
        self._jpeg = camera_meta.JpegCache(_encode_jpeg)

    def use_evidence(self, store):
        self.store = store

    def reachable(self):
        return (
            self.root.is_dir()
            and not self.root.is_symlink()
            and ready(
                self.root, self.contract, (self.kind,), model_spec=self.model_spec
            )
        )

    def submit(self, request):
        # Kept camera frames and the local observation time bind a provider's
        # reply to the exact scene it was asked to assess.
        if not self.reachable():
            raise ValueError("real provider is missing or stale")
        submitted_ns = self.clock.now()
        request = dict(request)
        if self.model_spec is not None:
            phase = (
                "scene"
                if self.kind == "scene"
                else "reset_final"
                if request["request_id"].endswith(":final")
                else "reset_active"
            )
            request.update(
                spec_sha256=self.model_spec.sha256,
                plan_sha256=self.model_spec.value["plan_sha256"],
                submitted_ns=submitted_ns,
                phase=phase,
                deadline_due_ns=submitted_ns
                + self.model_spec.value["deadlines_ns"][phase],
            )
        observed = self.robot.observe()
        if self.model_spec is not None and self.kind == "reset_judgement":
            request["observed_through_ns"] = observed["observed_ns"]
        frames = []
        meta = observed.get("image_meta")
        failure = None
        if self.store is None:
            failure = "current evidence store is missing"
        else:
            try:
                for view, pixels in observed["images_bgr"].items():
                    encoded = self._jpeg.get(view, pixels, (meta or {}).get(view))
                    saved = self.store.keep_frame(
                        view + ":" + key(request["request_id"])[:16], encoded
                    )
                    relative = Path(saved.get("file") or "")
                    if (
                        saved.get("skipped")
                        or not saved.get("file")
                        or relative.is_absolute()
                        or ".." in relative.parts
                        or saved.get("sha256") != hashlib.sha256(encoded).hexdigest()
                        or retained_hash(self.store.folder / relative)
                        != saved["sha256"]
                    ):
                        raise ValueError(
                            "current camera evidence was not durably retained"
                        )
                    frames.append(
                        {
                            "view": view,
                            **saved,
                            **camera_meta.frame_fields(meta, view),
                        }
                    )
            except (OSError, ValueError, KeyError, TypeError) as exc:
                failure = str(exc)
        if not frames and failure is None:
            failure = "current camera evidence is empty"
        base = self.root / self.kind
        for name in ("requests", "responses", "cancelled"):
            (base / name).mkdir(parents=True, exist_ok=True)
        name = key(request["request_id"])
        response = base / "responses" / (name + ".json")
        # Any prewritten response is unsolicited. Never consume it as fresh.
        if response.exists() or response.is_symlink():
            raise ValueError("unsolicited provider response")
        payload = {
            "schema": "levi.aeri.provider_request.v1",
            "kind": self.kind,
            "request": request,
            "clock_domain": self.clock.domain,
            "observed_ns": observed["observed_ns"],
            "frames": frames,
            "evidence_root": str(self.store.folder) if self.store is not None else None,
            "contract": None
            if self.contract is None
            else {
                "id": self.contract.contract_id,
                "version": self.contract.contract_version,
                "required": list(self.contract.required),
                "optional": list(self.contract.optional),
            },
        }
        write_durable(
            base / "requests" / (name + ".json"),
            (json.dumps(payload, indent=2) + "\n").encode(),
        )
        ticket = Ticket(
            request["request_id"],
            self.clock.now(),
            dict(request),
            response,
            observed["observed_ns"],
            tuple((f["ref"], f["sha256"], f["file"]) for f in frames),
            payload=payload,
        )
        if failure is not None:
            return self.unavailable(ticket, failure)
        self.pending[ticket.request_id] = ticket
        return ticket

    def unavailable(self, ticket, reason):
        return json.dumps(
            {
                "schema": aeri.SCHEMAS[
                    "scene" if self.kind == "scene" else "judgement"
                ],
                "minor": aeri.MINOR,
                "run_id": ticket.request["run_id"],
                "emitted_wall_ns": time.time_ns(),
                "kind": "unavailable",
                "request_id": ticket.request_id,
                "retryable": False,
                "code": "contract_violation",
                "detail": reason[:300],
                "produced_ns": self.clock.now(),
                "clock_domain": self.clock.domain,
            }
        ).encode()

    def current_response(self, ticket, data):
        contract = "scene" if self.kind == "scene" else "judgement"
        try:
            message = aeri.parse(
                data, contract, specs={**aeri.BUILTIN_SPECS, **self.contract.spec()}
            )
            if message.kind == "unavailable":
                return data
            if self.model_spec is not None:
                from .provider_model import validate_result

                validate_result(
                    self.model_spec,
                    ticket.payload,
                    json.loads(data),
                    now_ns=self.clock.now(),
                )
            if self.kind == "reset_judgement":
                if (message.spec_id, message.spec_version) != self.contract.key:
                    raise ValueError(
                        "reset judgement does not match the initial-state contract"
                    )
                required = set(self.contract.required)
                optional = set(self.contract.optional)
                predicates = {p.name: p for p in message.predicate_results}
                if set(predicates) != required | optional:
                    raise ValueError(
                        "reset judgement must report every contract predicate"
                    )
                if any(
                    p.required != (name in required) for name, p in predicates.items()
                ):
                    raise ValueError(
                        "reset judgement changed required/optional predicate semantics"
                    )
            observed = (
                message.observed_ns
                if contract == "scene"
                else message.observed_through_ns
            )
            if observed != ticket.observed_ns:
                raise ValueError(
                    "provider observation does not match this captured scene"
                )
            retained = {ref: digest for ref, digest, _ in ticket.frames}
            if not retained or any(
                retained_hash(self.store.folder / file) != digest
                for _, digest, file in ticket.frames
            ):
                raise ValueError("current retained evidence is missing or changed")
            cited = list(getattr(message, "evidence_refs", []))
            for predicate in message.predicate_results:
                if (
                    predicate.required
                    and predicate.value is not None
                    and not predicate.evidence_refs
                ):
                    raise ValueError("a required predicate has no current evidence")
                cited.extend(predicate.evidence_refs)
            if not cited or any(
                ref.kind != "frame" or retained.get(ref.ref) != ref.sha256
                for ref in cited
            ):
                raise ValueError(
                    "provider evidence is not one of this request's retained frames"
                )
            return data
        except (aeri.AeriError, ValueError, TypeError) as exc:
            return self.unavailable(ticket, str(exc))

    def collect(self, ticket, *, timeout_ns):
        deadline = self.clock.now() + timeout_ns
        while ticket.request_id in self.pending:
            data = read_answer(ticket.path)
            if data is not None:
                del self.pending[ticket.request_id]
                return self.current_response(ticket, data)
            remaining = deadline - self.clock.now()
            if remaining <= 0:
                return None
            self.clock.advance(min(remaining, 100_000_000))
        return None

    def cancel(self, ticket, reason):
        self.pending.pop(ticket.request_id, None)
        target = (
            self.root / self.kind / "cancelled" / (key(ticket.request_id) + ".json")
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        write_durable(
            target,
            json.dumps({"request_id": ticket.request_id, "reason": reason}).encode(),
        )


class FileEvents:
    def __init__(self, root, run_id, clock):
        self.root, self.run_id, self.clock = Path(root), run_id, clock
        self.streams = {}

    def open(self, episode_id, role):
        sid = key(episode_id)
        folder = self.root / "events" / sid
        folder.mkdir(parents=True, exist_ok=True)
        write_durable(
            folder / "request.json",
            json.dumps(
                {
                    "run_id": self.run_id,
                    "episode_id": episode_id,
                    "role": role,
                    "clock_domain": self.clock.domain,
                }
            ).encode(),
        )
        self.streams[sid] = (folder, set())
        return sid

    def poll(self, stream_id, *, through_step, max_events):
        folder, seen = self.streams[stream_id]
        out = []
        unseen = (path for path in folder.glob("event-*.json") if path.name not in seen)
        import heapq

        for path in heapq.nsmallest(1000, unseen):
            data = read_answer(path)
            if data is None:
                continue
            seen.add(path.name)
            out.append(data)
            if len(out) >= max_events:
                break
        return out

    def close(self, stream_id):
        self.streams.pop(stream_id, None)


class OnlineVerifier:
    """Forward C5 only. A reset uses its own provider and Initial State Contract."""

    def __init__(
        self, module, judge, clock, media, job, reset_provider=None, *, robot=None
    ):
        self.module, self.judge, self.clock = module, judge, clock
        self.media, self.job, self.reset_provider = media, job, reset_provider
        self.robot = robot

    def submit(self, request):
        if request["episode_role"] == "reset":
            if self.reset_provider is None:
                return self._response(
                    request,
                    {
                        "schema": "levi.online.judge.result.v1",
                        "status": "unavailable",
                        "reason": "provider_error: reset judge missing",
                    },
                )
            return self.reset_provider.submit(request)
        if request.get("event_ids"):
            from . import event_window

            try:
                ledger = self.media.event_ledger(request["episode_id"])
                if ledger is None:
                    raise ValueError("event source ledger unavailable")
                proof, spec, images = ledger.freeze(request, self.module, self.judge)
                episode = {
                    "run_id": request["run_id"],
                    "steps": None,
                    "fps": 1e9 / self.job["config"].control_period_ns,
                }
                submission = event_window.submit(
                    self.module,
                    self.judge,
                    spec,
                    images,
                    self.job["texts"]["forward"],
                    episode,
                )
                import copy

                return OnlineTicket(
                    request["request_id"],
                    self.clock.now(),
                    copy.deepcopy(request),
                    submission,
                    proof,
                )
            except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
                return self._response(
                    request,
                    {
                        "schema": "levi.online.judge.result.v1",
                        "status": "error",
                        "reason": "invalid_request: event window unavailable: "
                        + str(exc),
                    },
                )
        buffer = self.media.buffers.get(request["episode_id"])
        if buffer is None or not len(buffer):
            return self._response(
                request,
                {
                    "schema": "levi.online.judge.result.v1",
                    "status": "unavailable",
                    "reason": "provider_error: no recorded frames",
                },
            )
        if self.robot is not None:
            observed = self.robot.observe()
            buffer.add(
                request["observed_through_step"] + 1,
                time.time(),
                observed["raw"]["image"],
            )
        episode = {
            "run_id": request["run_id"],
            "steps": request["observed_through_step"] + 1,
            "fps": 1e9 / self.job["config"].control_period_ns,
        }
        submission = self.judge.submit(
            buffer.snapshot(),
            self.job["texts"]["forward"],
            episode,
            start_frame=buffer.start_frame(),
        )
        return OnlineTicket(
            request["request_id"], self.clock.now(), dict(request), submission
        )

    def _response(self, request, body, proof=None):
        value = legacy_live.from_c5(
            body,
            run_id=request["run_id"],
            request_id=request["request_id"],
            episode_id=request["episode_id"],
            episode_role=request["episode_role"],
            target=request["target"],
            observed_from_step=proof.observed_from_step if proof is not None else 0,
            observed_through_step=proof.observed_through_step
            if proof is not None
            else request["observed_through_step"],
            observed_through_ns=proof.observed_through_ns
            if proof is not None
            else request["observed_through_ns"],
            event_ids=proof.event_ids if proof is not None else (),
            produced_ns=self.clock.now(),
            clock_domain=self.clock.domain,
        )
        return value.model_dump_json().encode()

    def collect(self, ticket, *, timeout_ns):
        if isinstance(ticket, Ticket):
            return self.reset_provider.collect(ticket, timeout_ns=timeout_ns)
        if ticket.cancelled or ticket.delivered:
            return None
        pending = ticket.submission.pending
        if not pending.wait(timeout_ns / 1e9):
            return None
        if ticket.cancelled:
            return None
        result = pending.take()
        ticket.delivered = True
        if (
            ticket.proof is not None
            and self.clock.domain != ticket.proof.picks[0].source.clock_domain
        ):
            return self._response(
                ticket.request,
                {
                    "schema": "levi.online.judge.result.v1",
                    "status": "error",
                    "reason": "invalid_request: event proof clock domain changed",
                },
            )
        return self._response(
            ticket.request,
            result
            or {
                "schema": "levi.online.judge.result.v1",
                "status": "unavailable",
                "reason": "provider_error: empty reply",
            },
            ticket.proof,
        )

    def cancel(self, ticket, reason):
        if isinstance(ticket, Ticket):
            self.reset_provider.cancel(ticket, reason)
        else:
            ticket.cancelled = True
            ticket.submission.pending.cancel()
