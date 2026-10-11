"""Bounded pre-action sources and the actual fixed-offset C5 event request.

Recording rows are optional commit facts, never control-step provenance. No I/O
with a robot or model occurs here; transport uses the already loaded C5 client.
"""

import base64
import copy
import hashlib
import json
from collections import deque
from dataclasses import dataclass, replace
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from levi.live.online import MAX_IMAGES, MAX_SIDE

from .adapters.signal_events import MAX_FRAME_BYTES, CachedObserver


class EventObserver(CachedObserver):
    """Keep the original current observation identity alongside the stable cache."""

    original = None

    def observe(self):
        value = super().observe()
        self.original = value if self.latest is not None else None
        return value

    def begin(self, identity):
        super().begin(identity)
        self.original = None

    def end(self):
        super().end()
        self.original = None


@dataclass(frozen=True)
class Source:
    epoch: str
    serial: int
    run_id: str
    episode_id: str
    role: str
    clock_domain: str
    control_step: int
    observed_ns: int
    phase: str
    recording_row: int | None
    images: tuple[tuple[str, bytes, str], ...]


@dataclass(frozen=True)
class Pick:
    role: str
    offset_s: float
    source: Source
    jpeg_sha256: str


@dataclass(frozen=True)
class Proof:
    event_ids: tuple[str, ...]
    picks: tuple[Pick, ...]
    start_picks: tuple[Pick, ...]
    observed_from_step: int
    observed_through_step: int
    observed_through_ns: int


