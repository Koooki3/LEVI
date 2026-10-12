"""Read-only observation cache and an opt-in real signal EventStream.

The wrapper delegates each existing observe call exactly once. Poll consumes
that observation, never contacts hardware. Frames are encoded from that same
cached observation and retained through an explicit evidence-store callback.
No model, service or real adapter is imported or started by this module.
"""

import hashlib
import math
import os
import stat
import uuid
from collections import deque
from dataclasses import dataclass
from numbers import Real
from pathlib import Path

import numpy as np
from pydantic import TypeAdapter

from levi.domain import aeri
from levi.events.stream import CausalSignals, Sample, StreamConfig

MAX_FRAME_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class Binding:
    feature: str
    index: int
    path: tuple
    kind: str

    @property
    def key(self):
        return self.feature, self.index


def _value(observation, path):
    value = observation
    for part in path:
        value = value[part]
    return value


def jpeg(image):
    import cv2

    ok, encoded = cv2.imencode(".jpg", image)
    if not ok:
        raise ValueError("JPEG encoding failed")
    return encoded.tobytes()


class CachedObserver:
    """Drop-in robot wrapper; only observe caches, other calls delegate.

    One bounded observation is retained, not a history of full images. Begin
    and end invalidate it, fencing observations taken before a new episode.
    """

    def __init__(self, robot, clock, *, step_of):
        if not callable(step_of):
            raise TypeError("explicit read-only control-step source required")
        self.robot, self.clock, self.step_of = robot, clock, step_of
        self.identity = None
        self.latest = None
        self.serial = 0

    def __getattr__(self, name):
        return getattr(self.robot, name)

    def begin(self, identity):
        self.identity, self.latest = identity, None

    def end(self):
        self.identity, self.latest = None, None

    def observe(self):
        identity = self.identity
        started = self.clock.now()
        step, domain = self.step_of(), self.clock.domain
        value = self.robot.observe()
        ended = self.clock.now()
        if (
            identity is not None
            and self.identity == identity
            and self.step_of() == step
            and self.clock.domain == domain
        ):
            images = value.get("images_bgr") or {}
            if not isinstance(images, dict) or len(images) > 2:
                self.latest = None
                return value
            if (
                any(
                    not isinstance(k, str)
                    or not 1 <= len(k) <= 80
                    or not isinstance(image, np.ndarray)
                    or image.dtype != np.uint8
                    or image.ndim != 3
                    or image.shape[2] != 3
                    for k, image in images.items()
                )
                or sum(image.nbytes for image in images.values()) > MAX_FRAME_BYTES
            ):
                self.latest = None
                return value
            pose = value.get("pose")
            pose = (
                [
                    float(x)
                    if isinstance(x, Real) and not isinstance(x, (bool, np.bool_))
                    else None
                    for x in pose[:7]
                ]
                if isinstance(pose, (list, tuple, np.ndarray))
                else []
            )
            state = value.get("robot_state")
            state = (
                {"gripper_position": state.get("gripper_position")}
                if isinstance(state, dict)
                else {}
            )
            if type(state.get("gripper_position")) not in (int, float):
                state = {}
            proof = value.get("gripper_measurement")
            fields = {
                "route",
                "field",
                "generation",
                "read_from_ns",
                "read_through_ns",
                "clock_domain",
                "value",
            }
            if (
                not isinstance(proof, dict)
                or set(proof) != fields
                or any(type(v) not in (str, int, float) for v in proof.values())
            ):
                proof = None
            else:
                proof = dict(proof)
            selected = {
                "observed_ns": value.get("observed_ns"),
                "pose": pose,
                "robot_state": state,
                "gripper_measurement": proof,
                "state_fresh": value.get("state_fresh"),
                "images_bgr": {k: image.copy() for k, image in images.items()},
            }
            self.serial += 1
            self.latest = identity, self.serial, started, ended, step, domain, selected
        else:
            self.latest = None
        return value


