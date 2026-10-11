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


def ready(root, contract, capabilities, *, now=None):
    raw = read_answer(Path(root) / "READY.json")
    try:
        value = json.loads(raw) if raw else {}
    except (ValueError, UnicodeDecodeError):
        return False
    now = time.time_ns() if now is None else now
    stamp = value.get("updated_wall_ns") if isinstance(value, dict) else None
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
class Ticket:
    request_id: str
    submitted_ns: int
    request: dict
    path: Path
    observed_ns: int
    frames: tuple[tuple[str, str, str], ...]


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


class FileProvider:
    def __init__(self, root, kind, robot, clock, contract):
        self.root, self.kind = Path(root), kind
        self.robot, self.clock, self.contract = robot, clock, contract
        self.store = None
        self.pending = {}

    def use_evidence(self, store):
        self.store = store

    def reachable(self):
        return (
            self.root.is_dir()
            and not self.root.is_symlink()
            and ready(self.root, self.contract, (self.kind,))
        )

    def submit(self, request):
        # Kept camera frames and the local observation time bind a provider's
        # reply to the exact scene it was asked to assess.
        if not self.reachable():
            raise ValueError("real provider is missing or stale")
        observed = self.robot.observe()
        frames = []
        import cv2

        failure = None
        if self.store is None:
            failure = "current evidence store is missing"
        else:
            try:
                for view, pixels in observed["images_bgr"].items():
                    ok, data = cv2.imencode(".jpg", pixels)
                    if not ok:
                        raise ValueError("current camera frame encoding failed")
                    encoded = data.tobytes()
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
                    frames.append({"view": view, **saved})
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
        return type(
            "OnlineTicket",
            (),
            {
                "request_id": request["request_id"],
                "submitted_ns": self.clock.now(),
                "request": dict(request),
                "submission": submission,
            },
        )()

    def _response(self, request, body):
        value = legacy_live.from_c5(
            body,
            run_id=request["run_id"],
            request_id=request["request_id"],
            episode_id=request["episode_id"],
            episode_role=request["episode_role"],
            target=request["target"],
            observed_from_step=0,
            observed_through_step=request["observed_through_step"],
            observed_through_ns=request["observed_through_ns"],
            produced_ns=self.clock.now(),
            clock_domain=self.clock.domain,
        )
        return value.model_dump_json().encode()

    def collect(self, ticket, *, timeout_ns):
        if isinstance(ticket, Ticket):
            return self.reset_provider.collect(ticket, timeout_ns=timeout_ns)
        pending = ticket.submission.pending
        if not pending.wait(timeout_ns / 1e9):
            return None
        result = pending.take()
        return self._response(
            ticket.request,
            result
            or {
                "schema": "levi.online.judge.result.v1",
                "status": "unavailable",
                "reason": "provider_error: empty reply",
            },
        )

    def cancel(self, ticket, reason):
        if isinstance(ticket, Ticket):
            self.reset_provider.cancel(ticket, reason)
        else:
            ticket.submission.pending.cancel()
