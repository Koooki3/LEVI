"""Fresh-capture interface, verifier and fake cameras; no hardware, SDK or port."""

import dataclasses
import hashlib
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from levi.automatic.adapters.fresh_capture import (
    CapturedFrame,
    DeviceClockAlignment,
    FakeCamera,
    FreshCaptureBinding,
    FreshCaptureUnproven,
    FreshCaptureVerifier,
)

HOST = "host-mono:fresh-fake"
DEV = "global_time:fake"
MS = 1_000_000
GRANT = 10_000 * MS


def alignment(**kw):
    base = {
        "offset_ns": 5_000 * MS,
        "offset_uncertainty_ns": 2 * MS,
        "drift_ppm_bound": 50.0,
        "reference_device_ns": 5_000 * MS,
    }
    base.update(kw)
    return DeviceClockAlignment(**base)


def binding(**kw):
    base = {
        "camera_serials": frozenset({"CAM-A", "CAM-B"}),
        "host_clock_domain": HOST,
        "timestamp_domain": DEV,
        "timestamp_semantics": "start_of_exposure",
        "max_latency_ns": 60 * MS,
        "max_frame_interval_ns": 100 * MS,
        "min_frames_per_camera": 2,
        "alignment": alignment(),
    }
    base.update(kw)
    return FreshCaptureBinding(**base)


def cams(**kw):
    return {
        s: FakeCamera(
            serial=s,
            host_clock_domain=HOST,
            timestamp_domain=DEV,
            offset_ns=5_000 * MS,
            latency_ns=20 * MS,
            **kw,
        )
        for s in ("CAM-A", "CAM-B")
    }


def stream(cs, start_ns=GRANT + 10 * MS, n=3, step=33 * MS):
    out = {s: [] for s in cs}
    for i in range(n):
        for s, c in cs.items():
            out[s].append(c.capture(start_ns + i * step))
    return out