class Ledger:
    """One rollout, capped by frozen max_history and 16 MiB total retained JPEG bytes."""

    def __init__(self, observer, context, config, run_id, cv2):
        self.observer, self.context, self.config = observer, context, config
        self.run_id, self.cv2 = run_id, cv2
        self.domain = observer.clock.domain
        self.rows = deque(maxlen=config.max_history)
        self.first = None
        self.identity = None

    def stage(self, folder, observation):
        ctx, current = self.context(), self.observer.latest
        if ctx is None or current is None or self.observer.original is not observation:
            raise ValueError("stage does not match the current cached observation")
        epoch, serial, started, ended, step, domain, cached = current
        identity = epoch, self.run_id, ctx.episode_id, ctx.role, Path(folder)
        now = self.observer.clock.now()
        stamp = cached.get("observed_ns")
        if (
            ctx.rollout is None
            or Path(ctx.rollout.path) != Path(folder)
            or epoch != ctx.stream
            or epoch != self.observer.identity
            or domain != self.domain
            or self.observer.clock.domain != self.domain
            or type(step) is not int
            or step < 0
            or step != ctx.last_step
            or type(stamp) is not int
            or stamp != observation.get("observed_ns")
            or not 0 <= started <= stamp <= ended <= now
            or now - stamp > self.config.max_age_ns
            or (self.identity is not None and self.identity != identity)
        ):
            raise ValueError("stage source episode/epoch/clock/step/time mismatch")
        if self.rows and (
            serial <= self.rows[-1].serial
            or step <= self.rows[-1].control_step
            or stamp <= self.rows[-1].observed_ns
        ):
            raise ValueError("duplicate or reversed staged source")
        images = cached.get("images_bgr")
        if not isinstance(images, dict) or set(images) != {"side", "wrist"}:
            raise ValueError("staged source needs both declared camera views")
        encoded = []
        for role, image in sorted(images.items()):
            actual = observation.get("images_bgr", {}).get(role)
            if actual is None or not np.array_equal(actual, image):
                raise ValueError("stage image differs from cached source")
            ok, data = self.cv2.imencode(".jpg", image)
            data = data.tobytes() if ok else b""
            if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
                raise ValueError("staged JPEG unavailable")
            encoded.append((role, data, hashlib.sha256(data).hexdigest()))
        if sum(len(data) for _, data, _ in encoded) > MAX_FRAME_BYTES:
            raise ValueError("staged source exceeds existing frame byte budget")
        source = Source(
            epoch,
            serial,
            self.run_id,
            ctx.episode_id,
            ctx.role,
            domain,
            step,
            stamp,
            "pre_action",
            None,
            tuple(encoded),
        )
        self.identity = identity
        self.first = self.first or source
        # Count the pinned start only once, including when it leaves the deque.
        while (
            self.rows
            and self.retained_bytes + sum(len(data) for _, data, _ in source.images)
            > MAX_FRAME_BYTES
        ):
            self.rows.popleft()
        if (
            self.retained_bytes
            + (
                0
                if self.first is source
                else sum(len(data) for _, data, _ in source.images)
            )
            > MAX_FRAME_BYTES
        ):
            raise ValueError("event ledger total JPEG byte budget exhausted")
        self.rows.append(source)

    @property
    def retained_bytes(self):
        sources = {s.serial: s for s in self.rows}
        if self.first is not None:
            sources[self.first.serial] = self.first
        return sum(len(data) for s in sources.values() for _, data, _ in s.images)

    def commit(self, recording_row):
        if self.rows:
            source = replace(self.rows[-1], recording_row=recording_row)
            self.rows[-1] = source
            if self.first.serial == source.serial:
                self.first = source

    def freeze(self, request, module, judge):
        """Validate the exact picks, encode once, then freeze bytes and provenance."""
        current = self.observer.latest
        ctx = self.context()
        if not self.rows or current is None or ctx is None:
            raise ValueError("current event source unavailable")
        epoch, serial, _, _, step, domain, _ = current
        last = self.rows[-1]
        lo, hi = request.get("observed_from_step"), request.get("observed_through_step")
        event_ids = request.get("event_ids")
        now = self.observer.clock.now()
        if (
            not isinstance(event_ids, list)
            or not event_ids
            or any(not isinstance(value, str) or not value for value in event_ids)
            or len(set(event_ids)) != len(event_ids)
            or type(lo) is not int
            or type(hi) is not int
            or not 0 <= lo <= hi
            or (
                request.get("run_id"),
                request.get("episode_id"),
                request.get("episode_role"),
            )
            != (last.run_id, last.episode_id, last.role)
            or (epoch, serial, step, domain)
            != (last.epoch, last.serial, last.control_step, last.clock_domain)
            or (ctx.stream, ctx.episode_id, ctx.role, ctx.last_step)
            != (last.epoch, last.episode_id, last.role, last.control_step)
            or ctx.rollout is None
            or Path(ctx.rollout.path) != self.identity[-1]
            or current[-1].get("observed_ns") != last.observed_ns
            or self.observer.original is None
            or self.observer.original.get("observed_ns") != last.observed_ns
            or self.observer.identity != last.epoch
            or self.observer.clock.domain != self.domain
            or hi != last.control_step
            or request.get("observed_through_ns") != last.observed_ns
            or not 0 <= now - last.observed_ns <= self.config.max_age_ns
        ):
            raise ValueError("event request does not match staged current source")
        sources = tuple(self.rows)
        candidate = next((s for s in sources if s.control_step == lo), None)
        if candidate is None:
            raise ValueError("candidate source was never staged or has been evicted")
        window = [s for s in sources if lo <= s.control_step <= hi]
        if any(
            b.control_step != a.control_step + 1
            or b.observed_ns - a.observed_ns > self.config.max_gap_ns
            for a, b in pairwise(window)
        ):
            raise ValueError("event source window has a gap")
        spec = copy.deepcopy(judge.spec)
        if spec is None:
            raise ValueError("C5 sampling spec unavailable")
        if (
            not 0 < spec.max_side <= MAX_SIDE
            or sum(len(offsets) for _, offsets in spec.views) + len(spec.start_roles)
            > MAX_IMAGES
        ):
            raise ValueError("C5 spec exceeds existing image budget")
        frames, by_serial = [], {s.serial: s for s in (*sources, self.first)}
        for source in sources:
            frames.append(self._frame(source))
        picked = module.pick_frames(frames, spec)
        start = module.pick_start_frames(self._frame(self.first), spec)
        required = {role for role, _ in spec.views}
        if not required or any(role not in {"side", "wrist"} for role in required):
            raise ValueError("unsupported required C5 views")
        for role in required:
            used = [by_serial[p["step"]] for p in picked if p["role"] == role]
            if (
                len({s.serial for s in used}) < 2
                or min(s.control_step for s in used) > lo
                or min(s.observed_ns for s in used) > candidate.observed_ns
                or max(s.control_step for s in used) < hi
                or max(s.observed_ns for s in used) < last.observed_ns
            ):
                raise ValueError(
                    "actual fixed-offset picks do not cover the event window"
                )
        images, proofs = [], []
        for p in picked + start:
            source = by_serial[p["step"]]
            if p["image"] is None:
                raise ValueError("selected source view unavailable")
            pixels = self.cv2.imdecode(
                np.frombuffer(p["image"], dtype=np.uint8), self.cv2.IMREAD_COLOR
            )
            if pixels is None:
                raise ValueError("selected source JPEG cannot be decoded")
            data = module.encode_jpeg(pixels, spec.max_side)
            del pixels
            images.append(
                {
                    "role": p["role"],
                    "offset_s": p["offset_s"],
                    "step": p["step"],
                    "jpeg_b64": base64.b64encode(data).decode("ascii"),
                }
            )
            # Metadata/hashes only: a ticket must not pin an evicted full source.
            provenance = replace(
                source, images=tuple((r, b"", h) for r, _, h in source.images)
            )
            proofs.append(
                Pick(
                    p["role"],
                    p["offset_s"],
                    provenance,
                    hashlib.sha256(data).hexdigest(),
                )
            )
            if sum(len(image["jpeg_b64"]) for image in images) > MAX_FRAME_BYTES:
                raise ValueError("encoded event request exceeds total byte budget")
        end_proofs = tuple(proofs[: len(picked)])
        proof = Proof(
            tuple(event_ids),
            end_proofs,
            tuple(proofs[len(picked) :]),
            min(p.source.control_step for p in end_proofs),
            max(p.source.control_step for p in end_proofs),
            max(p.source.observed_ns for p in end_proofs),
        )
        return proof, spec, images

    def _frame(self, source):
        images = {}
        for role, data, digest in source.images:
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("staged source hash mismatch")
            images[{"side": "view1", "wrist": "hand"}[role]] = data
        # step is a private picker key. It is removed from the C5 HTTP request.
        return SimpleNamespace(
            step=source.serial, t=source.observed_ns / 1e9, images=images
        )


def submit(module, judge, spec, images, task, episode):
    """Reuse C5's filtering and transport, omitting optional executed-row step."""
    body = module.build_request(task, episode, images)
    for image in body["images"]:
        image.pop("step", None)
    if len(json.dumps(body).encode()) > MAX_FRAME_BYTES:
        raise ValueError("C5 event body exceeds total byte budget")
    timeout, opener = judge.timeout_s, judge.opener

    def work(pending):
        if pending.abandoned:
            return None
        return module.post_judge(spec.url, body, timeout, opener)

    return module.Submission(module.Pending(work), [])
