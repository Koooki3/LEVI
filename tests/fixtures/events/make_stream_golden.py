"""Write the stream golden fixture: CausalSignals' updates on fixed input.

    python -B tests/fixtures/events/make_stream_golden.py <out.json>

Run it with the code whose behaviour the fixture pins (the import path
decides which ``levi`` is used; the fixture records ``levi.events.stream``'s
file and its sha256). ``stream-golden.json`` was written from main
f256e131, before ``height_turn_m`` existed, so ``test_events_stream`` can
check that a stream without it still answers exactly as main did.

The input is synthetic and stored in the fixture with the output, so the
test replays it without this script: two grippers (a width open high, a
closure open low), an xyz position, still spans, a skipped step (gap), a
missing and an out-of-range sample, and a sample the stream rejects
(time does not advance).

History: the first fixture written had no motion candidates (the height
never stopped changing), so it did not test the motion path at all and
was discarded. The second -- the one committed -- holds the height still
for part of each cycle so the input has still spans; its output was again
computed by main's code.
"""

import hashlib
import json
import math
import sys
from pathlib import Path

PROFILE = {
    "schema_version": "levi.signal_profile.v1",
    "origin": "declared",
    "channels": [
        *[
            {
                "role": "position",
                "feature": "observation.state",
                "index": i,
                "axis": axis,
                "units": "m",
                "frame": "base",
                "kind": "measured",
            }
            for i, axis in enumerate("xyz")
        ],
        {
            "role": "gripper",
            "feature": "observation.state",
            "index": 6,
            "units": "m",
            "kind": "measured",
            "open_level": "high",
            "valid_range": [0.0, 0.08],
        },
        {
            "role": "gripper",
            "feature": "action",
            "index": 0,
            "units": "normalized",
            "kind": "commanded",
            "open_level": "low",
            "valid_range": [0.0, 1.0],
        },
    ],
}
CONFIG = {
    "max_history": 16,
    "max_age_ns": 500_000_000,
    "still_ns": 300_000_000,
    "motion_speed_m_s": 0.02,
    "max_gap_ns": 1_000_000_000,
}
STEP_NS = 100_000_000


def inputs():
    """``[(step, observed_ns, now_ns, {"feature|index": value})]``."""
    out = []
    step = 0
    for k in range(80):
        if k == 50:
            step += 1  # a skipped step: the stream reports a gap
        x = round(0.3 + 0.01 * min(k % 25, 12), 6)
        z = round(0.25 - 0.006 * min(k % 30, 15), 6)
        width = 0.08 if (k // 15) % 2 == 0 else 0.004
        closure = 0.0 if width > 0.04 else 0.9
        values = {
            "observation.state|0": x,
            "observation.state|1": 0.1,
            "observation.state|2": z,
            "observation.state|6": width,
            "action|0": closure,
        }
        if k == 33:
            values["observation.state|6"] = None  # missing
        if k == 61:
            values["action|0"] = 1.7  # out of range
        ns = (step + 1) * STEP_NS
        out.append((step, ns, ns, values))
        step += 1
    # Time does not advance: rejected, and nothing changes.
    last = out[-1]
    out.append((last[0] + 1, last[1], last[2], dict(last[3])))
    return out


def run(stream_module, profile_module, *, height_turn_m=None):
    profile = profile_module.SignalProfile.model_validate(PROFILE)
    kwargs = {} if height_turn_m is None else {"height_turn_m": height_turn_m}
    kernel = stream_module.CausalSignals(
        profile, config=stream_module.StreamConfig(**CONFIG), **kwargs
    )
    steps = []
    for step, observed, now, values in inputs():
        clean = {}
        for key, value in values.items():
            feature, index = key.split("|")
            clean[(feature, int(index))] = math.nan if value is None else float(value)
        try:
            update = kernel.feed(
                stream_module.Sample(step, observed, clean), now_ns=now
            )
        except ValueError as exc:
            steps.append({"rejected": str(exc)})
            continue
        steps.append(
            {
                "candidates": [c.model_dump(mode="json") for c in update.candidates],
                "window_steps": [list(w) for w in update.window_steps],
                "unknown": list(update.unknown),
                "dropped_history": update.dropped_history,
            }
        )
    return steps


def main(argv):
    from levi.events import signal_profiles, stream

    source = Path(stream.__file__)
    data = {
        "schema": "levi.test.stream_golden.v1",
        "made_with": {
            "file": "/".join(source.parts[-3:]),
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        },
        "profile": PROFILE,
        "config": CONFIG,
        "inputs": [list(x) for x in inputs()],
        "steps": run(stream, signal_profiles),
    }
    Path(argv[1]).write_text(json.dumps(data, indent=1) + "\n")
    count = sum(len(s.get("candidates", ())) for s in data["steps"])
    print(f"{len(data['steps'])} steps, {count} candidates, {source}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