class FreshCaptureTests(unittest.TestCase):
    def verify(self, frames, b=None, previous=None, grant=GRANT, domain=HOST):
        v = FreshCaptureVerifier(b or binding())
        flat = [f for fs in frames.values() for f in fs]
        return v.verify(grant, domain, flat, previous=previous)

    def reject(self, frames, text, **kw):
        with self.assertRaises(FreshCaptureUnproven) as cm:
            self.verify(frames, **kw)
        self.assertIn(text, str(cm.exception))

    def test_exposure_end_cannot_certify_cross_grant_capture(self):
        # The first exposure spans [GRANT - 10ms, GRANT + 10ms].
        # Reporting its end after the grant cannot prove its start was fresh.
        frames = stream(cams(), start_ns=GRANT + 10 * MS)
        with self.assertRaisesRegex(
            FreshCaptureUnproven, "timestamp_semantics must be start_of_exposure"
        ):
            self.verify(frames, b=binding(timestamp_semantics="end_of_exposure"))

    def test_unsupported_timestamp_semantics_refused_at_binding(self):
        for semantics in ("mid_exposure", "readout", "host_arrival", "unknown"):
            with (
                self.subTest(semantics=semantics),
                self.assertRaisesRegex(
                    FreshCaptureUnproven,
                    "timestamp_semantics must be start_of_exposure",
                ),
            ):
                binding(timestamp_semantics=semantics)

    def test_accepts_fresh_frames_and_lists_numbers_and_timestamps(self):
        frames = stream(cams())
        proof = self.verify(frames)
        self.assertEqual(proof.grant_ns, GRANT)
        self.assertEqual(proof.binding_sha256, binding().sha256())
        self.assertEqual({c.camera_id for c in proof.cameras}, {"CAM-A", "CAM-B"})
        for c in proof.cameras:
            self.assertEqual(c.frame_numbers, (1, 2, 3))
            self.assertEqual(
                c.device_timestamps_ns,
                tuple(f.device_timestamp_ns for f in frames[c.camera_id]),
            )
            self.assertGreaterEqual(c.earliest_capture_lower_ns, GRANT)

    def test_stale_frame_arrived_before_grant(self):
        frames = stream(cams(), start_ns=GRANT - 200 * MS)
        self.reject(frames, "before grant")

    def test_stale_frame_arrives_after_grant_but_captured_before(self):
        # arrival is after the grant, yet the device time says it was captured before
        cs = cams()
        frames = stream(cs, start_ns=GRANT - 30 * MS)
        frames = {
            s: [
                replace(f, host_arrival_ns=GRANT + 5 * MS + i * 33 * MS)
                for i, f in enumerate(fs)
            ]
            for s, fs in frames.items()
        }
        self.reject(frames, "captured before grant")

    def test_uncertainty_makes_borderline_capture_unproven(self):
        # captured 1 ms after grant but alignment uncertainty is 2 ms
        frames = stream(cams(), start_ns=GRANT + 1 * MS)
        self.reject(frames, "captured before grant")

    def test_duplicate_frame_number(self):
        frames = stream(cams())
        fs = frames["CAM-A"]
        frames["CAM-A"][1] = replace(fs[1], frame_number=fs[0].frame_number)
        self.reject(frames, "frame number")

    def test_regressed_frame_number(self):
        frames = stream(cams())
        fs = frames["CAM-B"]
        frames["CAM-B"][2] = replace(fs[2], frame_number=0)
        self.reject(frames, "frame number")

    def test_regressed_device_timestamp(self):
        frames = stream(cams())
        fs = frames["CAM-A"]
        frames["CAM-A"][2] = replace(
            fs[2], device_timestamp_ns=fs[0].device_timestamp_ns
        )
        self.reject(frames, "device timestamp")

    def test_missing_timestamp(self):
        frames = stream(cams())
        frames["CAM-A"][0] = replace(frames["CAM-A"][0], device_timestamp_ns=None)
        self.reject(frames, "missing device timestamp")

    def test_bool_and_non_int_timestamp(self):
        for bad in (True, 1.5, "7"):
            frames = stream(cams())
            frames["CAM-A"][0] = replace(frames["CAM-A"][0], device_timestamp_ns=bad)
            self.reject(frames, "device timestamp")

    def test_missing_camera(self):
        frames = stream(cams())
        del frames["CAM-B"]
        self.reject(frames, "missing camera")

    def test_unexpected_camera(self):
        cs = cams()
        cs["CAM-C"] = FakeCamera("CAM-C", HOST, DEV, 5_000 * MS, 20 * MS)
        self.reject(stream(cs), "unbound camera")

    def test_too_few_frames(self):
        frames = stream(cams(), n=1)
        self.reject(frames, "at least 2 frames")

    def test_timestamp_domain_mismatch(self):
        frames = stream(cams())
        frames["CAM-A"][1] = replace(frames["CAM-A"][1], timestamp_domain="system_time")
        self.reject(frames, "timestamp domain")

    def test_missing_timestamp_domain(self):
        frames = stream(cams())
        frames["CAM-A"][1] = replace(frames["CAM-A"][1], timestamp_domain=None)
        self.reject(frames, "timestamp domain")

    def test_host_clock_domain_mismatch_on_frame(self):
        frames = stream(cams())
        frames["CAM-A"][0] = replace(
            frames["CAM-A"][0], host_clock_domain="host-mono:other"
        )
        self.reject(frames, "host clock domain")

    def test_host_clock_domain_mismatch_on_grant(self):
        self.reject(stream(cams()), "host clock domain", domain="host-mono:other")

    def test_interval_too_large(self):
        frames = stream(cams(), step=150 * MS)
        self.reject(frames, "frame interval")

    def test_interval_checked_against_previous_baseline(self):
        cs = cams()
        prev = {s: c.capture(GRANT - 500 * MS) for s, c in cs.items()}
        frames = stream(cs)
        self.reject(frames, "frame interval", previous=prev)

    def test_previous_must_not_regress(self):
        cs = cams()
        frames = stream(cs)
        prev = {s: replace(fs[0], frame_number=99) for s, fs in frames.items()}
        self.reject(frames, "frame number", previous=prev)

    def test_stalled_image(self):
        cs = cams(stall_after=1)
        self.reject(stream(cs), "stalled")

    def test_stalled_against_previous_baseline(self):
        cs = cams()
        prev = {s: c.capture(GRANT - 5 * MS) for s, c in cs.items()}
        frames = stream(cs)
        frames["CAM-A"][0] = replace(
            frames["CAM-A"][0],
            image_sha256=prev["CAM-A"].image_sha256,
            image_bytes=None,
        )
        self.reject(
            frames,
            "stalled",
            previous=prev,
            b=binding(max_frame_interval_ns=2_000 * MS),
        )

    def test_latency_exceeded(self):
        cs = {
            s: FakeCamera(s, HOST, DEV, 5_000 * MS, 200 * MS)
            for s in ("CAM-A", "CAM-B")
        }
        self.reject(stream(cs), "latency")

    def test_capture_after_arrival(self):
        cs = {
            s: FakeCamera(s, HOST, DEV, 5_000 * MS, -50 * MS)
            for s in ("CAM-A", "CAM-B")
        }
        self.reject(stream(cs, start_ns=GRANT + 100 * MS), "after arrival")

    def test_drift_bound_widens_uncertainty(self):
        # far from the reference instant the drift bound can exceed the margin
        b = binding(
            alignment=alignment(drift_ppm_bound=50_000.0, reference_device_ns=0)
        )
        self.reject(stream(cams()), "captured before grant", b=b)

    def test_invalid_hash(self):
        frames = stream(cams())
        frames["CAM-A"][0] = replace(frames["CAM-A"][0], image_sha256="abc")
        self.reject(frames, "image_sha256")

    def test_image_bytes_must_match_hash(self):
        frames = stream(cams())
        frames["CAM-A"][0] = replace(frames["CAM-A"][0], image_bytes=b"tampered")
        self.reject(frames, "image bytes")

    def test_from_image_bytes_hashes(self):
        f = CapturedFrame.from_image_bytes(
            camera_id="CAM-A",
            frame_number=1,
            device_timestamp_ns=1,
            timestamp_domain=DEV,
            host_clock_domain=HOST,
            host_arrival_ns=2,
            image_bytes=b"xyz",
        )
        self.assertEqual(f.image_sha256, hashlib.sha256(b"xyz").hexdigest())

    def test_binding_rejects_missing_fields(self):
        base = {
            "camera_serials": frozenset({"A"}),
            "host_clock_domain": HOST,
            "timestamp_domain": DEV,
            "timestamp_semantics": "start_of_exposure",
            "max_latency_ns": MS,
            "max_frame_interval_ns": MS,
            "min_frames_per_camera": 1,
            "alignment": alignment(),
        }
        FreshCaptureBinding(**base)  # control
        for k in base:
            with self.assertRaises(TypeError, msg=k):
                FreshCaptureBinding(**{a: b for a, b in base.items() if a != k})

    def test_binding_rejects_undeclared_alignment(self):
        for bad in (None, {"offset_ns": 0}):
            with self.assertRaises(FreshCaptureUnproven):
                binding(alignment=bad)

    def test_binding_rejects_bad_values(self):
        for kw in (
            {"camera_serials": frozenset()},
            {"camera_serials": frozenset({""})},
            {"camera_serials": ["CAM-A"]},
            {"host_clock_domain": ""},
            {"timestamp_domain": None},
            {"timestamp_semantics": ""},
            {"max_latency_ns": 0},
            {"max_latency_ns": True},
            {"max_frame_interval_ns": -1},
            {"min_frames_per_camera": 0},
        ):
            with self.assertRaises(FreshCaptureUnproven, msg=kw):
                binding(**kw)

    def test_alignment_rejects_undeclared_bounds(self):
        for kw in (
            {"offset_ns": None},
            {"offset_uncertainty_ns": None},
            {"offset_uncertainty_ns": -1},
            {"drift_ppm_bound": None},
            {"drift_ppm_bound": -1.0},
            {"drift_ppm_bound": float("nan")},
            {"reference_device_ns": None},
        ):
            with self.assertRaises(FreshCaptureUnproven, msg=kw):
                alignment(**kw)

    def test_verifier_requires_binding_instance(self):
        with self.assertRaises(FreshCaptureUnproven):
            FreshCaptureVerifier(None)
        with self.assertRaises(FreshCaptureUnproven):
            FreshCaptureVerifier({"camera_serials": ["A"]})

    def test_bad_grant(self):
        for g in (None, True, 1.0):
            with self.assertRaises(FreshCaptureUnproven):
                self.verify(stream(cams()), grant=g)

    def test_binding_hash_changes_with_alignment(self):
        self.assertNotEqual(
            binding().sha256(), binding(alignment=alignment(offset_ns=1)).sha256()
        )

    def test_proof_is_frozen(self):
        proof = self.verify(stream(cams()))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            proof.grant_ns = 0


if __name__ == "__main__":
    unittest.main()
