"""Fresh-capture interface, verifier and fake cameras (software contract only).

Nothing here imports a camera SDK, opens a device or talks to a robot port.
``fr3_bridge.Robot.observe`` cannot prove an image was physically captured after
a grant: the openpi reader thread caches only the latest frame and ``read()``
carries no frame number, hardware timestamp or arrival time. A capture source
that does provide those is described by ``CapturedFrame``; the caller injects a
reviewed ``FreshCaptureBinding`` (serials, clock domains, timestamp semantics,
device-to-host alignment bounds, latency and interval limits, no defaults) and
``FreshCaptureVerifier`` refuses anything it cannot prove. Absence of a
timestamp, a declaration or a baseline is a refusal, never a guess.
"""

import hashlib
import json
import math
import re
from dataclasses import dataclass

__all__ = [
    "CameraFreshProof",
    "CapturedFrame",
    "DeviceClockAlignment",
    "FakeCamera",
    "FreshCaptureBinding",
    "FreshCaptureProof",
    "FreshCaptureUnproven",
    "FreshCaptureVerifier",
]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FreshCaptureUnproven(Exception):
    """Freshness of the capture could not be proven; the reason is the message."""


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _need_int(value, name: str, *, minimum: int | None = None) -> int:
    if not _is_int(value):
        raise FreshCaptureUnproven(f"{name} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise FreshCaptureUnproven(f"{name} must be >= {minimum}, got {value}")
    return value


def _need_str(value, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise FreshCaptureUnproven(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True)
class DeviceClockAlignment:
    """Declared mapping from device timestamps to the host monotonic clock.

    host_ns = device_ns + offset_ns, with uncertainty
    offset_uncertainty_ns + ceil(|device_ns - reference_device_ns| * ppm / 1e6).
    All values are measured and reviewed on site; none has a default.
    """

    offset_ns: int
    offset_uncertainty_ns: int
    drift_ppm_bound: float
    reference_device_ns: int

    def __post_init__(self):
        _need_int(self.offset_ns, "alignment offset_ns")
        _need_int(
            self.offset_uncertainty_ns, "alignment offset_uncertainty_ns", minimum=0
        )
        _need_int(self.reference_device_ns, "alignment reference_device_ns")
        ppm = self.drift_ppm_bound
        if (
            isinstance(ppm, bool)
            or not isinstance(ppm, (int, float))
            or not math.isfinite(ppm)
            or ppm < 0
        ):
            raise FreshCaptureUnproven(
                "alignment drift_ppm_bound must be a finite number >= 0"
            )

    def uncertainty_ns(self, device_ns: int) -> int:
        drift = math.ceil(
            abs(device_ns - self.reference_device_ns) * self.drift_ppm_bound / 1e6
        )
        return self.offset_uncertainty_ns + drift

    def capture_lower_ns(self, device_ns: int) -> int:
        """Earliest host time the frame can have been captured."""
        return device_ns + self.offset_ns - self.uncertainty_ns(device_ns)


@dataclass(frozen=True)
class FreshCaptureBinding:
    """Caller-injected, human-reviewed description of the capture source."""

    camera_serials: frozenset
    host_clock_domain: str
    timestamp_domain: str
    timestamp_semantics: str
    max_latency_ns: int
    max_frame_interval_ns: int
    min_frames_per_camera: int
    alignment: DeviceClockAlignment

    def __post_init__(self):
        serials = self.camera_serials
        if not isinstance(serials, frozenset) or not serials:
            raise FreshCaptureUnproven("camera_serials must be a non-empty frozenset")
        for s in serials:
            _need_str(s, "camera serial")
        _need_str(self.host_clock_domain, "host_clock_domain")
        _need_str(self.timestamp_domain, "timestamp_domain")
        _need_str(self.timestamp_semantics, "timestamp_semantics")
        if self.timestamp_semantics != "start_of_exposure":
            raise FreshCaptureUnproven(
                "timestamp_semantics must be start_of_exposure; exposure-end, "
                "readout and arrival timestamps cannot prove capture freshness"
            )
        _need_int(self.max_latency_ns, "max_latency_ns", minimum=1)
        _need_int(self.max_frame_interval_ns, "max_frame_interval_ns", minimum=1)
        _need_int(self.min_frames_per_camera, "min_frames_per_camera", minimum=1)
        if not isinstance(self.alignment, DeviceClockAlignment):
            raise FreshCaptureUnproven("device clock alignment is not declared")

    def sha256(self) -> str:
        a = self.alignment
        doc = {
            "camera_serials": sorted(self.camera_serials),
            "host_clock_domain": self.host_clock_domain,
            "timestamp_domain": self.timestamp_domain,
            "timestamp_semantics": self.timestamp_semantics,
            "max_latency_ns": self.max_latency_ns,
            "max_frame_interval_ns": self.max_frame_interval_ns,
            "min_frames_per_camera": self.min_frames_per_camera,
            "alignment": [
                a.offset_ns,
                a.offset_uncertainty_ns,
                float(a.drift_ppm_bound),
                a.reference_device_ns,
            ],
        }
        raw = json.dumps(doc, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class CapturedFrame:
    """One frame as delivered by a capture source that exposes frame metadata.

    device_timestamp_ns is integer nanoseconds in timestamp_domain; sources
    reporting milliseconds convert before constructing. None means the source
    gave no timestamp, which the verifier refuses. image_bytes is optional; when
    present it must hash to image_sha256.
    """

    camera_id: str
    frame_number: int
    device_timestamp_ns: int | None
    timestamp_domain: str | None
    host_clock_domain: str
    host_arrival_ns: int
    image_sha256: str
    image_bytes: bytes | None = None

    @classmethod
    def from_image_bytes(cls, *, image_bytes: bytes, **fields) -> "CapturedFrame":
        return cls(
            image_sha256=hashlib.sha256(image_bytes).hexdigest(),
            image_bytes=image_bytes,
            **fields,
        )


@dataclass(frozen=True)
class CameraFreshProof:
    camera_id: str
    timestamp_domain: str
    frame_numbers: tuple
    device_timestamps_ns: tuple
    arrival_ns: tuple
    image_sha256s: tuple
    earliest_capture_lower_ns: int


@dataclass(frozen=True)
class FreshCaptureProof:
    """Lists the frames and timestamps that justify 'captured after the grant'."""

    grant_ns: int
    host_clock_domain: str
    binding_sha256: str
    timestamp_semantics: str
    cameras: tuple


class FreshCaptureVerifier:
    """Decide whether every bound camera delivered frames captured after a grant."""

    def __init__(self, binding: FreshCaptureBinding):
        if not isinstance(binding, FreshCaptureBinding):
            raise FreshCaptureUnproven("a FreshCaptureBinding must be injected")
        self.binding = binding

    def verify(
        self,
        grant_ns: int,
        host_clock_domain: str,
        frames,
        *,
        previous: dict | None = None,
    ) -> FreshCaptureProof:
        """Return a proof or raise FreshCaptureUnproven.

        frames: every frame collected after the grant, in arrival order per
        camera. previous: optional {camera_id: last frame before the grant}, used
        only as the baseline for number/timestamp/interval/stall checks; it is not
        evidence of freshness.
        """
        b = self.binding
        _need_int(grant_ns, "grant_ns")
        if host_clock_domain != b.host_clock_domain:
            raise FreshCaptureUnproven(
                f"host clock domain mismatch: grant {host_clock_domain!r}, "
                f"binding {b.host_clock_domain!r}"
            )
        by_cam: dict = {}
        for f in frames:
            if not isinstance(f, CapturedFrame):
                raise FreshCaptureUnproven("frames must be CapturedFrame")
            if f.camera_id not in b.camera_serials:
                raise FreshCaptureUnproven(f"unbound camera {f.camera_id!r}")
            by_cam.setdefault(f.camera_id, []).append(f)
        missing = sorted(b.camera_serials - by_cam.keys())
        if missing:
            raise FreshCaptureUnproven(f"missing camera {missing}")
        previous = previous or {}
        for cid in previous:
            if cid not in b.camera_serials:
                raise FreshCaptureUnproven(f"unbound camera {cid!r} in previous")
        cams = []
        for cid in sorted(by_cam):
            cams.append(self._camera(grant_ns, cid, by_cam[cid], previous.get(cid)))
        return FreshCaptureProof(
            grant_ns=grant_ns,
            host_clock_domain=b.host_clock_domain,
            binding_sha256=b.sha256(),
            timestamp_semantics=b.timestamp_semantics,
            cameras=tuple(cams),
        )

    def _fields(self, f: CapturedFrame) -> None:
        b = self.binding
        cid = f.camera_id
        _need_int(f.frame_number, f"{cid} frame number", minimum=0)
        if f.device_timestamp_ns is None:
            raise FreshCaptureUnproven(
                f"{cid} frame {f.frame_number}: missing device timestamp"
            )
        if not _is_int(f.device_timestamp_ns):
            raise FreshCaptureUnproven(
                f"{cid} frame {f.frame_number}: device timestamp must be an integer"
            )
        if f.timestamp_domain != b.timestamp_domain:
            raise FreshCaptureUnproven(
                f"{cid} frame {f.frame_number}: timestamp domain {f.timestamp_domain!r} "
                f"!= bound {b.timestamp_domain!r}"
            )
        if f.host_clock_domain != b.host_clock_domain:
            raise FreshCaptureUnproven(
                f"{cid} frame {f.frame_number}: host clock domain {f.host_clock_domain!r} "
                f"!= bound {b.host_clock_domain!r}"
            )
        _need_int(f.host_arrival_ns, f"{cid} frame {f.frame_number} host_arrival_ns")
        if not isinstance(f.image_sha256, str) or not _SHA256.match(f.image_sha256):
            raise FreshCaptureUnproven(
                f"{cid} frame {f.frame_number}: bad image_sha256"
            )
        if f.image_bytes is not None and (
            hashlib.sha256(f.image_bytes).hexdigest() != f.image_sha256
        ):
            raise FreshCaptureUnproven(
                f"{cid} frame {f.frame_number}: image bytes do not match image_sha256"
            )

    def _sequence(self, prev: CapturedFrame, cur: CapturedFrame) -> None:
        cid = cur.camera_id
        if cur.frame_number <= prev.frame_number:
            raise FreshCaptureUnproven(
                f"{cid}: frame number not increasing ({prev.frame_number} -> {cur.frame_number})"
            )
        if cur.device_timestamp_ns <= prev.device_timestamp_ns:
            raise FreshCaptureUnproven(
                f"{cid}: device timestamp not increasing "
                f"({prev.device_timestamp_ns} -> {cur.device_timestamp_ns})"
            )
        if cur.host_arrival_ns < prev.host_arrival_ns:
            raise FreshCaptureUnproven(f"{cid}: host arrival time regressed")
        gap = cur.device_timestamp_ns - prev.device_timestamp_ns
        if gap > self.binding.max_frame_interval_ns:
            raise FreshCaptureUnproven(
                f"{cid}: frame interval {gap} ns exceeds {self.binding.max_frame_interval_ns}"
            )
        if cur.image_sha256 == prev.image_sha256:
            raise FreshCaptureUnproven(
                f"{cid}: stalled image, frame {cur.frame_number} repeats frame "
                f"{prev.frame_number}"
            )

    def _camera(self, grant_ns, cid, frames, previous) -> CameraFreshProof:
        b = self.binding
        if len(frames) < b.min_frames_per_camera:
            raise FreshCaptureUnproven(
                f"{cid}: need at least {b.min_frames_per_camera} frames, got {len(frames)}"
            )
        if previous is not None:
            if not isinstance(previous, CapturedFrame) or previous.camera_id != cid:
                raise FreshCaptureUnproven(
                    f"{cid}: previous baseline is not this camera's frame"
                )
            self._fields(previous)
        lowers = []
        last = previous
        for f in frames:
            self._fields(f)
            if f.host_arrival_ns < grant_ns:
                raise FreshCaptureUnproven(
                    f"{cid} frame {f.frame_number}: arrived before grant "
                    f"({f.host_arrival_ns} < {grant_ns})"
                )
            if last is not None:
                self._sequence(last, f)
            lower = b.alignment.capture_lower_ns(f.device_timestamp_ns)
            if lower < grant_ns:
                raise FreshCaptureUnproven(
                    f"{cid} frame {f.frame_number}: captured before grant cannot be excluded "
                    f"(earliest capture {lower} < {grant_ns})"
                )
            if lower > f.host_arrival_ns:
                raise FreshCaptureUnproven(
                    f"{cid} frame {f.frame_number}: captured after arrival "
                    f"({lower} > {f.host_arrival_ns}); alignment is wrong"
                )
            latency = f.host_arrival_ns - lower
            if latency > b.max_latency_ns:
                raise FreshCaptureUnproven(
                    f"{cid} frame {f.frame_number}: latency {latency} ns exceeds "
                    f"{b.max_latency_ns}"
                )
            lowers.append(lower)
            last = f
        return CameraFreshProof(
            camera_id=cid,
            timestamp_domain=b.timestamp_domain,
            frame_numbers=tuple(f.frame_number for f in frames),
            device_timestamps_ns=tuple(f.device_timestamp_ns for f in frames),
            arrival_ns=tuple(f.host_arrival_ns for f in frames),
            image_sha256s=tuple(f.image_sha256 for f in frames),
            earliest_capture_lower_ns=min(lowers),
        )


class FakeCamera:
    """Deterministic in-memory camera for tests; it simulates, proves nothing.

    capture(host_capture_ns) returns the frame a device whose clock is
    host - offset_ns would deliver latency_ns later. stall_after=N makes frames
    after the N-th repeat the N-th image.
    """

    def __init__(
        self,
        serial: str,
        host_clock_domain: str,
        timestamp_domain: str,
        offset_ns: int,
        latency_ns: int,
        *,
        stall_after: int | None = None,
    ):
        self.serial = serial
        self.host_clock_domain = host_clock_domain
        self.timestamp_domain = timestamp_domain
        self.offset_ns = offset_ns
        self.latency_ns = latency_ns
        self.stall_after = stall_after
        self.count = 0

    def capture(self, host_capture_ns: int) -> CapturedFrame:
        self.count += 1
        shown = self.count
        if self.stall_after is not None and shown > self.stall_after:
            shown = self.stall_after
        return CapturedFrame.from_image_bytes(
            camera_id=self.serial,
            frame_number=self.count,
            device_timestamp_ns=host_capture_ns - self.offset_ns,
            timestamp_domain=self.timestamp_domain,
            host_clock_domain=self.host_clock_domain,
            host_arrival_ns=host_capture_ns + self.latency_ns,
            image_bytes=f"{self.serial}:image:{shown}".encode(),
        )