class SignalEvents:
    """EventStream over the current observation, with epoch and evidence fences.

    The caller explicitly freezes profile/bindings/config and supplies the
    same clock as the robot and an owned evidence root and keep_frame callback.
    Only one episode is open. No default runtime wiring is changed.
    """

    def __init__(
        self,
        observer,
        profile,
        bindings,
        *,
        run_id,
        clock,
        evidence_root,
        keep_frame,
        config=None,
        encode=jpeg,
        actor_id="arm_0",
    ):
        TypeAdapter(aeri.Id).validate_python(run_id)
        TypeAdapter(aeri.ClockDomain).validate_python(clock.domain)
        self.observer, self.clock, self.run_id = observer, clock, run_id
        if observer.clock is not clock:
            raise ValueError("observer and event stream must share the clock object")
        self.config = config or StreamConfig()
        self._clock_domain = clock.domain
        prototype = CausalSignals(profile, config=self.config)
        self._profile = prototype.profile
        bindings = tuple(bindings)
        channels = {c.key: c for c in self._profile.channels if c.role != "ignore"}
        if len(bindings) != len(channels) or {b.key for b in bindings} != set(channels):
            raise ValueError("one explicit binding per signal channel required")
        for binding in bindings:
            channel = channels[binding.key]
            if channel.actor_id != actor_id:
                raise ValueError("signal actor differs from the observed robot")
            if channel.role == "position" and tuple(binding.path) != (
                "pose",
                "xyz".index(channel.axis),
            ):
                raise ValueError(
                    "measured position must bind the SDK observed xyz pose"
                )
            if (
                not binding.path
                or len(binding.path) > 4
                or channel.kind != binding.kind
            ):
                raise ValueError("binding path/kind differs from frozen profile")
            if channel.role == "gripper":
                path = tuple(binding.path)
                expected = (
                    ("robot_state", "gripper_position")
                    if channel.kind == "measured"
                    else ("pose", 6)
                )
                if (
                    path != expected
                    or channel.units not in ("1", "normalized", "binary")
                    or channel.valid_range != (0, 1)
                ):
                    raise ValueError(
                        "FR3 gripper binding needs explicit normalized measured or commanded source"
                    )
        self.bindings = bindings
        self.evidence_root = Path(evidence_root).absolute()
        self.keep_frame, self.encode = keep_frame, encode
        self.stream = None
        self.pending = deque(maxlen=128)
        self.status = {"state": "closed", "reasons": [], "dropped": 0}
        self._opened = set()
        self._last_serial = 0
        self._proof_generation = None
        self._emitted = deque(maxlen=128)

    def open(self, episode_id, role):
        if self.stream is not None:
            raise ValueError("an event episode is already open")
        TypeAdapter(aeri.EpisodeId).validate_python(episode_id)
        if role not in ("forward", "reset"):
            raise ValueError("invalid event episode role")
        run, actual_role, number = aeri.episode_parts(episode_id)
        if run != self.run_id or actual_role != role:
            raise ValueError("event episode identity mismatch")
        if episode_id in self._opened or len(self._opened) >= 1024:
            raise ValueError("episode replay or event stream episode limit")
        self._opened.add(episode_id)
        sid = uuid.uuid4().hex
        self.stream = (
            sid,
            episode_id,
            role,
            CausalSignals(self._profile, episode_index=number, config=self.config),
        )
        self.pending.clear()
        self._emitted.clear()
        self._last_serial, self._proof_generation = 0, None
        self._last_observed_ns = None
        self._seq, self._step = 0, -1
        self.observer.begin(sid)
        self.status = {"state": "waiting", "reasons": [], "dropped": 0}
        return sid

    def close(self, stream_id):
        if self.stream is None or self.stream[0] != stream_id:
            raise ValueError("stale event stream identity")
        self.observer.end()
        self.stream = None
        self.pending.clear()
        self.status["state"] = "closed"

    def _measured(self, observation, started, ended):
        proof = observation.get("gripper_measurement")
        expected = {
            "route",
            "field",
            "generation",
            "read_from_ns",
            "read_through_ns",
            "clock_domain",
            "value",
        }
        if not isinstance(proof, dict) or set(proof) != expected:
            return False
        generation = proof["generation"]
        value = proof["value"]
        valid = (
            proof["route"] == "getstate"
            and proof["field"] == "gripper_pos"
            and type(generation) is int
            and generation > 0
            and (self._proof_generation is None or generation > self._proof_generation)
            and type(proof["read_from_ns"]) is int
            and type(proof["read_through_ns"]) is int
            and started
            <= proof["read_from_ns"]
            <= proof["read_through_ns"]
            <= observation["observed_ns"]
            <= ended
            and proof["clock_domain"] == self.clock.domain
            and type(value) in (int, float)
            and 0 <= value <= 1
            and math.isfinite(value)
            and type(observation.get("robot_state", {}).get("gripper_position"))
            in (int, float)
            and observation["robot_state"]["gripper_position"] == value
        )
        if valid:
            self._proof_generation = generation
        return valid

    def _retained(self, ref, image, step):
        data = self.encode(image)
        if (
            not isinstance(data, bytes)
            or not 0 < len(data) <= MAX_FRAME_BYTES
            or not data.startswith(b"\xff\xd8")
            or not data.endswith(b"\xff\xd9")
        ):
            raise ValueError("current JPEG bytes unavailable")
        digest = hashlib.sha256(data).hexdigest()
        saved = self.keep_frame(ref, data)
        file = saved.get("file") if isinstance(saved, dict) else None
        if (
            not isinstance(saved, dict)
            or saved.get("skipped")
            or saved.get("ref") != ref
            or saved.get("sha256") != digest
            or not isinstance(file, str)
        ):
            raise ValueError("current retained frame identity/hash unavailable")
        relative = Path(file)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("unsafe retained frame path")
        self._verify_frame(relative, digest, len(data))
        return {"kind": "frame", "ref": ref, "sha256": digest, "step": step}, (
            relative,
            digest,
            len(data),
        )

    def _verify_frame(self, relative, digest, expected_size):
        # Every directory component is opened without following symlinks.
        fd = os.open(self.evidence_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in relative.parts[:-1]:
                next_fd = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
                os.close(fd)
                fd = next_fd
            leaf = os.open(
                relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
            )
            try:
                info = os.fstat(leaf)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or not 0 < info.st_size <= MAX_FRAME_BYTES
                ):
                    raise ValueError("retained JPEG is not bounded regular evidence")
                actual = hashlib.sha256()
                size = 0
                while block := os.read(leaf, 65536):
                    size += len(block)
                    if size > MAX_FRAME_BYTES:
                        raise ValueError("retained evidence grew past byte limit")
                    actual.update(block)
                if size != expected_size or actual.hexdigest() != digest:
                    raise ValueError("retained current JPEG hash mismatch")
            finally:
                os.close(leaf)
        finally:
            os.close(fd)

    def poll(self, stream_id, *, through_step, max_events):
        if self.stream is None or self.stream[0] != stream_id:
            raise ValueError("stale event stream identity")
        if (
            type(through_step) is not int
            or through_step < self._step
            or type(max_events) is not int
            or not 1 <= max_events <= 128
        ):
            raise ValueError("invalid current step/event budget")
        if self.clock.domain != self._clock_domain:
            raise ValueError("event clock changed")
        sid, episode_id, role, kernel = self.stream
        current = self.observer.latest
        advance = False
        if current and current[1] != self._last_serial:
            identity, serial, started, ended, step, domain, obs = current
            if (
                identity != sid
                or domain != self._clock_domain
                or self.clock.domain != self._clock_domain
            ):
                raise ValueError("observation epoch/clock mismatch")
            if type(serial) is not int or serial <= self._last_serial:
                raise ValueError("observation serial must advance")
            if type(step) is not int or step != through_step:
                raise ValueError("observation is not of the current control step")
            now = self.clock.now()
            observed_ns = obs.get("observed_ns")
            if (
                type(observed_ns) is not int
                or not 0 <= started <= observed_ns <= ended <= now
                or now - observed_ns > self.config.max_age_ns
                or (
                    self._last_observed_ns is not None
                    and observed_ns <= self._last_observed_ns
                )
            ):
                raise ValueError("timestamp is not from this observation call")
            self._last_serial = serial
            self._last_observed_ns = observed_ns
            # Resume refreshes the observation before the next action. Consume
            # its serial once, but only advancing control steps feed the detector.
            advance = through_step > self._step
        if advance:
            values, unknown = {}, []
            measured = None
            for binding in self.bindings:
                if binding.kind == "measured" and binding.path == (
                    "robot_state",
                    "gripper_position",
                ):
                    if measured is None:
                        measured = self._measured(obs, started, ended)
                    if not measured:
                        unknown.append("measured_gripper_unproven")
                        continue
                try:
                    values[binding.key] = _value(obs, binding.path)
                except (KeyError, IndexError, TypeError):
                    unknown.append(f"missing_binding:{binding.feature}:{binding.index}")
            if obs.get("state_fresh") is not True:
                values = {}
                unknown.append("state_unavailable")
            result = kernel.feed(
                Sample(through_step, obs["observed_ns"], values), now_ns=now
            )
            self._step = through_step
            unknown.extend(result.unknown)
            frames, proofs = [], []
            if result.candidates:
                try:
                    images = obs["images_bgr"]
                    if not images:
                        raise ValueError("current frames missing")
                    for view, image in sorted(images.items()):
                        ref = f"{view}:{sid}:{through_step}"
                        wire_ref, proof = self._retained(ref, image, through_step)
                        frames.append(wire_ref)
                        proofs.append(proof)
                    if self.clock.now() - obs["observed_ns"] > self.config.max_age_ns:
                        raise ValueError(
                            "current evidence expired while retaining frames"
                        )
                except (OSError, ValueError, TypeError) as exc:
                    unknown.append(f"evidence_unavailable:{type(exc).__name__}")
                    frames = []
            for candidate, window in (
                zip(result.candidates, result.window_steps) if frames else ()
            ):
                if len(self.pending) == self.pending.maxlen:
                    self.status["dropped"] += 1
                channels = candidate.sources
                event_type = candidate.event_type
                if (
                    event_type.startswith("gripper_")
                    and channels[0].kind == "commanded"
                ):
                    event_type = event_type.replace("gripper_", "gripper_command_", 1)
                event = aeri.EventProposal(
                    schema=aeri.SCHEMAS["event"],
                    minor=aeri.MINOR,
                    run_id=self.run_id,
                    emitted_wall_ns=0,
                    event_id=f"{sid}:{candidate.id}",
                    episode_id=episode_id,
                    episode_role=role,
                    seq=self._seq,
                    event_type=event_type,
                    status="candidate",
                    priority="goal_candidate",
                    actor_id=candidate.actor_id,
                    step=through_step,
                    window_steps=list(window),
                    observed_ns=obs["observed_ns"],
                    clock_domain=self.clock.domain,
                    signal_refs=[
                        {
                            "feature": x.feature,
                            "dimension": x.index,
                            "kind": x.kind,
                            "units": x.units,
                        }
                        for x in channels
                    ],
                    evidence_refs=frames,
                    salience=candidate.salience,
                    detector={
                        "name": "causal-signals",
                        "version": kernel.profile_sha256,
                    },
                )
                self._seq += 1
                self.pending.append((event, tuple(proofs)))
                self._emitted.append(event)
            self.status.update(
                state="degraded" if unknown else "ready",
                reasons=unknown[:32],
                dropped_history=result.dropped_history,
            )
        out, checked = [], set()
        while self.pending and len(out) < max_events:
            event, proofs = self.pending.popleft()
            if (
                event.step > through_step
                or event.clock_domain != self._clock_domain
                or self.clock.domain != self._clock_domain
                or event.observed_ns > self.clock.now()
                or self.clock.now() - event.observed_ns > self.config.max_age_ns
            ):
                self.status["dropped"] += 1
                continue
            try:
                for proof in proofs:
                    if proof not in checked:
                        self._verify_frame(*proof)
                        checked.add(proof)
            except (OSError, ValueError):
                self.status["dropped"] += 1
                self.status.update(
                    state="degraded", reasons=["retained_evidence_changed"]
                )
                continue
            now = self.clock.now()
            if (
                self.clock.domain != self._clock_domain
                or not event.observed_ns
                <= now
                <= event.observed_ns + self.config.max_age_ns
            ):
                self.status["dropped"] += 1
                continue
            out.append(event.model_dump_json(by_alias=True).encode())
        return out

    def retract(self, stream_id, event_id):
        """Withdraw one retained current-epoch candidate; no judgement or motion.

        Explicit invalidation of a signal/evidence source may call this. The
        bounded id history cannot resurrect candidates from previous episodes.
        """
        if self.stream is None or self.stream[0] != stream_id:
            raise ValueError("stale event stream identity")
        original = next((e for e in self._emitted if e.event_id == event_id), None)
        if original is None or original.status != "candidate":
            raise ValueError("candidate not in bounded current-epoch history")
        if self.clock.domain != self._clock_domain:
            raise ValueError("event clock changed")
        now = self.clock.now()
        if now < original.observed_ns or now < self.stream[3]._last.observed_ns:
            raise ValueError("event clock moved backwards")
        event = original.model_copy(
            update={
                "event_id": f"{stream_id}:retract:{self._seq}",
                "retracts": event_id,
                "status": "retracted",
                "seq": self._seq,
                "priority": "routine",
                "step": self._step,
                "window_steps": [self._step, self._step],
                "observed_ns": now,
                "evidence_refs": [],
            }
        )
        # Validate a constructed update too (model_copy alone bypasses validation).
        event = aeri.EventProposal.model_validate(event.model_dump(by_alias=True))
        self._emitted.remove(original)
        self._seq += 1
        if len(self.pending) == self.pending.maxlen:
            self.status["dropped"] += 1
        self.pending.append((event, ()))
